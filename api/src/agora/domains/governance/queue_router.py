"""승인 큐 + 게이트 파이프라인 + 판정 (§3-M1·M2).

큐 목록·게이트 상태·목표등급·승인/반려를 제공해요. 게이트는 등급 매트릭스(SoT)와
최신 스캔으로 gate.compute_gates가 계산하고, 판정은 DECISION으로 감사 기록해요.

큐 열거는 RegistryPort.list_records(상태 무관)로 전 상태(DRAFT/PENDING/APPROVED/REJECTED)를
심사 대상으로 실어요 — catalog(사용자)는 계속 search(APPROVED만)를 쓰지만, 큐는 심사 전
DRAFT 자산도 봐야 하거든요. 각 항목엔 진행상태(scan_status)를 주석 달아요.
"""
from __future__ import annotations

import datetime as _dt

from fastapi import APIRouter, Depends, HTTPException, Request

from ...shared.deps import get_gov_store, get_registry, get_registry_id
from .authz import ADMIN_ONLY, CONSOLE_ROLES, require_role
from .gate import (
    active_findings, asset_key_of, compute_gates, effective_verdict, gate_summary,
    risk_of_findings,
)
from . import approval_block
from .models import DecisionRecord
from .overlap_review import overlap_view
from .scan_applicability import (
    scan_applicability,
    scan_applicability_for_record,
)
from .tool_catalog import list_catalog_tools

router = APIRouter(tags=["governance-queue"])


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _asset_key(record_id: str) -> str | None:
    """record_id → 도구 자산 타입 키(mcp/skill/agent). registry best-effort 조회.

    조회 실패/매핑 없는 타입이면 None → 게이트 자산타입 필터 미적용(전체 노출).
    """
    try:
        rec = get_registry().get_record(get_registry_id(), record_id)
        dt = getattr(rec, "descriptor_type", None)
        return asset_key_of(dt.value if hasattr(dt, "value") else str(dt) if dt else None)
    except Exception:
        return None


def has_source_bundle(source_prefix: str, *, asset_key: str | None = None,
                      descriptors: dict | None = None) -> bool:
    """이 자산에 소스 기반 도구가 읽을 본문이 있는지.

    세 경로 중 하나면 True예요:
      - 소스 스토어 관리형: source_prefix 4-part(`{type}/{owner}/{name}/{version}/`).
      - skill 자산: 본문(SKILL.md)이 등록 필수라 인라인이어도 항상 검사 대상이 있어요.
        (큐 목록은 SearchHit만 있어 descriptors를 못 봐요 — 타입으로 판정해 목록과 상세가
        같은 답을 내게 해요.)
      - 인라인 본문형: descriptors에 본문이 직접 들어있는 자산.
    scan_service._load_source의 판정과 같은 규칙이라야 해요 — 어긋나면 "실행은 안 했는데
    게이트는 계속 기다리는"(또는 그 반대) 상태가 나요.
    """
    if len([p for p in (source_prefix or "").split("/") if p]) >= 4:
        return True
    if asset_key == "skill":
        return True
    from .scan_service import _inline_source_files
    return bool(_inline_source_files(descriptors or {}))


def _has_source(record_id: str) -> bool:
    """record_id → 검사 대상 본문 보유 여부. registry best-effort(실패 시 True=기존 동작)."""
    try:
        rec = get_registry().get_record(get_registry_id(), record_id)
        dt = getattr(rec, "descriptor_type", None)
        return has_source_bundle(
            getattr(rec, "source_prefix", "") or "",
            asset_key=asset_key_of(dt.value if hasattr(dt, "value") else (str(dt) if dt else None)),
            descriptors=getattr(rec, "descriptors", {}) or {},
        )
    except Exception:
        return True


def tier_for_record(record_id: str) -> str:
    """에셋 타입 → 설정의 스캔 등급 (게이트 SoT, SP-1).

    _asset_key로 타입(mcp/skill/agent) 조회 후 settings.asset_tier_map에서 등급을 읽어요.
    매핑 없는 타입(App/Model 등)이나 조회 실패면 "minimal"(가장 안전한 기본).
    """
    key = _asset_key(record_id)
    tmap = get_gov_store().get_settings().asset_tier_map or {}
    return tmap.get(key or "", "minimal")


def _record_status(record) -> str:
    status = getattr(record, "status", None)
    return status.value if hasattr(status, "value") else (str(status) if status else "UNKNOWN")


