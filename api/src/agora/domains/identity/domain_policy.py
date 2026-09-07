"""도메인 규칙 Cedar 정책 — 「환불 금액이 100,000원을 넘으면 안 된다」 같은 값 조건이에요.

## 이 모듈이 존재하는 이유

Agora 의 도구 호출 인가는 정확히 두 층, ④ agent tool binding 과 ⑦ 사람·그룹 grant예요.
둘 다 **REQUEST interceptor** 가 DynamoDB 원장을 읽어서 판정해요
(ADR-0099, `docs/research/2026-08-31-agentcore-interceptor-official-guidance.md` §4).
Cedar 는 ④·⑦ 판정에서 빠졌어요 — 외부 원장을 조회할 수 없으니 같은 판정을 못 해요.

그럼 Cedar 를 왜 남기나요. **Cedar 만 볼 수 있는 것이 하나 있어요 — 요청 본문의 인자 값**
이에요. `context.input` 이 그거예요. 그래서 Cedar 에는 「누가 무엇을 부를 수 있나」를 넣지
않고 **도메인 규칙만** 선택적으로 담아요(`docs/design/tool-authn-authz-final.html` §10).

## 왜 `permit` 만 만드나 — `forbid` 는 뚫려요

직관적으로는 「금액이 10만원을 넘으면 금지」라고 쓰고 싶어요. 그렇게 쓰면 안 돼요.

```cedar
/* ✗ 이렇게 쓰면 봇이 인자 이름만 바꿔도 통과해요 */
forbid(...) when { context.input.amount > 100000 };
```

`context.input` 은 **열린 레코드**예요. Gateway 가 도구 스키마에 없는 인자도 그대로 넘겨요
(`docs/research/2026-08-26-ia45f-cedar-order-scope-runtime.md` §5 — 인자를 주입해서 Cedar
판정이 DENY→ALLOW 로 뒤집힌 실측이 있어요). 그래서 `amount` 를 아예 빼거나 `Amount` 로
보내면 위 `when` 절이 평가 오류가 되고, **오류가 난 `forbid` 는 적용되지 않아** 요청이 통과해요.
금지가 조용히 사라지는 거예요.

```cedar
/* ✓ 없으면 default-deny 로 떨어져요 (fail-closed) */
permit(...) when { context.input has amount && context.input.amount <= 100000 };
```

`permit` 은 반대예요. 인자가 없으면 `has` 가드가 거짓이 되어 이 문장이 안 맞고, 맞는 permit
이 하나도 없으면 Cedar 는 **기본 거부**예요. 그래서 이 모듈은 `permit` 만 만들고, `has`
가드를 **반드시** 붙여요. 가드 생성이 빠졌는지는 조립 결과를 다시 읽어서 확인해요
(`assert_fail_closed_shape`) — 만드는 쪽과 검사하는 쪽을 나눠 뒀어요(ADR-0037 §4).

## 이름이 1글자만 달라도 정책은 무력해요

`has` 가드는 fail-closed 를 주는 대신 **인자 이름이 정확히 일치할 때만** 규칙이 걸린다는
성질을 함께 가져와요. `amount` 를 `Amount` 로 적으면 그 permit 은 아무 요청에도 안 맞고,
그러면 **다른 permit**(굵은 문)이 요청을 통과시켜요. 즉 오타는 「거부」가 아니라 「규칙 없음」
이에요. 화면이 이걸 접지 말고 항상 말해야 해서, 인자 목록을 MCP 자산 원장의 `inputSchema`
에서 읽어 **고르게** 하고, 못 읽으면 자유 입력을 허용하되 관측 실패를 그대로 표시해요.

## 굵은 문이 이미 열어놓으면 이 정책은 장식이에요

Cedar 의 ACTIVE `permit` 은 **합집합**이에요. 굵은 문
(`permit(principal is OAuthUser, action, resource == GW) when { hasTag("scope") }`)이 모든
action 을 허용하는 동안에는 도메인 정책을 추가해도 권한이 좁아지지 않아요. 그래서 만들기 전에
**같은 action 을 허용하는 다른 ACTIVE permit 이 있는지 라이브에서 읽어** 확인하고, 있으면
저장을 막아요. 관리자가 「알고 있다」를 명시하면(`acknowledge_ineffective`) 통과시키되 그
사실을 원장에 남겨요. 규약이 아니라 코드로 강제해요.

굵은 문 컴파일러(`agent_policy_compiler.compile_shared_gateway_policies`)가 도메인 룰이 붙은
action 을 열거에서 빼도록 배선하는 것이 진짜 해법이에요. 그건 이 모듈의 범위가 아니에요 —
후속 티켓이에요.

## 만들 때는 `LOG_ONLY`, 강제는 사람이 올려요

`CreatePolicy`/`UpdatePolicy` 에는 **정책 하나짜리** `enforcementMode` 가 있어요
(`['ACTIVE', 'LOG_ONLY']`, botocore 1.43.72 shape 확인). Gateway 전체의
`policyEngineConfiguration.mode` 와 별개예요. 공식문서 `policy-enforcement-modes` 는 LOG_ONLY 를
"evaluates and logs whether the action would be allowed or denied **without enforcing**" 로
정의해요.

도메인 규칙은 임계값을 잘못 잡으면 정상 업무를 막아요. 그래서 **생성은 항상 `LOG_ONLY`** 이고,
`ACTIVE` 로 올리는 건 `promote()` 라는 별도 동작이에요. 승격 시점에 굵은 문 충돌을 **다시**
재요 — 만든 뒤에 굵은 문이 바뀔 수 있어요.

⚠️ **`permit` 의 LOG_ONLY 는 「막지 않음」이 아니라 「허용하지 않음」이에요.** 뜻이 상황에 따라
뒤집혀요.

| 굵은 문이 이 action 을 | LOG_ONLY 인 동안 | ACTIVE 로 올리면 |
|---|---|---|
| 허용하고 있음 (지금) | 아무 영향 없어요. 관측만 해요 | 여전히 아무 영향 없어요 — permit 은 합집합이라 굵은 문이 이미 열어놨어요 |
| 허용하지 않음 (§10.7 이후) | **그 도구가 통째로 막혀요** — 맞는 permit 이 하나도 없어 default-deny 예요 | 임계값 안에 든 호출만 통과해요 (의도한 상태) |

그래서 화면은 「LOG_ONLY」만 말하면 안 되고, **굵은 문 관측 결과와 함께** 읽어야 해요.
`enforcement_effect()` 가 그 조합을 코드로 돌려줘요.

## 검증은 비동기예요

`create_policy` 200 은 통과가 아니에요. 없는 도구 이름은 그 뒤에 `unrecognized action` 으로
거부돼요(2026-08-28 실측). 그래서 종료 상태까지 폴링하고, 못 보면 `UNKNOWN` 으로 적어요 —
관측 실패를 성공으로 접지 않아요. 강제 모드도 요청값이 아니라 **다시 읽은 값**을 적어요.

## 후속 (이 모듈 범위 밖)

- 굵은 문 컴파일러가 도메인 룰이 붙은 action 을 열거에서 자동 제외하기.
- LOG_ONLY 정책이 **무엇을 거부했을지** 화면으로 가져오기. Cedar 는 `AllowDecisions`/
  `DenyDecisions` 를 도구 이름 단위로 내고 `Mode` 차원에 `LOG_ONLY` 가 있어요
  (`docs/research/2026-08-31-agentcore-interceptor-official-guidance.md` §6.4). 그 판정 로그를
  화면에 붙이는 건 별개 작업이에요.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field

from ...shared.gateway_tools import (
    McpGatewayTargetError,
    gateway_tool_name,
    mcp_gateway_target_index,
)
from .agent_policy_compiler import (
    MAX_CEDAR_POLICY_BYTES,
    _cedar_string_literal,  # 조립 경계의 방어적 escaping을 재사용해요(같은 패키지).
)
from .gateway_interceptor import OWNER_ARGUMENT
from .models import DomainPolicyEnforcementChange, DomainPolicyRule

_log = logging.getLogger(__name__)

# 프로덕션 배포와 같은 값이어야 해요 — `AgentPolicyDeployer.__init__` 기본값이에요. 여기서
# 다른 값을 쓰면 화면에서 저장한 정책이 배포 경로와 다른 검증을 받아요.
VALIDATION_MODE = "FAIL_ON_ANY_FINDINGS"

# 2026-08-29 실측: 정책 9장 엔진에서 `create_policy` 가 `CREATING` 을 주고 15초 뒤 ACTIVE
# 였어요. 상한이 실측에 가까우면 정상 저장이 `UNKNOWN` 으로 떨어져요.
_ACTIVE_MAX_POLLS = 60
_POLL_SECONDS = 1.0

# `DomainRule_{gwhash10}_{rulehash8}_r{n}` — 이 접두어가 소유권 표시예요.
NAME_PREFIX = "DomainRule_"
_NAME_BUDGET = 48

# 인자 이름은 Cedar 식별자로 그대로 들어가요. `context.input.amount` 처럼 쓰려면 식별자여야
# 해서, 하이픈·공백이 있는 이름은 거부해요. `context.input["a-b"]` 형태를 허용하면 문장이
# 읽기 어려워지고 검사 정규식도 두 갈래가 돼요 — 지금은 좁게 시작해요.
_ARGUMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
# Cedar 예약어·연산자 키워드를 인자 이름으로 쓰면 파싱이 깨져요.
_CEDAR_RESERVED = frozenset({
    "action", "context", "else", "false", "forbid", "has", "if", "in", "is",
    "like", "permit", "principal", "resource", "then", "true", "unless", "when",
})
# action 은 `<target>___<operation>` 두 조각 모두 안전 문자집합이어야 해요. 원격 MCP tool
# 이름에서 유래할 수 있어(따옴표·개행·Cedar 구문) statement 탈출을 막아요.
_ACTION_RE = re.compile(r"^[A-Za-z0-9_.-]+___[A-Za-z0-9_.-]+$")
_TARGET_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
_GATEWAY_RE = re.compile(r"^[A-Za-z0-9_.:/=+@-]+$")
# 문자열 임계값 — Cedar 문자열 리터럴에 들어가요. 따옴표·백슬래시·개행을 애초에 막아요.
_STRING_VALUE_RE = re.compile(r"^[A-Za-z0-9 _.:@/+=-]{1,128}$")
_DESCRIPTION_MAX = 300

#: 수치 비교와 문자열 동등만 받아요. 자유 표현식은 받지 않아요 — 임의 Cedar 를 받으면
#: 클라이언트가 인가 문장을 쓰는 셈이에요.
NUMERIC_OPERATORS: tuple[str, ...] = ("<=", "<", "==", ">=", ">")
STRING_OPERATORS: tuple[str, ...] = ("==",)
#: Cedar 의 정수는 64bit long 이에요. 소수는 `decimal()` 확장이 필요해서 지금은 안 받아요.
_INT_MIN = -(2 ** 53)
_INT_MAX = 2 ** 53

VALUE_NUMBER = "number"
VALUE_STRING = "string"

#: 정책 하나짜리 강제 모드 (`CreatePolicy.enforcementMode`, botocore 1.43.72).
ENFORCEMENT_LOG_ONLY = "LOG_ONLY"
ENFORCEMENT_ACTIVE = "ACTIVE"
#: **생성은 항상 관측 모드**예요. 이 값을 `ACTIVE` 로 바꾸면 임계값 오타가 곧 업무 중단이 돼요.
#: 회귀 테스트가 이 값을 단정해요 (`test_domain_policy_api.py`).
CREATE_ENFORCEMENT_MODE = ENFORCEMENT_LOG_ONLY

#: `enforcement_effect` 가 돌려주는 코드. 화면이 문구를 붙여요.
EFFECT_OBSERVING_NO_IMPACT = "observing_no_impact"
EFFECT_OBSERVING_TOOL_CLOSED = "observing_tool_closed"
EFFECT_ENFORCING = "enforcing"
EFFECT_ENFORCING_SHADOWED = "enforcing_shadowed"
EFFECT_UNKNOWN = "unknown"


def enforcement_effect(
    *, enforcement_mode: str, coarse_permits_action: bool | None
) -> str:
    """「이 정책이 지금 무슨 일을 하나」를 코드로 돌려줘요 (순수 함수).

    강제 모드 하나만 보면 틀려요. `permit` 의 `LOG_ONLY` 는 「막지 않음」이 아니라
    「허용하지 않음」이라, 굵은 문이 그 action 을 허용하는지에 따라 뜻이 뒤집혀요.

    `coarse_permits_action=None` 은 관측 실패예요 — `EFFECT_UNKNOWN` 이고, 「영향 없음」으로
    접지 않아요(ADR-0037 §4).
    """
    if coarse_permits_action is None:
        return EFFECT_UNKNOWN
    if enforcement_mode == ENFORCEMENT_ACTIVE:
        return (
            EFFECT_ENFORCING_SHADOWED if coarse_permits_action else EFFECT_ENFORCING
        )
    if enforcement_mode == ENFORCEMENT_LOG_ONLY:
        return (
            EFFECT_OBSERVING_NO_IMPACT
            if coarse_permits_action
            else EFFECT_OBSERVING_TOOL_CLOSED
        )
    return EFFECT_UNKNOWN

CONFLICT_ACTION_UNRESTRICTED = "action_unrestricted"
CONFLICT_ACTION_LISTED = "action_listed"
CONFLICT_TARGET_LISTED = "target_listed"
CONFLICT_DUPLICATE_DOMAIN_RULE = "duplicate_domain_rule"

_CONFLICT_LABELS = {
    CONFLICT_ACTION_UNRESTRICTED: (
        "action 을 제한하지 않는 permit 이 살아 있어요 — 이 Gateway 의 모든 도구가 이미 "
        "허용돼요"
    ),
    CONFLICT_ACTION_LISTED: "같은 도구를 이름으로 허용하는 permit 이 살아 있어요",
    CONFLICT_TARGET_LISTED: (
        "이 도구가 속한 Gateway Target 을 허용하는 permit 이 살아 있어요 — Target 단위 "
        "permit 은 그 안의 도구를 함께 열어요"
    ),
    CONFLICT_DUPLICATE_DOMAIN_RULE: (
        "같은 도구에 도메인 규칙이 이미 있어요 — permit 은 합집합이라 느슨한 쪽이 이겨요"
    ),
}


class InvalidDomainRule(ValueError):
    """구조화된 입력이 검증(allowlist)을 통과하지 못했어요.

    Cedar 원문·임의 표현식은 애초에 받지 않아요. 이 예외가 나면 조립을 하지 않아요.
    """


class DomainRuleIneffective(RuntimeError):
    """이 action 을 이미 허용하는 다른 ACTIVE permit 이 있어요 (합집합 → 장식)."""

    def __init__(self, conflicts: tuple[CoarseConflict, ...]) -> None:
        super().__init__("굵은 문이 이 action 을 이미 허용해요.")
        self.conflicts = conflicts


class ConflictUnobservable(RuntimeError):
    """굵은 문 충돌을 관측하지 못했어요. 못 본 것을 「충돌 없음」으로 적지 않아요."""


class EnforcementPromotionRefused(RuntimeError):
    """승격 전 확인에서 막혔어요 (라이브 문장 드리프트·관측 실패)."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


