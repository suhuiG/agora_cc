// 「사용자 권한」(/admin/grants) 화면의 판정과 문구 (순수 함수·상수만).
//
// 컴포넌트에 두지 않는 이유는 `toolAccess.ts` 와 같아요 — web 테스트 러너는
// `node --experimental-strip-types` 라 `.tsx` 를 실행하지 못해요(`web/package.json` 의 test
// 목록에 `.tsx` 가 0개예요). 문구를 여기 상수로 두면 **값으로** 단정할 수 있어요.
//
// ── 이 화면이 무엇인지 (2026-09-06 실측, IH-173) ─────────────────────────────────────
//
// 이 화면은 ⑦ grant 를 **필터 없이 전역으로 조회하고 회수하는 유일한 경로**예요. 근거는
// 서버 소스이고, 인용이 아니라 직접 확인한 것이에요:
//
//   · `access_router.list_access_grants` 는 `list_grants(principal_id=None, ...)` 를 불러요.
//     그 경로는 `dynamo_store.list_grants` 의 `self._scan()` 분기라 **모든** `GRANT#` 행이
//     나와요 — 현행 행과 옛 행이 함께 나올 수 있어요(비율은 라이브 값이라 박지 않아요).
//   · `access_router.revoke_access_grant` 는 `store.put_grant(REVOKED)` 를 써요.
//     `put_grant` 는 `store.grant_key_for` 로 `(PK, SK)` 를 만드는데, 그게
//     `gateway_interceptor._has_tool_grant` → `get_tool_grant` 가 읽는 **그 정확한 키**예요
//     (`PRINCIPAL#<sub>`|`GROUP#<group>` + `GRANT#<asset>#<operation>`, 2026-09-06 실행 확인).
//     interceptor 는 `grant.status is not GrantStatus.ACTIVE` 를 거부해요.
//     → **여기서 회수하면 실제 도구 호출 판정이 바뀌어요.**
//   · `create_access_grant`·`reissue_access_grant` 는 둘 다 `HTTPException(410)` 이에요
//     (라벨 시절 경로, ADR-0099 결정 2). 부여는 이 화면에서 할 수 없어요.
//
// ⚠️ 라이브 행 «개수» 는 여기에 적지 않아요 — 바뀌는 값이라 적는 순간 화면이 거짓말을 해요.

import { principalPresentation } from "./principal.ts";
import { toolGrantGroupLabel } from "./toolAccess.ts";
import type { AccessGrant } from "./api/identity";
import type { CognitoUser } from "./api/users";

// ── 화면 문구 ──────────────────────────────────────────────────────────────
//
// 상수로 두는 이유가 이 화면의 결함 이력이에요. 여기 있던 옛 문구는 「도구 인가 판정에는 쓰이지
// 않아요」·「회수해도 도구 호출 판정은 달라지지 않아요」였고, 위 실측과 **정반대**였어요.
// 관리자가 살아 있는 권한을 「무해한 옛 데이터 정리」로 읽을 수 있는 문구예요.

export const GRANTS_PAGE_TITLE = "사용자 권한 (전역 조회·회수)";

export const GRANTS_PAGE_DESCRIPTION =
  "필터 없이 모든 grant 행을 보여줘요 — 도구 하나에 주체 하나를 가리키는 현행 행과, 자산을 " +
  "가리키지 않는 옛 행이 함께 나올 수 있어요. 부여는 이 화면에서 할 수 없고, 회수는 실제 호출 판정을 " +
  "바꿔요.";

export const GRANTS_REVOKE_IS_LIVE_TITLE = "「회수」는 실제 도구 호출 판정을 바꿔요";

/**
 * 배너 본문. **이 문장이 이 티켓의 핵심이에요** — 뒤집으면 관리자가 살아 있는 권한을 무해한
 * 정리로 읽어요.
 */
export const GRANTS_REVOKE_IS_LIVE_NOTICE =
  "회수하면 이 행이 REVOKED 로 남고, Gateway REQUEST interceptor 가 호출마다 같은 키를 다시 " +
  "읽어 비-ACTIVE 를 거부해요. 다음 호출부터 이 행은 허용 근거가 되지 않아요 — 옛 데이터 " +
  "정리가 아니에요. 다만 판정은 «사람 키와 그룹 키의 합집합» 이라, 같은 도구에 다른 축의 " +
  "유효한 권한이 남아 있으면 그 경로로는 계속 호출돼요.";

/**
 * 검색·필터로 걸러 0행이 됐을 때의 안내.
 *
 * 「필터를 바꿔 보세요」만 말하면 관리자가 옛 권한 그룹 옵션을 하나씩 골라 계속 빈 화면을
 * 봐요 — 그 필터는 «옛 행» 에만 있는 축이라, 옛 행이 없으면 어떤 옵션으로도 매치되지 않아요
 * (2026-09-06 브라우저 실측: 옵션 9개 전부 0행). 왜 0인지 말해 줘요.
 *
 * ⚠️ 「지금 옛 행이 0건」이라는 라이브 값은 박지 않아요 — 바뀌는 값이에요.
 */
