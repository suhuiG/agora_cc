"use client";

import { useState } from "react";
import { ApiError, publishBundle, type PublishBundleResult } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Icon } from "@/components/ui/icon";
import { useToast } from "@/components/ui/toast";

// bundle 배포 버튼 (ScanRunButton 패턴). busy/error/result 로컬 상태.
// 성공 시 commit_sha(앞 7자)·version·skipped를 인라인으로 표시해요.
// 시각적으로 다른 카드 액션(편집·삭제)과 같은 Button vocabulary 를 써요.
export function PublishBundleButton({
  bundleId,
  onDone,
}: {
  bundleId: string;
  onDone?: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<PublishBundleResult | null>(null);
  const { success, error: toastError } = useToast();

  async function run(e: React.MouseEvent) {
    e.preventDefault();
    e.stopPropagation();
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      const res = await publishBundle(bundleId);
      setResult(res);
      success(
        "marketplace 에 배포했어요.",
        `commit ${res.commit_sha.slice(0, 7)} · v${res.version}` +
          (res.skipped.length > 0 ? ` · 건너뜀 ${res.skipped.length}건` : ""),
      );
      onDone?.();
    } catch (err) {
      const msg =
        err instanceof ApiError && err.status === 409
          ? "먼저 repo를 연결하세요 (연결 화면)"
          : err instanceof Error
            ? err.message
            : "배포 실패";
      setError(msg);
      toastError("배포하지 못했어요.", msg);
    } finally {
      setBusy(false);
    }
  }

  return (
    <span className="inline-flex items-center gap-2">
      <Button size="sm" onClick={run} disabled={busy}>
        <Icon name="external" size={14} />
        {busy ? "배포 중…" : "배포"}
      </Button>
      {result && (
        <span className="text-[11px] text-emerald-700">
          {result.commit_sha.slice(0, 7)} · v{result.version}
          {result.skipped.length > 0 && ` · skip ${result.skipped.length}`}
        </span>
      )}
      {error && <span className="text-[11px] text-red-600">{error}</span>}
    </span>
  );
}
