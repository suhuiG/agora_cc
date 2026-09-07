"use client";

// Agora 공용 토스트 — 등록·수정·삭제 같은 동작의 결과를 알려요.
//
// 왜 필요한가: 지금까지 성공은 "조용히 화면 이동", 실패는 "폼 안 인라인 텍스트"였어요.
// 사용자가 방금 한 일이 됐는지 안 됐는지 확인할 지점이 없었죠. ConfirmDialog 는
// "할까요?"를 묻는 도구라 "됐어요"를 알리는 데는 안 맞아요 — 그래서 둘을 나눠요.
//
//   ConfirmDialog / useConfirm  →  동작 전 확인 (모달, 차단형)
//   toast                       →  동작 후 결과 (비차단형, 자동 소멸)
//
// 접근성: 컨테이너가 aria-live 영역이라 스크린리더가 새 토스트를 읽어요.
// error 는 assertive(즉시 읽기), 나머지는 polite. 실패는 자동으로 안 사라져요 —
// 사용자가 놓치면 안 되는 정보라 직접 닫게 해요.
//
// 레이어링: 모달보다 위(--z-toast)여야 모달 안에서 띄운 토스트가 가려지지 않아요.
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { createPortal } from "react-dom";
import { Icon } from "./icon";
import {
  activeTrapCard,
  moveFocusWithinTrap,
  restoreFocusToTrap,
  TOAST_ROOT_ATTR,
} from "@/lib/focusTrap";
import { useIsClient } from "@/lib/useIsClient";
import {
  enqueueToast,
  toastDuration,
  type ToastItem,
  type ToastOptions,
  type ToastTone,
} from "./toastQueue";
import { cn } from "@/lib/ui";

export type { ToastOptions, ToastTone } from "./toastQueue";

interface ToastApi {
  toast: (options: ToastOptions) => void;
  /** 성공 단축 — toast({ tone: "success" }). */
  success: (title: string, description?: string) => void;
  /** 실패 단축 — 자동으로 안 닫혀요. */
  error: (title: string, description?: string) => void;
}

const ToastContext = createContext<ToastApi | null>(null);

const TONE_STYLES: Record<ToastTone, { card: string; icon: string; iconName: string }> = {
  success: {
    card: "border-emerald-200 bg-emerald-50 text-emerald-900",
    icon: "text-emerald-600",
    iconName: "check",
  },
  error: {
    card: "border-red-200 bg-red-50 text-red-900",
    icon: "text-red-600",
    iconName: "alert",
  },
  info: {
    card: "border-blue-200 bg-blue-50 text-blue-900",
    icon: "text-blue-700",
    iconName: "info",
  },
};

export function ToastProvider({ children }: { children: React.ReactNode }) {
  const [items, setItems] = useState<ToastItem[]>([]);
  const nextId = useRef(1);

  // 포커스를 가진 토스트가 사라지면 포커스가 body 로 떨어져요. 모달이 열려 있는 동안
  // 그러면 다음 Tab 이 트랩(모달·토스트 어느 핸들러도)을 거치지 않고 배경으로 새요.
  //
  // 사라지는 경로가 셋이에요 — 닫기 버튼, 자동 소멸 타이머, 큐 상한에 밀려남.
  // 세 경로 모두 결국 items 에서 빠지므로 **커밋 후 effect 에서 한 번에** 구제해요.
  // setItems updater 안에서 focus() 를 부르면 렌더 중 DOM 을 만지는 셈이라(순수성 위반)
  // StrictMode 재호출·미커밋 렌더에서 포커스가 먼저 튈 수 있어요.
  //
  // 어느 토스트에 포커스가 있었는지는 setState **전에** 기록해요 — 커밋 후에는 그 노드가
  // 이미 사라져서 document.activeElement 로 알 수 없거든요.
  const focusedToastIdRef = useRef<string | null>(null);

  const rememberFocusedToast = useCallback(() => {
    const holder = (document.activeElement as HTMLElement | null)?.closest(
      "[data-agora-toast-id]",
    );
    focusedToastIdRef.current = holder?.getAttribute("data-agora-toast-id") ?? null;
  }, []);

  const dismiss = useCallback(
    (id: number) => {
      rememberFocusedToast();
      setItems((current) => current.filter((item) => item.id !== id));
    },
    [rememberFocusedToast],
  );

  const toast = useCallback(
    (options: ToastOptions) => {
      const tone = options.tone ?? "info";
      const id = nextId.current++;
      // 상한에 밀려 사라지는 항목도 아래 effect 가 함께 구제해요.
      rememberFocusedToast();
      setItems((current) => enqueueToast(current, { ...options, id, tone }));
    },
    [rememberFocusedToast],
  );

  // 기억해 둔 토스트가 목록에서 빠졌으면 포커스를 트랩으로 되돌려요.
  useEffect(() => {
    const heldId = focusedToastIdRef.current;
    if (!heldId) return;
    if (items.some((item) => String(item.id) === heldId)) return; // 아직 살아 있어요
    focusedToastIdRef.current = null;
    // 포커스가 이미 다른 곳(모달 등)으로 옮겨졌으면 건드리지 않아요.
    if (document.activeElement && document.activeElement !== document.body) return;
    restoreFocusToTrap();
  }, [items]);

  const api = useMemo<ToastApi>(
    () => ({
      toast,
      success: (title, description) => toast({ title, description, tone: "success" }),
      error: (title, description) => toast({ title, description, tone: "error" }),
    }),
    [toast],
  );

  return (
    <ToastContext.Provider value={api}>
      {children}
      <ToastViewport items={items} onDismiss={dismiss} />
    </ToastContext.Provider>
  );
}

