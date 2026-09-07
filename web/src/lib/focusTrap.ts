// 모달 포커스 트랩의 순환 계산 — Modal 과 ConfirmDialog 가 공유해요.
//
// 왜 순환 목록에 토스트를 넣나: 토스트는 `document.body` 형제 포털이라 모달 트랩 밖이에요.
// 실패 토스트는 수동으로만 닫히는데, 트랩이 모달 안만 돌면 키보드 사용자가 그 닫기
// 버튼에 절대 닿지 못해요. 그래서 모달 → 토스트 → 모달 순으로 돌려요.
//
// 왜 브라우저 기본 Tab 동작에 맡기지 않나: 키 핸들러가 모달 backdrop 에 걸려 있어서,
// 포커스가 토스트(형제 포털)로 넘어가면 이후 Tab 이벤트가 핸들러에 도달하지 않아요.
// 매번 우리가 계산해서 옮기면 그 지점에서도 트랩이 유지돼요.

const FOCUSABLE_SELECTOR =
  'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

/** 토스트 뷰포트 식별자 — toast.tsx 가 이 속성을 달아요. */
export const TOAST_ROOT_ATTR = "data-agora-toast-root";

/**
 * 트랩 순환 대상 목록 — 모달 카드 안 + (있으면) 토스트 영역.
 * 호출 시점에 다시 조회해요. 모달 내용이 동적으로 바뀌어도(코드 블록 등장 등) 따라가요.
 */
export function trapTargets(card: HTMLElement): HTMLElement[] {
  const toastRoot = document.querySelector<HTMLElement>(`[${TOAST_ROOT_ATTR}]`);
  return [
    ...card.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR),
    ...(toastRoot ? toastRoot.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR) : []),
  ];
}

/**
 * Tab 키를 받아 다음 포커스 대상으로 옮겨요. 옮겼으면 true.
 *
 * 포커스가 트랩 밖이면(예: 토스트 버튼을 마우스로 누른 뒤) 첫 요소로 되돌려요.
 * 대상이 없으면 아무것도 하지 않고 false — 호출부가 기본 동작을 막지 않게요.
 */
/** 포커스 가능한 자식이 없을 때의 최후 수단 — 카드 자체를 잡아요.
 *  tabIndex=-1 은 프로그램 포커스만 허용하니 Tab 순서를 오염시키지 않아요. */
function focusCardItself(card: HTMLElement): void {
  card.tabIndex = -1;
  card.focus();
}

export function moveFocusWithinTrap(
  card: HTMLElement | null,
  options: { shiftKey: boolean },
): boolean {
  if (!card) return false;
  const targets = trapTargets(card);
  if (targets.length === 0) {
    // 처리 중(busy)이면 모든 버튼이 disabled 라 대상이 0개가 될 수 있어요.
    // 기본 동작에 맡기면 포커스가 배경으로 새서, 카드 자체를 잡아 트랩을 유지해요.
    focusCardItself(card);
    return true;
  }
  const active = document.activeElement as HTMLElement | null;
  const index = active ? targets.indexOf(active) : -1;
  const next =
    index === -1
      ? targets[0]
      : targets[(index + (options.shiftKey ? -1 : 1) + targets.length) % targets.length];
  next.focus();
  return true;
}

// ---------------------------------------------------------------------------
// 활성 트랩 등록소
// ---------------------------------------------------------------------------
//
// 모달의 키 핸들러는 모달 backdrop 에 걸려 있어요. 포커스가 토스트(body 형제 포털)로
// 넘어가면 그쪽 Tab 이벤트는 모달 핸들러에 도달하지 않아서, 한 방향(Shift+Tab)이
// 배경 페이지로 새요. 그래서 토스트 뷰포트도 같은 순환 함수를 호출해야 해요.
//
// 열려 있는 모달이 자기 카드를 여기 등록하고, 토스트가 그 카드를 기준으로 순환해요.
// 중첩은 지원하지 않아요(현재 화면에 중첩 모달이 없어요) — 마지막에 열린 것이 이겨요.

let activeCard: HTMLElement | null = null;

/**
 * 포커스를 트랩 안으로 되돌려요. 모달이 없으면 아무것도 하지 않아요(false).
 *
 * 토스트가 사라져 포커스가 body 로 떨어지는 모든 경로에서 써요 — 닫기 클릭, 타이머
 * 소멸, 큐 상한에 밀려 제거되는 경우까지. busy 로 대상이 0개면 카드 자체를 잡아요.
 */
export function restoreFocusToTrap(): boolean {
  const card = activeTrapCard();
  if (!card) return false;
  const target = card.querySelector<HTMLElement>(FOCUSABLE_SELECTOR);
  if (target) {
    target.focus();
  } else {
    // busy 등으로 포커스 가능한 자식이 없으면 카드를 잡아 트랩을 유지해요.
    focusCardItself(card);
  }
  return true;
}

/** 모달이 열릴 때 자기 카드를 등록해요. 반환값을 호출하면 해제돼요. */
export function registerTrap(card: HTMLElement | null): () => void {
  if (!card) return () => {};
  const previous = activeCard;
  activeCard = card;
  return () => {
    // 자기 것일 때만 해제해요 — 다른 모달이 이미 덮었으면 건드리지 않아요.
    if (activeCard === card) activeCard = previous;
  };
}

/**
 * 지금 열려 있는 모달 카드. 없으면 null.
 *
 * DOM 에 붙어 있는지 확인해요. 모달이 예상과 다른 순서로 닫히면(형제 컴포넌트의 effect
 * cleanup 순서는 보장되지 않아요) 떼어진 노드가 등록된 채 남을 수 있어요. 그 상태로
 * 순환하면 detached 요소에 focus() 를 걸어 Tab 이 죽은 것처럼 보여요.
 */
export function activeTrapCard(): HTMLElement | null {
  if (activeCard && !activeCard.isConnected) {
    activeCard = null;
  }
  return activeCard;
}
