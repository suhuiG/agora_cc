"""Aux Store — Registry가 담지 않는 부가 데이터(조회수).

JSON 파일로 영속화. GA 전환 후에도 이 스토어는 그대로 유지돼요
(Registry에는 조회수 개념이 없으니까).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path

from .registry.models import DescriptorType, RegistryRecord


_REGISTRY_AGENT_FIELDS = (
    "sourcePrefix",
    "runtimeArn",
    "endpoint",
    "agoraDependencies",
    "workloadIdentity",
    "model",
    "bedrockModelId",
    "executionBinding",
)


def _merge_agent_descriptors(registry_descriptors: dict, aux_descriptors: dict) -> dict:
    """Aux 원본 카드 위에 Registry에서 복원한 Runtime binding을 우선 병합해요."""
    if not isinstance(registry_descriptors, dict) or not isinstance(aux_descriptors, dict):
        return aux_descriptors
    registry_agent = registry_descriptors.get("agent")
    aux_agent = aux_descriptors.get("agent")
    if isinstance(registry_agent, dict) and not isinstance(aux_agent, dict):
        merged = {**registry_descriptors, **aux_descriptors}
        merged["agent"] = registry_agent
        return merged
    if not isinstance(registry_agent, dict) or not isinstance(aux_agent, dict):
        return aux_descriptors

    merged = {**registry_descriptors, **aux_descriptors}
    merged_agent = {**registry_agent, **aux_agent}
    for metadata_field in _REGISTRY_AGENT_FIELDS:
        if metadata_field in registry_agent:
            merged_agent[metadata_field] = registry_agent[metadata_field]
    merged["agent"] = merged_agent
    return merged


@dataclass
class AssetMeta:
    views: int = 0


class AuxStore:
    # 확장메타 기본값 (Registry가 담지 않는 Agora 고유 필드).
    # descriptors=None은 "Aux에 원본 descriptors 미보관" 의미 — merge_into가 record 것을 그대로 씀.
    # description=None은 "Aux에 미보관" 의미 — merge_into가 record(Registry 코어) 것을 그대로 씀.
    _EXT_DEFAULTS = {
        "owner_team": "", "owner_user": "", "owner_email": "",
        "owner_contact": "",
        "tags": (), "category": "",
        "changelog": "", "search_visible": True, "descriptors": None,
        "description": None,
        # Operational contacts. They are notification targets, never authorization subjects.
        "escalation_contact": "",
    }

    def __init__(self, store_path: str | Path | None = None):
        self._store_path = Path(store_path) if store_path else None
        self._data: dict[str, AssetMeta] = {}
        self._ext: dict[str, dict] = {}
        self._responsibility_history: dict[str, list[dict]] = {}
        if self._store_path and self._store_path.exists():
            self._load()

    def _ensure(self, record_id: str) -> AssetMeta:
        if record_id not in self._data:
            self._data[record_id] = AssetMeta()
        return self._data[record_id]

    def increment_views(self, record_id: str) -> int:
        meta = self._ensure(record_id)
        meta.views += 1
        self._persist()
        return meta.views

    def get_meta(self, record_id: str) -> AssetMeta:
        return self._ensure(record_id)

    # ── 확장메타 (owner/tags/category/changelog/search_visible/descriptors 원본) ──
    def get_ext(self, record_id: str) -> dict:
        """확장메타 dict를 반환해요(없으면 기본값). tags는 tuple로 정규화."""
        base = dict(self._EXT_DEFAULTS)
        base.update(self._ext.get(record_id, {}))
        base["tags"] = tuple(base.get("tags", ()))
        return base

    def get_agent_monitoring_ext(self, record_id: str) -> dict:
        """Return only owner and Agent declaration metadata for monitoring."""
        raw = self._ext.get(record_id, {})
        descriptors = raw.get("descriptors")
        agent = descriptors.get("agent") if isinstance(descriptors, dict) else None
        declaration = (
            {
                key: agent[key]
                for key in (
                    "agoraDependencies",
                    "builtinTools",
                    "sourcePrefix",
                )
                if key in agent
            }
            if isinstance(agent, dict)
            else None
        )
        return {
            "owner_team": str(raw.get("owner_team") or ""),
            "owner_user": str(raw.get("owner_user") or ""),
            "agent_declaration": declaration,
        }

    def set_meta(self, record_id: str, **fields) -> None:
        """확장메타 부분 갱신(주어진 키만). tags는 tuple로 정규화. 알 수 없는 키는 무시."""
        cur = dict(self._ext.get(record_id, {}))
        for k, v in fields.items():
            if k not in self._EXT_DEFAULTS:
                continue
            cur[k] = list(v) if k == "tags" else v
        self._ext[record_id] = cur
        self._persist()

    def apply_responsibility_change(self, event) -> None:
        """Atomically replace contacts and append an immutable audit event."""
        from ...shared.responsibility import ResponsibilityConflict

        current = self.get_ext(event.record_id)
        if (
            current["owner_contact"] != event.before.owner_contact
            or current["escalation_contact"] != event.before.escalation_contact
        ):
            raise ResponsibilityConflict("responsibility changed concurrently")
        events = self._responsibility_history.setdefault(event.record_id, [])
        if any(item["event_id"] == event.event_id for item in events):
            raise ResponsibilityConflict("responsibility event already exists")
        self._ext.setdefault(event.record_id, {})[
            "owner_contact"
        ] = event.after.owner_contact
        self._ext[event.record_id][
            "escalation_contact"
        ] = event.after.escalation_contact
        events.append(
            {
                "event_id": event.event_id,
                "record_id": event.record_id,
                "changed_at": event.changed_at,
                "changed_by": event.changed_by,
                "reason": event.reason,
                "before": {
                    "owner_contact": event.before.owner_contact,
                    "escalation_contact": event.before.escalation_contact,
                },
                "after": {
                    "owner_contact": event.after.owner_contact,
                    "escalation_contact": event.after.escalation_contact,
                },
            }
        )
        self._persist()

    def list_responsibility_changes(self, record_id: str):
        from ...shared.responsibility import (
            ResponsibilityChange,
            ResponsibilityContacts,
        )

        raw = self._responsibility_history.get(record_id, [])
        return [
            ResponsibilityChange(
                event_id=item["event_id"],
                record_id=item["record_id"],
                changed_at=item["changed_at"],
                changed_by=item["changed_by"],
                reason=item["reason"],
                before=ResponsibilityContacts(**item["before"]),
                after=ResponsibilityContacts(**item["after"]),
            )
            for item in raw
        ]

    def purge_record(self, record_id: str) -> None:
        """record의 확장 메타·ext를 제거해요(하드 삭제 잔재 정리)."""
        self._data.pop(record_id, None)
        self._ext.pop(record_id, None)
        self._persist()

    def merge_into(self, record: RegistryRecord) -> RegistryRecord:
        """저장된 확장메타를 record에 얹어 새 RegistryRecord를 반환해요(frozen이라 replace).

        descriptors 원본이 Aux에 있으면 그걸로 복원해요(결정 3): Registry엔 mcp.server만
        보내므로, MCP tools·skill markdown 원본을 무손실 왕복하려면 Aux 원본을 다시 씌워야 해요.
        """
        ext = self.get_ext(record.record_id)
        descriptors = ext["descriptors"] if ext["descriptors"] is not None else record.descriptors
        if (
            record.descriptor_type is DescriptorType.AGENT
            and ext["descriptors"] is not None
        ):
            descriptors = _merge_agent_descriptors(record.descriptors, ext["descriptors"])
        # description은 Registry 코어(record)가 우선이고, 비어 있으면 Aux 폴백을 써요.
        # (Registry가 top-level description을 돌려주면 그대로, 안 담긴 백엔드에선 Aux로 복원.)
        description = record.description or (ext["description"] or "")
        return replace(
            record, owner_team=ext["owner_team"], owner_user=ext["owner_user"],
            owner_contact=ext["owner_contact"],
            tags=ext["tags"], category=ext["category"],
            changelog=ext["changelog"], search_visible=ext["search_visible"],
            escalation_contact=ext["escalation_contact"],
            descriptors=descriptors, description=description,
        )

    def _persist(self) -> None:
        if not self._store_path:
            return
        data = {}
        for rid, meta in self._data.items():
            data[rid] = {"views": meta.views}
        payload = {
            "meta": data,
            "ext": self._ext,
            "responsibility_history": self._responsibility_history,
        }
        self._store_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))

    def _load(self) -> None:
        raw = json.loads(self._store_path.read_text())
        # 하위호환: 구 포맷은 평면 {rid: {views, reviews}} — raw 자체가 meta.
        # 신 포맷은 {"meta": {...}, "ext": {...}}.
        # reviews 키는 구 포맷에 존재할 수 있지만 조용히 무시해요.
        meta_raw = raw.get("meta", raw)
        for rid, d in meta_raw.items():
            self._data[rid] = AssetMeta(views=d.get("views", 0))
        self._ext = raw.get("ext", {})
        self._responsibility_history = raw.get(
            "responsibility_history", {}
        )
