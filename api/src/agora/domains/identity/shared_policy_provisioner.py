"""공유 Gateway Cedar 정책을 **인프라로 provisioning** 해요 (IA-71, ADR-0093).

## 컷오버가 아니에요

`agent_policy_cutover.py` 는 살아있는 per-agent 정책을 보존하며 갈아타는 마이그레이션 기계고
폐기됐어요(ADR-0093). 이 모듈은 그 자리를 대신하지 않아요 — **초기 상태를 만드는 쪽**이에요.
지킬 대상이 없으니 "옛 정책 삭제" 단계가 없어요.

## 언제 도나

정책 내용은 Registry Target operation과 identity ④ binding 원장의 교집합에서 나와요.
기대값은 caller가 주지 않고 이 provisioner가 identity store에서 직접 관측해요.

- Gateway 최초 설정과 MCP 자산 등록·삭제 — Target operation 목록이 바뀌니 재컴파일.
- 승인된 ④ binding 집합이나 ACTIVE 도메인 규칙 ② 집합이 바뀌는 경로도 같은
  provisioner를 호출해야 해요(ADR-0099 결정 8).

정책 장수는 여전히 Gateway 단위라 봇 수와 무관해요.

## interceptor 관측을 왜 여기서 하나

컴파일러는 순수 함수라 AWS 를 볼 수 없어요. 공유 ①은 승인된 ④ action을 열거하지만 사람별
⑦ grant를 모르므로 REQUEST interceptor가 반드시 필요해요. 그래서 이 모듈이 Gateway의
`interceptorConfigurations`를 읽어 컴파일러에 넣어줘요. 기대값의 출처가 Gateway 자신이라
정책(subject)과 소유자가 달라요(ADR-0037 §4).

읽지 못하면 `None` 으로 넘겨요 — 컴파일러가 "관측하지 않음" 으로 막아요. 못 본 것을 붙어
있는 것으로 접지 않아요.

## 교체 순서

정책을 바꿀 때는 리비전을 올려 **새로 만들고 → `ACTIVE` 확인 → 옛 리비전 삭제** 예요.
제자리 `update_policy` 를 쓰지 않아요 — 2026-08-29 실측으로 `UPDATE_FAILED` 가 되면
`get_policy` 가 **새(실패한) 문장**을 돌려줬어요. 그 상태에서 무엇이 강제되는지 알 수 없어요.
리비전 방식은 실패해도 옛 리비전이 그대로 ACTIVE 라 강제 상태가 항상 확정적이에요.

리비전은 gateway 당이라 봇 수와 무관해요 — per-agent 형태의 리비전 누적 문제가 아니에요.
"""

from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass, field

from .agent_policy_compiler import (
    _CEDAR_ACTION_LITERAL_RE,
    CompiledSharedGatewayPolicies,
    InvalidPolicyInput,
    LiveGatewayInventory,
    SharedGatewayPolicySpec,
    compile_shared_gateway_policies,
)
from .models import AgentToolBinding, DomainPolicyRule

_log = logging.getLogger(__name__)

# Target 당 `get_gateway_target` 이 0.124초예요(2026-09-05 실측).
#
# ⚠️ 이 비용은 활성화 8폴 예산 **밖**이에요 — 그 앞에서 따로 써요. 어드민 HTTP 한 요청의
# 실제 상한은 「인벤토리 읽기 + 활성화 최대 8초 + 삭제 부재확인 최대 8초」이고, 그 위에
# ALB idle 60초 / purge 경로의 CloudFront read timeout 30초가 있어요.
#
# 상한은 **선언 Target** 에 걸려요(`observe_live_gateway_inventory(needed_targets=…)`) —
# 등록된 자산 전체가 아니라 agent 가 실제로 쓰는 Target 수예요. 2026-09-05 라이브는 13개고,
# AgentCore 의 gateway 당 Target 상한 자체가 100 이라 40 은 그 사이의 여유값이에요.
# 넘는 Target 은 도구 목록을 미관측으로 두고 이름까지만 확인해요(리포트에 남겨요).
_LIVE_TOOL_SCHEMA_MAX_TARGETS = 40


def _statement_targets_gateway(statement: str, gateway_arn: str) -> bool:
    """Cedar 문장의 `resource` 가 이 Gateway 를 가리키나요.

    `resource == AgentCore::Gateway::"<arn>"`(정확 일치)와
    `resource is AgentCore::Gateway`(타입 전체) 둘 다 이 Gateway 를 포함해요. 다른 ARN 을
    정확 일치로 지목하면 포함하지 않아요.
    """
    folded = normalize_cedar(statement)
    if f'AgentCore::Gateway::"{gateway_arn}"' in folded:
        return True
    # 타입 전체를 가리키는 형태 — 특정 ARN 을 지목하지 않을 때만 참이에요.
    if "resource is AgentCore::Gateway" in folded:
        return 'AgentCore::Gateway::"' not in folded
    return False


def _declared_target_names(
    bindings: tuple[AgentToolBinding, ...] | None,
) -> frozenset[str]:
    """④ 원장이 선언한 Gateway Target 이름 — 도구 스키마를 읽어야 하는 Target 집합이에요.

    라이브 판정은 선언된 action 에만 의미가 있어서(컴파일러 주석), 이 집합 밖 Target 의 도구
    목록은 읽어도 결과가 같아요. 술어는 컴파일러의 열거 술어와 **같은 방향**으로 넓게 잡아요
    (`REJECTED` 만 제외) — 좁게 잡으면 판정에 필요한 Target 을 안 읽어서 조용히 덜 걸러요.
    """
    from .models import ApprovalState, DesiredState

    if bindings is None:
        return frozenset()
    return frozenset(
        binding.gateway_target_name.strip()
        for binding in bindings
        if binding.desired_state is DesiredState.ALLOWED
        and binding.approval_state is not ApprovalState.REJECTED
        and binding.gateway_target_name.strip()
    )


def _action_is_live(inventory: LiveGatewayInventory, action: str) -> bool:
    target_name, _, operation_id = action.partition("___")
    return inventory.has_action(target_name, operation_id)


def _inline_tool_names(
    target_configuration: object,
) -> frozenset[str] | None:
    """Target 설정의 인라인 도구 스키마에서 도구 이름을 읽어요.

    `None` 은 「이 Target 에서는 도구 목록을 알 수 없다」예요 — 연결형(`mcpServer`)은 설정에
    도구가 없고 상류가 주는 것이 전부거든요. 빈 집합과 구분해야 해요(빈 집합은 「도구가
    없음을 관측」이라 전부 걸러내요).
    """
    if not isinstance(target_configuration, dict):
        return None
    mcp = target_configuration.get("mcp")
    if not isinstance(mcp, dict):
        return None
    names: set[str] = set()
    found = False
    for key in ("lambda", "openApiSchema", "smithyModel"):
        node = mcp.get(key)
        if not isinstance(node, dict):
            continue
        schema = node.get("toolSchema")
        if not isinstance(schema, dict):
            continue
        payload = schema.get("inlinePayload")
        if not isinstance(payload, list):
            continue
        found = True
        for item in payload:
            if isinstance(item, dict) and str(item.get("name") or "").strip():
                names.add(str(item["name"]).strip())
    return frozenset(names) if found else None

# 프로덕션 배포와 같은 값이어야 해요 — `AgentPolicyDeployer.__init__` 기본값이에요.
# 화면·provisioning·배포가 서로 다른 검증을 받으면 한쪽에서 통과한 정책이 다른 쪽에서 막혀요.
VALIDATION_MODE = "FAIL_ON_ANY_FINDINGS"

# 2026-08-29 실측: 정책 9장 엔진에서 `create_policy` 가 `CREATING` 을 주고 **15초 뒤** ACTIVE
# 였어요. 검증은 엔진 전체와 교차해서 도구가 늘면 더 걸려요(도구 1,000개당 6~7.5초).
_ACTIVE_MAX_POLLS = 60
_DELETE_MAX_POLLS = 8
_POLL_SECONDS = 1.0

