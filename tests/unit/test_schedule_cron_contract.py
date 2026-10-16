from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from inktime.app.repositories.schedules import ScheduledTaskRepository


@pytest.mark.parametrize(("cron", "after", "expected"), [
    ("0 0 1 */3 *", "2026-02-28T23:59:00+00:00", "2026-04-01T00:00:00+00:00"),
    ("0 0 */3 * *", "2026-10-01T00:01:00+00:00", "2026-10-04T00:00:00+00:00"),
    ("0 0 1 * 0", "2026-10-12T00:00:00+00:00", "2026-10-18T00:00:00+00:00"),
    ("0 0 * * 7", "2026-10-12T00:00:00+00:00", "2026-10-18T00:00:00+00:00"),
    ("0 0 1 1 *", "2026-12-31T23:59:00+00:00", "2027-01-01T00:00:00+00:00"),
])
def test_supported_cron_subset_uses_field_origins_and_restricted_date_or(cron, after, expected):
    repository = ScheduledTaskRepository(None)
    assert repository._next_run(cron, datetime.fromisoformat(after), []).isoformat() == expected


@pytest.mark.parametrize("cron", ["60 * * * *", "0 24 * * *", "0 0 0 * *", "0 0 * 13 *", "0 0 * * 8", "*/0 * * * *", "0,,1 * * * *"])
def test_invalid_fields_are_rejected_before_search(cron):
    with pytest.raises(ValueError):
        ScheduledTaskRepository._cron(cron)


def test_dst_repeated_wall_minute_is_not_replayed():
    zone = ZoneInfo("America/New_York")
    after = datetime(2026, 11, 1, 1, 30, tzinfo=zone, fold=0)
    result = ScheduledTaskRepository(None)._next_run("30 1 * * *", after, [])
    assert result.astimezone(timezone.utc) == datetime(2026, 11, 2, 6, 30, tzinfo=timezone.utc)


def test_legacy_rule_persists_until_explicit_conversion_and_survives_restart(app):
    repository = app.extensions["inktime_schedule_repository"]
    task = repository.list()[0]
    key = task["key"]
    with repository.database.transaction() as connection:
        connection.execute("UPDATE scheduled_tasks SET cron='0 0 1 */3 *',config_json=json_remove(config_json,'$._cron_semantics') WHERE key=?", (key,))
    task = repository.get(key)
    assert task["cron_semantics"] == "legacy_v1"
    changed = repository.update(key, {"enabled": False, "cron": task["cron"]}, "UTC")
    assert changed["cron_semantics"] == "legacy_v1"
    after = datetime(2026, 2, 28, 23, 59, tzinfo=timezone.utc)
    assert repository._next_run(task["cron"], after, [], semantics="legacy_v1").month == 3
    changed = repository.update(key, {"cron_semantics": "cronie_v2"}, "UTC")
    restarted = ScheduledTaskRepository(repository.database)
    assert restarted.get(key)["cron_semantics"] == "cronie_v2"
    assert "_cron_semantics" not in changed["config"]
    assert restarted._next_run(task["cron"], after, [], semantics=restarted.get(key)["cron_semantics"]).month == 4
