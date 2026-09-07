"""등급×도구 매트릭스 (§3-M4). 이 매트릭스가 게이트 파이프라인의 SoT."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from ...shared.deps import get_gov_store, get_registry, get_registry_id
from .authz import ADMIN_ONLY, CONSOLE_ROLES, require_role
from .gate import asset_key_of, compute_gates, gate_summary
from .models import TierCell
from .queue_router import tier_for_record
from .scan_applicability import scan_applicability
from .tool_catalog import list_catalog_tools

router = APIRouter(tags=["governance-tiers"])


@router.get("/api/governance/tiers/{tier}/tools", dependencies=[Depends(require_role(*CONSOLE_ROLES))])
def get_tier(tier: str):
    return {"tier": tier, "cells": [c.__dict__ for c in get_gov_store().get_tier(tier)]}


_VALID_ENFORCEMENT = frozenset(("required", "warn", "off"))


@router.put("/api/governance/tiers/{tier}/tools", dependencies=[Depends(require_role(*ADMIN_ONLY))])
def put_tier(tier: str, body: dict):
    for c in body.get("cells", []):
        if not c.get("tool_id"):
            raise HTTPException(422, "cell에 tool_id 필수")
        enf = c.get("enforcement", "off")
        if enf not in _VALID_ENFORCEMENT:
            raise HTTPException(422, f"enforcement 값 '{enf}'은 유효하지 않아요. 허용: required / warn / off")
    cells = [TierCell(tier=tier, tool_id=c["tool_id"], enforcement=c.get("enforcement", "off"),
                      threshold=c.get("threshold", ""), asset_type_scope=c.get("asset_type_scope", "*"))
             for c in body.get("cells", [])]
    get_gov_store().put_tier(tier, cells)
    return {"tier": tier, "cells": [c.__dict__ for c in cells]}


@router.post("/api/governance/tiers/simulate", dependencies=[Depends(require_role(*ADMIN_ONLY))])
def simulate(body: dict):
    """저장 전 드라이런 — 이 등급 매트릭스 변경이 기존 자산 판정을 어떻게 바꾸는지 정밀 계산.

    대상: 타입 등급(SP-1 SoT: tier_for_record)이 이 tier인 자산만(그 등급 매트릭스를
    바꾸는 거니까). 각 자산을
    현재 매트릭스(old)와 body.cells(new)로 각각 compute_gates→gate_summary 판정해
    verdict 전이를 세요. auto-approve↔(auto-reject|pending) 전이만 카운트해요:
    pass_to_fail = auto-approve→(auto-reject|pending), fail_to_pass = (auto-reject|pending)→auto-approve.
    미스캔·미실행 자산은 old·new 모두 pending으로 안정적이라 전이에서 자연히 빠져요
    (fail-closed 계승). affected=이 등급 대상 자산 수(기존 계약 유지).
    """
    tier = body.get("tier", "")
    new_cells = [
        TierCell(tier=tier, tool_id=c["tool_id"], enforcement=c.get("enforcement", "off"),
                 threshold=c.get("threshold", ""), asset_type_scope=c.get("asset_type_scope", "*"))
        for c in body.get("cells", []) if c.get("tool_id")
    ]
    required = [c["tool_id"] for c in body.get("cells", []) if c.get("enforcement") == "required"]

    store = get_gov_store()
    tools = list_catalog_tools()
    old_cells = store.get_tier(tier)
    try:
        hits = get_registry().search([get_registry_id()], "", max_results=1000)
    except Exception:
        hits = []

    def _verdict(cells, scan, asset_type, applicability):
        return gate_summary(compute_gates(tier=tier, cells=cells, tools=tools,
                                          scan=scan, asset_type=asset_type,
                                          scan_applicability=applicability.state))["verdict"]

    pass_to_fail = fail_to_pass = 0
    by_type: dict[str, dict] = {}
    sample: list[dict] = []
    affected = 0
    for hit in hits:
        # 이 등급이 게이트를 지배하는 자산만 재판정 대상 (SP-1 SoT: 타입→등급).
        if tier_for_record(hit.record_id) != tier:
            continue
        affected += 1
        dt = hit.descriptor_type.value if hasattr(hit.descriptor_type, "value") else str(hit.descriptor_type)
        atype = asset_key_of(dt)
        tkey = atype or "other"
        agg = by_type.setdefault(tkey, {"affected": 0, "pass_to_fail": 0, "fail_to_pass": 0})
        agg["affected"] += 1

        scan = store.latest_scan(hit.record_id)
        applicability = scan_applicability(hit)
        old_v = _verdict(old_cells, scan, atype, applicability)
        new_v = _verdict(new_cells, scan, atype, applicability)
        if old_v == "auto-approve" and new_v in ("auto-reject", "pending"):
            pass_to_fail += 1
            agg["pass_to_fail"] += 1
            if len(sample) < 10:
                sample.append({"record_id": hit.record_id, "name": hit.name,
                               "old_verdict": old_v, "new_verdict": new_v})
        elif old_v in ("auto-reject", "pending") and new_v == "auto-approve":
            fail_to_pass += 1
            agg["fail_to_pass"] += 1
            if len(sample) < 10:
                sample.append({"record_id": hit.record_id, "name": hit.name,
                               "old_verdict": old_v, "new_verdict": new_v})

    return {
        "affected": affected,
        "required_tools": required,
        "transitions": {"pass_to_fail": pass_to_fail, "fail_to_pass": fail_to_pass},
        "by_asset_type": by_type,
        "sample": sample,
    }
