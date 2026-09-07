import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent))
from normalize import normalize_llm_judge


def test_llm_judge_maps_findings_and_risk():
    verdict = {"findings": [
        {"code": "THREAT_TOOL_POISONING", "severity": "high", "detail": "hidden instr", "location": "h.py:4"},
        {"code": "THREAT_EXCESSIVE_AGENCY", "severity": "medium", "detail": "broad perms", "location": ""},
    ], "risk": "high"}
    out = normalize_llm_judge(verdict)
    assert out["scanned_areas"] == ["agent_intent"]
    assert out["risk"] == "high"
    assert len(out["findings"]) == 2
    assert out["findings"][0]["code"] == "THREAT_TOOL_POISONING"


def test_llm_judge_sanitizes_bad_severity_and_unknown_code():
    # 인젝션 방어: 스키마 외 severity·모르는 code는 정제(악성 출력이 게이트 오염 방지).
    verdict = {"findings": [
        {"code": "THREAT_DATA_EXFILTRATION", "severity": "CATASTROPHIC", "detail": "x", "location": ""},
        {"code": "NOT_A_THREAT_CODE", "severity": "high", "detail": "y", "location": ""},
    ], "risk": "high"}
    out = normalize_llm_judge(verdict)
    # 잘못된 severity → low로 강등, THREAT_ prefix 아닌 code → 제외
    codes = {f["code"] for f in out["findings"]}
    assert codes == {"THREAT_DATA_EXFILTRATION"}
    assert out["findings"][0]["severity"] == "low"


def test_llm_judge_empty_is_none():
    out = normalize_llm_judge({"findings": [], "risk": "none"})
    assert out["risk"] == "none"
    assert out["findings"] == []
    assert out["scanned_areas"] == ["agent_intent"]
