"""Gateway 별 Cedar 정책 콘솔 — 라이브 정책을 읽고, 지우고, 문장을 고쳐요.

## 왜 별도 모듈인가

기존 `observe_agent_policy_inventory` 는 **원장이 아는 agent** 를 기준으로 대조해요. 그래서
원장에 없는 정책(옛 PoC, 손으로 만든 것, 컷오버 잔재)은 아예 보이지 않아요. 실측에서
두 개의 Gateway 에 11장이 살아 있는데 화면에는 한 줄도 없었어요.
이 모듈은 반대 방향으로 봐요 — **Gateway 를 출발점**으로 잡고 그 엔진에 실제로 등록된 정책을
전부 나열해요.

## 낡음 판정은 Gateway 가 소유해요 (ADR-0037 §4)

"이 정책이 낡았나" 를 정책 자신에게 묻지 않아요. 기대값의 출처가 subject 면 검사가 아니에요.
그래서 유효한 action 집합을 **Gateway 쪽에서** 만들어요 — `list_gateway_targets` 의 Target
이름과 각 Target 의 `toolSchema` 에서 유도한 `${target}___${tool}` 이에요. 정책이 그 집합에
없는 action 을 참조하면 `stale_actions` 로 표시해요. 두 입자 모두 유효하다는 건 2026-08-28
실측이에요(`docs/research/2026-08-28-cedar-action-granularity-measured.md`).

Target 목록을 못 읽으면 `stale_actions` 를 **비워 두고** `actions_observed=False` 를 실어요.
못 본 것을 "낡지 않았다" 로 적으면 관측 실패가 통과로 읽혀요.

## 편집은 원장을 거짓말하게 만들 수 있어요

Agora 가 소유한 정책(`Agent_…`·`Gateway_…`)은 원장에서 컴파일된 산출물이에요. 손으로 고치면
다음 배포가 덮어써서 편집이 사라지고, 그 사이에는 원장과 라이브가 어긋나요. 막지는 않지만
`edit_overwritten_by_deploy` 로 표시해서 화면이 그 사실을 말할 수 있게 해요.

## 검증은 비동기예요

`update_policy` 성공은 검증 통과가 아니에요. 없는 도구를 가리키면 `CreatePolicy`/`UpdatePolicy`
가 200 을 준 뒤에 `unrecognized action` 으로 실패해요(2026-08-28 실측). 그래서 종료 상태까지
폴링하고, 폴링이 끝나지 않으면 `UNKNOWN` 으로 돌려줘요 — 타임아웃은 한도가 아니에요.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import asdict, dataclass, field

_log = logging.getLogger(__name__)

# Agora 가 만든 정책 이름 규약. 출처는 `AgentPolicyDeployer.policy_name` 과
# `GatewayPolicyCutoverManager.policy_name` 이에요 — 여기 정규식은 그 형태를 읽는 쪽이라
# 이름 규약이 바뀌면 같이 고쳐야 해요.
_AGENT_POLICY_RE = re.compile(r"^Agent_.*_[0-9a-f]{8}_r\d+$")
_SHARED_POLICY_RE = re.compile(r"^Gateway_[0-9a-f]{10}_.+_r\d+$")
# 도메인 규칙 정책 (`domain_policy.policy_name`). 이 화면에서 `external` 로 보이면 관리자가
# 「누가 만든 건지 모르는 정책」으로 읽어요. 손으로 고치면 도메인 규칙 원장이 거짓이 되니
# `edit_overwritten_by_deploy` 경고 대상이기도 해요.
_DOMAIN_RULE_POLICY_RE = re.compile(r"^DomainRule_[0-9a-f]{10}_[0-9a-f]{8}_r\d+$")

# Cedar 문장에서 action 개체 이름을 뽑아요. `AgentCore::Action::"<name>"` 한 형태뿐이에요.
_ACTION_RE = re.compile(r'AgentCore::Action::"([^"]+)"')
# `action,` (제한 없음) 과 `action == …` / `action in [ … ]` 을 구분해요.
_BARE_ACTION_RE = re.compile(r"^\s*action\s*,\s*$", re.MULTILINE)
# principal 은 두 타입뿐이에요 — `AgentCore::OAuthUser` 와 `AgentCore::IamEntity`
# (§03). `principal is <Type>` 형태는 개체를 지목하지 않아서 id 가 없어요.
_PRINCIPAL_RE = re.compile(
    r'AgentCore::(OAuthUser|IamEntity)::"([^"]+)"'
)
_PRINCIPAL_TYPE_RE = re.compile(r"principal\s+is\s+AgentCore::(OAuthUser|IamEntity)")

_UPDATE_POLL_SECONDS = 1.0
# 2026-08-29 실측: 정책 9장이 있던 `AgoraCedarDemoDev` 엔진에서 `create_policy` 가
# `CREATING` 을 돌려주고 **15초 뒤**에 ACTIVE 가 됐어요. 검증은 엔진 전체와 교차해서
# 도구가 늘면 더 걸려요(도구 1,000개당 6~7.5초). 상한이 실측에 가까우면 정상 저장이
# `UNKNOWN` 으로 떨어져요.
_UPDATE_MAX_POLLS = 60
# 삭제는 다른 상한을 써요. 반영 지연은 0.8~3.1초 실측이라(§10) 검증과 같은 60초를 쓰면
# HTTP 요청이 아무 이유 없이 1분을 붙잡아요. 못 확인하면 실패가 아니라 "아직 모름" 이라
# 화면이 새로고침을 권해요.
_DELETE_MAX_POLLS = 8

# 프로덕션 배포와 **같은 값**을 써요 — `AgentPolicyDeployer.__init__` 의 기본값이에요
# (`agent_policy_deployer.py:247`). 여기서 다른 값을 추측하면 화면에서 저장한 정책이
# 배포 경로와 다른 검증을 받아요. (`STRICT` 로 적었다가 2026-08-29 에 잡았어요.)
_VALIDATION_MODE = "FAIL_ON_ANY_FINDINGS"

OWNERSHIP_AGENT = "agora-agent"
OWNERSHIP_SHARED = "agora-shared"
OWNERSHIP_DOMAIN_RULE = "agora-domain-rule"
OWNERSHIP_EXTERNAL = "external"


def classify_ownership(name: str) -> str:
    """정책 이름으로 소유자를 판정해요. 이름은 Agora 가 정한 규약이에요."""
    if _AGENT_POLICY_RE.match(name or ""):
        return OWNERSHIP_AGENT
    if _SHARED_POLICY_RE.match(name or ""):
        return OWNERSHIP_SHARED
    if _DOMAIN_RULE_POLICY_RE.match(name or ""):
        return OWNERSHIP_DOMAIN_RULE
    return OWNERSHIP_EXTERNAL


def cedar_actions(statement: str) -> tuple[tuple[str, ...], bool]:
    """(참조된 action 이름들, action 을 제한하지 않는지) 를 돌려줘요.

    `action,` 만 쓴 coarse gate 는 도구 이름을 열거하지 않아서 낡을 수가 없어요. 그 경우를
    "action 0개" 와 구분하지 않으면 굵은 정책이 stale 0건으로 보여 안전하다고 오독돼요.
    """
    names = tuple(dict.fromkeys(_ACTION_RE.findall(statement or "")))
    unrestricted = bool(_BARE_ACTION_RE.search(statement or "")) and not names
    return names, unrestricted


def cedar_principals(statement: str) -> tuple[tuple[str, ...], str]:
    """(지목된 principal id 들, principal 타입) 을 돌려줘요.

    "이 정책이 낡았나" 의 두 번째 축이에요. action 은 다 멀쩡한데 가리키는 봇이 사라진
    정책이 실제로 있어요(옛 PoC). id 를 화면에 띄우면 관리자가 그 판단을 할 수 있어요.
    id 존재 여부를 여기서 조회하지는 않아요 — Cognito·IAM 조회를 이 화면에 끌어들이면
    읽기 한 번이 계정 전체를 훑게 돼요.
    """
    text = statement or ""
    pairs = _PRINCIPAL_RE.findall(text)
    ids = tuple(dict.fromkeys(pid for _t, pid in pairs))
    if pairs:
        return ids, pairs[0][0]
    bound = _PRINCIPAL_TYPE_RE.search(text)
    # `principal is <Type>` — 타입 전체를 대상으로 해서 개체 id 가 없어요.
    return (), bound.group(1) if bound else ""


@dataclass(frozen=True)
class ConsolePolicy:
    policy_id: str
    name: str
    status: str
    enforcement_mode: str
    created_at: str
    updated_at: str
    cedar: str
    size_bytes: int
    status_reasons: tuple[str, ...]
    ownership: str
    actions: tuple[str, ...]
    action_unrestricted: bool
    stale_actions: tuple[str, ...]
    edit_overwritten_by_deploy: bool
    principal_type: str
    principal_ids: tuple[str, ...]
    read_error: str = ""


@dataclass(frozen=True)
class ConsoleGateway:
    gateway_id: str
    name: str
    gateway_arn: str
    engine_id: str
    engine_arn: str
    enforcement_mode: str
    engine_observed: bool
    reason: str
    target_names: tuple[str, ...]
    valid_actions: tuple[str, ...]
    actions_observed: bool
    policies: tuple[ConsolePolicy, ...] = ()


@dataclass
class ConsoleReport:
    gateways: list[ConsoleGateway] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "gateways": [asdict(gw) for gw in self.gateways],
            "warnings": list(self.warnings),
        }


class GatewayPolicyConsole:
    """boto `bedrock-agentcore-control` 를 직접 읽는 관리자용 정책 브라우저.

    기존 `AgentPolicyDeployer` 는 config 로 고정된 **엔진 하나**에 묶여 있어요. 이 화면은
    모든 Gateway 를 훑어야 해서 엔진을 호출 시점에 결정해요.
    """

    def __init__(self, control_client, *, sleep=None) -> None:
        self._c = control_client
        self._sleep = sleep or time.sleep

    # ── 읽기 ────────────────────────────────────────────────────────────────
    def observe(self) -> ConsoleReport:
        report = ConsoleReport()
        try:
            gateways = self._list_gateways()
        except Exception as exc:
            report.warnings.append(
                f"Gateway 목록을 읽지 못했어요: {type(exc).__name__}: {exc}"
            )
            return report

        for raw in gateways:
            report.gateways.append(self._observe_gateway(raw, report))
        report.gateways.sort(key=lambda gw: gw.name)
        return report

    def _list_gateways(self) -> list[dict]:
        items: list[dict] = []
        kwargs: dict = {"maxResults": 50}
        seen: set[str] = set()
        while True:
            page = self._c.list_gateways(**kwargs)
            items.extend(page.get("items") or [])
            token = str(page.get("nextToken") or "")
            if not token or token in seen:
                break
            seen.add(token)
            kwargs["nextToken"] = token
        return items

    def _observe_gateway(self, raw: dict, report: ConsoleReport) -> ConsoleGateway:
        gateway_id = str(raw.get("gatewayId") or "")
        name = str(raw.get("name") or gateway_id)

        engine_id = engine_arn = mode = ""
        gateway_arn = str(raw.get("gatewayArn") or "")
        engine_observed = False
        reason = ""
        try:
            full = self._c.get_gateway(gatewayIdentifier=gateway_id)
            gateway_arn = str(full.get("gatewayArn") or gateway_arn)
            # `policyEngineConfiguration` 은 boto3 로만 보여요. aws CLI 2.31.18 의
            # get-gateway 응답에는 이 필드가 아예 없어요(2026-08-29 실측).
            engine = full.get("policyEngineConfiguration") or {}
            engine_arn = str(engine.get("arn") or "")
            engine_id = engine_arn.rsplit("/", 1)[-1] if engine_arn else ""
            mode = str(engine.get("mode") or "")
            engine_observed = True
        except Exception as exc:
            reason = f"get_gateway 실패: {type(exc).__name__}: {exc}"
            report.warnings.append(f"{name}: {reason}")

        target_names, valid_actions, actions_observed, target_reason = (
            self._valid_actions(gateway_id)
        )
        if target_reason:
            reason = f"{reason} / {target_reason}" if reason else target_reason
            report.warnings.append(f"{name}: {target_reason}")

        policies: tuple[ConsolePolicy, ...] = ()
        if engine_id:
            policies, policy_reason = self._policies(
                engine_id,
                valid_actions=valid_actions,
                actions_observed=actions_observed,
            )
            if policy_reason:
                reason = f"{reason} / {policy_reason}" if reason else policy_reason
                report.warnings.append(f"{name}: {policy_reason}")
        elif engine_observed:
            reason = reason or "policy engine 이 붙어 있지 않아요."

        return ConsoleGateway(
            gateway_id=gateway_id,
            name=name,
            gateway_arn=gateway_arn,
            engine_id=engine_id,
            engine_arn=engine_arn,
            enforcement_mode=mode,
            engine_observed=engine_observed,
            reason=reason,
            target_names=target_names,
            valid_actions=valid_actions,
            actions_observed=actions_observed,
            policies=policies,
        )

    def _valid_actions(
        self, gateway_id: str
    ) -> tuple[tuple[str, ...], tuple[str, ...], bool, str]:
        """Gateway 가 소유한 유효 action 집합을 만들어요.

        Target 이름(action 그룹)과 `${target}___${tool}`(개별 action) **둘 다** 유효해요
        (2026-08-28 실측). 하나만 넣으면 정상 정책이 낡은 것으로 잡혀요.
        """
        names: list[str] = []
        actions: list[str] = []
        try:
            kwargs: dict = {"gatewayIdentifier": gateway_id, "maxResults": 50}
            seen: set[str] = set()
            targets: list[dict] = []
            while True:
                page = self._c.list_gateway_targets(**kwargs)
                targets.extend(page.get("items") or [])
                token = str(page.get("nextToken") or "")
                if not token or token in seen:
                    break
                seen.add(token)
                kwargs["nextToken"] = token
            for target in targets:
                target_name = str(target.get("name") or "")
                if not target_name:
                    continue
                names.append(target_name)
                actions.append(target_name)
                detail = self._c.get_gateway_target(
                    gatewayIdentifier=gateway_id,
                    targetId=target.get("targetId"),
                )
                for tool in _inline_tools(detail):
                    tool_name = str(tool.get("name") or "")
                    if tool_name:
                        actions.append(f"{target_name}___{tool_name}")
        except Exception as exc:
            # 못 읽었으면 낡음 판정을 아예 하지 않아요 — 빈 집합으로 판정하면 모든 정책이
            # 낡은 것으로 보여요(그 반대보다 덜 위험하지만 똑같이 틀린 관측이에요).
            return (), (), False, (
                f"Gateway Target 을 읽지 못해 낡음 판정을 못 했어요: "
                f"{type(exc).__name__}: {exc}"
            )
        return (
            tuple(dict.fromkeys(names)),
            tuple(dict.fromkeys(actions)),
            True,
            "",
        )

    def _policies(
        self,
        engine_id: str,
        *,
        valid_actions: tuple[str, ...],
        actions_observed: bool,
    ) -> tuple[tuple[ConsolePolicy, ...], str]:
        try:
            listed = self._list_policies(engine_id)
        except Exception as exc:
            return (), f"정책 목록을 읽지 못했어요: {type(exc).__name__}: {exc}"

        valid = set(valid_actions)
        out: list[ConsolePolicy] = []
        for item in listed:
            policy_id = str(item.get("policyId") or item.get("id") or "")
            name = str(item.get("name") or "")
            statement, reasons, detail, read_error = "", (), {}, ""
            try:
                detail = self._c.get_policy(
                    policyEngineId=engine_id, policyId=policy_id
                )
                statement = str(
                    ((detail.get("definition") or {}).get("cedar") or {})
                    .get("statement")
                    or ""
                )
                reasons = tuple(
                    str(r) for r in (detail.get("statusReasons") or ())
                )
            except Exception as exc:
                read_error = f"{type(exc).__name__}: {exc}"

            actions, unrestricted = cedar_actions(statement)
            principal_ids, principal_type = cedar_principals(statement)
            ownership = classify_ownership(name)
            stale = (
                tuple(a for a in actions if a not in valid)
                if actions_observed and not read_error
                else ()
            )
            out.append(ConsolePolicy(
                policy_id=policy_id,
                name=name,
                status=str(detail.get("status") or item.get("status") or ""),
                enforcement_mode=str(
                    detail.get("enforcementMode")
                    or item.get("enforcementMode")
                    or ""
                ),
                created_at=_iso(detail.get("createdAt") or item.get("createdAt")),
                updated_at=_iso(detail.get("updatedAt") or item.get("updatedAt")),
                cedar=statement,
                size_bytes=len(statement.encode("utf-8")),
                status_reasons=reasons,
                ownership=ownership,
                actions=actions,
                action_unrestricted=unrestricted,
                stale_actions=stale,
                edit_overwritten_by_deploy=ownership != OWNERSHIP_EXTERNAL,
                principal_type=principal_type,
                principal_ids=principal_ids,
                read_error=read_error,
            ))
        out.sort(key=lambda p: p.name)
        return tuple(out), ""

    def _list_policies(self, engine_id: str) -> list[dict]:
        # 응답 키는 `policies` 예요. `items` 를 읽으면 값이 있어도 빈 목록이 나와요.
        items: list[dict] = []
        kwargs: dict = {"policyEngineId": engine_id, "maxResults": 50}
        seen: set[str] = set()
        while True:
            page = self._c.list_policies(**kwargs)
            items.extend(page.get("policies") or [])
            token = str(page.get("nextToken") or "")
            if not token or token in seen:
                break
            seen.add(token)
            kwargs["nextToken"] = token
        return items

    # ── 쓰기 ────────────────────────────────────────────────────────────────
    def delete(self, engine_id: str, policy_id: str) -> dict:
        """정책을 지우고 **부재를 확인**해요.

        삭제는 즉시 반영되지 않아요 — 0.8~3.1초 동안 계속 허용돼요(§10 실측). 그래서 호출
        성공을 곧 "안 열려 있음" 으로 쓰지 않고, 목록에서 사라지는 것까지 봐요.
        """
        self._c.delete_policy(policyEngineId=engine_id, policyId=policy_id)
        for _ in range(_DELETE_MAX_POLLS):
            try:
                remaining = {
                    str(p.get("policyId") or p.get("id") or "")
                    for p in self._list_policies(engine_id)
                }
            except Exception as exc:
                return {
                    "deleted": True,
                    "absence_confirmed": False,
                    "reason": (
                        "삭제 호출은 성공했지만 부재를 확인하지 못했어요: "
                        f"{type(exc).__name__}: {exc}"
                    ),
                }
            if policy_id not in remaining:
                return {"deleted": True, "absence_confirmed": True, "reason": ""}
            self._sleep(_UPDATE_POLL_SECONDS)
        return {
            "deleted": True,
            "absence_confirmed": False,
            "reason": (
                "삭제 호출은 성공했지만 목록에서 아직 사라지지 않았어요. "
                "잠시 뒤 새로고침해 주세요."
            ),
        }

    def update_cedar(
        self,
        engine_id: str,
        policy_id: str,
        cedar: str,
        *,
        validation_mode: str = _VALIDATION_MODE,
    ) -> dict:
        """Cedar 문장을 바꾸고 **종료 상태까지** 폴링해요.

        `update_policy` 200 은 검증 통과가 아니에요. 없는 도구 이름은 그 뒤에
        `unrecognized action` 으로 거부돼요(2026-08-28 실측).
        """
        text = (cedar or "").strip()
        if not text:
            raise ValueError("Cedar 문장이 비어 있어요.")
        if len(text.encode("utf-8")) > 10_000:
            raise ValueError("Cedar 정책 1장은 10 KB 를 넘을 수 없어요.")

        before = ""
        try:
            current = self._c.get_policy(
                policyEngineId=engine_id, policyId=policy_id
            )
            before = str(
                ((current.get("definition") or {}).get("cedar") or {})
                .get("statement") or ""
            )
        except Exception as exc:
            _log.warning("이전 Cedar 문장을 읽지 못했어요: %s", exc)

        self._c.update_policy(
            policyEngineId=engine_id,
            policyId=policy_id,
            definition={"cedar": {"statement": text}},
            validationMode=validation_mode,
        )

        status, reasons = "", ()
        for _ in range(_UPDATE_MAX_POLLS):
            self._sleep(_UPDATE_POLL_SECONDS)
            try:
                detail = self._c.get_policy(
                    policyEngineId=engine_id, policyId=policy_id
                )
            except Exception as exc:
                return {
                    "status": "UNKNOWN",
                    "ok": False,
                    "previous_cedar": before,
                    "status_reasons": [f"{type(exc).__name__}: {exc}"],
                    "reason": "갱신 후 상태를 관측하지 못했어요.",
                }
            status = str(detail.get("status") or "")
            reasons = tuple(str(r) for r in (detail.get("statusReasons") or ()))
            if status and not status.endswith("ING"):
                break
        else:
            # 폴링 소진은 한도가 아니에요 — 뒤늦게 ACTIVE 가 될 수 있어요.
            return {
                "status": "UNKNOWN",
                "ok": False,
                "previous_cedar": before,
                "status_reasons": list(reasons),
                "reason": (
                    f"검증이 {_UPDATE_MAX_POLLS}회 관측 안에 끝나지 않았어요. "
                    "실패가 아니라 아직 모르는 상태예요 — 새로고침으로 확인해 주세요."
                ),
            }

        ok = status == "ACTIVE"
        return {
            "status": status,
            "ok": ok,
            "previous_cedar": before,
            "status_reasons": list(reasons),
            "reason": "" if ok else "; ".join(reasons) or f"상태가 {status} 예요.",
        }


def _inline_tools(target_detail: dict) -> list[dict]:
    """Target 상세에서 인라인 도구 스키마를 꺼내요 (lambda·openApi 형태 관용)."""
    config = target_detail.get("targetConfiguration") or {}
    mcp = config.get("mcp") or {}
    for key in ("lambda", "openApiSchema", "smithyModel"):
        node = mcp.get(key) or {}
        schema = node.get("toolSchema") or {}
        inline = schema.get("inlinePayload")
        if isinstance(inline, list):
            return [tool for tool in inline if isinstance(tool, dict)]
    return []


def _iso(value) -> str:
    if value is None:
        return ""
    isoformat = getattr(value, "isoformat", None)
    return isoformat() if callable(isoformat) else str(value)