function ToastViewport({
  items,
  onDismiss,
}: {
  items: ToastItem[];
  onDismiss: (id: number) => void;
}) {
  // 포털 대상(document)은 클라이언트에만 있어요. 마운트 후에만 렌더해요.
  const mounted = useIsClient();
  if (!mounted) return null;

  return createPortal(
    <div
      // 모달 포커스 트랩이 이 영역을 찾아 순환 범위에 포함해요(modal.tsx). 토스트는
      // body 형제 포털이라 트랩 밖이고, 실패 토스트는 수동으로만 닫혀서 키보드로
      // 닿을 방법이 필요해요.
      {...{ [TOAST_ROOT_ATTR]: "" }}
      // 모달이 열려 있는 동안에는 토스트도 그 트랩의 일부예요. 모달 핸들러는 모달
      // backdrop 에 걸려 있어서 여기서 발생한 Tab 은 거기까지 가지 않아요 — 직접 처리해야
      // Shift+Tab 이 배경 페이지로 새지 않아요.
      onKeyDown={(event) => {
        if (event.key !== "Tab") return;
        const card = activeTrapCard();
        if (!card) return; // 모달이 없으면 평범한 페이지 요소처럼 동작해요.
        if (moveFocusWithinTrap(card, { shiftKey: event.shiftKey })) {
          event.preventDefault();
        }
      }}
      // 두 영역으로 나눠요 — assertive 와 polite 를 한 컨테이너에 섞으면
      // 스크린리더가 우선순위를 구분하지 못해요.
      className="pointer-events-none fixed inset-x-0 bottom-0 z-[var(--z-toast)] flex flex-col items-center gap-2 p-4 sm:items-end"
    >
      <div aria-live="assertive" aria-atomic="false" className="contents">
        {items
          .filter((item) => item.tone === "error")
          .map((item) => (
            <ToastCard key={item.id} item={item} onDismiss={onDismiss} />
          ))}
      </div>
      <div aria-live="polite" aria-atomic="false" className="contents">
        {items
          .filter((item) => item.tone !== "error")
          .map((item) => (
            <ToastCard key={item.id} item={item} onDismiss={onDismiss} />
          ))}
      </div>
    </div>,
    document.body,
  );
}

function ToastCard({
  item,
  onDismiss,
}: {
  item: ToastItem;
  onDismiss: (id: number) => void;
}) {
  const style = TONE_STYLES[item.tone];
  // error 는 기본 0(수동 닫기) — 실패 원인을 놓치면 안 되니까요.
  const duration = toastDuration(item);
  const [entered, setEntered] = useState(false);

  useEffect(() => {
    const raf = requestAnimationFrame(() => setEntered(true));
    return () => cancelAnimationFrame(raf);
  }, []);

  useEffect(() => {
    if (duration <= 0) return;
    const timer = setTimeout(() => onDismiss(item.id), duration);
    return () => clearTimeout(timer);
  }, [duration, item.id, onDismiss]);

  return (
    <div
      data-agora-toast-id={item.id}
      role={item.tone === "error" ? "alert" : "status"}
      className={cn(
        "pointer-events-auto w-full max-w-sm rounded-xl border p-3.5 shadow-lg",
        "transition-all duration-150 ease-out motion-reduce:transition-none",
        entered ? "translate-y-0 opacity-100" : "translate-y-2 opacity-0",
        style.card,
      )}
    >
      <div className="flex items-start gap-2.5">
        <Icon name={style.iconName} size={16} className={cn("mt-0.5", style.icon)} />
        <div className="min-w-0 flex-1">
          <div className="text-sm font-semibold leading-tight">{item.title}</div>
          {item.description && (
            <p className="mt-1 break-words text-xs leading-relaxed opacity-90">
              {item.description}
            </p>
          )}
        </div>
        <button
          type="button"
          onClick={() => onDismiss(item.id)}
          aria-label="알림 닫기"
          className="-mr-1 -mt-1 shrink-0 rounded-lg p-1 opacity-60 transition-opacity hover:opacity-100"
        >
          <Icon name="close" size={14} />
        </button>
      </div>
    </div>
  );
}

/**
 * `const { success, error, toast } = useToast()`.
 *
 * Provider 밖에서 부르면 조용히 무시하지 않고 던져요 — 알림이 사라지는 버그를
 * 배포까지 들고 가지 않으려고요.
 */
export function useToast(): ToastApi {
  const api = useContext(ToastContext);
  if (!api) {
    throw new Error("useToast()는 <ToastProvider> 안에서만 쓸 수 있어요.");
  }
  return api;
}