export const GRANTS_FILTERED_EMPTY_HINT =
  "검색어를 지워 보세요. 「옛 권한 그룹」 필터는 자산을 가리키지 않는 옛 행에만 있는 축이라, " +
  "옛 행이 없으면 어떤 옵션을 골라도 결과가 비어요.";

/** 부여·재발급 경로가 없어진 사실. 지우면 관리자가 여기서 부여를 시도해요. */
export const GRANTS_CREATE_GONE_NOTICE =
  "부여·재발급 경로는 410 으로 없어졌어요 (capability 라벨 시절 경로예요). 새 권한은 " +
  "「도구 호출 주체」에서 도구별로 부여하고, agent 쪽 승인은 「도구 인가 승인」에서 해요.";

export const GRANTS_CREATE_DISABLED_LABEL = "권한 부여 (없어졌어요)";

/** 옛 행 배지 — 「없음」이 아니라 「판정에 참여하지 않음」이에요. */
export const GRANTS_LEGACY_ROW_BADGE = "옛 행 (판정 미참여)";

// ── 현행 행 / 옛 행 ────────────────────────────────────────────────────────

function trimmed(value: string | undefined): string {
  return (value ?? "").trim();
}

/**
 * 이 행이 소비자 경로에 보이는 모양인지.
 *
 * 기준은 **서버의 키 조립 규칙**이에요(`store.grant_key`): `asset_id`·`operation_id` 중
 * 하나라도 비면 `ValueError` 로 거부돼서 `GRANT#<asset>#<operation>` 을 만들 수 없어요.
 * 그래서 그 행은 interceptor 의 정확 키 `GetItem` 에 절대 잡히지 않아요.
 *
 * 필드가 응답에 «없는» 경우도 같은 판정이에요 — 그 응답을 만든 서버가 ⑦ 키를 모르는 서버니까요.
 */
export function grantIsCurrentShape(grant: AccessGrant): boolean {
  return Boolean(trimmed(grant.asset_id) && trimmed(grant.operation_id));
}

// ── 주체 ───────────────────────────────────────────────────────────────────

export type GrantSubjectKind = "person" | "group" | "unrecorded";

export type GrantSubjectPresentation = {
  kind: GrantSubjectKind;
  /** 첫 줄. */
  label: string;
  /** 둘째 줄. 없으면 빈 문자열이에요. */
  detail: string;
  /** `title` 속성에 넣을 원본 값. 없으면 빈 문자열이에요. */
  title: string;
};

/**
 * 「누구의 권한인가」. 사람이면 이름·email, 그룹이면 라벨, 둘 다 없으면 **정직하게 미기록**.
 *
 * 예전 행 표시는 사람 축만 그렸어요. 그래서 `principal_id` 가 비고 `subject_group` 만 있는
 * 그룹 행이 전부 `principalPresentation("")` 의 「—」로 떨어져서, 서로 다른 주체의 행이
 * 화면에서 구분되지 않았어요.
 *
 * 그룹 라벨은 `toolAccess.toolGrantGroupLabel` 을 그대로 써요 — 같은 ⑦ 행을 두 화면이 다른
 * 이름으로 부르면 한쪽이 조용히 낡아요.
 */
export function grantSubjectPresentation(
  grant: AccessGrant,
  user?: CognitoUser,
): GrantSubjectPresentation {
  const group = trimmed(grant.subject_group);
  const principalId = trimmed(grant.principal_id);

  if (principalId) {
    const uid = principalPresentation(principalId);
    const label = user?.name || user?.email || uid.label;
    const parts = [
      user?.email && user.email !== label ? user.email : "",
      `UID ${uid.label}`,
    ].filter(Boolean);
    return {
      kind: "person",
      label,
      detail: parts.join(" · "),
      title: principalId,
    };
  }

  if (group) {
    return {
      kind: "group",
      label: `${toolGrantGroupLabel(group)} (${group})`,
      detail: "그룹 단위 — 이 그룹의 회원 전체에게 적용돼요.",
      title: group,
    };
  }

  return {
    kind: "unrecorded",
    label: "주체 미기록",
    detail: "principal_id·subject_group 이 둘 다 비어 있어요 — 어떤 주체에도 붙지 않아요.",
    title: "",
  };
}

