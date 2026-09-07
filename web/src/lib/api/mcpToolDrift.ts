import { JSON_HEADERS, request } from "./client";

/**
 * MCP 도구 목록 드리프트 (LC-03).
 *
 * 진실의 원천이 셋이에요 — MCP 서버(실제 도구) / Gateway Target(Cedar 가 보는 스키마) /
 * Agora 원장(민감도 태그·승인). 이 API 는 **MCP 서버와 Agora 원장** 둘을 대조한 결과예요.
 */
export type ToolDriftState =
  | "DISCOVERED"
  | "ACTIVE"
  | "CHANGED"
  | "MISSING"
  | "RETIRED";

/** 관측 결과. `unknown`은 절대 통과가 아니에요 — 확인하지 못했다는 뜻이에요. */
export type DriftCheckStatus = "ok" | "never_checked" | "unknown";

export type ToolSchemaDiff = {
  added: string[];
  removed: string[];
  /** [옛 이름, 새 이름]. 1:1 개명만 짝으로 와요. */
  renamed: [string, string][];
  /** [필드, 옛 타입, 새 타입] */
  type_changed: [string, string, string][];
  required_added: string[];
  required_removed: string[];
};

export type ToolShape = {
  description: string;
  fields: Record<string, string>;
  required: string[];
};

/** 민감도 태그의 출처. 추정값과 관리자 확정값은 신뢰도가 달라요. */
export type SensitivitySource =
  | "descriptor"
  | "name_guess"
  | "auto"
  | "admin"
  | "unknown";

/** 관리자가 고를 수 있는 태그. 미분류는 `null` 이에요. */
export type SensitivityTag = "READ" | "CREATE" | "UPDATE" | "DELETE";

export const SENSITIVITY_TAGS: readonly SensitivityTag[] = [
  "READ",
  "CREATE",
  "UPDATE",
  "DELETE",
];

export type ToolTargetProjection = {
  kind: "derived" | "connected" | "none";
  name: string | null;
  basis:
    | "sensitivity_tag"
    | "connection_mode"
    | "missing_sensitivity"
    | "unknown_sensitivity";
  /** Always false here: this screen derives an expectation and does not observe Gateway. */
  observed: false;
};

export type SensitivityChangeStatus =
  | "PENDING_APPROVAL"
  | "APPROVED_PENDING_PROPAGATION"
  | "APPLYING"
  | "APPLIED"
  | "FAILED";

export type SensitivityMovement = {
  status?: string;
  stage?: string;
  error?: string;
  reason?: string;
  ticket?: string;
  retryable?: boolean;
  consistency?: "unchanged" | "restored" | "diverged";
  compensation?: Record<string, unknown>;
  coordinates?: Record<string, unknown>;
  gateway_targets?: Record<string, unknown>[];
  unassigned_tools?: Record<string, unknown>[];
};

export type SensitivityChangeRequest = {
  request_id: string;
  asset_key: string;
  record_id: string;
  tool_name: string;
  before: string | null;
  after: string | null;
  before_source: SensitivitySource | null;
  state_before: string;
  state_after: string;
  direction: "upgrade" | "downgrade";
  status: SensitivityChangeStatus;
  reason: string;
  requested_by: string;
  requested_by_label: string;
  requested_at: string;
  approved_by: string;
  approved_by_label: string;
  approved_at: string;
  attempt_started_at: string;
  applied_at: string;
  ledger_version: number;
  groups_gained: string[];
  groups_lost: string[];
  impact: SensitivityImpact[];
  target_before: string | null;
  target_after: string | null;
  movement: SensitivityMovement;
  error: string;
  retryable: boolean;
  request_version: number;
};

