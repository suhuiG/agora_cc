"""Issue short-lived credentials for authorized delegated AWS access."""
from __future__ import annotations

import json
import logging
from typing import Protocol

from .authorization import AuthorizationService
from .capability_actions import iam_actions_for
from .models import (
    AuthorizationOutcome,
    BrokeredCredentials,
    CredentialMode,
    DelegationContext,
)
from .policy_compiler import compile_policy
from .store import IdentityRecordNotFound, IdentityStore


class CredentialBrokerError(RuntimeError):
    pass


_logger = logging.getLogger("agora.identity.credential_broker")


def _source_identity_denied(exc) -> bool:
    """SetSourceIdentity가 (조직 SCP·권한 등으로) 거부된 AccessDenied인지 판별해요."""
    response = getattr(exc, "response", None)
    detail = response.get("Error", {}) if isinstance(response, dict) else {}
    code = detail.get("Code", "")
    message = detail.get("Message", "")
    return (
        code in {"AccessDenied", "AccessDeniedException"}
        and "SetSourceIdentity" in message
    )


class CredentialBrokerPort(Protocol):
    def assume(
        self,
        *,
        role_arn: str,
        external_id: str | None,
        source_identity: str,
        session_policy: dict | None,
        duration_seconds: int,
    ) -> BrokeredCredentials: ...


class SecretReferenceStore(Protocol):
    def get(self, ref: str) -> str: ...


class FakeBroker:
    """Record AssumeRole inputs and return deterministic offline credentials."""

    def __init__(self) -> None:
        self.last_call: dict | None = None

    def assume(
        self,
        *,
        role_arn: str,
        external_id: str | None,
        source_identity: str,
        session_policy: dict | None,
        duration_seconds: int,
    ) -> BrokeredCredentials:
        self.last_call = {
            "role_arn": role_arn,
            "external_id": external_id,
            "source_identity": source_identity,
            "session_policy": session_policy,
            "duration_seconds": duration_seconds,
        }
        return BrokeredCredentials(
            access_key_id="ASIAFAKE",
            secret_access_key="fake-secret",
            session_token="fake-session-token",
            expiration="2026-08-12T00:15:00Z",
            source_identity=source_identity,
        )


def issue_credentials(
    *,
    authorization: AuthorizationService,
    store: IdentityStore,
    broker: CredentialBrokerPort,
    credential_store: SecretReferenceStore,
    context: DelegationContext,
    asset_id: str,
    operation_id: str,
    duration_seconds: int = 900,
) -> BrokeredCredentials:
    """Reauthorize an invocation and issue its least-privilege AWS session."""
    decision = authorization.decide(
        context,
        asset_id=asset_id,
        operation_id=operation_id,
    )
    if decision.decision is not AuthorizationOutcome.ALLOW:
        raise CredentialBrokerError(
            f"authorization denied: {decision.reason.value}"
        )

    try:
        connection = store.get_connection(decision.connection_id)
    except IdentityRecordNotFound as exc:
        raise CredentialBrokerError("connection not found") from exc

    if connection.credential_mode != CredentialMode.STS.value:
        raise CredentialBrokerError(
            f"unsupported credential_mode: {connection.credential_mode}"
        )
    if not connection.role_arn:
        raise CredentialBrokerError("connection has no role_arn")
    if connection.resource is None or connection.resource.kind != "aws":
        raise CredentialBrokerError("connection target is not an AWS resource")

    session_policy = compile_policy(
        connection.resource,
        iam_actions_for(decision.capabilities),
    )
    if session_policy is None:
        raise CredentialBrokerError("no session policy could be compiled")

    external_id = None
    if connection.external_id_ref:
        try:
            external_id = credential_store.get(connection.external_id_ref)
        except KeyError as exc:
            raise CredentialBrokerError("external ID reference not found") from exc

    return broker.assume(
        role_arn=connection.role_arn,
        external_id=external_id,
        source_identity=context.principal_id,
        session_policy=session_policy,
        duration_seconds=duration_seconds,
    )


class AwsStsBroker:
    """Use AWS STS to assume a customer-delegated role."""

    def __init__(
        self,
        *,
        region: str,
        session_name_prefix: str = "agora",
        client=None,
    ) -> None:
        if client is None:
            import boto3

            client = boto3.client("sts", region_name=region)
        self._sts = client
        self._prefix = session_name_prefix

    def assume(
        self,
        *,
        role_arn: str,
        external_id: str | None,
        source_identity: str,
        session_policy: dict | None,
        duration_seconds: int,
    ) -> BrokeredCredentials:
        from botocore.exceptions import BotoCoreError, ClientError

        kwargs = {
            "RoleArn": role_arn,
            "RoleSessionName": f"{self._prefix}-{source_identity}"[:64],
            "DurationSeconds": duration_seconds,
        }
        if external_id:
            kwargs["ExternalId"] = external_id
        if session_policy is not None:
            kwargs["Policy"] = json.dumps(
                session_policy,
                separators=(",", ":"),
            )

        try:
            try:
                response = self._sts.assume_role(
                    **kwargs, SourceIdentity=source_identity[:64]
                )
            except ClientError as exc:
                if not _source_identity_denied(exc):
                    raise
                # 조직 SCP·권한 등으로 sts:SetSourceIdentity가 막힌 환경에선 SourceIdentity
                # 없이 assume해요. 감사 추적은 RoleSessionName(agora-<Cognito sub>)이 담아요.
                _logger.warning(
                    "sts:SetSourceIdentity denied (likely an org SCP); assuming "
                    "without SourceIdentity — RoleSessionName carries the principal",
                )
                response = self._sts.assume_role(**kwargs)
            credentials = response["Credentials"]
        except (BotoCoreError, ClientError, KeyError, TypeError) as exc:
            raise CredentialBrokerError("AssumeRole failed") from exc

        expiration = credentials["Expiration"]
        if hasattr(expiration, "isoformat"):
            expiration = expiration.isoformat()
        return BrokeredCredentials(
            access_key_id=credentials["AccessKeyId"],
            secret_access_key=credentials["SecretAccessKey"],
            session_token=credentials["SessionToken"],
            expiration=str(expiration),
            source_identity=source_identity,
        )
