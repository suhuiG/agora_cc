"""Gateway 거부 사유의 사람용 문구 — **단일 출처** (ADR-0104, IH-183).

## 왜 domain 이 아니라 shared 인가

이 표의 소비자가 세 도메인에 걸쳐 있어요.

- `identity/gateway_interceptor.py` — 거부를 **만드는** 쪽. JSON-RPC `message` 에 실어요.
- `playground/scaffold.py` — 생성 agent 가 도구 probe 결과에서 거부를 **알아보는** 쪽.
- `runtime/deploy/verify.py` — 포털이 배포 실패 문구를 **렌더하는** 쪽.

도메인끼리 직접 import 하지 않는다는 규약(AGENTS.md §Architecture And Ownership) 때문에
공용 계약은 여기 있어야 해요. 사본을 두면 문장을 한쪽만 고쳤을 때 정확 일치가 조용히
실패하고 — 사유 전달이 **꺼진 것도 모르게** 꺼져요.

## 왜 정확 일치인가

Strands 는 `McpError` 를 `str(exception)` 으로 접어요. 그 값은 `error.message` 뿐이고
(`api/tests/test_gateway_denial_mcp_sdk_contract.py` 가 그 계약을 못박아요), interceptor 가
거기 싣는 값은 아래 표의 문장 **그대로**예요. 그래서 프로즈 파싱이 필요 없고, 해서도 안 돼요 —
부분 일치를 허용하면 MCP 가 자기 에러 본문에 이 문장을 끼워 넣어 「권한 문제예요」로
위장할 수 있어요.

## ⚠️ 주체를 특정하는 값은 절대 넣지 마세요

ARN·client_id·handle·email·원장 id·스택 트레이스 금지. 이 문구는 배포 agent 를 거쳐 최종
사용자 화면까지 그대로 갈 수 있어요. 문장은 사유 자체만 말하고 좌표를 말하지 않아요.
"""
from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

#: 사유 슬러그 → 사람이 읽는 한국어 문구.
#:
#: 키는 `identity.gateway_interceptor.GatewayDenialReason` 의 값과 같은 문자열이에요. 여기에
#: enum 을 쓰지 않는 이유는 그 enum 이 identity 도메인 소유라서예요 — 이 모듈이 그걸 import
#: 하면 shared 가 domain 에 의존하게 돼요.
#:
#: **여기 없는 사유는 일부러 없어요.** 요청 형식·위임·신원 불일치·미허용 method 는 사용자가
#: 고칠 수 있는 것이 없고, 사유별 문장을 지어내면 내부 상태를 추측하게 만들어요.
DENIAL_MESSAGES_BY_REASON: Mapping[str, str] = MappingProxyType({
    "tool_pending_approval": (
        "이 도구는 관리자 승인 대기 중이에요. 승인되면 바로 호출할 수 있어요."
    ),
    "tool_not_approved": (
        "이 도구는 이 agent 에 승인되지 않았어요."
    ),
    "human_grant_missing": (
        "호출한 사람에게 이 도구 권한(grant)이 없어요."
    ),
})

#: 위 표에 없는 사유가 쓰는 고정 문구. **역방향 조회 대상이 아니에요** — 이 문장은 사유를
#: 구별하지 않으니 이걸로 슬러그를 만들면 서로 다른 거부가 한 사유로 뭉쳐요.
DEFAULT_DENIAL_MESSAGE = "Request denied by Agora authorization"

#: 문구 → 슬러그. 위 표에서 파생해요(사본이 아니에요).
_REASON_BY_MESSAGE: Mapping[str, str] = MappingProxyType({
    message: reason for reason, message in DENIAL_MESSAGES_BY_REASON.items()
})


def reason_for_denial_message(text: object) -> str:
    """이 문장이 Agora 가 쓴 거부 문구면 사유 슬러그, 아니면 빈 문자열이에요.

    정확 일치예요. 앞뒤 공백만 벗겨요 — 부분 일치를 허용하면 MCP 가 자기 본문에 이 문장을
    끼워 넣어 다른 실패를 인가 거부로 위장할 수 있어요.
    """
    if not isinstance(text, str):
        return ""
    return _REASON_BY_MESSAGE.get(text.strip(), "")


def denial_message_for_reason(reason: object) -> str:
    """알려진 사유 슬러그의 문장, 모르는 슬러그면 빈 문자열이에요.

    호출부가 **신뢰할 수 없는 입력**(배포 agent 가 보낸 값)을 그대로 넘겨도 안전하게
    설계했어요 — 모르는 값에서 문장을 지어내지 않아요.
    """
    if not isinstance(reason, str):
        return ""
    return DENIAL_MESSAGES_BY_REASON.get(reason.strip(), "")
