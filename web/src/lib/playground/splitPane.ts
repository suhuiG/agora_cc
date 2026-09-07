/**
 * 분할 크기 계산 — 컴포넌트에서 떼어낸 순수 로직이에요(그래서 테스트가 돼요).
 *
 * 좌우(가로 분할)와 상하(세로 분할)에 같은 로직을 써요. 크기는 "앞쪽 패널이 차지하는
 * 비율(%)" 하나로 표현하고, 최소·최대는 두 패널 모두 쓸 수 있는 크기를 남기려고 둬요.
 * 새로고침 후 유지는 localStorage 로 하고, 분할마다 저장 키가 달라요.
 */

/** 분할 하나의 규격. 좌우·상하가 서로 다른 최소·최대·기본값을 가져요. */
export type SplitSpec = {
  /** 포인터 축. `x` 는 좌우 분할, `y` 는 상하 분할이에요. */
  axis: "x" | "y";
  min: number;
  max: number;
  default: number;
  storageKey: string;
};

/** 좌우 분할 — 대화 / 오른쪽 패널. */
export const COLUMNS_SPLIT: SplitSpec = {
  axis: "x",
  min: 28,
  max: 72,
  default: 50,
  storageKey: "agora.playground.splitRatio",
};

/**
 * 상하 분할 — 실행 구성 카드 / 활동 트리 카드.
 *
 * 최소를 12% 로 두는 이유: 구성 카드는 접어두고 트리만 크게 보고 싶은 경우가 흔해요.
 * 최대 78% 는 트리 카드가 헤더+지표 타일만으로도 안 눌리게 남기는 값이에요.
 */
export const ROWS_SPLIT: SplitSpec = {
  axis: "y",
  min: 12,
  max: 78,
  default: 42,
  storageKey: "agora.playground.stackRatio",
};

/** 키보드 한 번에 움직이는 크기. Shift 를 누르면 크게 움직여요. */
export const STEP = 2;
export const LARGE_STEP = 10;

export function clampRatio(ratio: number, spec: SplitSpec = COLUMNS_SPLIT): number {
  if (!Number.isFinite(ratio)) return spec.default;
  return Math.min(spec.max, Math.max(spec.min, Math.round(ratio)));
}

/**
 * 포인터 좌표 → 앞쪽 패널 비율. `position` 은 축에 맞는 clientX/clientY 이고 `rect` 는
 * 컨테이너의 시작 좌표와 크기예요. 컨테이너 크기가 0이면 기존 값을 지켜요.
 */
export function ratioFromPointer(
  position: number,
  rect: { start: number; size: number },
  current: number,
  spec: SplitSpec = COLUMNS_SPLIT,
): number {
  if (rect.size <= 0) return current;
  return clampRatio(((position - rect.start) / rect.size) * 100, spec);
}

/**
 * 키보드 조작. 반환값이 `null` 이면 그 키는 우리 것이 아니에요(브라우저 기본 동작 유지).
 * separator 의 접근성 계약은 WAI-ARIA window splitter 패턴을 따라요 — 좌우 분할은
 * 좌/우 화살표, 상하 분할은 위/아래 화살표만 받아요. 축이 다른 화살표를 가로채면
 * 페이지 스크롤·캐럿 이동을 뺏어가요.
 */
export function ratioForKey(
  key: string,
  current: number,
  options: { shift?: boolean; spec?: SplitSpec } = {},
): number | null {
  const spec = options.spec ?? COLUMNS_SPLIT;
  const step = options.shift ? LARGE_STEP : STEP;
  const decrease = spec.axis === "x" ? "ArrowLeft" : "ArrowUp";
  const increase = spec.axis === "x" ? "ArrowRight" : "ArrowDown";
  if (key === decrease) return clampRatio(current - step, spec);
  if (key === increase) return clampRatio(current + step, spec);
  switch (key) {
    case "Home":
      return spec.min;
    case "End":
      return spec.max;
    case "Enter":
    case "Escape":
      // 기본 크기로 되돌려요 — 잘못 끌어서 좁아졌을 때의 탈출구예요.
      return spec.default;
    default:
      return null;
  }
}

export function loadRatio(
  storage: Pick<Storage, "getItem"> | null | undefined,
  spec: SplitSpec = COLUMNS_SPLIT,
): number {
  if (!storage) return spec.default;
  try {
    const raw = storage.getItem(spec.storageKey);
    if (raw === null) return spec.default;
    const parsed = Number(raw);
    // 저장된 값이 손상됐으면 조용히 기본값으로 — 사용자를 막을 이유가 없어요.
    return Number.isFinite(parsed) ? clampRatio(parsed, spec) : spec.default;
  } catch {
    return spec.default;
  }
}

export function saveRatio(
  storage: Pick<Storage, "setItem"> | null | undefined,
  ratio: number,
  spec: SplitSpec = COLUMNS_SPLIT,
): void {
  if (!storage) return;
  try {
    storage.setItem(spec.storageKey, String(clampRatio(ratio, spec)));
  } catch {
    // Safari 프라이빗 모드 등에서 던져요. 크기 유지를 못 해도 화면은 살아 있어야 해요.
  }
}
