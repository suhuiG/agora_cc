"use client";

import { useMemo, useState } from "react";
import useSWR from "swr";
import useSWRInfinite from "swr/infinite";

import { Button } from "@/components/ui/button";
import { Icon } from "@/components/ui/icon";
import { Input, Select } from "@/components/ui/input";
import {
  ALL_USERS_SWR_KEY,
  getCatalog,
  listAllCognitoUsers,
  listAuditTimeline,
  type AuditDecision,
  type AuditTimelineFilters,
  type AuditTimelinePage,
} from "@/lib/api";
import {
  AUDIT_EXPORT_MAX_PAGES,
  AUDIT_EXPORT_MAX_ROWS,
  AUDIT_TOOL_ARGUMENTS_NOTE,
  AUDIT_TOOL_SOURCE_FOOTNOTE,
  AuditCoverageUnknownError,
  agentDisplay,
  agentKind,
  auditEventTypeDisplay,
  auditEventsCsv,
  auditFindingLines,
  auditOperationDisplay,
  auditToolUsageCell,
  auditToolUsageIndex,
  collectAuditExport,
  coverageReasonMessage,
  defaultAuditRange,
  formatKst,
  invocationOutcomeDisplay,
  kstInputToUtc,
  principalDisplay,
  timelineListState,
  type AuditToolUsage,
} from "@/lib/auditCalls";
import { cn } from "@/lib/ui";

const PAGE_SIZE = 50;

type FilterDraft = {
  from: string;
  to: string;
  decision: "" | AuditDecision;
  agentId: string;
  principalId: string;
};

type InitialFilterState = {
  draft: FilterDraft;
  applied: AuditTimelineFilters;
};

function initialFilterState(): InitialFilterState {
  const range = defaultAuditRange();
  return {
    draft: {
      ...range,
      decision: "",
      agentId: "",
      principalId: "",
    },
    applied: {
      from: kstInputToUtc(range.from),
      to: kstInputToUtc(range.to),
    },
  };
}

