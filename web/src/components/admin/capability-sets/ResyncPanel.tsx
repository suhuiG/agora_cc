"use client";

import { useMemo, useState } from "react";
import useSWR from "swr";
import {
  getCatalog,
  listAccessGrants,
  listAssetCapabilityPolicies,
  type AccessCapability,
  type AccessConnection,
  type AccessGrant,
  type AssetCapabilityPolicy,
} from "@/lib/api";
import {
  capabilitySetResyncKey,
  capabilitySetResyncObservation,
  computeCapabilitySetDrift,
  type CapabilitySetDrift,
} from "@/lib/capabilitySetDrift";
import { Button } from "@/components/ui/button";
import { Icon } from "@/components/ui/icon";
import {
  InlineError,
  capsOf,
  effectiveGrantStatus,
} from "@/components/admin/access/shared";
import { cn } from "@/lib/ui";

// 재동기화 «액션» 을 지웠어요 (IH-162 ③ · ADR-0112).
//
// 옛 「재동기화」 버튼은 「전부 실패」가 아니라 **부분 성공** 이었어요. grant 루프는 410 라우트
// (`POST /api/admin/access-grants/{id}/reissue`, ADR-0099 결정 2 로 없어진 경로)를 grant 당 한 번
// 씩 때려서 전부 실패했고, 정책 루프는 **살아 있는 ⑤ 쓰기 라우트**
// (`PUT /api/assets/{id}/capabilities/{op}` · `.../reject`)로 죽은 층에 행을 갱신·REJECT 하는 데
// «성공» 했어요. 뒤쪽이 더 나쁜 진실성 결함이라 두 루프를 함께 지웠어요 — 확인 대화상자의
// 「다음 호출부터 적용돼요」 약속과, 없어진 버튼을 가리키던 설명 문구 3곳도 같이요.
//
// 어긋남 «관측» 은 남겨요 — 읽기 전용이고, legacy 행 감사에 쓰여요. 다만 두 가지를 지켜요:
//
// 1. **펼쳤을 때만 조회해요.** 이 조회는 `1 + ceil(N/100) + N` 요청이고, `/admin/capability-sets`
//    는 첫 그룹을 자동 선택해서 패널을 펼치지 않아도 mount 만으로 전부 나갔어요.
// 2. **접힘을 「최신 상태」로 그리지 않아요.** SWR 키가 `null` 이면 `isLoading` 이 `false` 이고
//    `data` 도 `error` 도 없어서, 옛 렌더 분기를 그대로 두면 어긋남이 있는 그룹도 초록
//    「최신 상태」로 보여요 — 미관측을 통과로 적는 거예요(ADR-0037 §4).

type ResyncData = {
  grants: AccessGrant[];
  policies: AssetCapabilityPolicy[];
};

const CATALOG_PAGE_SIZE = 100;
const POLICY_LOAD_CONCURRENCY = 6;

async function listApprovedMcpAssetIds(): Promise<string[]> {
  const ids: string[] = [];
  let offset = 0;
  let total = 0;
  do {
    const page = await getCatalog("MCP", offset, CATALOG_PAGE_SIZE);
    ids.push(...page.items.map((asset) => asset.record_id));
    total = page.total;
    offset += page.items.length;
    if (page.items.length === 0) break;
  } while (offset < total);
  return Array.from(new Set(ids));
}

async function mapWithConcurrency<T, R>(
  items: readonly T[],
  concurrency: number,
  task: (item: T) => Promise<R>,
): Promise<R[]> {
  const results = new Array<R>(items.length);
  let nextIndex = 0;
  async function worker() {
    while (nextIndex < items.length) {
      const index = nextIndex;
      nextIndex += 1;
      results[index] = await task(items[index]);
    }
  }
  await Promise.all(
    Array.from(
      { length: Math.min(concurrency, items.length) },
      () => worker(),
    ),
  );
  return results;
}

async function loadResyncData(connectionId: string): Promise<ResyncData> {
  const grantsPromise = listAccessGrants({ connectionId });
  const policiesPromise = listApprovedMcpAssetIds().then(async (assetIds) => {
    const byAsset = await mapWithConcurrency(
      assetIds,
      POLICY_LOAD_CONCURRENCY,
      listAssetCapabilityPolicies,
    );
    return byAsset.flat();
  });
  const [grants, policies] = await Promise.all([grantsPromise, policiesPromise]);
  return { grants, policies };
}

