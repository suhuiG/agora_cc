import type { ChangeEventHandler } from "react";

import type { SourceFile } from "@/lib/api";
import { ExcludedSecretsNotice } from "@/components/ui/ExcludedSecretsNotice";

import { unsafePathReason } from "./types";

type McpDeployModeStepProps = {
  files: SourceFile[];
  skippedFiles: string[];
  excludedSecrets: string[];
  onPickFolder: ChangeEventHandler<HTMLInputElement>;
};

export function McpDeployModeStep({
  files,
  skippedFiles,
  excludedSecrets,
  onPickFolder,
}: McpDeployModeStepProps) {
  return (
    <>
      <p className="text-slate-600 mb-2">
        배포할 MCP 소스 폴더를 통째로 선택해 주세요.
      </p>
      <p className="text-sm text-slate-500 mb-4">
        다음 단계에서 이름·메타데이터를 확인하고 배포를 시작하면, 승인 후
        Runtime에 배포하고 Gateway endpoint를 발급해요.
      </p>
      <label className="block text-sm px-4 py-6 border-2 border-dashed border-slate-300 rounded-lg hover:bg-slate-50 cursor-pointer text-center text-slate-600">
        📁 MCP 소스 폴더 선택
        <input
          type="file"
          /* @ts-expect-error webkitdirectory는 React 19 타입에 없지만 표준 web API예요 */
          webkitdirectory=""
          directory=""
          multiple
          onChange={onPickFolder}
          className="hidden"
        />
      </label>

      <ExcludedSecretsNotice paths={excludedSecrets} />

      {skippedFiles.length > 0 && (
        <div className="mt-3 text-xs text-amber-700 bg-amber-50 p-3 rounded-lg">
          다음 비텍스트 파일은 제외됐어요(현재 텍스트 업로드만 지원):
          <span className="font-mono"> {skippedFiles.join(", ")}</span>
        </div>
      )}

      {files.length > 0 && (
        <div className="mt-4 border border-slate-200 rounded-lg divide-y divide-slate-100">
          {files.map((file) => {
            const reason = unsafePathReason(file.path);
            return (
              <div
                key={file.path}
                className="flex items-center gap-2 px-3 py-2 text-sm"
              >
                <span className="font-mono text-slate-700 flex-1 truncate">
                  {file.path}
                </span>
                {reason && (
                  <span className="text-xs text-red-600">{reason}</span>
                )}
              </div>
            );
          })}
        </div>
      )}
    </>
  );
}