def _scan_applicability(record) -> dict[str, str]:
    """하위호환 dict 표현. 판정 정본은 scan_applicability 모듈이에요."""
    return scan_applicability(record).as_dict()


def _asset_meta(record_id: str) -> dict:
    """record_id → 자산 메타(이름·타입·소유·태그·endpoint·버전). registry best-effort 조회.

    상세화면 AssetMetaCard가 렌더할 표시용 메타예요. 조회 실패/미존재면 record_id를
    이름으로 한 빈 기본값을 돌려줘 500 없이 안전하게 폴백해요(게이트 계산과 독립).
    endpoint는 소스 바인딩 노드가 아니라 descriptors의 타입별 노드에 있어요(mcp/agent/app).
    """
    meta = {
        "name": record_id, "descriptor_type": "", "owner_user": "", "owner_team": "",
        "tags": [], "endpoint": "", "version": "", "updated_at": "",
        "status": "UNKNOWN",
        "scan_applicability": {
            "state": "unknown",
            "reason": "자산 정보를 관측하지 못해 스캔 적용 여부를 확인할 수 없음",
        },
    }
    try:
        rec = get_registry().get_record(get_registry_id(), record_id)
    except Exception:
        return meta
    dt = getattr(rec, "descriptor_type", None)
    meta["name"] = rec.name or record_id
    meta["descriptor_type"] = dt.value if hasattr(dt, "value") else (str(dt) if dt else "")
    meta["owner_user"] = getattr(rec, "owner_user", "") or ""
    meta["owner_team"] = getattr(rec, "owner_team", "") or ""
    meta["tags"] = list(getattr(rec, "tags", ()) or ())
    meta["version"] = getattr(rec, "version", "") or ""
    meta["updated_at"] = getattr(rec, "updated_at", "") or ""
    meta["status"] = _record_status(rec)
    meta["scan_applicability"] = _scan_applicability(rec)
    # endpoint: descriptors의 타입별 노드에서 best-effort. 소스 관리형(skill)엔 없을 수 있음.
    descriptors = getattr(rec, "descriptors", {}) or {}
    if isinstance(descriptors, dict):
        for key in ("mcp", "agent", "app"):
            node = descriptors.get(key)
            if isinstance(node, dict) and isinstance(node.get("endpoint"), str):
                meta["endpoint"] = node["endpoint"]
                break
    return meta


def _threat_summary(findings: list) -> list[dict]:
    """findings를 code별 개수로 집계 → [{"code":..,"count":..}] (검출위협 요약, F4)."""
    counts: dict[str, int] = {}
    for f in findings or []:
        code = str(f.get("code", "UNKNOWN"))
        counts[code] = counts.get(code, 0) + 1
    return [{"code": c, "count": n} for c, n in sorted(counts.items(), key=lambda x: -x[1])]


