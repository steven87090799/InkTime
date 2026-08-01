from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import importlib
import sqlite3
import sys
import types

import pytest

from inktime.app.db import Database, backfill_photo_capture_dates, migrate
from inktime.app.platform import initialize_platform


def _legacy_server(monkeypatch, tmp_path):
    monkeypatch.setenv("INKTIME_DATABASE", str(tmp_path / "legacy.db"))
    monkeypatch.setenv("INKTIME_PHOTO_DIR", str(tmp_path / "photos"))
    monkeypatch.delenv("INKTIME_ENABLE_LEGACY_WEBUI", raising=False)
    sys.modules.pop("legacy_server", None)
    return importlib.import_module("legacy_server")


def test_legacy_routes_are_disabled_before_any_database_query(monkeypatch, tmp_path):
    legacy = _legacy_server(monkeypatch, tmp_path)
    monkeypatch.setattr(legacy.sqlite3, "connect", lambda *_args, **_kwargs: pytest.fail("SQL"))
    client = legacy.app.test_client()
    for path in ("/review", "/sim", "/sim_render", "/api/md_list", "/images/a.jpg", "/files/"):
        assert client.get(path).status_code == 404


def test_enabled_legacy_route_keeps_platform_auth_boundary(monkeypatch, tmp_path):
    legacy = _legacy_server(monkeypatch, tmp_path)
    initialize_platform(
        legacy.app,
        database_path=legacy.DB_PATH,
        data_dir=tmp_path / "data",
        release_dir=tmp_path / "releases",
        testing=True,
    )
    legacy.app.config["INKTIME_ENABLE_LEGACY_WEBUI"] = True
    response = legacy.app.test_client().get("/review")
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/setup")


def test_legacy_review_has_page_limit_sort_allowlist_and_materialized_filter(monkeypatch, tmp_path):
    legacy = _legacy_server(monkeypatch, tmp_path)
    connection = sqlite3.connect(legacy.DB_PATH)
    connection.execute(
        "CREATE TABLE photo_scores(path TEXT PRIMARY KEY,caption TEXT,type TEXT,memory_score REAL,"
        "beauty_score REAL,reason TEXT,exif_json TEXT,width INTEGER,height INTEGER,orientation TEXT,"
        "used_at TEXT,side_caption TEXT,captured_date TEXT,captured_month_day TEXT,exif_datetime TEXT)"
    )
    connection.executemany(
        "INSERT INTO photo_scores(path,memory_score,beauty_score,captured_date,captured_month_day) "
        "VALUES (?,?,?,?,?)",
        [(f"p-{index:04d}", index, index, "2024-02-29", "02-29") for index in range(250)],
    )
    connection.commit()
    connection.close()
    rows, total = legacy.load_rows(page=-99, page_size=100_000, md="02-29", sort="path; DROP TABLE")
    assert len(rows) == 100
    assert total == 250
    with sqlite3.connect(legacy.DB_PATH) as checked:
        assert checked.execute("SELECT COUNT(*) FROM photo_scores").fetchone()[0] == 250


def test_legacy_metadata_backfill_is_repeatable_and_does_not_read_exif_json(monkeypatch, tmp_path):
    legacy = _legacy_server(monkeypatch, tmp_path)
    connection = sqlite3.connect(legacy.DB_PATH)
    connection.execute("CREATE TABLE photo_scores(path TEXT PRIMARY KEY,exif_datetime TEXT,exif_json TEXT)")
    connection.executemany(
        "INSERT INTO photo_scores(path,exif_datetime,exif_json) VALUES (?,?,?)",
        [
            ("a", "2024:02:29 12:00:00", "must-not-be-parsed"),
            ("b", "2026-02-30", "must-not-be-parsed"),
            ("c", None, "must-not-be-parsed"),
        ],
    )
    connection.commit()
    connection.close()
    assert legacy.prepare_legacy_data_schema(batch_size=2) == {
        "processed": 3,
        "valid": 1,
        "invalid": 1,
        "missing": 1,
    }
    assert legacy.prepare_legacy_data_schema(batch_size=2)["processed"] == 0
    with sqlite3.connect(legacy.DB_PATH) as checked:
        assert checked.execute(
            "SELECT captured_date,captured_month_day,capture_date_status FROM photo_scores WHERE path='a'"
        ).fetchone() == ("2024-02-29", "02-29", "valid")


