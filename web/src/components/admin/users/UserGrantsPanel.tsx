"use client";

import { useMemo, useState } from "react";
import useSWR from "swr";

import {
  ALL_USERS_SWR_KEY,
  deleteAccessGrant,
  getGovInventory,
  listAccessGrants,
  listAllCognitoUsers,
  type AccessGrant,
  type CognitoUser,
  type Inventory,
} from "@/lib/api";
import {
  ALL_GRANTS_SWR_KEY,
  GOV_INVENTORY_SWR_KEY,
  USER_GRANTS_EMPTY_DESCRIPTION,
  USER_GRANTS_EMPTY_TITLE,
  USER_GRANTS_GROUP_AXIS_LABEL,
  USER_GRANTS_GROUP_SCOPE_NOTE,
  USER_GRANTS_LOAD_ERROR,
  USER_GRANTS_ORPHAN_GROUP_LABEL,
  USER_GRANTS_PANEL_TITLE,
  USER_GRANTS_PERSON_AXIS_LABEL,
  USER_GRANTS_UNATTACHED_GROUP_AXIS_UNKNOWN,
  USER_GRANTS_UNATTACHED_NONE,
  USER_GRANTS_UNATTACHED_NOTICE,
  USER_GRANTS_UNATTACHED_TITLE,
  assetNameIndex,
  grantReachability,
  grantRevokeAvailability,
  userPanelRevokeConfirmMessage,
  grantSubjectPresentation,
  grantTargetPresentation,
  userGrantAxes,
} from "@/lib/adminGrants";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { useConfirm } from "@/components/ui/confirm-dialog";
import {
  EmptyState,
  InlineError,
  LoadError,
  LoadingRows,
  StatusBadge,
  effectiveGrantStatus,
  errorMessage,
  formatEpoch,
} from "@/components/admin/access/shared";

