from __future__ import annotations

from PIL import Image

from tests.conftest import create_admin, csrf, login


def _candidate(app, root, photo_id: str, *, eligible: int = 1, lifecycle: str = "active"):
    Image.new("RGB", (640, 480), "white").save(root / f"{photo_id}.jpg")
    photos = app.extensions["inktime_photo_repository"]
    library = photos.ensure_library("候選契約", root)
    now = "2026-07-22T00:00:00+00:00"
    with app.extensions["inktime_database"].session() as connection:
        connection.execute(
            """
            INSERT INTO photos(id,library_id,relative_path,status,eligible,lifecycle_status,
                               captured_at,created_at,updated_at)
            VALUES (?,?,?,'analyzed',?,?, '2020-07-22T00:00:00',?,?)
            """,
            (photo_id, library, f"{photo_id}.jpg", eligible, lifecycle, now, now),
        )
    photos.save_analysis(
        photo_id, None, "local", "local", "test",
        {"schema_version": 1, "caption": "測試", "types": ["其他"], "memory_score": 99,
         "beauty_score": 99, "technical_quality_score": 99, "emotion_score": 99,
         "side_caption": "", "should_keep": True, "sensitive": False, "reason": "測試"}, "{}",
        ranking_score=99, final_ranking_score=99,
    )


def test_manual_excluded_photo_returns_stable_error_without_fallback(client, app, tmp_path):
    root = tmp_path / "photos"
    root.mkdir()
    _candidate(app, root, "excluded-high", eligible=0)
    _candidate(app, root, "eligible-lower")
    create_admin(app)
    login(client)
    response = client.post(
        "/api/v1/releases",
        json={"photo_ids": ["excluded-high"]},
        headers={"X-CSRF-Token": csrf(client)},
    )
    assert response.status_code == 409
    assert response.get_json()["error_code"] == "RENDER-009"


def test_missing_or_removed_file_is_never_a_candidate(app, tmp_path):
    root = tmp_path / "photos"
    root.mkdir()
    _candidate(app, root, "removed")
    (root / "removed.jpg").unlink()
    assert app.extensions["inktime_render_candidate_repository"].get("removed") is None
