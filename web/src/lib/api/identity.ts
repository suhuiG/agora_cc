import { JSON_HEADERS, request } from "./client";
import type {
  SensitivityObservationStatus,
  SensitivitySource,
} from "../sensitivitySource";

// ---------------------------------------------------------------------------
// 접근 권한 관리 — admin 설정과 사용자 본인 조회/소유 자산 policy.
// ---------------------------------------------------------------------------

export type AccessConnection = {
  connection_id: string;
  name: string;
  kind: string;
  target: string;
  credential_mode: string;
  role_arn?: string;
  external_id_ref?: string;
  ceiling: string[];
  status: string;
  enforcement: string;
  created_by: string;
  created_at: string;
  updated_at: string;
};

export type AccessConnectionCreateInput = {
  name: string;
  kind: string;
  target: string;
  credential_mode: string;
  role_arn?: string;
  external_id_ref?: string;
  ceiling: string[];
  enforcement: string;
};

export type AccessConnectionPatchInput = Partial<
  Pick<
    AccessConnection,
    | "name"
    | "target"
    | "ceiling"
    | "status"
    | "enforcement"
  >
> & {
  role_arn?: string | null;
  external_id_ref?: string | null;
};

export type AccessCapability = {
  name: string;
  description: string;
  operations: string[];
  status: string;
};

// 낙관적 락(결함 #9): admin capability 목록 GET/PUT은 목록과 함께 version을 실어요.
// 호출부가 version을 보관했다가 다음 PUT에 expected_version으로 되돌려 줘야 lost update가
// 409로 막혀요. version을 버리면 백엔드 락이 무용지물이 돼요.
export type AccessCapabilityList = {
  items: AccessCapability[];
  version: number;
};

export type AccessConnectionSummary = Pick<
  AccessConnection,
  "connection_id" | "name" | "kind" | "status" | "enforcement"
>;

export type AccessCapabilitySummary = Pick<
  AccessCapability,
  "name" | "description" | "status"
>;

/**
 * ⑦ grant 한 행 — 서버 `identity/models.py` 의 `AccessGrant` dataclass 그대로예요.
 *
 * `GET /api/admin/access-grants` 에는 `response_model` 이 없어서 FastAPI 가 dataclass 를
 * 통째로 직렬화하고, BFF(`app/api/backend/[...path]/route.ts`)는 upstream body 를 그대로
 * 흘려보내요. 그래서 아래 필드는 «이미 오고 있는» 값이에요 — 새 API 호출이 필요하지 않아요.
 *
 * 현행 행과 옛 행이 한 목록에 섞여 와요. 가르는 기준은 `asset_id`·`operation_id` 예요:
 * 서버 `store.grant_key` 가 그 둘로 정렬 키(`GRANT#<asset>#<operation>`)를 만들고, 비어
 * 있으면 `ValueError` 로 거부해요. 즉 둘 중 하나라도 비면 소비자 경로(interceptor 의 정확 키
 * `GetItem`)에 **보이지 않아요**.
 */
export type AccessGrant = {
  grant_id: string;
  /** 주체가 사람일 때의 Cognito sub. 그룹 행은 빈 문자열이에요. */
  principal_id: string;
  status: string;
  expires_at?: number;
  version: number;
  granted_by: string;
  created_at: string;
  updated_at: string;
  /** 주체가 그룹일 때의 Cognito 그룹 이름 (`principal_id` 와 둘 중 하나만 채워요). */
  subject_group?: string;
  /** ⑦ 키의 재료. 옛 행에는 없거나 빈 문자열이에요. */
  asset_id?: string;
  operation_id?: string;
  /** 승인 당시 자산 버전 — 감사용이고 판정에는 쓰지 않아요(ADR-0099 결정 13). */
  asset_version?: string;
  // --- 아래 둘은 legacy 예요. 인가 경로에서 읽지 않아요. ---
  connection_id: string;
  capabilities: string[];
};

export type AccessGrantInput = {
  principal_id: string;
  connection_id: string;
  capabilities: string[];
  expires_at?: number;
};

export type AccessGrantReissueInput = {
  expected_version: number;
  expected_capabilities_version: number;
  capabilities: string[];
};

