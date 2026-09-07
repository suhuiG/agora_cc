"use client";

/**
 * Initializr처럼 활동 트리가 없는 화면에서 쓰는 접이식 원문 런타임 로그 패널.
 * Playground는 활동 트리만 유지하고 이 패널을 렌더링하지 않아요.
 */
import { useEffect, useRef, useState } from "react";
import { cn } from "@/lib/ui";
import { useRuntimeLogs } from "./useRuntimeLogs";

const STATUS_MSG: Record<string, string> = {
  not_deployed: "아직 배포되지 않은 agent예요. 로그는 배포 후에 볼 수 있어요.",
  empty: "아직 새 로그가 없어요. 대화를 보내면 여기에 찍혀요.",
  unavailable: "로그를 읽지 못했어요. 잠시 후 다시 시도해 주세요.",
};

export function RuntimeLogView({
  recordId,
  lines,
  status,
  error,
  onClear,
  onOpenChange,
  title = "원문 런타임 로그",
  closedHint = "트리가 분류하지 못한 줄도 여기서 볼 수 있어요",
  className,
}: {
  recordId: string;
  lines: string[];
  status: string;
  error: string;
  onClear: () => void;
  onOpenChange?: (open: boolean) => void;
  title?: string;
  closedHint?: string;
  className?: string;
}) {
  const [open, setOpen] = useState(false);
  const boxRef = useRef<HTMLPreElement | null>(null);
  const stickRef = useRef(true);

  useEffect(() => {
    const element = boxRef.current;
    if (element && stickRef.current) element.scrollTop = element.scrollHeight;
  }, [lines]);

  function toggle() {
    const next = !open;
    setOpen(next);
    onOpenChange?.(next);
  }

  function onScroll() {
    const element = boxRef.current;
    if (!element) return;
    stickRef.current = (
      element.scrollHeight - element.scrollTop - element.clientHeight < 40
    );
  }

  const hint = !recordId
    ? "agent를 먼저 선택해 주세요."
    : error || (lines.length === 0
      ? STATUS_MSG[status] ?? "로그를 불러오는 중이에요…"
      : "");

  return (
    <div className={className}>
      <button
        type="button"
        onClick={toggle}
        aria-expanded={open}
        className="flex w-full items-center gap-2 px-4 py-2 text-left text-xs text-muted-foreground hover:bg-accent/40"
      >
        <ChevronIcon open={open} />
        <span className="font-medium text-foreground">{title}</span>
        <span>{open ? "" : closedHint}</span>
        {lines.length > 0 && <span className="ml-auto">{lines.length}줄</span>}
      </button>

      {open && (
        <div className="border-t border-border p-3">
          <div className="mb-2 flex items-center justify-end">
            <button
              type="button"
              onClick={onClear}
              className="rounded px-2 py-1 text-xs text-muted-foreground hover:bg-accent"
            >
              지우기
            </button>
          </div>
          {hint && (
            <div className="rounded-lg bg-slate-50 p-3 text-xs text-slate-600">{hint}</div>
          )}
          {lines.length > 0 && (
            <pre
              ref={boxRef}
              onScroll={onScroll}
              className="max-h-64 overflow-auto rounded-lg bg-slate-900 p-3 font-mono text-[11px] leading-relaxed text-slate-100"
            >
              {lines.join("\n")}
            </pre>
          )}
        </div>
      )}
    </div>
  );
}

export function RuntimeLogPanel({ recordId }: { recordId: string }) {
  const [open, setOpen] = useState(false);
  const logs = useRuntimeLogs({ recordId, enabled: open });
  return (
    <RuntimeLogView
      recordId={recordId}
      lines={logs.lines}
      status={logs.status}
      error={logs.error}
      onClear={logs.clear}
      onOpenChange={setOpen}
      title="런타임 로그"
      closedHint="도구 호출·에러를 확인할 수 있어요"
      className="rounded-xl border border-border bg-card"
    />
  );
}

function ChevronIcon({ open }: { open?: boolean }) {
  return (
    <svg
      viewBox="0 0 24 24"
      className={cn(
        "h-3 w-3 shrink-0 text-slate-400 transition-transform",
        open && "rotate-90",
      )}
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
    >
      <path d="M9 18l6-6-6-6" />
    </svg>
  );
}
