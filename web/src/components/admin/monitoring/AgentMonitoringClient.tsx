"use client";

import { useMemo, useState } from "react";
import useSWR from "swr";

import {
  getAgentMonitoringFleet,
  type AgentMonitoring,
  type AgentMonitoringFleet,
  type Metric,
  type SummaryTile,
} from "@/lib/api";
import {
  ariaSortFor,
  fleetSortSummary,
  instrumentationGuidance,
  governanceLabel,
  metricPresentation,
  nextFleetSort,
  pipelineFreshnessPresentation,
  pipelineGapPresentation,
  pipelineNoticeKind,
  reconciliationLabel,
  sortAgents,
  type FleetSort,
  type FleetSortKey,
} from "@/lib/monitoring/model";
import { cn } from "@/lib/ui";
import { Card } from "@/components/ui/card";
import { Icon } from "@/components/ui/icon";

type FleetFilter =
  | "all"
  | "diverged"
  | "authorization"
  | "cost"
  | "errors"
  | "instrumentation";

const FILTER_LABELS: Record<FleetFilter, string> = {
  all: "전체",
  diverged: "정합 이상",
  authorization: "인가 거부",
  cost: "비용",
  errors: "오류",
  instrumentation: "계기 없음",
};

export function AgentMonitoringClient() {
  const { data, error, isLoading, isValidating, mutate } =
    useSWR<AgentMonitoringFleet>(
      "admin/agents/monitoring",
      getAgentMonitoringFleet,
      { refreshInterval: 0, revalidateOnFocus: false },
    );
  const [filter, setFilter] = useState<FleetFilter>("all");
  const [sort, setSort] = useState<FleetSort | null>(null);

  const agents = useMemo(
    () => sortAgents(filterAgents(data?.agents ?? [], filter), sort),
    [data, filter, sort],
  );
  const pipelineGap = data
    ? pipelineGapPresentation(data.pipeline)
    : null;
  const pipelineNotice = data
    ? pipelineNoticeKind(data.pipeline.reason)
    : "alert";
  const pipelineFreshness = data
    ? pipelineFreshnessPresentation(data.pipeline)
    : null;

  return (
    <div>
      <header className="mb-5 flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold">모니터링</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            Agent 플릿의 배포, 정합, 계기 상태를 확인해요.
          </p>
          {pipelineFreshness && (
            <p className="mt-1 text-xs text-muted-foreground">
              {pipelineFreshness}
            </p>
          )}
        </div>
        <button
          type="button"
          title="모니터링 데이터 갱신"
          aria-label="모니터링 데이터 갱신"
          disabled={isValidating}
          onClick={() => mutate()}
          className="grid size-8 place-items-center rounded border border-border bg-background hover:bg-muted disabled:opacity-50"
        >
          <Icon name="refresh" size={16} className={isValidating ? "animate-spin" : ""} />
        </button>
      </header>

      {isLoading ? (
        <LoadingState />
      ) : error || !data ? (
        <Card className="p-5 text-sm text-red-700">
          모니터링 데이터를 불러오지 못했어요.
        </Card>
      ) : (
        <div className="space-y-5">
          {/*
            적재 파이프라인 고지 — 문구는 그대로 남기고 «경보 연출» 만 껐어요(2026-09-06).
            이 한 덩어리가 적재 관련 고지 문구를 전부 그려요(호출 없음·적재 멈춤·호출 유무
            미확인·실패 증거 조회 실패·DLQ 일부 복구·적재 실패·적재 범위 각주). 서버는 span 에
            구멍이 있어도 집계를 가리지 않고 내려주니, 이 줄이 사라지면 표의 호출·토큰·지연·
            오류율이 「전수 관측값」처럼 보여요. 지우지 말고 색만 중립으로 둬요 —
            `pipelineNoticeKind` 가 "alert" 를 줘도 같은 회색 정보 줄로 그립니다.
          */}
          {pipelineGap && (
            <div
              role={pipelineNotice}
              className="border border-border bg-muted/50 px-4 py-2 text-sm text-foreground"
            >
              {pipelineGap}
            </div>
          )}
          {data.truncated && (
            <div
              role="status"
              className="border border-amber-300 bg-amber-50 px-4 py-2 text-sm text-amber-900"
            >
              모집단 불완전 · {data.observed_population.toLocaleString()}건 관측
            </div>
          )}
          <SummaryTiles
            summary={data.summary}
            active={filter}
            onSelect={setFilter}
          />
          <AgentTable
            agents={agents}
            filter={filter}
            onReset={() => setFilter("all")}
            sort={sort}
            onSort={(key) => setSort((current) => nextFleetSort(current, key))}
          />
        </div>
      )}
    </div>
  );
}

