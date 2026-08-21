#!/bin/sh
set -eu

usage() {
  echo "Usage: $0 [--initialize|--accept-path-change] <vMAJOR.MINOR.PATCH[-prerelease]|latest> [env-file]" >&2
  exit 2
}

mode=update
case "${1:-}" in
  --initialize) mode=initialize; shift ;;
  --accept-path-change) mode=accept_path_change; shift ;;
esac
[ "$#" -ge 1 ] && [ "$#" -le 2 ] || usage
release_tag=$1

if [ "$release_tag" != latest ] && \
  ! printf '%s\n' "$release_tag" | grep -Eq '^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-[0-9A-Za-z-]+(\.[0-9A-Za-z-]+)*)?$'; then
  echo "NAS-UPDATE-001 invalid image tag: ${release_tag}" >&2
  usage
fi

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
project_dir=$(CDPATH= cd -- "${script_dir}/.." && pwd)
compose_file="${project_dir}/docker-compose.nas.yml"
contract_file="${project_dir}/nas-deployment-contract.version"
env_file=${2:-"${project_dir}/.env.nas"}

[ -f "$compose_file" ] || { echo "NAS-UPDATE-002 missing compose file: ${compose_file}" >&2; exit 2; }
[ -f "$env_file" ] || {
  echo "NAS-UPDATE-003 missing env file: ${env_file}" >&2
  echo "Copy .env.nas.example to .env.nas and replace every placeholder first." >&2
  exit 2
}
[ -f "$contract_file" ] || { echo "NAS-UPDATE-CONTRACT-001 missing deployment contract" >&2; exit 2; }
deployment_contract=$(tr -d '[:space:]' < "$contract_file")
case "$deployment_contract" in
  ''|*[!0-9]*) echo "NAS-UPDATE-CONTRACT-001 invalid deployment contract" >&2; exit 2 ;;
esac
if grep -Eq '(^|=)/CHANGE_ME|^INKTIME_PUBLIC_URL=https://inktime\.example\.com/?$' "$env_file"; then
  echo "NAS-UPDATE-004 .env.nas still contains deployment placeholders." >&2
  exit 2
fi

env_value() {
  env_name=$1
  env_default=${2:-}
  env_count=$(grep -Ec "^[[:space:]]*${env_name}=" "$env_file" || true)
  [ "$env_count" -le 1 ] || { echo "NAS-UPDATE-ENV-001 duplicate ${env_name}" >&2; exit 2; }
  if [ "$env_count" -eq 0 ]; then
    printf '%s\n' "$env_default"
    return
  fi
  env_result=$(sed -n "s/^[[:space:]]*${env_name}=//p" "$env_file")
  case "$env_result" in
    \"*\") env_result=${env_result#\"}; env_result=${env_result%\"} ;;
    \'*\') env_result=${env_result#\'}; env_result=${env_result%\'} ;;
  esac
  printf '%s\n' "$env_result"
}

allow_mutable=$(env_value INKTIME_ALLOW_MUTABLE_IMAGE_TAG 0)
if [ "$release_tag" = latest ] && [ "$allow_mutable" != 1 ]; then
  echo "NAS-UPDATE-TAG-001 latest is mutable; set INKTIME_ALLOW_MUTABLE_IMAGE_TAG=1 for an explicit opt-in" >&2
  exit 2
fi

