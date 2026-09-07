"use client";

/**
 * 배포된 MCP 재배포 모달 — 소스 폴더를 다시 올려 같은 Gateway endpoint를 유지한 채 코드를 갱신해요.
 *
 * 왜 별도 경로인가: 신규 배포는 같은 이름을 거부해요. 배포된 MCP를 고치려면 예전엔 완전삭제→재생성뿐이었고,
 * 그때마다 리뷰·조회수·번들 멤버십이 날아갔어요. 여기서 재배포하면 record_id·endpoint(불변 Gateway URL)가
 * 그대로 유지돼요(ADR-0021). RedeployAgentModal의 MCP 짝이고, upload/start 엔드포인트만 달라요.
 */
import { useState } from "react";

import { EscalationContactPicker }
  from "@/app/(portal)/catalog/publish/_components/EscalationContactPicker";
import {
  uploadMcpFolder,
  startMcpDeploy,
  ApiError,
  SESSION_PRINCIPAL,
  type SourceFile,
} from "@/lib/api";
import { collectFolderFiles } from "@/lib/folderUpload";
import { Button } from "@/components/ui/button";
import { Modal } from "@/components/ui/modal";
import { ExcludedSecretsNotice } from "@/components/ui/ExcludedSecretsNotice";
import { useToast } from "@/components/ui/toast";

type Props = {
  recordId: string;
  assetName: string;
  currentVersion: string;
  onClose: () => void;
  onStarted: () => void;
};

// MCP codezip 진입점 후보 — 하나라도 있어야 백엔드가 배포를 시작해요(spec_check._ENTRY_HINTS와 동일).
// 없으면 백엔드가 반려하니 업로드 전에 알려줘요.
const MCP_ENTRY_HINTS = ["pyproject.toml", "server.py", "__main__.py", "app.py", "main.py"];

