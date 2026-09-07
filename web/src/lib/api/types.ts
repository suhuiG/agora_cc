// ---------------------------------------------------------------------------
// 타입 정의
// ---------------------------------------------------------------------------

export type AssetType = "skill" | "mcp" | "agent";
export type DescriptorType = "MCP" | "Agent Skills" | "Agent" | "App" | "Model" | "Custom";

/** publish/init 에 보내는 파일 스펙 (경로 + 바이트 크기). */
export type FileSpec = { path: string; size: number };

/** POST /api/source/publish/init 응답. */
export type UploadTicket = {
  asset_id: string;
  version: string;
  upload_id: string;
  /** path -> S3 presigned PUT URL */
  urls: Record<string, string>;
};

/** POST /api/source/publish/finalize 응답. */
export type FinalizeResult = {
  asset_id: string;
  version: string;
  status: string;            // source 버전 상태 (PUBLISHED)
  published_by: string;
  published_at: string;
  files: string[];
  catalog_record_id: string;
  /** 카탈로그 등재 상태 (APPROVED 또는 PENDING_APPROVAL). autoApproval 설정에 따라 달라요. */
  catalog_status?: string;
};

/** GET /api/source/assets/{id}/versions 항목. */
export type VersionInfo = {
  version: string;
  status: string;
  published_by: string;
  published_at: string;
  file_count: number;
  changelog: string;
  search_visible: boolean;
  record_id: string | null;
};

/** 카탈로그/리스트용 자산 카드. */
export type AssetCard = {
  record_id: string;
  name: string;
  descriptor_type: string;
  version: string;
  status: string;
  description: string;
  owner_team: string;
  owner_user: string;
  owner_email: string;
  tags: string[];
  category: string;
  views: number;
  downloads: number;
  // source 관리형이면 `{type}/{owner}/{name}/{version}/`, 아니면 "". 설치 버튼 노출 판단용.
  source_prefix: string;
  // MCP 호출 endpoint (Gateway URL 등). 없으면 null이에요.
  endpoint?: string | null;
};

/** 검색 결과 항목. */
export type SearchResult = {
  record_id: string;
  name: string;
  descriptor_type: string;
  version: string;
  description: string;
  score: number;
};

/** 자동승인이 진행되지 않은 사유 (R3).
 *
 *  `responsibility_incomplete`(값이 비었음)와 `responsibility_unobservable`(조회 실패)은
 *  서로 다른 사실이라 합치지 않아요. 값이 `null`이면 "막힌 기록이 없음"이고
 *  **승인됐다는 뜻이 아니에요** — 승인 여부는 `verdict`로만 읽어요.
 */
export type ApprovalBlock = {
  reason:
    | "responsibility_incomplete"
    | "responsibility_unobservable"
    | "gate_pending"
    | "gate_rejected"
    | "status_transition_failed"
    | "block_unobservable";
  observed_at: string;
  detail: string;
  remediation: string;
  missing_contacts: string[];
  incomplete_stages: string[];
};

export type TrustSummary = {
  record_id: string;
  tier: string;
  scan_state: "not_scanned" | "scanning" | "scanned" | "failed" | "exempt";
  scan_risk: string | null;
  verdict: string | null;
  overlap_state?: "not_reviewed" | "reviewing" | "reviewed" | "failed";
  overlap_count?: number;
  overlap_band?: "high" | "medium" | "low" | null;
  approval_block?: ApprovalBlock | null;
};

/** 상세 뷰 (카드 + descriptors). */
export type AssetDetail = AssetCard & {
  descriptors: Record<string, unknown>;
  trust?: TrustSummary | null;
  /**
   * 2차 담당자(에스컬레이션 연락처). **상세에만** 실려요 — 목록(`/api/catalog`)은 SearchHit
   * 기반이라 이 값을 채울 수 없어요. 옛 레코드엔 없어서 빈 문자열일 수 있어요.
   */
  escalation_contact?: string;
  // 소스 관리형(배포형) 여부. MCP는 sourcePrefix 좌표를 응답에서 숨기므로(CA-04), 재배포
  // 버튼 노출 판단은 이 boolean으로 해요(CA-16·ADR-0021). 연결형 MCP는 false.
  source_managed?: boolean;
  // 배포형(RUNTIME) agent 여부. Runtime ARN 은 응답에서 숨기므로(IA-89 ①), 재배포 버튼
  // 노출 판단은 이 boolean으로 해요. 연결형 agent는 false.
  runtime_deployed?: boolean;
};

/** GET/PUT /api/assets/{id}/responsibility 응답. */
export type ResponsibilityStatus = {
  record_id: string;
  owner_contact: string;
  escalation_contact: string;
  status: string;
  required_for: string;
  blocking_reasons: string[];
  authorization_effect: "none";
};

export type ResponsibilityUpdateInput = {
  owner_contact: string;
  escalation_contact: string;
  reason: string;
};

export type ResponsibilityContacts = {
  owner_contact: string;
  escalation_contact: string;
};

export type ResponsibilityChange = {
  event_id: string;
  record_id: string;
  changed_at: string;
  changed_by: string;
  reason: string;
  before: ResponsibilityContacts;
  after: ResponsibilityContacts;
};

/** GET /api/assets/{id}/responsibility/history 응답. */
export type ResponsibilityHistory = {
  items: ResponsibilityChange[];
};
