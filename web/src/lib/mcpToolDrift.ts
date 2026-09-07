import type {
  AssetToolDrift,
  SensitivityChangeRequest,
  SensitivityChangePlan,
  SensitivityChangeStatus,
  SensitivityHistoryEvent,
  SensitivityImpact,
  SensitivitySource,
  ToolDriftEntry,
  ToolDriftState,
  ToolSchemaDiff,
} from "./api/mcpToolDrift";
import type { GatewayTargetQuota } from "./api/governance";

/**
 * 드리프트 화면의 문구·색을 만드는 순수 함수들 (LC-03).
 *
 * 이 프로젝트의 반복 결함은 "조용히 막힘"이에요. 그래서 못 부르는 상태는 반드시
 * **못 부른다고 쓰여 있어야** 해요 — 빈 칸이나 회색 배지로 넘기지 않아요.
 */

export const DRIFT_STATE_LABEL: Record<ToolDriftState, string> = {
  DISCOVERED: "DISCOVERED",
  ACTIVE: "ACTIVE",
  CHANGED: "CHANGED",
  MISSING: "MISSING",
  RETIRED: "RETIRED",
};

/** 상태 배지 색. 닫힌 상태(빨강)와 확인 대기(주황)를 눈으로 구분해요. */
export const DRIFT_STATE_TONE: Record<ToolDriftState, string> = {
  DISCOVERED: "bg-amber-100 text-amber-900",
  ACTIVE: "bg-emerald-100 text-emerald-800",
  CHANGED: "bg-orange-100 text-orange-900",
  MISSING: "bg-red-100 text-red-800",
  RETIRED: "bg-slate-100 text-slate-600",
};

/** 상태의 뜻 — 표 헤더 툴팁·범례에 쓰는 한 줄. */
export const DRIFT_STATE_MEANING: Record<ToolDriftState, string> = {
  DISCOVERED: "MCP 에 새로 나타났고 아직 민감도 태그가 없어요",
  ACTIVE: "태깅되고 원장에 반영됐어요",
  CHANGED: "스키마·설명이 바뀌었어요 (이전 태그로 계속 불려요)",
  MISSING: "상류 목록에서 사라졌고 Target 정리 상태를 확인 중이에요",
  RETIRED: "Target catalog 동기화와 후속 부재 확인 완료",
};

/** "부를 수 있나" 열. 못 부르는 이유를 문장으로 말해요. */
export function callabilityText(
  entry: ToolDriftEntry,
  targetMode?: AssetToolDrift["target_mode"],
): string {
  if (entry.reappeared && entry.callable_now === null) {
    return "상류 목록에 다시 나타났어요 · 실제 호출 가능성은 확인하지 못했어요";
  }
  if (
    targetMode === "connected"
    && entry.state === "DISCOVERED"
    && entry.callable_now === null
  ) {
    return "상류 목록에 나타났어요 · 실제 호출 가능성은 확인하지 못했어요";
  }
  if (entry.state === "MISSING") {
    return targetMode === "connected"
      ? "상류 목록에서 사라졌어요 · Target 동기화 완료를 확인하지 못했어요"
      : "상류 목록에서 사라졌지만 배포된 Target에는 남아 있어요 · per-agent 정책이 남아 있으면 정책 갱신이 막혀요 · 제거는 IA-68";
  }
  if (entry.callable_now === null) {
    return "실제 호출 가능성은 확인하지 못했어요";
  }
  if (entry.callable_now) {
    return entry.callable_groups && entry.callable_groups.length > 0
      ? entry.callable_groups.join(" · ")
      : "호출 가능";
  }
  switch (entry.state) {
    case "DISCOVERED":
      return "아무도 못 불러요";
    case "RETIRED":
      return "아무도 못 불러요 (Target 동기화 완료)";
    default:
      return "아무도 못 불러요 (민감도 미지정)";
  }
}

export type TargetPresentation = {
  label: string;
  detail: string;
};