# `Gateway_{hash10}_{key}_r{n}` — 이 접두어로 우리 소유 정책을 식별해요. 이름 규약이 곧
# 소유권 표시라서, 규약을 바꾸면 옛 리비전을 못 찾아 정리가 조용히 멈춰요.
_NAME_PREFIX = "Gateway_"


@dataclass
class ProvisionReport:
    gateway_arn: str
    revision: int
    interceptor_attached: bool | None
    created: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    # `ok=False` 는 실패, `verdict="unknown"` 은 관측 못 함이에요. 둘을 섞지 않아요.
    ok: bool = True
    verdict: str = "provisioned"
    reason: str = ""
    warnings: list[str] = field(default_factory=list)
    # ── 관측 신호 (IH-154) ────────────────────────────────────────────────
    #
    # ⚠️ 이건 `warnings` 가 **아니에요.** `shared_policy_trigger.py:45` 가 warning
    # 하나로 요청을 실패시키고, purge 는 warning 을 stage 실패로 읽어 registry record
    # 를 보존해요(`catalog/purge/service.py:455-465`). 그래서 「입력 상태가 이랬다」는
    # 관측을 warning 으로 내면 **건강한 Gateway 에서** 어드민 엔드포인트가 502 가 되고
    # 정상 purge 가 record 를 남겨요. 별 필드로 실어요.
    #
    # 진입 시점에 소유 리비전이 둘 이상이면 그 이름들이에요. Cedar permit 은 합집합이라
    # 가장 넓은 옛 리비전이 계속 이기고, 그게 IH-154 의 「다음 회수를 무력화」예요.
    stale_revisions: list[str] = field(default_factory=list)
    # `create_policy` 가 `ACTIVE` 로 끝냈지만 AWS 가 함께 준 분석 소견이에요. 소유자가
    # AWS 라서 우리가 계산한 값이 아니에요 — 2026-09-05 실측으로 「기존 정책이 최신 도구
    # 스키마와 안 맞는다」를 여기로 알려줘요(IH-140 탐지 신호). 실패가 아니라서 `ok` 를
    # 내리지 않지만, 버리면 AWS 의 드리프트 관측을 우리가 조용히 삼키는 셈이에요.
    activation_findings: list[str] = field(default_factory=list)
    # ── 라이브 Gateway 기준 제외 공시 (IH-140) ────────────────────────────
    # `"observed"` / `"unknown"` — `unknown` 은 라이브를 못 읽어 **아무것도 걸러내지
    # 않았다**는 뜻이에요. 통과가 아니에요(ADR-0037 §4).
    live_inventory_status: str = "unknown"
    excluded_actions: list[str] = field(default_factory=list)
    # 승인됐는데 라이브에 없어서 빠진 것 — 관리자가 Target 을 복구해야 하는 신호예요.
    excluded_approved_actions: list[str] = field(default_factory=list)
    # 관측이 부분적이었던 이유(연결형 Target·throttle 등). ⚠️ `warnings` 가 **아니에요** —
    # 연결형 Target 하나만 있어도 매 provisioning 마다 warning 이 생겨 건강한 Gateway 에서
    # 어드민 엔드포인트 여섯 개가 502 가 돼요(`shared_policy_trigger.py:45`).
    live_inventory_reason: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ReclaimReport:
    """옛 리비전 회수 결과 (IH-154).

    `ProvisionReport` 와 따로 두는 이유는 **동작이 다르기 때문**이에요 — 이 pass 는 절대
    정책을 만들지 않아요. 같은 타입을 쓰면 `created`·`unchanged` 처럼 이 경로에서 영원히
    빈 필드가 생겨 「만들 수도 있다」는 오독을 만들어요.

    `verdict` 값:
      * `not_applicable` — 소유 리비전이 0장 또는 1장이라 회수할 게 없어요(정상 상태).
      * `reclaimed` — 최신 ACTIVE 리비전을 확인하고 옛 리비전을 지웠어요.
      * `blocked` — 확인이 안 돼서 **아무것도 지우지 않았어요**(최신이 ACTIVE 아님 등).
      * `partial` — 일부만 지웠거나 삭제를 확인하지 못했어요. 성공이 아니에요 — 남은 옛
        리비전이 계속 Cedar permit 합집합에서 이겨요.
      * `unknown` — 관측 자체를 못 했어요. 통과가 아니에요(ADR-0037 §4).
    """

    gateway_arn: str
    ok: bool = True
    verdict: str = "not_applicable"
    surviving_revision: int = 0
    deleted: list[str] = field(default_factory=list)
    stale_revisions: list[str] = field(default_factory=list)
    reason: str = ""
    warnings: list[str] = field(default_factory=list)
    # 관측 신호 — `warnings` 가 아니에요(`ProvisionReport` 와 같은 이유).
    live_inventory_status: str = "unknown"
    live_inventory_reason: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


class PolicyActivationPending(RuntimeError):
    """새 정책은 만들어졌지만 주어진 관측 예산 안에 ACTIVE가 되지 않았어요."""

    def __init__(
        self,
        *,
        policy_id: str,
        policy_name: str,
        max_polls: int,
    ) -> None:
        self.policy_id = policy_id
        self.policy_name = policy_name
        self.max_polls = max_polls
        super().__init__(
            f"{policy_name}: {max_polls}회 관측 안에 ACTIVE가 되지 않았어요. "
            "새 리비전은 만들어졌고 아직 활성화 중이에요 — 잠시 뒤 다시 실행해 주세요."
        )


