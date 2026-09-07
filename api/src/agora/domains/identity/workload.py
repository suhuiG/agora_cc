"""Runtime/Gateway Cognito M2M access token 검증."""
from __future__ import annotations

import base64
import json
import re
import time
from collections.abc import Callable
from threading import Lock

import jwt
from jwt import PyJWKClient
from jwt.exceptions import PyJWKClientConnectionError
from starlette.datastructures import Headers

from .authorization import record_security_rejection
from .models import DecisionReason, SecurityFailureType, WorkloadPrincipal
from .token_verifier import AuthenticationError, IdentityConfigurationError

_NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
_WORKLOAD_TOKEN_HEADER = "x-agora-workload-token"


class WorkloadTokenVerifier:
    def __init__(
        self,
        *,
        issuer: str,
        client_id: str,
        required_scope: str,
        signing_key_resolver: Callable[[str], object] | None = None,
    ) -> None:
        if not issuer or not client_id or not required_scope:
            raise IdentityConfigurationError(
                "workload Cognito issuer, client ID와 invoke scope가 필요해요."
            )
        self.issuer = issuer.rstrip("/")
        self.client_id = client_id
        self.required_scope = required_scope
        if signing_key_resolver is None:
            jwks = PyJWKClient(
                f"{self.issuer}/.well-known/jwks.json", cache_keys=True
            )

            def resolve(token: str) -> object:
                return jwks.get_signing_key_from_jwt(token).key

            signing_key_resolver = resolve
        self._resolve = signing_key_resolver

    def authenticate(self, headers: Headers) -> WorkloadPrincipal:
        token = headers.get(_WORKLOAD_TOKEN_HEADER, "").strip()
        if not token:
            raise self._authentication_error(
                "workload token이 필요해요.",
                SecurityFailureType.WORKLOAD_TOKEN_MISSING,
            )
        try:
            signing_key = self._resolve(token)
        except (PyJWKClientConnectionError, OSError, TimeoutError) as exc:
            raise IdentityConfigurationError(
                "workload JWKS를 조회할 수 없어요."
            ) from exc
        except (jwt.PyJWTError, ValueError, TypeError) as exc:
            raise self._authentication_error(
                "workload access token이 유효하지 않아요.",
                SecurityFailureType.WORKLOAD_TOKEN_INVALID,
            ) from exc
        try:
            claims = jwt.decode(
                token,
                signing_key,
                algorithms=["RS256"],
                issuer=self.issuer,
                options={"verify_aud": False, "require": ["exp", "iat", "iss", "sub"]},
            )
        except (jwt.PyJWTError, ValueError, TypeError) as exc:
            raise self._authentication_error(
                "workload access token이 유효하지 않아요.",
                SecurityFailureType.WORKLOAD_TOKEN_INVALID,
            ) from exc
        if claims.get("token_use") != "access":
            raise self._authentication_error(
                "workload access token만 사용할 수 있어요.",
                SecurityFailureType.WORKLOAD_TOKEN_INVALID,
            )
        if claims.get("client_id") != self.client_id:
            raise self._authentication_error(
                "승인되지 않은 workload client예요.",
                SecurityFailureType.WORKLOAD_TOKEN_INVALID,
            )
        scopes = tuple(
            sorted(
                scope
                for scope in str(claims.get("scope", "")).split()
                if scope
            )
        )
        if self.required_scope not in scopes:
            raise self._authentication_error(
                "workload invoke scope가 없어요.",
                SecurityFailureType.WORKLOAD_TOKEN_INVALID,
            )
        return WorkloadPrincipal(
            workload_id=self.client_id,
            client_id=self.client_id,
            scopes=scopes,
            token_id=str(claims.get("jti", "")),
        )

    def _authentication_error(
        self, message: str, failure_type: SecurityFailureType
    ) -> AuthenticationError:
        record_security_rejection(
            reason=DecisionReason.WORKLOAD_AUTH_FAILED,
            failure_type=failure_type,
            workload_id=self.client_id,
        )
        return AuthenticationError(message)


