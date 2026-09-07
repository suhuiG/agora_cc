"""게이트 파이프라인 계산 (§3-M2) — 등급 매트릭스 + 최신 스캔 → 단계별 상태.

등급×도구 매트릭스(TierCell)가 SoT. 자산의 목표 등급이 요구하는 도구들이
게이트 단계가 되고, 최신 ScanRecord의 findings를 각 도구의 area에 매핑해
단계별 PASS/FAIL/대기중을 판정해요.

[확장 지점] 지금은 finding.code 접두사(SECRET_·SAST_ 등)를 도구 area에 매핑하는
간단 규칙이에요. 실 스캐너 통합 시 finding에 tool_id/area를 직접 실어 정확 매핑으로
교체하면 이 모듈만 바뀌어요. (오너십: governance 도메인 — 정곤님과 향후 병합)
"""
from __future__ import annotations

from dataclasses import dataclass

from .models import GovTool, ScanRecord, TierCell

# finding.code 접두사 → 도구 area. 스캐너(scanner.py)의 코드 규약과 정합.
_CODE_PREFIX_TO_AREA = {
    "SECRET_": "secret",
    "SAST_": "sast",
    "MCP_": "mcp_poison",
    "CVE_": "sbom_cve",
    "SBOM_": "sbom_cve",
    "PII_": "pii",
    "SIGN_": "signing",
    "REDTEAM_": "llm_redteam",
    "THREAT_": "agent_intent",   # LLM-judge 위협 판정
}


def _area_of_finding(code: str) -> str | None:
    for prefix, area in _CODE_PREFIX_TO_AREA.items():
        if code.startswith(prefix):
            return area
    return None


def active_findings(findings: list, stages: list) -> list:
    """현재 게이트(stages)가 다루는 area의 finding만 남겨요.

    도구를 tier에서 제거하면(예: presidio 제거) 과거 스캔 레코드에 남은 그 도구의
    finding(PII_*)이 유령처럼 계속 조회되는 걸 막아요. 스캔 결과는 이력으로 보존하되,
    현재 파이프라인이 다루지 않는 area는 표시·집계에서 제외해요. area 매핑이 없는
    finding(예: SCANNER_ERROR)은 파이프라인 무관 신호라 항상 보존해요.
    """
    active_areas = {s.area for s in stages}
    kept = []
    for f in findings or []:
        area = _area_of_finding(str(f.get("code", "")))
        if area is None or area in active_areas:
            kept.append(f)
    return kept


_RISK_RANK = {"none": 0, "low": 1, "medium": 2, "high": 3}
_RANK_RISK = {v: k for k, v in _RISK_RANK.items()}


def risk_of_findings(findings: list) -> str:
    """finding 목록의 최고 severity를 risk로 계산해요(none/low/medium/high).

    active_findings로 걸러진 뒤 호출하면, 제거된 도구(예: presidio)의 finding이
    빠진 실제 risk가 나와요. 스캔 레코드에 저장된 risk는 옛 파이프라인 기준이라
    표시 계층에서 이 값으로 대체해 정합을 맞춰요.
    """
    worst = 0
    for f in findings or []:
        worst = max(worst, _RISK_RANK.get(str(f.get("severity", "none")), 0))
    return _RANK_RISK[worst]


# registry descriptor_type(값) → 도구 target_asset_types 키. 소스 바인딩이 있는 3종만.
# App·Model·Custom 등 구동형 아닌 타입은 매핑 없음(스캔 대상 아님 → 필터 시 게이트 0).
_DESCRIPTOR_TO_ASSET_KEY = {
    "MCP": "mcp",
    "Agent": "agent",
    "Agent Skills": "skill",
}


def asset_key_of(descriptor_type: str | None) -> str | None:
    """registry descriptor_type을 도구 자산 타입 키(mcp/skill/agent)로 변환해요.

    매핑 없는 타입(App·Model 등)이나 None이면 None을 돌려줘 게이트 필터를 적용하지
    않게 해요(등급 매트릭스 전체 노출 — 기존 동작 유지).
    """
    return _DESCRIPTOR_TO_ASSET_KEY.get(descriptor_type or "")


