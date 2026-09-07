"use client";

import { useState } from "react";
import { ApiError, getGovThreatReport } from "@/lib/api";
import { Button } from "@/components/ui/button";

// 위협리포트.md 다운로드 (M1 F5 · M2 F5). Bedrock Sonnet 4.6이 에셋별 맞춤
// 수정가이드를 생성하므로 클릭 후 몇 초 걸릴 수 있어요(생성 중 표시).
interface ThreatReportButtonProps {
  recordId: string;
  assetName: string;
  size?: "sm" | "md";
  variant?: "outline" | "ghost";
}

export function ThreatReportButton({
  recordId,
  assetName,
  size = "sm",
  variant = "outline",
}: ThreatReportButtonProps) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function download(e: React.MouseEvent) {
    e.preventDefault();
    e.stopPropagation();
    setBusy(true);
    setError(null);
    try {
      const md = await getGovThreatReport(recordId);
      // 브라우저 다운로드: blob → 임시 링크 클릭.
      const blob = new Blob([md], { type: "text/markdown;charset=utf-8" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      const ymd = new Date().toISOString().slice(2, 10).replace(/-/g, "");
      a.href = url;
      a.download = `${assetName}_위협리포트_${ymd}.md`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "다운로드 실패");
    } finally {
      setBusy(false);
    }
  }

  return (
    <span className="inline-flex items-center gap-1.5">
      <Button variant={variant} size={size} disabled={busy} onClick={download}>
        {busy ? "생성 중…" : "위협리포트.md ⭳"}
      </Button>
      {error && <span className="text-[11px] text-red-600">{error}</span>}
    </span>
  );
}
