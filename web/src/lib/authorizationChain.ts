// 통합 인가 승인 화면의 판정·문구 (순수 함수만). 층은 **둘**이에요 — ADR-0099.
//
// 화면 컴포넌트에 두지 않는 이유: web 테스트 러너는 `node --experimental-strip-types` 라
// JSX 를 변환하지 못해요(`web/package.json:10` — `.tsx` 항목이 0개예요). 판정을 여기 두면
// 테스트가 돌고, 컴포넌트는 그리기만 해요.
//
// **이 모듈은 인가를 판정하지 않아요.** 단계별 상태는 서버가 원장을 읽어 정해요
// (`api/src/agora/domains/identity/access_router.py:3073-3193`). 여기는 그 결과를 사람이 읽는
// 문구와 「다음에 뭘 눌러야 하나」로 바꾸는 표현 계층이에요.

import type {
  AuthorizationRequestRow,
  ChainApprovalResult,
  ChainLayerName,
  ChainLayerState,
  ChainLayerStatus,
} from "./api/authorizationRequests";

/**
 * 진행선에 그리는 **두 단계** — 강제 지점이 보는 순서 그대로예요 (ADR-0099 결정 1).
 *
 * 예전엔 네 칸이었어요(binding · 자산 권한 · 권한 그룹 · 사람 권한). 가운데 두 칸은 「이 사람이
 * 이 도구를 쓸 수 있나」를 capability 라벨과 connection 이라는 중간 화폐로 환전하던 층이고,
 * 그 환전이 IH-127·IH-130 의 원인이라 인가 경로에서 없어졌어요.
 *
 * 사슬 번호(④⑦)를 빼뒀어요. 번호는 ADR·백로그에서 쓰는 내부 좌표라 화면에서는 관리자가
 * 모르는 기호일 뿐이에요. 사유 문구가 층을 지목해 말하니 번호 없이도 어느 칸이 막혔는지
 * 알 수 있어요.
 */
export const CHAIN_LAYERS: {
  layer: ChainLayerName;
  title: string;
  hint: string;
}[] = [
  {
    layer: "tool_binding",
    title: "binding",
    hint: "이 agent 가 이 operation 을 부를 수 있게 승인됐나 (자산 버전 대조까지 여기서 해요)",
  },
  {
    layer: "human_grant",
    title: "사람 권한",
    hint: "부여 대상(사람·그룹)이 이 도구를 부를 수 있나",
  },
];

/** 배지 색 갈래. `unknown` 은 초록불도 빨간불도 아니에요(ADR-0037 §4). */
export type LayerTone = "ok" | "todo" | "blocked" | "unknown";

export function layerTone(state: ChainLayerState): LayerTone {
  if (state === "SATISFIED") return "ok";
  if (state === "UNKNOWN") return "unknown";
  return state === "MISSING" ? "todo" : "blocked";
}

export type AssetStateBadgeTone = "neutral" | "ok" | "warn" | "unknown";

export type AssetStateBadge = {
  axis: "registry" | "approval";
  label: string;
  detail: string;
  tone: AssetStateBadgeTone;
};

/**
 * Registry 관측과 자산 승인 여부는 서로 다른 축이라 배지도 둘로 유지해요.
 *
 * `assetObservation=observed`인데 `assetFound=false`이면 `assetApproved=false`도 오지만,
 * 그 값은 미승인 확정이 아니라 자산 부재로 승인 상태를 읽지 못한 결과예요. Registry 자체를
 * 못 읽은 경우에는 두 boolean이 모두 null이에요. 둘을 한 배지로 접으면 부재·미관측을 정상적인
 * 미승인 상태로 오진해요.
 */
export function assetStateBadges(
  row: AuthorizationRequestRow,
): [AssetStateBadge, AssetStateBadge] {
  if (row.assetObservation !== "observed") {
    return [
      {
        axis: "registry",
        label: "Registry를 관측하지 못함",
        detail:
          "Registry 조회에 실패해 자산이 있는지 확인하지 못했어요. 부재로 판정하지 않아요.",
        tone: "unknown",
      },
      {
        axis: "approval",
        label: "승인 상태를 확인할 수 없음",
        detail: "Registry 관측이 복구된 뒤 현재 승인 상태를 다시 확인해 주세요.",
        tone: "unknown",
      },
    ];
  }

  const registry: AssetStateBadge = row.assetFound === true
    ? {
        axis: "registry",
        label: "Registry에서 확인됨",
        detail: "이 자산을 Registry에서 찾았어요.",
        tone: "neutral",
      }
    : {
        axis: "registry",
        label: "Registry에서 찾지 못함",
        detail: "Registry 조회는 성공했고 이 자산을 찾지 못했어요.",
        tone: "warn",
      };

  if (row.assetFound === false) {
    return [
      registry,
      {
        axis: "approval",
        label: "승인 상태를 확인할 수 없음",
        detail: "Registry에서 자산을 찾지 못해 현재 승인 상태를 확인할 수 없어요.",
        tone: "unknown",
      },
    ];
  }

  return [
    registry,
    row.assetApproved
      ? {
          axis: "approval",
          label: "자산 승인됨",
          detail: "자산이 현재 APPROVED 상태예요.",
          tone: "ok",
        }
      : {
          axis: "approval",
          label: "현재 승인 상태가 아님",
          detail:
            "자산이 현재 APPROVED가 아니에요. CREATING·UPDATING 같은 과도 상태를 포함해 다른 상태일 수 있어요.",
          tone: "warn",
        },
  ];
}

