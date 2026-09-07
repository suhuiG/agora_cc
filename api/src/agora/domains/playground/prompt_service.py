"""system prompt 자동 생성 — Bedrock invoke_model + 템플릿 폴백.

governance/report_service.py의 Port+Bedrock+폴백 패턴을 계승해요.
LLM 실패/무크리덴셜이면 TemplatePromptGenerator로 폴백해 500을 내지 않아요.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Protocol, runtime_checkable

from .scaffold import ToolRef

_log = logging.getLogger(__name__)

# LLM이 결과 전체를 ```markdown ... ``` 코드펜스로 감쌀 때가 있어요. 감싼 경우에만
# 바깥 펜스를 벗겨요(안쪽 코드블록은 보존). system_prompt.md에 펜스가 새는 걸 막아요.
_FENCE_RE = re.compile(r"^\s*```[a-zA-Z0-9_-]*\n(.*)\n```\s*$", re.DOTALL)


def _strip_code_fence(text: str) -> str:
    m = _FENCE_RE.match(text)
    return m.group(1) if m else text


@runtime_checkable
class PromptGeneratorPort(Protocol):
    def generate(self, *, name: str, description: str, tools: list[ToolRef]) -> str: ...


def _tool_lines(tools: list[ToolRef]) -> str:
    if not tools:
        return "- (선택된 도구 없음)"
    kind_label = {"skill": "Skill", "mcp": "MCP", "agent": "Agent"}
    return "\n".join(
        f"- {t.name} ({kind_label.get(t.kind, t.kind)}): {t.description}".rstrip(": ")
        for t in tools
    )


class TemplatePromptGenerator:
    """LLM 없이 결정론적 마크다운 렌더 (폴백·무크리덴셜·테스트용)."""

    def generate(self, *, name: str, description: str, tools: list[ToolRef]) -> str:
        return f"""당신은 다음 역할을 수행하는 에이전트예요:
{description.strip() or "(설명을 입력하면 여기에 반영돼요)"}

## 원칙
- 근거 없는 추측은 하지 않아요. 모르면 모른다고 말해요.
- 주어진 도구를 적절히 활용해 정확한 답을 제공해요.
- 답변은 핵심부터, 필요한 경우에만 세부를 덧붙여요.
- **권한은 당신이 판정하지 않아요.** 호출자가 관리자인지, 어떤 데이터를 볼 수 있는지는
  볼 수 없어요. 도구를 호출하고, 권한 오류가 오면 그 메시지를 사용자에게 그대로 전해요.
  「관리자인지 확인한 뒤에」 같은 조건으로 스스로 막지 않아요.

## 사용 가능한 도구
{_tool_lines(tools)}"""


class BedrockPromptGenerator:
    """Bedrock invoke_model로 에이전트 맞춤 system prompt를 작성해요.

    boto3 bedrock-runtime.invoke_model 직접 호출(새 SDK 의존성 없음).
    모델 ID·리전은 deps에서 env로 주입돼요.
    """

    def __init__(self, model_id: str, region: str, client=None):
        self._model_id = model_id
        self._region = region
        self._client = client  # 테스트 주입용; 없으면 지연 생성

    def _rt(self):
        if self._client is None:
            import boto3
            self._client = boto3.client("bedrock-runtime", region_name=self._region)
        return self._client

    def generate(self, *, name: str, description: str, tools: list[ToolRef]) -> str:
        system = (
            "당신은 에이전트 설계 도우미예요. 주어진 에이전트 이름·설명·사용 가능한 도구를 "
            "바탕으로, 그 에이전트에 바로 쓸 수 있는 한국어 system prompt를 작성해요. "
            "역할·원칙·도구 사용 지침을 담되, 군더더기 없이 실용적으로 써요. 마크다운만 출력해요.\n\n"
            "**중요 — 권한 판정을 프롬프트에 넣지 마세요.** 에이전트는 호출자가 관리자인지, "
            "어떤 권한을 갖는지 **볼 수 없어요.** 권한은 플랫폼과 도구가 결정론적으로 판정하고, "
            "권한이 없으면 도구가 오류를 돌려줘요. 그러니 "
            "\"관리자인지 확인한 뒤 호출하세요\" 처럼 **에이전트가 확인할 수 없는 조건을 "
            "게이트로 삼는 문장은 절대 쓰지 마세요.** 그렇게 쓰면 에이전트가 도구를 아예 부르지 "
            "않고 스스로 거절해서, 권한이 있는 사용자도 쓸 수 없어요.\n"
            "설명에 역할별 차이가 적혀 있어도, 프롬프트에는 "
            "\"도구를 호출하고, 권한 오류가 오면 그 메시지를 사용자에게 그대로 전하세요\" 로 "
            "적어요. 판정은 도구의 일이에요."
        )
        user = (
            f"에이전트 이름: {name}\n"
            f"설명: {description}\n\n"
            f"사용 가능한 도구:\n{_tool_lines(tools)}\n\n"
            "위 정보로 이 에이전트의 system prompt를 작성해줘. 섹션: 역할 / 원칙 / 도구 사용 지침."
        )
        body = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 2000,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        resp = self._rt().invoke_model(modelId=self._model_id, body=json.dumps(body))
        out = json.loads(resp["body"].read())
        text = "".join(b.get("text", "") for b in out.get("content", []) if b.get("type") == "text")
        if not text.strip():
            raise RuntimeError("빈 LLM 응답")
        return _strip_code_fence(text)


class PromptService:
    """LLM 시도 → 실패 시 폴백. (캐싱 불필요 — 입력이 매번 달라짐.)"""

    def __init__(self, generator: PromptGeneratorPort, fallback=None):
        self._gen = generator
        self._fallback = fallback

    def generate(self, *, name: str, description: str, tools: list[ToolRef]) -> str:
        try:
            return self._gen.generate(name=name, description=description, tools=tools)
        except Exception as exc:
            if self._fallback is None:
                raise
            # 원인을 남기지 않고 삼키면 진단이 불가능해요 — 응답은 200이고 내용만
            # 템플릿으로 바뀌니, 운영자는 "LLM이 이상한 프롬프트를 줬다"로 오진해요.
            # 폴백 자체는 의도된 동작이라 실패시키지 않고 기록만 해요.
            _log.warning(
                "system prompt LLM 생성 실패 — 템플릿으로 폴백해요: %s: %s",
                type(exc).__name__,
                exc,
            )
            return self._fallback.generate(name=name, description=description, tools=tools)
