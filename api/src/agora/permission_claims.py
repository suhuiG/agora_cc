"""Cognito pre-token-generation V2 진입점."""
from __future__ import annotations

import logging
import os
from typing import Any

from .domains.identity.dynamo_store import DynamoIdentityStore
from .domains.identity.permission_source import (
    AgoraLocalAdapter,
    PermissionSourcePort,
    ScopedPermission,
)

_log = logging.getLogger(__name__)

_permission_source: PermissionSourcePort | None = None
_SCOPE_SEPARATOR = "|"


def _dict_field(parent: dict[str, Any], key: str) -> dict[str, Any]:
    value = parent.get(key)
    if value is None:
        value = {}
        parent[key] = value
    if not isinstance(value, dict):
        raise ValueError(f"Cognito {key} 필드가 객체가 아니에요.")
    return value


def _encode_permissions(permissions: frozenset[ScopedPermission]) -> str:
    encoded: list[str] = []
    for permission in permissions:
        components = (permission.connection_id, permission.capability)
        if any(
            not component
            or component != component.strip()
            or "," in component
            or _SCOPE_SEPARATOR in component
            for component in components
        ):
            raise ValueError("perms claim으로 인코딩할 수 없는 권한이 있어요.")
        # role:* 마커는 Cedar admin-bypass 정책이 bare `,role:admin,` 패턴으로
        # 매칭하므로 connection 접두사 없이 capability 그대로 기록해요.
        if permission.capability.startswith("role:"):
            encoded.append(permission.capability)
        else:
            encoded.append(_SCOPE_SEPARATOR.join(components))
    return f",{','.join(sorted(encoded))},"


def _team_of(user_attributes: dict[str, Any]) -> str:
    """User Pool에 저장된 소유 팀 속성을 읽어요. 없으면 "".

    저장된 사용자 속성을 참조하려면 그 attribute가 User Pool schema에 **존재해야** 해요
    (`request.userAttributes`는 schema에 있는 것만 담아요). 반면 토큰에 claim을 새로 넣는 건
    schema와 무관해요 — 그래서 여기서 읽기는 schema 의존, 쓰기는 자유예요.

    개별 사용자에게 값이 없는 것과 attribute 자체가 없는 것을 구분하지 않아요. 둘 다
    "팀 정보 없음"이고, 호출부의 처리도 같아요.
    """
    for name in ("custom:team", "team"):
        value = user_attributes.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _email_of(user_attributes: dict[str, Any]) -> str:
    """표준 email 속성을 읽어요. 값이 없으면 빈 문자열이에요."""
    value = user_attributes.get("email")
    return value.strip() if isinstance(value, str) else ""


def _gateway_scope() -> str:
    """사람 토큰이 M2 OAuth Gateway 입장 조건을 만족하도록 실을 scope.

    값의 출처는 공유 인프라 좌표 `AGORA_M2_OAUTH_SCOPE`([S]) 하나예요 — Gateway 의
    `allowedScopes` 와 **같은 문자열**이어야 하므로 키를 갈라 두지 않아요. 이 Lambda 는
    typed loader 를 쓰지 않고 `os.environ` 을 직접 읽는 관례라(위 `_get_permission_source`)
    그대로 따라요.

    비어 있거나 공백뿐이면 **빈 문자열**을 돌려줘요. 그 상태(=배포 전)는 오류가 아니라
    정상이에요 — 호출부가 scope 를 아무것도 더하지 않아요(IA-75 음성 대조 ①). 이 Lambda 가
    예외를 던지면 사람 로그인 전체가 깨지므로, env 가 없어도 조용히 넘기되 그 사실만 로그로
    남겨요.
    """
    return os.environ.get("AGORA_M2_OAUTH_SCOPE", "").strip()


def _get_permission_source() -> PermissionSourcePort:
    global _permission_source
    if _permission_source is not None:
        return _permission_source

    table_name = os.environ.get("AGORA_IDENTITY_TABLE", "").strip()
    region = (
        os.environ.get("AGORA_IDENTITY_REGION", "").strip()
        or os.environ.get("AWS_REGION", "").strip()
    )
    if not table_name:
        raise RuntimeError("AGORA_IDENTITY_TABLE이 필요해요.")
    if not region:
        raise RuntimeError("AGORA_IDENTITY_REGION이 필요해요.")

    store = DynamoIdentityStore(table_name=table_name, region=region)
    _permission_source = AgoraLocalAdapter(store)
    return _permission_source


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    # IA-77: 이 핸들러는 Cognito pre-token-generation V2 이벤트 계약만 구현해요.
    # `infra/lib/identity-stack.ts`의 `addTrigger(..., cognito.LambdaVersion.V2_0)`를
    # V3_0으로 올리면 event.version="3"이 들어와 이 가드에서 사람 pool의 모든 토큰
    # 발급이 실패해요(로그인·refresh 포함). 버전을 바꾸려면 핸들러와 회귀를 먼저 함께
    # 확장해야 하며, 단순 CDK 설정 변경으로 올리면 안 돼요.
    if str(event.get("version", "")) != "2":
        raise ValueError("Cognito pre-token-generation V2 요청만 지원해요.")

    user_attributes = event.get("request", {}).get("userAttributes", {})
    principal_id = str(user_attributes.get("sub", "")).strip()
    if not principal_id:
        raise ValueError("Cognito sub가 없는 token generation 요청이에요.")

    permissions = _get_permission_source().permissions_for(principal_id)
    response = _dict_field(event, "response")
    details = _dict_field(response, "claimsAndScopeOverrideDetails")
    access_token = _dict_field(details, "accessTokenGeneration")
    claims = _dict_field(access_token, "claimsToAddOrOverride")
    claims["perms"] = _encode_permissions(permissions)
    # IA-75 ②: 사람 pool 은 SRP 인증이라 access token 에 `aws.cognito.signin.user.admin`
    # scope 만 실려요. Gateway 는 `allowedScopes=[…/invoke]` 를 요구하니 그 scope 를 여기서
    # 얹어요. IA-77 실측(2026-09-02)에서 이 값은 HumanWebClient의 AllowedOAuthScopes에
    # 없어도 토큰에 실렸고, refresh 재발급에서도 새 jti와 함께 유지됐어요. 따라서 web
    # client의 allowedOAuthScopes를 이 이유로 넓히지 않아요. env 가 비면(배포 전) 아무것도
    # 더하지 않고, `scopesToAdd` 키 자체를 만들지 않아요 — 빈 리스트를 넣으면
    # 「배포됐는데 scope 0개」와 구분이 안 돼요.
    gateway_scope = _gateway_scope()
    if gateway_scope:
        access_token["scopesToAdd"] = [gateway_scope]
    else:
        _log.info(
            "AGORA_M2_OAUTH_SCOPE 가 비어 scopesToAdd 를 더하지 않아요 "
            "(사람 토큰이 Gateway invoke scope 없이 발급돼요)."
        )
    # 소유 팀을 access token에 실어요. User Pool schema에 `custom:team`이 있고 값이 채워져
    # 있을 때만 실려요 — 인사 IdP가 그 값을 안 주는 환경에서는 claim 자체가 없고, 백엔드는
    # team=""으로 다뤄요(등록 폼이 자유입력으로 폴백).
    team = _team_of(user_attributes)
    if team:
        claims["team"] = team
    # 담당자 표시용 email. access token에는 email scope가 있어도 값이 실리지 않으므로
    # pre-token 단계에서 추가해요. 인가 판정에는 사용하지 않아요.
    email = _email_of(user_attributes)
    if email:
        claims["email"] = email
    return event
