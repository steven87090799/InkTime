"""Passphrase-protected recovery bundles, deliberately separate from metadata backups."""

from __future__ import annotations

import base64
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import secrets

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
from cryptography.fernet import Fernet, InvalidToken

from inktime import __version__
from inktime.app.db import Database


class RecoveryBundleError(ValueError):
    pass


class RecoveryBundleService:
    VERSION = 1

    def __init__(self, database: Database, data_dir: Path) -> None:
        self.database, self.data_dir = database, data_dir.resolve()
        self.session_path = self.data_dir / "session.key"

    @staticmethod
    def _key(passphrase: str, salt: bytes) -> bytes:
        if len(passphrase) < 12:
            raise RecoveryBundleError("RECOVERY-001 Recovery Passphrase 至少需要 12 個字元")
        return Scrypt(salt=salt, length=32, n=2**15, r=8, p=1).derive(passphrase.encode("utf-8"))

    def create(self, destination: Path, passphrase: str) -> dict[str, object]:
        if self.session_path.is_symlink() or not self.session_path.is_file():
            raise RecoveryBundleError("RECOVERY-002 找不到安全的 session.key")
        session_key = self.session_path.read_text(encoding="utf-8").strip()
        with self.database.session() as connection:
            self._assert_database_key(connection, session_key)
            rows = connection.execute(
                "SELECT key,encrypted_value,updated_by,updated_at FROM secrets ORDER BY key"
            ).fetchall()
            schema = self.database.schema_version()
        plaintext = json.dumps(
            {
                "session_key": session_key,
                "secrets": [
                    {
                        "key": str(row["key"]),
                        "encrypted_value": base64.b64encode(bytes(row["encrypted_value"])).decode("ascii"),
                        "updated_by": row["updated_by"],
                        "updated_at": row["updated_at"],
                    }
                    for row in rows
                ],
            },
            separators=(",", ":"),
        ).encode("utf-8")
        self._validate_payload(json.loads(plaintext))
        salt, nonce = secrets.token_bytes(16), secrets.token_bytes(12)
        encrypted = AESGCM(self._key(passphrase, salt)).encrypt(
            nonce, plaintext, b"InkTime Recovery Bundle v1"
        )
        envelope = {
            "bundle_version": self.VERSION,
            "schema_version": schema,
            "application_version": __version__,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "kdf": {
                "name": "scrypt",
                "n": 32768,
                "r": 8,
                "p": 1,
                "salt": base64.b64encode(salt).decode("ascii"),
            },
            "nonce": base64.b64encode(nonce).decode("ascii"),
            "ciphertext": base64.b64encode(encrypted).decode("ascii"),
        }
        envelope["checksum"] = sha256(
            json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        target = destination.resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{secrets.token_hex(8)}.tmp")
        try:
            with temporary.open("x", encoding="utf-8") as handle:
                json.dump(envelope, handle, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, target)
        finally:
            if temporary.exists():
                temporary.unlink()
        return {"path": target.name, "schema_version": schema, "secrets": len(rows)}

    def _decrypt(self, bundle: Path, passphrase: str) -> tuple[dict, dict]:
        try:
            envelope = json.loads(bundle.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RecoveryBundleError("RECOVERY-003 Bundle 格式無效") from exc
        checksum = envelope.pop("checksum", "")
        calculated = sha256(json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        envelope["checksum"] = checksum
        if not secrets.compare_digest(str(checksum), calculated):
            raise RecoveryBundleError("RECOVERY-004 Bundle Checksum 不符")
        try:
            salt = base64.b64decode(envelope["kdf"]["salt"])
            nonce = base64.b64decode(envelope["nonce"])
            ciphertext = base64.b64decode(envelope["ciphertext"])
            plain = AESGCM(self._key(passphrase, salt)).decrypt(
                nonce, ciphertext, b"InkTime Recovery Bundle v1"
            )
            return envelope, json.loads(plain)
        except Exception as exc:
            raise RecoveryBundleError("RECOVERY-005 Passphrase 錯誤或 Bundle 已損毀") from exc

    def verify(self, bundle: Path, passphrase: str) -> dict[str, object]:
        envelope, payload = self._decrypt(bundle.resolve(), passphrase)
        self._validate_payload(payload)
        return {
            "bundle_version": envelope["bundle_version"],
            "schema_version": envelope["schema_version"],
            "secrets": len(payload["secrets"]),
        }

    def restore(self, bundle: Path, passphrase: str) -> dict[str, object]:
        envelope, payload = self._decrypt(bundle.resolve(), passphrase)  # validation happens before writes
        self._validate_payload(payload)
        lock = self.database.acquire_runtime_lock(exclusive=True, blocking=False)
        previous_key = None
        key_replaced = False
        try:
            with self.database.session() as connection:
                self._assert_database_key(connection, payload["session_key"])
            previous_key = self.session_path.read_bytes() if self.session_path.exists() else None
            safe_copy = (
                self.data_dir
                / "backups"
                / f"session-key-before-recovery-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
            )
            safe_copy.parent.mkdir(parents=True, exist_ok=True)
            if previous_key is not None:
                safe_copy.write_bytes(previous_key)
                os.chmod(safe_copy, 0o600)
            with self.database.transaction(operation="secret_recovery_restore") as connection:
                self._assert_database_key(connection, payload["session_key"])
                connection.execute("DELETE FROM secrets")
                connection.executemany(
                    "INSERT INTO secrets(key,encrypted_value,updated_by,updated_at) VALUES (?,?,?,?)",
                    [
                        (
                            item["key"],
                            base64.b64decode(item["encrypted_value"]),
                            item.get("updated_by"),
                            item["updated_at"],
                        )
                        for item in payload["secrets"]
                    ],
                )
                temporary = self.session_path.with_suffix(".key.recovery.tmp")
                temporary.write_text(str(payload["session_key"]), encoding="utf-8")
                os.chmod(temporary, 0o600)
                os.replace(temporary, self.session_path)
                key_replaced = True
            return {"schema_version": envelope["schema_version"], "secrets": len(payload["secrets"])}
        except Exception:
            if key_replaced and previous_key is not None:
                self.session_path.write_bytes(previous_key)
                os.chmod(self.session_path, 0o600)
            elif key_replaced:
                self.session_path.unlink(missing_ok=True)
            raise
        finally:
            lock.close()

    @staticmethod
    def _validate_payload(payload: dict) -> None:
        if (not isinstance(payload, dict) or not isinstance(payload.get("session_key"), str)
                or not payload["session_key"].strip() or not isinstance(payload.get("secrets"), list)):
            raise RecoveryBundleError("RECOVERY-006 Bundle 內容不完整")
        cipher = Fernet(base64.urlsafe_b64encode(sha256(payload["session_key"].encode("utf-8")).digest()))
        names: set[str] = set()
        try:
            for item in payload["secrets"]:
                if (not isinstance(item, dict) or not isinstance(item.get("key"), str)
                        or not item["key"] or item["key"] in names
                        or not isinstance(item.get("updated_at"), str)):
                    raise ValueError("invalid secret entry")
                names.add(item["key"])
                ciphertext = base64.b64decode(item["encrypted_value"], validate=True)
                cipher.decrypt(ciphertext).decode("utf-8")
        except (KeyError, TypeError, ValueError, InvalidToken) as exc:
            raise RecoveryBundleError("RECOVERY-007 Bundle 密鑰與加密設定不配對或內容無效") from exc

    @staticmethod
    def _assert_database_key(connection, session_key: str) -> None:
        fingerprint = sha256(b"inktime-master-secret-fingerprint\x00" + session_key.encode("utf-8")).hexdigest()
        identity = connection.execute(
            "SELECT value FROM runtime_identity WHERE key='master_secret_fingerprint'"
        ).fetchone()
        if identity and not secrets.compare_digest(str(identity[0]), fingerprint):
            raise RecoveryBundleError("RECOVERY-008 密鑰與目標資料庫身分不符，未還原")
        cipher = Fernet(base64.urlsafe_b64encode(sha256(session_key.encode("utf-8")).digest()))
        try:
            for row in connection.execute("SELECT encrypted_value FROM secrets"):
                cipher.decrypt(bytes(row[0])).decode("utf-8")
        except (InvalidToken, UnicodeDecodeError, ValueError, TypeError) as exc:
            raise RecoveryBundleError("RECOVERY-008 密鑰無法解密資料庫設定，未還原") from exc