def test_modern_month_day_query_uses_index_and_caps_results(monkeypatch, tmp_path):
    legacy = _legacy_server(monkeypatch, tmp_path)
    database = Database(legacy.DB_PATH)
    migrate(database)
    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO libraries(id,name,root_path,created_at,updated_at) VALUES ('lib','L','/photos',datetime('now'),datetime('now'))"
        )
        connection.executemany(
            "INSERT INTO photos(id,library_id,relative_path,status,captured_at,created_at,updated_at) "
            "VALUES (?,'lib',?,'discovered',?,datetime('now'),datetime('now'))",
            [(f"p-{index:06d}", f"{index}.jpg", "2024-02-29T12:00:00") for index in range(100_000)],
        )
    result = backfill_photo_capture_dates(database, batch_size=500)
    assert result == {"processed": 100_000, "valid": 100_000, "invalid": 0, "missing": 0}
    with database.session() as connection:
        plan = connection.execute(
            "EXPLAIN QUERY PLAN SELECT DISTINCT captured_month_day FROM photos "
            "INDEXED BY idx_photos_captured_month_day WHERE captured_month_day IS NOT NULL "
            "ORDER BY captured_month_day LIMIT 367"
        ).fetchall()
    assert "idx_photos_captured_month_day" in " ".join(str(value) for row in plan for value in row)
    assert legacy._query_all_md_list() == ["02-29"]
    assert len(legacy._query_all_md_list()) <= 366


def test_legacy_date_collection_batches_placeholders(monkeypatch, tmp_path):
    legacy = _legacy_server(monkeypatch, tmp_path)
    connection = sqlite3.connect(legacy.DB_PATH)
    connection.execute(
        "CREATE TABLE photo_scores(path TEXT PRIMARY KEY,caption TEXT,type TEXT,memory_score REAL,"
        "beauty_score REAL,reason TEXT,side_caption TEXT,exif_json TEXT,width INTEGER,height INTEGER,"
        "orientation TEXT,used_at TEXT,exif_gps_lat REAL,exif_gps_lon REAL,exif_city TEXT,captured_date TEXT)"
    )
    connection.commit()
    connection.close()
    dates = [f"2024-01-{(index % 28) + 1:02d}" for index in range(100_000)]
    assert legacy.load_sim_rows_for_dates(dates) == []
    assert legacy.LEGACY_QUERY_BATCH_SIZE <= 200


@pytest.mark.parametrize("exif_json", [None, "{broken-json", '{"make":"camera"}'])
def test_simulator_prefers_materialized_date_when_exif_json_is_unusable(monkeypatch, tmp_path, exif_json):
    legacy = _legacy_server(monkeypatch, tmp_path)
    photo_root = tmp_path / "photos"
    photo_root.mkdir()
    photo = photo_root / "memory.jpg"
    photo.write_bytes(b"synthetic")
    connection = sqlite3.connect(legacy.DB_PATH)
    connection.execute(
        "CREATE TABLE photo_scores(path TEXT PRIMARY KEY,caption TEXT,type TEXT,memory_score REAL,"
        "beauty_score REAL,reason TEXT,side_caption TEXT,captured_date TEXT,exif_datetime TEXT,"
        "exif_json TEXT,width INTEGER,height INTEGER,orientation TEXT,used_at TEXT,"
        "exif_gps_lat REAL,exif_gps_lon REAL,exif_city TEXT)"
    )
    connection.execute(
        "INSERT INTO photo_scores VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            str(photo),
            "caption",
            "memory",
            88.0,
            77.0,
            "reason",
            "side",
            "2024-02-29",
            "2020:01:02 03:04:05",
            exif_json,
            1200,
            800,
            "landscape",
            None,
            None,
            None,
            "Taipei",
        ),
    )
    connection.commit()
    connection.close()

    rows = legacy.load_sim_rows_for_dates(["2024-02-29"])
    assert len(rows) == 1
    assert rows[0][7:10] == ("2024-02-29", "2020:01:02 03:04:05", exif_json)
    assert legacy.load_sim_rows()[0][7:10] == rows[0][7:10]
    assert legacy.get_photo_meta_by_path(str(photo))["date"] == "2024-02-29"
    html = legacy.build_simulator_html(rows)
    assert '"date": "2024-02-29"' in html
    assert '"exif_json":' in html

    monkeypatch.setenv("INKTIME_LEGACY_OUTPUT_DIR", str(tmp_path / "output"))
    sys.modules.pop("render_daily_photo", None)
    renderer = importlib.import_module("render_daily_photo")
    rendered_rows = renderer.load_sim_rows()
    assert rendered_rows[0]["date"] == "2024-02-29"
    assert rendered_rows[0]["exif_json"] == (exif_json or "")