# ── 구조화된 입력 ──────────────────────────────────────────────────────────
@dataclass(frozen=True)
class DomainRuleDraft:
    """관리자가 폼에서 고른 값들. **Cedar 원문은 여기에 없어요.**"""

    gateway_arn: str
    asset_id: str
    asset_version: str
    target_name: str
    tool_name: str
    argument: str
    operator: str
    value_kind: str
    value: str
    description: str = ""

    @property
    def action(self) -> str:
        return gateway_tool_name(self.target_name, self.tool_name)


@dataclass(frozen=True)
class CompiledDomainRule:
    action: str
    cedar_policy: str
    policy_hash: str
    size_bytes: int


@dataclass(frozen=True)
class CoarseConflict:
    policy_id: str
    policy_name: str
    kind: str
    label: str
    detail: str


@dataclass(frozen=True)
class ConflictObservation:
    """`observed=False` 는 관측 실패예요. 「충돌 없음」과 다르게 취급해요."""

    observed: bool
    conflicts: tuple[CoarseConflict, ...] = ()
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "observed": self.observed,
            "reason": self.reason,
            "conflicts": [asdict(c) for c in self.conflicts],
        }


@dataclass(frozen=True)
class ToolArgument:
    name: str
    json_type: str
    required: bool
    #: Cedar 식별자로 못 쓰는 이름이면 고를 수 없어요. 이유를 화면이 말할 수 있게 실어요.
    usable: bool
    reason: str = ""


