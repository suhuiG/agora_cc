"use client";

import { useState } from "react";
import { ApiError, runGovScan } from "@/lib/api";
import { Button } from "@/components/ui/button";

// 수동 스캔 실행 + 진행상태 폴링 (M1 F8·M2 F9).
// 로컬 StaticScanner는 동기 완료(done)라 즉시 끝나지만, 계약상 status 폴링 형태를 유지해요.
interface ScanRunButtonProps {
  recordId: string;
  scanned: boolean;
  trigger?: "manual-queue" | "manual-detail";
  size?: "sm" | "md";
  /** 서버 기준 스캔 진행중 여부 — 다른 화면(큐)에서 시작한 스캔도 여기서 disabled 처리해요. */
  serverScanning?: boolean;
  /** 서버 적용성 판정에 따라 실행할 수 없을 때 표시할 사유. */
  disabledReason?: string;
  /** 스캔 시작 시 — 부모가 진행상태·게이트를 'running'으로 초기화하도록 알려요. */
  onStart?: () => void;
  /** 스캔 완료(성공/실패) 후 — 부모가 게이트를 실제 결과로 refresh 하도록 알려요. */
  onDone?: () => void;
}

export function ScanRunButton({
  recordId,
  scanned,
  trigger = "manual-queue",
  size = "sm",
  serverScanning = false,
  disabledReason = "",
  onStart,
  onDone,
}: ScanRunButtonProps) {
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // 로컬 클릭 진행중(running) 또는 서버가 이미 running(다른 화면에서 시작) → 스캔중으로 취급.
  const scanning = running || serverScanning;

  async function handleScan(e: React.MouseEvent) {
    e.preventDefault();
    e.stopPropagation();
    setRunning(true);
    setError(null);
    onStart?.();   // 이전 결과(FAIL/findings)를 즉시 비우고 진행중 표시로 초기화
    try {
      // 비동기 계약: runGovScan은 즉시 running으로 반환돼요. 부모의 폴링(SWR refreshInterval)이
      // running→done 전이를 감지해 결과를 갱신하니, 여기선 시작만 알리고 로컬 running을 풀어요.
      await runGovScan(recordId, trigger);
      onDone?.();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "스캔 실패");
      onDone?.();
    } finally {
      setRunning(false);
    }
  }

  return (
    <span className="inline-flex flex-col items-end gap-1">
      <Button
        variant="outline"
        size={size}
        disabled={scanning || Boolean(disabledReason)}
        title={disabledReason || undefined}
        onClick={handleScan}
      >
        {scanning
          ? "스캔 중…"
          : disabledReason
            ? "스캔 대상 아님"
            : scanned
              ? "재스캔"
              : "스캔 실행"}
      </Button>
      {disabledReason && (
        <span className="max-w-48 text-right text-[11px] leading-4 text-muted-foreground">
          {disabledReason}
        </span>
      )}
      {error && <span className="text-[11px] text-red-600">{error}</span>}
    </span>
  );
}
