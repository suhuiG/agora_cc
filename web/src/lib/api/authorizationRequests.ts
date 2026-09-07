import { JSON_HEADERS, request } from "./client";
import type { AgentToolBindingApproval } from "./identity";
import type { SensitivitySource } from "../sensitivitySource";

// ---------------------------------------------------------------------------
// 통합 인가 승인 — 도구 신청 하나의 인가 **두 층**(④⑦)을 읽고 채워요 (ADR-0099).
//
// 2026-08-31 에 층이 넷에서 둘로 줄었어요. `asset_capability`(⑤) 와 `connection`(⑥) 은
// 인가 경로에서 사라졌어요 — capability 라벨과 ceiling 이 「이 사람이 이 도구를 쓸 수 있나」를
// 중간 화폐로 세 번 환전하던 구조였고, 그게 IH-127(초록불인데 거부)·IH-130(승인된 DELETE
// 도구가 사라짐)의 공통 원인이었어요. 지금은 ④가 `(asset_id, asset_version, operation_id)` 를
// 확정하고, ⑦이 그 `(asset_id, operation_id)` 로 호출자를 확인해요.
//
// 왜 전용 엔드포인트인가: ⑦층(사람 grant) 판정은 interceptor 가 읽는 파티션으로 읽어야
// 맞아요. 클라이언트가 `GET /api/admin/access-grants`(필터 없으면 전체 scan)로 대신하면,
// 잘못된 파티션의 행이 "있다" 로 보이는 2026-08-29 사고가 화면에서 재현돼요. 그래서 조인은
// 서버가 하고 이 모듈은 결과만 받아요.
//
// 읽기 경로가 **두 단계**예요 — 목록(`listAuthorizationRequests`)은 ④층만 확정하고, 행을
// 펼칠 때 단건(`diagnoseAuthorizationRequest`)이 ⑦층까지 확정해요. 목록에서 ⑦층까지 보려면
// 행마다 사람별 Query + Cognito 그룹 조회가 붙는데 그걸 벌크로 읽을 IAM action 이 백엔드
// role 에 없어요(`infra/lib/identity-stack.ts:166-182`).
// ---------------------------------------------------------------------------

/**
 * 인가 사슬의 층 — **둘뿐이에요** (ADR-0099 결정 1).
 *
 * `"asset_capability"`·`"connection"` 은 더 이상 오지 않아요. 서버가 그 이름을 다시 보내기
 * 시작하면 화면은 그 칸을 그리지 않고 조용히 넘겨요 — 이름으로 층을 찾으니까요
 * (`lib/authorizationChain.ts` 의 `stepFor`).
 */
export type ChainLayerName =
  /** ④ agent tool binding — 신청이 만드는 유일한 층. 자산 버전 대조도 여기서 해요(결정 13) */
  | "tool_binding"
  /** ⑦ `(주체, asset_id, operation_id)` grant */
  | "human_grant";

/**
 * 층의 상태.
 *
 * `UNKNOWN` 은 **통과가 아니에요** — 관측하지 못한 상태예요(ADR-0037 §4). 초록불로 그리면
 * 안 되고, 그렇다고 거부 근거로 써도 안 돼요.
 */
export type ChainLayerState = "SATISFIED" | "MISSING" | "BLOCKED" | "UNKNOWN";

export type ChainLayerStatus = {
  layer: ChainLayerName;
  state: ChainLayerState;
  /** 기계가 분기할 코드. 사람이 읽는 문구는 `lib/authorizationChain.ts` 가 만들어요. */
  reason: string;
  detail: Record<string, unknown>;
};

/** Drift 원장에서 민감도를 실제로 읽었는지. `unknown` 이면 파생 근거가 없어요. */
export type SensitivityStatus = "observed" | "unknown";

/** Registry 자산 레코드의 존재·승인 상태를 실제로 읽었는지. */
export type AssetObservation = "observed" | "unknown";

