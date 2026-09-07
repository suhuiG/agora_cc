"""agent 배포·등록 시 AgentIdentityBinding을 자동 발급해요 (IA-08/IA-15b).

지금까지 실제 agent는 Ed25519 workload identity만 받아, IAM 신원 binding이 없었어요.
그래서 `AgentPolicyService.compile_and_deploy`가 모든 agent에 대해 `SKIPPED_NO_IDENTITY`를
반환하고 Cedar 컴파일이 실질적으로 죽어 있었어요(IA-08 '빠진 배선'). 이 발급기가 관리형
런타임 배포와 외부 enroll 시 binding을 써서 신원 평면을 살려요.

관리형 Runtime은 agent별 Cognito M2M client를 발급하고 그 `client_id`를
`AgentCore::OAuthUser` principal로 사용해요(ADR-0016). 외부 IAM binding의 role 정규화
도우미는 기존 경로의 하위 호환을 위해 유지해요.
"""
from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from .cognito_client_names import HUMAN_POOL_AGENT_CLIENT_PREFIX
from .models import AgentIdentityBinding, IdentityBindingStatus, IdentityType
from .store import IdentityRecordNotFound, IdentityStore

# arn:aws:sts::<acct>:assumed-role/<RoleName>/<session> → session 제거(role 수준)
_STS_SESSION_RE = re.compile(
    r"^(?P<base>arn:aws[^:]*:sts::\d+:assumed-role/[^/]+)/.+$"
)
# arn:aws:iam::<acct>:role[/path]/<RoleName> → STS assumed-role 형식으로 변환
_IAM_ROLE_RE = re.compile(
    r"^arn:aws[^:]*:iam::(?P<acct>\d+):role/(?P<path>.*)$"
)
_CLIENT_NAME_RE = re.compile(r"[^A-Za-z0-9._ -]+")


@dataclass(frozen=True)
class ManagedRuntimeClient:
    client_id: str
    created: bool