export type AssetCapabilityPolicy = {
  asset_id: string;
  asset_version: string;
  operation_id: string;
  connection_id: string;
  required_capabilities: string[];
  status: string;
  approved_by?: string;
  version: number;
  updated_at: string;
};

// ---------------------------------------------------------------------------
// Agent별 tool binding · 호출 자격 (Axis 1 M1).
// ---------------------------------------------------------------------------

export type AgentToolBinding = {
  agent_record_id: string;
  asset_id: string;
  asset_version: string;
  operation_id: string;
  gateway_id: string;
  gateway_target_name: string;
  gateway_action: string;
  approval_state: "REQUESTED" | "APPROVED" | "REJECTED";
  desired_state: "ALLOWED" | "REVOKED";
  effective_state: "PENDING" | "ACTIVE" | "REVOKED" | "FAILED" | "UNKNOWN";
  policy_revision: number;
  created_by: string;
  updated_by: string;
  approved_by?: string | null;
  approved_at?: string | null;
  sensitivity: string;
  /** 현재 drift 원장에서 다시 읽은 값. 위 `sensitivity`는 신청 시점 복사본이에요. */
  current_sensitivity?: string;
  sensitivity_source?: SensitivitySource;
  sensitivity_status?: SensitivityObservationStatus;
  sensitivity_reason?: string;
  request_justification: string;
};

export type AgentInvokeAuthorization = {
  agent_id: string;
  owner_principal_id: string;
  allowed_principals: string[];
  allowed_groups: string[];
  default_effect: "DENY";
  updated_by: string;
  updated_at: string;
  // 권한 그룹(tool sensitivity 상한): ReadOnly ⊂ ReadCreate ⊂ ReadWrite ⊂ FullAccess.
  // Cedar 컴파일러가 이 상한 안의 sensitivity tool만 남겨요(ADR-0018/0020).
  permission_group?: string;
  permission_group_justification?: string;
};

export type AgentPolicyDeployment = {
  agent_record_id: string;
  revision: number;
  gateway_id: string;
  policy_id: string;
  policy_hash: string;
  cedar_policy: string;
  action_count: number;
  mode: "LOG_ONLY" | "ENFORCE";
  status: "PENDING" | "ACTIVE" | "SUPERSEDED" | "FAILED";
  created_at: string;
  created_by: string;
  validation_findings: string[];
  agentcore_policy_id: string;
  deployed_policy_hash: string;
  deployed_at: string;
};

// 서버 `AgentPolicyDeployOutcome`(api/src/agora/domains/identity/models.py)와 **같은 집합**
// 이어야 해요. 빠진 값이 오면 `Record<AgentPolicyDeployOutcome, string>` 조회가 `undefined` 를
// 돌려주고, 그게 화면에서 빈 문단이나 리터럴 `undefined` 로 나가요 (IH-162 ①).
// `agentPolicyDeployment.test.ts` 가 서버 enum 파일을 직접 읽어 이 집합과 대조해요.
export type AgentPolicyDeployOutcome =
  | "DEPLOYED_ACTIVE"
  | "DEPLOY_IN_PROGRESS"
  | "DEPLOY_FAILED"
  | "SKIPPED_NO_IDENTITY"
  | "SKIPPED_NO_TOOL_ACCESS"
  | "SKIPPED_NO_DEPLOYER"
  | "SKIPPED_PER_AGENT_DEPRECATED";

export type AgentPolicyDeployResult = {
  outcome: AgentPolicyDeployOutcome;
  deployment: AgentPolicyDeployment | null;
};

export type AgentPermissionGroupUpdate = AgentInvokeAuthorization & {
  policy_deployment: AgentPolicyDeployResult;
};

export type AgentToolBindingApproval = AgentToolBinding & {
  policy_deployment: AgentPolicyDeployResult;
};

export type AgentPolicyReconciliation = {
  desired_actions: string[];
  desired_hash: string | null;
  no_tool_access: boolean;
  latest_deployment: AgentPolicyDeployment | null;
  in_sync: boolean;
  revision_lag: number;
  // per-agent Cedar 층이 폐기됐다는 «고유» 상태예요 (ADR-0093 · ADR-0112). `not_applicable`
  // (볼 게 없음)·`drift`·`unknown`(관측 실패)과 섞으면 화면이 배선 오류처럼 그려요.
  //
  // 값 상수는 `lib/agentPolicyDeployment.ts` 의 `PER_AGENT_POLICY_DEPRECATED_STATUS` 예요.
  // 이 모듈에 두지 않는 이유는 여기서 `./client` 를 확장자 없이 import 해서 `node --test` 가
  // 런타임 값을 못 가져오기 때문이에요 — 그러면 서버 상수와 대조하는 테스트를 쓸 수 없어요.
  status?: string;
  reason?: string;
};