@dataclass(frozen=True)
class ToolOption:
    tool_name: str
    target_name: str
    sensitivity: str
    description: str
    arguments: tuple[ToolArgument, ...]
    #: `False` 는 스키마를 읽지 못했다는 뜻이에요. 인자 0개와 구분해요.
    schema_observed: bool
    schema_reason: str = ""


@dataclass(frozen=True)
class AssetOption:
    asset_id: str
    asset_version: str
    name: str
    tools: tuple[ToolOption, ...]
    reason: str = ""


@dataclass
class DomainPolicyOptions:
    gateway_arn: str
    assets: list[AssetOption] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    numeric_operators: tuple[str, ...] = NUMERIC_OPERATORS
    string_operators: tuple[str, ...] = STRING_OPERATORS

    def to_dict(self) -> dict:
        return {
            "gateway_arn": self.gateway_arn,
            "assets": [asdict(a) for a in self.assets],
            "warnings": list(self.warnings),
            "numeric_operators": list(self.numeric_operators),
            "string_operators": list(self.string_operators),
        }


# ── 조립 (순수 함수) ───────────────────────────────────────────────────────
def normalize_draft(payload: dict) -> DomainRuleDraft:
    """구조화된 dict 를 검증된 draft 로 바꿔요.

    허용한 키만 읽어요. `cedar` 같은 키가 섞여 있으면 **거부**해요 — 조용히 무시하면
    클라이언트가 Cedar 를 보내도 되는 것처럼 보여요.
    """
    allowed = {
        "asset_id", "asset_version", "target_name", "tool_name", "argument",
        "operator", "value_kind", "value", "description",
    }
    unexpected = sorted(set(payload) - allowed)
    if unexpected:
        raise InvalidDomainRule(
            "허용되지 않은 항목이 있어요: " + ", ".join(unexpected)
            + ". Cedar 문장은 서버가 조립해요."
        )
    return DomainRuleDraft(
        gateway_arn="",  # 좌표는 서버가 config 에서 채워요.
        asset_id=str(payload.get("asset_id") or "").strip(),
        asset_version=str(payload.get("asset_version") or "").strip(),
        target_name=str(payload.get("target_name") or "").strip(),
        tool_name=str(payload.get("tool_name") or "").strip(),
        argument=str(payload.get("argument") or "").strip(),
        operator=str(payload.get("operator") or "").strip(),
        value_kind=str(payload.get("value_kind") or "").strip(),
        value=str(payload.get("value") or "").strip(),
        description=str(payload.get("description") or "").strip(),
    )