export type ToolDriftEntry = {
  tool_name: string;
  state: ToolDriftState;
  /** 현재 유효한 민감도 태그. MISSING이면 비워 두고 직전 값만 별도로 보존해요. */
  sensitivity: string | null;
  /** 그 태그가 어디서 왔나. 미분류면 null 이에요. */
  sensitivity_source: SensitivitySource | null;
  /** 닫힐 때 보존된 직전 태그. 현재 태그나 live 차단 증거로 사용하지 않아요. */
  previous_sensitivity: string | null;
  baseline: ToolShape;
  observed: ToolShape;
  diff: ToolSchemaDiff | null;
  description_changed: boolean;
  reappeared: boolean;
  first_seen_at: string;
  last_seen_at: string;
  state_since: string;
  /** null이면 live Target에서 실제 차단됐는지 확인하지 못한 상태예요. */
  callable_now: boolean | null;
  /** callability가 unknown이면 null이며, 확인된 호출 불가는 빈 배열이에요. */
  callable_groups: string[] | null;
  /** 태그로 계산한 예상 Target. 실제 배포 상태 증거가 아니에요. */
  target: ToolTargetProjection;
  /** 승인/전파 대기 또는 실패한 최신 변경. 적용 완료 요청은 행에 싣지 않아요. */
  pending_change: SensitivityChangeRequest | null;
};

export type AssetToolDrift = {
  /** 이름 기반 원장 키. 재등록해도 안 바뀌어요(IH-22). */
  asset_key: string;
  record_id: string;
  asset_name: string;
  check_status: DriftCheckStatus;
  last_checked_at: string | null;
  check_error: string | null;
  counts: Record<ToolDriftState, number>;
  has_drift: boolean;
  target_mode: "deployed" | "connected";
  tools: ToolDriftEntry[];
};

/**
 * `listMcpToolDrift` 를 쓰는 화면들이 **공유하는** SWR 캐시 키예요 (IH-164).
 *
 * `/admin/tools`(`McpToolDriftPanel`)와 `/admin/tool-access`(`ToolAccessClient`)가
 * 서로 다른 키를 써서 화면을 옮길 때마다 같은 응답을 다시 받았어요. 두 곳의
 * fetcher(`listMcpToolDrift`) · SWR 옵션(전부 전역 기본값) · 반환형이 같아요.
 *
 * ⚠️ `GET /api/mcp/tool-drift` 는 이름만 읽기고 실제로는 원장에 쓰는 경로예요
 * (`DriftLedgerConflict` 경합). 그래서 키를 합쳐 요청 수를 줄이는 방향은 맞지만,
 * **이 키에 `refreshInterval` 폴링을 붙이지 마세요** — 주기 폴링은 그 경합을 늘려요.
 */
export const TOOL_DRIFT_SWR_KEY = "admin/mcp/tool-drift";

/** GET /api/mcp/tool-drift — 원장만 읽어요(MCP 를 새로 떠오지 않아요). */
export function listMcpToolDrift(): Promise<{ assets: AssetToolDrift[] }> {
  return request<{ assets: AssetToolDrift[] }>("/api/mcp/tool-drift");
}

/** POST /api/mcp/tool-drift/{record_id}/resync — "다시 읽기". */
export function resyncMcpToolDrift(recordId: string): Promise<AssetToolDrift> {
  return request<AssetToolDrift>(
    `/api/mcp/tool-drift/${encodeURIComponent(recordId)}/resync`,
    { method: "POST", headers: JSON_HEADERS },
  );
}

/**
 * 영향 범위 한 항목. `known: false` 면 `count` 가 null 이에요 —
 * **0 으로 내려오지 않아요.** 0 은 "영향 없음"으로 읽히는데 그건 관측한 사실이 아니에요.
 */
export type SensitivityImpact = {
  label: string;
  known: boolean;
  count: number | null;
  reason: string;
  names: string[];
  /** 승인 스냅샷 비교용 안정 ID. 표시에는 names를 사용해요. */
  agent_ids?: string[];
};