@router.get("/api/governance/queue", dependencies=[Depends(require_role(*CONSOLE_ROLES))])
def list_queue():
    # deps에서 지역 import로 접근자를 그때그때 해석해요 — 모듈 전역 바인딩을 쓰면 테스트가
    # deps.get_registry_id를 monkeypatch해도 반영되지 않아 잘못된 registry_id로 열거하거든요(_decide와 동일).
    from ...shared.deps import get_registry as _get_registry, get_registry_id as _get_registry_id
    store = get_gov_store()
    registry = _get_registry()
    reg_id = _get_registry_id()
    tools = list_catalog_tools()

    # 성능(N+1 제거): record마다 불변인 로드를 루프 밖에서 한 번만 준비해요.
    # - asset_tier_map: 설정은 목록 조회 동안 안 바뀌므로 1회만 읽어요(기존엔 record마다 get_settings).
    # - tier cells: 등급은 minimal/standard/strong 3종뿐이라 등급별로 1회만 만들어 재사용해요
    #   (기존엔 record마다 store.get_tier()가 TierCell 리스트를 새로 조립).
    # 또한 tier는 hit.descriptor_type로 바로 도출해요 — tier_for_record가 하던 record마다의
    # registry get_record 왕복(진짜 N+1)을 없애요. hit.descriptor_type은 get_record 값과
    # 동일 매핑(aws_mapping._core)이라 반환 계약은 그대로예요.
    tier_map = store.get_settings().asset_tier_map or {}
    _cells_cache: dict[str, list] = {}

    def _cells_for(tier: str) -> list:
        cached = _cells_cache.get(tier)
        if cached is None:
            cached = store.get_tier(tier)
            _cells_cache[tier] = cached
        return cached

    # 큐는 모든 상태(DRAFT/PENDING/APPROVED/REJECTED)를 심사 대상으로 봐요.
    # catalog(사용자)는 계속 search(APPROVED만)를 쓰지만, 큐는 list_records로 전 상태 열거.
    try:
        hits = registry.list_records(reg_id, max_results=1000)
    except Exception:
        hits = []

    items = []
    for hit in hits:
        dt = hit.descriptor_type.value if hasattr(hit.descriptor_type, "value") else str(hit.descriptor_type)
        asset_key = asset_key_of(dt)
        # tier_for_record와 동일: 자산 타입 키 → asset_tier_map, 미매핑/None이면 minimal.
        tier = tier_map.get(asset_key or "", "minimal")
        scan = store.latest_scan(hit.record_id)
        cells = _cells_for(tier)
        applicability = scan_applicability(hit)
        # hit.source_prefix가 이미 실려 와서(N+1 회피) 추가 get_record 없이 소스 유무를 알아요.
        stages = compute_gates(tier=tier, cells=cells, tools=tools, scan=scan, asset_type=asset_key,
                               has_source=has_source_bundle(getattr(hit, "source_prefix", "") or "",
                                                            asset_key=asset_key),
                               scan_applicability=applicability.state)
        summary = gate_summary(stages)
        status_str = _record_status(hit)
        # 진행상태 verdict에 사람의 최종 결정(APPROVED/REJECTED/DEPRECATED)을 합쳐요.
        # soft override(스캔은 reject였지만 사람이 승인)를 'approved-override'로 구분해 표시.
        summary = {**summary, "verdict": effective_verdict(summary["verdict"], status_str)}
        scan_status = scan.status if scan else "none"      # none | running | done | failed
        scanning = scan_status == "running"
        # 현재 tier가 다루는 area의 finding만(제거된 도구의 과거 finding 제외).
        # risk도 이 필터된 findings 기준으로 재계산해 정합을 맞춰요(옛 저장 risk 대신).
        shown = active_findings(scan.findings, stages) if scan else []
        items.append({
            "record_id": hit.record_id,
            "name": hit.name,
            "descriptor_type": hit.descriptor_type.value if hasattr(hit.descriptor_type, "value") else str(hit.descriptor_type),
            "status": status_str,
            "owner_user": hit.owner_user,
            "updated_at": getattr(hit, "updated_at", "") or "",   # 등록/갱신일자(정렬·표시)
            "target_tier": tier,
            "progress": summary,                       # {passed,total,verdict(effective)}
            "risk": None if scanning else (risk_of_findings(shown) if scan else None),  # 진행중엔 감춤
            "scanned": scan is not None and not scanning,
            "scan_status": scan_status,                # 진행중 표시·버튼 disabled 근거
            "scan_applicability": applicability.as_dict(),
            "threats": [] if scanning else _threat_summary(shown),
            # 중복 후보 요약(목록은 개수·band만, 상세는 /gates에서). 스캔과 독립 축이라
            # 스캔 진행 중에도 감추지 않아요.
            "overlap": {k: v for k, v in overlap_view(hit.record_id).items()
                        if k != "candidates"},
            # 자동승인이 진행되지 않은 사유(R3). None은 "막힌 기록 없음"이고 승인됨이
            # 아니에요 — 승인 여부는 status/progress.verdict로만 읽어야 해요.
            # 이미 승인된 자산엔 싣지 않아요(trust_adapter와 같은 규칙) — 옛 사유가
            # "승인됐는데 연락처를 채우라"는 모순된 안내로 남지 않게요.
            "approval_block": (None if status_str == "APPROVED"
                               else approval_block.view(hit.record_id, store=store)),
        })
    # 기본 정렬: 등록/갱신일자 최신순(updated_at 내림차순). 값 없으면 뒤로.
    items.sort(key=lambda x: x["updated_at"], reverse=True)
    return {"items": items}


