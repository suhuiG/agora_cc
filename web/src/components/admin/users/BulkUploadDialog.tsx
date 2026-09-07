"use client";

import { useState } from "react";

import { Button } from "@/components/ui/button";
import { Modal } from "@/components/ui/modal";
import {
  commitBulkCognitoUsers,
  validateBulkCognitoUsers,
  type BulkUserResult,
} from "@/lib/api";

export function BulkUploadDialog({
  open,
  onClose,
  onCommitted,
}: {
  open: boolean;
  onClose: () => void;
  onCommitted: () => void;
}) {
  const [content, setContent] = useState("");
  const [fileName, setFileName] = useState("");
  const [result, setResult] = useState<BulkUserResult | null>(null);
  const [committed, setCommitted] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function chooseFile(file: File | undefined) {
    if (!file) return;
    setFileName(file.name);
    setContent(await file.text());
    setResult(null);
    setCommitted(false);
    setError("");
  }

  async function validate() {
    setBusy(true);
    setError("");
    try {
      setResult(await validateBulkCognitoUsers(content));
      setCommitted(false);
    } catch (caught) {
      setError(errorMessage(caught, "CSV 검증에 실패했어요."));
    } finally {
      setBusy(false);
    }
  }

  async function commit() {
    setBusy(true);
    setError("");
    try {
      const committedResult = await commitBulkCognitoUsers(content);
      setResult(committedResult);
      setCommitted(true);
      onCommitted();
    } catch (caught) {
      setError(errorMessage(caught, "일괄 등록에 실패했어요."));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal
      open={open}
      title="CSV 일괄 등록"
      description="email,name,team,groups 헤더를 사용해요. groups는 user|admin 형식이에요."
      size="lg"
      busy={busy}
      onClose={onClose}
      footer={
        <>
          <Button type="button" variant="outline" onClick={onClose} disabled={busy}>
            {committed ? "닫기" : "취소"}
          </Button>
          {!committed && (
            <Button
              type="button"
              onClick={result ? commit : validate}
              disabled={busy || !content || Boolean(result && result.passed === 0)}
            >
              {busy
                ? "처리 중..."
                : result
                  ? `통과 ${result.passed}행 등록`
                  : "검증"}
            </Button>
          )}
        </>
      }
    >
      <div className="space-y-4">
        <label className="block">
          <span className="mb-1.5 block text-sm font-medium">CSV 파일</span>
          <input
            type="file"
            accept=".csv,text/csv"
            onChange={(event) => chooseFile(event.target.files?.[0])}
            className="block w-full rounded-lg border border-input bg-card px-3 py-2 text-sm file:mr-3 file:rounded-md file:border-0 file:bg-muted file:px-3 file:py-1.5 file:text-xs file:font-medium"
          />
        </label>
        {fileName && (
          <p className="text-xs text-muted-foreground">{fileName}</p>
        )}
        {result && (
          <div>
            <div className="mb-2 flex flex-wrap gap-3 text-sm">
              <span>전체 {result.total}행</span>
              <span className="text-emerald-700">통과 {result.passed}</span>
              <span className="text-red-700">실패 {result.failed}</span>
            </div>
            <div className="max-h-64 overflow-auto rounded-lg border border-border">
              <table className="w-full min-w-[440px] text-left text-xs">
                <thead className="sticky top-0 bg-muted">
                  <tr>
                    <th className="px-3 py-2 font-medium">행</th>
                    <th className="px-3 py-2 font-medium">이메일</th>
                    <th className="px-3 py-2 font-medium">결과</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-border">
                  {result.rows.map((row) => (
                    <tr key={`${row.line}-${row.email}`}>
                      <td className="px-3 py-2">{row.line}</td>
                      <td className="break-all px-3 py-2">{row.email || "-"}</td>
                      <td
                        className={`px-3 py-2 ${
                          row.errors.length ? "text-red-700" : "text-emerald-700"
                        }`}
                      >
                        {row.errors.join(" · ") || (committed ? "등록됨" : "통과")}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}
        {error && <p className="text-sm text-red-700">{error}</p>}
      </div>
    </Modal>
  );
}

function errorMessage(error: unknown, fallback: string): string {
  return error instanceof Error && error.message ? error.message : fallback;
}
