from __future__ import annotations

from PIL import Image


def test_schedule_resolves_devices_limits_years_and_commits_history_after_publish(app, tmp_path):
    root = tmp_path / "scheduled"
    root.mkdir()
    photos = app.extensions["inktime_photo_repository"]
    library_id = photos.ensure_library("排程照片", root)
    now = "2026-07-22T00:00:00+00:00"
    for index, year in enumerate((2020, 2020, 2010), start=1):
        photo_id = f"scheduled-{index}"
        filename = f"{photo_id}.jpg"
        Image.new("RGB", (480, 800), (index * 40, 80, 120)).save(root / filename)
        with app.extensions["inktime_database"].session() as connection:
            connection.execute(
                """
                INSERT INTO photos(
                    id,library_id,relative_path,status,captured_at,eligible,lifecycle_status,
                    local_candidate_score,created_at,updated_at
                ) VALUES (?,?,?,'analyzed',?,1,'active',?,?,?)
                """,
                (photo_id, library_id, filename, f"{year}-07-22T10:00:00", 90-index, now, now),
            )
        photos.save_analysis(
            photo_id,
            None,
            "local",
            "local",
            "scheduled-test",
            {
                "schema_version": 1,
                "caption": "排程測試",
                "types": ["日常"],
                "memory_score": 90-index,
                "beauty_score": 80,
                "technical_quality_score": 80,
                "emotion_score": 80,
                "side_caption": "",
                "should_keep": True,
                "sensitive": False,
                "reason": "測試",
            },
            "{}",
            ranking_score=90-index,
            final_ranking_score=90-index,
        )

    devices = app.extensions["inktime_device_repository"]
    safe_device, _ = devices.create("四色", panel_profile="safe_4c")
    six_device, _ = devices.create("六色", panel_profile="gdep073e01_6c")
    result = app.extensions["inktime_display_preparation_service"].prepare(
        {
            "display_times": ["08:00"],
            "lead_minutes": 30,
            "daily_count": 1,
            "device_ids": [safe_device, six_device],
            "candidate_years": [2020],
            "prefetch_count": 2,
            "ai_fallback": "use_existing",
            "render_fallback": "keep_current",
        },
        created_by="scheduled-test",
    )

    assert result["output_count"] == 2
    assert set(result["photo_ids"]) == {"scheduled-1", "scheduled-2"}
    with app.extensions["inktime_database"].session() as connection:
        releases = connection.execute(
            "SELECT id,render_profile,status FROM releases ORDER BY render_profile"
        ).fetchall()
        history = connection.execute(
            "SELECT photo_id,release_id FROM display_history"
        ).fetchall()
    assert {row["render_profile"] for row in releases} == {"safe_4c", "gdep073e01_6c"}
    assert {row["status"] for row in releases} == {"published"}
    assert {row["release_id"] for row in history} == {row["id"] for row in releases}
    assert len(history) == 4
