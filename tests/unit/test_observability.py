from __future__ import annotations

from datetime import datetime, timedelta, timezone

from inktime.app.core.security import redact
from inktime.app.db import Database, migrate
from inktime.app.repositories.settings import SettingsRepository
from inktime.app.services.observability import ObservabilityService


def _service(tmp_path):
    database = Database(tmp_path / "db.sqlite")
    migrate(database)
    settings = SettingsRepository(database)
    settings.ensure_defaults()
    return database, settings, ObservabilityService(database, settings, None)


def test_debug_is_off_by_default_and_sensitive_values_are_redacted(tmp_path):
    database, settings, service = _service(tmp_path)
    assert settings.get("observability.debug_enabled") is False
    assert service.record("DEBUG", "test", "detail", "Bearer secret-token") is False
    hidden = redact(
        {"api_key": "abc", "payload": "data:image/jpeg;base64," + "a" * 300, "gps": "25.033000,121.565400"}
    )
    assert hidden["api_key"] == "[已遮蔽]" and "已遮蔽" in hidden["payload"] and "已遮蔽" in hidden["gps"]


def test_stale_job_alert_is_aggregated(tmp_path):
    database, _settings, service = _service(tmp_path)
    with database.session() as connection:
        connection.execute(
            "INSERT INTO jobs(id,kind,name,status,strategy,settings_json,created_at,heartbeat_at) VALUES ('job','analysis','測試','running','local','{}',?,?)",
            (
                datetime.now(timezone.utc).isoformat(),
                (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(),
            ),
        )
    service.tick()
    service.tick()
    with database.session() as connection:
        row = connection.execute(
            "SELECT occurrences FROM job_errors WHERE error_code='JOB-HEARTBEAT-STALE'"
        ).fetchone()
    assert row[0] == 1


def test_debug_expiry_disables_once(tmp_path):
    database, settings, service = _service(tmp_path)
    settings.update("observability.debug_enabled", True, changed_by="test", source_ip="test")
    with database.session() as connection:
        connection.execute(
            "UPDATE settings SET updated_at=? WHERE key='observability.debug_enabled'",
            ((datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),),
        )
    service.tick()
    service.tick()
    with database.session() as connection:
        events = connection.execute(
            "SELECT COUNT(*) FROM activity_events WHERE event='debug_auto_disabled'"
        ).fetchone()[0]
    assert settings.get("observability.debug_enabled") is False
    assert events == 1


def test_provider_and_release_incidents_recover_after_existing_data_recovers(tmp_path):
    database, _settings, service = _service(tmp_path)
    now = datetime.now(timezone.utc)
    with database.session() as connection:
        connection.executemany(
            "INSERT INTO api_usage(provider,model,request_type,started_at,completed_at,status,error_code) VALUES ('vision','model','analysis',?,?,?,?)",
            [(now.isoformat(), now.isoformat(), "failed", "timeout") for _ in range(3)],
        )
        connection.execute(
            "INSERT INTO releases(id,display_type,width,height,pixel_format,manifest_json,status,created_at) VALUES ('release','epd',480,800,'2bpp','{}','staged',?)",
            ((now - timedelta(minutes=30)).isoformat(),),
        )
    service.tick()
    with database.session() as connection:
        codes = {row[0] for row in connection.execute("SELECT error_code FROM job_errors WHERE resolved_at IS NULL")}
        connection.execute("UPDATE api_usage SET status='completed',error_code=NULL")
        connection.execute("UPDATE releases SET status='published',published_at=? WHERE id='release'", (now.isoformat(),))
    assert {"AI-PROVIDER-TIMEOUT", "RELEASE-STUCK"} <= codes
    service.tick()
    with database.session() as connection:
        unresolved = {row[0] for row in connection.execute("SELECT error_code FROM job_errors WHERE resolved_at IS NULL")}
    assert "AI-PROVIDER-TIMEOUT" not in unresolved
    assert "RELEASE-STUCK" not in unresolved


def test_retention_50000_rows_is_batched_and_preserves_unresolved_errors(tmp_path):
    database, settings, service = _service(tmp_path)
    settings.update("observability.activity_max_rows", 1000, changed_by="test", source_ip="test")
    now = datetime.now(timezone.utc)
    old = (now - timedelta(days=30)).isoformat()
    rows = []
    for index in range(50_000):
        severity = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")[index % 5]
        code = "KEEP" if severity in {"ERROR", "CRITICAL"} else "OLD"
        rows.append(("synthetic", str(index), severity, "test", "synthetic", "合成事件", code, "{}", old))
    with database.session() as connection:
        connection.executemany(
            "INSERT INTO activity_events(source,source_id,severity,component,event,message,error_code,details_json,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            rows,
        )
        connection.execute(
            "INSERT INTO job_errors(component,error_code,fingerprint,severity,message,first_seen_at,last_seen_at) VALUES ('test','KEEP','keep','critical','保留',?,?)",
            (old, old),
        )
    before = database.path.stat().st_size
    service.cleanup(now)
    with database.session() as connection:
        remaining = int(connection.execute("SELECT COUNT(*) FROM activity_events").fetchone()[0])
        kept = int(connection.execute("SELECT COUNT(*) FROM activity_events WHERE error_code='KEEP'").fetchone()[0])
        first_counts = {
            row[0]: int(row[1])
            for row in connection.execute("SELECT severity,COUNT(*) FROM activity_events GROUP BY severity")
        }
        connection.execute("UPDATE observability_state SET updated_at=? WHERE key='cleanup'", ((now - timedelta(hours=2)).isoformat(),))
    service.cleanup(now)
    with database.session() as connection:
        after = int(connection.execute("SELECT COUNT(*) FROM activity_events").fetchone()[0])
        unresolved = int(connection.execute("SELECT COUNT(*) FROM job_errors WHERE error_code='KEEP' AND resolved_at IS NULL").fetchone()[0])
        second_counts = {
            row[0]: int(row[1])
            for row in connection.execute("SELECT severity,COUNT(*) FROM activity_events GROUP BY severity")
        }
        plan = connection.execute("EXPLAIN QUERY PLAN SELECT id FROM activity_events WHERE severity='ERROR' ORDER BY id DESC LIMIT 200").fetchall()
    wal = database.path.with_name(database.path.name + "-wal")
    # A single aggregated capacity warning is intentionally retained, rather than looping writes.
    assert remaining == 48_001 and after == 46_001
    assert first_counts["DEBUG"] < first_counts["INFO"]
    assert second_counts["DEBUG"] < first_counts["DEBUG"]
    assert kept == 20_000 and unresolved == 1
    assert database.path.stat().st_size >= before and (not wal.exists() or wal.stat().st_size < 64 * 1024 * 1024)
    assert any("idx_activity_events" in str(tuple(row)) for row in plan)
