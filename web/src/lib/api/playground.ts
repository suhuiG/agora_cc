import { API_BASE, ApiError, JSON_HEADERS, request } from "./client";
import type { AgentRunConfig } from "@/lib/playground/runConfig";
import {
  excludedOperations,
  scaffoldDownloadPath,
  scaffoldResponseError,
} from "./scaffold-download";

// ---------------------------------------------------------------------------
// Agent Initializr (playground)
// ---------------------------------------------------------------------------

export type PlaygroundTool = {
  name: string;
  kind: "skill" | "mcp" | "agent";
  description?: string;
  endpoint?: string | null;
  // skill 본문(SKILL.md)을 서버가 S3에서 찾는 좌표. 백엔드가 이걸로 read_file 해요.
  source_prefix?: string | null;
  version?: string | null;
};

export type ScaffoldTool = PlaygroundTool & {
  // Runtime enforcement가 선택된 카탈로그 자산에 delegation을 결속하는 ID.
  asset_id: string;
  // 빈 배열은 전체 operation이라는 legacy 계약이므로 신규 화면은 항상 명시적으로 채워요.
  operations: string[];
};

export type ScaffoldFileNode = {
  name: string;
  path: string;
  kind: "file" | "dir";
  children?: ScaffoldFileNode[];
  content?: string;
};

export type MemoryStrategy =
  | "SEMANTIC"
  | "SUMMARIZATION";

export type MemoryStrategyConfigs = {
  SEMANTIC?: {
    name: string;
  };
  SUMMARIZATION?: {
    name: string;
  };
};

export type ScaffoldMemory =
  | { mode: "DISABLED" }
  | {
      mode: "MANAGED";
      strategies: MemoryStrategy[];
      strategy_configs: MemoryStrategyConfigs;
      retention_days: number;
    };

export type ScaffoldSpecInput = {
  name: string;
  model: string;
  description: string;
  system_prompt: string;
  tools: ScaffoldTool[];
  memory: ScaffoldMemory;
  builtin_tools?: ("browser" | "code_interpreter")[];
  truncation?: {
    strategy: "sliding_window" | "none";
    window_size?: number;
  };
  limits?: {
    max_tokens?: number;
    max_iterations?: number;
  };
};

/** POST /api/playground/prompt — 설명+도구로 system prompt 초안을 생성해요(Bedrock+폴백). */
export function generatePrompt(
  input: { name: string; description: string; tools: PlaygroundTool[] },
  signal?: AbortSignal,
): Promise<{ system_prompt: string }> {
  return request("/api/playground/prompt", {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify(input),
    signal,
  });
}

/** POST /api/playground/scaffold — spec으로 생성될 파일트리(미리보기)를 받아요. */
export function getScaffold(spec: ScaffoldSpecInput): Promise<{ files: ScaffoldFileNode[] }> {
  return request("/api/playground/scaffold", {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify(spec),
  });
}

export type ScaffoldDownload = {
  blob: Blob;
  /** 골랐지만 로컬 ZIP 에 담기지 않은 operation id. 비어 있으면 전부 담겼어요. */
  excludedOperations: string[];
};

/**
 * POST /api/playground/scaffold?format=zip — 스캐폴드 zip + 제외 목록을 받아요.
 *
 * 제외 목록은 **같은 응답의 헤더**로 와요(`excludedOperations`). Blob 만 돌려주면 화면이
 * 줄어든 도구 집합을 조용히 저장하게 돼요.
 */
export async function downloadScaffold(
  spec: ScaffoldSpecInput,
  includeDevIdentity = false,
): Promise<ScaffoldDownload> {
  const res = await fetch(API_BASE + scaffoldDownloadPath(includeDevIdentity), {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify(spec),
  });
  if (!res.ok) {
    const failure = await scaffoldResponseError(res);
    throw new ApiError(failure.message, res.status, failure.detail);
  }
  // 헤더를 blob() 보다 먼저 읽어요 — 본문 스트림을 소비한 뒤에는 순서 의존이 생겨요.
  const excluded = excludedOperations(res);
  return { blob: await res.blob(), excludedOperations: excluded };
}

/** 배포 완료 agent (Playground 셀렉터용). */
export type DeployedAgent = { record_id: string; name: string; version: string };
export type DeployedAgentPage = {
  items: DeployedAgent[];
  next_cursor: string | null;
  incomplete_reason: string;
};

/** GET /api/playground/agents — 서버 검색·커서 페이지의 배포 agent 목록. */
export function listDeployedAgents(options: {
  name?: string;
  pageSize?: number;
  cursor?: string | null;
} = {}): Promise<DeployedAgentPage> {
  const params = new URLSearchParams();
  if (options.name?.trim()) params.set("name", options.name.trim());
  params.set("page_size", String(options.pageSize ?? 20));
  if (options.cursor) params.set("cursor", options.cursor);
  return request<DeployedAgentPage>(`/api/playground/agents?${params.toString()}`);
}

/** `usage.metrics.tool_metrics` 의 한 도구 칸. 필드는 전부 optional 이에요 — 서버가
 *  `_non_negative_number` 를 통과한 값만 담아서, 값이 이상하면 키가 아예 빠져요. */
export type InvocationToolMetric = {
  call_count?: number;
  success_count?: number;
  error_count?: number;
  total_time?: number;
};

/**
 * invoke 응답의 `usage` — **agent 자기보고**예요(`source=agent_report`).
 *
 * 서버가 이미 내려주고 있어요(`playground/router.py` 의 invoke 응답 조립부). 형태는
 * `invoke_service._extract_agent_usage` / `_unknown_usage` 가 만들어요. `status` 가
 * `observed` 가 아니면 `metrics` 는 `null` 이고 `reason` 에 사유 코드가 담겨요.
 *
 * ⚠️ 이 값을 「Agora 가 관측한 도구 호출」로 읽지 마세요 — 판정·표시 규약은
 * `@/lib/playground/selfReportedTools` 에 모아 뒀어요.
 */
export type InvocationUsage = {
  status: "observed" | "unknown" | string;
  source: string | null;
  reason: string | null;
  remediation: string | null;
  metrics: {
    tool_metrics?: Record<string, InvocationToolMetric>;
    [key: string]: unknown;
  } | null;
  tool_metrics_status?: string;
  tool_metrics_reason?: string;
  tool_metrics_discarded_count?: number;
};

export type InvokeAgentResult = {
  result: string;
  invocation_id?: string;
  /** agent 자기보고 사용량. 옛 배포본은 안 실어 보내요. */
  usage?: InvocationUsage;
  usage_recording?: { status: string; reason: string | null };
};

/** POST /api/playground/invoke — 선택한 agent(record_id)에 A2A 메시지를 보내요. */
export function invokeAgent(
  record_id: string,
  prompt: string,
  session_id: string,
): Promise<InvokeAgentResult> {
  return request("/api/playground/invoke", {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify({ record_id, prompt, session_id }),
  });
}

/**
 * GET /api/playground/agents/{id}/run-config — 실행 구성(선언) + 실체 관측.
 *
 * `probe=true` 는 배포된 runtime 에 `agora/selfcheck` 를 한 번 호출해요(세션 microVM 하나를
 * 써요). Playground는 선택 직후 비동기로 호출하고 성공 관측을 짧게 캐시해요.
 */
export function getAgentRunConfig(
  record_id: string,
  probe = false,
): Promise<AgentRunConfig> {
  const qs = probe ? "?probe=true" : "";
  return request<AgentRunConfig>(
    `/api/playground/agents/${encodeURIComponent(record_id)}/run-config${qs}`,
  );
}