/**
 * 신청자를 디렉토리에서 확인했는지.
 *
 * `NOT_IN_DIRECTORY` 는 조회 실패가 아니에요 — 그 주체가 사람이 아니라는 뜻이에요
 * (예: baseline binding 의 `system:readonly-baseline`). 그때 ⑦층은 «권한 없음» 이 아니라
 * «부여 대상을 골라야 함» 이에요. `UNOBSERVED` 는 디렉토리 자체를 못 읽은 거라 그 둘과
 * 또 달라요 — 통과로도 거부로도 쓸 수 없어요.
 */
export type RequesterObservation = "RESOLVED" | "NOT_IN_DIRECTORY" | "UNOBSERVED";

export type AuthorizationRequestRow = {
  agentId: string;
  agentName: string;
  agentVersion: string;
  agentFound: boolean;
  assetId: string;
  assetName: string;
  assetObservation: AssetObservation;
  /** unknown이면 absence로 접지 않고 null이에요. */
  assetFound: boolean | null;
  /** unknown이면 미승인으로 접지 않고 null이에요. */
  assetApproved: boolean | null;
  /**
   * binding 이 가리키는 버전. 아래 `assetCurrentVersion` 과 다르면 ④층이 거부예요 —
   * 대조 상대가 원장의 «자산 현재 버전» 행이에요(ADR-0099 결정 13).
   */
  assetVersion: string;
  assetCurrentVersion: string;
  operationId: string;
  gatewayAction: string;
  /** Drift 원장 관측값(`""` = 미태깅). `sensitivityStatus` 가 "unknown" 이면 못 읽은 거예요. */
  sensitivity: string;
  sensitivityStatus: SensitivityStatus;
  sensitivityReason: string;
  /**
   * Drift 원장이 기록한 출처. `sensitivityStatus="observed"` + `"unknown"`은
   * legacy 행의 출처 기록이 없다는 뜻이고, status 자체가 unknown이면 원장을 못 읽었어요.
   */
  sensitivitySource: SensitivitySource;
  approvalState: "REQUESTED" | "APPROVED" | "REJECTED";
  effectiveState: string;
  requestedBy: string;
  /** 디렉토리에서 읽은 표시 이름. 못 읽으면 `""` — 「사람이 아님」과 구분해야 해요. */
  requestedByName: string;
  requestedByEmail: string;
  requesterObservation: RequesterObservation;
  /**
   * 신청 시각(ISO8601). `""` 는 «없음» 이 아니라 **이 기능 이전에 만들어진 행**이에요 —
   * 신청일이 audit 에서 오는데 그때는 그 이벤트를 남기지 않았어요. 화면은 «오래됨» 이 아니라
   * 빈칸으로 그려야 해요.
   */
  requestedAt: string;
  /** 승인 시각(ISO8601). 아직 승인 전이면 `""` 예요. */
  approvedAt: string;
  requestJustification: string;
  /**
   * ④층(`steps` 의 `tool_binding`)이 SATISFIED.
   *
   * 사슬 전체가 통과한다는 뜻이 **아니에요** — 목록은 ⑦층을 확정하지 않아요. 「부를 수 있다」를
   * 이 값으로 말하면 ADR-0037 §4 가 금지한 위장이 돼요. 사슬 전체는 단건 진단의
   * `AuthorizationRequestDiagnosis.ready` 만 답할 수 있어요.
   */
  upstreamReady: boolean;
  /**
   * 항상 길이 2, ④⑦ 순서예요 (ADR-0099).
   *
   * `human_grant` 는 목록에서 항상 `state:"UNKNOWN"` 이고 reason 은
   * `"deferred_to_row_expand"`(앞 단계가 막혀 있으면 그 reason)예요. 행을 펼쳐
   * `diagnoseAuthorizationRequest` 를 부를 때 확정돼요.
   *
   * **순서·길이를 가정하지 마세요.** 층을 고를 때는 항상 `layer` 이름으로 찾아요 — 칸 수가
   * 넷에서 둘로 줄었을 때 `steps[2]` 같은 인덱스 접근이 조용히 다른 층을 집었어요.
   */
  steps: ChainLayerStatus[];
};

export type AuthorizationRequestPage = {
  requests: AuthorizationRequestRow[];
  counts: {
    totalBindings: number;
    returned: number;
    skippedReadyRead: number;
    truncated: boolean;
    limit: number;
  };
  /** false 면 이름·email 이 빈 행이 있어요 — 「사람이 아님」과 구분해야 해요. */
  directoryObserved: boolean;
};

