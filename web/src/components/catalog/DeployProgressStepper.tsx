import { DEPLOY_PHASES } from "@/components/playground/initializr-strands/lifecycle";
import { cn } from "@/lib/ui";

/** 배포 단계를 가로 점 스텝퍼로 그려요 (IH-88 시안 A).
 *
 * 왜 이 형태인가: '나의 요청'은 목록 화면이라 한 행에 들어가야 하고, 동시 진행이 여러 건일
 * 때 세로로 쌓여도 읽혀야 해요. 단계 이름을 모두 늘어놓으면(칩 나열) 좁은 화면에서
 * 줄바꿈되고 3건 이상에서 화면이 칩으로 가득 차요. 그래서 점·선으로 전체 여정을 그리고
 * **현재 단계 이름만** 라벨로 키워요.
 *
 * 단계 목록은 `DEPLOY_PHASES`를 그대로 재사용해요 — Initializr와 문구가 어긋나면 같은
 * 배포가 화면마다 다른 이름으로 보여요.
 */
export function DeployProgressStepper({
  phase,
  detail,
  elapsed,
  errorMessage,
  onRetry,
}: {
  phase: string;
  detail?: string | null;
  elapsed?: string | null;
  errorMessage?: string | null;
  onRetry?: () => void;
}) {
  const failed = phase === "FAILED";
  // FAILED는 `DEPLOY_PHASES`에 없어요(정상 여정이 아니니까요). 실패는 **직전에 진행하던
  // 단계 자리**에 ✕로 표시해야 "어디까지 갔는지"를 잃지 않아요. job이 실패 단계를 따로
  // 알려주지 않으므로, error.phase가 있으면 그걸 쓰고 없으면 마지막 미완료 단계로 봐요.
  const currentIndex = failed
    ? -1
    : DEPLOY_PHASES.findIndex((step) => step.phase === phase);

  return (
    <div className="mt-1.5 text-xs">
      <div className="flex items-center gap-1.5">
        <div className="flex items-center" aria-hidden>
          {DEPLOY_PHASES.map((step, index) => {
            const done = currentIndex >= 0 && index < currentIndex;
            const active = index === currentIndex;
            return (
              <span key={step.phase} className="flex items-center">
                {index > 0 && (
                  <span
                    className={cn(
                      "block h-px w-3.5",
                      done || active ? "bg-slate-500" : "bg-slate-200",
                    )}
                  />
                )}
                <span
                  title={step.label}
                  className={cn(
                    "block rounded-full",
                    active
                      ? "h-2.5 w-2.5 ring-2 ring-slate-400 ring-offset-1 bg-slate-700"
                      : done
                        ? "h-2 w-2 bg-slate-600"
                        : "h-2 w-2 border border-slate-300 bg-white",
                  )}
                />
              </span>
            );
          })}
        </div>
        <span
          className={cn(
            "font-medium",
            failed ? "text-red-700" : "text-slate-700",
          )}
        >
          {failed
            ? "배포 실패"
            : (DEPLOY_PHASES[currentIndex]?.label ?? phase)}
        </span>
      </div>
      {(detail || elapsed || errorMessage) && (
        <div className="mt-0.5 flex flex-wrap items-center gap-1 pl-1 text-slate-500">
          <span aria-hidden>└</span>
          {errorMessage ? (
            <span className="text-red-600">{errorMessage}</span>
          ) : (
            <>
              {detail && <span>{detail}</span>}
              {detail && elapsed && <span aria-hidden>·</span>}
              {elapsed && <span className="text-slate-400">{elapsed}</span>}
            </>
          )}
          {errorMessage && onRetry && (
            <button
              type="button"
              onClick={onRetry}
              className="ml-1 rounded border border-slate-200 px-1.5 py-0.5 text-slate-600 hover:bg-slate-50"
            >
              다시 시도
            </button>
          )}
        </div>
      )}
      <p className="sr-only">
        {failed
          ? `배포 실패: ${errorMessage ?? ""}`
          : `전체 ${DEPLOY_PHASES.length}단계 중 ${currentIndex + 1}번째 ` +
            `${DEPLOY_PHASES[currentIndex]?.label ?? phase} 단계예요.` +
            (detail ? ` ${detail}` : "")}
      </p>
    </div>
  );
}
