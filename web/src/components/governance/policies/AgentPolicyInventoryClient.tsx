"use client";

import { Fragment, useMemo } from "react";
import useSWR from "swr";

import { Icon } from "@/components/ui/icon";
import {
  buildAgentPolicyInventoryView,
  callVerificationQualified,
  describeNegativeControlKind,
  getAgentPolicyInventory,
  negativeCallVerificationQualified,
  positiveCallVerificationQualified,
  type AgentPolicyInventoryReport,
  type AgentPolicyInventoryTone,
  type GatewayCallVerification,
  type GatewayPolicyCutover,
  type GatewayPolicyCutoverPhase,
  type PolicyInventoryItem,
} from "@/lib/api/agentPolicyInventory";
import { cn } from "@/lib/ui";

const ACTIVE_POLL_INTERVAL_MS = 5_000;

const PHASE_LABELS: Record<string, string> = {
  CREATING: "정책 생성 중",
  AWAITING_ACTIVE: "ACTIVE 확인 중",
  DELETING_LEGACY: "이전 정책 정리 중",
  VERIFYING_CALL: "실제 호출 검증 중",
  COMPLETED: "완료",
  FAILED: "실패",
  UNKNOWN: "관측 불가",
};

const TONE_PRESENTATION: Record<
  AgentPolicyInventoryTone,
  { label: string; className: string }
> = {
  pass: {
    label: "live 일치",
    className: "border-emerald-300 bg-emerald-50 text-emerald-800",
  },
  progress: {
    label: "전환 진행 중",
    className: "border-blue-300 bg-blue-50 text-blue-800",
  },
  warning: {
    label: "확인 필요",
    className: "border-red-300 bg-red-50 text-red-800",
  },
  unknown: {
    label: "관측 불가",
    className: "border-amber-300 bg-amber-50 text-amber-900",
  },
};

export function AgentPolicyInventoryClient() {
  const { data, error, isLoading, isValidating, mutate } =
    useSWR<AgentPolicyInventoryReport>(
      "identity/agent-policy-inventory",
      getAgentPolicyInventory,
      {
        revalidateOnFocus: true,
        refreshInterval: (latest) =>
          latest && buildAgentPolicyInventoryView(latest).shouldPoll
            ? ACTIVE_POLL_INTERVAL_MS
            : 0,
      },
    );
  const view = useMemo(
    () => (data ? buildAgentPolicyInventoryView(data) : null),
    [data],
  );

  return (
    <div className="mx-auto w-full max-w-[1440px]">
      <PageHeader
        refreshing={isValidating}
        polling={view?.shouldPoll ?? false}
        onRefresh={() => void mutate()}
      />

      {isLoading ? (
        <LoadingState />
      ) : error || !data || !view ? (
        <ErrorState onRetry={() => void mutate()} />
      ) : (
        <InventoryContent report={data} view={view} />
      )}
    </div>
  );
}

function PageHeader({
  refreshing,
  polling,
  onRefresh,
}: {
  refreshing: boolean;
  polling: boolean;
  onRefresh: () => void;
}) {
  return (
    <header className="mb-5 flex flex-wrap items-start justify-between gap-4">
      <div>
        <h1 className="text-2xl font-bold">Cedar 정책</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Gateway 공유 정책의 원장 기록과 live read-back을 대조합니다.
        </p>
      </div>
      <div className="flex items-center gap-3">
        {polling && (
          <span className="text-xs text-blue-700" role="status">
            전환 상태를 5초마다 확인 중
          </span>
        )}
        <button
          type="button"
          onClick={onRefresh}
          disabled={refreshing}
          aria-label="정책 인벤토리 새로고침"
          className="group relative inline-flex h-9 w-9 items-center justify-center rounded border border-border bg-background text-muted-foreground hover:bg-muted disabled:cursor-wait disabled:opacity-60"
        >
          <Icon
            name="refresh"
            size={17}
            className={cn(refreshing && "animate-spin")}
          />
          <span
            role="tooltip"
            className="pointer-events-none absolute right-0 top-11 z-20 hidden w-max rounded bg-slate-900 px-2 py-1 text-[11px] text-white shadow-sm group-hover:block group-focus-visible:block"
          >
            정책 인벤토리 새로고침
          </span>
        </button>
      </div>
    </header>
  );
}

