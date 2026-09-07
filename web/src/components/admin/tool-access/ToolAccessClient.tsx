"use client";

import { useMemo, useState } from "react";
import useSWR from "swr";
import {
  TOOL_DRIFT_SWR_KEY,
  listCognitoUsers,
  listMcpToolDrift,
  listToolGrants,
  putToolGrant,
  revokeToolGrant,
  type AssetToolDrift,
  type ToolGrant,
} from "@/lib/api";
import {
  GRANT_LIST_UNKNOWN_MESSAGE,
  NOBODY_CAN_CALL_MESSAGE,
  PRINCIPAL_GRANT_NOTE,
  REVOKE_LABEL,
  TOOL_GRANT_GROUPS,
  grantStatusPresentation,
  isToolGrantGroup,
  revokeConfirmMessage,
  sortToolGrants,
  subjectLabel,
  toolGrantBlastRadius,
  toolGrantGroupLabel,
  toolGrantListNoticeState,
  toolLabel,
  type GrantTone,
  type ToolGrantGroup,
} from "@/lib/toolAccess";
import { formatKst } from "@/lib/auditCalls";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Select } from "@/components/ui/input";
import { Icon } from "@/components/ui/icon";
import { HelpPopover } from "@/components/ui/help-popover";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { PrincipalPicker } from "@/components/admin/authorization/PrincipalPicker";
import {
  EmptyState,
  InlineError,
  LoadError,
  LoadingRows,
  PageHeading,
  errorMessage,
} from "@/components/admin/access/shared";
import { cn } from "@/lib/ui";

// 도구별 «호출 주체» 관리 (/admin/tool-access) — ADR-0099 결정 7, IH-145.
//
// **축이 도구예요.** `/admin/tool-authorization` 은 ④축(agent 의 도구 신청 승인)이고 이 화면은
// ⑦축(이 도구를 부를 자격이 있는 사람·그룹)이에요. ⑦은 agent 와 무관해요 — 그 사람이 어떤
// agent 로 부르든 같은 판정이라, 두 화면을 합치면 관리자가 「이 도구를 누가 부를 수 있나」를
// agent 목록 아래에서 찾아야 해요.
//
// 판정·문구는 `lib/toolAccess.ts` 에 있어요. 테스트 러너가 `.tsx` 를 돌리지 못해서, 이 파일은
// 그리기만 해요.
//
// 도구 목록의 출처는 `GET /api/mcp/tool-drift` 예요 — 원장만 읽고(MCP 를 새로 떠오지 않아요)
// 자산별 도구 목록을 그대로 갖고 있어요. 전용 「전체 도구」 엔드포인트를 새로 만들지 않은
// 이유예요. `record_id` 가 grant 경로의 `asset_id` 이고, `tool_name` 이 `operation_id` 예요.

type AddMode = "group" | "principal";

type Feedback = { tone: "ok" | "warn"; message: string };

const TONE_BADGE: Record<GrantTone, string> = {
  ok: "bg-emerald-100 text-emerald-700",
  revoked: "bg-slate-200 text-slate-700",
  expired: "bg-amber-100 text-amber-800",
  // 해석하지 못한 상태는 초록으로 그리지 않아요 (ADR-0037 §4).
  unknown: "bg-slate-200 text-slate-700",
};