validate_host_path() {
  path_name=$1
  raw_path=$2
  case "$raw_path" in
    ''|*[![:print:]]*|*=*|*,*|*'$'*|*'`'*)
      echo "NAS-UPDATE-PATH-001 ${path_name} contains unsupported characters" >&2; exit 2 ;;
    /*) ;;
    *) echo "NAS-UPDATE-PATH-001 ${path_name} must be absolute" >&2; exit 2 ;;
  esac
  [ -d "$raw_path" ] || { echo "NAS-UPDATE-PATH-002 ${path_name} must already exist as a directory" >&2; exit 2; }
  canonical_path=$(realpath -e -- "$raw_path") || {
    echo "NAS-UPDATE-PATH-002 ${path_name} cannot be canonicalized" >&2; exit 2
  }
  normalized_raw=${raw_path%/}
  [ -n "$normalized_raw" ] || normalized_raw=/
  [ "$normalized_raw" = "$canonical_path" ] || {
    echo "NAS-UPDATE-PATH-003 ${path_name} must be canonical and cannot use a symlink alias" >&2; exit 2
  }
  [ "$canonical_path" != / ] || { echo "NAS-UPDATE-PATH-003 ${path_name} cannot be /" >&2; exit 2; }
  printf '%s\n' "$canonical_path"
}

paths_overlap() {
  left=$1
  right=$2
  [ "$left" = "$right" ] && return 0
  case "$left" in "$right"/*) return 0 ;; esac
  case "$right" in "$left"/*) return 0 ;; esac
  return 1
}

data_path=$(validate_host_path INKTIME_DATA_PATH "$(env_value INKTIME_DATA_PATH)")
photo_path=$(validate_host_path INKTIME_PHOTO_PATH "$(env_value INKTIME_PHOTO_PATH)")
[ -w "$data_path" ] || { echo "NAS-UPDATE-PATH-004 INKTIME_DATA_PATH is not writable" >&2; exit 2; }
[ -r "$photo_path" ] || { echo "NAS-UPDATE-PATH-005 INKTIME_PHOTO_PATH is not readable" >&2; exit 2; }
if paths_overlap "$data_path" "$photo_path"; then
  echo "NAS-UPDATE-PATH-006 data and photo paths must be separate and cannot contain one another" >&2
  exit 2
fi

command -v flock >/dev/null 2>&1 || { echo "NAS-UPDATE-LOCK-001 host flock is required" >&2; exit 2; }
lock_file="${data_path}/.inktime-update.lock"
exec 9>"$lock_file"
if ! flock -n 9; then
  echo "NAS-UPDATE-LOCK-002 another InkTime update holds ${lock_file}" >&2
  exit 2
fi

marker="${data_path}/.inktime-deployment-root"
marker_value() {
  marker_name=$1
  marker_count=$(grep -Ec "^${marker_name}=" "$marker" || true)
  [ "$marker_count" -eq 1 ] || { echo "NAS-UPDATE-MARKER-002 invalid deployment marker" >&2; exit 2; }
  sed -n "s/^${marker_name}=//p" "$marker"
}

write_marker=0
case "$mode" in
  initialize)
    [ ! -e "$marker" ] || { echo "NAS-UPDATE-MARKER-003 deployment is already initialized" >&2; exit 2; }
    if [ ! -f "${data_path}/inktime.db" ] && find "$data_path" -mindepth 1 -maxdepth 1 ! -name '.inktime-update.lock' -print -quit | grep -q .; then
      echo "NAS-UPDATE-MARKER-004 refusing to initialize a non-empty unmanaged data directory" >&2
      exit 2
    fi
    write_marker=1
    ;;
  update|accept_path_change)
    [ -f "$marker" ] && [ ! -L "$marker" ] || {
      echo "NAS-UPDATE-MARKER-001 ordinary updates require ${marker}; run --initialize only after verifying the root" >&2
      exit 2
    }
    marker_version=$(marker_value marker_version)
    old_data_path=$(marker_value data_path)
    old_photo_path=$(marker_value photo_path)
    old_contract=$(marker_value deployment_contract)
    [ "$marker_version" = 1 ] || { echo "NAS-UPDATE-MARKER-002 unsupported marker version" >&2; exit 2; }
    if [ "$mode" = update ]; then
      [ "$old_data_path" = "$data_path" ] && [ "$old_photo_path" = "$photo_path" ] || {
        echo "NAS-UPDATE-MARKER-005 configured paths changed; review them and rerun with --accept-path-change" >&2
        exit 2
      }
      [ "$old_contract" = "$deployment_contract" ] || {
        echo "NAS-UPDATE-MARKER-006 deployment contract changed; synchronize deployment files and explicitly rerun with --accept-path-change" >&2
        exit 2
      }
    else
      echo "Accepting reviewed deployment identity change: data ${old_data_path} -> ${data_path}; photos ${old_photo_path} -> ${photo_path}; contract ${old_contract} -> ${deployment_contract}"
      write_marker=1
    fi
    [ "$old_contract" -le "$deployment_contract" ] 2>/dev/null || {
      echo "NAS-UPDATE-MARKER-002 marker contract is invalid or newer than this updater" >&2; exit 2
    }
    ;;
esac

repository=$(env_value INKTIME_IMAGE_REPOSITORY ghcr.io/steven87090799/inktime)
case "$repository" in
  ''|*[!A-Za-z0-9._:/-]*) echo "NAS-UPDATE-ENV-002 invalid image repository" >&2; exit 2 ;;
esac
target_image_ref="${repository}:${release_tag}"
compose() (
  compose_environment_names=$(env | sed -n 's/^\(INKTIME_[A-Za-z0-9_]*\)=.*/\1/p')
  for compose_environment_name in $compose_environment_names; do
    unset "$compose_environment_name"
  done
  export INKTIME_IMAGE_TAG="$release_tag"
  docker compose --env-file "$env_file" -f "$compose_file" "$@"
)

