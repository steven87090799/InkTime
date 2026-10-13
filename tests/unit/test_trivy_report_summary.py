from scripts.ci.trivy_report_summary import summarize


def test_summary_keeps_severity_package_versions_and_unique_vulnerability_identity():
    report = {"Results": [{"Vulnerabilities": [
        {"VulnerabilityID": "CVE-test", "Severity": "HIGH", "PkgName": "package-a", "InstalledVersion": "1", "FixedVersion": "2"},
        {"VulnerabilityID": "CVE-test", "Severity": "CRITICAL", "PkgName": "package-b", "InstalledVersion": "1"},
        {"VulnerabilityID": "CVE-low", "Severity": "LOW"},
    ]}]}
    summary = summarize(report)
    assert summary["package_findings"] == 2 and summary["unique_ids"] == 1
    assert summary["severities"] == {"HIGH": 1, "CRITICAL": 1}
    assert summary["findings"] == [{"id": "CVE-test", "severity": "CRITICAL", "packages": [
        {"name": "package-a", "installed": "1", "fixed": "2"}, {"name": "package-b", "installed": "1", "fixed": ""},
    ]}]


def test_summary_handles_empty_results_and_bounds_console_output_without_losing_total():
    assert summarize({"Results": None})["package_findings"] == 0
    result = summarize({"Results": [{"Vulnerabilities": [{"VulnerabilityID": f"CVE-{index}", "Severity": "HIGH"}
                                                          for index in range(50)]}]})
    assert result["unique_ids"] == 50 and len(result["findings"]) == 40 and result["omitted_ids"] == 10
