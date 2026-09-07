"""거버넌스 콘솔 데이터 — 로컬 JSON 백엔드 (AuxStore 패턴).

mock/dev 기본. aws 배포 시 동일 시그니처의 DynamoGovStore로 교체(GOVTOOL·TIER·
GOVSETTINGS 파티션 + AUX#{record_id}/SCAN#{ts}). P1은 JSON으로 시작.
"""
from __future__ import annotations

import json
from dataclasses import asdict, fields
from pathlib import Path

from .models import (
    ApprovalBlockRecord,
    ConsoleSettings,
    DecisionRecord,
    OverlapRecord,
    ScanRecord,
    MonitoringScanProjection,
    TierCell,
)

_APPROVAL_BLOCK_FIELDS = {f.name for f in fields(ApprovalBlockRecord)}

# 신규 자산 기본 목표 등급 (화면설계 확정: 최소).
DEFAULT_TIER = "minimal"

_SETTINGS_FIELDS = {f.name for f in fields(ConsoleSettings)}


def normalize_categories(items) -> list[str]:
    """공백·빈 값·중복을 제거하되 관리자가 정한 순서는 유지해요."""
    seen: set[str] = set()
    out: list[str] = []
    for item in items or []:
        if not isinstance(item, str):
            continue
        value = item.strip()
        if not value or value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


