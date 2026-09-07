"use client";

import { useState } from "react";
import useSWR from "swr";
import {
  TOOL_DRIFT_SWR_KEY,
  getGovInventory,
  listMcpToolDrift,
  listMcpToolSensitivityHistory,
  resyncMcpToolDrift,
  type AssetToolDrift,
  type GatewayTargetQuota,
  type Inventory,
  type SensitivityHistoryEvent,
  type ToolDriftEntry,
} from "@/lib/api";
import {
  DRIFT_STATE_MEANING,
  DRIFT_STATE_TONE,
  SENSITIVITY_SOURCE_LABEL,
  SENSITIVITY_SOURCE_MEANING,
  SENSITIVITY_SOURCE_TONE,
  callabilityText,
  describeDiff,
  driftBanner,
  editability,
  historyChangeText,
  historyStageText,
  nextActionText,
  quotaPresentation,
  sensitivityChangeStatusText,
  sortByUrgency,
  targetPresentation,
} from "@/lib/mcpToolDrift";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { cn } from "@/lib/ui";
import { SensitivityEditDialog } from "./SensitivityEditDialog";

/**
 * MCP 도구 · 민감도 (LC-03).
 *
 * `tools/list`는 등록 시점에 한 번만 떠오고 재동기화 경로가 없었어요 — MCP 가 도구를
 * 추가·수정·삭제해도 Agora 는 몰랐고, 화면에도 아무 신호가 없었어요. 이 패널이 그
 * 어긋남을 드러내요.
 *
 * 관측·표시에 더해 민감도 태그 확정과 방향별 승인 기록이 여기서 일어나요.
 * Target 열은 태그 기반 예상값만 표시하고, 실제 적용 대조와 Cedar 정책 재컴파일은
 * 별도 거버넌스 경로가 소유해요.
 */
export function McpToolDriftPanel() {
  const { data, error, isLoading, mutate } = useSWR<{ assets: AssetToolDrift[] }>(
    TOOL_DRIFT_SWR_KEY,
    listMcpToolDrift,
  );
  const { data: inventory, error: quotaError } = useSWR<Inventory>(
    "gov/inventory",
    getGovInventory,
    { revalidateOnFocus: true, refreshInterval: 60_000 },
  );
  const [busy, setBusy] = useState<string | null>(null);
  const [failed, setFailed] = useState<Record<string, string>>({});

  async function resync(recordId: string) {
    setBusy(recordId);
    setFailed((prev) => {
      const next = { ...prev };
      delete next[recordId];
      return next;
    });
    try {
      const fresh = await resyncMcpToolDrift(recordId);
      // 서버가 돌려준 최신 스냅샷으로 그 자산만 바꿔치기해요.
      await mutate(
        (current) =>
          current
            ? {
                assets: current.assets.map((a) =>
                  a.record_id === recordId ? fresh : a,
                ),
              }
            : current,
        { revalidate: false },
      );
    } catch (e) {
      // 재조회 실패를 조용히 삼키면 "눌렀는데 아무 일도 안 났다"가 돼요.
      setFailed((prev) => ({
        ...prev,
        [recordId]: e instanceof Error ? e.message : "다시 읽기에 실패했어요.",
      }));
    } finally {
      setBusy(null);
    }
  }

  if (isLoading) {
    return (
      <div className="space-y-2">
        {Array.from({ length: 3 }).map((_, i) => (
          <div key={i} className="h-24 animate-pulse rounded-lg bg-muted/50" />
        ))}
      </div>
    );
  }
  if (error) {
    return (
      <Card className="p-6 text-sm text-red-700">
        도구 드리프트 상태를 불러오지 못했어요. API 서버(:9100)가 떠 있는지, admin 권한인지
        확인해 주세요.
      </Card>
    );
  }

  const assets = sortByUrgency(data?.assets ?? []);
  if (assets.length === 0) {
    return (
      <Card className="p-6 text-sm text-muted-foreground">
        등록된 MCP 자산이 없어요.
      </Card>
    );
  }

  return (
    <div className="space-y-6">
      <QuotaStatus
        quota={inventory?.gateway_target_quota}
        failed={Boolean(quotaError)}
      />
      <Legend />
      {assets.map((asset) => (
        <AssetSection
          key={asset.asset_key || asset.record_id}
          asset={asset}
          busy={busy === asset.record_id}
          failure={failed[asset.record_id]}
          onResync={() => resync(asset.record_id)}
          onAssetChange={(fresh) =>
            mutate(
              (current) =>
                current
                  ? {
                      assets: current.assets.map((a) =>
                        a.record_id === fresh.record_id ? fresh : a,
                      ),
                    }
                  : current,
              { revalidate: false },
            )
          }
          onRefresh={() => mutate()}
        />
      ))}
    </div>
  );
}