class WorkloadAssertionVerifier:
    """Runtime별 Ed25519 key로 authorization 요청 전체의 출처를 검증해요."""

    def __init__(
        self,
        *,
        now=None,
        max_clock_skew_seconds: int = 60,
        claim_nonce: Callable[[str, str, int], bool] | None = None,
    ) -> None:
        self._now = now or (lambda: int(time.time()))
        self.max_clock_skew_seconds = max_clock_skew_seconds
        self._seen_nonces: dict[tuple[str, str], int] = {}
        self._nonce_lock = Lock()
        self._claim_nonce = claim_nonce or self._claim_nonce_locally

    def authenticate(
        self,
        headers: Headers,
        *,
        workload_id: str,
        public_key: str,
        request_body: dict,
    ) -> WorkloadPrincipal:
        claimed_id = headers.get("x-agora-workload-id", "").strip()
        timestamp_raw = headers.get("x-agora-workload-timestamp", "").strip()
        nonce = headers.get("x-agora-workload-nonce", "").strip()
        signature_raw = headers.get("x-agora-workload-signature", "").strip()
        if (
            not workload_id
            or not public_key
            or claimed_id != workload_id
            or not timestamp_raw
            or not _NONCE_RE.fullmatch(nonce)
            or not signature_raw
        ):
            raise _assertion_error(
                "Runtime workload 증명이 필요해요.",
                workload_id=workload_id,
                failure_type=SecurityFailureType.WORKLOAD_ASSERTION_INVALID,
            )
        try:
            timestamp = int(timestamp_raw)
        except ValueError as exc:
            raise _assertion_error(
                "Runtime workload 증명이 유효하지 않아요.",
                workload_id=workload_id,
                failure_type=SecurityFailureType.WORKLOAD_ASSERTION_INVALID,
            ) from exc
        if abs(self._now() - timestamp) > self.max_clock_skew_seconds:
            raise _assertion_error(
                "Runtime workload 증명이 만료됐어요.",
                workload_id=workload_id,
                failure_type=SecurityFailureType.WORKLOAD_ASSERTION_EXPIRED,
            )

        message = workload_assertion_message(
            workload_id=workload_id,
            timestamp=timestamp,
            nonce=nonce,
            request_body=request_body,
        )
        try:
            from cryptography.exceptions import InvalidSignature
            from cryptography.hazmat.primitives.asymmetric.ed25519 import (
                Ed25519PublicKey,
            )

            key = Ed25519PublicKey.from_public_bytes(_b64url_decode(public_key))
            key.verify(_b64url_decode(signature_raw), message)
        except InvalidSignature as exc:
            raise _assertion_error(
                "Runtime workload 증명이 유효하지 않아요.",
                workload_id=workload_id,
                failure_type=SecurityFailureType.WORKLOAD_ASSERTION_INVALID,
            ) from exc
        except (TypeError, ValueError) as exc:
            raise _assertion_error(
                "Runtime workload 증명이 유효하지 않아요.",
                workload_id=workload_id,
                failure_type=SecurityFailureType.WORKLOAD_ASSERTION_INVALID,
            ) from exc
        try:
            claimed = self._claim_nonce(
                workload_id,
                nonce,
                timestamp + self.max_clock_skew_seconds + 1,
            )
        except Exception as exc:
            raise IdentityConfigurationError(
                "Runtime workload nonce를 저장할 수 없어요."
            ) from exc
        if not claimed:
            raise _assertion_error(
                "Runtime workload 증명이 이미 사용됐어요.",
                workload_id=workload_id,
                failure_type=SecurityFailureType.WORKLOAD_ASSERTION_REPLAYED,
            )
        return WorkloadPrincipal(
            workload_id=workload_id,
            client_id=workload_id,
            scopes=("agora/authorize",),
            token_id=nonce,
        )

    def _claim_nonce_locally(
        self, workload_id: str, nonce: str, expires_at: int
    ) -> bool:
        now = self._now()
        key = (workload_id, nonce)
        with self._nonce_lock:
            self._seen_nonces = {
                item: expiry
                for item, expiry in self._seen_nonces.items()
                if expiry > now
            }
            if key in self._seen_nonces:
                return False
            self._seen_nonces[key] = expires_at
            return True


def workload_assertion_message(
    *,
    workload_id: str,
    timestamp: int,
    nonce: str,
    request_body: dict,
) -> bytes:
    payload = {
        "nonce": nonce,
        "request": request_body,
        "timestamp": timestamp,
        "version": 1,
        "workload_id": workload_id,
    }
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _assertion_error(
    message: str,
    *,
    workload_id: str,
    failure_type: SecurityFailureType,
) -> AuthenticationError:
    record_security_rejection(
        reason=DecisionReason.WORKLOAD_AUTH_FAILED,
        failure_type=failure_type,
        workload_id=workload_id,
    )
    return AuthenticationError(message)


def issuer_from_discovery_url(discovery_url: str) -> str:
    suffix = "/.well-known/openid-configuration"
    if not discovery_url.endswith(suffix):
        raise IdentityConfigurationError(
            "workload Cognito discovery URL 형식이 올바르지 않아요."
        )
    return discovery_url[: -len(suffix)]