export type AgentToolProposal = {
  asset_id: string;
  asset_version: string;
  operation_id: string;
  desired_state?: "ALLOWED";
  request_justification: string;
};

export type AgentToolCandidateOperation = {
  operationId: string;
  bindingState: "NONE" | "REQUESTED" | "APPROVED" | "REJECTED";
  desiredState: "" | "ALLOWED" | "REVOKED";
  ready: boolean;
  // MCP tool sensitivity 태그(READ/CREATE/UPDATE/DELETE, 미태깅이면 "").
  sensitivity?: string;
  sensitivitySource?: SensitivitySource;
};

export type AgentToolCandidateAsset = {
  assetId: string;
  assetName: string;
  version: string;
  approved: boolean;
  gatewayConnected: boolean;
  found: boolean;
  sensitivityStatus?: SensitivityObservationStatus;
  sensitivityReason?: string;
  operations: AgentToolCandidateOperation[];
};

export type AgentToolCandidates = {
  agentId: string;
  mcpAssets: AgentToolCandidateAsset[];
  dependencyWarning?: string;
};

export type AgentIdentity = {
  agentRecordId: string;
  identityType: "MANAGED_RUNTIME_ROLE" | "EXTERNAL_IAM_ROLE";
  status: "PROVISIONING" | "ACTIVE" | "FAILED" | "REVOKED";
  workloadIdentityName: string | null;
  runtimeRoleArn: string | null;
  externalSourceRoleArn: string | null;
  gatewayRoleArn: string | null;
  policyPrincipalId: string | null;
  verifiedAt: string | null;
};

/** GET /api/assets/{agent_id}/tool-bindings — agent별 operation binding 목록. */
export async function listAgentToolBindings(
  agentId: string,
): Promise<AgentToolBinding[]> {
  return request<AgentToolBinding[]>(
    `/api/assets/${encodeURIComponent(agentId)}/tool-bindings`,
  );
}

/** GET /api/assets/{agent_id}/tool-candidates — 선언된 MCP operation과 binding 상태. */
export function getAgentToolCandidates(
  agentId: string,
): Promise<AgentToolCandidates> {
  return request<AgentToolCandidates>(
    `/api/assets/${encodeURIComponent(agentId)}/tool-candidates`,
  );
}

/** PUT /api/assets/{agent_id}/tool-bindings/... — tool 허용 또는 회수 요청. */
export function putAgentToolBinding(
  agentId: string,
  assetId: string,
  assetVersion: string,
  operationId: string,
  desiredState: AgentToolBinding["desired_state"],
  requestJustification: string,
): Promise<AgentToolBinding> {
  return request<AgentToolBinding>(
    `/api/assets/${encodeURIComponent(agentId)}/tool-bindings/${encodeURIComponent(assetId)}/${encodeURIComponent(assetVersion)}/${encodeURIComponent(operationId)}`,
    {
      method: "PUT",
      headers: JSON_HEADERS,
      body: JSON.stringify({
        desired_state: desiredState,
        request_justification: requestJustification,
      }),
    },
  );
}

/** GET /api/assets/{agent_id}/agent-identity — IAM role/Cedar principal 조회. */
export function getAgentIdentity(agentId: string): Promise<AgentIdentity> {
  return request<AgentIdentity>(
    `/api/assets/${encodeURIComponent(agentId)}/agent-identity`,
  );
}

/** POST /api/admin/assets/{agent_id}/tool-bindings/.../approve — 요청 승인(admin). */
export function approveAgentToolBinding(
  binding: AgentToolBinding,
): Promise<AgentToolBindingApproval> {
  return request<AgentToolBindingApproval>(
    `/api/admin/assets/${encodeURIComponent(binding.agent_record_id)}/tool-bindings/${encodeURIComponent(binding.asset_id)}/${encodeURIComponent(binding.asset_version)}/${encodeURIComponent(binding.operation_id)}/approve`,
    { method: "POST", headers: JSON_HEADERS },
  );
}

