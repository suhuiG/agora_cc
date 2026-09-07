/**
 * agent 자기보고 도구 호출 — Playground 의 **두 번째** 도구 출처예요.
 *
 * `POST /api/playground/invoke` 응답이 `usage` 를 실어 보내요
 * (`playground/router.py` 의 invoke 응답 조립부, `record_agent_invoke_usage` 호출 바로 뒤).
 * 그 안의 `metrics.tool_metrics` 가 `도구 이름 → {call_count, success_count, error_count,
 * total_time}` 예요. 감사 화면(`web/src/lib/auditCalls.ts`)은 이미 이 출처를 쓰고 있었지만
 * Playground 는 읽지 않고 있었어요.
 *
 * ⚠️ **이건 Agora 가 관측한 값이 아니에요.** 생성 agent 가 스스로 보고해요
 * (`source=agent_report`). 그래서 두 규율을 지켜요.
 *
 * ① **런타임 로그로 관측한 `Tool #N:` 줄과 한 숫자로 합치지 않아요.** 합치면 어느 쪽이
 *    근거인지 사라져요. 화면은 두 값을 나란히 그려요.
 * ② **「자기보고 없음」과 「도구 안 씀」을 갈라요.** `tool_metrics` 키가 아예 없는 것과
 *    `{}` 로 보고한 것은 다른 사실이에요 — `invoke_service._sanitize_agent_usage` 가
 *    `isinstance(raw_tools, dict)` 일 때만 `metrics["tool_metrics"]` 를 채워요.
 *
 * 사유 문구는 감사 화면과 **같은 표**를 재사용해요(`toolUsageReasonMessage`). 그 표의
 * 기대값 소유자는 백엔드 writer 고 `api/tests/test_audit_tool_usage.py` 가 대조해요 —
 * 여기서 따로 베끼면 두 화면이 조용히 갈라져요.
 */

import { splitGatewayToolName, toolUsageReasonMessage } from "../auditCalls.ts";
import type { InvocationUsage } from "../api/playground.ts";

/** 도구 칸의 출처 각주. 감사 화면의 `AUDIT_TOOL_SOURCE_FOOTNOTE` 와 같은 규약이에요. */
export const SELF_REPORTED_TOOLS_FOOTNOTE =
  "이 목록은 agent 가 스스로 보고한 값이에요 (source=agent_report). Agora 가 런타임 로그에서 독립 관측한 호출 기록이 아니에요.";

/** 짧은 출처 라벨 — 트리 노드 이름과 요약 타일이 공유해요. */
export const SELF_REPORTED_TOOLS_SOURCE_LABEL = "agent 자기보고";

/** 로그로 관측한 쪽의 출처 라벨. 두 출처를 화면에서 갈라 부르려고 이름을 붙여 뒀어요. */
export const LOG_OBSERVED_TOOLS_SOURCE_LABEL = "런타임 로그 관측";

/** 호출이 실패한 턴은 오류 응답에 `usage` 가 실리지 않아요(본문이 `detail` 뿐이에요). */
export const SELF_REPORTED_TOOLS_FAILED_TURN_DETAIL =
  "호출이 실패한 턴은 오류 응답에 사용량이 실리지 않아요. 서버는 기록해 두니 admin 감사 조회에서는 볼 수 있어요.";

export type SelfReportedToolCall = {
  /** `${target}___${tool}` 전체 이름. */
  fullName: string;
  /** Gateway Target 이름 (`___` 앞쪽). 구분자가 없으면 빈 문자열. */
  targetName: string;
  /** 화면에 그리는 짧은 도구 이름 (`___` 뒤쪽). */
  toolName: string;
  callCount: number;
  successCount: number;
  errorCount: number;
};

export type SelfReportedTools = {
  /**
   * 세 상태를 서로 접지 않아요.
   *
   * - `not_reported` — 자기보고 자체가 없어요. **「도구 안 씀」이 아니에요.**
   * - `no_tools` — 도구를 부르지 않았다고 agent 가 보고했어요.
   * - `tools` — 보고된 도구 목록이 있어요.
   */
  kind: "not_reported" | "no_tools" | "tools";
  /** 짧은 라벨. `tools` 면 빈 문자열이에요(목록이 그 자리를 대신해요). */
  label: string;
  /** 왜 이 상태인지. */
  detail: string;
  tools: readonly SelfReportedToolCall[];
  /** 보고된 호출 횟수 합. `tools` 가 아니면 0. */
  callCount: number;
  /** 서버가 이름 규칙·Registry 선언 대조로 버린 이름 수. */
  discardedCount: number;
  /** 응답이 밝힌 출처 문자열(`agent_report`). 없으면 빈 문자열. */
  source: string;
};

function nonNegativeInt(value: unknown): number {
  return typeof value === "number"
    && Number.isInteger(value)
    && Number.isFinite(value)
    && value >= 0
    ? value
    : 0;
}

