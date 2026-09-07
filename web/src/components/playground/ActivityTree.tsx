"use client";

/**
 * 접이식 중첩 활동 트리 — 시안(`docs/design/playground-activity-tree-mockup.html`) 이식.
 *
 * 노드 종류는 리서치의 taxonomy 를 따라요(agent/step → model · memory · tool → authz).
 * 다만 **지금 관측되는 노드만 실선으로 그리고, 관측하지 못한 노드는 흐리게 + "미관측"** 로
 * 표시해요 — 시안의 토큰·비용·지연·span 계층은 OTEL span 이 출처인데 아직 읽지 않아서
 * 값을 만들어 채우지 않아요(ADR-0037 §4). 트리 조립은
 * `@/lib/playground/activityTree` 의 순수 함수가 담당해요.
 */

import { useState } from "react";
import type {
  ActivityKind,
  ActivityNode,
  ActivitySummary,
} from "@/lib/playground/activityTree";
import {
  LOG_OBSERVED_TOOLS_SOURCE_LABEL,
  SELF_REPORTED_TOOLS_SOURCE_LABEL,
  activityMetricValue,
  runtimeLogPollingPresentation,
} from "@/lib/playground/activityTree";
import { cn } from "@/lib/ui";

const DOT_CLASS: Record<ActivityKind, string> = {
  turn: "bg-blue-600",
  model: "bg-blue-500",
  memory: "bg-cyan-500",
  context: "bg-indigo-500",
  tool: "bg-violet-500",
  authz: "bg-emerald-500",
  error: "bg-red-500",
  warning: "bg-amber-500",
  app: "bg-slate-400",
  log: "bg-slate-300",
};

const TAG_CLASS: Record<ActivityKind, string> = {
  turn: "bg-blue-50 text-blue-700",
  model: "bg-blue-50 text-blue-700",
  memory: "bg-cyan-50 text-cyan-700",
  context: "bg-indigo-50 text-indigo-700",
  tool: "bg-violet-50 text-violet-700",
  authz: "bg-emerald-50 text-emerald-700",
  error: "bg-red-50 text-red-700",
  warning: "bg-amber-50 text-amber-700",
  app: "bg-slate-100 text-slate-600",
  log: "bg-slate-100 text-slate-500",
};

const LEGEND: { kind: ActivityKind; label: string }[] = [
  { kind: "model", label: "model" },
  { kind: "memory", label: "memory" },
  { kind: "context", label: "context" },
  { kind: "tool", label: "tool" },
  { kind: "authz", label: "authz" },
  { kind: "error", label: "error" },
];

function Caret({ open }: { open: boolean }) {
  return (
    <svg
      viewBox="0 0 24 24"
      aria-hidden="true"
      className={cn(
        "h-3 w-3 shrink-0 text-slate-400 transition-transform",
        open && "rotate-90",
      )}
      fill="none"
      stroke="currentColor"
      strokeWidth="2.5"
    >
      <path d="M9 6l6 6-6 6" />
    </svg>
  );
}

function TreeNode({ node, depth }: { node: ActivityNode; depth: number }) {
  // 턴은 기본 펼침, 하위는 기본 접힘 — 시안(그리고 Chainlit/assistant-ui)의 관례예요.
  const [open, setOpen] = useState(depth === 0);
  const [detailOpen, setDetailOpen] = useState(false);
  const hasChildren = node.children.length > 0;
  const hasDetail = (node.detail?.length ?? 0) > 0 || Boolean(node.note);

  return (
    <li className={cn("relative", !node.observed && "opacity-60")}>
      <div className="flex items-center gap-1.5">
        <button
          type="button"
          onClick={() => (hasChildren ? setOpen((v) => !v) : setDetailOpen((v) => !v))}
          aria-expanded={hasChildren ? open : detailOpen}
          className="flex min-w-0 flex-1 items-center gap-2 rounded px-1.5 py-1 text-left hover:bg-accent/50"
        >
          {hasChildren ? <Caret open={open} /> : <span className="w-3 shrink-0" />}
          <span
            aria-hidden="true"
            className={cn("h-2 w-2 shrink-0 rounded-full", DOT_CLASS[node.kind])}
          />
          <span className="min-w-0 truncate text-xs font-medium text-foreground">
            {node.label}
          </span>
          <span
            className={cn(
              "shrink-0 rounded px-1 py-px font-mono text-[9px] font-bold tracking-wide",
              TAG_CLASS[node.kind],
            )}
          >
            {node.tag}
          </span>
          <span className="flex-1" />
          {node.meta && (
            <span
              className={cn(
                "shrink-0 font-mono text-[10px]",
                node.observed ? "text-muted-foreground" : "text-slate-400",
              )}
            >
              {node.meta}
            </span>
          )}
          {node.status === "error" && (
            <span aria-label="실패" className="shrink-0 text-[10px] text-red-600">
              ✕
            </span>
          )}
        </button>
        {hasDetail && hasChildren && (
          <button
            type="button"
            onClick={() => setDetailOpen((v) => !v)}
            aria-expanded={detailOpen}
            className="shrink-0 rounded px-1.5 py-0.5 text-[10px] text-muted-foreground hover:bg-accent"
          >
            상세
          </button>
        )}
      </div>

      {detailOpen && hasDetail && (
        // 로그 원문이 길어도 트리를 밀어내지 않게 상세 카드 안에서 스크롤해요.
        <div className="ml-6 mb-1 max-h-48 overflow-y-auto rounded-lg border border-border bg-slate-50 p-2">
          {node.detail?.map((row, i) => (
            <div key={i} className="flex gap-2 py-px text-[11px]">
              <span className="w-20 shrink-0 text-muted-foreground">{row.key}</span>
              <span className="min-w-0 whitespace-pre-wrap break-all font-mono text-slate-700">
                {row.value}
              </span>
            </div>
          ))}
          {node.note && (
            <p className="mt-1.5 text-[11px] leading-relaxed text-slate-500">{node.note}</p>
          )}
        </div>
      )}

      {hasChildren && open && (
        <ul className="ml-3 space-y-px border-l border-border pl-3">
          {node.children.map((child) => (
            <TreeNode key={child.id} node={child} depth={depth + 1} />
          ))}
        </ul>
      )}
    </li>
  );
}

