"""Dynamo primary + JSON fallback AuxStore.

읽기는 Dynamo에 해당 데이터 종류가 없을 때만 JSON을 사용하고, 모든 쓰기는 Dynamo로만
보내요. 읽기 중 lazy migration은 하지 않아요.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from .aux_store import AssetMeta, AuxStore, _merge_agent_descriptors
from .registry.models import DescriptorType, RegistryRecord


class DualReadAuxStore:
    """Dynamo를 우선하고 아직 이관되지 않은 JSON 데이터만 폴백해요.

    Dynamo 구현이 ``has_ext``를 제공하면 저장된 기본값과 miss를 정확히 구분해요.
    결합 전처럼 probe가 없으면 필드별 기본값에 한해 JSON의 non-default 값을 사용해
    미이관 데이터가 사라지지 않게 해요.
    """

    def __init__(self, primary: Any, fallback: AuxStore):
        self._primary = primary
        self._fallback = fallback
        self._known_ext: set[str] = set()
        self._purged: set[str] = set()
        # 이관 전 JSON 조회수를 Dynamo에 흡수한 레코드. 프로세스 내 이중 계수를 막아요
        # (프로세스 간 중복은 Dynamo 카운터 존재 여부로 막혀요).
        self._baseline_absorbed: set[str] = set()

    def _has_ext(self, record_id: str) -> bool | None:
        if record_id in self._known_ext:
            return True
        probe = getattr(self._primary, "has_ext", None)
        if not callable(probe):
            return None
        return bool(probe(record_id))

    @staticmethod
    def _normalise_ext(ext: dict) -> dict:
        result = dict(AuxStore._EXT_DEFAULTS)
        result.update(ext)
        result["tags"] = tuple(result.get("tags", ()))
        return result

    def increment_views(self, record_id: str) -> int:
        """Dynamo 카운터를 증가시키고, 이관 전 JSON 값을 **한 번만** baseline으로 흡수해요.

        `max(primary, legacy)`만 쓰면 두 카운터의 기준점이 결합되지 않아요 — legacy가 100이고
        Dynamo가 0이면 새 조회 1~100회가 전부 100으로 보여서 그만큼 영구 유실돼요(실측:
        새 조회 10회 뒤에도 100). baseline을 Dynamo에 실제로 더해 기준점을 합쳐요.

        멱등: baseline은 `_baseline_absorbed`로 프로세스 내 1회, 그리고 Dynamo에 이미 카운터가
        있으면(=이관 또는 이전 흡수 완료) 다시 더하지 않아요.
        """
        self._purged.discard(record_id)
        self._absorb_legacy_views(record_id)
        return self._primary.increment_views(record_id)

    def _absorb_legacy_views(self, record_id: str) -> None:
        """이관 전 JSON 조회수를 Dynamo 카운터에 한 번 더해 기준점을 합쳐요."""
        if record_id in self._baseline_absorbed or record_id in self._purged:
            return
        self._baseline_absorbed.add(record_id)
        if self._primary.get_meta(record_id).views > 0:
            return          # 이미 이관됐거나 흡수됨 — 이중 계수 금지
        legacy = self._fallback.get_meta(record_id).views
        for _ in range(legacy):
            self._primary.increment_views(record_id)

    def get_meta(self, record_id: str) -> AssetMeta:
        """조회수. Dynamo 카운터가 아직 비어 있을 때만 JSON 값을 보여줘요.

        `max()`를 쓰지 않는 이유: 흡수 후에는 Dynamo가 유일한 진실이고, `max`를 유지하면
        JSON이 남아 있는 동안 Dynamo 감소(purge 후 재사용 등)를 가려요.
        """
        primary = self._primary.get_meta(record_id)
        if record_id in self._purged or primary.views > 0:
            return primary
        return AssetMeta(views=self._fallback.get_meta(record_id).views)

    def get_ext(self, record_id: str) -> dict:
        if record_id in self._purged:
            return self._normalise_ext(self._primary.get_ext(record_id))

        present = self._has_ext(record_id)
        if present is True:
            return self._normalise_ext(self._primary.get_ext(record_id))
        if present is False:
            return self._normalise_ext(self._fallback.get_ext(record_id))

        primary = self._normalise_ext(self._primary.get_ext(record_id))
        fallback = self._normalise_ext(self._fallback.get_ext(record_id))
        merged = {}
        for key, default in AuxStore._EXT_DEFAULTS.items():
            primary_value = primary[key]
            fallback_value = fallback[key]
            merged[key] = fallback_value if primary_value == default else primary_value
        return merged

    def get_agent_monitoring_ext(self, record_id: str) -> dict:
        """모니터링용 소유자·Agent 선언 메타를 dual-read 규약으로 돌려줘요.

        `get_ext` 와 같은 순서예요 — purge 된 record 와 primary 존재가 확인된 record 는
        primary, 없으면 fallback 이에요. 둘 다 불확실하면 primary 를 쓰고 빈 값만
        fallback 으로 채워요(값이 있는 쪽을 지우지 않아요).
        """
        if record_id in self._purged:
            return self._primary.get_agent_monitoring_ext(record_id)

        present = self._has_ext(record_id)
        if present is True:
            return self._primary.get_agent_monitoring_ext(record_id)
        if present is False:
            return self._fallback.get_agent_monitoring_ext(record_id)

        primary = self._primary.get_agent_monitoring_ext(record_id)
        fallback = self._fallback.get_agent_monitoring_ext(record_id)
        merged = dict(primary)
        for key, value in fallback.items():
            if not merged.get(key):
                merged[key] = value
        return merged

    def set_meta(self, record_id: str, **fields) -> None:
        """부분 갱신 전에 보이는 전체 값을 Dynamo에 승격해 다른 legacy 필드를 보존해요."""
        current = self.get_ext(record_id)
        for key, value in fields.items():
            if key in AuxStore._EXT_DEFAULTS:
                current[key] = value
        self._primary.set_meta(record_id, **current)
        self._known_ext.add(record_id)
        self._purged.discard(record_id)

    def apply_responsibility_change(self, event) -> None:
        current = self.get_ext(event.record_id)
        if not self._primary.has_ext(event.record_id):
            self._primary.set_meta(event.record_id, **current)
        self._primary.apply_responsibility_change(event)
        self._known_ext.add(event.record_id)
        self._purged.discard(event.record_id)

    def list_responsibility_changes(self, record_id: str):
        return self._primary.list_responsibility_changes(record_id)

    def purge_record(self, record_id: str) -> None:
        """**양쪽 모두** 지워요. 메모리 tombstone은 같은 프로세스 안에서만 보조로 써요.

        JSON을 남기고 메모리 set으로만 폴백을 막으면, 프로세스 재시작 뒤 그 set이 사라져서
        Dynamo miss가 보존된 JSON으로 다시 폴백해요 — 사용자가 완전 삭제한 자산의 owner·
        descriptors·조회수가 되살아나요(실측 재현됨).

        purge는 "하드 삭제 잔재 정리"라 롤백 근거를 남길 대상이 아니에요(이관 스크립트의
        원본 보존과는 목적이 달라요 — 그건 이관 실패 복구용이고, 이건 사용자의 삭제 의사예요).
        JSON 삭제가 실패해도 Dynamo 삭제는 유지해요.
        """
        self._primary.purge_record(record_id)
        try:
            self._fallback.purge_record(record_id)
        except Exception:
            # 폴백 정리 실패는 메모리 tombstone이 이 프로세스 동안 막아줘요. 다음 purge가 재시도해요.
            pass
        self._known_ext.add(record_id)
        self._purged.add(record_id)

    def merge_into(self, record: RegistryRecord) -> RegistryRecord:
        ext = self.get_ext(record.record_id)
        descriptors = ext["descriptors"] if ext["descriptors"] is not None else record.descriptors
        if record.descriptor_type is DescriptorType.AGENT and ext["descriptors"] is not None:
            descriptors = _merge_agent_descriptors(record.descriptors, ext["descriptors"])
        description = record.description or (ext["description"] or "")
        return replace(
            record,
            owner_team=ext["owner_team"],
            owner_user=ext["owner_user"],
            owner_contact=ext["owner_contact"],
            tags=ext["tags"],
            category=ext["category"],
            changelog=ext["changelog"],
            search_visible=ext["search_visible"],
            escalation_contact=ext["escalation_contact"],
            descriptors=descriptors,
            description=description,
        )
