"use client";

/**
 * 실행 구성 패널 — "이 agent 가 지금 무슨 모델·도구·기억으로 도는지".
 *
 * 모든 값에 출처 배지를 붙여요. **선언**은 배포 원장에서 읽은 값, **실체**는 배포된
 * runtime selfcheck 또는 AWS control plane이 보고한 값이에요. 관측하지 못한 축은 값 대신
 * "실제 배선 정보 없음" 을 보여줘요(IH-92 가 conversation manager 로 쓰는 패턴, ADR-0037 §4).
 *
 * 모델 ID와 memory 전략은 원장 기대값과 독립 관측값을 분리해 대조해요.
 */

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Icon } from "@/components/ui/icon";
import { ConversationManagerStatus } from "./ConversationManagerStatus";
import {
  type AgentRunConfig,
  type ToolApproval,
  UNOBSERVED_LABEL,
  isObserved,
  limitView,
  memoryDeclarationView,
  memoryObservationView,
  modelDeclarationLabel,
  observedStatusLabel,
  reconciliationStatusLabel,
  sessionMemoryAttached,
  toolRowApprovalIsNoteworthy,
  toolRowApprovalLabel,
  toolRowStatusLabel,
  toolRows,
} from "@/lib/playground/runConfig";
import { cn } from "@/lib/ui";

const STATUS_CLASS: Record<string, string> = {
  registered: "bg-emerald-50 text-emerald-700",
  missing: "bg-red-50 text-red-700",
  undetermined: "bg-amber-50 text-amber-700",
  unobserved: "bg-slate-100 text-slate-500",
  coherent: "bg-emerald-50 text-emerald-700",
  diverged: "bg-red-50 text-red-700",
  unknown: "bg-slate-100 text-slate-500",
};

/**
 * ④ 승인 축의 색 (ADR-0104). 등록 축과 **다른 뱃지**예요.
 *
 * 같은 뱃지에 합치면 「등록 확인 + 승인 대기」 같은 정상 조합을 표현할 수 없어요 — 미승인
 * 도구는 목록에 보이면서 호출은 거부되는 게 정상이에요(«listed but denied»).
 */
const APPROVAL_CLASS: Record<ToolApproval, string> = {
  pending_approval: "bg-amber-50 text-amber-700",
  not_requested: "bg-red-50 text-red-700",
  unobserved: "bg-slate-100 text-slate-500",
  // 뱃지를 띄우지 않는 두 상태(`toolRowApprovalIsNoteworthy` 가 false)예요. 키를 빼면
  // `Record<ToolApproval, …>` 가 성립하지 않고, 느슨한 `Record<string, …>` 로 두면 새 상태를
  // 더할 때 색 없는 뱃지가 조용히 렌더돼요.
  approved: "",
  not_applicable: "",
};

const APPROVAL_TITLE: Record<ToolApproval, string> = {
  pending_approval:
    "관리자가 이 도구를 승인하면 바로 호출할 수 있어요. 지금 호출하면 "
    + "「이 도구는 관리자 승인 대기 중이에요」로 거부돼요.",
  // 「승인 대기가 아닌 미승인」은 세 가지가 겹쳐요 — 신청이 없거나, 반려됐거나, 회수됐어요.
  // 셋 다 관리자 승인 큐에 없어서 「승인만 누르면 되는」 상태가 아니라는 점이 같아요.
  // 문구가 「신청이 없어요」만 말하면 반려·회수된 도구를 잘못 설명해요.
  not_requested:
    "이 도구는 관리자 승인 큐에 없어요 — 권한 신청이 접수되지 않았거나, 반려·회수됐어요. "
    + "신청이 접수되어야 승인 큐에 올라가요.",
  unobserved: "④ 권한 원장을 읽지 못했어요 — 승인 상태를 승인됨으로 읽지 마세요.",
  approved: "",
  not_applicable: "",
};

function SourceBadge({ source }: { source: "declared" | "observed" }) {
  return (
    <Badge
      variant="type"
      className={
        source === "declared"
          ? "bg-blue-50 text-blue-700"
          : "bg-violet-50 text-violet-700"
      }
      title={
        source === "declared"
          ? "배포 원장에 적힌 선언값이에요."
          : "배포 runtime 또는 AWS control plane에서 관측한 실체예요."
      }
    >
      {source === "declared" ? "선언" : "실체"}
    </Badge>
  );
}