/**
 * 단계 배열에서 한 층을 꺼내요. 서버는 길이 2·고정 순서로 보내지만 **항상 이름으로** 찾아요.
 *
 * 순서를 가정한 인덱스 접근은 층 수가 바뀌면 조용히 다른 층을 읽어요 — ADR-0099 로 칸이
 * 넷에서 둘로 줄면서 `steps[3]`·`steps.slice(0, 3)` 이 정확히 그렇게 됐어요.
 */
export function stepFor(
  steps: ChainLayerStatus[],
  layer: ChainLayerName,
): ChainLayerStatus | undefined {
  return steps.find((step) => step.layer === layer);
}

/**
 * 목록 진행선이 그릴 단계 — **exact-key 진단이 도착한 뒤에만** 그 값을 써요.
 *
 * 목록 payload 의 ⑦칸은 서버가 «일부러» `UNKNOWN`/`deferred_to_row_expand` 로 줘요. 확인에
 * 사람별 소비자 경로 Query 가 필요해서예요(`access_router.list_authorization_requests` 의
 * docstring). 그 회색을 목록 payload 만 보고 초록으로 바꾸면 IH-127 이 재발해요 — 잘못된
 * 파티션의 행이 「있다」로 보였던 2026-08-29 사고가 화면에서 재현되는 경로예요.
 *
 * 그래서 **초록의 근거는 「exact-key 로 관측했다」** 예요. 관측값의 소유자는 이 모듈도 목록
 * payload 도 아니고 `diagnoseAuthorizationRequest` 응답이에요(ADR-0037 §4 — 기대값과 대상이
 * 같은 주인이면 검사가 아니에요). 관측이 도착하기 전에는 목록 payload 그대로, 즉 회색이에요.
 *
 * 층별로 골라요 — 관측에 그 층이 있으면 관측값, 없으면 목록값. 서버가 어느 쪽에서 층을 하나만
 * 보내도 다른 칸이 조용히 사라지지 않아요.
 *
 * ⚠️ `progressFilled`(= `progress` 정렬)은 **목록 payload 만** 봐요. 관측을 정렬에 넣으면
 * 행을 펼칠 때마다 그 행이 목록에서 자리를 옮겨요.
 */
export function progressSteps(
  row: AuthorizationRequestRow,
  observed?: ChainLayerStatus[],
): ChainLayerStatus[] {
  if (observed === undefined) return row.steps;
  const listLayers = new Set(row.steps.map((step) => step.layer));
  return [
    ...row.steps.map((step) => stepFor(observed, step.layer) ?? step),
    ...observed.filter((step) => !listLayers.has(step.layer)),
  ];
}

/**
 * 이 관측을 목록 점에 «올려도 되나» — 주체가 신청자일 때만 참이에요.
 *
 * 서버는 ⑦ 를 `principal_id or binding.created_by` 로 판정하므로 exact-key 진단은 **그 주체에
 * 대한 답**이에요. 관리자가 부여 대상을 그룹이나 다른 사람으로 바꿔 관측한 초록을 목록에 올리면,
 * 목록이 「신청자가 부를 수 있다」로 읽히는데 신청자는 아직 못 불러요 — 초록 배지인데 호출은
 * 거부되는 IH-127 계열이에요.
 *
 * 신청자를 확정하지 못한 행(`defaultGrantPrincipal.mustPick`)은 **기대 주체가 없으니 올리지
 * 않아요.** 「빈 주체로 관측했다」를 「신청자로 관측했다」로 접으면 같은 거짓이 돼요.
 * 올리지 않으면 회색이고, 회색은 통과도 거부도 아니에요(ADR-0037 §4).
 */
export function liftObservationForRequester(
  observedSubject: string,
  expectedPrincipalId: string,
): boolean {
  if (!expectedPrincipalId) return false;
  return observedSubject === expectedPrincipalId;
}

function detailList(detail: Record<string, unknown>, key: string): string[] {
  const value = detail[key];
  return Array.isArray(value) ? value.map((item) => String(item)) : [];
}

function detailText(detail: Record<string, unknown>, key: string): string {
  const value = detail[key];
  return value === undefined || value === null ? "" : String(value);
}

/**
 * 단계 상태를 사람이 읽는 한 문장으로.
 *
 * ADR-0099 이후 `human_grant_missing` 을 던지는 곳이 **한 군데**예요 — 그 이름이 정확히 한
 * 원인(⑦ grant 행이 없거나 비활성·만료)을 뜻해요. 예전에는 여섯 군데가 같은 코드를 던져서
 * 관리자가 원인을 구분할 수 없었어요(IH-127).
 */
