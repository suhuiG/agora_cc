// 도구별 «호출 주체» 관리 화면(/admin/tool-access)의 판정·문구 (순수 함수만).
//
// 화면 컴포넌트에 두지 않는 이유: web 테스트 러너는 `node --experimental-strip-types` 라
// JSX 를 변환하지 못해요(`web/package.json` 의 test 목록에 `.tsx` 가 0개예요). 판정을 여기
// 두면 테스트가 돌고, 컴포넌트는 그리기만 해요.
//
// **이 화면의 축은 도구예요** — ADR-0099 결정 7. `/admin/tool-authorization` 은 ④층(agent 가
// 신청한 도구)을 승인하는 화면이고, 이 화면은 ⑦층(이 도구를 부를 자격이 있는 사람·그룹)을
// CRUD 해요. 두 축은 서로 무관해요: ⑦은 「그 사람이 어떤 agent 로 부르든 같다」예요. 그래서
// 두 화면을 합치지 않아요.

import type {
  ToolGrant,
  ToolGrantObservation,
  ToolGrantSubjectKind,
} from "./api/toolAccess";
import { blastRadiusMessage, type BlastRadiusSubject } from "./authorizationChain.ts";

// ── 그룹 ───────────────────────────────────────────────────────────────────

/**
 * 고를 수 있는 그룹 — **`user`·`admin` 둘뿐이에요** (ADR-0098).
 *
 * 호출 시점 판정은 delegation 행의 `principal_groups` 로 하는데, `token_verifier.py:99` 가
 * 그 값을 `PLATFORM_ROLES`(= `("user", "admin")`)로 **걸러요**. 다른 그룹 이름으로 부여하면
 * 행은 생기는데 판정에 절대 안 잡혀요 — 관리 화면은 초록불이고 호출은 계속 거부돼요.
 *
 * 사람 말 라벨을 함께 둬요. `user`·`admin` 만 보여주면 관리자가 「user 는 관리자를
 * 포함하나?」를 화면에서 알 수 없어요.
 *
 * ⚠️ 키와 화면은 그룹 수에 무관하게 설계해요(ADR-0099 결정 6) — 업무 그룹이 늘어도 이 배열만
 * 늘면 되고 판정 코드는 안 바뀌어야 해요.
 */
export const TOOL_GRANT_GROUPS = [
  { id: "user", label: "전 회원" },
  { id: "admin", label: "관리자만" },
] as const;

export type ToolGrantGroup = (typeof TOOL_GRANT_GROUPS)[number]["id"];

export function isToolGrantGroup(value: string): value is ToolGrantGroup {
  return TOOL_GRANT_GROUPS.some((group) => group.id === value);
}

/** 그룹 id 를 사람 말로. 모르는 그룹은 id 를 그대로 보여줘요 — 침묵하지 않아요. */
export function toolGrantGroupLabel(groupId: string): string {
  return TOOL_GRANT_GROUPS.find((group) => group.id === groupId)?.label ?? groupId;
}

// ── 도구 라벨 ──────────────────────────────────────────────────────────────

/**
 * 도구를 사람이 알아볼 이름으로. `asset_id` 는 `fAUPJWslfkyZ` 같은 값이라 그것만 보여주면
 * 관리자가 무엇을 여는지 알 수 없어요. 자산 이름을 못 읽었으면 id 를 쓰되 그렇게 말해요.
 */
export function toolLabel(assetName: string, operationId: string): string {
  if (!operationId) return "";
  return assetName ? `${assetName} · ${operationId}` : operationId;
}

// ── 경로 조립 ──────────────────────────────────────────────────────────────
//
// api 모듈(`api/toolAccess.ts`)이 이 함수들을 불러요. 경로를 그쪽에 두면 테스트가 못 봐요 —
// api 모듈은 확장자 없는 상대 import 때문에 strip-types 러너가 해석하지 못해요
// (`api/requests.test.ts` 가 같은 이유로 소스를 문자열로 읽어요).
//
// **회수는 하드 삭제가 아니에요.** `DELETE` 는 행을 지우지 않고 `REVOKED` 로 기록해요
// (ADR-0099 §6.1) — 하드 삭제면 회수 이력이 사라지고, 자동 부여 경로가 남아 있는 동안에는
// 다음 실행이 행을 되살려서 회수가 무효가 돼요.

export function toolGrantsPath(assetId: string, operationId: string): string {
  return (
    `/api/admin/tools/${encodeURIComponent(assetId)}` +
    `/${encodeURIComponent(operationId)}/grants`
  );
}

