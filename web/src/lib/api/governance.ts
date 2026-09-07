import { API_BASE, ApiError, SESSION_PRINCIPAL, jsonHeadersWithPrincipal, request } from "./client";
import type { ApprovalBlock } from "./types";

// ---------------------------------------------------------------------------
// 거버넌스 콘솔 (Admin 전용) — 도구 레지스트리·등급 매트릭스·설정·스캔.
// RBAC는 Cognito access token에서 검증한 admin 그룹으로만 판정해요.
// ---------------------------------------------------------------------------

/** GET /api/governance/me — 현재 principal·roles·권한 플래그(§2.1). */
export type GovBackendMode = {
  scanner: string;          // static | noop | fargate
  registry_adapter: string;
  source_store: string;
};

export type GovMe = {
  principal: string;
  roles: string[];
  can: { tools_write: boolean; tier_write: boolean; review: boolean };
  mode?: GovBackendMode;
};

/** 거버넌스 도구(스캐너) 정의 — 각 도구 = ScannerPort 어댑터 구성(§2.3 GOVTOOL). */
export type GovTool = {
  tool_id: string;
  name: string;
  area: string;             // 12영역 키 (secret·sast·mcp_poison·...)
  repo: string;
  license: string;          // MIT/Apache-2.0/LGPL-*/BSD-3-Clause 만 허용
  exec_kind: string;        // offline | cli | service
  invoke_cmd: string;
  timeout: number;
  finding_risk_map: Record<string, string>;
  target_asset_types: string[];  // mcp | agent | skill
  status: string;           // active | staged
  current_version: string;
  compute: string;            // lambda | fargate
  compute_rationale: string;  // LLM/규칙 근거
  image_uri: string;          // SP-3에서 채움
  deployed: boolean;          // AWS 실배포 여부(GET /tools 병합).
};

/** 등급×도구 매트릭스 셀(§2.3 TIER). */
export type TierCell = {
  tier: string;             // minimal | standard | strong
  tool_id: string;
  enforcement: string;      // required | warn | off
  threshold: string;
  asset_type_scope: string;
};

export const GOV_TIER_VALUES = ["minimal", "standard", "strong"] as const;
export type GovTier = (typeof GOV_TIER_VALUES)[number];

/** GET /api/governance/me — 콘솔 진입 시 권한 판정. */
export function getGovMe(): Promise<GovMe> {
  return request<GovMe>("/api/governance/me");
}

/** GET /api/governance/tools — 등록된 도구 전체(reviewer+). */
export function listGovTools(): Promise<{ tools: GovTool[] }> {
  return request<{ tools: GovTool[] }>("/api/governance/tools");
}

/** GET /api/governance/tiers/{tier}/tools — 등급별 도구 구성(reviewer+). */
export function getGovTier(tier: string): Promise<{ tier: string; cells: TierCell[] }> {
  return request(`/api/governance/tiers/${encodeURIComponent(tier)}/tools`);
}

/** PUT /api/governance/tiers/{tier}/tools — 등급 구성 저장(admin). cells 통째 교체. */
export function putGovTier(
  tier: string, cells: Partial<TierCell>[],
): Promise<{ tier: string; cells: TierCell[] }> {
  return request(`/api/governance/tiers/${encodeURIComponent(tier)}/tools`, {
    method: "PUT",
    headers: jsonHeadersWithPrincipal(SESSION_PRINCIPAL),
    body: JSON.stringify({ cells }),
  });
}

/** simulate 응답 — 풀 정밀화(전이·자산타입별·샘플). */
export type SimulateResult = {
  affected: number;
  required_tools: string[];
  transitions: { pass_to_fail: number; fail_to_pass: number };
  by_asset_type: Record<string, { affected: number; pass_to_fail: number; fail_to_pass: number }>;
  sample: { record_id: string; name: string; old_verdict: string; new_verdict: string }[];
};

/** POST /api/governance/tiers/simulate — 저장 전 기존 자산 재판정 드라이런(admin). */
export function simulateGovTier(
  tier: string, cells: Partial<TierCell>[],
): Promise<SimulateResult> {
  return request("/api/governance/tiers/simulate", {
    method: "POST",
    headers: jsonHeadersWithPrincipal(SESSION_PRINCIPAL),
    body: JSON.stringify({ tier, cells }),
  });
}

