// Agent × Tool 매트릭스의 데이터 조합 — 신규 API 없이 기존 3개를 엮어요
// (docs/design/admin-console-spec.md §4.3).
//
//   ① Agent 목록       GET /api/playground/agents (APPROVED + runtimeArn 보유)
//   ② 배선된 MCP 자산   Agent descriptor 의 agent.agoraDependencies.mcpAssets
//                      — 판정 1번(ASSET_NOT_DELEGATED)이 보는 것과 같은 자리예요
//   ③ 자산별 tool 목록  MCP 자산 descriptor 의 mcp.tools.inlineContent
//   ④ tool 허용 상태    GET /api/assets/{id}/capabilities — ② 목록만큼 N회
//
// 순수 함수만 둬요 — 화면이 SWR 로 불러온 결과를 넘겨 조합해요.

import type { AssetCapabilityPolicy, AssetDetail } from "./api";

/** Agent descriptor 에 배포 시 고정된 MCP 자산 1건. */
export type WiredMcpAsset = {
  assetId: string;
  /** 배포 시 Gateway target 이름으로 쓰인 표시명. */
  name: string;
  endpoint: string;
};

/** 매트릭스 한 행 — tool 하나. */
export type MatrixTool = {
  operationId: string;
  description: string;
  /** 저장된 정책. 없으면 아직 미설정(판정 3번 ASSET_CAPABILITY_NOT_FOUND). */
  policy: AssetCapabilityPolicy | null;
  /**
   * 판정이 실제로 ALLOW 로 갈 수 있는 상태인지.
   *
   * 정책 자체(3·5번)와 버전(4번)만으로는 부족해요 — 정책이 요구하는 capability 가
   * 지금도 ACTIVE 이고(7번) 상한 안이어야(8번) ALLOW 가 돼요. 그룹이 DISABLED 면
   * 6번에서 막혀요. 9번(사용자 grant)은 사용자별이라 이 행에서 판정하지 않아요.
   */
  allowed: boolean;
  /**
   * 정책의 asset_version 이 현재 Registry 버전과 다른 상태.
   * 판정 4번이 ASSET_VERSION_MISMATCH 로 DENY 하므로 스위치를 잠가요.
   */
  versionMismatch: boolean;
  /**
   * 정책이 가리키는 권한 그룹을 신뢰할 수 없는 상태 — DISABLED 이거나 아예 없어요.
   * 판정 6번(CONNECTION_INACTIVE)이 두 경우 모두 DENY 해요.
   */
  groupInactive: boolean;
  /**
   * 정책의 required_capabilities 중 지금 ACTIVE 가 아닌 것 (판정 7번 CAPABILITY_INACTIVE).
   * 그룹 정의에서 capability 를 Disabled 로 바꾸면 정책은 그대로인데 판정만 DENY 로 바뀌어요.
   */
  inactiveCapabilities: string[];
  /**
   * 정책의 required_capabilities 중 그룹 상한 밖인 것 (판정 8번 CAPABILITY_OUTSIDE_CEILING).
   * 상한을 좁히면 기존 정책이 조용히 무효가 돼요.
   */
  outsideCeilingCapabilities: string[];
};

/** 매트릭스의 자산 그룹 — 배선된 MCP 자산 하나. */
export type MatrixAssetGroup = {
  assetId: string;
  name: string;
  /** Registry 상 현재 버전. 정책을 쓸 때 이 버전으로 기록돼요. */
  version: string;
  /** 자산을 못 읽었을 때의 사유. 있으면 tool 목록이 비어요. */
  error: string;
  /** Registry 상태가 APPROVED 가 아니면 판정 2번이 ASSET_INACTIVE 로 막아요. */
  approved: boolean;
  tools: MatrixTool[];
};

type DescriptorRecord = Record<string, unknown>;

function asRecord(value: unknown): DescriptorRecord | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as DescriptorRecord)
    : null;
}

/**
 * Agent descriptor 에서 배선된 MCP 자산 목록을 읽어요.
 * 백엔드 `delegated_asset_ids`(delegation.py:93)와 같은 자리를 봐요 — 그쪽은 판정용
 * ID 튜플만 만들고, 여기는 화면 표시용으로 이름·endpoint 까지 함께 들고 와요.
 */
export function wiredMcpAssets(descriptors: DescriptorRecord | undefined): WiredMcpAsset[] {
  const agent = asRecord(descriptors?.agent);
  const dependencies = asRecord(agent?.agoraDependencies);
  const assets = dependencies?.mcpAssets;
  if (!Array.isArray(assets)) return [];
  const seen = new Set<string>();
  const out: WiredMcpAsset[] = [];
  for (const raw of assets) {
    const item = asRecord(raw);
    const assetId = String(item?.assetId ?? "").trim();
    if (!assetId || seen.has(assetId)) continue;
    seen.add(assetId);
    out.push({
      assetId,
      name: String(item?.name ?? "").trim() || assetId,
      endpoint: String(item?.endpoint ?? "").trim(),
    });
  }
  return out;
}

/**
 * MCP 자산 descriptor 에서 tool 목록을 읽어요.
 *
 * `mcp.tools.inlineContent` 는 **JSON 문자열**이에요 (registry.py:98 이 json.dumps 로
 * 저장하고, 판정과 무관한 governance·overlap 도 같은 자리를 파싱해요).
 * connect 모드는 등록 시 tools/list 로 발견한 것이고, 배포 모드는 빌드 때 추출한 거예요.
 */
