"""스캐너 JSON → ScanOutcome 정규화 (순수 함수, boto3·스캐너 무관).

entrypoint.py가 이 모듈을 써서 gitleaks·semgrep 출력을 기존 ScanOutcome 스키마
({"risk", "findings":[{"code","severity","detail","location"}], "scanned_areas":[...]})로
변환해요. code는 gate._CODE_PREFIX_TO_AREA(SECRET_→secret, SAST_→sast)와 정합하는
접두사를 써요. combine()이 여러 스캐너 결과를 합치고 실제 검사한 area 목록을 담아,
게이트가 "미실행"과 "검사 후 통과"를 구분할 수 있게 해요.
"""
from __future__ import annotations

_RISK_ORDER = {"none": 0, "low": 1, "medium": 2, "high": 3}


def log_proc(tool: str, proc, scan_id: str = "") -> None:
    """subprocess 실행의 stdout/stderr를 CloudWatch로 흘려요(도구 application 로그 노출).

    스캐너 CLI는 결과 JSON을 --output/--report-path로 파일에 쓰고, stdout/stderr엔 진행·요약만
    남겨요. capture_output=True로 잡은 그 출력을 여기서 print해야 콘솔 로그 modal에 보여요.
    각 줄에 scan_id를 붙여, 콘솔 modal의 CloudWatch quoted 필터("scan_id")가 start/done뿐
    아니라 이 상세 진행 로그까지 그 scan_id만 정확히 잡게 해요(scan_id 없으면 tool만 붙임).
    """
    import sys
    tag = f"[{scan_id}] " if scan_id else ""
    for stream_name, raw in (("out", getattr(proc, "stdout", None)),
                             ("err", getattr(proc, "stderr", None))):
        if not raw:
            continue
        text = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
        for line in text.splitlines():
            if line.strip():
                print(f"{tag}[{tool}:{stream_name}] {line}", file=sys.stderr, flush=True)


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
    return {"risk": "high" if findings else "none", "findings": findings, "scanned_areas": ["secret"]}


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
    return {"risk": worst, "findings": findings, "scanned_areas": ["sast"]}


def _trivy_severity(sev: str) -> str:
    # trivy severity(CRITICAL·HIGH·MEDIUM·LOW·UNKNOWN) → 내부 severity.
    s = (sev or "").upper()
    if s in ("CRITICAL", "HIGH"):
        return "high"
    if s == "MEDIUM":
        return "medium"
    return "low"


def normalize_trivy(data: dict) -> dict:
    """trivy fs JSON({"Results":[{"Vulnerabilities":[...]}]}) → {risk, findings, scanned_areas}.

    각 취약점 → CVE_VULNERABILITY(gate _CODE_PREFIX_TO_AREA: CVE_ → sbom_cve).
    """
    findings = []
    worst = "none"
    for res in (data or {}).get("Results", []) or []:
        target = res.get("Target", "")
        for v in res.get("Vulnerabilities", []) or []:
            severity = _trivy_severity(v.get("Severity", ""))
            if _RISK_ORDER[severity] > _RISK_ORDER[worst]:
                worst = severity
            vid = v.get("VulnerabilityID", "?")
            pkg = v.get("PkgName", "?")
            installed = v.get("InstalledVersion", "")
            fixed = v.get("FixedVersion", "")
            fix = f" (fixed: {fixed})" if fixed else ""
            findings.append({
                "code": "CVE_VULNERABILITY",
                "severity": severity,
                "detail": f"trivy {vid}: {pkg} {installed}{fix} — {v.get('Title', '')}".strip(),
                "location": target,
            })
    return {"risk": worst, "findings": findings, "scanned_areas": ["sbom_cve"]}


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


_VALID_SEVERITY = {"low", "medium", "high"}
_THREAT_CODES = {
    "THREAT_TOOL_POISONING", "THREAT_PROMPT_INJECTION", "THREAT_EXCESSIVE_AGENCY",
    "THREAT_DATA_EXFILTRATION", "THREAT_TOOL_SHADOWING", "THREAT_UNSAFE_CREDENTIAL",
}


def normalize_llm_judge(verdict: dict) -> dict:
    """LLM-judge 구조화 판정 → {risk, findings, scanned_areas}. 인젝션 방어 정제 포함.

    THREAT_ 화이트리스트에 없는 code는 제외(악성 출력 무시), severity가 low/medium/high가
    아니면 low로 강등해요. risk는 남은 finding의 최고 severity로 재계산(모델 자기신고 무시).
    """
    findings, worst = [], "none"
    for f in (verdict or {}).get("findings", []) or []:
        code = str(f.get("code", ""))
        if code not in _THREAT_CODES:
            continue
        sev = str(f.get("severity", "")).lower()
        if sev not in _VALID_SEVERITY:
            sev = "low"
        if _RISK_ORDER[sev] > _RISK_ORDER[worst]:
            worst = sev
        findings.append({"code": code, "severity": sev,
                         "detail": str(f.get("detail", ""))[:500],
                         "location": str(f.get("location", ""))[:200]})
    return {"risk": worst, "findings": findings, "scanned_areas": ["agent_intent"]}