/**
 * 신청 한 건의 사슬 **확정** 진단 — 목록이 미뤄둔 ⑦층이 여기서 채워져요.
 *
 * `steps` 는 목록과 같은 모양·같은 순서(길이 2)인데, `human_grant` 가 소비자 경로
 * (interceptor 와 같은 파티션·같은 SK 로 읽은 `(주체, asset_id, operation_id)` grant)로
 * 판정된 값이에요. 그래서 사슬 전체 통과를 말할 수 있는 건 이 응답의 `ready` 뿐이에요.
 */
export type AuthorizationRequestDiagnosis = {
  agentId: string;
  assetId: string;
  assetVersion: string;
  operationId: string;
  /**
   * 어떤 사람 기준으로 ⑦층을 봤는지. `principalId` 를 넘기지 않았으면 신청자예요.
   *
   * 부여 대상을 바꿔 미리 확인할 수 있어서, 화면이 보여주는 판정이 「누구에 대한 판정」인지
   * 이 값으로 밝혀야 해요.
   */
  subjectPrincipalId: string;
  requesterObservation: RequesterObservation;
  /** 그 사람의 Cognito 그룹. `requesterObservation` 이 `RESOLVED` 가 아니면 비어요. */
  subjectGroups: string[];
  sensitivity: string;
  sensitivityStatus: SensitivityStatus;
  sensitivityReason: string;
  sensitivitySource: SensitivitySource;
  /** 두 층이 모두 SATISFIED. 이 화면에서 「지금 부를 수 있다」를 말할 수 있는 유일한 값이에요. */
  ready: boolean;
  steps: ChainLayerStatus[];
};

// ---------------------------------------------------------------------------
// ⑦층 부여 주체
// ---------------------------------------------------------------------------

/**
 * 그룹 grant 로 쓸 수 있는 Cognito 그룹 — **`user`·`admin` 둘뿐이에요.**
 *
 * 호출 시점 판정은 delegation 행의 `principal_groups` 로 하는데, 그 값은 handle 발급 때
 * 세션 role 을 담은 것이고 `token_verifier.py:99` 가 그걸
 * `PLATFORM_ROLES`(`api/src/agora/domains/identity/models.py:10` = `("user", "admin")`)로
 * **걸러요**. 그래서 다른 그룹 이름으로 부여하면 **행은 생기는데 판정에 절대 안 잡혀요** —
 * 관리 화면은 초록불이고 호출은 계속 거부돼요. IH-127 이 고친 그 증상이라 서버가 422 로
 * 막고(`access_router.py:194-203`), 여기서는 타입으로 먼저 막아요.
 */
export const PLATFORM_GRANT_GROUPS = ["user", "admin"] as const;

export type PlatformGrantGroup = (typeof PLATFORM_GRANT_GROUPS)[number];

/** `<select>` 값처럼 `string` 으로 들어온 그룹을 좁혀요. */
export function isPlatformGrantGroup(value: string): value is PlatformGrantGroup {
  return (PLATFORM_GRANT_GROUPS as readonly string[]).includes(value);
}

/**
 * ⑦층을 받을 주체 — **사람 또는 그룹 하나**예요 (ADR-0098).
 *
 * `?: never` 로 배타를 강제해요. 둘 다 채우면 `_grant_partition`
 * (`dynamo_store.py:107-120`)이 그룹 파티션을 골라서 `principal_id` 가 **조용히 무시돼요** —
 * 특정 사람에게 줬다고 생각한 관리자의 의도가 사라져요. 서버도 422 로 막지만
 * (`access_router.py:189-193`), 그건 왕복 한 번 뒤예요. 컴파일에서 막는 게 싸요.
 */
export type GrantSubject =
  | { principalId: string; subjectGroup?: never }
  | { principalId?: never; subjectGroup: PlatformGrantGroup };