// ── 자산 이름 (asset_id → MCP 이름) ────────────────────────────────────────
//
// grant 행에는 `asset_id` 만 와요(예: `BiWjMmav9QKe`). 이름은 «다른» 응답에서 가져와야 해요.
//
// ── 어느 경로를 골랐나 (2026-09-06) ─────────────────────────────────────────
//
// 고른 것: **`GET /api/governance/inventory`** (`api/governance.getGovInventory`).
//   · 읽기 전용이에요 — 라우터 docstring 이 그렇게 말하는 것 말고, 실제 호출 사슬을 읽었어요:
//     `inventory()` → `dashboard_router._queue_annotations()` → `list_records` ·
//     `latest_scan` · `get_settings` · `all_overlaps`. 쓰기 호출이 0개예요.
//   · 자산 «전 상태» 를 줘요(DRAFT/PENDING/APPROVED/REJECTED) — `list_records` 소스라서요.
//   · SWR 키를 「자산 인벤토리」·「거버넌스 도구」 화면과 **같은 문자열**로 두면 요청이
//     합쳐져요(`GOV_INVENTORY_SWR_KEY`).
//
// 버린 후보:
//   · **`GET /api/mcp/tool-drift`** (`AssetToolDrift.record_id` + `.asset_name`) — 이름만 보면
//     제일 맞는 모양이지만 **읽기 경로가 원장에 써요.** `drift_service.snapshot()` 은 원장이 빈
//     자산마다 `_seed(record)` 를 부르고, `_seed` 는 `self._store.put(...)` 로 드리프트 원장을
//     갱신해요(`DriftLedgerConflict` 를 잡는 코드가 그 증거예요). 사용자 화면을 열 때마다
//     원장에 쓰는 건 받아들일 수 없어요.
//   · **`GET /api/catalog`** (`AssetCard.record_id` + `.name`) — 읽기 전용이지만 페이지네이션이고
//     노출 대상만 줘요. 승인 전·비공개 자산의 grant 가 「이름 없음」으로 떨어져요.
//   · **`GET /api/assets/{id}`** — 상세 한 건씩이라 N+1 이고, **조회수를 올리는 부수효과**가
//     있어요(`catalog.getAsset` 주석).
//
// ⚠️ **이름을 못 찾은 경우를 id 로 조용히 떨어뜨리지 않아요.** 「자산 목록에 없다」와 「자산
// 목록을 아직 못 읽었다」와 「이름이 곧 id 다」는 서로 다른 사실이에요(미관측 ≠ 부재).
// 2026-09-06 라이브에서 실제로 갈렸어요: grant 34행이 가리키는 자산 id 5개 중 `fAUPJWslfkyZ`
// (10행)가 registry 에 **없어요** — purge 된 자산이에요. 나머지 4개는 이름이 있어요.

/**
 * `record_id → 자산 이름` 색인.
 *
 * `observed` 가 핵심이에요 — 목록을 못 읽은 상태와 「그 id 가 목록에 없다」를 가르는 유일한
 * 축이고, 둘을 뭉치면 purge 된 자산과 로딩 중을 화면이 같은 문구로 말해요.
 */
export type AssetNameIndex = {
  observed: boolean;
  names: Map<string, string>;
  /**
   * `true` 면 **그 화면이 자산 목록을 아예 요청하지 않아요.** `observed: false` 와 다른 사실이에요 —
   * 「아직 못 읽었다」는 곧 읽힐 수 있다는 뜻이지만, 이건 영원히 안 읽혀요.
   *
   * ⚠️ 이 축이 없어서 결함이 하나 났어요(2026-09-06 적대적 검증): `/admin/grants` 는 인벤토리를
   * 안 부르는데 기본값이 `observed: false` 라, 모든 현행 행이 「자산 목록을 아직 읽지 못해서…」라는
   * **진행 중 문구를 영구히** 달고 있었어요. 관리자가 기다리면 이름이 나온다고 읽어요.
   */
  notRequested?: boolean;
};

/** 아직 자산 목록을 읽지 못한 상태(로딩·실패). 곧 읽힐 수 있어요. */
export const ASSET_NAMES_UNOBSERVED: AssetNameIndex = {
  observed: false,
  names: new Map(),
};

/** 이 화면이 자산 이름을 «조회하지 않는» 상태. 위와 다른 사실이에요. */
export const ASSET_NAMES_NOT_REQUESTED: AssetNameIndex = {
  observed: false,
  names: new Map(),
  notRequested: true,
};

/** 이름 색인의 입력 — `governance.Inventory` 와 구조가 같아요(그 타입에 의존하진 않아요). */
export type AssetNameSource = {
  assets: { record_id: string; name: string }[];
};

/**
 * 「자산 인벤토리」 응답에서 이름 색인을 만들어요.
 *
 * 응답이 없으면(로딩·실패) `observed: false` 예요. 빈 배열을 「자산이 0개」로 읽지 않아요 —
 * 그건 관측이지만, `undefined` 는 관측이 아니에요.
 */
