// shadcn 스타일 Button (경량 직접 구현).
import type { ButtonHTMLAttributes, Ref } from "react";
import { cn } from "@/lib/ui";

type ButtonVariant = "primary" | "outline" | "ghost" | "destructive";
type ButtonSize = "sm" | "md";

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  size?: ButtonSize;
  // React 19: forwardRef 없이 ref 를 평범한 prop 으로 받아요(모달 autoFocus 등).
  ref?: Ref<HTMLButtonElement>;
}

const VARIANT_CLASSES: Record<ButtonVariant, string> = {
  primary:
    "bg-primary text-primary-foreground hover:bg-primary/90 focus-visible:ring-ring",
  outline:
    "border border-input bg-card text-foreground hover:bg-accent focus-visible:ring-ring",
  ghost: "text-foreground hover:bg-accent focus-visible:ring-ring",
  destructive:
    "bg-red-600 text-white hover:bg-red-700 focus-visible:ring-red-500",
};

const SIZE_CLASSES: Record<ButtonSize, string> = {
  sm: "h-8 px-3 text-xs",
  md: "h-10 px-4 text-sm",
};

export function Button({
  variant = "primary",
  size = "md",
  className,
  ref,
  ...props
}: ButtonProps) {
  return (
    <button
      ref={ref}
      className={cn(
        "inline-flex items-center justify-center gap-1.5 rounded-lg font-medium transition-colors",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-offset-1",
        "disabled:pointer-events-none disabled:opacity-50",
        VARIANT_CLASSES[variant],
        SIZE_CLASSES[size],
        className,
      )}
      {...props}
    />
  );
}
