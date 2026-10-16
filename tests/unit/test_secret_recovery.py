from __future__ import annotations

from hashlib import sha256

import pytest

from inktime.app.db import Database
from inktime.app.db.migrations import migrate
from inktime.app.repositories.settings import SecretStore
from inktime.app.services.secret_recovery import RecoveryBundleError, RecoveryBundleService


def _service(tmp_path, name, key):
    directory = tmp_path / name
    directory.mkdir()
    database = Database(directory / "test.db")
    migrate(database)
    (directory / "session.key").write_text(key, encoding="utf-8")
    SecretStore(database, key).set("fixture.secret", "dummy-value", "test")
    fingerprint = sha256(b"inktime-master-secret-fingerprint\x00" + key.encode()).hexdigest()
    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO runtime_identity(key,value,updated_at) VALUES ('master_secret_fingerprint',?,?)",
            (fingerprint, "2026-10-13"),
        )
    return RecoveryBundleService(database, directory)


def test_recovery_matching_pair_roundtrips_and_rejects_wrong_source_key(tmp_path):
    service = _service(tmp_path, "source", "dummy-key-a")
    bundle = tmp_path / "bundle.json"
    service.create(bundle, "dummy-passphrase-long")
    assert service.verify(bundle, "dummy-passphrase-long")["secrets"] == 1
    service.restore(bundle, "dummy-passphrase-long")
    assert SecretStore(service.database, "dummy-key-a").get("fixture.secret") == "dummy-value"
    service.session_path.write_text("dummy-key-b", encoding="utf-8")
    rejected = tmp_path / "must-not-exist.json"
    with pytest.raises(RecoveryBundleError, match="RECOVERY-008"):
        service.create(rejected, "dummy-passphrase-long")
    assert not rejected.exists()


def test_recovery_wrong_target_identity_preserves_database_and_key(tmp_path):
    source = _service(tmp_path, "source", "dummy-key-a")
    target = _service(tmp_path, "target", "dummy-key-b")
    bundle = tmp_path / "bundle.json"
    source.create(bundle, "dummy-passphrase-long")
    with pytest.raises(RecoveryBundleError, match="RECOVERY-008"):
        target.restore(bundle, "dummy-passphrase-long")
    assert target.session_path.read_text(encoding="utf-8") == "dummy-key-b"
    assert SecretStore(target.database, "dummy-key-b").get("fixture.secret") == "dummy-value"
    assert not (target.data_dir / "backups").exists()


def test_verify_checks_inner_ciphertext_and_duplicate_secret_names(tmp_path, monkeypatch):
    import base64
    service = _service(tmp_path, "source", "dummy-key-a")
    cipher = SecretStore(service.database, "dummy-key-b").cipher
    payload = {"session_key": "dummy-key-a", "secrets": [{
        "key": "fixture.secret", "encrypted_value": base64.b64encode(cipher.encrypt(b"dummy")).decode(),
        "updated_at": "2026-10-13",
    }]}
    monkeypatch.setattr(service, "_decrypt", lambda *args: ({"bundle_version": 1, "schema_version": 64}, payload))
    with pytest.raises(RecoveryBundleError, match="RECOVERY-007"):
        service.verify(tmp_path / "fixture", "dummy-passphrase-long")
    payload["session_key"] = "dummy-key-b"
    payload["secrets"].append(payload["secrets"][0].copy())
    with pytest.raises(RecoveryBundleError, match="RECOVERY-007"):
        service.verify(tmp_path / "fixture", "dummy-passphrase-long")