verify_compose_environment_value() {
  identity_name=$1
  identity_value=$2
  identity_count=$(printf '%s\n' "$resolved_compose_environment" | grep -Ec "^${identity_name}=" || true)
  [ "$identity_count" -eq 1 ] || {
    echo "NAS-UPDATE-IDENTITY-001 Compose resolved ${identity_name} an unexpected number of times" >&2
    exit 2
  }
  resolved_identity_value=$(
    printf '%s\n' "$resolved_compose_environment" | sed -n "s/^${identity_name}=//p"
  )
  [ "$resolved_identity_value" = "$identity_value" ] || {
    echo "NAS-UPDATE-IDENTITY-001 Compose ${identity_name} differs from the updater-validated value" >&2
    exit 2
  }
}

echo "Validating InkTime NAS deployment configuration..."
compose config --quiet
resolved_compose_environment=$(compose config --environment) || {
  echo "NAS-UPDATE-IDENTITY-001 cannot resolve Compose interpolation environment" >&2
  exit 2
}
verify_compose_environment_value INKTIME_DATA_PATH "$data_path"
verify_compose_environment_value INKTIME_PHOTO_PATH "$photo_path"
verify_compose_environment_value INKTIME_IMAGE_TAG "$release_tag"
resolved_compose_images=$(compose config --images | sort -u) || {
  echo "NAS-UPDATE-IDENTITY-001 cannot resolve Compose image identity" >&2
  exit 2
}
[ "$resolved_compose_images" = "$target_image_ref" ] || {
  echo "NAS-UPDATE-IDENTITY-001 Compose image differs from ${target_image_ref}" >&2
  exit 2
}
echo "Pulling deployment image ${target_image_ref}..."
compose pull

image_contract=$(docker image inspect --format '{{ index .Config.Labels "io.inktime.nas-deployment-contract" }}' "$target_image_ref") || {
  echo "NAS-UPDATE-CONTRACT-001 cannot inspect pulled image contract" >&2; exit 2
}
[ "$image_contract" = "$deployment_contract" ] || {
  echo "NAS-UPDATE-CONTRACT-001 image contract ${image_contract:-missing} does not match updater contract ${deployment_contract}" >&2
  exit 2
}

previous_image_ref=none
previous_image_digest=none
web_container=$(compose ps -q inktime-web 2>/dev/null || true)
if [ -n "$web_container" ]; then
  previous_image_ref=$(docker inspect --format '{{.Config.Image}}' "$web_container")
  previous_image_digest=$(docker inspect --format '{{.Image}}' "$web_container")
fi

