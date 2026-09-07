import type {
  AgentMonitoring,
  Metric,
  UnobservedReason,
} from "@/lib/api/monitoring";

const REASON_LABELS: Record<UnobservedReason, string> = {
  no_instrumentation: "계기 없음",
  no_aggregate: "집계 없음",
  cost_contract_unavailable: "비용 계약 미정",
  population_incomplete: "모집단 불완전",
  not_sampled: "샘플링되지 않음",
  sampling_unknown: "도착 커버리지 미확인",
  pipeline_lag: "적재 지연",
  ingest_stalled: "적재 중단",
  traffic_unobserved: "호출 유무 미관측",
  pipeline_timestamps_unobserved: "적재·span 시각 미관측",
  not_applicable: "적용 대상 아님",
};

export type MetricPresentation = {
  text: string;
  detail: string;
  tooltip: string;
  unobserved: boolean;
};

export function reconciliationLabel(
  reconciliation: AgentMonitoring["reconciliation"],
): string {
  if (reconciliation.verdict === "coherent") return "일치";
  if (reconciliation.verdict === "diverged") return "불일치";
  const reasons = {
    unknown_authorization: "인가 축 미관측",
    not_applicable: "도구 없음",
    builtin_reachability_unprobed: "내장 도구 도달성 미관측",
    verify_missing: "검증 결과 없음",
    report_invalid: "검증 보고서 오류",
    invalid_tool_identifier: "도구 식별자 오류",
    verify_unknown: "검증 사유 미확인",
    self_validation: "자기검증",
    deployment_version_mismatch: "배포 버전 불일치",
    verdict_conflict: "검증 판정 모순",
    declaration_unresolvable: "선언 해석 불가",
  } satisfies Record<
    NonNullable<AgentMonitoring["reconciliation"]["reason"]>,
    string
  >;
  return reconciliation.reason
    ? `미관측 — ${reasons[reconciliation.reason]}`
    : "미관측";
}

export function governanceLabel(
  governance: AgentMonitoring["governance"],
): string {
  if (governance.reason === "scan_version_mismatch") {
    return "미관측 — 스캔 버전 불일치";
  }
  if (governance.reason === "scan_version_unknown") {
    return "미관측 — 스캔 버전 미확인";
  }
  return governance.scan_status ?? "미관측";
}

export function metricPresentation(
  metric: Metric,
  format: (value: number) => string = String,
): MetricPresentation {
  const freshness = metric.as_of ?? "시각 미관측";
  const traceSampling =
    metric.sampling_rate == null
      ? "aws/spans 도착 커버리지 미확인"
      : `aws/spans 관측 도착 비율 ${Math.round(metric.sampling_rate * 100)}%`;
  const tooltip = (
    `source: ${metric.source} | as_of: ${freshness} | `
    + traceSampling
  );
  if (metric.status === "unobserved") {
    return {
      text: "미관측",
      detail: metric.reason ? REASON_LABELS[metric.reason] : "관측 데이터 없음",
      tooltip,
      unobserved: true,
    };
  }
  return {
    text: format(metric.value),
    detail:
      metric.sampling_rate == null
        ? "받은 span 기준 · 전수 여부 미확인"
        : `관측 도착 비율 ${Math.round(metric.sampling_rate * 100)}%`,
    tooltip,
    unobserved: false,
  };
}

function utcMinute(value: string): string {
  return `${value.slice(0, 10)} ${value.slice(11, 16)} UTC`;
}

export function pipelineFreshnessPresentation(
  pipeline: AgentMonitoring["pipeline"],
): string {
  const ingested = pipeline.last_success_at
    ? utcMinute(pipeline.last_success_at)
    : "미관측";
  const newestSpan = pipeline.newest_span_at
    ? utcMinute(pipeline.newest_span_at)
    : "미관측";
  return `마지막 적재 ${ingested} · 최신 span ${newestSpan}`;
}

