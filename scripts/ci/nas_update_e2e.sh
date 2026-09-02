#!/usr/bin/env bash
set -euo pipefail

repository_root=$(git rev-parse --show-toplevel)
ci_root="${repository_root}/.ci-nas"
data_path="${ci_root}/data"
photo_path="${ci_root}/photos"
env_file="${ci_root}/nas.env"
compose_file="${repository_root}/docker-compose.nas.yml"
updater="${repository_root}/scripts/update_nas.sh"
registry=localhost:5000/inktime
contract=$(tr -d '[:space:]' < "${repository_root}/nas-deployment-contract.version")

compose() (
  local tag=${1:-v1.0.0-ci-a}
  shift
  local environment_name
  while IFS= read -r environment_name; do
    unset "$environment_name"
  done < <(env | sed -n 's/^\(INKTIME_[A-Za-z0-9_]*\)=.*/\1/p')
  export INKTIME_IMAGE_TAG="$tag"
  docker compose --env-file "$env_file" -f "$compose_file" "$@"
)

cleanup() {
  if [ -f "$env_file" ]; then
    compose v1.0.0-ci-b down --remove-orphans || true
  fi
  if [[ "$ci_root" == "${repository_root}/.ci-nas" ]]; then
    sudo rm -rf -- "$ci_root"
  fi
}
trap cleanup EXIT

mkdir -p "$data_path" "$photo_path"
chmod 777 "$data_path" "$photo_path"
mkdir -p "${photo_path}/nested"
chmod 777 "${photo_path}/nested"
printf 'original-photo-one\n' > "${photo_path}/one.jpg"
printf 'original-photo-two\n' > "${photo_path}/nested/two.jpg"
chmod 666 "${photo_path}/one.jpg" "${photo_path}/nested/two.jpg"

write_env() {
  local configured_data=$1
  local configured_photos=$2
  cat > "$env_file" <<EOF
INKTIME_IMAGE_REPOSITORY=${registry}
INKTIME_ALLOW_MUTABLE_IMAGE_TAG=0
INKTIME_DATA_PATH=${configured_data}
INKTIME_PHOTO_PATH=${configured_photos}
INKTIME_PUBLIC_URL=http://127.0.0.1:8765
INKTIME_COOKIE_SECURE=0
INKTIME_ALLOW_INSECURE_HTTP=1
INKTIME_PROXY_TRUST=0
INKTIME_ALLOW_UNSAFE_NETWORK_DATABASE=0
INKTIME_BIND_ADDRESS=127.0.0.1
INKTIME_PORT=8765
EOF
  chmod 600 "$env_file"
}
write_env "$data_path" "$photo_path"

build_push() {
  local tag=$1
  local image_contract=$2
  docker build \
    --build-arg "INKTIME_GIT_REVISION=${tag}" \
    --build-arg "INKTIME_BUILD_TIME=2026-01-01T00:00:00Z" \
    --label "io.inktime.nas-deployment-contract=${image_contract}" \
    --tag "${registry}:${tag}" "$repository_root"
  docker push "${registry}:${tag}"
}

build_push v1.0.0-ci-a "$contract"
build_push v1.0.0-ci-b "$contract"
build_push v1.0.0-ci-contract-mismatch 999

docker build --tag "${registry}:v1.0.0-ci-recovery-fail" - <<EOF
FROM ${registry}:v1.0.0-ci-b
USER root
RUN printf '%s\n' '#!/usr/bin/env python3' 'raise RuntimeError("CI forced recovery failure")' > /app/scripts/create_update_recovery.py
USER 10001:10001
EOF
docker push "${registry}:v1.0.0-ci-recovery-fail"

export INKTIME_DATA_PATH="$photo_path"
export INKTIME_PHOTO_PATH="$data_path"
export INKTIME_IMAGE_REPOSITORY=localhost:5000/ambient-wrong
export INKTIME_IMAGE_TAG=latest

run_updater() {
  sudo env \
    INKTIME_DATA_PATH="$INKTIME_DATA_PATH" \
    INKTIME_PHOTO_PATH="$INKTIME_PHOTO_PATH" \
    INKTIME_IMAGE_REPOSITORY="$INKTIME_IMAGE_REPOSITORY" \
    INKTIME_IMAGE_TAG="$INKTIME_IMAGE_TAG" \
    "$updater" "$@"
}

run_updater --initialize v1.0.0-ci-a "$env_file"