def _validated_argument(argument: str) -> str:
    if not _ARGUMENT_RE.fullmatch(argument):
        raise InvalidDomainRule(
            f"인자 이름이 Cedar 식별자가 아니에요: {argument!r}. "
            "영문자·숫자·밑줄만 쓸 수 있고 숫자로 시작할 수 없어요."
        )
    if argument in _CEDAR_RESERVED:
        raise InvalidDomainRule(f"Cedar 예약어는 인자 이름으로 쓸 수 없어요: {argument!r}")
    if argument == OWNER_ARGUMENT:
        # ADR-0095 의 결정이에요 — 예약 인자는 Cedar 정책에서 참조하지 않아요.
        #
        # 이 인자는 interceptor 가 본문에 **써 넣는** 값이에요. 그리고 Cedar 는 interceptor 가
        # 변형한 본문을 봐요(AWS 가 의도한 순서예요 — 2026-08-31 조사 §6.2). 그래서 이걸
        # 조건으로 쓰면 Agora 가 자기가 넣은 값을 자기가 검사하는 셈이고, 조건은 항상 참이
        # 돼요. 도메인 규칙으로는 아무 일도 하지 않으면서 「걸어 뒀다」로 읽혀요.
        raise InvalidDomainRule(
            f"{OWNER_ARGUMENT!r} 는 Agora 가 채우는 예약 인자예요 — 도메인 규칙의 조건으로 "
            "쓸 수 없어요(ADR-0095). Cedar 는 interceptor 가 채운 본문을 보니까, 이 인자를 "
            "조건으로 쓰면 항상 참이 되어 규칙이 아무 일도 하지 않아요."
        )
    return argument


def _validated_value(operator: str, value_kind: str, value: str) -> str:
    """Cedar 문장에 들어갈 리터럴을 만들어요."""
    if value_kind == VALUE_NUMBER:
        if operator not in NUMERIC_OPERATORS:
            raise InvalidDomainRule(
                f"수치 비교에 쓸 수 없는 연산자예요: {operator!r}. "
                f"허용: {', '.join(NUMERIC_OPERATORS)}"
            )
        try:
            number = int(value)
        except ValueError as exc:
            raise InvalidDomainRule(
                f"임계값이 정수가 아니에요: {value!r}. Cedar 기본 문법에는 소수 리터럴이 "
                "없어요(decimal 확장이 필요해요)."
            ) from exc
        if not _INT_MIN <= number <= _INT_MAX:
            raise InvalidDomainRule(f"임계값이 허용 범위를 벗어났어요: {number}")
        return str(number)
    if value_kind == VALUE_STRING:
        if operator not in STRING_OPERATORS:
            raise InvalidDomainRule(
                f"문자열 비교에 쓸 수 없는 연산자예요: {operator!r}. "
                f"허용: {', '.join(STRING_OPERATORS)}"
            )
        if not _STRING_VALUE_RE.fullmatch(value):
            raise InvalidDomainRule(
                f"허용되지 않는 문자열 임계값이에요: {value!r}"
            )
        return f'"{_cedar_string_literal(value)}"'
    raise InvalidDomainRule(
        f"알 수 없는 값 종류예요: {value_kind!r}. "
        f"{VALUE_NUMBER} 또는 {VALUE_STRING} 이어야 해요."
    )


def build_when_clause(argument: str, operator: str, literal: str) -> str:
    """`when { … }` 안에 들어갈 조건을 만들어요.

    `has` 가드가 **먼저** 와요. `&&` 는 왼쪽이 거짓이면 오른쪽을 평가하지 않아서
    (short-circuit), 인자가 없을 때 비교가 평가 오류를 내지 않고 이 문장만 안 맞게 돼요 →
    맞는 permit 이 없으니 default-deny 예요.

    **모듈 수준 함수로 뺀 이유가 검증 때문이에요.** 테스트가 이 함수만 갈아끼워서
    `compile_domain_rule` 이 정말 독립 검사(`assert_fail_closed_shape`)를 거치는지 확인해요.
    조립 안에 인라인으로 두면 「검사를 호출하지 않아도 테스트가 통과하는」 상태가 되고, 그건
    검사가 아니에요(ADR-0037 §4).
    """
    return f"context.input has {argument} && context.input.{argument} {operator} {literal}"


def compile_domain_rule(draft: DomainRuleDraft) -> CompiledDomainRule:
    """draft 를 `permit` + `has` 가드 Cedar 문장으로 조립해요 (순수 함수).

    조립한 뒤 **다시 읽어서** 모양을 확인해요(`assert_fail_closed_shape`). 만드는 쪽과
    검사하는 쪽을 나누면, 가드 조립이 빠졌을 때 조용히 통과하지 않아요.
    """
    gateway = draft.gateway_arn.strip()
    if not gateway.startswith("arn:") or not _GATEWAY_RE.fullmatch(gateway):
        raise InvalidDomainRule(f"실제 gateway ARN 이 필요해요: {gateway!r}")
    if not _TARGET_RE.fullmatch(draft.target_name) or draft.target_name == "CallTool":
        raise InvalidDomainRule(
            f"허용되지 않는 Gateway Target 형식이에요: {draft.target_name!r}"
        )
    action = draft.action
    if not _ACTION_RE.fullmatch(action):
        raise InvalidDomainRule(f"허용되지 않는 action 형식이에요: {action!r}")
    argument = _validated_argument(draft.argument)
    literal = _validated_value(draft.operator, draft.value_kind, draft.value)
    if len(draft.description) > _DESCRIPTION_MAX:
        raise InvalidDomainRule(
            f"설명은 {_DESCRIPTION_MAX}자를 넘을 수 없어요."
        )

    when_clause = build_when_clause(argument, draft.operator, literal)
    cedar = (
        "permit(\n"
        "  principal is AgentCore::OAuthUser,\n"
        f'  action == AgentCore::Action::"{_cedar_string_literal(action)}",\n'
        f'  resource == AgentCore::Gateway::"{_cedar_string_literal(gateway)}"\n'
        ")\n"
        f"when {{ {when_clause} }};"
    )
    encoded = cedar.encode("utf-8")
    if len(encoded) > MAX_CEDAR_POLICY_BYTES:
        raise InvalidDomainRule(
            f"Cedar 정책 1장은 {MAX_CEDAR_POLICY_BYTES} 바이트를 넘을 수 없어요."
        )
    # 조립 결과를 독립 검사에 통과시켜요. 여기서 걸리면 조립 코드가 깨진 거예요.
    assert_fail_closed_shape(
        cedar,
        argument=argument,
        action=action,
        gateway_arn=gateway,
    )
    return CompiledDomainRule(
        action=action,
        cedar_policy=cedar,
        policy_hash=hashlib.sha256(encoded).hexdigest(),
        size_bytes=len(encoded),
    )


_SHAPE_RE = re.compile(
    r"^permit\(\s*"
    r"principal is AgentCore::OAuthUser,\s*"
    r'action == AgentCore::Action::"(?P<action>[^"]+)",\s*'
    r'resource == AgentCore::Gateway::"(?P<gateway>[^"]+)"\s*'
    r"\)\s*when\s*\{(?P<body>.+)\}\s*;\s*$",
    re.DOTALL,
)