@dataclass
class GateStage:
    """게이트 파이프라인의 한 단계 (도구 1개)."""
    tool_id: str
    tool_name: str
    area: str
    enforcement: str          # required | warn | off
    # pending | running | pass | fail | not_run | not_applicable | unknown
    #   not_run        = 실행돼야 했는데 안 됨(가짜 통과 금지 → verdict pending).
    #   not_applicable = 이 자산엔 검사 대상이 아예 없음(소스 없는 자산의 소스 기반 도구).
    #                    verdict 판정에서 제외해요 — "실패"도 "통과"도 아니에요.
    #   unknown        = 적용성을 관측하지 못함. 분모에는 남고 자동승인을 막아요.
    state: str
    findings_count: int = 0


def _settled_state(scan, area: str, findings_by_area: dict, scanned_areas: list) -> tuple[str, int]:
    """검사가 끝난(또는 base로 확정된) area의 게이트 상태·검출 수.

    finding 있으면 fail, 이 area를 실제 검사했으면 pass, 아니면 not_run(가짜 통과 금지).
    부분 재스캔 중 "재실행하지 않는 area"의 이전 상태를 그대로 보여줄 때도 재사용해요.
    """
    count = findings_by_area.get(area, 0)
    # scanner_kind가 채워져 있으면(stepfn/fargate 등) area 기반 엄격 판정.
    # "" 이면 레거시(StaticScanner) 하위호환.
    strict = bool(getattr(scan, "scanner_kind", ""))
    if count > 0:
        return "fail", count
    if area in scanned_areas:
        # 실제로 이 area를 검사했고 finding 없음 → 진짜 통과.
        return "pass", count
    if scanned_areas or strict:
        # 스캔은 됐지만 이 area는 검사 안 됨(부분 스캔), 또는 비레거시인데 아무 area도
        # 검사 못 함. finding 0을 통과로 오인하지 않도록 미실행 표시.
        return "not_run", count
    # 레거시(StaticScanner, scanned_areas 빈) 하위호환으로 pass(무회귀).
    return "pass", count