/** 콘솔 설정(§2.3 GOVSETTINGS). auto_scan 기본 OFF. */
export type ConsoleSettings = {
  auto_scan: boolean;
  auto_detect_rate: string;   // ISO8601 duration (P1W·P2W·P1M …)
  updated_by: string;
  updated_at: string;
  asset_tier_map: Record<string, string>;   // skill|mcp|agent → minimal|standard|strong
  judge_model_map: Record<string, string>;  // minimal|standard|strong → haiku-4-5|sonnet-4-6|sonnet-5
};

/** GET /api/governance/settings — 콘솔 설정(reviewer+). */
export function getGovSettings(): Promise<ConsoleSettings> {
  return request<ConsoleSettings>("/api/governance/settings");
}

/** PUT /api/governance/settings — 콘솔 설정 저장(admin). */
export function putGovSettings(
  fields: { auto_scan?: boolean; auto_detect_rate?: string; asset_tier_map?: Record<string, string>; judge_model_map?: Record<string, string> },
): Promise<ConsoleSettings> {
  return request<ConsoleSettings>("/api/governance/settings", {
    method: "PUT",
    headers: jsonHeadersWithPrincipal(SESSION_PRINCIPAL),
    body: JSON.stringify(fields),
  });
}

/** 카테고리 목록과 자산이 사용 중인 미등록 값. */
export type GovCategories = {
  items: string[];
  in_use_unlisted: string[];
};

/** GET /api/governance/categories — 관리자 카테고리 현황. */
export function getGovCategories(): Promise<GovCategories> {
  return request<GovCategories>("/api/governance/categories");
}

/** PUT /api/governance/categories — 카테고리 목록 통째 교체. */
export function putGovCategories(
  items: string[],
): Promise<{ items: string[] }> {
  return request<{ items: string[] }>("/api/governance/categories", {
    method: "PUT",
    headers: jsonHeadersWithPrincipal(SESSION_PRINCIPAL),
    body: JSON.stringify({ items }),
  });
}

// ── 승인 큐 · 게이트 · 판정 (§3-M1·M2) ──────────────────────────────

/** 게이트 진행상태 요약 (verdict: none|pending|auto-approve|auto-reject). */
export type GateProgress = { passed: number; total: number; verdict: string };

/** 검출 위협 요약 항목 (code별 집계). */
export type ThreatCount = { code: string; count: number };

/** 승인 큐 행 1건. */
export type QueueItem = {
  record_id: string;
  name: string;
  descriptor_type: string;
  owner_user: string;
  updated_at: string;        // 등록/갱신일자 (정렬·표시)
  target_tier: string;       // minimal | standard | strong
  progress: GateProgress;
  risk: string | null;       // 미스캔이면 null
  scanned: boolean;
  scan_status: string;       // none | running | done | failed
  scan_applicability: GovAssetMeta["scan_applicability"];
  threats: ThreatCount[];
  status: string;            // DRAFT | PENDING_APPROVAL | APPROVED | REJECTED | DEPRECATED (registry lifecycle)
  /** 자동승인이 진행되지 않은 사유(R3). null = 막힌 기록 없음(승인됨이 아니에요). */
  approval_block?: ApprovalBlock | null;
};

/** 게이트 파이프라인 한 단계 (도구 1개). */
export type GateStage = {
  tool_id: string;
  tool_name: string;
  area: string;
  enforcement: string;       // required | warn
  state: string;             // pending | running | pass | fail | not_run | not_applicable | unknown
  findings_count: number;
};

/** 자산 표시용 메타 (상세화면 AssetMetaCard §3-M2). */
export type GovAssetMeta = {
  name: string;
  descriptor_type: string;   // MCP | Agent | Agent Skills | App | Model | Custom | ""
  owner_user: string;
  owner_team: string;
  tags: string[];
  endpoint: string;          // 소스 비관리형(mcp/agent/app)만. 없으면 ""
  version: string;
  updated_at: string;
  status: string;
  scan_applicability: {
    state: "applicable" | "not_applicable" | "unknown";
    reason: string;
  };
};

