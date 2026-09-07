"use client";

import Link from "next/link";
import { useMemo, useState } from "react";
import useSWR from "swr";
import {
  getGovInventory,
  type Inventory,
  type InventoryAsset,
} from "@/lib/api";
import { ADMIN_QUEUE } from "@/lib/adminNav";
import { registryStatusLabel } from "@/lib/governance";
import { Card } from "@/components/ui/card";
import { cn } from "@/lib/ui";
import {
  MeasurementBadge,
  NotMeasuredCell,
  PendingSourceEmpty,
} from "./MeasurementBadge";

// 자산 유형 필터. "갭"은 미스캔이거나 중복 high인 자산 — 관리자가 먼저 봐야 하는 축이에요.
type TypeFilter = "all" | "Agent" | "MCP" | "Agent Skills" | "gap";

// 테이블 정렬 가능한 컬럼.
type SortKey =
  | "name" | "asset_type" | "owner_team" | "model" | "version"
  | "status" | "risk" | "overlap" | "authorization_enforcement";

// 위험도 정렬 순위 — 문자열 정렬로는 high가 low보다 뒤로 가서 의미가 없어요.
const RISK_ORDER: Record<string, number> = {
  high: 4, medium: 3, low: 2, none: 1,
};
const BAND_ORDER: Record<string, number> = { high: 3, medium: 2, low: 1, none: 0 };

const TYPE_FILTERS: TypeFilter[] = ["all", "Agent", "MCP", "Agent Skills", "gap"];

