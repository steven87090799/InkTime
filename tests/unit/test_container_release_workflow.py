from pathlib import Path
import re

import yaml

from scripts.ci.run_selected_suites import RUNNER_SUITE_TEST_PATHS


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_PATH = ROOT / ".github/workflows/publish-container.yml"
COMPOSE_PATH = ROOT / "docker-compose.nas.yml"
UPDATER_PATH = ROOT / "scripts/update_nas.sh"


def _workflow() -> dict[str, object]:
    loaded = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def test_release_workflow_is_tag_only_and_uses_least_required_permissions():
    workflow = _workflow()

    assert workflow["on"] == {"push": {"tags": ["v*"]}}
    assert workflow["permissions"] == {
        "contents": "read",
        "packages": "write",
        "attestations": "write",
        "id-token": "write",
    }
    assert workflow["env"]["IMAGE_NAME"] == "ghcr.io/steven87090799/inktime"
    assert workflow["concurrency"]["cancel-in-progress"] is False


def test_release_workflow_pins_actions_and_preserves_release_identity():
    workflow = _workflow()
    publish = workflow["jobs"]["publish"]
    steps = publish["steps"]
    action_uses = [step["uses"] for step in steps if "uses" in step]

    assert action_uses
    assert all(re.search(r"@[0-9a-f]{40}$", action) for action in action_uses)
    release = next(step for step in steps if step.get("id") == "release")
    assert "origin/main" in release["run"]
    assert "git merge-base --is-ancestor" in release["run"]
    assert "tagged_commit" in release["run"]
    assert "^v(0|[1-9][0-9]*)" in release["run"]

    immutable = next(
        step for step in steps if step.get("name") == "Refuse to overwrite an existing version tag"
    )
    assert "imagetools inspect" in immutable["run"]

    build = next(step for step in steps if step.get("id") == "push")
    assert build["with"]["platforms"] == "linux/amd64,linux/arm64"
    assert build["with"]["push"] is True
    assert "INKTIME_GIT_REVISION=${{ steps.release.outputs.commit }}" in build["with"][
        "build-args"
    ]
    assert "INKTIME_BUILD_TIME=${{ steps.release.outputs.build_time }}" in build["with"][
        "build-args"
    ]


def test_stable_latest_alias_and_prerelease_boundary_are_explicit():
    workflow = _workflow()
    steps = workflow["jobs"]["publish"]["steps"]
    metadata = next(step for step in steps if step.get("id") == "metadata")
    tags = metadata["with"]["tags"]

    assert "type=raw,value=${{ steps.release.outputs.tag }}" in tags
    assert "type=sha,prefix=sha-" in tags
    assert "value=latest" in tags
    assert "steps.release.outputs.stable == 'true'" in tags


def test_nas_compose_is_pull_only_and_keeps_data_and_photos_external():
    compose = yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))
    services = compose["services"]

    assert set(services) == {"inktime-web", "inktime-worker", "inktime-scheduler"}
    for service in services.values():
        assert "build" not in service
        assert service["image"].startswith("${INKTIME_IMAGE_REPOSITORY")
        assert service["pull_policy"] == "always"
        assert service["restart"] == "unless-stopped"
        assert service["read_only"] is True
        assert "${INKTIME_DATA_PATH:?請在 .env.nas 設定 INKTIME_DATA_PATH}:/data" in service[
            "volumes"
        ]
        assert (
            "${INKTIME_PHOTO_PATH:?請在 .env.nas 設定 INKTIME_PHOTO_PATH}:/photos:ro"
            in service["volumes"]
        )


def test_nas_updater_pulls_before_no_build_recreate_and_never_deletes_volumes():
    updater = UPDATER_PATH.read_text(encoding="utf-8")

    assert "compose pull" in updater
    assert "compose up -d --no-build --remove-orphans --wait" in updater
    assert "INKTIME_IMAGE_TAG" in updater
    assert "docker compose" in updater
    assert "down -v" not in updater
    assert "docker volume rm" not in updater
    assert ".env.nas" in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()

    contract_path = "tests/unit/test_container_release_workflow.py"
    assert contract_path in RUNNER_SUITE_TEST_PATHS["ci_planner_contracts"]
    assert contract_path in RUNNER_SUITE_TEST_PATHS["ci_routing_contracts"]