function InventoryContent({
  report,
  view,
}: {
  report: AgentPolicyInventoryReport;
  view: ReturnType<typeof buildAgentPolicyInventoryView>;
}) {
  if (view.empty) {
    return (
      <section className="border-y border-border bg-muted/30 px-5 py-12 text-center">
        <h2 className="text-sm font-semibold">공유 정책 전환 기록이 없습니다</h2>
        <p className="mt-2 text-sm text-muted-foreground">
          아직 관측된 Gateway 공유 정책 원장 revision이 없습니다.
        </p>
      </section>
    );
  }

  const cutover = view.latestCutover;
  if (!cutover) {
    return (
      <section className="border-y border-amber-200 bg-amber-50 px-5 py-8">
        <h2 className="text-sm font-semibold text-amber-950">
          정책 원장과 live 상태를 확인하지 못했습니다
        </h2>
        <p className="mt-2 break-all text-sm text-amber-900">
          {report.reason || "관측 가능한 공유 정책 정보가 없습니다."}
        </p>
      </section>
    );
  }

  return (
    <div className="space-y-6">
      <CutoverSummary report={report} cutover={cutover} tone={view.tone} />
      <CallVerification verification={cutover.call_verification} />
      {(report.reason ||
        cutover.findings.length > 0 ||
        view.policySetFindings.length > 0) && (
        <Findings
          reportReason={report.reason}
          findings={[...view.policySetFindings, ...cutover.findings]}
        />
      )}
      <PolicyTable policies={view.policies} />
      {view.additionalPolicies.length > 0 && (
        <AdditionalPolicyTable policies={view.additionalPolicies} />
      )}
      <CutoverHistory cutovers={report.shared_cutovers} />
    </div>
  );
}

function CutoverSummary({
  report,
  cutover,
  tone,
}: {
  report: AgentPolicyInventoryReport;
  cutover: GatewayPolicyCutover;
  tone: AgentPolicyInventoryTone;
}) {
  const presentation = TONE_PRESENTATION[tone];
  return (
    <section className="border-y border-border bg-muted/20">
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-border px-4 py-3">
        <div className="flex flex-wrap items-center gap-2">
          <h2 className="text-sm font-semibold">현재 컷오버</h2>
          <span
            className={cn(
              "inline-flex border px-2 py-0.5 text-xs font-semibold",
              presentation.className,
            )}
          >
            {presentation.label}
          </span>
          <PhaseBadge phase={cutover.phase} />
        </div>
        <span className="text-xs text-muted-foreground">
          revision {cutover.revision} · {formatTimestamp(cutover.updated_at)}
        </span>
      </div>
      <dl className="grid gap-px bg-border sm:grid-cols-2 xl:grid-cols-4">
        <SummaryItem label="Gateway" value={cutover.gateway_arn} mono />
        {/* 기대 형태는 굵은 문 한 장이에요 (ADR-0099 결정 8). danger 백스톱을 세지 않아요. */}
        <SummaryItem
          label="공유 정책"
          value={`${cutover.policies.length}장 / 기대 1장`}
          alert={cutover.policies.length !== 1}
        />
        <SummaryItem
          label="live 누락"
          value={
            report.status === "observed"
              ? `${report.shared_missing.length}건`
              : "확인 불가"
          }
          alert={
            report.status !== "observed" ||
            report.shared_missing.length > 0
          }
        />
        <SummaryItem
          label="원장 밖 live 정책"
          value={
            report.status === "observed"
              ? `${report.unmanaged.length}건`
              : "확인 불가"
          }
          alert={
            report.status !== "observed" || report.unmanaged.length > 0
          }
        />
      </dl>
    </section>
  );
}

