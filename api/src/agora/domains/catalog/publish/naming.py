"""plugin·marketplace 이름 검증 — managed 콘솔 규약.

콘솔이 거부하는 이름을 배포 전에 걸러요. 예약어 목록과 길이 상한은 공식 문서
"Manage plugins for your organization" 기준이에요.
"""
from __future__ import annotations

from .generators.common import slugify

# 공식 문서가 명시한 예약 marketplace 이름. slug 형태로 비교해요.
RESERVED_MARKETPLACE_NAMES = frozenset({
    "claude-code-marketplace",
    "claude-code-plugins",
    "claude-plugins-official",
    "anthropic-marketplace",
    "anthropic-plugins",
    "agent-skills",
    "life-sciences",
})

MAX_NAME_LENGTH = 64


def validate_plugin_name(name: str) -> str:
    """이름을 검증하고 slug를 돌려줘요. 위반 시 ValueError(사용자 노출 메시지)."""
    slug = slugify(name or "")
    if not slug:
        raise ValueError(
            "이름에 영문자나 숫자가 하나는 있어야 해요 (소문자·하이픈으로 변환돼요)")
    if len(slug) > MAX_NAME_LENGTH:
        raise ValueError(
            f"이름이 너무 길어요 — {MAX_NAME_LENGTH}자 이내여야 해요 (현재 {len(slug)}자)")
    if slug in RESERVED_MARKETPLACE_NAMES:
        raise ValueError(f"'{slug}'는 예약된 이름이라 쓸 수 없어요")
    return slug