export function pipelineGapPresentation(
  pipeline: AgentMonitoring["pipeline"],
): string | null {
  if (pipeline.reason === "no_traffic_in_range") {
    return "이 구간에 호출이 없었어요.";
  }
  if (pipeline.reason === "ingest_stalled") {
    const count = pipeline.traffic_count;
    return count == null
      ? "적재가 멈췄어요."
      : `적재가 멈췄어요 · 감사 원장에서 호출 ${count.toLocaleString()}건을 확인했어요.`;
  }
  if (pipeline.reason === "traffic_unobserved") {
    return "호출 유무를 확인하지 못했어요 · 적재 상태를 판정할 수 없어요.";
  }
  if (
    pipeline.reason === "dlq_unobserved"
    || pipeline.reason === "failure_evidence_unobserved"
  ) {
    return "적재 실패 증거를 조회하지 못했어요 · 해소 여부 미확인";
  }
  if (pipeline.reason === "ingest_replay_partial") {
    return (
      `DLQ 재처리 일부 완료 · `
      + `${pipeline.replay_recovered_count}/${pipeline.replay_expected_count}개 복구 · `
      + `${pipeline.replay_failed_count}개 남음`
    );
  }
  if (pipeline.reason === "ingest_failed") {
    if (pipeline.dlq_depth && !pipeline.pending_failure_count) {
      return `적재 실패 · DLQ ${pipeline.dlq_depth}개 메시지 복구 대기`;
    }
    const pending: string[] = [];
    if (pipeline.dlq_depth) {
      pending.push(`DLQ ${pipeline.dlq_depth}개 메시지`);
    }
    if (pipeline.pending_failure_count) {
      pending.push(
        `실패 원장 ${pipeline.pending_failure_count}개 span`,
      );
    }
    if (pending.length > 0) {
      return `적재 실패 · ${pending.join(" · ")} 재처리 대기`;
    }
    return "적재 실패 · DLQ와 실패 원장이 비어 있어 재처리 증거가 없어요.";
  }
  if (pipeline.reason !== "ingest_data_gap") return null;
  const interval =
    pipeline.gap_start_at && pipeline.gap_end_at
      ? `${utcMinute(pipeline.gap_start_at)} ~ ${utcMinute(pipeline.gap_end_at)}`
      : "시각 미확인";
  // 「사건 경보」가 아니라 «범위 각주» 로 읽히게 쓴 문구예요(2026-09-06). 서버가 결손이 있어도
  // 집계를 가리지 않고 내려주니, 이 한 줄이 표의 숫자를 한정하는 유일한 자리예요. 그래서 세
  // 사실을 반드시 담아요 — ⑴ 어느 구간의 span 이 비었는지 ⑵ 그래서 표의 수치가 전수가
  // 아니라는 것 ⑶ 재처리 증거가 없어 이 고지가 닫히지 않는다는 것.
  return (
    `적재 범위 · ${interval} 구간의 span 이 비어 있어 이 표의 수치는 전수가 아니에요 · `
    + "재처리 증거가 남지 않아 이 각주는 계속 붙어 있어요."
  );
}

export function pipelineNoticeKind(
  reason: string | null,
): "status" | "alert" {
  return reason === "no_traffic_in_range" ? "status" : "alert";
}

export function instrumentationGuidance(
  status: AgentMonitoring["instrumentation"],
): string | null {
  return status === "not_configured"
    ? "지표를 만들려면 재배포가 필요해요"
    : null;
}

// ── 플릿 표 정렬 ────────────────────────────────────────────────────────────────────
//
// ⚠️ 이 파일에 두는 이유: 판정을 컴포넌트(`.tsx`)에 두면 테스트 러너가 실행할 수 없어서
// 음성 대조를 걸 수 없어요. 정렬 «판정» 은 여기, 클릭 배선만 컴포넌트예요.
//
// ⚠️ 핵심 규칙 — **미관측은 값이 아니에요.** 미관측 행을 0 이나 -Infinity 로 접어 넣으면
// 「오류율 오름차순」 첫 줄이 「미관측」이 되고, 그건 «오류율이 가장 낮다» 로 읽혀요. 관측하지
// 못한 것을 가장 좋은 값으로 표시하는 셈이라 이 저장소가 금지하는 계열이에요.
// 그래서 미관측 행은 **별 묶음**으로 빼서 **방향과 무관하게 항상 뒤**에 둬요.

export type FleetSortKey =
  | "name"
  | "registry"
  | "reconciliation"
  | "instrumentation"
  | "invocations"
  | "tokens"
  | "p95_latency"
  | "error_rate";

export type SortDirection = "asc" | "desc";

export type FleetSort = { key: FleetSortKey; direction: SortDirection };

/** 정합 판정의 순서 — 문제를 먼저 볼 수 있게요. `unknown` 은 여기 없어요(미관측이니까요). */
const RECONCILIATION_RANK: Record<string, number> = {
  diverged: 0,
  coherent: 1,
};

/** 계기 상태의 순서. `unknown` 은 여기 없어요(미관측이니까요). */
const INSTRUMENTATION_RANK: Record<string, number> = {
  not_configured: 0,
  enabled: 1,
};

/**
 * 정렬 좌표. **`null` 은 「관측하지 못했다」는 뜻이고 값이 아니에요** — 숫자로 접지 마세요.
 *
 * 기대값의 출처가 이 함수 밖이에요: 숫자 열은 서버가 내려준 `Metric.status` 를 그대로 믿고,
 * 정합·계기는 서버가 내려준 열거값을 그대로 읽어요. 여기서 다시 계산하지 않아요.
 */
