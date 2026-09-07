import type {
  AuditEvent,
  AuditTimelineFilters,
  AuditTimelinePage,
  AuditToolCall,
  AuditToolUsage,
} from "./api/audit.ts";
import type { CognitoUser } from "./api/users.ts";

export type {
  AuditEvent,
  AuditTimelineFilters,
  AuditTimelinePage,
  AuditToolCall,
  AuditToolUsage,
} from "./api/audit.ts";

export const AUDIT_EXPORT_MAX_ROWS = 5_000;
export const AUDIT_EXPORT_MAX_PAGES = 100;
/**
 * 도구 열의 출처 각주예요. ADR-0065 가 요구하는 계약이라 문구를 지워선 안 돼요 —
 * `tool_metrics` 는 생성 agent 의 자기보고(`source=agent_report`)고, Agora 가 Gateway 에서
 * 독립 관측한 값이 아니에요.
 */
export const AUDIT_TOOL_SOURCE_FOOTNOTE =
  "도구 목록은 agent 가 스스로 보고한 값이에요 (source=agent_report). Agora 가 직접 관측한 호출 기록이 아니에요.";
/** 도구 인자·결과는 담지 않는다는 사실을 화면에서도 밝혀요 (ADR-0065). */
export const AUDIT_TOOL_ARGUMENTS_NOTE =
  "도구 인자와 결과는 수집하지 않아요.";
const KST_OFFSET_MS = 9 * 60 * 60 * 1_000;

export function kstInputToUtc(value: string): string {
  const match = value.match(
    /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?$/,
  );
  if (!match) throw new Error("KST 일시 형식이 올바르지 않아요.");
  const [, year, month, day, hour, minute, second = "00"] = match;
  const instant = new Date(
    Date.UTC(
      Number(year),
      Number(month) - 1,
      Number(day),
      Number(hour),
      Number(minute),
      Number(second),
    ) - KST_OFFSET_MS,
  );
  if (Number.isNaN(instant.getTime())) {
    throw new Error("KST 일시 형식이 올바르지 않아요.");
  }
  return instant.toISOString();
}

export function utcToKstInput(value: Date | string): string {
  const instant = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(instant.getTime())) return "";
  return new Date(instant.getTime() + KST_OFFSET_MS)
    .toISOString()
    .slice(0, 19);
}

export function defaultAuditRange(now = new Date()): { from: string; to: string } {
  return {
    from: utcToKstInput(new Date(now.getTime() - 24 * 60 * 60 * 1_000)),
    to: utcToKstInput(now),
  };
}

export function formatKst(value: string): string {
  const instant = new Date(value);
  if (Number.isNaN(instant.getTime())) return value || "-";
  return new Intl.DateTimeFormat("ko-KR", {
    timeZone: "Asia/Seoul",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hourCycle: "h23",
  }).format(instant);
}

export function principalDisplay(
  principalId: string,
  users: ReadonlyMap<string, CognitoUser>,
): { primary: string; secondary: string } {
  const user = users.get(principalId);
  if (!user) {
    return {
      primary: principalId || "-",
      secondary: "이름 미확인",
    };
  }
  return {
    primary: user.name || user.email || principalId,
    secondary: user.team || "부서 미등록",
  };
}

/**
 * `workload_id` 로 agent 종류를 판정해요.
 *
 * ⚠️ 빈 값은 「관리형」이 아니라 「미기록」이에요. `record_agent_invoke_audit` 이
 * `workload_id=""` 로 쓰기 때문에(`access_router.py` `record_agent_invoke_audit`) 예전 판정은
 * **모든 호출 행을 근거 없이 「관리형」으로** 그렸어요. 값이 없으면 추측하지 않아요.
 */
export function agentKind(workloadId: string): "관리형" | "외부" | "미기록" {
  const normalized = (workloadId ?? "").trim();
  if (!normalized) return "미기록";
  return normalized.startsWith("agora-external-") ? "외부" : "관리형";
}

/**
 * 감사 이벤트 종류를 관리자용 사람 말 라벨로 옮겨요.
 *
 * 모르는 값이 오면 조용히 「호출」로 떨어지지 않아요 — 원문을 그대로 드러내요.
 * 판정 함수라 컴포넌트 밖(순수 함수)에 두고 계약 테스트로 묶어요.
 */
