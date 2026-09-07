"use client";

import Link from "next/link";
import useSWR from "swr";
import { getGovDashboard, type Dashboard, type DashboardAsset } from "@/lib/api";
import { ADMIN_QUEUE } from "@/lib/adminNav";
import { areaLabel, tierLabel, registryStatusLabel } from "@/lib/governance";
import { Card } from "@/components/ui/card";
import { cn } from "@/lib/ui";
import { DonutChart, type DonutDatum } from "./DonutChart";

// 미스캔 리스트에 최대 몇 개까지 이름을 노출할지 (넘치면 "외 X건").
const UNSCANNED_LIMIT = 8;

// SVG 도넛 세그먼트 색 — governance.ts 배지 색 관례(빨강/노랑/초록/회색)를 hex로 매핑해요.
// 배지는 Tailwind 클래스라 SVG stroke엔 못 써서, 같은 색 계열의 hex 값을 여기서 둬요.
const RISK_COLORS: Record<string, string> = {
  high: "#ef4444",       // red-500
  medium: "#f59e0b",     // amber-500
  low: "#facc15",        // yellow-400
  none: "#10b981",       // emerald-500
  unscanned: "#cbd5e1",  // slate-300 (미스캔)
};
const TIER_COLORS: Record<string, string> = {
  minimal: "#38bdf8",    // sky-400
  standard: "#6366f1",   // indigo-500
  strong: "#8b5cf6",     // violet-500
};
const STATUS_COLORS: Record<string, string> = {
  DRAFT: "#94a3b8",            // slate-400 (초안)
  PENDING_APPROVAL: "#f59e0b", // amber-500 (심사중)
  APPROVED: "#10b981",         // emerald-500 (승인)
  REJECTED: "#ef4444",         // red-500 (반려)
  DEPRECATED: "#cbd5e1",       // slate-300 (폐기)
};
const FALLBACK_COLOR = "#cbd5e1";