export type OverlapCandidate = {
  record_id: string;
  name: string;
  asset_type: string;
  version: string;
  score: number;
  band: "high" | "medium" | "low";
  reasons: string[];
};

export type OverlapReview = {
  state: "not_reviewed" | "reviewing" | "reviewed" | "failed";
  count: number;
  band: "high" | "medium" | "low" | null;
  candidates: OverlapCandidate[];
  compared?: number;
  reviewed_at?: string;
  error?: string;
};

/** GET /queue/{id}/gates 응답. */
export type GatesResponse = {
  record_id: string;
  tier: string;
  /** 위협리포트에 표시할 자산별 라벨. 게이트 계산의 `tier`와 별개예요. */
  threat_report_tier: GovTier;
  scanned: boolean;
  scan_status: string;       // none | running | done | failed
  risk: string | null;
  stages: GateStage[];
  findings: Record<string, unknown>[];
  /** 진행 중인 부분 재스캔의 area. "" = 전체 스캔 또는 미진행.
   *  이 area 행만 running으로 표시하고 나머지는 stages의 base 상태를 그대로 써요. */
  rescan_area?: string;
  summary: GateProgress;
  asset: GovAssetMeta;       // 상세화면 메타(소유·태그·endpoint)
  overlap: OverlapReview;
  /** 자동승인이 진행되지 않은 사유(R3). null = 막힌 기록 없음. */
  approval_block?: ApprovalBlock | null;
};

/** 스캔 진행상태 (queued|running|done|failed|none). */
export type ScanStatus = {
  status: string;
  risk: string | null;
  findings: Record<string, unknown>[];
  trigger?: string;
};

/** GET /api/governance/queue — 승인 큐 목록(reviewer+). */
export function listGovQueue(): Promise<{ items: QueueItem[] }> {
  return request<{ items: QueueItem[] }>("/api/governance/queue", {
    headers: { "x-agora-principal": SESSION_PRINCIPAL },
  });
}

/** GET /api/governance/queue/{id}/gates — 게이트 파이프라인(reviewer+). */
export function getGovGates(recordId: string): Promise<GatesResponse> {
  return request<GatesResponse>(
    `/api/governance/queue/${encodeURIComponent(recordId)}/gates`,
    { headers: { "x-agora-principal": SESSION_PRINCIPAL } },
  );
}

/** POST /api/governance/queue/{id}/overlap-review — 중복 후보를 동기로 다시 검토해요. */
export function runOverlapReview(recordId: string): Promise<OverlapReview> {
  return request<OverlapReview>(
    `/api/governance/queue/${encodeURIComponent(recordId)}/overlap-review`,
    {
      method: "POST",
      headers: { "x-agora-principal": SESSION_PRINCIPAL },
    },
  );
}

/** 게이트 스캔 로그 modal 응답. */
export type GateScanLogs = {
  tool_id: string;
  gate_state: string;        // pending | running | pass | fail | not_run
  findings: Record<string, unknown>[];
  log_status: string;        // ok | empty | not_run | unavailable
  lines: string[];
  scan_id: string;
  scan_ts: string;
};

export function getGateScanLogs(recordId: string, toolId: string): Promise<GateScanLogs> {
  return request<GateScanLogs>(
    `/api/governance/queue/${encodeURIComponent(recordId)}/gates/${encodeURIComponent(toolId)}/logs`,
    { headers: { "x-agora-principal": SESSION_PRINCIPAL } },
  );
}

