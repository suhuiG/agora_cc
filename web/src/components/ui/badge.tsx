// shadcn 스타일 Badge (pill) 컴포넌트.
// UI 원칙: 라운드 박스(pill)는 "태그" 표현에만 써요.
// variant:
//  - tag      : 태그 칩 (회색, 기본)
//  - type     : 자산 타입 색상 칩 (color prop 으로 Tailwind 색 클래스를 직접 받음)
//  - outline  : 테두리만 있는 칩
import type { HTMLAttributes } from "react";
import { cn } from "@/lib/ui";

type BadgeVariant = "tag" | "type" | "outline";

interface BadgeProps extends HTMLAttributes<HTMLSpanElement> {
  variant?: BadgeVariant;
}

const VARIANT_CLASSES: Record<BadgeVariant, string> = {
  tag: "bg-muted text-muted-foreground",
  type: "", // color 는 호출부에서 className 으로 주입 (예: bg-blue-100 text-blue-800)
  outline: "border border-border text-muted-foreground",
};

export function Badge({ variant = "tag", className, ...props }: BadgeProps) {
  return (
    <span
      className={cn(
        "inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium",
        VARIANT_CLASSES[variant],
        className,
      )}
      {...props}
    />
  );
}
