"use client";

import { useMemo, useState } from "react";
import Link from "next/link";
import useSWR from "swr";
import { listGovDecisions, type DecisionEntry } from "@/lib/api";
import { ADMIN_QUEUE } from "@/lib/adminNav";
import { Card } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/ui";

// 감사 로그 (§3-M6) — 판정 이력(누가·언제·무엇을·override 사유). append-only, 읽기 전용.
export function AuditClient() {
  const { data, error, isLoading } = useSWR<{ items: DecisionEntry[] }>(
    "gov/decisions", () => listGovDecisions(),
  );

  const [principal, setPrincipal] = useState("");
  const [record, setRecord] = useState("");

  const items = useMemo(() => data?.items ?? [], [data]);
  const filtered = useMemo(() => {
    const p = principal.trim().toLowerCase();
    const r = record.trim().toLowerCase();
    return items.filter((it) => {
      if (p && !it.principal.toLowerCase().includes(p)) return false;
      if (r && !it.record_id.toLowerCase().includes(r)) return false;
      return true;
    });
  }, [items, principal, record]);

  return (
    <div>
      <div className="mb-5">
        <h1 className="text-2xl font-bold tracking-tight">심사 로그</h1>
      </div>

      <div className="mb-4 flex flex-wrap items-center gap-3">
        <div className="w-52"><Input placeholder="principal 필터" value={principal} onChange={(e) => setPrincipal(e.target.value)} /></div>
        <div className="w-52"><Input placeholder="자산(record) 필터" value={record} onChange={(e) => setRecord(e.target.value)} /></div>
      </div>

      {isLoading ? (
        <div className="space-y-2">{Array.from({ length: 5 }).map((_, i) => <div key={i} className="h-12 animate-pulse rounded-lg bg-muted/50" />)}</div>
      ) : error ? (
        <Card className="p-6 text-sm text-red-700">감사 이력을 불러오지 못했어요.</Card>
      ) : filtered.length === 0 ? (
        <Card className="p-12 text-center text-muted-foreground">
          {items.length === 0 ? "아직 판정 이력이 없어요. 승인 큐에서 자산을 판정하면 여기 기록돼요." : "조건에 맞는 기록이 없어요."}
        </Card>
      ) : (
        <div className="overflow-x-auto rounded-xl border border-border">
          <table className="w-full min-w-[720px] border-collapse text-sm">
            <thead>
              <tr className="border-b border-border bg-muted/40 text-left text-xs text-muted-foreground">
                <Th>시각 (KST)</Th><Th>판정자</Th><Th>결정</Th><Th>자산</Th><Th>사유 / override</Th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((it, i) => (
                <tr key={i} className="border-b border-border last:border-0 hover:bg-accent/40">
                  <Td className="whitespace-nowrap text-muted-foreground">{formatTs(it.ts)}</Td>
                  <Td className="font-medium">{it.principal}</Td>
                  <Td>
                    <Badge variant="type" className={it.decision === "APPROVE" ? "bg-emerald-100 text-emerald-800" : "bg-red-100 text-red-800"}>
                      {it.decision}
                    </Badge>
                  </Td>
                  <Td>
                    <Link href={`${ADMIN_QUEUE}/${encodeURIComponent(it.record_id)}`} className="text-[13px] font-medium text-blue-700 hover:underline">
                      {it.name || it.record_id}
                    </Link>
                  </Td>
                  <Td>
                    {it.override && <Badge variant="type" className="mr-1.5 bg-amber-100 text-amber-800 text-[11px]">override</Badge>}
                    <span className={cn("text-[13px]", it.reason ? "text-foreground" : "text-muted-foreground")}>
                      {it.reason || "—"}
                    </span>
                  </Td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

// ISO(UTC) → KST 표기 (memory: 모든 시간 KST).
function formatTs(iso: string): string {
  try {
    return new Date(iso).toLocaleString("ko-KR", { timeZone: "Asia/Seoul", dateStyle: "short", timeStyle: "short" });
  } catch { return iso; }
}
function Th({ children }: { children: React.ReactNode }) {
  return <th className="px-4 py-2.5 font-medium">{children}</th>;
}
function Td({ children, className }: { children: React.ReactNode; className?: string }) {
  return <td className={cn("px-4 py-3 align-middle", className)}>{children}</td>;
}