export function layerMessage(status: ChainLayerStatus): string {
  const { reason, detail } = status;
  switch (reason) {
    case "":
      return "충족됐어요.";
    // 목록 응답의 ⑦층은 항상 이 사유예요(`access_router.py:3185-3191`). 목록에서는 소비자
    // 경로 Query 를 할 수 없어 **판정 자체를 하지 않아요** — 통과로도, 거부로도 읽히지
    // 않게 말해야 해요(ADR-0037 §4).
    case "deferred_to_row_expand":
      return "아직 확인하지 않았어요 — 통과도 거부도 아니에요. 행을 펼치면 확인해요.";
    case "binding_requested":
      return "신청이 아직 승인 대기예요. 이 화면에서 승인할 수 있어요.";
    case "binding_rejected":
      return "이 신청은 반려됐어요. 다시 신청해야 해요.";
    case "binding_revoked":
      return "회수 요청된 binding 이에요. Agent × Tool 화면에서 다시 허용해 주세요.";
    case "duplicate_gateway_action": {
      const versions = detailList(detail, "assetVersions").join(", ");
      return (
        `같은 Gateway 도구 이름을 가리키는 승인 행이 ${detailText(detail, "approvedRowCount")}개예요` +
        `(자산 버전 ${versions}). 강제 지점은 이 경우 양쪽 다 거부해요 — 옛 버전 binding 을 회수해 주세요.`
      );
    }
    // ④ 안으로 들어온 버전 대조 (ADR-0099 결정 13). 대조 상대는 원장의 «자산 현재 버전»
    // 행이라, 승인 흐름이 만든 값이 아니에요 — 기대값의 소유자가 등록 흐름이에요.
    case "asset_version_mismatch":
      return (
        `이 자산은 v${detailText(detail, "currentAssetVersion")} 로 올라갔는데 이 승인은 ` +
        `v${detailText(detail, "bindingAssetVersion")} 을 가리켜요. ` +
        "행을 더 만들어도 낫지 않아요 — 재등록은 승인을 물려받지 않으니(ADR-0090) " +
        "현재 버전으로 다시 신청해야 해요."
      );
    case "asset_version_unknown":
      return (
        "이 자산의 현재 버전을 관측하지 못했어요 — 통과가 아니라 회색이에요(ADR-0037 §4). " +
        "버전 행이 채워질 때까지 승인하지 않아요."
      );
    case "human_grant_missing":
      return "부여 대상이 이 도구를 부를 권한이 없어요. 이 화면에서 부여할 수 있어요.";
    case "subject_groups_unobserved":
      return "신청자의 그룹을 확인할 수 없어서 그룹 권한을 판정하지 못했어요. 통과로 볼 수 없어요.";
    case "requester_not_in_directory":
      return `신청자 «${detailText(detail, "principalId")}» 는 사용자 디렉토리에 없어요 (시스템이 만든 신청이거나 삭제된 계정이에요). 권한을 받을 사람을 직접 골라 주세요.`;
    default:
      return reason;
  }
}

/** 관리자가 다음에 할 수 있는 조작. `none` 이면 이 화면에서 할 게 없어요. */
export type ChainAction =
  /** 한 번에 binding 승인 + 사람·그룹 권한까지 (`POST .../approve-chain`) */
  | "approve_chain"
  | "grant_human"
  | "rerequest"
  /** 자산의 현재 버전을 관측하지 못했어요 — 통과도 거부도 아니라 승인을 열지 않아요 */
  | "version_unknown"
  /** 자산을 찾았지만 현재 APPROVED 상태가 아니어서 공유 정책 쓰기를 열지 않아요 */
  | "asset_not_approved"
  /** Registry 자체를 관측하지 못해 존재·승인 여부를 판정할 수 없어요 */
  | "asset_observation_unknown"
  | "revoke_orphan"
  | "none";

/**
 * 첫 미충족 단계에서 다음 조작을 정해요. 강제 지점도 위에서 아래로 보므로 순서가 같아요.
 *
 * 층이 둘이라 판단이 얇아요 — ④가 막혔으면 승인, ④가 차 있으면 ⑦ 부여예요. 서버가 순서를
 * 잡고 첫 실패에서 멈춰요.
 *
 * 「고쳐야 하는데 이 화면 밖」인 경우를 `rerequest`·`version_unknown` 으로 갈라요 — 버튼을
 * 눌러도 안 되는 상태를 버튼으로 보여주면 관리자가 같은 실패를 반복해요.
 *
 * ⚠️ 예전에 있던 `!row.recommendedConnectionId` 가드를 지웠어요. 서버가 그 필드를 더 이상
 * 보내지 않으니(ADR-0099) 값이 항상 falsy 라 승인 버튼이 **영구히** 막혀요. 같은 이유로
 * `sensitivityStatus` 가드도 지웠어요 — 민감도가 인가 입력이 아니고(결정 5), 태그가 없는
 * 도구는 이제 정상이에요.
 */