function notReported(
  detail: string,
  discardedCount: number,
  source: string,
): SelfReportedTools {
  return {
    kind: "not_reported",
    // ⚠️ 「자기보고 없음」에서 출처 이름을 뺐어요 (IH-187, 제품 오너 결정 — 도구 호출 칸에서
    // 「agent 자기보고」 문구를 걷었어요). **뜻은 그대로예요** — 「보고가 없다」이지 「도구를
    // 안 썼다」가 아니고, `kind: "not_reported"` 가 그 구분의 주인이에요(`no_tools` 와 별개).
    // 숫자 0 으로 그려지지 않게 막는 건 `activityMetricValue` 고, 이 문자열은 표시용이에요.
    label: "보고 없음",
    detail,
    tools: [],
    callCount: 0,
    discardedCount,
    source,
  };
}

/**
 * invoke 응답의 `usage` 를 화면이 그릴 수 있는 형태로 옮겨요.
 *
 * `turnFailed` 는 «왜 자기보고가 없는지» 를 정확히 말하기 위한 힌트예요 — 실패한 턴의 부재는
 * 「agent 가 안 보냈다」가 아니라 「오류 응답에는 안 실린다」예요.
 */
export function selfReportedTools(
  usage: InvocationUsage | null | undefined,
  options: { turnFailed?: boolean } = {},
): SelfReportedTools {
  if (!usage) {
    return notReported(
      options.turnFailed
        ? SELF_REPORTED_TOOLS_FAILED_TURN_DETAIL
        : "이 응답에는 agent 사용량 보고가 없어요.",
      0,
      "",
    );
  }
  const discardedCount = nonNegativeInt(usage.tool_metrics_discarded_count);
  const source = typeof usage.source === "string" ? usage.source : "";
  if (usage.status !== "observed") {
    return notReported(
      toolUsageReasonMessage(usage.reason ?? ""),
      discardedCount,
      source,
    );
  }
  const raw = usage.metrics?.tool_metrics;
  if (!raw || typeof raw !== "object") {
    // `metrics` 는 왔지만 `tool_metrics` 키가 없어요. 「도구 안 씀」과 다른 사실이에요.
    return notReported(
      "agent 가 도구 사용량 항목을 보고하지 않았어요.",
      discardedCount,
      source,
    );
  }
  const tools = Object.entries(raw)
    .map(([fullName, metric]) => {
      const { targetName, toolName } = splitGatewayToolName(fullName);
      return {
        fullName,
        targetName,
        toolName,
        callCount: nonNegativeInt(metric?.call_count),
        successCount: nonNegativeInt(metric?.success_count),
        errorCount: nonNegativeInt(metric?.error_count),
      };
    })
    .sort((a, b) => a.fullName.localeCompare(b.fullName));
  if (tools.length === 0) {
    return {
      kind: "no_tools",
      label: "도구 미사용",
      detail: "이 턴에서는 도구를 부르지 않았다고 agent 가 보고했어요.",
      tools: [],
      callCount: 0,
      discardedCount,
      source,
    };
  }
  return {
    kind: "tools",
    label: "",
    detail: "",
    tools,
    callCount: tools.reduce((sum, tool) => sum + tool.callCount, 0),
    discardedCount,
    source,
  };
}

/** `list_items ×2` 형태의 한 칸 표시. */
export function selfReportedToolLabel(tool: SelfReportedToolCall): string {
  return `${tool.toolName} ×${tool.callCount}`;
}

/** 목록 전체를 한 줄로 (툴팁·문자열 자리용). 전체 이름을 써요. */
export function selfReportedToolsSummaryText(reported: SelfReportedTools): string {
  if (reported.kind === "tools") {
    return reported.tools
      .map((tool) => `${tool.fullName} ×${tool.callCount}`)
      .join(" | ");
  }
  return reported.detail ? `${reported.label} — ${reported.detail}` : reported.label;
}

/**
 * 두 출처가 «어긋날» 때 그 사실을 문장으로 내놔요. 어긋남을 숨기지 않는 게 규율이에요.
 *
 * `selfReportAvailable` 이 거짓이면 빈 문자열이에요 — 비교할 값이 없는 걸 「어긋난다」고
 * 말하면 없는 사실을 만드는 거예요. 어느 쪽 값도 다른 쪽으로 «고치지» 않아요.
 */
export function toolSourceDisagreementText(
  logObservedCalls: number,
  selfReportedCalls: number,
  selfReportAvailable: boolean,
): string {
  if (!selfReportAvailable) return "";
  if (logObservedCalls === selfReportedCalls) return "";
  return (
    `${LOG_OBSERVED_TOOLS_SOURCE_LABEL} ${logObservedCalls}회 · `
    + `${SELF_REPORTED_TOOLS_SOURCE_LABEL} ${selfReportedCalls}회 — 두 출처가 어긋나요. `
    + "로그 조회는 폴링을 시작한 시점 이후만 읽어서 앞선 턴의 줄이 빠질 수 있어요. "
    + "어느 쪽도 다른 쪽으로 고치지 않고 둘 다 그대로 둬요."
  );
}

/** 턴 하나의 두 출처를 비교해요. */
export function toolObservationDisagreement(
  logObservedCalls: number,
  reported: SelfReportedTools,
): string {
  return toolSourceDisagreementText(
    logObservedCalls,
    reported.callCount,
    reported.kind !== "not_reported",
  );
}
