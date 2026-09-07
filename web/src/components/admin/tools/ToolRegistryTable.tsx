"use client";

import { useMemo, useState } from "react";
import { type GovTool } from "@/lib/api";
import {
  AREA_OPTIONS,
  areaLabel,
  assetTypeLabel,
  statusLabel,
} from "@/lib/governance";
import { Badge } from "@/components/ui/badge";
import { Input, Select } from "@/components/ui/input";
import { cn } from "@/lib/ui";

// 도구 레지스트리 목록 테이블 (§3-M3 ToolRegistryTable + 필터).
// 어느 등급에서 이 도구를 쓰는지(적용등급)는 tierByTool 맵으로 받아요(등급 매트릭스 join).
interface ToolRegistryTableProps {
  tools: GovTool[];
  /** tool_id → 이 도구가 등장하는 등급 한글 라벨 배열 (예: ["최소","표준"]). */
  tierByTool: Record<string, string[]>;
}

export function ToolRegistryTable({ tools, tierByTool }: ToolRegistryTableProps) {
  const [areaFilter, setAreaFilter] = useState("");
  const [statusFilter, setStatusFilter] = useState("");
  const [q, setQ] = useState("");

  const filtered = useMemo(() => {
    const query = q.trim().toLowerCase();
    return tools.filter((t) => {
      if (areaFilter && t.area !== areaFilter) return false;
      if (statusFilter && t.status !== statusFilter) return false;
      if (query && !t.name.toLowerCase().includes(query) && !t.tool_id.includes(query))
        return false;
      return true;
    });
  }, [tools, areaFilter, statusFilter, q]);

  const activeCount = tools.filter((t) => t.status === "active").length;
  const stagedCount = tools.length - activeCount;

  return (
    <div>
      {/* 필터 바 */}
      <div className="mb-4 flex flex-wrap items-center gap-3">
        <div className="flex items-center gap-2">
          <span className="text-sm text-muted-foreground">영역</span>
          <Select value={areaFilter} onChange={(e) => setAreaFilter(e.target.value)}>
            <option value="">전체</option>
            {AREA_OPTIONS.map((a) => (
              <option key={a.value} value={a.value}>{a.label}</option>
            ))}
          </Select>
        </div>
        <div className="flex items-center gap-2">
          <span className="text-sm text-muted-foreground">상태</span>
          <Select value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)}>
            <option value="">전체</option>
            <option value="active">활성</option>
            <option value="staged">준비</option>
          </Select>
        </div>
        <div className="ml-auto w-56">
          <Input
            placeholder="도구 검색"
            value={q}
            onChange={(e) => setQ(e.target.value)}
          />
        </div>
      </div>

      {/* 테이블 — 좁은 화면에서는 가로 스크롤(잘림 방지) */}
      <div className="overflow-x-auto rounded-xl border border-border">
        <table className="w-full min-w-[720px] border-collapse text-sm">
          <thead>
            <tr className="border-b border-border bg-muted/40 text-left text-xs text-muted-foreground">
              <Th>도구</Th>
              <Th>영역</Th>
              <Th>버전</Th>
              <Th>라이선스</Th>
              <Th>대상 자산</Th>
              <Th>상태</Th>
              <Th>적용등급</Th>
              <Th>배포</Th>
            </tr>
          </thead>
          <tbody>
            {filtered.length === 0 ? (
              <tr>
                <td colSpan={8} className="px-4 py-12 text-center text-muted-foreground">
                  조건에 맞는 도구가 없어요.
                </td>
              </tr>
            ) : (
              filtered.map((t) => (
                <tr
                  key={t.tool_id}
                  className="border-b border-border last:border-0 hover:bg-accent/40"
                >
                  <Td>
                    <div className="font-semibold text-foreground">{t.name}</div>
                    <div className="font-mono text-[11px] text-muted-foreground">
                      {t.tool_id}
                    </div>
                  </Td>
                  <Td className="text-muted-foreground">{areaLabel(t.area)}</Td>
                  <Td className="font-mono text-[13px]">{t.current_version || "—"}</Td>
                  <Td className="text-muted-foreground">{t.license}</Td>
                  <Td>
                    <div className="flex flex-wrap gap-1">
                      {t.target_asset_types.length === 0 ? (
                        <span className="text-muted-foreground">—</span>
                      ) : (
                        t.target_asset_types.map((at) => (
                          <Badge key={at} variant="outline" className="text-[11px]">
                            {assetTypeLabel(at)}
                          </Badge>
                        ))
                      )}
                    </div>
                  </Td>
                  <Td>
                    <Badge
                      variant="type"
                      className={cn(
                        t.status === "active"
                          ? "bg-emerald-100 text-emerald-800"
                          : "bg-slate-100 text-slate-600",
                      )}
                    >
                      {statusLabel(t.status)}
                    </Badge>
                  </Td>
                  <Td>
                    <div className="flex flex-wrap gap-1">
                      {(tierByTool[t.tool_id] ?? []).length === 0 ? (
                        <span className="text-muted-foreground">—</span>
                      ) : (
                        (tierByTool[t.tool_id] ?? []).map((label) => (
                          <Badge key={label} variant="outline" className="text-[11px]">
                            {label}
                          </Badge>
                        ))
                      )}
                    </div>
                  </Td>
                  <Td>
                    {t.deployed ? (
                      <Badge variant="type" className="bg-emerald-100 text-emerald-800">
                        배포됨
                      </Badge>
                    ) : (
                      <Badge variant="type" className="bg-slate-100 text-slate-500">
                        미배포
                      </Badge>
                    )}
                  </Td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>

      <p className="mt-3 text-xs text-muted-foreground">
        {tools.length}개 도구 · 활성 {activeCount} · 준비 {stagedCount}.
      </p>
    </div>
  );
}

function Th({ children, className }: { children: React.ReactNode; className?: string }) {
  return <th className={cn("px-4 py-2.5 font-medium", className)}>{children}</th>;
}

function Td({ children, className }: { children: React.ReactNode; className?: string }) {
  return <td className={cn("px-4 py-3 align-middle", className)}>{children}</td>;
}
