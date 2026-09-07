/**
 * Playground 실행 구성 — 선언(원장)과 실체(runtime 관측)를 화면용으로 정리해요.
 *
 * 이 파일의 규율은 하나예요: **관측하지 못한 걸 사실처럼 만들지 않아요**(ADR-0037 §4).
 * 그래서 모든 표시 함수는 값 대신 "무엇을 모르는지"를 돌려줄 수 있어요.
 *
 * 실체는 두 소유자에게서 관측해요. runtime `agora/selfcheck` 는 모델 ID·도구·
 * conversation manager·retrieval namespace·실행 상한을, AWS GetMemory는 전략을 보고해요.
 */

export type DeclaredModel = { alias: string; bedrock_model_id: string };

export type DeclaredTool = {
  kind: "mcp" | "builtin" | string;
  asset_id: string;
  target_name: string;
  operation: string;
  label: string;
  operations_declared: boolean;
  expected_tool_names: string[];
};

export type DeclaredMemory = {
  mode: string;
  strategies: string[];
  namespaces: Record<string, string[]>;
  retention_days: number | null;
  memory_id_present: boolean;
};

export type ObservedStatus = "ok" | "not_probed" | "unsupported" | "unavailable";

export type ObservedRunConfig = {
  status: ObservedStatus;
  reason: string;
  model: { status: "ok" | "unknown"; model_id: string | null } | null;
  tools: string[] | null;
  conversation_manager: { name: string; parameters: Record<string, unknown> } | null;
  memory: Record<string, unknown> | null;
  memory_resource: {
    status: "ok" | "unknown" | "not_applicable";
    reason: string;
    strategies: string[] | null;
  } | null;
  limits: Record<string, unknown> | null;
};

export type ReconciliationRow = {
  kind: string;
  label: string;
  expected_tool_names: string[];
  observed: boolean | null;
};

/**
 * ④ 원장이 말하는 도구별 승인 상태 (ADR-0104).
 *
 * 「런타임 등록」과 **다른 축**이에요. 미승인 도구는 Gateway `tools/list` 에 나타나지만
 * 호출은 거부돼요(ADR-0099 §4.6 «listed but denied») — 그래서 등록 뱃지만 보면 「쓸 수 있다」로
 * 잘못 읽혀요. 값은 서버가 원장에서 읽어요(웹이 owner 전용 엔드포인트를 붙이지 않아요).
 */
export type ToolApproval =
  | "approved"
  | "pending_approval"
  | "not_requested"
  | "not_applicable"
  | "unobserved";

export type ToolAuthorizationRow = {
  kind: string;
  label: string;
  expected_tool_names: string[];
  approval: ToolApproval;
};

export type AgentRunConfig = {
  record_id: string;
  declared: {
    model: DeclaredModel | null;
    tools: DeclaredTool[];
    memory: DeclaredMemory;
    conversation_manager: {
      declared: Record<string, unknown> | null;
      actual: { name: string; parameters: Record<string, unknown> } | null;
      verdict: string;
    } | null;
  };
  observed: ObservedRunConfig;
  model_reconciliation: ModelReconciliation;
  memory_reconciliation: MemoryReconciliation;
  reconciliation: ReconciliationRow[];
  tool_authorization: ToolAuthorizationRow[];
  unexpected_tools: UnexpectedTools;
  logging_outlet: {
    status: "unknown";
    reason: string;
    source: "unobserved";
    as_of: string | null;
  };
};

export type ReconciliationVerdict = "coherent" | "diverged" | "unknown";

export type ModelReconciliation = {
  expected_model_id: string | null;
  observed_model_id: string | null;
  verdict: ReconciliationVerdict;
};

type MemoryConfiguration = {
  mode: string | null;
  strategies: string[];
};

export type MemoryReconciliation = {
  expected: MemoryConfiguration;
  observed: MemoryConfiguration | null;
  verdict: ReconciliationVerdict;
};

/**
 * 선언에 없는데 runtime 에 등록된 도구.
 *
 * 빈 목록 하나로 뭉치면 "없음" 과 "판정 불가" 가 합쳐져요 — 그래서 상태를 분리해요.
 * `undecidable` 은 operation 선언이 없는 legacy binding 때문에 기대 이름을 도출할 수 없어
 * 대조 자체가 불가능한 경우예요(추측해서 정상 도구를 예상 밖으로 몰면 안 되니까요).
 */
export type UnexpectedTools = {
  status: "judged" | "undecidable" | "unobserved";
  names: string[];
};

export type ObservationRequestAction = "reuse_success" | "cooldown" | "probe";

export type RequestGeneration = {
  recordId: string;
  generation: number;
};

export function requestGenerationMatches(
  current: RequestGeneration,
  request: RequestGeneration,
): boolean {
  return current.recordId === request.recordId
    && current.generation === request.generation;
}

/** non-OK config를 저장하지 않고 자동 재시도 가능 시각만 계산해요. */
export function observationRetryAfter(
  status: ObservedStatus,
  attemptedAt: number,
  cooldownMs: number,
): number | null {
  return status === "ok" ? null : attemptedAt + cooldownMs;
}

