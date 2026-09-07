"use client";

import { useEffect, useState } from "react";
import {
  approveMcpToolSensitivity,
  previewMcpToolSensitivity,
  retryMcpToolSensitivity,
  SENSITIVITY_TAGS,
  setMcpToolSensitivity,
  type AssetToolDrift,
  type SensitivityChangePlan,
  type SensitivityChangeRequest,
  type SensitivityTag,
  type ToolDriftEntry,
} from "@/lib/api";
import {
  approvalBlockedReason,
  groupsPhrase,
  impactText,
  SENSITIVITY_SOURCE_LABEL,
  SENSITIVITY_SOURCE_MEANING,
  saveBlockedReason,
  sensitivityChangeFromError,
  sensitivityChangeStatusText,
} from "@/lib/mcpToolDrift";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Modal } from "@/components/ui/modal";
import { Select } from "@/components/ui/input";
import { cn } from "@/lib/ui";

/**
 * 민감도 태그 확정 대화상자 (티켓 A).
 *
 * 설계 의도가 하나예요 — 관리자가 **숫자를 고르는 게 아니라 결과를 보고 판단**하게
 * 하는 거요. 그래서 값을 고르면 저장하기 전에 서버에서 계획서를 받아 "어느 등급이 이
 * 도구를 얻고 잃는지"를 먼저 보여줘요. 위험도를 낮추는 변경에는 사유를 받아요 —
 * 등록자는 자기 도구를 낮게 태깅할 동기가 있으니까요(설계 §15 A4).
 */