def assert_fail_closed_shape(
    statement: str,
    *,
    argument: str,
    action: str,
    gateway_arn: str,
) -> None:
    """조립된 문장이 fail-closed 모양인지 **독립적으로** 확인해요.

    기대값의 출처가 조립기가 아니라 draft 예요(ADR-0037 §4) — 조립기가 `has` 가드를 빼먹으면
    여기서 걸려요. 그래서 이 함수는 조립 코드를 참조하지 않고 문장 텍스트만 봐요.

    확인하는 것 네 가지예요.
      ① `forbid` 가 아니에요 — `forbid` 는 인자가 빠지면 평가 오류로 무력화돼요.
      ② `unless` 절이 없어요 — 빠져나갈 구멍을 만들지 않아요.
      ③ `when` 절이 `context.input has <argument>` 로 **시작**해요.
      ④ 비교 대상이 그 `argument` 예요 (가드와 다른 인자를 비교하면 가드가 무의미해요).
    """
    text = (statement or "").strip()
    if "forbid" in text:
        raise InvalidDomainRule(
            "도메인 정책은 `permit` 만 만들어요. `forbid` 는 봇이 인자를 빼면 평가 오류로 "
            "적용되지 않아 요청이 통과해요."
        )
    if "unless" in text:
        raise InvalidDomainRule("`unless` 절은 빠져나갈 구멍이라 만들지 않아요.")
    match = _SHAPE_RE.match(text)
    if not match:
        raise InvalidDomainRule(
            "조립된 Cedar 문장이 기대한 모양이 아니에요 "
            "(permit + action == + resource == + when)."
        )
    if match.group("action") != action:
        raise InvalidDomainRule(
            f"문장의 action 이 입력과 달라요: {match.group('action')!r} != {action!r}"
        )
    if match.group("gateway") != gateway_arn:
        raise InvalidDomainRule("문장의 Gateway 가 입력과 달라요.")
    body = " ".join(match.group("body").split())
    guard = f"context.input has {argument}"
    if not body.startswith(f"{guard} &&"):
        raise InvalidDomainRule(
            f"`has` 가드가 없어요. `when` 절은 `{guard} && …` 로 시작해야 해요. "
            "가드가 없으면 인자가 빠졌을 때 평가 오류가 나고, 그 정책은 적용되지 않아요."
        )
    comparison = body[len(guard) + 3:].strip()
    if not comparison.startswith(f"context.input.{argument} "):
        raise InvalidDomainRule(
            f"가드와 비교 대상이 달라요. `context.input.{argument}` 를 비교해야 해요."
        )


# ── 굵은 문 충돌 판정 (순수 함수) ──────────────────────────────────────────
_COMMENT_RE = re.compile(r"/\*.*?\*/|//[^\n]*", re.DOTALL)
_CEDAR_ACTION_NAME_RE = re.compile(r'AgentCore::Action::"([^"]+)"')
_BARE_ACTION_RE = re.compile(r"(?m)^\s*action\s*,\s*$")


def detect_permit_overlap(
    *,
    action: str,
    target_name: str,
    policies: Iterable[dict],
    exclude_policy_ids: Sequence[str] = (),
) -> tuple[CoarseConflict, ...]:
    """이 action 을 이미 허용하는 다른 ACTIVE `permit` 을 찾아요.

    `policies` 항목은 `{"policy_id", "name", "status", "cedar"}` 예요. 판정 대상은 라이브
    정책 문장이고, 기대값은 draft 에서 온 action/target 이라 소유자가 달라요.

    보수적으로 판정해요 — `when`/`unless` 조건이 붙은 permit 도 충돌로 세요. 굵은 문의
    `when { principal.hasTag("scope") }` 는 실제 호출자가 거의 항상 만족하는 조건이라,
    조건이 있다는 이유로 「충돌 아님」이라고 접으면 틀려요.
    """
    excluded = set(exclude_policy_ids)
    out: list[CoarseConflict] = []
    for item in policies:
        policy_id = str(item.get("policy_id") or "")
        if policy_id in excluded:
            continue
        if str(item.get("status") or "") != "ACTIVE":
            continue
        text = _COMMENT_RE.sub(" ", str(item.get("cedar") or ""))
        stripped = text.lstrip()
        if not stripped.startswith("permit"):
            # `forbid` 는 권한을 주지 않아요 — 합집합의 구성원이 아니에요.
            continue
        name = str(item.get("name") or "")
        listed = set(_CEDAR_ACTION_NAME_RE.findall(text))
        kind = ""
        if not listed and _BARE_ACTION_RE.search(text):
            kind = CONFLICT_ACTION_UNRESTRICTED
            detail = "action 제약이 없는 permit 이에요."
        elif action in listed:
            kind = (
                CONFLICT_DUPLICATE_DOMAIN_RULE
                if name.startswith(NAME_PREFIX)
                else CONFLICT_ACTION_LISTED
            )
            detail = f'이 정책이 `{action}` 을 열거해요.'
        elif target_name in listed:
            kind = CONFLICT_TARGET_LISTED
            detail = f'이 정책이 Target `{target_name}` 을 열거해요.'
        if not kind:
            continue
        out.append(CoarseConflict(
            policy_id=policy_id,
            policy_name=name,
            kind=kind,
            label=_CONFLICT_LABELS[kind],
            detail=detail,
        ))
    return tuple(out)


# ── 자산 원장에서 인자 이름 읽기 ────────────────────────────────────────────
def _declared_tools(descriptors: object) -> tuple[list[dict], str]:
    """MCP descriptor 의 인라인 도구 목록과 관측 실패 사유를 돌려줘요.

    `access_router._mcp_inline_tools` 와 형제예요. 그 함수는 라우터 안에 있어서 재사용하면
    도메인이 라우터를 import 하게 돼요. 규약이 바뀌면 두 곳을 같이 고쳐야 해요.
    """
    mcp = descriptors.get("mcp") if isinstance(descriptors, dict) else None
    if not isinstance(mcp, dict):
        return [], "descriptor_mcp_missing"
    tools_node = mcp.get("tools")
    if not isinstance(tools_node, dict):
        return [], "descriptor_tools_invalid"
    inline = tools_node.get("inlineContent")
    if not isinstance(inline, str) or not inline.strip():
        return [], "descriptor_tools_empty"
    try:
        parsed = json.loads(inline)
    except (TypeError, ValueError):
        return [], "descriptor_json_invalid"
    tools = parsed.get("tools") if isinstance(parsed, dict) else None
    if not isinstance(tools, list):
        return [], "descriptor_tools_invalid"
    return [tool for tool in tools if isinstance(tool, dict)], ""