export function assetNameIndex(
  inventory: AssetNameSource | undefined,
): AssetNameIndex {
  if (!inventory) return ASSET_NAMES_UNOBSERVED;
  const names = new Map<string, string>();
  for (const asset of inventory.assets ?? []) {
    const id = trimmed(asset.record_id);
    const name = trimmed(asset.name);
    if (id && name) names.set(id, name);
  }
  return { observed: true, names };
}

/** 이름 해석 결과. 네 상태를 «절대» 접지 않아요. */
export type AssetNameStatus =
  | "named"
  | "unnamed"
  | "unobserved"
  | "not_requested";

export type AssetNameResolution = {
  status: AssetNameStatus;
  /** `status === "named"` 일 때만 채워져요. */
  name: string;
};

export function resolveAssetName(
  assetId: string,
  assets: AssetNameIndex = ASSET_NAMES_UNOBSERVED,
): AssetNameResolution {
  const id = trimmed(assetId);
  if (assets.notRequested) return { status: "not_requested", name: "" };
  if (!assets.observed) return { status: "unobserved", name: "" };
  const name = id ? trimmed(assets.names.get(id)) : "";
  return name ? { status: "named", name } : { status: "unnamed", name: "" };
}

/** 자산 목록에 그 id 가 없을 때의 문구. purge 가 정상적인 원인이에요. */
export const ASSET_NAME_UNNAMED_NOTE =
  "자산 이름을 확인하지 못했어요 — 자산 목록에 이 id 가 없어요(purge 됐을 수 있어요). " +
  "앞이 자산 id, 뒤가 도구 이름이에요.";

/** 자산 목록 자체를 아직 못 읽었을 때의 문구. 위와 «다른» 사실이에요. */
export const ASSET_NAME_UNOBSERVED_NOTE =
  "자산 목록을 아직 읽지 못해서 이름을 확인하지 못했어요. 앞이 자산 id, 뒤가 도구 이름이에요.";

/**
 * 그 화면이 자산 이름을 **조회하지 않을** 때의 문구. 위와 «다른» 사실이에요 — 기다려도 안 나와요.
 * 그래서 이름을 보려면 어디로 가야 하는지 함께 말해요.
 */
export const ASSET_NAME_NOT_REQUESTED_NOTE =
  "이 화면은 자산 이름을 조회하지 않아요 — 이름은 「사용자 관리」에서 사용자를 고르면 보여요. " +
  "앞이 자산 id, 뒤가 도구 이름이에요.";

// ── 대상 ───────────────────────────────────────────────────────────────────

export type GrantTargetPresentation = {
  kind: "tool" | "legacy";
  label: string;
  /** 덧붙일 사실. 없으면 빈 문자열이에요. */
  note: string;
  /** `title` 속성용 전체 값. */
  title: string;
  /** 이름 해석 결과. 옛 행은 `"unobserved"` (대상 자체가 없어요). */
  nameStatus: AssetNameStatus;
  /** 원장과 대조할 수 있게 «항상» 남겨요. 옛 행은 빈 문자열이에요. */
  assetId: string;
  /**
   * `label` 을 두 칸으로 쪼갠 값 — 「MCP」 열과 「Tool」 열에 각각 들어가요(2026-09-06 요청).
   *
   * ⚠️ `label` 을 지우지 않았어요. 회수 확인 문구와 도달 불가 목록은 한 줄로 읽혀야 해서
   * 그대로 쓰고, 표만 쪼개요. 두 값을 « · » 로 다시 붙이면 `label` 과 같아야 해요 — 그
   * 항등식을 테스트가 지켜요.
   */
  mcpLabel: string;
  /** 옛 행은 도구 자체가 없어서 빈 문자열이에요 — 「—」 같은 표시는 화면이 정해요. */
  toolLabel: string;
};

/**
 * 「무슨 도구의 권한인가」.
 *
 * 자산 «이름» 은 `assets` 색인으로 해석해요(위 「어느 경로를 골랐나」 참고). 이름을 찾았을
 * 때도 **`asset_id` 를 지우지 않아요** — 보조 텍스트(`note`)와 `title` 에 남겨서 운영자가
 * 원장과 대조할 수 있게 해요. 못 찾았으면 id 를 앞에 두고 「확인하지 못했어요」라고 말해요.
 *
 * `toolAccess.toolLabel` 은 이름이 없으면 `operation_id` 만 돌려줘서 `asset_id` 가 사라져요 —
 * 그래서 여기서 재사용하지 않았어요.
 */
