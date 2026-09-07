"use client";

import { useMemo, useState } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import useSWR from "swr";
import { listGovQueue, type QueueItem } from "@/lib/api";
import { ADMIN_QUEUE } from "@/lib/adminNav";
import { tierLabel, registryStatusLabel, registryStatusBadgeClass } from "@/lib/governance";
import { Card } from "@/components/ui/card";
import { Input, Select } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/ui";
import { GateProgressBar, RiskBadge } from "./gateBits";
import { ApprovalBlockNotice } from "@/components/catalog/ApprovalBlockNotice";
import { ScanRunButton } from "./ScanRunButton";
import { ThreatReportButton } from "./ThreatReportButton";
import { scanRunAvailability } from "@/lib/assetDetailPresentation";

// 정렬 대상 열 키와 방향.
type SortKey = "name" | "status" | "target_tier" | "risk" | "updated_at";
type SortState = { key: SortKey; dir: "asc" | "desc" };

// 논리적 순서 map — 문자열이 아니라 의미 순서로 비교해요.
const TIER_ORDER: Record<string, number> = { minimal: 0, standard: 1, strong: 2 };
const RISK_ORDER: Record<string, number> = { none: 0, low: 1, medium: 2, high: 3 };
const STATUS_ORDER: Record<string, number> = {
  DRAFT: 0, PENDING_APPROVAL: 1, APPROVED: 2, REJECTED: 3, DEPRECATED: 4,
};

// 열별 정렬 비교값. 등록일자·자산명은 문자열, 나머지는 순서 map(미지값은 뒤로).
function sortValue(it: QueueItem, key: SortKey): number | string {
  switch (key) {
    case "name": return it.name.toLowerCase();
    case "updated_at": return it.updated_at ?? "";
    case "status": return STATUS_ORDER[it.status] ?? 99;
    case "target_tier": return TIER_ORDER[it.target_tier] ?? 99;
    case "risk": return RISK_ORDER[it.risk ?? "unscanned"] ?? -1; // 미스캔은 최하위
  }
}