// 「사용자 관리」에서 사용자를 고르면 하단에 나오는 ⑦ grant 패널이에요 (제품 오너 결정,
// 2026-09-06). 옛 「사용자 권한」(`/admin/grants`) 화면의 **조회·회수** 기능을 여기로 흡수해요.
//
// 판정·문구는 전부 `@/lib/adminGrants` 의 순수 함수·상수예요 — 테스트 러너가 `.tsx` 를
// 실행하지 못하니까요(`web/package.json` 의 test 목록에 `.tsx` 가 0개). 여기는 그리기만 해요.
//
// ── 왜 전역 목록을 받아 클라이언트에서 갈라요 ─────────────────────────────────────
//
// `listAccessGrants({ principalId })` 는 서버에서 `PRINCIPAL#<sub>` 파티션만 Query 해요
// (`access_router.list_access_grants` 가 `subject_groups` 를 안 넘겨요). 그런데 ⑦ 판정은
// «사람 키 + 그룹 키의 합집합» 이라(`gateway_interceptor._has_tool_grant`) 사람 축만 보여주면
// 화면이 권한을 **과소** 표시해요.
//
// 그건 이론이 아니에요 — 2026-09-06 읽기 전용 실측(`agora-identity-dev` 전량 scan): `GRANT#`
// 34행 중 **32행이 그룹 행**이고 사람 행은 2행이에요. 사람 축만 조회하면 거의 모든 사용자에게
// 「권한 없음」으로 보여요. 그래서 전역 목록(필터 없음)을 한 번 받아 축을 갈라요.
//
// ⚠️ SWR 키는 「사용자 권한」 화면과 **같은 문자열**이에요(`ALL_GRANTS_SWR_KEY`) — 화면을
// 옮겨도 같은 응답을 다시 받지 않아요.
export function UserGrantsPanel({ user }: { user: CognitoUser }) {
  const {
    data: grants,
    error,
    isLoading,
    mutate,
  } = useSWR<AccessGrant[]>(ALL_GRANTS_SWR_KEY, () => listAccessGrants());
  // 자산 이름 색인. 읽기 전용 경로예요 — `GET /api/mcp/tool-drift` 를 «일부러» 쓰지 않아요
  // (그 읽기 경로가 드리프트 원장에 써요, `adminGrants.ts` 머리말 참고).
  const { data: inventory } = useSWR<Inventory>(
    GOV_INVENTORY_SWR_KEY,
    getGovInventory,
  );
  // 도달성 판정용 — 「어느 사용자에도 붙지 않는 행」을 세려면 전체 사용자의 그룹이 필요해요.
  // 「사용자 권한」 화면이 쓰던 그 SWR 키를 그대로 써요.
  const { data: allUsers } = useSWR<CognitoUser[]>(
    ALL_USERS_SWR_KEY,
    listAllCognitoUsers,
  );
  const [actionError, setActionError] = useState("");
  const [revoking, setRevoking] = useState("");
  const { confirm, dialog } = useConfirm();

  const assets = useMemo(() => assetNameIndex(inventory), [inventory]);
  const axes = useMemo(
    () => userGrantAxes(grants, { sub: user.sub, groups: user.groups }),
    [grants, user.sub, user.groups],
  );
  // 도달성 판정에는 «전체» 사용자의 그룹이 필요해요 — 회원이 0명인 그룹의 행은 어느 사용자를
  // 골라도 안 나오니까요. 목록을 못 읽으면 그 축을 「없음」이 아니라 «미확인» 으로 둬요
  // (`knownGroups` 를 `undefined` 로 넘기면 `groupAxisObserved: false` 예요).
  const reachability = useMemo(
    () =>
      grantReachability(
        grants,
        allUsers
          ? allUsers.flatMap((item) => item.groups ?? [])
          : undefined,
      ),
    [grants, allUsers],
  );

  async function revoke(grant: AccessGrant) {
    const subject = grantSubjectPresentation(
      grant,
      grant.principal_id === user.sub ? user : undefined,
    );
    const target = grantTargetPresentation(grant, assets);
    // 그룹 행이면 폭발 반경이 한 사람이 아니에요 — 회원 «전체» 예요. 인원을 셀 수 있으면 세고,
    // 디렉터리를 아직 못 읽었으면 `null` 을 넘겨서 «확인하지 못했다» 로 말해요(0명으로 접지 않아요).
    const memberCount =
      subject.kind === "group" && allUsers
        ? allUsers.filter((item) => (item.groups ?? []).includes(subject.title))
            .length
        : null;
    const accepted = await confirm({
      title: "권한 회수",
      description: userPanelRevokeConfirmMessage(subject, target, memberCount),
      confirmLabel: "회수",
      variant: "destructive",
    });
    if (!accepted) return;
    setRevoking(grant.grant_id);
    setActionError("");
    try {
      await deleteAccessGrant(grant.grant_id);
      await mutate();
    } catch (caught) {
      setActionError(errorMessage(caught, "권한 회수에 실패했어요."));
    } finally {
      setRevoking("");
    }
  }

  return (
    <section
      className="mt-6 min-w-0 rounded-lg border border-border p-4"
      aria-label={USER_GRANTS_PANEL_TITLE}
    >
      {dialog}
      {/* 2026-09-06: 제목에서 사용자 이름 suffix 를 뺐고 설명 문단을 없앴어요(제품 오너 요청).
          어느 사용자의 권한인지는 위 사용자 표에서 그 행이 선택돼 있어 이미 보여요.
          ⚠️ 설명이 담고 있던 「최대 900초 지연」 사실은 버리지 않고 **그룹 회수 확인 문구로**
          옮겼어요 — 회수를 누른 관리자가 즉시 막힌다고 믿으면 그 창 동안 호출이 통과해요. */}
      <div className="mb-4">
        <h2 className="text-base font-semibold">{USER_GRANTS_PANEL_TITLE}</h2>
      </div>

      {actionError && (
        <div className="mb-4">
          <InlineError message={actionError} />
        </div>
      )}

      {isLoading ? (
        <LoadingRows label="사용자 권한 불러오는 중" />
      ) : error ? (
        <LoadError message={USER_GRANTS_LOAD_ERROR} />
      ) : axes.union.length === 0 ? (
        <EmptyState
          title={USER_GRANTS_EMPTY_TITLE}
          description={USER_GRANTS_EMPTY_DESCRIPTION}
        />
      ) : (
        <div className="space-y-5">
          <Axis
            label={USER_GRANTS_PERSON_AXIS_LABEL}
            scope=""
            grants={axes.person}
            assets={assets}
            revoking={revoking}
            onRevoke={revoke}
          />
          {axes.groups.map((axis) => (
            <Axis
              key={axis.group}
              label={`${USER_GRANTS_GROUP_AXIS_LABEL} · ${axis.group}`}
              scope={USER_GRANTS_GROUP_SCOPE_NOTE}
              grants={axis.grants}
              assets={assets}
              revoking={revoking}
              onRevoke={revoke}
            />
          ))}
        </div>
      )}

      {/* ⚠️ 옛 `/admin/grants` 는 필터 없는 전역 목록이었어요. 사용자를 골라야 보이는 화면으로
          옮기면 주체가 비어 있는 행은 어느 사용자를 골라도 안 나와요 — 「없는 셈」 치지 않고
          여기서 세어 밝혀요. */}
      <div className="mt-6 rounded-lg border border-dashed border-border bg-muted/30 p-3.5 text-xs text-muted-foreground">
        <b className="block text-[13px] text-foreground">
          {USER_GRANTS_UNATTACHED_TITLE}
        </b>
        <p className="mt-1 break-keep leading-relaxed">
          {USER_GRANTS_UNATTACHED_NOTICE}
        </p>
        {reachability.unrecorded.length > 0 && (
          <ul className="mt-1.5 space-y-1">
            {reachability.unrecorded.map((grant) => (
              <li key={grant.grant_id} className="break-all font-mono">
                주체 미기록 · {grantTargetPresentation(grant, assets).label}
              </li>
            ))}
          </ul>
        )}
        {reachability.orphanGroups.length > 0 && (
          <div className="mt-1.5">
            <span className="block">{USER_GRANTS_ORPHAN_GROUP_LABEL}</span>
            <ul className="mt-1 space-y-1">
              {reachability.orphanGroups.flatMap((axis) =>
                axis.grants.map((grant) => (
                  <li key={grant.grant_id} className="break-all font-mono">
                    {axis.group} · {grantTargetPresentation(grant, assets).label}
                  </li>
                )),
              )}
            </ul>
          </div>
        )}
        {/* ⚠️ 「없어요」와 「확인 못 했어요」를 갈라요 — 전체 사용자 목록을 못 읽었으면 회원이
            0명인 그룹의 행을 «본 적이 없어요». 그걸 「모든 행이 도달 가능」으로 적으면 미관측을
            통과로 기록하는 거예요. */}
        {reachability.unrecorded.length === 0 &&
          reachability.orphanGroups.length === 0 && (
            <p className="mt-1.5 break-keep leading-relaxed">
              {reachability.groupAxisObserved
                ? USER_GRANTS_UNATTACHED_NONE
                : USER_GRANTS_UNATTACHED_GROUP_AXIS_UNKNOWN}
            </p>
          )}
      </div>
    </section>
  );
}