export function grantTargetPresentation(
  grant: AccessGrant,
  assets: AssetNameIndex = ASSET_NAMES_UNOBSERVED,
): GrantTargetPresentation {
  const assetId = trimmed(grant.asset_id);
  const operationId = trimmed(grant.operation_id);

  if (assetId && operationId) {
    const resolved = resolveAssetName(assetId, assets);
    const title = `asset_id=${assetId} operation_id=${operationId}`;
    if (resolved.status === "named") {
      return {
        kind: "tool",
        label: `${resolved.name} · ${operationId}`,
        note: `자산 id ${assetId}`,
        title: `${title} asset_name=${resolved.name}`,
        nameStatus: "named",
        assetId,
        mcpLabel: resolved.name,
        toolLabel: operationId,
      };
    }
    return {
      kind: "tool",
      label: `${assetId} · ${operationId}`,
      note:
        resolved.status === "unnamed"
          ? ASSET_NAME_UNNAMED_NOTE
          : resolved.status === "not_requested"
            ? ASSET_NAME_NOT_REQUESTED_NOTE
            : ASSET_NAME_UNOBSERVED_NOTE,
      title,
      nameStatus: resolved.status,
      assetId,
      // 이름을 못 찾았으면 MCP 칸에 id 를 그대로 둬요 — 빈 칸이면 「대상이 없다」로 읽혀요.
      mcpLabel: assetId,
      toolLabel: operationId,
    };
  }

  const missing = [
    assetId ? "" : "asset_id",
    operationId ? "" : "operation_id",
  ].filter(Boolean);
  return {
    kind: "legacy",
    label: GRANTS_LEGACY_ROW_BADGE,
    note:
      `${missing.join("·")} 가 비어 있어서 GRANT#<asset>#<operation> 키를 만들 수 없어요 — ` +
      "interceptor 의 정확 키 조회에 잡히지 않아요.",
    title: missing.join(" "),
    // 대상 자산 자체가 없어요 — 이름을 「못 찾았다」고 말할 대상이 아니에요.
    nameStatus: "unobserved",
    assetId,
    mcpLabel: GRANTS_LEGACY_ROW_BADGE,
    toolLabel: "",
  };
}

// ── 회수 ───────────────────────────────────────────────────────────────────

export type GrantRevokeAvailability = {
  enabled: boolean;
  /** 막힌 이유. 가능할 때는 빈 문자열이에요. */
  reason: string;
};

/**
 * 옛 행은 이 화면에서 회수할 수 없어요.
 *
 * 서버가 거부해서예요 — `revoke_access_grant` 가 `put_grant` 를 부르고, `put_grant` 는
 * `grant_key_for` → `grant_key` 로 정렬 키를 다시 만들어요. `asset_id` 가 비어 있으면 거기서
 * `ValueError` 가 나요(2026-09-06 실행 확인: `grant 키에 빈 asset_id·operation_id 를 쓸 수
 * 없어요`). 버튼을 열어 두면 관리자가 500 을 받고 그게 회수 실패인지 서버 장애인지 몰라요.
 *
 * 그리고 애초에 회수할 것이 없어요 — 그 행은 소비자 경로에 보이지 않아 판정에 참여하지 않아요.
 */
export function grantRevokeAvailability(
  grant: AccessGrant,
): GrantRevokeAvailability {
  if (!grantIsCurrentShape(grant)) {
    return {
      enabled: false,
      reason:
        "옛 행은 회수할 수 없어요 — 서버가 이 행의 키를 다시 만들 수 없고, 이미 판정에 " +
        "참여하지 않아요.",
    };
  }
  return { enabled: true, reason: "" };
}

/**
 * 회수 확인 문구. 무엇이 남고 언제 막히는지 둘 다 말해요.
 *
 * 「즉시」로 뭉치지 않아요 — 행은 지워지지 않고 `REVOKED` 로 남고(하드 삭제가 아니에요),
 * 막히는 시점은 **다음 호출**이에요. interceptor 가 매 호출 `get_tool_grant` 를
 * `ConsistentRead=True` 로 다시 읽으니까요 (사람 행·그룹 행 모두 같은 경로예요).
 */
export function grantRevokeConfirmMessage(
  subject: GrantSubjectPresentation,
  target: GrantTargetPresentation,
): string {
  return (
    `${subject.label} 의 «${target.label}» 호출 권한을 회수할까요? ` +
    "행은 지워지지 않고 회수 이력(REVOKED)으로 남고, 다음 호출부터 이 행은 허용 근거가 " +
    "되지 않아요. 같은 도구에 다른 축(사람 또는 그룹)의 유효한 권한이 남아 있으면 그 경로로는 " +
    "계속 호출돼요 — 그 축도 함께 확인해 주세요."
  );
}