// 승인 큐 (§3-M1) — 검토 대기 자산 목록 + 진행상태 + 검출 위협 + 수동 스캔.
export function QueueClient() {
  const { data, error, isLoading, mutate } = useSWR<{ items: QueueItem[] }>(
    "gov/queue", listGovQueue,
    // 스캔이 진행 중(running)인 자산이 있으면 짧게 폴링해 running→done 전이를 반영해요.
    { refreshInterval: (d) => (d?.items?.some((it) => it.scan_status === "running") ? 3000 : 0) },
  );

  // 대시보드 드릴다운(?tier=)이 초기 필터로 들어와요.
  const searchParams = useSearchParams();
  const [tierFilter, setTierFilter] = useState(searchParams.get("tier") ?? "");
  const [riskFilter, setRiskFilter] = useState("");
  const [scanFilter, setScanFilter] = useState("");
  const [q, setQ] = useState("");
  // 기본 정렬은 기존과 동일: 등록일자(updated_at) 역순.
  const [sort, setSort] = useState<SortState>({ key: "updated_at", dir: "desc" });

  const items = useMemo(() => data?.items ?? [], [data]);
  const filtered = useMemo(() => {
    const query = q.trim().toLowerCase();
    return items.filter((it) => {
      if (tierFilter && it.target_tier !== tierFilter) return false;
      if (riskFilter && (it.risk ?? "unscanned") !== riskFilter) return false;
      if (scanFilter === "scanned" && !it.scanned) return false;
      if (scanFilter === "unscanned" && it.scanned) return false;
      if (query && !it.name.toLowerCase().includes(query)) return false;
      return true;
    });
  }, [items, tierFilter, riskFilter, scanFilter, q]);

  // 필터 결과에 정렬을 적용해요(폴링·낙관적 갱신은 items→filtered 단계에서 그대로 유지).
  const sorted = useMemo(() => {
    const rows = [...filtered];
    rows.sort((a, b) => {
      const av = sortValue(a, sort.key);
      const bv = sortValue(b, sort.key);
      const cmp = av < bv ? -1 : av > bv ? 1 : 0;
      return sort.dir === "asc" ? cmp : -cmp;
    });
    return rows;
  }, [filtered, sort]);

  // 헤더 클릭: 같은 열이면 방향 토글, 다른 열이면 그 열 오름차순부터.
  const toggleSort = (key: SortKey) =>
    setSort((cur) => (cur.key === key ? { key, dir: cur.dir === "asc" ? "desc" : "asc" } : { key, dir: "asc" }));

  const pending = items.filter((it) => it.progress.verdict === "pending").length;

  return (
    <div>
      <div className="mb-5">
        <h1 className="text-2xl font-bold tracking-tight">승인 큐</h1>
      </div>

      {/* 필터 */}
      <div className="mb-4 flex flex-wrap items-center gap-3">
        <FilterSelect label="등급" value={tierFilter} onChange={setTierFilter}
          options={[["", "전체"], ["minimal", "최소"], ["standard", "표준"], ["strong", "강화"]]} />
        <FilterSelect label="위험" value={riskFilter} onChange={setRiskFilter}
          options={[["", "전체"], ["high", "HIGH"], ["medium", "MED"], ["low", "LOW"], ["none", "NONE"], ["unscanned", "미스캔"]]} />
        <FilterSelect label="스캔" value={scanFilter} onChange={setScanFilter}
          options={[["", "전체"], ["scanned", "스캔됨"], ["unscanned", "미스캔"]]} />
        <div className="ml-auto w-56">
          <Input placeholder="자산 검색" value={q} onChange={(e) => setQ(e.target.value)} />
        </div>
      </div>

      {isLoading ? (
        <TableSkeleton />
      ) : error ? (
        <Card className="p-6 text-sm text-red-700">
          큐를 불러오지 못했어요. API 서버(:9100)가 떠 있는지 확인해 주세요.
        </Card>
      ) : filtered.length === 0 ? (
        <Card className="p-12 text-center text-muted-foreground">
          {items.length === 0 ? "검토 대기 자산이 없어요." : "조건에 맞는 자산이 없어요."}
        </Card>
      ) : (
        <div>
          {/* 전체 건수 — 테이블 우측 상단. */}
          <div className="mb-2 text-right text-sm text-muted-foreground">
            전체 {items.length}건 · 검토중 {pending}건.
          </div>
          <div className="overflow-x-auto rounded-xl border border-border">
          <table className="w-full min-w-[980px] border-collapse text-sm">
            <thead>
              <tr className="border-b border-border bg-muted/40 text-left text-xs text-muted-foreground">
                <SortTh label="자산" col="name" sort={sort} onSort={toggleSort} />
                <Th>타입</Th>
                <SortTh label="상태" col="status" sort={sort} onSort={toggleSort} />
                <SortTh label="목표등급" col="target_tier" sort={sort} onSort={toggleSort} />
                <Th>진행상태</Th>
                <SortTh label="위험" col="risk" sort={sort} onSort={toggleSort} />
                <Th>검출 위협</Th>
                <SortTh label="등록일자" col="updated_at" sort={sort} onSort={toggleSort} />
                <Th className="text-right">스캔</Th>
              </tr>
            </thead>
            <tbody>
              {sorted.map((it) => {
                const scanAvailability = scanRunAvailability(it.scan_applicability);
                return (
                  <tr key={it.record_id} className="border-b border-border last:border-0 hover:bg-accent/40">
                  <Td>
                    <Link
                      href={`${ADMIN_QUEUE}/${encodeURIComponent(it.record_id)}`}
                      className="font-semibold text-blue-700 hover:underline"
                    >
                      {it.name}
                    </Link>
                    <div className="text-[11px] text-muted-foreground">{it.owner_user?.split("/").pop() || ""}</div>
                  </Td>
                  <Td className="text-muted-foreground">{it.descriptor_type}</Td>
                  <Td>
                    <Badge variant="type" className={registryStatusBadgeClass(it.status)}>
                      {registryStatusLabel(it.status)}
                    </Badge>
                  </Td>
                  <Td><Badge variant="outline">{tierLabel(it.target_tier)}</Badge></Td>
                  <Td>
                    {it.scan_status === "running"
                      ? <span className="inline-flex items-center gap-1.5 text-xs text-blue-600">
                          <span className="inline-block h-2 w-2 animate-pulse rounded-full bg-blue-500" />스캔 진행 중…
                        </span>
                      : <GateProgressBar progress={it.progress} scanned={it.scanned} />}
                    {/* 자동승인이 안 된 이유 + 관리자가 무엇을 하면 풀리는지(R3). 진행상태만
                        보여주면 "자동 APPROVE인데 심사중"인 상태의 원인을 알 수 없어요. */}
                    <ApprovalBlockNotice block={it.approval_block} audience="admin" compact />
                  </Td>
                  <Td>{it.scan_status === "running" ? <span className="text-xs text-muted-foreground">—</span> : <RiskBadge risk={it.risk} />}</Td>
                  <Td>
                    {it.threats.length === 0 ? (
                      <span className="text-muted-foreground">—</span>
                    ) : (
                      <div className="flex flex-col gap-1.5">
                        <div className="flex flex-wrap gap-1">
                          {it.threats.map((t) => (
                            <Badge key={t.code} variant="type" className="bg-red-50 text-red-700 text-[11px]">
                              {t.code}×{t.count}
                            </Badge>
                          ))}
                        </div>
                        <ThreatReportButton recordId={it.record_id} assetName={it.name} variant="ghost" />
                      </div>
                    )}
                  </Td>
                  <Td className="whitespace-nowrap text-[12px] text-muted-foreground">{fmtDateOnly(it.updated_at)}</Td>
                  <Td className="text-right">
                    <ScanRunButton recordId={it.record_id} scanned={it.scanned}
                      serverScanning={it.scan_status === "running"}
                      disabledReason={scanAvailability.enabled ? "" : scanAvailability.reason}
                      trigger="manual-queue"
                      // 클릭 즉시 해당 자산의 scan_status를 running으로 낙관적 반영 →
                      // 진행상태 컬럼이 서버 폴링(≤3s)을 기다리지 않고 바로 "스캔 진행 중…"으로 바뀌어요.
                      // revalidate:false로 낙관적 상태를 폴링이 확정할 때까지 유지해요.
                      onStart={() =>
                        mutate(
                          (cur) => cur && {
                            items: cur.items.map((x) =>
                              x.record_id === it.record_id
                                ? { ...x, scan_status: "running" as const }
                                : x,
                            ),
                          },
                          { revalidate: false },
                        )
                      }
                      onDone={() => mutate()} />
                  </Td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          </div>
        </div>
      )}
    </div>
  );
}

function FilterSelect({
  label, value, onChange, options,
}: {
  label: string; value: string; onChange: (v: string) => void; options: [string, string][];
}) {
  return (
    <div className="flex items-center gap-2">
      <span className="text-sm text-muted-foreground">{label}</span>
      <Select value={value} onChange={(e) => onChange(e.target.value)}>
        {options.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
      </Select>
    </div>
  );
}

// 등록/갱신일자 표시 — KST 기준 날짜만(YYYY-MM-DD, 시각 제외). 값 없으면 —.
// 표준 ISO 타임스탬프면 KST 날짜를 표시하고, 혹시 파싱 안 되는 값이 와도
// 앞 날짜(YYYY-MM-DD)만이라도 보여주는 방어적 폴백을 둬요.
function fmtDateOnly(iso: string): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (!isNaN(d.getTime())) {
    return new Intl.DateTimeFormat("ko-KR", {
      timeZone: "Asia/Seoul", year: "numeric", month: "2-digit", day: "2-digit",
    }).format(d).replace(/\. /g, "-").replace(/\.$/, "");
  }
  const m = iso.match(/^(\d{4}-\d{2}-\d{2})/);   // 파싱 불가 → 날짜 부분만
  return m ? m[1] : "—";
}

function Th({ children, className }: { children: React.ReactNode; className?: string }) {
  return <th className={cn("px-4 py-2.5 font-medium", className)}>{children}</th>;
}

// 클릭 정렬 가능한 헤더 셀 — 정렬 중인 열에 방향 화살표(↑/↓)를 붙여요.
function SortTh({
  label, col, sort, onSort, className,
}: {
  label: string; col: SortKey; sort: SortState; onSort: (k: SortKey) => void; className?: string;
}) {
  const active = sort.key === col;
  return (
    <th className={cn("px-4 py-2.5 font-medium", className)}>
      <button
        type="button"
        onClick={() => onSort(col)}
        className={cn(
          "inline-flex items-center gap-1 hover:text-foreground",
          active && "text-foreground",
        )}
      >
        {label}
        <span className="text-[10px]">{active ? (sort.dir === "asc" ? "↑" : "↓") : ""}</span>
      </button>
    </th>
  );
}
function Td({ children, className }: { children: React.ReactNode; className?: string }) {
  return <td className={cn("px-4 py-3 align-middle", className)}>{children}</td>;
}
function TableSkeleton() {
  return (
    <div className="space-y-2">
      {Array.from({ length: 6 }).map((_, i) => (
        <div key={i} className="h-14 animate-pulse rounded-lg bg-muted/50" />
      ))}
    </div>
  );
}
