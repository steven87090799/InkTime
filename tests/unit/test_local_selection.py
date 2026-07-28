from __future__ import annotations

from datetime import date
from pathlib import Path

from PIL import Image

from inktime.app.services.local_selection import LocalSelectionPolicy


def _candidate(app, root: Path, photo_id: str, score: float, month_day: str):
    Image.new("RGB", (900, 1600), "#4477aa").save(root / f"{photo_id}.jpg")
    photos = app.extensions["inktime_photo_repository"]
    library_id = photos.ensure_library("本機選片", root)
    with app.extensions["inktime_database"].session() as connection:
        connection.execute(
            """INSERT INTO photos(id,library_id,relative_path,width,height,status,eligible,lifecycle_status,exclusion_status,
                local_features_status,local_candidate_score,captured_at,captured_date,captured_month_day,created_at,updated_at)
                VALUES (?,?,?,?,?,'analyzed',1,'active','eligible','complete',?,?,?,?,?,?)""",
            (photo_id, library_id, f"{photo_id}.jpg", 900, 1600, score, f"2020-{month_day}T10:00:00+00:00", f"2020-{month_day}", month_day, "2020-01-01", "2020-01-01"),
        )


def test_local_policy_is_bounded_deterministic_and_pairs_different_photos(app, tmp_path):
    root = tmp_path / "photos"
    root.mkdir()
    _candidate(app, root, "a", 90, "07-28")
    _candidate(app, root, "b", 80, "07-28")
    _candidate(app, root, "c", 99, "01-01")
    policy = LocalSelectionPolicy(app.extensions["inktime_database"], app.extensions["inktime_settings_repository"], app.extensions["inktime_resilience_repository"])
    result = policy.select(target=date(2026, 7, 28), orientation="portrait", quantity=2, layout="photo_pair")
    assert [row["id"] for row in result["selected"][:2]] == ["a", "b"]
    assert result["selected"][0]["id"] != result["selected"][1]["id"]
    assert "base_local_score" in result["selected"][0]["score_components"]


def test_disabled_library_never_enters_local_candidates_or_trace(app, tmp_path):
    root = tmp_path / "disabled-library"
    root.mkdir()
    _candidate(app, root, "disabled", 99, "07-28")
    with app.extensions["inktime_database"].session() as connection:
        library_id = connection.execute("SELECT library_id FROM photos WHERE id='disabled'").fetchone()[0]
        connection.execute("UPDATE libraries SET enabled=0 WHERE id=?", (library_id,))
    policy = LocalSelectionPolicy(
        app.extensions["inktime_database"], app.extensions["inktime_settings_repository"],
        app.extensions["inktime_resilience_repository"],
    )
    result = policy.select(target=date(2026, 7, 28), orientation="portrait", quantity=2, layout="photo_pair")
    assert result["candidates"] == []
    assert result["selected"] == []
    with app.extensions["inktime_database"].session() as connection:
        connection.execute("UPDATE libraries SET enabled=1 WHERE id=?", (library_id,))
    assert policy.ranked(target=date(2026, 7, 28), orientation="portrait")[0]["id"] == "disabled"
