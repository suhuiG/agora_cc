"use client";

// `?` 도움말 팝오버 — 표 헤더·라벨 옆에서 그 칼럼이 무엇을 뜻하는지 펼쳐 보여줘요.
//
// 왜 직접 만드나: 이 저장소엔 Tooltip·Popover 가 없고 headless UI 라이브러리도 없어요
// (package.json 런타임 의존성 = next·react·react-dom·swr·aws-sdk·amazon-cognito 뿐).
// 그렇다고 `title` 속성으로 대신할 수는 없어요 — 터치·키보드로는 안 열리고 줄바꿈·목록도 못 담아요.
//
// 왜 `createPortal(…, document.body)` 인가: 도구 인가 승인 표는 `overflow-x-auto` 래퍼 안에
// 있어서, 그 안에 `absolute` 로 띄운 팝오버는 스크롤 컨테이너 경계에서 잘려요. modal.tsx:108
// 과 같은 방식으로 body 로 빼내고 `position: fixed` 로 좌표를 잡아요.
//
// 왜 hover 가 아니라 click 인가: 터치 기기엔 hover 가 없고, 본문(children)에 링크·목록 같은
// JSX 를 허용하기로 했으니 포인터가 팝오버 안으로 들어갈 수 있어야 해요.
import { useCallback, useEffect, useId, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Icon } from "./icon";
import { useIsClient } from "@/lib/useIsClient";
import { cn } from "@/lib/ui";

export interface HelpPopoverProps {
  /** 스크린리더용 이름. 트리거의 `aria-label` 이자 팝오버의 접근 이름이에요. 예: "민감도 설명". */
  label: string;
  /** 도움말 본문. 목록·강조·코드 같은 JSX 를 넣을 수 있어요. */
  children: React.ReactNode;
  /** 트리거 버튼에 덧붙일 클래스 (여백 조정 등). */
  className?: string;
}

/** 트리거와 팝오버 사이 간격(px). */
const GAP = 6;
/** 뷰포트 가장자리에서 남길 여유(px). 이보다 붙으면 그림자가 잘려 보여요. */
const MARGIN = 8;

/**
 * 트리거 기준 팝오버 좌표를 계산해요 (기본 = 트리거 아래·왼쪽 정렬).
 *
 * 넘칠 때 반대편으로 뒤집어요: 오른쪽으로 넘치면 오른쪽 끝을 트리거 오른쪽에 맞추고,
 * 아래로 넘치면 트리거 위로 올려요. 뒤집어도 안 들어가면 뷰포트 안으로 clamp 해요 —
 * 화면보다 큰 도움말이 들어와도 최소한 위쪽 시작 부분은 읽을 수 있어야 하거든요.
 */
function computePlacement(
  trigger: { top: number; bottom: number; left: number; right: number },
  panel: { width: number; height: number },
  viewport: { width: number; height: number },
): { top: number; left: number } {
  let left = trigger.left;
  if (left + panel.width > viewport.width - MARGIN) {
    left = trigger.right - panel.width;
  }
  left = Math.max(MARGIN, Math.min(left, viewport.width - panel.width - MARGIN));

  let top = trigger.bottom + GAP;
  if (top + panel.height > viewport.height - MARGIN) {
    const above = trigger.top - GAP - panel.height;
    top = above >= MARGIN ? above : Math.max(MARGIN, viewport.height - panel.height - MARGIN);
  }

  return { top, left };
}