export type ToolGrantRequestSpec = { method: "GET" | "PUT" | "DELETE"; path: string };

export function toolGrantListRequest(
  assetId: string,
  operationId: string,
): ToolGrantRequestSpec {
  return { method: "GET", path: toolGrantsPath(assetId, operationId) };
}

export function toolGrantPutRequest(
  assetId: string,
  operationId: string,
): ToolGrantRequestSpec {
  return { method: "PUT", path: toolGrantsPath(assetId, operationId) };
}

/**
 * 회수 요청 — `DELETE .../grants/{subjectKind}/{subjectId}`.
 *
 * 메서드가 `DELETE` 인데 결과가 `REVOKED` 기록이에요. 화면 문구는 「삭제」가 아니라
 * 「회수」여야 해요 — 행이 사라지는 게 아니니까요.
 */
export function toolGrantRevokeRequest(
  assetId: string,
  operationId: string,
  subjectKind: ToolGrantSubjectKind,
  subjectId: string,
): ToolGrantRequestSpec {
  return {
    method: "DELETE",
    path:
      `${toolGrantsPath(assetId, operationId)}` +
      `/${encodeURIComponent(subjectKind)}/${encodeURIComponent(subjectId)}`,
  };
}

// ── 상태 표현 ──────────────────────────────────────────────────────────────

export type GrantTone = "ok" | "revoked" | "expired" | "unknown";

export type GrantStatusPresentation = {
  tone: GrantTone;
  label: string;
  /** 이 상태가 호출에 어떤 뜻인지. 빈 문자열이면 덧붙일 말이 없다는 뜻이에요. */
  note: string;
};

/**
 * grant 상태를 배지 색과 문구로.
 *
 * `REVOKED` 는 **행이 남아 있는 상태**예요 — 「없음」이 아니라 「회수됨」이라고 말해야 관리자가
 * 목록에 남은 행을 오해하지 않아요. 모르는 상태는 초록으로 그리지 않아요(ADR-0037 §4).
 */
export function grantStatusPresentation(status: string): GrantStatusPresentation {
  switch (status.toUpperCase()) {
    case "ACTIVE":
      return { tone: "ok", label: "호출 가능", note: "" };
    case "REVOKED":
      return {
        tone: "revoked",
        label: "회수됨",
        note: "행은 이력으로 남아 있고, 호출은 막혀요.",
      };
    case "EXPIRED":
      return {
        tone: "expired",
        label: "만료됨",
        note: "만료 시각이 지났어요. 호출은 막혀요.",
      };
    default:
      return {
        tone: "unknown",
        label: status || "확인 필요",
        note: "이 상태를 해석하지 못했어요 — 통과로 볼 수 없어요.",
      };
  }
}

/** 이 grant 로 지금 부를 수 있는지. 모르는 상태는 `false` 예요 (fail-closed). */
export function isGrantActive(grant: ToolGrant): boolean {
  return grantStatusPresentation(grant.status).tone === "ok";
}

// ── 회수 문구 ──────────────────────────────────────────────────────────────

/** 버튼 글자. 「삭제」가 아니에요 — 행은 `REVOKED` 로 남아요. */
export const REVOKE_LABEL = "회수";

export function subjectLabel(grant: ToolGrant): string {
  if (grant.subjectKind === "group") {
    return `${toolGrantGroupLabel(grant.subjectId)} (${grant.subjectId})`;
  }
  return grant.subjectName || grant.subjectEmail || grant.subjectId;
}

/**
 * 회수 확인 문구. 무엇이 남고 언제 막히는지 둘 다 말해요.
 *
 * 그룹은 delegation TTL 때문에 최대 900초 늦게 막히고, 사람은 다음 호출부터 막혀요
 * (interceptor 가 매 호출 `ConsistentRead`). 「즉시」로 뭉치면 그룹 회수 뒤 900초 동안
 * 호출이 되는 걸 관리자가 버그로 읽어요.
 */
export function revokeConfirmMessage(grant: ToolGrant, tool: string): string {
  const when =
    grant.subjectKind === "group"
      ? "그룹 회수는 늦어도 900초 뒤 호출부터 막혀요(delegation TTL ≤ 900초)."
      : "다음 호출부터 바로 막혀요.";
  return (
    `${subjectLabel(grant)} 의 «${tool}» 호출 권한을 회수할까요? ` +
    `행은 지워지지 않고 회수 이력(REVOKED)으로 남아요. ${when}`
  );
}