class SharedPolicyProvisioner:
    """Gateway 하나의 공유 정책을 원하는 상태로 맞춰요 (멱등)."""

    def __init__(
        self,
        control_client,
        identity_store,
        *,
        sleep=None,
        active_max_polls: int = _ACTIVE_MAX_POLLS,
    ) -> None:
        if active_max_polls < 1:
            raise ValueError("active_max_polls는 1 이상이어야 해요.")
        self._c = control_client
        self._store = identity_store
        self._sleep = sleep or time.sleep
        self._active_max_polls = active_max_polls

    # ── interceptor 관측 (독립 소유자) ──────────────────────────────────────
    def observe_interceptor(self, gateway_id: str) -> tuple[bool | None, str]:
        """Gateway 에 REQUEST interceptor 가 붙어 있는지 읽어요.

        `None` 은 관측 실패예요. 붙어 있는 것으로 접지 않아요.

        boto3 로만 읽어요 — `aws` CLI 2.31.18 의 `get-gateway` 는
        `interceptorConfigurations` 를 아예 안 보여줘서 붙어 있는 Gateway 가 "없음" 으로
        보여요(2026-08-29 실측).
        """
        try:
            full = self._c.get_gateway(gatewayIdentifier=gateway_id)
        except Exception as exc:
            return None, (
                f"interceptor 부착 여부를 읽지 못했어요: {type(exc).__name__}: {exc}"
            )
        configs = (
            full.get("interceptorConfigurations")
            or full.get("interceptors")
            or []
        )
        for config in configs:
            points = config.get("interceptionPoints") or []
            if "REQUEST" in points:
                return True, ""
        return False, ""

    # ── 라이브 Gateway 관측 (독립 소유자, IH-140) ──────────────────────────
    def observe_live_gateway_inventory(
        self,
        gateway_id: str,
        needed_targets: frozenset[str] | None = None,
    ) -> tuple[LiveGatewayInventory | None, str]:
        """라이브 Target 이름과 (읽을 수 있으면) 도구 이름을 읽어요.

        Target **이름**은 `list_gateway_targets` 한 번이면 돼요 — 2026-09-05 실측 **0.072초**.
        도달 가능한 드리프트(mover 보상 실패·out-of-band 삭제·개명)가 이 축이라 **항상**
        전량을 읽어요.

        **도구 이름**은 Target 당 `get_gateway_target` 이 필요해요 — 응답 item 에
        `targetConfiguration` 이 없고(모델된 멤버는 name·status·targetId·description·
        createdAt·updatedAt 뿐), `get_gateway_target` 은 이름이 아니라 **불투명 `targetId`**
        만 받아요(이름을 주면 `ValidationException`, 2026-09-05 실측). 그래서 이름→id 매핑에
        위 목록 호출이 필요하고, 도구 축은 **Target 당 0.124초**예요.

        ## `needed_targets` 로 비용을 줄여요

        라이브 판정은 **선언된 action 에만** 의미가 있어요(컴파일러 주석 참고). 그래서 호출자가
        선언 Target 집합을 주면 그 Target 만 도구 스키마를 읽어요 — 결과는 전량 읽기와
        **동일**하고, 비용은 「등록된 자산 수」가 아니라 「agent 가 실제로 쓰는 Target 수」에만
        비례해요. 2026-09-05 라이브: 15개 → **13개**.

        `None` 이면 전량을 읽어요(옛 동작). 어느 쪽이든 상한을 넘는 Target 은 「도구 목록
        미관측」이 되어 이름까지만 확인하고, 무엇이 잘렸는지 이유로 돌려줘요.
        """
        try:
            items = self._list_gateway_targets(gateway_id)
        except Exception as exc:  # noqa: BLE001 - 관측 실패는 거부가 아니에요.
            return None, (
                "라이브 Gateway Target 목록을 읽지 못했어요: "
                f"{type(exc).__name__}: {exc}"
            )
        names = frozenset(
            str(item.get("name") or "").strip()
            for item in items
            if str(item.get("name") or "").strip()
        )
        tools_by_target: dict[str, frozenset[str]] = {}
        unread: list[str] = []
        # ⚠️ 상한을 **이름 정렬** 순서에 걸어요. `list_gateway_targets` 응답 순서는 보장되지
        # 않으므로 원래 순서에 상한을 걸면 Target 이 40개를 넘는 순간 매 호출마다 다른
        # 부분집합의 도구 목록을 읽어요. 읽지 못한 Target 은 이름까지만 확인하니, 그 부분집합이
        # 흔들리면 `excluded_actions` 가 흔들리고 → 문장이 흔들리고 → 매 provisioning 이 새
        # 리비전을 만들어요(리비전 churn). 정렬하면 같은 인벤토리에서 항상 같은 답이 나와요.
        ordered = sorted(
            (
                item for item in items
                if str(item.get("name") or "").strip()
                and (
                    needed_targets is None
                    or str(item.get("name")).strip() in needed_targets
                )
            ),
            key=lambda item: str(item.get("name")).strip(),
        )
        skipped_unneeded = len(names) - len(ordered)
        for index, item in enumerate(ordered):
            name = str(item.get("name")).strip()
            if index >= _LIVE_TOOL_SCHEMA_MAX_TARGETS:
                unread.append(name)
                continue
            try:
                detail = self._c.get_gateway_target(
                    gatewayIdentifier=gateway_id,
                    targetId=str(item.get("targetId") or ""),
                )
            except Exception:  # noqa: BLE001
                unread.append(name)
                continue
            tools = _inline_tool_names(detail.get("targetConfiguration"))
            if tools is None:
                # 연결형(`mcpServer`)은 Target 설정에 도구 목록이 없어요 — 상류가 주는 것이
                # 전부예요. 없는 것으로 접지 않고 미관측으로 둬요.
                unread.append(name)
                continue
            tools_by_target[name] = tools
        if skipped_unneeded:
            # 조용히 자르지 않아요. 다만 이건 «관측 실패»가 아니라 「결과에 영향이 없어서 안
            # 읽었다」예요 — 선언되지 않은 Target 의 action 은 `_is_live` 대상이 아니거든요.
            # 그래서 `live_inventory_status` 는 건드리지 않고 로그로만 남겨요.
            _log.info(
                "live gateway inventory: 선언되지 않은 Target %s개의 도구 목록은 "
                "읽지 않았어요(결과 동일)", skipped_unneeded,
            )
        reason = ""
        if unread:
            reason = (
                "라이브 도구 목록을 읽지 못한 Target 이 있어 그 Target 은 이름까지만 "
                "확인했어요: " + ", ".join(sorted(unread))
            )
        return LiveGatewayInventory(
            targets=names,
            tools_by_target=tools_by_target,
            tool_lists_complete=not unread,
        ), reason

    def _list_gateway_targets(self, gateway_id: str) -> list[dict]:
        # 응답 키는 `items` 예요 (`runtime/deploy/aws_adapter.py:1369-1374` 와 같은 키).
        out: list[dict] = []
        kwargs: dict = {"gatewayIdentifier": gateway_id, "maxResults": 50}
        seen: set[str] = set()
        while True:
            page = self._c.list_gateway_targets(**kwargs)
            if "items" not in page:
                raise RuntimeError(
                    "list_gateway_targets 응답에 `items` 가 없어요. 실제 키: "
                    f"{sorted(k for k in page if k != 'ResponseMetadata')}"
                )
            out.extend(page.get("items") or [])
            token = str(page.get("nextToken") or "")
            if not token:
                return out
            if token in seen:
                # 잘린 인벤토리로 라이브 필터를 돌리면 살아 있는 Target 을 「없다」로 읽어
                # 열거에서 빼요. 관측 불가로 올려서 `live_inventory_status=unknown` 이 되게 해요.
                raise RuntimeError(
                    "list_gateway_targets 페이지 토큰이 반복돼요 — "
                    "Target 목록을 완전히 관측하지 못했어요."
                )
            seen.add(token)
            kwargs["nextToken"] = token

    # ── identity 원장 관측 (독립 소유자) ────────────────────────────────
    @staticmethod
    def _binding_key(binding: AgentToolBinding) -> tuple[str, str, str, str]:
        return (
            binding.agent_record_id,
            binding.asset_id,
            binding.asset_version,
            binding.operation_id,
        )

    def observe_policy_ledger(
        self,
    ) -> tuple[
        tuple[AgentToolBinding, ...] | None,
        tuple[DomainPolicyRule, ...] | None,
        str,
    ]:
        """④·② 원장을 소비자와 같은 Query 경로로 읽어요.

        전체 snapshot은 agent partition을 발견하는 데만 쓰고, 기대값 자체는 각 partition의
        `list_agent_tool_bindings` 결과예요. snapshot과 Query key가 다르면 scan이 본 행을
        소비자가 못 읽는 상태라 부분 기대값으로 계속하지 않아요.
        """
        try:
            snapshot = self._store.get_agent_authorization_ledger_snapshot()
            agent_ids = sorted({
                binding.agent_record_id
                for binding in snapshot.tool_bindings
            } | {
                identity.agent_record_id
                for identity in snapshot.identities
            })
            bindings = tuple(
                binding
                for agent_id in agent_ids
                for binding in self._store.list_agent_tool_bindings(agent_id)
            )
            snapshot_keys = {
                self._binding_key(binding)
                for binding in snapshot.tool_bindings
            }
            queried_keys = {
                self._binding_key(binding)
                for binding in bindings
            }
            if snapshot_keys != queried_keys:
                return None, None, (
                    "④ binding 원장의 전체 관측과 소비자 Query 결과가 달라요."
                )
            rules = tuple(self._store.list_domain_policy_rules())
        except Exception as exc:
            return None, None, (
                "공유 정책 기대 원장을 읽지 못했어요: "
                f"{type(exc).__name__}: {exc}"
            )
        return bindings, rules, ""

    # ── provisioning ──────────────────────────────────────────────────────
    def provision(
        self,
        *,
        gateway_id: str,
        engine_id: str,
        spec_without_interceptor: SharedGatewayPolicySpec,
        created_by: str,
    ) -> ProvisionReport:
        """공유 정책을 원하는 상태로 맞춰요.

        `spec_without_interceptor` 의 interceptor·④ binding·② rule 값은 신뢰하지 않아요.
        이 메서드가 각 소유자에서 관측해 덮어써요. 호출자가 손으로 넣으면 subject가 자기
        기대값을 정하는 셈이에요.
        """
        attached, observe_reason = self.observe_interceptor(gateway_id)
        report = ProvisionReport(
            gateway_arn=spec_without_interceptor.gateway_arn,
            revision=0,
            interceptor_attached=attached,
        )
        if observe_reason:
            report.warnings.append(observe_reason)

        bindings, rules, ledger_reason = self.observe_policy_ledger()
        if ledger_reason:
            report.ok = False
            report.verdict = "unknown"
            report.reason = ledger_reason
            return report

        # 아직 아무것도 없는 엔진 — 컴파일러의 「빈 집합으로 교체하지 않아요」 가드가 지킬
        # 리비전이 없는 상태예요.
        #
        # 그 가드는 관측 실패가 **살아 있는** 리비전을 덮는 걸 막아요. 정책이 0장이면 그
        # 사고가 성립하지 않는데, 거부로 두면 신규 계정의 첫 MCP 배포가 `registering_target`
        # 에서 영구히 막혀요 — 도구 인가 승인은 배포 **후** 절차라서 순환이에요.
        #
        # ⚠️ 여기서 조기 반환하지 않아요. compile 앞에서 빠져나가면 컴파일러의 선행 검사
        # (gateway ARN·scope 접두어 충돌·Target 이름 중복·interceptor 부착·원장 관측 여부)를
        # 전부 건너뛰고, 그 검사들을 이 자리에 손으로 복제해야 해요. 대신 **그 가드 하나만**
        # 끄는 플래그를 넘겨서 나머지 검사와 삭제·멱등 판정은 정상 경로를 그대로 타요.
        #
        # 판정 근거는 이 계층이 소유해요(컴파일러는 순수 함수라 AWS 를 볼 수 없어요).
        # `_owned_policies` 가 아니라 `_list_policies` 로 **계열 무관** 전량을 봐요 —
        # `Gateway_{hash}_` 만 세면 `DomainRule_*` 같은 다른 계열의 잔존 permit 을 「없음」으로
        # 읽어서, 폐기된 permit 이 살아 있는데도 빈 집합을 성공으로 보고해요.
        allow_empty_declaration = False
        if not bindings and not rules:
            try:
                allow_empty_declaration = not self._list_policies(engine_id)
            except Exception as exc:
                report.ok = False
                report.verdict = "unknown"
                report.reason = (
                    "기존 정책 목록을 읽지 못해 아무것도 바꾸지 않았어요: "
                    f"{type(exc).__name__}: {exc}"
                )
                return report

        inventory, inventory_reason = self.observe_live_gateway_inventory(
            gateway_id, needed_targets=_declared_target_names(bindings)
        )
        report.live_inventory_reason = inventory_reason

        from dataclasses import replace

        spec = replace(
            spec_without_interceptor,
            request_interceptor_attached=attached,
            tool_bindings=bindings,
            domain_policy_rules=rules,
            live_gateway_inventory=inventory,
        )
        # 라이브 관측 상태와 제외 공시는 **거부 경로에도** 실어요. 거부 사유가 라이브 필터
        # 때문일 수 있는데, 그때 두 필드가 빈 채로 나가면 리포트가 「아무것도 안 뺐다」고
        # 거짓을 말해요.
        inventory_status = "unknown"
        if inventory is not None:
            inventory_status = (
                "observed" if inventory.tool_lists_complete else "partial"
            )
        report.live_inventory_status = inventory_status
        try:
            compiled = compile_shared_gateway_policies(
                spec, allow_empty_declaration=allow_empty_declaration
            )
        except InvalidPolicyInput as exc:
            report.ok = False
            report.verdict = "refused"
            report.reason = str(exc)
            # 컴파일러가 실어 보낸 값을 그대로 써요 — 여기서 다시 계산하면 로직이 두 곳으로
            # 갈려요(처음엔 Registry action 전량으로 과대 보고하고 승인 부분집합은 비웠어요).
            report.excluded_actions = list(
                getattr(exc, "excluded_actions", ()) or ()
            )
            report.excluded_approved_actions = list(
                getattr(exc, "excluded_approved_actions", ()) or ()
            )
            return report
        report.live_inventory_status = compiled.live_inventory_status
        report.excluded_actions = list(compiled.excluded_actions)
        report.excluded_approved_actions = list(
            compiled.excluded_approved_actions
        )

        try:
            existing = self._owned_policies(engine_id, compiled.gateway_arn)
        except Exception as exc:
            report.ok = False
            report.verdict = "unknown"
            report.reason = (
                f"기존 정책 목록을 읽지 못해 아무것도 바꾸지 않았어요: "
                f"{type(exc).__name__}: {exc}"
            )
            return report

        report.stale_revisions = self._stale_revision_names(existing)

        revision = self._next_revision(existing) if existing else 1
        report.revision = revision

        # ⚠️ ② 실재 확인은 `_already_current` **앞**에 있어야 해요.
        #
        # ① 리비전이 이미 0장이면 `_already_current([], ())` 가 True 라 `unchanged` 로 즉시
        # 반환해요. 그 상태에서 원장이 ACTIVE 라고 말한 ② 정책이 엔진에 실제로 없으면
        # **전면 거부인데 `ok=True, verdict="unchanged"`** 예요 — 삭제 전환 때 한 번만 보고
        # 그 뒤로는 영원히 안 보는 게 문제예요(codex 리뷰 P2).
        #
        # `allow_empty_declaration` 인 경우는 빼요. 이 검사는 「원장이 ACTIVE 라고 말한 ②
        # 정책」이 실재하는지 대조하는 건데, 그때 원장은 ② 를 하나도 주장하지 않아요(`rules`
        # 가 비어 있고 그게 이 플래그의 전제예요). 기대 집합이 없으면
        # `_observe_domain_rule_policies` 는 정의상 `False` 라, 검사가 아니라 무조건 거부가
        # 돼요.
        if not compiled.policies and not allow_empty_declaration:
            live_rules, why = self._observe_domain_rule_policies(
                engine_id, compiled.gateway_arn, rules
            )
            if not live_rules:
                report.ok = False
                report.verdict = "refused"
                report.reason = (
                    "열거가 0건인 이유가 「ACTIVE ② 도메인 규칙이 선언을 전부 덮음」인데 "
                    f"그 ② 정책이 엔진에 살아 있는지 확인하지 못했어요: {why}"
                )
                return report

        # 원하는 상태가 이미 라이브면 아무것도 하지 않아요(멱등). hash 로 비교해요 —
        # 정책 장수만 세면 내용이 달라도 같다고 읽어요.
        if self._already_current(engine_id, existing, compiled):
            report.verdict = "unchanged"
            report.unchanged = [p.policy_key for p in compiled.policies]
            # 아무것도 안 만들었으니 `max+1` 은 **존재하지 않는 번호**예요. 그대로 실으면
            # 화면·로그가 라이브에 없는 리비전을 가리켜요(IH-154 관측 신호의 함정).
            report.revision = max(
                (self._revision_of(name) for name, _ in existing), default=0
            )
            return report

        resumable = self._latest_resumable_revision(
            engine_id,
            existing,
            compiled,
        )
        if resumable is not None:
            revision, observed = resumable
            report.revision = revision
            try:
                for name, policy_id, status in observed:
                    if status != "ACTIVE":
                        self._wait_active(
                            engine_id,
                            policy_id,
                            name,
                            report.activation_findings,
                        )
            except PolicyActivationPending as exc:
                report.ok = False
                report.verdict = "provisioning"
                report.reason = str(exc)
                report.unchanged = [p.policy_key for p in compiled.policies]
                return report
            except Exception as exc:
                report.ok = False
                report.verdict = "create_failed"
                report.reason = f"{name}: {type(exc).__name__}: {exc}"
                return report
            report.unchanged = [p.policy_key for p in compiled.policies]
        else:
            created: list[tuple[str, str]] = []
            for policy in compiled.policies:
                name = policy_name(
                    compiled.gateway_arn,
                    revision,
                    policy.policy_key,
                )
                try:
                    policy_id = self._create_active(
                        engine_id,
                        name,
                        policy.cedar_policy,
                        report.activation_findings,
                    )
                except PolicyActivationPending as exc:
                    # 새 정책은 이미 생성됐어요. 옛 ACTIVE 리비전과 새 CREATING 리비전을
                    # 모두 남겨 다음 재시도가 같은 리비전을 관측·수렴하게 해요. 여기서 보상
                    # 삭제하면 단순한 활성화 지연을 create 실패로 바꿔요.
                    report.ok = False
                    report.verdict = "provisioning"
                    report.reason = str(exc)
                    report.created.append(name)
                    return report
                except Exception as exc:
                    # 만든 것만 되돌려요. 옛 리비전은 손대지 않았으니 강제 상태는 그대로예요.
                    report.ok = False
                    report.verdict = "create_failed"
                    report.reason = f"{name}: {type(exc).__name__}: {exc}"
                    report.warnings.extend(
                        self._rollback(engine_id, [pid for _n, pid in created])
                    )
                    return report
                created.append((name, policy_id))
                report.created.append(name)

        # 새 리비전이 전부 ACTIVE 인 뒤에만 옛 리비전을 지워요. 순서를 뒤집으면 4~5초
        # 전면 거부 창이 열려요(§10 실측).
        #
        # `compiled.policies == ()` 인 합법 케이스(선언 전량이 ACTIVE ② 에 덮임)는 위에서
        # ② 실재를 이미 확인했어요 — 그 확인 없이는 여기 도달하지 않아요.
        for name, policy_id in existing:
            if self._revision_of(name) == revision:
                continue
            deleted, warning = self._delete_confirmed(engine_id, policy_id)
            if deleted:
                report.deleted.append(name)
            if warning:
                report.warnings.append(warning)
        if not compiled.policies:
            # 우리 소유 리비전을 전부 지웠으니 `max+1` 은 존재하지 않는 번호예요 —
            # `unchanged` 경로와 같은 팬텀이에요(ADR-0107 결정 6).
            report.revision = 0

        _log.info(
            "shared policy provisioned gateway=%s revision=%s created=%s deleted=%s by=%s",
            compiled.gateway_arn, revision, len(report.created),
            len(report.deleted), created_by,
        )
        return report

    # ── 수렴 (IH-154) ─────────────────────────────────────────────────────
    def reclaim_precheck(
        self, *, engine_id: str, gateway_arn: str
    ) -> ReclaimReport | None:
        """회수할 게 있나만 먼저 봐요 — 없으면 완성된 리포트, 있으면 `None`.

        `list_policies` 한 번이에요. 호출자가 이걸 통과한 뒤에만 무거운 spec(APPROVED
        registry 전량 열거)을 만들면, 회수할 게 없는 대부분의 주기 비용이 그 한 번이에요.
        """
        report = ReclaimReport(gateway_arn=gateway_arn)
        try:
            existing = self._owned_policies(engine_id, gateway_arn)
        except Exception as exc:  # noqa: BLE001
            report.ok = False
            report.verdict = "unknown"
            report.reason = (
                f"정책 목록을 읽지 못했어요: {type(exc).__name__}: {exc}"
            )
            return report
        report.stale_revisions = self._stale_revision_names(existing)
        revisions = {self._revision_of(name) for name, _ in existing}
        if len(revisions) <= 1:
            report.verdict = "not_applicable"
            report.surviving_revision = max(revisions, default=0)
            return report
        return None

    def reclaim_stale_revisions(
        self,
        *,
        gateway_id: str,
        engine_id: str,
        spec_without_interceptor: SharedGatewayPolicySpec,
    ) -> ReclaimReport:
        """옛 ACTIVE 리비전만 **지우는** 수렴 pass 예요. 절대 만들지 않아요.

        ## 왜 별 메서드인가

        `provision()` 안의 pre-pass 로 넣으면 `_next_revision` 이 prune 뒤에 `max+1` 을
        다시 계산해서 재시도 리비전 재사용 계약이 깨져요
        (`test_retry_reuses_the_pending_revision_and_finishes_the_replacement` 의
        `second.revision == 2`).

        ## 왜 만들지 않는가

        `create_policy` 는 같은 이름을 `ConflictException: Policy with the same name
        already exists` 로 거부해요(2026-09-05 임시 엔진 실측). `_next_revision` 은 락도
        캐시도 없이 매번 `max+1` 을 계산하니, 무인 pass 가 만들기까지 하면 동시 어드민
        승인과 반드시 같은 번호를 계산해 한쪽이 `create_failed` 로 떨어져요. 만들지 않으면
        그 경합이 **구조적으로 없어요.** 새 리비전이 필요한 상태는 `provision()` 의 일이에요.

        ## 지우기 전 확인

        ⛔ 옛 리비전을 지우기 전에 **최신 리비전이 ACTIVE 이고 그 라이브 문장이 지금 원장
        산출물과 같은지** 확인해요. 최신이 `CREATE_FAILED` 인데 아래를 지우면 정책이 0장이
        되어 전면 거부예요 — 삭제 루프를 pending 반환 앞으로 옮기는 금지 사항과 같은 결과예요.
        """
        report = ReclaimReport(gateway_arn=spec_without_interceptor.gateway_arn)
        try:
            existing = self._owned_policies(
                engine_id, spec_without_interceptor.gateway_arn
            )
        except Exception as exc:  # noqa: BLE001
            report.ok = False
            report.verdict = "unknown"
            report.reason = (
                f"정책 목록을 읽지 못했어요: {type(exc).__name__}: {exc}"
            )
            return report

        report.stale_revisions = self._stale_revision_names(existing)
        revisions = {self._revision_of(name) for name, _ in existing}
        # 「1장이 아니면」이 아니라 **엄격히 둘 이상**이에요. 0장은 ②-전용 Gateway 의 정상
        # 상태라 여기서 손댈 게 없어요.
        if len(revisions) <= 1:
            report.verdict = "not_applicable"
            report.surviving_revision = max(revisions, default=0)
            return report

        attached, observe_reason = self.observe_interceptor(gateway_id)
        if observe_reason:
            report.warnings.append(observe_reason)
        bindings, rules, ledger_reason = self.observe_policy_ledger()
        if ledger_reason:
            report.ok = False
            report.verdict = "unknown"
            report.reason = ledger_reason
            return report

        # `provision()` 과 **같은** 입력으로 컴파일해야 해요. 라이브 관측을 빼면 이 pass 가
        # 계산한 「원하는 상태」가 `provision()` 것과 달라져서, 최신 리비전이 정상인데도
        # 「문장이 다르다」로 영원히 `blocked` 이 돼요.
        inventory, inventory_reason = self.observe_live_gateway_inventory(
            gateway_id, needed_targets=_declared_target_names(bindings)
        )
        # ⚠️ `warnings` 가 아니라 별 필드예요 — 부분 관측은 실패가 아니에요. 여기 report 가
        # `shared_policy_trigger` 를 타지는 않지만, 「관측을 warning 으로 내지 않는다」는
        # 규약을 두 경로에서 다르게 쓰면 다음 사람이 한쪽을 보고 반대로 배워요.
        report.live_inventory_reason = inventory_reason
        report.live_inventory_status = (
            "unknown" if inventory is None
            else ("observed" if inventory.tool_lists_complete else "partial")
        )

        from dataclasses import replace

        try:
            compiled = compile_shared_gateway_policies(replace(
                spec_without_interceptor,
                request_interceptor_attached=attached,
                tool_bindings=bindings,
                domain_policy_rules=rules,
                live_gateway_inventory=inventory,
            ))
        except InvalidPolicyInput as exc:
            report.ok = False
            report.verdict = "blocked"
            report.reason = (
                "지금 원장으로는 원하는 상태를 컴파일할 수 없어 아무것도 지우지 않았어요: "
                f"{exc}"
            )
            return report

        if not compiled.policies:
            # ②-전량-덮임 상태에서의 ① 전량 삭제는 `provision()` 이 소유해요 — 거기에만
            # ② 실재 확인 게이트가 있어요. 수렴 pass 가 이 판단을 흉내내지 않아요.
            report.ok = False
            report.verdict = "blocked"
            report.reason = (
                "원하는 상태가 ① 정책 0장이에요. 이 전환은 provision() 이 ② 실재 확인과 "
                "함께 소유해요."
            )
            return report

        top = max(revisions)
        report.surviving_revision = top
        # 최신 리비전의 **상태**를 먼저 읽어요. 문장 비교로 뭉개면 `CREATE_FAILED` 가
        # 「문장이 다르다」로 보고돼 진단이 틀려요 — 어드민이 원장을 의심하게 돼요.
        try:
            observed = [
                (
                    name,
                    self._c.get_policy(
                        policyEngineId=engine_id, policyId=policy_id
                    ),
                )
                for name, policy_id in existing
                if self._revision_of(name) == top
            ]
        except Exception as exc:  # noqa: BLE001 - 관측 실패는 통과가 아니에요.
            report.ok = False
            report.verdict = "unknown"
            report.reason = (
                f"최신 리비전 상태를 읽지 못했어요: {type(exc).__name__}: {exc}"
            )
            return report
        not_active = [
            f"{name}={detail.get('status')}"
            for name, detail in observed
            if str(detail.get("status") or "") != "ACTIVE"
        ]
        if not_active:
            report.ok = False
            report.verdict = "blocked"
            report.reason = (
                "최신 리비전이 아직 ACTIVE 가 아니라 옛 리비전을 지우지 않았어요: "
                + ", ".join(not_active)
            )
            return report
        want = {
            _name_safe(policy.policy_key): normalize_cedar(policy.cedar_policy)
            for policy in compiled.policies
        }
        live = {
            self._policy_key_of(name, compiled.gateway_arn): normalize_cedar(
                str(((detail.get("definition") or {}).get("cedar") or {})
                    .get("statement") or "")
            )
            for name, detail in observed
        }
        if live != want:
            report.ok = False
            report.verdict = "blocked"
            # 라이브를 못 읽었으면 「원장과 다르다」가 아니라 **우리 관측이 부족했다** 예요.
            # 그 구분 없이 원장을 지목하면 어드민이 원장을 의심해요(codex 리뷰 P3).
            report.reason = (
                "최신 리비전의 라이브 문장이 지금 원장 산출물과 달라요 — 좁히는 회수는 "
                "새 리비전을 만드는 provision() 이 해야 해요."
                if report.live_inventory_status == "observed"
                else "최신 리비전의 라이브 문장이 이번 컴파일 결과와 달라요. 다만 라이브 "
                     f"Gateway 관측이 `{report.live_inventory_status}` 라 원장 쪽 차이인지 "
                     f"관측 부족인지 확정할 수 없어 아무것도 지우지 않았어요: "
                     f"{report.live_inventory_reason}"
            )
            return report

        survivor_ids = [
            policy_id for name, policy_id in existing
            if self._revision_of(name) == top
        ]
        stale = [
            (name, policy_id) for name, policy_id in existing
            if self._revision_of(name) != top
        ]
        for name, policy_id in stale:
            # ⚠️ **삭제마다 생존자가 아직 있는지 다시 확인해요.** 검증과 삭제 사이에 다른
            # 주체(어드민의 `provision()`·손 삭제)가 최신 리비전을 지울 수 있어요. 그러면
            # 우리가 아래를 지워 **정책 0장** 이 되고, 그건 삭제 루프를 pending 앞으로 옮기는
            # 금지 사항과 같은 전면 거부예요(codex 리뷰 P1).
            gone, why = self._survivor_missing(engine_id, survivor_ids)
            if gone:
                report.ok = False
                report.verdict = "blocked"
                report.reason = (
                    "생존 리비전이 검증 뒤 사라져서 남은 옛 리비전을 지우지 않았어요 "
                    f"(r{top}): {why}"
                )
                return report
            deleted, warning = self._delete_confirmed(engine_id, policy_id)
            if deleted:
                report.deleted.append(name)
            if warning:
                report.warnings.append(warning)
        # 「지웠다고 관측한 것」이 「지우려던 것」과 같을 때만 성공이에요. 하나라도 실패·
        # 미확인이면 가장 넓은 옛 리비전이 아직 이기고 있으니, 그 상태를 `reclaimed` 로
        # 보고하면 IH-154 그 자체를 성공으로 적는 셈이에요(codex 리뷰 P2).
        if len(report.deleted) == len(stale) and not report.warnings:
            report.verdict = "reclaimed"
        else:
            report.ok = False
            report.verdict = "partial"
            report.reason = (
                f"옛 리비전 {len(stale)}개 중 {len(report.deleted)}개만 삭제를 확인했어요 — "
                "남은 리비전이 계속 Cedar permit 합집합에서 이겨요."
            )
        _log.info(
            "shared policy stale revisions gateway=%s verdict=%s surviving=r%s "
            "deleted=%s/%s",
            compiled.gateway_arn, report.verdict, top,
            len(report.deleted), len(stale),
        )
        return report

    def _survivor_missing(
        self, engine_id: str, survivor_ids: list[str]
    ) -> tuple[bool, str]:
        """생존 리비전이 엔진에서 사라졌나요. 못 읽으면 「사라짐」으로 봐요 (fail-closed).

        여기서 「모르면 진행」을 고르면 관측 실패가 곧 전면 거부가 될 수 있어요 — 삭제는
        되돌릴 수 없으니 모를 때는 멈춰요.
        """
        try:
            present = {
                str(item.get("policyId") or item.get("id") or "")
                for item in self._list_policies(engine_id)
            }
        except Exception as exc:  # noqa: BLE001
            return True, f"생존 확인 불가: {type(exc).__name__}: {exc}"
        missing = [pid for pid in survivor_ids if pid not in present]
        if missing:
            return True, "엔진에서 사라졌어요: " + ", ".join(missing)
        return False, ""

    # ── 내부 ──────────────────────────────────────────────────────────────
    def _owned_policies(
        self, engine_id: str, gateway_arn: str
    ) -> list[tuple[str, str]]:
        """이 Gateway 용 공유 정책만 (이름, id) 로 돌려줘요."""
        prefix = f"{_NAME_PREFIX}{_gateway_hash(gateway_arn)}_"
        out: list[tuple[str, str]] = []
        for item in self._list_policies(engine_id):
            name = str(item.get("name") or "")
            if name.startswith(prefix):
                out.append((name, str(item.get("policyId") or item.get("id") or "")))
        return out

    def _list_policies(self, engine_id: str) -> list[dict]:
        # 응답 키는 `policies` 예요. `items` 를 읽으면 값이 있어도 빈 목록이 나오고,
        # 그 "0건" 이 "옛 리비전 없음" 으로 읽혀 정리가 조용히 누락돼요.
        items: list[dict] = []
        kwargs: dict = {"policyEngineId": engine_id, "maxResults": 50}
        seen: set[str] = set()
        while True:
            page = self._c.list_policies(**kwargs)
            if "policies" not in page:
                raise RuntimeError(
                    "list_policies 응답에 `policies` 가 없어요. 실제 키: "
                    f"{sorted(k for k in page if k != 'ResponseMetadata')}"
                )
            items.extend(page.get("policies") or [])
            token = str(page.get("nextToken") or "")
            if not token:
                return items
            if token in seen:
                # 반복 토큰을 「끝」으로 접으면 목록이 조용히 잘려요 — 그 잘린 목록으로 부재를
                # 인증하면(`_policy_absent`) 살아 있는 정책을 없다고 말해요(codex 리뷰 P2).
                raise RuntimeError(
                    "list_policies 페이지 토큰이 반복돼요 — 목록을 완전히 관측하지 못했어요."
                )
            seen.add(token)
            kwargs["nextToken"] = token

    @staticmethod
    def _revision_of(name: str) -> int:
        tail = name.rsplit("_r", 1)
        if len(tail) != 2 or not tail[1].isdigit():
            return 0
        return int(tail[1])

    def _next_revision(self, existing: list[tuple[str, str]]) -> int:
        return max((self._revision_of(n) for n, _ in existing), default=0) + 1

    def _stale_revision_names(
        self, existing: list[tuple[str, str]]
    ) -> list[str]:
        """소유 리비전이 둘 이상이면 최신 미만 리비전 이름을 돌려줘요 (IH-154).

        「둘 이상」이 기준이에요 — **0장은 정상 상태**예요(선언 전량이 ACTIVE ② 규칙에
        덮이면 ① 리비전이 없는 게 맞아요). 「1장이 아니면」으로 잡으면 ②-전용 Gateway 에서
        영원히 신호가 켜져요.
        """
        revisions = sorted(
            {self._revision_of(name) for name, _ in existing}
        )
        if len(revisions) <= 1:
            return []
        top = revisions[-1]
        return sorted(
            name for name, _ in existing if self._revision_of(name) != top
        )

    def _observe_domain_rule_policies(
        self,
        engine_id: str,
        gateway_arn: str,
        rules: tuple[DomainPolicyRule, ...],
    ) -> tuple[bool, str]:
        """원장이 ACTIVE 라고 말한 ② 정책이 **엔진에** 살아 있고, **그 action 을 실제로
        열거하는지** 확인해요.

        기대값의 소유자는 원장(`remote_policy_id`·`gateway_action`)이고 관측의 소유자는
        엔진이에요. 둘이 같은 곳에서 오면 검사가 아니에요(ADR-0037 §4).

        ⚠️ **존재와 ACTIVE 만 보면 부족해요** (codex 리뷰 P1). 원장의 규칙 A 가 정책 P 를
        가리키는데 P 가 밖에서 B 를 허용하도록 갱신됐으면, P 는 존재하고 ACTIVE 지만 A 는
        아무도 안 덮어요. 그 상태에서 ① 마지막 리비전을 지우면 A 가 거부돼요. 그래서 라이브
        statement 가 그 규칙의 `gateway_action` 을 담고 있는지까지 봐요.

        `(살아 있음, 이유)` 를 돌려줘요. 기대 집합이 비어 있거나 하나라도 없거나 ACTIVE 가
        아니거나 그 action 을 안 담으면 `False` 예요 — 「관측 못 함」을 통과로 접지 않아요.
        """
        expected: dict[str, set[str]] = {}
        for rule in rules:
            if (
                rule.gateway_arn.strip() != gateway_arn
                or rule.observed_status.strip().upper() != "ACTIVE"
                or rule.enforcement_mode.strip().upper() != "ACTIVE"
                or not rule.remote_policy_id.strip()
            ):
                continue
            expected.setdefault(rule.remote_policy_id.strip(), set()).add(
                rule.gateway_action.strip()
            )
        if not expected:
            return False, (
                "원장에 ACTIVE ② 도메인 규칙이 0건이에요 — 열거 0건을 정당화할 근거가 없어요"
            )
        try:
            present = {
                str(item.get("policyId") or item.get("id") or "")
                for item in self._list_policies(engine_id)
            }
        except Exception as exc:  # noqa: BLE001 - 관측 실패는 통과가 아니에요.
            return False, f"엔진 정책 목록을 읽지 못했어요: {type(exc).__name__}: {exc}"
        missing = [pid for pid in sorted(expected) if pid not in present]
        if missing:
            return False, (
                "원장이 ACTIVE 라고 한 ② 정책이 엔진에 없어요: " + ", ".join(missing)
            )
        problems: list[str] = []
        for policy_id, actions in sorted(expected.items()):
            try:
                detail = self._c.get_policy(
                    policyEngineId=engine_id, policyId=policy_id
                )
            except Exception as exc:  # noqa: BLE001
                return False, (
                    f"② 정책 상태를 읽지 못했어요 {policy_id}: {type(exc).__name__}: {exc}"
                )
            status = str(detail.get("status") or "")
            if status != "ACTIVE":
                problems.append(f"{policy_id}={status}")
                continue
            statement = str(
                ((detail.get("definition") or {}).get("cedar") or {})
                .get("statement") or ""
            )
            # **이 Gateway** 를 가리키는지도 봐요. 같은 action 을 열거하면서 `resource` 가 다른
            # gateway 면 우리 Gateway 의 그 도구는 아무도 덮지 않아요 — 그 상태로 ① 마지막
            # 리비전을 지우면 거부돼요(codex 리뷰 P1).
            #
            # ⚠️ per-policy enforcement mode 는 **존재하지 않아요.** 2026-09-05 실측으로
            # `get_policy` 응답 키는 createdAt·definition·name·policyArn·policyEngineId·
            # policyId·status·statusReasons·updatedAt 뿐이에요. ENFORCE/LOG_ONLY 는 gateway 의
            # `policyEngineConfiguration.mode` 축이라 정책 단위로 읽을 수 없어요 — 그 축은
            # `agora-gateway-is-m2oauth-enforce` 규약과 `cdk diff` 가 지켜요.
            if not _statement_targets_gateway(statement, gateway_arn):
                problems.append(
                    f"{policy_id} 의 resource 가 이 Gateway 가 아니에요"
                )
                continue
            enumerated = set(_CEDAR_ACTION_LITERAL_RE.findall(statement))
            uncovered = sorted(
                action for action in actions if action and action not in enumerated
            )
            if uncovered:
                problems.append(
                    f"{policy_id} 가 {', '.join(uncovered)} 를 열거하지 않아요"
                )
        if problems:
            return False, (
                "② 정책이 원장이 말한 상태가 아니에요: " + "; ".join(problems)
            )
        return True, ""

    def _latest_resumable_revision(
        self,
        engine_id: str,
        existing: list[tuple[str, str]],
        compiled: CompiledSharedGatewayPolicies,
    ) -> tuple[int, list[tuple[str, str, str]]] | None:
        """원하는 문장으로 이미 만들어진 최신 ACTIVE/CREATING 리비전을 찾아요.

        짧은 HTTP 예산이 끝난 뒤 다음 클릭에서 새 리비전을 또 만들면 매번 활성화 시계를
        처음부터 시작해 영원히 완료되지 않을 수 있어요. 최신 리비전의 **실제 문장**이 현재
        원장 산출물과 같을 때만 재사용하고, terminal 실패나 다른 문장이면 새 리비전으로
        진행해요.
        """
        if not existing or not compiled.policies:
            return None
        revision = max(self._revision_of(name) for name, _ in existing)
        candidates = [
            (name, policy_id)
            for name, policy_id in existing
            if self._revision_of(name) == revision
        ]
        if len(candidates) != len(compiled.policies):
            return None
        want = {
            _name_safe(policy.policy_key): normalize_cedar(policy.cedar_policy)
            for policy in compiled.policies
        }
        observed: list[tuple[str, str, str]] = []
        for name, policy_id in candidates:
            key = self._policy_key_of(name, compiled.gateway_arn)
            if key not in want:
                return None
            try:
                detail = self._c.get_policy(
                    policyEngineId=engine_id,
                    policyId=policy_id,
                )
            except Exception:
                return None
            status = str(detail.get("status") or "")
            if status != "ACTIVE" and not status.endswith("ING"):
                return None
            statement = str(
                ((detail.get("definition") or {}).get("cedar") or {})
                .get("statement") or ""
            )
            if normalize_cedar(statement) != want.pop(key):
                return None
            observed.append((name, policy_id, status))
        if want:
            return None
        return revision, observed

    def _already_current(
        self,
        engine_id: str,
        existing: list[tuple[str, str]],
        compiled: CompiledSharedGatewayPolicies,
    ) -> bool:
        """라이브 문장이 원하는 것과 같은지 **문장으로** 비교해요.

        장수만 세면 내용이 달라도 같다고 읽어요. 그러면 정책 갱신이 조용히 누락돼요.

        읽지 못하면 `False` 를 돌려요 — 모를 때는 다시 만들어요. 잘못 "같다" 고 하는 쪽이
        위험해요(갱신 누락), 잘못 "다르다" 고 하는 쪽은 리비전이 하나 늘 뿐이에요.
        """
        if len(existing) != len(compiled.policies):
            return False
        # `policy_key` 의 하이픈은 이름에서 `_` 로 정규화돼요(`coarse-gate` → `coarse_gate`).
        # 복원 비교도 같은 정규화를 거쳐야 짝이 맞아요.
        want = {
            _name_safe(policy.policy_key): normalize_cedar(policy.cedar_policy)
            for policy in compiled.policies
        }
        for name, policy_id in existing:
            key = self._policy_key_of(name, compiled.gateway_arn)
            if key not in want:
                return False
            try:
                detail = self._c.get_policy(
                    policyEngineId=engine_id, policyId=policy_id
                )
            except Exception:
                return False
            if str(detail.get("status") or "") != "ACTIVE":
                return False
            statement = str(
                ((detail.get("definition") or {}).get("cedar") or {})
                .get("statement") or ""
            )
            if normalize_cedar(statement) != want.pop(key):
                return False
        return not want

    @staticmethod
    def _policy_key_of(name: str, gateway_arn: str) -> str:
        """이름에서 `policy_key` 를 복원해요 (`Gateway_{hash}_{key}_r{n}`)."""
        prefix = f"{_NAME_PREFIX}{_gateway_hash(gateway_arn)}_"
        if not name.startswith(prefix):
            return ""
        body = name[len(prefix):]
        return body.rsplit("_r", 1)[0] if "_r" in body else body

    def _create_active(
        self,
        engine_id: str,
        name: str,
        cedar: str,
        findings: list[str] | None = None,
    ) -> str:
        """정책을 만들고 **종료 상태까지** 폴링해요.

        `create_policy` 200 은 검증 통과가 아니에요 — 없는 도구 이름은 그 뒤에
        `unrecognized action` 으로 거부돼요(2026-08-28 실측).
        """
        resp = self._c.create_policy(
            policyEngineId=engine_id,
            name=name,
            definition={"cedar": {"statement": cedar}},
            validationMode=VALIDATION_MODE,
        )
        policy_id = str(resp.get("policyId") or resp.get("id") or "")
        if not policy_id:
            raise RuntimeError(f"{name}: create_policy 응답에 policyId 가 없어요")
        self._wait_active(engine_id, policy_id, name, findings)
        return policy_id

    def _wait_active(
        self,
        engine_id: str,
        policy_id: str,
        name: str,
        findings: list[str] | None = None,
    ) -> None:
        """이미 만들어진 정책을 주어진 관측 예산 안에서 ACTIVE까지 따라가요.

        `ACTIVE` 에도 `statusReasons` 가 실릴 수 있어요 — 2026-09-05 실측: 도구를
        Target 에서 뺀 뒤 무관한 정상 정책을 만들면 `ACTIVE` 인데 「preexisting policy …
        found to not match the most recent tool input schema during analysis」가 와요.
        AWS 가 소유한 드리프트 관측이라 버리지 않고 리포트로 올려요(IH-140 탐지 신호).
        """
        for _ in range(self._active_max_polls):
            self._sleep(_POLL_SECONDS)
            detail = self._c.get_policy(
                policyEngineId=engine_id, policyId=policy_id
            )
            status = str(detail.get("status") or "")
            if status == "ACTIVE":
                if findings is not None:
                    findings.extend(
                        f"{name}: {reason}"
                        for reason in (detail.get("statusReasons") or ())
                        if str(reason).strip()
                    )
                return
            if status and not status.endswith("ING"):
                reasons = "; ".join(
                    str(r) for r in (detail.get("statusReasons") or ())
                )
                raise RuntimeError(f"{name}: 상태 {status}. {reasons}")
        raise PolicyActivationPending(
            policy_id=policy_id,
            policy_name=name,
            max_polls=self._active_max_polls,
        )

    def _delete_confirmed(
        self, engine_id: str, policy_id: str
    ) -> tuple[bool, str]:
        """지우고 목록에서 사라지는 것까지 봐요 (반영 지연 0.8~3.1초, §10).

        ⚠️ **이미 없는 정책은 「삭제 실패」가 아니에요.** IH-154 수렴 pass 가 두 번째 삭제
        주체로 들어와서(같은 non-top 리비전을 노려요) 무해한 이중 삭제가 흔해졌어요. 그걸
        실패로 읽으면 `warnings` 가 생기고, `shared_policy_trigger.py:45` 가 그 warning
        하나로 **건강한 Gateway 에서** 어드민 요청을 502 로 만들고 purge 는 registry record
        를 보존해요. AGENTS.md 의 「error 코드는 부재의 증거가 아니다」는 반대 방향도
        같아요 — 여기서는 부재를 목록 재조회로 확인해요.
        """
        try:
            self._c.delete_policy(policyEngineId=engine_id, policyId=policy_id)
        except Exception as exc:
            absent, absence_reason = self._policy_absent(engine_id, policy_id)
            if absent:
                # 다른 주체가 먼저 지웠어요 — 원하는 상태에 도달했으니 성공이에요.
                return True, ""
            return False, (
                f"옛 정책 삭제 실패 {policy_id}: {type(exc).__name__}: {exc}"
                + (f" ({absence_reason})" if absence_reason else "")
            )
        for _ in range(_DELETE_MAX_POLLS):
            self._sleep(_POLL_SECONDS)
            try:
                remaining = {
                    str(p.get("policyId") or p.get("id") or "")
                    for p in self._list_policies(engine_id)
                }
            except Exception as exc:
                return False, (
                    f"삭제는 수락됐지만 부재를 관측하지 못했어요 {policy_id}: "
                    f"{type(exc).__name__}: {exc}"
                )
            if policy_id not in remaining:
                return True, ""
        # ⚠️ **부재를 관측하지 못하면 `deleted=True` 가 아니에요.** 예전에는 True 를 돌려줘서
        # `report.deleted` 가 「지웠다」고 적었는데, 그 옛 리비전이 아직 ACTIVE 면 Cedar permit
        # 합집합에서 계속 이겨요 — 즉 IH-154 상태 그대로인데 리포트가 반대로 말해요. warning 은
        # 그대로 남아서 HTTP 경로는 이미 실패로 읽고, 이제 `deleted` 목록도 사실만 담아요
        # (codex 리뷰 P2). 삭제 자체를 다시 시도하지는 않아요 — 수락은 됐으니까요.
        return False, (
            f"삭제는 수락됐지만 부재를 확인하지 못했어요 {policy_id}. "
            "그 리비전이 아직 ACTIVE 면 계속 permit 합집합에서 이겨요."
        )

    def _policy_absent(
        self, engine_id: str, policy_id: str
    ) -> tuple[bool, str]:
        """엔진 목록을 다시 읽어 그 정책이 **없음**을 확인해요.

        `(부재, 이유)`. 목록을 못 읽으면 `(False, 이유)` 예요 — 못 본 것을 부재로 접지 않아요.
        """
        try:
            remaining = {
                str(item.get("policyId") or item.get("id") or "")
                for item in self._list_policies(engine_id)
            }
        except Exception as exc:  # noqa: BLE001
            return False, f"부재 확인 불가: {type(exc).__name__}: {exc}"
        return policy_id not in remaining, ""

    def _rollback(self, engine_id: str, policy_ids: list[str]) -> list[str]:
        warnings: list[str] = []
        for policy_id in policy_ids:
            try:
                self._c.delete_policy(
                    policyEngineId=engine_id, policyId=policy_id
                )
            except Exception as exc:
                warnings.append(
                    f"보상 삭제 실패 {policy_id}: {type(exc).__name__}: {exc}. "
                    "이 정책이 남아 있으면 다음 provisioning 이 리비전을 올려요."
                )
        return warnings


