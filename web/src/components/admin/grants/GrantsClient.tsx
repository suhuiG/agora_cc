"use client";

import { useMemo, useState } from "react";
import useSWR from "swr";
import {
  ALL_USERS_SWR_KEY,
  deleteAccessGrant,
  listAccessConnections,
  listAccessGrants,
  listAllCognitoUsers,
  type AccessConnection,
  type AccessGrant,
  type CognitoUser,
} from "@/lib/api";
import {
  GRANTS_CREATE_DISABLED_LABEL,
  GRANTS_CREATE_GONE_NOTICE,
  GRANTS_FILTERED_EMPTY_HINT,
  GRANTS_PAGE_DESCRIPTION,
  GRANTS_PAGE_TITLE,
  GRANTS_REVOKE_IS_LIVE_NOTICE,
  GRANTS_REVOKE_IS_LIVE_TITLE,
  filterAccessGrants,
  grantRevokeAvailability,
  grantRevokeConfirmMessage,
  grantSubjectPresentation,
  grantTargetPresentation,
  ASSET_NAMES_NOT_REQUESTED,
  grantUsersBySub,
} from "@/lib/adminGrants";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input, Select } from "@/components/ui/input";
import { useConfirm } from "@/components/ui/confirm-dialog";
import {
  EmptyState,
  InlineError,
  LoadError,
  LoadingRows,
  PageHeading,
  StatusBadge,
  TagList,
  effectiveGrantStatus,
  errorMessage,
  formatEpoch,
} from "@/components/admin/access/shared";

