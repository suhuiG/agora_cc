"""GovernanceTrustAdapter — TrustSummaryPort 구현.

dashboard_router의 record 단위 조합을 재사용해 대시보드와 risk/verdict가 정합해요.
tier는 get_settings().asset_tier_map[asset_key](미매핑=minimal), risk는
active_findings 재계산, verdict는 gate_summary + effective_verdict.
"""
from __future__ import annotations

from ...shared.trust import (
    RECORD_NOT_PROVIDED,
    OverlapState,
    ScanState,
    TrustSummary,
)
from . import approval_block
from .gate import (
    active_findings,
    asset_key_of,
    compute_gates,
    effective_verdict,
    gate_summary,
    risk_of_findings,
)
from .scan_applicability import scan_applicability
from .tool_catalog import list_catalog_tools


def _scan_state(status: str) -> ScanState:
    if status == "done":
        return ScanState.SCANNED
    if status == "failed":
        return ScanState.FAILED
    return ScanState.SCANNING  # queued | running (및 미상)


def _enum_value(v) -> str:
    return v.value if hasattr(v, "value") else str(v)


class GovernanceTrustAdapter:
    def __init__(self, store, registry, registry_id) -> None:
        self._store = store
        self._registry = registry
        self._registry_id = registry_id

    def _get_record(self, record_id: str):
        # 조회 실패(RecordNotFound 등)는 안전 폴백 — trust 요약은 읽기라 절대 예외로 안 깨요.
        try:
            return self._registry.get_record(self._registry_id, record_id)
        except Exception:
            return None

    def summarize(
        self,
        record_id: str,
        *,
        record: object | None = RECORD_NOT_PROVIDED,
    ) -> TrustSummary:
        rec = self._get_record(record_id) if record is RECORD_NOT_PROVIDED else record
        asset_key = asset_key_of(_enum_value(rec.descriptor_type)) if rec else None
        tier_map = self._store.get_settings().asset_tier_map or {}
        tier = tier_map.get(asset_key or "", "minimal")
        # 스캔 축과 중복검토 축은 독립이에요 — 스캔 경로가 어디서 early-return하든 overlap이
        # 빠지지 않도록, 스캔 판정을 먼저 구한 뒤 **한 곳에서** 두 축을 합성해요.
        scan_state, risk, verdict = self._scan_axis(record_id, rec, tier, asset_key)
        overlap_state, overlap_count, overlap_band = self._overlap_axis(record_id)
        return TrustSummary(
            record_id, tier, scan_state, risk, verdict,
            overlap_state=overlap_state, overlap_count=overlap_count,
            overlap_band=overlap_band,
            approval_block=self._approval_block(record_id, rec),
        )

    def _approval_block(self, record_id, rec):
        """자동승인이 막힌 사유(R3). 이미 승인된 자산엔 싣지 않아요.

        승인된 뒤에도 옛 사유를 계속 보여주면 "승인됐는데 담당자 연락처를 채우라"는
        모순된 안내가 나와요. 반대로 **사유가 없다고 승인된 것도 아니에요** — 승인 여부는
        `verdict`/Registry status가 정본이고, 이 값은 대기 원인을 덧붙이는 용도예요.
        """
        if _enum_value(getattr(rec, "status", "")) == "APPROVED":
            return None
        return approval_block.as_summary(
            approval_block.load(record_id, store=self._store))

    def _scan_axis(self, record_id, rec, tier, asset_key):
        """(scan_state, risk, verdict). 미스캔·진행중·실패면 risk/verdict는 None이에요."""
        applicability = scan_applicability(rec) if rec else None
        scan = self._store.latest_scan(record_id)
        if scan is None:
            # 스캔 기록이 없어도 적용성 정본이 해당없음이고 승인됐으면 면제로 표시.
            if (
                applicability is not None
                and applicability.state == "not_applicable"
                and _enum_value(getattr(rec, "status", "")) == "APPROVED"
            ):
                return ScanState.EXEMPT, None, None
            return ScanState.NOT_SCANNED, None, None
        state = _scan_state(scan.status)
        if state is not ScanState.SCANNED:
            return state, None, None
        stages = compute_gates(
            tier=tier, cells=self._store.get_tier(tier),
            tools=list_catalog_tools(), scan=scan, asset_type=asset_key,
            scan_applicability=(
                applicability.state if applicability is not None else "unknown"
            ))
        risk = risk_of_findings(active_findings(scan.findings, stages))
        summary = gate_summary(stages)
        status = _enum_value(rec.status) if rec else ""
        return state, risk, effective_verdict(summary["verdict"], status)

    def _overlap_axis(self, record_id):
        """(overlap_state, count, top_band). GovStore가 overlap을 모르면 미검토로 degrade해요."""
        getter = getattr(self._store, "latest_overlap", None)
        if getter is None:
            return OverlapState.NOT_REVIEWED, 0, None
        try:
            overlap = getter(record_id)
        except Exception:
            return OverlapState.NOT_REVIEWED, 0, None
        if overlap is None:
            return OverlapState.NOT_REVIEWED, 0, None
        if overlap.status == "failed":
            return OverlapState.FAILED, 0, None
        if overlap.status != "done":
            return OverlapState.REVIEWING, 0, None
        count = len(overlap.candidates or [])
        band = overlap.top_band if count else None
        return OverlapState.REVIEWED, count, band