/** 성공 config 재사용과 실패 후 자동 재시도 억제를 서로 다른 상태로 판정해요. */
export function observationRequestAction({
  now,
  successfulObservedAt,
  retryAfter,
  successCacheMs,
  force = false,
}: {
  now: number;
  successfulObservedAt: number | null;
  retryAfter: number | null;
  successCacheMs: number;
  force?: boolean;
}): ObservationRequestAction {
  if (force) return "probe";
  if (retryAfter !== null && now < retryAfter) return "cooldown";
  if (
    successfulObservedAt !== null
    && now - successfulObservedAt < successCacheMs
  ) {
    return "reuse_success";
  }
  return "probe";
}

/**
 * 관측하지 못한 축에 화면이 쓰는 문구. IH-92 가 세운 「미관측을 통과로 그리지 않는다」계열이에요.
 *
 * 「실제 배선 정보 없음」이었어요. 「배선」을 뺐고, **「없음」으로 바꾸지 않았어요** — 이 문구가
 * 뜻하는 건 부재가 아니라 미관측이에요(ADR-0037 §4 · ADR-0111 결정 2). 아래 `observedStatusLabel`
 * 의 「실체 관측됨」·「실체 미관측」과 같은 말을 써요.
 */
export const UNOBSERVED_LABEL = "실체 미관측";

export function observedStatusLabel(observed: ObservedRunConfig): string {
  switch (observed.status) {
    case "ok":
      return "실체 관측됨";
    case "not_probed":
      return "실체 미관측 — 아직 확인하지 않았어요";
    case "unsupported":
      return "실체 관측 불가 — 이 배포본은 selfcheck 를 지원하지 않아요";
    default:
      return "실체 관측 실패";
  }
}

export function isObserved(observed: ObservedRunConfig): boolean {
  return observed.status === "ok";
}

/** 모델은 선언만 있어요 — selfcheck 가 모델 ID 를 보고하지 않거든요. */
export function modelDeclarationLabel(model: DeclaredModel | null): string | null {
  return model ? `${model.alias} · ${model.bedrock_model_id}` : null;
}

export function reconciliationStatusLabel(verdict: ReconciliationVerdict): string {
  if (verdict === "coherent") return "일치";
  if (verdict === "diverged") return "불일치";
  return UNOBSERVED_LABEL;
}

export type MemoryDeclarationView = {
  enabled: boolean;
  modeLabel: string;
  /** 장기 기억 전략(SEMANTIC·SUMMARIZATION). 비어 있으면 선언된 전략이 없어요. */
  strategies: { name: string; namespaces: string[] }[];
  retentionLabel: string | null;
};

export function memoryDeclarationView(memory: DeclaredMemory): MemoryDeclarationView {
  const enabled = memory.mode === "MANAGED";
  return {
    enabled,
    modeLabel:
      memory.mode === "MANAGED"
        ? "사용 (AgentCore Memory · MANAGED)"
        : memory.mode === "DISABLED"
          ? "사용 안 함"
          : UNOBSERVED_LABEL,
    strategies: memory.strategies.map((name) => ({
      name,
      namespaces: memory.namespaces[name] ?? [],
    })),
    retentionLabel:
      enabled && memory.retention_days !== null
        ? `보존 ${memory.retention_days}일`
        : null,
  };
}

export function memoryObservationView(
  observed: ObservedRunConfig,
): {
  mode: string | null;
  strategies: string[] | null;
  strategyReason: string;
  runtimeRetrievalNamespaces: string[] | null;
} | null {
  if (!isObserved(observed)) return null;
  const mode = (
    observed.memory !== null && typeof observed.memory.mode === "string"
      ? observed.memory.mode
      : null
  );
  const retrievalNamespaces = observed.memory?.runtime_retrieval_namespaces;
  const runtimeRetrievalNamespaces = (
    observed.memory?.configuration_status === "ok"
    && Array.isArray(retrievalNamespaces)
      ? retrievalNamespaces.filter(
        (value): value is string => typeof value === "string",
      )
      : null
  );
  const resource = observed.memory_resource;
  const strategies = (
    resource?.status === "ok" && Array.isArray(resource.strategies)
      ? resource.strategies.filter(
        (value): value is string => typeof value === "string",
      )
      : null
  );
  if (mode === null && runtimeRetrievalNamespaces === null && resource === null) {
    return null;
  }
  return {
    mode,
    strategies,
    strategyReason: resource?.reason ?? "",
    runtimeRetrievalNamespaces,
  };
}

/**
 * 세션 기억이 실제로 붙었는지 — selfcheck 의 session manager 관측이에요.
 * 관측이 없으면 `null` 이고, 화면은 그걸 "없음" 이 아니라 "미관측" 으로 표시해요.
 */
export function sessionMemoryAttached(observed: ObservedRunConfig): boolean | null {
  if (!isObserved(observed) || observed.memory === null) return null;
  const attached = observed.memory.session_manager_attached;
  return typeof attached === "boolean" ? attached : null;
}

export type ToolRowStatus = "registered" | "missing" | "undetermined" | "unobserved";

