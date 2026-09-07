/**
 * Backend environment values are split by ownership, not by AGORA_STAGE.
 *
 * SHARED values identify Agora-managed data and infrastructure used by both
 * local development and the portal. PER-ENV values control one process and
 * must never be copied from a developer shell into the portal task. Lambda
 * stacks inject AGORA_ROLE=lambda directly for handlers that package the API.
 */
export const SHARED_ALLOWLIST = [
  // Registry and catalog coordinates.
  "AGORA_STAGE",
  "AGORA_REGION",
  "AGORA_REGISTRY_NAMESPACE",
  "AGORA_REGISTRY_ID",
  "AGORA_TABLE_NAME",
  "AGORA_BUCKET_NAME",
  "AGORA_SOURCE_REGION",
  // Shared application stores.
  "AGORA_GOV_TABLE",
  "AGORA_BUNDLE_TABLE",
  "AGORA_CONNECTION_TABLE",
  "AGORA_MONITORING_AGGREGATE_TABLE",
  "AGORA_MONITORING_INGEST_DLQ_URL",
  "AGORA_IDENTITY_TABLE",
  "AGORA_IDENTITY_REGION",
  // Human Cognito coordinates used by the API.
  "AGORA_AUTH_COGNITO_ISSUER",
  "AGORA_AUTH_COGNITO_CLIENT_ID",
  // Runtime deployment coordinates and execution roles.
  "AGORA_DEPLOY_REGION",
  "AGORA_DEPLOY_GATEWAY_ID",
  "AGORA_DEPLOY_JOBS_TABLE",
  "AGORA_DEPLOY_EXEC_ROLE_ARN",
  "AGORA_DEPLOY_AGENT_EXEC_ROLE_ARN",
  "AGORA_DEPLOY_AGENT_SHARED_POLICY_ARN",
  "AGORA_DEPLOY_AGENT_PERMISSIONS_BOUNDARY_ARN",
  "AGORA_DEPLOY_BUILTIN_EXEC_ROLE_ARN",
  "AGORA_DEPLOY_BUILTIN_RECORDING_BUCKET",
  "AGORA_DEPLOY_ECR_URI",
  "AGORA_DEPLOY_CODEBUILD_PROJECT",
  "AGORA_DEPLOY_ARTIFACT_BUCKET",
  "AGORA_DEPLOY_COGNITO_DISCOVERY_URL",
  "AGORA_DEPLOY_COGNITO_CLIENT_ID",
  "AGORA_DEPLOY_COGNITO_HUMAN_CLIENT_ID",
  "AGORA_DEPLOY_COGNITO_SCOPE",
  "AGORA_DEPLOY_COGNITO_TOKEN_URL",
  "AGORA_RUNTIME_AUTHORIZATION_URL",
  // IAM and OAuth Gateway coordinates.
  "AGORA_M2_POLICY_ENGINE_ID",
  "AGORA_M2_GATEWAY_ARN",
  "AGORA_M2_OAUTH_COGNITO_USER_POOL_ID",
  "AGORA_M2_OAUTH_SCOPE",
  "AGORA_M2_OAUTH_DISCOVERY_URL",
  // Existing Cognito domains can predate this CDK prefix; pass the real endpoint verbatim.
  "AGORA_M2_OAUTH_TOKEN_URL",
  "AGORA_M2_OAUTH_GATEWAY_ID",
  "AGORA_M2_OAUTH_GATEWAY_URL",
  "AGORA_M2_OAUTH_GATEWAY_ARN",
  "AGORA_M2_OAUTH_POLICY_ENGINE_ARN",
  // Shared expiry policy until HP-09 removes cross-environment sweeping risk.
  "AGORA_DEV_IDENTITY_CREDENTIAL_TTL_DAYS",
  "AGORA_DEV_IDENTITY_CREDENTIAL_TTL_MAX_DAYS",
  "AGORA_DEV_IDENTITY_TOKEN_TTL_MINUTES",
  // Governance scan infrastructure coordinates.
  "AGORA_SFN_ARN",
  "AGORA_SCAN_BUCKET",
  "AGORA_SCAN_REGION",
  "AGORA_SCAN_TOOLS_STAGE",
  "AGORA_SCAN_CLUSTER",
  "AGORA_SCAN_TASKDEF",
  "AGORA_SCAN_SUBNETS",
  "AGORA_SCAN_SG",
] as const;

