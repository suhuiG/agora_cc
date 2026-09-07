export function WizardProgress({
  current,
  total,
}: {
  current: number;
  total: number;
}) {
  return (
    <nav aria-label={`등록 진행: 총 ${total}단계 중 ${current}단계`} className="mb-6">
      <div className="mb-2 flex items-center justify-between text-xs">
        <span className="font-medium text-slate-700">등록 진행</span>
        <span className="text-slate-500">
          {current} / {total}단계
        </span>
      </div>
      <ol className="grid gap-2" style={{ gridTemplateColumns: `repeat(${total}, minmax(0, 1fr))` }}>
        {Array.from({ length: total }, (_, index) => {
          const step = index + 1;
          const complete = step < current;
          const active = step === current;
          return (
            <li
              key={step}
              aria-current={active ? "step" : undefined}
              className="flex min-w-0 items-center gap-2"
            >
              <span
                className={[
                  "flex h-7 w-7 shrink-0 items-center justify-center rounded-full border text-xs font-semibold",
                  complete
                    ? "border-emerald-600 bg-emerald-600 text-white"
                    : active
                      ? "border-slate-900 bg-slate-900 text-white"
                      : "border-slate-300 bg-white text-slate-500",
                ].join(" ")}
              >
                {step}
              </span>
              {index < total - 1 && (
                <span
                  aria-hidden="true"
                  className={`h-0.5 min-w-0 flex-1 ${complete ? "bg-emerald-600" : "bg-slate-200"}`}
                />
              )}
            </li>
          );
        })}
      </ol>
    </nav>
  );
}