export function nextAction(row: AuthorizationRequestRow): ChainAction {
  // unknown은 absence도 미승인도 아니에요. 둘 중 하나로 접으면 일시 Registry 장애가
  // 수동 원장 삭제나 잘못된 승인 처방의 근거가 돼요.
  if (row.assetObservation !== "observed") return "asset_observation_unknown";
  // 자산이 Registry 에 없으면 어떤 단계도 채울 수 없어요 — 승인이 `mcp_descriptor_unavailable`
  // 로 409 예요. 현재 회수 API도 자산·agent 조회에서 404라 이 화면에서 정리할 수 없어요.
  // 버튼을 주거나 Agent × Tool 화면으로 보내면 관리자가 같은 실패를 반복해요.
  if (!row.assetFound) return "revoke_orphan";
  // `false` 는 여러 Registry 상태를 뭉친 값이에요. 상태를 추측하지 않고, 현재 APPROVED가
  // 아니라는 확정 사실만으로 공유 Cedar 정책을 갱신하는 승인 조작을 닫아요.
  if (!row.assetApproved) return "asset_not_approved";

  const binding = stepFor(row.steps, "tool_binding");

  if (binding !== undefined && binding.state !== "SATISFIED") {
    // 관측하지 못한 버전은 통과도 거부도 아니에요 — 승인 버튼을 열면 못 본 것을 통과로
    // 취급하는 거예요(ADR-0037 §4).
    if (binding.reason === "asset_version_unknown") return "version_unknown";
    // 버전이 어긋난 승인은 이 화면에서 고칠 수 없어요. 재등록이 승인을 물려받지 않으니
    // (ADR-0090) 신청을 다시 해야 해요.
    if (binding.reason === "asset_version_mismatch") return "rerequest";
    if (binding.reason !== "binding_requested") return "rerequest";
    return "approve_chain";
  }

  // `upstreamReady` 는 서버가 ④층에서 만들어요(ADR-0097). 두 신호가 어긋나면 사슬을 제대로
  // 못 읽은 상태라, 그때 부여를 권하지 않아요 — 열어 놓고 "왜 여전히 막히지" 를 반복하는 게
  // IH-127 이 없애려는 증상이에요.
  if (!row.upstreamReady) return "none";
  const grant = stepFor(row.steps, "human_grant");
  // 목록의 ⑦층은 `UNKNOWN`(deferred)이라 여기서 대개 `grant_human` 이에요. 이미 있는
  // grant 를 다시 부여해도 서버가 `unchanged` 로 받아요 — 헛클릭이 원장을 늘리지 않아요.
  return grant !== undefined && grant.state === "SATISFIED" ? "none" : "grant_human";
}

export const ACTION_LABELS: Record<ChainAction, string> = {
  approve_chain: "승인하고 권한 부여",
  grant_human: "권한 부여",
  rerequest: "현재 버전으로 다시 신청해야 해요",
  version_unknown: "자산의 현재 버전을 확인하지 못해서 승인할 수 없어요",
  asset_not_approved:
    "이 자산은 현재 APPROVED 상태가 아니어서 승인할 수 없어요. 생성·갱신 같은 과도 상태일 수도 있으니 자산이 승인된 뒤 다시 확인해 주세요",
  asset_observation_unknown:
    "Registry에서 자산의 존재·승인 상태를 관측하지 못했어요. 부재나 미승인으로 판정하지 않고, 조회가 복구된 뒤 다시 확인해 주세요",
  revoke_orphan:
    "이 신청이 가리키는 MCP 자산을 Registry에서 찾지 못했어요. 이 화면에서는 정리할 수 없으니 운영자가 원장을 확인해 직접 정리해야 해요",
  none: "이 화면에서 할 조작이 없어요",
};

/** 이 화면에서 바로 누를 수 있는 조작인지. 나머지는 안내 문구로만 보여줘요. */
export function isActionable(action: ChainAction): boolean {
  return action === "approve_chain" || action === "grant_human";
}

/** 신청자를 사람이 알아볼 값으로. 디렉토리를 못 읽었으면 sub 를 보여줘요. */
export function requesterLabel(row: AuthorizationRequestRow): string {
  return row.requestedByEmail || row.requestedByName || row.requestedBy || "(알 수 없음)";
}

/**
 * ⑦층을 채울 기본 대상.
 *
 * 기본은 신청자예요 — 그 사람이 쓰겠다고 신청했으니까요. 다만 신청자가 디렉토리에 없으면
 * (baseline binding 의 `system:readonly-baseline`) 기본값으로 쓸 수 없어요. 그 값으로
 * grant 를 만들면 서버가 `principal_unverifiable` 로 막지만(`access_router.py:3526-3535`),
 * 화면이 그걸 기본값으로 보여주면 관리자는 «부여» 를 누르고 실패를 받아요. 그래서 골라야
 * 한다고 먼저 말해요.
 */
export function defaultGrantPrincipal(row: AuthorizationRequestRow): {
  principalId: string;
  label: string;
  mustPick: boolean;
} {
  if (row.requesterObservation !== "RESOLVED" || !row.requestedBy) {
    return {
      principalId: "",
      label: "선택하지 않았어요",
      mustPick: true,
    };
  }
  return {
    principalId: row.requestedBy,
    label: requesterLabel(row),
    mustPick: false,
  };
}

/** 자산을 이름 + 버전으로. Registry 에서 못 읽었으면 그렇게 말해요. */
export function assetLabel(row: AuthorizationRequestRow): string {
  if (row.assetObservation !== "observed") {
    return `${row.assetId} (Registry를 관측하지 못했어요)`;
  }
  if (row.assetFound === false) return `${row.assetId} (Registry 에 없어요)`;
  return `${row.assetName} v${row.assetVersion}`;
}

