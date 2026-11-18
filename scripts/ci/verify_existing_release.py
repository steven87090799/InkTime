"""Verify a published multi-platform image before resuming an immutable release."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess


DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


def verify_existing_release(manifest: dict, *, revision: str, tag: str, source: str, contract: str, load_config) -> str:
    digest = manifest.get("digest")
    if not isinstance(digest, str) or DIGEST.fullmatch(digest) is None:
        raise ValueError("Existing release has no immutable index digest")
    required = {"amd64", "arm64"}
    found = set()
    for item in manifest.get("manifests", []):
        platform = item.get("platform") or {}
        architecture = platform.get("architecture")
        if platform.get("os") != "linux" or architecture not in required:
            continue
        child = item.get("digest")
        if architecture in found or not isinstance(child, str) or DIGEST.fullmatch(child) is None:
            raise ValueError("Existing release has invalid or duplicate platform descriptors")
        config = load_config(child)
        labels = (config.get("config") or {}).get("Labels") or {}
        expected = {"org.opencontainers.image.revision": revision, "org.opencontainers.image.version": tag,
                    "org.opencontainers.image.source": source, "io.inktime.nas-deployment-contract": contract}
        if config.get("os") != "linux" or config.get("architecture") != architecture or any(labels.get(k) != v for k, v in expected.items()):
            raise ValueError("Existing release platform identity differs; refusing to overwrite")
        found.add(architecture)
    if found != required:
        raise ValueError("Existing release is missing a required platform")
    return digest


def main() -> None:
    parser = argparse.ArgumentParser()
    for key in ("manifest", "image", "revision", "tag", "source", "contract", "output"):
        parser.add_argument("--" + key, required=True)
    args = parser.parse_args()
    manifest = json.loads(Path(args.manifest).read_text())

    def load_config(digest: str) -> dict:
        result = subprocess.run(  # noqa: S603 -- fixed Docker argv, no shell.
            ["docker", "buildx", "imagetools", "inspect", f"{args.image}@{digest}", "--format", "{{json .Image}}"],  # noqa: S607 -- installed by CI.
            capture_output=True, text=True, check=True, timeout=60,
        )
        return json.loads(result.stdout)

    digest = verify_existing_release(manifest, revision=args.revision, tag=args.tag, source=args.source,
                                     contract=args.contract, load_config=load_config)
    with Path(args.output).open("a") as output:
        output.write(f"reuse=true\ndigest={digest}\n")


if __name__ == "__main__":
    main()