function QuotaStatus({
  quota,
  failed,
}: {
  quota?: GatewayTargetQuota;
  failed: boolean;
}) {
  if (!quota && !failed) {
    return (
      <div className="border-y border-border bg-muted/30 px-4 py-3 text-xs text-muted-foreground">
        Target 쿼터를 읽는 중…
      </div>
    );
  }
  const shown = quotaPresentation(
    quota ?? {
      status: "unknown",
      current: null,
      quota: null,
      usage_ratio: null,
      threshold_ratio: 0.7,
      threshold_reached: null,
      reason: "쿼터 관측 응답을 불러오지 못했어요",
      sources: {
        current: "ListGatewayTargets",
        quota: "ServiceQuotas.GetServiceQuota",
      },
    },
  );
  const tone = {
    ok: "border-emerald-200 bg-emerald-50 text-emerald-900",
    warning: "border-amber-300 bg-amber-50 text-amber-900",
    unknown: "border-slate-300 bg-slate-50 text-slate-700",
  }[shown.tone];
  return (
    <div className={cn("border-y px-4 py-3", tone)}>
      <p className="text-xs font-semibold">{shown.label}</p>
      <p className="mt-0.5 text-xs opacity-90">{shown.detail}</p>
    </div>
  );
}

function Legend() {
  return (
    <Card className="p-4">
      <p className="mb-2 text-xs font-semibold text-foreground">
        Target 변경은 전파 대기
      </p>
      <p className="mb-3 text-xs text-muted-foreground">
        민감도 승인 기록은 남지만 배포된 Target 구성은 IA-68 전파 경로가 준비될 때까지
        바뀌지 않아요. 배포형 미분류 도구는 어떤 Target에도 들어가지 않아 호출할 수
        없어요.
      </p>
      <p className="mb-3 text-xs text-muted-foreground">
        상류에서 사라진 연결형 도구는 Gateway Target을 명시 동기화하고 완료와 후속
        부재를 확인해 정리해요. 연결형 신규 도구의 실제 호출 가능성은 확인 전까지
        알 수 없고, 배포형 도구 제거는 IA-68에서 진행해요.
      </p>
      <p className="mb-3 text-xs text-muted-foreground">
        배포형 MISSING 도구는 Target에 남아 호출 가능성이 확인되지 않았고, per-agent
        정책이 남은 Gateway에서는 정책 갱신도 막힐 수 있어요. IA-53 컷오버 상태를
        확인한 뒤 IA-68에서 제거해요.
      </p>
      <p className="mb-3 text-xs text-muted-foreground">
        Target 열은 민감도 태그에서 계산한 읽기 전용 예상값이에요. 실제 Gateway 적용
        여부는 정책 화면의 실물 대조 결과로 확인해요.
      </p>
      <div className="flex flex-wrap gap-x-4 gap-y-1.5">
        {(
          ["DISCOVERED", "ACTIVE", "CHANGED", "MISSING", "RETIRED"] as const
        ).map((state) => (
          <span key={state} className="flex items-center gap-1.5 text-[11px]">
            <Badge variant="type" className={cn("text-[10px]", DRIFT_STATE_TONE[state])}>
              {state}
            </Badge>
            <span className="text-muted-foreground">{DRIFT_STATE_MEANING[state]}</span>
          </span>
        ))}
      </div>
    </Card>
  );
}

const BANNER_TONE = {
  ok: "border-emerald-200 bg-emerald-50 text-emerald-900",
  drift: "border-amber-300 bg-amber-50 text-amber-900",
  unknown: "border-slate-300 bg-slate-50 text-slate-700",
} as const;