export function auditEventTypeDisplay(eventType: string): {
  label: string;
  kind: "invoke" | "decision" | "admin" | "security" | "unknown";
} {
  switch (eventType) {
    case "AGENT_INVOKE":
      return { label: "Agent 호출", kind: "invoke" };
    case "AUTHORIZATION_DECISION":
      return { label: "인가 판정", kind: "decision" };
    case "SECURITY_REJECTION":
      return { label: "보안 차단", kind: "security" };
    case "AGENT_POLICY_DEPLOYMENT":
      return { label: "정책 배포", kind: "admin" };
    case "AGENT_TOOL_BINDING_REQUESTED":
      return { label: "도구 신청", kind: "admin" };
    case "AGENT_TOOL_BINDING_APPROVED":
      return { label: "도구 승인", kind: "admin" };
    case "AGENT_TOOL_BINDING_REJECTED":
      return { label: "도구 거부", kind: "admin" };
    case "AGENT_TOOL_BINDING_REVOKED":
      return { label: "도구 회수", kind: "admin" };
    case "WORKLOAD_IDENTITY_REVOKED":
      return { label: "workload 신원 회수", kind: "admin" };
    case "FAIL_CLOSED_AUTHORIZATION_GATE_APPROVED":
      return { label: "fail-closed 게이트 승인", kind: "admin" };
    case "ASSET_CAPABILITY_ASSET_PURGE_DELETED":
      return { label: "자산 정리 (승인 삭제)", kind: "admin" };
    case "ASSET_CAPABILITY_REREGISTRATION_DELETED":
      return { label: "재등록 정리 (승인 삭제)", kind: "admin" };
    case "ASSET_CAPABILITY_ASSET_PURGE_CLEANUP_UNKNOWN":
      return { label: "자산 정리 결과 미관측", kind: "unknown" };
    case "ASSET_CAPABILITY_REREGISTRATION_CLEANUP_UNKNOWN":
      return { label: "재등록 정리 결과 미관측", kind: "unknown" };
    default:
      return {
        label: eventType
          ? `이벤트 종류 미확인 (${eventType})`
          : "이벤트 종류 미기록",
        kind: "unknown",
      };
  }
}

/** `decision=ALLOW` 와 별개인 Runtime 실행 결과 (ADR-0065). */
export function invocationOutcomeDisplay(
  item: Pick<AuditEvent, "event_type" | "invocation_outcome">,
): { label: string; tone: "success" | "failure" | "unknown" } | null {
  if (item.event_type !== "AGENT_INVOKE") return null;
  switch (item.invocation_outcome) {
    case "SUCCESS":
      return { label: "실행 성공", tone: "success" };
    case "FAILED":
      return { label: "실행 실패", tone: "failure" };
    default:
      // 빈 문자열은 필드가 없던 옛 레코드예요. 성공으로 간주하지 않아요.
      return { label: "실행 결과 미기록", tone: "unknown" };
  }
}

export type AuditToolCallDisplay = {
  /** `${target}___${tool}` 전체 이름 — 툴팁·CSV 용이에요. */
  fullName: string;
  /** Gateway Target 이름 (`___` 앞쪽). */
  targetName: string;
  /** 표에 그리는 짧은 도구 이름 (`___` 뒤쪽). */
  toolName: string;
  callCount: number;
  successCount: number;
  errorCount: number;
};

/** `${target}___${tool}` 를 쪼개요. 구분자가 없으면 통째로 도구 이름으로 둬요. */
export function splitGatewayToolName(name: string): {
  targetName: string;
  toolName: string;
} {
  const index = (name ?? "").indexOf("___");
  if (index < 0) return { targetName: "", toolName: name ?? "" };
  return {
    targetName: name.slice(0, index),
    toolName: name.slice(index + 3),
  };
}

export function toolCallDisplay(tool: AuditToolCall): AuditToolCallDisplay {
  const { targetName, toolName } = splitGatewayToolName(tool.name);
  return {
    fullName: tool.name,
    targetName,
    toolName,
    callCount: tool.call_count ?? 0,
    successCount: tool.success_count ?? 0,
    errorCount: tool.error_count ?? 0,
  };
}

/**
 * ⚠️ 이 표의 기대값 소유자는 **백엔드 writer** 예요 — 이 파일이 아니에요.
 *
 * 첫 판이 `invocation_failed` 를 빠뜨렸어요. 그건 «호출이 실패한 턴» 이 쓰는 이유
 * (`playground/router.py` `_unknown_invocation_usage("invocation_failed")`) 이고, 관리자가
 * 감사 로그에서 **가장 먼저 열어 볼 행**이라 하필 그 자리에서 날 문자열이 보였어요. 표를 눈으로
 * 채웠기 때문에 생긴 누락이라, `api/tests/test_audit_tool_usage.py` 가 api 소스에서 이유
 * 리터럴을 긁어 이 표와 대조해요 — 새 이유를 백엔드에 추가하면 그 테스트가 빨개져요.
 */
