"""Refuse stable latest alias regressions using the existing OCI image labels."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re


def stable_version(value: str) -> tuple[int, int, int]:
    match = re.fullmatch(r"v?(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", value)
    if match is None:
        raise ValueError("latest 版本必須是可辨識的 stable SemVer；不自動覆寫未知版本")
    return int(match[1]), int(match[2]), int(match[3])


def _versions(value) -> list[str]:
    versions = []
    if isinstance(value, dict):
        config = value.get("config", value.get("Config", {}))
        labels = config.get("Labels", {}) if isinstance(config, dict) else {}
        if isinstance(labels, dict) and "org.opencontainers.image.version" in labels:
            versions.append(str(labels["org.opencontainers.image.version"]))
        for child in value.values():
            versions.extend(_versions(child))
    elif isinstance(value, list):
        for child in value:
            versions.extend(_versions(child))
    return versions


def can_promote_latest(candidate: str, current_image=None) -> bool:
    desired = stable_version(candidate)
    if current_image is None:
        return True
    versions = {stable_version(value) for value in _versions(current_image)}
    if len(versions) != 1:
        raise ValueError("latest 的架構版本資訊缺少或不一致；保留目前 alias 待人工核對")
    return desired > next(iter(versions))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--current-image-json", type=Path)
    args = parser.parse_args()
    current = json.loads(args.current_image_json.read_text(encoding="utf-8")) if args.current_image_json else None
    if args.current_image_json and current is None:
        raise ValueError("latest image inspect 沒有可驗證的版本資訊")
    print("publish_latest=" + str(can_promote_latest(args.candidate, current)).lower())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