function SummaryItem({
  label,
  value,
  mono = false,
  alert = false,
}: {
  label: string;
  value: string;
  mono?: boolean;
  alert?: boolean;
}) {
  return (
    <div className="min-w-0 bg-background px-4 py-3">
      <dt className="text-[11px] font-medium text-muted-foreground">{label}</dt>
      <dd
        className={cn(
          "mt-1 break-all text-sm font-semibold",
          mono && "font-mono text-xs font-normal",
          alert && "text-red-700",
        )}
      >
        {value}
      </dd>
    </div>
  );
}

function CallVerification({
  verification,
}: {
  verification: GatewayCallVerification;
}) {
  const qualified = callVerificationQualified(verification);
  const positiveObserved =
    positiveCallVerificationQualified(verification);
  const negativeObserved =
    negativeCallVerificationQualified(verification);
  const negativeKind = describeNegativeControlKind(
    verification.negative_control_kind,
  );
  return (
    <section
      className={cn(
        "border-y px-4 py-3",
        qualified
          ? "border-emerald-200 bg-emerald-50/60"
          : "border-amber-200 bg-amber-50/70",
      )}
    >
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="text-sm font-semibold">독립 호출 검증</h2>
          <p className="mt-1 text-xs text-muted-foreground">
            실제 Gateway 허용 호출과 음성 대조 거부 호출을 각각 관측해야 합니다.
            거부를 만드는 지점은 REQUEST interceptor 한 곳입니다.
          </p>
        </div>
        <div className="flex flex-wrap gap-4 text-xs">
          <Observation
            label="허용 호출"
            observed={positiveObserved}
          />
          <Observation
            label="거부 음성 대조"
            observed={negativeObserved}
          />
          <span>
            증거 출처:{" "}
            <strong>{verification.evidence_source || "확인 불가"}</strong>
          </span>
        </div>
      </div>
      {verification.detail && (
        <p className="mt-2 text-xs text-muted-foreground">
          {verification.detail}
        </p>
      )}
      <dl className="mt-3 grid gap-x-6 gap-y-2 border-t border-current/10 pt-3 text-xs sm:grid-cols-2 xl:grid-cols-3">
        <EvidenceItem label="허용 호출 ID" value={verification.positive_call_id} />
        <EvidenceItem label="거부 호출 ID" value={verification.negative_call_id} />
        <EvidenceItem
          label="허용 target event"
          value={verification.positive_target_event_id}
        />
        <EvidenceItem
          label="거부 decision event"
          value={verification.negative_decision_event_id}
        />
        <EvidenceItem
          label="관측 window"
          value={verification.observation_window_id}
        />
        <EvidenceItem
          label="음성 대조 종류"
          value={verification.negative_control_kind}
          note={negativeKind.label}
          noteAlert={!negativeKind.supported}
        />
      </dl>
    </section>
  );
}

function EvidenceItem({
  label,
  value,
  note,
  noteAlert = false,
}: {
  label: string;
  value: string;
  note?: string;
  noteAlert?: boolean;
}) {
  return (
    <div className="min-w-0">
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="mt-0.5 break-all font-mono">{value || "미관측"}</dd>
      {note && (
        <p
          className={cn(
            "mt-0.5 break-words text-[11px]",
            noteAlert
              ? "font-semibold text-amber-900"
              : "text-muted-foreground",
          )}
        >
          {note}
        </p>
      )}
    </div>
  );
}

function Observation({
  label,
  observed,
}: {
  label: string;
  observed: boolean;
}) {
  return (
    <span className={observed ? "text-emerald-800" : "text-amber-900"}>
      {label}: <strong>{observed ? "관측" : "미관측"}</strong>
    </span>
  );
}

