import { JSON_HEADERS, request } from "./client";
import {
  toolGrantListRequest,
  toolGrantPutRequest,
  toolGrantRevokeRequest,
} from "../toolAccess";

// ---------------------------------------------------------------------------
// ⑦층 — 도구별 «호출 주체» (ADR-0099 결정 2·7, IH-145).
//
// grant 의 키가 `(subject, asset_id, operation_id)` 예요. capability 라벨도 connection 도
// 없어요 — 「이 사람·그룹이 **이 도구**를 부를 수 있나」 하나만 물어요.
//
// 경로 조립은 `lib/toolAccess.ts` 가 해요. 이 모듈은 확장자 없는 상대 import 때문에 테스트
// 러너(`node --experimental-strip-types`)가 해석하지 못하니, 검증해야 하는 부분(메서드·경로)을
// 순수 모듈에 두고 여기서는 그걸 그대로 실행해요.
// ---------------------------------------------------------------------------

export type ToolGrantSubjectKind = "group" | "principal";
export type ToolGrantObservation = "observed" | "unknown";

export type ToolGrant = {
  grantId: string;
  subjectKind: ToolGrantSubjectKind;
  /** 그룹이면 그룹 이름(`user`·`admin`), 사람이면 Cognito sub 예요. */
  subjectId: string;
  /** 디렉토리에서 읽은 표시 이름. 못 읽으면 `""` 예요 — 없는 사람이라는 뜻이 아니에요. */
  subjectName: string;
  subjectEmail: string;
  /** `ACTIVE` · `REVOKED` · `EXPIRED`. 모르는 값은 통과로 그리지 않아요. */
  status: string;
  grantedBy: string;
  createdAt: string;
  updatedAt: string;
  /** 만료 시각(epoch 초). 없으면 `null` 이에요 — 서버가 `grant.expires_at` 을 그대로 실어요. */
  expiresAt: number | null;
};

export type ToolGrantPage = {
  assetId: string;
  operationId: string;
  /** 사람 축 scan까지 완료했는지. unknown이면 빈 목록을 권한 없음으로 판정하면 안 돼요. */
  grantsObservation: ToolGrantObservation;
  grants: ToolGrant[];
  /** false 면 이름·email 이 빈 행이 있어요 — 「사람이 아님」과 구분해야 해요. */
  directoryObserved: boolean;
  /** 서버가 인가에 쓰는 그룹 목록. 화면의 `TOOL_GRANT_GROUPS` 와 어긋나면 그걸 알려요. */
  allowedGroups: string[];
};

/**
 * 부여 본문 — **snake_case 예요** (`ToolGrantUpsert`, `access_router.py`).
 *
 * 응답 payload 는 camelCase 인데 요청 본문은 snake_case 라 두 방향이 달라요. 서버 모델을
 * 따라가는 게 맞아요 — camelCase 로 보내면 필수 필드가 비어 422 예요.
 */
export type ToolGrantInput = {
  subject_kind: ToolGrantSubjectKind;
  subject_id: string;
  /** 만료를 두지 않으면 `null`(epoch 초). 이 화면은 만료를 다루지 않아 항상 `null` 이에요. */
  expires_at: number | null;
};

/** 쓰기 응답 — 행이 새로 생겼는지, 회수됐던 걸 되살렸는지, 이미 그랬는지. */
export type ToolGrantMutation = {
  outcome: "created" | "reactivated" | "revoked" | "already_revoked";
  grant: ToolGrant;
  /** PUT 에만 있어요. false 면 자산의 현재 버전을 못 읽어 감사 값이 비었다는 뜻이에요. */
  assetVersionObserved?: boolean;
};

/** GET /api/admin/tools/{asset_id}/{operation_id}/grants — 이 도구를 부를 수 있는 주체(admin). */
export function listToolGrants(
  assetId: string,
  operationId: string,
): Promise<ToolGrantPage> {
  const spec = toolGrantListRequest(assetId, operationId);
  return request<ToolGrantPage>(spec.path);
}

/**
 * PUT /api/admin/tools/{asset_id}/{operation_id}/grants — 주체 하나를 더하거나 되살려요(admin).
 *
 * `PUT` 이라 멱등이에요. 이미 있는 주체를 다시 보내도 행이 늘지 않고, 회수됐던 행은 다시
 * `ACTIVE` 가 돼요.
 */
export function putToolGrant(
  assetId: string,
  operationId: string,
  body: ToolGrantInput,
): Promise<ToolGrantMutation> {
  const spec = toolGrantPutRequest(assetId, operationId);
  return request<ToolGrantMutation>(spec.path, {
    method: spec.method,
    headers: JSON_HEADERS,
    body: JSON.stringify(body),
  });
}

/**
 * DELETE .../grants/{subjectKind}/{subjectId} — 회수(admin).
 *
 * **하드 삭제가 아니에요** — 서버가 행을 `REVOKED` 로 기록해요(ADR-0099 §6.1). 화면 문구도
 * 「삭제」가 아니라 「회수」예요. 이미 회수된 주체는 `already_revoked` 로 와요(멱등).
 */
export function revokeToolGrant(
  assetId: string,
  operationId: string,
  subjectKind: ToolGrantSubjectKind,
  subjectId: string,
): Promise<ToolGrantMutation> {
  const spec = toolGrantRevokeRequest(assetId, operationId, subjectKind, subjectId);
  return request<ToolGrantMutation>(spec.path, { method: spec.method });
}
