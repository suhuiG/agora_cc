"""Read-only authorization for AgentCore Gateway REQUEST interception."""
from __future__ import annotations

import base64
import copy
import json
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum

from ...shared.gateway_denial import (
    DEFAULT_DENIAL_MESSAGE,
    DENIAL_MESSAGES_BY_REASON,
)
from .delegation import DelegationService
from .models import (
    AgentToolBinding,
    ApprovalState,
    DelegationContext,
    DesiredState,
    GrantStatus,
    IdentityBindingStatus,
)
from .store import IdentityRecordNotFound, IdentityStore

#: Agora 가 채워주는 **예약 인자** (ADR-0095). 하나예요.
#:
#: MCP 도구가 `inputSchema` 에 이 이름을 선언하면 Agora 가 값을 채워요. 선언하지 않으면
#: **아무것도 넣지 않아요** — 넣으면 그 인자를 받지 않는 도구가 `TypeError` 로 죽고, 그러면
#: Agora 가 직접 만들지 않은 MCP 를 하나도 못 쓰게 돼요(2026-08-29 실측).
#:
#: `agora_` 접두어를 쓰는 이유: 어떤 MCP 가 `user_id` 를 이미 다른 뜻(조회 대상 사용자 등)으로
#: 쓰고 있으면 Agora 가 그 값을 조용히 덮어써서 다른 동작을 하게 돼요. 이름 공간을 격리해요.
#:
#: **값은 로그인 email 이에요** — Cognito `sub` 가 아니에요. MCP 의 데이터가 사람을 email 로
#: 식별하고 사람이 화면에서 보는 값도 email 이라서요(2026-08-30 제품 오너 결정). `sub` 는 Agora
#: 내부 인가 키로만 쓰고 밖으로 내보내지 않아요.
#:
#: 출처는 이미 GetItem 한 delegation 행의 `principal_email` 이에요. **호출 시점에 Cognito 를
#: 조회하지 않아요** — interceptor 는 판정을 자기 안에서 끝내야 하고 외부 조회 실패가 곧 전면
#: 거부라서요(ADR-0091). 그 행은 발급 시점에 서버가 검증된 access token 에서 써둔 값이라
#: 클라이언트가 위조할 수 없어요. 대가는 email 이 바뀐 사람의 기존 handle 이 만료까지 옛
#: email 을 들고 있다는 것이에요(TTL ≤ 900초).
OWNER_ARGUMENT = "agora_user_id"
HUMAN_CLIENT_IDS_ENV = "AGORA_GATEWAY_HUMAN_CLIENT_IDS"

_PASSTHROUGH_METHODS = frozenset({"initialize", "ping", "tools/list"})
_ALLOWED_META_KEYS = frozenset({"progressToken"})
_AGENTCORE_GATEWAY_META_PREFIX = "aws.bedrock-agentcore.gateway/"


class GatewayDenialReason(str, Enum):
    INVALID_REQUEST = "invalid_request"
    INVALID_DELEGATION = "invalid_delegation"
    AGENT_MISMATCH = "agent_mismatch"
    PRINCIPAL_MISMATCH = "principal_mismatch"
    HUMAN_ROUTE_UNCONFIGURED = "human_route_unconfigured"
    METHOD_NOT_ALLOWED = "method_not_allowed"
    TOOL_NOT_APPROVED = "tool_not_approved"
    #: ④ binding 이 **있고** `REQUESTED` 예요 — 관리자 승인만 남았어요 (ADR-0104).
    #:
    #: `TOOL_NOT_APPROVED` 와 섞으면 진단이 반대로 가요. 「행이 없음」은 그 agent 가 그 도구를
    #: 신청한 적이 없다는 뜻이고, 이건 신청했고 대기 중이라는 뜻이에요. 2026-09-04 실측에서
    #: LLM 이 뭉쳐진 사유를 보고 「관리자 권한이 필요한데 현재 계정으로는 승인되지 않았어요」
    #: 라고 **틀리게** 설명했어요 — 실제로는 그 agent 의 도구 승인 대기였어요.
    TOOL_PENDING_APPROVAL = "tool_pending_approval"
    HUMAN_GRANT_MISSING = "human_grant_missing"
    #: 도구가 `agora_user_id` 를 선언했는데 원장에 호출자 email 이 없어요 (ADR-0095).
    #: 인가 사슬은 통과한 상태라 `human_grant_missing` 과 섞으면 진단이 엉켜요 — 별도 사유예요.
    OWNER_IDENTITY_MISSING = "owner_identity_missing"