/**
 * 「사용자 관리」 하단 패널 전용 회수 확인 문구.
 *
 * ⚠️ 왜 공유 함수를 그대로 쓰지 않나: 그 패널은 **「이 사용자의 권한」이라는 1인 프레임**이에요.
 * 그런데 그 목록에는 그 사람이 «그룹을 통해» 얻은 행도 함께 나오고, 그 행을 회수하면 **그룹
 * 회원 전체**의 권한이 사라져요. 1인 프레임 안에서 「이 사용자의 … 권한을 회수할까요」만 읽으면
 * 관리자가 폭발 반경을 한 사람으로 착각해요.
 *
 * 그리고 이건 이론이 아니에요 — 2026-09-06 라이브 실측에서 `GRANT#` 행이 **전부 그룹 행**이라,
 * 이 패널의 회수는 사실상 **항상** 그룹 전체 회수예요.
 *
 * 공유 함수(`grantRevokeConfirmMessage`)는 안 건드려요. 전역 화면은 「주체」 열이 그룹임을
 * 이미 보여주니 프레임이 다르고, 그 함수의 합집합 경고는 IH-173 의 blocker 수정이었어요.
 */
export function userPanelRevokeConfirmMessage(
  subject: GrantSubjectPresentation,
  target: GrantTargetPresentation,
  memberCount: number | null = null,
): string {
  const base = grantRevokeConfirmMessage(subject, target);
  if (subject.kind !== "group") return base;
  const scope =
    memberCount === null
      ? "그 그룹의 회원 «전체» 예요(인원은 확인하지 못했어요)"
      : `그 그룹의 회원 «전체» — 지금 ${memberCount}명이에요`;
  return (
    `⚠️ 이건 한 사람의 권한이 아니에요. 그룹 «${subject.title}» 에 부여된 행이라 회수 대상은 ` +
    `${scope}. ` + base +
    " 그리고 그룹 축은 delegation TTL 때문에 최대 900초 늦게 판정에 반영돼요 — 그 사이에는" +
    " 이 경로로 호출이 계속 통과해요."
  );
}

// ── 조회 ───────────────────────────────────────────────────────────────────

/**
 * 전역 grant 목록의 SWR 키. 「사용자 권한」 화면이 쓰는 **그 문자열**이에요 —
 * 갈라 놓으면 같은 응답을 두 번 받아요(IH-164 와 같은 계열).
 */
export const ALL_GRANTS_SWR_KEY = "admin/access-grants";

/**
 * 자산 이름 색인의 SWR 키. 「자산 인벤토리」·「거버넌스 도구」 화면이 쓰는 **그 문자열**이에요.
 */
export const GOV_INVENTORY_SWR_KEY = "gov/inventory";

export function grantUsersBySub(
  users: CognitoUser[] | undefined,
): Map<string, CognitoUser> {
  return new Map((users ?? []).map((user) => [user.sub, user]));
}

/**
 * 목록 검색·필터.
 *
 * 현행 행의 축(`subject_group`·`asset_id`·`operation_id`)도 검색해요. 사람 이름과 옛
 * capability 라벨만 보면, 주체가 그룹이고 capability 가 빈 현행 행은 **어떤 검색어로도
 * 찾을 수 없어요.**
 */
export function filterAccessGrants(
  grants: AccessGrant[] | undefined,
  {
    query,
    groupFilter,
    connectionNames,
    usersBySub,
  }: {
    query: string;
    groupFilter: string;
    connectionNames: Map<string, string>;
    usersBySub: Map<string, CognitoUser>;
  },
): AccessGrant[] {
  const needle = query.trim().toLowerCase();
  return (grants ?? []).filter((grant) => {
    if (groupFilter && grant.connection_id !== groupFilter) return false;
    if (!needle) return true;
    const groupName = connectionNames.get(grant.connection_id) ?? "";
    const user = usersBySub.get(grant.principal_id);
    const subjectGroup = trimmed(grant.subject_group);
    const haystack = [
      grant.principal_id,
      user?.name ?? "",
      user?.email ?? "",
      groupName,
      subjectGroup,
      subjectGroup ? toolGrantGroupLabel(subjectGroup) : "",
      trimmed(grant.asset_id),
      trimmed(grant.operation_id),
      ...grant.capabilities,
    ];
    return haystack.some((value) => value.toLowerCase().includes(needle));
  });
}