const TOOL_USAGE_REASON_MESSAGES: Record<string, string> = {
  invalid_or_excess_tool_names:
    "이름 형식·개수 제한을 넘은 도구 이름을 버렸어요.",
  invocation_failed:
    "이 호출이 실패해서 어떤 도구를 불렀는지 기록되지 않았어요.",
  tool_names_not_declared:
    "Registry 에 선언되지 않은 도구 이름을 버렸어요.",
  agent_usage_not_reported:
    "agent 가 사용량을 보고하지 않았어요 (구 scaffold 재배포가 필요해요).",
  agent_usage_invalid: "agent 가 보고한 사용량 형식이 올바르지 않았어요.",
  usage_recording_failed: "사용량 기록이 실패했어요.",
  tool_usage_throttled: "사용량 원장 조회가 제한돼 확인하지 못했어요.",
  tool_usage_access_denied:
    "사용량 원장을 읽을 권한이 없어 확인하지 못했어요.",
  tool_usage_unavailable: "사용량 원장을 읽지 못했어요.",
  tool_usage_incomplete: "사용량 원장 일부를 읽지 못했어요.",
  invocation_id_missing: "감사 행에 invocation_id 가 없어 짝을 찾지 못했어요.",
};

export function toolUsageReasonMessage(reason: string): string {
  if (!reason) return "이유가 기록되지 않았어요.";
  return TOOL_USAGE_REASON_MESSAGES[reason] ?? `확인되지 않은 이유 (${reason})`;
}

export type AuditToolUsageCell = {
  /**
   * 다섯 상태를 서로 접지 않아요.
   *
   * - `not_applicable` — 호출 행이 아니라 도구 개념이 없어요.
   * - `tools` — 보고된 도구 목록이 있어요.
   * - `no_tools` — 도구를 안 썼다고 보고했어요.
   * - `unknown` — 관측 실패예요. 「도구 없음」이 아니에요.
   * - `not_recorded` — 사용량 항목 자체가 없는 옛 레코드예요.
   */
  kind: "not_applicable" | "tools" | "no_tools" | "unknown" | "not_recorded";
  label: string;
  detail: string;
  tools: AuditToolCallDisplay[];
  source: string;
};

/**
 * 감사 행 하나에 그릴 도구 셀을 정해요.
 *
 * `serverReported=false` 는 응답에 `tool_usage` 키 자체가 없다는 뜻이에요(옛 백엔드).
 * 그건 「기록 없음」이 아니라 미관측이라 `unknown` 으로 그려요.
 */
export function auditToolUsageCell(
  item: Pick<AuditEvent, "event_type">,
  usage: AuditToolUsage | undefined,
  serverReported = true,
): AuditToolUsageCell {
  const empty = { tools: [] as AuditToolCallDisplay[], source: "" };
  if (item.event_type !== "AGENT_INVOKE") {
    return {
      kind: "not_applicable",
      label: "해당 없음",
      detail: "",
      ...empty,
    };
  }
  if (!usage) {
    if (!serverReported) {
      return {
        kind: "unknown",
        label: "미관측",
        detail: "이 응답에는 도구 요약이 없어요 (서버가 아직 내려주지 않아요).",
        ...empty,
      };
    }
    return {
      kind: "not_recorded",
      label: "기록 없음",
      detail: "이 호출에는 사용량 기록이 없어요.",
      ...empty,
    };
  }
  const tools = (usage.tools ?? []).map(toolCallDisplay);
  if (usage.status === "unknown") {
    const discarded =
      usage.discarded_count > 0
        ? ` 버린 이름 ${usage.discarded_count}개.`
        : "";
    return {
      kind: "unknown",
      label: "미관측",
      detail: `${toolUsageReasonMessage(usage.reason)}${discarded}`,
      tools,
      source: usage.source,
    };
  }
  if (usage.status === "not_recorded") {
    return {
      kind: "not_recorded",
      label: "기록 없음",
      detail: "이 호출에는 사용량 기록이 없어요.",
      ...empty,
    };
  }
  if (tools.length === 0) {
    return {
      kind: "no_tools",
      label: "도구 미사용",
      detail: "이 호출에서는 도구를 부르지 않았다고 보고했어요.",
      tools,
      source: usage.source,
    };
  }
  return {
    kind: "tools",
    label: "",
    detail: "",
    tools,
    source: usage.source,
  };
}

