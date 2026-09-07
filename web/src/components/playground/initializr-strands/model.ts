import type {
  AgentToolRequest,
  AssetDetail,
  MemoryStrategy,
  MemoryStrategyConfigs,
  ScaffoldSpecInput,
} from "@/lib/api";
import type { ToolOption } from "@/lib/initializr";

/** sliding window 하한. 백엔드(`SLIDING_WINDOW_MIN`)와 같은 값이어야 해요.
 *
 * IH-70: 도구 왕복 하나가 메시지 4개(user + assistant(toolUse) + user(toolResult) +
 * assistant)를 쓰고, AgentCore Memory 복원은 toolResult 만 담긴 메시지를 통째로 버려서
 * 역할 교대가 깨진 이력이 만들어져요. 작은 window 는 그 구간을 그대로 모델에 보내
 * 대화를 영구히 못 쓰게 해요(실측 2026-08-22: window 5 + MCP 도구에서 5번째 턴 실패).
 */
export const SLIDING_WINDOW_MIN = 10;

export const JUSTIFICATION_MIN_LENGTH = 10;
export const MCP_EMPTY_SELECTION_MESSAGE =
  "operation을 고르지 않으면 이 MCP는 배포에서 제외돼요.";
export const MEMORY_RETENTION_MIN = 3;
export const MEMORY_RETENTION_MAX = 365;

export type ContextStrategy = "sliding_window" | "none";
export type MemoryMode = "DISABLED" | "MANAGED";

export const MEMORY_STRATEGIES: MemoryStrategy[] = [
  "SEMANTIC",
  "SUMMARIZATION",
];

const MEMORY_STRATEGY_NAMES: Record<MemoryStrategy, string> = {
  SEMANTIC: "SemanticMemory",
  SUMMARIZATION: "SummarizationMemory",
};

type MemorySettings = {
  mode: MemoryMode;
  strategies: Set<MemoryStrategy>;
  retentionDays: number;
};

export function validateMemorySettings(input: MemorySettings): string[] {
  if (input.mode === "DISABLED") return [];

  const errors: string[] = [];
  if (
    !Number.isInteger(input.retentionDays)
    || input.retentionDays < MEMORY_RETENTION_MIN
    || input.retentionDays > MEMORY_RETENTION_MAX
  ) {
    errors.push(
      `보존 기간은 ${MEMORY_RETENTION_MIN}일에서 ${MEMORY_RETENTION_MAX}일 사이여야 해요.`,
    );
  }
  if (input.strategies.size === 0) {
    errors.push("장기 전략을 하나 이상 선택해 주세요.");
  }
  return errors;
}

function selectedStrategyConfigs(input: {
  memoryStrategies: Set<MemoryStrategy>;
}): MemoryStrategyConfigs {
  const configs: MemoryStrategyConfigs = {};
  for (const strategy of MEMORY_STRATEGIES) {
    if (!input.memoryStrategies.has(strategy)) continue;
    configs[strategy] = { name: MEMORY_STRATEGY_NAMES[strategy] };
  }
  return configs;
}

export type CatalogOperation = {
  id: string;
  description: string;
  sensitivity: "READ" | "CREATE" | "UPDATE" | "DELETE" | "";
};

export function operationDeploymentApprovalLabel(
  sensitivity: CatalogOperation["sensitivity"],
): string {
  return sensitivity === "READ" ? "배포 자동 승인" : "배포 후 승인 필요";
}

/**
 * 로컬 다운로드 축 라벨. READ 가 아니면 ZIP 에 담기지 않아요.
 *
 * 위 라벨은 **배포** 축만 말해요 — "배포 후 승인 필요" 를 읽은 사람은 다운로드도 될 거라고
 * 생각해요. 실제로는 백엔드가 그 operation 을 크리덴셜에서 빼요
 * (`dev_identity_service._DOWNLOAD_SENSITIVITY_CEILING`). 고르는 시점에 알려주려고 나눴어요.
 *
 * 서버 값이 필요 없어요 — 민감도는 이미 카탈로그 응답에 실려 와요(`parseMcpOperations`).
 * 빈 문자열은 "표시할 게 없다" 는 뜻이에요(READ, 또는 민감도 미상 — 미상은 발급이 막으니
 * 여기서 다운로드 가능/불가를 단정하지 않아요).
 */
export function operationLocalDownloadLabel(
  sensitivity: CatalogOperation["sensitivity"],
): string {
  if (sensitivity === "READ" || sensitivity === "") return "";
  return "로컬 다운로드에는 안 담겨요";
}

export type SelectedCatalogTool = ToolOption & {
  operations: CatalogOperation[];
  selectedOperations: Set<string>;
};

export function visibleOperations(
  tool: SelectedCatalogTool,
): CatalogOperation[] {
  return tool.operations.filter((operation) => (
    tool.selectedOperations.has(operation.id)
  ));
}

export function shouldIncludeDevIdentity(
  tools: SelectedCatalogTool[],
): boolean {
  return tools.some((tool) => (
    tool.kind === "mcp" && tool.selectedOperations.size > 0
  ));
}

