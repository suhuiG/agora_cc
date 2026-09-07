"""BundleService — bundle CRUD + 멤버 record_id 확장 조회.

멤버는 라이브 참조(record_id만 보관). expand_members가 조회 시점에 registry로 확장하고,
삭제(RecordNotFound)·미승인(status != APPROVED)은 available=False로 표시(spec 결정 4·5).
registry는 shared.deps.get_registry()로 주입 — bundle은 SoT를 직접 쓰지 않아요(ADR-004).
"""
from __future__ import annotations

from dataclasses import dataclass

from ..catalog.publish.naming import validate_plugin_name
from ..catalog.registry.models import RecordStatus
from .models import Bundle


@dataclass
class ExpandedMember:
    record_id: str
    name: str
    type: str
    status: str
    available: bool


_UPDATABLE = ("name", "description", "member_ids", "surfaces", "category", "tags")


class BundleService:
    def __init__(self, *, store, registry, registry_id, new_id, now) -> None:
        self.store = store
        self.registry = registry
        self.registry_id = registry_id
        self.new_id = new_id
        self.now = now

    def create(self, *, name, description, member_ids, surfaces,
               category, tags, created_by, owner_principal="") -> Bundle:
        ts = self.now()
        b = Bundle(
            bundle_id=self.new_id(), name=name, description=description,
            member_ids=list(member_ids), surfaces=list(surfaces),
            category=category, tags=list(tags), created_by=created_by,
            owner_principal=owner_principal,
            created_at=ts, updated_at=ts,
        )
        self.store.put(b)
        return b

    def can_modify(self, bundle: Bundle, *, principal: str, is_admin: bool) -> bool:
        """그룹을 편집·삭제·신청할 수 있는지. admin은 항상, 사용자는 자기 것만.

        owner_principal이 빈 그룹(관리자가 만든 기존 레코드)은 admin만 손댈 수 있어요 —
        빈 문자열을 아무 사용자와 매칭시키면 남의 그룹을 열어주게 돼요.
        """
        if is_admin:
            return True
        return bool(bundle.owner_principal) and bundle.owner_principal == principal

    def get(self, bundle_id):
        return self.store.get(bundle_id)

    def list(self):
        return self.store.list()

    def update(self, bundle_id, **fields) -> Bundle | None:
        b = self.store.get(bundle_id)
        if b is None:
            return None
        for k, v in fields.items():
            if k in _UPDATABLE and v is not None:
                setattr(b, k, list(v) if k in ("member_ids", "surfaces", "tags") else v)
        b.updated_at = self.now()
        self.store.put(b)
        return b

    def delete(self, bundle_id) -> bool:
        return self.store.delete(bundle_id)

    def mark_published(self, bundle_id, *, sha, version, at,
                       pr_url="") -> Bundle | None:
        b = self.store.get(bundle_id)
        if b is None:
            return None
        b.publish_status = "PUBLISHED"
        b.last_published_sha = sha
        b.last_published_version = version
        b.last_published_at = at
        b.last_published_pr_url = pr_url
        b.updated_at = self.now()
        self.store.put(b)
        return b

    def request_deploy(self, bundle_id, *, principal) -> Bundle | None:
        """배포 신청. 미승인 자산·빈 그룹·예약 이름이면 ValueError로 거부해요.

        확정 결정: 미승인 자산은 skip이 아니라 신청 자체를 거부해요 — 사용자가
        자기가 고른 게 빠진 걸 모르는 상황을 만들지 않으려고요.
        """
        b = self.store.get(bundle_id)
        if b is None:
            return None
        if not b.member_ids:
            raise ValueError("자산을 하나 이상 담아야 신청할 수 있어요")
        validate_plugin_name(b.name)      # 예약어·길이·slug — ValueError를 그대로 올려요

        unapproved = [m.record_id for m in self.expand_members(b) if not m.available]
        if unapproved:
            raise ValueError(
                "승인되지 않은 자산이 있어 신청할 수 없어요: " + ", ".join(unapproved))

        b.owner_principal = principal
        b.request_status = "PENDING"
        b.updated_at = self.now()
        self.store.put(b)
        return b

    def review(self, bundle_id, *, approved, reviewer, note="") -> Bundle | None:
        """관리자 검토 결과를 기록해요. 실제 배포는 라우터가 승인 후 호출해요."""
        b = self.store.get(bundle_id)
        if b is None:
            return None
        b.request_status = "APPROVED" if approved else "REJECTED"
        b.reviewed_by = reviewer
        b.review_note = note
        b.updated_at = self.now()
        self.store.put(b)
        return b

    def expand_members(self, bundle: Bundle) -> list[ExpandedMember]:
        out = []
        for rid in bundle.member_ids:
            try:
                rec = self.registry.get_record(self.registry_id, rid)
            except Exception:
                # RecordNotFound(삭제)뿐 아니라 실 AWS 조회 실패(botocore
                # ParamValidationError 등 잘못된/빈 record_id)도 흡수해요.
                # 멤버 하나가 잘못돼도 번들 전체 상세가 500으로 깨지면 안 돼요.
                out.append(ExpandedMember(rid, rid, "", "MISSING", False))
                continue
            available = rec.status == RecordStatus.APPROVED
            out.append(ExpandedMember(
                rid, rec.name, rec.descriptor_type.value, rec.status.value, available))
        return out
