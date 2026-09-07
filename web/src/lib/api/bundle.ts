import { SESSION_PRINCIPAL, jsonHeadersWithPrincipal, request } from "./client";

// ---------------------------------------------------------------------------
// Bundle (자산 묶음) — 큐레이션 허브의 조직 단위
// ---------------------------------------------------------------------------

export type BundleSummary = {
  bundle_id: string;
  name: string;
  description: string;
  member_count: number;
  // 타입별 멤버 수 (목록 카드용). create/update 직후 응답은 0 — 목록 refetch 시 채워져요.
  skill_count: number;
  mcp_count: number;
  agent_count: number;
  surfaces: string[];
  category: string;
  tags: string[];
  created_by: string;
  // 그룹을 만든 사람(사용자 신청 흐름의 소유자). 관리자 생성 기존 그룹은 빈 문자열.
  owner_principal: string;
  // NONE | PENDING | APPROVED | REJECTED
  request_status: string;
  updated_at: string;
};

export type BundleMember = {
  record_id: string;
  name: string;
  type: string;
  status: string;
  available: boolean;
};

export type BundleDetail = BundleSummary & { members: BundleMember[] };

export type BundleInput = {
  name: string;
  description?: string;
  member_ids?: string[];
  surfaces?: string[];
  category?: string;
  tags?: string[];
};

export function listBundles(): Promise<BundleSummary[]> {
  return request<BundleSummary[]>("/api/bundles");
}

export function getBundle(id: string): Promise<BundleDetail> {
  return request<BundleDetail>(`/api/bundles/${encodeURIComponent(id)}`);
}

export function createBundle(
  body: BundleInput,
  principal: string = SESSION_PRINCIPAL,
): Promise<BundleSummary> {
  return request<BundleSummary>("/api/bundles", {
    method: "POST",
    headers: jsonHeadersWithPrincipal(principal),
    body: JSON.stringify(body),
  });
}

export function updateBundle(
  id: string,
  body: Partial<BundleInput>,
  principal: string = SESSION_PRINCIPAL,
): Promise<BundleSummary> {
  return request<BundleSummary>(`/api/bundles/${encodeURIComponent(id)}`, {
    method: "PATCH",
    headers: jsonHeadersWithPrincipal(principal),
    body: JSON.stringify(body),
  });
}

export function deleteBundle(
  id: string,
  principal: string = SESSION_PRINCIPAL,
): Promise<{ deleted: boolean; repo_files_deleted: number }> {
  return request<{ deleted: boolean; repo_files_deleted: number }>(
    `/api/bundles/${encodeURIComponent(id)}`,
    {
      method: "DELETE",
      headers: jsonHeadersWithPrincipal(principal),
    },
  );
}

// ── Publish (repo 연결 + bundle 배포) ─────────────────────
export type PublishBundleResult = {
  commit_sha: string;
  version: string;
  surfaces: string[];
  skipped: string[];
  files_written: number;
};

export function publishBundle(
  id: string,
  principal: string = SESSION_PRINCIPAL,
): Promise<PublishBundleResult> {
  return request(`/api/bundles/${encodeURIComponent(id)}/publish`, {
    method: "POST",
    headers: jsonHeadersWithPrincipal(principal),
  });
}

// ── 배포 신청 · 검토 · 콘솔 인계 ─────────────────────────
export type RequestDeployResult = {
  bundle_id: string;
  request_status: string;
  owner_principal: string;
};

export type ReviewResult = {
  request_status: string;
  published: boolean;
  error?: string;
  commit_sha?: string;
  version?: string;
  surfaces?: string[];
  pr_url?: string;
};

export type HandoffInfo = {
  console_url: string;
  repo_slug: string;
  marketplace_name: string;
  plugin_slug: string;
  steps: string[];
  requirements: string[];
};

/** 사용자 배포 신청. 미승인 자산·예약어·빈 그룹이면 422. */
export function requestDeployBundle(
  id: string,
  principal: string = SESSION_PRINCIPAL,
): Promise<RequestDeployResult> {
  return request(`/api/bundles/${encodeURIComponent(id)}/request-deploy`, {
    method: "POST",
    headers: jsonHeadersWithPrincipal(principal),
  });
}

/** 관리자 검토. 승인이면 배포(PR 생성)까지 이어져요. */
export function reviewBundle(
  id: string,
  approved: boolean,
  note = "",
  principal: string = SESSION_PRINCIPAL,
): Promise<ReviewResult> {
  return request(`/api/bundles/${encodeURIComponent(id)}/review`, {
    method: "POST",
    headers: jsonHeadersWithPrincipal(principal),
    body: JSON.stringify({ approved, note }),
  });
}

/** 관리자 콘솔 인계 안내 (Agora 범위 밖 단계). */
export function getBundleHandoff(
  id: string,
  principal: string = SESSION_PRINCIPAL,
): Promise<HandoffInfo> {
  return request(`/api/bundles/${encodeURIComponent(id)}/handoff`, {
    headers: jsonHeadersWithPrincipal(principal),
  });
}

export type RepoConnection = {
  provider: string;
  repo_url: string;
  status: string;
  connected_at: string;
};
export type ConnectionState = { status: "NOT_CONNECTED" } | RepoConnection;

export function getConnection(): Promise<ConnectionState> {
  return request("/api/publish/connection");
}

export function setConnection(
  body: { provider?: string; repo_url: string; token: string },
  principal: string = SESSION_PRINCIPAL,
): Promise<RepoConnection> {
  return request("/api/publish/connection", {
    method: "POST",
    headers: jsonHeadersWithPrincipal(principal),
    body: JSON.stringify({ provider: "github", ...body }),
  });
}

export function verifyConnection(
  principal: string = SESSION_PRINCIPAL,
): Promise<{ ok: boolean; reason: string }> {
  return request("/api/publish/connection/verify", {
    method: "POST",
    headers: jsonHeadersWithPrincipal(principal),
  });
}
