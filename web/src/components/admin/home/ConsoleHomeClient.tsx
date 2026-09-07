"use client";

import Link from "next/link";
import useSWR from "swr";
import { ADMIN_NAV, type AdminNavItem } from "@/lib/adminNav";
import { listGovQueue, type QueueItem } from "@/lib/api";
import { Icon } from "@/components/ui/icon";
import { cn } from "@/lib/ui";

// 콘솔 홈 (/admin) — 6개 화면으로 가는 요약.
// 신규 API 에 의존하는 카드는 "준비 중"으로 렌더해요. 홈이 없는 API 에 막혀
// 콘솔 진입 자체가 안 되는 상황을 만들지 않는 게 설계 요구예요
// (docs/design/admin-console-spec.md §4.1).
export function ConsoleHomeClient() {
  const { data, error, isLoading } = useSWR<{ items: QueueItem[] }>(
    "governance/queue",
    listGovQueue,
    { shouldRetryOnError: false },
  );
  // 심사 대기 = 승인 큐에서 아직 판정이 안 난 것. 큐 응답은 판정 완료 자산도 담아요.
  const pending = data?.items.filter(
    (item) => item.status === "PENDING_APPROVAL" || item.status === "DRAFT",
  ).length;

  return (
    <div className="min-w-0">
      <div className="mb-6">
        <h1 className="text-2xl font-bold tracking-tight">관리자 콘솔</h1>
        <p className="mt-1 max-w-3xl text-sm text-muted-foreground">
          자산 심사(거버넌스)와 Agent 인가를 한 콘솔에서 관리해요.
        </p>
      </div>

      <section aria-labelledby="console-summary-title" className="mb-9">
        <h2 id="console-summary-title" className="mb-3 text-base font-semibold">
          지금 봐야 할 것
        </h2>
        <div className="grid min-w-0 gap-3 sm:grid-cols-2 xl:grid-cols-4">
          <SummaryCard
            label="심사 대기 자산"
            href="/admin/queue"
            value={
              isLoading
                ? "…"
                : error
                  ? "조회 실패"
                  : pending === undefined
                    ? "—"
                    : `${pending}건`
            }
            hint={error ? "승인 큐를 불러오지 못했어요." : "승인 큐에서 판정해요."}
            tone={error ? "error" : "ready"}
          />
          <SummaryCard
            label="그룹 미배정 사용자"
            href="/admin/users"
            value="준비 중"
            hint="Cognito 사용자 API 가 필요해요 (S6)."
            tone="pending"
          />
          <SummaryCard
            label="미설정 tool"
            href="/admin/agents"
            value="Agent 별 확인"
            hint="Agent 를 고르면 그 agent 에 연결된 tool 의 허용 상태가 보여요."
            tone="ready"
          />
          <SummaryCard
            label="최근 24h DENY"
            href="/admin/audit-calls"
            value="준비 중"
            hint="감사 조회 GSI 가 필요해요 (S7)."
            tone="pending"
          />
        </div>
      </section>

      <section aria-labelledby="console-nav-title">
        <h2 id="console-nav-title" className="mb-3 text-base font-semibold">
          전체 화면
        </h2>
        <div className="grid min-w-0 gap-5 lg:grid-cols-3">
          {ADMIN_NAV.map((section) => (
            <div
              key={section.key}
              className="min-w-0 rounded-xl border border-border bg-card p-4"
            >
              <p className="mb-2.5 text-[10px] font-bold uppercase tracking-[0.06em] text-slate-400">
                {section.label}
              </p>
              <div className="space-y-0.5">
                {section.items.map((item) => (
                  <NavCardLink key={item.href} item={item} />
                ))}
              </div>
            </div>
          ))}
        </div>
      </section>
    </div>
  );
}

function SummaryCard({
  label,
  value,
  hint,
  href,
  tone,
}: {
  label: string;
  value: string;
  hint: string;
  href: string;
  tone: "ready" | "pending" | "error";
}) {
  return (
    <Link
      href={href}
      className="min-w-0 rounded-xl border border-border bg-card p-4 transition-colors hover:bg-accent/40"
    >
      <div className="text-xs font-medium text-muted-foreground">{label}</div>
      <div
        className={cn(
          "mt-1.5 text-xl font-bold tracking-tight",
          tone === "pending" && "text-base font-semibold text-amber-700",
          tone === "error" && "text-base font-semibold text-red-700",
        )}
      >
        {value}
      </div>
      <p className="mt-1.5 text-xs text-muted-foreground">{hint}</p>
    </Link>
  );
}

function NavCardLink({ item }: { item: AdminNavItem }) {
  return (
    <Link
      href={item.href}
      className="flex min-w-0 items-center gap-2.5 rounded-lg px-2 py-1.5 text-[13px] text-muted-foreground hover:bg-accent hover:text-foreground"
    >
      <Icon name={item.icon} size={15} className="opacity-80" />
      <span className="min-w-0 truncate">{item.label}</span>
      {item.pending && (
        <span className="ml-auto shrink-0 rounded-full bg-amber-100 px-1.5 py-0.5 text-[9px] font-semibold text-amber-700">
          준비 중
        </span>
      )}
    </Link>
  );
}
