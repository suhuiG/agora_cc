"""Bundle 값객체 — 자산 record_id 라이브 참조 묶음 + 메타.

registry 레코드가 아니라 로컬 JSON(JsonBundleStore)에 저장돼요(AWS Registry 오염 회피).
멤버는 record_id만 보관하고, 조회 시점에 BundleService가 registry로 확장해요.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Bundle:
    bundle_id: str
    name: str
    description: str = ""
    member_ids: list[str] = field(default_factory=list)
    surfaces: list[str] = field(default_factory=list)
    category: str = ""
    tags: list[str] = field(default_factory=list)
    created_by: str = ""
    created_at: str = ""
    updated_at: str = ""
    publish_status: str = "DRAFT"
    last_published_at: str = ""
    last_published_sha: str = ""
    last_published_version: str = ""
    # 그룹을 만든 사용자. created_by는 관리자 생성 이력이라 별도로 둬요.
    owner_principal: str = ""
    # NONE | PENDING | APPROVED | REJECTED
    request_status: str = "NONE"
    reviewed_by: str = ""
    review_note: str = ""
    last_published_pr_url: str = ""

    def to_dict(self) -> dict:
        return {
            "bundle_id": self.bundle_id, "name": self.name,
            "description": self.description, "member_ids": list(self.member_ids),
            "surfaces": list(self.surfaces), "category": self.category,
            "tags": list(self.tags), "created_by": self.created_by,
            "created_at": self.created_at, "updated_at": self.updated_at,
            "publish_status": self.publish_status,
            "last_published_at": self.last_published_at,
            "last_published_sha": self.last_published_sha,
            "last_published_version": self.last_published_version,
            "owner_principal": self.owner_principal,
            "request_status": self.request_status,
            "reviewed_by": self.reviewed_by,
            "review_note": self.review_note,
            "last_published_pr_url": self.last_published_pr_url,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Bundle":
        return cls(
            bundle_id=d["bundle_id"], name=d["name"],
            description=d.get("description", ""),
            member_ids=list(d.get("member_ids", [])),
            surfaces=list(d.get("surfaces", [])),
            category=d.get("category", ""), tags=list(d.get("tags", [])),
            created_by=d.get("created_by", ""),
            created_at=d.get("created_at", ""), updated_at=d.get("updated_at", ""),
            publish_status=d.get("publish_status", "DRAFT"),
            last_published_at=d.get("last_published_at", ""),
            last_published_sha=d.get("last_published_sha", ""),
            last_published_version=d.get("last_published_version", ""),
            owner_principal=d.get("owner_principal", ""),
            request_status=d.get("request_status", "NONE"),
            reviewed_by=d.get("reviewed_by", ""),
            review_note=d.get("review_note", ""),
            last_published_pr_url=d.get("last_published_pr_url", ""),
        )
