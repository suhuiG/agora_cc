"""도구 카탈로그 — 도구 메타의 읽기전용 코드 상수 정본 (AWS SoT 설계).

도구 목록·메타(area·license·compute·image·버전)는 여기서만 정의해요. git 리뷰와
CDK 배포로만 변경되고, 런타임(GovStore JSON)에서는 바뀌지 않아요. "배포됐나"(존재)는
deployment_probe가 AWS에서 실시간 열거하고, "뭘 검사하나"(메타)는 이 상수가 정본이에요.
"""
from __future__ import annotations

from .models import GovTool

# (tool_id, name, area, license, exec_kind, invoke_cmd, version, target_asset_types, compute, image_uri, needs_source)
#   compute/image_uri: 실제 배포 이미지가 있는 도구만 image_uri를 채워요(비어 있으면
#   dispatcher가 not_run). 지금 배포된 이미지: gitleaks·trivy(lambda)·semgrep(fargate).
#   needs_source: 업로드된 소스 번들이 있어야 검사가 성립하는 도구(True). 소스 없는 자산
#   (connect형 MCP·도메인 연결 agent)에서는 실행하지 않고 게이트를 not_applicable로 표시해요.
#   llm-judge만 False — 자산 descriptor(MCP tool 설명·agent card)로 판정할 수 있어요.
#   presidio(pii)는 제거됨(2026-07-17): NLP PII 탐지기가 소스코드엔 부적합해 오탐 폭증
#   (변수명→PERSON, URL/경로→URL PII, score 0.01까지). 코드 PII 우려는 gitleaks(시크릿)로
#   충분하고, agent 고유 위협(prompt injection·tool poisoning)은 별도 스캐너로 대체 예정
#   (Asana [Governance] Agent/MCP 위협분석 백로그).
TOOL_CATALOG: list[tuple] = [
    ("gitleaks", "Gitleaks", "secret", "MIT", "offline", "gitleaks detect", "v8.30.0", ("mcp", "skill", "agent"), "lambda", "agora-tool-gitleaks", True),
    ("semgrep", "Semgrep", "sast", "LGPL-2.1", "offline", "semgrep --config auto", "v1.16.0", ("mcp", "skill", "agent"), "fargate", "agora-tool-semgrep", True),
    ("trivy", "Trivy", "sbom_cve", "Apache-2.0", "cli", "trivy fs .", "v0.72.0", ("mcp", "agent"), "lambda", "agora-tool-trivy", True),
    # agentic-radar 제거됨(2026-07-17 조기검증): LangGraph/CrewAI 등 특정 프레임워크
    # 워크플로우만 스캔 — Agora의 범용 A2A/MCP raw 코드엔 부적합. agent 위협은 llm-judge가 커버.
    # cosign·garak·snyk-agent-scan 제거됨(2026-07-19): 미배포·미사용, 실배포 4종만 노출.
    #   cosign(signing, 서명 파이프라인 부재)·garak(llm_redteam, 라이브 LLM 필요)·
    #   snyk-agent-scan(mcp_poison, 실물 없음+클라우드 필수) — air-gap 스캔에서 완주 불가.
    # needs_source=False: 소스 대신 자산 descriptor(MCP tool 이름·설명·inputSchema, agent card)를
    # 입력으로 받을 수 있어 connect형 MCP·연결형 agent에서도 유일하게 실효 검사가 돼요.
    ("llm-judge", "LLM Judge", "agent_intent", "proprietary", "service", "bedrock converse", "v1", ("agent", "mcp", "skill"), "lambda", "agora-tool-llm-judge", False),
]

# 남은 4종은 전부 active(실제 스캔 파이프라인 대상·실배포): gitleaks·semgrep·trivy·llm-judge.
_ACTIVE_IDS = {"gitleaks", "semgrep", "trivy", "llm-judge"}


def list_catalog_tools() -> list[GovTool]:
    """도구 메타 정본을 GovTool 리스트로 반환해요(매 호출 새 객체 — 정본 불변)."""
    return [
        GovTool(
            tool_id=tid, name=name, area=area, repo=f"github.com/{tid}", license=lic,
            exec_kind=kind, invoke_cmd=cmd, timeout=60, finding_risk_map={},
            target_asset_types=targets,
            status="active" if tid in _ACTIVE_IDS else "staged",
            current_version=ver, compute=compute, image_uri=image_uri,
            needs_source=needs_source,
        )
        for tid, name, area, lic, kind, cmd, ver, targets, compute, image_uri, needs_source
        in TOOL_CATALOG
    ]
