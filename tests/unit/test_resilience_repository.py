from __future__ import annotations

from pathlib import Path

from inktime.app.db import Database, migrate
from inktime.app.repositories.resilience import ResilienceRepository


def test_decision_trace_caps_candidate_detail_at_fifty(tmp_path: Path):
    database = Database(tmp_path / "inktime.sqlite3")
    migrate(database)
    repository = ResilienceRepository(database)
    version = repository.algorithm_version(
        name="selection", version="v1", configuration={"weight": 1}, renderer="r1",
        layout="l1", pairing="p1", scoring="s1",
    )
    trace_id = repository.create_trace(
        execution_mode="test", algorithm_version_id=version, primary_photo_id=None,
        candidates=[{"adjusted_score": float(index)} for index in range(100)], candidate_count=100,
    )
    trace = repository.trace(trace_id)
    assert trace is not None
    assert len(trace["candidates"]) == 50
    assert trace["candidate_count"] == 100


def test_algorithm_version_uses_stable_configuration_hash(tmp_path: Path):
    database = Database(tmp_path / "inktime.sqlite3")
    migrate(database)
    repository = ResilienceRepository(database)
    first = repository.algorithm_version(name="selection", version="v1", configuration={"a": 1}, renderer="r1", layout="l1", pairing="p1", scoring="s1")
    second = repository.algorithm_version(name="selection", version="v1", configuration={"a": 1}, renderer="r1", layout="l1", pairing="p1", scoring="s1")
    assert first == second