export function SensitivityEditDialog({
  open,
  recordId,
  assetName,
  targetMode,
  tool,
  onClose,
  onSaved,
  onRefresh,
}: {
  open: boolean;
  recordId: string;
  assetName: string;
  targetMode: AssetToolDrift["target_mode"];
  tool: ToolDriftEntry;
  onClose: () => void;
  onSaved: (asset: AssetToolDrift) => void;
  onRefresh: () => Promise<unknown> | unknown;
}) {
  // 초기값은 props 에서 와요. 부모가 도구마다 `key` 를 주니 다른 도구를 열면 이
  // 컴포넌트가 새로 마운트되고, 직전 입력이 남지 않아요.
  const [choice, setChoice] = useState<SensitivityTag | "">(
    (tool.pending_change?.after ?? tool.sensitivity ?? "") as SensitivityTag | "",
  );
  const [reason, setReason] = useState(tool.pending_change?.reason ?? "");
  const [saveError, setSaveError] = useState("");
  const [saving, setSaving] = useState(false);
  const [change, setChange] = useState<SensitivityChangeRequest | null>(
    tool.pending_change,
  );
  const [previewRevision, setPreviewRevision] = useState(0);
  // 계획서는 **어느 값에 대한 계산인지**를 함께 들고 있어요. 고른 값과 어긋나면 아직
  // 계산 중이라는 뜻이에요 — effect 안에서 상태를 지우지 않고도 로딩을 표현할 수 있어요.
  const [planState, setPlanState] = useState<{
    choice: SensitivityTag | "";
    plan: SensitivityChangePlan | null;
    error: string;
  } | null>(null);

  // 고른 값마다 서버에서 계획서를 받아요. 등급 매핑을 화면에서 다시 구현하면
  // 백엔드와 어긋나는 순간 관리자가 틀린 결과를 보고 판단하게 돼요.
  useEffect(() => {
    if (!open) return;
    let alive = true;
    previewMcpToolSensitivity(recordId, tool.tool_name, choice || null)
      .then((next) => {
        if (alive) setPlanState({ choice, plan: next, error: "" });
      })
      .catch((e: unknown) => {
        if (alive) {
          setPlanState({
            choice,
            plan: null,
            error: e instanceof Error ? e.message : "영향을 계산하지 못했어요.",
          });
        }
      });
    return () => {
      alive = false;
    };
  }, [open, recordId, tool.tool_name, choice, previewRevision]);

  const fresh = planState?.choice === choice ? planState : null;
  const plan = fresh?.plan ?? null;
  const planError = fresh?.error ?? "";
  const blocked = saveBlockedReason(plan, reason);
  const approvalBlocked = approvalBlockedReason(plan);

  async function reobserveAfterFailure() {
    setPlanState(null);
    setPreviewRevision((current) => current + 1);
    await onRefresh();
  }

  async function save() {
    setSaving(true);
    setSaveError("");
    try {
      const result = await setMcpToolSensitivity(
        recordId,
        tool.tool_name,
        choice || null,
        reason,
      );
      onSaved(result.asset);
      setChange(result.change);
      if (result.change.status === "APPLIED") onClose();
    } catch (e) {
      const failedChange = sensitivityChangeFromError(e);
      if (failedChange) setChange(failedChange);
      setSaveError(e instanceof Error ? e.message : "저장에 실패했어요.");
      await reobserveAfterFailure();
    } finally {
      setSaving(false);
    }
  }

  async function continueChange(action: "approve" | "retry") {
    if (!change) return;
    setSaving(true);
    setSaveError("");
    try {
      const result = action === "approve"
        ? await approveMcpToolSensitivity(
            recordId,
            tool.tool_name,
            change.request_id,
          )
        : await retryMcpToolSensitivity(
            recordId,
            tool.tool_name,
            change.request_id,
          );
      onSaved(result.asset);
      setChange(result.change);
      if (result.change.status !== "APPROVED_PENDING_PROPAGATION") {
        onClose();
      }
    } catch (e) {
      const failedChange = sensitivityChangeFromError(e);
      if (failedChange) setChange(failedChange);
      setSaveError(e instanceof Error ? e.message : "이동에 실패했어요.");
      await reobserveAfterFailure();
    } finally {
      setSaving(false);
    }
  }

  const actionBlocked = change?.status === "PENDING_APPROVAL"
    ? approvalBlocked
    : change?.status === "APPROVED_PENDING_PROPAGATION"
      ? change.movement.reason || "IA-68 전파 경로가 준비되기를 기다리고 있어요"
    : change?.status === "APPLYING"
      ? change.retryable ? approvalBlocked : "이동 처리가 끝나기를 기다리고 있어요"
    : change?.status === "FAILED"
      ? change.retryable ? approvalBlocked : "재시도할 수 없는 실패예요"
      : blocked;
  const actionLabel = saving
    ? "처리 중…"
    : change?.status === "PENDING_APPROVAL"
      ? "승인 후 전파 대기"
      : change?.status === "APPROVED_PENDING_PROPAGATION"
        ? "승인됨·전파 대기(IA-68)"
      : change?.status === "APPLYING"
        ? "중단된 이동 재시도"
      : change?.status === "FAILED"
        ? "이동 재시도"
        : plan?.reason_required
          ? "하향 승인 요청"
          : "변경 요청";

  return (
    <Modal
      open={open}
      title="민감도 확정"
      description={
        <span>
          <span className="font-mono">{tool.tool_name}</span> · {assetName} — 이 딱지가
          어느 등급이 이 도구를 부를 수 있는지 결정해요.
        </span>
      }
      busy={saving}
      onClose={onClose}
      footer={
        <div className="flex flex-wrap items-center justify-end gap-2">
          {actionBlocked ? (
            <span className="mr-auto text-xs text-amber-700">{actionBlocked}</span>
          ) : null}
          <Button variant="outline" onClick={onClose} disabled={saving}>
            취소
          </Button>
          <Button
            onClick={() => {
              if (change?.status === "PENDING_APPROVAL") {
                void continueChange("approve");
              } else if (
                change?.status === "FAILED"
                || (change?.status === "APPLYING" && change.retryable)
              ) {
                void continueChange("retry");
              } else {
                void save();
              }
            }}
            disabled={saving || Boolean(actionBlocked)}
          >
            {actionLabel}
          </Button>
        </div>
      }
    >
      <div className="space-y-4 text-sm">
        <div className="flex flex-wrap items-center gap-3">
          <label className="flex items-center gap-2">
            <span className="text-xs text-muted-foreground">민감도</span>
            <Select
              aria-label="민감도"
              value={choice}
              onChange={(e) => setChoice(e.target.value as SensitivityTag | "")}
              disabled={Boolean(change)}
            >
              <option value="">— 미분류 —</option>
              {SENSITIVITY_TAGS.map((tag) => (
                <option key={tag} value={tag}>
                  {tag}
                </option>
              ))}
            </Select>
          </label>
          <span className="text-xs text-muted-foreground">
            현재 출처:{" "}
            {tool.sensitivity ? (
              <span title={SENSITIVITY_SOURCE_MEANING[tool.sensitivity_source ?? "unknown"]}>
                {SENSITIVITY_SOURCE_LABEL[tool.sensitivity_source ?? "unknown"]}
              </span>
            ) : (
              "없음"
            )}
            {" · 확정하면 "}
            <b>관리자 지정</b>
            {" 이 돼요"}
          </span>
        </div>

        {planError ? (
          <p className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">
            영향을 계산하지 못했어요: {planError} — 확인하지 못한 채로는 저장하지 않아요.
          </p>
        ) : null}

        {plan ? <PlanPanel plan={plan} targetMode={targetMode} /> : null}

        {change ? <ChangeState change={change} /> : null}

        {plan?.reason_required ? (
          <div>
            <label
              className="mb-1 block text-xs font-semibold text-amber-800"
              htmlFor="sensitivity-reason"
            >
              사유 (필수) — 위험도를 낮추는 변경이에요
            </label>
            <textarea
              id="sensitivity-reason"
              rows={3}
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              disabled={Boolean(change)}
              placeholder="왜 낮춰도 안전한지 적어 주세요. 이 사유는 감사 이력에 남아요."
              className={cn(
                "w-full rounded-lg border border-input bg-card px-3 py-2 text-sm",
                "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
              )}
            />
          </div>
        ) : null}

        {saveError ? (
          <p className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">
            저장 실패: {saveError}
          </p>
        ) : null}
      </div>
    </Modal>
  );
}