// 대시보드 (§3-M5) — 3열 구성: 승인 KPI · SLA/미스캔 · 분포 도넛. 결정 아님, 드릴다운은 큐로.
export function DashboardClient() {
  // 큐에서 승인/반려/스캔이 일어나면 대시보드도 최신이어야 해요. 탭 복귀 시 재검증하고,
  // 화면을 열어둔 동안에도 주기적으로(30s) 새로고침해 큐와 어긋나지 않게 해요.
  const { data, error, isLoading } = useSWR<Dashboard>("gov/dashboard", getGovDashboard, {
    revalidateOnFocus: true,
    refreshInterval: 30_000,
  });

  if (isLoading) return <DashboardShell><WidgetSkeleton /></DashboardShell>;
  if (error || !data) {
    return (
      <DashboardShell>
        <Card className="p-6 text-sm text-red-700">대시보드를 불러오지 못했어요.</Card>
      </DashboardShell>
    );
  }

  // 위험 분포 도넛 데이터 (미스캔은 unscanned 키).
  const riskData: DonutDatum[] = Object.entries(data.risk_distribution).map(([risk, value]) => ({
    label: risk === "unscanned" ? "미스캔" : risk.toUpperCase(),
    value,
    color: RISK_COLORS[risk] ?? FALLBACK_COLOR,
  }));
  // 등급 분포 도넛 데이터 (범례가 큐 드릴다운 링크 ?tier= 를 유지).
  const tierData: DonutDatum[] = Object.entries(data.tier_distribution).map(([tier, value]) => ({
    label: tierLabel(tier),
    value,
    color: TIER_COLORS[tier] ?? FALLBACK_COLOR,
    href: `${ADMIN_QUEUE}?tier=${tier}`,
  }));
  // 상태 분포 도넛 데이터 (라이프사이클 순서).
  const statusData: DonutDatum[] = orderedStatuses(data.status_distribution).map(([status, value]) => ({
    label: registryStatusLabel(status),
    value,
    color: STATUS_COLORS[status] ?? FALLBACK_COLOR,
  }));

  const shownUnscanned = data.unscanned_assets.slice(0, UNSCANNED_LIMIT);
  const restUnscanned = data.unscanned_assets.length - shownUnscanned.length;

  return (
    <DashboardShell>
      <div className="space-y-5">
        {/* ── 1열: 승인 현황 KPI 카드 ─────────────────────────── */}
        <section className="grid grid-cols-2 gap-5 xl:grid-cols-4">
          <KpiCard label="APPROVE" value={data.approvals.approve} cls="text-emerald-600" />
          <KpiCard label="PENDING" value={data.approvals.pending} cls="text-amber-600" />
          <KpiCard label="REJECT" value={data.approvals.reject} cls="text-red-600" />
          <KpiCard
            label="자동화율"
            value={data.approvals.automation_rate}
            suffix="%"
            cls="text-blue-600"
            note={data.approvals.override > 0 ? `override ${data.approvals.override}` : undefined}
          />
        </section>

        {/* ── 2열: SLA·미결 + 미스캔(대기) 리스트 ──────────────── */}
        <section className="grid grid-cols-1 gap-5 lg:grid-cols-3">
          <Widget title="SLA · 미결">
            <div className="flex flex-wrap gap-6">
              <Metric label="미스캔(대기)" value={data.sla.unscanned} />
              <Metric label="전체 자산" value={data.sla.total} />
              <Metric label="판정 완료" value={data.sla.decided} />
            </div>
            <Link href={ADMIN_QUEUE} className="mt-3 inline-block text-xs text-blue-700 hover:underline">
              → 승인 큐에서 처리하기
            </Link>
          </Widget>

          <Widget title="미스캔(대기) 자산" className="lg:col-span-2">
            {data.unscanned_assets.length === 0 ? (
              <Empty text="미스캔 자산이 없어요" />
            ) : (
              <>
                <ul className="space-y-1.5">
                  {shownUnscanned.map((a) => (
                    <li key={a.record_id} className="flex items-center justify-between gap-3 text-sm">
                      <Link
                        href={`${ADMIN_QUEUE}/${encodeURIComponent(a.record_id)}`}
                        className="truncate font-medium text-blue-700 hover:underline"
                      >
                        {a.name}
                      </Link>
                      <span className="shrink-0 rounded bg-slate-100 px-1.5 py-0.5 text-xs text-slate-500">미스캔</span>
                    </li>
                  ))}
                </ul>
                {restUnscanned > 0 && (
                  <div className="mt-2 text-xs text-muted-foreground">외 {restUnscanned}건</div>
                )}
              </>
            )}
          </Widget>
        </section>

        {/* ── 3열: 게이트 통과율 + 위험/등급/상태 분포 도넛 ──────── */}
        {/* 각 위젯 아래에 그 분포를 구성하는 자산 리스트를 카테고리별로 상시 표시해요. */}
        <section className="grid grid-cols-1 gap-5 md:grid-cols-2 xl:grid-cols-4">
          <Widget title="게이트 통과율">
            {Object.keys(data.gate_pass_rate).length === 0 ? (
              <Empty text="스캔된 자산이 없어요" />
            ) : (
              <>
                {Object.entries(data.gate_pass_rate).map(([area, rate]) => (
                  <BarRow key={area} label={areaLabel(area)} pct={rate} />
                ))}
                <AssetGroups
                  groups={gateGroups(data.assets)}
                  emptyText="차단된 게이트가 없어요"
                />
              </>
            )}
          </Widget>

          <Widget title="위험 분포">
            <DonutChart data={riskData} ariaLabel="위험 분포" />
            <AssetGroups groups={riskGroups(data.assets)} />
          </Widget>

          <Widget title="등급 분포">
            <DonutChart data={tierData} ariaLabel="등급 분포" />
            <AssetGroups groups={tierGroups(data.assets)} />
          </Widget>

          <Widget title="상태 분포">
            <DonutChart data={statusData} ariaLabel="상태 분포" />
            <AssetGroups groups={statusGroups(data.assets)} />
          </Widget>
        </section>

        {/* 도구별 차단 기여 (§3-M5 후속) — fail 게이트를 많이 유발한 도구 상위 */}
        <section>
          <Widget title="도구별 차단 기여">
            {data.top_blockers.length === 0 ? (
              <Empty text="차단된 게이트가 없어요" />
            ) : (
              <ul className="space-y-1.5">
                {data.top_blockers.slice(0, 6).map((b) => (
                  <li key={`${b.area}:${b.tool_name}`}
                      className="flex items-center justify-between gap-3 text-sm">
                    <span className="flex items-center gap-2 truncate">
                      <span className="truncate font-medium text-foreground">{b.tool_name}</span>
                      <span className="shrink-0 text-xs text-muted-foreground">{areaLabel(b.area)}</span>
                    </span>
                    <Link href={`${ADMIN_QUEUE}?risk=high`}
                          className="shrink-0 rounded bg-red-50 px-2 py-0.5 text-xs font-semibold tabular-nums text-red-700 hover:bg-red-100">
                      {b.count}건 차단
                    </Link>
                  </li>
                ))}
              </ul>
            )}
          </Widget>
        </section>
      </div>
    </DashboardShell>
  );
}

