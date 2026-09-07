"use client";

import type { McpToolPreview } from "@/lib/api";
import { SensitivityBadge } from "@/components/SensitivityBadge";

type McpDeployToolsStepProps = {
  loading: boolean;
  tools: McpToolPreview[];
  error: string;
  submitting: boolean;
  selected: Set<string>;
  onToggle: (name: string) => void;
  onPrevious: () => void;
  onDeploy: (selectedTools: string[]) => void;
};

export function McpDeployToolsStep({
  loading,
  tools,
  error,
  submitting,
  selected,
  onToggle,
  onPrevious,
  onDeploy,
}: McpDeployToolsStepProps) {
  const selectedTools = tools
    .filter((tool) => selected.has(tool.name))
    .map((tool) => tool.name);

  return (
    <div>
      <p className="text-slate-600 mb-1">Gateway에 노출할 tool을 선택해요.</p>
      <p className="text-sm text-slate-500 mb-4">
        {/* 2026-09-06: 「Agent × Tool」을 사이드바에서 숨겼어요. 등록자가 그 이름을 찾아
            헤매지 않게 «지금 사이드바에 있는» 화면으로 안내를 바꿨어요 — 같은 ④층을 닫는
            화면이에요. 숨긴 메뉴 이름을 안내문에 남겨 두면 화면이 없는 길을 가리켜요. */}
        배포가 끝나면 <b>관리자 콘솔 › 도구 인가 승인</b>에서 이 tool 들의 권한을
        승인하세요. 승인 전에는 호출이 거부돼요.
      </p>

      {loading ? (
        <p className="text-sm text-slate-500">소스에서 tool을 찾는 중…</p>
      ) : error ? (
        <div className="text-sm text-amber-700 bg-amber-50 p-3 rounded-lg">
          {error} (tool 미리보기에 실패해도 배포는 진행할 수 있어요.)
        </div>
      ) : tools.length === 0 ? (
        <div className="text-sm text-amber-700 bg-amber-50 p-3 rounded-lg">
          소스에서 tool을 찾지 못했어요. <span className="font-mono">@mcp.tool</span>{" "}
          데코레이터가 있는지 확인하세요. 배포 자체는 그대로 진행돼요.
        </div>
      ) : (
        <div className="border border-slate-200 rounded-lg divide-y divide-slate-100">
          {tools.map((t) => (
            <label
              key={t.name}
              className="flex cursor-pointer items-start gap-3 px-3 py-2 text-sm hover:bg-slate-50"
            >
              <input
                type="checkbox"
                checked={selected.has(t.name)}
                onChange={() => onToggle(t.name)}
                className="mt-0.5 h-4 w-4 shrink-0 accent-blue-600"
              />
              <span className="min-w-0">
                <span className="flex flex-wrap items-center gap-2">
                  <span className="break-all font-mono text-slate-800">
                    {t.name}
                  </span>
                  <SensitivityBadge
                    sensitivity={t.sensitivity}
                    rationale={t.sensitivityRationale}
                  />
                </span>
                {t.description && (
                  <span className="mt-0.5 block text-slate-500">
                    {t.description}
                  </span>
                )}
                {t.sensitivityRationale && (
                  <span className="mt-0.5 block text-xs text-slate-400">
                    {t.sensitivityRationale}
                  </span>
                )}
              </span>
            </label>
          ))}
        </div>
      )}

      <div className="mt-6 flex flex-wrap items-center gap-3">
        <button
          type="button"
          onClick={onPrevious}
          className="px-4 py-2 border border-slate-300 rounded-lg hover:bg-slate-50"
        >
          이전
        </button>
        <button
          type="button"
          onClick={() => onDeploy(selectedTools)}
          disabled={submitting || loading || (tools.length > 0 && selectedTools.length === 0)}
          className="px-4 py-2 bg-slate-900 text-white rounded-lg hover:bg-slate-700 disabled:opacity-40"
        >
          {submitting ? "배포 요청 중…" : "배포 요청"}
        </button>
        {tools.length > 0 && (
          <span className="text-xs text-slate-500">
            {selectedTools.length}/{tools.length}개 선택
          </span>
        )}
      </div>
    </div>
  );
}
