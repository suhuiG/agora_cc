"""타입별 AssetBinding — 자산 타입마다 다른 부분만 분리.

공통(파일트리·버전·감사)은 SourceStorePort가, 타입별 필수 파일 검증과
descriptors 구성은 여기가 담당해요. agent 카탈로그/playground 확장 시
새 Binding을 추가하고 _REGISTRY에 등록하면 끝이에요.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from .models import Manifest, SourceStoreError


@runtime_checkable
class AssetBinding(Protocol):
    """skill·mcp·agent 공통 계약."""

    asset_type: str

    def validate(self, manifest: Manifest) -> None:
        """타입별 필수 파일 검증. 실패 시 SourceStoreError."""
        ...

    def build_descriptors(self, manifest: Manifest, extra: dict) -> dict:
        """RegistryPort.create_record에 넘길 descriptors 구성."""
        ...


class _RequireFileBinding:
    """필수 파일 1개를 요구하는 공통 베이스."""

    asset_type = ""
    required_file = ""
    descriptor_key = ""

    def validate(self, manifest: Manifest) -> None:
        if self.required_file not in manifest.paths():
            raise SourceStoreError(
                f"{self.asset_type} requires '{self.required_file}' at the root of "
                f"the uploaded folder"
            )

    def build_descriptors(self, manifest: Manifest, extra: dict) -> dict:
        return {self.descriptor_key: {"sourcePrefix": extra.get("s3_prefix", "")}}


class SkillBinding(_RequireFileBinding):
    asset_type = "skill"
    required_file = "SKILL.md"
    descriptor_key = "skill"


class AgentBinding(_RequireFileBinding):
    """A2A agent. 필수 파일은 agent-card.json (v0.3.0 규약). 구 규약 agent.json도 허용."""

    asset_type = "agent"
    required_file = "agent-card.json"
    descriptor_key = "agent"

    def validate(self, manifest: Manifest) -> None:
        paths = manifest.paths()
        if self.required_file not in paths and "agent.json" not in paths:
            raise SourceStoreError(
                "agent requires 'agent-card.json' (or legacy 'agent.json') at the root "
                "of the uploaded folder"
            )

    def build_descriptors(self, manifest: Manifest, extra: dict) -> dict:
        # 업로드된 agent-card 본문(extra['agent_card'])이 있으면 agentCard.inlineContent로 담아,
        # aws_mapping이 A2A 카드 원본을 무손실로 registry에 올려요. 없으면 sourcePrefix만.
        node: dict = {"sourcePrefix": extra.get("s3_prefix", "")}
        card_text = extra.get("agent_card")
        if card_text:
            node["agentCard"] = {"inlineContent": card_text}
        return {self.descriptor_key: node}


class McpBinding:
    """배포형 MCP. 소스 파일 트리 + Runtime 배포 후 확보한 endpoint·tools를 descriptor로.

    connect 모드(catalog/mcp/registry.py)와 동일한 mcp descriptor 형태를 만들어,
    카탈로그 상세·설치 경로가 배포형·연결형을 구분 없이 다루게 해요.
    """

    asset_type = "mcp"

    def validate(self, manifest: Manifest) -> None:
        paths = manifest.paths()
        if not any(p.endswith(".py") or p.endswith("pyproject.toml") for p in paths):
            raise SourceStoreError(
                "mcp requires at least one Python source (.py or pyproject.toml)"
            )

    def build_descriptors(self, manifest: Manifest, extra: dict) -> dict:
        node: dict = {"sourcePrefix": extra.get("s3_prefix", "")}
        if extra.get("endpoint"):
            node["endpoint"] = extra["endpoint"]
        if extra.get("tools_inline"):
            node["tools"] = {"inlineContent": extra["tools_inline"]}
        if extra.get("gateway_identifier"):
            node["gatewayIdentifier"] = extra["gateway_identifier"]
        if "gateway_targets" in extra:
            node["gatewayTargets"] = list(extra["gateway_targets"])
        if "unassigned_tools" in extra:
            node["unassignedTools"] = list(extra["unassigned_tools"])
        # 배포형 MCP도 connect처럼 gatewayTargetName을 저장해요(CA-07). 이게 없으면
        # _mcp_gateway_target이 빈값→tool 인가 propose가 409(gateway 미연결)로 막히고,
        # agent 인가 비교가 slug 폴백으로 어긋나요. 실제 Gateway target 이름과 동일해야 해
        # 배포 시 target에 쓴 이름(job.meta.name)을 그대로 넘겨받아 저장해요.
        if extra.get("gateway_target_name"):
            node["gatewayTargetName"] = extra["gateway_target_name"]
        return {"mcp": node}


_REGISTRY: dict[str, AssetBinding] = {
    "skill": SkillBinding(),
    "agent": AgentBinding(),
    "mcp": McpBinding(),
}


def get_binding(asset_type: str) -> AssetBinding:
    binding = _REGISTRY.get(asset_type)
    if binding is None:
        raise SourceStoreError(f"unknown asset_type: {asset_type!r}")
    return binding