function SummaryTiles({
  summary,
  active,
  onSelect,
}: {
  summary: AgentMonitoringFleet["summary"];
  active: FleetFilter;
  onSelect: (filter: FleetFilter) => void;
}) {
  // 2026-09-06: 「비용」·「오류」·「관측 건강」 타일을 화면에서 뺐어요. 근거가 세 타일마다
  // 달라서 하나로 묶어 적으면 거짓이 돼요 — 실제로 첫 판 주석이 그렇게 틀렸어요.
  //
  // ⑴ 「비용」·「오류」 — 서버가 사유와 함께 **하드코딩 미관측**을 내려요
  //    (`monitoring/router.py` `_summary` 의 `cost=`·`errors=` 는 `_unobserved(...)` 리터럴이에요).
  //    어떤 데이터가 들어와도 숫자가 될 수 없으니, 값이 될 수 없는 칸을 자리만 잡아 두는 것보다
  //    제공하지 않는 편이 정직해요.
  // ⑵ 「관측 건강」 — 이건 **실제로 계산되는 원장 카운트**예요(같은 함수의
  //    `observability_health=` 는 `ledger_count(sum(...))` 이고, 라이브에서 `0 / 0` 을 냈어요).
  //    ⑴ 과 같은 이유로 뺀 게 아니에요. 뺀 이유는 제품 오너 요청이고, **같은 사실이 agent 표의
  //    「계기」 열·회색 처리된 행·「지표를 만들려면 재배포가 필요해요」 안내로 agent 별로 계속
  //    보이기 때문에** 고지가 사라지지 않아요.
  //
  // ⚠️ 그래서 비용·오류 타일을 되살릴 때는 **관측 건강 타일도 같이** 되살려요. 그 타일이 없으면
  //    span 기반 타일이 「계기가 없어서 미관측」인지 「집계가 없어서 미관측」인지 구분되지 않아요.
  //
  // ⚠️ 그래도 `FleetFilter` 유니온과 `filterAgents` 의 cost/errors/instrumentation 분기는
  // 그대로 남겼어요. 유니온을 줄이면 `FILTER_LABELS`(exhaustive Record)·`filterAgents`·
  // `unavailableFilter` 배열까지 연쇄로 손대야 하고, 그 분기들은 지금도 「집계가 미관측이라
  // 대상 Agent를 특정할 수 없어요」 라는 정직한 빈 상태를 만들어요. 타일만 빠진 거예요.
  const tiles: Array<{
    label: string;
    filter: FleetFilter;
    value: SummaryTile;
    primarySuffix?: string;
    secondaryLabel?: string;
  }> = [
    {
      label: "정합 이상",
      filter: "diverged",
      value: summary.coherence_anomalies,
      primarySuffix: "불일치",
      secondaryLabel: "미관측",
    },
    {
      label: "인가 거부",
      filter: "authorization",
      value: summary.authorization_denials,
    },
  ];

  return (
    <section className="grid grid-cols-1 gap-3 sm:grid-cols-2">
      {tiles.map((tile) => {
        const primary = metricPresentation(
          tile.value.primary,
          (value) => `${value.toLocaleString()}${tile.primarySuffix ? ` ${tile.primarySuffix}` : ""}`,
        );
        const secondary = tile.value.secondary
          ? metricPresentation(
              tile.value.secondary,
              (value) => `${value.toLocaleString()} ${tile.secondaryLabel ?? ""}`.trim(),
            )
          : null;
        const selected = active === tile.filter;
        return (
          <button
            key={tile.label}
            type="button"
            aria-pressed={selected}
            // 눌린 타일을 한 번 더 누르면 전체로 돌아와요. 「필터를 켠 버튼」이 「끄는 버튼」이기도
            // 해야 해요 — 표 헤더의 「전체 보기」만 두면 어디서 풀어야 하는지 안 보여요.
            title={
              selected
                ? `${tile.label} 필터를 해제하고 전체로 돌아가요`
                : `${tile.label} 인 Agent만 봐요`
            }
            onClick={() => onSelect(selected ? "all" : tile.filter)}
            className={cn(
              "min-h-28 rounded border bg-card p-4 text-left transition-colors",
              selected
                ? "border-blue-500 ring-1 ring-blue-500"
                : "border-border hover:border-slate-400",
            )}
          >
            <span className="flex items-center gap-1.5 text-xs font-semibold text-muted-foreground">
              {tile.label}
              {selected && (
                <span className="rounded bg-blue-50 px-1.5 py-0.5 text-[10px] font-medium text-blue-700">
                  필터 켜짐 · 다시 누르면 전체
                </span>
              )}
            </span>
            <span
              title={primary.tooltip}
              className={cn(
                "mt-2 block text-xl font-bold",
                primary.unobserved && "text-slate-500",
              )}
            >
              {primary.text}
            </span>
            <span
              title={secondary?.tooltip}
              className="mt-1 block text-[11px] text-muted-foreground"
            >
              {primary.unobserved ? primary.detail : secondary?.text ?? " "}
            </span>
          </button>
        );
      })}
    </section>
  );
}

