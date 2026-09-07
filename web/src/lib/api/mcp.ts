import { ApiError, JSON_HEADERS, SESSION_PRINCIPAL, jsonHeadersWithPrincipal, request } from "./client";
import type { FileSpec, UploadTicket } from "./types";
import type { SourceFile } from "./source";
import { buildMcpDeployStartBody } from "../mcpDeploySelection";

export type McpRegisterInput = {
  /**
   * 연결형만 지원해요. 옛 `"deploy"` 모드는 서버가 410으로 거부해요 — 배포형 MCP는
   * 소스 업로드 흐름(`deployMcpInit` 등)이 담당해요. 타입에서 지워 컴파일 단계에서 막아요.
   */
  mode: "connect";
  name: string;
  endpoint?: string;
  description?: string;
  owner_team?: string;
  escalation_contact?: string;
  tags?: string[];
  category?: string;
};

export type McpRegisterResult = {
  record_id: string;
  name: string;
  // 연결형만 남아서 항상 "hosted"예요(옛 "pending"은 제거된 deploy 모드의 값이었어요).
  hosting: "hosted";
  status: string;
  message: string;
};

/** POST /api/mcp/register — 중앙 호스팅 MCP 등록. */
export function registerMcp(input: McpRegisterInput): Promise<McpRegisterResult> {
  return request<McpRegisterResult>("/api/mcp/register", {
    method: "POST",
    headers: jsonHeadersWithPrincipal(SESSION_PRINCIPAL),
    body: JSON.stringify(input),
  });
}

export type McpToolPreview = {
  name: string;
  description: string;
  // MCP tool sensitivity 태그(READ/CREATE/UPDATE/DELETE, 미태깅이면 ""). tools-preview가 내려줘요.
  sensitivity?: string;
  sensitivityRationale?: string;
};

export type McpConnectTestResult = {
  ok: boolean;
  name: string | null;
  instructions: string | null;
  tools: McpToolPreview[];
};

/** POST /api/mcp/connect-test — endpoint에 핸드셰이크해 tool 목록을 미리 가져와요(등록 안 함). */
export function connectTestMcp(endpoint: string): Promise<McpConnectTestResult> {
  return request<McpConnectTestResult>("/api/mcp/connect-test", {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify({ endpoint }),
  });
}

// ---------------------------------------------------------------------------
// MCP 배포형 (소스 업로드 → Runtime 배포 → Gateway endpoint)
// ---------------------------------------------------------------------------

/** GET /api/mcp/deploy/{job_id} · POST /api/mcp/deploy 응답. 폴링으로 phase가 전진해요. */
export type McpDeployJob = {
  job_id: string;
  phase: string;                 // QUEUED|BUILDING|REGISTERING_TARGET|READY|FAILED
  build_type: string;
  endpoint: string | null;       // 불변 Gateway URL (D1 리팩터 후 backend의 gateway_url을 endpoint 키로 내려요)
  record_id: string | null;
  error: { phase: string; message: string } | null;
  updated_at: string;
};

/** POST /api/mcp/deploy/init 요청 바디. asset_type=mcp는 서버가 고정해요. */
export type McpDeployInitInput = {
  name: string;
  files: FileSpec[];
  description?: string;
  owner_team?: string;
  escalation_contact?: string;
  tags?: string[];
  category?: string;
};

/** POST /api/mcp/deploy/init — 배포형 MCP 소스 업로드 티켓(presigned URL들)을 발급받아요. */
export function deployMcpInit(
  input: McpDeployInitInput,
  principal: string,
): Promise<UploadTicket> {
  return request<UploadTicket>("/api/mcp/deploy/init", {
    method: "POST",
    headers: jsonHeadersWithPrincipal(principal),
    body: JSON.stringify(input),
  });
}