/** 배포 agent 런타임 로그 응답 (Playground 로그 패널). */
export type AgentRuntimeLogs = {
  log_status: string;        // ok | empty | unavailable | not_deployed
  lines: string[];
  /**
   * `lines` 와 **같은 길이·같은 순서** 의 이벤트 시각(ms). IH-187 로 계약에 실었어요.
   *
   * 왜 필요한가: 서버 커서가 도착 여유(15초)만큼 뒤에 머물러 최근 구간이 매 폴링마다 다시
   * 와요. 그걸 줄 문자열로만 걸르면 «같은 문자열의 새 이벤트» 도 함께 버려져요 — 같은
   * 도구를 두 턴에서 부르면 `Tool #1: x` 가 글자까지 같거든요. 시각이 붙으면 갈려요.
   *
   * 옛 서버는 이 필드를 안 줘요 — 소비자는 없을 때를 견뎌야 해요(`?? []`).
   */
  timestamps?: (number | null)[];
  /**
   * `lines` 와 같은 길이·순서의 CloudWatch `eventId`. **중복 판정의 1순위 키** 예요.
   *
   * 시각+본문으로는 «같은 밀리초에 찍힌 동일 문자열» 두 건을 못 갈라요(codex 리뷰
   * 2026-09-07 2라운드). `eventId` 는 이벤트당 하나뿐이라 그 경계가 사라져요.
   */
  event_ids?: (string | null)[];
  next_since_ms: number;    // 다음 폴링에 넘기면 새로 생긴 줄만 받아요(증분 tail)
};

/** GET /api/playground/agents/{record_id}/logs — 배포 agent CloudWatch 로그. */
export function getAgentRuntimeLogs(
  recordId: string,
  sinceMs?: number,
): Promise<AgentRuntimeLogs> {
  const qs = sinceMs ? `?since_ms=${sinceMs}` : "";
  return request<AgentRuntimeLogs>(
    `/api/playground/agents/${encodeURIComponent(recordId)}/logs${qs}`,
    { headers: { "x-agora-principal": SESSION_PRINCIPAL } },
  );
}

/**
 * PATCH /api/governance/queue/{id}/tier — 위협리포트 라벨 전용(admin).
 *
 * 저장값은 report.md 라벨에 쓰이고, gates 응답은 편집 UI에 원값만 전달해요. 게이트
 * 상세는 `tier_for_record`, 큐 목록·필터는 `asset_tier_map`, 자동승인은
 * `apply_verdict_to_registry`의 타입 등급을 계속 사용하므로 이 호출은 게이트 재계산·
 * 큐 필터·자동승인 판정을 바꾸지 않아요.
 */
export function patchGovTargetTier(
  recordId: string, tier: GovTier,
): Promise<{ record_id: string; target_tier: GovTier; summary: GateProgress }> {
  return request(`/api/governance/queue/${encodeURIComponent(recordId)}/tier`, {
    method: "PATCH",
    headers: jsonHeadersWithPrincipal(SESSION_PRINCIPAL),
    body: JSON.stringify({ tier }),
  });
}

export type DecisionResult = {
  record_id: string; decision: string; override: boolean; reason: string;
};

/** POST /api/governance/queue/{id}/approve — 승인(reviewer+, 미통과 시 사유 필수). */
export function approveGovAsset(recordId: string, reason = ""): Promise<DecisionResult> {
  return request(`/api/governance/queue/${encodeURIComponent(recordId)}/approve`, {
    method: "POST",
    headers: jsonHeadersWithPrincipal(SESSION_PRINCIPAL),
    body: JSON.stringify({ reason }),
  });
}

/** POST /api/governance/queue/{id}/reject — 반려(reviewer+, 사유 필수). */
export function rejectGovAsset(recordId: string, reason: string): Promise<DecisionResult> {
  return request(`/api/governance/queue/${encodeURIComponent(recordId)}/reject`, {
    method: "POST",
    headers: jsonHeadersWithPrincipal(SESSION_PRINCIPAL),
    body: JSON.stringify({ reason }),
  });
}

/** POST /api/governance/queue/{id}/scan — 수동 스캔 실행(reviewer+). */
export function runGovScan(
  recordId: string, trigger: "manual-queue" | "manual-detail" = "manual-queue",
): Promise<ScanStatus & { ts?: string }> {
  return request(`/api/governance/queue/${encodeURIComponent(recordId)}/scan`, {
    method: "POST",
    headers: jsonHeadersWithPrincipal(SESSION_PRINCIPAL),
    body: JSON.stringify({ trigger }),
  });
}

/**
 * POST /api/governance/queue/{id}/gates/{toolId}/rescan — 단일 게이트(도구) 부분 재스캔(reviewer+).
 * base done scan에서 이 도구 area만 갈아끼워요(다른 area 결과 보존). running 레코드를 돌려줘요.
 * 부적합 상황(base 없음/running/scan-level 에러, not_applicable/off 도구)은 서버가 422.
 */
