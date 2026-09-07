"""요청 인증 모드와 Principal 생성."""
from __future__ import annotations

from starlette.datastructures import Headers

from ...shared.config import Config, load_config
from .models import Principal
from .token_verifier import (
    AuthenticationError,
    CognitoTokenVerifier,
    IdentityConfigurationError,
)


class IdentityService:
    def __init__(self, config: Config, *, verifier: CognitoTokenVerifier | None = None):
        self.config = config
        self.mode = config.auth_mode
        if config.stage == "prod" and self.mode != "cognito":
            raise IdentityConfigurationError("prod에서는 AGORA_AUTH_MODE=cognito만 허용돼요.")
        if self.mode == "cognito":
            if not config.auth_cognito_issuer or not config.auth_cognito_client_id:
                raise IdentityConfigurationError(
                    "cognito 모드는 AGORA_AUTH_COGNITO_ISSUER와 "
                    "AGORA_AUTH_COGNITO_CLIENT_ID가 필요해요."
                )
            self.verifier = verifier or CognitoTokenVerifier(
                issuer=config.auth_cognito_issuer,
                client_id=config.auth_cognito_client_id,
            )
        else:
            self.verifier = verifier

    @classmethod
    def from_env(cls) -> "IdentityService":
        return cls(load_config())

    def authenticate(self, headers: Headers) -> Principal:
        if self.mode == "cognito":
            scheme, _, token = headers.get("authorization", "").partition(" ")
            if scheme.lower() != "bearer" or not token:
                raise AuthenticationError("Authorization Bearer token이 필요해요.")
            assert self.verifier is not None
            return self.verifier.verify(token)
        if self.mode == "dev":
            return Principal(
                principal_id=self.config.dev_principal,
                roles=self.config.dev_roles,
                source="dev",
            )

        # test 모드만 기존 헤더를 허용해 기존 계약 테스트의 사용자 전환을 격리해요.
        principal_id = headers.get("x-agora-principal", "test-user").strip() or "test-user"
        roles = tuple(
            role.strip().lower()
            for role in headers.get("x-agora-roles", "admin").split(",")
            if role.strip()
        )
        # 담당자 연락처는 서버가 principal.email 에서 파생해요(CA-29). test 모드에서 그
        # 파생을 검증할 수 있어야 해서 email 도 헤더로 받아요 — 헤더 신뢰는 이 모드에만
        # 있고, cognito/dev 모드는 토큰·설정에서만 email 을 얻어요.
        email = headers.get("x-agora-email", "").strip()
        return Principal(
            principal_id=principal_id, roles=roles, source="test", email=email,
        )