def normalize_cedar(text: str) -> str:
    """공백을 접어 문장을 비교 가능하게 해요. AWS 가 되돌려주는 문장은 우리가 보낸 것과
    공백이 다를 수 있어서, 원문 비교는 항상 "다르다" 로 나와요."""
    import re

    return re.sub(r"\s+", " ", text or "").strip()


def _name_safe(policy_key: str) -> str:
    import re

    return re.sub(r"[^A-Za-z0-9_]", "_", policy_key)


def _gateway_hash(gateway_arn: str) -> str:
    import hashlib

    return hashlib.sha256(gateway_arn.encode("utf-8")).hexdigest()[:10]


def policy_name(gateway_arn: str, revision: int, policy_key: str) -> str:
    """`Gateway_{hash10}_{key}_r{n}`.

    이름 규약은 `agent_policy_cutover.GatewayPolicyCutoverManager.policy_name` 과 같은
    형태예요. 그 모듈은 폐기됐지만 **이름은 소유권 표시**라서 바꾸면 옛 리비전을 못 찾아요.
    """
    key = _name_safe(policy_key)
    prefix = f"{_NAME_PREFIX}{_gateway_hash(gateway_arn)}_"
    suffix = f"_r{revision}"
    budget = max(1, 48 - len(prefix) - len(suffix))
    return f"{prefix}{key[:budget]}{suffix}"
