"""Cognito access token 검증."""
from __future__ import annotations

from collections.abc import Callable

import jwt
from jwt import PyJWKClient

from .models import Principal, filter_platform_roles

_CLOCK_SKEW_SECONDS = 120


class AuthenticationError(ValueError):
    """인증 정보가 없거나 유효하지 않음."""


class AuthorizationError(ValueError):
    """인증은 됐지만 Agora 플랫폼 역할이 없음."""


class IdentityConfigurationError(RuntimeError):
    """운영 인증 설정이 안전하지 않거나 누락됨."""


# access token에서 소유 팀을 읽을 claim 이름. pre-token Lambda는 `team`으로 넣지만,
# IdP attribute mapping을 직접 쓰는 배포에서는 `custom:team`이 그대로 실릴 수 있어요.
_TEAM_CLAIMS = ("team", "custom:team")
_EMAIL_CLAIMS = ("email",)


def _team_of(claims: dict) -> str:
    """소유 팀 claim을 읽어요. 없으면 "" — 인사 IdP가 값을 안 주는 환경이 정상이에요.

    이 값은 **표시·기본값 용도**예요. 인가 판정에 쓰지 않아요 — 팀 문자열은 IdP가 채우는
    자유 텍스트라 권한 경계로 삼기엔 검증이 없어요(권한은 `perms` claim과 grant가 담당).
    """
    for name in _TEAM_CLAIMS:
        value = claims.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _email_of(claims: dict) -> str:
    """검증된 claim에서 표시용 email을 읽어요. 미제공은 정상이에요."""
    for name in _EMAIL_CLAIMS:
        value = claims.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


class CognitoTokenVerifier:
    """RS256 Cognito access token을 검증해 Principal로 변환해요."""

    def __init__(
        self,
        *,
        issuer: str,
        client_id: str,
        signing_key_resolver: Callable[[str], object] | None = None,
    ):
        self.issuer = issuer.rstrip("/")
        self.client_id = client_id
        if signing_key_resolver is None:
            jwks = PyJWKClient(f"{self.issuer}/.well-known/jwks.json", cache_keys=True)

            def resolve_signing_key(token: str) -> object:
                return jwks.get_signing_key_from_jwt(token).key

            signing_key_resolver = resolve_signing_key
        self._signing_key_resolver = signing_key_resolver

    def verify(self, token: str) -> Principal:
        if not token:
            raise AuthenticationError("Bearer token이 필요해요.")
        try:
            key = self._signing_key_resolver(token)
            claims = jwt.decode(
                token,
                key,
                algorithms=["RS256"],
                issuer=self.issuer,
                leeway=_CLOCK_SKEW_SECONDS,
                options={"verify_aud": False, "require": ["exp", "iat", "iss", "sub"]},
            )
        except (jwt.PyJWTError, ValueError, TypeError) as exc:
            raise AuthenticationError("Cognito access token이 유효하지 않아요.") from exc

        if claims.get("token_use") != "access":
            raise AuthenticationError("Cognito access token만 사용할 수 있어요.")
        if claims.get("client_id") != self.client_id:
            raise AuthenticationError("다른 Cognito app client의 token이에요.")

        roles = filter_platform_roles(claims.get("cognito:groups", ()))
        if not roles:
            raise AuthorizationError("Agora user/admin 그룹이 필요해요.")

        return Principal(
            principal_id=str(claims["sub"]),
            roles=roles,
            source="cognito",
            token_id=str(claims.get("jti", "")),
            token_expires_at=int(claims["exp"]),
            team=_team_of(claims),
            email=_email_of(claims),
        )
