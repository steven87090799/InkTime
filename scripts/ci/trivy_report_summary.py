"""Emit bounded, actionable findings from the existing Trivy JSON without rescanning."""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
from typing import Any


def summarize(report: dict[str, Any]) -> dict[str, Any]:
    counts: Counter[str] = Counter()
    findings: dict[str, dict[str, Any]] = {}
    for result in report.get("Results") or []:
        for item in result.get("Vulnerabilities") or []:
            severity = str(item.get("Severity") or "UNKNOWN")
            if severity not in {"HIGH", "CRITICAL"}:
                continue
            identifier = str(item.get("VulnerabilityID") or "unknown")
            counts[severity] += 1
            finding = findings.setdefault(identifier, {"id": identifier, "severity": severity, "packages": []})
            if severity == "CRITICAL":
                finding["severity"] = severity
            package = {"name": str(item.get("PkgName") or "unknown"), "installed": str(item.get("InstalledVersion") or ""),
                       "fixed": str(item.get("FixedVersion") or "")}
            if package not in finding["packages"]:
                finding["packages"].append(package)
    ordered = sorted(findings.values(), key=lambda value: (value["severity"] != "CRITICAL", value["id"]))
    return {"package_findings": sum(counts.values()), "severities": dict(counts), "unique_ids": len(ordered),
            "findings": ordered[:40], "omitted_ids": max(0, len(ordered) - 40)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    args = parser.parse_args()
    summary = summarize(json.loads(args.report.read_text(encoding="utf-8")))
    print(json.dumps({key: value for key, value in summary.items() if key != "findings"}, ensure_ascii=False))
    for finding in summary["findings"]:
        print(json.dumps(finding, ensure_ascii=False))
    return 0  # The workflow separately preserves the original scanner's exit code.


if __name__ == "__main__":
    raise SystemExit(main())