export function fleetSortValue(
  agent: AgentMonitoring,
  key: FleetSortKey,
): number | string | null {
  switch (key) {
    case "name":
      return agent.name;
    case "registry":
      // 빈 문자열은 「비어 있다」가 관측 결과일 수도 있어 미관측으로 접지 않아요.
      return agent.registry_status;
    case "reconciliation": {
      const rank = RECONCILIATION_RANK[agent.reconciliation.verdict];
      return rank === undefined ? null : rank;
    }
    case "instrumentation": {
      const rank = INSTRUMENTATION_RANK[agent.instrumentation];
      return rank === undefined ? null : rank;
    }
    case "invocations":
      return metricSortValue(agent.metrics.invocations);
    case "tokens":
      return metricSortValue(agent.metrics.tokens);
    case "p95_latency":
      return metricSortValue(agent.metrics.p95_latency);
    case "error_rate":
      return metricSortValue(agent.metrics.error_rate);
  }
}

function metricSortValue(metric: Metric): number | null {
  return metric.status === "unobserved" ? null : metric.value;
}

/**
 * 정렬된 사본을 돌려줘요. 원본 배열은 건드리지 않아요.
 *
 * - `sort` 가 `null` 이면 **서버가 준 순서** 그대로예요(정렬 해제).
 * - 미관측 행은 방향과 무관하게 뒤로 가요(위 규칙).
 * - 같은 값이면 이름으로 갈라서 결정적이에요 — 안 그러면 다시 조회할 때마다 순서가 흔들려요.
 */
export function sortAgents(
  agents: AgentMonitoring[],
  sort: FleetSort | null,
): AgentMonitoring[] {
  if (!sort) return agents;
  const factor = sort.direction === "asc" ? 1 : -1;
  return [...agents].sort((left, right) => {
    const a = fleetSortValue(left, sort.key);
    const b = fleetSortValue(right, sort.key);
    // 미관측은 값이 아니에요 — 방향과 무관하게 항상 뒤.
    if (a === null && b === null) return compareNames(left, right);
    if (a === null) return 1;
    if (b === null) return -1;
    const primary =
      typeof a === "string" && typeof b === "string"
        ? a.localeCompare(b, "ko")
        : Number(a) - Number(b);
    if (primary !== 0) return primary * factor;
    return compareNames(left, right);
  });
}

function compareNames(left: AgentMonitoring, right: AgentMonitoring): number {
  const byName = left.name.localeCompare(right.name, "ko");
  return byName !== 0 ? byName : left.record_id.localeCompare(right.record_id);
}

/**
 * 열 제목을 눌렀을 때 다음 정렬 상태. **오름 → 내림 → 해제** 3단이에요.
 *
 * 해제 단을 넣은 건 「원래 순서로 돌아가는 길」을 만들려는 거예요 — 2단 토글이면 서버가 준
 * 순서를 다시 볼 방법이 없어요.
 */
export function nextFleetSort(
  current: FleetSort | null,
  key: FleetSortKey,
): FleetSort | null {
  if (!current || current.key !== key) return { key, direction: "asc" };
  if (current.direction === "asc") return { key, direction: "desc" };
  return null;
}

/** `<th aria-sort>` 에 넣을 값. 스크린리더가 정렬 상태를 읽을 수 있게요. */
export function ariaSortFor(
  sort: FleetSort | null,
  key: FleetSortKey,
): "ascending" | "descending" | "none" {
  if (!sort || sort.key !== key) return "none";
  return sort.direction === "asc" ? "ascending" : "descending";
}

/**
 * 지금 정렬을 사람 말로. 미관측 묶음이 뒤에 있다는 사실을 **화면에서 밝혀요** — 안 밝히면
 * 관리자가 「오류율 오름차순인데 왜 미관측이 위에 없지」를 결함으로 읽거나, 더 나쁘게는
 * 미관측을 정렬 결과의 일부로 믿어요.
 */
export function fleetSortSummary(sort: FleetSort | null): string | null {
  if (!sort) return null;
  const direction = sort.direction === "asc" ? "오름차순" : "내림차순";
  return `${FLEET_SORT_LABELS[sort.key]} ${direction} · 미관측 행은 방향과 무관하게 뒤에 있어요`;
}

export const FLEET_SORT_LABELS: Record<FleetSortKey, string> = {
  name: "Agent",
  registry: "상태",
  reconciliation: "정합",
  instrumentation: "계기",
  invocations: "호출",
  tokens: "토큰",
  p95_latency: "p95 지연",
  error_rate: "오류율",
};
