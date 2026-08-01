from __future__ import annotations

from collections import OrderedDict
from datetime import date, datetime
import logging
import re
import threading
import time
from typing import Callable
from typing import Generic, TypeVar


LOGGER = logging.getLogger(__name__)

_DATE_ONLY = re.compile(r"^(\d{4})([-/:])(\d{2})\2(\d{2})$")
_EXIF_DATETIME = re.compile(r"^(\d{4})([-/:])(\d{2})\2(\d{2})[ T](\d{2}):(\d{2}):(\d{2})(\.\d+)?$")
_ISO_WITH_ZONE = re.compile(
    r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$",
    re.IGNORECASE,
)


class BoundedDateWarningLimiter:
    """Deduplicate parser warnings without retaining input values or paths."""

    def __init__(self, *, max_entries: int = 64, window_seconds: float = 300.0) -> None:
        self.max_entries = max(1, int(max_entries))
        self.window_seconds = max(1.0, float(window_seconds))
        self._seen: OrderedDict[str, float] = OrderedDict()
        self._lock = threading.Lock()

    def warn(
        self,
        reason: str,
        value: object,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        shape = _safe_shape(value)
        key = f"{reason}:{shape}"
        now = clock()
        with self._lock:
            previous = self._seen.get(key)
            if previous is not None and now - previous < self.window_seconds:
                return
            self._seen[key] = now
            self._seen.move_to_end(key)
            while len(self._seen) > self.max_entries:
                self._seen.popitem(last=False)
        LOGGER.warning(
            "照片日期解析失敗 reason=%s format=%s；原值與私人路徑未記錄",
            reason,
            shape,
        )


def _safe_shape(value: object) -> str:
    if value is None:
        return "none"
    if not isinstance(value, str):
        return type(value).__name__[:24]
    text = value.strip()
    if not text:
        return "empty"
    shape = re.sub(r"[A-Za-z]", "A", text)
    shape = re.sub(r"\d", "#", shape)
    shape = re.sub(r"[^A#:+\-/. T]", "?", shape)
    return shape[:48]


_WARNINGS = BoundedDateWarningLimiter()
T = TypeVar("T")


class _CacheEntry(Generic[T]):
    def __init__(self, value: T, expires_at: float) -> None:
        self.value = value
        self.expires_at = expires_at


class BoundedSingleflightTTLCache(Generic[T]):
    """Small process-local cache with stale-on-error and one loader per key."""

    def __init__(
        self, *, ttl_seconds: float = 300.0, max_entries: int = 16, wait_seconds: float = 2.0
    ) -> None:
        self.ttl_seconds = max(0.01, float(ttl_seconds))
        self.max_entries = max(1, int(max_entries))
        self.wait_seconds = max(0.01, float(wait_seconds))
        self._entries: OrderedDict[str, _CacheEntry[T]] = OrderedDict()
        self._in_flight: set[str] = set()
        self._condition = threading.Condition(threading.Lock())

    def get(self, key: str, loader: Callable[[], T]) -> T:
        now = time.monotonic()
        stale: _CacheEntry[T] | None = None
        with self._condition:
            entry = self._entries.get(key)
            if entry is not None:
                self._entries.move_to_end(key)
                if entry.expires_at > now:
                    return entry.value
                stale = entry
            if key in self._in_flight:
                if stale is not None:
                    return stale.value
                deadline = now + self.wait_seconds
                while key in self._in_flight:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("date cache refresh wait timed out")
                    self._condition.wait(remaining)
                completed = self._entries.get(key)
                if completed is not None:
                    self._entries.move_to_end(key)
                    return completed.value
            self._in_flight.add(key)

        try:
            value = loader()
        except Exception:
            with self._condition:
                self._in_flight.discard(key)
                self._condition.notify_all()
                preserved = self._entries.get(key)
                if preserved is not None:
                    self._entries.move_to_end(key)
                    return preserved.value
            raise

        with self._condition:
            self._entries[key] = _CacheEntry(value, time.monotonic() + self.ttl_seconds)
            self._entries.move_to_end(key)
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)
            self._in_flight.discard(key)
            self._condition.notify_all()
        return value


def parse_photo_datetime(value: object, *, warn: bool = True) -> datetime | None:
    """Parse supported EXIF/ISO values without inventing a timezone."""

    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    if not isinstance(value, str):
        if warn and value is not None:
            _WARNINGS.warn("type", value)
        return None
    text = value.strip()
    if not text:
        return None

    match = _DATE_ONLY.fullmatch(text)
    if match:
        try:
            return datetime(int(match[1]), int(match[3]), int(match[4]))
        except ValueError:
            if warn:
                _WARNINGS.warn("invalid-date", text)
            return None

    match = _EXIF_DATETIME.fullmatch(text)
    if match:
        try:
            microseconds = int(float(match[8] or "0") * 1_000_000)
            return datetime(
                int(match[1]),
                int(match[3]),
                int(match[4]),
                int(match[5]),
                int(match[6]),
                int(match[7]),
                microseconds,
            )
        except (OverflowError, ValueError):
            if warn:
                _WARNINGS.warn("invalid-datetime", text)
            return None

    if _ISO_WITH_ZONE.fullmatch(text):
        try:
            normalized = text[:-1] + "+00:00" if text[-1:].upper() == "Z" else text
            return datetime.fromisoformat(normalized)
        except ValueError:
            if warn:
                _WARNINGS.warn("invalid-iso", text)
            return None

    if warn:
        _WARNINGS.warn("unsupported-format", text)
    return None


def parse_photo_date(value: object, *, warn: bool = True) -> date | None:
    parsed = parse_photo_datetime(value, warn=warn)
    return parsed.date() if parsed is not None else None


def materialized_capture_fields(value: object, *, warn: bool = True) -> tuple[str | None, str | None, str]:
    """Return ISO date, MM-DD and an auditable parse status."""

    if value is None or (isinstance(value, str) and not value.strip()):
        return None, None, "missing"
    parsed = parse_photo_date(value, warn=warn)
    if parsed is None:
        return None, None, "invalid"
    return parsed.isoformat(), parsed.strftime("%m-%d"), "valid"
