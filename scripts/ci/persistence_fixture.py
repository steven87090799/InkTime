"""Deterministic paid-state fixture for real container persistence acceptance."""
import json
import sqlite3
import sys


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
        "INSERT INTO budget_reservations(id,amount,state,created_at) VALUES ('ci-paid-reservation',0.01,'active','2026-09-16')"
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