/**
 * 표 열 template. 열 제목과 «모든» 행이 이 하나를 공유해야 해요 — 3열 시절 `auto` 트랙 때문에
 * 행마다 계산이 갈려 열이 어긋난 결함이 있었어요(2026-09-06). 마지막 두 칸은 고정폭이에요.
 */
const GRANT_ROW_GRID =
  "md:grid-cols-[minmax(0,1fr)_minmax(0,1fr)_9rem_5rem]";

function Axis({
  label,
  scope,
  grants,
  assets,
  revoking,
  onRevoke,
}: {
  label: string;
  /** 축 전체에 걸리는 사실(예: 그룹은 회원 전체에게 적용). 없으면 빈 문자열이에요. */
  scope: string;
  grants: AccessGrant[];
  assets: ReturnType<typeof assetNameIndex>;
  revoking: string;
  onRevoke: (grant: AccessGrant) => void;
}) {
  return (
    <div className="min-w-0">
      <div className="mb-2 text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
        {label} · {grants.length}건
        {/* 「그룹 단위 — 회원 전체에게 적용」을 행마다 반복하던 것을 여기 한 번으로 모았어요.
            주체 열이 없어져서 그 사실이 사라지면 안 돼요. */}
        {scope && <span className="ml-2 normal-case">{scope}</span>}
      </div>
      {grants.length === 0 ? (
        <p className="rounded-md border border-dashed border-border px-3 py-3 text-xs text-muted-foreground">
          이 축에는 부여된 행이 없어요.
        </p>
      ) : (
        <div className="rounded-lg border border-border">
          {/* 4열: MCP · Tool · 상태 · 회수 (2026-09-06 요청). 「주체」 열은 뺐어요 — 축 제목이
              `User 권한`/`Group · <이름>` 으로 갈려서 누구의 권한인지 그 줄이 말해요.
              ⚠️ 행과 «같은» template 을 써요. 3열 시절 `auto` 트랙이 행마다 갈려서 열이
              어긋난 결함이 있었어요(2026-09-06) — 마지막 두 칸을 고정폭으로 둬요. */}
          <div className={`hidden gap-3 border-b border-border bg-muted/40 px-4 py-2 text-[11px] font-medium uppercase tracking-wide text-muted-foreground md:grid ${GRANT_ROW_GRID}`}>
            <div>MCP</div>
            <div>Tool</div>
            <div>상태</div>
            <div className="md:text-right">회수</div>
          </div>
          <div className="divide-y divide-border">
            {grants.map((grant) => (
              <Row
                key={grant.grant_id}
                grant={grant}
                assets={assets}
                revoking={revoking === grant.grant_id}
                onRevoke={() => onRevoke(grant)}
              />
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

// 주체(사람/그룹)는 이 행이 «그리지» 않아요 — 축 제목이 말해요. 다만 «판정» 에는 여전히
// 필요해서 부모의 `revoke()` 가 계산해요(그룹이면 폭발 반경이 회원 전체라 확인 문구가 달라요).
function Row({
  grant,
  assets,
  revoking,
  onRevoke,
}: {
  grant: AccessGrant;
  assets: ReturnType<typeof assetNameIndex>;
  revoking: boolean;
  onRevoke: () => void;
}) {
  const target = grantTargetPresentation(grant, assets);
  const revokable = grantRevokeAvailability(grant);
  const status = effectiveGrantStatus(grant);
  const reasonId = `user-grant-revoke-reason-${grant.grant_id}`;
  return (
    <div className={`grid min-w-0 gap-3 px-4 py-4 ${GRANT_ROW_GRID}`}>
      {/* MCP */}
      <div className="min-w-0">
        <div className="flex flex-wrap items-center gap-2">
          {target.kind === "legacy" ? (
            <Badge variant="outline" className="max-w-full break-all text-amber-700">
              {target.mcpLabel}
            </Badge>
          ) : (
            <span className="break-all text-sm font-semibold" title={target.title}>
              {target.mcpLabel}
            </span>
          )}
          {/* 이름을 «못 찾은» 경우를 id 로 조용히 떨어뜨리지 않아요 — 배지로 갈라요. */}
          {target.nameStatus === "unnamed" && (
            <Badge variant="outline" className="text-amber-700">
              이름 확인 못 함
            </Badge>
          )}
        </div>
        {target.note && (
          <p className="mt-1 break-all text-xs text-muted-foreground">
            {target.note}
          </p>
        )}
      </div>
      {/* Tool */}
      <div className="min-w-0">
        {target.toolLabel ? (
          <span className="break-all font-mono text-xs">{target.toolLabel}</span>
        ) : (
          // 옛 행은 도구 자체가 없어요 — 빈 칸으로 두면 「아직 안 나왔다」로 읽혀요.
          <span className="text-xs text-muted-foreground">없음</span>
        )}
      </div>
      {/* 상태 */}
      <div className="min-w-0">
        <StatusBadge status={status} />
        <p className="mt-1 text-xs text-muted-foreground">
          {grant.expires_at ? `만료 ${formatEpoch(grant.expires_at)}` : "만료 없음"}
        </p>
      </div>
      <div className="flex min-w-0 flex-col items-start gap-1 md:items-end">
        {grant.status === "ACTIVE" ? (
          <>
            <Button
              variant="outline"
              size="sm"
              onClick={onRevoke}
              disabled={revoking || !revokable.enabled}
              aria-describedby={revokable.enabled ? undefined : reasonId}
              className="text-red-600"
            >
              {revoking ? "회수 중…" : "회수"}
            </Button>
            {!revokable.enabled && (
              <p
                id={reasonId}
                className="max-w-[16rem] text-xs leading-relaxed text-muted-foreground md:text-right"
              >
                {revokable.reason}
              </p>
            )}
          </>
        ) : (
          <span className="text-xs text-muted-foreground">v{grant.version}</span>
        )}
      </div>
    </div>
  );
}
