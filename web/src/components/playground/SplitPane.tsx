"use client";

/**
 * 두 패널 분할 — 가운데 손잡이를 끌어서 크기를 바꿔요. 좌우(`spec.axis="x"`)와
 * 상하(`spec.axis="y"`) 둘 다 같은 컴포넌트로 처리해요.
 *
 * 접근성: 손잡이는 WAI-ARIA window splitter 패턴대로 `role="separator"` + `aria-valuenow`
 * 를 갖고 탭으로 포커스돼요. 마우스 없이도 축에 맞는 화살표(±2%)·Shift+화살표(±10%)·
 * Home/End·Enter(기본 크기 복귀)로 조작할 수 있어요.
 *
 * 저장된 크기는 localStorage 라는 **외부 store** 라서 `useSyncExternalStore` 로 읽어요 —
 * effect 로 읽어 setState 하면 렌더가 연쇄되고, 렌더 중에 직접 읽으면 SSR 결과와 어긋나요.
 * 서버 스냅샷은 기본 크기라 첫 페인트는 기본값이고, 하이드레이션에서 저장값으로 맞춰져요.
 *
 * 크기 계산·저장은 `@/lib/playground/splitPane` 의 순수 함수가 담당해요(테스트 대상).
 */

import { useCallback, useRef, useState, useSyncExternalStore } from "react";
import {
  COLUMNS_SPLIT,
  type SplitSpec,
  clampRatio,
  loadRatio,
  ratioForKey,
  ratioFromPointer,
  saveRatio,
} from "@/lib/playground/splitPane";
import { cn } from "@/lib/ui";

// 같은 탭 안의 구독자에게 변경을 알리는 최소 store. `storage` 이벤트는 다른 탭만 쏴요.
const listeners = new Set<() => void>();

function subscribe(onChange: () => void) {
  listeners.add(onChange);
  window.addEventListener("storage", onChange);
  return () => {
    listeners.delete(onChange);
    window.removeEventListener("storage", onChange);
  };
}

function publish(ratio: number, spec: SplitSpec) {
  saveRatio(window.localStorage, ratio, spec);
  listeners.forEach((listener) => listener());
}

export function SplitPane({
  first,
  second,
  firstLabel,
  secondLabel,
  spec = COLUMNS_SPLIT,
  className,
}: {
  first: React.ReactNode;
  second: React.ReactNode;
  firstLabel: string;
  secondLabel: string;
  spec?: SplitSpec;
  className?: string;
}) {
  const horizontal = spec.axis === "x";
  const storedRatio = useSyncExternalStore(
    subscribe,
    // getSnapshot 은 원시값을 돌려줘야 React 가 무한 재렌더에 빠지지 않아요.
    useCallback(() => loadRatio(window.localStorage, spec), [spec]),
    useCallback(() => spec.default, [spec]),
  );
  // 끄는 중에는 저장하지 않아요 — 손을 뗄 때 한 번만 써요(localStorage 쓰기 폭주 방지).
  const [dragRatio, setDragRatio] = useState<number | null>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const ratio = dragRatio ?? storedRatio;

  const commit = useCallback(
    (next: number) => {
      setDragRatio(null);
      publish(clampRatio(next, spec), spec);
    },
    [spec],
  );

  function onPointerDown(event: React.PointerEvent<HTMLDivElement>) {
    event.currentTarget.setPointerCapture(event.pointerId);
    setDragRatio(ratio);
  }

  function onPointerMove(event: React.PointerEvent<HTMLDivElement>) {
    if (dragRatio === null) return;
    const rect = containerRef.current?.getBoundingClientRect();
    if (!rect) return;
    setDragRatio(
      ratioFromPointer(
        horizontal ? event.clientX : event.clientY,
        horizontal
          ? { start: rect.left, size: rect.width }
          : { start: rect.top, size: rect.height },
        dragRatio,
        spec,
      ),
    );
  }

  function onPointerUp(event: React.PointerEvent<HTMLDivElement>) {
    if (dragRatio === null) return;
    event.currentTarget.releasePointerCapture(event.pointerId);
    commit(dragRatio);
  }

  function onKeyDown(event: React.KeyboardEvent<HTMLDivElement>) {
    const next = ratioForKey(event.key, ratio, { shift: event.shiftKey, spec });
    if (next === null) return;
    event.preventDefault();
    commit(next);
  }

  return (
    <div
      ref={containerRef}
      // 저장된 크기는 하이드레이션에서 들어와 SSR HTML(기본 크기)과 다를 수 있어요.
      suppressHydrationWarning
      style={{ ["--pg-first" as string]: `${ratio}%` }}
      className={cn(
        "flex min-h-0 min-w-0",
        horizontal ? "flex-col gap-4 lg:flex-row lg:gap-0" : "flex-col",
        dragRatio !== null && "select-none",
        className,
      )}
    >
      <div
        className={cn(
          "min-h-0 min-w-0",
          horizontal
            ? "lg:w-[var(--pg-first)] lg:shrink-0"
            : "h-[var(--pg-first)] shrink-0",
        )}
      >
        {first}
      </div>

      <div
        role="separator"
        aria-orientation={horizontal ? "vertical" : "horizontal"}
        aria-label={`${firstLabel} · ${secondLabel} ${horizontal ? "폭" : "높이"} 조절`}
        aria-valuenow={ratio}
        aria-valuemin={spec.min}
        aria-valuemax={spec.max}
        aria-valuetext={`${firstLabel} ${ratio}%`}
        tabIndex={0}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerCancel={onPointerUp}
        onDoubleClick={() => commit(spec.default)}
        onKeyDown={onKeyDown}
        title={
          horizontal
            ? "드래그하거나 ←/→ 로 폭을 조절해요 (Enter: 기본 폭)"
            : "드래그하거나 ↑/↓ 로 높이를 조절해요 (Enter: 기본 높이)"
        }
        className={cn(
          "group shrink-0 touch-none items-center justify-center focus-visible:outline-none",
          horizontal
            ? "hidden cursor-col-resize px-1.5 lg:flex"
            : "flex cursor-row-resize py-1.5",
        )}
      >
        <span
          aria-hidden="true"
          className={cn(
            "rounded-full bg-border transition-colors",
            horizontal ? "h-16 w-1" : "h-1 w-16",
            "group-hover:bg-blue-400 group-focus-visible:bg-blue-600",
            "group-focus-visible:ring-2 group-focus-visible:ring-blue-200",
            dragRatio !== null && "bg-blue-600",
          )}
        />
      </div>

      <div className="min-h-0 min-w-0 flex-1">{second}</div>
    </div>
  );
}
