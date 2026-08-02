from __future__ import annotations

from tests.conftest import create_admin, csrf, login
from tests.integration.test_jobs import add_photos


def test_review_workbench_is_bounded_and_uses_optimistic_versions(client, app):
    create_admin(app)
    login(client)
    first, second = add_photos(app, 2)

    page = client.get("/review/photos")
    listing = client.get("/api/v1/review/photos?limit=1")
    summary = client.get("/api/v1/review/summary")

    assert page.status_code == 200
    assert "Review 工作台" in page.get_data(as_text=True)
    assert listing.status_code == 200
    assert listing.json["has_more"] is True
    assert len(listing.json["items"]) == 1
    assert summary.json["total"] == 2
    assert summary.json["states"]["unreviewed"] == 2
    assert listing.json["items"][0]["id"] in {first, second}
    assert "raw_json" not in listing.json["items"][0]

    item = client.get(f"/api/v1/review/photos/{first}").json
    response = client.patch(
        f"/api/v1/review/photos/{first}",
        json={"expected_version": item["review_version"], "review_state": "keep", "candidate_pool": True},
        headers={"X-CSRF-Token": csrf(client)},
    )
    assert response.status_code == 200
    assert response.json["photo"]["review_state"] == "keep"
    assert response.json["photo"]["candidate_pool"] is True

    conflict = client.patch(
        f"/api/v1/review/photos/{first}",
        json={"expected_version": item["review_version"], "review_state": "exclude"},
        headers={"X-CSRF-Token": csrf(client)},
    )
    assert conflict.status_code == 409
    assert conflict.json["error_code"] == "REVIEW-409"
    with app.extensions["inktime_database"].session() as connection:
        event_count = connection.execute(
            "SELECT COUNT(*) FROM photo_review_events WHERE photo_id=?", (first,)
        ).fetchone()[0]
        eligibility = connection.execute("SELECT eligible FROM photos WHERE id=?", (first,)).fetchone()[0]
    assert event_count == 1
    assert eligibility == 1
    assert client.get(f"/api/v1/review/photos/{second}").status_code == 200


def test_review_update_binds_to_latest_analysis_after_analysis_is_added(client, app):
    create_admin(app)
    login(client)
    photo_id = add_photos(app, 1)[0]
    now = "2026-08-02T00:00:00+00:00"
    with app.extensions["inktime_database"].session() as connection:
        connection.execute(
            """
            INSERT INTO photo_analysis(
                photo_id,schema_version,stage,provider,model,caption,types_json,
                memory_score,beauty_score,technical_quality_score,emotion_score,
                side_caption,should_keep,sensitive,reason,raw_json,created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                photo_id,
                3,
                "single",
                "test-provider",
                "test-model",
                "測試照片",
                "[]",
                80,
                70,
                75,
                65,
                "測試短文案",
                1,
                0,
                "test",
                "{}",
                now,
            ),
        )
        analysis_id = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])

    item = client.get(f"/api/v1/review/photos/{photo_id}").json
    assert item["analysis_id"] == analysis_id
    assert item["review_version"] == 0
    response = client.patch(
        f"/api/v1/review/photos/{photo_id}",
        json={"expected_version": 0, "analysis_id": analysis_id, "review_state": "keep"},
        headers={"X-CSRF-Token": csrf(client)},
    )
    assert response.status_code == 200
    with app.extensions["inktime_database"].session() as connection:
        review = connection.execute(
            "SELECT analysis_id,version FROM photo_reviews WHERE photo_id=?", (photo_id,)
        ).fetchone()
        event = connection.execute(
            "SELECT analysis_id FROM photo_review_events WHERE photo_id=?", (photo_id,)
        ).fetchone()
    assert int(review["analysis_id"]) == analysis_id
    assert int(review["version"]) == 1
    assert int(event["analysis_id"]) == analysis_id