/** 여러 페이지의 `tool_usage` 를 `event_id` → 항목 맵으로 합쳐요. */
export function auditToolUsageIndex(
  pages: readonly Pick<AuditTimelinePage, "tool_usage">[],
): { byEventId: Map<string, AuditToolUsage>; serverReported: boolean } {
  const byEventId = new Map<string, AuditToolUsage>();
  let serverReported = pages.length === 0;
  for (const page of pages) {
    if (!Array.isArray(page.tool_usage)) continue;
    serverReported = true;
    for (const row of page.tool_usage) byEventId.set(row.event_id, row);
  }
  return { byEventId, serverReported };
}

/** 도구 셀을 한 줄 텍스트로 (CSV·툴팁 용). 전체 이름을 써요. */
export function toolUsageSummaryText(cell: AuditToolUsageCell): string {
  if (cell.kind === "tools") {
    return cell.tools
      .map((tool) => `${tool.fullName} ×${tool.callCount}`)
      .join(" | ");
  }
  return cell.detail ? `${cell.label} — ${cell.detail}` : cell.label;
}

export function agentDisplay(
  agentId: string,
  agents: ReadonlyMap<string, string>,
): { primary: string; secondary: string } {
  const name = agents.get(agentId)?.trim();
  if (!name) {
    return {
      primary: agentId || "-",
      secondary: "이름 미확인",
    };
  }
  return {
    primary: name,
    secondary: agentId,
  };
}

export function auditOperationDisplay(operationId: string): {
  label: string;
  kind: "agent" | "admin" | "other";
} {
  switch (operationId) {
    case "agent-invoke":
      return { label: "Agent 호출", kind: "agent" };
    case "policy-deploy":
      return { label: "정책 배포", kind: "admin" };
    default:
      return { label: operationId || "동작 미확인", kind: "other" };
  }
}

export function auditFindingLines(
  item: Pick<AuditEvent, "validation_findings">,
): string[] {
  if (!Array.isArray(item.validation_findings)) return [];
  return item.validation_findings.flatMap((finding) => {
    if (typeof finding !== "string") return [];
    const normalized = finding.trim();
    return normalized ? [normalized] : [];
  });
}

export function timelineListState(
  page: AuditTimelinePage,
): "unknown" | "data" | "continuation" | "empty" {
  if (page.coverage.status === "unknown") return "unknown";
  if (page.items.length > 0) return "data";
  if (page.next_cursor) return "continuation";
  return "empty";
}

export function coverageReasonMessage(
  reason: AuditTimelinePage["coverage"]["reason"],
): string {
  switch (reason) {
    case "index_backfilling":
      return "감사 인덱스를 백필하는 중이라 현재 기간의 이벤트 수를 확정할 수 없어요.";
    case "index_missing":
      return "감사 인덱스가 아직 배포되지 않아 현재 기간의 이벤트 수를 확인할 수 없어요.";
    case "index_disappeared":
      return "조회 중 감사 인덱스가 사라져 관측 범위를 확정할 수 없어요.";
    case "backfill_not_run":
      return "과거 감사 이벤트 백필이 아직 실행되지 않아 현재 기간의 이벤트 수를 확정할 수 없어요.";
    case "backfill_in_progress":
      return "과거 감사 이벤트를 다시 백필하는 중이라 현재 기간의 이벤트 수를 확정할 수 없어요.";
    case "backfill_failed":
      return "과거 감사 이벤트 백필이 실패해 현재 기간의 이벤트 수를 확정할 수 없어요.";
    case "range_before_backfill":
      return "선택한 구간은 감사 백필 관측 범위 밖이에요.";
    case "range_partially_backfilled":
      return "선택한 구간 일부가 감사 백필 관측 범위 밖이에요.";
    case "coverage_access_denied":
      return "감사 인덱스 상태를 확인할 권한이 없어 관측 범위를 확정할 수 없어요.";
    case "coverage_throttled":
      return "감사 인덱스 상태 확인이 제한되어 관측 범위를 확정할 수 없어요.";
    case "coverage_unavailable":
      return "감사 인덱스 상태를 관측할 수 없어 현재 기간의 이벤트 수를 확정할 수 없어요.";
    case "query_access_denied":
      return "감사 이벤트를 조회할 권한이 없어 현재 결과를 완전한 목록으로 확정할 수 없어요.";
    case "query_throttled":
      return "감사 이벤트 조회가 제한되어 현재 결과를 완전한 목록으로 확정할 수 없어요.";
    default:
      return "";
  }
}

export class AuditCoverageUnknownError extends Error {
  readonly reason: AuditTimelinePage["coverage"]["reason"];

