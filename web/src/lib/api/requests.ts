import { SESSION_PRINCIPAL, jsonHeadersWithPrincipal, request } from "./client";
import type { TrustSummary } from "./types";
import type { ConversationManagerObservation } from "@/lib/conversationManager";

// ── My Requests (요청 이력) ─────────────────────────────────────────────
export type PublishRequestItem = {
  request_id: string;
  kind: string;         // deploy-mcp | deploy-agent | skill | mcp-connect | agent-json | agent-domain
  status: string;       // pending | running | succeeded | failed
  title: string;
  created_at: string;
  updated_at: string;
  record_id: string | null;
  job_id: string | null;
  // 배포 job 실패는 `{phase, message}`, 동기 등록 실패는 `{reason, remediation}`(CA-34).
  error: {
    phase?: string;
    message?: string;
    reason?: string;
    remediation?: string;
  } | null;
  catalog_status: string | null;
  governance?: TrustSummary | null;
  // IH-77: 배포 진행을 이 화면에서 보여줘요. 서버가 job 전진 시 복사해 두므로 화면이
  // 배포 job 상태를 직접 조회하지 않아요 — 그 조회는 job 을 전진시켜서(IH-80) 이중 actor
  // 문제를 다시 만들어요.
  phase?: string | null;
  phase_detail?: string | null;
  conversation_manager?: ConversationManagerObservation | null;
};

export type PublishRequestPage = {
  items: PublishRequestItem[];
  total_count: number;
  next_cursor: string | null;
};

/** 내 퍼블리시 요청 이력. 최신순 10건과 다음 페이지 cursor. */
export async function listMyRequests(
  principal: string = SESSION_PRINCIPAL,
  cursor?: string | null,
): Promise<PublishRequestPage> {
  const query = cursor ? `?cursor=${encodeURIComponent(cursor)}` : "";
  return request<PublishRequestPage>(`/api/requests${query}`, {
    headers: jsonHeadersWithPrincipal(principal),
  });
}

/**
 * POST /api/requests/poke — 진행 중인 배포 job을 1단계 강제 전진('재요청').
 * 서버측 폴러가 꺼진 환경에서 QUEUED에 멈춘 배포를 수동으로 풀어요.
 * 갱신된 내 요청 목록을 반환해요(job phase → 요청 status sync 반영).
 */
export async function pokeRequests(
  principal: string = SESSION_PRINCIPAL,
): Promise<PublishRequestPage> {
  return request<PublishRequestPage>("/api/requests/poke", {
    method: "POST",
    headers: jsonHeadersWithPrincipal(principal),
  });
}