export function HelpPopover({ label, children, className }: HelpPopoverProps) {
  const mounted = useIsClient();
  const [open, setOpen] = useState(false);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const panelId = useId();

  const close = useCallback((returnFocus = true) => {
    setOpen(false);
    // 팝오버를 열 때 포커스를 안으로 옮겼으니, 닫을 때 트리거로 되돌려야 키보드 사용자가
    // 표의 원래 위치를 잃지 않아요 (modal.tsx:84 와 같은 이유).
    if (returnFocus) triggerRef.current?.focus();
  }, []);

  // 좌표는 state 가 아니라 DOM 노드에 직접 써요.
  // (1) 측정 → setState → 리렌더 왕복이 한 프레임 더 늘고, (2) effect 안의 setState 는
  // react-hooks/set-state-in-effect 가 막아요 (useIsClient.ts:6-7 에 같은 사정이 적혀 있어요).
  // 좌표는 화면에만 존재하는 파생값이라 React state 로 들 이유가 없어요.
  const applyPlacement = useCallback(() => {
    const trigger = triggerRef.current;
    const panel = panelRef.current;
    if (!trigger || !panel) return;
    const rect = panel.getBoundingClientRect();
    const { top, left } = computePlacement(
      trigger.getBoundingClientRect(),
      { width: rect.width, height: rect.height },
      { width: window.innerWidth, height: window.innerHeight },
    );
    panel.style.top = `${top}px`;
    panel.style.left = `${left}px`;
    // 측정 전에는 hidden 으로 렌더해요 — 좌표를 잡기 전 좌상단에 한 프레임 깜빡이지 않게요.
    panel.style.visibility = "visible";
  }, []);

  useEffect(() => {
    if (!open) return;
    applyPlacement();
    // role="dialog" 는 포커스가 안으로 들어가야 스크린리더가 이름과 본문을 읽어요.
    panelRef.current?.focus();
  }, [open, applyPlacement]);

  useEffect(() => {
    if (!open) return;
    // 표가 가로 스크롤되거나 창 크기가 바뀌면 fixed 좌표가 트리거와 어긋나요.
    // capture 로 듣는 이유: 스크롤 이벤트는 버블하지 않아서 안쪽 스크롤 컨테이너
    // (`overflow-x-auto` 래퍼)의 스크롤이 document 리스너까지 올라오지 않아요.
    const onScroll = () => applyPlacement();
    window.addEventListener("scroll", onScroll, { capture: true, passive: true });
    window.addEventListener("resize", onScroll);
    return () => {
      window.removeEventListener("scroll", onScroll, { capture: true });
      window.removeEventListener("resize", onScroll);
    };
  }, [open, applyPlacement]);

  useEffect(() => {
    if (!open) return;

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      // 포털 형제라 트리거의 React 트리로는 팝오버 안 키 입력이 안 올라오는 경우가 있어요
      // (마우스로 열고 포커스가 아직 body 인 순간). document 에서 듣는 게 확실해요.
      event.stopPropagation();
      close();
    };

    const onPointerDown = (event: PointerEvent) => {
      const target = event.target as Node | null;
      if (!target) return;
      // 트리거 클릭은 아래 onClick 토글이 처리해요. 여기서 먼저 닫으면 그 직후 토글이
      // 다시 열어버려요.
      if (triggerRef.current?.contains(target)) return;
      if (panelRef.current?.contains(target)) return;
      const focusWasInside = panelRef.current?.contains(document.activeElement) ?? false;
      close(false);
      if (!focusWasInside) return;
      // 방금 누른 곳이 포커스를 받을 수 있으면 그쪽이 이겨야 해요(빼앗으면 안 돼요).
      // 그래서 pointerdown 기본 동작이 끝난 다음 프레임에, 아무도 포커스를 안 받았을
      // 때만(= body 로 떨어졌을 때) 트리거로 되돌려요.
      requestAnimationFrame(() => {
        if (!document.activeElement || document.activeElement === document.body) {
          triggerRef.current?.focus();
        }
      });
    };

    document.addEventListener("keydown", onKeyDown);
    document.addEventListener("pointerdown", onPointerDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      document.removeEventListener("pointerdown", onPointerDown);
    };
  }, [open, close]);

  return (
    <>
      <button
        ref={triggerRef}
        type="button"
        aria-label={label}
        aria-expanded={open}
        // 닫혀 있을 때 팝오버 노드가 없어요. 그때도 aria-controls 를 남기면 존재하지 않는
        // id 를 가리키는 깨진 IDREF 가 돼요.
        aria-controls={open ? panelId : undefined}
        aria-haspopup="dialog"
        onClick={() => (open ? close() : setOpen(true))}
        className={cn(
          "inline-flex shrink-0 items-center justify-center rounded-full p-0.5 align-middle text-muted-foreground transition-colors hover:bg-accent hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
          className,
        )}
      >
        <Icon name="help" size={14} />
      </button>

      {mounted &&
        open &&
        createPortal(
          <div
            ref={panelRef}
            id={panelId}
            role="dialog"
            aria-label={label}
            // aria-modal 은 일부러 안 붙여요 — 배경을 막지 않는 비모달 팝오버라, 붙이면
            // 스크린리더가 나머지 표를 감춰버려요.
            tabIndex={-1}
            style={{
              top: 0,
              left: 0,
              visibility: "hidden",
              // 좁은 화면(모바일)에서 w-72(288px)가 뷰포트보다 넓어지는 걸 막아요.
              // 클래스(`max-w-[calc(…)]`)로 안 쓰는 이유: 임의값 안의 calc 는 Tailwind 가
              // 연산자 공백을 채워주길 기대해야 하고, 여기선 MARGIN 상수와 값을 묶어두는
              // 편이 어긋날 여지가 없어요.
              maxWidth: `calc(100vw - ${MARGIN * 2}px)`,
            }}
            className="fixed z-[var(--z-popover)] w-72 rounded-lg border border-border bg-card p-3 text-xs leading-relaxed text-card-foreground shadow-xl focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            {children}
          </div>,
          document.body,
        )}
    </>
  );
}
