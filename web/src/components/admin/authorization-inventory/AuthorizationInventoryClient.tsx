"use client";

import { useState } from "react";
import useSWR from "swr";

import {
  approveAuthorizationInventoryGate,
  getAuthorizationInventoryGate,
  type AgentAuthorizationInventoryItem,
  type AuthorizationInventory,
  type AuthorizationInventoryGateView,
  type ObservationCount,
  type ObservationDistribution,
} from "@/lib/api";
import {
  approvalHistoryPresentation,
  approvalCurrencyLabel,
  cleanupCandidatePresentation,
  gatePresentation,
  inventoryDetailPresentation,
} from "@/lib/authorizationInventory";
import { cn } from "@/lib/ui";
import { Icon } from "@/components/ui/icon";

export function AuthorizationInventoryClient() {
  const { data, error, isLoading, isValidating, mutate } =
    useSWR<AuthorizationInventoryGateView>(
      "admin/identity/fail-closed-authorization-gate",
      getAuthorizationInventoryGate,
      { refreshInterval: 0, revalidateOnFocus: false },
    );
  const [confirmed, setConfirmed] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [approvalError, setApprovalError] = useState("");

  async function approve() {
    if (!data?.gate.can_approve || !confirmed || submitting) return;
    setSubmitting(true);
    setApprovalError("");
    try {
      const updated = await approveAuthorizationInventoryGate(
        data.gate.observation_id,
      );
      await mutate(updated, { revalidate: false });
      setConfirmed(false);
    } catch (approvalFailure) {
      setApprovalError(
        approvalFailure instanceof Error
          ? approvalFailure.message
          : "승인 기록에 실패했습니다.",
      );
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div>
      <header className="mb-5 flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold">인가 원장 인벤토리</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            Agent 신원, 도구 binding, Cedar 정책과 실 리소스 정합 상태
          </p>
        </div>
        <button
          type="button"
          title="인가 원장 새로고침"
          aria-label="인가 원장 새로고침"
          disabled={isValidating}
          onClick={() => {
            setConfirmed(false);
            setApprovalError("");
            void mutate();
          }}
          className="grid size-8 place-items-center rounded border border-border bg-background hover:bg-muted disabled:opacity-50"
        >
          <Icon
            name="refresh"
            size={16}
            className={isValidating ? "animate-spin" : ""}
          />
        </button>
      </header>

      {isLoading ? (
        <LoadingState />
      ) : error || !data ? (
        <div className="border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-800">
          인가 원장 인벤토리를 불러오지 못했습니다.
        </div>
      ) : (
        <div className="space-y-6">
          <GatePanel
            data={data}
            confirmed={confirmed}
            submitting={submitting}
            approvalError={approvalError}
            onConfirmed={setConfirmed}
            onApprove={() => void approve()}
          />
          <Summary inventory={data.inventory} />
          <InventoryDetails inventory={data.inventory} />
        </div>
      )}
    </div>
  );
}

function GatePanel({
  data,
  confirmed,
  submitting,
  approvalError,
  onConfirmed,
  onApprove,
}: {
  data: AuthorizationInventoryGateView;
  confirmed: boolean;
  submitting: boolean;
  approvalError: string;
  onConfirmed: (value: boolean) => void;
  onApprove: () => void;
}) {
  const presentation = gatePresentation(data.gate);
  const unreadableApprovals = data.approval_history.filter(
    (item) => item.state === "unknown",
  );
  const canApprove = data.gate.can_approve
    && data.approval_observation === "observed";
  return (
    <section
      aria-labelledby="fail-closed-gate-title"
      className={cn(
        "rounded-md border px-4 py-4",
        presentation.tone === "warning"
          ? "border-amber-300 bg-amber-50"
          : presentation.tone === "danger"
            ? "border-red-300 bg-red-50"
            : "border-slate-300 bg-slate-50",
      )}
    >
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <h2 id="fail-closed-gate-title" className="text-sm font-semibold">
              Fail-closed 전환 승인 게이트
            </h2>
            <span
              className={cn(
                "rounded px-2 py-0.5 text-xs font-semibold",
                presentation.tone === "warning"
                  ? "bg-amber-200 text-amber-950"
                  : presentation.tone === "danger"
                    ? "bg-red-200 text-red-950"
                    : "bg-slate-200 text-slate-800",
              )}
            >
              {presentation.label}
            </span>
            {data.approved && (
              <span className="rounded bg-blue-100 px-2 py-0.5 text-xs font-medium text-blue-800">
                현재 관측 승인 기록 있음
              </span>
            )}
          </div>
          <p className="mt-2 text-sm">{presentation.detail}</p>
          {data.gate.state === "unknown" && data.inventory.reason && (
            <p className="mt-1 break-words text-xs text-amber-900">
              {data.inventory.reason}
            </p>
          )}
          <code className="mt-2 block break-all text-[11px] text-slate-700">
            AGORA_RUNTIME_FAIL_CLOSED_ON_UNKNOWN_AUTHORIZATION
          </code>
          {data.gate.affected_agent_ids.length > 0 && (
            <p className="mt-2 break-all font-mono text-xs text-red-800">
              {data.gate.affected_agent_ids.join(", ")}
            </p>
          )}
        </div>

        <div className="w-full max-w-md border-l-0 border-slate-300 pl-0 sm:border-l sm:pl-4">
          <label className="flex items-start gap-2 text-xs">
            <input
              type="checkbox"
              checked={confirmed}
              disabled={!canApprove || submitting}
              onChange={(event) => onConfirmed(event.target.checked)}
              className="mt-0.5 size-4"
            />
            <span>
              이 관측 snapshot을 전환 승인 근거로 기록합니다.
              실제 설정 전환은 실행하지 않습니다.
            </span>
          </label>
          <button
            type="button"
            disabled={!canApprove || !confirmed || submitting}
            onClick={onApprove}
            className="mt-3 inline-flex h-9 items-center gap-2 rounded border border-slate-900 bg-slate-900 px-3 text-sm font-medium text-white hover:bg-slate-700 disabled:cursor-not-allowed disabled:border-slate-300 disabled:bg-slate-300"
          >
            <Icon name="check" size={15} />
            {submitting ? "기록 중" : "관측 결과 승인"}
          </button>
          {approvalError && (
            <p role="alert" className="mt-2 text-xs text-red-700">
              {approvalError}
            </p>
          )}
        </div>
      </div>

      {data.latest_approval && (
        <div className="mt-4 border-t border-slate-300 pt-3 text-xs text-slate-700">
          최근 승인: {data.latest_approval.approved_by} ·{" "}
          <time dateTime={data.latest_approval.approved_at}>
            {formatTimestamp(data.latest_approval.approved_at)}
          </time>
          {" · "}
          당시 영향 {data.latest_approval.evidence.affected_agent_count.toLocaleString()}건
          {" · "}
          {approvalCurrencyLabel(data.approved)}
        </div>
      )}
      {unreadableApprovals.map((item) => {
        const history = approvalHistoryPresentation(item);
        return (
          <div
            key={`${item.observation_id}:${item.approved_at}`}
            role="alert"
            className="mt-3 border-t border-amber-300 pt-3 text-xs text-amber-950"
          >
            <span className="font-semibold">{history.label}</span>
            {" · "}
            {history.detail}
            <code className="mt-1 block break-all text-[10px]">
              {item.observation_id}
            </code>
          </div>
        );
      })}
    </section>
  );
}

function Summary({ inventory }: { inventory: AuthorizationInventory }) {
  const summary = inventory.summary;
  const metrics: Array<{ label: string; value: ObservationCount }> = [
    { label: "IDENTITY", value: summary.identity_total },
    {
      label: "TOOL 0 · POLICY 0",
      value: summary.agents_without_tools_or_policies,
    },
    {
      label: "미존재 client 참조",
      value: summary.missing_client_reference_count,
    },
    { label: "고아 Cognito client", value: summary.orphan_client_count },
    { label: "카탈로그 고아", value: summary.catalog_orphan_count },
    {
      label: "전환 평가 대상",
      value: summary.fail_closed_evaluated_agent_count,
    },
    { label: "인가 미관측", value: summary.unknown_agent_count },
  ];
  return (
    <section aria-labelledby="inventory-summary-title">
      <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
        <h2 id="inventory-summary-title" className="text-sm font-semibold">
          집계
        </h2>
        <InventoryObservation status={inventory.status} reason={inventory.reason} />
      </div>
      <div className="grid grid-cols-2 border-l border-t border-border sm:grid-cols-4 xl:grid-cols-7">
        {metrics.map((metric) => (
          <div
            key={metric.label}
            className="min-h-24 border-b border-r border-border bg-background p-3"
          >
            <div className="text-xs text-muted-foreground">{metric.label}</div>
            <div
              className={cn(
                "mt-2 text-2xl font-semibold tabular-nums",
                metric.value === "unknown" && "text-amber-700",
              )}
            >
              {countLabel(metric.value)}
            </div>
          </div>
        ))}
      </div>
      <div className="grid border-x border-b border-border lg:grid-cols-2">
        <Distribution
          label="TOOL effective_state"
          value={summary.tool_effective_state_counts}
        />
        <Distribution
          label="POLICY status"
          value={summary.policy_status_counts}
          className="border-t border-border lg:border-l lg:border-t-0"
        />
      </div>
    </section>
  );
}

function InventoryObservation({
  status,
  reason,
}: {
  status: AuthorizationInventory["status"];
  reason: string;
}) {
  return (
    <span
      title={reason || undefined}
      className={cn(
        "rounded px-2 py-0.5 text-xs font-medium",
        status === "unknown"
          ? "bg-amber-100 text-amber-800"
          : "bg-blue-50 text-blue-800",
      )}
    >
      {status === "unknown" ? "관측 불완전" : "관측됨"}
    </span>
  );
}

function InventoryDetails({
  inventory,
}: {
  inventory: AuthorizationInventory;
}) {
  const unavailable = inventoryDetailPresentation(
    inventory.status,
    inventory.reason,
  );
  if (unavailable) {
    return (
      <section
        aria-labelledby="inventory-details-unavailable-title"
        className="border border-amber-300 bg-amber-50 px-4 py-4 text-amber-950"
      >
        <h2
          id="inventory-details-unavailable-title"
          className="text-sm font-semibold"
        >
          {unavailable.title}
        </h2>
        <p className="mt-1 break-words text-xs">{unavailable.detail}</p>
      </section>
    );
  }
  return (
    <>
      <AgentTable agents={inventory.agents} />
      <CleanupCandidates inventory={inventory} />
    </>
  );
}

function Distribution({
  label,
  value,
  className,
}: {
  label: string;
  value: ObservationDistribution;
  className?: string;
}) {
  return (
    <div className={cn("min-h-20 p-3", className)}>
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className="mt-2 flex flex-wrap gap-1.5">
        {value === "unknown" ? (
          <span className="rounded bg-amber-100 px-2 py-1 text-xs text-amber-800">
            미관측
          </span>
        ) : Object.keys(value).length === 0 ? (
          <span className="text-xs text-muted-foreground">항목 없음</span>
        ) : (
          Object.entries(value).map(([state, count]) => (
            <span
              key={state}
              className="rounded bg-slate-100 px-2 py-1 text-xs text-slate-700"
            >
              {state} {count.toLocaleString()}
            </span>
          ))
        )}
      </div>
    </div>
  );
}

function AgentTable({
  agents,
}: {
  agents: AgentAuthorizationInventoryItem[];
}) {
  return (
    <section aria-labelledby="agent-ledger-title">
      <div className="mb-2 flex items-center justify-between gap-2">
        <h2 id="agent-ledger-title" className="text-sm font-semibold">
          Agent별 3축
        </h2>
        <span className="text-xs text-muted-foreground">
          {agents.length.toLocaleString()}건
        </span>
      </div>
      <div className="overflow-x-auto border border-border">
        <table className="w-full min-w-[1120px] table-fixed text-left text-xs">
          <thead className="bg-muted/60 text-muted-foreground">
            <tr>
              <th className="w-44 px-3 py-2 font-medium">Agent</th>
              <th className="w-36 px-3 py-2 font-medium">IDENTITY</th>
              <th className="w-36 px-3 py-2 font-medium">TOOL</th>
              <th className="w-44 px-3 py-2 font-medium">POLICY</th>
              <th className="w-28 px-3 py-2 font-medium">Cognito</th>
              <th className="w-40 px-3 py-2 font-medium">Catalog</th>
              <th className="w-32 px-3 py-2 font-medium">CLIENTCLAIM</th>
              <th className="w-40 px-3 py-2 font-medium">대조 결과</th>
            </tr>
          </thead>
          <tbody>
            {agents.length === 0 ? (
              <tr>
                <td colSpan={8} className="px-3 py-8 text-center text-muted-foreground">
                  원장 Agent 없음
                </td>
              </tr>
            ) : (
              agents.map((agent) => <AgentRow key={agent.record_id} agent={agent} />)
            )}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function AgentRow({ agent }: { agent: AgentAuthorizationInventoryItem }) {
  return (
    <tr className="border-t border-border align-top">
      <td className="break-all px-3 py-3 font-mono">{agent.record_id}</td>
      <td className="px-3 py-3">
        <StatusText value={agent.identity_status} />
        <div className="mt-1 break-words text-[11px] text-muted-foreground">
          {agent.identity_type}
        </div>
        <div
          title={agent.client_id || undefined}
          className="mt-1 break-all font-mono text-[10px] text-muted-foreground"
        >
          {agent.client_id || "client 없음"}
        </div>
        {agent.policy_principal_id !== agent.client_id && (
          <div
            title={agent.policy_principal_id}
            className="mt-1 break-all font-mono text-[10px] text-red-700"
          >
            principal {agent.policy_principal_id || "없음"}
          </div>
        )}
      </td>
      <td className="px-3 py-3">
        <span className="font-semibold tabular-nums">{agent.tool_count}</span>
        <div className="mt-1 break-words text-[11px] text-muted-foreground">
          {distributionText(agent.tool_effective_state_counts)}
        </div>
      </td>
      <td className="px-3 py-3">
        <span className="font-semibold tabular-nums">{agent.policy_count}</span>
        <div className="mt-1 break-words text-[11px] text-muted-foreground">
          {distributionText(agent.policy_status_counts)}
        </div>
        <div className="mt-1 text-[11px] text-muted-foreground">
          {cedarStatusText(agent)}
        </div>
      </td>
      <td className="px-3 py-3">
        <BooleanObservation value={agent.client_exists} />
      </td>
      <td className="break-words px-3 py-3">{catalogLabel(agent.catalog_record)}</td>
      <td className="break-words px-3 py-3">{claimLabel(agent.client_claim)}</td>
      <td className="px-3 py-3">
        <Verdict verdict={agent.authorization_verdict} />
        {agent.reasons.length > 0 && (
          <div className="mt-1 break-words text-[11px] text-muted-foreground">
            {agent.reasons.join(" · ")}
          </div>
        )}
      </td>
    </tr>
  );
}

function CleanupCandidates({
  inventory,
}: {
  inventory: AuthorizationInventory;
}) {
  return (
    <section aria-labelledby="cleanup-candidates-title">
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <h2 id="cleanup-candidates-title" className="text-sm font-semibold">
          정리 후보
        </h2>
        <span className="rounded bg-slate-100 px-2 py-0.5 text-xs text-slate-700">
          보고 전용
        </span>
      </div>
      <div className="divide-y divide-border border border-border">
        <CandidateGroup
          title="Agent 인가 artifact"
          count={inventory.reclaim_candidates.length}
        >
          {inventory.reclaim_candidates.length === 0 ? (
            <EmptyRow />
          ) : (
            inventory.reclaim_candidates.map((candidate) => {
              const item = cleanupCandidatePresentation(candidate);
              return (
                <div
                  key={candidate.record_id}
                  className="grid gap-1 px-3 py-2 text-xs sm:grid-cols-[minmax(0,1fr)_minmax(0,1fr)_auto]"
                >
                  <span className="break-all font-mono">{item.title}</span>
                  <span className="break-all text-muted-foreground">{item.detail}</span>
                  <span className="text-slate-600">{item.reason}</span>
                </div>
              );
            })
          )}
        </CandidateGroup>
        <CandidateGroup
          title="고아 Cognito client"
          count={inventory.orphan_clients.length}
        >
          {inventory.orphan_clients.length === 0 ? (
            <EmptyRow />
          ) : (
            inventory.orphan_clients.map((client) => (
              <div
                key={client.client_id}
                className="grid gap-1 px-3 py-2 text-xs sm:grid-cols-2"
              >
                <span className="break-all font-mono">{client.client_id}</span>
                <span className="break-words text-muted-foreground">
                  {client.name || "이름 없음"}
                </span>
              </div>
            ))
          )}
        </CandidateGroup>
        <CandidateGroup
          title="원장 미소유 Cedar policy"
          count={inventory.unmanaged.length}
        >
          {inventory.unmanaged.length === 0 ? (
            <EmptyRow />
          ) : (
            inventory.unmanaged.map((policy) => (
              <div
                key={policy.policy_id}
                className="grid gap-1 px-3 py-2 text-xs sm:grid-cols-2"
              >
                <span className="break-all font-mono">{policy.policy_id}</span>
                <span className="break-words text-muted-foreground">
                  {policy.name || "이름 없음"} · {policy.remote_status}
                </span>
              </div>
            ))
          )}
        </CandidateGroup>
      </div>
    </section>
  );
}

function CandidateGroup({
  title,
  count,
  children,
}: {
  title: string;
  count: number;
  children: React.ReactNode;
}) {
  return (
    <div>
      <div className="flex items-center justify-between bg-muted/40 px-3 py-2 text-xs font-medium">
        <span>{title}</span>
        <span className="tabular-nums">{count.toLocaleString()}</span>
      </div>
      {children}
    </div>
  );
}

function EmptyRow() {
  return (
    <div className="px-3 py-4 text-center text-xs text-muted-foreground">
      후보 없음
    </div>
  );
}

function LoadingState() {
  return (
    <div className="space-y-4" aria-label="인가 원장 불러오는 중">
      <div className="h-32 animate-pulse rounded-md bg-muted" />
      <div className="grid grid-cols-4 gap-2">
        {Array.from({ length: 8 }, (_, index) => (
          <div key={index} className="h-20 animate-pulse bg-muted" />
        ))}
      </div>
      <div className="h-64 animate-pulse bg-muted" />
    </div>
  );
}

function countLabel(value: ObservationCount) {
  return value === "unknown" ? "미관측" : value.toLocaleString();
}

function distributionText(value: Record<string, number>) {
  const entries = Object.entries(value);
  return entries.length
    ? entries.map(([state, count]) => `${state} ${count}`).join(" · ")
    : "항목 없음";
}

function cedarStatusText(agent: AgentAuthorizationInventoryItem) {
  if (agent.cedar_observation === "unknown") return "Cedar 미관측";
  const states = [
    agent.cedar_in_sync.length
      ? `일치 ${agent.cedar_in_sync.length}`
      : "",
    agent.cedar_stale.length
      ? `stale ${agent.cedar_stale.length}`
      : "",
    agent.cedar_drift.length
      ? `drift ${agent.cedar_drift.length}`
      : "",
    agent.cedar_missing.length
      ? `missing ${agent.cedar_missing.length}`
      : "",
  ].filter(Boolean);
  return states.length ? `Cedar ${states.join(" · ")}` : "Cedar policy 없음";
}

function formatTimestamp(value: string) {
  const date = new Date(value);
  return Number.isNaN(date.valueOf())
    ? value
    : date.toLocaleString("ko-KR", { hour12: false });
}

function StatusText({ value }: { value: string }) {
  return <span className="font-medium">{value || "미관측"}</span>;
}

function BooleanObservation({
  value,
}: {
  value: boolean | "unknown";
}) {
  if (value === "unknown") {
    return <span className="text-amber-700">미관측</span>;
  }
  return (
    <span className={value ? "text-blue-700" : "text-red-700"}>
      {value ? "실재" : "미존재"}
    </span>
  );
}

function catalogLabel(value: string) {
  if (value === "unknown") return "미관측";
  if (value === "absent") return "record 없음";
  return value.replace("present(", "").replace(/\)$/, "");
}

function claimLabel(value: string) {
  if (value === "present") return "정합";
  if (value === "absent") return "없음";
  return value;
}

function Verdict({
  verdict,
}: {
  verdict: AgentAuthorizationInventoryItem["authorization_verdict"];
}) {
  const presentation = {
    coherent: ["정합", "bg-blue-50 text-blue-800"],
    diverged: ["불일치", "bg-red-100 text-red-800"],
    not_applicable: ["대상 없음", "bg-slate-100 text-slate-700"],
    unknown: ["미관측", "bg-amber-100 text-amber-800"],
  } as const;
  const [label, className] = presentation[verdict];
  return (
    <span className={cn("rounded px-2 py-0.5 text-xs font-medium", className)}>
      {label}
    </span>
  );
}
