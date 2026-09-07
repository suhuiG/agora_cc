// 토스트 큐 정책 — JSX 가 없는 순수 로직이라 별도 모듈로 둬요.
// (node --experimental-strip-types 가 .tsx 를 못 읽어서 테스트를 위해서도 분리가 필요해요.)

export type ToastTone = "success" | "error" | "info";

export interface ToastOptions {
  /** 한 줄 요약. 필수. */
  title: string;
  /** 보조 설명 (원인·다음 행동). 실패 토스트에 특히 유용해요. */
  description?: string;
  tone?: ToastTone;
  /**
   * 자동 소멸까지 ms. 0 이면 자동으로 안 닫혀요.
   * 기본값: error 는 0(수동 닫기), 나머지는 4500.
   */
  durationMs?: number;
}

export interface ToastItem extends ToastOptions {
  id: number;
  tone: ToastTone;
}

const DEFAULT_DURATION_MS = 4500;
/** 자동 소멸 안 하는(수동 닫기) 항목의 최대 보관 수. */
const STICKY_CAP = 6;
/** 자동 소멸하는 항목의 최대 동시 표시 수. */
const TRANSIENT_CAP = 3;

/** 자동 소멸까지 남은 ms. 0 이면 수동으로만 닫혀요(error 기본값). */
export function toastDuration(item: Pick<ToastItem, "tone" | "durationMs">): number {
  return item.durationMs ?? (item.tone === "error" ? 0 : DEFAULT_DURATION_MS);
}

/**
 * 큐에 항목을 넣고 표시 개수를 제한해요.
 *
 * 전체를 `slice(-N)` 하면 안 돼요 — 수동으로만 닫히는 실패 토스트가 뒤이은 성공
 * 알림에 밀려 사라져서 "실패는 사용자가 닫을 때까지 남는다"는 계약이 깨져요.
 * 그래서 sticky(수동)와 transient(자동)를 따로 세고, 표시 순서는 id 순으로 유지해요.
 */
export function enqueueToast(current: ToastItem[], incoming: ToastItem): ToastItem[] {
  const next = [...current, incoming];
  const sticky = next.filter((item) => toastDuration(item) === 0);
  const transient = next.filter((item) => toastDuration(item) !== 0);
  return [...sticky.slice(-STICKY_CAP), ...transient.slice(-TRANSIENT_CAP)].sort(
    (a, b) => a.id - b.id,
  );
}
