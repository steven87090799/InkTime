#!/bin/sh
set -eu

allow_dirty=0
manifest_path="build-manifest.json"
repository="inktime"
while [ "$#" -gt 0 ]; do
  case "$1" in
    --allow-dirty-development) allow_dirty=1 ;;
    --manifest) shift; manifest_path=${1:?missing_manifest_path} ;;
    --repository) shift; repository=${1:?missing_repository} ;;
    *) echo "BUILD-001 unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done

git_revision=$(git rev-parse --verify HEAD)
dirty=false
if [ -n "$(git status --porcelain --untracked-files=normal)" ]; then
  dirty=true
  if [ "$allow_dirty" -ne 1 ]; then
    echo "BUILD-002 dirty worktree；正式 build 已拒絕。開發驗證才可使用 --allow-dirty-development" >&2
    exit 2
  fi
fi

build_time=$(date -u +%Y-%m-%dT%H:%M:%SZ)
image_tag=$git_revision
if [ "$dirty" = true ]; then image_tag="${git_revision}-dirty"; fi
image_reference="${repository}:${image_tag}"

docker build --pull \
  --build-arg "INKTIME_GIT_REVISION=${git_revision}" \
  --build-arg "INKTIME_BUILD_TIME=${build_time}" \
  --tag "$image_reference" .
image_id=$(docker image inspect --format '{{.Id}}' "$image_reference")

umask 077
printf '%s\n' \
  '{' \
  "  \"git_revision\": \"${git_revision}\"," \
  "  \"build_time\": \"${build_time}\"," \
  "  \"image_tag\": \"${image_tag}\"," \
  "  \"image_reference\": \"${image_reference}\"," \
  "  \"image_id\": \"${image_id}\"," \
  '  "migration_version": 25,' \
  "  \"dirty\": ${dirty}" \
  '}' > "$manifest_path"
echo "BUILD-OK ${image_reference} manifest=${manifest_path}"
