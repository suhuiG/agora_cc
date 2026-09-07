import { request } from "./client";

export type AuditDecision = "ALLOW" | "DENY";

export type AuditEvent = {
  event_id: string;
  invocation_id: string;
  principal_id: string;
  agent_id: string;
  asset_id: string;
  operation_id: string;
  connection_id: string;
  capabilities: string[];
  decision: AuditDecision;
  reason: string;
  target: string;
  timestamp: string;
  workload_id: string;
  event_type: string;
  severity: string;
  failure_type: string;
  mode: string;
  policy_deployment_outcome: string;
  policy_revision: number;
  validation_findings: string[];
  request_justification: string;
  trace_id: string;
  span_id: string;
  // ADR-0065: decision=ALLOW 는 「호출 인가 통과」까지만 뜻해요. Runtime 실행 결과는 이 필드예요.
  // 필드가 없던 옛 레코드는 빈 문자열이고, 그건 성공이 아니라 미관측이에요.
  invocation_outcome: string;
};

/** agent 가 자기보고한 도구 호출 카운터 한 줄. 인자·결과는 담기지 않아요(ADR-0065). */
export type AuditToolCall = {
  /** `${target}___${tool}` 전체 이름이에요. */
  name: string;
  call_count: number;
  success_count: number;
  error_count: number;
};

/**
 * `AGENT_INVOKE` 감사 행 하나에 붙는 도구 호출 요약이에요.
 *
 * - `observed` + `tools` 있음 → 그 도구들을 불렀다고 보고했어요.
 * - `observed` + `tools` 빈 배열 → 도구를 안 썼다고 보고했어요.
 * - `unknown` → 관측 실패예요. 「도구 없음」이 아니에요.
 * - `not_recorded` → `USAGE` 항목 자체가 없는 옛 레코드예요.
 */
export type AuditToolUsage = {
  event_id: string;
  invocation_id: string;
  status: "observed" | "unknown" | "not_recorded";
  /** ADR-0065: 값은 `agent_report` — Agora 의 독립 관측이 아니에요. */
  source: string;
  reason: string;
  discarded_count: number;
  tools: AuditToolCall[];
};

export type AuditCoverage = {
  status: "ok" | "unknown";
  reason:
    | ""
    | "index_backfilling"
    | "index_missing"
    | "index_disappeared"
    | "backfill_not_run"
    | "backfill_in_progress"
    | "backfill_failed"
    | "range_before_backfill"
    | "range_partially_backfilled"
    | "coverage_access_denied"
    | "coverage_throttled"
    | "coverage_unavailable"
    | "query_access_denied"
    | "query_throttled";
  backfilled_from: string | null;
  backfill_status:
    | "unknown"
    | "not_run"
    | "in_progress"
    | "completed"
    | "failed";
};

export type AuditTimelinePage = {
  items: AuditEvent[];
  next_cursor: string | null;
  coverage: AuditCoverage;
  /**
   * `items` 중 `AGENT_INVOKE` 행에만 있는 사이드카예요. `event_id` 로 이어 붙여요.
   *
   * 옵셔널인 이유는 이 필드를 내려주지 않는 옛 백엔드와 구분해야 해서예요. 없으면
   * 「도구 없음」이 아니라 「미관측」이에요.
   */
  tool_usage?: AuditToolUsage[];
};

export type AuditTimelineFilters = {
  from?: string;
  to?: string;
  decision?: AuditDecision;
  agent_id?: string;
  principal_id?: string;
  limit?: number;
  cursor?: string;
};

export function listAuditTimeline(
  filters: AuditTimelineFilters,
): Promise<AuditTimelinePage> {
  const params = new URLSearchParams();
  if (filters.from) params.set("from", filters.from);
  if (filters.to) params.set("to", filters.to);
  if (filters.decision) params.set("decision", filters.decision);
  if (filters.agent_id) params.set("agent_id", filters.agent_id);
  if (filters.principal_id) params.set("principal_id", filters.principal_id);
  if (filters.limit) params.set("limit", String(filters.limit));
  if (filters.cursor) params.set("cursor", filters.cursor);
  const suffix = params.size ? `?${params.toString()}` : "";
  return request<AuditTimelinePage>(`/api/admin/access/audit${suffix}`);
}