/** POST /api/admin/assets/{agent_id}/tool-bindings/.../reject — 요청 반려(admin). */
export function rejectAgentToolBinding(binding: AgentToolBinding): Promise<AgentToolBinding> {
  return request<AgentToolBinding>(
    `/api/admin/assets/${encodeURIComponent(binding.agent_record_id)}/tool-bindings/${encodeURIComponent(binding.asset_id)}/${encodeURIComponent(binding.asset_version)}/${encodeURIComponent(binding.operation_id)}/reject`,
    { method: "POST", headers: JSON_HEADERS },
  );
}

/** GET /api/assets/{agent_id}/policy-reconciliation — desired와 최신 배포 policy 비교. */
export function getAgentPolicyReconciliation(
  agentId: string,
): Promise<AgentPolicyReconciliation> {
  return request<AgentPolicyReconciliation>(
    `/api/assets/${encodeURIComponent(agentId)}/policy-reconciliation`,
  );
}

/** POST /api/admin/assets/{agent_id}/policy/deploy — policy 재컴파일·배포(admin). */
export function deployAgentPolicy(
  agentId: string,
): Promise<AgentPolicyDeployResult> {
  return request<AgentPolicyDeployResult>(
    `/api/admin/assets/${encodeURIComponent(agentId)}/policy/deploy`,
    { method: "POST", headers: JSON_HEADERS },
  );
}

/** GET /api/assets/{agent_id}/invoke-authorization — 호출 allowlist와 owner. */
export function getAgentInvokeAuthorization(
  agentId: string,
): Promise<AgentInvokeAuthorization> {
  return request<AgentInvokeAuthorization>(
    `/api/assets/${encodeURIComponent(agentId)}/invoke-authorization`,
  );
}

/** PUT /api/assets/{agent_id}/invoke-authorization — 호출 allowlist 저장. */
export function putAgentInvokeAuthorization(
  agentId: string,
  body: Pick<AgentInvokeAuthorization, "allowed_principals" | "allowed_groups">,
): Promise<AgentInvokeAuthorization> {
  return request<AgentInvokeAuthorization>(
    `/api/assets/${encodeURIComponent(agentId)}/invoke-authorization`,
    {
      method: "PUT",
      headers: JSON_HEADERS,
      body: JSON.stringify(body),
    },
  );
}

/**
 * PUT /api/admin/assets/{agent_id}/permission-group — Agent의 tool sensitivity 상한 설정(admin).
 *
 * ReadWrite·FullAccess는 justification이 비면 서버가 422를 돌려줘요. 설정 즉시 서버가
 * Cedar policy를 다시 컴파일하므로, 호출부는 invoke-authorization·policy-reconciliation을
 * 재검증해요. 갱신된 invoke-authorization와 policy 배포 결과를 반환해요.
 */
export function setAgentPermissionGroup(
  agentId: string,
  body: { permission_group: string; justification: string },
): Promise<AgentPermissionGroupUpdate> {
  return request<AgentPermissionGroupUpdate>(
    `/api/admin/assets/${encodeURIComponent(agentId)}/permission-group`,
    {
      method: "PUT",
      headers: JSON_HEADERS,
      body: JSON.stringify(body),
    },
  );
}

type ItemList<T> = T[] | { items: T[] };

function itemsFrom<T>(response: ItemList<T>): T[] {
  return Array.isArray(response) ? response : response.items;
}

/** GET /api/admin/access/connections — 등록된 업무 시스템 connection 목록. */
export async function listAccessConnections(): Promise<AccessConnection[]> {
  return itemsFrom(
    await request<ItemList<AccessConnection>>("/api/admin/access/connections"),
  );
}

/** POST /api/admin/access/connections — connection 생성(admin). */
export function createAccessConnection(
  body: AccessConnectionCreateInput,
): Promise<AccessConnection> {
  return request<AccessConnection>("/api/admin/access/connections", {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify(body),
  });
}

