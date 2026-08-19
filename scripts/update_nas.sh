#!/bin/sh
set -eu

usage() {
  echo "Usage: $0 <latest|vMAJOR.MINOR.PATCH[-prerelease]> [env-file]" >&2
  exit 2
}

[ "$#" -ge 1 ] && [ "$#" -le 2 ] || usage
release_tag=$1

if [ "${release_tag}" != "latest" ] && \
  ! printf '%s\n' "${release_tag}" | grep -Eq '^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-[0-9A-Za-z-]+(\.[0-9A-Za-z-]+)*)?$'; then
  echo "NAS-UPDATE-001 invalid image tag: ${release_tag}" >&2
  usage
fi

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
project_dir=$(CDPATH= cd -- "${script_dir}/.." && pwd)
compose_file="${project_dir}/docker-compose.nas.yml"
env_file=${2:-"${project_dir}/.env.nas"}

if [ ! -f "${compose_file}" ]; then
  echo "NAS-UPDATE-002 missing compose file: ${compose_file}" >&2
  exit 2
fi
if [ ! -f "${env_file}" ]; then
  echo "NAS-UPDATE-003 missing env file: ${env_file}" >&2
  echo "Copy .env.nas.example to .env.nas and replace every placeholder first." >&2
  exit 2
fi
if grep -Eq '(^|=)/CHANGE_ME|^INKTIME_PUBLIC_URL=https://inktime\.example\.com/?$' "${env_file}"; then
  echo "NAS-UPDATE-004 .env.nas still contains deployment placeholders." >&2
  exit 2
fi

export INKTIME_IMAGE_TAG="${release_tag}"
compose() {
  docker compose --env-file "${env_file}" -f "${compose_file}" "$@"
}

echo "Validating InkTime NAS deployment configuration..."
compose config --quiet

echo "Pulling immutable deployment image tag ${release_tag}..."
compose pull

echo "Recreating InkTime services with persistent data volumes unchanged..."
compose up -d --no-build --remove-orphans --wait --wait-timeout "${INKTIME_UPDATE_TIMEOUT_SECONDS:-180}"

compose ps
echo "NAS-UPDATE-OK tag=${release_tag}"
