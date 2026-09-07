import type {
  AccessConnection,
  AccessGrant,
  AssetCapabilityPolicy,
} from "./api/identity";

export type CapabilitySetSnapshot = Pick<AccessConnection, "connection_id"> & {
  effectiveCapabilities: readonly string[];
};

export type CapabilitySetDrift = {
  driftedGrants: AccessGrant[];
  driftedPolicies: AssetCapabilityPolicy[];
};

/**
 * 어긋남 조회는 **펼쳤을 때만** 돌아요 (IH-162 ⑤).
 *
 * 이 조회 하나가 `1 + ceil(N/100) + N` 요청이에요 — 카탈로그 첫 페이지, MCP 자산 페이지들,
 * 그리고 자산마다 정책 목록. `/admin/capability-sets` 는 첫 그룹을 자동 선택해서 패널을 펼치지
 * 않아도 mount 만으로 전부 나갔어요. SWR 키가 `null` 이면 fetcher 가 아예 안 돌아요.
 */
export function capabilitySetResyncKey(
  connectionId: string,
  expanded: boolean,
): string | null {
  if (!expanded || !connectionId) return null;
  return `admin/capability-sets/${connectionId}/resync`;
}

/**
 * 어긋남을 «관측했는가» 를 네 상태로 갈라요.
 *
 * ⚠️ `not_requested`(접혀 있어서 아직 안 봄)를 `observed` 와 같은 분기로 그리면 어긋남이 있는
 * 그룹도 「최신 상태」 초록으로 보여요 — 미관측을 통과로 적는 거예요(ADR-0037 §4). SWR 키를
 * `null` 로 주면 `isLoading` 이 `false` 이고 `data` 도 `error` 도 없어서, 기존 렌더 분기는
 * 그대로 두면 정확히 그 거짓을 그려요.
 */
export type CapabilitySetResyncObservation =
  | "not_requested"
  | "observing"
  | "unreadable"
  | "observed";

export function capabilitySetResyncObservation({
  expanded,
  loading,
  failed,
  loaded,
}: {
  expanded: boolean;
  loading: boolean;
  failed: boolean;
  loaded: boolean;
}): CapabilitySetResyncObservation {
  if (!expanded) return "not_requested";
  if (failed) return "unreadable";
  if (loading || !loaded) return "observing";
  return "observed";
}

function isSameCapabilitySet(
  stored: readonly string[],
  current: readonly string[],
): boolean {
  const storedSet = new Set(stored);
  const currentSet = new Set(current);
  if (storedSet.size !== currentSet.size) return false;
  return storedSet.values().every((capability) => currentSet.has(capability));
}

/**
 * Finds assignment snapshots that no longer match a capability set.
 *
 * The caller decides which records are eligible for re-sync (for example, active grants and
 * approved policies). This function only applies the connection and set-equality rules.
 */
export function computeCapabilitySetDrift(
  group: CapabilitySetSnapshot,
  grants: readonly AccessGrant[],
  policies: readonly AssetCapabilityPolicy[],
): CapabilitySetDrift {
  return {
    driftedGrants: grants.filter(
      (grant) =>
        grant.connection_id === group.connection_id &&
        !isSameCapabilitySet(grant.capabilities, group.effectiveCapabilities),
    ),
    driftedPolicies: policies.filter(
      (policy) =>
        policy.connection_id === group.connection_id &&
        !isSameCapabilitySet(
          policy.required_capabilities,
          group.effectiveCapabilities,
        ),
    ),
  };
}
