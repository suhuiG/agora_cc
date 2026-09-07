"""scan-runner normalize 단위 테스트 (이미지 밖에서 로컬 실행).
실행: cd infra/scan-runner && python -m pytest test_normalize.py -v
"""
from normalize import normalize_gitleaks, normalize_semgrep, combine, fail_closed


def test_empty_leaks_is_none_risk():
    assert normalize_gitleaks([]) == {"risk": "none", "findings": [], "scanned_areas": ["secret"]}


def test_aws_key_rule_maps_to_aws_code():
    leaks = [{
        "RuleID": "aws-access-token",
        "File": "config.py",
        "StartLine": 12,
        "Description": "AWS Access Token",
    }]
    out = normalize_gitleaks(leaks)
    assert out["risk"] == "high"
    assert len(out["findings"]) == 1
    f = out["findings"][0]
    assert f["code"] == "SECRET_AWS_ACCESS_KEY"
    assert f["severity"] == "high"
    assert f["location"] == "config.py:12"
    assert "AWS Access Token" in f["detail"]


def test_private_key_rule_maps_to_private_key_code():
    leaks = [{"RuleID": "private-key", "File": "id_rsa", "StartLine": 1, "Description": "Private Key"}]
    assert normalize_gitleaks(leaks)["findings"][0]["code"] == "SECRET_PRIVATE_KEY"


def test_unknown_secret_rule_falls_back_to_hardcoded():
    leaks = [{"RuleID": "generic-api-key", "File": "app.js", "StartLine": 5, "Description": "Generic"}]
    assert normalize_gitleaks(leaks)["findings"][0]["code"] == "SECRET_HARDCODED"


def test_missing_fields_do_not_crash():
    # gitleaks 필드 누락 시에도 정규화가 죽지 않아야(location 빈 문자열 등).
    out = normalize_gitleaks([{"RuleID": "x"}])
    assert out["risk"] == "high"
    assert out["findings"][0]["location"] == ""


def test_fail_closed_shape():
    out = fail_closed("timeout")
    assert out["risk"] == "high"
    assert out["findings"][0]["code"] == "SCANNER_ERROR"
    assert out["findings"][0]["detail"] == "timeout"


# ── semgrep 정규화 ──────────────────────────────────────────────
def test_semgrep_empty_results_is_none():
    assert normalize_semgrep({"results": []}) == {"risk": "none", "findings": [], "scanned_areas": ["sast"]}


def test_semgrep_result_maps_to_sast_code():
    data = {"results": [{
        "check_id": "python.lang.security.audit.dangerous-eval",
        "path": "app.py",
        "start": {"line": 42},
        "extra": {"message": "Detected eval()", "severity": "ERROR"},
    }]}
    out = normalize_semgrep(data)
    assert out["risk"] == "high"
    f = out["findings"][0]
    assert f["code"] == "SAST_FINDING"
    assert f["location"] == "app.py:42"
    assert "eval" in f["detail"].lower()
    assert f["severity"] == "high"


def test_semgrep_warning_severity_is_medium():
    data = {"results": [{
        "check_id": "x", "path": "a.py", "start": {"line": 1},
        "extra": {"message": "m", "severity": "WARNING"},
    }]}
    assert normalize_semgrep(data)["findings"][0]["severity"] == "medium"


def test_semgrep_missing_fields_do_not_crash():
    out = normalize_semgrep({"results": [{"check_id": "x"}]})
    assert out["findings"][0]["location"] == ""


# ── combine: 여러 스캐너 결과 병합 + scanned_areas ──────────────
def test_combine_merges_findings_and_areas():
    gl = {"risk": "high", "findings": [{"code": "SECRET_HARDCODED", "severity": "high", "detail": "d", "location": "a:1"}]}
    sg = {"risk": "none", "findings": []}
    out = combine([("secret", gl), ("sast", sg)])
    assert out["risk"] == "high"                       # 최대 위험도
    assert len(out["findings"]) == 1
    assert set(out["scanned_areas"]) == {"secret", "sast"}   # 둘 다 실제 검사됨


def test_combine_risk_escalates_to_max():
    a = {"risk": "none", "findings": []}
    b = {"risk": "medium", "findings": [{"code": "SAST_FINDING", "severity": "medium", "detail": "d", "location": "x:2"}]}
    out = combine([("secret", a), ("sast", b)])
    assert out["risk"] == "medium"
    assert set(out["scanned_areas"]) == {"secret", "sast"}


def test_combine_clean_scan_still_records_areas():
    # 둘 다 clean이어도 scanned_areas는 채워져야(게이트가 '검사 후 통과'로 판정하도록).
    out = combine([("secret", {"risk": "none", "findings": []}),
                   ("sast", {"risk": "none", "findings": []})])
    assert out["risk"] == "none"
    assert out["findings"] == []
    assert set(out["scanned_areas"]) == {"secret", "sast"}


# ── single-area 계약 테스트: 도구별 이미지는 combine 없이 자기 area만 ────────────────
def test_normalize_gitleaks_single_area_shape():
    # 단일 도구 이미지는 combine 없이 자기 area 결과만 반환 — 계약(risk/findings/scanned_areas) 유지.
    from normalize import normalize_gitleaks
    out = normalize_gitleaks([{
        "RuleID": "aws-access-token", "File": "app.py", "StartLine": 3,
        "Description": "AWS", "Match": "AKIA...",
    }])
    assert set(out.keys()) >= {"risk", "findings", "scanned_areas"}
    assert out["scanned_areas"] == ["secret"]
    assert out["findings"] and out["risk"] == "high"