function DashboardShell({ children }: { children: React.ReactNode }) {
  return (
    <div>
      <div className="mb-6">
        <h1 className="text-2xl font-bold tracking-tight">대시보드</h1>
      </div>
      {children}
    </div>
  );
}

function Widget({ title, children, className }: { title: string; children: React.ReactNode; className?: string }) {
  return (
    <Card className={cn("p-5", className)}>
      <div className="mb-3 text-sm font-semibold">{title}</div>
      <div className="space-y-1">{children}</div>
    </Card>
  );
}
// 승인 현황 항목별 KPI 카드 — 큰 숫자 + 라벨(Metric 확대 스타일).
function KpiCard({ label, value, suffix, cls, note }: {
  label: string; value: number; suffix?: string; cls?: string; note?: string;
}) {
  return (
    <Card className="p-5">
      <div className="text-xs font-medium text-muted-foreground">{label}</div>
      <div className={cn("mt-1 text-4xl font-bold tabular-nums", cls)}>
        {value}{suffix}
      </div>
      {note && <div className="mt-1 text-xs text-muted-foreground">{note}</div>}
    </Card>
  );
}
function BarRow({ label, pct }: { label: string; pct: number }) {
  return (
    <div className="py-1">
      <div className="mb-0.5 flex items-center justify-between text-xs">
        <span className="text-muted-foreground">{label}</span>
        <span className="tabular-nums font-medium">{pct}%</span>
      </div>
      <div className="h-1.5 w-full overflow-hidden rounded-full bg-slate-100">
        <div className={cn("h-full rounded-full", pct >= 80 ? "bg-emerald-500" : pct >= 50 ? "bg-amber-500" : "bg-red-500")}
          style={{ width: `${pct}%` }} />
      </div>
    </div>
  );
}
function Metric({ label, value }: { label: string; value: number }) {
  return (
    <div>
      <div className="text-2xl font-bold tabular-nums">{value}</div>
      <div className="text-xs text-muted-foreground">{label}</div>
    </div>
  );
}
function Empty({ text }: { text: string }) {
  return <div className="py-3 text-center text-xs text-muted-foreground">{text}</div>;
}

// ── 자산별 리스트 (분포 위젯 아래) ─────────────────────────────────────
// 한 카테고리(예: 위험 HIGH)와 거기 속한 자산들. 색은 위 도넛과 같은 색맵을 재사용해요.
type AssetGroup = { key: string; label: string; color: string; assets: DashboardAsset[] };

const asc = (a: string, b: string) => a.localeCompare(b);

// 위험 분포: none/low/medium/high + 미스캔(null). 도넛(riskData)과 같은 순서·색.
function riskGroups(assets: DashboardAsset[]): AssetGroup[] {
  const order = ["high", "medium", "low", "none", "unscanned"];
  const by: Record<string, DashboardAsset[]> = {};
  for (const a of assets) {
    const k = a.risk ?? "unscanned";
    (by[k] ??= []).push(a);
  }
  return order
    .filter((k) => by[k]?.length)
    .map((k) => ({
      key: k,
      label: k === "unscanned" ? "미스캔" : k.toUpperCase(),
      color: RISK_COLORS[k] ?? FALLBACK_COLOR,
      assets: by[k].sort((x, y) => asc(x.name, y.name)),
    }));
}

// 상태 분포: 라이프사이클 순서. 도넛(statusData)과 같은 순서·색.
function statusGroups(assets: DashboardAsset[]): AssetGroup[] {
  const order = ["DRAFT", "PENDING_APPROVAL", "APPROVED", "REJECTED", "DEPRECATED"];
  const by: Record<string, DashboardAsset[]> = {};
  for (const a of assets) (by[a.status] ??= []).push(a);
  const keys = [...order.filter((s) => by[s]), ...Object.keys(by).filter((s) => !order.includes(s))];
  return keys.map((k) => ({
    key: k,
    label: registryStatusLabel(k),
    color: STATUS_COLORS[k] ?? FALLBACK_COLOR,
    assets: by[k].sort((x, y) => asc(x.name, y.name)),
  }));
}

