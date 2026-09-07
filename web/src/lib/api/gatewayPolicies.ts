// Gateway 별 Cedar 정책 콘솔 API.
//
// 기존 `agentPolicyInventory` 는 **원장이 아는 agent** 를 대조해요. 그래서 원장에 없는
// 정책(옛 PoC·손으로 만든 것·컷오버 잔재)은 화면에 아예 안 나와요. 이 모듈은 Gateway 를
// 출발점으로 잡아 그 사각지대를 봐요. 단일 출처는
// `api/src/agora/domains/identity/policy_console.py` 예요.

import { JSON_HEADERS, request } from "./client.ts";

// `agora-domain-rule` = 도메인 정책 화면(`/admin/domain-policies`)이 만든 값 조건 정책이에요
// (IH-132). 여기서 빼면 그 정책이 «외부 · 수동» 으로 보여서 출처를 모르는 정책으로 읽혀요.
export type PolicyOwnership =
  | "agora-agent"
  | "agora-shared"
  | "agora-domain-rule"
  | "external";

export type GatewayConsolePolicy = {
  policy_id: string;
  name: string;
  status: string;
  enforcement_mode: string;
  created_at: string;
  updated_at: string;
  cedar: string;
  size_bytes: number;
  status_reasons: string[];
  ownership: PolicyOwnership;
  actions: string[];
  /** `action,` 만 쓴 굵은 정책. action 0개와 구분해야 해요. */
  action_unrestricted: boolean;
  /** Gateway 가 더 이상 갖지 않는 action. 낡음의 근거예요. */
  stale_actions: string[];
  /** Agora 가 컴파일하는 정책이라 다음 배포가 편집을 덮어써요. */
  edit_overwritten_by_deploy: boolean;
  /** `OAuthUser` | `IamEntity` | "" (문장을 못 읽었을 때) */
  principal_type: string;
  /** `principal is <Type>` 처럼 개체를 지목하지 않는 정책은 비어요. */
  principal_ids: string[];
  read_error: string;
};

export type GatewayConsoleEntry = {
  gateway_id: string;
  name: string;
  gateway_arn: string;
  engine_id: string;
  engine_arn: string;
  enforcement_mode: string;
  engine_observed: boolean;
  reason: string;
  target_names: string[];
  valid_actions: string[];
  /** false 면 낡음 판정을 하지 않았어요. 빈 `stale_actions` 를 "깨끗함" 으로 읽으면 안 돼요. */
  actions_observed: boolean;
  policies: GatewayConsolePolicy[];
};

export type GatewayPolicyReport = {
  gateways: GatewayConsoleEntry[];
  warnings: string[];
};

export type PolicyDeleteResult = {
  deleted: boolean;
  absence_confirmed: boolean;
  reason: string;
};

export type PolicyUpdateResult = {
  status: string;
  ok: boolean;
  previous_cedar: string;
  status_reasons: string[];
  reason: string;
};

const BASE = "/api/admin/identity/gateway-policies";

export function getGatewayPolicies(): Promise<GatewayPolicyReport> {
  return request<GatewayPolicyReport>(BASE);
}

export function deleteGatewayPolicy(
  engineId: string,
  policyId: string,
): Promise<PolicyDeleteResult> {
  return request<PolicyDeleteResult>(
    `${BASE}/${encodeURIComponent(engineId)}/${encodeURIComponent(policyId)}`,
    { method: "DELETE" },
  );
}

export function updateGatewayPolicyCedar(
  engineId: string,
  policyId: string,
  cedar: string,
): Promise<PolicyUpdateResult> {
  return request<PolicyUpdateResult>(
    `${BASE}/${encodeURIComponent(engineId)}/${encodeURIComponent(policyId)}`,
    { method: "PUT", headers: JSON_HEADERS, body: JSON.stringify({ cedar }) },
  );
}

export type PolicyTone = "stale" | "unknown" | "unrestricted" | "ok";

/** 정책 한 장의 표시 색을 정해요. 판정 순서가 곧 우선순위예요. */
export function policyTone(
  policy: GatewayConsolePolicy,
  actionsObserved: boolean,
): PolicyTone {
  if (policy.read_error) return "unknown";
  if (policy.status && policy.status !== "ACTIVE") return "unknown";
  if (policy.stale_actions.length > 0) return "stale";
  // 관측을 못 했으면 "깨끗함" 이라고 말할 수 없어요.
  if (!actionsObserved) return "unknown";
  if (policy.action_unrestricted) return "unrestricted";
  return "ok";
}

export type GatewaySummary = {
  total: number;
  stale: number;
  unknown: number;
  external: number;
};

export function summarize(entry: GatewayConsoleEntry): GatewaySummary {
  let stale = 0;
  let unknown = 0;
  let external = 0;
  for (const policy of entry.policies) {
    const tone = policyTone(policy, entry.actions_observed);
    if (tone === "stale") stale += 1;
    if (tone === "unknown") unknown += 1;
    if (policy.ownership === "external") external += 1;
  }
  return { total: entry.policies.length, stale, unknown, external };
}