/** 태그를 바꾸기 전에 서버가 계산해 주는 계획서. */
export type SensitivityChangePlan = {
  tool_name: string;
  before: string | null;
  after: string | null;
  before_source: SensitivitySource | null;
  after_source: SensitivitySource;
  state_before: ToolDriftState;
  state_after: ToolDriftState;
  groups_before: string[];
  groups_after: string[];
  /** 이 변경으로 이 도구를 **부를 수 있게 되는** 등급. */
  groups_gained: string[];
  /** 이 변경으로 **못 부르게 되는** 등급. */
  groups_lost: string[];
  downgrade: boolean;
  /** 위험도를 낮추는 변경이면 true — 사유 없이 저장하면 서버가 막아요. */
  reason_required: boolean;
  no_op: boolean;
  impact: SensitivityImpact[];
};

export type SensitivityHistoryEvent = {
  event_id: string;
  asset_key: string;
  tool_name: string;
  at: string;
  actor: string;
  /** 표시용 이름(email). 비면 `actor`(안정 식별자)로 폴백해요. */
  actor_label?: string;
  before: string | null;
  after: string | null;
  before_source: SensitivitySource | null;
  after_source: SensitivitySource;
  state_before: string;
  state_after: string;
  downgrade: boolean;
  reason: string;
  groups_gained: string[];
  groups_lost: string[];
  request_id: string;
  stage:
    | "requested"
    | "impact_changed"
    | "approved"
    | "retrying"
    | "moved"
    | "failed";
  change_status: SensitivityChangeStatus;
  impact: SensitivityImpact[];
  movement: SensitivityMovement;
};

function toolPath(recordId: string, toolName: string): string {
  return (
    `/api/mcp/tool-drift/${encodeURIComponent(recordId)}` +
    `/tools/${encodeURIComponent(toolName)}`
  );
}

/** POST …/preview — 바꾸기 전 영향 계산. 원장을 쓰지 않아요. */
export function previewMcpToolSensitivity(
  recordId: string,
  toolName: string,
  sensitivity: SensitivityTag | null,
): Promise<SensitivityChangePlan> {
  return request<SensitivityChangePlan>(`${toolPath(recordId, toolName)}/preview`, {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify({ sensitivity }),
  });
}

/** PUT …/sensitivity — 관리자 확정. 하향에는 `reason` 이 필요해요. */
export function setMcpToolSensitivity(
  recordId: string,
  toolName: string,
  sensitivity: SensitivityTag | null,
  reason: string,
): Promise<SensitivityChangeResponse> {
  return request<SensitivityChangeResponse>(
    `${toolPath(recordId, toolName)}/sensitivity`,
    {
      method: "PUT",
      headers: JSON_HEADERS,
      body: JSON.stringify({ sensitivity, reason }),
    },
  );
}

export type SensitivityChangeResponse = {
  asset: AssetToolDrift;
  plan: SensitivityChangePlan;
  change: SensitivityChangeRequest;
  history_recorded: true;
};

export function approveMcpToolSensitivity(
  recordId: string,
  toolName: string,
  requestId: string,
): Promise<SensitivityChangeResponse> {
  return request<SensitivityChangeResponse>(
    `${toolPath(recordId, toolName)}/sensitivity-changes/`
      + `${encodeURIComponent(requestId)}/approve`,
    { method: "POST", headers: JSON_HEADERS },
  );
}

export function retryMcpToolSensitivity(
  recordId: string,
  toolName: string,
  requestId: string,
): Promise<SensitivityChangeResponse> {
  return request<SensitivityChangeResponse>(
    `${toolPath(recordId, toolName)}/sensitivity-changes/`
      + `${encodeURIComponent(requestId)}/retry`,
    { method: "POST", headers: JSON_HEADERS },
  );
}

/** GET /api/mcp/tool-drift/{record_id}/history — 변경 이력(최신순). */
export function listMcpToolSensitivityHistory(
  recordId: string,
): Promise<{ events: SensitivityHistoryEvent[] }> {
  return request<{ events: SensitivityHistoryEvent[] }>(
    `/api/mcp/tool-drift/${encodeURIComponent(recordId)}/history`,
  );
}
