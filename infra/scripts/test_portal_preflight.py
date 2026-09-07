"""portal_preflight 의 Deny 검사 helper 단위 테스트.

이 저장소에 pytest 설정이 있는 곳은 `api/` 하나예요. 그래서 이 파일은 파일 경로를
직접 지정해 돌려요:

    api/.venv/bin/python -m pytest infra/scripts/test_portal_preflight.py

## 왜 음성 대조가 이 형태인가

preflight 는 라이브 정책을 읽는 스크립트라 EC2 에서 실행할 수 없어요. 그래서 게이트의
「이빨」은 **가짜 정책 문서 문자열**로 확인해요 — Deny action 이 하나라도 빠진 문서를
검사 함수에 먹였을 때 그 action 이 「누락」으로 잡혀야 해요. 양성 회귀(값이 맞다)만으로는
게이트가 실제로 막는지 알 수 없어요(AGENTS.md §Gates must not validate themselves).
"""
from __future__ import annotations

import importlib.util
import pathlib

_MODULE_PATH = pathlib.Path(__file__).with_name("portal_preflight.py")
_spec = importlib.util.spec_from_file_location("portal_preflight", _MODULE_PATH)
assert _spec and _spec.loader
preflight = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(preflight)


IA76_ACTIONS = [
    "GetWorkloadAccessTokenForUserId",
    "GetWorkloadAccessTokenForJWT",
    "GetWorkloadAccessToken",
    "GetResourceOauth2Token",
    "GetResourceApiKey",
]


def _policy_with(actions: list[str]) -> str:
    """합성 결과를 흉내낸 YAML 조각. 각 action 은 `bedrock-agentcore:` 접두어로 한 줄."""
    lines = ["      Statement:", "        - Effect: Deny", "          Action:"]
    for action in actions:
        lines.append(f"            - bedrock-agentcore:{action}")
    return "\n".join(lines) + "\n"


def test_all_actions_present_reports_no_missing() -> None:
    text = _policy_with(IA76_ACTIONS)
    assert preflight.missing_denied_actions(text, IA76_ACTIONS) == []


def test_one_missing_action_is_reported() -> None:
    # GetResourceApiKey 를 빼요 — 정확히 그게 누락으로 잡혀야 해요.
    present = [a for a in IA76_ACTIONS if a != "GetResourceApiKey"]
    text = _policy_with(present)
    assert preflight.missing_denied_actions(text, IA76_ACTIONS) == [
        "GetResourceApiKey"
    ]


def test_substring_collision_does_not_mask_missing_action() -> None:
    """`GetWorkloadAccessToken` 은 `GetWorkloadAccessTokenForUserId` 의 substring 이에요.

    standalone `GetWorkloadAccessToken` 이 정책에서 사라졌는데 `...ForUserId` 만 남으면,
    단순 substring 검사(`name in text`)는 **여전히 통과**해요(거짓 통과). 경계 인식
    검사는 그 action 을 누락으로 잡아야 해요.
    """
    # ForUserId 는 있고 standalone GetWorkloadAccessToken 은 없는 문서.
    present = [
        "GetWorkloadAccessTokenForUserId",
        "GetWorkloadAccessTokenForJWT",
        "GetResourceOauth2Token",
        "GetResourceApiKey",
    ]
    text = _policy_with(present)
    missing = preflight.missing_denied_actions(text, IA76_ACTIONS)
    assert missing == ["GetWorkloadAccessToken"], missing
    # 순진한 substring 검사는 이 거짓 통과를 놓쳐요(왜 경계 인식이 필요한지 못박아요).
    assert all(name in text for name in IA76_ACTIONS)