#: 사유 → 사람이 읽는 한국어 문구. **문장의 단일 출처는 `shared.gateway_denial` 이에요**
#: (ADR-0104, IH-183). 이 표는 그 표를 enum 키로 다시 씌운 파생물이에요 — 사본이 아니에요.
#:
#: 왜 필요한가: JSON-RPC `message` 가 고정 영문(`Request denied by Agora authorization`)이던
#: 동안 LLM 이 사유를 추측해서 사용자에게 틀린 설명을 했어요(2026-09-04 실측). `data.reason`
#: 은 기계용 어휘고, 모델이 사용자에게 옮길 문장은 여기 있어야 해요.
#:
#: 왜 shared 로 옮겼나: 같은 문장을 **알아보는** 쪽이 두 군데 더 있어요 — 생성 agent 의 도구
#: probe(`playground/scaffold.py`)와 포털의 배포 실패 문구(`runtime/deploy/verify.py`).
#: 사본을 두면 문장을 한쪽만 고쳤을 때 정확 일치가 조용히 실패하고, 사유 전달이 **꺼진 것도
#: 모르게** 꺼져요(IH-183).
#:
#: ⚠️ **주체를 특정하는 값은 절대 넣지 마세요** — ARN·client_id·handle·email·원장 id·스택
#: 트레이스 금지. 이 문구는 배포 agent 를 거쳐 최종 사용자 화면까지 그대로 갈 수 있어요.
#: 그래서 문장은 사유 자체만 말하고 좌표를 말하지 않아요.
DENIAL_MESSAGES: Mapping[GatewayDenialReason, str] = {
    GatewayDenialReason(reason): message
    for reason, message in DENIAL_MESSAGES_BY_REASON.items()
}


def denial_message(reason: GatewayDenialReason) -> str:
    """이 사유를 사람에게 보여줄 문장. 없으면 고정 문구예요."""
    return DENIAL_MESSAGES.get(reason, DEFAULT_DENIAL_MESSAGE)


class GatewayRequestDenied(PermissionError):
    def __init__(self, reason: GatewayDenialReason):
        super().__init__(reason.value)
        self.reason = reason


@dataclass(frozen=True)
class ValidatedCall:
    context: DelegationContext
    client_id: str


@dataclass(frozen=True)
class BearerClaims:
    client_id: str
    sub: str
    token_use: str


def _header(headers: object, name: str) -> str:
    if not isinstance(headers, dict):
        return ""
    expected = name.casefold()
    for key, value in headers.items():
        if str(key).casefold() == expected and isinstance(value, str):
            return value.strip()
    return ""


def _bearer_claims(headers: object) -> BearerClaims:
    authorization = _header(headers, "authorization")
    scheme, separator, token = authorization.partition(" ")
    if not separator or scheme.casefold() != "bearer":
        raise GatewayRequestDenied(GatewayDenialReason.AGENT_MISMATCH)
    parts = token.split(".")
    if len(parts) != 3:
        raise GatewayRequestDenied(GatewayDenialReason.AGENT_MISMATCH)
    try:
        payload_part = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_part))
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise GatewayRequestDenied(GatewayDenialReason.AGENT_MISMATCH) from exc
    if not isinstance(payload, dict):
        raise GatewayRequestDenied(GatewayDenialReason.AGENT_MISMATCH)
    client_id = payload.get("client_id")
    if not isinstance(client_id, str) or not client_id.strip():
        raise GatewayRequestDenied(GatewayDenialReason.AGENT_MISMATCH)
    sub = payload.get("sub")
    token_use = payload.get("token_use")
    return BearerClaims(
        client_id=client_id.strip(),
        sub=sub.strip() if isinstance(sub, str) else "",
        token_use=token_use.strip() if isinstance(token_use, str) else "",
    )