def test_legacy_analyzer_futures_and_sql_batches_are_bounded(monkeypatch):
    config = types.ModuleType("config")
    monkeypatch.setitem(sys.modules, "config", config)
    sys.modules.pop("legacy_analyze_photos", None)
    analyzer = importlib.import_module("legacy_analyze_photos")
    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE photo_scores(path TEXT PRIMARY KEY)")
    paths = [analyzer.Path(f"/{index}.jpg") for index in range(2_001)]
    assert len(list(analyzer.filter_unscored(connection, iter(paths), batch_size=200))) == 2_001
    assert max(len(chunk) for chunk in (paths[i : i + 200] for i in range(0, len(paths), 200))) == 200

    pending_counts: list[int] = []
    completed = 0

    def worker(index):
        analyzer.time.sleep(0.005)
        if index % 17 == 0:
            raise RuntimeError("synthetic worker failure")
        return index

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = analyzer.bounded_future_results(
            executor,
            ((index,) for index in range(100)),
            worker,
            max_in_flight=8,
            on_pending_change=pending_counts.append,
        )
        for future in futures:
            completed += 1
            try:
                future.result()
            except RuntimeError:
                pass
    assert completed == 100
    assert max(pending_counts) == 8
    assert pending_counts[-1] == 0

    class ManualExecutor:
        def __init__(self):
            self.futures = []

        def submit(self, callback, *args):
            future = analyzer.Future()
            self.futures.append(future)
            if len(self.futures) == 1:
                future.set_result(callback(*args))
            return future

    manual = ManualExecutor()
    results = analyzer.bounded_future_results(
        manual, ((index,) for index in range(100)), lambda index: index, max_in_flight=8
    )
    assert next(results).result() == 0
    results.close()
    assert len(manual.futures) == 8
    assert all(future.cancelled() for future in manual.futures[1:])


def test_path_spool_bulk_flow_uses_one_iterator_pass(monkeypatch):
    config = types.ModuleType("config")
    monkeypatch.setitem(sys.modules, "config", config)
    sys.modules.pop("legacy_analyze_photos", None)
    analyzer = importlib.import_module("legacy_analyze_photos")

    class CountingPathSpool(analyzer.PathSpool):
        def __init__(self, paths):
            super().__init__(paths)
            self.iterations = 0

        def __iter__(self):
            self.iterations += 1
            yield from super().__iter__()

    spool = CountingPathSpool(analyzer.Path(f"/{index}.jpg") for index in range(1_201))
    try:
        chunks = list(analyzer.chunked_paths(iter(spool), 500))
    finally:
        spool.close()
    assert [len(chunk) for chunk in chunks] == [500, 500, 201]
    assert spool.iterations == 1
    assert sum(len(chunk) for chunk in chunks) == 1_201
    source = analyzer.Path("legacy_analyze_photos.py").read_text(encoding="utf-8")
    assert "imgs[i : i + CHUNK]" not in source


def test_production_entrypoints_do_not_import_legacy_analyzer():
    for path in (
        "server.py",
        "inktime/app/workers/runner.py",
        "inktime/app/workers/scheduler.py",
        "docker-compose.yml",
    ):
        assert "legacy_analyze_photos" not in open(path, encoding="utf-8").read()