export function AuditCallsClient() {
  const [initial] = useState(initialFilterState);
  const [draft, setDraft] = useState(initial.draft);
  const [filters, setFilters] = useState(initial.applied);
  const [filterError, setFilterError] = useState("");
  const [exportState, setExportState] = useState({
    running: false,
    rows: 0,
    pages: 0,
    message: "",
  });

  const {
    data: pages,
    error,
    isLoading,
    isValidating,
    size,
    setSize,
  } = useSWRInfinite<AuditTimelinePage>(
    (pageIndex, previousPage) => {
      if (previousPage && !previousPage.next_cursor) return null;
      return [
        "admin/access/audit",
        filters.from,
        filters.to,
        filters.decision ?? "",
        filters.agent_id ?? "",
        filters.principal_id ?? "",
        pageIndex === 0 ? "" : previousPage?.next_cursor ?? "",
      ] as const;
    },
    (key) =>
      listAuditTimeline({
        from: key[1] as string,
        to: key[2] as string,
        decision: key[3] ? (key[3] as AuditDecision) : undefined,
        agent_id: (key[4] as string) || undefined,
        principal_id: (key[5] as string) || undefined,
        cursor: (key[6] as string) || undefined,
        limit: PAGE_SIZE,
      }),
    { revalidateFirstPage: false },
  );

  const {
    data: users = [],
    error: usersError,
    isLoading: usersLoading,
  } = useSWR(
    ALL_USERS_SWR_KEY,
    listAllCognitoUsers,
  );
  const usersBySub = useMemo(
    () => new Map(users.map((user) => [user.sub, user])),
    [users],
  );
  const {
    data: agentCatalog,
    error: agentsError,
    isLoading: agentsLoading,
  } = useSWR("catalog/agents/all-for-audit-calls", () =>
    getCatalog("Agent", 0, 100),
  );
  const agentsById = useMemo(
    () =>
      new Map(
        (agentCatalog?.items ?? []).map((agent) => [
          agent.record_id,
          agent.name,
        ]),
      ),
    [agentCatalog],
  );
  const items = useMemo(
    () => pages?.flatMap((page) => page.items) ?? [],
    [pages],
  );
  // 도구 요약은 `event_id` 로 붙는 사이드카예요 (`AGENT_INVOKE` 행에만 있어요).
  const { byEventId: toolUsageByEvent, serverReported: toolUsageReported } =
    useMemo(() => auditToolUsageIndex(pages ?? []), [pages]);
  const unknownCoverage = pages?.find(
    (page) => page.coverage.status === "unknown",
  )?.coverage;
  const backfilledFrom = pages
    ?.map((page) => page.coverage.backfilled_from)
    .find((value): value is string => Boolean(value));
  const nextCursor = pages?.at(-1)?.next_cursor ?? null;
  const viewPage: AuditTimelinePage = {
    items,
    next_cursor: nextCursor,
    coverage: unknownCoverage ?? {
      status: "ok",
      reason: "",
      backfilled_from: backfilledFrom ?? null,
      backfill_status: "completed",
    },
  };
  const listState = timelineListState(viewPage);

  function applyFilters(event: React.FormEvent) {
    event.preventDefault();
    try {
      const from = kstInputToUtc(draft.from);
      const to = kstInputToUtc(draft.to);
      if (new Date(from) >= new Date(to)) {
        throw new Error("시작 시각은 종료 시각보다 빨라야 해요.");
      }
      setFilterError("");
      setExportState({ running: false, rows: 0, pages: 0, message: "" });
      setFilters({
        from,
        to,
        decision: draft.decision || undefined,
        agent_id: draft.agentId.trim() || undefined,
        principal_id: draft.principalId.trim() || undefined,
      });
      void setSize(1);
    } catch (caught) {
      setFilterError(
        caught instanceof Error ? caught.message : "기간을 확인해 주세요.",
      );
    }
  }

  async function exportCsv() {
    setExportState({ running: true, rows: 0, pages: 0, message: "" });
    try {
      const result = await collectAuditExport(filters, listAuditTimeline, {
        maxRows: AUDIT_EXPORT_MAX_ROWS,
        onProgress: (rows, pages) =>
          setExportState({ running: true, rows, pages, message: "" }),
      });
      const csv = auditEventsCsv(
        result.items,
        usersBySub,
        agentsById,
        result.toolUsage,
        result.toolUsageReported,
      );
      const blob = new Blob([`\uFEFF${csv}`], {
        type: "text/csv;charset=utf-8",
      });
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = `audit-calls-${new Date().toISOString().slice(0, 10)}${
        result.truncated ? "-truncated" : ""
      }.csv`;
      link.click();
      URL.revokeObjectURL(url);
      setExportState({
        running: false,
        rows: result.items.length,
        pages: result.pages,
        message: result.truncated
          ? `CSV가 ${result.pages.toLocaleString()}페이지 · ${result.items.length.toLocaleString()}건에서 잘렸어요.`
          : `CSV ${result.items.length.toLocaleString()}건을 내보냈어요.`,
      });
    } catch (caught) {
      const message =
        caught instanceof AuditCoverageUnknownError
          ? "관측 범위가 확인되지 않아 CSV를 내보내지 않았어요."
          : "CSV 내보내기에 실패했어요.";
      setExportState({ running: false, rows: 0, pages: 0, message });
    }
  }

  return (
    <div className="min-w-0">
      <header className="mb-5 flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold">호출 감사 로그</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            Agent 호출과 정책 배포 감사 이력을 시간순으로 조회해요.
          </p>
        </div>
        <div className="flex flex-col items-end gap-1">
          <Button
            type="button"
            variant="outline"
            size="sm"
            disabled={
              exportState.running ||
              isLoading ||
              usersLoading ||
              agentsLoading
            }
            onClick={() => void exportCsv()}
          >
            <Icon name="download" size={15} />
            {exportState.running
              ? `CSV 준비 ${exportState.pages.toLocaleString()}페이지 · ${exportState.rows.toLocaleString()}건`
              : "CSV 내보내기"}
          </Button>
          {exportState.message && (
            <p
              className={cn(
                "max-w-sm text-right text-xs",
                exportState.message.includes("실패") ||
                  exportState.message.includes("않았")
                  ? "text-red-700"
                  : exportState.message.includes("잘렸")
                    ? "text-amber-700"
                    : "text-muted-foreground",
              )}
              role="status"
            >
              {exportState.message}
            </p>
          )}
          {!exportState.message && !exportState.running && (
            <p className="text-xs text-muted-foreground">
              최대 {AUDIT_EXPORT_MAX_PAGES.toLocaleString()}페이지 ·{" "}
              {AUDIT_EXPORT_MAX_ROWS.toLocaleString()}건
            </p>
          )}
        </div>
      </header>

      <form
        onSubmit={applyFilters}
        className="mb-4 grid gap-3 border-y border-border py-4 md:grid-cols-2 xl:grid-cols-[minmax(180px,1fr)_minmax(180px,1fr)_140px_minmax(160px,1fr)_minmax(180px,1fr)_auto]"
      >
        <FilterField label="시작 (KST)" htmlFor="audit-from">
          <Input
            id="audit-from"
            type="datetime-local"
            step={1}
            required
            value={draft.from}
            onChange={(event) =>
              setDraft((current) => ({ ...current, from: event.target.value }))
            }
          />
        </FilterField>
        <FilterField label="종료 (KST)" htmlFor="audit-to">
          <Input
            id="audit-to"
            type="datetime-local"
            step={1}
            required
            value={draft.to}
            onChange={(event) =>
              setDraft((current) => ({ ...current, to: event.target.value }))
            }
          />
        </FilterField>
        <FilterField label="판정" htmlFor="audit-decision">
          <Select
            id="audit-decision"
            value={draft.decision}
            onChange={(event) =>
              setDraft((current) => ({
                ...current,
                decision: event.target.value as FilterDraft["decision"],
              }))
            }
            className="w-full"
          >
            <option value="">전체</option>
            <option value="ALLOW">ALLOW</option>
            <option value="DENY">DENY</option>
          </Select>
        </FilterField>
        <FilterField label="Agent" htmlFor="audit-agent">
          <Input
            id="audit-agent"
            placeholder="Agent ID"
            value={draft.agentId}
            onChange={(event) =>
              setDraft((current) => ({
                ...current,
                agentId: event.target.value,
              }))
            }
          />
        </FilterField>
        <FilterField label="호출 주체" htmlFor="audit-principal">
          <Input
            id="audit-principal"
            placeholder="principal sub"
            value={draft.principalId}
            onChange={(event) =>
              setDraft((current) => ({
                ...current,
                principalId: event.target.value,
              }))
            }
          />
        </FilterField>
        <div className="flex items-end">
          <Button type="submit" className="w-full xl:w-auto">
            <Icon name="search" size={15} />
            조회
          </Button>
        </div>
        {filterError && (
          <p className="text-sm text-red-700 md:col-span-2 xl:col-span-6">
            {filterError}
          </p>
        )}
      </form>

      {unknownCoverage && (
        <CoverageBanner reason={unknownCoverage.reason} />
      )}
      {backfilledFrom && (
        <div className="mb-4 border-l-4 border-amber-400 bg-amber-50 px-4 py-3 text-sm text-amber-900">
          {formatKst(backfilledFrom)} (KST) 이전 감사 이벤트는 미백필 상태예요.
        </div>
      )}
      {usersError && (
        <div className="mb-4 border-l-4 border-slate-300 bg-slate-50 px-4 py-3 text-sm text-slate-700">
          사용자 정보를 결합하지 못해 principal sub와 &apos;이름 미확인&apos;으로
          표시해요.
        </div>
      )}
      {agentsError && (
        <div className="mb-4 border-l-4 border-slate-300 bg-slate-50 px-4 py-3 text-sm text-slate-700">
          Agent 정보를 결합하지 못해 Agent ID와 &apos;이름 미확인&apos;으로
          표시해요.
        </div>
      )}

      {isLoading ? (
        <LoadingRows />
      ) : error && !pages?.length ? (
        <Message tone="error">호출 감사 로그를 불러오지 못했어요.</Message>
      ) : listState === "unknown" && items.length === 0 ? (
        <Message tone="warning">
          감사 범위를 확인하는 중이에요. 인덱스 상태가 확인된 뒤 다시 조회해 주세요.
        </Message>
      ) : listState === "empty" ? (
        <Message>선택한 조건에 해당하는 감사 이벤트가 없어요.</Message>
      ) : listState === "continuation" ? (
        <Message tone="neutral">
          현재 페이지에는 필터와 일치하는 이벤트가 없어요. 다음 페이지가 남아 있어요.
        </Message>
      ) : (
        <AuditTable
          items={items}
          usersBySub={usersBySub}
          agentsById={agentsById}
          toolUsageByEvent={toolUsageByEvent}
          toolUsageReported={toolUsageReported}
        />
      )}

      {error && pages?.length ? (
        <p className="mt-3 text-sm text-red-700">
          다음 페이지를 불러오지 못했어요. 다시 시도해 주세요.
        </p>
      ) : null}

      <div className="mt-4 flex flex-wrap items-center justify-between gap-3">
        <p className="text-xs text-muted-foreground">
          {items.length.toLocaleString()}건 표시
        </p>
        {nextCursor && !unknownCoverage && (
          <Button
            type="button"
            variant="outline"
            size="sm"
            disabled={isValidating}
            onClick={() => void setSize(size + 1)}
          >
            {isValidating ? "불러오는 중" : "더 보기"}
          </Button>
        )}
      </div>
    </div>
  );
}

