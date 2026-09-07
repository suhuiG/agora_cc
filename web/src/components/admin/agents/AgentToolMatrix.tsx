"use client";

import { useState } from "react";
import useSWR from "swr";
import {
  ALL_USERS_SWR_KEY,
  approveAgentToolBinding,
  deployAgentPolicy,
  getAgentInvokeAuthorization,
  getAgentIdentity,
  getAgentPolicyReconciliation,
  getAgentToolCandidates,
  getCatalog,
  listAllCognitoUsers,
  listAgentToolBindings,
  listCognitoUsers,
  putAgentInvokeAuthorization,
  putAgentToolBinding,
  rejectAgentToolBinding,
  setAgentPermissionGroup,
  type AgentInvokeAuthorization,
  type AgentIdentity,
  type AgentPolicyDeployResult,
  type AgentPolicyReconciliation,
  type AgentToolBinding,
  type AgentToolCandidateAsset,
  type AgentToolCandidateOperation,
  type AssetCard,
  type CognitoUser,
  ApiError,
} from "@/lib/api";
import {
  candidateOperationListPresentation,
  candidateUnavailableReason,
  findToolCandidateBinding,
  requestJustificationLabel,
} from "@/lib/agentToolCandidates";
import { agentToolBindingErrorMessage } from "@/lib/agentToolBindingError";
import {
  PER_AGENT_POLICY_DEPRECATED_STATUS,
  deployOutcomeMessage,
  policyDeploymentFindings,
  policyDeploymentIsSuccessful,
} from "@/lib/agentPolicyDeployment";
import {
  cognitoUserLabel,
  filterCognitoUsers,
} from "@/lib/cognitoUsers";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { Input, Select } from "@/components/ui/input";
import { Icon } from "@/components/ui/icon";
import { SensitivityDisclosure } from "@/components/admin/SensitivityDisclosure";
import {
  EmptyState,
  Field,
  InlineError,
  LoadError,
  LoadingRows,
  PageHeading,
  StatusBadge,
  errorMessage,
  textAreaClass,
} from "@/components/admin/access/shared";
import { AgentBindingScopeNotice } from "./AgentBindingScopeNotice";

const AGENT_LIMIT = 100;

export function AgentToolMatrix() {
  const { data, error, isLoading } = useSWR(
    "catalog/agents-for-tool-bindings",
    () => getCatalog("Agent", 0, AGENT_LIMIT),
  );
  const [selectedId, setSelectedId] = useState("");
  const agents = data?.items ?? [];
  const selected = agents.find((agent) => agent.record_id === selectedId) ?? agents[0];

  return (
    <div className="min-w-0">
      <PageHeading
        title="Agent × Tool"
        description="Agent별 MCP operation binding을 관리해요. 새 허용 요청은 관리자 승인 후 policy에 반영돼요."
      />
      <AgentBindingScopeNotice />

      {isLoading ? (
        <LoadingRows label="Agent 목록 불러오는 중" />
      ) : error ? (
        <LoadError message="Agent 목록을 불러오지 못했어요." />
      ) : agents.length === 0 ? (
        <EmptyState
          title="관리할 Agent가 없어요."
          description="승인된 Agent가 카탈로그에 등록되면 여기에서 tool 권한을 관리할 수 있어요."
        />
      ) : (
        <>
          <div className="mb-5">
            <Field label="Agent" htmlFor="agent-tool-agent">
              <Select
                id="agent-tool-agent"
                value={selected?.record_id ?? ""}
                onChange={(event) => setSelectedId(event.target.value)}
                className="w-full sm:w-96"
              >
                {agents.map((agent) => (
                  <option key={agent.record_id} value={agent.record_id}>
                    {agent.name} (v{agent.version})
                  </option>
                ))}
              </Select>
            </Field>
          </div>
          {selected && <AgentPolicyPanel key={selected.record_id} agent={selected} />}
          {data && data.total > agents.length && (
            <p className="mt-4 text-xs text-muted-foreground">
              처음 {AGENT_LIMIT}개 Agent를 표시하고 있어요.
            </p>
          )}
        </>
      )}
    </div>
  );
}

