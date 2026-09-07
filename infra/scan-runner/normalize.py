"""스캐너 JSON → ScanOutcome 정규화 (순수 함수, boto3·스캐너 무관).

entrypoint.py가 이 모듈을 써서 gitleaks·semgrep 출력을 기존 ScanOutcome 스키마
({"risk", "findings":[{"code","severity","detail","location"}], "scanned_areas":[...]})로
변환해요. code는 gate._CODE_PREFIX_TO_AREA(SECRET_→secret, SAST_→sast)와 정합하는
접두사를 써요. combine()이 여러 스캐너 결과를 합치고 실제 검사한 area 목록을 담아,
게이트가 "미실행"과 "검사 후 통과"를 구분할 수 있게 해요.
"""
from __future__ import annotations

_RISK_ORDER = {"none": 0, "low": 1, "medium": 2, "high": 3}


def _rule_to_code(rule_id: str) -> str:
    r = (rule_id or "").lower()
    if "aws" in r and ("key" in r or "token" in r):
        return "SECRET_AWS_ACCESS_KEY"
    if "private" in r and "key" in r:
        return "SECRET_PRIVATE_KEY"
    return "SECRET_HARDCODED"


def normalize_gitleaks(leaks: list[dict]) -> dict:
    findings = []
    for leak in leaks or []:
        file = leak.get("File", "")
        line = leak.get("StartLine")
        location = f"{file}:{line}" if file and line is not None else (file or "")
        findings.append({
            "code": _rule_to_code(leak.get("RuleID", "")),
            "severity": "high",
            "detail": f"gitleaks rule: {leak.get('RuleID', '?')} — {leak.get('Description', '')}".strip(),
            "location": location,
        })
    return {"risk": "high" if findings else "none", "findings": findings}


def _semgrep_severity(sev: str) -> str:
    # semgrep severity(ERROR·WARNING·INFO) → 내부 severity.
    s = (sev or "").upper()
    if s == "ERROR":
        return "high"
    if s == "WARNING":
        return "medium"
    return "low"


def normalize_semgrep(data: dict) -> dict:
    """semgrep JSON({"results":[...]}) → {risk, findings}. 각 result → SAST_FINDING."""
    findings = []
    worst = "none"
    for r in (data or {}).get("results", []) or []:
        path = r.get("path", "")
        line = (r.get("start") or {}).get("line")
        location = f"{path}:{line}" if path and line is not None else (path or "")
        extra = r.get("extra") or {}
        severity = _semgrep_severity(extra.get("severity", ""))
        if _RISK_ORDER[severity] > _RISK_ORDER[worst]:
            worst = severity
        findings.append({
            "code": "SAST_FINDING",
            "severity": severity,
            "detail": f"semgrep {r.get('check_id', '?')}: {extra.get('message', '')}".strip(),
            "location": location,
        })
    return {"risk": worst, "findings": findings}


def combine(area_outcomes: list) -> dict:
    """여러 (area, outcome) 결과를 하나로 합쳐요.

    - findings: 전부 이어붙임.
    - risk: 개별 outcome risk의 최댓값(none<low<medium<high).
    - scanned_areas: 실제로 실행된 스캐너의 area 목록(전달된 area 전부) — clean이어도 포함.
      게이트가 "이 area는 검사됐고 finding 0 → 통과" vs "미검사 → 미실행"을 구분하는 근거.
    """
    findings: list = []
    worst = "none"
    areas: list = []
    for area, outcome in area_outcomes:
        areas.append(area)
        findings.extend(outcome.get("findings", []))
        r = outcome.get("risk", "none")
        if _RISK_ORDER.get(r, 0) > _RISK_ORDER[worst]:
            worst = r
    return {"risk": worst, "findings": findings, "scanned_areas": areas}


def fail_closed(reason: str) -> dict:
    return {"risk": "high", "findings": [
        {"code": "SCANNER_ERROR", "severity": "high", "detail": reason, "location": ""}
    ]}
