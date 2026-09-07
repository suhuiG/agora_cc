/**
 * 등록 폼의 담당자 계약 (CA-29 · ADR-0069).
 *
 * 서버가 정본이에요 — 여기 검증은 왕복 한 번을 줄이는 것뿐이고, 통과했다고 등록이 보장되는
 * 건 아니에요. 서버와 같은 세 규칙만 봐요: 값이 있어야 하고, email 형식이어야 하고,
 * 등록자 본인과 달라야 해요.
 */

// 서버 `catalog/responsibility.py` 의 `_EMAIL_ROUTE` 와 같은 모양이에요.
const EMAIL_RE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;

export function isContactEmail(value: string): boolean {
  const route = value.trim().toLowerCase();
  return route.length <= 254 && EMAIL_RE.test(route);
}

/**
 * 2차 담당자 입력의 문제를 사람이 읽는 한 줄로. 문제가 없으면 null.
 *
 * `ownerContact` 는 서버가 principal 에서 파생하는 값(=등록자 email)이에요. 화면은 그것을
 * 읽기 전용으로 보여주기만 하고 보내지 않아요.
 */
export function escalationContactProblem(
  escalation: string,
  ownerContact: string,
): string | null {
  const value = escalation.trim();
  if (!value) {
    return "2차 담당자(에스컬레이션)를 선택해 주세요 — 회원 검색으로 골라요.";
  }
  if (!isContactEmail(value)) {
    return "2차 담당자는 email 이어야 해요 — 회원 검색 결과에서 골라 주세요.";
  }
  const owner = ownerContact.trim().toLowerCase();
  if (owner && value.toLowerCase() === owner) {
    return "2차 담당자는 등록자 본인과 달라야 해요 — 다른 구성원을 골라 주세요.";
  }
  return null;
}

export type ResponsibilityUpdateFields = {
  ownerContact: string;
  escalationContact: string;
  reason: string;
};

/**
 * 관리자 사후 보정 화면의 저장 전 검증.
 *
 * 등록 폼은 1차 담당자를 서버가 파생하지만, 이 화면은 과거 자산과 파생 실패를 보정하므로
 * 두 연락처를 모두 입력받아요. 최종 판정은 계속 서버 계약이 정본이에요.
 */
export function responsibilityUpdateProblem({
  ownerContact,
  escalationContact,
  reason,
}: ResponsibilityUpdateFields): string | null {
  const owner = ownerContact.trim();
  const escalation = escalationContact.trim();
  const changeReason = reason.trim();

  if (!owner) {
    return "1차 담당자를 회원 검색으로 선택해 주세요.";
  }
  if (!isContactEmail(owner)) {
    return "1차 담당자는 유효한 email 이어야 해요.";
  }
  if (!escalation) {
    return "2차 담당자(에스컬레이션)를 회원 검색으로 선택해 주세요.";
  }
  if (!isContactEmail(escalation)) {
    return "2차 담당자는 유효한 email 이어야 해요.";
  }
  if (owner.toLowerCase() === escalation.toLowerCase()) {
    return "1차와 2차 담당자는 서로 달라야 해요.";
  }
  if (!changeReason) {
    return "변경 사유를 입력해 주세요.";
  }
  if (changeReason.length > 1000) {
    return "변경 사유는 1000자 이하여야 해요.";
  }
  return null;
}

/** HTTP 성공과 계약 완료를 구분해요. 차단 사유가 남으면 성공 안내를 하면 안 돼요. */
export function responsibilitySaveIsComplete(status: {
  status: string;
  blocking_reasons: readonly string[];
}): boolean {
  return status.status === "complete" && status.blocking_reasons.length === 0;
}

/** 지금 바로 보정해야 하는 두 사유에서만 편집기를 펼쳐 시작해요. */
export function responsibilityEditorStartsOpen(
  reason: string | undefined,
): boolean {
  return (
    reason === "responsibility_incomplete" ||
    reason === "responsibility_unobservable"
  );
}
