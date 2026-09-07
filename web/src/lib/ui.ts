// 경량 className 머지 헬퍼예요.
// clsx/tailwind-merge 의존성 없이, falsy 를 거르고 공백으로 join 하는 최소 구현이에요.
// (Tailwind v4 + React 19 + Next 16 최신 조합에서 외부 패키지 호환 리스크를 피하려고 직접 만들었어요.)
export type ClassValue = string | number | null | false | undefined;

export function cn(...classes: ClassValue[]): string {
  return classes.filter(Boolean).join(" ");
}
