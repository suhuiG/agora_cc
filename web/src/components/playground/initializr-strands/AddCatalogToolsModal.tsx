"use client";

import { useMemo, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Modal } from "@/components/ui/modal";
import { SensitivityBadge } from "@/components/SensitivityBadge";
import { getAsset } from "@/lib/api";
import type { ToolOption } from "@/lib/initializr";
import { cn } from "@/lib/ui";
import {
  JUSTIFICATION_MIN_LENGTH,
  MCP_EMPTY_SELECTION_MESSAGE,
  defaultSelectedOperationIds,
  operationKey,
  parseMcpOperations,
  type CatalogOperation,
  type OperationJustifications,
  type SelectedCatalogTool,
} from "./model";

function cloneSelection(tools: SelectedCatalogTool[]): Map<string, SelectedCatalogTool> {
  return new Map(tools.map((tool) => [
    tool.id,
    { ...tool, selectedOperations: new Set(tool.selectedOperations) },
  ]));
}

export function AddCatalogToolsModal({
  tools,
  selected,
  justifications,
  onConfirm,
  onClose,
}: {
  tools: ToolOption[];
  selected: SelectedCatalogTool[];
  justifications: OperationJustifications;
  onConfirm: (
    tools: SelectedCatalogTool[],
    justifications: OperationJustifications,
  ) => void;
  onClose: () => void;
}) {
  const [query, setQuery] = useState("");
  const [draft, setDraft] = useState(() => cloneSelection(selected));
  const [draftJustifications, setDraftJustifications] =
    useState<OperationJustifications>(justifications);
  const [loadingId, setLoadingId] = useState("");
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [expanded, setExpanded] = useState<Set<string>>(
    () => new Set(selected.filter((tool) => tool.kind === "mcp").map((tool) => tool.id)),
  );
  const [reasonTarget, setReasonTarget] = useState<{
    tool: ToolOption;
    operation: CatalogOperation;
  } | null>(null);
  const [reason, setReason] = useState("");

  const results = useMemo(() => {
    const normalized = query.trim().toLowerCase();
    return tools.filter((tool) => (
      !normalized
      || tool.name.toLowerCase().includes(normalized)
      || tool.description.toLowerCase().includes(normalized)
    ));
  }, [query, tools]);

  function toggleSkill(tool: ToolOption) {
    setDraft((current) => {
      const next = new Map(current);
      if (next.has(tool.id)) next.delete(tool.id);
      else {
        next.set(tool.id, {
          ...tool,
          operations: [],
          selectedOperations: new Set(),
        });
      }
      return next;
    });
  }

  async function expandMcp(tool: ToolOption) {
    if (expanded.has(tool.id)) {
      setExpanded((current) => {
        const next = new Set(current);
        next.delete(tool.id);
        return next;
      });
      return;
    }

    let operations = draft.get(tool.id)?.operations;
    if (!operations) {
      setLoadingId(tool.id);
      try {
        operations = parseMcpOperations(await getAsset(tool.id));
        if (operations.length === 0) {
          setErrors((current) => ({
            ...current,
            [tool.id]: "카탈로그에서 operation 목록을 확인할 수 없어요.",
          }));
          return;
        }
        setDraft((current) => {
          const next = new Map(current);
          next.set(tool.id, {
            ...tool,
            operations: operations!,
            selectedOperations: new Set(defaultSelectedOperationIds(operations!)),
          });
          return next;
        });
      } catch (error) {
        setErrors((current) => ({
          ...current,
          [tool.id]: error instanceof Error
            ? error.message
            : "operation 목록을 불러오지 못했어요.",
        }));
        return;
      } finally {
        setLoadingId("");
      }
    }
    setExpanded((current) => new Set(current).add(tool.id));
  }

  function commitOperation(
    tool: ToolOption,
    operation: CatalogOperation,
    checked: boolean,
  ) {
    setDraft((current) => {
      const existing = current.get(tool.id);
      if (!existing) return current;
      const next = new Map(current);
      const selectedOperations = new Set(existing.selectedOperations);
      if (checked) selectedOperations.add(operation.id);
      else selectedOperations.delete(operation.id);
      next.set(tool.id, { ...existing, selectedOperations });
      return next;
    });
  }

  function toggleOperation(
    tool: ToolOption,
    operation: CatalogOperation,
    checked: boolean,
  ) {
    if (checked && operation.sensitivity !== "READ") {
      setReasonTarget({ tool, operation });
      setReason(draftJustifications[operationKey(tool.id, operation.id)] ?? "");
      return;
    }
    commitOperation(tool, operation, checked);
    if (!checked) {
      const key = operationKey(tool.id, operation.id);
      setDraftJustifications((current) => {
        const next = { ...current };
        delete next[key];
        return next;
      });
    }
  }

  function saveReason() {
    if (!reasonTarget || reason.trim().length < JUSTIFICATION_MIN_LENGTH) return;
    const key = operationKey(reasonTarget.tool.id, reasonTarget.operation.id);
    setDraftJustifications((current) => ({ ...current, [key]: reason.trim() }));
    commitOperation(reasonTarget.tool, reasonTarget.operation, true);
    setReasonTarget(null);
  }

  function removeMcp(toolId: string) {
    setDraft((current) => {
      const next = new Map(current);
      next.delete(toolId);
      return next;
    });
  }

  return (
    <>
      <Modal
        open
        size="lg"
        title="카탈로그 도구 추가"
        description="MCP는 사용할 operation만 고르세요. Skill은 자산 단위로 추가돼요."
        onClose={onClose}
        footer={(
          <>
            <Button variant="ghost" onClick={onClose}>취소</Button>
            <Button
              onClick={() => {
                const selectedTools = [...draft.values()].filter((tool) => (
                  tool.kind !== "mcp" || tool.selectedOperations.size > 0
                ));
                onConfirm(selectedTools, draftJustifications);
                onClose();
              }}
            >
              추가
            </Button>
          </>
        )}
      >
        <Input
          autoFocus
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder="이름·설명으로 검색"
        />
        <div className="mt-3 max-h-[52vh] space-y-2 overflow-y-auto pr-1">
          {results.length === 0 && (
            <p className="py-8 text-center text-sm text-muted-foreground">
              검색 결과가 없어요.
            </p>
          )}
          {results.map((tool) => {
            const selectedTool = draft.get(tool.id);
            if (tool.kind === "skill") {
              return (
                <label
                  key={tool.id}
                  className={cn(
                    "flex cursor-pointer items-start gap-3 rounded-lg border p-3",
                    selectedTool ? "border-blue-300 bg-blue-50/50" : "border-border",
                  )}
                >
                  <input
                    type="checkbox"
                    checked={Boolean(selectedTool)}
                    onChange={() => toggleSkill(tool)}
                    className="mt-0.5 h-4 w-4 accent-blue-600"
                  />
                  <ToolSummary tool={tool} />
                </label>
              );
            }

            const isExpanded = expanded.has(tool.id);
            return (
              <div key={tool.id} className="rounded-lg border border-border">
                <button
                  type="button"
                  onClick={() => void expandMcp(tool)}
                  className="flex w-full items-start gap-3 p-3 text-left hover:bg-accent/40"
                >
                  <span className="mt-0.5 text-xs text-muted-foreground">
                    {loadingId === tool.id ? "…" : isExpanded ? "−" : "+"}
                  </span>
                  <ToolSummary tool={tool} />
                  {selectedTool && selectedTool.selectedOperations.size > 0 && (
                    <span className="ml-auto shrink-0 text-xs font-medium text-blue-700">
                      {selectedTool.selectedOperations.size}개
                    </span>
                  )}
                </button>
                {errors[tool.id] && (
                  <p className="border-t border-border px-3 py-2 text-xs text-red-700">
                    {errors[tool.id]}
                  </p>
                )}
                {isExpanded && selectedTool && (
                  <div className="border-t border-border px-3 py-2">
                    <div className="space-y-1">
                      {selectedTool.operations.map((operation) => {
                        const checked = selectedTool.selectedOperations.has(operation.id);
                        return (
                          <label
                            key={operation.id}
                            className="flex cursor-pointer items-center gap-2 rounded px-2 py-2 hover:bg-accent/50"
                          >
                            <input
                              type="checkbox"
                              checked={checked}
                              onChange={(event) => (
                                toggleOperation(tool, operation, event.target.checked)
                              )}
                              className="h-4 w-4 accent-blue-600"
                            />
                            <span className="min-w-0 flex-1 truncate font-mono text-xs">
                              {operation.id}
                            </span>
                            <SensitivityBadge sensitivity={operation.sensitivity} />
                            {operation.sensitivity !== "READ" && checked && (
                              <span className="text-[11px] text-amber-700">사유 입력됨</span>
                            )}
                          </label>
                        );
                      })}
                    </div>
                    {selectedTool.selectedOperations.size === 0 && (
                      <p className="mt-2 text-xs text-amber-700">
                        {MCP_EMPTY_SELECTION_MESSAGE}
                      </p>
                    )}
                    {selectedTool.selectedOperations.size > 0 && (
                      <button
                        type="button"
                        onClick={() => removeMcp(tool.id)}
                        className="mt-2 text-xs text-red-700 hover:underline"
                      >
                        선택 해제
                      </button>
                    )}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </Modal>

      <Modal
        open={Boolean(reasonTarget)}
        size="sm"
        title={`${reasonTarget?.operation.id ?? ""} 사용 사유`}
        description={`READ가 아닌 operation은 ${JUSTIFICATION_MIN_LENGTH}자 이상의 신청 사유가 필요해요.`}
        onClose={() => setReasonTarget(null)}
        footer={(
          <>
            <Button variant="ghost" onClick={() => setReasonTarget(null)}>취소</Button>
            <Button
              disabled={reason.trim().length < JUSTIFICATION_MIN_LENGTH}
              onClick={saveReason}
            >
              저장
            </Button>
          </>
        )}
      >
        <textarea
          value={reason}
          onChange={(event) => setReason(event.target.value)}
          rows={4}
          placeholder="이 operation이 필요한 업무 목적을 적어주세요."
          className="w-full resize-none rounded-lg border border-input bg-card px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        />
        <p className="mt-1 text-right text-xs text-muted-foreground">
          {reason.trim().length}/{JUSTIFICATION_MIN_LENGTH}자 이상
        </p>
      </Modal>
    </>
  );
}

function ToolSummary({ tool }: { tool: ToolOption }) {
  return (
    <span className="min-w-0 flex-1">
      <span className="flex items-center gap-2">
        <span className="font-mono text-sm font-medium">{tool.name}</span>
        <span className={cn(
          "rounded px-1.5 py-0.5 text-[10px] font-medium",
          tool.kind === "mcp"
            ? "bg-violet-50 text-violet-700"
            : "bg-sky-50 text-sky-700",
        )}>
          {tool.kind === "mcp" ? "MCP" : "Skill"}
        </span>
      </span>
      <span className="mt-0.5 block text-xs text-muted-foreground">
        {tool.description}
      </span>
    </span>
  );
}