/**
 * agent 를 한 줄로 — 이름을 못 읽었으면 **부재로 단정하지 않아요.**
 *
 * `agentFound:false` 는 「Registry 에 없다」가 아니에요. 서버는 이 행을 bulk sweep 에서 못
 * 찾았을 때도(선택 status 목록 누락 = exact absence 아님) 같은 값으로 접어요
 * (`access_router._AuthorizationRequestContext.record`, `exact=False` 분기). 미관측을 부재로
 * 적으면 그게 원장 직접 정리 같은 처방의 근거가 돼요(ADR-0111 결정 2).
 */
export function agentLabel(row: AuthorizationRequestRow): string {
  if (!row.agentFound || !row.agentName) {
    return `${row.agentId} (Registry 에서 이름을 확인하지 못했어요)`;
  }
  return row.agentVersion ? `${row.agentName} v${row.agentVersion}` : row.agentName;
}

/**
 * 목록 «대상 agent» 칸에 그릴 값. **출처가 둘이에요.**
 *
 * - `agentId` 는 **인가 원장**(`AgentToolBinding.agent_record_id`)이라 늘 있어요. 이게 ④ 행의
 *   주체 그 자체라서, 이름을 못 읽어도 「어느 agent 의 신청인가」는 답할 수 있어요.
 * - `agentName`·`agentVersion` 은 **Registry** 조회 결과예요. 못 읽으면 서버가 `agentFound`
 *   `false` 로 접는데, 그 값은 부재 확정이 아니라 «부재 또는 미관측» 이에요.
 *
 * 정렬도 이 값을 써요 — 화면에 보이는 값과 정렬 기준이 갈라지면 관리자가 「이름순인데 왜 이
 * 행이 여기 있나」를 풀 수 없어요.
 */
export function agentColumnLabel(row: AuthorizationRequestRow): string {
  return row.agentFound && row.agentName ? row.agentName : row.agentId;
}

/** 그 칸의 작은 글씨 — 값의 출처를 밝혀요. 미관측을 「없어요」로 접지 않아요. */
export function agentColumnNote(row: AuthorizationRequestRow): string {
  if (row.agentFound && row.agentName) {
    return row.agentVersion ? `v${row.agentVersion}` : "";
  }
  return "Registry 에서 이름을 확인하지 못했어요";
}

/**
 * 폭발 반경 문구를 쓸 대상. 전송용 주체(`api/authorizationRequests.ts` 의 `GrantSubject`)와
 * 따로 두는 이유는 이쪽이 **문구에 필요한 값**만 받기 때문이에요 — 그룹 이름은 사람이 읽는
 * 라벨(«전 회원»)이고, 멤버 수는 Cognito 디렉토리 조회 결과라 인가 응답에 없어요.
 * 순수 함수가 그걸 알 방법이 없으니 화면이 세어서 넘겨요.
 */
export type BlastRadiusSubject =
  | { kind: "principal" }
  | { kind: "group"; groupLabel: string; memberCount: number };

/**
 * 이 grant 가 닿는 범위를 한 문장으로 — **ADR-0097 결정 3′** 의 폭발 반경이에요.
 *
 * ADR-0099 로 자산 축의 반경이 사라졌어요. grant 키가 `(주체, asset_id, operation_id)` 라
 * 행 하나가 정확히 **도구 하나**를 열어요 — 옛 capability 라벨처럼 「같은 그룹을 쓰는 모든
 * 자산」으로 번지지 않아요(그게 IH-130 의 원인이었어요).
 *
 * 남는 반경은 **사람 축**이고, 그룹 부여에서만 넓어져요 — 지금 멤버 + 앞으로 들어올 사람.
 * 대신 회수가 자동이에요: 강제 지점이 보는 그룹은 delegation handle 안에 있고 TTL 이 최대
 * 900초라(`delegation.py:63`, `models.py:285`) 그룹에서 빼면 그 안에 막혀요. 사람 grant 는
 * 그 자동 회수가 없는 대신 회수가 **즉시** 반영돼요(interceptor 가 매 호출 `ConsistentRead`).
 *
 * `toolLabel` 이 비면 빈 문자열을 돌려줘요 — 무엇을 여는지 모르는 채로 「부여해요」라고 말할
 * 수는 없어요.
 */
export function blastRadiusMessage(
  toolLabel: string,
  subject: BlastRadiusSubject = { kind: "principal" },
): string {
  if (!toolLabel) return "";
  const parts: string[] = [];
  if (subject.kind === "group") {
    parts.push(`«${toolLabel}» 도구를 «${subject.groupLabel}» 그룹에 열어요.`);
    parts.push(
      subject.memberCount > 0
        ? `지금 이 그룹에 있는 ${subject.memberCount}명과, 앞으로 이 그룹에 들어오는 모든 사람이 부를 수 있어요.`
        : "지금 이 그룹에 있는 사람은 없지만, 앞으로 이 그룹에 들어오는 모든 사람이 부를 수 있어요.",
    );
  } else {
    parts.push(`«${toolLabel}» 도구를 이 사람 한 명에게 열어요.`);
  }
  parts.push("이 권한은 이 도구 하나에만 닿아요 — 다른 도구는 열리지 않아요.");
  parts.push(
    subject.kind === "group"
      ? "회수는 자동이에요 — 그룹에서 빼면 늦어도 900초 뒤 호출부터 막혀요(delegation TTL ≤ 900초)."
      : "회수하면 다음 호출부터 바로 막혀요.",
  );
  return parts.join(" ");
}