class GovStore:
    def __init__(self, store_path: str | Path | None = None):
        self._path = Path(store_path) if store_path else None
        self._tiers: dict[str, list] = {}
        self._settings: dict = asdict(ConsoleSettings())
        self._categories: list[str] = []
        self._scans: dict[str, list] = {}
        self._overlaps: dict[str, list] = {}
        self._decisions: dict[str, list] = {}
        self._target_tiers: dict[str, str] = {}
        self._approval_blocks: dict[str, dict] = {}
        self._load()

    # --- tiers ---
    def get_tier(self, tier: str) -> list[TierCell]:
        return [TierCell(**c) for c in self._tiers.get(tier, [])]

    def put_tier(self, tier: str, cells: list[TierCell]) -> None:
        self._tiers[tier] = [asdict(c) for c in cells]
        self._persist()

    # --- settings ---
    def get_settings(self) -> ConsoleSettings:
        s = ConsoleSettings(**{k: v for k, v in self._settings.items() if k in _SETTINGS_FIELDS})
        if not s.asset_tier_map:
            s.asset_tier_map = {"skill": "minimal", "mcp": "standard", "agent": "strong"}
        if not s.judge_model_map:
            s.judge_model_map = {"minimal": "haiku-4-5", "standard": "sonnet-4-6", "strong": "sonnet-5"}
        return s

    def put_settings(self, settings: ConsoleSettings) -> None:
        self._settings = asdict(settings)
        self._persist()

    # --- categories (등록 폼 카테고리 마스터) ---
    def get_categories(self) -> list[str]:
        """관리자가 정한 카테고리 목록. 비어 있으면 등록 폼은 자유입력으로 폴백해요."""
        return list(self._categories)

    def put_categories(self, items: list[str]) -> None:
        """정규화한 목록으로 통째 교체해요."""
        self._categories = normalize_categories(items)
        self._persist()

    # --- scans ---
    def add_scan(self, record_id: str, scan: ScanRecord) -> None:
        self._scans.setdefault(record_id, []).append(asdict(scan))
        self._persist()

    def latest_scan(self, record_id: str) -> ScanRecord | None:
        items = self._scans.get(record_id) or []
        if not items:
            return None
        return ScanRecord(**items[-1])

    def monitoring_latest_scans(
        self,
        record_ids: list[str],
    ) -> dict[str, MonitoringScanProjection]:
        output = {}
        for record_id in dict.fromkeys(record_ids):
            scan = self.latest_scan(record_id)
            if scan is not None:
                output[record_id] = MonitoringScanProjection(
                    ts=scan.ts,
                    risk=scan.risk,
                    status=scan.status,
                    version=scan.version,
                )
        return output

    def running_record_ids(self) -> list[str]:
        """최신 스캔 status가 'running'인 record_id 목록."""
        out = []
        for rid, items in self._scans.items():
            if items and items[-1].get("status") == "running":
                out.append(rid)
        return out

    def done_record_ids(self) -> list[str]:
        """최신 스캔 status가 'done'인 record_id 목록(verdict backstop 후보)."""
        out = []
        for rid, items in self._scans.items():
            if items and items[-1].get("status") == "done":
                out.append(rid)
        return out

    # --- overlaps (중복검토, append) ---
    def add_overlap(self, record_id: str, overlap: OverlapRecord) -> None:
        self._overlaps.setdefault(record_id, []).append(asdict(overlap))
        self._persist()

    def latest_overlap(self, record_id: str) -> OverlapRecord | None:
        items = self._overlaps.get(record_id) or []
        if not items:
            return None
        return OverlapRecord(**items[-1])

    def all_overlaps(self) -> dict[str, OverlapRecord]:
        """record_id → 최신 OverlapRecord. 기록 없는 자산은 키가 없어요.

        인벤토리 화면이 자산마다 `latest_overlap`을 부르면 자산 수에 비례한 왕복이 생겨요
        (N+1). 한 번에 모아 돌려줘요. 기록 없는 자산에 빈 레코드를 채우지 않는 건 "미검토"와
        "검토했고 후보 0건"이 구분돼야 하기 때문이에요(OverlapRecord docstring 참조).
        """
        out: dict[str, OverlapRecord] = {}
        for record_id, items in self._overlaps.items():
            if items:
                out[record_id] = OverlapRecord(**items[-1])
        return out

    # --- decisions (판정 감사, append-only) ---
    def add_decision(self, record_id: str, decision: DecisionRecord) -> None:
        self._decisions.setdefault(record_id, []).append(asdict(decision))
        self._persist()

    def list_decisions(self, record_id: str) -> list[DecisionRecord]:
        return [DecisionRecord(**d) for d in self._decisions.get(record_id, [])]

    def all_decisions(self) -> list[tuple[str, DecisionRecord]]:
        """모든 자산의 판정 이력을 (record_id, DecisionRecord)로 평탄화 (M6 감사 전체 조회)."""
        out: list[tuple[str, DecisionRecord]] = []
        for record_id, items in self._decisions.items():
            for d in items:
                out.append((record_id, DecisionRecord(**d)))
        return out

    # --- approval blocks (자동승인 차단 사유, 자산별 최신 1건) ---
    def put_approval_block(self, record_id: str, block: ApprovalBlockRecord) -> None:
        self._approval_blocks[record_id] = asdict(block)
        self._persist()

    def get_approval_block(self, record_id: str) -> ApprovalBlockRecord | None:
        raw = self._approval_blocks.get(record_id)
        if not raw:
            return None
        return ApprovalBlockRecord(
            **{k: v for k, v in raw.items() if k in _APPROVAL_BLOCK_FIELDS})

    def clear_approval_block(self, record_id: str) -> None:
        if self._approval_blocks.pop(record_id, None) is not None:
            self._persist()

    # --- target tier (자산별 목표 등급, admin 전권) ---
    def get_target_tier(self, record_id: str) -> str:
        return self._target_tiers.get(record_id, DEFAULT_TIER)

    def set_target_tier(self, record_id: str, tier: str) -> None:
        self._target_tiers[record_id] = tier
        self._persist()

    def purge_record(self, record_id: str) -> None:
        """하드 삭제 시 record의 governance 잔재(스캔·중복검토·판정·목표등급·차단사유)를 제거해요."""
        self._scans.pop(record_id, None)
        self._overlaps.pop(record_id, None)
        self._decisions.pop(record_id, None)
        self._target_tiers.pop(record_id, None)
        self._approval_blocks.pop(record_id, None)
        self._persist()

    # --- persistence ---
    def _persist(self) -> None:
        if not self._path:
            return
        self._path.write_text(json.dumps({
            "tiers": self._tiers,
            "settings": self._settings, "categories": self._categories,
            "scans": self._scans,
            "overlaps": self._overlaps,
            "decisions": self._decisions, "target_tiers": self._target_tiers,
            "approval_blocks": self._approval_blocks,
        }, ensure_ascii=False, indent=2))

    def _load(self) -> None:
        if not self._path or not self._path.exists():
            return
        data = json.loads(self._path.read_text())
        self._tiers = data.get("tiers", {})
        self._settings = data.get("settings", asdict(ConsoleSettings()))
        self._categories = data.get("categories", [])
        self._scans = data.get("scans", {})
        self._overlaps = data.get("overlaps", {})
        self._decisions = data.get("decisions", {})
        self._target_tiers = data.get("target_tiers", {})
        self._approval_blocks = data.get("approval_blocks", {})
