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
        with self.database.session() as connection:
            rows = connection.execute(
                "SELECT key,encrypted_value,updated_by,updated_at FROM secrets ORDER BY key"
            ).fetchall()
            schema = self.database.schema_version()
        plaintext = json.dumps(
            {
                "session_key": self.session_path.read_text(encoding="utf-8").strip(),
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
        if not payload.get("session_key") or not isinstance(payload.get("secrets"), list):
            raise RecoveryBundleError("RECOVERY-006 Bundle 內容不完整")
        return {
            "bundle_version": envelope["bundle_version"],
            "schema_version": envelope["schema_version"],
            "secrets": len(payload["secrets"]),
        }

    def restore(self, bundle: Path, passphrase: str) -> dict[str, object]:
        envelope, payload = self._decrypt(bundle.resolve(), passphrase)  # validation happens before writes
        lock = self.database.acquire_runtime_lock(exclusive=True, blocking=False)
        previous_key = self.session_path.read_bytes() if self.session_path.exists() else None
        try:
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
            return {"schema_version": envelope["schema_version"], "secrets": len(payload["secrets"])}
        except Exception:
            if previous_key is not None:
                self.session_path.write_bytes(previous_key)
                os.chmod(self.session_path, 0o600)
            raise
        finally:
            lock.close()