function Row({
  label,
  source,
  children,
}: {
  label: string;
  source: "declared" | "observed";
  children: React.ReactNode;
}) {
  return (
    <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1 py-1.5">
      <span className="flex items-center gap-1.5 text-xs text-muted-foreground">
        <SourceBadge source={source} />
        {label}
      </span>
      <span className="min-w-0 text-sm text-foreground">{children}</span>
    </div>
  );
}

function Unobserved({ reason }: { reason?: string }) {
  return (
    <span className="text-xs text-slate-400" title={reason || undefined}>
      {UNOBSERVED_LABEL}
    </span>
  );
}

export function RunConfigPanel({
  config,
  loading,
  error,
  probing,
  observedAt,
  cacheMs,
  retryAfter,
  retryCooldownMs,
  onProbe,
}: {
  config: AgentRunConfig | null;
  loading: boolean;
  error: string;
  probing: boolean;
  observedAt: number | null;
  cacheMs: number;
  retryAfter: number | null;
  retryCooldownMs: number;
  onProbe: () => void;
}) {
  if (probing && !config) {
    return <p className="px-4 py-3 text-sm text-muted-foreground">실체를 관측 중이에요...</p>;
  }
  if (loading && !config) {
    return (
      <p className="px-4 py-3 text-sm text-muted-foreground">실행 구성을 불러오는 중이에요…</p>
    );
  }
  if (error && !config) {
    return (
      <div className="flex items-start gap-2 px-4 py-3">
        <p className="min-w-0 flex-1 text-sm text-amber-700">
          미관측: {error}
        </p>
        <Button
          variant="ghost"
          size="sm"
          onClick={onProbe}
          title="실체 관측 다시 시도"
          aria-label="실체 관측 다시 시도"
          className="h-7 w-7 shrink-0 p-0"
        >
          <Icon name="refresh" size={14} />
        </Button>
      </div>
    );
  }
  if (!config) {
    return (
      <p className="px-4 py-3 text-sm text-muted-foreground">
        agent 를 선택하면 사용 중인 모델·도구·기억이 여기 나와요.
      </p>
    );
  }

  const observed = config.observed;
  const model = modelDeclarationLabel(config.declared.model);
  const memory = memoryDeclarationView(config.declared.memory);
  const observedMemory = memoryObservationView(observed);
  const attached = sessionMemoryAttached(observed);
  const rows = toolRows(config);
  const limits = limitView(observed);
  const manager = config.declared.conversation_manager;
  // 라이브 selfcheck 관측이 있으면 그걸 쓰고, 없으면 배포 검증 때 기록된 관측으로 내려가요.
  const liveManager = isObserved(observed) ? observed.conversation_manager : null;

  return (
    <div className="space-y-3 px-4 py-3">
      <div className="flex flex-wrap items-center gap-2">
        <span
          className={cn(
            "inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-xs font-medium",
            isObserved(observed)
              ? "bg-violet-50 text-violet-700"
              : "bg-slate-100 text-slate-500",
          )}
          title={observed.reason || undefined}
        >
          <span
            className={cn(
              "h-1.5 w-1.5 rounded-full",
              isObserved(observed) ? "bg-violet-500" : "bg-slate-400",
            )}
          />
          {probing ? "실체 관측 중..." : observedStatusLabel(observed)}
        </span>
        <Button
          variant="ghost"
          size="sm"
          onClick={onProbe}
          disabled={probing}
          className="ml-auto h-7 w-7 shrink-0 p-0"
          title="배포된 runtime 실체를 다시 관측"
          aria-label="배포된 runtime 실체를 다시 관측"
        >
          <Icon name="refresh" size={14} className={probing ? "animate-spin" : ""} />
        </Button>
      </div>
      <p className="text-[11px] text-muted-foreground">
        성공 관측은 같은 agent에서 {Math.round(cacheMs / 1000)}초간 재사용해 probe 호출을 줄여요.
        {observedAt && (
          <> 마지막 관측: {new Date(observedAt).toLocaleTimeString("ko-KR")}</>
        )}
        {" "}실패·미지원 뒤 자동 재시도는 {Math.round(retryCooldownMs / 1000)}초간 쉬고,
        수동 새로고침은 즉시 시도해요.
      </p>
      {retryAfter && (
        <p className="text-xs text-amber-700">
          자동 관측 재시도 대기 중: {new Date(retryAfter).toLocaleTimeString("ko-KR")} 이후
        </p>
      )}
      {error && (
        <p className="text-xs text-amber-700">
          실체 미관측: {error}
        </p>
      )}

      {/* ① 모델 — 원장 기대값과 live Agent 관측값을 분리해 표시해요. */}
      <section>
        <Row label="모델 기대값" source="declared">
          {model ? (
            <code className="rounded bg-muted px-1.5 py-0.5 font-mono text-xs">{model}</code>
          ) : (
            <Unobserved reason="원장에 현재 계약과 맞는 모델 선언이 없어요." />
          )}
        </Row>
        <Row label="모델 관측값" source="observed">
          {config.model_reconciliation.observed_model_id ? (
            <span className="inline-flex flex-wrap items-center gap-1.5">
              <code className="rounded bg-muted px-1.5 py-0.5 font-mono text-xs">
                {config.model_reconciliation.observed_model_id}
              </code>
              <span className={cn(
                "rounded px-1.5 py-0.5 text-[11px]",
                STATUS_CLASS[config.model_reconciliation.verdict],
              )}>
                {reconciliationStatusLabel(config.model_reconciliation.verdict)}
              </span>
            </span>
          ) : (
            <Unobserved reason={observed.reason} />
          )}
        </Row>
      </section>

      {/* ② 도구 — 선언(원장) 기대값 vs 실체(runtime 등록) 대조. */}
      <section className="border-t border-border pt-2">
        <div className="flex items-center gap-2 pb-1">
          <span className="text-xs font-medium text-foreground">도구</span>
          <span className="text-[11px] text-muted-foreground">
            MCP operation · 내장 도구 ({rows.length})
          </span>
        </div>
        {rows.length === 0 ? (
          <p className="text-xs text-muted-foreground">선언된 도구가 없어요.</p>
        ) : (
          <ul className="space-y-1">
            {rows.map((row) => (
              <li key={`${row.kind}:${row.label}`} className="flex flex-wrap items-center gap-2">
                <Badge variant="type" className="bg-slate-100 text-slate-600">
                  {row.kind === "builtin" ? "내장" : "MCP"}
                </Badge>
                <code className="font-mono text-xs text-foreground">{row.label}</code>
                <span
                  className={cn(
                    "rounded px-1.5 py-0.5 text-[11px]",
                    STATUS_CLASS[row.status],
                  )}
                  title={
                    row.expectedToolNames.length > 0
                      ? `기대 등록 이름: ${row.expectedToolNames.join(", ")}`
                      : "operation 선언이 없어 기대 이름을 도출할 수 없어요."
                  }
                >
                  {toolRowStatusLabel(row.status)}
                </span>
                {toolRowApprovalIsNoteworthy(row.approval) && (
                  <span
                    className={cn(
                      "rounded px-1.5 py-0.5 text-[11px]",
                      APPROVAL_CLASS[row.approval],
                    )}
                    title={APPROVAL_TITLE[row.approval]}
                  >
                    {toolRowApprovalLabel(row.approval)}
                  </span>
                )}
              </li>
            ))}
          </ul>
        )}
        {/* runtime 이 실제로 등록한 이름 — 선언에 operation 이 없어 대조가 안 되는 자산도
            무엇이 올라갔는지는 볼 수 있어야 해요. */}
        {isObserved(observed) && (observed.tools?.length ?? 0) > 0 && (
          <div className="mt-1.5 flex flex-wrap items-baseline gap-x-2 gap-y-1">
            <span className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
              <SourceBadge source="observed" />
              runtime 등록 도구 ({observed.tools!.length})
            </span>
            <span className="min-w-0 break-all font-mono text-[11px] text-slate-600">
              {observed.tools!.join(", ")}
            </span>
          </div>
        )}
        {/* "예상 밖 도구 없음" 과 "판정 불가" 를 구분해 보여줘요 — 합치면 실제로 올라간
            미선언 도구가 조용히 묻혀요. */}
        {config.unexpected_tools.status === "judged"
          && config.unexpected_tools.names.length > 0 && (
          <p className="mt-1.5 rounded bg-amber-50 p-2 text-[11px] leading-relaxed text-amber-700">
            선언에 없는데 runtime 에 등록된 도구예요:{" "}
            <code className="font-mono">{config.unexpected_tools.names.join(", ")}</code>
          </p>
        )}
        {config.unexpected_tools.status === "undecidable" && (
          <p className="mt-1.5 rounded bg-slate-100 p-2 text-[11px] leading-relaxed text-slate-500">
            선언에 없는 도구가 올라갔는지는 <b>판정할 수 없어요</b> — operation 선언이 없는
            자산이 있어서 기대 이름을 도출할 수 없어요. 위 &quot;runtime 등록 도구&quot;
            목록과 직접 비교해 주세요.
          </p>
        )}
      </section>

      {/* ③ 기억 — 원장 전략과 AWS Memory 리소스 구성을 대조해요. */}
      <section className="border-t border-border pt-2">
        <Row label="기억 사용" source="declared">
          {memory.modeLabel}
        </Row>
        <Row label="장기 메모리 전략" source="declared">
          {memory.strategies.length === 0 ? (
            <span className="text-xs text-muted-foreground">선언된 전략이 없어요</span>
          ) : (
            <span className="flex flex-wrap items-center gap-1.5">
              {memory.strategies.map((strategy) => (
                <Badge
                  key={strategy.name}
                  variant="type"
                  className="bg-cyan-50 font-mono text-cyan-700"
                  title={
                    strategy.namespaces.length > 0
                      ? `namespace: ${strategy.namespaces.join(", ")}`
                      : "namespace 선언이 없어요."
                  }
                >
                  {strategy.name}
                </Badge>
              ))}
              {memory.retentionLabel && (
                <span className="text-xs text-muted-foreground">{memory.retentionLabel}</span>
              )}
            </span>
          )}
        </Row>
        <Row label="AWS Memory 전략 관측값" source="observed">
          {observedMemory?.strategies === null || observedMemory === null ? (
            <Unobserved
              reason={observedMemory?.strategyReason || observed.reason}
            />
          ) : (
            <span className="flex flex-wrap items-center gap-1.5">
              {observedMemory.strategies.length === 0 ? (
                <span className="text-xs text-muted-foreground">
                  관측된 전략이 없어요
                </span>
              ) : observedMemory.strategies.map((strategy) => (
                <Badge
                  key={strategy}
                  variant="type"
                  className="bg-violet-50 font-mono text-violet-700"
                >
                  {strategy}
                </Badge>
              ))}
              <span className={cn(
                "rounded px-1.5 py-0.5 text-[11px]",
                STATUS_CLASS[config.memory_reconciliation.verdict],
              )}>
                {reconciliationStatusLabel(config.memory_reconciliation.verdict)}
              </span>
            </span>
          )}
        </Row>
        <Row label="runtime retrieval namespace" source="observed">
          {observedMemory?.runtimeRetrievalNamespaces === null
            || observedMemory === null ? (
              <Unobserved reason={observed.reason} />
            ) : observedMemory.runtimeRetrievalNamespaces.length === 0 ? (
              <span className="text-xs text-muted-foreground">
                관측된 retrieval namespace가 없어요
              </span>
            ) : (
              <span className="min-w-0 break-all font-mono text-[11px] text-slate-600">
                {observedMemory.runtimeRetrievalNamespaces.join(", ")}
              </span>
            )}
        </Row>
        <Row label="세션 기억 부착" source="observed">
          {attached === null ? (
            <Unobserved reason={observed.reason} />
          ) : attached ? (
            <span className="text-sm text-emerald-700">붙어 있어요</span>
          ) : (
            <span className="text-sm text-red-700">붙어 있지 않아요</span>
          )}
        </Row>
      </section>

      {/* context 전략(IH-92)과 실행 상한(selfcheck 관측). */}
      <section className="border-t border-border pt-2">
        <Row
          label={
            liveManager
              ? "context 전략"
              : "context 전략 (배포 검증 시점 관측)"
          }
          source="observed"
        >
          <ConversationManagerStatus
            manager={liveManager ?? manager?.actual ?? null}
            compact
          />
        </Row>
        <Row label="실행 상한" source="observed">
          {limits.rows.length === 0 ? (
            <Unobserved reason={observed.reason} />
          ) : (
            <span
              className="font-mono text-xs text-foreground"
              title={limits.notes.join("\n") || undefined}
            >
              {limits.rows.map((limit) => `${limit.key}=${limit.value}`).join(" · ")}
            </span>
          )}
        </Row>
      </section>
    </div>
  );
}