/** 두 부여 엔드포인트가 공유하는 본문. 서버는 `extra="forbid"` 라 빈 키를 보내면 안 돼요. */
function grantBody(subject: GrantSubject, expiresAt?: number): string {
  const body: Record<string, string | number> =
    subject.subjectGroup !== undefined
      ? { subject_group: subject.subjectGroup }
      : { principal_id: subject.principalId };
  if (expiresAt !== undefined) body.expires_at = expiresAt;
  return JSON.stringify(body);
}

// ---------------------------------------------------------------------------
// 단계별 응답
// ---------------------------------------------------------------------------

export type ChainAccessGrantResult = {
  /** `reactivated` 는 회수됐던 행을 되살린 거예요 — 새 행이 아니에요. */
  outcome: "created" | "unchanged" | "reactivated";
  /** `unchanged` 면 null 이에요 — 이미 통과해서 아무것도 쓰지 않았다는 뜻이에요. */
  grant: {
    grant_id: string;
    /** 그룹 grant 는 **빈 문자열**이에요(`access_router.py:3657-3660`). */
    principal_id: string;
    subject_group: string;
    asset_id: string;
    operation_id: string;
    status: string;
    expires_at: number | null;
  } | null;
  /** 실제로 부여된 주체. 요청한 주체를 서버가 정규화(trim)한 값이에요. */
  subject: { kind: "principal" | "group"; id: string };
  detail?: Record<string, unknown>;
};

/**
 * approve-chain 이 채우는 단계 — 층 이름과 같아요.
 *
 * ADR-0099 전에는 `asset_capability` 단계가 하나 더 있었어요. 그 층이 인가 경로에서
 * 사라졌으니 채울 것도 없어졌어요.
 */
export type ChainApprovalStepName = ChainLayerName;

/**
 * 단계 결과.
 *
 * `unchanged` 는 **성공**이에요 — 그 단계가 이미 끝나 있었다는 뜻이라 멱등의 근거예요.
 * 실패로 그리면 관리자가 다시 눌러요.
 */
export type ChainStepOutcome =
  | "approved"
  | "created"
  | "reactivated"
  | "unchanged"
  | "failed";

export type ChainApprovalStep = {
  step: ChainApprovalStepName;
  outcome: ChainStepOutcome;
  /**
   * `failed` 면 `{ status: number, detail: string | ApiErrorDetail }` 예요
   * (`access_router.py:3823-3826`). `human_grant` 성공이면 `subject` 가 들어 있어요.
   */
  detail: Record<string, unknown>;
};

/**
 * approve-chain 응답. **항상 HTTP 200 이에요** — 부분 실패도 200 이라 `ApiError` 로 안 와요.
 *
 * 서버가 4xx 로 올리지 않는 이유가 「어디까지 됐는지」가 본문의 핵심이기 때문이에요
 * (`access_router.py:3827-3829`). 그래서 화면은 `completed` 만 보고 «성공/실패» 를 그리면
 * 안 되고, `steps` 를 그려야 해요.
 */
export type ChainApprovalResult = {
  completed: boolean;
  steps: ChainApprovalStep[];
};

/** 사슬을 읽고 채우는 엔드포인트들이 공유하는 경로 좌표. */
export type ChainTarget = Pick<
  AuthorizationRequestRow,
  "agentId" | "assetId" | "assetVersion" | "operationId"
>;

function chainPath(target: ChainTarget, suffix: string): string {
  return (
    `/api/admin/assets/${encodeURIComponent(target.agentId)}/tool-bindings` +
    `/${encodeURIComponent(target.assetId)}` +
    `/${encodeURIComponent(target.assetVersion)}` +
    `/${encodeURIComponent(target.operationId)}/${suffix}`
  );
}

/** GET /api/admin/authorization-requests — 신청별 사슬 진단(admin). */
export function listAuthorizationRequests(
  include: "actionable" | "all" = "actionable",
): Promise<AuthorizationRequestPage> {
  return request<AuthorizationRequestPage>(
    `/api/admin/authorization-requests?include=${include}`,
  );
}

