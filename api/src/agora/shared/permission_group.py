"""권한 그룹 ↔ 민감도 태그 매핑 (ADR-0018 §3·§4, ADR-0020 admin 관리).

agent에게 부여하는 권한 그룹은 민감도 태그(READ/CREATE/UPDATE/DELETE) 집합의 누적 프리셋이에요.
`agent_policy_compiler`가 이 매핑으로 agent가 호출 가능한 tool을 태그로 필터해요(IA-22e).

이 모듈은 도메인 간 공유 계약이라 `shared/`에 둬요 — identity(컴파일러)가 catalog의
`SensitivityTag`를 직접 import하지 않도록, 태그는 문자열("READ" 등)로 비교해요.
"""
from __future__ import annotations

from enum import Enum


class PermissionGroup(str, Enum):
    """agent tool 권한 그룹. 값은 사용자 노출 라벨이에요(ADR-0018 §3)."""

    READ_ONLY = "ReadOnly"
    READ_CREATE = "ReadCreate"
    READ_WRITE = "ReadWrite"
    FULL_ACCESS = "FullAccess"


# 최소권한 기본값(ADR-0018 §4).
DEFAULT_PERMISSION_GROUP = PermissionGroup.READ_ONLY

# 누적 태그 집합. 상위 그룹은 하위 그룹의 태그를 포함해요.
_GROUP_TAGS: dict[PermissionGroup, frozenset[str]] = {
    PermissionGroup.READ_ONLY: frozenset({"READ"}),
    PermissionGroup.READ_CREATE: frozenset({"READ", "CREATE"}),
    PermissionGroup.READ_WRITE: frozenset({"READ", "CREATE", "UPDATE"}),
    PermissionGroup.FULL_ACCESS: frozenset({"READ", "CREATE", "UPDATE", "DELETE"}),
}

# 상향(관리자 사유·감사 필요) 그룹 — UPDATE/DELETE를 포함하는 그룹(ADR-0018 §4).
_ELEVATED = {PermissionGroup.READ_WRITE, PermissionGroup.FULL_ACCESS}


def allowed_tags(group: PermissionGroup) -> frozenset[str]:
    """그룹이 허용하는 민감도 태그 집합(대문자 문자열)."""
    return _GROUP_TAGS[group]


def group_allows(group: PermissionGroup, tag: str) -> bool:
    """그룹이 주어진 민감도 태그를 허용하는지. 빈 값·미지 태그는 fail-closed(False)."""
    if not tag:
        return False
    return tag.strip().upper() in _GROUP_TAGS[group]


def requires_admin_elevation(group: PermissionGroup) -> bool:
    """ReadWrite·FullAccess는 관리자 사유(justification)·감사가 필요해요(ADR-0018 §4)."""
    return group in _ELEVATED