@router.get("/api/governance/queue/{record_id}/gates", dependencies=[Depends(require_role(*CONSOLE_ROLES))])
def get_gates(record_id: str):
    store = get_gov_store()
    asset = _asset_meta(record_id)
    # UI(ReviewClient)는 /gates만 폴링하므로, 여기서 stepfn running을 SF 완료로 당겨요.
    # 안 하면 SF가 SUCCEEDED여도 스캔이 running에 영구히 갇혀 게이트가 pending에서 멈춰요.
    from .scan_service import sync_running_stepfn
    sync_running_stepfn(record_id)
    tier = tier_for_record(record_id)
    scan = store.latest_scan(record_id)
    cells = store.get_tier(tier)
    tools = list_catalog_tools()
    stages = compute_gates(tier=tier, cells=cells, tools=tools, scan=scan,
                           asset_type=_asset_key(record_id), has_source=_has_source(record_id),
                           scan_applicability=asset["scan_applicability"]["state"])
    scan_status = scan.status if scan else "none"   # none | running | done | failed
    # 부분 재스캔(rescan_area 있음)은 "전체 진행중"이 아니에요 — base 결과·risk를 그대로
    # 보여주고 그 도구 행만 running으로 표시해요. 전체 스캔만 화면을 비워요.
    rescan_area = str(getattr(scan, "rescan_area", "") or "") if scan else ""
    full_scanning = scan_status == "running" and not rescan_area
    # 상세 표시와 verdict가 같은 Registry 관측값을 사용해야 해요.
    status_str = asset["status"]
    summary = gate_summary(stages)
    summary = {**summary, "verdict": effective_verdict(summary["verdict"], status_str)}
    # 현재 tier가 다루는 area의 finding만(제거된 도구의 과거 finding 제외). risk도 이 기준으로.
    shown = active_findings(scan.findings, stages) if scan else []
    return {
        "record_id": record_id,
        "tier": tier,
        # 자산별 저장값은 위협리포트 라벨 전용이에요. 게이트 tier와 한 응답에서
        # 분리해 내려 화면이 둘을 같은 등급으로 오인하지 않게 해요.
        "threat_report_tier": store.get_target_tier(record_id),
        "scanned": scan is not None and not full_scanning,
        "scan_status": scan_status,                   # 진행중 표시·버튼 disabled 근거
        # 부분 재스캔 중엔 base risk·findings를 유지해요(전체 스캔만 비움).
        "risk": None if full_scanning else (risk_of_findings(shown) if scan else None),
        "stages": [s.__dict__ for s in stages],       # running이면 compute_gates가 pending 처리
        "findings": [] if full_scanning else shown,
        # 진행 중인 부분 재스캔의 area("" = 전체 스캔 또는 미진행). 프론트가 이 area 행만
        # running으로 표시하고 나머지 행은 base 상태를 그대로 보여주는 근거예요.
        "rescan_area": rescan_area,
        "summary": summary,                            # verdict에 사람 결정(override) 반영
        "asset": asset,                               # 상세화면 AssetMetaCard용 메타(§3-M2)
        # 중복 후보 상세(후보 목록 포함) — 검토자가 무엇과 겹쳤는지 보고 판정해요.
        "overlap": overlap_view(record_id),
        # 자동승인이 진행되지 않은 사유(R3). 없으면 None. 승인된 자산엔 싣지 않아요.
        "approval_block": (None if status_str == "APPROVED"
                           else approval_block.view(record_id, store=store)),
    }


@router.get("/api/governance/queue/{record_id}/gates/{tool_id}/logs",
            dependencies=[Depends(require_role(*CONSOLE_ROLES))])
def gate_logs(record_id: str, tool_id: str):
    """게이트(도구) 스캔 로그 + 상태·findings. 진행상태 게이트 클릭 modal용.

    스캔 없으면 not_run(리더 미호출). 있으면 compute_gates로 이 도구 게이트 상태·findings를
    산출하고, running이면 SF 완료를 당긴 뒤 CloudWatch에서 scan_id 필터로 로그를 읽어요.
    """
    from ...shared.deps import get_scan_log_reader
    from .scan_service import sync_running_stepfn
    store = get_gov_store()
    sync_running_stepfn(record_id)               # running이면 done으로 당김(gates와 동일)
    scan = store.latest_scan(record_id)
    tier = tier_for_record(record_id)
    cells = store.get_tier(tier)
    applicability = scan_applicability_for_record(record_id)
    stages = compute_gates(tier=tier, cells=cells, tools=list_catalog_tools(), scan=scan,
                           asset_type=_asset_key(record_id), has_source=_has_source(record_id),
                           scan_applicability=applicability.state)
    stage = next((s for s in stages if s.tool_id == tool_id), None)
    gate_state = stage.state if stage else "pending"
    gate_findings = active_findings(scan.findings, [stage]) if (scan and stage) else []
    if scan is None:
        return {"tool_id": tool_id, "gate_state": gate_state, "findings": [],
                "log_status": "not_run", "lines": [], "scan_id": "", "scan_ts": ""}
    scan_id = getattr(scan, "scan_id", "") or record_id
    log = get_scan_log_reader().fetch(tool_id, scan_id)
    return {"tool_id": tool_id, "gate_state": gate_state, "findings": gate_findings,
            "log_status": log["log_status"], "lines": log["lines"],
            "scan_id": scan_id, "scan_ts": scan.ts}


