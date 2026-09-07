import type { PublishRequestItem } from "./api/requests.ts";

const CATALOG_PENDING = new Set(["CREATING", "DRAFT", "PENDING_APPROVAL"]);
const CATALOG_APPROVED = new Set(["APPROVED"]);
const CATALOG_REJECTED = new Set(["REJECTED", "DEPRECATED"]);
const CATALOG_DELETED = new Set(["DELETED"]);
const VERDICT_APPROVED = new Set(["approved", "approved-override"]);
const VERDICT_REJECTED = new Set(["rejected", "auto-reject", "deprecated"]);

function catalogStatus(request: PublishRequestItem): string {
  return (request.catalog_status ?? "").toUpperCase();
}

function deploymentInProgress(request: PublishRequestItem): boolean {
  return request.status === "pending" || request.status === "running";
}

export function publishMeta(
  request: PublishRequestItem,
): { label: string; cls: string } {
  const status = catalogStatus(request);
  const verdict = request.governance?.verdict ?? "";

  if (CATALOG_DELETED.has(status)) {
    return { label: "삭제됨", cls: "bg-slate-100 text-slate-600" };
  }
  if (CATALOG_APPROVED.has(status) || VERDICT_APPROVED.has(verdict)) {
    return { label: "게시완료", cls: "bg-emerald-50 text-emerald-700" };
  }
  if (CATALOG_REJECTED.has(status) || VERDICT_REJECTED.has(verdict)) {
    return { label: "검토필요", cls: "bg-amber-50 text-amber-700" };
  }
  if (request.status === "failed") {
    return { label: "게시실패", cls: "bg-red-50 text-red-700" };
  }
  return { label: "승인대기", cls: "bg-blue-50 text-blue-700" };
}

export function isRequestActive(request: PublishRequestItem): boolean {
  const status = catalogStatus(request);
  if (
    CATALOG_APPROVED.has(status) ||
    CATALOG_REJECTED.has(status) ||
    CATALOG_DELETED.has(status)
  ) {
    return false;
  }
  if (CATALOG_PENDING.has(status)) {
    return true;
  }
  if (
    request.governance?.scan_state === "scanning" ||
    request.governance?.overlap_state === "reviewing"
  ) {
    return true;
  }
  const verdict = request.governance?.verdict ?? "";
  if (VERDICT_APPROVED.has(verdict) || VERDICT_REJECTED.has(verdict)) {
    return false;
  }
  return deploymentInProgress(request);
}

/**
 * 실패한 요청을 사람이 읽을 한 줄로. 없으면 null.
 *
 * 두 모양을 받아요 — 배포 job 실패는 `{phase, message}`, 동기 등록 실패는
 * `{reason, remediation}`(CA-34) 예요. 하나만 알면 다른 쪽이 `[undefined] undefined` 로
 * 보여요. "실패했어요"만 남기지 않고 원인과 다음 행동을 함께 실어요.
 */
export function requestErrorText(request: PublishRequestItem): string | null {
  const error = request.error;
  if (!error) return null;
  const head = error.phase ? `[${error.phase}] ` : "";
  const body = error.message ?? error.reason ?? "";
  const tail = error.remediation ? ` — ${error.remediation}` : "";
  const text = `${head}${body}${tail}`.trim();
  return text || "실패 사유가 기록되지 않았어요 — 관리자에게 문의해 주세요.";
}

export function shouldPokeRequest(request: PublishRequestItem): boolean {
  return (
    !!request.job_id &&
    !request.record_id &&
    deploymentInProgress(request) &&
    (request.kind === "deploy-mcp" || request.kind === "deploy-agent")
  );
}
