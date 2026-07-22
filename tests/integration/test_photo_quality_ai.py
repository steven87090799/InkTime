from __future__ import annotations

import json

from PIL import Image

from inktime.app.domain.photos import PhotoPreprocessor
from inktime.app.providers.base import ProviderResponse, Usage, VisionProvider
from inktime.app.workers.scanner import PhotoScanner
from tests.conftest import create_admin, login
from tests.unit.test_analysis_schema import valid_result


class CountingProvider(VisionProvider):
    name = "Counting Provider"

    def __init__(self, result=None):
        self.result = result or valid_result()
        self.analyze_calls = 0

    def analyze(self, **_kwargs):
        self.analyze_calls += 1
        return ProviderResponse(json.dumps(self.result, ensure_ascii=False), Usage(100, 20, 0))

    def repair_json(self, **_kwargs):
        raise AssertionError("valid JSON should not need repair")

    def submit_batch(self, requests, completion_window="24h"):
        return "batch"

    def poll_batch(self, batch_id):
        return {"status": "completed"}

    def cancel_batch(self, batch_id):
        return {"status": "cancelled"}

    def estimate_cost(self, model, usage):
        return (usage.input_tokens + usage.output_tokens) / 1_000_000

    def validate_config(self):
        return True, "ok"


def _scan(app, tmp_path, *, screenshot=False, duplicate=False):
    root = tmp_path / "photos"
    root.mkdir()
    filename = "螢幕快照.png" if screenshot else "photo.jpg"
    Image.effect_noise((900, 600), 90).convert("RGB").save(root / filename)
    if duplicate:
        (root / "copy.jpg").write_bytes((root / filename).read_bytes())
    PhotoScanner(
        app.extensions["inktime_photo_repository"],
        PhotoPreprocessor(),
        app.extensions["inktime_thumbnail_cache"],
    ).scan("測試照片", root, build_thumbnails=False)
    with app.extensions["inktime_database"].session() as connection:
        return [str(row[0]) for row in connection.execute("SELECT id FROM photos ORDER BY relative_path")]


def _setting(app, key, value):
    app.extensions["inktime_settings_repository"].update(
        key, value, changed_by="test", source_ip="127.0.0.1"
    )


def test_excluded_photo_is_shown_and_restore_is_selectable(client, app, tmp_path):
    user_id = create_admin(app)
    login(client)
    photo_id = _scan(app, tmp_path, screenshot=True)[0]

    page = client.get("/photos/excluded")
    assert page.status_code == 200
    assert "螢幕快照.png" in page.get_data(as_text=True)

    restored = app.extensions["inktime_photo_repository"].set_exclusion(
        photo_id, action="restore", changed_by=user_id
    )
    assert restored["eligible"] == 1
    assert photo_id in app.extensions["inktime_photo_repository"].eligible_photo_ids()


def test_manual_restore_does_not_immediately_reexclude(app, tmp_path):
    user_id = create_admin(app)
    photo_id = _scan(app, tmp_path, screenshot=True)[0]
    repository = app.extensions["inktime_photo_repository"]
    repository.set_exclusion(photo_id, action="restore", changed_by=user_id)
    unchanged = repository.set_exclusion(photo_id, action="reanalyze", changed_by=user_id)
    assert unchanged["exclusion_status"] == "manually_restored"
    assert unchanged["manual_override"] == 1


def test_ai_off_does_not_call_provider(app, tmp_path):
    photo_id = _scan(app, tmp_path)[0]
    _setting(app, "analysis.ai_mode", "off")
    provider = CountingProvider()
    result = app.extensions["inktime_analysis_service"].analyze_photo(
        photo_id=photo_id, job_id=None, provider=provider, strategy="high_quality", high_model="test"
    )
    assert result["stage"] == "local_fallback"
    assert provider.analyze_calls == 0


def test_ai_cache_hit_does_not_call_provider_twice(app, tmp_path):
    first_id, second_id = _scan(app, tmp_path, duplicate=True)
    _setting(app, "analysis.ai_mode", "eligible")
    first = CountingProvider()
    service = app.extensions["inktime_analysis_service"]
    service.analyze_photo(photo_id=first_id, job_id=None, provider=first, strategy="high_quality", high_model="test")
    second = CountingProvider()
    cached = service.analyze_photo(photo_id=second_id, job_id=None, provider=second, strategy="high_quality", high_model="test")
    assert cached["stage"] == "cache"
    assert second.analyze_calls == 0


def test_full_json_allows_missing_nonessential_fields_and_travel_bonus_is_independent(app, tmp_path):
    photo_id = _scan(app, tmp_path)[0]
    _setting(app, "analysis.ai_mode", "eligible")
    with app.extensions["inktime_database"].session() as connection:
        connection.execute("UPDATE photos SET gps_lat=22.6273,gps_lon=120.3014 WHERE id=?", (photo_id,))
    result = valid_result(schema_version=2)
    provider = CountingProvider(result)
    analyzed = app.extensions["inktime_analysis_service"].analyze_photo(
        photo_id=photo_id, job_id=None, provider=provider, strategy="high_quality", high_model="travel"
    )
    assert analyzed["analysis"]["memory_score"] == result["memory_score"]
    with app.extensions["inktime_database"].session() as connection:
        row = connection.execute(
            "SELECT memory_score,travel_bonus,base_ranking_score,final_ranking_score FROM photo_analysis WHERE photo_id=?",
            (photo_id,),
        ).fetchone()
    assert row["memory_score"] == result["memory_score"]
    assert row["travel_bonus"] > 0
    assert row["final_ranking_score"] == row["base_ranking_score"] + row["travel_bonus"]
