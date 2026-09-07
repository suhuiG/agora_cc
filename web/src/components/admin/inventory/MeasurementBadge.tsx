import type { MeasurementState } from "@/lib/api";

// 측정 미구현 표기 — 시안의 "빈칸=risk"(빨간 `?`)를 쓰지 않아요. 측정 자체가 미구현이면 전
// 자산이 빨갛게 채워져 노이즈가 되고, **미측정은 자산별 결함이 아니라 플랫폼 상태**예요.
// telemetry가 붙어 state가 available이 되면 배지가 사라지고, 그때부터 빈칸이 진짜 risk예요.
// (W3에서 미스캔을 안전색으로 안 보이게 한 것과 같은 규칙.)

/** 컬럼 헤더에 붙는 "측정 미구현" 배지. available이면 아무것도 안 그려요. */
export function MeasurementBadge({ state }: { state: MeasurementState }) {
  if (state === "available") return null;
  return (
    <span
      className="ml-1 shrink-0 rounded bg-slate-100 px-1.5 py-0.5 text-[10px] font-medium text-slate-500"
      title="측정이 아직 구현되지 않았어요. 값이 없는 게 아니라 재지 않고 있어요."
    >
      측정 미구현
    </span>
  );
}

/** 미측정 셀 — 회색 대시. 위험색을 쓰지 않아요(자산 결함이 아니니까요). */
export function NotMeasuredCell() {
  return <span className="text-slate-300">—</span>;
}

/** 데이터 소스가 없는 카드의 빈 상태. 무엇이 선행돼야 채워지는지 명시해요. */
export function PendingSourceEmpty({ blockedBy }: { blockedBy: string }) {
  return (
    <div className="px-4 py-8 text-center">
      <div className="text-xs font-medium text-slate-400">데이터 없음</div>
      <div className="mt-1 text-[11px] text-slate-400">{blockedBy}</div>
    </div>
  );
}