// ── 사용자 한 명의 권한 (사용자 관리 화면 하단 패널) ──────────────────────────
//
// ⑦ 판정은 **사람 키와 그룹 키의 합집합**이에요 — `gateway_interceptor._has_tool_grant` 가
// `[{principal_id}] + [{subject_group} …]` 을 돌며 처음 ACTIVE 에서 통과시켜요. 그래서 사람
// 축만 보여주면 화면이 「이 사람이 가진 권한」을 **과소** 표시해요.
//
// ⚠️ 라이브에서 그건 이론이 아니었어요. 2026-09-06 읽기 전용 실측(`agora-identity-dev` 전량
// scan, `GRANT#` 34행): **32행이 그룹 행**(`GROUP#admin` 21 · `GROUP#user` 11)이고 사람 행은
// 2행뿐이에요. 사람 축만 조회하면 거의 모든 사용자에게 「권한 없음」으로 보여요.
//
// 그래서 이 패널은 «전역 목록»(`GET /api/admin/access-grants`, 필터 없음)을 한 번 받아
// 클라이언트에서 사람 축·그룹 축으로 갈라요. `?principal_id=` 필터를 안 쓴 이유가 이거예요 —
// 서버의 그 경로는 `PRINCIPAL#<sub>` 파티션만 Query 하고 `subject_groups` 를 안 넘겨서
// (`access_router.list_access_grants`) 그룹 행이 응답에 **안 들어와요.**

export type UserGrantAxes = {
  /** `PRINCIPAL#<sub>` 축. */
  person: AccessGrant[];
  /** 이 사용자가 속한 Cognito 그룹 축. `group` 은 그룹 id 예요. */
  groups: { group: string; grants: AccessGrant[] }[];
  /** 사람 축 + 그룹 축 전부. 판정의 합집합과 같은 집합이에요. */
  union: AccessGrant[];
};

/**
 * 사용자 한 명에게 «판정상» 붙는 grant 를 축별로 갈라요.
 *
 * 그룹 축의 근거는 이 사용자의 Cognito 그룹이에요. 런타임은 delegation handle 에 기록된
 * 그룹을 읽으니(`DelegationContext.principal_groups`) 방금 바뀐 그룹은 최대 900초 늦게
 * 반영돼요 — 그래서 이 화면은 「지금 Cognito 기준」이라고 말해요.
 */
export function userGrantAxes(
  grants: AccessGrant[] | undefined,
  user: { sub: string; groups: string[] },
): UserGrantAxes {
  const rows = grants ?? [];
  const sub = trimmed(user.sub);
  const person = sub
    ? rows.filter((grant) => trimmed(grant.principal_id) === sub)
    : [];
  const wanted = [
    ...new Set((user.groups ?? []).map((group) => trimmed(group)).filter(Boolean)),
  ];
  const groups = wanted.map((group) => ({
    group,
    grants: rows.filter((grant) => trimmed(grant.subject_group) === group),
  }));
  return {
    person,
    groups,
    union: [...person, ...groups.flatMap((axis) => axis.grants)],
  };
}

/**
 * 「어느 사용자를 골라도 안 나오는 행」.
 *
 * ⚠️ 옛 `/admin/grants` 는 **필터 없는 전역 목록**이었어요. 사용자를 골라야 보이는 화면으로
 * 옮기면, 어떤 사용자에도 붙지 않는 행이 조용히 도달 불가가 돼요. 「없는 셈」 치지 않고 세서
 * 화면에 남겨요.
 *
 * 두 종류예요:
 *   · `unrecorded` — `principal_id`·`subject_group` 이 둘 다 빈 행. 어느 사용자에도 안 붙어요.
 *   · `orphanGroups` — 지금 목록에 보이는 사용자 중 아무도 속하지 않은 그룹의 행.
 *     회원이 0명인 그룹에 부여된 권한이 여기 걸려요.
 *
 * `knownGroups` 를 안 주면 그룹 축은 판정하지 않아요 — 「사용자 목록을 못 봤다」를
 * 「고아 그룹이 없다」로 접지 않으려고요(미관측 ≠ 부재).
 */
export type GrantReachability = {
  total: number;
  unrecorded: AccessGrant[];
  orphanGroups: { group: string; grants: AccessGrant[] }[];
  /** 그룹 축을 판정할 수 있었는지. `knownGroups` 가 없으면 `false` 예요. */
  groupAxisObserved: boolean;
};

export function grantReachability(
  grants: AccessGrant[] | undefined,
  knownGroups?: Iterable<string>,
): GrantReachability {
  const rows = grants ?? [];
  const unrecorded = rows.filter(
    (grant) => !trimmed(grant.principal_id) && !trimmed(grant.subject_group),
  );
  if (knownGroups === undefined) {
    return {
      total: rows.length,
      unrecorded,
      orphanGroups: [],
      groupAxisObserved: false,
    };
  }
  const known = new Set(
    [...knownGroups].map((group) => trimmed(group)).filter(Boolean),
  );
  const byGroup = new Map<string, AccessGrant[]>();
  for (const grant of rows) {
    const group = trimmed(grant.subject_group);
    if (!group || known.has(group)) continue;
    byGroup.set(group, [...(byGroup.get(group) ?? []), grant]);
  }
  return {
    total: rows.length,
    unrecorded,
    orphanGroups: [...byGroup].map(([group, rowsForGroup]) => ({
      group,
      grants: rowsForGroup,
    })),
    groupAxisObserved: true,
  };
}