export function mcpToolNames(
  descriptors: DescriptorRecord | undefined,
): { name: string; description: string }[] {
  const mcp = asRecord(descriptors?.mcp);
  const tools = asRecord(mcp?.tools);
  const inline = tools?.inlineContent;
  let parsed: unknown = inline;
  if (typeof inline === "string") {
    try {
      parsed = JSON.parse(inline);
    } catch {
      return [];
    }
  }
  const list = asRecord(parsed)?.tools;
  if (!Array.isArray(list)) return [];
  const seen = new Set<string>();
  const out: { name: string; description: string }[] = [];
  for (const raw of list) {
    const item = asRecord(raw);
    const name = String(item?.name ?? "").trim();
    if (!name || seen.has(name)) continue;
    seen.add(name);
    out.push({ name, description: String(item?.description ?? "").trim() });
  }
  return out;
}

/**
 * 자산 하나의 tool 목록과 저장된 정책을 합쳐 매트릭스 행을 만들어요.
 *
 * `operation_id` 는 MCP 자산 descriptor 의 tool 이름을 그대로 써요. 강제 지점이
 * Gateway tool 이름(`{target}___{tool}`)에서 `___` 뒤를 떼어 보내기 때문이에요
 * (scaffold.py:382 `_bound_operation`). 여기서 다른 키를 쓰면 화면은 켜졌는데
 * 판정은 ASSET_CAPABILITY_NOT_FOUND 로 DENY 하는 불일치가 생겨요.
 */
/**
 * 판정 6·7·8번에 필요한 권한 그룹 정보.
 *
 * `activeCapabilities`·`ceiling` 이 `undefined` 면 "아직 못 읽었다"는 뜻이라
 * 위반으로 단정하지 않아요 (로딩·조회 실패를 허위 경보로 만들지 않기 위해서예요).
 */
export type MatrixGroupInfo = {
  status: string;
  activeCapabilities?: Set<string>;
  ceiling?: Set<string>;
};

export function buildAssetGroup(
  wired: WiredMcpAsset,
  asset: AssetDetail | undefined,
  policies: AssetCapabilityPolicy[] | undefined,
  loadError: boolean,
  /**
   * connection_id → 그룹 정보. 맵이 비어 있으면 그룹 목록을 아직 못 읽은 상태로 보고
   * 위반 판정을 보류해요. 맵이 채워진 뒤 키가 없으면 **그룹이 삭제된 것**이라
   * 판정 6번이 DENY 하므로 groupInactive 로 처리해요.
   */
  groupsById: Map<string, MatrixGroupInfo> = new Map(),
): MatrixAssetGroup {
  if (loadError || !asset) {
    return {
      assetId: wired.assetId,
      name: wired.name,
      version: "",
      error: loadError ? "자산을 불러오지 못했어요." : "",
      approved: false,
      tools: [],
    };
  }
  const byOperation = new Map(
    (policies ?? []).map((policy) => [policy.operation_id, policy] as const),
  );
  // 그룹 목록을 아직 못 읽었으면(맵이 비어 있음) 위반으로 단정하지 않아요 —
  // 전부 비활성으로 보이는 허위 경보가 되거든요.
  const groupsKnown = groupsById.size > 0;
  const evaluate = (policy: AssetCapabilityPolicy | null, currentVersion: string) => {
    const versionMismatch = Boolean(policy && policy.asset_version !== currentVersion);
    if (!policy) {
      return {
        versionMismatch,
        groupInactive: false,
        inactiveCapabilities: [],
        outsideCeilingCapabilities: [],
        allowed: false,
      };
    }
    const info = groupsById.get(policy.connection_id);
    // 목록을 읽은 뒤에도 키가 없으면 그룹이 삭제된 거예요 — 판정 6번이 DENY 해요.
    const groupInactive = groupsKnown && (info === undefined || info.status !== "ACTIVE");
    const required = policy.required_capabilities;
    const inactiveCapabilities = info?.activeCapabilities
      ? required.filter((name) => !info.activeCapabilities!.has(name))
      : [];
    const outsideCeilingCapabilities = info?.ceiling
      ? required.filter((name) => !info.ceiling!.has(name))
      : [];
    return {
      versionMismatch,
      groupInactive,
      inactiveCapabilities,
      outsideCeilingCapabilities,
      allowed:
        policy.status === "APPROVED" &&
        !versionMismatch &&
        !groupInactive &&
        inactiveCapabilities.length === 0 &&
        outsideCeilingCapabilities.length === 0,
    };
  };
  const tools = mcpToolNames(asset.descriptors).map((tool) => {
    const policy = byOperation.get(tool.name) ?? null;
    return {
      operationId: tool.name,
      description: tool.description,
      policy,
      ...evaluate(policy, asset.version),
    };
  });
  // descriptor 에 없지만 정책만 남은 operation 도 보여줘요 — 자산이 tool 을 뺀 뒤
  // 남은 APPROVED 정책은 판정 3~5번을 통과하므로 admin 이 볼 수 있어야 해요.
  const knownNames = new Set(tools.map((tool) => tool.operationId));
  for (const policy of policies ?? []) {
    if (knownNames.has(policy.operation_id)) continue;
    tools.push({
      operationId: policy.operation_id,
      description: "자산 descriptor 에 없는 operation 이에요 (tool 이 제거됐을 수 있어요).",
      policy,
      ...evaluate(policy, asset.version),
    });
  }
  return {
    assetId: wired.assetId,
    name: asset.name || wired.name,
    version: asset.version,
    error: "",
    approved: asset.status === "APPROVED",
    tools,
  };
}
