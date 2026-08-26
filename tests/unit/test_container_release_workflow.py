from pathlib import Path
import re

import yaml

from scripts.ci.run_selected_suites import RUNNER_SUITE_TEST_PATHS


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_PATH = ROOT / ".github/workflows/publish-container.yml"
COMPOSE_PATH = ROOT / "docker-compose.nas.yml"
UPDATER_PATH = ROOT / "scripts/update_nas.sh"
RECOVERY_PATH = ROOT / "scripts/create_update_recovery.py"
CONTRACT_PATH = ROOT / "nas-deployment-contract.version"
CI_WORKFLOW_PATH = ROOT / ".github/workflows/ci.yml"
NAS_E2E_PATH = ROOT / "scripts/ci/nas_update_e2e.sh"


def _workflow() -> dict[str, object]:
    loaded = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def test_release_workflow_is_tag_only_and_uses_least_required_permissions():
    workflow = _workflow()

    assert workflow["on"] == {"push": {"tags": ["v*"]}}
    assert workflow["permissions"] == {
        "contents": "read",
        "checks": "read",
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

    exact_head_gate = next(
        step for step in steps if step.get("name") == "Require successful exact-commit CI gates"
    )
    assert exact_head_gate["env"]["RELEASE_COMMIT"] == "${{ steps.release.outputs.commit }}"
    assert "repos/${GITHUB_REPOSITORY}/commits/${RELEASE_COMMIT}/check-runs" in exact_head_gate[
        "run"
    ]
    assert 'required_checks=("Repository gate" "Container security gate")' in exact_head_gate[
        "run"
    ]
    assert "completed:success" in exact_head_gate["run"]
    assert steps.index(exact_head_gate) < steps.index(
        next(step for step in steps if step.get("name") == "Set up QEMU")
    )

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
    labels = metadata["with"]["labels"]
    assert "io.inktime.nas-deployment-contract=${{ steps.release.outputs.deployment_contract }}" in labels
    assert CONTRACT_PATH.read_text(encoding="utf-8").strip() == "3"


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
        assert service["user"] == "10001:10001"
        volumes = {mount["target"]: mount for mount in service["volumes"]}
        assert volumes["/data"] == {
            "type": "bind",
            "source": "${INKTIME_DATA_PATH:?請在 .env.nas 設定 INKTIME_DATA_PATH}",
            "target": "/data",
            "bind": {"create_host_path": False},
        }
        assert volumes["/photos"] == {
            "type": "bind",
            "source": "${INKTIME_PHOTO_PATH:?請在 .env.nas 設定 INKTIME_PHOTO_PATH}",
            "target": "/photos",
            "read_only": True,
            "bind": {"create_host_path": False},
        }
        environment = service["environment"]
        assert environment["INKTIME_DATA_DIR"] == "/data"
        assert environment["INKTIME_DATABASE"] == "/data/inktime.db"
        assert environment["INKTIME_RELEASE_DIR"] == "/data/releases"
        assert environment["INKTIME_BACKUP_DIR"] == "/data/backups"
        assert environment["INKTIME_CACHE_DIR"] == "/data/cache"

    compose_text = COMPOSE_PATH.read_text(encoding="utf-8")
    env_example = (ROOT / ".env.nas.example").read_text(encoding="utf-8")
    assert "INKTIME_IMAGE_TAG:-latest" not in compose_text
    assert "INKTIME_IMAGE_TAG=latest" not in env_example
    assert "INKTIME_ALLOW_MUTABLE_IMAGE_TAG=0" in env_example


def test_nas_updater_pulls_before_no_build_recreate_and_never_deletes_volumes():
    updater = UPDATER_PATH.read_text(encoding="utf-8")

    assert "compose pull" in updater
    assert "compose up -d --no-build --remove-orphans --wait" in updater
    assert updater.index("compose pull") < updater.index("compose up -d --no-build")
    assert "docker image inspect" in updater
    assert "io.inktime.nas-deployment-contract" in updater
    assert "--initialize" in updater
    assert "--accept-path-change" in updater
    assert ".inktime-deployment-root" in updater
    assert ".inktime-update.lock" in updater
    assert "flock -n" in updater
    assert "realpath -e" in updater
    assert "paths_overlap" in updater
    assert "INKTIME_ALLOW_MUTABLE_IMAGE_TAG=1" in updater
    assert "create_update_recovery.py" in updater
    assert updater.index("create_update_recovery.py") < updater.index("compose up -d --no-build")
    assert "INKTIME_IMAGE_TAG" in updater
    assert "docker compose" in updater
    assert "unset \"$compose_environment_name\"" in updater
    assert "export INKTIME_IMAGE_TAG=\"$release_tag\"" in updater
    assert "compose config --environment" in updater
    assert "compose config --images" in updater
    assert "verify_compose_environment_value INKTIME_DATA_PATH" in updater
    assert "verify_compose_environment_value INKTIME_PHOTO_PATH" in updater
    assert "NAS-UPDATE-IDENTITY-001" in updater
    assert 'target=/source,readonly' in updater
    assert 'source=${recovery_dir},target=/recovery' in updater
    assert 'source=${data_path},target=/data' not in updater
    assert 'docker exec -i --user 10001:10001 "$web_container"' in updater
    assert "source_connection.backup(target_connection)" in updater
    assert "--staged-snapshot /recovery/.source-snapshot.sqlite3" in updater
    assert "--network none" in updater
    assert "--read-only" in updater
    assert "--security-opt no-new-privileges" in updater
    assert "--cap-drop ALL" in updater
    assert "down -v" not in updater
    assert "docker volume rm" not in updater
    assert "down " not in updater
    assert "rm -rf" not in updater
    assert ".env.nas" in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()

    contract_path = "tests/unit/test_container_release_workflow.py"
    assert contract_path in RUNNER_SUITE_TEST_PATHS["ci_planner_contracts"]
    assert contract_path in RUNNER_SUITE_TEST_PATHS["ci_routing_contracts"]


def test_update_recovery_validates_staged_snapshot_and_excludes_payload_copies():
    recovery = RECOVERY_PATH.read_text(encoding="utf-8")

    assert 'if __package__ in {None, ""}' in recovery
    assert "sys.path.insert(0, str(Path(__file__).resolve().parents[1]))" in recovery
    assert "BackupService" in recovery
    assert 'f"{path.as_uri()}?mode=ro"' in recovery
    assert "PRAGMA query_only = ON" in recovery
    assert "source_connection.backup(target_connection)" not in recovery
    assert "BackupService(Database(staged_snapshot), destination_root)" in recovery
    assert "--source-root" in recovery
    assert "--destination-root" in recovery
    assert "--staged-snapshot" in recovery
    assert "NAS-RECOVERY-SOURCE-RO-001" in recovery
    assert "NAS-RECOVERY-DEST-RW-001" in recovery
    assert "create(include_secrets=True)" in recovery
    assert "service.validate(archive)" in recovery
    assert "session.key" in recovery
    assert "0o600" in recovery
    assert "previous_image_digest" in recovery
    assert "database_schema_version" in recovery
    assert "backup_archive_sha256" in recovery
    assert "copytree" not in recovery
    assert "photo" not in recovery.casefold()
    assert "release" not in recovery.casefold()


def test_hosted_ci_runs_real_nas_updater_a_to_b_and_negative_safety_cases():
    workflow = yaml.safe_load(CI_WORKFLOW_PATH.read_text(encoding="utf-8"))
    job = workflow["jobs"]["nas-pull-only-update-e2e"]
    script = NAS_E2E_PATH.read_text(encoding="utf-8")

    assert job["needs"] == "changes"
    assert "run_nas_update_e2e" in job["if"]
    assert "registry:2.8.3@sha256:" in job["services"]["registry"]["image"]
    assert job["steps"][-1]["run"] == "scripts/ci/nas_update_e2e.sh"
    assert "build_push v1.0.0-ci-a" in script
    assert "build_push v1.0.0-ci-b" in script
    assert "run_updater --initialize v1.0.0-ci-a" in script
    assert "run_updater v1.0.0-ci-b" in script
    assert "NAS-UPDATE-PATH-002" in script
    assert "NAS-UPDATE-PATH-006" in script
    assert "NAS-UPDATE-PATH-003" in script
    assert "NAS-UPDATE-MARKER-001" in script
    assert "NAS-UPDATE-CONTRACT-001" in script
    assert "NAS-UPDATE-TAG-001" in script
    assert "NAS-UPDATE-LOCK-002" in script
    assert "INKTIME_IMAGE_REPOSITORY=localhost:5000/ambient-wrong" in script
    assert "NAS-RECOVERY-SOURCE-RO-001" in script
    assert "v1.0.0-ci-recovery-fail" in script
    assert "container_ids_before" in script
    assert "photo_state_before" in script
