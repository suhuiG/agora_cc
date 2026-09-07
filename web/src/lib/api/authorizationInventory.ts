import { JSON_HEADERS, request } from "./client";

export type ObservationCount = number | "unknown";
export type ObservationDistribution = Record<string, number> | "unknown";

export type PolicyInventoryItem = {
  policy_id: string;
  name: string;
  remote_status: string;
  agent_record_id: string | null;
  ledger_revision: number | null;
  ledger_status: string | null;
  reason: string;
};

export type AgentAuthorizationInventoryItem = {
  record_id: string;
  identity_type: string;
  identity_status: string;
  policy_principal_id: string;
  client_id: string;
  client_claim: string;
  tool_count: number;
  tool_effective_state_counts: Record<string, number>;
  policy_count: number;
  policy_status_counts: Record<string, number>;
  cedar_observation: "observed" | "unknown";
  cedar_in_sync: string[];
  cedar_stale: string[];
  cedar_drift: string[];
  cedar_missing: string[];
  client_exists: boolean | "unknown";
  catalog_record: string;
  classification: string;
  authorization_verdict:
    | "coherent"
    | "diverged"
    | "not_applicable"
    | "unknown";
  fail_closed_blocked: boolean;
  reasons: string[];
};

export type CognitoClientInventoryItem = {
  client_id: string;
  name: string;
  reason: string;
};

export type AuthorizationReclaimCandidate = {
  record_id: string;
  client_id: string;
  policy_ids: string[];
  classification: string;
  reason: string;
};

export type AuthorizationInventorySummary = {
  identity_total: ObservationCount;
  agents_without_tools_or_policies: ObservationCount;
  missing_client_reference_count: ObservationCount;
  orphan_client_count: ObservationCount;
  tool_effective_state_counts: ObservationDistribution;
  policy_status_counts: ObservationDistribution;
  catalog_orphan_count: ObservationCount;
  fail_closed_evaluated_agent_count: ObservationCount;
  fail_closed_blocked_agent_count: ObservationCount;
  not_applicable_agent_count: ObservationCount;
  unknown_agent_count: ObservationCount;
};

export type AuthorizationInventory = {
  status: "observed" | "unknown";
  managed: PolicyInventoryItem[];
  orphan: PolicyInventoryItem[];
  unmanaged: PolicyInventoryItem[];
  reconciliation: Array<{
    agent_record_id: string;
    catalog_record_present: boolean | "unknown";
    in_sync: string[];
    stale: string[];
    drift: string[];
    missing: string[];
  }>;
  agents: AgentAuthorizationInventoryItem[];
  orphan_clients: CognitoClientInventoryItem[];
  reclaim_candidates: AuthorizationReclaimCandidate[];
  summary: AuthorizationInventorySummary;
  reason: string;
};

export type AuthorizationGateAssessment = {
  state: "not_applicable" | "unknown" | "observed";
  observation_id: string;
  target_agent_count: number | null;
  evaluated_agent_count: number | null;
  affected_agent_count: number | null;
  affected_agent_ids: string[];
  reason: string;
  can_approve: boolean;
};

export type AuthorizationGateApprovalEvidence = {
  gate_state: string;
  target_agent_count: number;
  evaluated_agent_count: number;
  affected_agent_count: number;
  affected_agent_ids: string[];
  inventory_summary: AuthorizationInventorySummary;
  observation_snapshot: {
    schema_version: 1;
    inventory: AuthorizationInventory;
    inventory_status: string;
    inventory_reason: string;
    inventory_summary: AuthorizationInventorySummary;
    catalog_agent_ids: string[];
    unknown_agent_ids: string[];
  };
};

export type AuthorizationGateApproval = {
  approved_by: string;
  approved_at: string;
  observation_id: string;
  evidence: AuthorizationGateApprovalEvidence;
};

export type AuthorizationGateApprovalHistoryItem = {
  state: "observed" | "unknown";
  reason: string;
  approved_by: string;
  approved_at: string;
  observation_id: string;
};

export type AuthorizationInventoryGateView = {
  inventory: AuthorizationInventory;
  gate: AuthorizationGateAssessment;
  approval_observation: "observed" | "unknown";
  approval_observation_reason: string;
  latest_approval: AuthorizationGateApproval | null;
  approval_history: AuthorizationGateApprovalHistoryItem[];
  approved: boolean;
};

const GATE_PATH =
  "/api/admin/identity/fail-closed-authorization-gate";

export function getAuthorizationInventoryGate():
  Promise<AuthorizationInventoryGateView> {
  return request<AuthorizationInventoryGateView>(GATE_PATH);
}

export function approveAuthorizationInventoryGate(
  observationId: string,
): Promise<AuthorizationInventoryGateView> {
  return request<AuthorizationInventoryGateView>(`${GATE_PATH}/approve`, {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify({
      observation_id: observationId,
      confirmation: "approve_observed_inventory",
    }),
  });
}