/**
 * GET /api/admin/authorization-requests/{…} — 신청 한 건의 두 층을 확정해요(admin).
 *
 * 행을 펼칠 때 불러요. 목록이 ⑦층을 `UNKNOWN` 으로 둔 걸 여기서만 채울 수 있어요.
 *
 * `principalId` 를 주면 그 사람 기준으로 봐요 — 부여 대상을 바꿔 미리 확인하는 용도예요.
 * 안 주면 신청자 기준이고, 그때 서버가 `subjectPrincipalId` 로 누구를 봤는지 알려줘요.
 */
export function diagnoseAuthorizationRequest(
  target: ChainTarget,
  principalId?: string,
): Promise<AuthorizationRequestDiagnosis> {
  const query = principalId
    ? `?principal_id=${encodeURIComponent(principalId)}`
    : "";
  return request<AuthorizationRequestDiagnosis>(
    `/api/admin/authorization-requests` +
      `/${encodeURIComponent(target.agentId)}` +
      `/${encodeURIComponent(target.assetId)}` +
      `/${encodeURIComponent(target.assetVersion)}` +
      `/${encodeURIComponent(target.operationId)}${query}`,
  );
}

/**
 * POST .../approve-chain — ④⑦ 을 **한 번**에 채워요(admin).
 *
 * 두 엔드포인트를 클라이언트가 차례로 부르면 부분 실패 때 화면이 «어디까지 됐는지» 를
 * 잃어요. 서버가 순서를 잡고 첫 실패에서 멈춰요 — 실패한 단계 뒤를 계속 밀면 원장이 반쯤
 * 열린 상태로 남거든요.
 *
 * 멱등이라 부분 실패 뒤 같은 인자로 다시 불러도 남은 것만 해요. 「이어서」 버튼이 따로
 * 필요하지 않아요.
 *
 * **누르기 전에 폭발 반경을 보여줘야 해요.** grant 하나가 여는 것은 이제 정확히 이 도구
 * 하나예요(ADR-0099 결정 2) — 옛 capability 라벨처럼 자산에 무관하게 번지지 않아요. 다만
 * **그룹** 부여는 사람 축으로 넓어져요: 지금 그 그룹에 있는 사람과 앞으로 들어올 모든 사람이
 * 부를 수 있게 돼요.
 */
export function approveAuthorizationChain(
  target: ChainTarget,
  subject: GrantSubject,
  expiresAt?: number,
): Promise<ChainApprovalResult> {
  return request<ChainApprovalResult>(chainPath(target, "approve-chain"), {
    method: "POST",
    headers: JSON_HEADERS,
    body: grantBody(subject, expiresAt),
  });
}

/**
 * POST .../approve — ④층 승인(admin). **기존 엔드포인트를 그대로 불러요.**
 *
 * `approveAgentToolBinding`(identity.ts)은 `AgentToolBinding` 전체를 받아 경로를 조립해요.
 * 이 화면은 진단 행만 갖고 있어서 binding 을 가짜로 만들어 넘기게 되는데, 그러면 실제와
 * 어긋난 좌표로 다른 레코드를 승인할 위험이 생겨요. 좌표 4개만 받아 같은 경로를 부르는
 * 얇은 함수를 따로 뒀어요 — 엔드포인트는 하나예요.
 */
export function approveChainToolBinding(
  target: ChainTarget,
): Promise<AgentToolBindingApproval> {
  return request<AgentToolBindingApproval>(chainPath(target, "approve"), {
    method: "POST",
    headers: JSON_HEADERS,
  });
}

/**
 * POST .../access-grant — ⑦층 `(주체, asset_id, operation_id)` grant 를 부여해요(admin).
 *
 * 관리자가 정하는 값은 **주체 하나**예요. 자산·operation 은 경로가 정하고, 라벨이라는 중간
 * 화폐가 없으니 grant 가 엉뚱한 범위로 번질 수 없어요(ADR-0099 결정 2).
 */
export function provisionChainAccessGrant(
  target: ChainTarget,
  subject: GrantSubject,
  expiresAt?: number,
): Promise<ChainAccessGrantResult> {
  return request<ChainAccessGrantResult>(chainPath(target, "access-grant"), {
    method: "POST",
    headers: JSON_HEADERS,
    body: grantBody(subject, expiresAt),
  });
}
