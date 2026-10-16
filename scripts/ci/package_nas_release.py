"""Package pull-only NAS installation files for one immutable image release."""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
import re
import zipfile


ROOT = Path(__file__).resolve().parents[2]
IMAGE = "ghcr.io/steven87090799/inktime"


def package_release(tag: str, commit: str, digest: str, output: Path, *, root: Path = ROOT) -> Path:
    if not re.fullmatch(r"v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:-[0-9A-Za-z.-]+)?", tag):
        raise ValueError("Expected an immutable SemVer image tag")
    if not re.fullmatch(r"[0-9a-f]{40}", commit) or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise ValueError("Expected a full source commit and image digest")
    template = (root / ".env.nas.example").read_text(encoding="utf-8")
    marker = "INKTIME_IMAGE_TAG=latest"
    if template.count(marker) != 1:
        raise ValueError("Cannot identify the unique image tag setting")
    environment = template.replace(marker, f"INKTIME_IMAGE_TAG={tag}")
    metadata = {"image": f"{IMAGE}:{tag}", "digest": digest, "commit": commit,
                "platforms": ["linux/amd64", "linux/arm64"],
                "deployment_contract": (root / "nas-deployment-contract.version").read_text().strip()}
    notes = (f"InkTime {tag}\n\nImage: `{IMAGE}:{tag}`\n\nDigest: `{digest}`\n\n"
             f"Source: `{commit}`\n\nPlatforms: linux/amd64, linux/arm64.\n\n"
             "下載安裝包，將 `.env.example` 複製為 `.env`，填妥資料、照片與網址，"
             "再由 UGREEN Project 匯入 Compose；NAS 不需要自行建置。"
             "先閱讀首次使用 Checklist，保留完整 /data 及獨立照片備份。\n")
    def document(relative: str) -> bytes:
        path = root / relative
        def replace_link(match):
            target, separator, anchor = match[1].partition("#")
            repository_path = (path.parent / target).resolve().relative_to(root.resolve()).as_posix()
            return f"](https://github.com/steven87090799/InkTime/blob/{tag}/{repository_path}{separator}{anchor})"
        return re.sub(r"\]\((?!https?://|#)([^)]+)\)", replace_link, path.read_text(encoding="utf-8")).encode()

    files = {
        "docker-compose.nas.yml": (root / "docker-compose.nas.yml").read_bytes(),
        ".env.example": environment.encode(),
        "FIRST_USE_CHECKLIST_ZH_TW.md": document("docs/getting-started/FIRST_USE_CHECKLIST_ZH_TW.md"),
        "NAS_TAG_DEPLOYMENT_ZH_TW.md": document("docs/operations/NAS_TAG_DEPLOYMENT_ZH_TW.md"),
        "release.json": (json.dumps(metadata, ensure_ascii=False, indent=2) + "\n").encode(),
    }
    output.mkdir(parents=True, exist_ok=True)
    archive = output / f"inktime-nas-{tag}.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for name, payload in files.items():
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            bundle.writestr(info, payload)
    (output / "SHA256SUMS").write_text(f"{sha256(archive.read_bytes()).hexdigest()}  {archive.name}\n")
    (output / "release-notes.md").write_text(notes, encoding="utf-8")
    return archive


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for option in ("tag", "commit", "digest"):
        parser.add_argument(f"--{option}", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    print(package_release(args.tag, args.commit, args.digest, args.output))


if __name__ == "__main__":
    main()
