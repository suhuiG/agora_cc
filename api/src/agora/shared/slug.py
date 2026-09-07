"""asset_id 세그먼트 슬러그화 헬퍼."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass

from .permission_group import PermissionGroup, allowed_tags


GATEWAY_TARGET_NAME_LIMIT = 100
SENSITIVITY_TARGET_SUFFIXES = {
    tag: f"-{tag.lower()}"
    for tag in allowed_tags(PermissionGroup.FULL_ACCESS)
}
GATEWAY_TARGET_SUFFIX_RESERVE = max(
    len(suffix) for suffix in SENSITIVITY_TARGET_SUFFIXES.values()
)
GATEWAY_TARGET_PREFIX_LIMIT = (
    GATEWAY_TARGET_NAME_LIMIT - GATEWAY_TARGET_SUFFIX_RESERVE
)


@dataclass(frozen=True)
class GatewayTargetNameCollision:
    target_name: str
    conflicting_record_id: str
    conflicting_asset_name: str


def slugify(text: str) -> str:
    r"""이름/principal을 asset_id 세그먼트로 슬러그화해요 (slug-safe 보장).

    소문자화 → [a-z0-9._-] 외 문자를 '-'로 → 중복 '-' 축약 → 양끝 비-alnum 제거.
    결과는 validate_asset_id의 세그먼트 규칙(\A[a-z0-9][a-z0-9._-]*\Z)을 만족해요.

    전부 비-ASCII(한글 등)라 ASCII 슬러그가 비면, 원문의 결정적 해시 8자리를 써요.
    같은 이름은 항상 같은 슬러그 → 재퍼블리시가 같은 asset_id(새 버전)로 가고,
    서로 다른 한글 이름은 서로 다른 asset_id를 받아 충돌하지 않아요.
    """
    s = re.sub(r"[^a-z0-9._-]+", "-", text.lower())
    s = re.sub(r"-{2,}", "-", s)
    s = s.strip("-._")
    if s:
        return s
    # ASCII 슬러그가 비었어요(전부 비-ASCII/기호). 원문 해시로 안정적 고유 세그먼트 생성.
    # hex ∈ [0-9a-f] 이라 선두 문자 규칙(\A[a-z0-9])을 항상 만족해요.
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


def normalized_gateway_target_prefix(name: str) -> str:
    """Gateway 제약 문자로 정규화한 절단 전 prefix를 돌려줘요."""
    normalized = re.sub(r"[^0-9a-zA-Z]+", "-", name).strip("-")
    normalized = re.sub(r"-{2,}", "-", normalized).lower()
    return (
        normalized
        or "mcp-" + hashlib.sha256(name.encode("utf-8")).hexdigest()[:8]
    )


def gateway_target_name(name: str, *, sensitivity: str | None = None) -> str:
    r"""표시명을 Gateway target name 제약 ([0-9a-zA-Z][-]?){1,100} 에 맞게 정규화해요.

    실측(2026-07-20): CreateGatewayTarget은 영숫자·하이픈만 허용하고 공백·`.`·`_` 를
    거부해요(slugify는 `.`·`_` 를 남겨 부족). 여기서 영숫자 외 전부를 하이픈으로
    치환하고 중복·양끝 하이픈을 정리해요. 비면(전부 비-ASCII) `mcp-<sha8>` 해시 폴백.
    민감도 Target 접미어를 위한 최대 길이를 먼저 예약해 최종 이름도 100자를 넘지 않아요.

    Gateway는 이 정규화된 이름을 라이브 tool 접두어(`{target}___{op}`)에 써요. 그래서
    등록 시점의 이 값을 descriptor(gatewayTargetName)에 저장해 authorization 비교 기준을
    통일해요(결함 #10). 저장돼 있지 않은 기존 자산은 비교 시점에 slug(record.name)를 이
    함수로 정규화해 폴백하면, `.`·`_` 자산은 backfill 없이 즉시 정합해요. 단 비-ASCII는
    slug가 이미 hash8로 손실돼(`mcp-` 접두어 없음) 원본 없이는 복원할 수 없으니, 그런
    기존 자산은 재등록/backfill로 저장된 gatewayTargetName이 있어야 정합해요.
    """
    prefix = normalized_gateway_target_prefix(name)
    prefix = prefix[:GATEWAY_TARGET_PREFIX_LIMIT].rstrip("-")
    if sensitivity is None:
        return prefix
    normalized = sensitivity.strip().upper()
    try:
        suffix = SENSITIVITY_TARGET_SUFFIXES[normalized]
    except KeyError as exc:
        raise ValueError(f"알 수 없는 민감도 태그예요: {sensitivity!r}") from exc
    return prefix + suffix


def resolved_gateway_target_name(name: str, stored_name: object = "") -> str:
    """저장된 불변 Target 이름을 우선하고, legacy 레코드만 표시명에서 파생해요."""
    stored = stored_name.strip() if isinstance(stored_name, str) else ""
    return stored or gateway_target_name(name)


def find_gateway_target_name_collision(
    records: Iterable[object],
    *,
    target_name: str,
    current_record_id: str = "",
) -> GatewayTargetNameCollision | None:
    """다른 MCP 자산이 같은 Target 이름을 소유하는지 찾아요.

    명시된 재배포 record만 충돌에서 제외해요. 표시명은 등록 주체가 다른 자산에서도 같을
    수 있으므로 자산 동일성 근거로 사용하지 않아요.
    """
    for record in records:
        record_id = str(getattr(record, "record_id", "") or "")
        record_name = str(getattr(record, "name", "") or "")
        if record_id == current_record_id:
            continue
        descriptor_type = getattr(record, "descriptor_type", "")
        if getattr(descriptor_type, "value", descriptor_type) != "MCP":
            continue
        descriptors = getattr(record, "descriptors", None)
        mcp = descriptors.get("mcp") if isinstance(descriptors, dict) else None
        stored_name = mcp.get("gatewayTargetName") if isinstance(mcp, dict) else ""
        existing_name = resolved_gateway_target_name(record_name, stored_name)
        if existing_name == target_name:
            return GatewayTargetNameCollision(
                target_name=target_name,
                conflicting_record_id=record_id,
                conflicting_asset_name=record_name,
            )
    return None


def gateway_target_name_collision_detail(
    collision: GatewayTargetNameCollision,
) -> dict[str, object]:
    """등록 API가 공통으로 반환하는 행동 가능한 409 본문."""
    return {
        "message": (
            f"Gateway Target 이름 `{collision.target_name}`이 기존 MCP 자산 "
            f"`{collision.conflicting_asset_name}`과 겹쳐요."
        ),
        "remediation": (
            "기존 자산과 다른 Gateway Target 이름이 만들어지도록 MCP 표시명을 바꿔 주세요."
        ),
        "target_name": collision.target_name,
        "conflicting_asset": {
            "record_id": collision.conflicting_record_id,
            "name": collision.conflicting_asset_name,
        },
    }
