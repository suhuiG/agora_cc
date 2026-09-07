"use client";

import { useMemo, useState } from "react";
import useSWR from "swr";
import {
  approveAgentToolBinding,
  getCatalog,
  listAgentToolBindings,
  rejectAgentToolBinding,
  type AgentPolicyDeployResult,
  type AgentToolBinding,
  type AssetCard,
} from "@/lib/api";
import { requestJustificationLabel } from "@/lib/agentToolCandidates";
import { policyDeploymentPresentation } from "@/lib/agentPolicyDeployment";
import { Button } from "@/components/ui/button";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { SensitivityDisclosure } from "@/components/admin/SensitivityDisclosure";
import {
  EmptyState,
  InlineError,
  LoadError,
  LoadingRows,
  PageHeading,
  StatusBadge,
  errorMessage,
} from "@/components/admin/access/shared";
import { AgentBindingScopeNotice } from "./AgentBindingScopeNotice";

type PendingBinding = { agent: AssetCard; binding: AgentToolBinding };
const AGENT_LIMIT = 100;

export function AgentToolApprovalQueue() {
  const agents = useSWR("catalog/agents-for-tool-approval", () =>
    getCatalog("Agent", 0, AGENT_LIMIT),
  );
  const [actionError, setActionError] = useState("");
  const [busyKey, setBusyKey] = useState("");
  const [policyDeployment, setPolicyDeployment] =
    useState<AgentPolicyDeployResult>();
  const { confirm, dialog } = useConfirm();
  const agentItems = agents.data?.items ?? [];
  const pending = useSWR<PendingBinding[]>(
    agentItems.length > 0 ? `agent-tool-approval-queue/${agentItems.map((agent) => agent.record_id).join(",")}` : null,
    async () => {
      const all = await Promise.all(
        agentItems.map(async (agent) => ({
          agent,
          bindings: await listAgentToolBindings(agent.record_id),
        })),
      );
      return all.flatMap(({ agent, bindings }) =>
        bindings
          .filter((binding) => binding.approval_state === "REQUESTED")
          .map((binding) => ({ agent, binding })),
      );
    },
  );

  const queue = useMemo(() => pending.data ?? [], [pending.data]);

  async function act(item: PendingBinding, action: "approve" | "reject") {
    if (
      action === "approve" &&
      !(await confirm({
        title: "Tool binding을 승인할까요?",
        description: `신청 사유: ${requestJustificationLabel(item.binding.request_justification)}`,
        confirmLabel: "승인",
      }))
    ) {
      return;
    }
    const key = `${item.agent.record_id}:${item.binding.asset_id}:${item.binding.operation_id}`;
    setBusyKey(key);
    setActionError("");
    setPolicyDeployment(undefined);
    try {
      if (action === "approve") {
        const approved = await approveAgentToolBinding(item.binding);
        setPolicyDeployment(approved.policy_deployment);
      } else {
        await rejectAgentToolBinding(item.binding);
      }
      await pending.mutate();
    } catch (caught) {
      setActionError(errorMessage(caught, "승인 요청을 처리하지 못했어요."));
    } finally {
      setBusyKey("");
    }
  }

  const policyPresentation = policyDeployment
    ? policyDeploymentPresentation(policyDeployment)
    : undefined;

  return (
    <div className="min-w-0">
      <PageHeading
        title="Tool binding 승인 큐"
        description="Agent별 MCP operation 허용 요청을 검토해 승인하거나 반려해요."
      />
      <AgentBindingScopeNotice />
      {actionError && <div className="mb-4"><InlineError message={actionError} /></div>}
      {policyPresentation && (
        <div
          role={policyPresentation.successful ? "status" : "alert"}
          className={
            policyPresentation.successful
              ? "mb-4 rounded-md border border-emerald-300 bg-emerald-50 px-3 py-2 text-sm text-emerald-900"
              : "mb-4 rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-900"
          }
        >
          <p>
            {policyPresentation.successful
              ? "Tool binding 승인과 policy 배포가 완료됐어요."
              : "Tool binding은 승인됐지만 policy 배포 결과를 확인해야 해요."}
          </p>
          {policyPresentation.findings.length > 0 && (
            <ul className="mt-1 list-disc space-y-1 pl-5">
              {policyPresentation.findings.map((finding, index) => (
                <li key={`${index}:${finding}`} className="break-words">
                  {finding}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
      {agents.isLoading || (agentItems.length > 0 && pending.isLoading) ? (
        <LoadingRows label="승인 요청 불러오는 중" />
      ) : agents.error || pending.error ? (
        <LoadError message="Tool binding 승인 요청을 불러오지 못했어요." />
      ) : agentItems.length === 0 || queue.length === 0 ? (
        <EmptyState
          title="검토할 tool binding 요청이 없어요."
          description="REQUESTED 상태의 Agent별 tool 허용 요청이 여기에 표시돼요."
        />
      ) : (
        <div className="divide-y divide-border rounded-lg border border-border">
          {queue.map((item) => {
            const key = `${item.agent.record_id}:${item.binding.asset_id}:${item.binding.operation_id}`;
            const busy = busyKey === key;
            return (
              <div key={key} className="grid min-w-0 gap-3 px-4 py-4 md:grid-cols-[minmax(0,1fr)_auto] md:items-center">
                <div className="min-w-0">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="text-sm font-semibold">{item.agent.name}</span>
                    <StatusBadge status={item.binding.approval_state} />
                    <StatusBadge status={item.binding.effective_state} />
                  </div>
                  <p className="mt-1 break-all text-xs text-muted-foreground">
                    {item.binding.asset_id} · v{item.binding.asset_version} · {item.binding.operation_id}
                  </p>
                  <p className="mt-1 break-all text-xs text-muted-foreground">
                    요청자 {item.binding.created_by} · {item.binding.gateway_action}
                  </p>
                  <SensitivityDisclosure
                    className="mt-2"
                    sensitivity={item.binding.current_sensitivity}
                    source={item.binding.sensitivity_source}
                    status={item.binding.sensitivity_status}
                  />
                  <p className="mt-2 break-words text-sm">
                    <span className="font-medium">신청 사유</span>{" "}
                    {requestJustificationLabel(item.binding.request_justification)}
                  </p>
                </div>
                <div className="flex flex-wrap gap-2 md:justify-end">
                  <Button size="sm" onClick={() => act(item, "approve")} disabled={busy}>
                    {busy ? "처리 중…" : "승인"}
                  </Button>
                  <Button variant="outline" size="sm" className="text-red-600" onClick={() => act(item, "reject")} disabled={busy}>
                    반려
                  </Button>
                </div>
              </div>
            );
          })}
        </div>
      )}
      {agents.data && agents.data.total > agentItems.length && (
        <p className="mt-4 text-xs text-muted-foreground">처음 {AGENT_LIMIT}개 Agent의 요청을 조회했어요.</p>
      )}
      {dialog}
    </div>
  );
}
