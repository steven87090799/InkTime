"""Attest exact, vendor-fixed Debian packages from this image's raw report.

Trivy 0.60 treats a missing Trixie FixedVersion as affected regardless of
installed version. The signed Forky snapshot supplies the reviewed fixes.
This is fixed-version evidence, not an exploitability or non-root waiver.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import parse_qs, unquote


# Exact binary AND source versions reviewed against the signed 20261012
# amd64/arm64 Packages metadata and Debian's CVE pages. New versions require
# another review; they are deliberately not accepted by a >= comparison.
UTIL_PACKAGES = {
    name: "2.42.4-1" for name in (
        "libblkid1", "liblastlog2-2", "libmount1", "libsmartcols1",
        "libuuid1", "mount", "util-linux",
    )
} | {"bsdutils": "1:2.42.4-1", "login": "1:4.16.0-2+really2.42.4-1"}
FIXES = {
    **{code: ("util-linux", "2.42.4-1", UTIL_PACKAGES) for code in (
        "CVE-2026-76642", "CVE-2026-78408", "CVE-2026-78409", "CVE-2026-78410",
    )},
    "CVE-2026-54369": ("acl", "2.4.0-1", {"libacl1": "2.4.0-1"}),
    "CVE-2025-69720": ("ncurses", "6.6+20260608-2", {
        name: "6.6+20260608-2" for name in ("ncurses-base", "ncurses-bin", "libtinfo6", "libncursesw6")
    }),
    "CVE-2026-9538": ("perl", "5.42.3-1", {"perl-base": "5.42.3-1"}),
    "CVE-2026-16742": ("systemd", "262-1", {"libsystemd0": "262-1", "libudev1": "262-1"}),
}


def _version(package: dict, prefix: str = "") -> str:
    value = str(package.get(prefix + "Version") or "")
    release = package.get(prefix + "Release")
    epoch = package.get(prefix + "Epoch")
    if release:
        value += "-" + str(release)
    if epoch:
        value = str(epoch) + ":" + value
    return value


def build_vex(report: dict, image_id: str) -> dict:
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
        raise ValueError("Invalid immutable image ID")
    metadata = report.get("Metadata") or {}
    operating_system = metadata.get("OS") or {}
    if (report.get("SchemaVersion") != 2 or metadata.get("ImageID") != image_id
            or operating_system.get("Family") != "debian"
            or not re.fullmatch(r"13(?:\.\d+)*", str(operating_system.get("Name")))
            or not isinstance(report.get("Results"), list)):
        raise ValueError("Raw scan must identify the exact Debian 13 image")
    products: dict[str, set[str]] = {}
    for result in report["Results"]:
        if result.get("Class") != "os-pkgs" or result.get("Type") != "debian":
            continue
        packages = {pkg["ID"]: pkg for pkg in result.get("Packages", []) if pkg.get("ID")}
        for finding in result.get("Vulnerabilities", []):
            code = finding.get("VulnerabilityID")
            if code not in FIXES:
                continue
            source_name, source_version, binaries = FIXES[code]
            package = packages.get(finding.get("PkgID"), {})
            name = finding.get("PkgName")
            approved = binaries.get(name)
            if (not approved or finding.get("InstalledVersion") != approved
                    or package.get("Name") != name or _version(package) != approved
                    or package.get("SrcName") != source_name or _version(package, "Src") != source_version):
                continue
            purl = (finding.get("PkgIdentifier") or {}).get("PURL", "")
            body, _, query = purl.partition("?")
            qualifiers = parse_qs(query, strict_parsing=True)
            epoch, _, binary_version = approved.partition(":")
            if not binary_version:
                epoch, binary_version = "0", approved
            if (unquote(body) != f"pkg:deb/debian/{name}@{binary_version}"
                    or qualifiers.get("arch") != [package.get("Arch")]
                    or package.get("Arch") not in ({"all"} if name == "ncurses-base" else {"amd64", "arm64"})
                    or qualifiers.get("distro") != ["debian-" + str(operating_system["Name"])]
                    or qualifiers.get("epoch", ["0"]) != [epoch]):
                continue
            products.setdefault(code, set()).add(purl)
    statements = [{
        "vulnerability": {"name": code},
        "products": [{"@id": purl} for purl in sorted(purls)],
        "status": "fixed",
        "status_notes": (
            f"Exact signed Debian 20261012 snapshot binary/source versions; image {image_id}. "
            f"Vendor evidence: https://security-tracker.debian.org/tracker/{code}"
        ),
    } for code, purls in sorted(products.items())]
    identity = hashlib.sha256(json.dumps([image_id, statements], sort_keys=True).encode()).hexdigest()
    return {
        "@context": "https://openvex.dev/ns/v0.2.0",
        "@id": "https://github.com/steven87090799/InkTime/vex/" + identity,
        "author": "https://github.com/steven87090799/InkTime",
        "timestamp": datetime.now(timezone.utc).isoformat(), "version": 1,
        "statements": statements,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("report", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--image-id", required=True)
    args = parser.parse_args()
    document = build_vex(json.loads(args.report.read_text(encoding="utf-8")), args.image_id)
    args.output.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"vendor_fixed_cves": len(document["statements"]),
                      "image_id": args.image_id, "evidence": str(args.output)}))
    return 0 if document["statements"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