/** Target 열은 태그 기반 예상값이지 실제 Gateway 관측 결과가 아니에요. */
export function targetPresentation(
  entry: ToolDriftEntry,
  targetMode?: AssetToolDrift["target_mode"],
): TargetPresentation {
  if (entry.state === "MISSING" && targetMode === "connected") {
    return {
      label: "동기화 미완료",
      detail: "연결형 Target을 명시 동기화하고 완료와 후속 부재를 다시 확인해요",
    };
  }
  if (entry.state === "MISSING") {
    return {
      label: entry.previous_sensitivity
        ? `직전 민감도 ${entry.previous_sensitivity}`
        : "직전 민감도 확인 불가",
      detail: "배포된 Target에는 남아 있어요 · per-agent 정책 잔존 시 정책 갱신 차단 위험 · 제거는 IA-68",
    };
  }
  if (entry.target.kind === "connected") {
    return {
      label: "분할 불가(연결형)",
      detail: entry.state === "RETIRED"
        ? "명시적 동기화 완료와 후속 상류 부재를 확인했어요"
        : "연결형 자산은 민감도별 Target으로 나누지 않아요",
    };
  }
  if (entry.target.basis === "unknown_sensitivity") {
    return {
      label: "확인 불가",
      detail: "민감도를 확인하지 못해 Target을 계산할 수 없어요",
    };
  }
  if (entry.target.kind === "none" || !entry.target.name) {
    return {
      label: "없음",
      detail: "아무도 못 불러요",
    };
  }
  return {
    label: entry.target.name,
    detail: "태그로 계산한 예상값 · 실제 적용 여부는 정책 화면에서 확인",
  };
}

export function sensitivityChangeFromError(
  error: unknown,
): SensitivityChangeRequest | null {
  const apiError = (
    error && typeof error === "object"
      ? error as {
          status?: unknown;
          detail?: { code?: unknown; change?: unknown };
        }
      : null
  );
  const carriesChange = (
    apiError?.status === 502
    && apiError.detail?.code === "sensitivity_propagation_failed"
  ) || (
    apiError?.status === 409
    && apiError.detail?.code === "impact_changed"
  );
  if (!carriesChange) {
    return null;
  }
  const change = apiError?.detail?.change;
  if (!change || typeof change !== "object") return null;
  const candidate = change as Record<string, unknown>;
  if (
    typeof candidate.request_id !== "string"
    || ![
      "PENDING_APPROVAL",
      "APPROVED_PENDING_PROPAGATION",
      "APPLYING",
      "APPLIED",
      "FAILED",
    ].includes(
      String(candidate.status),
    )
    || !Array.isArray(candidate.impact)
    || !candidate.movement
    || typeof candidate.movement !== "object"
  ) {
    return null;
  }
  return change as SensitivityChangeRequest;
}

export type QuotaPresentation = {
  tone: "ok" | "warning" | "unknown";
  label: string;
  detail: string;
};

export function quotaPresentation(quota: GatewayTargetQuota): QuotaPresentation {
  if (
    quota.status !== "ok"
    || quota.current === null
    || quota.quota === null
    || quota.usage_ratio === null
    || quota.threshold_reached === null
  ) {
    return {
      tone: "unknown",
      label: "Target 쿼터 확인 불가",
      detail: quota.reason || "현재 사용량이나 적용 한도를 관측하지 못했어요",
    };
  }
  const percent = Math.round(quota.usage_ratio * 100);
  return {
    tone: quota.threshold_reached ? "warning" : "ok",
    label: `Target ${quota.current} / ${quota.quota} (${percent}%)`,
    detail: quota.threshold_reached
      ? `${Math.round(quota.threshold_ratio * 100)}% 임계 도달 · 한도 상향 요청이 필요해요`
      : `${Math.round(quota.threshold_ratio * 100)}% 임계 미도달`,
  };
}