export const PER_ENV_DENYLIST = [
  // Process identity and operating policy.
  "AGORA_ROLE",
  "AGORA_POLLER_ENABLED",
  "AGORA_POLLER_FORCE",
  "AGORA_POLLER_INTERVAL",
  "AGORA_MCP_DRIFT_POLL_ENABLED",
  "AGORA_MCP_DRIFT_POLL_INTERVAL",
  "AGORA_WEB_BASE_URL",
  "AGORA_AUTH_MODE",
  "AGORA_AUTHORIZATION_MODE",
  "AGORA_M2_OAUTH_GATEWAY_MODE",
  "AGORA_MONITORING_MAX_INGEST_LAG_SECONDS",
  "AGORA_MONITORING_TRAFFIC_BOUNDARY_MARGIN_SECONDS",
  "AGORA_MONITORING_INGEST_OWNER",
  "AGORA_RUNTIME_FAIL_CLOSED_ON_UNKNOWN_AUTHORIZATION",
  "AGORA_RUNTIME_FAIL_CLOSED_ON_UNKNOWN_BUILTIN_TOOLS",
  "AGORA_RUNTIME_FAIL_CLOSED_ON_IDENTITY_OUTBOUND",
  "AGORA_RUNTIME_PER_AGENT_ROLES_ENABLED",
  "AGORA_RUNTIME_FORCE_TRACE_SAMPLING_ENABLED",
  // 배포 verify 의 fail-closed 정책. 형제 AGORA_RUNTIME_FAIL_CLOSED_* 와 같은 부류라
  // 개발자 셸 값이 포털 task 로 새면 안 돼요. 기본은 "막지 않음" 이고, 포털이 그 기본을
  // 쓰길 원하면 아무것도 주입하지 않는 게 맞아요.
  "AGORA_DEPLOY_REQUIRE_NEGATIVE_CONTROL",
  "AGORA_DEV_PRINCIPAL",
  "AGORA_DEV_ROLES",
  "AGORA_SCANNER",
  "AGORA_SCAN_TIMEOUT",
  "AGORA_REPORT_LLM",
  "AGORA_REPORT_MODEL",
  "AGORA_REPORT_REGION",
  "AGORA_COMPUTE_LLM",
  "AGORA_COMPUTE_MODEL",
  "AGORA_COMPUTE_REGION",
  "AGORA_PROMPT_LLM",
  "AGORA_PROMPT_MODEL",
  "AGORA_PROMPT_REGION",
  // [S] Identity coordinate owned by the Gateway interceptor Lambda. The M2
  // stack injects it directly; it must not be copied into the Portal process.
  "AGORA_GATEWAY_HUMAN_CLIENT_IDS",
  // Telemetry archive Lambda-internal coordinates and policy are injected
  // directly by TelemetryArchiveStack, never copied from a deployer shell.
  "AGORA_TELEMETRY_STAGE",
  "AGORA_TELEMETRY_RUNTIME_LOG_GROUP_PREFIX",
  "AGORA_TELEMETRY_SHARED_SPAN_LOG_GROUP",
  "AGORA_TELEMETRY_MANAGE_SHARED_SPANS",
  "AGORA_TELEMETRY_DESTINATION_ARN",
  "AGORA_TELEMETRY_SUBSCRIPTION_ROLE_ARN",
  "AGORA_TELEMETRY_SUBSCRIPTION_FILTER_NAME",
  "AGORA_TELEMETRY_SHARED_SPAN_FILTER_NAME",
  "AGORA_TELEMETRY_DEPLOY_JOBS_TABLE",
  "AGORA_TELEMETRY_COVERAGE_TABLE",
  "AGORA_TELEMETRY_ARCHIVE_BUCKET",
  "AGORA_TELEMETRY_MAX_INGEST_LAG_SECONDS",
  // Monitoring ingest Lambda coordinates and retention policy are stack-owned.
  "AGORA_MONITORING_STAGE",
  "AGORA_MONITORING_DEPLOY_JOBS_TABLE",
  "AGORA_MONITORING_RETENTION_DAYS",
  // Synth-only or web-only values do not belong in the API container.
  "AGORA_PORTAL_ORIGIN",
  "AGORA_API_URL",
  "AGORA_WEB_AUTH_MODE",
  "AGORA_AUTH_SESSION_TABLE",
  "AGORA_AUTH_COGNITO_DOMAIN",
  "AGORA_AUTH_COGNITO_USER_POOL_ID",
  "AGORA_AUTH_REGION",
  "NEXT_PUBLIC_API_URL",
] as const;

