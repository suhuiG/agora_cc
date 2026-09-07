"""Issue and broker short-lived local-development OAuth identities."""
from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from dataclasses import replace
from datetime import datetime, timezone

from ...shared.dev_identity import (
    DevAuthorizationFailure,
    DevAuthorizationReason,
    DevAuthorizationState,
    DevIdentityAuthorizationError,
    DevIdentityNoAccessError,
    DevSelectedTool,
)
from ...shared.gateway_tools import McpGatewayTargetError, gateway_tool_name
from .agent_policy_compiler import (
    AgentPolicySpec,
    NoToolAccessError,
    compile_agent_policy,
)
from .agent_policy_deployer import STALE_REVISION_CLEANUP_FAILED
from .dev_identity_models import (
    DevActionGrant,
    DevIdentityAuthenticationError,
    DevIdentityCredential,
    DevIdentityInfrastructureError,
    DevIdentityNotFound,
    DevIdentityOwnershipError,
    IssuedDevIdentityCredential,
    SharedPolicyConvergence,
)
from .dev_identity_store import DevIdentityStore
from .models import (
    AgentIdentityBinding,
    AgentPolicyDeployment,
    AgentToolBinding,
    ApprovalState,
    AssetCapabilityStatus,
    CapabilityStatus,
    ConnectionStatus,
    DesiredState,
    EffectiveState,
    GrantStatus,
    IdentityBindingStatus,
    IdentityType,
    PolicyDeploymentStatus,
)
from .store import IdentityRecordNotFound, IdentityStore

_log = logging.getLogger(__name__)


#: 이 행을 만든 주체. 사람 principal 이 아닌 시스템 표식을 써서, 관리자가 손댄 binding 과
#: provenance 를 구분해요(배포 경로의 `system:readonly-baseline` 과 같은 규약).
_DEV_BASELINE_PRINCIPAL = "system:dev-credential"

#: Registry 가 이 민감도로 태그한 operation 만 자동 승인해요. 넓히지 마세요 — 넓히면
#: dev 크리덴셜이 관리자 승인 게이트를 우회해요.
_AUTO_APPROVED_SENSITIVITY = "READ"

#: 로컬 다운로드 크리덴셜에 담을 수 있는 **최대 민감도**. 위 자동승인 상수와 값이 같지만
#: 다른 질문에 답해요 — 저건 "승인을 대신해 줄까", 이건 "ZIP 에 담아도 되나" 예요.
#: (`catalog_read_access.AUTO_GRANT_SENSITIVITY` 는 또 다른 축인 회원 기본 부여예요.)
#:
#: ## 이건 조이기가 아니라 **새로 붙이는 강제**예요
#:
#: `_authorized_actions` 에는 지금까지 민감도 게이트가 **한 곳도 없었어요.** 쓰기 operation 이
#: 안 나가던 건 `provision_read_access` 가 READ 에만 `AssetCapability` 행을 만들어서
#: (`catalog_read_access.py:202-205`) 아래 `ASSET_CAPABILITY_MISSING` 에서 걸렸기 때문이에요.
#: 즉 관리자가 쓰기 capability 를 한 번 승인하면 그 순간부터 쓰기가 다운로드 가능한 크리덴셜로
#: 흘러갔어요. 그래서 **승인 상태와 무관하게** 여기서 잘라요.
#:
#: ## 왜 배포는 되는데 다운로드는 안 되나
#:
#: 배포된 agent 는 Agora 가 통제하는 환경에서 돌아요 — 코드도 env 도 사람이 못 고쳐요.
#: 로컬 ZIP 은 반대예요: 사용자가 그 코드를 편집하고 LLM 이 그걸 운전해요. 그래서 같은 사람의
#: 같은 권한이라도 로컬로 나가는 쪽에는 되돌릴 수 없는 조작을 담지 않아요
#: (ADR-0095 §02 · §13 — 생성 코드에 사람의 원본 토큰을 주지 않는 것과 같은 근거).
_DOWNLOAD_SENSITIVITY_CEILING = "READ"

#: 만료 sweep 이 미수렴 공유 ① 정책을 **자동으로** 다시 맞추는 최대 횟수 (IH-160, ADR-0110).
#:
#: ## 왜 무한 재시도가 아닌가
#:
#: `provision()` 이 `create_failed` 로 끝나면 **활성화 실패한 정책이 엔진에 남아요** —
#: `_rollback` 은 이미 성공한 것만 지우고, 터미널 실패한 그 정책의 id 는 `_create_active`
#: 안에서 사라져요. 다음 시도는 `_latest_resumable_revision` 이 그 리비전을 재사용하지 못해
#: `max+1` 을 새로 만들고, 그것도 같은 이유로 실패해요. 즉 무인 pass 가 60초마다 무한 재시도하면
#: **하루 1,440장의 실패 리비전**이 쌓이고, engine 당 정책 1,000장 상한(ADR-0099 §05)을 약
#: 17시간 만에 넘겨 그 Gateway 의 **모든** 정책 쓰기가 막혀요. 수렴 장치가 장애를 만드는 셈이에요.
#:
#: ## 이 값을 왜 5 로 두나
#:
#: 실제로 관측된 미수렴 원인은 「활성화가 8폴(≈8초) 예산보다 느림」이에요(IH-154, 활성화 10초).
#: 그 상태는 `_latest_resumable_revision` 이 **같은 리비전을 재개**하므로 재시도가 리비전을
#: 만들지 않고, 한두 주기 안에 끝나요. 5 는 그 관측값의 몇 배 여유이고, 동시에 실패 리비전
#: 상한이기도 해요.
#:
#: 예산이 소진되면 조용히 멈추지 않아요 — 매 주기 WARNING 으로 남기고, 다음 원장 변경
#: (승인·반려·회수·배포·purge·재발급)이 어차피 `provision()` 을 다시 걸어요(ADR-0104 결정 3).
_CONVERGENCE_MAX_RETRIES = 5


def _iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _clean_groups(groups: tuple[str, ...] | list[str] | None) -> tuple[str, ...]:
    """그룹 이름을 정규화해요 — 빈 값·공백을 걸러 중복 없이 정렬해요.

    `list_grants` 가 빈 문자열 그룹을 받으면 `GROUP#` 파티션을 Query 해서 엉뚱한 행을 볼 수
    있어요. 한 곳에서 걸러요.
    """
    if not groups:
        return ()
    return tuple(sorted({item.strip() for item in groups if item and item.strip()}))