/** "다음 행동" 열 — 관리자가 실제로 무엇을 해야 하는지. */
export function nextActionText(
  entry: ToolDriftEntry,
  targetMode?: AssetToolDrift["target_mode"],
): string {
  if (
    entry.pending_change?.status === "APPROVED_PENDING_PROPAGATION"
  ) {
    return "IA-68 전파 경로 대기";
  }
  switch (entry.state) {
    case "DISCOVERED":
      return entry.reappeared
        ? "사라졌다 다시 나타났어요 — 민감도를 다시 정해주세요"
        : "민감도를 정해주세요";
    case "CHANGED":
      return "인가 조건을 재확인해 주세요";
    case "MISSING":
      return targetMode === "connected"
        ? "Target 동기화를 다시 확인해요"
        : "IA-53 컷오버 확인 · IA-68에서 Target 제거";
    case "RETIRED":
    case "ACTIVE":
    default:
      return "—";
  }
}

const SENSITIVITY_CHANGE_STATUS_TEXT: Record<
  SensitivityChangeStatus,
  string
> = {
  PENDING_APPROVAL: "승인 대기",
  APPROVED_PENDING_PROPAGATION: "승인됨·전파 대기(IA-68)",
  APPLYING: "이동 중",
  APPLIED: "적용 완료",
  FAILED: "이동 실패",
};

export function sensitivityChangeStatusText(
  change: SensitivityChangeRequest,
): string {
  if (change.status === "APPLYING" && change.retryable) {
    return "이동 복구 필요";
  }
  return SENSITIVITY_CHANGE_STATUS_TEXT[change.status];
}

/** 무엇이 바뀌었나 — `user_id → owner_id` 같은 사람이 읽는 줄들. */
export function describeDiff(diff: ToolSchemaDiff | null): string[] {
  if (!diff) return [];
  const lines: string[] = [];
  for (const [before, after] of diff.renamed) {
    lines.push(`${before} → ${after}`);
  }
  for (const [field, before, after] of diff.type_changed) {
    lines.push(`${field}: ${before} → ${after}`);
  }
  if (diff.added.length > 0) lines.push(`추가: ${diff.added.join(", ")}`);
  if (diff.removed.length > 0) lines.push(`삭제: ${diff.removed.join(", ")}`);
  if (diff.required_added.length > 0) {
    lines.push(`필수로 바뀜: ${diff.required_added.join(", ")}`);
  }
  if (diff.required_removed.length > 0) {
    lines.push(`필수 아님으로 바뀜: ${diff.required_removed.join(", ")}`);
  }
  return lines;
}

/** "마지막 확인 N분 전". 확인한 적이 없거나 관측 실패면 그렇게 말해요. */
export function lastCheckedText(asset: AssetToolDrift, now: Date = new Date()): string {
  if (asset.check_status === "never_checked" || !asset.last_checked_at) {
    return "아직 확인한 적 없어요";
  }
  const then = Date.parse(asset.last_checked_at);
  if (Number.isNaN(then)) return "마지막 확인 시각을 읽을 수 없어요";
  const minutes = Math.max(0, Math.floor((now.getTime() - then) / 60000));
  if (minutes < 1) return "마지막 확인 방금 전";
  if (minutes < 60) return `마지막 확인 ${minutes}분 전`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `마지막 확인 ${hours}시간 전`;
  return `마지막 확인 ${Math.floor(hours / 24)}일 전`;
}

export type DriftBanner = {
  /** "ok" = 드리프트 없음, "drift" = 어긋남, "unknown" = 확인 못 했어요. */
  tone: "ok" | "drift" | "unknown";
  headline: string;
  detail: string;
};

/**
 * 자산별 드리프트 배너.
 *
 * `unknown`은 "드리프트 없음"과 절대 합치지 않아요 — 관측하지 못한 걸 통과로 보여주면
 * 화면이 거짓말을 해요(ADR-0037 §4).
 */