def compute_gates(
    tier: str,
    cells: list[TierCell],
    tools: list[GovTool],
    scan: ScanRecord | None,
    asset_type: str | None = None,
    has_source: bool = True,
    scan_applicability: str = "applicable",
) -> list[GateStage]:
    """목표 등급의 요구 도구별 게이트 단계 상태를 계산해요.

    - enforcement == "off" 셀은 제외.
    - asset_type 지정 시: 그 자산 타입을 target_asset_types에 포함하지 않는 도구의
      셀은 제외해요(예: skill 자산엔 mcp 전용 도구가 게이트로 뜨지 않음). 미지정(None)
      이면 자산 타입 필터 없이 등급 매트릭스 전체를 게이트로 노출(기존 동작·무회귀).
    - has_source=False(소스 번들 없는 자산: connect형 MCP·도메인 연결 agent) → 소스가
      있어야 성립하는 도구(needs_source)는 "not_applicable". 검사 대상이 없는데 빈 입력을
      돌려 "통과"로 굳히지 않고, verdict 판정에서도 빼요(실측 2026-07-28 결함 수정).
    - scan_applicability=not_applicable/unknown이면 소스 기반 도구에 그 상태를 먼저
      투영해 목록·상세·자동/수동 판정이 같은 사실을 사용해요.
    - scan 없음(미스캔) → 모든 단계 state="pending".
    - scan 있음 → 도구 area에 해당하는 finding이 있으면 "fail", 없으면 "pass".
    - 부분 재스캔 진행 중(running + rescan_area) → 그 area만 "running", 나머지는 running
      레코드에 실린 base 상태를 유지해요. 전체를 running으로 덮으면 "도구 하나만 재시도했는데
      전부 스캔중"으로 보여요(실측 2026-07-28 제품 오너 확인).
    """
    tool_by_id = {t.tool_id: t for t in tools}
    scanned_areas = list(getattr(scan, "scanned_areas", None) or []) if scan is not None else []
    # 부분 재스캔 중이면 이 area만 running으로 보여요(나머지는 base 상태 유지).
    rescan_area = str(getattr(scan, "rescan_area", "") or "") if scan is not None else ""

    # 스캔 findings를 area별 개수로 집계.
    findings_by_area: dict[str, int] = {}
    # 스캔 자체가 실패한 finding(소스 로드 실패·스캐너 예외 — 특정 도구 area에 안 붙음).
    # 있으면 어떤 도구도 실제 검사 못 한 것이므로 전 게이트를 not_run으로 처리해요.
    # 단, finding이 특정 area를 지목하면(code 접두사 매핑 또는 명시 area 필드) 그 area만
    # 다뤄요 — 부분 재스캔 실패는 재실행한 area에만 SCANNER_ERROR를 실어 다른 area 결과가
    # 통째로 회귀하지 않게 해요.
    scan_failed = False
    if scan is not None:
        for f in scan.findings or []:
            code = str(f.get("code", ""))
            # area는 code 접두사 매핑 우선, 없으면 finding의 명시 area 필드로 폴백.
            area = _area_of_finding(code) or (str(f.get("area")) if f.get("area") else None)
            if code in ("SOURCE_MISSING", "SCANNER_ERROR") and area is None:
                # area를 특정 못 하는 scan-level 실패 → 전 게이트 미실행(가짜 통과 방지).
                scan_failed = True
            if area:
                findings_by_area[area] = findings_by_area.get(area, 0) + 1

    stages: list[GateStage] = []
    for cell in cells:
        if cell.enforcement == "off":
            continue
        tool = tool_by_id.get(cell.tool_id)
        # 자산 타입 필터: 도구가 이 자산 타입을 대상으로 하지 않으면 게이트에서 제외.
        # (도구 미등록이면 필터하지 않고 노출 — 매트릭스 셀이 SoT.)
        if asset_type and tool is not None and asset_type not in (tool.target_asset_types or ()):
            continue
        area = tool.area if tool else cell.tool_id
        name = tool.name if tool else cell.tool_id

        needs_source = tool is not None and getattr(tool, "needs_source", True)
        applicability_state = (
            scan_applicability
            if needs_source and scan_applicability in ("not_applicable", "unknown")
            else None
        )
        # 적용성은 스캔 결과보다 먼저 판정해요. unknown은 기존 pass를 재사용하지 않고
        # 관측 실패로 남기며, not_applicable만 verdict 분모에서 제외해요. 단, 독립적으로
        # 관측된 finding은 unknown보다 강한 실패 증거이므로 fail 판정을 보존해요.
        has_observed_finding = findings_by_area.get(area, 0) > 0
        if (
            applicability_state is not None
            and not (
                applicability_state == "unknown"
                and has_observed_finding
            )
        ):
            stages.append(GateStage(
                tool_id=cell.tool_id, tool_name=name, area=area,
                enforcement=cell.enforcement, state=applicability_state, findings_count=0,
            ))
            continue

        # 소스 없는 자산의 소스 기반 도구 → 검사 대상 자체가 없음. 스캔 상태와 무관하게
        # not_applicable(스캔 전에도 "대기중"으로 헛기다리지 않게).
        if not has_source and needs_source:
            stages.append(GateStage(
                tool_id=cell.tool_id, tool_name=name, area=area,
                enforcement=cell.enforcement, state="not_applicable", findings_count=0,
            ))
            continue

        if scan is not None and scan.status == "running" and rescan_area:
            # 부분 재스캔 진행 중 — 재실행 중인 area만 running, 나머지는 running 레코드에
            # 실린 base(findings/scanned_areas)로 이전 상태를 그대로 보여줘요. 전체를
            # running으로 덮으면 "trivy만 눌렀는데 전부 스캔중"으로 보여요(실측 2026-07-28).
            if area == rescan_area:
                state, count = "running", 0
            else:
                state, count = _settled_state(scan, area, findings_by_area, scanned_areas)
        elif scan is None or scan.status != "done":
            state, count = "pending", 0
        elif scan_failed:
            # 소스 로드 실패·스캐너 예외 → 이 도구도 실제로 검사되지 않음(미실행).
            state, count = "not_run", 0
        else:
            state, count = _settled_state(scan, area, findings_by_area, scanned_areas)

        stages.append(GateStage(
            tool_id=cell.tool_id, tool_name=name, area=area,
            enforcement=cell.enforcement, state=state, findings_count=count,
        ))
    return stages


