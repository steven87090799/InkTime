from __future__ import annotations

from datetime import timezone, timedelta
from concurrent.futures import ThreadPoolExecutor
import threading

from inktime.app.domain.photos.dates import (
    BoundedDateWarningLimiter,
    BoundedSingleflightTTLCache,
    materialized_capture_fields,
    parse_photo_date,
    parse_photo_datetime,
)


def test_parser_supports_exif_date_iso_and_offsets():
    for value in (
        "2026:02:28 23:59:58",
        "2026-02-28 23:59:58",
        "2026/02/28 23:59:58",
        "2026-02-28",
        "2026/02/28",
        "2026:02:28",
        "2026-02-28T23:59:58",
        "2026-02-28T23:59:58.123456",
    ):
        parsed = parse_photo_datetime(value)
        assert parsed is not None
        assert parsed.tzinfo is None
        assert parsed.date().isoformat() == "2026-02-28"

    utc = parse_photo_datetime("2026-02-28T23:59:58Z")
    offset = parse_photo_datetime("2026-03-01T07:59:58+08:00")
    spaced_offset = parse_photo_datetime("2026-03-01 07:59:58.250+08:00")
    assert utc is not None and utc.tzinfo == timezone.utc
    assert offset is not None and offset.utcoffset() == timedelta(hours=8)
    assert spaced_offset is not None and spaced_offset.utcoffset() == timedelta(hours=8)
    assert utc.timestamp() == offset.timestamp()


def test_parser_handles_leap_day_and_rejects_invalid_or_incomplete_values():
    assert parse_photo_date("2024-02-29") is not None
    for value in (
        "2026-02-29",
        "2026-02-30",
        "2026-2-03",
        "2026-02",
        "2026:02:28 12:00",
        "2026-02-28T12:00:00+0800",
    ):
        assert parse_photo_datetime(value) is None
    assert materialized_capture_fields("2026-02-30") == (None, None, "invalid")
    assert materialized_capture_fields(None) == (None, None, "missing")


def test_date_warning_limiter_is_bounded_and_deduplicated(caplog):
    limiter = BoundedDateWarningLimiter(max_entries=2, window_seconds=60)
    limiter.warn("invalid", "2026-02-30", clock=lambda: 10)
    limiter.warn("invalid", "2026-02-30", clock=lambda: 11)
    limiter.warn("other", "private/path/2026", clock=lambda: 12)
    limiter.warn("third", "x", clock=lambda: 13)
    assert len(limiter._seen) == 2
    assert "private/path" not in caplog.text


def test_singleflight_twenty_threads_only_load_once():
    cache = BoundedSingleflightTTLCache[list[str]](
        ttl_seconds=60, max_entries=4, wait_seconds=5
    )
    barrier = threading.Barrier(20)
    calls = 0
    lock = threading.Lock()

    def load() -> list[str]:
        nonlocal calls
        with lock:
            calls += 1
        threading.Event().wait(0.05)
        return ["02-29"]

    def read(_index: int) -> list[str]:
        barrier.wait()
        return cache.get("db:library", load)

    with ThreadPoolExecutor(max_workers=20) as executor:
        assert list(executor.map(read, range(20))) == [["02-29"]] * 20
    assert calls == 1


def test_singleflight_failure_keeps_last_successful_stale_value():
    cache = BoundedSingleflightTTLCache[list[str]](
        ttl_seconds=0.01, max_entries=1, wait_seconds=1
    )
    assert cache.get("db", lambda: ["01-01"]) == ["01-01"]
    threading.Event().wait(0.02)

    def fail() -> list[str]:
        raise RuntimeError("database unavailable")

    assert cache.get("db", fail) == ["01-01"]