export function driftBanner(asset: AssetToolDrift, now?: Date): DriftBanner {
  const checked = lastCheckedText(asset, now);
  const summary =
    `신규 ${asset.counts.DISCOVERED} · ` +
    `변경 ${asset.counts.CHANGED} · ` +
    `사라짐 ${asset.counts.MISSING}`;

  if (asset.check_status === "unknown") {
    return {
      tone: "unknown",
      headline: `${asset.asset_name} — 지금 상태를 확인하지 못했어요`,
      detail: `${checked} · ${asset.check_error ?? "MCP 조회 실패"} · 아래 상태는 마지막 관측 결과예요`,
    };
  }
  if (asset.check_status === "never_checked") {
    return {
      tone: "unknown",
      headline: `${asset.asset_name} — 등록 기록만 있어요`,
      detail: `${checked} · 아래는 등록 시점 목록이에요 · ${summary}`,
    };
  }
  if (asset.has_drift) {
    return {
      tone: "drift",
      headline: `${asset.asset_name} — 목록이 어긋났어요`,
      detail: `${checked} · MCP 가 도구를 바꿨어요 · ${summary}`,
    };
  }
  return {
    tone: "ok",
    headline: `${asset.asset_name} — 목록이 일치해요`,
    detail: `${checked} · 도구 ${asset.tools.length}개`,
  };
}

// --- 민감도 태그 편집 (티켓 A) ------------------------------------------------

/** 출처 라벨. "MCP 가 선언한 값"과 "우리가 짐작한 값"이 같아 보이면 안 돼요. */
export const SENSITIVITY_SOURCE_LABEL: Record<SensitivitySource, string> = {
  descriptor: "descriptor",
  name_guess: "이름 추정",
  auto: "자동 분류",
  admin: "관리자 지정",
  unknown: "출처 미기록",
};

/** 출처를 얼마나 믿을 수 있나 — 배지 색으로 구분해요. */
export const SENSITIVITY_SOURCE_TONE: Record<SensitivitySource, string> = {
  descriptor: "bg-sky-100 text-sky-900",
  name_guess: "bg-amber-100 text-amber-900",
  auto: "bg-amber-100 text-amber-900",
  admin: "bg-emerald-100 text-emerald-800",
  unknown: "bg-slate-100 text-slate-600",
};

/** 출처 한 줄 설명 — 관리자가 무엇을 먼저 확인해야 하는지 알려줘요. */
export const SENSITIVITY_SOURCE_MEANING: Record<SensitivitySource, string> = {
  descriptor: "MCP 서버가 tools/list 에서 직접 선언했어요",
  name_guess: "도구 이름의 첫 낱말로 우리가 짐작했어요 — 확인이 필요해요",
  auto: "등록 시점 자동 분류가 채웠어요 — 확인이 필요해요",
  admin: "관리자가 확정했어요",
  unknown: "태그는 있는데 출처 기록이 없어요 — 확인이 필요해요",
};

/** 출처 표시. 미분류면 태그가 없으니 출처도 없어요. */
export function sourceLabel(entry: ToolDriftEntry): string {
  if (!entry.sensitivity) return "없음";
  return SENSITIVITY_SOURCE_LABEL[entry.sensitivity_source ?? "unknown"];
}

/** 관리자가 확정한 값인가 — 추정값에는 확인 표시를 붙여요. */
export function isConfirmedByAdmin(entry: ToolDriftEntry): boolean {
  return entry.sensitivity_source === "admin" && Boolean(entry.sensitivity);
}

/**
 * 태그를 바꿀 수 있나. 못 바꾸면 **왜 못 바꾸는지**를 함께 돌려줘요 —
 * 버튼만 비활성화하면 화면이 다시 조용히 막혀요.
 */
export function editability(entry: ToolDriftEntry): {
  editable: boolean;
  reason: string;
} {
  if (entry.state === "MISSING") {
    return {
      editable: false,
      reason: "MCP 에서 사라진 도구예요 — 없는 도구의 민감도는 정할 수 없어요",
    };
  }
  if (entry.state === "RETIRED") {
    return { editable: false, reason: "정리(회수)된 도구예요" };
  }
  return { editable: true, reason: "" };
}