assert_runtime_contract() {
  local tag=$1
  local service container mount_json
  for service in inktime-web inktime-worker inktime-scheduler; do
    container=$(compose "$tag" ps -q "$service")
    test -n "$container"
    test "$(docker inspect --format '{{.Config.User}}' "$container")" = "10001:10001"
    test "$(docker inspect --format '{{.Config.Image}}' "$container")" = "${registry}:${tag}"
    test "$(docker inspect --format '{{.HostConfig.ReadonlyRootfs}}' "$container")" = true
    docker inspect --format '{{json .HostConfig.SecurityOpt}}' "$container" | grep -q 'no-new-privileges'
    mount_json=$(docker inspect --format '{{json .Mounts}}' "$container")
    jq -e --arg source "$data_path" 'any(.[]; .Destination == "/data" and .Source == $source and .RW == true)' <<<"$mount_json"
    jq -e --arg source "$photo_path" 'any(.[]; .Destination == "/photos" and .Source == $source and .RW == false)' <<<"$mount_json"
    test "$(compose "$tag" exec -T "$service" id -u)" = 10001
    for mutation in \
      'printf mutation > /photos/ci-created' \
      'printf changed > /photos/one.jpg' \
      'mv /photos/nested/two.jpg /photos/nested/two-renamed.jpg' \
      'rm /photos/one.jpg' \
      'chmod 600 /photos/one.jpg' \
      'chown 10001:10001 /photos/one.jpg'; do
      if compose "$tag" exec -T "$service" sh -c "$mutation"; then
        echo "NAS-E2E-PHOTO-RO-001 ${service} succeeded: ${mutation}" >&2
        exit 1
      fi
    done
  done
}

assert_runtime_contract v1.0.0-ci-a
photo_state() {
  sha256sum "${photo_path}/one.jpg" "${photo_path}/nested/two.jpg"
  stat -c '%n %s' "${photo_path}/one.jpg" "${photo_path}/nested/two.jpg"
  od -An -tx1 "${photo_path}/one.jpg" "${photo_path}/nested/two.jpg"
}
photo_state_before=$(photo_state)

compose v1.0.0-ci-a exec -T inktime-web python - <<'PY'
from datetime import datetime, timezone
from pathlib import Path

from inktime.app.db import Database
from inktime.app.repositories.auth import AuthRepository
from inktime.app.repositories.devices import DeviceRepository
from inktime.app.repositories.photos import PhotoRepository
from inktime.app.repositories.schedules import ScheduledTaskRepository
from inktime.app.repositories.settings import SecretStore, SettingsRepository
from inktime.app.services.backups import BackupService

database = Database(Path("/data/inktime.db"))
admin_id = AuthRepository(database).create_initial_administrator("ci-admin", "CI-only-password-2026!")
device_id, _token = DeviceRepository(database, "ci-device-pepper").create("ci-device")
settings = SettingsRepository(database)
settings.ensure_defaults()
settings.update_many(
    {"analysis.execution_mode": "local_only"},
    changed_by=admin_id,
    source_ip="127.0.0.1",
    reason="NAS E2E durable setting",
)
ScheduledTaskRepository(database).ensure_defaults()
photos = PhotoRepository(database)
library_id = photos.ensure_library("CI NAS photos", Path("/photos"))
now = datetime.now(timezone.utc).isoformat()
with database.transaction(operation="nas_e2e.seed_photo") as connection:
    connection.execute(
        """INSERT INTO photos(
            id,library_id,relative_path,file_size,modified_time,sha256,width,height,format,
            status,created_at,updated_at,lifecycle_status,metadata_status,local_features_status
        ) VALUES (?,?,?,?,?,?,?,?,?,'discovered',?,?, 'active','complete','complete')""",
        ("ci-photo", library_id, "one.jpg", 19, 1.0, "ci-photo-sha", 1, 1, "JPEG", now, now),
    )
