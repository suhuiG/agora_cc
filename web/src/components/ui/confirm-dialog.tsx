"use client";

// Agora 공용 확인 다이얼로그.
// 브라우저 window.confirm()/alert() 대신 화면 내부에서 열리는 접근성 있는 모달이에요.
//
// 두 가지 사용법을 제공해요 (둘 다 같은 primitive 를 씀):
//  1) 제어형 <ConfirmDialog open title ... onConfirm onCancel /> — 상태를 직접 들고 있을 때.
//  2) useConfirm() 훅 — `const { confirm, dialog } = useConfirm()` 후
//     `if (await confirm({ title, description, variant })) { ... }`.
//     window.confirm 을 그대로 대체하는 가장 간단한 형태라 화면들에서 이쪽을 주로 써요.
//
// 접근성: role="dialog" + aria-modal, Esc 로 취소, backdrop 클릭 취소, 확인 버튼 autoFocus,
// Tab 포커스 트랩, 닫힐 때 직전 포커스 복원.
// 레이어링: position:fixed 오버레이(overflow 잘림 방지) + z 시맨틱 스케일(globals.css).
// 모션: 150ms fade+scale, prefers-reduced-motion 이면 instant.
import { useCallback, useEffect, useId, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Button } from "./button";
import { moveFocusWithinTrap, registerTrap } from "@/lib/focusTrap";
import { useIsClient } from "@/lib/useIsClient";
import { cn } from "@/lib/ui";

export type ConfirmVariant = "default" | "destructive";

export interface ConfirmDialogProps {
  open: boolean;
  title: string;
  description?: string;
  confirmLabel?: string;
  cancelLabel?: string;
  /** destructive 면 확인 버튼이 red 로 바뀌어요(삭제·되돌릴 수 없는 액션). */
  variant?: ConfirmVariant;
  /** 확인 처리 중이면 버튼 비활성 + 라벨 유지(스피너 대신 disabled). */
  busy?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}

