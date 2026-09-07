"""배포 파이프라인 — MCP 소스 → AgentCore Runtime → Gateway endpoint.

runtime 도메인 소유(런타임 오너). DeployPort 뒤에 Mock/Aws 어댑터가 있고, DeployJob
상태 머신이 poll-driven으로 단계를 전진해요. 카탈로그 등재는 catalog registry
port를 경유해요(직접 put_item 금지).
"""
from .models import (
    BuildArtifact, BuildType, DeployError, DeployJob, DeployPhase,
    GateRejected, SourceRef, SpecCheckError, terminal,
)

__all__ = [
    "BuildArtifact", "BuildType", "DeployError", "DeployJob", "DeployPhase",
    "GateRejected", "SourceRef", "SpecCheckError", "terminal",
]
