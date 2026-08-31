from inktime.app.web.help_content import PAGE_GUIDES, page_guide_for_endpoint


def test_every_base_template_management_page_has_specific_help_content():
    expected = {
        "dashboard.dashboard",
        "settings.settings_page",
        "settings.providers_page",
        "settings.costs_page",
        "photos.photos_page",
        "photos.excluded_photos_page",
        "photos.photo_detail",
        "review.review_page",
        "jobs.jobs_page",
        "jobs.job_detail",
        "analysis_batches.batches_page",
        "analysis_batches.batch_detail_page",
        "ai_traces.trace_list_page",
        "ai_traces.trace_detail_page",
        "scoring.scoring_page",
        "rendering.rendering_page",
        "rendering.simulator_page",
        "devices.devices_page",
        "devices.energy_page",
        "operations.activity_page",
        "operations.diagnostics_page",
        "operations.errors_page",
        "operations.backups_page",
        "operations.maintenance_page",
        "operations.schedules_page",
        "resilience.decision_traces_page",
        "resilience.feedback_page",
        "resilience.shadow_page",
        "resilience.queues_page",
        "resilience.retention_page",
        "resilience.rollouts_page",
        "auth.setup",
        "auth.login",
        "auth.change_password",
    }
    assert expected <= PAGE_GUIDES.keys()

    for endpoint in expected:
        guide = page_guide_for_endpoint(endpoint)
        assert guide["title"]
        assert guide["purpose"]
        assert guide["steps"]
        assert guide["tips"]


def test_unknown_page_gets_a_safe_general_guide():
    guide = page_guide_for_endpoint("unknown.page")
    assert guide["title"] == "本頁使用提示"
    assert "預覽" in guide["tips"][0]
