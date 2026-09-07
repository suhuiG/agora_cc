// 스캔 도구 정적 카탈로그 (SP-3 SoT). 앱 GovTool.compute/image_uri와 동기화 대상.
// GA에선 CDK가 GovStore를 읽어 동적 생성 방향이나, P1은 정적(YAGNI).
export interface ScanToolDef {
  toolId: string;
  area: string;
  compute: "lambda" | "fargate";
  imageDir: string;    // Dockerfile 경로(빌드 컨텍스트 infra/scan-tools 기준)
  memoryMiB: number;
  timeoutSec: number;
}

export const SCAN_TOOLS: ScanToolDef[] = [
  { toolId: "gitleaks", area: "secret", compute: "lambda", imageDir: "gitleaks", memoryMiB: 1024, timeoutSec: 300 },
  { toolId: "semgrep", area: "sast", compute: "fargate", imageDir: "semgrep", memoryMiB: 4096, timeoutSec: 300 },
  { toolId: "trivy", area: "sbom_cve", compute: "lambda", imageDir: "trivy", memoryMiB: 2048, timeoutSec: 300 },
  { toolId: "llm-judge", area: "agent_intent", compute: "lambda", imageDir: "llm-judge", memoryMiB: 1024, timeoutSec: 300 },
  // agentic-radar(agent_threat) 제거됨(2026-07-17 조기검증): LangGraph/CrewAI 등 특정
  // 프레임워크 워크플로우만 스캔 — 범용 A2A/MCP raw 코드엔 부적합. agent 위협은 llm-judge가 커버.
  // presidio(pii) 제거됨(2026-07-17): NLP PII 탐지기가 소스코드엔 부적합해 오탐 폭증.
];