@router.get("/api/governance/queue/{record_id}/report.md", dependencies=[Depends(require_role(*CONSOLE_ROLES))])
def threat_report(record_id: str):
    """위협리포트.md 렌더 (§2.1). 최신 스캔 findings + 에셋별 맞춤 수정가이드(Bedrock Sonnet 4.6)."""
    from fastapi.responses import PlainTextResponse
    from ...shared.deps import get_report_service

    store = get_gov_store()
    # 자산 메타(이름·타입·버전)는 registry에서 best-effort 조회. 없으면 record_id로 폴백.
    name, asset_type, version = record_id, "asset", ""
    try:
        rec = get_registry().get_record(get_registry_id(), record_id)
        name = rec.name or record_id
        dt = getattr(rec, "descriptor_type", None)
        asset_type = dt.value if hasattr(dt, "value") else (str(dt) if dt else "asset")
        version = rec.version or ""
    except Exception:
        pass

    tier = store.get_target_tier(record_id)
    md = get_report_service().get_report(
        record_id, asset_name=name, asset_type=asset_type, version=version, tier=tier,
    )
    # 파일명: {asset}_{version}_위협리포트_{YYMMDD}.md (화면설계 §2.1)
    # 한글 파일명은 latin-1 헤더에 못 담으니 RFC 5987 filename* 로 UTF-8 인코딩해요.
    from urllib.parse import quote
    ymd = _dt.datetime.now(_dt.timezone.utc).strftime("%y%m%d")
    fname = f"{name}_{version or 'v'}_위협리포트_{ymd}.md"
    disposition = f"attachment; filename*=UTF-8''{quote(fname)}"
    return PlainTextResponse(
        md, media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": disposition},
    )


@router.patch("/api/governance/queue/{record_id}/tier", dependencies=[Depends(require_role(*ADMIN_ONLY))])
def set_tier(record_id: str, body: dict):
    """자산별 위협리포트 라벨을 저장해요(admin 전용).

    이 값은 `threat_report`가 리포트 라벨 입력으로 읽고, `get_gates`는 편집 UI에 원값을
    전달만 해요. 게이트 상세는 `get_gates`→`tier_for_record`, 승인 큐 목록·필터는
    `list_queue`의 `settings.asset_tier_map`, 자동승인은
    `scan_service.apply_verdict_to_registry`→`tier_for_record`를 읽으므로 이 변경값이
    게이트 셀·큐 등급·자동승인 판정을 바꾸지 않아요. 응답의 `summary`도 같은 타입 등급으로
    계산하며, 저장한 라벨은 `target_tier`로 따로 돌려줘요.

    ⚠️ 이 쓰기가 `report.md` 본문에 도달하는 건 `ReportService._signature` 가 tier 를
    포함하기 때문이에요(IH-166 F3). 그 서명에서 tier 를 빼면 이 라우트는 조용히 no-op 이
    되고, 화면의 「이 값은 위협리포트 라벨에만 쓰여요」가 거짓이 돼요.
    """
    tier = body.get("tier", "")
    if tier not in ("minimal", "standard", "strong"):
        raise HTTPException(422, f"허용되지 않은 등급: {tier} (minimal/standard/strong)")
    store = get_gov_store()
    store.set_target_tier(record_id, tier)
    # 하위호환 summary는 타입 등급으로 계산해요. 위협리포트 라벨은 입력으로 쓰지 않아요.
    scan = store.latest_scan(record_id)
    eff_tier = tier_for_record(record_id)   # 게이트는 타입 등급으로 계산
    applicability = scan_applicability_for_record(record_id)
    stages = compute_gates(tier=eff_tier, cells=store.get_tier(eff_tier),
                           tools=list_catalog_tools(), scan=scan, asset_type=_asset_key(record_id),
                           has_source=_has_source(record_id),
                           scan_applicability=applicability.state)
    return {"record_id": record_id, "target_tier": tier, "summary": gate_summary(stages)}


