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
