import { ApiError, JSON_HEADERS, jsonHeadersWithPrincipal, request } from "./client";
import type { AssetType, FileSpec, FinalizeResult, UploadTicket, VersionInfo } from "./types";

// ---------------------------------------------------------------------------
// SOURCE 퍼블리시 (업로드 기반)
// ---------------------------------------------------------------------------

export type SourceInitInput = {
  asset_type: AssetType;
  name: string;            // 서버가 asset_id를 name+principal에서 파생해요.
  files: FileSpec[];
  version?: string;        // 미지정 시 서버가 자동결정(§11.3)
  description?: string;
  owner_team?: string;
  escalation_contact?: string;
  tags?: string[];
  category?: string;
  changelog?: string;      // §12: 버전별 주요 변경사항 요약
};

/** POST /api/source/publish/init — 업로드 티켓(presigned URL 들)을 받아요. */
export function sourceInit(
  input: SourceInitInput,
  principal: string,
): Promise<UploadTicket> {
  return request<UploadTicket>("/api/source/publish/init", {
    method: "POST",
    headers: jsonHeadersWithPrincipal(principal),
    body: JSON.stringify(input),
  });
}

export type SourceFinalizeInput = {
  asset_id: string;
  version: string;
  upload_id: string;
};

/** POST /api/source/publish/finalize — 업로드 완료 처리 + 카탈로그 등록. */
export function sourceFinalize(
  input: SourceFinalizeInput,
  principal: string,
): Promise<FinalizeResult> {
  return request<FinalizeResult>("/api/source/publish/finalize", {
    method: "POST",
    headers: jsonHeadersWithPrincipal(principal),
    body: JSON.stringify(input),
  });
}

/**
 * asset_id의 '/'를 path 구분자로 살린 인코딩 (백엔드 {asset_id:path} 매칭).
 * asset_id = "{owner}/{name}" — '/'는 path 구분자로 살리고 세그먼트만 인코딩해요.
 * (백엔드 라우트가 {asset_id:path} 라 %2F가 아니라 리터럴 '/'를 받아야 해요.)
 */
function encodeAssetIdPath(assetId: string): string {
  return assetId.split("/").map(encodeURIComponent).join("/");
}

/** GET /api/source/assets/{asset_id}/versions — 오름차순 semver, 미존재 시 빈 배열. */
export function listVersions(asset_id: string): Promise<VersionInfo[]> {
  return request<VersionInfo[]>(
    `/api/source/assets/${encodeAssetIdPath(asset_id)}/versions`,
  );
}

/** GET …/versions/{version}/files/{path} — 소스 파일 1개 본문(텍스트). 상세 SKILL.md 표시용. */
export function readSourceFile(
  asset_id: string,
  version: string,
  path: string,
): Promise<{ asset_id: string; version: string; path: string; content: string }> {
  return request(
    `/api/source/assets/${encodeAssetIdPath(asset_id)}/versions/${encodeURIComponent(version)}/files/${encodeAssetIdPath(path)}`,
  );
}

// ---------------------------------------------------------------------------

// 고수준 헬퍼: SOURCE 업로드 시퀀스 (mock vs prod 분기)
// ---------------------------------------------------------------------------

/** 업로드할 소스 파일 (경로 + UTF-8 텍스트 내용). */
export type SourceFile = { path: string; content: string };

export type PublishSourceInput = {
  asset_type: AssetType;
  name: string;
  files: SourceFile[];
  version?: string;
  description?: string;
  owner_team?: string;
  escalation_contact?: string;
  tags?: string[];
  category?: string;
  changelog?: string;      // §12: 버전별 주요 변경사항 요약
};

/**
 * SOURCE 업로드 전체 시퀀스를 한 번에 처리해요. (init -> 업로드 -> finalize)
 *
 * ticket.urls[path] 는 실제 S3 presigned URL 이에요. 각 파일을 fetch PUT 으로 올려요.
 * ApiError 는 그대로 위로 전파해요 (마법사가 잡아서 보여줘요).
 */
export async function publishSource(
  input: PublishSourceInput,
  principal: string,
): Promise<FinalizeResult> {
  const encoder = new TextEncoder();

  // (1) init: 파일을 {path, size} 로 매핑. asset_id/version은 서버가 결정해요.
  const fileSpecs: FileSpec[] = input.files.map((f) => ({
    path: f.path,
    size: encoder.encode(f.content).length,
  }));

  const ticket = await sourceInit(
    {
      asset_type: input.asset_type,
      name: input.name,
      files: fileSpecs,
      version: input.version,
      description: input.description,
      owner_team: input.owner_team,
      escalation_contact: input.escalation_contact,
      tags: input.tags,
      category: input.category,
      changelog: input.changelog,
    },
    principal,
  );

  // 서버가 파생/자동결정한 값을 이후 단계의 진실로 삼아요.
  const assetId = ticket.asset_id;
  const version = ticket.version;

  // (2) 각 파일을 presigned URL 로 직접 PUT 해요.
  for (const file of input.files) {
    const url = ticket.urls[file.path];
    if (!url) {
      throw new ApiError(`업로드 URL 누락: ${file.path}`, 422);
    }
    const res = await fetch(url, { method: "PUT", body: file.content });
    if (!res.ok) {
      throw new ApiError(
        `파일 업로드 실패 (${file.path}): ${res.statusText}`,
        res.status,
      );
    }
  }

  // (3) finalize: 카탈로그 메타는 서버가 STAGING에서 가져와요(durable). 여기선 키만 전달.
  return sourceFinalize(
    { asset_id: assetId, version, upload_id: ticket.upload_id },
    principal,
  );
}

// ---------------------------------------------------------------------------

export function setVersionVisibility(
  assetId: string, version: string, visible: boolean,
): Promise<{ asset_id: string; version: string; search_visible: boolean }> {
  return request(
    `/api/source/assets/${encodeAssetIdPath(assetId)}/versions/${encodeURIComponent(version)}/visibility`,
    { method: "PATCH", headers: JSON_HEADERS, body: JSON.stringify({ visible }) },
  );
}