function ChangeState({ change }: { change: SensitivityChangeRequest }) {
  const status = sensitivityChangeStatusText(change);
  const agents = change.impact.find((item) => item.label.endsWith("agent"));
  return (
    <div className="border-y border-border py-3 text-xs">
      <p className="font-semibold">
        변경 요청 <span className="font-mono">{change.request_id.slice(0, 8)}</span>
        {" · "}{status}
      </p>
      <p className="mt-1 text-muted-foreground">
        민감도: {change.before ?? "미분류"} → {change.after ?? "미분류"}
      </p>
      <p className="mt-1 text-muted-foreground">
        Target: {change.target_before ?? "없음"} → {change.target_after ?? "없음"}
      </p>
      {agents ? (
        <p className="mt-1 text-muted-foreground">
          영향 agent: {agents.known
            ? agents.names.length
              ? agents.names.join(" · ")
              : `${agents.count ?? 0}개`
            : `확인 불가 · ${agents.reason}`}
        </p>
      ) : null}
      {change.movement.reason ? (
        <p className="mt-2 text-amber-800">{change.movement.reason}</p>
      ) : null}
      {change.error ? (
        <p className="mt-1 text-red-700">
          {change.error}
          {change.retryable ? " · 좌표를 보존해 재시도할 수 있어요" : ""}
        </p>
      ) : null}
    </div>
  );
}

function PlanPanel({
  plan,
  targetMode,
}: {
  plan: SensitivityChangePlan;
  targetMode: AssetToolDrift["target_mode"];
}) {
  return (
    <div
      className={cn(
        "space-y-2 rounded-lg border px-3 py-3",
        plan.downgrade
          ? "border-amber-300 bg-amber-50 text-amber-900"
          : "border-border bg-muted/40",
      )}
    >
      <p className="text-xs font-semibold">전파 후 예상 영향</p>
      <p className="text-xs">
        부를 수 있는 등급: <b>{groupsPhrase(plan.groups_before)}</b> →{" "}
        <b>{groupsPhrase(plan.groups_after)}</b>
      </p>
      {plan.groups_gained.length > 0 ? (
        <p className="text-xs">
          새로 부를 수 있게 되는 등급:{" "}
          <b>{plan.groups_gained.join(" · ")}</b>
        </p>
      ) : null}
      {plan.groups_lost.length > 0 ? (
        <p className="text-xs">
          못 부르게 되는 등급: <b>{plan.groups_lost.join(" · ")}</b>
        </p>
      ) : null}
      <p className="text-xs">
        상태: {plan.state_before} → <b>{plan.state_after}</b>
      </p>
      <ul className="space-y-0.5">
        {plan.impact.map((item) => (
          <li key={item.label} className="text-xs">
            {item.label}:{" "}
            {item.known ? (
              <b>{impactText(item)}</b>
            ) : (
              <span className="text-muted-foreground">{impactText(item)}</span>
            )}
          </li>
        ))}
      </ul>
      {plan.after === null ? (
        <p className="text-xs font-semibold">
          미분류로 두면{" "}
          <Badge variant="type">
            {targetMode === "connected"
              ? "실제 호출 가능성 확인 불가"
              : "아무도 못 불러요"}
          </Badge>
        </p>
      ) : null}
    </div>
  );
}
