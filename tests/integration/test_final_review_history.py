from __future__ import annotations

from pathlib import Path
import sqlite3
import time

import pytest

from tests.unit.test_analysis_schema import valid_result


class FirstChoice:
    def choice(self, values):
        return values[0]

    def choices(self, values, *, weights, k):
        return [values[0]] * k


def _insert_photo(
    app, root: Path, photo_id: str, captured_at: str, *, eligible: int = 1, score: float = 80
) -> None:
    photos = app.extensions["inktime_photo_repository"]
    library_id = photos.ensure_library("歷史選片", root)
    now = "2026-07-22T00:00:00+00:00"
    with app.extensions["inktime_database"].session() as connection:
        connection.execute(
            """
            INSERT INTO photos(id,library_id,relative_path,status,captured_at,captured_date,
                captured_month_day,capture_date_status,eligible,lifecycle_status,
                local_candidate_score,created_at,updated_at)
            VALUES (?,?,?,'analyzed',?,?,?,'valid',?, 'active',?,?,?)
            """,
            (
                photo_id,
                library_id,
                f"{photo_id}.jpg",
                captured_at,
                captured_at[:10],
                captured_at[5:10],
                eligible,
                score,
                now,
                now,
            ),
        )
    photos.save_analysis(
        photo_id,
        None,
        "local",
        "local",
        "local-quality-v3",
        valid_result(
            caption="這是一段歷史選片測試說明。",
            types=["旅行", "人物"],
            memory_score=score,
            visual_score=score,
            side_caption="歷史選片測試短句。",
        ),
        "{}",
        ranking_score=score,
        final_ranking_score=score,
    )


def test_history_selection_filters_unavailable_excluded_and_same_day_reroll(app, tmp_path):
    root = tmp_path / "photos"
    root.mkdir()
    for photo_id in ("good-a", "good-b", "excluded"):
        (root / f"{photo_id}.jpg").write_bytes(b"not-decoded")
    _insert_photo(app, root, "good-a", "2019-07-22T10:00:00", score=80)
    _insert_photo(app, root, "good-b", "2020-07-22T10:00:00", score=90)
    _insert_photo(app, root, "excluded", "2021-07-22T10:00:00", eligible=0, score=99)
    _insert_photo(app, root, "missing", "2018-07-22T10:00:00", score=100)

    service = app.extensions["inktime_render_service"]
    selected = service.select_random_history_day({}, rng=FirstChoice())
    assert selected["status"] == "ok"
    assert {candidate["id"] for candidate in selected["candidates"]} <= {"good-a", "good-b"}
    rerolled = service.reroll_history_day(
        {"month_day": "07-22", "current_photo_id": "good-b", "mode": "top_n", "top_n": 1},
        rng=FirstChoice(),
    )
    assert rerolled["candidates"][0]["id"] == "good-a"
    service.record_display(["good-a"], history_date="2019-07-22", selection_method="test")
    unseen = service.select_random_history_day({"unseen_only": True}, rng=FirstChoice())
    assert [candidate["id"] for candidate in unseen["candidates"]] == ["good-b"]


@pytest.mark.parametrize("row_count", [10_000, 100_000])
def test_history_selection_synthetic_rows_is_bounded_sqlite_work(app, tmp_path, row_count):
    root = tmp_path / "synthetic"
    root.mkdir()
    (root / "selected.jpg").write_bytes(b"x")
    photos = app.extensions["inktime_photo_repository"]
    library_id = photos.ensure_library("十萬合成", root)
    database = app.extensions["inktime_database"]
    now = "2026-07-22T00:00:00+00:00"
    rows = [
        (
            f"synthetic-{index}",
            library_id,
            f"not-present-{index}.jpg",
            f"{2000 + index % 25:04d}-07-22T10:00:00",
            f"{2000 + index % 25:04d}-07-22",
            "07-22",
            now,
            now,
        )
        for index in range(row_count)
    ]
    rows[25] = (
        "selected",
        library_id,
        "selected.jpg",
        "2000-07-22T10:00:00",
        "2000-07-22",
        "07-22",
        now,
        now,
    )
    with database.session() as connection:
        connection.execute("BEGIN")
        connection.executemany(
            "INSERT INTO photos(id,library_id,relative_path,status,captured_at,captured_date,"
            "captured_month_day,capture_date_status,lifecycle_status,eligible,local_candidate_score,"
            "created_at,updated_at) VALUES (?,?,?,'analyzed',?,?,?,'valid','active',1,75,?,?)",
            rows,
        )
        connection.commit()
    photos.save_analysis(
        "selected",
        None,
        "local",
        "local",
        "synthetic",
        valid_result(
            caption="這是一段合成歷史選片測試說明。",
            types=["其他"],
            memory_score=75,
            visual_score=75,
            side_caption="這是一段測試回憶短句。",
        ),
        "{}",
        ranking_score=75,
        final_ranking_score=75,
    )
    statements: list[str] = []
    with database.session() as connection:
        connection.set_trace_callback(statements.append)
    started = time.monotonic()
    result = app.extensions["inktime_render_service"].select_random_history_day({}, rng=FirstChoice())
    elapsed = time.monotonic() - started
    assert result["status"] == "ok"
    assert result["candidates"][0]["id"] == "selected"
    assert elapsed < 8
    assert (
        len([statement for statement in statements if statement.lstrip().upper().startswith("SELECT")]) <= 2
    )
    with sqlite3.connect(database.path) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