// ── 정렬 ───────────────────────────────────────────────────────────────────
//
// 상태 모양은 새 관례를 따라요: `{ key, dir }` (`components/admin/queue/QueueClient.tsx:57`).

export type SortKey =
  | "agentName"
  | "assetName"
  | "operationId"
  | "requestJustification"
  | "requestedByName"
  | "requestedByEmail"
  | "requestedAt"
  | "approvedAt"
  | "progress";

export type SortState = { key: SortKey; dir: "asc" | "desc" };

/**
 * 표 헤더 — 정렬 키가 있는 칼럼만 클릭 가능해요. 마지막 칸은 버튼 자리라 라벨이 없어요.
 *
 * 컴포넌트가 아니라 여기 두는 이유는 테스트예요. web 테스트 러너는 `.tsx` 를 변환하지 못해서
 * (`web/package.json` 의 `test` 스크립트에 `.tsx` 항목이 0개예요) 컴포넌트 안에 있는 표 정의는
 * 「이 칼럼이 사라지면 빨개진다」를 단정할 수 없어요.
 *
 * 첫 칸이 **대상 agent** 예요. 목록이 답해야 하는 문장이 「어느 agent 가 어느 MCP 의 어느
 * 도구를 신청했나」라서, agent 가 없으면 관리자는 무엇을 승인하는지 알 수 없어요.
 *
 * ⚠️ 칸을 더하거나 빼면 펼침 행의 `colSpan` 도 같이 움직여요 — 화면은 그 값을 상수로 쓰지 않고
 * `AUTHORIZATION_COLUMNS.length` 로 읽어요.
 */
export const AUTHORIZATION_COLUMNS: {
  key: SortKey | null;
  label: string;
}[] = [
  { key: "agentName", label: "대상 agent" },
  { key: "assetName", label: "MCP" },
  { key: "operationId", label: "도구" },
  { key: "requestJustification", label: "신청 사유" },
  { key: "requestedByName", label: "신청자" },
  { key: "requestedByEmail", label: "이메일" },
  { key: "requestedAt", label: "신청일" },
  { key: "approvedAt", label: "승인일" },
  { key: "progress", label: "진행" },
  { key: null, label: "" },
];

/**
 * 첫 화면 정렬 — 덜 채워진 사슬이 위로 와요.
 *
 * `sortRequests`(할 일 먼저)와 겹쳐 보이지만 역할이 달라요. `sortRequests` 는 **동점일 때의
 * 기준 순서**로 남아요(`sortRows` 가 정렬 전에 한 번 적용해요). `progress` asc 는 그 첫
 * 기준(못 부르는 것 먼저)과 같은 방향이라 초기 화면이 예전과 같고, 관리자가 다른 칼럼으로
 * 정렬해도 동점 행들은 여전히 「할 일 먼저 → REQUESTED → 이름」 순서로 남아요.
 */
export const DEFAULT_SORT: SortState = { key: "progress", dir: "asc" };

/** 헤더 클릭: 같은 열이면 방향 토글, 다른 열이면 그 열 오름차순부터 (QueueClient.tsx:85-86). */
export function toggleSort(current: SortState, key: SortKey): SortState {
  if (current.key === key) {
    return { key, dir: current.dir === "asc" ? "desc" : "asc" };
  }
  return { key, dir: "asc" };
}

/**
 * 진행 랭크 — 단계 상태를 **문자열로** 정렬하면 의미가 없어요.
 *
 * 코드유닛 순서로는 `BLOCKED < MISSING < SATISFIED < UNKNOWN` 이라 "가장 잘 채워진 행"이
 * 중간에 오고, 관측하지 못한 `UNKNOWN` 이 맨 끝(= 제일 진행된 것처럼)에 와요. 그래서
 * 채워진 칸 수로 랭크해요.
 *
 * `SATISFIED` 만 1 이에요. `UNKNOWN` 을 세면 목록의 ⑦층(항상 `deferred_to_row_expand`)이
 * 전부 완료로 보여요 — 그게 ADR-0037 §4 가 금지하는 위장이에요. 칸이 둘로 줄어서 한 칸의
 * 무게가 커졌으니 더 그래요.
 */
const STEP_FILL_RANK: Record<ChainLayerState, number> = {
  SATISFIED: 1,
  MISSING: 0,
  BLOCKED: 0,
  UNKNOWN: 0,
};

/** 채워진 단계 수(0~2). 진행선 라벨과 `progress` 정렬이 같은 값을 써요. */
export function progressFilled(row: AuthorizationRequestRow): number {
  return row.steps.reduce((sum, step) => sum + (STEP_FILL_RANK[step.state] ?? 0), 0);
}

/** 한국어가 섞인 칼럼. `localeCompare(…, "ko")` 로 비교해요(InventoryClient.tsx:503). */
const TEXT_SORT_VALUES: Record<
  Exclude<SortKey, "progress" | "requestedAt" | "approvedAt">,
  (row: AuthorizationRequestRow) => string
> = {
  // 화면에 그리는 값과 같은 함수를 써요 — 두 자리가 갈라지면 정렬이 보이지 않는 값을 기준으로
  // 돌아요(이름을 못 읽은 행이 `""` 로 정렬되면서 id 를 보여주는 그 행만 맨 끝으로 밀렸어요).
  agentName: (row) => agentColumnLabel(row),
  assetName: (row) => row.assetName,
  operationId: (row) => row.operationId,
  requestJustification: (row) => row.requestJustification,
  requestedByName: (row) => row.requestedByName,
  requestedByEmail: (row) => row.requestedByEmail,
};

