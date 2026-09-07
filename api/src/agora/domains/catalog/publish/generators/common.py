"""생성기 공통 헬퍼 — slug, source_prefix 파싱, 해석된 member 값객체."""
from __future__ import annotations

import re
from dataclasses import dataclass


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def parse_source_prefix(source_prefix: str) -> tuple[str, str]:
    """`{type}/{owner}/{name}/{version}/` → (asset_id=owner/name, version)."""
    parts = [p for p in (source_prefix or "").split("/") if p]
    if len(parts) < 4:
        raise ValueError(f"source_prefix 형식이 아니에요: {source_prefix!r}")
    return "/".join(parts[1:-1]), parts[-1]


@dataclass
class ResolvedMember:
    """생성기 입력 — registry 레코드에서 뽑은 배포에 필요한 정보."""

    record_id: str
    name: str
    asset_type: str
    version: str
    asset_id: str
    descriptors: dict