function Findings({
  reportReason,
  findings,
}: {
  reportReason: string;
  findings: string[];
}) {
  const items = [reportReason, ...findings].filter(Boolean);
  return (
    <section className="border-y border-red-200 bg-red-50/70 px-4 py-3">
      <h2 className="text-sm font-semibold text-red-900">확인 필요 사유</h2>
      <ul className="mt-2 space-y-1 text-sm text-red-800">
        {items.map((finding, index) => (
          <li key={`${finding}-${index}`}>{finding}</li>
        ))}
      </ul>
    </section>
  );
}

function PolicyTable({
  policies,
}: {
  policies: ReturnType<typeof buildAgentPolicyInventoryView>["policies"];
}) {
  return (
    <section>
      <div className="mb-2 flex flex-wrap items-end justify-between gap-2">
        <div>
          <h2 className="text-base font-semibold">공유 정책 대조</h2>
          <p className="mt-1 text-xs text-muted-foreground">
            왼쪽은 원장 checkpoint, 오른쪽은 독립 live read-back입니다.
          </p>
        </div>
        <span className="text-xs text-muted-foreground">
          {policies.length}개 정책
        </span>
      </div>
      <div className="overflow-x-auto border-y border-border">
        <table className="w-full min-w-[1020px] table-fixed text-left text-sm">
          <thead className="bg-muted/50 text-[11px] text-muted-foreground">
            <tr>
              <th className="w-[18%] px-3 py-2 font-semibold">정책</th>
              <th className="w-[34%] px-3 py-2 font-semibold">
                원장 checkpoint
              </th>
              <th className="w-[28%] px-3 py-2 font-semibold">
                live read-back
              </th>
              <th className="w-[20%] px-3 py-2 font-semibold">규모</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-border">
            {policies.map((policy) => (
              <Fragment key={`${policy.key}-${policy.policyId}`}>
                <tr className="align-top">
                  <td className="px-3 py-3">
                    <div className="font-semibold">{policy.key}</div>
                    <div className="mt-1 break-all font-mono text-[11px] text-muted-foreground">
                      {policy.name}
                    </div>
                    <div className="mt-1 break-all font-mono text-[11px] text-muted-foreground">
                      {policy.policyId || "policyId 미기록"}
                    </div>
                  </td>
                  <td className="px-3 py-3">
                    <div className="flex flex-wrap gap-2 text-xs">
                      <span className="border border-blue-200 bg-blue-50 px-1.5 py-0.5 text-blue-800">
                        원장 기록 {policy.ledger.checkpointStatus}
                      </span>
                      <PhaseBadge phase={policy.ledger.phase} />
                    </div>
                    <HashLine label="기대 hash" value={policy.ledger.policyHash} />
                    <HashLine
                      label="배포 시 hash"
                      value={policy.ledger.deployedPolicyHash || "미기록"}
                    />
                  </td>
                  <td className="px-3 py-3">
                    <LiveObservation
                      remoteStatus={policy.live.remoteStatus}
                      reason={policy.live.reason}
                      detail={policy.live.detail}
                    />
                  </td>
                  <td className="px-3 py-3 text-xs">
                    <div>{formatBytes(policy.ledger.sizeBytes)}</div>
                    {/* 굵은 문은 Target 을 갖지 않아요. 「위험 Target 0개」를 상시 보여 주면
                        없어진 danger 백스톱이 여전히 있는 것처럼 읽혀요 (ADR-0099 결정 8).
                        0 이 아니면 옛 형태이니 조용히 넘기지 않고 표시해요. */}
                    {policy.ledger.targetCount > 0 && (
                      <div className="mt-1 font-semibold text-red-700">
                        Target {policy.ledger.targetCount}개 — 굵은 문은 Target 을
                        갖지 않아요
                      </div>
                    )}
                  </td>
                </tr>
                <tr className="bg-muted/15">
                  <td colSpan={4} className="px-3 py-2">
                    <details>
                      <summary className="cursor-pointer text-xs font-medium text-muted-foreground">
                        원장 기대 Cedar 보기 (live 문장 아님)
                      </summary>
                      <pre className="mt-2 max-h-72 overflow-auto border-l-2 border-slate-300 bg-slate-950 p-3 text-[11px] leading-5 text-slate-100">
                        <code>{policy.ledger.cedarPolicy}</code>
                      </pre>
                    </details>
                  </td>
                </tr>
              </Fragment>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function HashLine({ label, value }: { label: string; value: string }) {
  return (
    <div className="mt-2 grid grid-cols-[76px_minmax(0,1fr)] gap-2 text-[11px]">
      <span className="text-muted-foreground">{label}</span>
      <code className="break-all">{value}</code>
    </div>
  );
}

function LiveObservation({
  remoteStatus,
  reason,
  detail,
}: {
  remoteStatus: string;
  reason: "in_sync" | "drift" | "missing" | "unknown";
  detail: string;
}) {
  const presentation = {
    in_sync: {
      label: "일치",
      className: "border-emerald-300 bg-emerald-50 text-emerald-800",
    },
    drift: {
      label: "불일치",
      className: "border-red-300 bg-red-50 text-red-800",
    },
    missing: {
      label: "누락",
      className: "border-red-300 bg-red-50 text-red-800",
    },
    unknown: {
      label: "확인 불가",
      className: "border-amber-300 bg-amber-50 text-amber-900",
    },
  }[reason];
  return (
    <div>
      <div className="flex flex-wrap items-center gap-2 text-xs">
        <span
          className={cn(
            "inline-flex border px-1.5 py-0.5 font-semibold",
            presentation.className,
          )}
        >
          {presentation.label}
        </span>
        <span>
          remote_status: <strong>{remoteStatus}</strong>
        </span>
      </div>
      <div className="mt-2 break-all text-[11px] text-muted-foreground">
        reason: {reason}
      </div>
      {detail && detail !== "shared_missing" && (
        <div className="mt-1 break-all text-[11px] text-muted-foreground">
          {detail}
        </div>
      )}
    </div>
  );
}

function AdditionalPolicyTable({
  policies,
}: {
  policies: Array<
    PolicyInventoryItem & {
      classification: "managed" | "orphan" | "unmanaged";
      preserved: boolean;
    }
  >;
}) {
  return (
    <section>
      <h2 className="text-base font-semibold">추가 live 정책</h2>
      <p className="mt-1 text-xs text-muted-foreground">
        최신 공유 revision 밖의 정책입니다. 컷오버 원장에 보존 ID로
        기록된 정책만 예외입니다.
      </p>
      <div className="mt-2 overflow-x-auto border-y border-border">
        <table className="w-full min-w-[900px] text-left text-sm">
          <thead className="bg-muted/50 text-[11px] text-muted-foreground">
            <tr>
              <th className="px-3 py-2 font-semibold">policyId</th>
              <th className="px-3 py-2 font-semibold">분류</th>
              <th className="px-3 py-2 font-semibold">live 상태</th>
              <th className="px-3 py-2 font-semibold">원장 revision</th>
              <th className="px-3 py-2 font-semibold">분류 사유</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-border">
            {policies.map((policy) => (
              <tr key={policy.policy_id}>
                <td className="break-all px-3 py-2 font-mono text-xs">
                  {policy.policy_id}
                </td>
                <td className="px-3 py-2">
                  <span
                    className={cn(
                      "inline-flex border px-1.5 py-0.5 text-xs font-semibold",
                      policy.preserved
                        ? "border-slate-300 bg-slate-50 text-slate-700"
                        : "border-red-300 bg-red-50 text-red-800",
                    )}
                  >
                    {policy.preserved
                      ? "보존 대상"
                      : policy.classification}
                  </span>
                </td>
                <td className="px-3 py-2">{policy.remote_status}</td>
                <td className="px-3 py-2">
                  {policy.ledger_revision ?? "미기록"}
                </td>
                <td className="break-all px-3 py-2 text-xs text-muted-foreground">
                  {policy.reason}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function CutoverHistory({ cutovers }: { cutovers: GatewayPolicyCutover[] }) {
  const ordered = [...cutovers].sort((a, b) => b.revision - a.revision);
  return (
    <section>
      <h2 className="text-base font-semibold">컷오버 원장 이력</h2>
      <div className="mt-2 overflow-x-auto border-y border-border">
        <table className="w-full min-w-[720px] text-left text-sm">
          <thead className="bg-muted/50 text-[11px] text-muted-foreground">
            <tr>
              <th className="px-3 py-2 font-semibold">revision</th>
              <th className="px-3 py-2 font-semibold">phase</th>
              <th className="px-3 py-2 font-semibold">정책 수</th>
              <th className="px-3 py-2 font-semibold">삭제 확인</th>
              <th className="px-3 py-2 font-semibold">갱신 시각</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-border">
            {ordered.map((cutover) => (
              <tr key={`${cutover.gateway_arn}-${cutover.revision}`}>
                <td className="px-3 py-2 font-mono">{cutover.revision}</td>
                <td className="px-3 py-2">
                  <PhaseBadge phase={cutover.phase} />
                </td>
                <td className="px-3 py-2">{cutover.policies.length}</td>
                <td className="px-3 py-2">
                  {cutover.deleted_policy_ids.length}/
                  {cutover.delete_requested_policy_ids.length}
                </td>
                <td className="px-3 py-2 text-xs text-muted-foreground">
                  {formatTimestamp(cutover.updated_at)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function PhaseBadge({ phase }: { phase: GatewayPolicyCutoverPhase }) {
  const dangerous = phase === "FAILED" || phase === "UNKNOWN";
  const completed = phase === "COMPLETED";
  return (
    <span
      className={cn(
        "inline-flex border px-1.5 py-0.5 text-xs font-semibold",
        completed && "border-slate-300 bg-slate-50 text-slate-700",
        dangerous && "border-red-300 bg-red-50 text-red-800",
        !completed &&
          !dangerous &&
          "border-blue-300 bg-blue-50 text-blue-800",
      )}
    >
      {PHASE_LABELS[phase] ?? phase}
    </span>
  );
}

function LoadingState() {
  return (
    <div aria-label="정책 인벤토리 불러오는 중" className="space-y-5">
      <div className="h-24 animate-pulse border-y border-border bg-muted/50" />
      <div className="space-y-px border-y border-border bg-border">
        {Array.from({ length: 4 }).map((_, index) => (
          <div
            key={index}
            className="h-20 animate-pulse bg-background"
          />
        ))}
      </div>
    </div>
  );
}

function ErrorState({ onRetry }: { onRetry: () => void }) {
  return (
    <section className="border-y border-red-200 bg-red-50 px-5 py-8">
      <h2 className="text-sm font-semibold text-red-900">
        정책 인벤토리를 불러오지 못했습니다
      </h2>
      <p className="mt-2 text-sm text-red-800">
        live 상태를 확인하지 못했으므로 정상으로 간주하지 않습니다.
      </p>
      <button
        type="button"
        onClick={onRetry}
        className="mt-4 inline-flex h-9 items-center gap-2 border border-red-300 bg-background px-3 text-sm font-medium text-red-800 hover:bg-red-100"
      >
        <Icon name="refresh" size={15} />
        다시 시도
      </button>
    </section>
  );
}

function formatTimestamp(value: string): string {
  if (!value) return "시각 미기록";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return new Intl.DateTimeFormat("ko-KR", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(parsed);
}

function formatBytes(value: number): string {
  if (value < 1024) return `${value} B`;
  return `${(value / 1024).toFixed(1)} KB`;
}