def cedar_principal_arn(arn: str) -> str:
    """Cedar principal(`AgentCore::IamEntity`)로 쓸 role 수준 **STS assumed-role ARN**을 만들어요.

    M0/M2 실측: Gateway를 SigV4로 부르는 실제 caller는 `arn:aws:sts::<A>:assumed-role/<R>/<session>`
    이고, Cedar permit의 principal.id는 **세션만 제거한 STS ARN** `arn:aws:sts::<A>:assumed-role/<R>`
    예요(Gateway 스택이 출력하는 예상 principal ARN).
    IAM role ARN(iam::role/...)은 이 STS 형식으로 변환해야 permit이 실제 caller와 매칭돼요.

    - STS 세션 ARN(.../assumed-role/R/session) → `.../assumed-role/R`
    - IAM role ARN(iam::A:role[/path]/R) → `arn:aws:sts::A:assumed-role/R` (path 제거, 이름만)
    - 그 외/미상 형식 → 공백만 정리해 그대로(주입 방지는 컴파일러 allowlist가 담당)
    """
    if not arn:
        return ""
    stripped = arn.strip()
    session = _STS_SESSION_RE.match(stripped)
    if session:
        return session.group("base")
    iam = _IAM_ROLE_RE.match(stripped)
    if iam:
        role_name = iam.group("path").split("/")[-1]  # role 경로 제거, 이름만
        return f"arn:aws:sts::{iam.group('acct')}:assumed-role/{role_name}"
    return stripped


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class AgentIdentityIssuer:
    """AgentIdentityBinding을 만들어 identity 스토어에 기록하는 좁은 서비스.

    runtime 도메인은 이 협력자를 duck-typed로 주입받아 identity 도메인을 직접 import하지
    않아요(도메인 경계 유지). 실제 배선은 합성 루트(`shared.deps`)가 해요.
    """

    def __init__(
        self,
        store: IdentityStore,
        *,
        cognito_client=None,
        user_pool_id: str = "",
        invoke_scope: str = "",
        now=None,
        sleep=None,
        reservation_poll_attempts: int = 600,
    ) -> None:
        self._store = store
        self._cognito = cognito_client
        self._user_pool_id = user_pool_id
        self._invoke_scope = invoke_scope
        self._now = now or _now_iso
        self._sleep = sleep or time.sleep
        self._reservation_poll_attempts = reservation_poll_attempts

    @staticmethod
    def _client_name(agent_key: str) -> str:
        safe = _CLIENT_NAME_RE.sub("-", agent_key).strip(" .-") or "agent"
        return f"{HUMAN_POOL_AGENT_CLIENT_PREFIX}{safe}"[:128]

    def _find_managed_client(self, name: str) -> str:
        paginator = self._cognito.get_paginator("list_user_pool_clients")
        matches = {
            str(client.get("ClientId") or "")
            for page in paginator.paginate(
                UserPoolId=self._user_pool_id,
                MaxResults=60,
            )
            for client in page.get("UserPoolClients", ())
            if client.get("ClientName") == name and client.get("ClientId")
        }
        if len(matches) > 1:
            raise RuntimeError(
                "multiple Cognito clients match the managed agent name"
            )
        return next(iter(matches), "")

    def provision_managed_runtime_client(self, agent_key: str) -> str:
        """agent별 M2M app client를 멱등하게 발급하고 client_id를 반환해요."""
        return self.provision_managed_runtime_client_with_provenance(
            agent_key
        ).client_id

    def provision_managed_runtime_client_with_provenance(
        self,
        agent_key: str,
    ) -> ManagedRuntimeClient:
        """M2M client를 발급하고 이번 호출이 생성했는지 함께 반환해요."""
        if self._cognito is None or not self._user_pool_id or not self._invoke_scope:
            return ManagedRuntimeClient(client_id="", created=False)
        name = self._client_name(agent_key)
        reservation_id = uuid.uuid4().hex
        for attempt in range(self._reservation_poll_attempts):
            reservation = self._store.reserve_managed_client(
                agent_key, reservation_id
            )
            if reservation.deleting:
                raise RuntimeError(
                    "managed client deletion is in progress"
                )
            if reservation.client_id:
                return ManagedRuntimeClient(
                    client_id=reservation.client_id,
                    created=False,
                )
            if reservation.acquired:
                break
            observed_client_id = self._find_managed_client(name)
            if observed_client_id:
                self._store.complete_observed_managed_client_reservation(
                    agent_key,
                    observed_client_id,
                )
                return ManagedRuntimeClient(
                    client_id=observed_client_id,
                    created=False,
                )
            if attempt + 1 == self._reservation_poll_attempts:
                raise RuntimeError(
                    "managed client provisioning is already in progress"
                )
            self._sleep(0.05)
        else:
            raise RuntimeError(
                "managed client provisioning reservation was not acquired"
            )

        try:
            client_id = self._find_managed_client(name)
            created = False
            if not client_id:
                response = self._cognito.create_user_pool_client(
                    UserPoolId=self._user_pool_id,
                    ClientName=name,
                    GenerateSecret=True,
                    AllowedOAuthFlowsUserPoolClient=True,
                    AllowedOAuthFlows=["client_credentials"],
                    AllowedOAuthScopes=[self._invoke_scope],
                )
                client_id = str(response["UserPoolClient"]["ClientId"])
                created = True
        except Exception:
            self._store.release_managed_client_reservation(
                agent_key, reservation_id
            )
            raise
        self._store.complete_managed_client_reservation(
            agent_key, reservation_id, client_id
        )
        return ManagedRuntimeClient(client_id=client_id, created=created)

    def get_existing_client_id(self, agent_record_id: str) -> str:
        """기존 binding의 client_id를 반환해 재배포 principal 회전을 막아요."""
        try:
            binding = self._store.get_agent_identity_binding(agent_record_id)
        except IdentityRecordNotFound:
            return ""
        return binding.client_id

    def delete_managed_runtime_client(self, client_id: str) -> None:
        """실패한 신규 배포가 만든 M2M app client를 제거해요."""
        if not client_id or self._cognito is None or not self._user_pool_id:
            return
        try:
            self._cognito.delete_user_pool_client(
                UserPoolId=self._user_pool_id,
                ClientId=client_id,
            )
        except Exception as exc:
            response = getattr(exc, "response", None)
            error = response.get("Error", {}) if isinstance(response, dict) else {}
            if error.get("Code") == "ResourceNotFoundException":
                return
            raise

    def _managed_client_absent(self, client_id: str) -> bool:
        """이 M2M app client가 정말 없는지 Cognito에 직접 물어봐요.

        `NotFound`만 부재로 인정해요. client가 살아 있으면(소유 증거 없이 지우면 안 되니)
        `False`이고, 조회 자체가 실패해도 `False`예요 — 관측 못 한 것을 통과로 접으면
        살아 있는 client를 지웠다고 보고해요.
        """
        if not client_id or self._cognito is None or not self._user_pool_id:
            return False
        try:
            self._cognito.describe_user_pool_client(
                UserPoolId=self._user_pool_id,
                ClientId=client_id,
            )
        except Exception as exc:
            response = getattr(exc, "response", None)
            error = response.get("Error", {}) if isinstance(response, dict) else {}
            return error.get("Code") == "ResourceNotFoundException"
        return False

    def delete_managed_runtime_client_for_record(
        self,
        agent_record_id: str,
        client_id: str,
    ) -> bool:
        """reservation을 tombstone 처리한 뒤 client와 tombstone을 순서대로 지워요.

        reservation이 없으면 소유 증거가 없어서 client를 지우지 않아요 — 남의 client를
        지우지 않으려는 가드예요. 다만 **client도 이미 없으면 목표 상태가 달성돼 있어요.**
        그때만 완료로 봐요(2026-08-29 실측: `k6OLshAhQZdo`의 reservation이 없고 client
        `74jakh487l8b3qae0uh7rhpjq1`도 이미 삭제돼 있었는데, 둘을 구분하지 못해
        `PurgeService`가 identity 원장 정리까지 통째로 멈췄어요).

        부재는 Cognito를 직접 조회해 확인해요 — 기대값의 출처가 원장이 아니라 실체예요
        (ADR-0037 §4). 조회하지 못하면 부재를 주장하지 않아요.
        """
        tombstoned = self._store.tombstone_managed_client_reservation(
            agent_record_id,
            client_id,
        )
        if not tombstoned:
            return self._managed_client_absent(client_id)
        self.delete_managed_runtime_client(client_id)
        finalized = self._store.finalize_managed_client_reservation_deletion(
            agent_record_id,
            client_id,
        )
        if not finalized:
            raise RuntimeError(
                "managed client reservation tombstone could not be finalized"
            )
        return True

    def issue_managed_runtime_binding(
        self,
        *,
        agent_record_id: str,
        runtime_role_arn: str,
        workload_identity_name: str = "",
        client_id: str = "",
    ) -> AgentIdentityBinding | None:
        """관리형 런타임 agent의 OAuth 신원을 발급해요(멱등 — 재배포 시 덮어써요)."""
        client_id = client_id or self.provision_managed_runtime_client(
            agent_record_id
        )
        if not client_id:
            return None
        binding = AgentIdentityBinding(
            agent_record_id=agent_record_id,
            identity_type=IdentityType.OAUTH_CLIENT,
            status=IdentityBindingStatus.ACTIVE,
            workload_identity_name=workload_identity_name or "",
            runtime_role_arn=runtime_role_arn,
            gateway_role_arn="",
            policy_principal_id=client_id,
            client_id=client_id,
            verified_at=self._now(),
        )
        self._store.put_agent_identity_binding(binding)
        return binding

    def issue_external_iam_binding(
        self,
        *,
        agent_record_id: str,
        external_source_role_arn: str,
    ) -> AgentIdentityBinding | None:
        """외부 Agent가 IAM role을 함께 제출하면 EXTERNAL_IAM_ROLE 신원을 발급해요.

        외부 agent는 각자 고유 role을 가지므로 `gateway_role_arn`에 정규화 ARN을 넣어
        uniqueness(같은 role을 두 agent가 주장하지 못하게)를 유지해요.
        """
        if not external_source_role_arn:
            return None
        principal = cedar_principal_arn(external_source_role_arn)
        binding = AgentIdentityBinding(
            agent_record_id=agent_record_id,
            identity_type=IdentityType.EXTERNAL_IAM_ROLE,
            status=IdentityBindingStatus.ACTIVE,
            external_source_role_arn=external_source_role_arn,
            gateway_role_arn=principal,
            policy_principal_id=principal,
            verified_at=self._now(),
        )
        self._store.put_agent_identity_binding(binding)
        return binding