export function RedeployMcpModal({
  recordId, assetName, currentVersion, onClose, onStarted,
}: Props) {
  const [files, setFiles] = useState<SourceFile[]>([]);
  const [skipped, setSkipped] = useState<string[]>([]);
  // 크리덴셜이라 빼둔 파일 — "비텍스트라 건너뜀" 과 섞지 않아요.
  const [excludedSecrets, setExcludedSecrets] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  // 백엔드는 `escalation_contact` 를 **필수**로 요구해요(승인 계약: 유효한 email 2개).
  // 이 모달에 입력이 없어서 `POST /api/mcp/deploy/init` 이 항상 422 였어요 — 재배포가
  // 아예 불가능했어요(2026-08-30 실측).
  const [escalationContact, setEscalationContact] = useState("");
  const { success, error: toastError } = useToast();

  const hasEntry = files.some(
    (f) => MCP_ENTRY_HINTS.some((h) => f.path === h || f.path.endsWith("/" + h)),
  );
  const ready = files.length > 0 && hasEntry && !!escalationContact.trim();

  async function onPick(e: React.ChangeEvent<HTMLInputElement>) {
    const list = e.target.files;
    if (!list || list.length === 0) return;
    const { files: collected, skipped: sk, excludedSecrets: secrets } =
      await collectFolderFiles(list);
    collected.sort((a, b) => a.path.localeCompare(b.path));
    setFiles(collected);
    setSkipped(sk);
    setExcludedSecrets(secrets);
    setError("");
    e.target.value = "";
  }

  async function onSubmit() {
    setBusy(true);
    setError("");
    try {
      // init이 소스 버전을 자동 증가시켜요(1.0.0 → 1.0.1). 이름은 기존 자산과 같아야
      // 같은 asset_id로 새 버전이 쌓여요.
      const { asset_id, version } = await uploadMcpFolder(
        { name: assetName, files, escalation_contact: escalationContact },
        SESSION_PRINCIPAL,
      );
      // selected_tools는 비워서 보내요(빈 목록 = 전체 노출). 재배포의 본 목적은 같은
      // endpoint에 새 버전 코드를 올리는 거예요 — tool 선택은 신규 배포 흐름에서 다뤄요.
      await startMcpDeploy(asset_id, version, [], SESSION_PRINCIPAL, recordId);
      // 성공 인라인 안내는 상세 페이지 최하단(버전 이력 뒤)에 붙어서, 상단 버튼으로
      // 재배포한 사용자의 뷰포트에는 안 보여요. 토스트로 현재 화면에서 알려요.
      success("재배포를 시작했어요.", `${assetName} · v${version} 으로 올라가요.`);
      onStarted();
      onClose();
    } catch (e) {
      const message = e instanceof ApiError || e instanceof Error
        ? e.message : "재배포를 시작하지 못했어요.";
      setError(message);
      toastError("재배포를 시작하지 못했어요.", message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal
      open
      size="lg"
      busy={busy}
      title="MCP 재배포"
      onClose={onClose}
      description={
        <>
          <span className="font-mono">{assetName}</span> 의 코드를 갱신해요. 이름과
          endpoint(Gateway URL)는 그대로 유지되고, 리뷰·조회수도 남아요.
        </>
      }
      footer={
        <>
          <Button variant="outline" size="md" onClick={onClose} disabled={busy}>
            취소
          </Button>
          <Button size="md" onClick={onSubmit} disabled={!ready || busy}>
            {busy ? "시작 중…" : "재배포"}
          </Button>
        </>
      }
    >
        <div className="mb-4">
          <EscalationContactPicker
            value={escalationContact}
            onChange={setEscalationContact}
            initialMode="button"
          />
        </div>

        <div className="mb-4 rounded-lg bg-slate-50 p-3 text-sm text-slate-600">
          <div>현재 버전 <span className="font-mono">v{currentVersion}</span> → 새 버전으로 올라가요</div>
          <div className="mt-1 text-xs">
            업로드한 소스로 다시 빌드하고 거버넌스 스캔(시크릿·SAST·CVE)을 다시 통과해야 해요.
            스캔에서 반려되면 카탈로그에서 반려 상태로 표시돼요.
          </div>
        </div>

        <label className="block cursor-pointer rounded-lg border-2 border-dashed border-slate-300 px-4 py-6 text-center text-sm text-slate-600 hover:bg-slate-50">
          📁 MCP 소스 폴더 선택
          <input
            type="file"
            /* @ts-expect-error webkitdirectory는 React 19 타입에 없지만 표준 web API예요 */
            webkitdirectory=""
            directory=""
            multiple
            onChange={onPick}
            className="hidden"
          />
        </label>

        {files.length > 0 && (
          <div className="mt-3 text-sm">
            <div className="mb-1 text-slate-700">{files.length}개 파일</div>
            <div className="max-h-32 overflow-auto rounded-lg border border-slate-200 divide-y divide-slate-100">
              {files.map((f) => (
                <div key={f.path} className="px-3 py-1.5 font-mono text-xs text-slate-600">
                  {f.path}
                </div>
              ))}
            </div>
          </div>
        )}

        {files.length > 0 && !ready && (
          <div className="mt-3 rounded-lg bg-amber-50 p-3 text-xs text-amber-800">
            <div>
              MCP 진입점이 필요해요 —{" "}
              <span className="font-mono">{MCP_ENTRY_HINTS.join(", ")}</span> 중 하나가
              루트(또는 하위 폴더)에 있어야 해요.
            </div>
          </div>
        )}

        <ExcludedSecretsNotice paths={excludedSecrets} />

        {skipped.length > 0 && (
          <div className="mt-3 rounded-lg bg-slate-50 p-3 text-xs text-slate-600">
            비텍스트 파일은 제외됐어요: <span className="font-mono">{skipped.join(", ")}</span>
          </div>
        )}

        {error && (
          <div className="mt-3 rounded-lg bg-red-50 p-3 text-sm text-red-700">{error}</div>
        )}

    </Modal>
  );
}
