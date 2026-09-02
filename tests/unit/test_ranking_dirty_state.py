from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from tests.integration.test_jobs import add_photos


def _library_id(repository, photo_id: str) -> str:
    return str(repository.get_with_path(photo_id)["library_id"])


def _set_clean(repository, library_id: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with repository.database.session() as connection:
        connection.execute(
            """INSERT INTO library_ranking_state(library_id,dirty,updated_at)
               VALUES (?,0,?)
               ON CONFLICT(library_id) DO UPDATE SET dirty=0,updated_at=excluded.updated_at""",
            (library_id, now),
        )


def _begin_scan(repository, library_id: str) -> str:
    return repository.begin_scan(
        library_id,
        Path("/photos"),
        mode="incremental",
        trigger_source="test",
        missing_threshold_ratio=1.0,
    )


def test_unchanged_active_scan_bookkeeping_keeps_ranking_clean(app):
    repository = app.extensions["inktime_photo_repository"]
    photo_ids = add_photos(app, 2)
    library_id = _library_id(repository, photo_ids[0])
    scan_id = _begin_scan(repository, library_id)
    _set_clean(repository, library_id)

    repository.mark_seen_batch(scan_id, photo_ids)

    assert repository.ranking_state(library_id)["dirty"] == 0


def test_missing_to_active_scan_transition_marks_ranking_dirty(app):
    repository = app.extensions["inktime_photo_repository"]
    photo_id = add_photos(app, 1)[0]
    library_id = _library_id(repository, photo_id)
    with repository.database.session() as connection:
        connection.execute(
            "UPDATE photos SET lifecycle_status='missing' WHERE id=?", (photo_id,)
        )
    scan_id = _begin_scan(repository, library_id)
    _set_clean(repository, library_id)

    repository.mark_seen_batch(scan_id, [photo_id])

    assert repository.get_with_path(photo_id)["lifecycle_status"] == "active"
    assert repository.ranking_state(library_id)["dirty"] == 1


def test_active_to_missing_scan_transition_marks_ranking_dirty(app):
    repository = app.extensions["inktime_photo_repository"]
    seen_id, missing_id = add_photos(app, 2)
    library_id = _library_id(repository, seen_id)
    scan_id = _begin_scan(repository, library_id)
    repository.mark_seen_batch(scan_id, [seen_id])
    _set_clean(repository, library_id)

    result = repository.finish_scan(
        scan_id,
        counts={"checked": 2, "processed": 2},
        full_census=True,
        cancelled=False,
        major_io_errors=0,
    )

    assert result["missing_marked_count"] == 1
    assert repository.get_with_path(missing_id)["lifecycle_status"] == "missing"
    assert repository.ranking_state(library_id)["dirty"] == 1


def test_ranking_refresh_clears_dirty_only_after_success(app, monkeypatch):
    repository = app.extensions["inktime_photo_repository"]
    photo_id = add_photos(app, 1)[0]
    library_id = _library_id(repository, photo_id)
    with repository.database.session() as connection:
        repository._mark_library_ranking_dirty(connection, library_id)
    original = repository._refresh_library_ranking

    def fail_refresh(*_args):
        raise RuntimeError("ranking refresh failed")

    monkeypatch.setattr(repository, "_refresh_library_ranking", fail_refresh)
    with pytest.raises(RuntimeError, match="ranking refresh failed"):
        repository.refresh_library_ranking(library_id)
    assert repository.ranking_state(library_id)["dirty"] == 1

    monkeypatch.setattr(repository, "_refresh_library_ranking", original)
    assert repository.refresh_library_ranking(library_id) is True
    state = repository.ranking_state(library_id)
    assert state["dirty"] == 0
    assert state["last_refreshed_at"] is not None