// ── 패널 문구 ──────────────────────────────────────────────────────────────

export const USER_GRANTS_PANEL_TITLE = "도구 호출 권한";

/**
 * ⚠️ 2026-09-06: 제품 오너 요청으로 패널의 설명 문단을 **없앴어요**(`USER_GRANTS_PANEL_DESCRIPTION`
 * 삭제). 그 문단이 담고 있던 두 사실은 버리지 않고 옮겼어요 — 문장을 지울 때 그 안의 «다른»
 * 사실이 같이 사라지는 게 이 저장소의 반복 실수예요.
 *
 * ⑴ 「사람 키 + 그룹 키의 합집합」 → 섹션 제목이 `User 권한`·`Group` 으로 갈려서 두 축이 눈에
 *    보여요. 그리고 회수 확인 문구가 합집합 경고를 이미 담고 있어요.
 * ⑵ 「그룹 변경은 delegation TTL 때문에 최대 900초 늦게 반영」 → **그룹 회수 확인 문구로**
 *    옮겼어요(아래 `userPanelRevokeConfirmMessage`). 그게 이 사실이 실제로 필요한 순간이에요 —
 *    회수를 누른 관리자가 즉시 막힌다고 믿으면 그 창 동안 호출이 통과해요.
 */

export const USER_GRANTS_PERSON_AXIS_LABEL = "User 권한";

export const USER_GRANTS_GROUP_AXIS_LABEL = "Group";

/**
 * 그룹 축 제목 줄에 한 번 붙는 사실. 옛 표는 이걸 «행마다» 반복했는데, 주체 열을 없애면서
 * 그 자리가 사라졌어요 — 문구를 지울 때 그 안의 사실이 같이 사라지면 안 돼요.
 */
export const USER_GRANTS_GROUP_SCOPE_NOTE =
  "이 그룹의 회원 전체에게 적용돼요";

export const USER_GRANTS_EMPTY_TITLE = "이 사용자에게 붙는 grant 행이 없어요.";

export const USER_GRANTS_EMPTY_DESCRIPTION =
  "사람 축과 이 사용자의 그룹 축을 둘 다 봤는데 행이 없어요 — 지금은 어떤 도구도 부를 수 " +
  "없어요. 새 권한은 「도구 호출 주체」에서 도구별로 부여해요.";

/**
 * 도달성 공개 문구. 「전역 목록이 사라져서 못 보게 된 행」을 화면에서 밝혀요.
 */
export const USER_GRANTS_UNATTACHED_TITLE = "어느 사용자에도 붙지 않는 행";

export const USER_GRANTS_UNATTACHED_NOTICE =
  "이 화면은 사용자를 골라야 권한이 보여요. 그래서 주체가 비어 있는 행이나, 지금 목록에 " +
  "보이는 사용자 중 아무도 속하지 않은 그룹의 행은 어느 사용자를 골라도 나오지 않아요. " +
  "숨기지 않고 여기 그대로 세어 보여줘요.";

/**
 * 「그런 행이 없어요」 — **두 축을 다 봤을 때만** 쓸 수 있는 문구예요.
 *
 * ⚠️ 첫 판은 이 문장 하나만 두고 `unrecorded.length === 0` 이면 그렸어요. 그러면 회원이 0명인
 * 그룹의 행을 «본 적도 없이» 「모든 행이 도달 가능」이라고 단정해요 — 미관측을 통과로 기록하는
 * 그 결함이에요. 아래 `…_GROUP_AXIS_UNKNOWN` 과 반드시 갈라 써요.
 */
export const USER_GRANTS_UNATTACHED_NONE =
  "지금 전역 목록에는 그런 행이 없어요 — 주체가 비어 있는 행도, 회원이 없는 그룹의 행도 " +
  "없어서 모든 행이 어느 사용자를 골라 도달할 수 있어요.";

/** 그룹 축을 관측하지 못했을 때. 「확인 못 함」이지 「없음」이 아니에요. */
export const USER_GRANTS_UNATTACHED_GROUP_AXIS_UNKNOWN =
  "주체가 비어 있는 행은 없어요. 다만 회원이 0명인 그룹의 행은 **확인하지 못했어요** — 이 " +
  "화면이 전체 사용자 목록을 읽지 못했어요. 「없음」이 아니라 「미확인」이에요.";

/** 회원이 아무도 없는 그룹에 붙은 행의 머리말. */
export const USER_GRANTS_ORPHAN_GROUP_LABEL =
  "회원이 없는 그룹에 붙은 행 (어느 사용자를 골라도 안 나와요)";

export const USER_GRANTS_LOAD_ERROR = "이 사용자의 권한을 불러오지 못했어요.";