export function InventoryClient() {
  const { data, error, isLoading, mutate, isValidating } = useSWR<Inventory>(
    "gov/inventory",
    getGovInventory,
    { revalidateOnFocus: true, refreshInterval: 60_000 },
  );
  const [filter, setFilter] = useState<TypeFilter>("all");
  const [sort, setSort] = useState<{ key: SortKey; asc: boolean }>({
    key: "name", asc: true,
  });

  const rows = useMemo(() => {
    const assets = data?.assets ?? [];
    const filtered = assets.filter((a) => {
      if (filter === "all") return true;
      if (filter === "gap") {
        return a.risk === null
          || a.overlap_band === "high"
          // Gateway target 이 없는 자산도 갭이에요 (CA-32).
          || a.gateway_target_state === "provisioning"
          || ["log_only", "unmanaged", "unknown"].includes(
            a.authorization_enforcement,
          );
      }
      return a.asset_type === filter;
    });
    return [...filtered].sort((a, b) => {
      const r = compare(a, b, sort.key);
      return sort.asc ? r : -r;
    });
  }, [data, filter, sort]);

  if (isLoading) {
    return (
      <InventoryShell onRefresh={() => mutate()} refreshing={false}>
        <Skeleton />
      </InventoryShell>
    );
  }
  if (error || !data) {
    return (
      <InventoryShell onRefresh={() => mutate()} refreshing={false}>
        <Card className="p-6 text-sm text-red-700">인벤토리를 불러오지 못했어요.</Card>
      </InventoryShell>
    );
  }

  const { kpi, measurement, overlap_candidates: candidates } = data;

  return (
    <InventoryShell onRefresh={() => mutate()} refreshing={isValidating}>
      <div className="space-y-5">
        {/* ── KPI ─────────────────────────────────────────────── */}
        <section className="grid grid-cols-2 gap-5 xl:grid-cols-4">
          <Kpi label="총 자산" value={kpi.total} note={typeBreakdown(kpi.by_type)} />
          <Kpi label="미스캔" value={kpi.unscanned} cls="text-amber-600"
               note="보안 스캔을 아직 돌리지 않은 자산" />
          <Kpi label="중복 High" value={kpi.overlap_high} cls="text-red-600"
               note="소유자 통합 검토 권장" />
          {/* CA-32: 등록만 되고 Gateway target 이 없는 자산 — 호출 불가인데 다른 열만
              보면 정상으로 보여요. 자동 삭제하지 않고 관리자에게 드러내요. */}
          <Kpi label="Gateway target 없음" value={kpi.gateway_target_missing}
               cls="text-red-600"
               note="등록만 되고 Gateway 연결이 없는 MCP — 호출 불가" />
          <Card className="p-5">
            <div className="flex items-center text-xs font-medium text-muted-foreground">
              총 호출<MeasurementBadge state={measurement.invocations} />
            </div>
            <div className="mt-1 text-4xl font-bold text-slate-300">—</div>
            <div className="mt-1 text-xs text-muted-foreground">
              Evaluation telemetry 구현 후 연결돼요
            </div>
          </Card>
        </section>

        {/* ── 자산 인벤토리 테이블 ─────────────────────────────── */}
        <Card className="overflow-hidden p-0">
          <div className="flex flex-wrap items-center justify-between gap-3 border-b border-border px-5 py-3">
            <div className="text-sm font-semibold">
              자산 인벤토리
              <span className="ml-2 text-xs font-normal text-muted-foreground">
                헤더 클릭 정렬 · 행 클릭 시 심사 상세로
              </span>
            </div>
            <div className="flex gap-1.5">
              {TYPE_FILTERS.map((f) => (
                <button
                  key={f}
                  type="button"
                  onClick={() => setFilter(f)}
                  className={cn(
                    "rounded border px-2.5 py-1 text-xs",
                    filter === f
                      ? "border-transparent bg-foreground font-semibold text-background"
                      : "border-border text-muted-foreground hover:bg-muted",
                  )}
                >
                  {f === "all" ? "전체" : f === "gap" ? "갭만" : f}
                </button>
              ))}
            </div>
          </div>

          <div className="max-h-[420px] overflow-auto">
            <table className="w-full text-sm">
              <thead className="sticky top-0 z-10 bg-muted/60 backdrop-blur">
                <tr className="text-left text-[11px] uppercase tracking-wide text-muted-foreground">
                  <Th sort={sort} setSort={setSort} k="name">자산</Th>
                  <Th sort={sort} setSort={setSort} k="asset_type">유형</Th>
                  <Th sort={sort} setSort={setSort} k="owner_team">소유팀</Th>
                  <th className="px-4 py-2.5 font-semibold">담당자</th>
                  <Th sort={sort} setSort={setSort} k="model">모델</Th>
                  <Th sort={sort} setSort={setSort} k="version">버전</Th>
                  <Th sort={sort} setSort={setSort} k="status">상태</Th>
                  <Th sort={sort} setSort={setSort} k="risk">위험</Th>
                  <Th sort={sort} setSort={setSort} k="authorization_enforcement">
                    인가 강제
                  </Th>
                  <th className="whitespace-nowrap px-4 py-2.5 font-semibold">
                    Gateway target
                  </th>
                  <Th sort={sort} setSort={setSort} k="overlap">중복</Th>
                  <th className="whitespace-nowrap px-4 py-2.5 text-right font-semibold">
                    호출<MeasurementBadge state={measurement.invocations} />
                  </th>
                  <th className="whitespace-nowrap px-4 py-2.5 text-right font-semibold">
                    토큰<MeasurementBadge state={measurement.tokens} />
                  </th>
                  <th className="whitespace-nowrap px-4 py-2.5 font-semibold">
                    품질<MeasurementBadge state={measurement.quality} />
                  </th>
                </tr>
              </thead>
              <tbody>
                {rows.length === 0 ? (
                  <tr>
                    <td colSpan={14}
                        className="px-4 py-8 text-center text-xs text-muted-foreground">
                      조건에 맞는 자산이 없어요
                    </td>
                  </tr>
                ) : (
                  rows.map((a) => (
                    <tr key={a.record_id} className="border-t border-border hover:bg-muted/40">
                      <td className="px-4 py-2.5 font-medium">
                        <Link
                          href={`${ADMIN_QUEUE}/${encodeURIComponent(a.record_id)}`}
                          className="text-blue-700 hover:underline"
                        >
                          {a.name}
                        </Link>
                      </td>
                      <td className="px-4 py-2.5">{a.asset_type || "—"}</td>
                      <td className="px-4 py-2.5">{a.owner_team || <Missing />}</td>
                      <td className="px-4 py-2.5" title={a.owner_user || undefined}>
                        {a.owner_user ? shortPrincipal(a.owner_user) : <Missing />}
                      </td>
                      <td className="px-4 py-2.5">{a.model || "—"}</td>
                      <td className="px-4 py-2.5 tabular-nums">{a.version || "—"}</td>
                      <td className="px-4 py-2.5">{registryStatusLabel(a.status)}</td>
                      <td className="px-4 py-2.5"><RiskCell risk={a.risk} /></td>
                      <td className="px-4 py-2.5"><AuthorizationCell asset={a} /></td>
                      <td className="px-4 py-2.5"><GatewayTargetCell asset={a} /></td>
                      <td className="px-4 py-2.5"><OverlapCell asset={a} /></td>
                      <td className="px-4 py-2.5 text-right"><NotMeasuredCell /></td>
                      <td className="px-4 py-2.5 text-right"><NotMeasuredCell /></td>
                      <td className="px-4 py-2.5"><NotMeasuredCell /></td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
          <div className="border-t border-border px-5 py-2.5 text-[11px] text-muted-foreground">
            전체 {kpi.total}건 중 {rows.length}건 표시 ·
            <span className="ml-1 text-slate-300">—</span> = 측정 미구현(값 없음이 아니에요) ·
            <span className="ml-1 rounded bg-red-50 px-1 text-red-600">비어 있음</span> = 소유 정보 누락
          </div>
        </Card>

        {/* ── 중복 후보 · 미사용 · owner 상태 ───────────────────── */}
        <section className="grid grid-cols-1 gap-5 lg:grid-cols-3">
          <Panel title="중복 후보" hint="Overlap Analysis">
            {candidates.length === 0 ? (
              <div className="px-4 py-8 text-center text-xs text-muted-foreground">
                중복 후보가 없어요
              </div>
            ) : (
              <ul className="divide-y divide-border">
                {candidates.slice(0, 8).map((c) => (
                  <li key={`${c.record_id}:${c.candidate_record_id}`} className="px-4 py-3">
                    <div className="flex items-start justify-between gap-2">
                      <div className="min-w-0">
                        <div className="truncate text-[13px] font-medium">
                          {c.name} ↔ {c.candidate_name || c.candidate_record_id}
                        </div>
                        {c.reasons.length > 0 && (
                          <div className="mt-0.5 truncate text-[11px] text-muted-foreground">
                            {c.reasons.join(" · ")}
                          </div>
                        )}
                      </div>
                      <span className={cn(
                        "shrink-0 rounded px-1.5 py-0.5 text-[11px] font-semibold tabular-nums",
                        c.band === "high"
                          ? "bg-red-50 text-red-600"
                          : "bg-amber-50 text-amber-700",
                      )}>
                        {c.score}%
                      </span>
                    </div>
                    <Link
                      href={`${ADMIN_QUEUE}/${encodeURIComponent(c.record_id)}`}
                      className="mt-1 inline-block text-[11px] font-medium text-blue-700 hover:underline"
                    >
                      상세 →
                    </Link>
                  </li>
                ))}
              </ul>
            )}
          </Panel>

          <Panel title="90일+ 미사용" hint="lifecycle">
            <PendingSourceEmpty blockedBy="사용 추적(telemetry) 구현 후 연결돼요" />
          </Panel>

          <Panel title="owner 상태 변경" hint="인사시스템 연동">
            <PendingSourceEmpty blockedBy="사내 인사시스템 연동 후 연결돼요" />
          </Panel>
        </section>

        {/* ── 비용 · 이상 사용 ─────────────────────────────────── */}
        <section className="grid grid-cols-1 gap-5 lg:grid-cols-2">
          <Panel title="비용 상위 자산" hint="Agent별 · 토큰">
            <PendingSourceEmpty blockedBy="모델 비용 가시성 구현 후 연결돼요" />
          </Panel>
          <Panel title="이상 사용 감지" hint="baseline 대비 급변">
            <PendingSourceEmpty blockedBy="호출 telemetry 구현 후 연결돼요" />
          </Panel>
        </section>
      </div>
    </InventoryShell>
  );
}

// 시안엔 일/주/월 기준기간 세그먼트가 있지만 넣지 않아요. 지금 집계에 시간축이 없어서
// (Registry 현재 상태 스냅샷) 눌러도 값이 안 바뀌고, **동작하지 않는 컨트롤은 데이터가
// 갱신됐다고 오해시켜요** — "측정 미구현" 배지로 정직하게 알리는 것과 정반대예요.
function InventoryShell({ children, onRefresh, refreshing }: {
  children: React.ReactNode; onRefresh: () => void; refreshing: boolean;
}) {
  return (
    <div>
      <div className="mb-6 flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">자산 인벤토리</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            소유·상태·중복 관점의 전 자산 현황이에요. 호출·비용·품질은 측정 구현 후 채워져요.
          </p>
        </div>
        <button
          type="button"
          onClick={onRefresh}
          disabled={refreshing}
          className="shrink-0 rounded-lg border border-border bg-background px-3 py-1.5 text-xs font-semibold hover:bg-muted disabled:opacity-60"
        >
          {refreshing ? "갱신 중…" : "즉시 갱신"}
        </button>
      </div>
      {children}
    </div>
  );
}

function Panel({ title, hint, children }: {
  title: string; hint: string; children: React.ReactNode;
}) {
  return (
    <Card className="overflow-hidden p-0">
      <div className="flex items-center justify-between border-b border-border px-5 py-3">
        <div className="text-sm font-semibold">{title}</div>
        <div className="text-[11px] text-muted-foreground">{hint}</div>
      </div>
      {children}
    </Card>
  );
}

function Kpi({ label, value, cls, note }: {
  label: string; value: number; cls?: string; note?: string;
}) {
  return (
    <Card className="p-5">
      <div className="text-xs font-medium text-muted-foreground">{label}</div>
      <div className={cn("mt-1 text-4xl font-bold tabular-nums", cls)}>{value}</div>
      {note && <div className="mt-1 text-xs text-muted-foreground">{note}</div>}
    </Card>
  );
}

function Th({ children, k, sort, setSort }: {
  children: React.ReactNode; k: SortKey;
  sort: { key: SortKey; asc: boolean };
  setSort: (s: { key: SortKey; asc: boolean }) => void;
}) {
  const on = sort.key === k;
  return (
    <th className="whitespace-nowrap px-4 py-2.5 font-semibold">
      <button
        type="button"
        className={cn("inline-flex items-center gap-1 hover:text-foreground", on && "text-foreground")}
        onClick={() => setSort({ key: k, asc: on ? !sort.asc : true })}
      >
        {children}
        <span className={cn("text-[9px]", on ? "opacity-100" : "opacity-35")}>
          {on && !sort.asc ? "▼" : "▲"}
        </span>
      </button>
    </th>
  );
}

// 소유 정보 누락은 진짜 risk예요(측정 미구현과 달라요) — 등록 시 받았어야 하는 값이니까요.
function Missing() {
  return (
    <span className="rounded bg-red-50 px-1.5 py-0.5 text-xs font-medium text-red-600">
      비어 있음
    </span>
  );
}

function RiskCell({ risk }: { risk: string | null }) {
  // 미스캔을 안전색으로 그리지 않아요 — 스캔 안 한 것과 위험 없음은 다른 상태예요(W3 규칙).
  if (risk === null) {
    return (
      <span className="rounded bg-slate-100 px-1.5 py-0.5 text-xs text-slate-500">미스캔</span>
    );
  }
  const cls =
    risk === "high" ? "bg-red-50 text-red-600"
    : risk === "medium" ? "bg-amber-50 text-amber-700"
    : risk === "low" ? "bg-yellow-50 text-yellow-700"
    : "bg-emerald-50 text-emerald-700";
  return (
    <span className={cn("rounded px-1.5 py-0.5 text-xs font-medium", cls)}>
      {risk.toUpperCase()}
    </span>
  );
}

function AuthorizationCell({ asset }: { asset: InventoryAsset }) {
  const state = asset.authorization_enforcement;
  if (state === "not_applicable") {
    return <span className="text-xs text-slate-400">해당 없음</span>;
  }
  const presentation = {
    enforcing: ["강제 중", "bg-emerald-50 text-emerald-700"],
    log_only: ["LOG ONLY", "bg-amber-50 text-amber-800"],
    unmanaged: ["미관리", "bg-red-50 text-red-700"],
    unknown: ["미관측", "bg-slate-100 text-slate-600"],
  }[state] ?? ["미관측", "bg-slate-100 text-slate-600"];
  return (
    <span
      className={cn("whitespace-nowrap rounded px-1.5 py-0.5 text-xs font-semibold", presentation[1])}
      title={`관측 출처: ${asset.authorization_enforcement_source}`}
    >
      {presentation[0]}
    </span>
  );
}

function GatewayTargetCell({ asset }: { asset: InventoryAsset }) {
  // CA-32 ②: Gateway target 이 없는 자산을 드러내요. 도구 목록·endpoint 가 채워져 있어서
  // 다른 열만 보면 정상 자산처럼 보이지만, target 이 없으면 **호출이 불가능해요**.
  const state = asset.gateway_target_state;
  if (state === "not_applicable") {
    return <span className="text-xs text-slate-400">해당 없음</span>;
  }
  const presentation = {
    ready: ["연결됨", "bg-emerald-50 text-emerald-700"],
    provisioning: ["target 없음", "bg-red-50 text-red-700"],
    unmanaged: ["미관리", "bg-amber-50 text-amber-800"],
    unknown: ["미관측", "bg-slate-100 text-slate-600"],
  }[state] ?? ["미관측", "bg-slate-100 text-slate-600"];
  return (
    <span
      className={cn(
        "whitespace-nowrap rounded px-1.5 py-0.5 text-xs font-semibold",
        presentation[1],
      )}
      title={
        state === "provisioning"
          ? "등록은 됐지만 Gateway 에 target 이 없어요 — 호출할 수 없는 자산이에요. 등록 실패의 잔해일 수 있어요."
          : `Registry descriptor 기준: ${state}`
      }
    >
      {presentation[0]}
    </span>
  );
}

function OverlapCell({ asset }: { asset: InventoryAsset }) {
  // "중복검토 안 함"이 반드시 "중복 없음"과 달라야 해요 — 미검토를 보장으로 오인시키면
  // 안 되니까요(overlap_view의 state 4-enum과 같은 취지).
  if (asset.overlap_state === "not_reviewed") {
    return <span className="text-xs text-slate-400">검토 안 함</span>;
  }
  if (asset.overlap_state === "reviewing") {
    return <span className="text-xs text-blue-600">검토 중</span>;
  }
  if (asset.overlap_state === "failed") {
    return <span className="text-xs text-red-600">검토 실패</span>;
  }
  if (asset.overlap_band === null || asset.overlap_band === "none") {
    return <span className="text-xs text-emerald-700">중복 없음</span>;
  }
  const cls = asset.overlap_band === "high"
    ? "bg-red-50 text-red-600"
    : "bg-amber-50 text-amber-700";
  return (
    <span className={cn("rounded px-1.5 py-0.5 text-xs font-semibold tabular-nums", cls)}>
      {asset.overlap_top_score ?? 0}%
    </span>
  );
}

function Skeleton() {
  return (
    <div className="space-y-5">
      <div className="grid grid-cols-2 gap-5 xl:grid-cols-4">
        {Array.from({ length: 4 }).map((_, i) => (
          <div key={i} className="h-28 animate-pulse rounded-xl bg-muted/50" />
        ))}
      </div>
      <div className="h-80 animate-pulse rounded-xl bg-muted/50" />
    </div>
  );
}

// Cognito sub는 UUID라 그대로 보이면 읽을 수 없어요. 앞 8자만 보이고 hover로 전체를 봐요.
// (owner_email 배선 후에는 이메일이 우선이고 이건 폴백이에요.)
function shortPrincipal(value: string): string {
  return value.length > 12 ? `${value.slice(0, 8)}…` : value;
}

function typeBreakdown(byType: Record<string, number>): string {
  const parts = Object.entries(byType)
    .filter(([type]) => type)
    .sort((a, b) => b[1] - a[1])
    .map(([type, count]) => `${type} ${count}`);
  return parts.length > 0 ? parts.join(" · ") : "자산 없음";
}

function compare(a: InventoryAsset, b: InventoryAsset, key: SortKey): number {
  if (key === "risk") {
    // 미스캔(null)을 5로 둬서 위험도 정렬에서 맨 위로 와요 — 미스캔은 "위험 없음"보다
    // 먼저 봐야 하는 상태예요.
    const rank = (r: string | null) => (r === null ? 5 : RISK_ORDER[r] ?? 0);
    return rank(a.risk) - rank(b.risk);
  }
  if (key === "overlap") {
    const rank = (x: InventoryAsset) =>
      x.overlap_state === "reviewed" ? BAND_ORDER[x.overlap_band ?? "none"] ?? 0 : -1;
    return rank(a) - rank(b);
  }
  return String(a[key] ?? "").localeCompare(String(b[key] ?? ""), "ko");
}