// 사용자 권한 (/admin/grants) — ⑦ grant 를 **필터 없이 전역으로** 조회하고 회수하는 화면이에요.
//
// 문구와 판정은 전부 `@/lib/adminGrants` 의 순수 함수·상수예요. 컴포넌트는 그리기만 해요 —
// 테스트 러너가 `.tsx` 를 못 돌리니까요(`web/package.json` 의 test 목록에 `.tsx` 가 0개).
//
// ⚠️ **부여는 여기서 못 해요.** `POST /api/admin/access-grants` 와 `.../{id}/reissue` 는 둘 다
// `HTTPException(410)` 이에요(ADR-0099 결정 2). 그래서 부여 폼을 지웠고, 대신 **비활성 버튼 +
// 이유** 를 남겨요 — 버튼까지 지우면 「이 화면에서 부여할 수 없다」가 화면에서 안 보여요.
export function GrantsClient() {
  const {
    data: grants,
    error,
    isLoading,
    mutate,
  } = useSWR<AccessGrant[]>("admin/access-grants", listAccessGrants);
  const { data: connections } = useSWR<AccessConnection[]>(
    "admin/access/connections",
    listAccessConnections,
  );
  const { data: users, error: usersError } = useSWR<CognitoUser[]>(
    ALL_USERS_SWR_KEY,
    listAllCognitoUsers,
  );
  const [actionError, setActionError] = useState("");
  const [revoking, setRevoking] = useState("");
  const [query, setQuery] = useState("");
  const [groupFilter, setGroupFilter] = useState("");
  const { confirm, dialog } = useConfirm();

  const connectionNames = useMemo(
    () => new Map(connections?.map((item) => [item.connection_id, item.name]) ?? []),
    [connections],
  );
  const usersBySub = useMemo(() => grantUsersBySub(users), [users]);

  const visible = useMemo(
    () =>
      filterAccessGrants(grants, {
        query,
        groupFilter,
        connectionNames,
        usersBySub,
      }),
    [grants, groupFilter, query, connectionNames, usersBySub],
  );

  async function revoke(grant: AccessGrant) {
    const subject = grantSubjectPresentation(
      grant,
      usersBySub.get(grant.principal_id),
    );
    // ⚠️ 이 화면은 자산 인벤토리를 «부르지 않아요». 기본값(`observed: false`)을 쓰면 모든 행이
  // 「아직 읽지 못해서」라는 진행 중 문구를 영구히 달아요 — 기다리면 나온다고 읽혀요.
  const target = grantTargetPresentation(grant, ASSET_NAMES_NOT_REQUESTED);
    const accepted = await confirm({
      title: "권한 회수",
      description: grantRevokeConfirmMessage(subject, target),
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
    <div className="min-w-0">
      {dialog}
      <PageHeading title={GRANTS_PAGE_TITLE} description={GRANTS_PAGE_DESCRIPTION} />

      {/* 「회수」가 실제 판정을 바꾼다는 사실을 배너로 올려요.
          근거(직접 확인, 2026-09-06): `access_router.revoke_access_grant` → `put_grant` →
          `store.grant_key_for` 가 만드는 `(PRINCIPAL#|GROUP#, GRANT#<asset>#<operation>)` 은
          `gateway_interceptor._has_tool_grant` → `get_tool_grant` 가 읽는 그 키예요.
          interceptor 는 `status is not GrantStatus.ACTIVE` 를 거부해요. 자세한 근거는
          `@/lib/adminGrants` 머리말에 있어요. */}
      <div
        role="alert"
        className="mb-5 rounded-lg border border-amber-300 bg-amber-50 p-3.5 text-xs text-amber-900"
      >
        <b className="block text-[13px]">{GRANTS_REVOKE_IS_LIVE_TITLE}</b>
        <p className="mt-1 leading-relaxed">{GRANTS_REVOKE_IS_LIVE_NOTICE}</p>
        <p className="mt-1.5 leading-relaxed">{GRANTS_CREATE_GONE_NOTICE}</p>
      </div>

      <div className="mb-5 flex flex-wrap items-start justify-between gap-3">
        <div className="flex min-w-0 flex-wrap items-center gap-2">
          <label htmlFor="grant-search" className="sr-only">
            주체 또는 대상 도구 검색
          </label>
          <Input
            id="grant-search"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="이름 · email · 그룹 · 자산 id · 도구 이름 검색"
            className="w-full min-w-0 sm:w-80"
          />
          <label htmlFor="grant-group-filter" className="sr-only">
            옛 권한 그룹 필터
          </label>
          <Select
            id="grant-group-filter"
            value={groupFilter}
            onChange={(event) => setGroupFilter(event.target.value)}
            className="w-full sm:w-56"
          >
            <option value="">옛 권한 그룹 전체</option>
            {connections?.map((connection) => (
              <option key={connection.connection_id} value={connection.connection_id}>
                {connection.name}
              </option>
            ))}
          </Select>
        </div>
        {/* 비활성 + 이유 — 410 인 경로를 버튼으로 열어 두지 않고, 그렇다고 숨기지도 않아요. */}
        <Button size="sm" disabled title={GRANTS_CREATE_GONE_NOTICE}>
          {GRANTS_CREATE_DISABLED_LABEL}
        </Button>
      </div>

      {actionError && (
        <div className="mb-4">
          <InlineError message={actionError} />
        </div>
      )}
      {usersError && (
        <div className="mb-4">
          <InlineError message="사용자 정보를 불러오지 못해 UID로 표시해요." />
        </div>
      )}

      {isLoading ? (
        <LoadingRows label="사용자 권한 불러오는 중" />
      ) : error ? (
        <LoadError message="사용자 권한을 불러오지 못했어요." />
      ) : grants?.length === 0 ? (
        <EmptyState
          title="grant 행이 없어요."
          description="도구별 부여는 「도구 호출 주체」 화면에서 해요."
        />
      ) : visible.length === 0 ? (
        <EmptyState
          title="조건에 맞는 행이 없어요."
          description={GRANTS_FILTERED_EMPTY_HINT}
        />
      ) : (
        <div className="rounded-lg border border-border">
          {/* 열 제목 — 행과 «같은» grid template 이어야 열이 맞아요. `<table>` 이 아니라
              `<div>` grid 라 colSpan 은 없어요(빈 상태·로딩은 별 컴포넌트예요).
              ⚠️ 3열은 «고정폭» 이어야 해요. `auto` 로 두면 각 행이 독립된 grid container 라서
              그 트랙이 «행마다 다른 내용 폭» 에 맞춰지고, 남은 폭이 1·2열에 다르게 배분돼
              **클래스 문자열이 같아도 계산 결과가 갈라져요.** 2026-09-06 브라우저 실측:
              열 제목 `599.9 654.5 19.6` / 회수 버튼 행 `586.9 640.3 46.8` /
              REVOKED 행 `602.7 657.5 13.8` → 대상 열이 헤더 대비 −13px, 행끼리 16px 어긋났어요.
              옛 주석은 「같은 template 이라 어긋나지 않아요」라고 단정했는데 거짓이었어요 —
              이 계열은 **브라우저에서만** 잡혀요. 폭을 바꿀 때 세 grid 를 함께 보세요. */}
          <div className="hidden gap-3 border-b border-border bg-muted/40 px-4 py-2 text-[11px] font-medium uppercase tracking-wide text-muted-foreground md:grid md:grid-cols-[minmax(0,1.1fr)_minmax(0,1.2fr)_4rem]">
            <div>주체 (누구의 권한인가)</div>
            <div>대상 (어느 도구인가)</div>
            <div className="md:text-right">회수</div>
          </div>
          <div className="divide-y divide-border">
            {visible.map((grant) => (
              <GrantRow
                key={grant.grant_id}
                grant={grant}
                user={usersBySub.get(grant.principal_id)}
                connectionName={
                  connectionNames.get(grant.connection_id) ?? grant.connection_id
                }
                revoking={revoking === grant.grant_id}
                onRevoke={() => revoke(grant)}
              />
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

function GrantRow({
  grant,
  user,
  connectionName,
  revoking,
  onRevoke,
}: {
  grant: AccessGrant;
  user?: CognitoUser;
  connectionName: string;
  revoking: boolean;
  onRevoke: () => void;
}) {
  const subject = grantSubjectPresentation(grant, user);
  // ⚠️ 이 화면은 자산 인벤토리를 «부르지 않아요». 기본값(`observed: false`)을 쓰면 모든 행이
  // 「아직 읽지 못해서」라는 진행 중 문구를 영구히 달아요 — 기다리면 나온다고 읽혀요.
  const target = grantTargetPresentation(grant, ASSET_NAMES_NOT_REQUESTED);
  const revokable = grantRevokeAvailability(grant);
  const status = effectiveGrantStatus(grant);
  const reasonId = `grant-revoke-reason-${grant.grant_id}`;

  return (
    <div className="grid min-w-0 gap-3 px-4 py-4 md:grid-cols-[minmax(0,1.1fr)_minmax(0,1.2fr)_4rem]">
      <div className="min-w-0">
        <div className="flex flex-wrap items-center gap-2">
          <span className="break-words text-sm font-semibold" title={subject.title}>
            {subject.label}
          </span>
          <StatusBadge status={status} />
        </div>
        {subject.detail && (
          <p className="mt-1 break-all text-xs text-muted-foreground">
            {subject.detail}
          </p>
        )}
      </div>
      <div className="min-w-0">
        <div className="flex flex-wrap items-center gap-2">
          {target.kind === "legacy" ? (
            <Badge variant="outline" className="max-w-full break-all text-amber-700">
              {target.label}
            </Badge>
          ) : (
            <span className="break-all font-mono text-xs" title={target.title}>
              {target.label}
            </span>
          )}
        </div>
        {target.note && (
          <p className="mt-1 break-words text-xs text-muted-foreground">
            {target.note}
          </p>
        )}
        {/* 옛 행이 실제로 들고 있는 값 — 감사용으로 남겨요. 현행 행은 이 둘이 비어 있어요. */}
        {target.kind === "legacy" && (
          <div className="mt-1.5">
            {connectionName && (
              <p className="break-all text-xs text-muted-foreground">
                옛 권한 그룹 {connectionName}
              </p>
            )}
            {grant.capabilities.length > 0 && (
              <div className="mt-1">
                <TagList items={grant.capabilities} />
              </div>
            )}
          </div>
        )}
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