const COGNITO_DISCOVERY_SUFFIX = "/.well-known/openid-configuration";

function cognitoPoolIdFromDiscovery(discoveryUrl: string): string {
  let parsed: URL;
  try {
    parsed = new URL(discoveryUrl);
  } catch {
    throw new Error(
      "AGORA_M2_OAUTH_DISCOVERY_URL must be a Cognito OIDC discovery URL",
    );
  }
  const host = parsed.hostname.match(
    /^cognito-idp\.([a-z0-9-]+)\.amazonaws\.com(?:\.cn)?$/,
  );
  if (parsed.protocol !== "https:" || host === null) {
    throw new Error(
      "AGORA_M2_OAUTH_DISCOVERY_URL must be a Cognito OIDC discovery URL",
    );
  }
  if (!parsed.pathname.endsWith(COGNITO_DISCOVERY_SUFFIX)) {
    throw new Error(
      `AGORA_M2_OAUTH_DISCOVERY_URL must end with ${COGNITO_DISCOVERY_SUFFIX}`,
    );
  }
  const poolId = parsed.pathname
    .slice(1, -COGNITO_DISCOVERY_SUFFIX.length)
    .replace(/\/$/, "");
  if (!poolId || !poolId.startsWith(`${host[1]}_`)) {
    throw new Error(
      "AGORA_M2_OAUTH_DISCOVERY_URL contains an invalid Cognito pool ID",
    );
  }
  return poolId;
}

export function assertM2OAuthPoolConsistency(
  source: NodeJS.ProcessEnv = process.env,
): void {
  const poolId =
    source.AGORA_M2_OAUTH_COGNITO_USER_POOL_ID?.trim() ?? "";
  const discoveryUrl =
    source.AGORA_M2_OAUTH_DISCOVERY_URL?.trim() ?? "";
  if (!poolId && !discoveryUrl) return;
  if (!poolId || !discoveryUrl) {
    throw new Error(
      "AGORA_M2_OAUTH_COGNITO_USER_POOL_ID and "
      + "AGORA_M2_OAUTH_DISCOVERY_URL must be configured together",
    );
  }
  const discoveryPoolId = cognitoPoolIdFromDiscovery(discoveryUrl);
  if (poolId !== discoveryPoolId) {
    throw new Error(
      "M2 OAuth Cognito pool mismatch: "
      + `configured_pool_id=${poolId}, discovery_pool_id=${discoveryPoolId}`,
    );
  }
  const deployRegion = source.AGORA_DEPLOY_REGION?.trim() ?? "";
  const poolRegion = poolId.split("_", 1)[0];
  if (deployRegion && deployRegion !== poolRegion) {
    throw new Error(
      "M2 OAuth Cognito pool region mismatch: "
      + `configured_deploy_region=${deployRegion}, pool_region=${poolRegion}`,
    );
  }
  // AGORA_M2_OAUTH_TOKEN_URL is a Cognito domain URL. It does not embed the
  // user-pool ID, so this gate deliberately does not guess or parse one.
}

export function pickPortalBackendEnv(
  source: NodeJS.ProcessEnv = process.env,
): Record<string, string> {
  assertM2OAuthPoolConsistency(source);
  const out: Record<string, string> = {};
  for (const key of SHARED_ALLOWLIST) {
    const value = source[key];
    if (typeof value === "string" && value.length > 0) {
      out[key] = value;
    }
  }
  return out;
}
