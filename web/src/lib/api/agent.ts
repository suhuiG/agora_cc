import { ApiError, JSON_HEADERS, SESSION_PRINCIPAL, jsonHeadersWithPrincipal, request } from "./client";
import type { FileSpec, UploadTicket } from "./types";
import type { SourceFile } from "./source";

// Agent 배포형 (소스 업로드 → AgentCore Runtime 배포 → A2A invoke endpoint)
// ---------------------------------------------------------------------------

/** GET /api/agent/deploy/{job_id} · POST /api/agent/deploy 응답. 폴링으로 phase가 전진해요. */
export type AgentDeployJob = {
  job_id: string;
  phase: string;                   // QUEUED|BUILDING|DEPLOYING|READY|FAILED
  build_type: string;
  runtime_arn: string | null;      // AgentCore Runtime ARN (READY에서 채워져요)
  endpoint: string | null;         // A2A invoke URL — READY가 되면 채워짐
  record_id: string | null;
  error: { phase: string; message: string } | null;
  verify_report: {
    ok?: boolean;
    tools?: string[];
    missing?: string[];
    warnings?: string[];
    reason?: string;
    verdict?: string;
    negative_control_denied?: boolean | null;
    negative_control_verdict?: "denied" | "allowed" | "unknown";
    builtin_observability?: {
      ok?: boolean;
      verdict?: string;
      resources?: Record<string, string>;
      spans?: string;
      metrics?: string;
      reason?: string;
    } | null;
    expected_conversation_manager?: {
      name: string;
      parameters: Record<string, unknown>;
    } | null;
    conversation_manager?: {
      name: string;
      parameters: Record<string, unknown>;
    } | null;
  } | null;
  updated_at: string;
};

export type AgentToolRequest = {
  asset_id: string;
  asset_version: string;
  operation_id: string;
  request_justification: string;
};

/** POST /api/agent/deploy/init 요청 바디. asset_type=agent는 서버가 고정해요. */
export type AgentDeployInitInput = {
  name: string;
  files: FileSpec[];
  description?: string;
  owner_team?: string;
  escalation_contact?: string;
  tags?: string[];
  category?: string;
  deployment_source?: "catalog" | "initializr";
  tool_requests?: AgentToolRequest[];
  /**
   * 사용 모델 id(MODELS 의 id, 예 "sonnet-5"). Initializr 경로에서만 보내요 — 소스를 직접
   * 올리는 배포는 코드가 어떤 모델을 쓰는지 Agora가 알 수 없어요.
   *
   * 서버가 descriptors.agent.model 로 저장해 자산상세·거버넌스 카드의 모델 칸 데이터가 돼요.
   * 서버는 allowlist 검증을 하므로 임의 문자열을 보내면 422 예요.
   */
  model?: string;
};

/** POST /api/agent/deploy/init — 배포형 agent 소스 업로드 티켓(presigned URL들)을 발급받아요. */
export function deployAgentInit(
  input: AgentDeployInitInput,
  principal: string,
): Promise<UploadTicket> {
  return request<UploadTicket>("/api/agent/deploy/init", {
    method: "POST",
    headers: jsonHeadersWithPrincipal(principal),
    body: JSON.stringify(input),
  });
}

/** POST /api/agent/deploy/finalize — 업로드 확정(PUBLISHED). 카탈로그 등재는 배포 job이 완주 후에 해요. */
export function deployAgentFinalize(
  input: { asset_id: string; version: string; upload_id: string },
  principal: string,
): Promise<{ asset_id: string; version: string; files: string[] }> {
  return request("/api/agent/deploy/finalize", {
    method: "POST",
    headers: jsonHeadersWithPrincipal(principal),
    body: JSON.stringify(input),
  });
}

/** POST /api/agent/deploy — 확정된 agent 소스 버전으로 배포 job을 시작해요(승인 게이트 → QUEUED).
 *
 * redeploy_record_id를 주면 새 runtime을 만들지 않고 기존 것을 갱신해요(재배포).
 * 이름·endpoint가 유지되고 리뷰·조회수도 남아요. 소유자만 가능해요.
 */
export function startAgentDeploy(
  asset_id: string,
  version: string,
  principal: string,
  redeployRecordId?: string,
  requestId?: string,
): Promise<AgentDeployJob> {
  return request<AgentDeployJob>("/api/agent/deploy", {
    method: "POST",
    headers: jsonHeadersWithPrincipal(principal),
    body: JSON.stringify({
      asset_id, version,
      ...(redeployRecordId ? { redeploy_record_id: redeployRecordId } : {}),
      ...(requestId ? { request_id: requestId } : {}),
    }),
  });
}

/** GET /api/agent/deploy/{job_id} — 배포 job 상태를 조회해요(호출마다 한 단계 전진). */
export function pollAgentDeploy(
  job_id: string,
  signal?: AbortSignal,
): Promise<AgentDeployJob> {
  return request<AgentDeployJob>(
    `/api/agent/deploy/${encodeURIComponent(job_id)}`,
    { signal },
  );
}