// ── 부여 전 고지 ───────────────────────────────────────────────────────────

/**
 * 부여 버튼 옆에 붙일 폭발 반경 문구. `authorizationChain.ts` 의 구현을 그대로 써요 —
 * 두 화면이 같은 문장을 말해야 하고, 문구가 갈라지면 한쪽이 조용히 낡아요.
 */
export function toolGrantBlastRadius(
  tool: string,
  subject: BlastRadiusSubject,
): string {
  return blastRadiusMessage(tool, subject);
}

/**
 * 개인(사용자) 단위 부여를 고를 때 붙이는 주의 문구.
 *
 * ⚠️ **배포 주의(코드 주석으로 남기는 이유가 이거예요).** 개인 grant 는 `PRINCIPAL#` 파티션에
 * 쓰여요. 그 파티션을 Cognito `PRE_TOKEN_GENERATION` Lambda `agora-perms-claim-dev` 가
 * Query 하는데, 새 필드를 만나면 `IdentitySchemaTooNew` 로 **fail-closed** 로 멈춰요
 * (`store.py:114-128`). 즉 이 Lambda 가 새 스키마로 재배포되기 전에 개인 grant 를 만들면
 * **그 사용자의 로그인이 깨져요.** `cdk deploy AgoraIdentity-dev` 가 선행 조건이에요
 * (ADR-0099 §6.1). 그룹 grant 는 `GROUP#` 파티션이라 이 경로를 타지 않아요.
 */
export const PRINCIPAL_GRANT_NOTE =
  "개인 부여는 그 사람 한 명에게만 열려요. 그룹으로 관리할 수 있는 권한이면 그룹 쪽이 회수도 쉬워요.";

// ── 정렬 ───────────────────────────────────────────────────────────────────

const TONE_RANK: Record<GrantTone, number> = {
  ok: 0,
  unknown: 1,
  expired: 2,
  revoked: 3,
};

/**
 * 지금 유효한 것 먼저, 그다음 판정하지 못한 것, 만료, 회수 순서예요.
 *
 * 회수된 행을 위에 두면 목록의 첫 줄이 「이 도구는 열려 있다」로 잘못 읽혀요. 같은 등급
 * 안에서는 그룹을 먼저 두고(반경이 넓어 먼저 봐야 해요) 이름으로 정렬해요.
 */
export function sortToolGrants(grants: ToolGrant[]): ToolGrant[] {
  return [...grants].sort((left, right) => {
    const byTone =
      TONE_RANK[grantStatusPresentation(left.status).tone] -
      TONE_RANK[grantStatusPresentation(right.status).tone];
    if (byTone !== 0) return byTone;
    if (left.subjectKind !== right.subjectKind) {
      return left.subjectKind === "group" ? -1 : 1;
    }
    return subjectLabel(left).localeCompare(subjectLabel(right), "ko");
  });
}

/** 이 도구를 지금 부를 수 있는 주체가 하나도 없는지. 회수된 행이 있어도 「없음」이에요. */
export function nobodyCanCall(grants: ToolGrant[]): boolean {
  return grants.every((grant) => !isGrantActive(grant));
}

export type ToolGrantListNoticeState = "unknown" | "nobody" | "none";

/**
 * 목록 관측과 목록 내용은 별개예요. scan 을 못 했으면 빈 배열이어도 「아무도 없음」으로
 * 확정하지 않고 세 번째 상태를 그려요.
 */
export function toolGrantListNoticeState(
  grants: ToolGrant[],
  observation: ToolGrantObservation,
): ToolGrantListNoticeState {
  if (observation !== "observed") return "unknown";
  return nobodyCanCall(grants) ? "nobody" : "none";
}

export const GRANT_LIST_UNKNOWN_MESSAGE =
  "사람 권한 목록을 모두 관측하지 못했어요. 지금 보이는 행은 일부일 수 있고, 빈 목록도 권한 없음으로 확정할 수 없어요. 다시 조회해 주세요.";

/**
 * 아무 주체도 없을 때의 안내. **이게 fail-closed 기본값이에요** — 등록자가 아무 그룹도 고르지
 * 않았다는 «선언» 의 결과이고, 빠진 설정이 아니에요(ADR-0099 §4.6).
 */
export const NOBODY_CAN_CALL_MESSAGE =
  "지금 이 도구를 부를 수 있는 사람이 없어요. 빠진 설정이 아니라, 아무에게도 열지 않았다는 뜻이에요 — 필요하면 아래에서 그룹이나 사용자를 더해 주세요.";
