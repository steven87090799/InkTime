from __future__ import annotations

import json
from pathlib import Path

from inktime.app.db import Database, migrate
from inktime.app.repositories.photo_analysis_retention import PhotoAnalysisRetentionRepository


def _database(tmp_path: Path) -> Database:
    database = Database(tmp_path / "retention.db")
    migrate(database)
    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO libraries(id,name,root_path,created_at,updated_at) "
            "VALUES ('library','Retention','/photos','2026-01-01','2026-01-01')"
        )
    return database


def _photo(database: Database, photo_id: str) -> None:
    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO photos(id,library_id,relative_path,status,created_at,updated_at) "
            "VALUES (?,'library',?,'analyzed','2026-01-01','2026-01-01')",
            (photo_id, f"{photo_id}.jpg"),
        )


def _analysis(
    database: Database,
    photo_id: str,
    sequence: int,
    *,
    fingerprint: str = "historical",
    semantic_json: str | None = "{}",
    analysis_spec: str | None = None,
) -> int:
    with database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO photo_analysis(
                photo_id,schema_version,stage,raw_json,semantic_json,
                analysis_fingerprint,analysis_spec_json,created_at
            ) VALUES (?,3,'complete','{}',?,?,?,?)
            """,
            (
                photo_id,
                semantic_json,
                fingerprint,
                analysis_spec or json.dumps({"sequence": sequence}, sort_keys=True),
                f"2026-01-{sequence:02d}T00:00:00+00:00",
            ),
        )
        return int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])


def _seed_protected_graph(database: Database) -> dict[str, int]:
    _photo(database, "photo")
    ids = {
        "candidate_one": _analysis(database, "photo", 1),
        "candidate_two": _analysis(database, "photo", 2),
        "event": _analysis(database, "photo", 3),
        "inherited_source": _analysis(database, "photo", 4),
    }
    ids["inherited_child"] = _analysis(
        database,
        "photo",
        5,
        semantic_json=json.dumps(
            {"inherited_from": {"analysis_id": ids["inherited_source"]}},
            sort_keys=True,
        ),
    )
    ids.update(
        {
            "review": _analysis(database, "photo", 6),
            "current": _analysis(database, "photo", 7, fingerprint="current"),
            "buffer_one": _analysis(database, "photo", 8),
            "buffer_two": _analysis(database, "photo", 9),
            "latest": _analysis(database, "photo", 10),
        }
    )
    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO photo_review_events(photo_id,analysis_id,action,created_at) "
            "VALUES ('photo',?,'reviewed','2026-02-01')",
            (ids["event"],),
        )
        for analysis_id in (ids["inherited_child"], ids["review"]):
            connection.execute(
                "INSERT INTO photo_reviews(photo_id,analysis_id,updated_at) "
                "VALUES ('photo',?,'2026-02-01')",
                (analysis_id,),
            )
    return ids


def test_inventory_is_deterministic_and_protects_every_dependency_class(tmp_path: Path):
    database = _database(tmp_path)
    ids = _seed_protected_graph(database)
    _photo(database, "corrupt")
    for sequence in range(1, 5):
        _analysis(database, "corrupt", sequence + 10, semantic_json="{invalid")
    repository = PhotoAnalysisRetentionRepository(database)

    first = repository.inventory(current_fingerprints=["current"])
    second = repository.inventory(current_fingerprints=["current"])

    assert first == second
    assert first["total_rows"] == 14
    assert first["latest_rows"] == 2
    assert first["current_rows"] == 1
    assert first["invalid_semantic_json_rows"] == 3
    assert first["review_or_event_rows"] == 3
    assert first["inherited_source_rows"] == 1
    assert first["historical_buffer_rows"] == 2
    assert first["candidate_rows"] == 2
    assert [row["id"] for row in first["candidate_sample"]] == [
        ids["candidate_one"],
        ids["candidate_two"],
    ]
    assert first["policy"]["age_grace_days"] == 0


def test_prune_rechecks_and_commits_only_one_bounded_batch(tmp_path: Path, monkeypatch):
    database = _database(tmp_path)
    ids = _seed_protected_graph(database)
    statements: list[str] = []
    original_connect = database.connect

    def traced_connect():
        connection = original_connect()
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(database, "connect", traced_connect)
    repository = PhotoAnalysisRetentionRepository(database)

    preview = repository.inventory(current_fingerprints=["current"])
    first = repository.prune_batch(
        current_fingerprints=["current"],
        batch_size=1,
        expected_inventory_digest=preview["inventory_digest"],
    )
    assert first["deleted_rows"] == 1
    assert first["remaining_candidate_rows"] == 1
    assert first["complete"] is False
    with database.session() as connection:
        remaining = {
            int(row[0]) for row in connection.execute("SELECT id FROM photo_analysis").fetchall()
        }
    assert ids["candidate_one"] not in remaining
    for protected in (
        "event",
        "inherited_source",
        "inherited_child",
        "review",
        "current",
        "buffer_one",
        "buffer_two",
        "latest",
    ):
        assert ids[protected] in remaining

    try:
        repository.prune_batch(
            current_fingerprints=["current"],
            batch_size=1,
            expected_inventory_digest=preview["inventory_digest"],
        )
    except RuntimeError:
        pass
    else:
        raise AssertionError("a stale dry-run digest must fail closed")
    second = repository.prune_batch(
        current_fingerprints=["current"],
        batch_size=1,
        expected_inventory_digest=first["inventory"]["inventory_digest"],
    )
    third = repository.prune_batch(
        current_fingerprints=["current"],
        batch_size=1,
        expected_inventory_digest=second["inventory"]["inventory_digest"],
    )
    assert second["deleted_rows"] == 1 and second["complete"] is True
    assert third["deleted_rows"] == 0 and third["complete"] is True
    assert statements.count("BEGIN IMMEDIATE") == 4
    assert statements.count("COMMIT") == 3
    assert statements.count("ROLLBACK") == 1
    assert all("VACUUM" not in statement.upper() for statement in statements)


def test_exact_current_spec_is_protected_when_its_fingerprint_is_historical(tmp_path: Path):
    database = _database(tmp_path)
    _photo(database, "spec")
    candidate = _analysis(database, "spec", 1)
    current_spec = _analysis(database, "spec", 2, analysis_spec='{"current":true}')
    _analysis(database, "spec", 3)
    _analysis(database, "spec", 4)
    _analysis(database, "spec", 5)

    inventory = PhotoAnalysisRetentionRepository(database).inventory(
        current_fingerprints=["not-present"],
        current_specs=['{"current":true}'],
    )

    assert inventory["current_rows"] == 1
    assert [row["id"] for row in inventory["candidate_sample"]] == [candidate]
    assert current_spec not in {row["id"] for row in inventory["candidate_sample"]}


def test_new_review_after_dry_run_invalidates_apply_without_deleting(tmp_path: Path):
    database = _database(tmp_path)
    ids = _seed_protected_graph(database)
    repository = PhotoAnalysisRetentionRepository(database)
    preview = repository.inventory(current_fingerprints=["current"])
    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO photo_reviews(photo_id,analysis_id,updated_at) "
            "VALUES ('photo',?,'2026-03-01')",
            (ids["candidate_one"],),
        )

    try:
        repository.prune_batch(
            current_fingerprints=["current"],
            batch_size=200,
            expected_inventory_digest=preview["inventory_digest"],
        )
    except RuntimeError:
        pass
    else:
        raise AssertionError("concurrent review must invalidate the dry-run")
    with database.session() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM photo_analysis WHERE id=?", (ids["candidate_one"],)
        ).fetchone()[0] == 1


def test_retention_rejects_missing_identity_and_unsafe_batch_sizes(tmp_path: Path):
    repository = PhotoAnalysisRetentionRepository(_database(tmp_path))

    for operation in (
        lambda: repository.inventory(current_fingerprints=[]),
        lambda: repository.prune_batch(
            current_fingerprints=["current"], batch_size=0, expected_inventory_digest="invalid"
        ),
        lambda: repository.prune_batch(
            current_fingerprints=["current"], batch_size=501, expected_inventory_digest="invalid"
        ),
    ):
        try:
            operation()
        except ValueError:
            pass
        else:
            raise AssertionError("unsafe retention input must fail closed")