export type ToolRow = {
  kind: string;
  label: string;
  expectedToolNames: string[];
  status: ToolRowStatus;
  approval: ToolApproval;
};

const TOOL_APPROVAL_LABEL: Record<ToolApproval, string> = {
  // 「관리자 승인 대기」 — interceptor 의 `tool_pending_approval` 문구와 같은 표현이에요.
  // 두 곳이 다른 말을 하면 사용자가 화면과 오류 메시지를 이어 읽지 못해요.
  pending_approval: "관리자 승인 대기",
  // 「신청 안 됨」이라고 쓰면 안 돼요 — 이 상태는 **셋**을 덮어요(신청 없음 · 반려 · 회수).
  // 서버가 `desired_state != ALLOWED` 행을 빼서 회수된 도구도 여기로 와요. 공통점은
  // 「관리자 승인 큐에 없다」예요.
  not_requested: "승인 큐에 없음",
  approved: "도구 권한 승인됨",
  not_applicable: "",
  // 등록 축의 미관측 칩과 **다른 문구**를 써요. 둘이 같으면 나란히 붙은 칩 두 개가 같은
  // 말을 하는 것처럼 보여서 어느 축이 미관측인지 알 수 없어요.
  unobserved: "권한 원장 미관측",
};

export function toolRowApprovalLabel(approval: ToolApproval): string {
  return TOOL_APPROVAL_LABEL[approval];
}

/**
 * 이 상태를 화면에 **뱃지로 띄워야** 하는지.
 *
 * `approved` 는 안 띄워요 — 모든 줄에 초록 뱃지를 더하면 정작 막힌 줄이 안 보여요.
 * `not_applicable`(내장 도구·legacy binding)도 안 띄워요: ④ binding 이 있을 수 없는 줄에
 * 「신청 안 됨」을 띄우면 없는 문제를 만들어요. `unobserved` 는 **띄워요** — 원장을 못 읽은
 * 걸 「승인됨」으로 접지 않아요(ADR-0037 §3).
 */
export function toolRowApprovalIsNoteworthy(approval: ToolApproval): boolean {
  return (
    approval === "pending_approval"
    || approval === "not_requested"
    || approval === "unobserved"
  );
}

const TOOL_STATUS_LABEL: Record<ToolRowStatus, string> = {
  registered: "런타임 등록 확인",
  missing: "런타임에 등록 안 됨",
  undetermined: "대조 불가 — operation 선언이 없어요",
  unobserved: UNOBSERVED_LABEL,
};

export function toolRowStatusLabel(status: ToolRowStatus): string {
  return TOOL_STATUS_LABEL[status];
}

/**
 * 선언된 도구마다 실체 관측 결과와 ④ 승인 상태를 붙여요.
 *
 * 기대값은 선언 원장에서(백엔드가 `gateway_tool_names` 로 도출), 대조 대상은 runtime
 * 자기보고예요 — 양쪽 소유자가 달라야 대조가 의미 있어요(ADR-0037 §4).
 *
 * 승인 축은 **또 다른 소유자**(identity 원장)에서 와요. 서버가 붙여 준 `tool_authorization`
 * 을 label 로 이어 붙이고, 그 줄이 없으면 `unobserved` 예요 — 「승인됨」으로 접지 않아요.
 */
export function toolRows(config: AgentRunConfig): ToolRow[] {
  const byLabel = new Map(config.reconciliation.map((row) => [row.label, row]));
  const approvalByLabel = new Map(
    (config.tool_authorization ?? []).map((row) => [row.label, row.approval]),
  );
  return config.declared.tools.map((tool) => {
    const row = byLabel.get(tool.label);
    let status: ToolRowStatus = "unobserved";
    if (row) {
      status =
        row.observed === null
          ? "undetermined"
          : row.observed
            ? "registered"
            : "missing";
    }
    return {
      kind: tool.kind,
      label: tool.label,
      expectedToolNames: tool.expected_tool_names,
      status,
      approval: approvalByLabel.get(tool.label) ?? "unobserved",
    };
  });
}

/**
 * 실행 상한(관측). 없으면 빈 목록 — 화면은 미관측으로 표시해요.
 *
 * selfcheck 의 `limits` 에는 상한값과 함께 `*_description` 산문 항목이 섞여 있어요
 * (예: `max_iterations_description`). 그건 상한이 아니라 설명이라 여기서 빼고
 * `notes` 로 따로 넘겨요 — 값 줄에 문단이 들어가면 읽을 수가 없거든요.
 */
export function limitRows(observed: ObservedRunConfig): { key: string; value: string }[] {
  return limitView(observed).rows;
}

export function limitView(observed: ObservedRunConfig): {
  rows: { key: string; value: string }[];
  notes: string[];
} {
  if (!isObserved(observed) || observed.limits === null) return { rows: [], notes: [] };
  const rows: { key: string; value: string }[] = [];
  const notes: string[] = [];
  for (const [key, value] of Object.entries(observed.limits)) {
    if (value === null || value === undefined) continue;
    if (key.endsWith("_description")) notes.push(String(value));
    else rows.push({ key, value: String(value) });
  }
  return { rows, notes };
}