function AgentPolicyPanel({ agent }: { agent: AssetCard }) {
  const bindingKey = `assets/${agent.record_id}/tool-bindings`;
  const bindings = useSWR<AgentToolBinding[]>(bindingKey, () =>
    listAgentToolBindings(agent.record_id),
  );
  const candidates = useSWR(
    `assets/${agent.record_id}/tool-candidates`,
    () => getAgentToolCandidates(agent.record_id),
  );
  const reconciliation = useSWR<AgentPolicyReconciliation>(
    `assets/${agent.record_id}/policy-reconciliation`,
    () => getAgentPolicyReconciliation(agent.record_id),
  );
  const invokeAuthorization = useSWR<AgentInvokeAuthorization>(
    `assets/${agent.record_id}/invoke-authorization`,
    () => getAgentInvokeAuthorization(agent.record_id),
  );
  const identity = useSWR<AgentIdentity>(
    `assets/${agent.record_id}/agent-identity`,
    () => getAgentIdentity(agent.record_id),
    { shouldRetryOnError: false },
  );
  const [deploying, setDeploying] = useState(false);
  const [deployResult, setDeployResult] = useState<AgentPolicyDeployResult>();
  const [deployError, setDeployError] = useState("");

  async function refreshAll(result?: AgentPolicyDeployResult) {
    if (result) setDeployResult(result);
    await Promise.all([
      bindings.mutate(),
      candidates.mutate(),
      reconciliation.mutate(),
      invokeAuthorization.mutate(),
    ]);
  }

  async function deployPolicy() {
    setDeploying(true);
    setDeployError("");
    try {
      const result = await deployAgentPolicy(agent.record_id);
      setDeployResult(result);
      await reconciliation.mutate();
    } catch (caught) {
      setDeployError(errorMessage(caught, "Policy를 배포하지 못했어요."));
    } finally {
      setDeploying(false);
    }
  }

  return (
    <div className="space-y-6">
      <section className="rounded-lg border border-border">
        <header className="flex flex-wrap items-start justify-between gap-3 border-b border-border px-4 py-3">
          <div className="min-w-0">
            <h2 className="truncate text-base font-semibold">{agent.name}</h2>
            <p className="mt-1 break-all text-xs text-muted-foreground">{agent.record_id}</p>
          </div>
          <StatusBadge status={agent.status} />
        </header>
        <div className="space-y-5 p-4">
          <ReconciliationStatus
            reconciliation={reconciliation.data}
            loading={reconciliation.isLoading}
            error={Boolean(reconciliation.error)}
          />
          <PolicyDeploymentDetails
            reconciliation={reconciliation.data}
            deploying={deploying}
            deployResult={deployResult}
            deployError={deployError}
            onDeploy={deployPolicy}
          />
        </div>
      </section>

      <AgentIdentitySection
        identity={identity.data}
        loading={identity.isLoading}
        error={identity.error}
      />

      <section id="tool-bindings" className="scroll-mt-6">
        <div className="mb-3">
          <h2 className="text-base font-semibold">Tool binding</h2>
          <p className="mt-1 text-sm text-muted-foreground">
            Agent가 선언한 MCP operation을 검토해 허용하거나 회수해요.
          </p>
        </div>
        {invokeAuthorization.data && (
          <div className="mb-4">
            <PermissionGroupSelector
              key={invokeAuthorization.data.updated_at}
              agentId={agent.record_id}
              authorization={invokeAuthorization.data}
              onSaved={async (result) => {
                setDeployResult(result);
                await Promise.all([
                  invokeAuthorization.mutate(),
                  reconciliation.mutate(),
                ]);
              }}
            />
          </div>
        )}
        <div>
          {candidates.data?.dependencyWarning && (
            <div
              role="alert"
              className="mb-3 rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-900"
            >
              {candidates.data.dependencyWarning}
            </div>
          )}
          {bindings.isLoading || candidates.isLoading ? (
            <LoadingRows label="Tool binding 불러오는 중" count={2} />
          ) : bindings.error || candidates.error ? (
            <LoadError message="Tool binding을 불러오지 못했어요." />
          ) : candidates.data?.mcpAssets.length === 0 ? (
            <EmptyState
              title="이 Agent에 선언된 MCP 의존성이 없어요."
              description="Agent의 의존성 정책에 MCP 자산을 선언하면 operation이 자동으로 표시돼요."
            />
          ) : (
            <div className="space-y-3">
              {candidates.data?.mcpAssets.map((asset) => (
                <ToolCandidateGroup
                  key={asset.assetId}
                  agentId={agent.record_id}
                  asset={asset}
                  bindings={bindings.data ?? []}
                  onSaved={refreshAll}
                />
              ))}
            </div>
          )}
        </div>
      </section>

      <section className="rounded-lg border border-border p-4 sm:p-5">
        <h2 className="text-base font-semibold">호출 자격</h2>
        <p className="mt-1 text-sm text-muted-foreground">
          추가 사용자와 그룹을 allowlist로 관리해요.
        </p>
        {invokeAuthorization.isLoading ? (
          <div className="mt-4"><LoadingRows label="호출 자격 불러오는 중" count={2} /></div>
        ) : invokeAuthorization.error || !invokeAuthorization.data ? (
          <div className="mt-4"><LoadError message="호출 자격을 불러오지 못했어요." /></div>
        ) : (
          <InvokeAuthorizationForm
            key={invokeAuthorization.data.updated_at}
            authorization={invokeAuthorization.data}
            onSaved={async () => {
              await invokeAuthorization.mutate();
            }}
          />
        )}
      </section>
    </div>
  );
}