def gate_summary(stages: list[GateStage]) -> dict:
    """진행상태 요약 — 통과/전체, 자동 판정 힌트.

    not_applicable(이 자산엔 검사 대상 없음) 단계는 분모(total)와 verdict 판정에서 모두
    빼요 — 영원히 통과하지 않는 단계를 "0/3 통과"로 세면 진행률이 거짓이 되고, 대기로
    세면 승인이 영구 차단돼요.
    """
    countable = [s for s in stages if s.state != "not_applicable"]
    total = len(countable)
    passed = sum(1 for s in countable if s.state == "pass")
    required = [s for s in countable if s.enforcement == "required"]
    required_fail = any(s.state == "fail" for s in required)
    # 필수 게이트가 아직 안 끝난 상태 = 대기(pending) 또는 미실행(not_run).
    # 미실행을 통과로 오인해 auto-approve로 넘기지 않도록 둘 다 "대기"로 묶어요.
    required_incomplete = any(
        s.state in ("pending", "not_run", "running", "unknown")
        for s in required
    )
    # warn 단계도 unknown이면 관측 실패예요. enforcement와 무관하게 unknown을 자동 통과로
    # 표시하지 않으며, 정책상 진행을 허용하더라도 사람 판정과 원장에는 override로 남겨요.
    has_unknown = any(s.state == "unknown" for s in countable)
    # 경고(warn) 게이트가 fail이면 자동승인하지 않고 사람 판단으로 넘겨요(2026-07-28 제품 오너
    # 결정). warn은 "차단하진 않지만 그냥 통과시킬 수도 없는" 신호예요 — 특히 소스 없는
    # 자산에선 llm-judge가 유일한 실효 검사라, 그 fail을 자동승인으로 삼키면 위협 있는 자산이
    # 무검토 통과해요. auto-reject(자동 반려)는 아니고 pending(사람 심사 유도)이에요.
    warn_fail = any(s.state == "fail" for s in countable if s.enforcement == "warn")

    if total == 0:
        # 요구 게이트가 없거나 전부 not_applicable → 자동 판정 대상 아님(사람 심사 대기).
        verdict = "none"
    elif required_fail:
        verdict = "auto-reject"       # 필수 게이트 미통과
    elif required_incomplete or warn_fail or has_unknown:
        verdict = "pending"           # 스캔·검토 대기, 일부 미실행, 또는 경고 게이트 fail
    else:
        verdict = "auto-approve"      # 필수 전부 통과 + 경고 fail 없음
    return {"passed": passed, "total": total, "verdict": verdict}


def effective_verdict(scan_verdict: str, status: str) -> str:
    """스캔 게이트 verdict + 레지스트리 status(사람 최종 결정)를 합친 진행상태.

    사람이 최종 결정(APPROVED/REJECTED/DEPRECATED)한 자산은 그 결정을 우선 반영해요.
    특히 스캔 게이트가 auto-reject/pending이었는데 사람이 승인했다면 soft override —
    "approved-override"로 구분해 '자동 REJECT'로 오인되지 않게 해요. 아직 결정 전
    (DRAFT/PENDING_APPROVAL)이면 스캔 게이트 verdict를 그대로 써요.
    반환값: approved | approved-override | rejected | deprecated | 그리고 미결정 시
    scan_verdict(none/auto-approve/auto-reject/pending) 그대로.
    """
    if status == "APPROVED":
        # 스캔이 통과가 아니었는데 승인 = override.
        return "approved-override" if scan_verdict in ("auto-reject", "pending") else "approved"
    if status == "REJECTED":
        return "rejected"
    if status == "DEPRECATED":
        return "deprecated"
    return scan_verdict
