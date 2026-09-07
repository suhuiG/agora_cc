import { request } from "./client";

export type MetricSource = "span" | "log" | "ledger" | "aws_api" | "aggregate";
export type UnobservedReason =
  | "no_instrumentation"
  | "no_aggregate"
  | "cost_contract_unavailable"
  | "population_incomplete"
  | "not_sampled"
  | "sampling_unknown"
  | "pipeline_lag"
  | "ingest_stalled"
  | "traffic_unobserved"
  | "pipeline_timestamps_unobserved"
  | "not_applicable";

type MetricBase = {
  as_of: string | null;
  source: MetricSource;
  sampling_rate: number | null;
};

export type ObservedMetric = MetricBase & {
  value: number;
  status: "ok";
};

export type UnobservedMetric = MetricBase & {
  value: null;
  status: "unobserved";
  reason: UnobservedReason | null;
};

export type Metric = ObservedMetric | UnobservedMetric;

export type SummaryTile = {
  primary: Metric;
  secondary: Metric | null;
};

export type PipelineHealth = {
  status: "ok" | "idle" | "unknown" | "stale" | "failed";
  last_success_at: string | null;
  newest_span_at: string | null;
  failure_count: number;
  dlq_depth: number | null;
  pending_failure_count: number | null;
  reason: string | null;
  gap_start_at: string | null;
  gap_end_at: string | null;
  replay_status: "partial" | "completed" | null;
  replay_expected_count: number;
  replay_recovered_count: number;
  replay_failed_count: number;
  traffic_window_start?: string;
  traffic_window_end?: string;
  traffic_count?: number;
  traffic_observation_status?: "ok" | "unknown";
  traffic_observation_reason?: string;
};

export type AgentMonitoring = {
  record_id: string;
  name: string;
  version: string;
  owner: string;
  owner_team: string;
  registry_status: string;
  deployment: {
    phase: string | null;
    as_of: string | null;
  };
  reconciliation: {
    expected: string[] | null;
    observed: string[] | null;
    verdict: "coherent" | "diverged" | "unknown";
    reason:
      | "unknown_authorization"
      | "not_applicable"
      | "builtin_reachability_unprobed"
      | "verify_missing"
      | "report_invalid"
      | "invalid_tool_identifier"
      | "verify_unknown"
      | "self_validation"
      | "deployment_version_mismatch"
      | "verdict_conflict"
      | "declaration_unresolvable"
      | null;
    expected_source: "deployment_ledger" | "runtime_selfcheck";
    observed_source: "deployment_ledger" | "runtime_selfcheck";
    self_validation: boolean;
  };
  instrumentation: "enabled" | "not_configured" | "unknown";
  metrics: {
    invocations: Metric;
    tokens: Metric;
    estimated_cost: Metric;
    p95_latency: Metric;
    error_rate: Metric;
  };
  pipeline: PipelineHealth;
  governance: {
    scan_status: string | null;
    scan_risk: string | null;
    gate_verdict: string | null;
    as_of: string | null;
    reason: "scan_version_mismatch" | "scan_version_unknown" | null;
  };
};

export type AgentMonitoringFleet = {
  summary: {
    coherence_anomalies: SummaryTile;
    authorization_denials: SummaryTile;
    cost: SummaryTile;
    errors: SummaryTile;
    observability_health: SummaryTile;
  };
  agents: AgentMonitoring[];
  pipeline: PipelineHealth;
  observed_population: number;
  truncated: boolean;
};

export function getAgentMonitoringFleet(): Promise<AgentMonitoringFleet> {
  return request<AgentMonitoringFleet>("/api/admin/agents/monitoring");
}