export function rescanGate(
  recordId: string, toolId: string,
): Promise<ScanStatus & { rescan_area?: string }> {
  return request(
    `/api/governance/queue/${encodeURIComponent(recordId)}/gates/${encodeURIComponent(toolId)}/rescan`,
    {
      method: "POST",
      headers: jsonHeadersWithPrincipal(SESSION_PRINCIPAL),
      body: JSON.stringify({ trigger: "manual-detail" }),
    },
  );
}

/** GET /api/governance/queue/{id}/scan/status — 스캔 진행상태 폴링(reviewer+). */
export function getGovScanStatus(recordId: string): Promise<ScanStatus> {
  return request<ScanStatus>(
    `/api/governance/queue/${encodeURIComponent(recordId)}/scan/status`,
    { headers: { "x-agora-principal": SESSION_PRINCIPAL } },
  );
}

/**
 * GET /api/governance/queue/{id}/report.md — 위협리포트.md를 텍스트로 받아요(§2.1).
 * Bedrock Sonnet 4.6이 에셋별 맞춤 수정가이드를 생성하므로 몇 초 걸릴 수 있어요.
 * 파일 다운로드용이라 JSON이 아닌 raw 텍스트를 반환해요.
 */
export async function getGovThreatReport(recordId: string): Promise<string> {
  const res = await fetch(
    `${API_BASE}/api/governance/queue/${encodeURIComponent(recordId)}/report.md`,
    { headers: { "x-agora-principal": SESSION_PRINCIPAL } },
  );
  if (!res.ok) throw new ApiError(`위협리포트 생성 실패 (${res.status})`, res.status);
  return res.text();
}

// ── 대시보드 · 감사 (§3-M5·M6) ──────────────────────────────────────

/** GET /api/governance/dashboard 응답. */
export type Dashboard = {
  approvals: {
    approve: number; reject: number; pending: number;
    override: number; automation_rate: number;
  };
  gate_pass_rate: Record<string, number>;   // area → 통과율(%)
  risk_distribution: Record<string, number>;
  tier_distribution: Record<string, number>;
  status_distribution: Record<string, number>;  // RecordStatus → count (DRAFT/PENDING_APPROVAL/APPROVED/REJECTED/DEPRECATED)
  sla: { unscanned: number; total: number; decided: number };
  unscanned_assets: { record_id: string; name: string }[];  // 미스캔(대기) 자산 목록 (2열 리스트용)
  top_blockers: { area: string; tool_name: string; count: number }[];  // 도구별 차단 기여
  assets: DashboardAsset[];  // 자산별 현황 (분포 위젯이 카테고리별로 그룹핑)
};

/** 대시보드 자산별 현황 한 건 — 분포 위젯 아래 자산 리스트에 씀. */
export type DashboardAsset = {
  record_id: string;
  name: string;
  risk: string | null;       // none|low|medium|high|null(미스캔)
  status: string;            // DRAFT|PENDING_APPROVAL|APPROVED|REJECTED|DEPRECATED
  tier: string;              // minimal|standard|strong
  gates: string[];           // fail한 area 목록
  pass_areas: string[];      // pass한 area 목록
};

/** 감사 이력 한 건 (§3-M6). */
export type DecisionEntry = {
  record_id: string;
  name: string;              // 자산명 (registry 조인, 없으면 record_id 폴백)
  ts: string;
  principal: string;
  decision: string;          // APPROVE | REJECT
  reason: string;
  override: boolean;
};

/** GET /api/governance/dashboard — 관측 집계(reviewer+). */
export function getGovDashboard(): Promise<Dashboard> {
  return request<Dashboard>("/api/governance/dashboard", {
    headers: { "x-agora-principal": SESSION_PRINCIPAL },
  });
}

/** 측정 축의 상태. `not_implemented`면 화면이 "측정 미구현" 배지 + 회색 `—`를 그려요.
 *  서버가 알려주는 이유: 프론트에 하드코딩하면 telemetry가 붙을 때 두 곳을 고쳐야 하고,
 *  한쪽만 고치면 실데이터가 있는데 배지가 남아요. */
