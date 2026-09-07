"""스캔 오케스트레이션 순수함수 (SP-4).

resolve_tools: 등급 셀 + 도구 카탈로그 + 자산타입 → 실행할 도구목록(Step Functions Map 입력).
aggregate_results: 도구별 result.json → 병합 결과(fan-in). 둘 다 순수함수 — Lambda 핸들러와
StepFunctionScanRunner가 이 로직을 공유(핸들러는 배포 패키징상 복제).
"""
from __future__ import annotations

from .gate import asset_key_of  # noqa: F401 (asset_key_of는 호출부가 이미 타입 변환 후 넘김)

_RISK_ORDER = {"none": 0, "low": 1, "medium": 2, "high": 3}


def resolve_tools(cells: list, tools: list, asset_type: str | None, judge_model: str = "",
                  has_source: bool = True) -> list[dict]:
    """등급이 요구하는(off 아님) + 자산타입 대상 도구를 실행 목록으로.

    반환: [{"tool_id","area","compute","image_ref"}]. gate.compute_gates의 필터 규칙과 정합
    (enforcement off 제외, asset_type이 target_asset_types에 없으면 제외, has_source=False면
    needs_source 도구 제외 → 게이트 not_applicable과 1:1).
    llm-judge 항목에는 추가로 model_alias(등급별 Bedrock 모델 별칭)가 붙어요.
    """
    tool_by_id = {t.tool_id: t for t in tools}
    out: list[dict] = []
    for cell in cells:
        if cell.enforcement == "off":
            continue
        tool = tool_by_id.get(cell.tool_id)
        if tool is None:
            continue
        if asset_type and asset_type not in (tool.target_asset_types or ()):
            continue
        # 소스 없는 자산에서 소스 기반 도구는 실행하지 않아요 — 빈 입력을 스캔하면
        # finding 0 + scanned_areas 채워짐 = 게이트가 "진짜 통과"로 오인해요(가짜 승인).
        if not has_source and getattr(tool, "needs_source", True):
            continue
        out.append({
            "tool_id": tool.tool_id,
            "area": tool.area,
            "compute": getattr(tool, "compute", "lambda"),
            "image_ref": getattr(tool, "image_uri", "") or "",
            # model_alias는 모든 도구 항목에 항상 포함해요.
            # Step Functions itemSelector의 "model_alias.$" 참조가 필드 부재 시 실패하므로
            # llm-judge에는 실제 모델 별칭을, 그 외 도구에는 ""를 붙여요.
            # dispatch_handler는 falsy("")를 받으면 payload에서 제외해요(Task 6).
            "model_alias": judge_model if tool.tool_id == "llm-judge" else "",
        })
    return out


def merge_partial_scan(base_findings: list, base_areas: list, rescan_area: str,
                       new_findings: list, new_areas: list) -> dict:
    """부분 재스캔 결과를 base done scan에 merge — "재실행한 area"만 교체해요.

    부분 재스캔이 새 scan_id로 done 레코드를 만들면 그 부분 결과(재실행한 도구 1개 area만 든
    scanned_areas)가 latest_scan이 돼 compute_gates가 나머지 area를 not_run으로 회귀시켜요
    (전체 스캔 결과가 시각적으로 "지워지는" 결함). 그래서 base에서 rescan_area 몫만 갈아끼워요.

    - findings: base_findings에서 rescan_area에 매칭되는 finding 제거(gate._area_of_finding) +
      new_findings 삽입. area 매핑 없는 finding(SCANNER_ERROR 등)은 특정 도구 area에 안 붙으니
      보존해요.
    - scanned_areas: (base_areas에서 rescan_area 제외) ∪ new_areas(순서 보존).
    - risk: 남은 findings 최고 severity(gate.risk_of_findings 재사용 — 저장 risk 대신 재계산).
    """
    from .gate import _area_of_finding, risk_of_findings

    kept = [f for f in (base_findings or [])
            if _area_of_finding(str(f.get("code", ""))) != rescan_area]
    findings = kept + list(new_findings or [])

    areas: list = [a for a in (base_areas or []) if a != rescan_area]
    for a in (new_areas or []):
        if a not in areas:
            areas.append(a)

    return {"findings": findings, "scanned_areas": areas, "risk": risk_of_findings(findings)}


def aggregate_results(tool_results: list[dict]) -> dict:
    """도구별 result({risk,findings,scanned_areas})를 병합 (fan-in).

    max risk, findings 합침, scanned_areas 합집합(순서 보존). scanned_areas 결측 방어(.get).
    """
    findings: list = []
    areas: list = []
    worst = "none"
    for r in tool_results or []:
        findings.extend(r.get("findings", []) or [])
        for a in (r.get("scanned_areas") or []):
            if a not in areas:
                areas.append(a)
        risk = r.get("risk", "none")
        if _RISK_ORDER.get(risk, 0) > _RISK_ORDER.get(worst, 0):
            worst = risk
    return {"risk": worst, "findings": findings, "scanned_areas": areas}