/** POST /api/admin/access/capability-sets/seed-presets — 예제 권한 그룹 멱등 시드. */
export function seedCapabilityPresets(): Promise<{
  created: string[];
  skipped: string[];
}> {
  return request("/api/admin/access/capability-sets/seed-presets", {
    method: "POST",
    headers: JSON_HEADERS,
  });
}

/** PATCH /api/admin/access/connections/{id} — connection 일부 수정(admin). */
export function updateAccessConnection(
  connectionId: string,
  body: AccessConnectionPatchInput,
): Promise<AccessConnection> {
  return request<AccessConnection>(
    `/api/admin/access/connections/${encodeURIComponent(connectionId)}`,
    {
      method: "PATCH",
      headers: JSON_HEADERS,
      body: JSON.stringify(body),
    },
  );
}

/** GET /api/admin/access/connections/{id}/capabilities — 목록만 필요할 때. */
export async function listAccessCapabilities(
  connectionId: string,
): Promise<AccessCapability[]> {
  return (await listAccessCapabilityList(connectionId)).items;
}

/**
 * GET /api/admin/access/connections/{id}/capabilities — 목록 + version.
 *
 * 낙관적 락(결함 #9)을 왕복하려는 호출부(capability 편집 화면)는 이걸 써서 version을
 * 보관했다가 PUT 시 expected_version으로 넘겨요. version이 필요 없는 조회는
 * `listAccessCapabilities`로 items만 받아요.
 */
export async function listAccessCapabilityList(
  connectionId: string,
): Promise<AccessCapabilityList> {
  const response = await request<AccessCapabilityList>(
    `/api/admin/access/connections/${encodeURIComponent(connectionId)}/capabilities`,
  );
  return { items: response.items ?? [], version: response.version ?? 0 };
}

/** GET /api/access/connections — policy 작성에 필요한 비민감 connection 목록. */
export async function listAvailableAccessConnections(): Promise<
  AccessConnectionSummary[]
> {
  return itemsFrom(
    await request<ItemList<AccessConnectionSummary>>("/api/access/connections"),
  );
}

/** GET /api/access/connections/{id}/capabilities — 활성 capability 공개 필드. */
export async function listAvailableAccessCapabilities(
  connectionId: string,
): Promise<AccessCapabilitySummary[]> {
  return itemsFrom(
    await request<ItemList<AccessCapabilitySummary>>(
      `/api/access/connections/${encodeURIComponent(connectionId)}/capabilities`,
    ),
  );
}

/**
 * PUT /api/admin/access/connections/{id}/capabilities — 목록 전체 교체(admin).
 *
 * `expectedVersion`을 주면 백엔드가 저장된 목록 version과 대조해, 그새 다른 admin이
 * 저장했으면 409를 돌려줘요(낙관적 락, 결함 #9). GET에서 받은 version을 그대로 넘기세요.
 * 생략하면 조건 없는 하위호환 교체(무조건 덮어쓰기)라 락이 걸리지 않아요.
 */
export async function putAccessCapabilities(
  connectionId: string,
  items: AccessCapability[],
  expectedVersion?: number,
): Promise<AccessCapabilityList> {
  const response = await request<AccessCapabilityList>(
    `/api/admin/access/connections/${encodeURIComponent(connectionId)}/capabilities`,
    {
      method: "PUT",
      headers: JSON_HEADERS,
      body: JSON.stringify(
        expectedVersion === undefined
          ? { items }
          : { items, expected_version: expectedVersion },
      ),
    },
  );
  return { items: response.items ?? [], version: response.version ?? 0 };
}

/** GET /api/admin/access-grants — 사용자 grant 목록(admin). */
export async function listAccessGrants(filters?: {
  principalId?: string;
  connectionId?: string;
}): Promise<AccessGrant[]> {
  const params = new URLSearchParams();
  if (filters?.principalId) params.set("principal_id", filters.principalId);
  if (filters?.connectionId) params.set("connection_id", filters.connectionId);
  const query = params.size > 0 ? `?${params.toString()}` : "";
  return itemsFrom(
    await request<ItemList<AccessGrant>>(`/api/admin/access-grants${query}`),
  );
}

/** POST /api/admin/access-grants — Cognito sub에 capability grant 부여(admin). */
export function createAccessGrant(body: AccessGrantInput): Promise<AccessGrant> {
  return request<AccessGrant>("/api/admin/access-grants", {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify(body),
  });
}

