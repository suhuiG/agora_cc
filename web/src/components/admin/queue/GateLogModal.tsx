"use client";

import { createPortal } from "react-dom";
import { useEffect } from "react";
import useSWR from "swr";
import { getGateScanLogs, type GateScanLogs } from "@/lib/api";
import { cn } from "@/lib/ui";

const STATE_LABEL: Record<string, string> = {
  pass: "통과", fail: "실패", running: "진행 중", pending: "대기", not_run: "미실행",
  not_applicable: "해당 없음", unknown: "미해석",
};

const LOG_STATUS_MSG: Record<string, string> = {
  empty: "아직 로그가 없어요. 스캐너가 실행되면 채워져요.",
  not_run: "아직 스캔하지 않았어요.",
  unavailable: "로그를 불러오지 못했어요(로그 그룹 없음 또는 조회 실패).",
};

export function GateLogModal({
  recordId, toolId, gateLabel, onClose,
}: {
  recordId: string;
  toolId: string;
  gateLabel: string;
  onClose: () => void;
}) {
  // gate_state가 running이면 폴링, 아니면 1회.
  const { data } = useSWR<GateScanLogs>(
    ["gate-logs", recordId, toolId],
    () => getGateScanLogs(recordId, toolId),
    { refreshInterval: (d) => (d?.gate_state === "running" ? 4000 : 0) },
  );

  useEffect(() => {
    const onEsc = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    document.addEventListener("keydown", onEsc);
    return () => document.removeEventListener("keydown", onEsc);
  }, [onClose]);

  const state = data?.gate_state ?? "pending";
  const stateColor = { pass: "text-emerald-600", fail: "text-red-600",
    running: "text-blue-500", pending: "text-slate-400", not_run: "text-slate-400",
    unknown: "text-amber-700" }[state] ?? "text-slate-400";

  return createPortal(
    <div
      className="fixed inset-0 z-[var(--z-modal-backdrop)] flex items-center justify-center bg-black/40 p-4"
      onClick={onClose}
    >
      <div
        role="dialog"
        aria-modal="true"
        className="flex max-h-[80vh] w-full max-w-2xl flex-col overflow-hidden rounded-xl border border-border bg-card shadow-xl z-[var(--z-modal)]"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between border-b border-border px-5 py-3">
          <div className="flex items-center gap-2">
            <span className="font-semibold text-foreground">{gateLabel} 로그</span>
            <span className={cn("text-sm font-medium", stateColor)}>
              {STATE_LABEL[state] ?? state}
            </span>
          </div>
          <button type="button" onClick={onClose}
            className="rounded p-1 text-muted-foreground hover:bg-accent hover:text-foreground">✕</button>
        </div>

        <div className="flex-1 overflow-auto px-5 py-4">
          {data?.scan_id && (
            <p className="mb-3 font-mono text-[11px] text-muted-foreground">scan_id: {data.scan_id}</p>
          )}

          {/* findings */}
          {data && data.findings.length > 0 && (
            <div className="mb-4">
              <p className="mb-1.5 text-xs font-semibold text-foreground">검출 ({data.findings.length})</p>
              <ul className="space-y-1.5">
                {data.findings.map((f, i) => (
                  <li key={i} className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-xs">
                    <span className="font-mono font-medium text-red-700">{String(f.code ?? "")}</span>
                    <span className="ml-2 text-red-600">{String(f.severity ?? "")}</span>
                    <p className="mt-0.5 text-red-800">{String(f.detail ?? "")}</p>
                    {f.location ? <p className="mt-0.5 font-mono text-[11px] text-red-500">{String(f.location)}</p> : null}
                  </li>
                ))}
              </ul>
            </div>
          )}

          {/* 로그 */}
          <p className="mb-1.5 text-xs font-semibold text-foreground">실행 로그</p>
          {data && data.log_status === "ok" ? (
            <pre className="max-h-64 overflow-auto rounded-md bg-slate-900 px-3 py-2 font-mono text-[11px] leading-relaxed text-slate-100">
              {data.lines.join("\n")}
            </pre>
          ) : (
            <p className="rounded-md border border-dashed border-border px-3 py-4 text-center text-xs text-muted-foreground">
              {data ? (LOG_STATUS_MSG[data.log_status] ?? "로그가 없어요.") : "불러오는 중…"}
            </p>
          )}
        </div>
      </div>
    </div>,
    document.body,
  );
}