photos.save_analysis(
    "ci-photo", None, "final", "ci-provider", "ci-model",
    {"schema_version": 4, "caption": "NAS persistent analysis for the stored test photo and its visible scene.",
     "types": ["日常"], "memory_score": 88, "visual_score": 77,
     "special_level": 0, "special_codes": [], "people_count": 0,
     "side_caption": "相框留住此刻的光影。", "subject_position": "center", "text_safe_area": "bottom_right",
     "content_filter": {code: {"detected": False, "confidence": 1} for code in ("sexualized_content", "explicit_nudity", "female_glamour_portrait")},
     "visual_orientation": {"rotation_cw": None, "confidence": 0, "ambiguous": True, "evidence": ["insufficient_visual_cues"]}},
    '{"ci":true}',
)
session_secret = Path("/data/session.key").read_text(encoding="utf-8")
SecretStore(database, session_secret).set("ci_recovery_marker", "ci-not-a-credential", admin_id)
release = Path("/data/releases/ci-release/frame.bin")
release.parent.mkdir(parents=True, exist_ok=True)
release.write_bytes(b"INKTIME-NAS-RENDERED-RELEASE-V1\n")
BackupService(database, Path("/data/backups")).create(include_secrets=False)
print(device_id)
PY

snapshot() {
  compose "$1" exec -T inktime-web python - <<'PY'
import json
import sqlite3
from pathlib import Path

connection = sqlite3.connect("/data/inktime.db")
tables = ["users", "devices", "settings", "scheduled_tasks", "libraries", "photos", "photo_analysis", "secrets"]
result = {table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in tables}
result["admin"] = connection.execute("SELECT username FROM users WHERE username='ci-admin'").fetchone()[0]
result["device"] = connection.execute("SELECT name FROM devices WHERE name='ci-device'").fetchone()[0]
result["caption"] = connection.execute("SELECT caption FROM photo_analysis WHERE photo_id='ci-photo'").fetchone()[0]
result["setting"] = connection.execute("SELECT value_json FROM settings WHERE key='analysis.execution_mode'").fetchone()[0]
result["release_sha256"] = __import__("hashlib").sha256(Path("/data/releases/ci-release/frame.bin").read_bytes()).hexdigest()
print(json.dumps(result, sort_keys=True))
PY
}

state_before=$(snapshot v1.0.0-ci-a)
session_hash_before=$(compose v1.0.0-ci-a exec -T inktime-web sha256sum /data/session.key)
marker_before=$(sudo cat "${data_path}/.inktime-deployment-root")
container_ids_before=$(compose v1.0.0-ci-a ps -q | sort)

assert_unchanged_after_failure() {
  local expected_code=$1
  shift
  local output
  if output=$(run_updater "$@" "$env_file" 2>&1); then
    echo "NAS-E2E-NEGATIVE-001 expected failure ${expected_code}" >&2
    exit 1
  fi
  grep -q "$expected_code" <<<"$output"
  test "$container_ids_before" = "$(compose v1.0.0-ci-a ps -q | sort)"
  test "$state_before" = "$(snapshot v1.0.0-ci-a)"
  test "$session_hash_before" = "$(compose v1.0.0-ci-a exec -T inktime-web sha256sum /data/session.key)"
  test "$photo_state_before" = "$(photo_state)"
}

rw_recovery_source="${ci_root}/rw-recovery-source"
rw_recovery_destination="${ci_root}/rw-recovery-destination"
mkdir -p "$rw_recovery_source" "$rw_recovery_destination"
touch "${rw_recovery_source}/inktime.db"
touch "${rw_recovery_destination}/.source-snapshot.sqlite3"
printf 'ci-recovery-session\n' > "${rw_recovery_source}/session.key"
sudo chown -R 10001:10001 "$rw_recovery_source" "$rw_recovery_destination"
sudo chmod 600 "${rw_recovery_source}/session.key"
if rw_recovery_output=$(docker run --rm --read-only --network none --user 10001:10001 \
  --security-opt no-new-privileges --cap-drop ALL \
  --mount "type=bind,source=${rw_recovery_source},target=/source" \
  --mount "type=bind,source=${rw_recovery_destination},target=/recovery" \
  --tmpfs /tmp:size=64m,mode=1777 \
  "${registry}:v1.0.0-ci-b" python scripts/create_update_recovery.py \
  --source-root /source \
  --destination-root /recovery \
  --staged-snapshot /recovery/.source-snapshot.sqlite3 \
  --previous-image-ref "${registry}:v1.0.0-ci-a" \
  --previous-image-digest sha256:ci \
  --target-image-ref "${registry}:v1.0.0-ci-b" \
  --deployment-contract "$contract" 2>&1); then
  echo "NAS-E2E-RECOVERY-RO-001 writable recovery source was accepted" >&2
  exit 1
