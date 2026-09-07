// 의존성 없는 인라인 SVG 아이콘. nav.ts 의 ICON_PATHS 를 dangerouslySetInnerHTML 로 렌더해요.
// (lucide-react 미설치 — 외부 의존성 0 원칙. path 는 우리가 정의한 신뢰된 정적 문자열이라 안전해요.)
import { ICON_PATHS } from "@/lib/nav";
import { cn } from "@/lib/ui";

interface IconProps {
  name: keyof typeof ICON_PATHS | string;
  size?: number;
  className?: string;
}

export function Icon({ name, size = 16, className }: IconProps) {
  const paths = ICON_PATHS[name] ?? "";
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={2}
      strokeLinecap="round"
      strokeLinejoin="round"
      className={cn("shrink-0", className)}
      dangerouslySetInnerHTML={{ __html: paths }}
    />
  );
}