recovery_point=none
if [ -f "${data_path}/inktime.db" ]; then
  backups_path="${data_path}/backups"
  [ -d "$backups_path" ] && [ ! -L "$backups_path" ] || {
    echo "NAS-UPDATE-RECOVERY-003 ${backups_path} must be an existing non-symlink directory" >&2
    exit 2
  }
  canonical_backups_path=$(realpath -e -- "$backups_path") || {
    echo "NAS-UPDATE-RECOVERY-003 cannot canonicalize the recovery parent" >&2
    exit 2
  }
  [ "$canonical_backups_path" = "$backups_path" ] || {
    echo "NAS-UPDATE-RECOVERY-003 recovery parent must be a direct directory below the validated data root" >&2
    exit 2
  }
  data_owner=$(stat -c '%u:%g' "$data_path") || {
    echo "NAS-UPDATE-RECOVERY-003 cannot inspect production data ownership" >&2
    exit 2
  }
  recovery_dir=$(mktemp -d "${backups_path}/update-recovery-XXXXXXXXXX") || {
    echo "NAS-UPDATE-RECOVERY-003 cannot create a bounded recovery destination" >&2
    exit 2
  }
  recovery_dir_created=1
  cleanup_new_recovery() {
    [ "$recovery_dir_created" -eq 1 ] || return 0
    [ -d "$recovery_dir" ] && [ ! -L "$recovery_dir" ] || return 1
    bounded_recovery_dir=$(realpath -e -- "$recovery_dir") || return 1
    [ "$bounded_recovery_dir" = "$recovery_dir" ] || return 1
    [ "$(dirname -- "$bounded_recovery_dir")" = "$backups_path" ] || return 1
    case "$(basename -- "$bounded_recovery_dir")" in update-recovery-*) ;; *) return 1 ;; esac
    find "$bounded_recovery_dir" -xdev -mindepth 1 -delete
    rmdir -- "$bounded_recovery_dir"
    recovery_dir_created=0
  }
  if find "$recovery_dir" -mindepth 1 -print -quit | grep -q . ||
    ! chown 10001:10001 "$recovery_dir" ||
    ! chmod 700 "$recovery_dir"; then
    cleanup_new_recovery || true
    echo "NAS-UPDATE-RECOVERY-003 cannot safely prepare recovery destination for UID 10001; data owner is ${data_owner}" >&2
    exit 2
  fi
  recovery_owner=$(stat -c '%u:%g' "$recovery_dir") || {
    cleanup_new_recovery || true
    echo "NAS-UPDATE-RECOVERY-003 cannot inspect recovery destination ownership" >&2
    exit 2
  }
  recovery_mode=$(stat -c '%a' "$recovery_dir") || {
    cleanup_new_recovery || true
    echo "NAS-UPDATE-RECOVERY-003 cannot inspect recovery destination mode" >&2
    exit 2
  }
  if [ "$recovery_owner" != 10001:10001 ] || [ "$recovery_mode" != 700 ]; then
    cleanup_new_recovery || true
    echo "NAS-UPDATE-RECOVERY-003 recovery destination ownership or mode is unsafe" >&2
    exit 2
  fi
  if [ -z "$web_container" ] ||
    [ "$(docker inspect --format '{{.State.Running}}' "$web_container" 2>/dev/null || true)" != true ]; then
    cleanup_new_recovery || true
    echo "NAS-UPDATE-RECOVERY-004 a running current inktime-web container is required to snapshot the live SQLite database" >&2
    exit 2
  fi
  recovery_name=$(basename -- "$recovery_dir")
  staged_snapshot_host="${recovery_dir}/.source-snapshot.sqlite3"
  staged_snapshot_container="/data/backups/${recovery_name}/.source-snapshot.sqlite3"
  echo "Creating verified online recovery point before container replacement..."
  if ! docker exec -i --user 10001:10001 "$web_container" \
    python - "$staged_snapshot_container" <<'PY'
from pathlib import Path
import os
import sqlite3
import stat
import sys

source = Path("/data/inktime.db")
destination = Path(sys.argv[1])
source_details = os.lstat(source)
if stat.S_ISLNK(source_details.st_mode) or not stat.S_ISREG(source_details.st_mode):
    raise RuntimeError("NAS-UPDATE-RECOVERY-DB-001 live SQLite source must be a regular file")
if destination.exists() or destination.is_symlink():
    raise RuntimeError("NAS-UPDATE-RECOVERY-DB-001 staged snapshot already exists")

