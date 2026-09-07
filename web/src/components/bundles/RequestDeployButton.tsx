"use client";

import { useState } from "react";
import { ApiError, requestDeployBundle } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Icon } from "@/components/ui/icon";
import { useToast } from "@/components/ui/toast";

// 신청 상태 배지 — NONE은 아무것도 안 보여줘요(신청 전이라 노이즈예요).
const STATUS_STYLE: Record<string, { label: string; cls: string }> = {
  PENDING: { label: "검토 대기", cls: "bg-amber-50 text-amber-700 ring-amber-200" },
  APPROVED: { label: "승인됨", cls: "bg-emerald-50 text-emerald-700 ring-emerald-200" },
  REJECTED: { label: "거부됨", cls: "bg-red-50 text-red-700 ring-red-200" },
};

export function RequestStatusBadge({ status }: { status: string }) {
  const s = STATUS_STYLE[status];
  if (!s) return null;
  return (
    <span className={`rounded px-2 py-0.5 text-xs ring-1 ring-inset ${s.cls}`}>
      {s.label}
    </span>
  );
}

/** 사용자 배포 신청 버튼. 신청 후에는 상태 배지로 대체돼요. */
export function RequestDeployButton({
  bundleId,
  status,
  onDone,
}: {
  bundleId: string;
  status: string;
  onDone?: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const { success, error: toastError } = useToast();

  // 이미 신청했거나 승인된 그룹은 버튼을 감춰요. 거부된 건 다시 신청할 수 있게 둬요.
  if (status === "PENDING" || status === "APPROVED") {
    return <RequestStatusBadge status={status} />;
  }

  async function run() {
    setBusy(true);
    setError(null);
    try {
      await requestDeployBundle(bundleId);
      success("배포 신청을 접수했어요.", "관리자 검토 후 marketplace 에 반영돼요.");
      onDone?.();
    } catch (err) {
      // 422 = 거버넌스 거부(미승인 자산·예약어·빈 그룹·64자 초과). 서버 문구를 그대로
      // 보여줘요 — 사유가 구체적이라 우리가 다시 쓰면 정보가 줄어요.
      const message =
        err instanceof ApiError && err.status === 422
          ? err.message
          : err instanceof ApiError && err.status === 403
            ? "본인이 만든 플러그인만 신청할 수 있어요"
            : err instanceof Error
              ? err.message
              : "신청에 실패했어요";
      setError(message);
      toastError("배포 신청에 실패했어요.", message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex flex-col items-end gap-1.5">
      <div className="flex items-center gap-2">
        {status === "REJECTED" && <RequestStatusBadge status={status} />}
        <Button size="sm" onClick={run} disabled={busy}>
          <Icon name="external" size={14} />
          {busy ? "신청 중…" : status === "REJECTED" ? "다시 신청" : "배포 신청"}
        </Button>
      </div>
      {error && (
        <p className="max-w-md text-right text-xs text-red-600">{error}</p>
      )}
    </div>
  );
}
