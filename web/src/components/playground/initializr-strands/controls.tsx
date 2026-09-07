import type { ReactNode } from "react";
import { cn } from "@/lib/ui";

export function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section>
      <h2 className="mb-3 text-sm font-semibold">{title}</h2>
      <div className="space-y-3">{children}</div>
    </section>
  );
}

export function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div>
      <label className="mb-1.5 block text-xs font-medium text-muted-foreground">{label}</label>
      {children}
    </div>
  );
}

export function SettingSection({
  title,
  onInfo,
  children,
}: {
  title: string;
  onInfo?: () => void;
  children: ReactNode;
}) {
  return (
    <section>
      <div className="mb-3 flex items-center gap-1.5">
        <h2 className="text-sm font-semibold">{title}</h2>
        {onInfo && (
          <button
            type="button"
            onClick={onInfo}
            aria-label={`${title} 설명 보기`}
            className="flex h-4 w-4 items-center justify-center rounded-full border border-slate-300 text-[10px] font-bold leading-none text-slate-500 transition-colors hover:border-blue-500 hover:text-blue-600"
          >
            ?
          </button>
        )}
      </div>
      <div className="space-y-3">{children}</div>
    </section>
  );
}

export function ChipGroup({
  options,
  value,
  onChange,
  cols,
}: {
  options: { v: string; label: string; sub?: string }[];
  value: string;
  onChange: (value: string) => void;
  cols: 2 | 3;
}) {
  return (
    <div className={cn("grid gap-2", cols === 2 ? "grid-cols-2" : "grid-cols-3")}>
      {options.map((option) => (
        <button
          key={option.v}
          type="button"
          onClick={() => onChange(option.v)}
          aria-pressed={value === option.v}
          className={cn(
            "rounded-lg border px-3 py-2.5 text-center text-xs font-semibold transition-colors",
            value === option.v
              ? "border-blue-500 bg-blue-50/60 text-blue-700 ring-1 ring-blue-500"
              : "border-input bg-card text-foreground hover:bg-accent/60",
          )}
        >
          {option.label}
          {option.sub && (
            <span className="mt-0.5 block text-[11px] font-normal text-muted-foreground">
              {option.sub}
            </span>
          )}
        </button>
      ))}
    </div>
  );
}

export function RangeRow({
  label,
  value,
  display,
  min,
  max,
  step,
  onChange,
}: {
  label: string;
  value: number;
  display: string;
  min: number;
  max: number;
  step: number;
  onChange: (value: number) => void;
}) {
  return (
    <div>
      <div className="mb-1 flex items-center justify-between">
        <span className="text-xs font-medium text-muted-foreground">{label}</span>
        <span className="font-mono text-sm font-semibold text-foreground">{display}</span>
      </div>
      <input
        type="range"
        min={min}
        max={max}
        step={step}
        value={value}
        onChange={(event) => onChange(Number(event.target.value))}
        className="w-full accent-blue-600"
      />
    </div>
  );
}

export function RadioCard({
  active,
  onClick,
  title,
  note,
}: {
  active: boolean;
  onClick: () => void;
  title: string;
  note: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={active}
      className={cn(
        "flex items-start gap-2.5 rounded-lg border px-3 py-2.5 text-left transition-colors",
        active
          ? "border-blue-500 bg-blue-50/60 ring-1 ring-blue-500"
          : "border-input bg-card hover:bg-accent/60",
      )}
    >
      <span
        className={cn(
          "mt-0.5 flex h-4 w-4 shrink-0 items-center justify-center rounded-full border",
          active ? "border-blue-500" : "border-slate-300",
        )}
      >
        {active && <span className="h-2 w-2 rounded-full bg-blue-500" />}
      </span>
      <span className="min-w-0">
        <span className="block text-sm font-medium text-foreground">{title}</span>
        <span className="block text-xs text-muted-foreground">{note}</span>
      </span>
    </button>
  );
}