fi
grep -q NAS-RECOVERY-SOURCE-RO-001 <<<"$rw_recovery_output"
test "$photo_state_before" = "$(photo_state)"

write_env "${ci_root}/missing-data" "$photo_path"
assert_unchanged_after_failure NAS-UPDATE-PATH-002 v1.0.0-ci-b
write_env "$data_path" "${ci_root}/missing-photos"
assert_unchanged_after_failure NAS-UPDATE-PATH-002 v1.0.0-ci-b
write_env "$data_path" "$data_path"
assert_unchanged_after_failure NAS-UPDATE-PATH-006 v1.0.0-ci-b
mkdir -p "${data_path}/nested-photos"
write_env "$data_path" "${data_path}/nested-photos"
assert_unchanged_after_failure NAS-UPDATE-PATH-006 v1.0.0-ci-b
mkdir -p "${photo_path}/nested-data"
write_env "${photo_path}/nested-data" "$photo_path"
assert_unchanged_after_failure NAS-UPDATE-PATH-006 v1.0.0-ci-b
ln -s "$data_path" "${ci_root}/photo-alias"
write_env "$data_path" "${ci_root}/photo-alias"
assert_unchanged_after_failure NAS-UPDATE-PATH-003 v1.0.0-ci-b
write_env "$data_path" "$photo_path"
mv "${data_path}/.inktime-deployment-root" "${data_path}/.inktime-deployment-root.held"
assert_unchanged_after_failure NAS-UPDATE-MARKER-001 v1.0.0-ci-b
mv "${data_path}/.inktime-deployment-root.held" "${data_path}/.inktime-deployment-root"
assert_unchanged_after_failure NAS-UPDATE-TAG-001 latest
assert_unchanged_after_failure NAS-UPDATE-CONTRACT-001 v1.0.0-ci-contract-mismatch
recovery_dirs_before=$(sudo find "${data_path}/backups" -maxdepth 1 -type d -name 'update-recovery-*' | sort)
assert_unchanged_after_failure NAS-UPDATE-RECOVERY-001 v1.0.0-ci-recovery-fail
test "$recovery_dirs_before" = "$(sudo find "${data_path}/backups" -maxdepth 1 -type d -name 'update-recovery-*' | sort)"
sudo flock "${data_path}/.inktime-update.lock" -c 'sleep 10' &
lock_pid=$!
sleep 1
assert_unchanged_after_failure NAS-UPDATE-LOCK-002 v1.0.0-ci-b
wait "$lock_pid"

run_updater v1.0.0-ci-b "$env_file"
assert_runtime_contract v1.0.0-ci-b
test "$state_before" = "$(snapshot v1.0.0-ci-b)"
test "$session_hash_before" = "$(compose v1.0.0-ci-b exec -T inktime-web sha256sum /data/session.key)"
test "$marker_before" = "$(sudo cat "${data_path}/.inktime-deployment-root")"
test "$photo_state_before" = "$(photo_state)"

recovery_dir=$(sudo find "${data_path}/backups" -maxdepth 1 -type d -name 'update-recovery-*' | sort | tail -1)
test -n "$recovery_dir"
sudo test -f "${recovery_dir}/recovery-metadata.json"
test "$(sudo stat -c '%a' "${recovery_dir}/session.key")" = 600
sudo jq -e --argjson contract "$contract" '
  .nas_deployment_contract == $contract and
  .previous_image_ref != "none" and
  .previous_image_digest != "none" and
  .target_image_ref == "localhost:5000/inktime:v1.0.0-ci-b" and
  .source_mount == "read-only" and
  .destination_mount == "bounded-read-write" and
  .secrets_policy == "included" and
  .backup_scope.original_photos == false and
  .backup_scope.release_payloads == false
' "${recovery_dir}/recovery-metadata.json"
sudo python - "$recovery_dir" <<'PY'
from pathlib import Path
import json
import sys
import zipfile

root = Path(sys.argv[1])
metadata = json.loads((root / "recovery-metadata.json").read_text())
with zipfile.ZipFile(root / metadata["backup_archive"]) as bundle:
    assert set(bundle.namelist()) == {"inktime.sqlite3", "settings.json", "manifest.json"}
    manifest = json.loads(bundle.read("manifest.json"))
    assert manifest["secrets_policy"] == "included"
PY

echo "NAS-UPDATE-E2E-OK A=v1.0.0-ci-a B=v1.0.0-ci-b contract=${contract}"