/** POST /api/mcp/deploy/finalize — 업로드 확정(PUBLISHED). 카탈로그 등재는 배포 job이 완주 후에 해요. */
export function deployMcpFinalize(
  input: { asset_id: string; version: string; upload_id: string },
  principal: string,
): Promise<{ asset_id: string; version: string; files: string[] }> {
  return request("/api/mcp/deploy/finalize", {
    method: "POST",
    headers: jsonHeadersWithPrincipal(principal),
    body: JSON.stringify(input),
  });
}

/** POST /api/mcp/deploy — 확정된 소스 버전으로 배포 job을 시작해요(승인 게이트 → QUEUED).
 *
 * redeployRecordId를 주면 새 Lambda/Gateway target을 만들지 않고 기존 것을 코드만 갱신해요(재배포).
 * record_id·endpoint(불변 Gateway URL)가 유지되고 리뷰·조회수도 남아요. 소유자만 가능해요(ADR-0021).
 */
export function startMcpDeploy(
  asset_id: string,
  version: string,
  selected_tools: string[],
  principal: string,
  redeployRecordId?: string,
): Promise<McpDeployJob> {
  return request<McpDeployJob>("/api/mcp/deploy", {
    method: "POST",
    headers: jsonHeadersWithPrincipal(principal),
    body: JSON.stringify({
      ...buildMcpDeployStartBody(asset_id, version, selected_tools),
      ...(redeployRecordId ? { redeploy_record_id: redeployRecordId } : {}),
    }),
  });
}

/** GET /api/mcp/deploy/{job_id} — 배포 job 상태를 조회해요(호출마다 한 단계 전진). */
export function pollMcpDeploy(job_id: string): Promise<McpDeployJob> {
  return request<McpDeployJob>(`/api/mcp/deploy/${encodeURIComponent(job_id)}`);
}

/** POST /api/mcp/deploy/tools-preview — 업로드된 소스를 정적 파싱해 tool 목록을 미리 봐요. */
export function previewMcpDeployTools(
  asset_id: string,
  version: string,
  principal: string,
): Promise<{ tools: McpToolPreview[] }> {
  return request("/api/mcp/deploy/tools-preview", {
    method: "POST",
    headers: jsonHeadersWithPrincipal(principal),
    body: JSON.stringify({ asset_id, version }),
  });
}

/**
 * 폴더 업로드 전체 시퀀스: init → presigned PUT → finalize.
 * ⚠️ 머지(AWS 단일화)로 file://·demoStage 분기는 제거됐어요 — 항상 presigned PUT이에요.
 * publishSource(SOURCE 업로드)와 동일 패턴이고, 배포형 MCP 전용 엔드포인트만 달라요.
 * 반환한 {asset_id, version}으로 startMcpDeploy를 호출해요.
 */
export async function uploadMcpFolder(
  input: {
    name: string;
    files: SourceFile[];
    description?: string;
    owner_team?: string;
    escalation_contact?: string;
    tags?: string[];
    category?: string;
  },
  principal: string,
): Promise<{ asset_id: string; version: string }> {
  const encoder = new TextEncoder();
  const fileSpecs: FileSpec[] = input.files.map((f) => ({
    path: f.path,
    size: encoder.encode(f.content).length,
  }));
  const ticket = await deployMcpInit(
    {
      name: input.name,
      files: fileSpecs,
      description: input.description,
      owner_team: input.owner_team,
      escalation_contact: input.escalation_contact,
      tags: input.tags,
      category: input.category,
    },
    principal,
  );
  for (const file of input.files) {
    const url = ticket.urls[file.path];
    if (!url) throw new ApiError(`업로드 URL 누락: ${file.path}`, 422);
    const res = await fetch(url, { method: "PUT", body: file.content });
    if (!res.ok) {
      throw new ApiError(
        `파일 업로드 실패 (${file.path}): ${res.statusText}`,
        res.status,
      );
    }
  }
  await deployMcpFinalize(
    { asset_id: ticket.asset_id, version: ticket.version, upload_id: ticket.upload_id },
    principal,
  );
  return { asset_id: ticket.asset_id, version: ticket.version };
}

// ---------------------------------------------------------------------------
