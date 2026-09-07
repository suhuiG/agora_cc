import { jsonHeadersWithPrincipal, request } from "./client";
import type { AssetCard, AssetDetail, DescriptorType, SearchResult } from "./types";

// 인라인 퍼블리시 / 카탈로그 / 검색 / 상세
// ---------------------------------------------------------------------------

/** GET /api/catalog 페이지 응답 (offset pagination). */
export type CatalogPage = {
  items: AssetCard[];
  total: number;
  offset: number;
  limit: number;
};

/** GET /api/catalog?type=&offset=&limit= — 카탈로그 카드 페이지. */
export function getCatalog(
  type?: DescriptorType,
  offset = 0,
  limit = 6,
): Promise<CatalogPage> {
  const params = new URLSearchParams();
  if (type) params.set("type", type);
  params.set("offset", String(offset));
  params.set("limit", String(limit));
  return request<CatalogPage>(`/api/catalog?${params.toString()}`);
}

/** GET /api/catalog/categories — 등록 폼용 목록. 빈 배열이면 자유입력 폴백. */
export function getCatalogCategories(): Promise<{ items: string[] }> {
  return request<{ items: string[] }>("/api/catalog/categories");
}

/** GET /api/search?q=&type=&max_results= — 검색 결과. */
export function searchAssets(
  q: string,
  type?: DescriptorType,
  maxResults?: number,
): Promise<SearchResult[]> {
  const params = new URLSearchParams();
  params.set("q", q);
  if (type) params.set("type", type);
  if (maxResults !== undefined) params.set("max_results", String(maxResults));
  return request<SearchResult[]>(`/api/search?${params.toString()}`);
}

/**
 * GET /api/assets/{record_id} — 상세.
 * SIDE EFFECT: 조회수(views)를 증가시켜요. 실제 상세 뷰에서만 호출하고,
 * 리스트에서는 호출하지 마세요.
 */
export function getAsset(record_id: string): Promise<AssetDetail> {
  return request<AssetDetail>(`/api/assets/${encodeURIComponent(record_id)}`);
}

/** POST /api/assets/{id}/view — 상세 조회 1회 기록(조회수 +1). */
export function recordView(record_id: string): Promise<{ record_id: string; views: number }> {
  return request(`/api/assets/${encodeURIComponent(record_id)}/view`, { method: "POST" });
}

/** 인기 톱5 항목 1건. */
export type TopEntry = {
  record_id: string;
  name: string;
  descriptor_type: string;
  downloads: number;
  /** 등록자(자산을 올린 사람). 미상이면 빈 문자열이에요. */
  owner_user: string;
  /** 등록 시 검증된 email claim. 기존 자산은 빈 문자열이에요. */
  owner_email: string;
};

/** 자산 타입 하나의 톱5 — 화면의 카드 하나. items가 비면 아직 다운로드가 없어요. */
export type TopDownloadGroup = { descriptor_type: string; items: TopEntry[] };

/** GET /api/catalog/top-downloads 응답. groups는 항상 고정 순서·길이(빈 타입 포함). */
export type TopDownloads = { computed_at: string; groups: TopDownloadGroup[] };

/**
 * GET /api/catalog/top-downloads — 타입별 인기 자산 톱5.
 * 서버가 매 호출마다 실집계해요(1시간 캐시에 갇히지 않음).
 * force=true는 새로고침 버튼용이에요.
 */
export function getTopDownloads(force = false): Promise<TopDownloads> {
  const qs = force ? "?force=1" : "";
  return request<TopDownloads>(`/api/catalog/top-downloads${qs}`);
}

/** POST /api/assets/{id}/download — 다운로드 1회 기록(다운로드 수 +1). */
export function recordDownload(record_id: string): Promise<{ record_id: string; downloads: number }> {
  return request(`/api/assets/${encodeURIComponent(record_id)}/download`, { method: "POST" });
}

// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// 본인 자산 수정 / 삭제 (§13)
// ---------------------------------------------------------------------------

export type CurationInput = {
  description?: string;
  tags?: string[];
  category?: string;
  changelog?: string;
};

/** PATCH /api/assets/{record_id} — 큐레이션 필드만 수정(본인 소유). */
export function updateCuration(
  recordId: string, fields: CurationInput, principal: string,
): Promise<AssetDetail> {
  return request<AssetDetail>(`/api/assets/${encodeURIComponent(recordId)}`, {
    method: "PATCH",
    headers: jsonHeadersWithPrincipal(principal),
    body: JSON.stringify(fields),
  });
}

export type PurgeReport = {
  deleted: string[];
  failed: { store: string; reason: string }[];
  skipped: string[];
};

/** DELETE /api/assets/{record_id} — 완전 삭제(모든 버전·데이터·리소스, 본인 소유). 되돌릴 수 없어요. */
export function purgeAsset(
  recordId: string, principal: string,
): Promise<{ record_id: string; report: PurgeReport; message: string }> {
  return request(`/api/assets/${encodeURIComponent(recordId)}`, {
    method: "DELETE",
    headers: jsonHeadersWithPrincipal(principal),
  });
}