export function ToolAccessClient() {
  const { data, error, isLoading } = useSWR(TOOL_DRIFT_SWR_KEY, listMcpToolDrift);
  const assets = useMemo(
    () => [...(data?.assets ?? [])].sort((a, b) => a.asset_name.localeCompare(b.asset_name, "ko")),
    [data?.assets],
  );
  const [assetId, setAssetId] = useState("");
  const [operationId, setOperationId] = useState("");
  const asset = assets.find((item) => item.record_id === assetId) ?? assets[0];
  const selectedAssetId = asset?.record_id ?? "";
  const tools = useMemo(
    () => [...(asset?.tools ?? [])].sort((a, b) => a.tool_name.localeCompare(b.tool_name, "ko")),
    [asset?.tools],
  );
  const selectedTool = tools.find((tool) => tool.tool_name === operationId);

  return (
    <div className="min-w-0">
      <PageHeading
        title="도구 호출 주체"
        description={
          <span className="inline-flex flex-wrap items-center gap-1.5">
            도구 하나를 부를 수 있는 그룹과 사용자를 관리해요.
            <HelpPopover label="이 화면이 하는 일">
              <p>
                도구를 부르려면 <strong>부르는 사람</strong>에게 그 도구의 권한이 있어야 해요.
                여기서 도구 하나를 고르면 그 도구를 부를 수 있는 그룹·사용자가 다 보이고,
                더하거나 회수할 수 있어요.
              </p>
              <p className="mt-2">
                agent 쪽 승인은 다른 화면이에요 — «도구 인가 승인» 이 「이 agent 가 이 도구를
                부를 수 있나」를 다뤄요. 두 축이 모두 갖춰져야 호출이 돼요.
              </p>
            </HelpPopover>
          </span>
        }
      />

      {isLoading ? (
        <LoadingRows label="도구 목록 불러오는 중" />
      ) : error ? (
        <LoadError message="도구 목록을 불러오지 못했어요." />
      ) : assets.length === 0 ? (
        <EmptyState
          title="등록된 MCP 도구가 없어요."
          description="MCP 자산을 등록하면 그 도구가 여기 나타나요."
        />
      ) : (
        <div className="grid gap-5 lg:grid-cols-[minmax(0,320px)_minmax(0,1fr)]">
          <div className="space-y-3 rounded-lg border border-border p-3">
            <label htmlFor="tool-access-asset" className="block text-[12px] font-medium">
              MCP 자산
            </label>
            <Select
              id="tool-access-asset"
              className="w-full"
              value={selectedAssetId}
              onChange={(event) => {
                setAssetId(event.target.value);
                setOperationId("");
              }}
            >
              {assets.map((item) => (
                <option key={item.record_id} value={item.record_id}>
                  {item.asset_name}
                </option>
              ))}
            </Select>

            <div className="text-[12px] font-medium">도구</div>
            {tools.length === 0 ? (
              <p className="text-[11.5px] text-muted-foreground">
                이 자산에 원장이 아는 도구가 없어요.
              </p>
            ) : (
              <div className="max-h-[420px] divide-y divide-border overflow-auto rounded-md border border-border">
                {tools.map((tool) => (
                  <button
                    key={tool.tool_name}
                    type="button"
                    onClick={() => setOperationId(tool.tool_name)}
                    className={cn(
                      "flex w-full items-center justify-between gap-2 px-3 py-2 text-left hover:bg-accent",
                      tool.tool_name === operationId && "bg-accent",
                    )}
                  >
                    <span className="min-w-0 truncate font-mono text-[12px]">
                      {tool.tool_name}
                    </span>
                    {tool.state === "MISSING" && (
                      <Badge variant="type" className="shrink-0 bg-amber-100 text-amber-800">
                        상류에 없어요
                      </Badge>
                    )}
                  </button>
                ))}
              </div>
            )}
          </div>

          <div className="min-w-0">
            {!selectedTool ? (
              <EmptyState
                title="도구를 골라 주세요."
                description="왼쪽에서 도구를 고르면 그 도구를 부를 수 있는 그룹·사용자가 보여요."
              />
            ) : (
              <ToolGrantPanel
                assetId={selectedAssetId}
                assetName={asset?.asset_name ?? ""}
                operationId={selectedTool.tool_name}
                targetMode={asset?.target_mode}
              />
            )}
          </div>
        </div>
      )}
    </div>
  );
}