function AgentIdentitySection({
  identity,
  loading,
  error,
}: {
  identity?: AgentIdentity;
  loading: boolean;
  error: unknown;
}) {
  const notIssued = error instanceof ApiError && error.status === 404;

  return (
    <section className="rounded-lg border border-border p-4 sm:p-5">
      <div className="mb-4 flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="text-base font-semibold">IAM 신원</h2>
          <p className="mt-1 text-sm text-muted-foreground">
            Gateway 호출과 Cedar policy principal에 사용되는 읽기 전용 신원이에요.
          </p>
        </div>
        {identity && <StatusBadge status={identity.status} />}
      </div>

      {loading ? (
        <LoadingRows label="IAM 신원 불러오는 중" count={2} />
      ) : notIssued ? (
        <EmptyState
          title="IAM 신원 미발급"
          description="Agent 배포 또는 외부 IAM role enroll 완료 후 생성돼요."
        />
      ) : error || !identity ? (
        <LoadError message="IAM 신원을 불러오지 못했어요." />
      ) : (
        <dl className="grid gap-x-6 gap-y-4 sm:grid-cols-2">
          <IdentityValue
            label="신원 유형"
            value={
              identity.identityType === "MANAGED_RUNTIME_ROLE"
                ? "관리형 Runtime role"
                : "외부 IAM role"
            }
          />
          <IdentityValue label="Workload identity" value={identity.workloadIdentityName} />
          <IdentityValue label="Runtime role ARN" value={identity.runtimeRoleArn} />
          <IdentityValue label="외부 source role ARN" value={identity.externalSourceRoleArn} />
          <IdentityValue label="Gateway role ARN" value={identity.gatewayRoleArn} />
          <IdentityValue label="Cedar principal" value={identity.policyPrincipalId} />
          <IdentityValue label="검증 시각" value={identity.verifiedAt} />
        </dl>
      )}
    </section>
  );
}

function IdentityValue({
  label,
  value,
}: {
  label: string;
  value: string | null;
}) {
  return (
    <div className="min-w-0">
      <dt className="text-xs font-medium text-muted-foreground">{label}</dt>
      <dd className="mt-1 break-all font-mono text-xs text-foreground">
        {value || "미지정"}
      </dd>
    </div>
  );
}

// per-agent Cedar 층 폐기 배너 (ADR-0093 · ADR-0112). 「미배포」·「drift」 배지로 그리면
// 배선이 덜 된 것처럼 읽혀서 관리자가 «동기화» 를 누르는데, 그 버튼은 설계상 아무것도 만들지
// 않아요. 그래서 상태값을 그대로 말해 줘요 — 빈 상태가 아니라 폐기 배너예요.
function PerAgentPolicyDeprecatedNotice({ reason }: { reason?: string }) {
  return (
    <div
      role="alert"
      className="rounded-lg border border-amber-300 bg-amber-50 p-3.5 text-xs text-amber-900"
    >
      <b className="block text-[13px]">
        agent별 Cedar 정책은 폐기됐어요 (ADR-0093)
      </b>
      <p className="mt-1 leading-relaxed">
        {reason ||
          "agent 하나당 Cedar 정책 한 장을 만드는 경로가 없어졌어요. 공유 정책은 Gateway 인프라로 provisioning 해요."}
      </p>
      <p className="mt-1 leading-relaxed">
        도구 인가는 두 층이에요 — <b>agent 의 도구 승인</b>과{" "}
        <b>사람·그룹의 도구 권한</b>. 아래 Tool binding 에서 바꿔요. 여기서 보이는 배포 원장은
        폐기 전 기록이라 <b>읽기 전용</b>이고, 어긋남 관측은 더 하지 않아요.
      </p>
    </div>
  );
}

function isPerAgentPolicyDeprecated(
  reconciliation?: AgentPolicyReconciliation,
): boolean {
  return reconciliation?.status === PER_AGENT_POLICY_DEPRECATED_STATUS;
}

function ReconciliationStatus({
  reconciliation,
  loading,
  error,
}: {
  reconciliation?: AgentPolicyReconciliation;
  loading: boolean;
  error: boolean;
}) {
  if (loading) return <LoadingRows label="Policy reconciliation 불러오는 중" count={1} />;
  if (error || !reconciliation) return <InlineError message="Policy reconciliation을 불러오지 못했어요." />;
  if (isPerAgentPolicyDeprecated(reconciliation)) {
    return <PerAgentPolicyDeprecatedNotice reason={reconciliation.reason} />;
  }

  const deployment = reconciliation.latest_deployment;
  const deploymentHasFindings =
    policyDeploymentFindings(deployment).length > 0;
  const reconciliationBadge = !deployment
    ? { label: "미배포", className: "bg-slate-100 text-slate-700" }
    : deploymentHasFindings
      ? { label: "확인 필요", className: "bg-amber-100 text-amber-800" }
      : reconciliation.in_sync
        ? { label: "in sync", className: "bg-emerald-100 text-emerald-700" }
        : { label: "drift", className: "bg-amber-100 text-amber-800" };
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-2">
        <Badge
          variant="type"
          className={reconciliationBadge.className}
        >
          {reconciliationBadge.label}
        </Badge>
        {deployment && (
          deploymentHasFindings
            ? (
                <Badge
                  variant="type"
                  className="bg-amber-100 text-amber-800"
                >
                  finding
                </Badge>
              )
            : <StatusBadge status={deployment.status} />
        )}
        {deployment && <Badge variant="outline">revision {deployment.revision}</Badge>}
        <Badge variant="outline">revision lag {reconciliation.revision_lag}</Badge>
        {reconciliation.no_tool_access && (
          <Badge variant="type" className="bg-slate-100 text-slate-700">no tool access</Badge>
        )}
      </div>
      <p className="text-xs text-muted-foreground">
        {deployment
          ? deployment.deployed_at
            ? `배포 시각 ${deployment.deployed_at}`
            : "Policy가 아직 ACTIVE로 확인되지 않았어요."
          : "아직 배포된 policy revision이 없어요."}
      </p>
    </div>
  );
}