// 등급 분포: minimal/standard/strong. 도넛(tierData)과 같은 색.
function tierGroups(assets: DashboardAsset[]): AssetGroup[] {
  const order = ["minimal", "standard", "strong"];
  const by: Record<string, DashboardAsset[]> = {};
  for (const a of assets) (by[a.tier] ??= []).push(a);
  return order
    .filter((k) => by[k]?.length)
    .map((k) => ({
      key: k,
      label: tierLabel(k),
      color: TIER_COLORS[k] ?? FALLBACK_COLOR,
      assets: by[k].sort((x, y) => asc(x.name, y.name)),
    }));
}

// 게이트 통과율: fail한 area별로 그 area에서 막힌 자산을 묶어요(차단 기여를 자산 단위로).
// 한 자산이 여러 area에서 fail이면 각 area 그룹에 들어가요.
function gateGroups(assets: DashboardAsset[]): AssetGroup[] {
  const by: Record<string, DashboardAsset[]> = {};
  for (const a of assets) {
    for (const area of a.gates) (by[area] ??= []).push(a);
  }
  return Object.keys(by)
    .sort()
    .map((area) => ({
      key: area,
      label: `${areaLabel(area)} 차단`,
      color: RISK_COLORS.high,   // fail = 위험(빨강)로 통일
      assets: by[area].sort((x, y) => asc(x.name, y.name)),
    }));
}

// 카테고리별 자산 리스트 — 각 그룹에 색 점 + 라벨 + 건수, 아래 자산명(큐 링크).
function AssetGroups({ groups, emptyText }: { groups: AssetGroup[]; emptyText?: string }) {
  if (groups.length === 0) {
    return <div className="mt-3 border-t border-border pt-3 text-xs text-muted-foreground">{emptyText ?? "자산이 없어요"}</div>;
  }
  return (
    <div className="mt-3 space-y-2.5 border-t border-border pt-3">
      {groups.map((g) => (
        <div key={g.key}>
          <div className="mb-1 flex items-center gap-1.5 text-xs font-medium">
            <span className="h-2 w-2 shrink-0 rounded-sm" style={{ backgroundColor: g.color }} aria-hidden />
            <span className="text-muted-foreground">{g.label}</span>
            <span className="tabular-nums text-slate-400">{g.assets.length}</span>
          </div>
          <ul className="space-y-0.5 pl-3.5">
            {g.assets.map((a) => (
              <li key={`${g.key}:${a.record_id}`} className="truncate text-xs">
                <Link
                  href={`${ADMIN_QUEUE}/${encodeURIComponent(a.record_id)}`}
                  className="text-blue-700 hover:underline"
                >
                  {a.name}
                </Link>
              </li>
            ))}
          </ul>
        </div>
      ))}
    </div>
  );
}
// 상태 분포를 라이프사이클 순서(초안→심사중→승인→반려→폐기)로 정렬. 미등록 상태는 뒤에 붙여요.
function orderedStatuses(dist: Record<string, number>): [string, number][] {
  const order = ["DRAFT", "PENDING_APPROVAL", "APPROVED", "REJECTED", "DEPRECATED"];
  const known = order.filter((s) => s in dist).map((s) => [s, dist[s]] as [string, number]);
  const extra = Object.entries(dist).filter(([s]) => !order.includes(s));
  return [...known, ...extra];
}
function WidgetSkeleton() {
  return (
    <div className="space-y-5">
      <div className="grid grid-cols-2 gap-5 xl:grid-cols-4">
        {Array.from({ length: 4 }).map((_, i) => (
          <div key={i} className="h-28 animate-pulse rounded-xl bg-muted/50" />
        ))}
      </div>
      <div className="grid grid-cols-1 gap-5 md:grid-cols-2 xl:grid-cols-4">
        {Array.from({ length: 4 }).map((_, i) => (
          <div key={i} className="h-40 animate-pulse rounded-xl bg-muted/50" />
        ))}
      </div>
    </div>
  );
}