export function ActivityTree({
  nodes,
  summary,
  live,
  runtimeLogs,
}: {
  nodes: ActivityNode[];
  summary: ActivitySummary;
  live: boolean;
  runtimeLogs: {
    ready: boolean;
    status: string;
    error: string;
    permanentlyStopped: boolean;
    enabled: boolean;
  };
  // `loggingOutlet` prop 은 없어졌어요 — 유일한 소비자가 위에서 지운 「로그 출구 미관측」
  // 문구였어요. prop 을 남겨 두면 안 쓰이는 값이 계속 흘러 다녀요.
}) {
  const polling = runtimeLogPollingPresentation(runtimeLogs);
  const runtimeLogsObservable = (
    !runtimeLogs.error
    && (runtimeLogs.status === "ok" || runtimeLogs.status === "empty")
  );
  const logObservationMessage = runtimeLogs.permanentlyStopped
    ? `로그 관측 중단: ${runtimeLogs.error}`
    : runtimeLogs.error
      ? `로그 조회 실패 · 자동 재시도 중: ${runtimeLogs.error}`
      : !runtimeLogs.enabled
        ? "로그 관측이 일시정지됐어요."
        : runtimeLogs.status === "unavailable"
          ? "로그를 읽지 못했어요. 자동으로 다시 조회해요."
          : runtimeLogs.status === "not_deployed"
            ? "배포된 runtime이 없어 로그 관측을 시작할 수 없어요."
            : runtimeLogsObservable
              ? "런타임 로그 관측 가능"
              : "런타임 로그를 확인하는 중이에요.";
  return (
    // h-full 이 있어야 트리 목록이 카드에 남은 높이를 다 써요 — 없으면 내용 높이만
    // 차지해서 목록 영역이 그만큼 좁아져요.
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex items-center gap-2 border-b border-border px-4 py-2">
        <span className="text-sm font-semibold text-foreground">실행 활동 트리</span>
        <span
          className={cn(
            "flex items-center gap-1.5 text-[11px]",
            polling.active ? "text-blue-700" : "text-muted-foreground",
          )}
        >
          <span
            className={cn(
              "h-1.5 w-1.5 rounded-full",
              polling.active ? "animate-pulse bg-blue-600" : "bg-slate-300",
            )}
          />
          {live && polling.active ? "활동 관측 중" : polling.badge}
        </span>
      </div>

      {/* 관측된 셈만 보여줘요 — 시안의 토큰·비용 타일은 데이터가 없어 넣지 않았어요.
          ⚠️ 도구 호출 타일이 «두» 개예요. 출처가 다르기 때문에 한 숫자로 합치지 않아요 —
          도구 호출은 Agora 가 런타임 로그에서 관측한 값이에요(자기보고 축은 2026-09-06 에
          걷었어요 — 제품 오너 결정). */}
      <dl className="grid grid-cols-5 border-b border-border text-center">
        {[
          { label: "대화 턴", value: summary.turnCount, title: "" },
          {
            label: "model 호출",
            value: activityMetricValue(summary.modelInvokes, runtimeLogsObservable),
            title: "",
          },
          {
            // IH-184 (2026-09-06 밤, 제품 오너 결정): 이 타일은 **agent 자기보고**예요.
            //
            // 같은 날 오전 IH-182 는 반대로 로그 관측만 남겼어요. 뒤집은 근거는 실측이에요 —
            // 로그 관측은 ⑴ OTel exporter 가 1 MiB 초과 배치를 버리고 ⑵ 조회 창(`since_ms`)
            // 앞쪽을 못 봐서 **하한**이에요. 자기보고는 매 턴 응답에서 와서 세션 전체가 남아요.
            //
            // ⚠️ 그래도 **총계가 아니에요** — agent 의 주장이에요. 로그 관측은 트리의
            // `Tool #N:` 노드로 계속 보여주고 두 값을 합치지 않아요.
            // 「보고 없음」이 0 으로 그려지지 않게 하는 건 두 번째 인자예요.
            //
            // ⚠️ 라벨에서 출처 표기를 뺐어요 (제품 오너 결정, 2026-09-07 IH-187). 출처는
            // **호버 `title`** 에 그대로 있어요 — 화면에 항상 켜진 산문을 빼는 게 지시의
            // 취지였고, 값의 주인이 누구인지는 여전히 확인할 수 있어야 해요.
            label: "도구 호출",
            value: activityMetricValue(
              summary.selfReportedToolCalls,
              summary.selfReportedAvailable,
            ),
            title:
              `${SELF_REPORTED_TOOLS_SOURCE_LABEL} — agent 가 응답에 실어 보낸 값이에요. `
              + `Agora 가 관측한 값은 트리의 ${LOG_OBSERVED_TOOLS_SOURCE_LABEL} 노드예요.`,
          },
          {
            label: "memory",
            value: activityMetricValue(summary.memoryHits, runtimeLogsObservable),
            title: "",
          },
          {
            label: "오류",
            value: activityMetricValue(summary.errors, runtimeLogsObservable),
            title: "",
          },
        ].map((tile) => (
          <div
            key={tile.label}
            className="border-r border-border px-1.5 py-2 last:border-r-0"
            title={tile.title || undefined}
          >
            <dd className="font-mono text-sm font-semibold text-foreground">{tile.value}</dd>
            <dt className="text-[10px] leading-tight text-muted-foreground">{tile.label}</dt>
          </div>
        ))}
      </dl>

      {/* 범례만 둬요.
          ⚠️ 오른쪽에 있던 「토큰·비용·지연·span 계층 미표시 (OTEL span 미독)」 문구를 뺐어요
          (제품 오너 결정, 2026-09-06). 대화 턴 상세에서 그 세 칸을 지웠으니 **주장할 대상이
          없어졌어요** — 없는 칸에 대한 미표시 고지는 화면에 남을 이유가 없어요. 근거 사실
          (출처가 OTEL span 이고 Agora 가 아직 안 읽는다)은 `activityTree.ts` 의
          `buildTurnNode` 주석에 남겼어요. */}
      <div className="flex min-w-0 items-center gap-x-3 border-b border-border px-4 py-1 text-[10px] text-muted-foreground">
        <span className="flex shrink-0 flex-wrap gap-x-2.5 gap-y-1">
          {LEGEND.map((item) => (
            <span key={item.kind} className="flex items-center gap-1">
              <span className={cn("h-2 w-2 rounded-full", DOT_CLASS[item.kind])} />
              {item.label}
            </span>
          ))}
        </span>
      </div>
      <div
        className={cn(
          "border-b border-border px-4 py-1.5 text-[10px]",
          runtimeLogs.error || runtimeLogs.status === "unavailable"
            ? "text-amber-700"
            : "text-slate-500",
        )}
      >
        {logObservationMessage}
      </div>
      {/* ⚠️ 여기 있던 두 문구를 뺐어요 (제품 오너 결정, 2026-09-06).
          ① 「분류되지 않은 DEBUG·INFO·응답 출력은 트리에 넣지 않고…」 — 라이브 로그 포맷을
             읽도록 파서를 고치면서 분류되는 종류가 크게 늘어서 낡은 설명이 됐어요.
          ② 「로그 출구 미관측: …」 — 파서 수정으로 `agora`·`strands`·`bedrock_agentcore`
             줄이 실제로 분류되기 시작했으니 「출구가 있는지 관측할 수단이 없다」는 이제
             **거짓**이에요. 거짓 고지를 남기는 게 지우는 것보다 나빠요.
          여전히 참인 사실(로그가 아직 도착하지 않았을 수 있다)은 트리 안의 도구 호출 미관측
          노드가 계속 말해요 — 그 문구는 `activityTree.ts` 의 `toolPlaceholder` 에 있어요. */}

      <div className="min-h-0 flex-1 overflow-y-auto px-3 py-2">
        {nodes.length === 0 ? (
          <p className="py-10 text-center text-xs text-muted-foreground">
            메시지를 보내면 관측된 활동이 여기에 트리로 쌓여요.
            <br />
            <span className="text-slate-400">{polling.hint}</span>
          </p>
        ) : (
          <ul className="space-y-1">
            {nodes.map((node) => (
              <TreeNode key={node.id} node={node} depth={0} />
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}