function PolicyDeploymentDetails({
  reconciliation,
  deploying,
  deployResult,
  deployError,
  onDeploy,
}: {
  reconciliation?: AgentPolicyReconciliation;
  deploying: boolean;
  deployResult?: AgentPolicyDeployResult;
  deployError: string;
  onDeploy: () => Promise<void>;
}) {
  const deployment = reconciliation?.latest_deployment;
  const deprecated = isPerAgentPolicyDeprecated(reconciliation);
  const findings = Array.from(new Set([
    ...policyDeploymentFindings(deployment),
    ...policyDeploymentFindings(deployResult?.deployment),
  ]));

  return (
    <div className="space-y-4 border-t border-border pt-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h3 className="text-sm font-semibold">
            Cedar policy{deprecated ? " (폐기 · 읽기 전용)" : ""}
          </h3>
          <p className="mt-1 text-xs text-muted-foreground">
            {deprecated ? (
              <>
                이 아래는 폐기 전에 남은 배포 기록이에요 — 되돌림·감사용으로만 봐요. 도구 인가는{" "}
                <a
                  className="font-medium text-primary hover:underline"
                  href="#tool-bindings"
                >
                  Tool binding
                </a>
                에서 바꿔요. 오른쪽 버튼은 <b>새 정책을 만들지 않아요</b> — 눌러도 「설계상 만들지
                않아요」라는 결과만 돌아와요.
              </>
            ) : (
              <>
                Policy는{" "}
                <a
                  className="font-medium text-primary hover:underline"
                  href="#tool-bindings"
                >
                  Tool binding
                </a>
                에서 관리하며 Cedar 문서는 읽기 전용이에요.
              </>
            )}
          </p>
        </div>
        {/* ⚠️ 버튼을 지우거나 disabled 로 바꾸지 않았어요. 이 화면(`/admin/agents`)은 09-07 시연
            경로라 «동작» 변경이 금지예요. 이 라우트는 죽은 층에 **쓰지 않아요**(폐기 게이트가
            `SKIPPED_PER_AGENT_DEPRECATED` 만 돌려줘요) — ⑤ 처럼 「죽은 층에 성공적으로 쓰는」
            액션이 아니라서 IH-162 의 액션 삭제 범위 밖이에요. 잔여는 ADR-0112 Open risks. */}
        <Button
          type="button"
          size="sm"
          variant="outline"
          onClick={onDeploy}
          disabled={deploying}
        >
          {deploying
            ? "배포 중…"
            : reconciliation?.in_sync
              ? "재배포"
              : "동기화"}
        </Button>
      </div>

      {deployResult && (
        <p
          className={
            policyDeploymentIsSuccessful(deployResult)
              ? "text-sm text-emerald-700"
              : "text-sm text-amber-800"
          }
        >
          {deployOutcomeMessage(deployResult.outcome)}
        </p>
      )}
      {deployError && <InlineError message={deployError} />}

      {deployment?.cedar_policy ? (
        <pre className="max-h-96 max-w-full overflow-auto rounded-lg bg-zinc-950 p-4 font-mono text-xs leading-5 text-zinc-100">
          <code>{deployment.cedar_policy}</code>
        </pre>
      ) : (
        // 폐기된 층에서 「아직 배포된 정책 없음」은 «아직» 이 거짓이에요 — 앞으로도 안 생겨요.
        <p className="rounded-lg bg-muted/50 px-3 py-4 text-sm text-muted-foreground">
          {deprecated
            ? "이 agent 에는 폐기 전 배포 기록이 없어요. agent별 Cedar 정책은 앞으로도 만들지 않아요."
            : "아직 배포된 정책 없음"}
        </p>
      )}

      {findings.length > 0 && (
        <div>
          <h3 className="text-sm font-semibold text-red-700">Validation findings</h3>
          <ul className="mt-2 list-disc space-y-1 pl-5 text-sm text-red-700">
            {findings.map((finding, index) => (
              <li key={`${index}:${finding}`} className="break-words">
                {finding}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

const PERMISSION_GROUPS = [
  "ReadOnly",
  "ReadCreate",
  "ReadWrite",
  "FullAccess",
] as const;

const PERMISSION_GROUP_LABELS: Record<(typeof PERMISSION_GROUPS)[number], string> = {
  ReadOnly: "ReadOnly · 읽기 전용",
  ReadCreate: "ReadCreate · 읽기+생성",
  ReadWrite: "ReadWrite · 읽기+쓰기",
  FullAccess: "FullAccess · 전체 권한",
};

// ReadWrite·FullAccess는 최소 권한 원칙을 넘어서니 사유가 필수예요(서버도 422로 막아요).
function permissionGroupNeedsJustification(group: string): boolean {
  return group === "ReadWrite" || group === "FullAccess";
}

function PermissionGroupSelector({
  agentId,
  authorization,
  onSaved,
}: {
  agentId: string;
  authorization: AgentInvokeAuthorization;
  onSaved: (result: AgentPolicyDeployResult) => Promise<void>;
}) {
  const currentGroup = authorization.permission_group || "ReadOnly";
  const currentJustification = authorization.permission_group_justification ?? "";
  const [group, setGroup] = useState<string>(currentGroup);
  const [justification, setJustification] = useState(currentJustification);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState(false);
  const [savedResult, setSavedResult] = useState<AgentPolicyDeployResult>();

  const needsJustification = permissionGroupNeedsJustification(group);
  const dirty = group !== currentGroup || justification !== currentJustification;
  const canSave =
    !busy && dirty && (!needsJustification || justification.trim().length > 0);
  const savedFindings = policyDeploymentFindings(savedResult?.deployment);
  const savedPolicySucceeded = savedResult
    ? policyDeploymentIsSuccessful(savedResult)
    : false;

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError("");
    setSaved(false);
    setSavedResult(undefined);
    try {
      const response = await setAgentPermissionGroup(agentId, {
        permission_group: group,
        justification: justification.trim(),
      });
      setSavedResult(response.policy_deployment);
      setSaved(true);
      await onSaved(response.policy_deployment);
    } catch (caught) {
      setError(errorMessage(caught, "권한 그룹을 저장하지 못했어요."));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form
      onSubmit={submit}
      className="rounded-lg border border-border bg-muted/20 p-4"
    >
      <div className="flex flex-wrap items-end gap-3">
        <Field
          label="권한 그룹"
          htmlFor={`perm-group-${agentId}`}
          hint="기록용 · 도구 인가 판정에 쓰이지 않아요"
        >
          <Select
            id={`perm-group-${agentId}`}
            value={group}
            onChange={(event) => setGroup(event.target.value)}
            className="w-full sm:w-64"
          >
            {PERMISSION_GROUPS.map((value) => (
              <option key={value} value={value}>
                {PERMISSION_GROUP_LABELS[value]}
              </option>
            ))}
          </Select>
        </Field>
        <Badge variant="outline">현재 {currentGroup}</Badge>
      </div>
      {needsJustification && (
        <div className="mt-3">
          <Field
            label="사유"
            htmlFor={`perm-justify-${agentId}`}
            required
            hint="ReadWrite·FullAccess는 사유 필수"
          >
            <textarea
              id={`perm-justify-${agentId}`}
              value={justification}
              onChange={(event) => setJustification(event.target.value)}
              rows={2}
              className={textAreaClass}
              placeholder="상향 권한이 필요한 이유를 적어 주세요."
            />
          </Field>
        </div>
      )}
      {/* 옛 문구는 「Cedar가 그룹 범위 안의 tool만 남기고 나머지는 걸러내요」였고, 그건 **거짓**
          이에요. 2026-09-05 실측: `permission_group` 을 읽는 자리는 `agent_policy_compiler.py`
          에 세 곳뿐이고(import · `build_policy_spec` 인자 · 그 안의 `group_allows`) 전부
          **폐기된 per-agent 컴파일러** 안이에요. 살아 있는 공유 컴파일러
          (`compile_shared_gateway_policies` / `SharedGatewayPolicySpec`)에는 그 필드가 없고,
          `gateway_interceptor.py` 에는 `permission_group`·`allowed_tags` 가 **0회** 나와요. */}
      <p className="mt-3 text-xs text-muted-foreground">
        Cedar 는 도구 이름만 열거해요. 누가 부를 수 있는지는 원장의 <b>agent 도구 승인</b>과{" "}
        <b>사람·그룹 도구 권한</b> 행이 정하고, 권한 그룹은 그 판정에 들어가지 않아요. 이 값은
        기록으로만 남아요 (ADR-0093 · ADR-0099).
      </p>
      <div className="mt-3 flex flex-wrap items-center gap-3">
        <Button type="submit" size="sm" disabled={!canSave}>
          {busy ? "저장 중…" : "권한 그룹 저장"}
        </Button>
        {saved && !error && savedResult && (
          <div className="min-w-0">
            <p
              className={
                savedPolicySucceeded
                  ? "text-sm text-emerald-700"
                  : "text-sm text-amber-800"
              }
            >
              {savedPolicySucceeded
                ? "권한 그룹을 저장하고 policy를 다시 컴파일했어요."
                : `권한 그룹은 저장됐지만 ${deployOutcomeMessage(savedResult.outcome)}`}
            </p>
            {savedFindings.length > 0 && (
              <ul className="mt-1 list-disc space-y-1 pl-5 text-sm text-amber-800">
                {savedFindings.map((finding, index) => (
                  <li key={`${index}:${finding}`} className="break-words">
                    {finding}
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}
        {error && <InlineError message={error} />}
      </div>
    </form>
  );
}

function ToolCandidateGroup({
  agentId,
  asset,
  bindings,
  onSaved,
}: {
  agentId: string;
  asset: AgentToolCandidateAsset;
  bindings: AgentToolBinding[];
  onSaved: (result?: AgentPolicyDeployResult) => Promise<void>;
}) {
  const unavailable = candidateUnavailableReason(asset);
  const operationList = candidateOperationListPresentation(asset);

  return (
    <section className="overflow-hidden rounded-lg border border-border">
      <header className="flex flex-wrap items-start justify-between gap-2 bg-muted/30 px-4 py-3">
        <div className="min-w-0">
          <h3 className="break-words text-sm font-semibold">{asset.assetName}</h3>
          <p className="mt-0.5 break-all text-xs text-muted-foreground">
            {asset.assetId}{asset.version ? ` · v${asset.version}` : ""}
          </p>
        </div>
        <div className="flex flex-wrap gap-1.5">
          <StatusBadge status={asset.approved ? "APPROVED" : "미승인"} />
          <StatusBadge status={asset.gatewayConnected ? "CONNECTED" : "미연결"} />
        </div>
      </header>
      {unavailable && (
        <p className="border-t border-border bg-amber-50 px-4 py-2 text-xs text-amber-800">
          {unavailable}
        </p>
      )}
      {operationList.state !== "operations" ? (
        <p
          role={operationList.state === "unknown" ? "alert" : undefined}
          className={
            operationList.state === "unknown"
              ? "border-t border-amber-300 bg-amber-50 px-4 py-5 text-sm text-amber-900"
              : "border-t border-border px-4 py-5 text-sm text-muted-foreground"
          }
        >
          {operationList.message}
        </p>
      ) : (
        <div className="divide-y divide-border border-t border-border">
          {asset.operations.map((operation) => (
            <ToolCandidateRow
              key={operation.operationId}
              agentId={agentId}
              asset={asset}
              operation={operation}
              binding={findToolCandidateBinding(asset, operation, bindings)}
              onSaved={onSaved}
            />
          ))}
        </div>
      )}
    </section>
  );
}

const BINDING_LABELS: Record<AgentToolCandidateOperation["bindingState"], string> = {
  NONE: "미설정",
  REQUESTED: "요청됨",
  APPROVED: "승인됨",
  REJECTED: "반려됨",
};

function ToolCandidateRow({
  agentId,
  asset,
  operation,
  binding,
  onSaved,
}: {
  agentId: string;
  asset: AgentToolCandidateAsset;
  operation: AgentToolCandidateOperation;
  binding?: AgentToolBinding;
  onSaved: (result?: AgentPolicyDeployResult) => Promise<void>;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [requestJustification, setRequestJustification] = useState("");
  const { confirm, dialog } = useConfirm();

  async function act(action: "allow" | "approve" | "reject" | "revoke") {
    if (
      action === "approve" &&
      binding &&
      !(await confirm({
        title: "Tool binding을 승인할까요?",
        description: `신청 사유: ${requestJustificationLabel(binding.request_justification)}`,
        confirmLabel: "승인",
      }))
    ) {
      return;
    }
    setBusy(true);
    setError("");
    try {
      let policyResult: AgentPolicyDeployResult | undefined;
      if (action === "approve" && binding) {
        const approved = await approveAgentToolBinding(binding);
        policyResult = approved.policy_deployment;
      } else if (action === "reject" && binding) {
        await rejectAgentToolBinding(binding);
      } else if (action === "revoke") {
        await putAgentToolBinding(
          agentId,
          asset.assetId,
          asset.version,
          operation.operationId,
          "REVOKED",
          binding?.request_justification ?? "",
        );
      } else if (action === "allow") {
        const requested = await putAgentToolBinding(
          agentId,
          asset.assetId,
          asset.version,
          operation.operationId,
          "ALLOWED",
          requestJustification,
        );
        const approved = await approveAgentToolBinding(requested);
        policyResult = approved.policy_deployment;
        setRequestJustification("");
      }
      await onSaved(policyResult);
    } catch (caught) {
      setError(agentToolBindingErrorMessage(caught));
      await onSaved().catch(() => undefined);
    } finally {
      setBusy(false);
    }
  }

  const requested = operation.bindingState === "REQUESTED";
  const active =
    operation.bindingState === "APPROVED" &&
    operation.desiredState === "ALLOWED";
  const needsRequestJustification = operation.sensitivity !== "READ";
  const canAllow =
    operation.ready &&
    (!needsRequestJustification || Boolean(requestJustification.trim()));

  return (
    <>
      <div className={requested ? "grid min-w-0 gap-3 bg-amber-50/60 px-4 py-3 md:grid-cols-[minmax(0,1fr)_auto] md:items-center" : "grid min-w-0 gap-3 px-4 py-3 md:grid-cols-[minmax(0,1fr)_auto] md:items-center"}>
      <div className="min-w-0">
        <div className="flex flex-wrap items-center gap-2">
          <span className="break-all font-mono text-sm font-semibold">
            {operation.operationId}
          </span>
          <Badge
            variant="type"
            className={
              requested
                ? "bg-amber-100 text-amber-800"
                : active
                  ? "bg-emerald-100 text-emerald-700"
                  : operation.bindingState === "REJECTED"
                    ? "bg-red-100 text-red-700"
                    : "bg-slate-100 text-slate-600"
            }
          >
            {BINDING_LABELS[operation.bindingState]}
          </Badge>
          {operation.desiredState === "REVOKED" && (
            <Badge variant="outline">회수 요청</Badge>
          )}
        </div>
        <SensitivityDisclosure
          className="mt-1.5"
          sensitivity={operation.sensitivity}
          source={operation.sensitivitySource}
          status={asset.sensitivityStatus}
        />
        {requested && (
          <>
            <p className="mt-1 text-xs font-medium text-amber-800">
              신청자가 제안한 operation이에요. 승인 여부를 검토해 주세요.
            </p>
            <p className="mt-1 break-words text-sm">
              <span className="font-medium">신청 사유</span>{" "}
              {requestJustificationLabel(binding?.request_justification)}
            </p>
          </>
        )}
        {!operation.ready && (
          <p className="mt-1 text-xs text-muted-foreground">
            {candidateUnavailableReason(asset)}
          </p>
        )}
        {!requested && !active && needsRequestJustification && (
          <div className="mt-3 max-w-xl">
            <Field
              label="신청 사유"
              htmlFor={`tool-justify-${asset.assetId}-${operation.operationId}`}
              required
            >
              <textarea
                id={`tool-justify-${asset.assetId}-${operation.operationId}`}
                value={requestJustification}
                onChange={(event) => setRequestJustification(event.target.value)}
                rows={2}
                maxLength={2000}
                className={textAreaClass}
                placeholder="이 operation 권한이 필요한 이유를 적어 주세요."
              />
            </Field>
          </div>
        )}
        {error && <div className="mt-2"><InlineError message={error} /></div>}
      </div>
      <div className="flex flex-wrap gap-2 md:justify-end">
        {requested ? (
          <>
            <Button
              size="sm"
              onClick={() => act("approve")}
              disabled={busy || !binding || !operation.ready}
              title={!operation.ready ? candidateUnavailableReason(asset) : undefined}
            >
              {busy ? "처리 중…" : "승인"}
            </Button>
            <Button
              variant="outline"
              size="sm"
              className="text-red-600"
              onClick={() => act("reject")}
              disabled={busy || !binding}
            >
              반려
            </Button>
          </>
        ) : active ? (
          <Button
            variant="outline"
            size="sm"
            className="text-red-600"
            onClick={() => act("revoke")}
            disabled={busy}
          >
            {busy ? "처리 중…" : "회수"}
          </Button>
        ) : (
          <Button
            size="sm"
            onClick={() => act("allow")}
            disabled={busy || !canAllow}
            title={!operation.ready ? candidateUnavailableReason(asset) : undefined}
          >
            {busy ? "처리 중…" : "허용"}
          </Button>
        )}
      </div>
      </div>
      {dialog}
    </>
  );
}

function InvokeAuthorizationForm({
  authorization,
  onSaved,
}: {
  authorization: AgentInvokeAuthorization;
  onSaved: () => Promise<void>;
}) {
  const [principals, setPrincipals] = useState(
    authorization.allowed_principals.map((sub) => ({ sub, groups: [] as string[] })),
  );
  const [groups, setGroups] = useState(authorization.allowed_groups);
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<CognitoUser[]>([]);
  const [searching, setSearching] = useState(false);
  const [searchError, setSearchError] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const directory = useSWR(
    ALL_USERS_SWR_KEY,
    listAllCognitoUsers,
  );

  async function searchUsers(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const value = query.trim();
    if (!value) {
      setSearchError("이름 또는 email을 입력해 주세요.");
      return;
    }
    setSearching(true);
    setSearchError("");
    try {
      const page = await listCognitoUsers({ query: value });
      let matches = filterCognitoUsers(page.items, value);
      if (matches.length === 0) {
        const allUsers = directory.data ?? await listAllCognitoUsers();
        matches = filterCognitoUsers(allUsers, value);
      }
      setResults(matches);
      if (matches.length === 0) setSearchError("검색 결과가 없어요.");
    } catch (caught) {
      setSearchError(errorMessage(caught, "사용자를 검색하지 못했어요."));
    } finally {
      setSearching(false);
    }
  }

  function addPrincipal(user: CognitoUser) {
    setPrincipals((current) =>
      current.some((item) => item.sub === user.sub)
        ? current
        : [...current, {
            sub: user.sub,
            groups: user.groups,
          }],
    );
  }

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      await putAgentInvokeAuthorization(authorization.agent_id, {
        allowed_principals: principals.map((item) => item.sub),
        allowed_groups: groups,
      });
      await onSaved();
    } catch (caught) {
      setError(errorMessage(caught, "호출 자격을 저장하지 못했어요."));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="mt-4 space-y-4">
      <div className="grid gap-4 md:grid-cols-2">
        <div>
          <form onSubmit={searchUsers}>
            <Field label="추가 사용자" htmlFor="invoke-user-search" hint="이름 또는 email 검색">
              <div className="flex gap-2">
                <Input
                  id="invoke-user-search"
                  value={query}
                  onChange={(event) => setQuery(event.target.value)}
                  placeholder="user@example.com"
                />
                <Button
                  type="submit"
                  variant="outline"
                  disabled={searching}
                  className="shrink-0 whitespace-nowrap [writing-mode:horizontal-tb]"
                >
                  <Icon name="search" size={15} />
                  {searching ? "검색 중" : "검색"}
                </Button>
              </div>
            </Field>
          </form>
          {searchError && <div className="mt-2"><InlineError message={searchError} /></div>}
          {results.length > 0 && (
            <div className="mt-2 max-h-44 divide-y divide-border overflow-auto rounded-lg border border-border">
              {results.map((user) => (
                <button
                  key={user.sub}
                  type="button"
                  onClick={() => addPrincipal(user)}
                  disabled={principals.some((item) => item.sub === user.sub)}
                  className="flex w-full min-w-0 items-center justify-between gap-3 px-3 py-2 text-left hover:bg-accent disabled:opacity-50"
                >
                  <span className="min-w-0">
                    <span className="block truncate text-sm font-medium">{user.name || user.email}</span>
                    <span className="block truncate text-xs text-muted-foreground">{user.email}</span>
                  </span>
                  <span className="shrink-0 text-xs text-primary">추가</span>
                </button>
              ))}
            </div>
          )}
          <ChipList
            items={principals.map((item) => {
              const user = [...results, ...(directory.data ?? [])].find(
                (candidate) => candidate.sub === item.sub,
              );
              return {
                key: item.sub,
                label: user
                  ? cognitoUserLabel(user)
                  : directory.isLoading
                    ? "사용자 정보 불러오는 중"
                    : "확인할 수 없는 사용자",
              };
            })}
            empty="추가로 허용된 사용자가 없어요."
            onRemove={(sub) => setPrincipals((current) => current.filter((item) => item.sub !== sub))}
          />
        </div>
        <div>
          <span className="mb-1.5 block text-xs font-medium">허용 그룹</span>
          <div className="flex flex-wrap gap-2 rounded-lg border border-border p-3">
            {Array.from(new Set([
              "admin",
              "user",
              ...groups,
              ...results.flatMap((user) => user.groups),
              ...principals.flatMap((item) => item.groups),
            ])).sort().map((group) => {
              const selected = groups.includes(group);
              return (
                <Button
                  key={group}
                  type="button"
                  size="sm"
                  variant={selected ? "primary" : "outline"}
                  onClick={() =>
                    setGroups((current) =>
                      selected
                        ? current.filter((item) => item !== group)
                        : [...current, group],
                    )
                  }
                >
                  {group}
                </Button>
              );
            })}
          </div>
          <ChipList
            items={groups.map((group) => ({ key: group, label: group }))}
            empty="추가로 허용된 그룹이 없어요."
            onRemove={(group) => setGroups((current) => current.filter((item) => item !== group))}
          />
        </div>
      </div>
      <form onSubmit={submit} className="flex flex-wrap items-center gap-3">
        <Button type="submit" size="sm" disabled={busy}>
          {busy ? "저장 중…" : "호출 자격 저장"}
        </Button>
        <Badge variant="outline">default effect {authorization.default_effect}</Badge>
        {error && <InlineError message={error} />}
      </form>
    </div>
  );
}

function ChipList({
  items,
  empty,
  onRemove,
}: {
  items: { key: string; label: string }[];
  empty: string;
  onRemove: (key: string) => void;
}) {
  return items.length === 0 ? (
    <p className="mt-2 text-xs text-muted-foreground">{empty}</p>
  ) : (
    <div className="mt-2 flex flex-wrap gap-1.5">
      {items.map((item) => (
        <span
          key={item.key}
          className="inline-flex max-w-full items-center gap-1 rounded-full border border-border bg-card py-1 pl-2.5 pr-1 text-xs"
        >
          <span className="max-w-64 truncate">{item.label}</span>
          <button
            type="button"
            onClick={() => onRemove(item.key)}
            className="rounded-full p-1 text-muted-foreground hover:bg-accent hover:text-foreground"
            aria-label={`${item.label} 제거`}
            title="제거"
          >
            <Icon name="close" size={12} />
          </button>
        </span>
      ))}
    </div>
  );
}
