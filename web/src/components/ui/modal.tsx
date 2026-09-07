"use client";

// Agora 공용 모달 셸 — 임의 내용을 담는 대화상자.
//
// ConfirmDialog 는 "제목 + 설명 + 확인/취소" 고정 형태라 입력 필드나 코드 블록이
// 들어가는 화면(자산 삭제 확인 입력, 설치 명령 보기 등)에는 못 써요. 그래서 화면마다
// `fixed inset-0` 오버레이를 직접 짜고 있었는데, 그 사본들은 Esc·포커스 트랩·포털이
// 빠져 있었어요. 이 컴포넌트가 그 껍데기를 한 곳으로 모아요.
//
// 접근성: role="dialog" + aria-modal, Esc 취소, backdrop 클릭 취소, 첫 포커스 대상
// 지정(initialFocusRef) 또는 카드 내 첫 포커스 요소, Tab 트랩, 닫힐 때 포커스 복원.
// 하드코딩 색(bg-white)이 아니라 토큰(bg-card)을 써서 테마와 어긋나지 않아요.
import { useCallback, useEffect, useId, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Icon } from "./icon";
import { moveFocusWithinTrap, registerTrap } from "@/lib/focusTrap";
import { useIsClient } from "@/lib/useIsClient";
import { cn } from "@/lib/ui";

export interface ModalProps {
  open: boolean;
  title: string;
  /** 제목 아래 보조 설명 (선택). */
  description?: React.ReactNode;
  children?: React.ReactNode;
  /** 하단 버튼 영역. 없으면 닫기 버튼만 렌더해요. */
  footer?: React.ReactNode;
  /** 카드 최대 너비. 기본 max-w-lg. */
  size?: "sm" | "md" | "lg";
  /** 처리 중이면 backdrop·Esc·X 로 닫히지 않아요(중간 취소 방지). */
  busy?: boolean;
  /** 열릴 때 포커스를 받을 요소. 없으면 카드 내 첫 포커스 요소. */
  initialFocusRef?: React.RefObject<HTMLElement | null>;
  onClose: () => void;
}

const SIZE_CLASSES = {
  sm: "max-w-sm",
  md: "max-w-md",
  lg: "max-w-lg",
} as const;

function prefersReducedMotion(): boolean {
  if (typeof window === "undefined" || !window.matchMedia) return false;
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

export function Modal({
  open,
  title,
  description,
  children,
  footer,
  size = "lg",
  busy = false,
  initialFocusRef,
  onClose,
}: ModalProps) {
  const mounted = useIsClient();
  const [entered, setEntered] = useState(false);
  const cardRef = useRef<HTMLDivElement>(null);
  const restoreFocusRef = useRef<HTMLElement | null>(null);
  const titleId = useId();
  const descId = useId();

  useEffect(() => {
    if (!open) return;
    restoreFocusRef.current = (document.activeElement as HTMLElement) ?? null;
    // 토스트 뷰포트도 같은 순환을 써야 해서 활성 카드를 등록해요(focusTrap 주석 참고).
    const unregister = registerTrap(cardRef.current);
    const raf = requestAnimationFrame(() => {
      setEntered(true);
      const target =
        initialFocusRef?.current ??
        cardRef.current?.querySelector<HTMLElement>(
          'input:not([disabled]), textarea:not([disabled]), select:not([disabled]), button:not([disabled])',
        );
      target?.focus();
    });
    return () => {
      cancelAnimationFrame(raf);
      unregister();
      setEntered(false);
      restoreFocusRef.current?.focus?.();
    };
  }, [open, initialFocusRef]);

  const onKeyDown = useCallback(
    (event: React.KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        if (!busy) onClose();
        return;
      }
      if (event.key !== "Tab") return;
      // 순환 계산은 lib/focusTrap 이 담당해요(ConfirmDialog 와 동일 동작).
      // 옮겼을 때만 기본 동작을 막아요 — 대상이 없으면 브라우저에 맡겨요.
      if (moveFocusWithinTrap(cardRef.current, { shiftKey: event.shiftKey })) {
        event.preventDefault();
      }
    },
    [busy, onClose],
  );

  if (!mounted || !open) return null;
  const reduced = prefersReducedMotion();

  return createPortal(
    <div
      className={cn(
        "fixed inset-0 flex items-center justify-center overflow-y-auto p-4",
        // 루트를 modal 레이어에 둬요. backdrop 값(40)으로 두면 이 요소가 스태킹
        // 컨텍스트를 만들어 자식 카드의 z-50 이 전역 50 으로 올라가지 못하고,
        // 전역 z-50 인 모바일 사이드바 드로어 아래에 깔려요.
        "z-[var(--z-modal)] bg-black/40",
        !reduced && "transition-opacity duration-150 ease-out motion-reduce:transition-none",
        entered ? "opacity-100" : "opacity-0",
      )}
      onClick={() => !busy && onClose()}
      onKeyDown={onKeyDown}
    >
      <div
        ref={cardRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={description ? descId : undefined}
        onClick={(event) => event.stopPropagation()}
        className={cn(
          "relative z-[var(--z-modal)] my-auto w-full rounded-xl border border-border bg-card p-6 text-card-foreground shadow-xl",
          SIZE_CLASSES[size],
          !reduced && "transition-all duration-150 ease-out motion-reduce:transition-none",
          entered ? "scale-100 opacity-100" : "scale-95 opacity-0",
        )}
      >
        <div className="mb-3 flex items-start justify-between gap-3">
          <div className="min-w-0">
            <h3 id={titleId} className="text-base font-semibold leading-tight">
              {title}
            </h3>
            {description && (
              <div id={descId} className="mt-1.5 text-sm text-muted-foreground">
                {description}
              </div>
            )}
          </div>
          <button
            type="button"
            onClick={onClose}
            disabled={busy}
            aria-label="닫기"
            className="-mr-2 -mt-2 shrink-0 rounded-lg p-1.5 text-muted-foreground transition-colors hover:bg-accent hover:text-foreground disabled:opacity-40"
          >
            <Icon name="close" size={16} />
          </button>
        </div>

        {children}

        {footer && (
          <div className="mt-5 flex flex-wrap justify-end gap-2">{footer}</div>
        )}
      </div>
    </div>,
    document.body,
  );
}
