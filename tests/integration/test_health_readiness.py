def test_readiness_does_not_scan_database_integrity(app, monkeypatch):
    database = app.extensions["inktime_database"]

    def forbidden(*args, **kwargs):
        raise AssertionError("Readiness must not run an integrity scan")

    monkeypatch.setattr(database, "integrity_check", forbidden)
    with app.test_client() as client:
        for _ in range(3):
            response = client.get("/health/ready")
            assert response.status_code in {200, 503}
            assert response.json["checks"]["database"] is True


def test_platform_health_reports_missing_or_stale_background_services(app):
    observability = app.extensions["inktime_observability_service"]
    database = app.extensions["inktime_database"]
    with app.test_client() as client:
        assert client.get("/health/web-ready").status_code == 200
        first = client.get("/health/ready")
        assert first.status_code == 503
        assert first.json["checks"]["worker"] is False
        assert first.json["checks"]["scheduler"] is False
        observability.heartbeat("worker")
        observability.heartbeat("scheduler")
        assert client.get("/health/ready").status_code == 200
        with database.transaction() as connection:
            connection.execute("UPDATE observability_state SET updated_at='2000-01-01T00:00:00+00:00' "
                               "WHERE key='heartbeat:scheduler'")
        stale = client.get("/health/ready")
        assert stale.status_code == 503
        assert stale.json["checks"]["worker"] is True
        assert stale.json["checks"]["scheduler"] is False
