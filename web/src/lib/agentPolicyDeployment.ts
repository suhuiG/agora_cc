import type {
  AgentPolicyDeployment,
  AgentPolicyDeployOutcome,
  AgentPolicyDeployResult,
} from "./api/identity.ts";

/**
 * `/policy-reconciliation` 이 이 값을 보내면 per-agent Cedar 층 «자체» 가 없어진 거예요 —
 * 어긋남(`drift`)도, 「볼 게 없음」(`not_applicable`)도, 「관측 못 함」(`unknown`)도 아니에요
 * (ADR-0093 · ADR-0112 결정 3).
 *
 * 서버 상수는 `access_router.PER_AGENT_POLICY_DEPRECATED_STATUS` 이고,
 * `deprecatedLayerSurfaces.test.ts` 가 그 소스를 읽어서 이 값과 대조해요 — 어긋나면 화면이
 * 폐기 배너 대신 「미배포」를 그려요.
 */
export const PER_AGENT_POLICY_DEPRECATED_STATUS = "per_agent_policy_deprecated";

// 배포 outcome 문구를 컴포넌트 밖에 둬요. `.tsx` 는 `node --test` 로 import 할 수 없어서
// (JSX 는 타입 제거만으로 실행되지 않아요) 컴포넌트 안에 있는 동안은 이 표를 테스트가 볼 수
// 없었고, 그래서 두 값이 빠진 채로 「빈 앰버 문단」과 리터럴 `undefined` 가 나갔어요 (IH-162 ①).
export const DEPLOY_OUTCOME_MESSAGES: Record<AgentPolicyDeployOutcome, string> = {
  DEPLOYED_ACTIVE: "Policy가 ACTIVE 상태로 배포됐어요.",
  DEPLOY_IN_PROGRESS: "Policy 배포가 진행 중이에요. 잠시 뒤 다시 확인해 주세요.",
  DEPLOY_FAILED: "Policy 배포에 실패했어요. validation findings를 확인하세요.",
  SKIPPED_NO_IDENTITY: "Agent identity가 없어 배포를 건너뛰었어요.",
  SKIPPED_NO_TOOL_ACCESS: "승인된 tool access가 없어 policy를 만들지 않았어요.",
  SKIPPED_NO_DEPLOYER:
    "M2 Policy Engine이 설정되지 않아 compile 결과를 PENDING으로 남겼어요.",
  // 「설정이 없어서 건너뜀」이 아니라 **설계상 만들지 않음** 이에요 (ADR-0093 · ADR-0099).
  // 다른 `SKIPPED_*` 와 같은 말로 보여주면 배선 오류처럼 읽혀요 — 서버 enum 주석의 요구예요.
  SKIPPED_PER_AGENT_DEPRECATED:
    "agent별 Cedar 정책은 폐기됐어요 (ADR-0093) — 설정 누락이 아니라 설계상 만들지 않아요. " +
    "도구 인가는 원장의 agent 도구 승인과 사람·그룹 도구 권한이 정해요.",
};

/** outcome 문구를 «빈 문자열 없이» 돌려줘요 — 모르는 값이 와도 화면이 비지 않아야 해요. */
export function deployOutcomeMessage(outcome: string): string {
  const known = (DEPLOY_OUTCOME_MESSAGES as Record<string, string>)[outcome];
  return (
    known ??
    `이 배포 결과(${outcome})에 대한 설명이 아직 화면에 없어요. 서버 응답을 확인해 주세요.`
  );
}

export function policyDeploymentFindings(
  deployment:
    | Pick<AgentPolicyDeployment, "validation_findings">
    | null
    | undefined,
): string[] {
  return deployment?.validation_findings ?? [];
}

export function policyDeploymentIsSuccessful(
  result: AgentPolicyDeployResult,
): boolean {
  return (
    result.outcome === "DEPLOYED_ACTIVE" &&
    policyDeploymentFindings(result.deployment).length === 0
  );
}

export function policyDeploymentPresentation(
  result: AgentPolicyDeployResult,
): { successful: boolean; findings: string[] } {
  return {
    successful: policyDeploymentIsSuccessful(result),
    findings: policyDeploymentFindings(result.deployment),
  };
}