function AuditTable({
  items,
  usersBySub,
  agentsById,
  toolUsageByEvent,
  toolUsageReported,
}: {
  items: AuditTimelinePage["items"];
  usersBySub: Map<string, Awaited<ReturnType<typeof listAllCognitoUsers>>[number]>;
  agentsById: ReadonlyMap<string, string>;
  toolUsageByEvent: ReadonlyMap<string, AuditToolUsage>;
  toolUsageReported: boolean;
}) {
  return (
    <div className="overflow-x-auto rounded-lg border border-border">
      <p className="border-b border-border bg-muted/40 px-3 py-2 text-xs text-muted-foreground">
        {AUDIT_TOOL_SOURCE_FOOTNOTE} {AUDIT_TOOL_ARGUMENTS_NOTE}
      </p>
      <table className="w-full min-w-[1320px] text-left text-sm">
        <thead className="bg-muted/70 text-xs text-muted-foreground">
          <tr>
            <Th>일시 (KST)</Th>
            <Th>이벤트 종류</Th>
            <Th>호출 주체</Th>
            <Th>Agent</Th>
            <Th>동작</Th>
            <Th>도구 (agent 자기보고)</Th>
            <Th>결과</Th>
            <Th>사유</Th>
          </tr>
        </thead>
        <tbody className="divide-y divide-border">
          {items.map((item) => {
            const principal = principalDisplay(item.principal_id, usersBySub);
            const agent = agentDisplay(item.agent_id, agentsById);
            const kind = agentKind(item.workload_id);
            const operation = auditOperationDisplay(item.operation_id);
            const eventType = auditEventTypeDisplay(item.event_type);
            const outcome = invocationOutcomeDisplay(item);
            const toolCell = auditToolUsageCell(
              item,
              toolUsageByEvent.get(item.event_id),
              toolUsageReported,
            );
            const findings = auditFindingLines(item);
            return (
              <tr key={item.event_id} className="hover:bg-muted/40">
                <Td className="whitespace-nowrap text-xs text-muted-foreground">
                  {formatKst(item.timestamp)}
                </Td>
                <Td className="max-w-48">
                  <span
                    className={cn(
                      "inline-flex rounded px-1.5 py-0.5 text-[11px] font-medium",
                      eventType.kind === "invoke"
                        ? "bg-cyan-100 text-cyan-800"
                        : eventType.kind === "admin"
                          ? "bg-violet-100 text-violet-800"
                          : eventType.kind === "security"
                            ? "bg-red-100 text-red-800"
                            : eventType.kind === "decision"
                              ? "bg-slate-100 text-slate-700"
                              : "bg-amber-100 text-amber-900",
                    )}
                    title={item.event_type}
                  >
                    {eventType.label}
                  </span>
                </Td>
                <Td className="max-w-64">
                  <div className="truncate font-medium">{principal.primary}</div>
                  <div
                    className="truncate text-xs text-muted-foreground"
                    title={item.principal_id}
                  >
                    {principal.secondary}
                  </div>
                </Td>
                <Td className="max-w-64">
                  <div className="flex items-center gap-2">
                    <div className="min-w-0 flex-1">
                      <div className="truncate font-medium" title={agent.primary}>
                        {agent.primary}
                      </div>
                      <div
                        className="truncate font-mono text-xs text-muted-foreground"
                        title={item.agent_id}
                      >
                        {agent.secondary}
                      </div>
                    </div>
                    <span
                      className={cn(
                        "shrink-0 rounded px-1.5 py-0.5 text-[11px] font-medium",
                        kind === "외부"
                          ? "bg-amber-100 text-amber-800"
                          : kind === "관리형"
                            ? "bg-blue-100 text-blue-800"
                            : // workload_id 가 비어 있으면 추측하지 않아요.
                              "bg-slate-100 text-slate-600",
                      )}
                      title={
                        kind === "미기록"
                          ? "이 행에는 workload_id 가 기록되지 않아 종류를 판정할 수 없어요."
                          : item.workload_id
                      }
                    >
                      {kind}
                    </span>
                  </div>
                </Td>
                <Td className="max-w-80">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="font-medium">{operation.label}</span>
                    <span
                      className={cn(
                        "rounded px-1.5 py-0.5 text-[11px] font-medium",
                        operation.kind === "admin"
                          ? "bg-violet-100 text-violet-800"
                          : operation.kind === "agent"
                            ? "bg-cyan-100 text-cyan-800"
                            : "bg-slate-100 text-slate-700",
                      )}
                    >
                      {operation.kind === "admin"
                        ? "관리자 작업"
                        : operation.kind === "agent"
                          ? "Agent 작업"
                          : "기타 작업"}
                    </span>
                  </div>
                  {outcome && (
                    <div
                      className={cn(
                        "mt-1 text-xs",
                        outcome.tone === "success"
                          ? "text-emerald-700"
                          : outcome.tone === "failure"
                            ? "text-red-700"
                            : "text-amber-800",
                      )}
                    >
                      {outcome.label}
                    </div>
                  )}
                </Td>
                <Td className="max-w-72 text-xs">
                  {toolCell.kind === "tools" ? (
                    <ul className="space-y-1">
                      {toolCell.tools.map((tool) => (
                        <li key={tool.fullName} title={tool.fullName}>
                          <span className="font-mono font-medium">
                            {tool.toolName}
                          </span>
                          <span className="text-muted-foreground">
                            {" "}
                            ×{tool.callCount}
                          </span>
                          {tool.errorCount > 0 && (
                            <span className="text-red-700">
                              {" "}
                              (오류 {tool.errorCount})
                            </span>
                          )}
                          {tool.targetName && (
                            <div className="truncate text-[11px] text-muted-foreground">
                              {tool.targetName}
                            </div>
                          )}
                        </li>
                      ))}
                    </ul>
                  ) : (
                    <div
                      className={cn(
                        toolCell.kind === "unknown"
                          ? "text-amber-800"
                          : "text-muted-foreground",
                      )}
                    >
                      {toolCell.label}
                    </div>
                  )}
                  {toolCell.kind === "unknown" && toolCell.detail && (
                    <div className="mt-1 text-[11px] text-amber-800">
                      {toolCell.detail}
                    </div>
                  )}
                  {toolCell.kind === "unknown" && toolCell.tools.length > 0 && (
                    <ul className="mt-1 space-y-0.5 text-[11px] text-muted-foreground">
                      {toolCell.tools.map((tool) => (
                        <li key={tool.fullName} title={tool.fullName}>
                          <span className="font-mono">{tool.toolName}</span> ×
                          {tool.callCount}
                        </li>
                      ))}
                    </ul>
                  )}
                </Td>
                <Td>
                  <span
                    className={cn(
                      "inline-flex rounded px-2 py-0.5 text-xs font-semibold",
                      item.decision === "ALLOW"
                        ? "bg-emerald-100 text-emerald-800"
                        : "bg-red-100 text-red-800",
                    )}
                  >
                    {item.decision}
                  </span>
                </Td>
                <Td className="max-w-56 break-words text-xs">
                  <div>{item.reason || "-"}</div>
                  {findings.length > 0 && (
                    <ul className="mt-1 list-disc space-y-1 pl-4 text-amber-800">
                      {findings.map((finding, index) => (
                        <li key={`${index}:${finding}`}>{finding}</li>
                      ))}
                    </ul>
                  )}
                </Td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function CoverageBanner({
  reason,
}: {
  reason: AuditTimelinePage["coverage"]["reason"];
}) {
  const text = coverageReasonMessage(reason);
  return (
    <div
      role="alert"
      className="mb-4 border-l-4 border-amber-500 bg-amber-50 px-4 py-3 text-sm text-amber-950"
    >
      <div className="font-semibold">감사 데이터 범위 미확인</div>
      <div className="mt-1">
        {text} <span className="font-mono text-xs">({reason})</span>
      </div>
    </div>
  );
}

function FilterField({
  label,
  htmlFor,
  children,
}: {
  label: string;
  htmlFor: string;
  children: React.ReactNode;
}) {
  return (
    <label htmlFor={htmlFor} className="min-w-0">
      <span className="mb-1 block text-xs font-medium text-muted-foreground">
        {label}
      </span>
      {children}
    </label>
  );
}

function Th({ children }: { children: React.ReactNode }) {
  return <th className="px-3 py-2.5 font-medium">{children}</th>;
}

function Td({
  children,
  className,
}: {
  children: React.ReactNode;
  className?: string;
}) {
  return <td className={cn("px-3 py-3 align-top", className)}>{children}</td>;
}

function LoadingRows() {
  return (
    <div className="space-y-2" aria-label="호출 감사 로그 불러오는 중">
      {Array.from({ length: 6 }).map((_, index) => (
        <div
          key={index}
          className="h-14 animate-pulse rounded-md bg-muted/60"
        />
      ))}
    </div>
  );
}

function Message({
  children,
  tone = "neutral",
}: {
  children: React.ReactNode;
  tone?: "neutral" | "warning" | "error";
}) {
  return (
    <div
      className={cn(
        "border-y border-border px-4 py-12 text-center text-sm",
        tone === "error"
          ? "bg-red-50 text-red-800"
          : tone === "warning"
            ? "bg-amber-50 text-amber-900"
            : "text-muted-foreground",
      )}
    >
      {children}
    </div>
  );
}