  constructor(reason: AuditTimelinePage["coverage"]["reason"]) {
    super("감사 범위를 확인할 수 없어 CSV를 만들지 않았어요.");
    this.name = "AuditCoverageUnknownError";
    this.reason = reason;
  }
}

export async function collectAuditExport(
  filters: Omit<AuditTimelineFilters, "cursor" | "limit">,
  fetchPage: (filters: AuditTimelineFilters) => Promise<AuditTimelinePage>,
  options: {
    maxRows?: number;
    maxPages?: number;
    onProgress?: (rows: number, pages: number) => void;
  } = {},
): Promise<{
  items: AuditEvent[];
  pages: number;
  truncated: boolean;
  toolUsage: Map<string, AuditToolUsage>;
  toolUsageReported: boolean;
}> {
  const maxRows = options.maxRows ?? AUDIT_EXPORT_MAX_ROWS;
  const maxPages = options.maxPages ?? AUDIT_EXPORT_MAX_PAGES;
  const items: AuditEvent[] = [];
  const collectedPages: Pick<AuditTimelinePage, "tool_usage">[] = [];
  const seen = new Set<string>();
  let cursor: string | undefined;
  let pages = 0;

  const finish = (truncated: boolean) => {
    const { byEventId, serverReported } = auditToolUsageIndex(collectedPages);
    return {
      items,
      pages,
      truncated,
      toolUsage: byEventId,
      toolUsageReported: serverReported,
    };
  };

  while (items.length < maxRows && pages < maxPages) {
    const page = await fetchPage({ ...filters, cursor, limit: 200 });
    pages += 1;
    if (page.coverage.status === "unknown") {
      throw new AuditCoverageUnknownError(page.coverage.reason);
    }
    const remaining = maxRows - items.length;
    const pageWasCut = page.items.length > remaining;
    items.push(...page.items.slice(0, remaining));
    collectedPages.push({ tool_usage: page.tool_usage });
    options.onProgress?.(items.length, pages);
    if (!page.next_cursor) return finish(pageWasCut);
    if (seen.has(page.next_cursor)) return finish(true);
    seen.add(page.next_cursor);
    cursor = page.next_cursor;
  }

  return finish(Boolean(cursor));
}

function csvCell(value: unknown): string {
  let text = String(value ?? "");
  if (/^[=+\-@]/.test(text.trimStart())) text = `'${text}`;
  return `"${text.replaceAll('"', '""')}"`;
}

export function auditEventsCsv(
  items: AuditEvent[],
  users: ReadonlyMap<string, CognitoUser>,
  agents: ReadonlyMap<string, string>,
  toolUsage: ReadonlyMap<string, AuditToolUsage> = new Map(),
  toolUsageReported = true,
): string {
  const headers = [
    "일시(KST)",
    "principal_id",
    "사용자 이름",
    "부서",
    "Agent 이름",
    "Agent ID",
    "Agent 종류",
    "이벤트 종류",
    "이벤트 종류 ID",
    "동작",
    "동작 ID",
    "실행 결과",
    // 도구 이름과 카운터만이에요. 인자·결과는 담지 않아요(ADR-0065).
    "도구 관측 상태",
    "도구 호출 (agent 자기보고)",
    "도구 관측 사유",
    "asset_id",
    "결과",
    "사유",
    "validation findings",
    "invocation_id",
  ];
  const rows = items.map((item) => {
    const user = users.get(item.principal_id);
    const agent = agentDisplay(item.agent_id, agents);
    const operation = auditOperationDisplay(item.operation_id);
    const eventType = auditEventTypeDisplay(item.event_type);
    const outcome = invocationOutcomeDisplay(item);
    const cell = auditToolUsageCell(
      item,
      toolUsage.get(item.event_id),
      toolUsageReported,
    );
    return [
      formatKst(item.timestamp),
      item.principal_id,
      user?.name || "이름 미확인",
      user?.team || "",
      agent.secondary === "이름 미확인" ? "이름 미확인" : agent.primary,
      item.agent_id,
      agentKind(item.workload_id),
      eventType.label,
      item.event_type,
      operation.label,
      item.operation_id,
      outcome?.label ?? "해당 없음",
      cell.kind === "tools" ? "관측됨" : cell.label,
      cell.tools
        .map((tool) => `${tool.fullName} ×${tool.callCount}`)
        .join("\n"),
      cell.detail,
      item.asset_id,
      item.decision,
      item.reason,
      auditFindingLines(item).join("\n"),
      item.invocation_id,
    ];
  });
  return [headers, ...rows]
    .map((row) => row.map(csvCell).join(","))
    .join("\r\n");
}
