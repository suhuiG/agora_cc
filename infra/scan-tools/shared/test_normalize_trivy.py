"""normalize_trivy 계약 테스트 — trivy fs JSON → ScanOutcome."""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from normalize import normalize_trivy


def test_no_vulns_is_clean():
    out = normalize_trivy({"Results": []})
    assert out == {"risk": "none", "findings": [], "scanned_areas": ["sbom_cve"]}


def test_results_without_vulnerabilities_key_clean():
    # Results는 있지만 취약점 없는 타겟(예: lockfile 없음).
    out = normalize_trivy({"Results": [{"Target": "requirements.txt", "Class": "lang-pkgs"}]})
    assert out["risk"] == "none"
    assert out["findings"] == []
    assert out["scanned_areas"] == ["sbom_cve"]


def test_critical_vuln_maps_to_high_and_cve_code():
    data = {"Results": [{
        "Target": "requirements.txt",
        "Vulnerabilities": [{
            "VulnerabilityID": "CVE-2023-1234", "PkgName": "flask",
            "InstalledVersion": "2.0.0", "FixedVersion": "2.0.1",
            "Severity": "CRITICAL", "Title": "RCE in flask",
        }],
    }]}
    out = normalize_trivy(data)
    assert out["risk"] == "high"
    assert out["scanned_areas"] == ["sbom_cve"]
    assert len(out["findings"]) == 1
    f = out["findings"][0]
    assert f["code"] == "CVE_VULNERABILITY"      # gate _CODE_PREFIX_TO_AREA: CVE_ → sbom_cve
    assert f["severity"] == "high"                # CRITICAL·HIGH → high
    assert "CVE-2023-1234" in f["detail"]
    assert "flask" in f["detail"]
    assert f["location"] == "requirements.txt"


def test_severity_mapping():
    def _one(sev):
        return normalize_trivy({"Results": [{"Target": "t", "Vulnerabilities": [
            {"VulnerabilityID": "CVE-X", "PkgName": "p", "Severity": sev}]}]})["findings"][0]["severity"]
    assert _one("CRITICAL") == "high"
    assert _one("HIGH") == "high"
    assert _one("MEDIUM") == "medium"
    assert _one("LOW") == "low"
    assert _one("UNKNOWN") == "low"


def test_worst_risk_across_multiple():
    data = {"Results": [{"Target": "t", "Vulnerabilities": [
        {"VulnerabilityID": "CVE-1", "PkgName": "a", "Severity": "LOW"},
        {"VulnerabilityID": "CVE-2", "PkgName": "b", "Severity": "MEDIUM"},
    ]}]}
    out = normalize_trivy(data)
    assert out["risk"] == "medium"
    assert len(out["findings"]) == 2
