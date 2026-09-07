"""대시보드 집계 + 감사 이력 조회 (§3-M5·M6).

대시보드는 큐 자산(등급·위험·게이트 통과율)과 판정 이력(DECISION)을 집계해요.
감사(decisions)는 append-only DECISION 기록을 principal·record 축으로 조회해요.
관측이지 결정이 아니에요 — 읽기 전용.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from ...shared.deps import get_gov_store, get_registry, get_registry_id
from .authz import CONSOLE_ROLES, require_role
from .gate import (
    active_findings, asset_key_of, compute_gates, effective_verdict, gate_summary,
    risk_of_findings,
)
from .tool_catalog import list_catalog_tools
from .scan_applicability import scan_applicability

router = APIRouter(tags=["governance-dashboard"])


def _agent_model_of(descriptors: dict) -> str:
    """Agent가 쓰는 모델 표시명. Agent가 아니거나 미저장이면 "".

    W4에서 `descriptors.agent.model`(표시명)과 `bedrockModelId`(실 ID)를 함께 저장해요.
    화면에는 표시명을 쓰고, 없으면 빈 문자열로 degrade해요 — 옛 배포 자산은 값이 없어요.
    """
    if not isinstance(descriptors, dict):
        return ""
    node = descriptors.get("agent")
    if not isinstance(node, dict):
        return ""
    value = node.get("model")
    return value.strip() if isinstance(value, str) else ""


def _queue_annotations():
    """큐 자산별 (record_id, name, tier, risk, status, gate summary, stages)를 집계 소스로 돌려줘요.

    큐(queue_router)와 동일하게 list_records(전 상태: DRAFT/PENDING/APPROVED/REJECTED)를 소스로
    써요 — search는 APPROVED만 돌려줘 대시보드가 큐와 불일치했거든요(DRAFT 등 미표시). 각 row에
    라이프사이클 status를 담아 상태 분포 집계에 쓰게 해요. record_id·name은 미스캔 자산 목록용.

    성능(N+1 제거, queue_router.list_queue와 동일 패턴): record마다 불변인 로드를 루프 밖에서
    한 번만 준비해요.
    - tier는 rec.descriptor_type로 바로 도출 — tier_for_record가 하던 record마다의 registry
      get_record 왕복(진짜 N+1)을 없애요. rec.descriptor_type은 get_record 값과 동일 매핑이라
      파생 tier·반환 집계는 그대로예요(순수 최적화).
    - asset_tier_map: 목록 조회 동안 안 바뀌므로 1회만 읽어요(기존엔 record마다 get_settings).
    - tier cells: 등급은 minimal/standard/strong 3종뿐이라 등급별 1회만 만들어 재사용해요.
    """
    store = get_gov_store()
    tools = list_catalog_tools()
    tier_map = store.get_settings().asset_tier_map or {}
    _cells_cache: dict[str, list] = {}

    def _cells_for(tier: str) -> list:
        cached = _cells_cache.get(tier)
        if cached is None:
            cached = store.get_tier(tier)
            _cells_cache[tier] = cached
        return cached

    try:
        records = get_registry().list_records(get_registry_id(), max_results=1000)
    except Exception:
        records = []
    rows = []
    for rec in records:
        dt = rec.descriptor_type.value if hasattr(rec.descriptor_type, "value") else str(rec.descriptor_type)
        asset_key = asset_key_of(dt)
        # tier_for_record와 동일: 자산 타입 키 → asset_tier_map, 미매핑/None이면 minimal.
        tier = tier_map.get(asset_key or "", "minimal")
        scan = store.latest_scan(rec.record_id)
        applicability = scan_applicability(rec)
        stages = compute_gates(tier=tier, cells=_cells_for(tier), tools=tools, scan=scan,
                               asset_type=asset_key,
                               scan_applicability=applicability.state)
        st = rec.status.value if hasattr(rec.status, "value") else str(rec.status)
        # 큐(queue_router.list_queue)와 동일한 표시 계층 계산을 그대로 써서 대시보드가
        # 큐와 어긋나지 않게 해요.
        # - risk: 현재 게이트가 다루는 area의 finding만(active_findings) 남겨 재계산 —
        #   제거된 도구의 유령 finding이 분포를 왜곡하지 않아요(scan.risk 옛값 대신).
        # - verdict: effective_verdict로 사람 최종결정(APPROVED/REJECTED/DEPRECATED)을 합쳐요.
        shown = active_findings(scan.findings, stages) if scan else []
        summary = gate_summary(stages)
        summary = {**summary, "verdict": effective_verdict(summary["verdict"], st)}
        rows.append({
            "record_id": rec.record_id,
            "name": rec.name,
            "tier": tier,
            "risk": risk_of_findings(shown) if scan else None,
            "status": st,
            "summary": summary,
            "stages": stages,
            # ── 인벤토리 화면(W5)용 표시 메타 ──────────────────────────
            # 대시보드 응답에는 실리지 않아요(그 라우터가 쓰는 키만 골라 담음). 같은 순회에
            # 담는 이유는 인벤토리가 registry를 한 번 더 전수 조회하면 두 화면 숫자가
            # 어긋날 수 있기 때문이에요.
            "owner_team": getattr(rec, "owner_team", "") or "",
            "owner_user": getattr(rec, "owner_user", "") or "",
            "asset_type": dt,
            "model": _agent_model_of(getattr(rec, "descriptors", {}) or {}),
            "version": getattr(rec, "version", "") or "",
            # inventory_router가 같은 Registry snapshot에서 인가 강제 상태를 투영해요.
            # dashboard 응답은 필요한 키만 골라서 이 body를 외부로 내보내지 않아요.
            "descriptors": getattr(rec, "descriptors", {}) or {},
        })
    return rows


@router.get("/api/governance/dashboard", dependencies=[Depends(require_role(*CONSOLE_ROLES))])
def dashboard():
    store = get_gov_store()
    rows = _queue_annotations()

    # 승인 현황 — 레지스트리 status 기준(큐의 status 컬럼과 100% 일치).
    # 과거엔 approve/reject를 DECISION 이력으로 셌는데, auto-approve/auto-reject(스캐너
    # 자동 판정)는 DECISION에 안 남아서 큐(APPROVED/REJECTED status)와 크게 어긋났어요
    # (실측 2026-07-22: 대시보드 approve 3·reject 1인데 큐는 APPROVED 6·REJECTED 5).
    # status를 직접 세면 사람 판정·자동 판정 구분 없이 최종 상태로 일치해요.
    approve = sum(1 for r in rows if r["status"] == "APPROVED")
    reject = sum(1 for r in rows if r["status"] == "REJECTED")
    pending = sum(1 for r in rows if r["status"] in ("PENDING_APPROVAL", "DRAFT"))
    decided = approve + reject
    # override(soft override) 건수는 여전히 판정 이력에서 — "스캔은 reject였지만 사람이 승인".
    override = sum(1 for _, d in store.all_decisions() if d.override)
    # 자동화율: 사람 개입 없이 자동 판정된 비중 = 결정된 것 중 DECISION 이력에 없는 것.
    # (auto-approve/auto-reject는 status가 APPROVED/REJECTED로 바뀌지만 DECISION은 안 남김.)
    decided_by_human = {rid for rid, _ in store.all_decisions()}
    auto = sum(1 for r in rows
               if r["status"] in ("APPROVED", "REJECTED") and r["record_id"] not in decided_by_human)
    automation_rate = round(auto / decided * 100) if decided else 0

    # 게이트 통과율 — 도구(area)별 pass / (pass+fail).
    area_pass: dict[str, list[int]] = {}
    for r in rows:
        for s in r["stages"]:
            if s.state in ("pass", "fail"):
                agg = area_pass.setdefault(s.area, [0, 0])
                agg[0] += 1 if s.state == "pass" else 0
                agg[1] += 1
    gate_pass_rate = {
        area: round(p / t * 100) if t else 0 for area, (p, t) in area_pass.items()
    }

    # 위험 분포 (미스캔은 unscanned).
    risk_dist: dict[str, int] = {}
    for r in rows:
        key = r["risk"] or "unscanned"
        risk_dist[key] = risk_dist.get(key, 0) + 1

    # 등급 분포.
    tier_dist: dict[str, int] = {}
    for r in rows:
        tier_dist[r["tier"]] = tier_dist.get(r["tier"], 0) + 1

    # 라이프사이클 상태 분포 (DRAFT/PENDING_APPROVAL/APPROVED/REJECTED/DEPRECATED).
    # 승인 현황(판정 이력/verdict 축)과 별개로, registry 상태 자체의 분포를 보여줘요.
    status_dist: dict[str, int] = {}
    for r in rows:
        st = r.get("status") or "UNKNOWN"
        status_dist[st] = status_dist.get(st, 0) + 1

    # SLA — 미스캔(대기) 자산 수 (오래된 큐 근사).
    unscanned = sum(1 for r in rows if r["risk"] is None)
    # 미스캔 자산 목록 (2열 리스트용) — 개수(sla.unscanned)와 별개로 자산명·record_id를 실어요.
    unscanned_assets = [
        {"record_id": r["record_id"], "name": r["name"]}
        for r in rows if r["risk"] is None
    ]

    # 도구별 차단 기여(top_blockers) — fail 상태 게이트를 (area, tool)별로 카운트.
    # "어떤 스캔 도구가 가장 많이 자산을 막았나"를 area+도구명으로 보여줘요(§3-M5 후속).
    blocker_counts: dict[tuple[str, str], int] = {}
    for r in rows:
        for s in r["stages"]:
            if s.state == "fail":
                k = (s.area, s.tool_name)
                blocker_counts[k] = blocker_counts.get(k, 0) + 1
    top_blockers = [
        {"area": area, "tool_name": name, "count": n}
        for (area, name), n in sorted(blocker_counts.items(), key=lambda x: -x[1])
    ]

    # 자산별 현황 — 각 분포 위젯이 "합산 숫자"뿐 아니라 "어떤 자산이 그 값을 이루나"를
    # 카테고리별로 펼쳐 보여줄 수 있게 자산 단위 행을 함께 내려요. 프론트가 이 배열을
    # risk/status/tier/게이트(area)별로 그룹핑해요(백엔드 그룹핑 중복 없이 한 소스로).
    # - risk: 미스캔은 None → 프론트에서 "unscanned"로 묶어요.
    # - gates: fail한 area 목록(게이트 통과율 위젯이 area별 fail 자산을 나열).
    # - pass_areas: pass한 area 목록(게이트 통과율 위젯이 area별 통과 자산도 나열).
    assets = [
        {
            "record_id": r["record_id"],
            "name": r["name"],
            "risk": r["risk"],                 # none|low|medium|high|null(미스캔)
            "status": r["status"],
            "tier": r["tier"],
            "gates": [s.area for s in r["stages"] if s.state == "fail"],
            "pass_areas": [s.area for s in r["stages"] if s.state == "pass"],
        }
        for r in rows
    ]

    return {
        "approvals": {
            "approve": approve, "reject": reject, "pending": pending,
            "override": override, "automation_rate": automation_rate,
        },
        "gate_pass_rate": gate_pass_rate,
        "risk_distribution": risk_dist,
        "tier_distribution": tier_dist,
        "status_distribution": status_dist,
        "sla": {"unscanned": unscanned, "total": len(rows), "decided": decided},
        "unscanned_assets": unscanned_assets,
        "top_blockers": top_blockers,
        "assets": assets,
    }


@router.get("/api/governance/decisions", dependencies=[Depends(require_role(*CONSOLE_ROLES))])
def list_decisions(record_id: str = "", principal: str = ""):
    """판정 감사 이력 (§3-M6). record_id·principal 필터. 최신순."""
    store = get_gov_store()
    # record_id→자산명 맵을 한 번 조회해요(루프 밖 1회). 감사 로그가 해시 대신 자산명을
    # 보여주도록. registry 조회 실패는 관대하게 흡수하고 record_id로 폴백해요(_queue_annotations 패턴).
    name_map: dict[str, str] = {}
    try:
        for rec in get_registry().list_records(get_registry_id(), max_results=1000):
            name_map[rec.record_id] = rec.name
    except Exception:
        name_map = {}
    items = []
    for rid, d in store.all_decisions():
        if record_id and rid != record_id:
            continue
        if principal and d.principal != principal:
            continue
        items.append({
            "record_id": rid,
            "name": name_map.get(rid) or rid,  # 삭제된 자산 대비 record_id 폴백.
            "ts": d.ts,
            "principal": d.principal,
            "decision": d.decision,
            "reason": d.reason,
            "override": d.override,
        })
    # 최신순 (ts 역순).
    items.sort(key=lambda x: x["ts"], reverse=True)
    return {"items": items}
