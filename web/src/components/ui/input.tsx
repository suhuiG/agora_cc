// shadcn 스타일 Input / Select (경량 직접 구현).
// React 19: forwardRef 없이 ref 를 평범한 prop 으로 받아요(모달 초기 포커스 등).
import type { InputHTMLAttributes, Ref, SelectHTMLAttributes } from "react";
import { cn } from "@/lib/ui";

interface InputProps extends InputHTMLAttributes<HTMLInputElement> {
  ref?: Ref<HTMLInputElement>;
}

export function Input({ className, ...props }: InputProps) {
  return (
    <input
      className={cn(
        "h-10 w-full rounded-lg border border-input bg-card px-3.5 text-sm text-foreground",
        "placeholder:text-muted-foreground",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-1",
        className,
      )}
      {...props}
    />
  );
}

export function Select({ className, ...props }: SelectHTMLAttributes<HTMLSelectElement>) {
  return (
    <select
      className={cn(
        "h-10 rounded-lg border border-input bg-card px-3 text-sm text-foreground",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-1",
        className,
      )}
      {...props}
    />
  );
}