function AgentTable({
  agents,
  filter,
  onReset,
  sort,
  onSort,
}: {
  agents: AgentMonitoring[];
  filter: FleetFilter;
  onReset: () => void;
  sort: FleetSort | null;
  onSort: (key: FleetSortKey) => void;
}) {
  const unavailableFilter = ["authorization", "cost", "errors"].includes(filter);
  const sortSummary = fleetSortSummary(sort);
  return (
    <Card className="overflow-hidden p-0">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border px-4 py-3">
        <div className="text-sm font-semibold">
          Agent 플릿
          <span className="ml-2 text-xs font-normal text-muted-foreground">
            {FILTER_LABELS[filter]} · {agents.length}건
          </span>
          {sortSummary && (
            <span className="ml-2 text-xs font-normal text-muted-foreground">
              · {sortSummary}
            </span>
          )}
        </div>
        {filter !== "all" && (
          // 전체로 돌아가는 두 번째 길이에요(첫 번째는 켜진 타일을 다시 누르기). 텍스트 링크가
          // 아니라 테두리 있는 버튼이라야 필터가 켜졌을 때 눈에 들어와요.
          <button
            type="button"
            onClick={onReset}
            title={`${FILTER_LABELS[filter]} 필터를 해제하고 전체 Agent를 봐요`}
            className="inline-flex items-center gap-1 rounded border border-blue-300 bg-blue-50 px-2.5 py-1 text-xs font-medium text-blue-800 hover:bg-blue-100"
          >
            {/* `refresh` 는 「다시 불러오기」로 읽혀요 — 이 버튼은 필터를 «지우는» 거예요. */}
            <Icon name="close" size={12} />
            전체 보기
          </button>
        )}
      </div>
      <div className="overflow-x-auto">
        {/* 「추정 비용」 열을 뺀 만큼(대략 100px) 최소 폭도 줄였어요. */}
        <table className="min-w-[1180px] w-full text-sm">
          <thead className="bg-muted/60 text-left text-[11px] text-muted-foreground">
            {/* 여덟 열 전부 정렬 가능해요. 클릭은 오름 → 내림 → 해제 3단이고, 해제가 서버가 준
                원래 순서예요. 미관측 행은 어느 방향에서도 뒤에 남아요(`sortAgents` 규칙). */}
            <tr>
              <Th sortKey="name" sort={sort} onSort={onSort}>Agent</Th>
              <Th sortKey="registry" sort={sort} onSort={onSort}>상태</Th>
              <Th sortKey="reconciliation" sort={sort} onSort={onSort}>정합</Th>
              <Th sortKey="instrumentation" sort={sort} onSort={onSort}>계기</Th>
              <Th sortKey="invocations" sort={sort} onSort={onSort} align="right">호출</Th>
              <Th sortKey="tokens" sort={sort} onSort={onSort} align="right">토큰</Th>
              {/* 「추정 비용」 열은 뺐어요(2026-09-06) — 서버가 비용을 계산하지 않아서 어떤
                  데이터가 들어와도 모든 행이 `미관측 · 비용 계약 미정` 이거든요. 되살리려면
                  비용 계산이 먼저예요. 「오류율」 열은 실제 관측값이 있어서 남겨 뒀어요. */}
              <Th sortKey="p95_latency" sort={sort} onSort={onSort} align="right">p95 지연</Th>
              <Th sortKey="error_rate" sort={sort} onSort={onSort} align="right">오류율</Th>
            </tr>
          </thead>
          <tbody>
            {agents.map((agent) => (
              <AgentRow key={agent.record_id} agent={agent} />
            ))}
            {agents.length === 0 && (
              <tr>
                <td colSpan={8} className="px-4 py-10 text-center text-xs text-muted-foreground">
                  {unavailableFilter
                    ? "집계가 미관측이라 대상 Agent를 특정할 수 없어요."
                    : "조건에 맞는 Agent가 없어요."}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </Card>
  );
}

function AgentRow({ agent }: { agent: AgentMonitoring }) {
  const guidance = instrumentationGuidance(agent.instrumentation);
  return (
    <tr
      className={cn(
        "border-t border-border align-top",
        agent.instrumentation === "not_configured"
          ? "bg-slate-100/80 text-slate-600"
          : "hover:bg-muted/30",
      )}
    >
      <td className="px-4 py-3">
        <div className="font-semibold text-foreground">{agent.name}</div>
        <div className="mt-0.5 text-[11px] text-muted-foreground">
          v{agent.version} · {agent.owner || "소유자 미관측"}
        </div>
        {agent.owner_team && (
          <div className="text-[11px] text-muted-foreground">{agent.owner_team}</div>
        )}
      </td>
      <td className="px-4 py-3 text-xs">
        <LabeledValue label="Registry" value={agent.registry_status} />
        <LabeledValue label="배포 job" value={agent.deployment.phase ?? "미관측"} />
        <LabeledValue label="스캔" value={governanceLabel(agent.governance)} />
      </td>
      <td className="px-4 py-3 text-xs">
        <Verdict agent={agent} />
      </td>
      <td className="px-4 py-3 text-xs">
        <Instrumentation agent={agent} />
        {guidance && (
          <p className="mt-1 max-w-36 text-[11px] leading-4 text-slate-500">
            {guidance}
          </p>
        )}
      </td>
      <MetricCell metric={agent.metrics.invocations} />
      <MetricCell metric={agent.metrics.tokens} />
      <MetricCell
        metric={agent.metrics.p95_latency}
        format={(value) => `${value.toLocaleString()} ms`}
      />
      <MetricCell
        metric={agent.metrics.error_rate}
        format={(value) => `${value.toLocaleString()}%`}
      />
    </tr>
  );
}

function Verdict({ agent }: { agent: AgentMonitoring }) {
  const { reconciliation } = agent;
  const label = reconciliationLabel(reconciliation);
  return (
    <div>
      <span
        className={cn(
          "inline-flex rounded px-1.5 py-0.5 font-semibold",
          reconciliation.verdict === "coherent" && "bg-emerald-50 text-emerald-700",
          reconciliation.verdict === "diverged" && "bg-red-50 text-red-700",
          reconciliation.verdict === "unknown" && "bg-slate-200 text-slate-600",
        )}
      >
        {label}
      </span>
      <LabeledValue
        label="선언"
        value={reconciliation.expected == null ? "미관측" : `${reconciliation.expected.length}개`}
      />
      <LabeledValue
        label="실체"
        value={reconciliation.observed == null ? "미관측" : `${reconciliation.observed.length}개`}
      />
      {reconciliation.self_validation && (
        <div className="mt-1 text-[11px] font-medium text-amber-700">자기검증 주의</div>
      )}
    </div>
  );
}

function Instrumentation({ agent }: { agent: AgentMonitoring }) {
  const labels = {
    enabled: "있음",
    not_configured: "없음",
    unknown: "미관측",
  };
  return (
    <span className="inline-flex rounded bg-slate-200 px-1.5 py-0.5 font-semibold">
      {labels[agent.instrumentation]}
    </span>
  );
}

function MetricCell({
  metric,
  format,
}: {
  metric: Metric;
  format?: (value: number) => string;
}) {
  const presentation = metricPresentation(metric, format);
  return (
    <td className="px-4 py-3 text-right text-xs tabular-nums" title={presentation.tooltip}>
      {presentation.unobserved ? (
        <span>
          <span className="inline-flex rounded bg-slate-200 px-1.5 py-0.5 font-semibold text-slate-600">
            {presentation.text}
          </span>
          <span className="mt-1 block text-[10px] text-muted-foreground">
            {presentation.detail}
          </span>
        </span>
      ) : (
        <span>
          <span className="font-semibold text-foreground">
            {presentation.text}
          </span>
          {presentation.detail && (
            <span className="mt-1 block text-[10px] text-muted-foreground">
              {presentation.detail}
            </span>
          )}
        </span>
      )}
    </td>
  );
}

function LabeledValue({ label, value }: { label: string; value: string }) {
  return (
    <div className="mt-1 flex items-baseline gap-1.5 whitespace-nowrap">
      <span className="text-[10px] font-semibold text-muted-foreground">{label}</span>
      <span>{value}</span>
    </div>
  );
}

function Th({
  children,
  sortKey,
  sort,
  onSort,
  align = "left",
}: {
  children: React.ReactNode;
  sortKey: FleetSortKey;
  sort: FleetSort | null;
  onSort: (key: FleetSortKey) => void;
  align?: "left" | "right";
}) {
  const state = ariaSortFor(sort, sortKey);
  const active = state !== "none";
  return (
    <th
      aria-sort={state}
      className={cn(
        "whitespace-nowrap px-4 py-2.5 font-semibold",
        align === "right" && "text-right",
      )}
    >
      <button
        type="button"
        onClick={() => onSort(sortKey)}
        title={
          state === "none"
            ? "오름차순으로 정렬해요"
            : state === "ascending"
              ? "내림차순으로 바꿔요"
              : "정렬을 해제하고 원래 순서로 돌아가요"
        }
        className={cn(
          "inline-flex items-center gap-1 rounded hover:text-foreground",
          align === "right" && "flex-row-reverse",
          active && "text-foreground",
        )}
      >
        {children}
        <span aria-hidden="true" className={cn("text-[9px]", !active && "opacity-40")}>
          {state === "ascending" ? "▲" : state === "descending" ? "▼" : "▲▼"}
        </span>
      </button>
    </th>
  );
}

function LoadingState() {
  return (
    <div className="space-y-5 animate-pulse">
      {/* 타일 개수와 grid 열 수는 `SummaryTiles` 와 같은 짝이어야 해요 — 어긋나면 로딩 중
          칸 수가 본문과 달라서 화면이 튑니다. */}
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        {Array.from({ length: 2 }, (_, index) => (
          <div key={index} className="h-28 rounded border border-border bg-muted" />
        ))}
      </div>
      <div className="h-72 rounded border border-border bg-muted" />
    </div>
  );
}

function filterAgents(
  agents: AgentMonitoring[],
  filter: FleetFilter,
): AgentMonitoring[] {
  if (filter === "all") return agents;
  if (filter === "diverged") {
    return agents.filter((agent) => agent.reconciliation.verdict === "diverged");
  }
  if (filter === "instrumentation") {
    return agents.filter((agent) => agent.instrumentation === "not_configured");
  }
  return [];
}