/** 이름을 쓸 수 없는 사유. rejected_remains는 카탈로그에 보이지 않는 반려·폐기 자산이
 * 이름을 점유한 경우예요. */
export type NameConflictReason =
  | ""
  | "runtime_exists"
  | "active_record"
  | "rejected_remains";

export type AgentNameCheck = {
  available: boolean;
  reason: NameConflictReason;
  conflicting_status: string;
};

/** 사유별 사용자 안내. 서버 문구와 같은 뜻을 유지해요. */
export function nameConflictMessage(reason: NameConflictReason): string {
  if (reason === "rejected_remains") {
    return "반려·폐기된 동명 자산이 남아 있어 이 이름을 쓸 수 없어요. 관리자에게 정리를 요청하거나 다른 이름을 써주세요.";
  }
  return "이미 사용 중인 이름이에요. 다른 이름을 써주세요.";
}

/** GET /api/agent/name-available?name= — 배포 전 이름 중복 사전 체크(blur 시점). */
export function checkAgentName(name: string): Promise<AgentNameCheck> {
  return request<AgentNameCheck>(
    `/api/agent/name-available?name=${encodeURIComponent(name)}`,
  );
}

/**
 * 폴더 업로드 전체 시퀀스: init → presigned PUT → finalize (agent 배포형 전용).
 * uploadMcpFolder와 동일 패턴이고, agent 전용 엔드포인트만 달라요.
 * 반환한 {asset_id, version}으로 startAgentDeploy를 호출해요.
 */
export async function uploadAgentFolder(
  input: {
    name: string;
    files: SourceFile[];
    description?: string;
    owner_team?: string;
    escalation_contact?: string;
    tags?: string[];
    category?: string;
    deployment_source?: "catalog" | "initializr";
    tool_requests?: AgentToolRequest[];
    model?: string;
  },
  principal: string,
): Promise<{ asset_id: string; version: string }> {
  const encoder = new TextEncoder();
  const fileSpecs: FileSpec[] = input.files.map((f) => ({
    path: f.path,
    size: encoder.encode(f.content).length,
  }));
  const ticket = await deployAgentInit(
    {
      name: input.name,
      files: fileSpecs,
      description: input.description,
      owner_team: input.owner_team,
      escalation_contact: input.escalation_contact,
      tags: input.tags,
      category: input.category,
      deployment_source: input.deployment_source,
      tool_requests: input.tool_requests,
      model: input.model,
    },
    principal,
  );
  // 파일마다 순차 `await` 였어요 — presigned PUT 왕복이 파일 수만큼 직렬로 쌓여서
  // scaffold 12개면 체감 지연의 대부분이 여기였어요(2026-08-30). 병렬로 보내요.
  //
  // 무제한이 아니라 6개씩이에요. 재배포 모달은 사용자가 고른 폴더를 그대로 올려서 파일 수가
  // 클 수 있고, 그때 브라우저 연결 한도에 걸리면 오히려 느려져요.
  const UPLOAD_CONCURRENCY = 6;
  const queue = [...input.files];
  async function drain(): Promise<void> {
    for (let file = queue.shift(); file; file = queue.shift()) {
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
  }
  await Promise.all(
    Array.from({ length: Math.min(UPLOAD_CONCURRENCY, input.files.length) }, drain),
  );
  await deployAgentFinalize(
    { asset_id: ticket.asset_id, version: ticket.version, upload_id: ticket.upload_id },
    principal,
  );
  return { asset_id: ticket.asset_id, version: ticket.version };
}

// ── A2A Agent (도메인 연결) ──────────────────────────────────────────
export type AgentSkillPreview = { id: string; name: string; description: string; tags: string[] };

export type AgentConnectTestResult = {
  ok: boolean;
  name: string | null;
  description: string | null;
  version: string | null;
  protocol_version: string | null;
  url: string | null;
  capabilities: Record<string, unknown>;
  security_schemes: string[];
  skills: AgentSkillPreview[];
};

/** POST /api/agent/connect-test — 도메인의 agent-card를 GET해 정체성·skills를 미리 가져와요. */
export function connectTestAgent(endpoint: string): Promise<AgentConnectTestResult> {
  return request<AgentConnectTestResult>("/api/agent/connect-test", {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify({ endpoint }),
  });
}

export type AgentRegisterInput = {
  endpoint: string;
  name?: string;
  description?: string;
  owner_team?: string;
  escalation_contact?: string;
  tags?: string[];
  category?: string;
};

export type AgentRegisterResult = {
  record_id: string;
  name: string;
  status: string;
  message: string;
};

/** POST /api/agent/register — 도메인 연결형 A2A agent 등록. */
export function registerAgent(input: AgentRegisterInput): Promise<AgentRegisterResult> {
  return request<AgentRegisterResult>("/api/agent/register", {
    method: "POST",
    headers: jsonHeadersWithPrincipal(SESSION_PRINCIPAL),
    body: JSON.stringify(input),
  });
}

/** 버전 노출 토글. PATCH /versions/{version}/visibility */
