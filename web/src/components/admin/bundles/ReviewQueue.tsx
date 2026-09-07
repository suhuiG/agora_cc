"use client";

import { useState } from "react";
import useSWR from "swr";
import {
  listBundles, reviewBundle,
  type BundleSummary, type ReviewResult,
} from "@/lib/api";
import { Button } from "@/components/ui/button";
import { useToast } from "@/components/ui/toast";

/**
 * 검토 대기(PENDING) 그룹 큐. 승인하면 배포(PR 생성)까지 이어져요.
 *
 * SWR 키는 `BundlesClient`와 같은 `admin-bundles`를 써요 — 같은 엔드포인트를 두 번
 * 부르지 않고, 검토 후 mutate 하면 아래 플러그인 목록의 상태도 같이 갱신돼요.
 * 목록 로딩 실패 문구도 같은 페이지의 `BundlesClient`가 이미 보여주니, 여기서는
 * 대기 건이 없으면 아무것도 렌더하지 않아요(빈 큐는 노이즈예요).
 */
export function ReviewQueue({ onReviewed }: { onReviewed?: () => void }) {
  const { data, mutate } = useSWR<BundleSummary[]>("admin-bundles", () => listBundles());
  // 방금 처리한 결과 — 승인/거부하면 그 그룹은 PENDING에서 빠지지만, PR 링크·배포 실패
  // 사유는 계속 보여야 해요. 그래서 처리한 그룹을 이 맵에 남겨 큐에 붙잡아 둬요.
  const [reviewed, setReviewed] = useState<Record<string, ReviewResult>>({});

  const rows = (data ?? []).filter(
    (b) => b.request_status === "PENDING" || b.bundle_id in reviewed,
  );
  const pendingCount = rows.filter((b) => !(b.bundle_id in reviewed)).length;

  if (rows.length === 0) return null;

  return (
    <section className="mb-8 rounded-xl border border-amber-200 bg-amber-50/50 p-4">
      <h2 className="mb-1 text-sm font-semibold text-amber-900">
        {pendingCount > 0 ? `검토 대기 ${pendingCount}건` : "검토 결과"}
      </h2>
      <p className="mb-3 text-xs text-amber-800">
        승인하면 연결된 repo에 PR이 열려요. 그다음 조직 콘솔에서 marketplace를 등록해요.
      </p>
      <ul className="space-y-2">
        {rows.map((b) => (
          <ReviewRow
            key={b.bundle_id}
            bundle={b}
            result={reviewed[b.bundle_id] ?? null}
            onDone={(res) => {
              setReviewed((m) => ({ ...m, [b.bundle_id]: res }));
              mutate();
              onReviewed?.();
            }}
          />
        ))}
      </ul>
    </section>
  );
}

function ReviewRow({
  bundle,
  result,
  onDone,
}: {
  bundle: BundleSummary;
  result: ReviewResult | null;
  onDone: (res: ReviewResult) => void;
}) {
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState<"approve" | "reject" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const done = result !== null;

  const { success, error: toastError } = useToast();

  async function act(approved: boolean) {
    setBusy(approved ? "approve" : "reject");
    setError(null);
    try {
      const res = await reviewBundle(bundle.bundle_id, approved, note);
      // 결과가 세 갈래예요 — 배포 성공 / 거부 / 승인은 됐지만 배포 실패.
      if (res.published) {
        success("배포했어요.", `${bundle.name} · v${res.version}`);
      } else if (res.request_status === "REJECTED") {
        success("거부 처리했어요.", bundle.name);
      } else {
        toastError("승인했지만 배포에 실패했어요.", res.error ?? bundle.name);
      }
      onDone(res);
    } catch (err) {
      const message = err instanceof Error ? err.message : "검토 처리에 실패했어요";
      setError(message);
      toastError("검토 처리에 실패했어요.", message);
    } finally {
      setBusy(null);
    }
  }

  return (
    <li className="rounded-lg border border-border bg-background p-3">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="truncate text-sm font-medium">{bundle.name}</p>
          <p className="mt-0.5 text-xs text-muted-foreground">
            신청자 {bundle.owner_principal || "(알 수 없음)"} · 자산{" "}
            {bundle.member_count}개 · {bundle.surfaces.join(", ") || "대상 없음"}
          </p>
        </div>
        {!done && (
          <div className="flex shrink-0 items-center gap-2">
            <Button size="sm" onClick={() => act(true)} disabled={busy !== null}>
              {busy === "approve" ? "승인 중…" : "승인"}
            </Button>
            <Button
              size="sm"
              variant="outline"
              onClick={() => act(false)}
              disabled={busy !== null}
            >
              {busy === "reject" ? "처리 중…" : "거부"}
            </Button>
          </div>
        )}
      </div>

      {!done && (
        <>
          <label htmlFor={`review-note-${bundle.bundle_id}`} className="sr-only">
            검토 메모
          </label>
          <input
            id={`review-note-${bundle.bundle_id}`}
            value={note}
            onChange={(e) => setNote(e.target.value)}
            placeholder="검토 메모 (선택)"
            className="mt-2 w-full rounded-md border border-border bg-background px-2.5 py-1.5 text-xs"
          />
        </>
      )}

      {result && (
        <div className="mt-2 text-xs">
          {result.published ? (
            <span className="text-emerald-700">
              배포됨 · v{result.version}
              {result.pr_url && (
                <>
                  {" · "}
                  <a
                    href={result.pr_url}
                    target="_blank"
                    rel="noreferrer"
                    className="underline"
                  >
                    PR 열기
                  </a>
                </>
              )}
            </span>
          ) : result.request_status === "REJECTED" ? (
            <span className="text-slate-600">거부 처리됐어요.</span>
          ) : (
            // 승인은 됐지만 배포가 실패한 경우 — 사유가 조치 방법을 담고 있어요.
            <span className="text-red-600">배포 실패: {result.error}</span>
          )}
        </div>
      )}
      {error && <p className="mt-2 text-xs text-red-600">{error}</p>}
    </li>
  );
}