/** ISO8601 고정 폭 문자열이라 코드유닛 순서가 곧 시간순이에요 — 서버도 같은 비교로
 * 가장 이른 신청을 골라요(`access_router.py:2726`). 그래서 파싱하지 않아요. */
const DATE_SORT_VALUES: Record<
  "requestedAt" | "approvedAt",
  (row: AuthorizationRequestRow) => string
> = {
  requestedAt: (row) => row.requestedAt,
  approvedAt: (row) => row.approvedAt,
};

function compareRows(
  left: AuthorizationRequestRow,
  right: AuthorizationRequestRow,
  sort: SortState,
): number {
  if (sort.key === "progress") {
    const cmp = progressFilled(left) - progressFilled(right);
    return sort.dir === "asc" ? cmp : -cmp;
  }
  const read =
    sort.key === "requestedAt" || sort.key === "approvedAt"
      ? DATE_SORT_VALUES[sort.key]
      : TEXT_SORT_VALUES[sort.key];
  const leftValue = read(left);
  const rightValue = read(right);
  // 빈 값은 «없는 값» 이에요 — 신청일 `""` 는 이 기능 이전에 만들어진 행이고
  // (`access_router.py:3031-3036`), 승인일 `""` 는 아직 승인 안 된 행이에요. 방향을 뒤집었을
  // 때 그게 «가장 이른 날짜» 로 맨 위에 오면 관리자가 빈 칸을 데이터로 읽어요. 그래서
  // 오름·내림 어느 쪽이든 항상 끝으로 밀어요.
  if (!leftValue || !rightValue) {
    if (leftValue === rightValue) return 0;
    return leftValue ? -1 : 1;
  }
  const cmp =
    sort.key === "requestedAt" || sort.key === "approvedAt"
      ? leftValue < rightValue
        ? -1
        : leftValue > rightValue
          ? 1
          : 0
      : // raw `<`/`>` 는 UTF-16 코드유닛 순서라 한글 이름이 엉켜요.
        leftValue.localeCompare(rightValue, "ko");
  return sort.dir === "asc" ? cmp : -cmp;
}

/**
 * 관리자가 고른 칼럼으로 정렬해요.
 *
 * 정렬 전에 `sortRequests` 를 한 번 적용해요. `Array.prototype.sort` 는 ES2019 부터
 * stable 이라, 같은 값(예: 정당성 문구가 둘 다 빈 칸)인 행들이 「할 일 먼저」 순서를 그대로
 * 유지해요 — 그 기준이 없으면 동점 행 순서가 서버 응답 순서(원장 scan 순)로 흔들려요.
 */
export function sortRows(
  rows: AuthorizationRequestRow[],
  sort: SortState = DEFAULT_SORT,
): AuthorizationRequestRow[] {
  const base = sortRequests(rows);
  base.sort((left, right) => compareRows(left, right, sort));
  return base;
}

/**
 * 동점일 때의 기준 순서 — ① 아직 못 부르는 것(할 일), ② 그다음 REQUESTED, ③ 그다음
 * 자산·operation 이름.
 *
 * `sortRows` 의 기반으로만 쓰고 화면이 직접 부르지 않아요. 남겨 두는 이유는 위 stable sort
 * 기준이고, `upstreamReady` 인 행은 `include=all` 에서만 오니 맨 뒤로 밀려요.
 */
export function sortRequests(
  rows: AuthorizationRequestRow[],
): AuthorizationRequestRow[] {
  return [...rows].sort((left, right) => {
    if (left.upstreamReady !== right.upstreamReady) return left.upstreamReady ? 1 : -1;
    const leftRequested = left.approvalState === "REQUESTED" ? 0 : 1;
    const rightRequested = right.approvalState === "REQUESTED" ? 0 : 1;
    if (leftRequested !== rightRequested) return leftRequested - rightRequested;
    return (
      left.assetName.localeCompare(right.assetName, "ko") ||
      left.operationId.localeCompare(right.operationId, "ko")
    );
  });
}

// ── 1클릭 결과 ─────────────────────────────────────────────────────────────

// `ChainApprovalResult`·`ChainApprovalStep` 은 전송 모양이라 api 모듈이 갖고 있어요
// (`api/authorizationRequests.ts:296-317`). 여기서는 아래 지도를 `Record<string, …>` 로 둬서
// 서버가 단계나 outcome 을 하나 더 추가해도 문구가 빈 칸이 되지 않게 해요 — 지도에 없는
// 값은 코드 그대로 보여줘요(`layerMessage` 의 default 절과 같은 태도예요).

/** 1클릭 응답의 단계 이름 → 진행선 이름. `CHAIN_LAYERS` 와 같은 짧은 이름을 써요. */
const APPROVAL_STEP_TITLES: Record<string, string> = {
  tool_binding: "binding",
  human_grant: "사람 권한",
};

const OUTCOME_LABELS: Record<string, string> = {
  approved: "승인",
  created: "생성",
  reactivated: "되살림",
  unchanged: "그대로",
  failed: "실패",
};

