from inktime.app.domain.photos.quality_policy import FEATURE_VERSION
from inktime.app.repositories.photos import StoredPhotoSignature
from inktime.app.workers.scanner import PhotoScanner
from tests.performance.test_scanner_scale import seed


def test_current_complete_features_skip_preprocessor_and_legacy_features_are_upgraded():
    values = dict(id="test", relative_path="test.jpg", file_size=123, modified_time=456.0, sha256="a" * 64,
                  lifecycle_status="active", metadata_status="complete", local_features_status="complete", status="preprocessed")
    current = StoredPhotoSignature(**values, feature_version=FEATURE_VERSION)
    legacy = StoredPhotoSignature(**values, feature_version="local-quality-v3")
    assert PhotoScanner._sections("incremental", current, signature_changed=False) == (False, False)
    assert PhotoScanner._sections("incremental", legacy, signature_changed=False) == (False, True)


def test_scale_fixture_seeds_current_complete_feature_contract(app, tmp_path):
    repository = app.extensions["inktime_photo_repository"]
    library = repository.ensure_library("feature-contract", tmp_path)
    seed(repository.database, library, 3)
    with repository.database.session() as connection:
        rows = connection.execute("SELECT feature_version,metadata_status,local_features_status FROM photos WHERE library_id=?", (library,)).fetchall()
    assert len(rows) == 3
    assert {tuple(row) for row in rows} == {(FEATURE_VERSION, "complete", "complete")}
