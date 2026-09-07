import { request } from "./client.ts";

export type AgentPolicyInventoryTone =
  | "pass"
  | "progress"
  | "warning"
  | "unknown";

export type GatewayPolicyCutoverPhase =
  | "CREATING"
  | "AWAITING_ACTIVE"
  | "DELETING_LEGACY"
  | "VERIFYING_CALL"
  | "COMPLETED"
  | "FAILED"
  | "UNKNOWN"
  | string;

export type GatewaySharedPolicyDeployment = {
  policy_key: string;
  policy_name: string;
  cedar_policy: string;
  policy_hash: string;
  target_count: number;
  size_bytes: number;
  agentcore_policy_id: string;
  remote_status: string;
  deployed_policy_hash: string;
  status_reasons: string[];
};

export type GatewayCallVerification = {
  status: string;
  reachable_call_observed: boolean;
  negative_control_observed: boolean;
  evidence_source: string;
  positive_call_id: string;
  negative_call_id: string;
  positive_target_event_id: string;
  negative_decision_event_id: string;
  observation_window_id: string;
  negative_control_kind: string;
  detail: string;
};

export type GatewayPolicyCutover = {
  gateway_arn: string;
  revision: number;
  phase: GatewayPolicyCutoverPhase;
  policies: GatewaySharedPolicyDeployment[];
  created_at: string;
  created_by: string;
  updated_at: string;
  version: number;
  delete_requested_policy_ids: string[];
  deleted_policy_ids: string[];
  unmanaged_policy_ids: string[];
  preserved_policy_ids: string[];
  call_verification: GatewayCallVerification;
  findings: string[];
};

export type PolicyInventoryItem = {
  policy_id: string;
  name: string;
  remote_status: string;
  agent_record_id: string | null;
  ledger_revision: number | null;
  ledger_status: string | null;
  reason: string;
};

export type AgentPolicyInventoryReport = {
  status: string;
  managed: PolicyInventoryItem[];
  orphan: PolicyInventoryItem[];
  unmanaged: PolicyInventoryItem[];
  reconciliation: unknown[];
  shared_cutovers: GatewayPolicyCutover[];
  shared_missing: string[];
  reason: string;
};

export type AgentPolicyInventoryView = {
  tone: AgentPolicyInventoryTone;
  empty: boolean;
  shouldPoll: boolean;
  latestCutover: GatewayPolicyCutover | null;
  /** 최신 revision 이 「굵은 문 한 장」 형태를 벗어난 사유예요. 비어 있어야 정상이에요. */
  policySetFindings: string[];
  policies: Array<{
    key: string;
    name: string;
    policyId: string;
    ledger: {
      phase: GatewayPolicyCutoverPhase;
      policyHash: string;
      deployedPolicyHash: string;
      checkpointStatus: string;
      sizeBytes: number;
      targetCount: number;
      cedarPolicy: string;
      cedarPolicySource: "ledger_expected";
    };
    live: {
      remoteStatus: string;
      reason: "in_sync" | "drift" | "missing" | "unknown";
      detail: string;
    };
  }>;
  additionalPolicies: Array<
    PolicyInventoryItem & {
      classification: "managed" | "orphan" | "unmanaged";
      preserved: boolean;
    }
  >;
};

const TERMINAL_PHASES = new Set<GatewayPolicyCutoverPhase>([
  "COMPLETED",
  "FAILED",
]);

function latestCutover(
  cutovers: GatewayPolicyCutover[],
): GatewayPolicyCutover | null {
  return cutovers.reduce<GatewayPolicyCutover | null>(
    (latest, item) =>
      latest === null || item.revision > latest.revision ? item : latest,
    null,
  );
}

function liveReason(
  item: PolicyInventoryItem | undefined,
  missing: boolean,
): "in_sync" | "drift" | "missing" | "unknown" {
  if (missing) return "missing";
  if (!item) return "unknown";
  if (item.reason.endsWith(":in_sync")) return "in_sync";
  if (item.reason.endsWith(":drift")) return "drift";
  return "unknown";
}

export function positiveCallVerificationQualified(
  verification: GatewayCallVerification,
): boolean {
  return (
    verification.status === "observed" &&
    verification.reachable_call_observed &&
    Boolean(verification.evidence_source) &&
    Boolean(verification.positive_call_id) &&
    Boolean(verification.positive_target_event_id) &&
    Boolean(verification.observation_window_id)
  );
}