def _human_client_ids() -> frozenset[str]:
    return frozenset(
        client_id
        for raw_client_id in os.environ.get(HUMAN_CLIENT_IDS_ENV, "").split(",")
        if (client_id := raw_client_id.strip())
    )


def _sanitize_request_metadata(body: dict) -> dict:
    """Keep only MCP metadata Agora explicitly permits downstream."""
    params = body.get("params")
    if not isinstance(params, dict) or "_meta" not in params:
        return body

    metadata = params.get("_meta")
    sanitized = (
        {
            key: value
            for key, value in metadata.items()
            if isinstance(key, str)
            and not key.startswith(_AGENTCORE_GATEWAY_META_PREFIX)
            and key in _ALLOWED_META_KEYS
        }
        if isinstance(metadata, dict)
        else {}
    )
    if sanitized:
        params["_meta"] = sanitized
    else:
        params.pop("_meta", None)
    return body


class GatewayRequestAuthorizer:
    """Evaluate one request from current identity-ledger state without writes or cache."""

    def __init__(self, store: IdentityStore, *, now=None) -> None:
        self._store = store
        self._now = now or (lambda: int(time.time()))

    def authorize_and_transform(
        self,
        *,
        body: object,
        headers: object,
    ) -> dict:
        if not isinstance(body, dict):
            raise GatewayRequestDenied(GatewayDenialReason.INVALID_REQUEST)
        method = body.get("method")
        if not isinstance(method, str) or not method:
            raise GatewayRequestDenied(GatewayDenialReason.INVALID_REQUEST)

        call = self._validate_call(headers)
        if method == "tools/call":
            transformed = self._authorize_tool(body, call.context)
        elif method.startswith(("prompts/", "resources/")):
            raise GatewayRequestDenied(GatewayDenialReason.METHOD_NOT_ALLOWED)
        elif method.startswith("notifications/") or method in _PASSTHROUGH_METHODS:
            transformed = copy.deepcopy(body)
        else:
            raise GatewayRequestDenied(GatewayDenialReason.METHOD_NOT_ALLOWED)
        return _sanitize_request_metadata(transformed)

    def _validate_call(self, headers: object) -> ValidatedCall:
        handle = _header(headers, "x-agora-call")
        if not handle:
            raise GatewayRequestDenied(GatewayDenialReason.INVALID_DELEGATION)
        try:
            context = self._store.get_delegation(
                DelegationService.hash_handle(handle)
            )
        except IdentityRecordNotFound as exc:
            raise GatewayRequestDenied(
                GatewayDenialReason.INVALID_DELEGATION
            ) from exc
        if context.revoked_at is not None or context.expires_at <= self._now():
            raise GatewayRequestDenied(GatewayDenialReason.INVALID_DELEGATION)

        claims = _bearer_claims(headers)
        human_shaped = bool(
            claims.sub and claims.sub != claims.client_id
        )
        human_client_ids = _human_client_ids()
        if human_shaped and not human_client_ids:
            raise GatewayRequestDenied(
                GatewayDenialReason.HUMAN_ROUTE_UNCONFIGURED
            )
        is_human = (
            human_shaped
            and claims.client_id in human_client_ids
        )
        try:
            identity = self._store.get_agent_identity_binding(context.agent_id)
        except IdentityRecordNotFound as exc:
            raise GatewayRequestDenied(GatewayDenialReason.AGENT_MISMATCH) from exc
        if (
            identity.status is not IdentityBindingStatus.ACTIVE
            or not identity.client_id
        ):
            raise GatewayRequestDenied(GatewayDenialReason.AGENT_MISMATCH)
        if is_human:
            if (
                claims.token_use != "access"
                or claims.sub != context.principal_id
            ):
                raise GatewayRequestDenied(
                    GatewayDenialReason.PRINCIPAL_MISMATCH
                )
        elif identity.client_id != claims.client_id:
            raise GatewayRequestDenied(GatewayDenialReason.AGENT_MISMATCH)
        return ValidatedCall(context=context, client_id=claims.client_id)

    def _has_tool_grant(
        self,
        context: DelegationContext,
        binding: AgentToolBinding,
    ) -> bool:
        """⑦ 를 **소비자 경로 그대로** 확인해요 — 사람 축과 그룹 축 각각 정확한 키 하나.

        하나라도 ACTIVE 이고 만료 전이면 통과예요(합집합). 그룹이 여러 개면 그룹당 `GetItem`
        한 번이라 왕복이 `1 + 그룹 수` 예요 — `Query` 도 `scan` 도 쓰지 않아요.
        """
        subjects: list[dict[str, str]] = [{"principal_id": context.principal_id}]
        for group in dict.fromkeys(
            g.strip() for g in context.principal_groups if g and g.strip()
        ):
            subjects.append({"subject_group": group})
        for subject in subjects:
            try:
                grant = self._store.get_tool_grant(
                    asset_id=binding.asset_id,
                    operation_id=binding.operation_id,
                    **subject,
                )
            except IdentityRecordNotFound:
                continue
            if grant.status is not GrantStatus.ACTIVE:
                continue
            if grant.expires_at is not None and grant.expires_at <= self._now():
                continue
            return True
        return False

    def _authorize_tool(
        self,
        body: dict,
        context: DelegationContext,
    ) -> dict:
        params = body.get("params")
        if not isinstance(params, dict):
            raise GatewayRequestDenied(GatewayDenialReason.INVALID_REQUEST)
        tool_name = params.get("name")
        arguments = params.get("arguments", {})
        if (
            not isinstance(tool_name, str)
            or not tool_name
            or not isinstance(arguments, dict)
        ):
            raise GatewayRequestDenied(GatewayDenialReason.INVALID_REQUEST)

        candidates = [
            binding
            for binding in self._store.list_agent_tool_bindings(context.agent_id)
            if binding.gateway_action == tool_name
            and binding.desired_state is DesiredState.ALLOWED
        ]
        approved = [
            binding
            for binding in candidates
            if binding.approval_state is ApprovalState.APPROVED
        ]
        # One Gateway action must resolve to one independently approved ledger row.
        #
        # ④ 판정을 **여기서** 끝내는 이유 (ADR-0104):
        #
        # 「승인 대기」는 ④ 층의 상태예요. 자산 경계·버전 대조·⑦ grant·owner identity 는 그
        # **다음** 층들이고, 이 함수는 원래부터 «가장 먼저 막히는 층» 을 사유로 돌려줘요
        # (행이 없으면 ⑦ 를 조회하지 않아요). 그 순서를 그대로 유지해요 — 세 가지 이유예요.
        #
        # ⑴ 사슬 순서가 진단 순서예요. 뒤로 미루면 「승인 대기인데 grant 도 없음」이
        #    `human_grant_missing` 으로 보고돼서, 관리자가 grant 를 주고도 여전히 막히는
        #    것을 봐요. 실제로 먼저 필요한 건 ④ 승인이에요.
        # ⑵ 정보 노출. 이 agent 에 승인되지 않은 도구에 대해 ⑦ 를 조회하면 「당신은 이 도구
        #    권한이 있다/없다」를 미승인 경로로 알려주게 돼요.
        # ⑶ 뒤 층들의 **동작과 사유는 하나도 안 바꿔요.** 승인 대기는 `REQUESTED` 행에서만
        #    나오고, `APPROVED` 행은 예전과 **완전히 같은** 경로를 타요. 그래서 자산 경계·
        #    버전·⑦·owner 거부가 「승인 대기」로 가려지는 일이 구조적으로 없어요.
        #
        # `len(approved) != 1` 일 때만 승인 대기를 판정해요 — `APPROVED` 행이 하나면
        # 예전과 동일하게 통과시켜서, 「승인 1행 + 대기 1행」(재등록으로 버전이 둘일 때
        # 생겨요)이 갑자기 거부로 바뀌지 않아요.
        if len(approved) != 1:
            pending = [
                binding
                for binding in candidates
                if binding.approval_state is ApprovalState.REQUESTED
            ]
            # 「행 2개」는 승인이든 대기든 여전히 `tool_not_approved` 예요 — 「하나의 Gateway
            # action 은 독립 승인 행 하나로 풀려야 한다」는 규칙을 약화시키지 않아요.
            if not approved and len(pending) == 1:
                raise GatewayRequestDenied(
                    GatewayDenialReason.TOOL_PENDING_APPROVAL
                )
            raise GatewayRequestDenied(GatewayDenialReason.TOOL_NOT_APPROVED)
        binding = approved[0]
        # handle 의 자산 경계 — **빈 tuple 은 deny-all 이에요.**
        #
        # 이 티켓 초안은 빈 tuple 을 "옛 행 하위호환" 으로 통과시켰어요. 그 근거가 틀렸어요:
        # delegation TTL 이 최대 900초이고 배포 시점에 살아 있는 행이 0건이라, 배포 후의 빈
        # tuple 은 레거시가 아니라 **새 발급**이에요. 그러면 통과 규칙은 하위호환이 아니라
        # "자산을 하나도 지정하지 않은 handle 에 무제한 권한" 이 돼요.
        #
        # 같은 필드를 읽는 다른 소비자도 이미 deny-all 로 해석해요
        # (`identity/authorization.py` 의 `asset_id not in context.allowed_asset_ids`).
        # 한 필드를 두 곳에서 반대 의미로 읽으면 어느 쪽이 강제인지 알 수 없어요.
        #
        # 발급부는 세 곳 모두 agent 가 **선언한 MCP** 를 넣어요 — `/api/invocations` 는
        # `delegated_asset_ids(record.descriptors)`, 배포 검증은 `job.mcp_assets`,
        # dev 크리덴셜은 좁힌 목록이에요. `issue_verify_call_handle` docstring 이 이미
        # "비워 두면 handle 하나로 그 Gateway 의 모든 자산을 부를 수 있게 돼요" 라고
        # 경고해 뒀고, 이 검사가 그 경고를 강제로 만들어요.
        #
        # MCP 를 하나도 선언하지 않은 agent 는 빈 tuple 을 받아 모든 `tools/call` 이
        # 거부돼요 — 부를 도구가 애초에 없으니 정상이에요. `initialize`·`tools/list` 는
        # `_PASSTHROUGH_METHODS` 라 영향받지 않아요.
        if binding.asset_id not in context.allowed_asset_ids:
            raise GatewayRequestDenied(GatewayDenialReason.TOOL_NOT_APPROVED)

        # ④ 의 버전 대조 — 기대값은 **원장의 «자산 현재 버전» 행**이에요 (ADR-0099 결정 13).
        #
        # 왜 여기 있나: ADR-0090 은 「재등록이 승인을 물려받지 않는다」예요. 자산이 새 버전으로
        # 올라가면 옛 버전에 대한 승인은 무효여야 해요. 옛 코드는 그 대조를 ⑤
        # (`AssetCapability`) 에 뒀는데, 승인 경로가 `asset_version=binding.asset_version` 으로
        # **binding 값을 그대로 복사**해서 구조적으로 절대 걸리지 않았어요 — 기대값을 대상에게서
        # 받아온 것이에요(ADR-0037 §4).
        #
        # 그래서 기대값의 소유자를 **등록 흐름**으로 옮겼어요. 승인 흐름은 이 행을 쓰지 않아요.
        #
        # 행이 없으면 **거부**예요. 「모르니까 통과」는 이 층을 없애는 것과 같아요.
        try:
            asset_version = self._store.get_asset_version(binding.asset_id)
        except IdentityRecordNotFound as exc:
            raise GatewayRequestDenied(
                GatewayDenialReason.TOOL_NOT_APPROVED
            ) from exc
        if asset_version.asset_version != binding.asset_version:
            raise GatewayRequestDenied(GatewayDenialReason.TOOL_NOT_APPROVED)

        # ⑦ — 이 사람·그룹이 **이 도구**를 부를 수 있나.
        #
        # `(subject, asset_id, operation_id)` 정확한 키로 읽어요. 옛 코드는 capability 라벨의
        # 집합 연산이었고, 라벨을 요구하는 자산이 늘면 부여하지 않은 자산까지 열렸어요
        # (IH-130). 이제 중간 화폐가 없어서 부여한 것과 열리는 것이 같아요.
        #
        # 그룹은 `context.principal_groups` 에서 와요 — Agora 가 handle 발급 시점에 인증된
        # 세션의 `principal.roles` 를 그 행에 써둔 값이에요. 여기서 Cognito 를 조회하지
        # 않아요: interceptor 는 판정을 자기 안에서 끝내야 하고 외부 조회 실패가 곧 전면
        # 거부예요(ADR-0091). delegation 행은 이미 GetItem 하니 추가 조회가 0회예요.
        #
        # 클라이언트가 그룹을 위조할 수 없어요 — 이 행은 서버가 쓰고 handle 의 SHA-256
        # 해시로만 조회돼요. 옛 delegation 행에는 이 필드가 없어서 빈 tuple 이고, 그러면
        # 그룹 grant 가 적용되지 않아요(fail-closed).
        #
        # **읽기는 사람 1회 + 그룹당 1회의 `GetItem` 이에요.** `list_grants` 를 쓰면
        # `principal_id=None` 경로가 전체 scan 이라 저장 위치를 무시해요 — 잘못된 키의 행이
        # 「있다」로 읽히는 그 사고(2026-08-29)의 SK 버전이 가능해져요.
        if not self._has_tool_grant(context, binding):
            raise GatewayRequestDenied(GatewayDenialReason.HUMAN_GRANT_MISSING)

        # 예약 인자는 **선언된 경우에만** 채워요 (ADR-0095).
        #
        # "있으면 덮어쓴다" 가 곧 "선언된 것만" 이에요 — LLM 은 스키마에 있는 인자만 보내니,
        # 요청에 그 키가 있다는 건 도구가 선언했다는 뜻이거든요. 없는 키를 만들어 넣으면
        # 그 인자를 받지 않는 도구가 죽어요.
        #
        # 값은 LLM 이 아니라 원장이 정해요. 봇이 남의 email 을 실어 보내도 여기서 교체돼요
        # (패턴 02 — LLM 을 판단 주체에서 배제).
        transformed = copy.deepcopy(body)
        rewritten = dict(arguments)
        if OWNER_ARGUMENT in rewritten:
            if not context.principal_email:
                # 도구는 호출자를 요구하는데 원장에 email 이 없어요. **거부해요.**
                # 안 채우고 통과시키면 봇이 실어 보낸 값이 그대로 도구에 도달해서, MCP 가
                # 봇이 고른 소유자를 신뢰하게 돼요 — 그게 이 계약의 유일한 보장을 깨요.
                raise GatewayRequestDenied(
                    GatewayDenialReason.OWNER_IDENTITY_MISSING
                )
            rewritten[OWNER_ARGUMENT] = context.principal_email
        transformed["params"]["arguments"] = rewritten
        return transformed
