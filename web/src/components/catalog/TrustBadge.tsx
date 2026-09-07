import type { TrustSummary } from "@/lib/api/types";
import { TONE_CLASSES, trustPresentation } from "@/lib/trust";

export function TrustBadge({ trust }: { trust?: TrustSummary | null }) {
  const presentation = trustPresentation(trust);
  if (!presentation) return null;

  return (
    <div
      role="group"
      aria-label={presentation.ariaLabel}
      className="mb-4 flex flex-wrap items-center gap-2"
    >
      <span
        className={`inline-flex min-h-7 items-center gap-2 rounded-full border px-2.5 py-1 text-xs font-semibold ${TONE_CLASSES[presentation.tone]}`}
      >
        {presentation.animated && (
          <span
            aria-hidden="true"
            className="size-2 rounded-full bg-blue-600 motion-safe:animate-pulse"
          />
        )}
        {presentation.statusLabel}
        {presentation.riskLabel && (
          <>
            <span aria-hidden="true">·</span>
            <span>{presentation.riskLabel}</span>
          </>
        )}
      </span>
      <span className="inline-flex min-h-7 items-center rounded-full border border-slate-300 bg-white px-2.5 py-1 text-xs font-medium text-slate-700">
        보안 등급 · {presentation.tierLabel}
      </span>
    </div>
  );
}