function ToolGrantPanel({
  assetId,
  assetName,
  operationId,
  targetMode,
}: {
  assetId: string;
  assetName: string;
  operationId: string;
  targetMode?: AssetToolDrift["target_mode"];
}) {
  const tool = toolLabel(assetName, operationId);
  const cacheKey = `admin/tools/${assetId}/${operationId}/grants`;
  const { data, error, isLoading, mutate } = useSWR(cacheKey, () =>
    listToolGrants(assetId, operationId),
  );
  const grants = useMemo(() => sortToolGrants(data?.grants ?? []), [data?.grants]);
  const grantListNotice = toolGrantListNoticeState(
    grants,
    data?.grantsObservation ?? "unknown",
  );

  const [mode, setMode] = useState<AddMode>("group");
  const [group, setGroup] = useState<ToolGrantGroup>("admin");
  const [principalId, setPrincipalId] = useState("");
  const [principalLabel, setPrincipalLabel] = useState("선택하지 않았어요");
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState("");
  const [feedback, setFeedback] = useState<Feedback>();
  const { confirm, dialog } = useConfirm();

  // 그룹 멤버 수는 폭발 반경 문구에 들어가요. 못 읽으면 0 으로 두고 문구가 「지금 없음」으로
  // 말해요 — 숫자를 못 읽었다고 부여를 막지는 않아요.
  const members = useSWR(
    mode === "group" ? `admin/identity/users?group=${group}` : null,
    () => listCognitoUsers({ group }),
  );

  async function run(work: () => Promise<Feedback>) {
    setBusy(true);
    setFailure("");
    setFeedback(undefined);
    try {
      setFeedback(await work());
      await mutate();
    } catch (caught) {
      setFailure(errorMessage(caught, "요청을 처리하지 못했어요."));
    } finally {
      setBusy(false);
    }
  }

  function add() {
    if (mode === "principal" && !principalId) return;
    return run(async () => {
      const subjectId = mode === "group" ? group : principalId;
      // 본문은 snake_case 예요 — 서버 모델(`ToolGrantUpsert`)이 그 이름을 요구해요.
      const result = await putToolGrant(assetId, operationId, {
        subject_kind: mode,
        subject_id: subjectId,
        expires_at: null,
      });
      const who =
        mode === "group"
          ? `«${toolGrantGroupLabel(group)}» 그룹`
          : `${principalLabel} 님`;
      const how =
        result.outcome === "reactivated"
          ? "회수됐던 권한을 되살렸어요"
          : "부를 수 있게 했어요";
      return { tone: "ok", message: `${who}이 «${tool}» 을 ${how}.` };
    });
  }

  async function revoke(grant: ToolGrant) {
    const accepted = await confirm({
      title: `${REVOKE_LABEL}할까요?`,
      description: revokeConfirmMessage(grant, tool),
      confirmLabel: REVOKE_LABEL,
      variant: "destructive",
    });
    if (!accepted) return;
    return run(async () => {
      const result = await revokeToolGrant(
        assetId,
        operationId,
        grant.subjectKind,
        grant.subjectId,
      );
      return {
        tone: "ok",
        // 「지웠어요」로 말하면 안 돼요 — 행은 회수 이력으로 남아요.
        message:
          result.outcome === "already_revoked"
            ? `${subjectLabel(grant)} 의 «${tool}» 권한은 이미 회수돼 있었어요.`
            : `${subjectLabel(grant)} 의 «${tool}» 권한을 회수했어요. 행은 회수 이력으로 남아 있어요.`,
      };
    });
  }

  return (
    <div className="space-y-4">
      {dialog}

      <div className="rounded-lg border border-border px-3 py-2.5">
        <div className="font-mono text-[13px] font-semibold">{operationId}</div>
        <div className="text-[11.5px] text-muted-foreground">
          {assetName || assetId}
          {targetMode ? ` · ${targetMode === "deployed" ? "배포형" : "연결형"}` : ""}
        </div>
      </div>

      {data && !data.directoryObserved && (
        <div
          role="alert"
          className="rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-[12px] text-amber-900"
        >
          사용자 목록을 읽지 못해 이름·이메일이 비어 있어요. 「사람이 아님」과 구분할 수 없는
          상태라, 회수 전에 주체를 한 번 더 확인해 주세요.
        </div>
      )}

      {isLoading ? (
        <LoadingRows label="호출 주체 불러오는 중" />
      ) : error ? (
        <LoadError message="이 도구의 호출 주체를 불러오지 못했어요." />
      ) : (
        <div className="space-y-2">
          {grantListNotice === "unknown" ? (
            <p
              role="alert"
              className="rounded-md border border-slate-300 bg-slate-50 px-3 py-2 text-[12px] text-slate-800"
            >
              {GRANT_LIST_UNKNOWN_MESSAGE}
            </p>
          ) : grantListNotice === "nobody" ? (
            <p className="rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-[12px] text-amber-900">
              {NOBODY_CAN_CALL_MESSAGE}
            </p>
          ) : null}

          {grants.length > 0 && (
            <div className="overflow-x-auto rounded-lg border border-border">
              <table className="w-full min-w-[640px] border-collapse text-left">
                <thead>
                  <tr className="border-b border-border bg-muted/40 text-[11.5px] text-muted-foreground">
                    <th scope="col" className="px-3 py-2 font-semibold">
                      주체
                    </th>
                    <th scope="col" className="px-3 py-2 font-semibold">
                      상태
                    </th>
                    <th scope="col" className="px-3 py-2 font-semibold">
                      부여자
                    </th>
                    <th scope="col" className="px-3 py-2 font-semibold">
                      갱신일
                    </th>
                    <th scope="col" className="px-3 py-2" />
                  </tr>
                </thead>
                <tbody>
                  {grants.map((grant) => {
                    const status = grantStatusPresentation(grant.status);
                    return (
                      <tr
                        key={grant.grantId || `${grant.subjectKind}:${grant.subjectId}`}
                        className="border-b border-border align-top"
                      >
                        <td className="px-3 py-2.5 text-[12.5px]">
                          <div className="font-medium">{subjectLabel(grant)}</div>
                          <div className="text-[11px] text-muted-foreground">
                            {grant.subjectKind === "group" ? "그룹" : "사용자"}
                            {grant.subjectKind === "principal" && grant.subjectEmail
                              ? ` · ${grant.subjectEmail}`
                              : ""}
                          </div>
                        </td>
                        <td className="px-3 py-2.5">
                          <Badge variant="type" className={TONE_BADGE[status.tone]}>
                            {status.label}
                          </Badge>
                          {status.note && (
                            <div className="mt-1 max-w-[220px] text-[11px] text-muted-foreground">
                              {status.note}
                            </div>
                          )}
                        </td>
                        <td className="px-3 py-2.5 text-[11.5px] text-muted-foreground">
                          {grant.grantedBy || "—"}
                        </td>
                        <td className="whitespace-nowrap px-3 py-2.5 text-[11.5px] text-muted-foreground">
                          {grant.updatedAt ? formatKst(grant.updatedAt) : "—"}
                        </td>
                        <td className="whitespace-nowrap px-3 py-2.5 text-right">
                          {status.tone === "revoked" ? (
                            <span className="text-[11px] text-muted-foreground">
                              회수됨
                            </span>
                          ) : (
                            <Button
                              size="sm"
                              variant="outline"
                              disabled={busy}
                              onClick={() => revoke(grant)}
                            >
                              {REVOKE_LABEL}
                            </Button>
                          )}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}

      <div className="space-y-3 rounded-lg border border-[#bfdbfe] bg-white p-3">
        <div className="flex flex-wrap items-center gap-2 text-[12.5px] font-semibold">
          호출 주체 추가
          <HelpPopover label="추가 설명">
            같은 주체를 다시 더해도 행이 늘지 않아요(멱등). 회수됐던 주체를 다시 더하면 다시
            부를 수 있게 돼요.
          </HelpPopover>
        </div>

        <div className="inline-flex overflow-hidden rounded-lg border border-input">
          {(["group", "principal"] as const).map((value) => (
            <button
              key={value}
              type="button"
              onClick={() => setMode(value)}
              className={cn(
                "px-3.5 py-1.5 text-[12.5px] font-semibold",
                mode === value ? "bg-foreground text-background" : "bg-card text-muted-foreground",
              )}
            >
              {value === "group" ? "그룹에게" : "사용자에게"}
            </button>
          ))}
        </div>

        <div className="grid gap-4 lg:grid-cols-2">
          <div>
            {mode === "group" ? (
              <div>
                <div className="mb-1.5 flex items-center gap-1 text-[12px] font-medium">
                  그룹
                  <HelpPopover label="선택 가능한 그룹 설명">
                    Agora 는 <code>user</code>·<code>admin</code> 두 그룹만 인가에 써요. 다른
                    Cognito 그룹은 호출 시점 판정에 실리지 않아서, 부여해도 권한이 동작하지
                    않아요.
                  </HelpPopover>
                </div>
                <div className="flex flex-wrap gap-2">
                  {TOOL_GRANT_GROUPS.map((item) => (
                    <button
                      key={item.id}
                      type="button"
                      onClick={() => {
                        if (isToolGrantGroup(item.id)) setGroup(item.id);
                      }}
                      className={cn(
                        "rounded-full border px-3 py-1 text-[12px] font-semibold",
                        group === item.id
                          ? "border-primary text-primary"
                          : "border-border text-muted-foreground",
                      )}
                    >
                      {item.id}
                      <span className="ml-1 font-normal">{item.label}</span>
                    </button>
                  ))}
                </div>
                <p className="mt-2 text-[11.5px] text-muted-foreground">
                  그룹 권한은 <strong>한 행</strong>이에요. 사람마다 만들지 않아요.
                </p>
              </div>
            ) : (
              <div className="space-y-2">
                <PrincipalPicker
                  rowKey={`${assetId}:${operationId}`}
                  label={principalLabel}
                  mustPick={!principalId}
                  isDefault={false}
                  noDefaultHint=""
                  emptyHint="email 로 검색해 권한을 줄 사용자를 골라 주세요."
                  onPick={(user) => {
                    setPrincipalId(user.sub);
                    setPrincipalLabel(user.email || user.sub);
                  }}
                />
                <p className="text-[11.5px] text-muted-foreground">{PRINCIPAL_GRANT_NOTE}</p>
              </div>
            )}
          </div>
          <div>
            <div className="mb-1.5 flex items-center gap-1 text-[12px] font-medium">
              이 권한이 닿는 범위
              <HelpPopover label="폭발 반경 설명">
                grant 하나가 정확히 <strong>도구 하나</strong>를 열어요 — 키가{" "}
                <code>(주체, asset_id, operation_id)</code> 예요. 남는 반경은{" "}
                <strong>사람 축</strong>이에요: 그룹에 주면 지금 멤버와 앞으로 들어오는 사람까지
                부를 수 있어요.
              </HelpPopover>
            </div>
            <p
              className={cn(
                "rounded-md border px-3 py-2 text-[11.5px]",
                mode === "group"
                  ? "border-red-300 bg-red-50 text-red-900"
                  : "border-amber-300 bg-amber-50 text-amber-900",
              )}
            >
              {toolGrantBlastRadius(
                tool,
                mode === "group"
                  ? {
                      kind: "group",
                      groupLabel: toolGrantGroupLabel(group),
                      memberCount: members.data?.items.length ?? 0,
                    }
                  : { kind: "principal" },
              )}
            </p>
          </div>
        </div>

        <div className="flex flex-wrap items-center gap-2 border-t border-[#bfdbfe] pt-3">
          <Button
            size="sm"
            onClick={add}
            disabled={busy || (mode === "principal" && !principalId)}
            title={
              mode === "principal" && !principalId
                ? "권한을 받을 사용자를 먼저 골라 주세요."
                : undefined
            }
          >
            {busy ? "처리 중…" : "호출 권한 열기"}
          </Button>
          {failure && <InlineError message={failure} />}
          <Button size="sm" variant="outline" className="ml-auto" onClick={() => mutate()}>
            <Icon name="refresh" size={14} />
            다시 조회
          </Button>
        </div>
      </div>

      {feedback && (
        <div
          role={feedback.tone === "ok" ? "status" : "alert"}
          className={cn(
            "rounded-md border px-3 py-2 text-[12.5px]",
            feedback.tone === "ok"
              ? "border-emerald-300 bg-emerald-50 text-emerald-900"
              : "border-amber-300 bg-amber-50 text-amber-900",
          )}
        >
          {feedback.message}
        </div>
      )}
    </div>
  );
}