export type MeasurementState = "not_implemented" | "available";

/** 인벤토리 자산 한 건 — 소유·상태·중복 축. 측정 축(호출·토큰·품질)은 아직 없어요. */
export type InventoryAsset = {
  record_id: string;
  name: string;
  asset_type: string;        // MCP | Agent | Agent Skills | ...
  owner_team: string;
  owner_user: string;        // Cognito sub. owner_email 배선 전이라 UUID일 수 있어요.
  model: string;             // Agent만. 그 외/미저장은 ""
  version: string;
  status: string;            // DRAFT|PENDING_APPROVAL|APPROVED|REJECTED|DEPRECATED
  risk: string | null;       // none|low|medium|high|null(미스캔)
  tier: string;              // minimal|standard|strong
  overlap_state: "not_reviewed" | "reviewing" | "reviewed" | "failed";
  overlap_band: string | null;      // high|medium|low|none|null
  overlap_top_score: number | null;
  authorization_enforcement:
    | "enforcing"
    | "log_only"
    | "unmanaged"
    | "unknown"
    | "not_applicable";
  authorization_enforcement_source:
    | "runtime_configuration"
    | "registry_descriptor";
  /**
   * Gateway target 실체 상태 (CA-32 ②). `provisioning` 은 **호출 불가한 자산**이에요 —
   * 레코드는 있는데 Gateway 에 target 이 없어요(등록 실패의 잔해일 수 있어요).
   * 자동으로 지우지 않고 관리자가 식별할 수 있게 드러내기만 해요.
   */
  gateway_target_state:
    | "ready"
    | "provisioning"
    | "unmanaged"
    | "unknown"
    | "not_applicable";
};

/** 중복 후보 카드 한 줄 — 대상 자산 ↔ 가장 점수 높은 후보. */
export type InventoryOverlapCandidate = {
  record_id: string;
  name: string;
  candidate_record_id: string;
  candidate_name: string;
  score: number;
  band: string;
  reasons: string[];
};

export type GatewayTargetQuota = {
  status: "ok" | "unknown";
  current: number | null;
  quota: number | null;
  usage_ratio: number | null;
  threshold_ratio: number;
  threshold_reached: boolean | null;
  reason: string | null;
  sources: {
    current: "ListGatewayTargets";
    quota: "ServiceQuotas.GetServiceQuota";
  };
};

/** GET /api/governance/inventory 응답. */
export type Inventory = {
  kpi: {
    total: number;
    by_type: Record<string, number>;
    unscanned: number;
    overlap_high: number;
    /** Gateway target 이 없는(=호출 불가) 자산 수 (CA-32). */
    gateway_target_missing: number;
  };
  assets: InventoryAsset[];
  overlap_candidates: InventoryOverlapCandidate[];
  authorization: {
    m2_gateway_mode: "enforce" | "log_only" | "unknown";
    source: "runtime_configuration" | "unobserved";
    counts: Record<string, number>;
  };
  measurement: {
    invocations: MeasurementState;
    tokens: MeasurementState;
    cost: MeasurementState;
    quality: MeasurementState;
    last_used: MeasurementState;
    owner_status: MeasurementState;
  };
  gateway_target_quota: GatewayTargetQuota;
};

/** GET /api/governance/inventory — 자산 인벤토리 집계(admin). */
export function getGovInventory(): Promise<Inventory> {
  return request<Inventory>("/api/governance/inventory", {
    headers: { "x-agora-principal": SESSION_PRINCIPAL },
  });
}

/** GET /api/governance/decisions — 판정 감사 이력(reviewer+). principal·record 필터. */
export function listGovDecisions(
  filters: { record_id?: string; principal?: string } = {},
): Promise<{ items: DecisionEntry[] }> {
  const params = new URLSearchParams();
  if (filters.record_id) params.set("record_id", filters.record_id);
  if (filters.principal) params.set("principal", filters.principal);
  const qs = params.toString();
  return request<{ items: DecisionEntry[] }>(
    `/api/governance/decisions${qs ? "?" + qs : ""}`,
    { headers: { "x-agora-principal": SESSION_PRINCIPAL } },
  );
}