/** 음성 대조 종류별 사람이 읽는 문구예요.
 *
 * 강제 지점이 바뀌면 이 목록도 같이 바뀌어요 (ADR-0099 결정 8). 도구를 막는 것은 REQUEST
 * interceptor 하나이고, 그 두 층을 각각 겨눈 대조만 인정해요.
 *
 * ⚠️ 옛 값 `danger_scope_absent`·`danger_scope_prefix_overlap` 은 **남기지 않아요.** Cedar
 * danger 백스톱을 없앤 뒤로는 시험할 `forbid` 가 존재하지 않아서 그 대조를 만들 수가 없어요.
 * 서버(`agent_policy_cutover.GatewayCallVerifier`)가 옛 값을 거부하는데 화면이 받아 주면,
 * 아무것도 시험하지 않은 기록을 「이빨 확인됨」으로 그리게 돼요.
 */
const NEGATIVE_CONTROL_KIND_LABELS = new Map<string, string>([
  [
    "tool_binding_absent",
    "④ 봇에게 승인되지 않은 도구를 불러 거부되는지 확인한 대조예요",
  ],
  [
    "human_grant_absent",
    "④ 는 있고 ⑦ 사람·그룹 권한이 없어 거부되는지 확인한 대조예요",
  ],
]);

export type NegativeControlKindDescription = {
  /** 지원하는 종류일 때만 true 예요. 모르는 종류는 초록불이 아니에요. */
  supported: boolean;
  label: string;
};

/** 음성 대조 종류를 설명해요. 모르는 종류·미기록은 **확인으로 인정하지 않아요**. */
export function describeNegativeControlKind(
  kind: string,
): NegativeControlKindDescription {
  const label = NEGATIVE_CONTROL_KIND_LABELS.get(kind);
  if (label !== undefined) {
    return { supported: true, label };
  }
  return {
    supported: false,
    label: kind
      ? `알 수 없는 음성 대조 종류라 확인으로 인정하지 않아요: ${kind}`
      : "음성 대조 종류가 기록되지 않았어요",
  };
}

export function negativeCallVerificationQualified(
  verification: GatewayCallVerification,
): boolean {
  return (
    verification.status === "observed" &&
    verification.negative_control_observed &&
    Boolean(verification.evidence_source) &&
    Boolean(verification.negative_call_id) &&
    Boolean(verification.negative_decision_event_id) &&
    Boolean(verification.observation_window_id) &&
    describeNegativeControlKind(verification.negative_control_kind).supported
  );
}

/** 공유 정책 세트의 기대 형태예요 — **굵은 문 정확히 한 장** (ADR-0099 결정 8).
 *
 * 옛 형태는 「굵은 문 1 + danger 백스톱 N장」이었어요. 화면이 백스톱을 세거나 기대하면,
 * 없어진 `forbid` 를 여전히 보호막처럼 그려요. 서버 계약은
 * `agent_policy_cutover._validate_compiled` 예요.
 */
const EXPECTED_SHARED_POLICY_KEYS = ["coarse-gate"];

/** 원장 revision 이 기대 형태를 벗어났으면 그 사유를 돌려줘요. 통과로 삼키지 않아요. */
export function sharedPolicySetFindings(
  cutover: GatewayPolicyCutover,
): string[] {
  const findings: string[] = [];
  const keys = cutover.policies.map((policy) => policy.policy_key);
  if (
    keys.length !== EXPECTED_SHARED_POLICY_KEYS.length ||
    keys.some((key, index) => key !== EXPECTED_SHARED_POLICY_KEYS[index])
  ) {
    findings.push(
      `공유 정책 세트가 굵은 문 한 장이 아니에요: ${
        keys.join(", ") || "정책 없음"
      }. Cedar danger 백스톱은 ADR-0099 결정 8로 없앴어요.`,
    );
  }
  for (const policy of cutover.policies) {
    if (policy.target_count > 0) {
      findings.push(
        `굵은 문은 Target 을 갖지 않아요: ${policy.policy_key} 가 Target ${policy.target_count}개를 들고 있어요.`,
      );
    }
  }
  return findings;
}