@router.post("/api/governance/queue/{record_id}/approve", dependencies=[Depends(require_role(*CONSOLE_ROLES))])
def approve(record_id: str, body: dict, request: Request):
    return _decide(record_id, "APPROVE", body, request)


@router.post("/api/governance/queue/{record_id}/reject", dependencies=[Depends(require_role(*CONSOLE_ROLES))])
def reject(record_id: str, body: dict, request: Request):
    reason = (body or {}).get("reason", "").strip()
    if not reason:
        raise HTTPException(422, "반려 사유는 필수예요.")
    return _decide(record_id, "REJECT", body, request)


def _decide(record_id: str, decision: str, body: dict, request: Request):
    from ...shared.deps import get_current_principal

    store = get_gov_store()
    principal = get_current_principal(request).principal_id
    reason = (body or {}).get("reason", "").strip()

    # soft override 판정: 승인인데 필수 게이트가 통과 아니면 사유 필수 + override 플래그.
    tier = tier_for_record(record_id)
    scan = store.latest_scan(record_id)
    applicability = scan_applicability_for_record(record_id)
    stages = compute_gates(tier=tier, cells=store.get_tier(tier), tools=list_catalog_tools(), scan=scan,
                           asset_type=_asset_key(record_id), has_source=_has_source(record_id),
                           scan_applicability=applicability.state)
    summary = gate_summary(stages)
    override = decision == "APPROVE" and summary["verdict"] in ("auto-reject", "pending")
    if override and not reason:
        raise HTTPException(422, "게이트 미통과 상태 승인(soft override)은 사유가 필요해요.")

    from ...shared.deps import get_registry as _get_registry, get_registry_id as _get_registry_id
    from ...domains.catalog.registry.models import RecordStatus  # 순환 방지
    from .scan_service import _hide_sibling_versions, _ensure_pending_before_verdict
    target = RecordStatus.APPROVED if decision == "APPROVE" else RecordStatus.REJECTED
    registry, reg_id = _get_registry(), _get_registry_id()
    if target is RecordStatus.APPROVED:
        from ...shared.deps import get_asset_responsibility_port
        from ...shared.responsibility import ResponsibilityIncomplete

        try:
            get_asset_responsibility_port().require_for_approval(record_id)
        except ResponsibilityIncomplete as exc:
            raise HTTPException(
                422,
                {
                    "message": (
                        "production 승인을 위해 책임자 연락 계약을 완성해야 해요."
                    ),
                    "blocking_reasons": list(
                        exc.status.blocking_reasons
                    ),
                },
            )
    rec = DecisionRecord(
        ts=_now(), principal=principal, decision=decision, reason=reason, override=override,
    )
    store.add_decision(record_id, rec)
    # reviewer 결정을 registry 상태로 반영. APPROVE→APPROVED(+이전버전 숨김), REJECT→REJECTED.
    # reviewer 결정이 authoritative — soft override APPROVE도 APPROVED로 굳혀요(gate verdict 무관).
    # deps에서 지역 import로 접근자를 그때그때 해석해요 — 모듈 전역 바인딩을 쓰면 테스트가
    # deps.get_registry_id를 monkeypatch해도 반영되지 않아 잘못된 registry_id로 조회하거든요.
    # DRAFT면 먼저 PENDING으로 승격 — 큐가 DRAFT도 싣는데(Task 8) DRAFT→APPROVED/REJECTED는
    # 불법이라 그냥 두면 아래 update_status가 삼켜져 판정-상태 불일치가 나요(self-heal).
    _ensure_pending_before_verdict(registry, reg_id, record_id)
    try:
        registry.update_status(reg_id, record_id, target,
                               reason=reason or f"reviewer {decision}")
        if target is RecordStatus.APPROVED:
            # 사람이 승인했으니 자동승인 차단 사유는 더 이상 사실이 아니에요 — 지워요.
            approval_block.clear(record_id, store=store)
            _hide_sibling_versions(record_id)
    except Exception:
        pass
    return {"record_id": record_id, "decision": decision, "override": override, "reason": reason}
