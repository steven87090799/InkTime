"""Deterministic paid-state fixture for real container persistence acceptance."""
import json
from hashlib import sha256
from pathlib import Path
import sqlite3
import sys
from datetime import datetime, timezone


LAN_SOURCE_ID = "ci-lan-safe-source"
LAN_SOURCE_BYTES = b"P6\n1 1\n255\n\xff\xff\xff"


def seed_lan_release_source(connection, directory: Path, *, runtime_root: str) -> str:
    """Give the isolated LAN fixture current v6 provenance, without a model call."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "ci-lan-source.ppm").write_bytes(LAN_SOURCE_BYTES)
    digest = sha256(LAN_SOURCE_BYTES).hexdigest()
    now = _now()
    connection.execute(
        "INSERT INTO libraries(id,name,root_path,created_at,updated_at) VALUES (?,?,?,?,?)",
        ("ci-lan-source-library", "CI synthetic source", runtime_root, now, now),
    )
    connection.execute(
        "INSERT INTO photos(id,library_id,relative_path,sha256,width,height,status,eligible,"
        "local_features_status,visual_orientation_rotation_cw,visual_orientation_confidence,"
        "visual_orientation_ambiguous,created_at,updated_at) "
        "VALUES (?,?,?, ?,1,1,'analyzed',1,'complete',0,1,0,?,?)",
        (LAN_SOURCE_ID, "ci-lan-source-library", "ci-lan-source.ppm", digest, now, now),
    )
    result = {
        "schema_version": 6, "types": ["人物", "日常"], "memory_score": 72,
        "visual_score": 81, "special_level": 2, "side_caption": "這是隔離測試的合成來源。",
        "content_filter": {key: {"detected": False, "confidence": 1}
                           for key in ("sexualized_content", "explicit_nudity", "female_glamour_portrait")},
        "display_exclusion": {"category": "none", "confidence": 1},
        "visual_orientation": {"rotation_cw": 0, "confidence": 1, "ambiguous": False,
                               "evidence": ["faces_upright", "horizon_level"]},
    }
    connection.execute(
        "INSERT INTO photo_analysis(photo_id,schema_version,stage,provider,model,"
        "caption,side_caption,types_json,raw_json,score_kind,ranking_score,final_ranking_score,created_at) "
        "VALUES (?,6,'single','ci-fixture','synthetic','CI source',?,'[]',?,'semantic',99,99,?)",
        (LAN_SOURCE_ID, result["side_caption"], json.dumps(result), now),
    )
    return digest


def _now() -> str:
    """Use a realistic timestamp; age alone never settles an unknown charge.

    Separate regression coverage ages reservations past the reconciliation
    window and verifies that unaccounted requests retain their budget.
    """

    return datetime.now(timezone.utc).isoformat()


def seed(connection):
    connection.execute("INSERT INTO libraries(id,name,root_path,created_at,updated_at) VALUES ('ci-paid-library','CI paid','/photos','2026-09-16','2026-09-16')")
    connection.execute("INSERT INTO photos(id,library_id,relative_path,sha256,status,created_at,updated_at) VALUES ('ci-paid-photo','ci-paid-library','ci-paid.jpg','ci-paid-sha','analyzed','2026-09-16','2026-09-16')")
    connection.execute("INSERT INTO photo_analysis(photo_id,schema_version,stage,caption,types_json,raw_json,created_at) VALUES ('ci-paid-photo',5,'single','CI persisted analysis','[]','{}','2026-09-16')")
    connection.execute(
        "INSERT INTO billable_operations(id,content_sha256,request_fingerprint,state,response_json,created_at,updated_at) "
        "VALUES ('ci-paid-operation','ci-paid-sha','ci-paid-fingerprint','response','{}','2026-09-16','2026-09-16')"
    )
    connection.execute(
        "INSERT INTO api_usage(provider,model,request_type,estimated_cost,actual_cost,started_at,status,cost_source,operation_id,usage_complete) "
        "VALUES ('ci-paid','ci-model','ci-persistence',NULL,NULL,'2026-09-16','failed','unknown','ci-paid-operation',0)"
    )
    connection.execute(
        "INSERT INTO budget_reservations(id,amount,state,created_at) VALUES ('ci-paid-reservation',0.01,'active',?)",
        (_now(),),
    )
    connection.execute(
        "INSERT INTO provider_quota_state(scope,failures,circuit_until) VALUES ('ci-paid-provider',2,2000000000)"
    )
    connection.commit()


def verify(connection):
    assert connection.execute("SELECT caption FROM photo_analysis WHERE photo_id='ci-paid-photo'").fetchone() == ("CI persisted analysis",)
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert connection.execute("SELECT state,response_json FROM billable_operations WHERE id='ci-paid-operation'").fetchone() == ("response", "{}")
    assert connection.execute("SELECT operation_id,estimated_cost,actual_cost,usage_complete FROM api_usage WHERE request_type='ci-persistence'").fetchone() == ("ci-paid-operation", None, None, 0)
    assert connection.execute("SELECT amount,state FROM budget_reservations WHERE id='ci-paid-reservation'").fetchone() == (0.01, "active")
    assert connection.execute("SELECT failures,circuit_until FROM provider_quota_state WHERE scope='ci-paid-provider'").fetchone() == (2, 2000000000)


if __name__ == "__main__":
    with sqlite3.connect("/data/inktime.db") as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        if sys.argv[1] == "seed":
            seed(connection)
        verify(connection)
        print(json.dumps({"paid_state": "preserved"}))