def schema_arguments(input_schema: object) -> tuple[tuple[ToolArgument, ...], str]:
    """`inputSchema.properties` 에서 인자 이름·타입을 뽑아요.

    스키마를 못 읽으면 사유를 돌려줘요. 「인자 0개」와 「관측 실패」를 구분해요 —
    관측 실패를 0개로 접으면 화면이 "인자가 없는 도구" 라고 거짓말해요.
    """
    if not isinstance(input_schema, dict):
        return (), "input_schema_missing"
    properties = input_schema.get("properties")
    if not isinstance(properties, dict):
        return (), "input_schema_properties_missing"
    required = input_schema.get("required")
    required_names = {
        str(name) for name in required if isinstance(name, str)
    } if isinstance(required, list) else set()
    out: list[ToolArgument] = []
    for name, spec in properties.items():
        if not isinstance(name, str) or not name:
            continue
        json_type = ""
        if isinstance(spec, dict):
            raw_type = spec.get("type")
            json_type = str(raw_type) if isinstance(raw_type, str) else ""
        usable, reason = True, ""
        try:
            _validated_argument(name)
        except InvalidDomainRule as exc:
            usable, reason = False, str(exc)
        out.append(ToolArgument(
            name=name,
            json_type=json_type,
            required=name in required_names,
            usable=usable,
            reason=reason,
        ))
    out.sort(key=lambda arg: arg.name)
    return tuple(out), ""


def build_options(
    *,
    gateway_arn: str,
    assets: Iterable[tuple[str, str, str, object]],
) -> DomainPolicyOptions:
    """(asset_id, version, name, descriptors) 목록을 폼 선택지로 바꿔요 (순수 함수)."""
    options = DomainPolicyOptions(gateway_arn=gateway_arn)
    for asset_id, version, name, descriptors in assets:
        try:
            index = mcp_gateway_target_index(descriptors)
        except McpGatewayTargetError as exc:
            options.assets.append(AssetOption(
                asset_id=asset_id,
                asset_version=version,
                name=name,
                tools=(),
                reason=f"Gateway Target 원장을 읽지 못했어요: {exc}",
            ))
            options.warnings.append(f"{name}: Gateway Target 원장이 유효하지 않아요.")
            continue
        if not index.split:
            options.assets.append(AssetOption(
                asset_id=asset_id,
                asset_version=version,
                name=name,
                tools=(),
                reason=(
                    "민감도별 Gateway Target 원장이 없는 자산이에요(legacy). "
                    "Cedar action 이름을 확정할 수 없어 도메인 규칙을 걸 수 없어요."
                ),
            ))
            continue
        sensitivity_by_target = {
            target.name: target.sensitivity for target in index.targets
        }
        tools, tools_reason = _declared_tools(descriptors)
        tool_options: list[ToolOption] = []
        for tool in tools:
            tool_name = tool.get("name")
            if not isinstance(tool_name, str) or not tool_name.strip():
                continue
            tool_name = tool_name.strip()
            target_name = index.target_name(tool_name)
            if not target_name:
                continue
            arguments, schema_reason = schema_arguments(tool.get("inputSchema"))
            tool_options.append(ToolOption(
                tool_name=tool_name,
                target_name=target_name,
                sensitivity=sensitivity_by_target.get(target_name, ""),
                description=str(tool.get("description") or "")[:200],
                arguments=arguments,
                schema_observed=not schema_reason,
                schema_reason=schema_reason,
            ))
        tool_options.sort(key=lambda option: option.tool_name)
        options.assets.append(AssetOption(
            asset_id=asset_id,
            asset_version=version,
            name=name,
            tools=tuple(tool_options),
            reason=(
                "" if not tools_reason
                else f"자산 원장의 도구 목록을 읽지 못했어요: {tools_reason}"
            ),
        ))
        if tools_reason:
            options.warnings.append(
                f"{name}: 도구 목록을 읽지 못했어요({tools_reason}). "
                "인자 이름을 확인할 수 없으니 자유 입력의 오타 위험이 커요."
            )
    options.assets.sort(key=lambda asset: asset.name)
    return options


def policy_name(gateway_arn: str, policy_hash: str, revision: int) -> str:
    """`DomainRule_{gwhash10}_{rulehash8}_r{n}`."""
    digest = hashlib.sha256(gateway_arn.encode("utf-8")).hexdigest()[:10]
    name = f"{NAME_PREFIX}{digest}_{policy_hash[:8]}_r{revision}"
    return name[:_NAME_BUDGET]


# ── 서비스 ─────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class GatewayCoordinates:
    gateway_arn: str
    gateway_id: str
    engine_id: str


