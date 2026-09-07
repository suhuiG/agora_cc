"use client";

import Link from "next/link";
import { Suspense, useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";
import useSWR from "swr";
import {
  listMyRequests,
  pokeRequests,
  type PublishRequestItem,
  type PublishRequestPage,
} from "@/lib/api";
import { DeployProgressStepper } from "@/components/catalog/DeployProgressStepper";
import { TrustSummaryDetails } from "@/components/catalog/TrustSummaryDetails";
import { ApprovalBlockNotice } from "@/components/catalog/ApprovalBlockNotice";
import { ConversationManagerStatus } from "@/components/playground/ConversationManagerStatus";
import { useToast } from "@/components/ui/toast";
import {
  isRequestActive,
  publishMeta,
  requestErrorText,
  shouldPokeRequest,
} from "@/lib/requestStatus";

// kind → 한국어 라벨.
const KIND_LABELS: Record<string, string> = {
  "deploy-mcp": "MCP 배포형",
  "deploy-agent": "Agent 배포형",
  "tool-request": "도구 권한 신청",
  "skill": "Skill",
  "mcp-connect": "MCP 연결형",
  "agent-json": "Agent (JSON)",
  "agent-domain": "Agent (도메인)",
};

// 마지막 전진 이후 경과. 배포는 정상적으로도 수 분 걸리므로(실측 412초) 경과를 보여줘야
// 사용자가 멈춘 것과 진행 중인 것을 구분할 수 있어요.
function fmtElapsed(iso: string): string {
  if (!iso) return "";
  const started = new Date(iso).getTime();
  if (isNaN(started)) return "";
  const sec = Math.max(0, Math.round((Date.now() - started) / 1000));
  if (sec < 60) return `${sec}초 전 갱신`;
  const min = Math.floor(sec / 60);
  return `${min}분 ${sec % 60}초 전 갱신`;
}

// 요청일시를 KST(Asia/Seoul) 기준 `yyyy-mm-dd hh:mm:ss (KST)` 형식으로 표시해요.
// QueueClient의 fmtDate와 같이 Intl.DateTimeFormat + timeZone:"Asia/Seoul"을 쓰되,
// 로케일 구분자에 흔들리지 않게 formatToParts로 정확한 형식을 조립해요.
function fmtKst(iso: string): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso; // 파싱 불가 → 원본 폴백
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Seoul",
    year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23",
  }).formatToParts(d);
  const get = (t: string) => parts.find((p) => p.type === t)?.value ?? "";
  return `${get("year")}-${get("month")}-${get("day")} ${get("hour")}:${get("minute")}:${get("second")} (KST)`;
}

/** `useSearchParams()` 는 정적 프리렌더에서 Suspense 경계를 요구해요(Next.js). 본문을
 *  내부 컴포넌트로 두고 default export 가 그 경계를 제공해요. */
export default function RequestsPage() {
  return (
    <Suspense fallback={null}>
      <RequestsPageBody />
    </Suspense>
  );
}


