from __future__ import annotations

from copy import deepcopy

import pytest

from scripts.ci.debian_fixed_vex import build_vex


IMAGE = "sha256:" + "a" * 64
PURL = "pkg:deb/debian/libmount1@2.42.4-1?arch=amd64&distro=debian-13.6"


@pytest.mark.parametrize("arch", ["amd64", "arm64"])
@pytest.mark.parametrize("release,accepted", [("3", False), ("4", True), ("5", False)])
def test_zlib_requires_exact_vendor_fixed_source_and_binary(arch, release, accepted):
    version = "1.3.dfsg+really1.3.2"
    package = {"ID": "zlib-fixed", "Name": "zlib1g", "Version": version, "Release": release, "Epoch": 1,
               "SrcName": "zlib", "SrcVersion": version, "SrcRelease": release, "SrcEpoch": 1, "Arch": arch}
    finding = {"VulnerabilityID": "CVE-2026-76844", "PkgID": "zlib-fixed", "PkgName": "zlib1g",
               "InstalledVersion": f"1:{version}-{release}",
               "PkgIdentifier": {"PURL": f"pkg:deb/debian/zlib1g@{version}-{release}?arch={arch}&distro=debian-13.6&epoch=1"}}
    report = {"SchemaVersion": 2, "Metadata": {"ImageID": IMAGE, "OS": {"Family": "debian", "Name": "13.6"}},
              "Results": [{"Class": "os-pkgs", "Type": "debian", "Packages": [package], "Vulnerabilities": [finding]}]}
    assert bool(build_vex(report, IMAGE)["statements"]) is accepted
    package["SrcRelease"] = "3"
    assert build_vex(report, IMAGE)["statements"] == []


def _report():
    return {"SchemaVersion": 2, "Metadata": {"ImageID": IMAGE, "OS": {"Family": "debian", "Name": "13.6"}},
            "Results": [{"Class": "os-pkgs", "Type": "debian", "Packages": [{
                "ID": "libmount1@2.42.4-1", "Name": "libmount1", "Version": "2.42.4", "Release": "1",
                "SrcName": "util-linux", "SrcVersion": "2.42.4", "SrcRelease": "1", "Arch": "amd64",
            }], "Vulnerabilities": [{"VulnerabilityID": "CVE-2026-76642", "PkgID": "libmount1@2.42.4-1",
                                    "PkgName": "libmount1", "InstalledVersion": "2.42.4-1",
                                    "PkgIdentifier": {"PURL": PURL}}]}]}


def test_exact_vendor_fixed_product_is_attested_and_unknown_cves_remain_unattested():
    report = _report()
    unknown = deepcopy(report["Results"][0]["Vulnerabilities"][0])
    unknown["VulnerabilityID"] = "CVE-2099-12345"
    report["Results"][0]["Vulnerabilities"].append(unknown)
    document = build_vex(report, IMAGE)
    assert document["statements"] == [{
        "vulnerability": {"name": "CVE-2026-76642"}, "products": [{"@id": PURL}], "status": "fixed",
        "status_notes": f"Exact signed Debian 20261012 snapshot binary/source versions; image {IMAGE}. "
                        "Vendor evidence: https://security-tracker.debian.org/tracker/CVE-2026-76642",
    }]
    assert len(report["Results"][0]["Vulnerabilities"]) == 2


@pytest.mark.parametrize("field,value", [("Version", "2.41.5"), ("Version", "2.99"),
                                        ("SrcVersion", "2.41.5"), ("SrcName", "unreviewed"),
                                        ("Arch", "unknown"), ("ID", "different")])
def test_old_future_or_inconsistent_package_evidence_cannot_suppress_findings(field, value):
    report = _report()
    report["Results"][0]["Packages"][0][field] = value
    assert build_vex(report, IMAGE)["statements"] == []


@pytest.mark.parametrize("purl", [PURL.replace("2.42.4-1", "2.41.5-1"), PURL.replace("amd64", "arm64"),
                                 PURL.replace("debian-13.6", "debian-14"), PURL + "&epoch=1"])
def test_purl_must_match_the_actual_fixed_binary_and_image(purl):
    report = _report()
    report["Results"][0]["Vulnerabilities"][0]["PkgIdentifier"]["PURL"] = purl
    assert build_vex(report, IMAGE)["statements"] == []


