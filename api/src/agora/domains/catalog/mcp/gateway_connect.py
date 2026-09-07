"""ConnectGatewayService — connect-mode MCP를 Gateway mcpServer 타깃으로 등재.

deploy(M1)와 달리 빌드·Lambda가 없어 동기 처리해요. tool 스키마는 등록 시 이미
확보한 것을 sanitize해 재사용해요. gateway_id 누락은 direct upstream으로 강등하지 않고
실패시켜 인가 없는 connect 자산이 영속화되지 않게 해요.
"""
from __future__ import annotations

import json

# Gateway target name 정규화는 shared.slug의 단일 함수를 써요 — 등록(여기)과 비교
# 시점(playground.scaffold의 폴백)이 같은 규칙을 공유해야 어긋나지 않아요(결함 #10).
from ....shared.slug import gateway_target_name as _gateway_target_name


class ConnectGatewayService:
    def __init__(self, *, port, gateway_id, now, legacy_gateway_id=""):
        self.port = port
        self.gateway_id = gateway_id
        self.legacy_gateway_id = legacy_gateway_id
        self.now = now

    def prepare_target(self, name: str) -> dict:
        """target 생성 전 durable descriptor에 넣을 소유 gateway/name 좌표를 만들어요."""
        if not self.gateway_id:
            raise RuntimeError(
                "connect registration requires an M2 OAuth Gateway identifier"
            )
        target_name = _gateway_target_name(name)
        return {
            "gateway_id": self.gateway_id,
            "gateway_url": self.port.gateway_endpoint(self.gateway_id),
            "target_name": target_name,
        }

    def register_target(self, name: str, endpoint: str, tools_inline: str) -> dict:
        prepared = self.prepare_target(name)
        sanitized = self._sanitize(tools_inline)
        # 실 AWS CreateGatewayTarget은 name을 ([0-9a-zA-Z][-]?){1,100}로 제한해요
        # (실측 2026-07-20: 공백·`.`·`_` 포함 시 ValidationException). 정규화해요.
        # Gateway는 이 정규화된 이름을 라이브 tool 접두어(`{target}___{op}`)에 써요.
        # 그래서 등록 시점의 이 값을 descriptor에 저장해 authorization 비교 기준을
        # 하나로 통일해요(결함 #10). 카탈로그 레코드 이름(slug)은 `.`·`_`를 남겨
        # 어긋나므로 재계산으로는 복원할 수 없거든요.
        target_name = prepared["target_name"]
        target_id = self.port.wire_mcp_server_target(
            self.gateway_id, target_name, endpoint, sanitized)
        return {
            "target_id": target_id,
            "gateway_id": self.gateway_id,
            "gateway_url": prepared["gateway_url"],
            "target_name": target_name,
        }

    def teardown(self, target_id: str, *, gateway_id: str = "") -> None:
        # IA-42 이전 descriptor에는 gateway owner가 없어요. 그 target들은 deploy
        # gateway에 생성됐으므로 legacy 좌표를 먼저 써야 새 OAuth gateway에서 엉뚱한
        # target ID를 지우지 않아요.
        owner_gateway_id = (
            gateway_id or self.legacy_gateway_id or self.gateway_id
        )
        if owner_gateway_id and target_id:
            self.port.delete_target(owner_gateway_id, target_id)

    def _sanitize(self, tools_inline: str) -> str:
        from agora.domains.runtime.deploy.tool_extract import sanitize_schema, inline_refs
        try:
            data = json.loads(tools_inline) if tools_inline else {"tools": []}
        except Exception:
            return tools_inline
        for t in data.get("tools", []):
            if "inputSchema" in t:
                t["inputSchema"] = sanitize_schema(inline_refs(t["inputSchema"]))
        return json.dumps(data)