function AssetSection({
  asset,
  busy,
  failure,
  onResync,
  onAssetChange,
  onRefresh,
}: {
  asset: AssetToolDrift;
  busy: boolean;
  failure?: string;
  onResync: () => void;
  onAssetChange: (fresh: AssetToolDrift) => void;
  onRefresh: () => Promise<unknown> | unknown;
}) {
  const banner = driftBanner(asset);
  const [editing, setEditing] = useState<ToolDriftEntry | null>(null);
  // 이력은 열어 볼 때만 읽어요 — 목록을 열 때마다 자산마다 한 번씩 더 부르면
  // 화면이 느려지고, 대부분은 이력을 보지 않아요.
  const [history, setHistory] = useState<SensitivityHistoryEvent[] | null>(null);
  const [historyError, setHistoryError] = useState("");
  const [historyOpen, setHistoryOpen] = useState(false);

  async function loadHistory() {
    setHistoryError("");
    try {
      const { events } = await listMcpToolSensitivityHistory(asset.record_id);
      setHistory(events);
    } catch (e) {
      setHistory([]);
      setHistoryError(e instanceof Error ? e.message : "이력을 읽지 못했어요.");
    }
  }

  function toggleHistory() {
    const next = !historyOpen;
    setHistoryOpen(next);
    if (next) void loadHistory();
  }

  return (
    <section>
      <div
        className={cn(
          "flex flex-wrap items-center gap-3 rounded-t-xl border px-4 py-3",
          BANNER_TONE[banner.tone],
        )}
      >
        <div className="min-w-0 flex-1">
          <p className="text-sm font-semibold">{banner.headline}</p>
          <p className="mt-0.5 text-xs opacity-90">{banner.detail}</p>
          <p className="mt-0.5 font-mono text-[10px] opacity-70">
            {asset.asset_key} · {asset.record_id}
          </p>
        </div>
        <Button variant="outline" size="sm" onClick={toggleHistory}>
          {historyOpen ? "변경 이력 닫기" : "변경 이력"}
        </Button>
        <Button variant="outline" size="sm" onClick={onResync} disabled={busy}>
          {busy ? "읽는 중…" : "다시 읽기"}
        </Button>
      </div>
      {failure ? (
        <p className="border-x border-red-200 bg-red-50 px-4 py-2 text-xs text-red-700">
          다시 읽기 실패: {failure}
        </p>
      ) : null}
      {historyOpen ? (
        <HistoryPanel events={history} error={historyError} />
      ) : null}
      <div className="overflow-x-auto rounded-b-xl border border-t-0 border-border">
        <table className="w-full min-w-[1120px] border-collapse text-sm">
          <thead>
            <tr className="border-b border-border bg-muted/40 text-left text-xs text-muted-foreground">
              <th className="px-4 py-2.5 font-medium">도구</th>
              <th className="px-4 py-2.5 font-medium">상태</th>
              <th className="px-4 py-2.5 font-medium">민감도</th>
              <th className="px-4 py-2.5 font-medium">Target (파생)</th>
              <th className="px-4 py-2.5 font-medium">부를 수 있는 등급</th>
              <th className="px-4 py-2.5 font-medium">출처</th>
              <th className="px-4 py-2.5 font-medium">다음 행동</th>
            </tr>
          </thead>
          <tbody>
            {asset.tools.length === 0 ? (
              <tr>
                <td colSpan={7} className="px-4 py-10 text-center text-muted-foreground">
                  이 자산의 도구 원장이 비어 있어요. &quot;다시 읽기&quot; 로 채울 수 있어요.
                </td>
              </tr>
            ) : (
              asset.tools.map((tool) => (
                <ToolRow
                  key={tool.tool_name}
                  tool={tool}
                  targetMode={asset.target_mode}
                  onEdit={() => setEditing(tool)}
                />
              ))
            )}
          </tbody>
        </table>
      </div>
      {editing ? (
        <SensitivityEditDialog
          // 도구·현재 태그가 바뀌면 새로 마운트돼 입력이 초기화돼요.
          key={`${editing.tool_name}:${editing.sensitivity ?? ""}`}
          open
          recordId={asset.record_id}
          assetName={asset.asset_name}
          targetMode={asset.target_mode}
          tool={editing}
          onClose={() => setEditing(null)}
          onSaved={(fresh) => {
            onAssetChange(fresh);
            // 방금 변경한 이력이 화면에 남아야 해요 — 열려 있으면 다시 읽어요.
            if (historyOpen) void loadHistory();
          }}
          onRefresh={onRefresh}
        />
      ) : null}
    </section>
  );
}