/** POST /api/admin/access-grants/{id}/reissue — 기존 grant를 조건부 원자 갱신. */
export function reissueAccessGrant(
  grantId: string,
  body: AccessGrantReissueInput,
): Promise<AccessGrant> {
  return request<AccessGrant>(
    `/api/admin/access-grants/${encodeURIComponent(grantId)}/reissue`,
    {
      method: "POST",
      headers: JSON_HEADERS,
      body: JSON.stringify(body),
    },
  );
}

/** DELETE /api/admin/access-grants/{id} — grant 즉시 회수(admin). */
export function deleteAccessGrant(grantId: string): Promise<unknown> {
  return request(
    `/api/admin/access-grants/${encodeURIComponent(grantId)}`,
    { method: "DELETE" },
  );
}

/** GET /api/me/access-grants — 현재 Cognito principal의 유효 grant. */
export async function listMyAccessGrants(): Promise<AccessGrant[]> {
  return itemsFrom(
    await request<ItemList<AccessGrant>>("/api/me/access-grants"),
  );
}

/** GET /api/assets/{asset_id}/capabilities — 자산 operation별 요구 권한. */
export async function listAssetCapabilityPolicies(
  assetId: string,
): Promise<AssetCapabilityPolicy[]> {
  return itemsFrom(
    await request<ItemList<AssetCapabilityPolicy>>(
      `/api/assets/${encodeURIComponent(assetId)}/capabilities`,
    ),
  );
}

/**
 * PUT /api/assets/{asset_id}/capabilities/{operation_id} — 정책 생성/수정.
 *
 * 기존 정책을 수정할 때는 `expectedVersion`(GET에서 받은 policy.version)을 함께 넘겨요 —
 * 그새 다른 사람이 저장했으면 백엔드가 409를 돌려줘요(낙관적 락, 결함 #9). 신규 생성이면
 * 볼 version이 없으니 생략하고, 백엔드가 조건 없이 version 1로 저장해요.
 */
export function putAssetCapabilityPolicy(
  assetId: string,
  operationId: string,
  body: { connection_id: string; required_capabilities: string[] },
  expectedVersion?: number,
  expectedCapabilitiesVersion?: number,
): Promise<AssetCapabilityPolicy> {
  const versionedBody = {
    ...body,
    ...(expectedVersion === undefined
      ? {}
      : { expected_version: expectedVersion }),
    ...(expectedCapabilitiesVersion === undefined
      ? {}
      : { expected_capabilities_version: expectedCapabilitiesVersion }),
  };
  return request<AssetCapabilityPolicy>(
    `/api/assets/${encodeURIComponent(assetId)}/capabilities/${encodeURIComponent(operationId)}`,
    {
      method: "PUT",
      headers: JSON_HEADERS,
      body: JSON.stringify(versionedBody),
    },
  );
}

/** POST /api/admin/assets/{asset_id}/capabilities/{operation_id}/approve. */
export function approveAssetCapabilityPolicy(
  assetId: string,
  operationId: string,
  expectedVersion: number,
): Promise<AssetCapabilityPolicy> {
  return request<AssetCapabilityPolicy>(
    `/api/admin/assets/${encodeURIComponent(assetId)}/capabilities/${encodeURIComponent(operationId)}/approve`,
    {
      method: "POST",
      headers: JSON_HEADERS,
      body: JSON.stringify({ expected_version: expectedVersion }),
    },
  );
}

/** POST /api/admin/assets/{asset_id}/capabilities/{operation_id}/reject. */
export function rejectAssetCapabilityPolicy(
  assetId: string,
  operationId: string,
  expectedVersion: number,
  expectedCapabilitiesVersion?: number,
): Promise<AssetCapabilityPolicy> {
  return request<AssetCapabilityPolicy>(
    `/api/admin/assets/${encodeURIComponent(assetId)}/capabilities/${encodeURIComponent(operationId)}/reject`,
    {
      method: "POST",
      headers: JSON_HEADERS,
      body: JSON.stringify({
        expected_version: expectedVersion,
        ...(expectedCapabilitiesVersion === undefined
          ? {}
          : {
              expected_capabilities_version: expectedCapabilitiesVersion,
            }),
      }),
    },
  );
}