source_connection = sqlite3.connect(f"{source.as_uri()}?mode=ro", uri=True)
target_connection = sqlite3.connect(destination)
try:
    source_connection.execute("PRAGMA query_only = ON")
    query_only = source_connection.execute("PRAGMA query_only").fetchone()
    if query_only is None or int(query_only[0]) != 1:
        raise RuntimeError("NAS-UPDATE-RECOVERY-DB-001 SQLite query_only verification failed")
    source_connection.backup(target_connection)
    integrity = target_connection.execute("PRAGMA integrity_check").fetchone()
    if integrity is None or str(integrity[0]) != "ok":
        raise RuntimeError("NAS-UPDATE-RECOVERY-DB-001 staged snapshot integrity check failed")
    target_connection.commit()
finally:
    target_connection.close()
    source_connection.close()

descriptor = os.open(destination, os.O_RDONLY)
try:
    os.fsync(descriptor)
finally:
    os.close(descriptor)
PY
  then
    cleanup_new_recovery || true
    echo "NAS-UPDATE-RECOVERY-001 live SQLite snapshot failed; existing containers and data were not replaced" >&2
    exit 2
  fi
  if [ ! -f "$staged_snapshot_host" ] || [ -L "$staged_snapshot_host" ]; then
    cleanup_new_recovery || true
    echo "NAS-UPDATE-RECOVERY-001 live SQLite snapshot was not created safely; existing containers and data were not replaced" >&2
    exit 2
  fi
  recovery_output=$(docker run --rm --read-only --network none --user 10001:10001 \
    --security-opt no-new-privileges --cap-drop ALL \
    --mount "type=bind,source=${data_path},target=/source,readonly" \
    --mount "type=bind,source=${recovery_dir},target=/recovery" \
    --tmpfs /tmp:size=64m,mode=1777 \
    "$target_image_ref" python scripts/create_update_recovery.py \
    --source-root /source \
    --destination-root /recovery \
    --staged-snapshot /recovery/.source-snapshot.sqlite3 \
    --previous-image-ref "$previous_image_ref" \
    --previous-image-digest "$previous_image_digest" \
    --target-image-ref "$target_image_ref" \
    --deployment-contract "$deployment_contract") || {
      cleanup_new_recovery || true
      echo "NAS-UPDATE-RECOVERY-001 recovery point failed; existing containers and data were not replaced" >&2
      exit 2
    }
  recovery_container_path=$(printf '%s\n' "$recovery_output" | sed -n 's/^RECOVERY_POINT=//p')
  [ "$recovery_container_path" = /recovery ] || {
    cleanup_new_recovery || true
    echo "NAS-UPDATE-RECOVERY-002 recovery tool returned an invalid path" >&2
    exit 2
  }
  recovery_dir_created=0
  recovery_point=$recovery_dir
fi

if [ "$write_marker" -eq 1 ]; then
  marker_tmp="${data_path}/.inktime-deployment-root.new.$$"
  umask 077
  {
    echo "marker_version=1"
    echo "data_path=${data_path}"
    echo "photo_path=${photo_path}"
    echo "deployment_contract=${deployment_contract}"
  } > "$marker_tmp"
  chmod 600 "$marker_tmp"
  mv -f "$marker_tmp" "$marker"
fi

echo "Recreating InkTime services; data and recovery point remain on the host if health checks fail..."
if ! compose up -d --no-build --remove-orphans --wait --wait-timeout "${INKTIME_UPDATE_TIMEOUT_SECONDS:-180}"; then
  echo "NAS-UPDATE-HEALTH-001 deployment did not become healthy; data=${data_path} recovery=${recovery_point} target=${target_image_ref}" >&2
  echo "Inspect 'docker compose --env-file <env> -f docker-compose.nas.yml logs'; preserve /data and recovery metadata, and do not downgrade across an unverified schema." >&2
  exit 1
fi

compose ps
echo "NAS-UPDATE-OK tag=${release_tag} contract=${deployment_contract} recovery=${recovery_point}"