function stepTitle(step: string): string {
  return APPROVAL_STEP_TITLES[step] ?? step;
}

/** 실패 단계의 `detail` 에서 사람이 읽을 사유를 뽑아요. 서버는 dict 또는 문자열을 실어요
 * (`HTTPException(409, {...})` 와 `HTTPException(404, "…")` 가 섞여 있어요). */
function failureText(detail: Record<string, unknown>): string {
  const body = detail.detail;
  if (typeof body === "string") return body;
  if (body !== null && typeof body === "object") {
    const record = body as Record<string, unknown>;
    const message = record.message === undefined ? "" : String(record.message);
    const reason = record.reason === undefined ? "" : String(record.reason);
    if (message && reason) return `${message} (${reason})`;
    return message || reason;
  }
  return "";
}

function failureBody(
  detail: Record<string, unknown>,
): Record<string, unknown> | undefined {
  const body = detail.detail;
  return body !== null && typeof body === "object"
    ? (body as Record<string, unknown>)
    : undefined;
}

function isSharedPolicyProvisioning(result: ChainApprovalResult): boolean {
  return result.steps.some((step) => {
    if (step.outcome !== "failed") return false;
    const body = failureBody(step.detail);
    const report = body?.shared_policy_provisioning;
    return (
      report !== null &&
      typeof report === "object" &&
      String((report as Record<string, unknown>).verdict ?? "") === "provisioning"
    );
  });
}

export type ChainApprovalTone = "ok" | "warn" | "pending";

/** 미완료 중에서도 정책 활성화 대기는 장애 경고와 다른 중립 진행 상태예요. */
export function chainApprovalTone(result: ChainApprovalResult): ChainApprovalTone {
  if (result.completed) return "ok";
  return isSharedPolicyProvisioning(result) ? "pending" : "warn";
}

function grantSubjectText(detail: Record<string, unknown>): string {
  const subject = detail.subject;
  if (subject === null || typeof subject !== "object") return "";
  const record = subject as Record<string, unknown>;
  const id = record.id === undefined ? "" : String(record.id);
  if (!id) return "";
  return record.kind === "group" ? `«${id}» 그룹` : id;
}

/**
 * 1클릭 결과를 한 문단으로.
 *
 * 실패했을 때 **어느 단계에서 멈췄는지**가 핵심이에요. 서버는 첫 실패에서 멈추고 200 으로
 * 돌려주니(`access_router.py:3820-3826`) 앞 단계는 원장에 남아 있어요 — 다시 누르면 남은
 * 것만 해요(멱등). 그 사실을 문구에 담지 않으면 관리자가 처음부터 다시 하려고 해요.
 */
export function chainApprovalMessage(result: ChainApprovalResult): string {
  if (isSharedPolicyProvisioning(result)) {
    return (
      "공유 Gateway 정책이 활성화 중이에요. 원장 변경과 새 리비전 생성은 저장됐지만 " +
      "아직 완료로 표시하지 않아요. 잠시 뒤 다시 눌러 주세요 — 현재 상태를 다시 관측해 " +
      "남은 단계만 이어서 해요."
    );
  }
  const failed = result.steps.find((step) => step.outcome === "failed");
  if (failed) {
    const why = failureText(failed.detail);
    const status = failed.detail.status === undefined ? "" : String(failed.detail.status);
    const done = result.steps
      .filter((step) => step.outcome !== "failed")
      .map((step) => `${stepTitle(step.step)} ${OUTCOME_LABELS[step.outcome] ?? step.outcome}`);
    return (
      `«${stepTitle(failed.step)}» 단계에서 멈췄어요` +
      (status ? ` (HTTP ${status})` : "") +
      (why ? `: ${why}` : ".") +
      (done.length > 0 ? ` 여기까지는 됐어요 — ${done.join(", ")}.` : "") +
      " 원인을 고치고 다시 누르면 남은 단계만 이어서 해요."
    );
  }
  if (result.steps.length === 0) {
    // 서버가 단계를 하나도 기록하지 않았어요 — 「완료」로 말하면 안 되는 상태예요.
    return result.completed
      ? "끝났다고 했지만 단계 기록이 비어 있어요. 표를 새로 불러 확인해 주세요."
      : "단계를 하나도 시작하지 못했어요. 표를 새로 불러 확인해 주세요.";
  }
  const done = result.steps
    .map((step) => `${stepTitle(step.step)} ${OUTCOME_LABELS[step.outcome] ?? step.outcome}`)
    .join(", ");
  const grant = result.steps.find((step) => step.step === "human_grant");
  const subject = grant ? grantSubjectText(grant.detail) : "";
  // 「몇 개가 함께 열렸나」를 더 말하지 않아요 — grant 하나가 도구 하나예요(ADR-0099 결정 2).
  const radius = subject ? ` 사람 권한은 ${subject}에 이 도구 하나만 열었어요.` : "";
  // 「사슬을 닫았어요」였어요. 「사슬」은 ADR·백로그의 내부 좌표라 관리자에게는 모르는 말이에요
  // (진행선에서 ④⑦ 번호를 뺀 것과 같은 이유). 문서·주석에서는 그대로 쓰고 화면 문구만 바꿔요.
  return `승인이 완료됐어요 (${done}).${radius}`;
}
