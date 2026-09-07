"use client";

import { type GateProgress } from "@/lib/api";
import { GATE_STATE_META, riskBadgeClass, verdictClass, verdictLabel } from "@/lib/governance";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/ui";

// M1·M2 공용 게이트 표현 요소들. (화면설계 §2·§3)

/** 진행상태 바 `▓▓▓▓░ 4/6` + 상태 라벨 (M1 F2·M2 F1). */
export function GateProgressBar({
  progress,
  scanned,
}: {
  progress: GateProgress;
  scanned: boolean;
}) {
  const { passed, total } = progress;
  const label = verdictLabel(progress.verdict, scanned);
  const isOverride = progress.verdict === "approved-override";
  return (
    <div className="flex items-center gap-2">
      <div className="flex gap-0.5" aria-hidden>
        {total === 0 ? (
          <span className="text-xs text-muted-foreground">—</span>
        ) : (
          Array.from({ length: total }).map((_, i) => (
            <span
              key={i}
              className={cn(
                "inline-block h-2 w-2 rounded-sm",
                i < passed ? "bg-blue-500" : "bg-slate-200",
              )}
            />
          ))
        )}
      </div>
      {total > 0 && (
        <span className="text-xs tabular-nums text-muted-foreground">
          {passed}/{total}
        </span>
      )}
      <span className={cn("text-xs font-medium", verdictClass(progress.verdict))}>{label}</span>
      {isOverride && (
        <span
          className="rounded-sm bg-amber-100 px-1 py-0.5 text-[10px] font-medium text-amber-700"
          title="스캔 게이트는 미통과였지만 관리자가 사유를 남기고 승인했어요(soft override, 감사에 기록됨)."
        >
          override
        </span>
      )}
    </div>
  );
}

/** 위험도 배지 (M1 F3). 미스캔이면 "미스캔". */
export function RiskBadge({ risk }: { risk: string | null }) {
  return (
    <Badge variant="type" className={riskBadgeClass(risk)}>
      {risk ? risk.toUpperCase() : "미스캔"}
    </Badge>
  );
}

/** 게이트 단계 상태 아이콘 ○◐✓✗⊘ (M2 F2 공용). running은 파란 pulse로 반짝여요. */
export function GateStatusDot({ state }: { state: string }) {
  const meta = GATE_STATE_META[state] ?? GATE_STATE_META.pending;
  return (
    <span className={cn(
      "inline-flex items-center gap-1 text-sm font-semibold",
      state === "running" && "animate-pulse",
      meta.cls,
    )}>
      <span aria-hidden>{meta.icon}</span>
      <span className="text-xs">{meta.label}</span>
    </span>
  );
}