/** 계획서를 사람이 읽는 한 줄로. "FullAccess 만 → 전체 등급" 같은 문장이에요. */
export function callabilityShift(plan: SensitivityChangePlan): string {
  return `${groupsPhrase(plan.groups_before)} → ${groupsPhrase(plan.groups_after)}`;
}

/** 등급 목록을 문구로. 전체를 덮으면 "전체 등급", 비면 "아무도 못 불러요". */
export function groupsPhrase(groups: string[]): string {
  if (groups.length === 0) return "아무도 못 불러요";
  if (groups.length >= 4) return "전체 등급";
  if (groups.length === 1) return `${groups[0]} 만`;
  return groups.join(" · ");
}

/** 영향 범위 한 줄. **모르는 건 숫자로 쓰지 않아요.** */
export function impactText(item: SensitivityImpact): string {
  if (!item.known) return `확인 불가 — ${item.reason}`;
  const count = item.count ?? 0;
  if (count === 0) return "0개 — 아직 아무도 쓰지 않아요";
  const names = item.names.slice(0, 3).join(", ");
  const more = item.names.length > 3 ? ` 외 ${item.names.length - 3}개` : "";
  return names ? `${count}개 — ${names}${more}` : `${count}개`;
}

/**
 * 저장 버튼을 눌러도 되는지. 서버가 최종 판정을 하지만, 화면에서도 같은 이유로
 * 막아 관리자가 사유를 놓친 채 실패를 겪지 않게 해요.
 */
export function saveBlockedReason(
  plan: SensitivityChangePlan | null,
  reason: string,
): string {
  if (!plan) return "영향을 계산하는 중이에요";
  if (plan.no_op) return "지금 값과 같아요";
  if (plan.reason_required && reason.trim().length < 5) {
    return "위험도를 낮추는 변경이에요 — 사유를 5자 이상 적어 주세요";
  }
  return "";
}

/** 승인에는 Target 이름이 바뀌는 agent 영향 관측이 필요해요. */
export function approvalBlockedReason(plan: SensitivityChangePlan | null): string {
  if (!plan) return "영향을 계산하는 중이에요";
  const agents = plan.impact.find((item) => item.label.endsWith("agent"));
  if (!agents || !agents.known) {
    const reason = agents?.reason || "영향받는 agent를 확인하지 못했어요";
    return `영향 agent 확인 불가 — ${reason}`;
  }
  return "";
}

/** 이력 한 줄 — "DELETE → READ (관리자 지정)". 미분류는 그렇게 써요. */
export function historyChangeText(event: SensitivityHistoryEvent): string {
  const before = event.before ?? "미분류";
  const after = event.after ?? "미분류";
  return `${before} → ${after}`;
}

const HISTORY_STAGE_TEXT: Record<SensitivityHistoryEvent["stage"], string> = {
  requested: "요청",
  impact_changed: "영향 변경·재확인 필요",
  approved: "승인",
  retrying: "재시도",
  moved: "이동",
  failed: "실패",
};

export function historyStageText(event: SensitivityHistoryEvent): string {
  if (
    event.stage === "approved"
    && event.change_status === "APPROVED_PENDING_PROPAGATION"
  ) {
    return "승인됨·전파 대기(IA-68)";
  }
  return HISTORY_STAGE_TEXT[event.stage];
}

/** 어긋난 자산이 위로 오게 정렬해요 — 급한 건 스크롤 없이 보여야 해요. */
export function sortByUrgency(assets: AssetToolDrift[]): AssetToolDrift[] {
  const rank = (asset: AssetToolDrift): number => {
    if (asset.counts.MISSING > 0) return 0;
    if (asset.check_status === "unknown") return 1;
    if (asset.has_drift) return 2;
    if (asset.check_status === "never_checked") return 3;
    return 4;
  };
  return [...assets].sort(
    (a, b) => rank(a) - rank(b) || a.asset_name.localeCompare(b.asset_name),
  );
}
