"use client";

import { useEffect, useId, useRef } from "react";
import useSWR from "swr";
import { getBundleHandoff, type HandoffInfo } from "@/lib/api";
import { Button } from "@/components/ui/button";

/**
 * 콘솔 인계 안내 — Agora는 PR까지, 그다음은 관리자가 콘솔에서 해요.
 *
 * 접근성: role="dialog" + aria-modal + aria-labelledby, Esc 로 닫기, backdrop 클릭 닫기,
 * 열릴 때 카드에 포커스를 옮겨 스크린리더가 모달 안에서 읽기 시작하게 해요.
 * 레이어링은 다른 모달(GateLogModal·ConfirmDialog)과 같은 z 시맨틱 토큰을 써요.
 */
export function HandoffModal({
  bundleId,
  bundleName,
  onClose,
}: {
  bundleId: string;
  bundleName: string;
  onClose: () => void;
}) {
  const { data, error, isLoading } = useSWR<HandoffInfo>(
    `handoff-${bundleId}`,
    () => getBundleHandoff(bundleId),
  );
  const titleId = useId();
  const cardRef = useRef<HTMLDivElement>(null);

  // Esc 로 닫기 — 마우스 없이도 빠져나올 수 있어야 해요.
  useEffect(() => {
    const onEsc = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onEsc);
    return () => document.removeEventListener("keydown", onEsc);
  }, [onClose]);

  // 열릴 때 포커스를 모달 안으로 옮겨요(카드는 tabIndex=-1 로 포커스 대상).
  useEffect(() => {
    cardRef.current?.focus();
  }, []);

  return (
    <div
      className="fixed inset-0 z-[var(--z-modal-backdrop)] flex items-center justify-center bg-black/40 p-4"
      onClick={onClose}
    >
      <div
        ref={cardRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
        className="z-[var(--z-modal)] max-h-[85vh] w-full max-w-lg overflow-auto rounded-xl border border-border bg-card p-5 text-card-foreground shadow-xl focus-visible:outline-none"
        onClick={(e) => e.stopPropagation()}
      >
        <h2 id={titleId} className="mb-1 text-lg font-semibold">
          조직 콘솔 등록 안내
        </h2>
        <p className="mb-4 text-xs text-muted-foreground">{bundleName}</p>

        {isLoading && <p className="text-sm text-muted-foreground">불러오는 중…</p>}
        {error && (
          <p className="text-sm text-red-600">안내를 불러오지 못했어요.</p>
        )}

        {data && (
          <>
            <dl className="mb-4 space-y-1 rounded-lg bg-muted/40 p-3 text-xs">
              <div className="flex gap-2">
                <dt className="w-24 shrink-0 text-muted-foreground">repo</dt>
                <dd className="font-mono">{data.repo_slug || "(연결 안 됨)"}</dd>
              </div>
              <div className="flex gap-2">
                <dt className="w-24 shrink-0 text-muted-foreground">marketplace</dt>
                <dd className="font-mono">{data.marketplace_name}</dd>
              </div>
              <div className="flex gap-2">
                <dt className="w-24 shrink-0 text-muted-foreground">plugin</dt>
                <dd className="font-mono">{data.plugin_slug}</dd>
              </div>
            </dl>

            <h3 className="mb-1.5 text-sm font-medium">순서</h3>
            <ol className="mb-4 space-y-1.5 text-sm">
              {data.steps.map((s, i) => (
                <li key={i} className="flex gap-2">
                  <span className="shrink-0 text-muted-foreground">{i + 1}.</span>
                  <span>{s}</span>
                </li>
              ))}
            </ol>

            <h3 className="mb-1.5 text-sm font-medium">사전 조건</h3>
            <ul className="mb-4 space-y-1 text-xs text-muted-foreground">
              {data.requirements.map((r, i) => (
                <li key={i}>· {r}</li>
              ))}
            </ul>

            <a
              href={data.console_url}
              target="_blank"
              rel="noreferrer"
              className="text-sm text-blue-600 underline"
            >
              조직 콘솔 열기
            </a>
          </>
        )}

        <div className="mt-5 flex justify-end">
          <Button size="sm" variant="outline" onClick={onClose}>
            닫기
          </Button>
        </div>
      </div>
    </div>
  );
}