export function ResyncPanel({
  group,
  capabilities,
}: {
  group: AccessConnection;
  capabilities: AccessCapability[];
}) {
  const [expanded, setExpanded] = useState(false);
  const currentCapabilities = useMemo(
    () => capsOf(capabilities, group.ceiling),
    [capabilities, group.ceiling],
  );
  const {
    data,
    error: loadError,
    isLoading,
    isValidating,
    mutate,
  } = useSWR<ResyncData>(
    capabilitySetResyncKey(group.connection_id, expanded),
    () => loadResyncData(group.connection_id),
  );
  const [actionError, setActionError] = useState("");

  // 회수·재발급 대상 판정이 아니라 «감사 대상» 판정이에요. 이력(회수·만료) 행과 미승인 정책은
  // 그룹과 어긋나는 게 정상이라 관측에서 빼요.
  const eligibleGrants = useMemo(
    () =>
      (data?.grants ?? []).filter(
        (grant) => effectiveGrantStatus(grant) === "ACTIVE",
      ),
    [data?.grants],
  );
  const eligiblePolicies = useMemo(
    () => (data?.policies ?? []).filter((policy) => policy.status === "APPROVED"),
    [data?.policies],
  );
  const drift = useMemo<CapabilitySetDrift>(
    () =>
      computeCapabilitySetDrift(
        {
          connection_id: group.connection_id,
          effectiveCapabilities: currentCapabilities,
        },
        eligibleGrants,
        eligiblePolicies,
      ),
    [
      currentCapabilities,
      eligibleGrants,
      eligiblePolicies,
      group.connection_id,
    ],
  );
  const observation = capabilitySetResyncObservation({
    expanded,
    loading: isLoading,
    failed: Boolean(loadError),
    loaded: data !== undefined,
  });
  const driftCount =
    drift.driftedGrants.length + drift.driftedPolicies.length;

  async function refresh() {
    setActionError("");
    try {
      await mutate();
    } catch (caught) {
      setActionError(
        caught instanceof Error
          ? caught.message
          : "어긋남 상태를 다시 불러오지 못했어요.",
      );
    }
  }

  return (
    <div className="mt-4 border-t border-border pt-4">
      <div className="flex min-w-0 flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <h4 className="text-sm font-semibold">어긋남 조회 (읽기 전용)</h4>
            {observation === "observed" && (
              <span
                className={cn(
                  "inline-flex items-center gap-1 text-xs font-medium",
                  driftCount === 0 ? "text-emerald-700" : "text-amber-800",
                )}
              >
                <Icon name={driftCount === 0 ? "check" : "alert"} size={14} />
                {driftCount === 0 ? "어긋남 없음" : "어긋남 발견"}
              </span>
            )}
          </div>
          <p className="mt-1 text-xs leading-relaxed text-muted-foreground">
            그룹을 바꿔도 기존 부여·자산 정책에 자동 전파되지 않아요. 다만 이 층은 도구 인가
            판정에서 빠졌으니(ADR-0099) 어긋남이 도구를 막지는 않아요 — legacy 행 감사용
            조회예요. 일괄 갱신 액션은 없앴어요 (ADR-0112).
          </p>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={() => setExpanded((value) => !value)}
            aria-expanded={expanded}
          >
            <Icon name={expanded ? "close" : "search"} size={14} />
            {expanded ? "접기" : "어긋남 조회"}
          </Button>
          {expanded && (
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={refresh}
              disabled={isLoading || isValidating}
            >
              <Icon name="refresh" size={14} />
              {isValidating ? "확인 중…" : "새로고침"}
            </Button>
          )}
        </div>
      </div>

      {observation === "not_requested" ? (
        <p className="mt-3 text-sm text-muted-foreground">
          아직 조회하지 않았어요. 이 조회는 카탈로그 전체와 자산마다 정책 목록을 읽어서 요청이
          많아요 — 필요할 때만 눌러 주세요.
        </p>
      ) : observation === "observing" ? (
        <div
          role="status"
          className="mt-3 h-10 animate-pulse rounded-lg bg-muted/60"
          aria-label="어긋남 조회 중"
        />
      ) : observation === "unreadable" ? (
        <div className="mt-3">
          <InlineError message="어긋남 상태를 불러오지 못했어요. 다시 시도해 주세요." />
        </div>
      ) : (
        <div className="mt-3 min-w-0">
          <p
            className={cn(
              "text-sm font-medium",
              driftCount === 0 ? "text-emerald-700" : "text-amber-900",
            )}
          >
            {driftCount === 0
              ? "기존 부여와 정책이 현재 그룹과 같아요."
              : `${drift.driftedGrants.length}개 부여 / ${drift.driftedPolicies.length}개 정책이 그룹과 어긋나 있어요.`}
          </p>
        </div>
      )}

      {actionError && (
        <div className="mt-2">
          <InlineError message={actionError} />
        </div>
      )}
    </div>
  );
}