class DevIdentityService:
    def __init__(
        self,
        store: DevIdentityStore,
        identity_store: IdentityStore,
        *,
        client_provisioner,
        policy_manager,
        token_issuer,
        gateway_arn: str,
        credential_ttl_days: int,
        credential_ttl_max_days: int,
        asset_context_resolver,
        now,
        delegation_service=None,
        group_directory=None,
        provision_shared_policy=None,
        retry_actor_available=None,
    ) -> None:
        self._store = store
        self._identity = identity_store
        self._clients = client_provisioner
        self._policies = policy_manager
        self._tokens = token_issuer
        self._gateway_arn = gateway_arn
        self._ttl_days = max(1, credential_ttl_days)
        self._ttl_max_days = max(1, credential_ttl_max_days)
        self._asset_context_resolver = asset_context_resolver
        self._now = now
        # 없으면 같은 원장 위에 하나 만들어요 — handle 발급은 이 서비스의 계약이고, 배선을
        # 잊었을 때 조용히 실패하는 대신 항상 동작하게요. 시간 함수는 이 서비스와 공유해요.
        if delegation_service is None:
            from .delegation import DelegationService

            delegation_service = DelegationService(identity_store, now=now)
        self._delegation = delegation_service
        # 호출 시점에 **사람의 현재 그룹**을 다시 읽는 통로예요(`list_groups(sub)`).
        #
        # 없으면 발급 시점에 얼려둔 그룹을 쓰는데, 그러면 사람을 그룹에서 빼도 크리덴셜
        # 수명(기본 7일) 동안 접근이 유지돼요. 그게 이 필드가 있는 이유예요 — 배선을 잊으면
        # 조용히 낡은 값으로 돌아가니 `call_handle` 이 그 상태를 **거부**해요.
        self._groups = group_directory
        # 공유 ① Cedar 정책 재프로비저닝 통로예요 (IH-152 결정 2, ADR-0108).
        #
        # dev 크리덴셜 경로는 ④ 행을 **만들고 지우는** 쪽인데 재프로비저닝을 걸지 않아서,
        # ADR-0104 결정 3 이 나열한 「열거를 바꾸는 원장 쓰기」 중 유일하게 남은 갭이었어요.
        # 없으면 로그만 남기고 넘어가요 — 배선은 `deps.get_dev_identity_service` 가 하고
        # 회귀 테스트로 고정해 뒀어요.
        self._provision_shared_policy = provision_shared_policy
        # 이 프로세스에 수렴 재시도 actor가 실제로 있는지 묻는 통로예요.
        #
        # 기본값은 의도적으로 False예요. 조립점이 배선을 잊었는데도 사용자에게 「잠시 뒤
        # 자동으로 다시 맞춰요」라고 약속하는 쪽이 더 위험해서, 모르면 자동 복구가 없다고
        # 말하는 fail-safe 방향을 택해요. production은 server의 단일 cleanup-owner 판정을
        # shared/deps.py에서 주입해요.
        self._retry_actor_available = (
            retry_actor_available
            if retry_actor_available is not None
            else lambda: False
        )
        # 회수 잔여 ④ 정리를 이미 확인한 크리덴셜 — `_sweep_pending_tool_binding_cleanup`
        # 의 비용을 바운드해요. 관측·쓰기 실패는 여기 들어오지 않아요.
        self._cleanup_settled: set[str] = set()
        # ── 공유 ① 수렴 재시도 좌표 (IH-160, ADR-0110) ────────────────────────
        #
        # 발급이 ④ 행을 쓴 뒤 `_reprovision_shared_policy` 가 실패하면 **그 도구는 어떤
        # agent 에도 안 보여요**(Gateway 가 `tools/list` 를 공유 ① 정책으로 필터해요). 예전에는
        # 그 실패가 로그 한 줄로 끝났고, IH-154 수렴 pass 는 prune 전용이라 빠진 열거를
        # **만들어 주지 않아서**(ADR-0107 결정 4) 다른 누군가가 `provision()` 을 부를 때까지
        # 영구히 안 보였어요. 그 재시도 좌표를 여기 둬요 — 만료 sweep 이 이어받아요.
        #
        # ⚠️ **기동 시 `True` 예요.** 좌표가 프로세스 메모리라 재기동하면 잊는데, 그 방향은
        # 위험해요(못 본 미수렴이 영구 미수렴이 돼요). 그래서 기동 직후 한 바퀴는 무조건
        # 확인해요 — `_cleanup_settled` 가 빈 집합으로 시작해 한 바퀴 다시 보는 것과 같은
        # 방향이에요. 수렴 상태면 `provision()` 이 `unchanged` 로 끝나고 쓰기는 0건이에요.
        #
        # provisioning 훅 자체가 없는 격리 테스트에서는 좌표를 켜지 않아요. local production
        # graph는 훅은 있지만 actor가 없어서 좌표는 남고 응답만 수동 수렴을 안내해요.
        self._shared_policy_convergence_pending = provision_shared_policy is not None
        self._shared_policy_convergence_retries = 0

    def issue(
        self,
        *,
        principal: str,
        blueprint_id: str,
        selected_tools: tuple[DevSelectedTool, ...],
        ttl_days: int | None = None,
        principal_groups: tuple[str, ...] = (),
        principal_email: str = "",
    ) -> IssuedDevIdentityCredential:
        """dev 크리덴셜을 발급해요.

        `principal_groups` 는 **인증된 세션의 역할**이어야 해요(`Principal.roles`). 요청 본문·
        헤더에서 오면 안 돼요 — 클라이언트가 그룹을 정하면 회원 기본 권한을 스스로 부여할 수
        있어요.

        비어 있으면 사람 단위 grant 만 봐요. 그러면 회원 기본 READ(그룹 grant)로 쓰던 도구가
        전부 "grant 없음" 으로 막혀요 — 2026-08-30 에 `admin@agora.lab` 이 그렇게 막혔어요.

        ## 부분 발급이에요 (전부 아니면 전무가 아니에요)

        천장 밖(쓰기) operation 은 **제외**하고 나머지로 발급해요. 예전에는 하나만 걸려도 전체가
        실패해서, 쓰기 도구를 하나 체크한 순간 통과할 READ 도구까지 같이 죽었어요. 제외분은
        `IssuedDevIdentityCredential.excluded` 로 돌려줘요 — 조용히 줄이지 않아요.

        단 **전부 제외되면 그것도 실패**예요. 부를 수 있는 도구가 0개인 크리덴셜은 존재할 이유가
        없고, 그걸 발급하면 화면은 성공으로 보이는데 로컬에서는 아무것도 안 돌아요.
        """
        now = int(self._now())
        groups = _clean_groups(principal_groups)
        action_grants, excluded = self._authorized_actions(
            principal, selected_tools, now=now, principal_groups=groups
        )
        actions = tuple(item.action for item in action_grants)
        # Persist nothing until the compiler's fail-closed empty-action contract passes.
        try:
            compile_agent_policy(
                AgentPolicySpec(
                    principal_id="",
                    gateway_arn=self._gateway_arn,
                    actions=actions,
                    agent_record_id=f"dev-preview-{blueprint_id}",
                    revision=1,
                )
            )
        except NoToolAccessError as exc:
            # 제외 사유를 실어 보내요 — 이유 없는 403 은 "권한이 없다" 로 읽혀서 사용자가
            # 거버넌스에서 찾을 것도 없는 화면을 헤매요.
            raise DevIdentityNoAccessError(blueprint_id, excluded) from exc
        credential_id = hashlib.sha256(
            f"{principal}:{blueprint_id}".encode()
        ).hexdigest()[:24]
        requested_days = self._ttl_days if ttl_days is None else max(1, ttl_days)
        effective_days = min(requested_days, self._ttl_max_days)
        plaintext = self._new_plaintext(credential_id)
        record = DevIdentityCredential(
            credential_id=credential_id,
            credential_hash=self._hash(plaintext),
            principal=principal,
            blueprint_id=blueprint_id,
            client_id="",
            actions=actions,
            action_grants=action_grants,
            created_at=now,
            expires_at=now + effective_days * 86_400,
            principal_groups=groups,
            principal_email=principal_email.strip(),
        )
        created = self._store.create(record)
        if not created:
            current = self._store.get(credential_id)
            if current.principal != principal:
                raise DevIdentityOwnershipError(credential_id)
            if not current.client_id:
                raise RuntimeError("dev identity provisioning is already in progress")
            record = replace(
                current,
                credential_hash=self._hash(plaintext),
                actions=actions,
                action_grants=action_grants,
                created_at=now,
                expires_at=now + effective_days * 86_400,
                revoked_at=None,
                last_used_at=None,
                # 재발급 경로도 갱신해요 — 안 하면 첫 발급의 옛 값으로 굳어요.
                principal_groups=groups,
                principal_email=principal_email.strip(),
            )
            self._store.put(record)

        client_created = False
        try:
            if not record.client_id:
                client_id = self._clients.provision(
                    principal=principal, blueprint_id=blueprint_id
                )
                client_created = True
                record = replace(record, client_id=client_id)
                self._store.put(record)

            record = self._deploy_policy(record, now=now)
            self._store.put(record)
            # Cedar 정책만으로는 로컬에서 아무 도구도 못 불러요. Gateway REQUEST interceptor 가
            # 원장 신원과 tool binding 을 요구해서, 그 두 행까지 써야 크리덴셜이 실제로 쓸 수
            # 있는 물건이 돼요. 자세한 이유는 `_write_ledger_identity` 주석에 있어요.
            self._write_ledger_identity(record, now=now)
        except Exception:
            if created:
                if client_created:
                    self._clients.delete(record.client_id)
                self._store.delete(record.credential_id)
            raise
        # 발급은 ④ 행을 **만들고**(선택한 도구) **지워요**(재발급 시 이번에 안 고른 도구를
        # `_revoke_superseded_tool_bindings` 가 `REVOKED` 로). 양쪽 다 열거를 바꾸므로
        # 재프로비저닝을 걸어요 — ADR-0104 결정 3 이 남겨 둔 갭이에요.
        #
        # 결과를 **버리지 않아요** (IH-160). 미수렴이면 고른 도구가 `tools/list` 에 아직 안
        # 보이는데, 예전에는 그 사실이 서버 로그에만 있어서 발급은 200 이고 사용자는 「도구가
        # 있다고 했는데 안 된다」를 봤어요. 수렴은 만료 sweep 이 이어받고, 그 사실을 응답에
        # 실어 사용자가 기다릴 이유를 알게 해요.
        convergence = self._reprovision_shared_policy(
            change="dev-credential-issued"
        )
        return IssuedDevIdentityCredential(
            record=record,
            credential=plaintext,
            excluded=excluded,
            shared_policy_convergence=convergence,
        )

    #: dev 크리덴셜의 원장 agent id. `dev-` 접두어로 배포된 agent 와 구분해요.
    @staticmethod
    def ledger_agent_id(credential_id: str) -> str:
        return f"dev-{credential_id}"

    def _revoke_superseded_tool_bindings(
        self, agent_id: str, record: DevIdentityCredential, stamp: str
    ) -> None:
        """이번 발급의 도구 집합에 없는 옛 binding 을 회수해요.

        ## 왜 필요한가 (2026-08-30 codex 리뷰 P1)

        `credential_id` 는 `sha256(principal:blueprint)` 라 **결정적**이에요. 같은 사람이 같은
        blueprint 로 다시 받으면 같은 `agent_id` 를 써요. 그런데 interceptor 는 `agent_id` 로
        **모든** binding 을 훑어서 `gateway_action` 이 맞는 행을 찾아요
        (`gateway_interceptor.py` `_authorize_tool`).

        그래서 도구 A 로 받고 → 도구 B 만 골라 다시 받으면, A 의 `APPROVED` 행이 남아 **A 가
        계속 호출돼요.** 재다운로드는 정상 흐름이라(도구 선택을 바꿀 때마다) 드문 경우가
        아니에요.

        지우지 않고 `desired_state=REVOKED` 로 바꿔요 — 원장은 이력을 남기는 곳이고,
        interceptor 는 `ALLOWED` 만 통과시켜요. 삭제 API 도 없어요.

        ## 제외분도 회수돼요 — 의도한 동작이에요 (2026-08-30 부분 발급)

        천장 밖 operation 은 `action_grants` 에 없으니 `keep` 에서 빠지고, 그래서 **이전 발급에서
        만들어 둔 그 도구의 binding 이 `REVOKED` 로 내려가요.** 관리자가 그 쓰기 capability 를
        승인해 놨더라도 그래요.

        일부러 이렇게 둬요. interceptor 는 Cedar 와 **별개로** binding 을 봐요
        (`gateway_interceptor.py` `_authorize_tool` — `APPROVED` + `ALLOWED` 정확히 1행).
        binding 을 살려두면 정책에서 뺀 도구가 계속 호출돼서, 천장이 정책 문서에만 있는 장식이
        돼요. 즉 이 회수가 천장에 이빨을 붙이는 부분이에요.

        대가: 같은 blueprint 이름으로 다시 내려받으면 그 사람이 **배포 경로에서** 쓰던 게 아니라
        **이 dev 크리덴셜로** 쓰던 쓰기 도구를 잃어요. 배포된 agent 는 자기 `agent_record_id` 를
        따로 쓰니 영향이 없어요(`dev-` 접두어로 갈라져요).
        """
        keep = {grant.action for grant in record.action_grants if grant.action}
        try:
            existing = self._identity.list_agent_tool_bindings(agent_id)
        except Exception as exc:  # noqa: BLE001
            # 관측 실패를 성공으로 접지 않아요 — 발급을 세워요. 여기서 넘어가면 옛 행이
            # 조용히 살아남고, 그게 정확히 이 메서드가 막으려는 상태예요.
            _log.warning(
                "dev identity 옛 tool binding 을 관측하지 못했어요; agent=%s: %s",
                agent_id,
                type(exc).__name__,
            )
            raise
        for binding in existing:
            if binding.gateway_action in keep:
                continue
            if binding.desired_state is DesiredState.REVOKED:
                continue
            self._identity.put_agent_tool_binding(
                replace(
                    binding,
                    desired_state=DesiredState.REVOKED,
                    updated_by=_DEV_BASELINE_PRINCIPAL,
                    approved_at=binding.approved_at or stamp,
                )
            )
            _log.info(
                "dev identity 옛 tool binding 회수; agent=%s action=%s",
                agent_id,
                binding.gateway_action,
            )

    def _revoke_all_tool_bindings(self, agent_id: str, stamp: str) -> list[str]:
        """이 pseudo-agent 의 살아 있는 ④ 행 **전부**를 회수해요 (IH-152 결정 2).

        ## 왜 필요한가 (2026-09-04 IH-157 라이브 사고)

        `revoke()` 와 `sweep_expired()` 는 `_deactivate_ledger_identity` 로 **신원 행(②)만**
        `REVOKED` 로 내렸어요. ④ 도구 행은 `APPROVED` + `ALLOWED` 로 **영구히** 남았어요.

        인가가 열리는 건 아니에요 — interceptor 가 신원 행을 먼저 보니 만료된 크리덴셜로는
        호출이 막혀요. 문제는 두 가지예요.

        ⑴ 열거가 거짓말해요. 죽은 크리덴셜이 한 번 승인해 둔 도구가 계속 `tools/list` 에 떠요.
        ⑵ 그 자산 record 가 나중에 purge 되면 그 APPROVED 행이 `registry_actions` 에서 빠져
           완전성 게이트가 **그 Gateway 의 모든 정책 쓰기를 거부**해요. 2026-09-04 에 실제로
           났고, 고아 37행 중 **18행이 `AGENT#dev-*` 6개** 소유였어요.

        지우지 않고 `desired_state=REVOKED` 로 내려요 — `_revoke_superseded_tool_bindings` 와
        같은 규약이고, 원장은 이력을 남기는 곳이며 삭제 API 도 없어요. 컴파일러는
        `desired_state is not ALLOWED` 를 건너뛰므로 열거와 완전성 게이트에서 함께 빠져요.
        """
        try:
            existing = self._identity.list_agent_tool_bindings(agent_id)
        except Exception as exc:  # noqa: BLE001
            # 관측 실패를 성공으로 접지 않아요. 다만 크리덴셜 회수 자체를 막지는 않아요 —
            # 신원 행이 이미 내려가 인가는 닫혀 있고, 남은 행은 다음 회수·purge 가 봐요.
            _log.warning(
                "dev identity ④ 행을 관측하지 못해 회수하지 못했어요; agent=%s: %s",
                agent_id,
                type(exc).__name__,
            )
            return []
        revoked: list[str] = []
        failed: list[str] = []
        for binding in existing:
            if binding.desired_state is DesiredState.REVOKED:
                continue
            try:
                self._identity.put_agent_tool_binding(
                    replace(
                        binding,
                        desired_state=DesiredState.REVOKED,
                        updated_by=_DEV_BASELINE_PRINCIPAL,
                        approved_at=binding.approved_at or stamp,
                    )
                )
            except Exception as exc:  # noqa: BLE001
                # 쓰기 실패도 회수를 세우지 않아요 — 만료 sweep 은 크리덴셜 여러 건을 돌고,
                # 여기서 예외를 올리면 한 건의 throttle 이 그 주기 전체를 죽여요
                # (ADR-0108 결정 5). 남은 행은 `_pending_tool_binding_cleanup` 이 다음
                # 주기에 다시 봐요.
                failed.append(binding.gateway_action)
                _log.warning(
                    "dev identity ④ 행 회수 실패; agent=%s action=%s: %s",
                    agent_id,
                    binding.gateway_action,
                    type(exc).__name__,
                )
                continue
            revoked.append(binding.gateway_action)
        if revoked or failed:
            _log.info(
                "dev identity ④ 행 회수; agent=%s revoked=%s failed=%s",
                agent_id,
                len(revoked),
                len(failed),
            )
        return revoked

    def _sweep_pending_tool_binding_cleanup(self, stamp: str) -> int:
        """이미 회수된 크리덴셜에 남은 ④ 행을 다시 내려요 (재시도 좌표 보존).

        `revoke()`·만료가 관측·쓰기 실패를 삼킨 뒤에도 그 행을 다시 볼 경로를 남기는 게
        목적이에요. 신원 행도 함께 다시 내려요 — `_deactivate_ledger_identity` 도 같은
        이유로 관측 실패를 삼켜요.

        ## 왜 `settled` 집합이 있나

        회수된 크리덴셜은 지워지지 않으니, 매 주기 전량을 훑으면 **비용이 무한히 커져요**
        (크리덴셜 1,000건이면 분당 1,000 partition Query). 그래서 「이번에 볼 게 없었다」를
        확인한 크리덴셜은 집합에 넣고 다시 안 봐요 — `server._start_poller_tasks` 의
        `_verdict_settled` 와 같은 패턴이에요.

        관측·쓰기 실패는 settle 하지 **않아요** — 그게 이 sweep 의 존재 이유예요. 프로세스가
        재기동하면 집합이 비니 한 바퀴 다시 확인해요(그쪽이 안전한 방향이에요).
        """
        try:
            records = self._store.list_all()
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "dev identity: 회수 잔여 정리 대상을 읽지 못했어요: %s",
                type(exc).__name__,
            )
            return 0
        cleaned = 0
        for record in records:
            if record.revoked_at is None:
                continue
            if record.credential_id in self._cleanup_settled:
                continue
            agent_id = self.ledger_agent_id(record.credential_id)
            try:
                live = [
                    binding
                    for binding in self._identity.list_agent_tool_bindings(agent_id)
                    if binding.desired_state is not DesiredState.REVOKED
                ]
            except Exception as exc:  # noqa: BLE001
                # 관측 실패는 settle 하지 않아요 — 다음 주기에 다시 봐요.
                _log.warning(
                    "dev identity: 회수 잔여 ④ 행을 관측하지 못했어요; agent=%s: %s",
                    agent_id, type(exc).__name__,
                )
                continue
            actions: list[str] = []
            if live:
                actions = self._revoke_all_tool_bindings(agent_id, stamp)
                cleaned += len(actions)
                _log.info(
                    "dev identity: 회수 잔여 ④ 행을 정리했어요; agent=%s actions=%s/%s",
                    agent_id, len(actions), len(live),
                )
            # ⚠️ **신원 행 재시도는 ④ 행이 남아 있는지와 «무관»해야 해요** (codex 리뷰 P2).
            # `_deactivate_ledger_identity` 도 관측 실패를 삼키므로, 「신원은 못 내렸는데 ④ 는
            # 내려간」 조합이 생겨요. 그때 ④ 만 보고 settle 하면 신원 행이 **영원히 ACTIVE** 로
            # 남아요. 그래서 ④ 와 신원을 **따로** 확인하고 둘 다 정리됐을 때만 settle 해요.
            identity_down, identity_reason = self._identity_is_revoked(agent_id)
            if not identity_down:
                self._deactivate_ledger_identity(record)
                identity_down, identity_reason = self._identity_is_revoked(agent_id)
            if identity_down and len(actions) == len(live):
                self._cleanup_settled.add(record.credential_id)
            elif identity_reason:
                _log.warning(
                    "dev identity: 회수 잔여 신원 행을 확정하지 못했어요; agent=%s: %s",
                    agent_id, identity_reason,
                )
        return cleaned

    def _identity_is_revoked(self, agent_id: str) -> tuple[bool, str]:
        """신원 행이 `REVOKED`(또는 부재)인지 관측해요. 못 읽으면 `(False, 이유)` 예요."""
        try:
            current = self._identity.get_agent_identity_binding(agent_id)
        except IdentityRecordNotFound:
            return True, ""  # 행이 없으면 내릴 것도 없어요.
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"
        return current.status is IdentityBindingStatus.REVOKED, ""

    #: 미수렴을 사람에게 알리는 **한 문장**. ARN·정책 이름·예외 원문을 담지 않아요 — 이
    #: 문장은 발급 API 응답으로 나가고 grant 만 있으면 누구나 받아요.
    _CONVERGENCE_PENDING_DETAIL = (
        "고른 도구가 아직 Gateway 도구 목록에 반영되지 않았어요. "
        "잠시 뒤 자동으로 다시 맞춰요."
    )
    #: provisioning 통로는 있지만 이 프로세스에 자동 재시도 actor가 없을 때의 문구예요.
    #: 좌표는 그대로 남겨 다음 포털 재프로비저닝이 수렴시킬 수 있게 하되, 이 백엔드가
    #: 스스로 재시도한다고 약속하지 않아요.
    _CONVERGENCE_MANUAL_DETAIL = (
        "고른 도구가 아직 Gateway 도구 목록에 반영되지 않았어요. "
        "이 백엔드는 자동으로 다시 맞추지 않아요. "
        "어드민 조작이나 포털의 다음 재프로비저닝이 맞춰요."
    )
    #: 배선이 없을 때의 문구. 위 문장을 재사용하면 **지키지 못할 약속**이 돼요 — 부를 대상이
    #: 없으니 「잠시 뒤 자동으로 다시 맞춰요」가 거짓이에요.
    _CONVERGENCE_UNOBSERVED_DETAIL = (
        "고른 도구가 Gateway 도구 목록에 반영됐는지 확인하지 못했어요."
    )

    def _reprovision_shared_policy(
        self, *, change: str, retry: bool = False
    ) -> SharedPolicyConvergence:
        """열거가 바뀌었으니 공유 ① 정책을 다시 맞춰요 (ADR-0104 결정 3).

        **실패가 이 작업을 실패시키지 않아요.** 크리덴셜 발급·회수는 이미 커밋됐고, 인가는
        신원 행(②)과 interceptor 가 닫아요 — 열거는 「보이나」축이에요.

        미수렴이면 **재시도 좌표를 남겨요** (IH-160, ADR-0110). 예전에는 로그 한 줄로 끝나서,
        아무 실 agent 도 안 쓰는 도구를 고른 크리덴셜은 그 도구가 `tools/list` 에 영구히 안
        나타났어요 — IH-154 수렴 pass 는 prune 전용이라 빠진 열거를 만들어 주지 않거든요.

        `retry` 는 **만료 sweep 의 자동 재시도**라는 표식이에요. 사용자 조작(발급·회수)이나
        원장 변경(만료)으로 들어오면 `False` 이고, 그때는 재시도 예산을 다시 채워요 — 새 변경은
        새 수렴 기회예요. 예산을 세는 것은 자동 재시도뿐이라, 실패한 사용자 조작 하나가 예산을
        먹고 그 뒤 자동 수렴을 막는 일이 없어요.
        """
        if self._provision_shared_policy is None:
            _log.warning(
                "dev identity: 공유 정책 재프로비저닝 배선이 없어 건너뛰어요; change=%s",
                change,
            )
            # 배선 부재는 「반영됐다」가 아니에요 — 관측 불가를 통과 칸에 넣지 않아요
            # (ADR-0037 §4). 재시도 좌표는 켜지 않아요: 재시도해도 부를 대상이 없어요.
            return SharedPolicyConvergence(
                converged=False,
                verdict="not_wired",
                detail=self._CONVERGENCE_UNOBSERVED_DETAIL,
            )
        if not retry:
            self._shared_policy_convergence_retries = 0
        try:
            report = self._provision_shared_policy()
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "dev identity: 공유 정책 재프로비저닝 실패; change=%s: %s: %s",
                change, type(exc).__name__, exc,
            )
            return self._convergence_pending("unknown", retry=retry)
        # `ok=True` + 비어 있지 않은 `warnings` 도 실패예요 — 저장소 규약이
        # `shared_policy_trigger.py:45` 에 그렇게 박혀 있어요. 여기서 `ok` 만 보면 같은
        # 리포트를 두 경로가 다르게 판정해요(codex 리뷰 P2).
        if (
            not isinstance(report, dict)
            or report.get("ok") is not True
            or report.get("warnings")
        ):
            _log.warning(
                "dev identity: 공유 정책 재프로비저닝이 수렴하지 않았어요; "
                "change=%s report=%s",
                change, report,
            )
            verdict = (
                str(report.get("verdict") or "unknown")
                if isinstance(report, dict)
                else "unknown"
            )
            return self._convergence_pending(verdict, retry=retry)
        _log.info(
            "dev identity: 공유 정책 재프로비저닝 change=%s verdict=%s",
            change, report.get("verdict"),
        )
        # 수렴을 **관측했을 때만** 좌표를 내려요.
        self._shared_policy_convergence_pending = False
        self._shared_policy_convergence_retries = 0
        return SharedPolicyConvergence(
            converged=True, verdict=str(report.get("verdict") or "")
        )

    def _convergence_pending(
        self, verdict: str, *, retry: bool
    ) -> SharedPolicyConvergence:
        """미수렴을 기록하고 재시도 좌표를 남겨요 (IH-160)."""
        self._shared_policy_convergence_pending = True
        if retry:
            self._shared_policy_convergence_retries += 1
        detail = (
            self._CONVERGENCE_PENDING_DETAIL
            if self._retry_actor_available()
            else self._CONVERGENCE_MANUAL_DETAIL
        )
        return SharedPolicyConvergence(
            converged=False,
            verdict=verdict,
            detail=detail,
        )

    def _write_ledger_identity(
        self, record: DevIdentityCredential, *, now: int
    ) -> None:
        """dev 크리덴셜을 **원장 신원**으로 등재해요.

        ## 왜 필요한가 (2026-08-30 실측)

        dev 크리덴셜로 Gateway 를 부르면 토큰은 받아지는데 모든 호출이
        `Request denied by Agora authorization` 이었어요. interceptor 는 `tools/call` 마다 세 가지를
        요구해요(`gateway_interceptor.py`):

            ① `X-Agora-Call` handle → delegation 행
            ② `AgentIdentityBinding(agent_id)` ACTIVE 이고 `client_id` 가 토큰의 client 와 일치
            ③ `AgentToolBinding(agent_id, action)` APPROVED + ALLOWED 정확히 1행

        dev 발급 경로는 Cedar 정책만 만들고 ②③ 을 안 만들었어요. 그래서 ZIP 에 크리덴셜이
        들어가도 **부를 수 있는 도구가 0개**였어요.

        ## 승인을 발명하지 않아요

        `approval_state` 는 배포 경로와 **같은 규칙**이에요 — Registry 가 `READ` 로 태그한
        operation 만 자동 `APPROVED` 이고, 나머지는 `PENDING` 이에요
        (`access_router._build_readonly_baseline_binding`, ADR-0018 §4·ADR-0020). 민감도를
        모르면(legacy 미분할 Target) 자동 승인하지 않아요.

        게다가 `action_grants` 자체가 이미 **사람의 grant ∩ 선택**이에요. 즉 이 행들은 그 사람이
        이미 가진 권한을 원장에 표현한 것이고, 새 권한을 만들지 않아요.

        2026-08-30 부터는 `_DOWNLOAD_SENSITIVITY_CEILING` 이 상류에서 쓰기를 걷어내므로 여기
        도달하는 grant 는 전부 READ 예요. 아래 `REQUESTED` 분기는 그래서 신규 발급에서는 안
        타지만, 천장이 느슨해지면 승인 게이트가 조용히 사라지는 걸 막는 두 번째 층으로 남겨요.
        """
        agent_id = self.ledger_agent_id(record.credential_id)
        stamp = _iso(now)
        self._revoke_superseded_tool_bindings(agent_id, record, stamp)
        self._identity.put_agent_identity_binding(
            AgentIdentityBinding(
                agent_record_id=agent_id,
                identity_type=IdentityType.OAUTH_CLIENT,
                status=IdentityBindingStatus.ACTIVE,
                client_id=record.client_id,
                policy_principal_id=record.client_id,
                verified_at=stamp,
            )
        )
        for grant in record.action_grants:
            if not (
                grant.asset_id
                and grant.asset_version
                and grant.operation_id
                and grant.gateway_target_name
            ):
                # 옛 발급본에는 좌표가 없어요 — 행을 만들지 않아요(fail-closed).
                _log.warning(
                    "dev identity tool binding 좌표가 없어 건너뛰어요; action=%s",
                    grant.action,
                )
                continue
            auto_approved = grant.sensitivity == _AUTO_APPROVED_SENSITIVITY
            self._identity.put_agent_tool_binding(
                AgentToolBinding(
                    agent_record_id=agent_id,
                    asset_id=grant.asset_id,
                    asset_version=grant.asset_version,
                    operation_id=grant.operation_id,
                    gateway_id=self._gateway_arn,
                    gateway_target_name=grant.gateway_target_name,
                    gateway_action=grant.action,
                    approval_state=(
                        ApprovalState.APPROVED
                        if auto_approved
                        # 관리자 승인 대기 상태예요 — 관리자 매트릭스 경로와 같은 값을 써요
                        # (`PENDING` 은 이 enum 에 없어요).
                        else ApprovalState.REQUESTED
                    ),
                    desired_state=DesiredState.ALLOWED,
                    # 배포 경로와 같게 항상 PENDING 이에요 — interceptor 는 이 값을 보지
                    # 않고(`approval_state`+`desired_state` 만), 실체 상태는 정책 배포기가
                    # 갱신해요(`_build_readonly_baseline_binding` 과 동형).
                    effective_state=EffectiveState.PENDING,
                    policy_revision=record.policy_revision,
                    created_by=_DEV_BASELINE_PRINCIPAL,
                    updated_by=_DEV_BASELINE_PRINCIPAL,
                    approved_by=(
                        _DEV_BASELINE_PRINCIPAL if auto_approved else None
                    ),
                    approved_at=stamp if auto_approved else None,
                    sensitivity=grant.sensitivity,
                )
            )

    def rotate(
        self, credential_id: str, *, principal: str
    ) -> IssuedDevIdentityCredential:
        record = self._owned(credential_id, principal)
        now = int(self._now())
        # 재발급도 **지금** 그룹으로 판정해요 — 얼려둔 값으로 보면 그룹에서 빠진 사람의
        # 크리덴셜을 갱신해 줘요.
        if not self._grants_still_cover(
            record, now=now, groups=self._current_groups(record)
        ):
            raise NoToolAccessError(credential_id)
        plaintext = self._new_plaintext(credential_id)
        rotated = replace(
            record,
            credential_hash=self._hash(plaintext),
            created_at=now,
            expires_at=now + min(self._ttl_days, self._ttl_max_days) * 86_400,
            revoked_at=None,
            last_used_at=None,
        )
        rotated = self._deploy_policy(rotated, now=now)
        self._store.put(rotated)
        return IssuedDevIdentityCredential(record=rotated, credential=plaintext)

    def revoke(self, credential_id: str, *, principal: str) -> DevIdentityCredential:
        record = self._owned(credential_id, principal)
        if record.revoked_at is not None:
            return record
        self._delete_policy(record)
        # 원장 신원도 함께 내려요. handle 발급이 이미 막히니 이게 유일한 방어선은 아니지만,
        # 살아 있는 ACTIVE binding 을 남기면 원장이 실체와 어긋나 보여요.
        self._deactivate_ledger_identity(record)
        # ④ 도구 행도 같은 작업에서 내려요 (IH-152 결정 2). 신원 행만 내리면 그 행들이
        # `APPROVED`+`ALLOWED` 로 영구히 남아, 나중에 그 자산 record 가 purge 될 때
        # 완전성 게이트가 그 Gateway 의 정책 쓰기를 전부 막아요.
        actions = self._revoke_all_tool_bindings(
            self.ledger_agent_id(record.credential_id), _iso(int(self._now()))
        )
        revoked = replace(record, revoked_at=int(self._now()))
        self._store.put(revoked)
        if actions:
            self._reprovision_shared_policy(change="dev-credential-revoked")
        return revoked

    def _deactivate_ledger_identity(self, record: DevIdentityCredential) -> None:
        agent_id = self.ledger_agent_id(record.credential_id)
        try:
            current = self._identity.get_agent_identity_binding(agent_id)
        except IdentityRecordNotFound:
            return
        except Exception as exc:  # noqa: BLE001 - 회수 자체를 막지 않아요.
            _log.warning(
                "dev identity 원장 신원을 관측하지 못해 내리지 못했어요; agent=%s: %s",
                agent_id,
                type(exc).__name__,
            )
            return
        self._identity.put_agent_identity_binding(
            replace(current, status=IdentityBindingStatus.REVOKED)
        )

    #: dev 호출 handle 의 TTL. `DelegationService` 가 허용하는 **최소값**이에요.
    #:
    #: 이 경로는 handle 을 사용 후 회수하지 않아요 — 회수는 호출한 쪽이 해야 하는데, 로컬에서
    #: 도는 생성 코드가 그걸 성실히 할 거라고 믿을 수 없어요(playground 는 서버가 호출을
    #: 감싸니 회수할 수 있어요, `playground/router.py`). 그래서 회수 대신 **창을 최소로** 줄여요.
    #: 2026-08-30 실측: 900초로 두면 같은 handle 이 두 번째 호출에서도 통과했어요.
    _CALL_HANDLE_TTL_SECONDS = 60

    def call_handle(self, plaintext: str, *, ttl_seconds: int | None = None):
        """dev 크리덴셜을 **호출 handle 한 개**로 바꿔요.

        로컬에서 실행하는 생성 코드가 매 MCP 호출 전에 불러요. 크리덴셜 자체를 Gateway 에
        보내지 않고, Agora 가 그 사람 신원으로 delegation 행을 만들어 handle 만 내줘요.

        ## 왜 크리덴셜이 아니라 handle 인가

        interceptor 는 handle 로만 사람을 알아봐요(ADR-0091). 그리고 handle 은 **호출당
        발급·짧은 TTL** 이라, 크리덴셜을 길게 들고 있어도 권한 판정은 매번 새로 일어나요
        (ADR-0095 §13 — 권한 등급을 토큰에 담지 않아요).

        여기서 **사용 후 회수는 하지 않아요** — `_CALL_HANDLE_TTL_SECONDS` 주석 참고.

        검증은 `token()` 과 **같은 순서**예요 — 해시·회수·만료·grant coverage. 한쪽만 느슨하면
        토큰은 막히는데 handle 은 나오는 비대칭이 생겨요.
        """
        try:
            credential_id = self._credential_id(plaintext)
            record = self._store.get(credential_id)
            now = int(self._now())
            # 한 번만 조회해서 coverage 판정과 handle 내용에 **같은 값**을 써요. 두 번 읽으면
            # 그 사이 그룹이 바뀌어 판정과 실린 값이 어긋날 수 있어요.
            groups = self._current_groups(record)
            if (
                not hmac.compare_digest(
                    record.credential_hash, self._hash(plaintext)
                )
                or record.revoked_at is not None
                or record.expires_at <= now
                or not self._grants_still_cover(record, now=now, groups=groups)
            ):
                raise DevIdentityAuthenticationError()
            handle, context = self._delegation.issue(
                principal_id=record.principal,
                agent_id=self.ledger_agent_id(credential_id),
                workload_id="",
                allowed_asset_ids=tuple(
                    dict.fromkeys(
                        grant.asset_id
                        for grant in record.action_grants
                        if grant.asset_id
                    )
                ),
                # 위에서 관측한 **현재** 그룹이에요 — `_current_groups` 참고.
                principal_groups=groups,
                # email 은 얼려둔 값을 써요. 신원 표시용이고 인가 판정에 쓰이지 않아서
                # (MCP 가 `agora_user_id` 로 받는 값이에요) 고착돼도 권한이 안 넓어져요.
                principal_email=record.principal_email,
                ttl_seconds=ttl_seconds or self._CALL_HANDLE_TTL_SECONDS,
            )
            self._store.touch_last_used(credential_id, last_used_at=now)
            return {
                "delegation_handle": handle,
                "invocation_id": context.invocation_id,
                "expires_at": context.expires_at,
            }
        except (DevIdentityAuthenticationError, DevIdentityNotFound) as exc:
            raise DevIdentityAuthenticationError() from exc

    def list(self, *, principal: str) -> list[DevIdentityCredential]:
        return self._store.list_for_principal(principal)

    def token(self, plaintext: str) -> dict:
        try:
            credential_id = self._credential_id(plaintext)
            record = self._store.get(credential_id)
            valid_hash = hmac.compare_digest(
                record.credential_hash, self._hash(plaintext)
            )
            now = int(self._now())
            if (
                not valid_hash
                or record.revoked_at is not None
                or record.expires_at <= now
                or not self._grants_still_cover(
                    record, now=now, groups=self._current_groups(record)
                )
            ):
                raise DevIdentityAuthenticationError()
            token = self._tokens.issue(record.client_id)
            self._store.touch_last_used(
                record.credential_id, last_used_at=now
            )
            return token
        except (DevIdentityAuthenticationError, DevIdentityNotFound) as exc:
            raise DevIdentityAuthenticationError() from exc
        except DevIdentityInfrastructureError:
            raise
        except Exception as exc:
            raise DevIdentityInfrastructureError(
                "dev token broker dependency failed"
            ) from exc

    def reconcile_pending_policies(self, *, limit: int = 20) -> dict[str, int]:
        """dev identity Cedar policy 의 활성화를 관측해 원장과 **유효 권한**을 맞춰요.

        두 가지를 해요.

        1. **승격** — PENDING 으로 남은 최신 revision 을 관측해 ACTIVE·FAILED 로 확정해요.
           IH-78: 코드 다운로드는 Cedar 활성화를 기다리지 않아요(실측 8.5~72.6초, 증가 추세).
           그래서 발급 직후 원장은 PENDING 이고, 실제 활성화를 **관측해서** 올리는 주체가
           필요해요. 없으면 원장이 영구히 PENDING 이라 거짓말을 하게 돼요
           (ADR-0037 §4 — 관측 불가는 통과가 아니고, 상태는 관측한 것만 적어요).

        2. **옛 revision 회수** — 최신 revision 이 ACTIVE 인 걸 관측한 뒤, 그보다 낮은 소유
           revision 의 원격 정책을 지워요. **Cedar ACTIVE permit 은 합집합**이라 도구를 줄여
           r2 를 올려도 r1 이 살아 있으면 r1 이 계속 허용해요 — 회수가 무효예요. 2026-08-31
           라이브에서 `Agent_dev_aedd9c5fdaf5a2cc08e8193c_5206ac8c` 의 r1·r2 가 **둘 다
           ACTIVE** 인 걸 실측했어요.

           발급 경로(`_deploy_policy`)는 `wait_polls=1` 로 PENDING 을 받고 즉시 돌아오니
           `AgentPolicyDeployer.deploy` 안의 정리 지점을 **한 번도 지나가지 않아요.** ACTIVE 를
           관측하는 주체가 여기뿐이라 회수도 여기 있어야 해요.

        ## 더 이상 "비파괴" 가 아니에요

        예전 주석은 이 루프를 상태만 읽는 비파괴 작업이라 적었고, 그게 만료 sweeper(파괴적,
        IH-26 정렬 가드) 와 달리 항상 도는 근거였어요. 이제 superseded 된 옛 revision 정책을
        **지워요.** 그래도 항상 돌아야 해요 — 권한 회수가 실제로 반영되는지가 옵션 스위치에
        걸리면 안 되니까요. 삭제 대상은 "원장이 policyId 로 소유를 확인한, 최신보다 낮은
        revision" 하나로 좁혀져 있고 삭제가 멱등이라(`ResourceNotFoundException` 은 no-op)
        두 프로세스가 같이 돌아도 결과가 같아요.

        ## 회수 실패를 FAILED 로 적지 않는 이유

        원격 최신 revision 은 실제로 ACTIVE 예요. 그걸 FAILED 로 적으면
        `AgentPolicyDeployer._canonical_deployment` 가 "ACTIVE 중 최대 revision" 인 **옛
        revision** 을 canonical 로 골라, `reconcile`·`prune_stale` 이 살아남은 넓은 옛 정책을
        in_sync 로, 좁은 새 정책을 stale 로 뒤집어 봐요 — 정리가 **좁은 쪽을** 지워요. 그래서
        상태는 관측한 대로 ACTIVE 로 두고, 실패는 `validation_findings`(화면 노출)·
        `reclaim_failed` 카운트·error 로그로 드러내요. 다음 주기가 2번 경로로 재시도해요.
        """
        counts = {
            "checked": 0,
            "activated": 0,
            "failed": 0,
            "pending": 0,
            "reclaimed": 0,
            "reclaim_failed": 0,
        }
        for record in self._store.list_all():
            if counts["checked"] >= limit:
                break
            if record.revoked_at is not None or not record.policy_revision:
                continue
            agent_key = f"dev-{record.credential_id}"
            deployments = self._identity.list_agent_policy_deployments(
                agent_key
            )
            if not deployments:
                continue
            deployment = max(deployments, key=lambda item: item.revision)
            # 소유가 원장 policyId 로 확인되고 아직 SUPERSEDED 로 닫히지 않은 하위 revision.
            # 없으면 회수할 게 없으니 `list_policies` 를 부르지 않아요(첫 발급이 대부분).
            has_superseded = any(
                item.revision < deployment.revision
                and item.agentcore_policy_id
                and item.status is not PolicyDeploymentStatus.SUPERSEDED
                for item in deployments
            )
            if deployment.status is PolicyDeploymentStatus.ACTIVE:
                # 승격은 끝났는데 옛 revision 이 남아 있어요 — 회수 재시도 경로예요.
                if not has_superseded:
                    continue
                counts["checked"] += 1
                self._record_dev_revision_reclaim(
                    agent_key,
                    deployment,
                    policy_id=deployment.agentcore_policy_id,
                    deployed_policy_hash=deployment.deployed_policy_hash,
                    deployed_at=deployment.deployed_at,
                    credential_id=record.credential_id,
                    counts=counts,
                    deployments=tuple(deployments),
                )
                continue
            if deployment.status is not PolicyDeploymentStatus.PENDING:
                continue
            counts["checked"] += 1
            try:
                observed = self._policies.observe(deployment)
            except Exception as exc:  # noqa: BLE001 - 한 건 실패가 순회를 막지 않아요.
                _log.warning(
                    "dev identity policy 상태를 관측하지 못했어요; credential=%s: %s: %s",
                    record.credential_id,
                    type(exc).__name__,
                    exc,
                )
                continue
            if observed.status is PolicyDeploymentStatus.ACTIVE:
                # 새 revision 이 **ACTIVE 로 관측된 뒤에만** 옛 것을 지워요. 순서를 뒤집으면
                # 새 정책이 실패했을 때 이 크리덴셜이 권한 0 이 되는 창이 열려요.
                self._record_dev_revision_reclaim(
                    agent_key,
                    deployment,
                    policy_id=(
                        observed.agentcore_policy_id
                        or deployment.agentcore_policy_id
                    ),
                    deployed_policy_hash=observed.deployed_policy_hash,
                    deployed_at=observed.deployed_at,
                    credential_id=record.credential_id,
                    counts=counts,
                    deployments=tuple(deployments),
                    reclaim=has_superseded,
                )
                counts["activated"] += 1
            elif observed.status is PolicyDeploymentStatus.FAILED:
                self._identity.mark_agent_policy_deployment(
                    agent_key,
                    deployment.revision,
                    status=PolicyDeploymentStatus.FAILED,
                    agentcore_policy_id=observed.agentcore_policy_id,
                    validation_findings=observed.validation_findings,
                )
                counts["failed"] += 1
                _log.error(
                    "dev identity policy 배포가 확정 실패했어요; credential=%s findings=%s",
                    record.credential_id,
                    " / ".join(observed.validation_findings) or "(원인 미기록)",
                )
            else:
                counts["pending"] += 1
        return counts

    def _record_dev_revision_reclaim(
        self,
        agent_key: str,
        deployment: AgentPolicyDeployment,
        *,
        policy_id: str,
        deployed_policy_hash: str,
        deployed_at: str,
        credential_id: str,
        counts: dict[str, int],
        deployments: tuple[AgentPolicyDeployment, ...] = (),
        reclaim: bool = True,
    ) -> None:
        """ACTIVE 인 revision 을 원장에 확정하고 옛 revision permit 을 회수해요.

        회수 성공이면 findings 를 비워요 — ACTIVE 로 확정된 revision 에는 다른 findings 가
        없어요(`AgentPolicyDeployer.deploy`·`observe` 의 ACTIVE 경로가 `()` 로 적어요). 그래서
        빈 tuple 로 덮어써도 잃는 정보가 없고, 지난 주기의 회수 실패 문장이 화면에 남지 않아요.
        """
        errors: tuple[str, ...] = ()
        if reclaim:
            try:
                errors = self._policies.reclaim_superseded_revisions(
                    agent_key,
                    current_revision=deployment.revision,
                    gateway_arn=self._gateway_arn,
                    keep_policy_id=policy_id,
                )
            except Exception as exc:  # noqa: BLE001 - 회수 미관측은 통과가 아니에요.
                errors = (
                    "revision reclaim unobservable: "
                    f"{type(exc).__name__}: {exc}",
                )
        findings: tuple[str, ...] = ()
        if errors:
            findings = tuple(dict.fromkeys((
                STALE_REVISION_CLEANUP_FAILED,
                *errors,
            )))
            counts["reclaim_failed"] += 1
            _log.error(
                "dev identity 옛 revision 회수 실패; credential=%s r%s: %s",
                credential_id,
                deployment.revision,
                "; ".join(errors),
            )
        elif reclaim:
            counts["reclaimed"] += 1
            # 회수가 성공했다는 건 "현재 미만의 소유 정책이 live 에 하나도 없다" 는 뜻이에요 —
            # 목록이 불완전하거나 삭제·부재확인이 실패하면 errors 가 와요. 그런데
            # `_delete_prior_owned_revisions` 는 **자기가 지운** revision 만 원장에 닫아요.
            # 밖에서 먼저 지워진 revision(수동 삭제·`prune_stale`·teardown)은 ACTIVE 로 남고,
            # 그러면 다음 주기도 같은 판정을 해서 회수 루프가 영원히 `list_policies` 를 불러요.
            # 그래서 남은 하위 revision 도 여기서 닫아요.
            for item in deployments:
                if (
                    item.revision < deployment.revision
                    and item.status is not PolicyDeploymentStatus.SUPERSEDED
                ):
                    self._identity.mark_agent_policy_deployment(
                        agent_key,
                        item.revision,
                        status=PolicyDeploymentStatus.SUPERSEDED,
                    )
        self._identity.mark_agent_policy_deployment(
            agent_key,
            deployment.revision,
            status=PolicyDeploymentStatus.ACTIVE,
            agentcore_policy_id=policy_id,
            deployed_policy_hash=deployed_policy_hash,
            deployed_at=deployed_at,
            validation_findings=findings,
        )

    def sweep_expired(self) -> int:
        now = int(self._now())
        stamp = _iso(now)
        expired = self._store.list_expired(now)
        revoked_actions = 0
        for record in expired:
            self._delete_policy(record)
            # 만료도 회수와 같게 원장 신원을 내려요 — 안 내리면 `revoke()` 만 정리하고
            # 자연 만료된 크리덴셜의 binding 이 ACTIVE 로 남아요(codex 리뷰 P1).
            self._deactivate_ledger_identity(record)
            # ④ 도구 행도 같이 내려요 (IH-152 결정 2) — `revoke()` 와 같은 이유예요.
            revoked_actions += len(self._revoke_all_tool_bindings(
                self.ledger_agent_id(record.credential_id), stamp
            ))
            self._store.put(replace(record, revoked_at=now))
        # ⚠️ **회수·만료 때 ④ 를 못 지웠으면 그 좌표를 잃지 않아요.**
        #
        # `revoke()` 와 위 루프는 관측·쓰기 실패를 삼키고 크리덴셜을 `revoked_at` 으로
        # 확정해요(그게 보안 동작이에요 — 세우면 크리덴셜이 아예 안 내려가요). 그런데 그
        # 순간 `list_expired` 는 그 크리덴셜을 더 이상 돌려주지 않으니, 남은
        # `APPROVED`+`ALLOWED` ④ 행을 다시 볼 경로가 사라져요 — AGENTS.md 의 「정리를
        # 관측할 수 없으면 성공을 기록하지 말고 재시도 좌표를 지켜라」에 어긋나요
        # (codex 리뷰 P1).
        #
        # 그래서 **이미 회수된 크리덴셜 중 ④ 행이 아직 살아 있는 것**을 매 주기 다시 봐요.
        # 정상 상태에서는 전부 `REVOKED` 라 쓰기가 0건이에요.
        revoked_actions += self._sweep_pending_tool_binding_cleanup(stamp)
        if revoked_actions:
            # 크리덴셜 여러 건을 한 번에 쓸어도 재프로비저닝은 **한 번**이에요 —
            # 정책은 gateway 전체 자원이라 건당 부를 이유가 없어요.
            #
            # 이 호출이 미수렴 재시도까지 겸해요(`retry=False` — 실제 원장 변경이라 예산을
            # 다시 채우는 게 맞아요). 그래서 아래 재시도 분기와 `elif` 로 묶어 한 주기에
            # `provision()` 을 **두 번** 부르지 않아요.
            self._reprovision_shared_policy(change="dev-credential-expired")
        elif self._shared_policy_convergence_pending:
            # ⚠️ **이 pass 는 정책을 «만들어요».** ADR-0107 결정 4 가 prune 전용으로 설계한
            # `shared_policy_reclaimer` 와 다른 pass 예요 — 거기는 「빠진 열거」를 채울 수
            # 없어서 IH-160 을 닫지 못해요. 대신 여기는 ADR-0108 이 이미 만든 무인 create
            # 호출자(위 `dev-credential-expired`)와 **같은 통로**라 무인 create 주체 수를
            # 늘리지 않아요. 동시 어드민 승인과의 `ConflictException` 경합은 그대로 열려
            # 있어요(`docs/06-risks.md` 09-05 「무인 create 경합」) — 한쪽이 실패하고 옛
            # 리비전은 무사하며, 이제 그 실패에 재시도 좌표가 있어요.
            if (
                self._shared_policy_convergence_retries
                < _CONVERGENCE_MAX_RETRIES
            ):
                self._reprovision_shared_policy(
                    change="dev-credential-convergence-retry", retry=True
                )
            else:
                # 예산 소진을 **조용히** 넘기지 않아요. 멈추는 이유는
                # `_CONVERGENCE_MAX_RETRIES` 주석에 있어요(실패 리비전 누적 → engine 정책
                # 상한). 다음 원장 변경이 `provision()` 을 다시 걸어요.
                _log.warning(
                    "dev identity: 공유 정책 수렴 자동 재시도 예산(%s회)을 소진했어요 — "
                    "다음 원장 변경이 다시 시도해요",
                    _CONVERGENCE_MAX_RETRIES,
                )
        credential_ids = {
            record.credential_id for record in self._store.list_all()
        }
        orphaned = self._policies.prune_orphan_dev_policies(
            existing_credential_ids=credential_ids,
            gateway_arn=self._gateway_arn,
        )
        return len(expired) + len(orphaned)

    def _authorized_actions(
        self,
        principal: str,
        selected_tools: tuple[DevSelectedTool, ...],
        *,
        now: int,
        principal_groups: tuple[str, ...] = (),
    ) -> tuple[tuple[DevActionGrant, ...], tuple[DevAuthorizationFailure, ...]]:
        """`(발급할 grant, 제외한 사유)` 를 돌려줘요.

        두 결과를 구분하는 축은 하나예요 — **치명이냐 제외냐.**

        * 치명(`failures` → `DevIdentityAuthorizationError`): 권한 원장이 이 operation 을
          거부하거나, 판정 자체를 못 했어요. 사용자가 거버넌스에서 고칠 수 있는 상태예요.
        * 제외(`excluded`): 원장은 허용하는데 **로컬 다운로드 천장** 밖이에요. 고칠 게 없어요 —
          배포 경로로 쓰면 되니 발급을 막지 않고 빼고 진행해요.
        """
        granted: dict[str, set[str]] = {}
        grants_unobservable = False
        try:
            # 사람 grant + **그룹 grant** 합집합 — Gateway interceptor 5단 ⑤ 와 같은 규칙이에요
            # (`gateway_interceptor.py`). 여기만 사람 축으로 좁히면 interceptor 는 통과시키는
            # 도구를 발급이 거부해서 두 화면이 서로 다른 말을 해요.
            principal_grants = self._identity.list_grants(
                principal_id=principal,
                subject_groups=principal_groups,
            )
        except Exception as exc:  # noqa: BLE001 - unknown must remain distinct.
            grants_unobservable = True
            principal_grants = ()
            _log.warning(
                "dev identity principal grant를 관측하지 못했어요; "
                "principal=%s: %s",
                principal,
                type(exc).__name__,
            )
        for grant in principal_grants:
            if (
                grant.status is GrantStatus.ACTIVE
                and (grant.expires_at is None or grant.expires_at > now)
            ):
                granted.setdefault(grant.connection_id, set()).update(
                    grant.capabilities
                )

        result: dict[str, DevActionGrant] = {}
        failures: list[DevAuthorizationFailure] = []
        excluded: dict[tuple[str, str], DevAuthorizationFailure] = {}

        for tool in selected_tools:
            strict_selection = bool(tool.operations)
            legacy_failures: list[DevAuthorizationFailure] = []
            tool_authorized = False

            def exclude(
                operation_id: str,
                reason: DevAuthorizationReason,
            ) -> None:
                """발급을 막지 않고 이 operation 만 빼요 — 사유는 남겨요.

                legacy 경로에서도 `excluded` 로 가요(`legacy_failures` 아님). legacy 는
                "이 도구에서 아무것도 못 얻으면 이유를 알려준다" 는 규칙인데, 제외는 이유가
                이미 확정이라 승격 판정을 기다릴 필요가 없어요.
                """
                excluded[(tool.asset_id, operation_id)] = DevAuthorizationFailure(
                    asset_id=tool.asset_id,
                    operation_id=operation_id,
                    state=DevAuthorizationState.BLOCKED,
                    reason=reason,
                )

            def fail(
                operation_id: str,
                reason: DevAuthorizationReason,
                *,
                state: DevAuthorizationState = DevAuthorizationState.BLOCKED,
                connection_id: str = "",
                missing_capabilities: tuple[str, ...] = (),
            ) -> None:
                failure = DevAuthorizationFailure(
                    asset_id=tool.asset_id,
                    operation_id=operation_id,
                    state=state,
                    reason=reason,
                    connection_id=connection_id,
                    missing_capabilities=missing_capabilities,
                )
                if strict_selection or state is DevAuthorizationState.UNKNOWN:
                    failures.append(failure)
                else:
                    legacy_failures.append(failure)

            try:
                context = self._asset_context_resolver(
                    tool.asset_id,
                    tool.asset_version,
                )
            except McpGatewayTargetError as exc:
                _log.warning(
                    "dev identity Gateway target을 관측하지 못했어요; "
                    "asset=%s: %s",
                    tool.asset_id,
                    type(exc).__name__,
                )
                for operation_id in tool.operations or ("",):
                    fail(
                        operation_id,
                        DevAuthorizationReason.GATEWAY_TARGET_UNOBSERVABLE,
                        state=DevAuthorizationState.UNKNOWN,
                    )
                continue
            except Exception as exc:  # noqa: BLE001 - reported as unknown.
                _log.warning(
                    "dev identity MCP 자산 신원을 관측하지 못했어요; "
                    "asset=%s: %s",
                    tool.asset_id,
                    type(exc).__name__,
                )
                for operation_id in tool.operations or ("",):
                    fail(
                        operation_id,
                        DevAuthorizationReason.ASSET_BINDING_UNOBSERVABLE,
                        state=DevAuthorizationState.UNKNOWN,
                    )
                continue

            if context is None:
                for operation_id in tool.operations or ("",):
                    fail(
                        operation_id,
                        DevAuthorizationReason.ASSET_BINDING_INVALID,
                    )
                failures.extend(legacy_failures)
                continue
            if not context.targets:
                for operation_id in tool.operations or ("",):
                    fail(
                        operation_id,
                        DevAuthorizationReason.GATEWAY_TARGET_UNRESOLVED,
                    )
                failures.extend(legacy_failures)
                continue
            try:
                current_capabilities = tuple(
                    self._identity.list_asset_capabilities(tool.asset_id)
                )
            except Exception as exc:  # noqa: BLE001 - reported as unknown.
                _log.warning(
                    "dev identity target 자산 capability를 관측하지 못했어요; "
                    "asset=%s: %s",
                    tool.asset_id,
                    type(exc).__name__,
                )
                for operation_id in tool.operations or ("",):
                    fail(
                        operation_id,
                        DevAuthorizationReason.ASSET_CAPABILITY_UNOBSERVABLE,
                        state=DevAuthorizationState.UNKNOWN,
                    )
                continue

            by_operation = {
                capability.operation_id: capability
                for capability in current_capabilities
                if capability.asset_version == tool.asset_version
            }
            operation_ids = (
                tuple(dict.fromkeys(tool.operations))
                if strict_selection
                else tuple(sorted(by_operation)) or ("",)
            )

            for operation_id in operation_ids:
                # ── 로컬 다운로드 천장을 **가장 먼저** 봐요 ────────────────────────
                #
                # 순서가 중요해요. 지금 쓰기 operation 은 `AssetCapability` 행이 없어서
                # 아래 `ASSET_CAPABILITY_MISSING`(치명) 으로 떨어져요 — 그게 "쓰기 하나
                # 체크했더니 전체 다운로드가 죽는" 현상의 실제 경로였어요. 천장을 뒤에 두면
                # 그 치명이 먼저 나서 부분 발급이 성립하지 않아요.
                #
                # 승인 상태도 보지 않아요(`_DOWNLOAD_SENSITIVITY_CEILING` 주석 참고).
                #
                # `operation_id` 가 빈 문자열인 경우는 legacy 센티널이에요(capability 행이
                # 하나도 없을 때만 나와요). 그 경로는 어차피 바로 아래에서
                # `ASSET_CAPABILITY_MISSING` 으로 걸리니 여기서 "민감도 모름" 으로 덮지
                # 않아요 — 사용자에게 더 정확한 원인을 남기려고요.
                #
                # 대문자로 정규화해서 **아래 grant 에도 그 값을 저장**해요. 원본을 저장하면
                # 천장은 `read` 를 통과시키는데 `_write_ledger_identity` 의 자동 승인 비교는
                # 정확히 `"READ"` 라 실패해서, 정책엔 있는데 binding 은 REQUESTED 인 어긋난
                # 상태가 나와요. 실제 Registry 값은 이미 정규화돼 있어요
                # (`shared/gateway_tools.py` `normalized_sensitivity`) — 여기는 이중 안전장치예요.
                sensitivity = (
                    context.target_sensitivity(operation_id).strip().upper()
                    if operation_id
                    else ""
                )
                if operation_id:
                    if not sensitivity:
                        # 민감도를 모르면 천장 판정을 못 해요 → 제외가 아니라 **치명**이에요.
                        # 제외로 처리하면 "모르는 도구는 알아서 빠졌다" 는 통과 기록이 남아요.
                        fail(
                            operation_id,
                            DevAuthorizationReason.OPERATION_SENSITIVITY_UNKNOWN,
                            state=DevAuthorizationState.UNKNOWN,
                        )
                        continue
                    if sensitivity != _DOWNLOAD_SENSITIVITY_CEILING:
                        exclude(
                            operation_id,
                            DevAuthorizationReason.OPERATION_SENSITIVITY_NOT_READ,
                        )
                        continue

                capability = by_operation.get(operation_id)
                if capability is None:
                    fail(
                        operation_id,
                        DevAuthorizationReason.ASSET_CAPABILITY_MISSING,
                    )
                    continue

                if capability.status is not AssetCapabilityStatus.APPROVED:
                    fail(
                        operation_id,
                        DevAuthorizationReason.ASSET_CAPABILITY_NOT_APPROVED,
                    )
                    continue

                target_name = context.target_name(operation_id)
                if not target_name:
                    fail(
                        operation_id,
                        DevAuthorizationReason.GATEWAY_TARGET_UNRESOLVED,
                    )
                    continue

                connection_id = capability.connection_id

                required = set(capability.required_capabilities)
                if not required:
                    fail(
                        operation_id,
                        DevAuthorizationReason.REQUIRED_CAPABILITIES_MISSING,
                        connection_id=connection_id,
                    )
                    continue

                try:
                    connection = self._identity.get_connection(
                        connection_id
                    )
                except IdentityRecordNotFound:
                    fail(
                        operation_id,
                        DevAuthorizationReason.CONNECTION_MISSING,
                        connection_id=connection_id,
                    )
                    continue
                except Exception as exc:  # noqa: BLE001 - reported as unknown.
                    _log.warning(
                        "dev identity connection을 관측하지 못했어요; "
                        "connection=%s: %s",
                        connection_id,
                        type(exc).__name__,
                    )
                    fail(
                        operation_id,
                        DevAuthorizationReason.CONNECTION_UNOBSERVABLE,
                        state=DevAuthorizationState.UNKNOWN,
                        connection_id=connection_id,
                    )
                    continue

                if connection.status is not ConnectionStatus.ACTIVE:
                    fail(
                        operation_id,
                        DevAuthorizationReason.CONNECTION_INACTIVE,
                        connection_id=connection_id,
                    )
                    continue

                try:
                    active_capabilities = {
                        item.name
                        for item in (
                            self._identity.list_connection_capabilities(
                                connection_id
                            )
                        )
                        if item.status is CapabilityStatus.ACTIVE
                    }
                except Exception as exc:  # noqa: BLE001 - reported as unknown.
                    _log.warning(
                        "dev identity connection capability를 관측하지 "
                        "못했어요; connection=%s: %s",
                        connection_id,
                        type(exc).__name__,
                    )
                    fail(
                        operation_id,
                        DevAuthorizationReason.CONNECTION_CAPABILITIES_UNOBSERVABLE,
                        state=DevAuthorizationState.UNKNOWN,
                        connection_id=connection_id,
                    )
                    continue

                inactive = required - active_capabilities
                if inactive:
                    fail(
                        operation_id,
                        DevAuthorizationReason.CONNECTION_CAPABILITY_INACTIVE,
                        connection_id=connection_id,
                        missing_capabilities=tuple(sorted(inactive)),
                    )
                    continue

                outside_ceiling = required - set(connection.ceiling)
                if outside_ceiling:
                    fail(
                        operation_id,
                        DevAuthorizationReason.CONNECTION_CEILING_EXCEEDED,
                        connection_id=connection_id,
                        missing_capabilities=tuple(sorted(outside_ceiling)),
                    )
                    continue

                if grants_unobservable:
                    fail(
                        operation_id,
                        DevAuthorizationReason.PRINCIPAL_GRANTS_UNOBSERVABLE,
                        state=DevAuthorizationState.UNKNOWN,
                        connection_id=connection_id,
                    )
                    continue

                # ⑦ 을 **도구 단위로** 봐요 (ADR-0099 결정 2·10).
                #
                # 옛 코드는 `required ⊆ granted[connection_id]` 라벨 부분집합이었어요. 새 grant
                # 는 `capabilities=()` 라 그 식이 **모든 도구를 거부**해요 — 방향은 안전하지만
                # 사유가 「capability 가 없어요」로 거짓말하고 ZIP 이 통째로 비어요.
                #
                # 판정은 interceptor 와 **같은 방법**이에요: `(주체, asset_id, operation_id)`
                # 정확한 키로 사람 축 1회 + 그룹당 1회. 그래서 ZIP 내용과 런타임 판정이 같은
                # 행을 봐요 — 결정 10 이 없애려던 「두 기준이 어긋남」이 사라져요.
                if not self._has_tool_grant(
                    asset_id=tool.asset_id,
                    operation_id=operation_id,
                    principal=principal,
                    groups=principal_groups,
                    now=now,
                ):
                    fail(
                        operation_id,
                        DevAuthorizationReason.PRINCIPAL_GRANT_MISSING,
                        connection_id=connection_id,
                        missing_capabilities=tuple(sorted(required)),
                    )
                    continue

                action = gateway_tool_name(target_name, operation_id)
                result[action] = DevActionGrant(
                    action=action,
                    connection_id=connection_id,
                    required_capabilities=tuple(sorted(required)),
                    # 원장 tool binding 을 쓰기 위한 좌표. 민감도는 **capability 행의 값**을
                    # 써요 — 이름에서 추론하면 애매한 도구가 조용히 READ 로 분류돼요.
                    asset_id=tool.asset_id,
                    asset_version=tool.asset_version,
                    operation_id=operation_id,
                    gateway_target_name=target_name,
                    # 민감도는 **Registry 가 명시한 Target 태그**에서 와요
                    # (`AssetCapability` 에는 이 필드가 없어요). 위 천장 게이트를 지났으니
                    # 여기 오는 값은 항상 READ 예요 — 빈 값·쓰기는 이미 갈라졌어요.
                    sensitivity=sensitivity,
                )
                tool_authorized = True

            if not tool_authorized:
                failures.extend(legacy_failures)

        if failures:
            raise DevIdentityAuthorizationError(tuple(failures))
        return (
            tuple(result[action] for action in sorted(result)),
            tuple(excluded[key] for key in sorted(excluded)),
        )

    def _current_groups(self, record: DevIdentityCredential) -> tuple[str, ...]:
        """이 사람이 **지금** 속한 그룹. 얼려둔 값을 쓰지 않아요.

        ## 왜 다시 읽나 (2026-08-30 codex 리뷰 P0)

        회원 기본 READ 는 그룹에 달려 있어서(`catalog_read_access.py`) 그룹 이름이 그대로
        인가 입력이에요. 발급 시점 값을 그대로 쓰면 **사람을 그룹에서 빼도 크리덴셜 수명
        동안 접근이 유지**돼요. Gateway interceptor 는 외부 조회가 금지돼서(ADR-0091) 스스로
        고칠 수 없어요 — handle 을 만드는 이 지점이 그걸 바로잡을 수 있는 유일한 곳이에요.

        ## 실패하면 막아요

        조회가 안 되면 `DevIdentityAuthenticationError` 를 던져요. 얼려둔 값으로 넘어가면
        위 구멍이 그대로 열리고, 빈 tuple 로 넘어가면 그룹 grant 로 쓰던 도구가 조용히
        전부 막혀 원인을 알 수 없어요. **막고 이유를 로그에 남기는 쪽**을 골랐어요.

        대가: Cognito 장애가 로컬 개발을 세워요. 배포된 agent 경로는 영향이 없어요 — 그쪽은
        사람의 살아 있는 세션에서 그룹을 받아요(`access_router` 의 handle 발급).
        """
        if self._groups is None:
            _log.error(
                "dev identity group directory 가 배선되지 않았어요; "
                "credential=%s — 얼려둔 그룹으로 넘어가지 않고 막아요",
                record.credential_id,
            )
            raise DevIdentityAuthenticationError()
        try:
            current = self._groups.list_groups(record.principal)
        except Exception as exc:  # noqa: BLE001 - fail-closed by contract.
            _log.warning(
                "dev identity 그룹을 관측하지 못해 handle 발급을 막아요; "
                "credential=%s: %s",
                record.credential_id,
                type(exc).__name__,
            )
            raise DevIdentityAuthenticationError() from exc
        return _clean_groups(tuple(current))

    def _grants_still_cover(
        self,
        record: DevIdentityCredential,
        *,
        now: int,
        groups: tuple[str, ...],
    ) -> bool:
        """발급 때 근거였던 grant 가 아직 살아 있나요.

        `groups` 는 호출자가 **지금 관측한** 그룹이어야 해요(`_current_groups`). 얼려둔
        `record.principal_groups` 를 쓰면 사람을 그룹에서 빼도 크리덴셜 수명 동안 coverage 가
        통과해요 — 2026-08-30 codex 리뷰가 잡은 P0 예요.
        """
        # 발급 때와 **같은 술어**로 봐요 (ADR-0099). 라벨 부분집합이 아니라 도구 단위 grant 예요.
        return all(
            self._has_tool_grant(
                asset_id=binding.asset_id,
                operation_id=binding.operation_id,
                principal=record.principal,
                groups=groups,
                now=now,
            )
            for binding in record.action_grants
        )

    def _has_tool_grant(
        self,
        *,
        asset_id: str,
        operation_id: str,
        principal: str,
        groups: tuple[str, ...],
        now: int,
    ) -> bool:
        """⑦ 를 **소비자 경로 그대로** — 주체별 정확한 키 하나씩 (ADR-0099).

        `gateway_interceptor._has_tool_grant` 와 같은 질문이에요. `list_grants` 로 대체하면
        안 돼요: `principal_id=None` 경로가 전체 scan 이라 저장 위치를 무시해서, 잘못된 키의
        행도 「있다」로 읽혀요(2026-08-29 실사고).
        """
        subjects: list[dict[str, str]] = [{"principal_id": principal}]
        for group in dict.fromkeys(g.strip() for g in groups if g and g.strip()):
            subjects.append({"subject_group": group})
        for subject in subjects:
            try:
                grant = self._identity.get_tool_grant(
                    asset_id=asset_id,
                    operation_id=operation_id,
                    **subject,
                )
            except Exception:  # noqa: BLE001 - 없거나 못 읽으면 그 주체는 근거가 아니에요.
                continue
            if grant.status is not GrantStatus.ACTIVE:
                continue
            if grant.expires_at is not None and grant.expires_at <= now:
                continue
            return True
        return False

    def _deploy_policy(
        self, record: DevIdentityCredential, *, now: int
    ) -> DevIdentityCredential:
        """per-agent Cedar 정책을 만들지 않아요 (IH-142 / ADR-0101 §9 0단계 step 1).

        ADR-0099 로 per-agent 정책은 폐기됐어요 — gateway 당 굵은 공유 `permit` 하나가 모든
        도구를 열고, 도구 인가는 REQUEST interceptor 가 원장 신원·binding·⑦ grant 로 결정해요.
        그런데 dev 발급 경로만 `agent_policy_service.compile_and_deploy` 의
        `_PER_AGENT_POLICY_DEPRECATED` 게이트를 **지나지 않고** `compile_agent_policy` →
        `AgentPolicyDeployer.deploy` 를 직접 불러, dev 자격마다 `principal == OAuthUser::"<client_id>"`
        + 도구 이름 열거 형태의 `Agent_dev_*` 개체 permit 을 만들던 유일한 우회 경로였어요.
        그 두 호출을 끊어요.

        **원장의 deployment 행도 쓰지 않아요.** 그 행(`put/get_latest_agent_policy_deployment`,
        `dev-<credential_id>` 키)을 읽는 소비자는 이 서비스 내부의 `reconcile_pending_policies`
        와 `_delete_policy` 뿐이고(포털 라우트·화면·다른 서비스는 `dev-` 키를 읽지 않아요),
        둘 다 부재를 안전하게 다뤄요. 정책을 안 만드는데 PENDING 행만 남기면 원장이 「배포
        중」이라 거짓말을 하고 배경 reconciler 가 존재하지 않는 정책을 영원히 관측하려 들어요
        (ADR-0037 §4). 그래서 행 쓰기도 함께 정리해요. 이미 라이브에 존재하는 옛 `Agent_dev_*`
        행 정리는 AWS 쓰기라 이 변경의 범위 밖이에요 — reconcile·delete 경로는 그 옛 행을
        그대로 처리할 수 있게 남겨 둬요.

        `policy_revision` 은 원격 정책과 무관한 **원장 내부 순번**으로만 남겨요 — 재발급 때
        tool binding 이 이전 발급본을 앞지르는 supersession 순서에 쓰여요(`_write_ledger_identity`).
        """
        revision = record.policy_revision + 1
        return replace(record, policy_revision=revision)

    def _delete_policy(self, record: DevIdentityCredential) -> None:
        deployment = self._identity.get_latest_agent_policy_deployment(
            f"dev-{record.credential_id}"
        )
        if deployment is not None:
            self._policies.delete(deployment)

    def _owned(
        self, credential_id: str, principal: str
    ) -> DevIdentityCredential:
        record = self._store.get(credential_id)
        if record.principal != principal:
            raise DevIdentityOwnershipError(credential_id)
        return record

    @staticmethod
    def _new_plaintext(credential_id: str) -> str:
        return f"agora_dev_{credential_id}_{secrets.token_urlsafe(32)}"

    @staticmethod
    def _hash(plaintext: str) -> str:
        return hashlib.sha256(plaintext.encode()).hexdigest()

    @staticmethod
    def _credential_id(plaintext: str) -> str:
        if not plaintext.startswith("agora_dev_"):
            raise DevIdentityAuthenticationError()
        rest = plaintext.removeprefix("agora_dev_")
        credential_id, separator, secret = rest.partition("_")
        if not separator or len(credential_id) != 24 or not secret:
            raise DevIdentityAuthenticationError()
        return credential_id