@pytest.mark.parametrize("metadata", [{"ImageID": "sha256:" + "b" * 64, "OS": {"Family": "debian", "Name": "13.6"}},
                                     {"ImageID": IMAGE, "OS": {"Family": "ubuntu", "Name": "13.6"}},
                                     {"ImageID": IMAGE, "OS": {"Family": "debian", "Name": "14"}}])
def test_stale_or_different_image_evidence_is_rejected(metadata):
    report = _report()
    report["Metadata"] = metadata
    with pytest.raises(ValueError, match="exact Debian 13 image"):
        build_vex(report, IMAGE)


def test_missing_package_inventory_never_attests_a_fix():
    report = _report()
    del report["Results"][0]["Packages"]
    assert build_vex(report, IMAGE)["statements"] == []


def test_bsdutils_binary_epoch_and_distinct_source_epoch_must_both_match():
    report = _report()
    package = report["Results"][0]["Packages"][0]
    package.update(ID="bsdutils@1:2.42.4-1", Name="bsdutils", Epoch=1, SrcEpoch=0)
    finding = report["Results"][0]["Vulnerabilities"][0]
    finding.update(PkgID=package["ID"], PkgName="bsdutils", InstalledVersion="1:2.42.4-1",
                   PkgIdentifier={"PURL": PURL.replace("libmount1", "bsdutils") + "&epoch=1"})
    assert len(build_vex(report, IMAGE)["statements"]) == 1
    package["SrcEpoch"] = 1
    assert build_vex(report, IMAGE)["statements"] == []


@pytest.mark.parametrize("name,source,version,release,arch,code", [
    ("ncurses-base", "ncurses", "6.6+20260608", "2", "all", "CVE-2025-69720"),
    ("libsystemd0", "systemd", "262", "1", "amd64", "CVE-2026-16742"),
    ("libudev1", "systemd", "262", "1", "arm64", "CVE-2026-16742"),
])
def test_reviewed_independent_architecture_and_systemd_packages_have_exact_fixed_evidence(
    name, source, version, release, arch, code,
):
    report = _report()
    binary = version + "-" + release
    purl = f"pkg:deb/debian/{name}@{binary.replace('+', '%2B')}?arch={arch}&distro=debian-13.6"
    package = report["Results"][0]["Packages"][0]
    package.update(ID=name + "@" + binary, Name=name, Version=version, Release=release,
                   SrcName=source, SrcVersion=version, SrcRelease=release, Arch=arch)
    finding = report["Results"][0]["Vulnerabilities"][0]
    finding.update(VulnerabilityID=code, PkgID=package["ID"], PkgName=name,
                   InstalledVersion=binary, PkgIdentifier={"PURL": purl})
    assert len(build_vex(report, IMAGE)["statements"]) == 1
    package["Version"] = "unreviewed"
    assert build_vex(report, IMAGE)["statements"] == []


@pytest.mark.parametrize("architecture", ["amd64", "arm64"])
@pytest.mark.parametrize("version,accepted", [("3.53.4", False), ("3.54.1", True), ("3.99.0", False)])
@pytest.mark.parametrize("code", ["CVE-2026-119373", "CVE-2026-119374", "CVE-2026-119379"])
def test_sqlite_fix_attestation_is_exact_and_never_hides_old_or_future_builds(architecture, version, accepted, code):
    report = _report()
    binary = version + "-1"
    purl = f"pkg:deb/debian/libsqlite3-0@{binary}?arch={architecture}&distro=debian-13.6"
    package = report["Results"][0]["Packages"][0]
    package.update(ID="libsqlite3-0@" + binary, Name="libsqlite3-0", Version=version, Release="1",
                   SrcName="sqlite3", SrcVersion=version, SrcRelease="1", Arch=architecture)
    finding = report["Results"][0]["Vulnerabilities"][0]
    finding.update(VulnerabilityID=code, PkgID=package["ID"], PkgName="libsqlite3-0",
                   InstalledVersion=binary, PkgIdentifier={"PURL": purl})
    statements = build_vex(report, IMAGE)["statements"]
    assert bool(statements) == accepted
    if accepted:
        if code == "CVE-2026-119373":
            assert "Debian tracker still lists unfixed" in statements[0]["status_notes"]
            assert "e1c9934f9f11a499029abb020ea715a08970da80" in statements[0]["status_notes"]
        else:
            assert f"https://security-tracker.debian.org/tracker/{code}" in statements[0]["status_notes"]
            assert "20261103T023120Z sqlite3 3.54.1-1" in statements[0]["status_notes"]
        package["SrcVersion"] = "3.53.4"
        assert build_vex(report, IMAGE)["statements"] == []
