import type { ChangeEventHandler } from "react";

import { ExcludedSecretsNotice } from "@/components/ui/ExcludedSecretsNotice";

import { textareaClass } from "./styles";
import {
  unsafePathReason,
  type FileRow,
  type WizardChoice,
} from "./types";

type SourceModeStepProps = {
  choice: WizardChoice;
  files: FileRow[];
  skippedFiles: string[];
  excludedSecrets: string[];
  onPickFolder: ChangeEventHandler<HTMLInputElement>;
  onPickFile: ChangeEventHandler<HTMLInputElement>;
  onUpdateFile: (
    id: string,
    patch: Partial<Pick<FileRow, "path" | "content">>,
  ) => void;
  onAddFile: () => void;
  onRemoveFile: (id: string) => void;
};

export function SourceModeStep({
  choice,
  files,
  skippedFiles,
  excludedSecrets,
  onPickFolder,
  onPickFile,
  onUpdateFile,
  onAddFile,
  onRemoveFile,
}: SourceModeStepProps) {
  if (choice.assetType === "skill") {
    return (
      <>
        <p className="text-slate-600 mb-2">
          Skill 폴더를 통째로 선택해 주세요.
        </p>
        <p className="text-sm text-slate-500 mb-4">
          <span className="font-mono text-slate-700">SKILL.md</span> 가 폴더
          root 에 있어야 해요. 메타데이터는 SKILL.md 에서 자동으로 채워져요.
        </p>
        <label className="block text-sm px-4 py-6 border-2 border-dashed border-slate-300 rounded-lg hover:bg-slate-50 cursor-pointer text-center text-slate-600">
          📁 Skill 폴더 선택
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
                  key={file.id}
                  className="flex items-center gap-2 px-3 py-2 text-sm"
                >
                  <span className="font-mono text-slate-700 flex-1 truncate">
                    {file.path}
                  </span>
                  {file.path === "SKILL.md" && (
                    <span className="text-xs text-emerald-700 bg-emerald-50 px-2 py-0.5 rounded">
                      필수
                    </span>
                  )}
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

  return (
    <>
      <p className="text-slate-600 mb-2">
        업로드할 소스 파일을 작성해 주세요.
      </p>
      <p className="text-sm text-slate-500 mb-4">
        <span className="font-mono text-slate-700">
          {choice.requiredFile}
        </span>{" "}
        파일은 필수예요. 비워두면 다음으로 넘어갈 수 없어요.
      </p>

      <div className="space-y-4">
        {files.map((file) => {
          const isRequired = file.path.trim() === choice.requiredFile;
          const pathReason = unsafePathReason(file.path);
          return (
            <div
              key={file.id}
              className="border border-slate-200 rounded-lg p-3 bg-white"
            >
              <div className="flex items-center gap-2 mb-2">
                <input
                  type="text"
                  value={file.path}
                  onChange={(event) =>
                    onUpdateFile(file.id, { path: event.target.value })
                  }
                  placeholder="파일 경로 (예: SKILL.md, src/index.ts)"
                  className="flex-1 px-3 py-1.5 border border-slate-300 rounded font-mono text-sm bg-white focus:outline-none focus:ring-2 focus:ring-blue-500"
                />
                {isRequired && (
                  <span className="text-xs text-emerald-700 bg-emerald-50 px-2 py-0.5 rounded whitespace-nowrap">
                    필수
                  </span>
                )}
                <button
                  type="button"
                  onClick={() => onRemoveFile(file.id)}
                  className="text-sm text-slate-400 hover:text-red-600 px-2"
                  aria-label="파일 삭제"
                >
                  삭제
                </button>
              </div>
              {pathReason && file.path.length > 0 && (
                <p className="text-xs text-red-600 mb-2">{pathReason}</p>
              )}
              <textarea
                value={file.content}
                onChange={(event) =>
                  onUpdateFile(file.id, { content: event.target.value })
                }
                placeholder={
                  isRequired
                    ? `${choice.requiredFile} 내용을 작성하세요...`
                    : "파일 내용 (텍스트)"
                }
                rows={isRequired ? 8 : 5}
                className={textareaClass}
              />
            </div>
          );
        })}
      </div>

      <div className="flex flex-wrap items-center gap-3 mt-3">
        <button
          type="button"
          onClick={onAddFile}
          className="text-sm px-3 py-1.5 border border-slate-300 rounded-lg hover:bg-slate-50"
        >
          + 파일 추가
        </button>
        <label className="text-sm px-3 py-1.5 border border-slate-300 rounded-lg hover:bg-slate-50 cursor-pointer">
          파일 불러오기 (텍스트)
          <input
            type="file"
            multiple
            onChange={onPickFile}
            className="hidden"
          />
        </label>
      </div>
    </>
  );
}
