"use client";

import { useState } from "react";
import useSWR from "swr";
import {
  getGovMe,
  getGovTier,
  listGovTools,
  type GovMe,
  type GovTool,
  type TierCell,
} from "@/lib/api";
import { TIERS } from "@/lib/governance";
import { Card } from "@/components/ui/card";
import { cn } from "@/lib/ui";
import { ToolRegistryTable } from "./ToolRegistryTable";
import { TierMatrix } from "./TierMatrix";
import { McpToolDriftPanel } from "./McpToolDriftPanel";

// "MCP 도구 · 민감도" 는 스캐너 도구 레지스트리와 다른 대상이에요(전자는 자산이 제공하는
// 도구, 후자는 우리가 돌리는 검사 도구). 메뉴를 새로 만들지 않고 탭으로 붙인 이유는
// 관리자가 "도구" 를 찾을 때 들어오는 자리가 이미 여기라서예요.
type Tab = "registry" | "tiers" | "mcp-drift";

// 거버넌스 도구 화면 오케스트레이터. 도구 목록은 AWS 실배포+코드 상수 병합(읽기 전용),
// 등급별 구성(tiers 탭)만 admin이 편집해요.
export function ToolsClient() {
  const [tab, setTab] = useState<Tab>("registry");

  const { data: me } = useSWR<GovMe>("gov/me", getGovMe);
  const {
    data: toolsResp,
    error: toolsError,
    isLoading,
  } = useSWR<{ tools: GovTool[] }>("gov/tools", listGovTools);

  const { data: tiers, mutate: refetchTiers } = useSWR<{ tier: string; cells: TierCell[] }[]>(
    "gov/tiers",
    () => Promise.all(TIERS.map((t) => getGovTier(t.value))),
  );

  const tools = toolsResp?.tools ?? [];
  const canTierWrite = me?.can.tier_write ?? false;
  const tierVersion = (tiers ?? []).reduce((n, t) => n + t.cells.length, 0);

  const cellsByTier: Record<string, TierCell[]> = {};
  const tierByTool: Record<string, string[]> = {};
  for (const entry of tiers ?? []) {
    cellsByTier[entry.tier] = entry.cells;
    const label = TIERS.find((t) => t.value === entry.tier)?.label ?? entry.tier;
    for (const cell of entry.cells) {
      if (cell.enforcement === "off") continue;
      (tierByTool[cell.tool_id] ??= []).push(label);
    }
  }

  return (
    <div>
      <div className="mb-5">
        <h1 className="text-2xl font-bold tracking-tight">거버넌스 도구</h1>
      </div>

      <div className="mb-6 flex gap-1 border-b border-border">
        <TabButton active={tab === "registry"} onClick={() => setTab("registry")}>
          도구 레지스트리
        </TabButton>
        <TabButton active={tab === "tiers"} onClick={() => setTab("tiers")}>
          등급별 구성
        </TabButton>
        <TabButton active={tab === "mcp-drift"} onClick={() => setTab("mcp-drift")}>
          MCP 도구 · 민감도
        </TabButton>
      </div>

      {tab === "mcp-drift" ? (
        <McpToolDriftPanel />
      ) : isLoading ? (
        <TableSkeleton />
      ) : toolsError ? (
        <Card className="p-6 text-sm text-red-700">
          도구 목록을 불러오지 못했어요. API 서버(:9100)가 떠 있는지 확인해 주세요.
        </Card>
      ) : tab === "registry" ? (
        <ToolRegistryTable tools={tools} tierByTool={tierByTool} />
      ) : (
        <TierMatrix
          key={`tiers-${tierVersion}`}
          tools={tools}
          cellsByTier={cellsByTier}
          canWrite={canTierWrite}
          onSaved={() => refetchTiers()}
        />
      )}
    </div>
  );
}

function TabButton({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      onClick={onClick}
      className={cn(
        "-mb-px border-b-2 px-4 py-2.5 text-sm font-medium transition-colors",
        active
          ? "border-blue-600 text-blue-700"
          : "border-transparent text-muted-foreground hover:text-foreground",
      )}
    >
      {children}
    </button>
  );
}

function TableSkeleton() {
  return (
    <div className="space-y-2">
      {Array.from({ length: 6 }).map((_, i) => (
        <div key={i} className="h-12 animate-pulse rounded-lg bg-muted/50" />
      ))}
    </div>
  );
}