export function callVerificationQualified(
  verification: GatewayCallVerification,
): boolean {
  return (
    positiveCallVerificationQualified(verification) &&
    negativeCallVerificationQualified(verification) &&
    verification.positive_call_id !== verification.negative_call_id
  );
}

export function buildAgentPolicyInventoryView(
  report: AgentPolicyInventoryReport,
): AgentPolicyInventoryView {
  const latest = latestCutover(report.shared_cutovers);
  const managedById = new Map(
    report.managed.map((item) => [item.policy_id, item]),
  );
  const sharedIds = new Set(
    latest?.policies
      .map((policy) => policy.agentcore_policy_id)
      .filter(Boolean) ?? [],
  );
  const preservedIds = new Set(latest?.preserved_policy_ids ?? []);
  const missingIds = new Set(report.shared_missing);
  const policies = (latest?.policies ?? []).map((policy) => {
    const live = managedById.get(policy.agentcore_policy_id);
    const reason = liveReason(
      live,
      missingIds.has(policy.agentcore_policy_id),
    );
    return {
      key: policy.policy_key,
      name: policy.policy_name,
      policyId: policy.agentcore_policy_id,
      ledger: {
        phase: latest?.phase ?? "UNKNOWN",
        policyHash: policy.policy_hash,
        deployedPolicyHash: policy.deployed_policy_hash,
        checkpointStatus: policy.remote_status,
        sizeBytes: policy.size_bytes,
        targetCount: policy.target_count,
        cedarPolicy: policy.cedar_policy,
        cedarPolicySource: "ledger_expected" as const,
      },
      live: {
        remoteStatus: live?.remote_status ?? "UNKNOWN",
        reason,
        detail: live?.reason ?? (reason === "missing" ? "shared_missing" : ""),
      },
    };
  });
  const additionalPolicies: AgentPolicyInventoryView["additionalPolicies"] = [
    ...report.managed
      .filter((item) => !sharedIds.has(item.policy_id))
      .map((item) => ({
        ...item,
        classification: "managed" as const,
        preserved: preservedIds.has(item.policy_id),
      })),
    ...report.orphan.map((item) => ({
      ...item,
      classification: "orphan" as const,
      preserved: preservedIds.has(item.policy_id),
    })),
    ...report.unmanaged.map((item) => ({
      ...item,
      classification: "unmanaged" as const,
      preserved: false,
    })),
  ].sort((left, right) => left.policy_id.localeCompare(right.policy_id));
  const policySetFindings = latest ? sharedPolicySetFindings(latest) : [];
  const deletionFinished =
    latest?.phase === "VERIFYING_CALL" || latest?.phase === "COMPLETED";
  const unexpectedAdditionalPolicy =
    report.unmanaged.length > 0 ||
    (deletionFinished &&
      additionalPolicies.some((policy) => !policy.preserved));

  let tone: AgentPolicyInventoryTone;
  if (report.status !== "observed" || latest?.phase === "UNKNOWN") {
    tone = "unknown";
  } else if (!latest) {
    tone = "unknown";
  } else if (
    policies.some(
      (policy) =>
        policy.live.reason === "missing" ||
        policy.live.reason === "drift" ||
        policy.live.remoteStatus !== "ACTIVE",
    ) ||
    unexpectedAdditionalPolicy ||
    latest.phase === "FAILED" ||
    latest.findings.length > 0 ||
    policySetFindings.length > 0
  ) {
    tone = "warning";
  } else if (!TERMINAL_PHASES.has(latest.phase)) {
    tone = "progress";
  } else if (
    latest.phase === "COMPLETED" &&
    policies.length > 0 &&
    policies.every((policy) => policy.live.reason === "in_sync") &&
    callVerificationQualified(latest.call_verification)
  ) {
    tone = "pass";
  } else {
    tone = "unknown";
  }

  return {
    tone,
    empty: report.status === "observed" && latest === null,
    shouldPoll:
      report.status !== "observed" ||
      (latest !== null && !TERMINAL_PHASES.has(latest.phase)),
    latestCutover: latest,
    policySetFindings,
    policies,
    additionalPolicies,
  };
}

export function getAgentPolicyInventory(): Promise<AgentPolicyInventoryReport> {
  return request<AgentPolicyInventoryReport>(
    "/api/admin/identity/agent-policy-inventory",
  );
}