function HistoryPanel({
  events,
  error,
}: {
  events: SensitivityHistoryEvent[] | null;
  error: string;
}) {
  return (
    <div className="border-x border-border bg-muted/30 px-4 py-3">
      <p className="mb-2 text-xs font-semibold text-foreground">민감도 변경 이력</p>
      {error ? (
        <p className="text-xs text-red-700">이력을 읽지 못했어요: {error}</p>
      ) : events === null ? (
        <p className="text-xs text-muted-foreground">읽는 중…</p>
      ) : events.length === 0 ? (
        <p className="text-xs text-muted-foreground">
          아직 관리자가 바꾼 기록이 없어요.
        </p>
      ) : (
        <ul className="space-y-1.5">
          {events.map((event) => (
            <li key={event.event_id} className="text-xs">
              <span className="font-mono text-muted-foreground">{event.at}</span>{" "}
              <b className="font-mono">{event.tool_name}</b>{" "}
              <span className={event.downgrade ? "font-semibold text-amber-800" : ""}>
                {historyChangeText(event)}
              </span>{" "}
              <Badge variant="type" className="text-[10px]">
                {historyStageText(event)}
              </Badge>{" "}
              <span className="text-muted-foreground" title={event.actor}>
                · {event.actor_label || event.actor}
              </span>
              {event.groups_gained.length > 0 ? (
                <span className="text-muted-foreground">
                  {" "}
                  · 얻은 등급 {event.groups_gained.join(" · ")}
                </span>
              ) : null}
              {event.reason ? (
                <span className="block pl-4 text-muted-foreground">
                  사유: {event.reason}
                </span>
              ) : null}
              {(event.stage === "moved"
                || event.change_status === "APPROVED_PENDING_PROPAGATION")
                && event.impact[0]?.names.length ? (
                <span className="block pl-4 text-muted-foreground">
                  영향 agent: {event.impact[0].names.join(" · ")}
                </span>
              ) : null}
              {event.change_status === "APPROVED_PENDING_PROPAGATION"
                && event.movement.reason ? (
                <span className="block pl-4 text-amber-800">
                  {event.movement.reason}
                </span>
              ) : null}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function ToolRow({
  tool,
  targetMode,
  onEdit,
}: {
  tool: ToolDriftEntry;
  targetMode: AssetToolDrift["target_mode"];
  onEdit: () => void;
}) {
  const diffLines = describeDiff(tool.diff);
  const blocked = tool.callable_now === false;
  const callabilityUnknown = tool.callable_now === null;
  const { editable, reason: editReason } = editability(tool);
  const source = tool.sensitivity ? (tool.sensitivity_source ?? "unknown") : null;
  const target = targetPresentation(tool, targetMode);
  return (
    <tr className="border-b border-border align-top last:border-0">
      <td className="px-4 py-3">
        <div className="font-mono text-[13px] font-semibold text-foreground">
          {tool.tool_name}
        </div>
        {diffLines.length > 0 ? (
          <ul className="mt-1 space-y-0.5">
            {diffLines.map((line) => (
              <li key={line} className="font-mono text-[11px] text-orange-800">
                {line}
              </li>
            ))}
          </ul>
        ) : null}
        {tool.description_changed ? (
          <p className="mt-1 text-[11px] text-orange-800">설명이 바뀌었어요</p>
        ) : null}
      </td>
      <td className="px-4 py-3">
        <Badge variant="type" className={cn("text-[11px]", DRIFT_STATE_TONE[tool.state])}>
          {tool.state}
        </Badge>
      </td>
      <td className="px-4 py-3">
        {editable ? (
          <button
            type="button"
            onClick={onEdit}
            aria-label={`${tool.tool_name} 민감도 변경`}
            className={cn(
              "rounded-md border border-input px-2 py-1 text-left text-[11px]",
              "hover:bg-muted focus-visible:outline-none focus-visible:ring-2",
              "focus-visible:ring-ring",
            )}
          >
            {tool.sensitivity ? (
              <span className="font-semibold">{tool.sensitivity}</span>
            ) : (
              <span className="text-muted-foreground">— 미분류 —</span>
            )}
            <span className="ml-1.5 text-muted-foreground">변경</span>
          </button>
        ) : (
          <span className="text-xs text-muted-foreground" title={editReason}>
            {tool.sensitivity ?? "— 미분류 —"}
            <span className="mt-0.5 block text-[10px]">{editReason}</span>
          </span>
        )}
        {!tool.sensitivity && tool.previous_sensitivity ? (
          <span className="mt-0.5 block text-[10px] text-muted-foreground">
            직전 {tool.previous_sensitivity}
          </span>
        ) : null}
        {tool.pending_change ? (
          <Badge
            variant="type"
            className={cn(
              "mt-1 block w-fit text-[10px]",
              tool.pending_change.status === "FAILED"
                ? "bg-red-100 text-red-800"
                : "bg-amber-100 text-amber-900",
            )}
          >
            {sensitivityChangeStatusText(tool.pending_change)}
          </Badge>
        ) : null}
      </td>
      <td className="max-w-64 px-4 py-3">
        <span className="block break-words font-mono text-[11px] font-semibold">
          {target.label}
        </span>
        <span className="mt-0.5 block text-[10px] text-muted-foreground">
          {target.detail}
        </span>
      </td>
      <td
        className={cn(
          "px-4 py-3 text-xs",
          blocked
            ? "font-semibold text-red-700"
            : callabilityUnknown
              ? "font-semibold text-amber-800"
              : "text-foreground",
        )}
      >
        {callabilityText(tool, targetMode)}
      </td>
      <td className="px-4 py-3">
        {source ? (
          <Badge
            variant="type"
            className={cn("text-[10px]", SENSITIVITY_SOURCE_TONE[source])}
            title={SENSITIVITY_SOURCE_MEANING[source]}
          >
            {SENSITIVITY_SOURCE_LABEL[source]}
          </Badge>
        ) : (
          <span className="text-xs text-muted-foreground">없음</span>
        )}
      </td>
      <td className="px-4 py-3 text-xs text-muted-foreground">
        {nextActionText(tool, targetMode)}
      </td>
    </tr>
  );
}