function RequestsPageBody() {
  // Initializr 가 업로드를 기다리지 않고 바로 여기로 보내요(2026-08-30). 그래서 도착한
  // 시점에는 **job 이 아직 없어요** — `active` 가 false 라 아래 폴링 타이머가 안 걸리고,
  // 사용자는 자기 요청이 없는 목록을 보게 돼요. 이 신호로 그 창을 메워요.
  const startingDeploy = useSearchParams().get("starting") === "1";
  // 포기 여부만 상태로 둬요. "아직 기다리는 중" 은 아래에서 파생값으로 계산해요 —
  // effect 안에서 동기적으로 setState 하면 `react-hooks/set-state-in-effect` 에 걸려요.
  const [gaveUpWaiting, setGaveUpWaiting] = useState(false);
  const [cursors, setCursors] = useState<(string | null)[]>([null]);
  const [pageIndex, setPageIndex] = useState(0);
  const cursor = cursors[pageIndex] ?? null;
  const { data, error, isLoading, mutate } = useSWR<PublishRequestPage>(
    ["my-requests", cursor],
    () => listMyRequests(undefined, cursor),
  );

  // IH-77: 이 화면이 배포 진행의 단일 출처예요. 그래서 갱신이 확실해야 해요.
  //
  // 원래는 SWR의 `refreshInterval`을 함수형으로 줬는데(진행 중이면 3초), 실측(2026-08-22
  // 브라우저 e2e)에서 **2회만 호출되고 멈췄어요** — 진행 중 배포가 있는데도요. 원인 추적
  // 대신 갱신 주기를 우리가 직접 소유해요. 진행 중 항목이 없으면 타이머를 걸지 않아요.
  //
  // 주의: 여기서 배포 job 상태를 직접 조회하면 그 조회가 job을 **전진**시켜요(IH-80).
  // 그래서 요청 목록만 다시 읽어요 — phase는 서버가 job 전진 시 복사해 둔 값이에요.
  const active = !!data?.items.some(isRequestActive);
  // 방금 시작한 배포가 목록에 뜰 때까지만 짧게 폴링해요. 뜨면(=`active`) 아래 타이머가
  // 이어받고, 안 뜨면 90초 뒤 포기해요 — 무한 폴링을 만들지 않아요.
  const awaitingNewRequest = startingDeploy && !active && !gaveUpWaiting;
  useEffect(() => {
    if (!awaitingNewRequest) return;
    let cancelled = false;
    const started = Date.now();
    let timer: ReturnType<typeof setTimeout>;
    const tick = async () => {
      if (Date.now() - started > 90_000) {
        setGaveUpWaiting(true);
        return;
      }
      try {
        await mutate();
      } finally {
        if (!cancelled) timer = setTimeout(tick, 2000);
      }
    };
    timer = setTimeout(tick, 2000);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [awaitingNewRequest, mutate]);

  useEffect(() => {
    if (!active) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    const tick = async () => {
      try {
        await mutate();
      } finally {
        // 응답이 느려도(실측 10~12초) 겹치지 않게 완료 후에 다음 주기를 걸어요.
        if (!cancelled) timer = setTimeout(tick, 3000);
      }
    };
    timer = setTimeout(tick, 3000);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [active, mutate]);

  // 재요청(poke) 진행 중인 request_id — 버튼 로딩 표시용.
  const [poking, setPoking] = useState<string | null>(null);
  const { error: toastError } = useToast();

  async function poke(r: PublishRequestItem) {
    if (!r.job_id || poking) return;
    setPoking(r.request_id);
    try {
      // 서버 poll_once가 진행 중 job을 1단계 advance + 요청 status를 sync해요.
      // 현재 페이지는 별도로 재검증해 cursor 위치를 보존해요.
      await pokeRequests();
      await mutate();
    } catch (err) {
      // 실패해도 목록은 재검증해서 최신 상태를 보여줘요.
      // 조용히 끝내면 버튼을 눌러도 아무 일 없는 것처럼 보여서 알림을 띄워요.
      await mutate();
      toastError(
        "진행 상황을 갱신하지 못했어요.",
        err instanceof Error ? err.message : "잠시 후 다시 시도해 주세요.",
      );
    } finally {
      setPoking(null);
    }
  }

  function nextPage() {
    if (!data?.next_cursor) return;
    setCursors((current) => [
      ...current.slice(0, pageIndex + 1),
      data.next_cursor,
    ]);
    setPageIndex((current) => current + 1);
  }

  return (
    <div className="max-w-3xl mx-auto">
      <h1 className="text-2xl font-bold mb-1">나의 요청</h1>
      <p className="text-sm text-slate-400 mb-6">
        퍼블리시·배포 요청의 진행 현황이에요. 배포는 자리를 떠나도 백그라운드에서 완주해요.
      </p>

      {awaitingNewRequest && (
        <div
          className="mb-4 rounded-lg border border-amber-500/40 bg-amber-500/10 p-3 text-sm text-slate-900"
          data-testid="requests-awaiting-new"
        >
          코드를 올리고 배포를 시작하고 있어요. 목록에 나타나면 자동으로 갱신돼요 —
          이 화면을 떠나도 백그라운드에서 완주해요.
        </div>
      )}

      {error && (
        <div className="text-red-600 text-sm bg-red-50 p-3 rounded-lg">
          요청 목록을 불러오지 못했어요.
        </div>
      )}

      {isLoading && <div className="text-slate-400 text-sm">불러오는 중…</div>}

      {data && data.items.length === 0 && data.total_count === 0 && (
        <div className="text-slate-500 text-sm border border-dashed border-slate-200 rounded-lg p-8 text-center">
          아직 요청이 없어요.{" "}
          <Link href="/catalog/publish" className="text-blue-600 hover:underline">
            퍼블리시하러 가기
          </Link>
        </div>
      )}

      {data && (data.items.length > 0 || pageIndex > 0) && (
        <>
          {data.items.length === 0 ? (
            <div className="border border-dashed border-slate-200 rounded-lg p-8 text-center text-sm text-slate-500">
              이 페이지의 요청이 삭제되었어요. 이전 페이지로 돌아가 주세요.
            </div>
          ) : (
            <div className="border border-slate-200 rounded-lg divide-y divide-slate-100">
              {data.items.map((r) => {
                const sm = publishMeta(r);
                const assetExists = r.catalog_status !== "DELETED";
                return (
                <div
                  key={r.request_id}
                  className="flex flex-col gap-3 px-4 py-3 sm:flex-row sm:items-center"
                >
                {isRequestActive(r) && (
                  <span className="inline-block h-3 w-3 shrink-0 rounded-full border-2 border-slate-300 border-t-slate-700 animate-spin" />
                )}
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2">
                    {r.record_id && assetExists ? (
                      <Link
                        href={`/catalog/assets/${encodeURIComponent(r.record_id)}`}
                        className="font-medium text-slate-800 truncate hover:underline"
                      >
                        {r.title}
                      </Link>
                    ) : (
                      <span className="font-medium text-slate-800 truncate">{r.title}</span>
                    )}
                    <span className="text-xs bg-slate-100 text-slate-600 px-2 py-0.5 rounded shrink-0">
                      {KIND_LABELS[r.kind] ?? r.kind}
                    </span>
                  </div>
                  <div className="text-xs text-slate-400 mt-0.5">{fmtKst(r.created_at || r.updated_at)}</div>
                  {r.kind === "deploy-agent" && r.record_id && (
                    <div className="mt-1 flex items-center gap-2">
                      <span className="text-xs text-slate-400">실제 context 전략</span>
                      <ConversationManagerStatus
                        manager={r.conversation_manager ?? null}
                        compact
                      />
                    </div>
                  )}
                  {r.phase && (isRequestActive(r) || r.error) && (
                    <DeployProgressStepper
                      phase={r.error ? "FAILED" : r.phase}
                      detail={r.phase_detail}
                      elapsed={fmtElapsed(r.updated_at)}
                      errorMessage={requestErrorText(r)}
                      onRetry={shouldPokeRequest(r) ? () => void poke(r) : undefined}
                    />
                  )}
                  {r.error && !r.phase && (
                    <div className="text-xs text-red-600 mt-1 whitespace-pre-line">
                      {requestErrorText(r)}
                    </div>
                  )}
                  {/* 내 자산이 왜 대기 중인지 + 내가 무엇을 하면 풀리는지(R3).
                      예전엔 "심사중"만 보여서 담당자 연락처가 비어 승인이 막힌 걸
                      등록자가 알 방법이 없었어요. */}
                  <ApprovalBlockNotice
                    block={r.governance?.approval_block}
                    audience="owner"
                  />
                </div>
                <div className="flex shrink-0 items-end gap-3 self-stretch sm:self-auto">
                  {shouldPokeRequest(r) && (
                    <button
                      onClick={() => poke(r)}
                      disabled={poking !== null}
                      title="배포를 한 단계 강제로 진행시켜요 (poller 대체)"
                      className="mb-0.5 text-xs px-2.5 py-1 rounded shrink-0 border border-slate-200 text-slate-600 hover:bg-slate-50 hover:border-slate-300 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
                    >
                      {poking === r.request_id ? "진행 중…" : "재요청"}
                    </button>
                  )}
                  <div className="flex min-w-0 flex-1 flex-col items-start gap-1 sm:flex-none">
                    <span className="text-xs text-slate-400">심사</span>
                    {r.governance ? (
                      <TrustSummaryDetails trust={r.governance} compact />
                    ) : (
                      <span className="inline-flex min-h-7 items-center rounded-full border border-slate-300 bg-slate-100 px-2.5 py-1 text-xs font-semibold text-slate-600">
                        시작 전
                      </span>
                    )}
                  </div>
                  <div className="flex flex-col items-start gap-1">
                    <span className="text-xs text-slate-400">게시</span>
                    <span className={`min-h-7 whitespace-nowrap text-xs px-2.5 py-1 rounded-full shrink-0 ${sm.cls}`}>
                      {sm.label}
                    </span>
                  </div>
                </div>
                </div>
                );
              })}
            </div>
          )}
          <div className="mt-4 flex items-center justify-between gap-3">
            <span className="text-xs text-slate-500">
              {data.items.length > 0 ? (
                <>
                  {pageIndex * 10 + 1}-
                  {Math.min(pageIndex * 10 + data.items.length, data.total_count)}
                  {" / "}{data.total_count}건
                </>
              ) : (
                <>0 / {data.total_count}건</>
              )}
            </span>
            <div className="flex items-center gap-2">
              <button
                type="button"
                onClick={() => setPageIndex((current) => Math.max(0, current - 1))}
                disabled={pageIndex === 0}
                className="min-w-16 rounded border border-slate-200 px-3 py-1.5 text-sm text-slate-700 hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-40"
              >
                이전
              </button>
              <span className="min-w-12 text-center text-sm text-slate-600">
                {pageIndex + 1}쪽
              </span>
              <button
                type="button"
                onClick={nextPage}
                disabled={!data.next_cursor}
                className="min-w-16 rounded border border-slate-200 px-3 py-1.5 text-sm text-slate-700 hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-40"
              >
                다음
              </button>
            </div>
          </div>
        </>
      )}
    </div>
  );
}
