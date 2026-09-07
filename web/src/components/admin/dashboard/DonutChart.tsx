"use client";

import Link from "next/link";

// 순수 SVG 도넛 차트 (외부 차트 라이브러리 없음). 위험/등급/상태 분포에 3번 재사용해요.
// 각 세그먼트는 stroke-dasharray/stroke-dashoffset로 원호를 그려요 — 둘레를 세그먼트 값 비율로
// 나눠 dash 길이를 정하고, 누적 오프셋으로 이어붙여요. 색은 호출부가 SVG용 색값으로 넘겨요.
// href가 있으면 범례 항목이 드릴다운 링크가 돼요(등급→큐?tier= 등 기존 링크 보존).

export type DonutDatum = { label: string; value: number; color: string; href?: string };

const SIZE = 120; // viewBox 한 변
const STROKE = 18; // 도넛 두께
const R = (SIZE - STROKE) / 2; // 반지름(두께 절반 여백)
const CX = SIZE / 2;
const CY = SIZE / 2;
const CIRC = 2 * Math.PI * R; // 둘레

export function DonutChart({ data, ariaLabel }: { data: DonutDatum[]; ariaLabel?: string }) {
  const total = data.reduce((sum, d) => sum + d.value, 0);

  if (total <= 0) {
    return <div className="py-6 text-center text-xs text-muted-foreground">데이터가 없어요</div>;
  }

  // 누적 오프셋으로 세그먼트별 dash 길이를 계산해요. 12시 방향에서 시작하도록 -90도 회전.
  const { segments } = data
    .filter((d) => d.value > 0)
    .reduce<{
      fraction: number;
      segments: Array<DonutDatum & { dash: number; offset: number }>;
    }>((result, d) => {
      const frac = d.value / total;
      const dash = frac * CIRC;
      const offset = result.fraction * CIRC;
      return {
        fraction: result.fraction + frac,
        segments: [...result.segments, { ...d, dash, offset }],
      };
    }, { fraction: 0, segments: [] });

  return (
    <div className="flex items-center gap-4">
      <svg
        viewBox={`0 0 ${SIZE} ${SIZE}`}
        className="h-28 w-28 shrink-0 -rotate-90"
        role="img"
        aria-label={ariaLabel ?? "분포 도넛 차트"}
      >
        {/* 트랙(배경 링) */}
        <circle cx={CX} cy={CY} r={R} fill="none" stroke="currentColor" strokeWidth={STROKE} className="text-slate-100" />
        {segments.map((s) => (
          <circle
            key={s.label}
            cx={CX}
            cy={CY}
            r={R}
            fill="none"
            stroke={s.color}
            strokeWidth={STROKE}
            strokeDasharray={`${s.dash} ${CIRC - s.dash}`}
            strokeDashoffset={-s.offset}
          />
        ))}
      </svg>
      {/* 범례 (라벨 + 수치). href가 있으면 드릴다운 링크. */}
      <ul className="min-w-0 flex-1 space-y-1">
        {data.map((d) => {
          const row = (
            <>
              <span className="flex min-w-0 items-center gap-1.5">
                <span className="h-2.5 w-2.5 shrink-0 rounded-sm" style={{ backgroundColor: d.color }} aria-hidden />
                <span className="truncate text-muted-foreground">{d.label}</span>
              </span>
              <span className="shrink-0 tabular-nums font-semibold">{d.value}</span>
            </>
          );
          return (
            <li key={d.label} className="text-sm">
              {d.href ? (
                <Link href={d.href} className="flex items-center justify-between gap-2 hover:text-blue-700">
                  {row}
                </Link>
              ) : (
                <div className="flex items-center justify-between gap-2">{row}</div>
              )}
            </li>
          );
        })}
      </ul>
    </div>
  );
}