export type OperationJustifications = Record<string, string>;

function object(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

function normalizeSensitivity(value: unknown): CatalogOperation["sensitivity"] {
  const sensitivity = String(value ?? "").toUpperCase();
  if (
    sensitivity === "READ"
    || sensitivity === "CREATE"
    || sensitivity === "UPDATE"
    || sensitivity === "DELETE"
  ) {
    return sensitivity;
  }
  return "";
}

export function parseMcpOperations(asset: AssetDetail): CatalogOperation[] {
  const mcp = object(asset.descriptors.mcp);
  const toolsNode = object(mcp?.tools);
  const inlineContent = toolsNode?.inlineContent;
  if (typeof inlineContent !== "string" || !inlineContent) return [];

  try {
    const document = object(JSON.parse(inlineContent));
    const tools = Array.isArray(document?.tools) ? document.tools : [];
    return tools.flatMap((raw) => {
      const tool = object(raw);
      if (!tool || typeof tool.name !== "string" || !tool.name) return [];
      return [{
        id: tool.name,
        description: typeof tool.description === "string" ? tool.description : "",
        sensitivity: normalizeSensitivity(
          tool.sensitivity ?? tool["x-agora-sensitivity"],
        ),
      }];
    });
  } catch {
    return [];
  }
}

export function operationKey(assetId: string, operationId: string): string {
  return `${assetId}:${operationId}`;
}

export function defaultSelectedOperationIds(
  operations: CatalogOperation[],
): string[] {
  return operations
    .filter((operation) => operation.sensitivity === "READ")
    .map((operation) => operation.id);
}

export function missingJustifications(
  tools: SelectedCatalogTool[],
  justifications: OperationJustifications,
): { assetId: string; operationId: string }[] {
  return tools.flatMap((tool) => tool.operations.flatMap((operation) => {
    if (
      operation.sensitivity === "READ"
      || !tool.selectedOperations.has(operation.id)
    ) {
      return [];
    }
    const value = justifications[operationKey(tool.id, operation.id)]?.trim() ?? "";
    return value.length >= JUSTIFICATION_MIN_LENGTH
      ? []
      : [{ assetId: tool.id, operationId: operation.id }];
  }));
}

export function buildScaffoldSpec(input: {
  name: string;
  model: string;
  description: string;
  systemPrompt: string;
  tools: SelectedCatalogTool[];
  contextStrategy: ContextStrategy;
  windowSize: number;
  summaryRatio: number;
  preserveRecentMessages: number;
  maxTokens: number;
  maxIterations: number;
  memoryMode: MemoryMode;
  memoryStrategies: Set<MemoryStrategy>;
  memoryRetentionDays: number;
  builtinTools?: Set<"browser" | "code_interpreter">;
}): ScaffoldSpecInput {
  const truncation: NonNullable<ScaffoldSpecInput["truncation"]> =
    input.contextStrategy === "sliding_window"
      ? { strategy: "sliding_window", window_size: input.windowSize }
      : { strategy: "none" };

  return {
    name: input.name.trim() || "agent",
    model: input.model,
    description: input.description,
    system_prompt: input.systemPrompt,
    memory: input.memoryMode === "MANAGED"
      ? {
          mode: "MANAGED",
          strategies: MEMORY_STRATEGIES.filter((strategy) => (
            input.memoryStrategies.has(strategy)
          )),
          strategy_configs: selectedStrategyConfigs(input),
          retention_days: input.memoryRetentionDays,
        }
      : { mode: "DISABLED" },
    builtin_tools: [],
    // IH-101 temporary disablement (2026-08-22). Re-enable only when the
    // strands-agents-tools extras permit bedrock-agentcore>=1.22.0:
    // builtin_tools: (["browser", "code_interpreter"] as const).filter((tool) => (
    //   input.builtinTools?.has(tool)
    // )),
    tools: input.tools.filter((tool) => (
      tool.kind !== "mcp" || tool.selectedOperations.size > 0
    )).map((tool) => ({
      asset_id: tool.id,
      name: tool.name,
      kind: tool.kind,
      description: tool.description,
      endpoint: tool.endpoint ?? null,
      source_prefix: tool.sourcePrefix ?? null,
      version: tool.version ?? null,
      operations: tool.kind === "mcp"
        ? [...tool.selectedOperations]
        : [],
    })),
    truncation,
    limits: {
      max_tokens: input.maxTokens,
      max_iterations: input.maxIterations,
    },
  };
}

export function buildAgentToolRequests(
  tools: SelectedCatalogTool[],
  justifications: OperationJustifications,
): AgentToolRequest[] {
  return tools.flatMap((tool) => tool.operations.flatMap((operation) => {
    if (
      operation.sensitivity === "READ"
      || !tool.selectedOperations.has(operation.id)
    ) {
      return [];
    }
    return [{
      asset_id: tool.id,
      asset_version: tool.version ?? "",
      operation_id: operation.id,
      request_justification:
        justifications[operationKey(tool.id, operation.id)]?.trim() ?? "",
    }];
  }));
}
