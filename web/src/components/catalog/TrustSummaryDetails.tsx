import type { TrustSummary } from "@/lib/api/types";
import {
  overlapPresentation,
  TONE_CLASSES,
  trustPresentation,
  type TrustTone,
} from "@/lib/trust";

type TrustSummaryDetailsProps = {
  trust?: TrustSummary | null;
  compact?: boolean;
};

function StatusBadge({
  animated,
  children,
  tone,
}: {
  animated?: boolean;
  children: React.ReactNode;
  tone: TrustTone;
}) {
  return (
    <span
      className={`inline-flex min-h-7 items-center gap-2 rounded-full border px-2.5 py-1 text-xs font-semibold ${TONE_CLASSES[tone]}`}
    >
      {animated && (
        <span
          aria-hidden="true"
          className="size-2 rounded-full bg-blue-600 motion-safe:animate-pulse"
        />
      )}
      {children}
    </span>
  );
}

export function TrustSummaryDetails({
  trust,
  compact = false,
}: TrustSummaryDetailsProps) {
  const scan = trustPresentation(trust);
  const overlap = overlapPresentation(trust);
  if (!scan || !overlap) return null;

  if (compact) {
    return (
      <div
        role="group"
        aria-label={`${scan.ariaLabel}, ${overlap.ariaLabel}`}
        className="flex flex-wrap justify-start gap-1.5"
      >
        <StatusBadge animated={scan.animated} tone={scan.tone}>
          {scan.statusLabel}
          {scan.riskLabel && ` · ${scan.riskLabel}`}
        </StatusBadge>
        <StatusBadge tone="neutral">등급 {scan.tierLabel}</StatusBadge>
        <StatusBadge animated={overlap.animated} tone={overlap.tone}>
          {overlap.statusLabel}
        </StatusBadge>
      </div>
    );
  }

  return (
    <div
      role="group"
      aria-label={`${scan.ariaLabel}, ${overlap.ariaLabel}`}
      className="grid gap-4 sm:grid-cols-3"
    >
      <div>
        <p className="mb-1.5 text-xs text-slate-500">보안 심사</p>
        <StatusBadge animated={scan.animated} tone={scan.tone}>
          {scan.statusLabel}
          {scan.riskLabel && ` · ${scan.riskLabel}`}
        </StatusBadge>
      </div>
      <div>
        <p className="mb-1.5 text-xs text-slate-500">보안 등급</p>
        <StatusBadge tone="neutral">{scan.tierLabel}</StatusBadge>
      </div>
      <div>
        <p className="mb-1.5 text-xs text-slate-500">중복검토</p>
        <StatusBadge animated={overlap.animated} tone={overlap.tone}>
          {overlap.statusLabel}
        </StatusBadge>
      </div>
    </div>
  );
}