function prefersReducedMotion(): boolean {
  if (typeof window === "undefined" || !window.matchMedia) return false;
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

/**
 * 제어형 확인 다이얼로그 primitive. open 이 true 일 때만 body 포털로 렌더돼요.
 */
export function ConfirmDialog({
  open,
  title,
  description,
  confirmLabel = "확인",
  cancelLabel = "취소",
  variant = "default",
  busy = false,
  onConfirm,
  onCancel,
}: ConfirmDialogProps) {
  const mounted = useIsClient();                   // 포털 대상(document) 준비 여부(SSR 가드)
  const [entered, setEntered] = useState(false);   // enter 트랜지션 토글
  const cardRef = useRef<HTMLDivElement>(null);
  const confirmRef = useRef<HTMLButtonElement>(null);
  const restoreFocusRef = useRef<HTMLElement | null>(null);
  const titleId = useId();
  const descId = useId();

  // 열릴 때: 직전 포커스 저장 → 다음 프레임에 enter 트랜지션 + 확인 버튼 포커스.
  // setState 는 effect 본문이 아니라 rAF 콜백/cleanup 안에서만 호출해요(react-hooks 규칙).
  useEffect(() => {
    if (!open) return;
    restoreFocusRef.current = (document.activeElement as HTMLElement) ?? null;
    const unregister = registerTrap(cardRef.current);
    const raf = requestAnimationFrame(() => {
      setEntered(true);
      // autoFocus 대신 명시 포커스 — 트랜지션 후에도 확실히 잡히게.
      confirmRef.current?.focus();
    });
    return () => {
      cancelAnimationFrame(raf);
      unregister();
      setEntered(false);
      // 닫히면 직전 포커스 복원.
      restoreFocusRef.current?.focus?.();
    };
  }, [open]);

  // Esc = 취소, Tab = 포커스 트랩.
  const onKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        if (!busy) onCancel();
        return;
      }
      if (e.key !== "Tab") return;
      // Modal 과 같은 순환 규칙 — 토스트 영역까지 포함해요. 실패 토스트는 수동으로만
      // 닫히는데 트랩이 다이얼로그 안만 돌면 키보드로 닫을 방법이 없어요.
      if (moveFocusWithinTrap(cardRef.current, { shiftKey: e.shiftKey })) {
        e.preventDefault();
      }
    },
    [busy, onCancel],
  );

  if (!mounted || !open) return null;

  const reduced = prefersReducedMotion();

  return createPortal(
    <div
      className={cn(
        "fixed inset-0 flex items-center justify-center p-4",
        // 루트를 modal 레이어에 둬요 — backdrop 값이면 스태킹 컨텍스트가 생겨
        // 자식 카드가 전역 z-50(모바일 드로어) 아래로 깔려요.
        "z-[var(--z-modal)] bg-black/40",
        !reduced && "transition-opacity duration-150 ease-out motion-reduce:transition-none",
        entered ? "opacity-100" : "opacity-0",
      )}
      onClick={() => !busy && onCancel()}
      onKeyDown={onKeyDown}
    >
      <div
        ref={cardRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={description ? descId : undefined}
        onClick={(e) => e.stopPropagation()}
        className={cn(
          "relative z-[var(--z-modal)] w-full max-w-sm rounded-xl border border-border bg-card p-6 text-card-foreground shadow-xl",
          !reduced && "transition-all duration-150 ease-out motion-reduce:transition-none",
          entered ? "scale-100 opacity-100" : "scale-95 opacity-0",
        )}
      >
        <h3 id={titleId} className="text-base font-semibold leading-tight text-card-foreground">
          {title}
        </h3>
        {description && (
          <p id={descId} className="mt-2 text-sm text-muted-foreground">
            {description}
          </p>
        )}
        <div className="mt-5 flex justify-end gap-2">
          <Button variant="outline" size="md" onClick={onCancel} disabled={busy}>
            {cancelLabel}
          </Button>
          <Button
            ref={confirmRef}
            variant={variant === "destructive" ? "destructive" : "primary"}
            size="md"
            onClick={onConfirm}
            disabled={busy}
          >
            {confirmLabel}
          </Button>
        </div>
      </div>
    </div>,
    document.body,
  );
}

// ---------------------------------------------------------------------------
// useConfirm() — promise 기반. window.confirm 대체용.
// ---------------------------------------------------------------------------

export interface ConfirmOptions {
  title: string;
  description?: string;
  confirmLabel?: string;
  cancelLabel?: string;
  variant?: ConfirmVariant;
}

interface ConfirmState extends ConfirmOptions {
  open: boolean;
  resolve: (ok: boolean) => void;
}

/**
 * `const { confirm, dialog } = useConfirm()` 로 쓰고, JSX 어딘가에 `{dialog}` 를 렌더해요.
 * `if (await confirm({ title, description, variant: "destructive" })) { ... }`.
 */
export function useConfirm() {
  const [state, setState] = useState<ConfirmState | null>(null);

  const confirm = useCallback((options: ConfirmOptions) => {
    return new Promise<boolean>((resolve) => {
      setState({ ...options, open: true, resolve });
    });
  }, []);

  const close = useCallback(
    (ok: boolean) => {
      setState((s) => {
        s?.resolve(ok);
        // open=false 로 두어 exit 트랜지션 후 자연스럽게 사라지게 하되,
        // 다음 confirm 호출 시 새 state 로 덮여요.
        return s ? { ...s, open: false } : null;
      });
    },
    [],
  );

  const dialog = state ? (
    <ConfirmDialog
      open={state.open}
      title={state.title}
      description={state.description}
      confirmLabel={state.confirmLabel}
      cancelLabel={state.cancelLabel}
      variant={state.variant}
      onConfirm={() => close(true)}
      onCancel={() => close(false)}
    />
  ) : null;

  return { confirm, dialog };
}