class DomainPolicyService:
    """도메인 규칙의 CRUD. Cedar 문장은 **서버가** 조립해요."""

    def __init__(
        self,
        *,
        control_client,
        store,
        coordinates: GatewayCoordinates,
        list_mcp_assets,
        now=None,
        sleep=None,
    ) -> None:
        self._c = control_client
        self._store = store
        self._coordinates = coordinates
        self._list_mcp_assets = list_mcp_assets
        self._now = now or _utc_now
        self._sleep = sleep or time.sleep

    @property
    def coordinates(self) -> GatewayCoordinates:
        return self._coordinates

    # ── 읽기 ────────────────────────────────────────────────────────────
    def list_rules(self) -> list[DomainPolicyRule]:
        rules = self._store.list_domain_policy_rules()
        return sorted(rules, key=lambda rule: rule.created_at, reverse=True)

    def options(self) -> DomainPolicyOptions:
        return build_options(
            gateway_arn=self._coordinates.gateway_arn,
            assets=self._list_mcp_assets(),
        )

    def preview(self, payload: dict) -> dict:
        """저장 전에 **서버가 조립할 문장 그대로** 와 충돌 판정을 돌려줘요."""
        draft = self._draft_with_coordinates(normalize_draft(payload))
        compiled = compile_domain_rule(draft)
        observation = self.observe_conflicts(compiled.action, draft.target_name)
        return {
            "action": compiled.action,
            "cedar": compiled.cedar_policy,
            "policy_hash": compiled.policy_hash,
            "size_bytes": compiled.size_bytes,
            "gateway_arn": draft.gateway_arn,
            "conflict": observation.to_dict(),
        }

    def observe_conflicts(
        self, action: str, target_name: str
    ) -> ConflictObservation:
        try:
            policies = self._live_policies()
        except Exception as exc:
            return ConflictObservation(
                observed=False,
                reason=(
                    "라이브 Cedar 정책을 읽지 못해 굵은 문 충돌을 판정하지 못했어요: "
                    f"{type(exc).__name__}: {exc}"
                ),
            )
        return ConflictObservation(
            observed=True,
            conflicts=detect_permit_overlap(
                action=action,
                target_name=target_name,
                policies=policies,
            ),
        )

    # ── 쓰기 ────────────────────────────────────────────────────────────
    def create(
        self,
        payload: dict,
        *,
        created_by: str,
        acknowledge_ineffective: bool = False,
    ) -> DomainPolicyRule:
        draft = self._draft_with_coordinates(normalize_draft(payload))
        compiled = compile_domain_rule(draft)
        observation = self.observe_conflicts(compiled.action, draft.target_name)
        if not observation.observed:
            # 관측 실패는 통과가 아니에요(ADR-0037 §4). 「충돌 없음」으로 적지 않아요.
            raise ConflictUnobservable(observation.reason)
        if observation.conflicts and not acknowledge_ineffective:
            raise DomainRuleIneffective(observation.conflicts)

        name = policy_name(draft.gateway_arn, compiled.policy_hash, 1)
        created = self._c.create_policy(
            policyEngineId=self._coordinates.engine_id,
            name=name,
            definition={"cedar": {"statement": compiled.cedar_policy}},
            validationMode=VALIDATION_MODE,
            # 생성은 **항상** 관측 모드예요. 강제는 사람이 `promote()` 로 올려요.
            enforcementMode=CREATE_ENFORCEMENT_MODE,
        )
        policy_id = str(created.get("policyId") or created.get("id") or "")
        if not policy_id:
            raise RuntimeError("create_policy 응답에 policyId 가 없어요.")
        status, reasons, observed_mode = self._await_terminal(policy_id)
        now = self._now()

        rule = DomainPolicyRule(
            rule_id=uuid.uuid4().hex,
            gateway_arn=draft.gateway_arn,
            engine_id=self._coordinates.engine_id,
            asset_id=draft.asset_id,
            asset_version=draft.asset_version,
            target_name=draft.target_name,
            tool_name=draft.tool_name,
            gateway_action=compiled.action,
            argument=draft.argument,
            operator=draft.operator,
            value_kind=draft.value_kind,
            # 임계값은 **문자열**로 저장해요. DynamoDB 숫자는 Decimal 로 돌아오고 로컬
            # JSON 은 int 라, 스토어를 지나면 타입이 달라져요. 비교는 Cedar 가 해요.
            threshold=draft.value,
            description=draft.description,
            cedar_policy=compiled.cedar_policy,
            policy_hash=compiled.policy_hash,
            remote_policy_id=policy_id,
            remote_policy_name=name,
            observed_status=status,
            status_reasons=reasons,
            coarse_conflicts=tuple(c.kind for c in observation.conflicts),
            coarse_conflict_policies=tuple(
                c.policy_name or c.policy_id for c in observation.conflicts
            ),
            conflict_acknowledged=bool(observation.conflicts),
            created_by=created_by,
            created_at=now,
            enforcement_mode=observed_mode,
            requested_enforcement_mode=CREATE_ENFORCEMENT_MODE,
            enforcement_changes=(
                DomainPolicyEnforcementChange(
                    requested_mode=CREATE_ENFORCEMENT_MODE,
                    observed_mode=observed_mode,
                    observed_status=status,
                    changed_by=created_by,
                    changed_at=now,
                    reason="생성",
                ),
            ),
        )
        self._store.put_domain_policy_rule(rule)
        _log.warning(
            "도메인 Cedar 정책 생성: action=%s arg=%s op=%s status=%s mode=%s "
            "conflicts=%s by=%s",
            compiled.action, draft.argument, draft.operator, status, observed_mode,
            len(observation.conflicts), created_by,
        )
        return rule

    def promote(
        self,
        rule_id: str,
        *,
        principal_id: str,
        acknowledge_ineffective: bool = False,
    ) -> DomainPolicyRule:
        """`LOG_ONLY` → `ACTIVE`. 사람이 명시적으로 하는 별도 동작이에요.

        승격 전에 두 가지를 다시 봐요.

          ① **라이브 문장이 원장과 같은지.** 다르면 승격하지 않아요 — 다른 사람이 고친 문장을
             우리가 강제로 올리면, 관리자는 원장에 적힌 규칙이 켜졌다고 읽지만 실제로 켜지는
             건 다른 문장이에요.
          ② **굵은 문 충돌.** 만든 뒤에 굵은 문이 바뀔 수 있어요. 생성 시점의 판정을 재사용하지
             않아요.
        """
        rule = self._store.get_domain_policy_rule(rule_id)
        try:
            live = self._c.get_policy(
                policyEngineId=rule.engine_id, policyId=rule.remote_policy_id
            )
        except Exception as exc:
            raise EnforcementPromotionRefused(
                "raw_state_unobservable",
                "라이브 정책을 읽지 못해 승격하지 않았어요: "
                f"{type(exc).__name__}: {exc}",
            ) from exc
        live_statement = str(
            ((live.get("definition") or {}).get("cedar") or {}).get("statement") or ""
        )
        if _fold_spaces(live_statement) != _fold_spaces(rule.cedar_policy):
            raise EnforcementPromotionRefused(
                "statement_drifted",
                "라이브 Cedar 문장이 원장과 달라요. 누군가 정책을 직접 고쳤을 수 있어요 — "
                "무엇이 강제될지 확정할 수 없어서 승격하지 않았어요.",
            )

        observation = self.observe_conflicts(rule.gateway_action, rule.target_name)
        if not observation.observed:
            raise ConflictUnobservable(observation.reason)
        conflicts = tuple(
            c for c in observation.conflicts if c.policy_id != rule.remote_policy_id
        )
        if conflicts and not acknowledge_ineffective:
            raise DomainRuleIneffective(conflicts)

        self._c.update_policy(
            policyEngineId=rule.engine_id,
            policyId=rule.remote_policy_id,
            # 문장을 함께 보내요. `UpdatePolicy` 가 부분 갱신인지 전체 교체인지 문서에
            # 없어서(shape 에선 `definition` 이 선택), 전체 교체여도 문장이 지워지지 않게요.
            # 방금 라이브와 일치를 확인했으니 같은 값을 다시 보내는 거예요.
            definition={"cedar": {"statement": rule.cedar_policy}},
            validationMode=VALIDATION_MODE,
            enforcementMode=ENFORCEMENT_ACTIVE,
        )
        status, reasons, observed_mode = self._await_terminal(rule.remote_policy_id)
        now = self._now()
        change = DomainPolicyEnforcementChange(
            requested_mode=ENFORCEMENT_ACTIVE,
            observed_mode=observed_mode,
            observed_status=status,
            changed_by=principal_id,
            changed_at=now,
            reason="승격" + ("(충돌 확인 후)" if conflicts else ""),
        )
        from dataclasses import replace

        updated = replace(
            rule,
            observed_status=status,
            status_reasons=reasons,
            enforcement_mode=observed_mode,
            requested_enforcement_mode=ENFORCEMENT_ACTIVE,
            enforcement_changes=(*rule.enforcement_changes, change),
            coarse_conflicts=tuple(c.kind for c in conflicts),
            coarse_conflict_policies=tuple(
                c.policy_name or c.policy_id for c in conflicts
            ),
            conflict_acknowledged=bool(conflicts),
        )
        self._store.put_domain_policy_rule(updated)
        _log.warning(
            "도메인 Cedar 정책 승격: rule=%s policy=%s mode=%s status=%s by=%s",
            rule_id, rule.remote_policy_id, observed_mode, status, principal_id,
        )
        return updated

    def refresh_status(self, rule_id: str) -> DomainPolicyRule:
        """원격 상태·강제 모드를 다시 관측해 원장을 맞춰요.

        `UNKNOWN` 을 뒤늦게 닫는 경로예요. 관측이 실패하면 다시 `UNKNOWN` 이고 강제 모드도
        `""` 로 비워요 — 옛 값을 남겨 두면 「확인했다」로 읽혀요.
        """
        from dataclasses import replace

        rule = self._store.get_domain_policy_rule(rule_id)
        try:
            detail = self._c.get_policy(
                policyEngineId=rule.engine_id, policyId=rule.remote_policy_id
            )
        except Exception as exc:
            updated = replace(
                rule,
                observed_status="UNKNOWN",
                status_reasons=(f"{type(exc).__name__}: {exc}",),
                enforcement_mode="",
            )
            self._store.put_domain_policy_rule(updated)
            return updated
        updated = replace(
            rule,
            observed_status=str(detail.get("status") or "UNKNOWN"),
            status_reasons=tuple(str(r) for r in (detail.get("statusReasons") or ())),
            enforcement_mode=str(detail.get("enforcementMode") or ""),
        )
        self._store.put_domain_policy_rule(updated)
        return updated

    def delete(self, rule_id: str, *, principal_id: str) -> dict:
        rule = self._store.get_domain_policy_rule(rule_id)
        _log.warning(
            "도메인 Cedar 정책 삭제: rule=%s policy=%s principal=%s",
            rule_id, rule.remote_policy_id, principal_id,
        )
        removed, reason = True, ""
        try:
            self._c.delete_policy(
                policyEngineId=rule.engine_id, policyId=rule.remote_policy_id
            )
        except Exception as exc:
            removed = False
            reason = (
                "원격 정책을 지우지 못해 원장을 남겨 뒀어요: "
                f"{type(exc).__name__}: {exc}"
            )
        if removed:
            self._store.delete_domain_policy_rule(rule_id)
        return {"deleted": removed, "reason": reason}

    # ── 내부 ────────────────────────────────────────────────────────────
    def _draft_with_coordinates(self, draft: DomainRuleDraft) -> DomainRuleDraft:
        from dataclasses import replace

        return replace(draft, gateway_arn=self._coordinates.gateway_arn)

    def _live_policies(self) -> list[dict]:
        """엔진의 정책을 문장까지 읽어와요.

        응답 키는 `policies` 예요. `items` 를 읽으면 값이 있어도 빈 목록이 나오고, 그 "0건"
        이 「충돌 없음」으로 읽혀요 — 이 화면에서는 그게 곧 오판이에요.
        """
        engine_id = self._coordinates.engine_id
        listed: list[dict] = []
        kwargs: dict = {"policyEngineId": engine_id, "maxResults": 50}
        seen: set[str] = set()
        while True:
            page = self._c.list_policies(**kwargs)
            if "policies" not in page:
                raise RuntimeError(
                    "list_policies 응답에 `policies` 가 없어요. 실제 키: "
                    f"{sorted(k for k in page if k != 'ResponseMetadata')}"
                )
            listed.extend(page.get("policies") or [])
            token = str(page.get("nextToken") or "")
            if not token or token in seen:
                break
            seen.add(token)
            kwargs["nextToken"] = token

        out: list[dict] = []
        for item in listed:
            policy_id = str(item.get("policyId") or item.get("id") or "")
            detail = self._c.get_policy(
                policyEngineId=engine_id, policyId=policy_id
            )
            out.append({
                "policy_id": policy_id,
                "name": str(detail.get("name") or item.get("name") or ""),
                "status": str(detail.get("status") or item.get("status") or ""),
                "cedar": str(
                    ((detail.get("definition") or {}).get("cedar") or {})
                    .get("statement") or ""
                ),
            })
        return out

    def _await_terminal(
        self, policy_id: str
    ) -> tuple[str, tuple[str, ...], str]:
        """종료 상태까지 폴링해 (상태, 사유, 관측된 강제 모드) 를 돌려줘요.

        못 보면 `UNKNOWN` + 강제 모드 `""` 예요 — 실패가 아니라 모름이에요. 강제 모드는
        요청값이 아니라 **응답에서 읽은 값**이에요.
        """
        for _ in range(_ACTIVE_MAX_POLLS):
            self._sleep(_POLL_SECONDS)
            try:
                detail = self._c.get_policy(
                    policyEngineId=self._coordinates.engine_id,
                    policyId=policy_id,
                )
            except Exception as exc:
                return "UNKNOWN", (
                    f"상태를 관측하지 못했어요: {type(exc).__name__}: {exc}",
                ), ""
            status = str(detail.get("status") or "")
            reasons = tuple(str(r) for r in (detail.get("statusReasons") or ()))
            mode = str(detail.get("enforcementMode") or "")
            if status and not status.endswith("ING"):
                return status, reasons, mode
        return "UNKNOWN", (
            f"검증이 {_ACTIVE_MAX_POLLS}회 관측 안에 끝나지 않았어요. 실패가 아니라 아직 "
            "모르는 상태예요 — 새로고침으로 확인해 주세요.",
        ), ""


def _fold_spaces(text: str) -> str:
    """공백을 접어 문장을 비교 가능하게 해요.

    AWS 가 되돌려주는 문장은 우리가 보낸 것과 공백이 다를 수 있어서, 원문 비교는 항상
    "다르다" 로 나와요 (`shared_policy_provisioner.normalize_cedar` 와 같은 이유예요).
    """
    return " ".join((text or "").split())


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
