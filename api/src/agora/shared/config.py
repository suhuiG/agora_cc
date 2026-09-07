"""환경변수 단일 로더.

코드 곳곳에서 os.environ 을 흩뿌리지 않고, 여기 한 곳에서만 읽어요.
필수 누락을 부팅 시 한 번에 잡고, docs/05-operations.md §3 환경변수 계약 표와
어긋나지 않게 유지해요.

계약 (docs/05-operations.md §3):
  AGORA_ROLE      local | portal | lambda. 실행환경 구분자이며 명시 필수(AGORA_STAGE와 별개).
  AGORA_STAGE     dev | prod
  AGORA_REGION    Registry(AgentCore) 리전 (us-east-1 고정)
  AGORA_REGISTRY_NAMESPACE  Registry 서비스 네임스페이스 (bedrock-agentcore 기본 | agent-registry).
                    agent-registry는 boto 서비스명·`.api.aws` endpoint·descriptor 스키마가 달라요(CA-05/ADR-0015).
  AGORA_TABLE_NAME  DynamoDB 테이블명 (필수)
  AGORA_BUCKET_NAME S3 버킷명 (필수)
  AGORA_REGISTRY_ID Registry ID (미지정 시 이름으로 조회/생성)
  AGORA_SOURCE_REGION 소스스토어(S3·DDB) 리전 (미지정 시 ap-northeast-2)
                    메타=us-east-1 Registry / 실물=서울 분리.
  AGORA_GOV_TABLE   거버넌스 콘솔 상태 DynamoDB 테이블명. 설정 시 DynamoGovStore(공유), 없으면 로컬 JSON.
  AGORA_BUNDLE_TABLE 플러그인(bundle) 스토어 DynamoDB 테이블명. 설정 시 DynamoBundleStore, 없으면 로컬 JSON(HP-02).
  AGORA_CONNECTION_TABLE repo 연결(connection) 스토어 DynamoDB 테이블명. 설정 시 DynamoConnectionStore, 없으면 로컬 JSON(HP-03).
  AGORA_SCAN_REGION 스캔 도구 Lambda/ECS(agora-tool-*) 배포 리전 (미지정 시 ap-northeast-2)
                    배포여부 열거는 이 리전에서 해요(Registry us-east-1과 별개).
  AGORA_SCAN_TOOLS_STAGE 스캔 도구 리소스(로그그룹) 조회 stage 오버라이드 (미지정 시 AGORA_STAGE)
                    로컬 prod-mode(cognito 로그인)에서 dev 계정에 -dev 접미사로 배포된
                    스캔 도구 로그를 조회할 때만 dev로 지정해요.
  AGORA_DEPLOY_REGION      MCP 배포(AgentCore Runtime/Gateway) 리전 (미지정 시 ap-northeast-2)
                    Gateway/Runtime은 서울 GA라 서울 배포. Registry(메타)만 us-east-1로 분리.
  AGORA_DEPLOY_GATEWAY_ID  배포 대상 AgentCore Gateway ID
  AGORA_DEPLOY_EXEC_ROLE_ARN Lambda 실행롤 ARN (MCP 배포)
  AGORA_DEPLOY_AGENT_EXEC_ROLE_ARN AgentCore Runtime 실행롤 ARN (agent 배포; 미지정 시 EXEC_ROLE_ARN 폴백)
  AGORA_DEPLOY_AGENT_SHARED_POLICY_ARN [S] agent별 role 공용 관리형 정책 ARN
  AGORA_DEPLOY_AGENT_PERMISSIONS_BOUNDARY_ARN [S] agent별 role permission boundary ARN
  AGORA_DEPLOY_BUILTIN_EXEC_ROLE_ARN agent별 내장 도구 실행롤 ARN
  AGORA_DEPLOY_BUILTIN_RECORDING_BUCKET Browser 세션 녹화 버킷
  AGORA_DEPLOY_JOBS_TABLE  배포 job 상태 DynamoDB 테이블명
  AGORA_DEPLOY_ECR_URI          컨테이너 빌드 산출물 ECR 리포지토리 URI (§7)
  AGORA_DEPLOY_CODEBUILD_PROJECT 소스→아티팩트 빌드 CodeBuild 프로젝트명 (§7)
  AGORA_DEPLOY_ARTIFACT_BUCKET  codezip 빌드 산출물 S3 버킷 (§7)
  AGORA_DEPLOY_COGNITO_DISCOVERY_URL  [S] Runtime workload JWT 발급자인 사람 pool discovery URL.
  AGORA_DEPLOY_COGNITO_CLIENT_ID      [S] 사람 pool의 기계 client ID. Runtime 호출·workload 검증용.
  AGORA_DEPLOY_COGNITO_HUMAN_CLIENT_ID [S] 【폐기 예정 · IA-89 ②】 같은 사람 pool의 public web
                                      client ID. IA-79 가 Runtime `allowedClients` 에 넣었지만
                                      소비자가 0건이라 제거했어요(deps.py `_deploy_cognito_config`).
                                      env 키 삭제는 Configuration Contract 6단계를 다시 밟아야 해서
                                      다음 회차로 남겨요 — 지금은 로더만 남고 아무도 안 읽어요.
  AGORA_DEPLOY_COGNITO_SCOPE          [S] M2M invoke OAuth scope. Playground invoke 토큰 발급용.
  AGORA_DEPLOY_COGNITO_TOKEN_URL      [S] 같은 사람 pool의 oauth2/token URL. 명시값만 사용.
  AGORA_RUNTIME_AUTHORIZATION_URL     Runtime의 MCP 호출 전 authorization decision URL.
  AGORA_RUNTIME_FAIL_CLOSED_ON_UNKNOWN_AUTHORIZATION
                                      인가 관측 unknown 시 agent 배포 차단 여부 (0|1, 기본 0)
  AGORA_RUNTIME_FAIL_CLOSED_ON_UNKNOWN_BUILTIN_TOOLS
                                      내장 도구 도달성 unknown 시 배포 차단 여부 (0|1, 기본 0)
  AGORA_RUNTIME_FAIL_CLOSED_ON_IDENTITY_OUTBOUND
                                      MCP identity 배선 실패 시 배포 차단 여부 (0|1, 기본 1)
  AGORA_RUNTIME_PER_AGENT_ROLES_ENABLED [E] agent별 실행 role 사용 여부 (0|1, 기본 1)
  AGORA_POLLER_ENABLED  배포 job 백그라운드 폴러 on/off (실서버 기동 시에만 on; 서버가 읽음)
  AGORA_DEPLOY_REQUIRE_NEGATIVE_CONTROL  [E] 인가 negative control 을 못 돌렸을 때
                        배포를 막을지(기본 0). 관측 불가는 `unknown` 이고 차단은 명시적
                        정책이어야 해요 — `runtime/deploy/verify.py` 가 직접 읽어요.
  AGORA_POLLER_INTERVAL 폴러 주기(초, 기본 3.0)
  AGORA_MCP_DRIFT_POLL_ENABLED  [E] MCP 도구 목록 드리프트 주기 관측 on/off (0|1, 기본 0)
  AGORA_MCP_DRIFT_POLL_INTERVAL [E] 드리프트 관측 주기(초, 기본 900. 최소 60으로 하한)
  AGORA_AUTH_MODE       cognito | dev | test (prod는 cognito만 허용)
  AGORA_AUTH_COGNITO_ISSUER    사람용 Cognito User Pool issuer
  AGORA_AUTH_COGNITO_CLIENT_ID 사람용 Cognito public app client id
  AGORA_DEV_PRINCIPAL   dev 모드 principal (기본 dev-user)
  AGORA_DEV_ROLES       dev 모드 역할 CSV (기본 user)
  AGORA_IDENTITY_TABLE  [S] AccessGrant/Delegation/Audit DynamoDB 테이블
  AGORA_IDENTITY_REGION [S] Identity 데이터 리전 (기본 ap-northeast-2)
  AGORA_GATEWAY_HUMAN_CLIENT_IDS [S] REQUEST interceptor가 사람 경로로 인정할 Cognito
                                  web client ID CSV. 이 Lambda는 앱 Config/deps를 import하지
                                  않는 독립 handler라 typed Config 필드가 없어요. 대신
                                  infra/bin/agora.ts가 synth 시 필수·비어 있지 않음을 검증하고
                                  M2 stack이 주입하며, handler는 os.environ에서 직접 읽어요.
  AGORA_M2_POLICY_ENGINE_ID   M2 Gateway Policy Engine ID (AgentPolicyDeployer가 정책 배포 대상)
  AGORA_M2_GATEWAY_ARN        M2 Gateway ARN (Cedar resource·reconcile target scope)
  AGORA_M2_OAUTH_COGNITO_USER_POOL_ID agent별 M2M app client 발급 pool
  AGORA_M2_OAUTH_SCOPE        [S] M2 OAuth Gateway invoke scope. Gateway `allowedScopes` 와
                              같은 문자열. pre-token Lambda(`permission_claims`)도 이 값을
                              읽어 사람 토큰에 `scopesToAdd` 로 실어요(IA-75 ②).
  AGORA_M2_OAUTH_DISCOVERY_URL [S] 사람 pool 의 OIDC discovery URL. **소비자가 둘이에요** —
                              이 typed Config(포털 백엔드, SHARED_ALLOWLIST 경유)와
                              infra/bin/agora.ts(AgoraM2OAuthGateway 의 Gateway
                              customJWTAuthorizer.discoveryUrl, IA-80). 두 경로가 같은 값을
                              봐야 해서 포털과 Gateway 스택을 같은 컷오버 단위로 배포해요.
  AGORA_M2_OAUTH_TOKEN_URL   M2 OAuth Cognito client_credentials endpoint
  AGORA_M2_OAUTH_GATEWAY_ID   M2 OAuth Gateway short ID (deploy-mode target wiring)
  AGORA_M2_OAUTH_GATEWAY_URL  배포 agent outbound MCP endpoint
  AGORA_M2_OAUTH_GATEWAY_ARN  OAuthUser Cedar Gateway resource scope
  AGORA_M2_OAUTH_POLICY_ENGINE_ARN OAuthUser Cedar policy 배포 대상
  AGORA_M2_OAUTH_GATEWAY_MODE [E] 실제 Gateway policy mode (LOG_ONLY | ENFORCE)
  AGORA_DEV_IDENTITY_CREDENTIAL_TTL_DAYS dev credential 기본 수명(일)
  AGORA_DEV_IDENTITY_CREDENTIAL_TTL_MAX_DAYS dev credential 서버 최대 수명(일)
  AGORA_DEV_IDENTITY_TOKEN_TTL_MINUTES dev Cognito access token 수명(분)
  AGORA_WEB_BASE_URL  [E] 이 환경의 공개 포털 origin. PortalStack이 CloudFront domain으로
                      정하고 portal role에서는 loopback을 거부해요.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from urllib.parse import urlparse

_REGISTRY_REGION = "us-east-1"       # Agent Registry(메타 SoT) 리전 — 서울 미GA라 고정
_DEFAULT_SOURCE_REGION = "ap-northeast-2"
_DEFAULT_DEPLOY_REGION = "ap-northeast-2"  # Gateway/Runtime 배포 리전 — 서울 GA O

# Registry 서비스 네임스페이스(CA-05/ADR-0015). 구=공개 프리뷰, 신=전용 네임스페이스.
_OLD_REGISTRY_NAMESPACE = "bedrock-agentcore"
_NEW_REGISTRY_NAMESPACE = "agent-registry"
_REGISTRY_NAMESPACES = (_OLD_REGISTRY_NAMESPACE, _NEW_REGISTRY_NAMESPACE)
_EXTERNAL_OAUTH_CALLBACK_PATH = "/api/auth/external/callback"


def registry_service_names(namespace: str) -> tuple[str, str]:
    """네임스페이스별 (control_plane, data_plane) boto3 서비스명.

    - bedrock-agentcore: (bedrock-agentcore-control, bedrock-agentcore)
    - agent-registry:    (agent-registry-control, agent-registry)
    """
    if namespace == _NEW_REGISTRY_NAMESPACE:
        return "agent-registry-control", "agent-registry"
    return "bedrock-agentcore-control", "bedrock-agentcore"


def registry_endpoint_url(namespace: str, service: str, region: str) -> str | None:
    """네임스페이스별 boto endpoint_url.

    신 네임스페이스는 `.api.aws` 도메인을 써요(공식 registry-faq). botocore 1.43.71의
    기본 endpoint 해석은 아직 `.amazonaws.com`을 돌려주므로 **명시적으로 넘겨야** 올바른
    호스트에 도달해요. 구 네임스페이스는 boto 기본(None) — `.amazonaws.com`.
    """
    if namespace == _NEW_REGISTRY_NAMESPACE:
        return f"https://{service}.{region}.api.aws"
    return None


def cognito_pool_id_from_discovery(url: str) -> str:
    """discovery_url에서 user pool id를 파싱해요.
    형식: https://cognito-idp.{region}.amazonaws.com/{POOL_ID}/.well-known/openid-configuration
    """
    if not url or "amazonaws.com/" not in url:
        return ""
    tail = url.split("amazonaws.com/", 1)[1]
    pool = tail.split("/", 1)[0]
    return pool if "_" in pool else ""


def cognito_discovery_url_from_pool_id(region: str, pool_id: str) -> str:
    """Cognito User Pool ID의 표준 OIDC discovery URL을 만들어요."""
    normalized_region = region.strip()
    normalized_pool_id = pool_id.strip()
    if not normalized_region:
        raise ValueError("Cognito region is required")
    if not normalized_pool_id:
        raise ValueError("Cognito user pool ID is required")
    if not normalized_pool_id.startswith(f"{normalized_region}_"):
        raise ValueError(
            "Cognito user pool ID does not belong to the configured region"
        )
    return (
        f"https://cognito-idp.{normalized_region}.amazonaws.com/"
        f"{normalized_pool_id}/.well-known/openid-configuration"
    )


def external_oauth_return_url(web_base_url: str) -> str:
    """Portal origin에서 AgentCore outbound 3LO 복귀 주소를 한 곳에서 만들어요."""
    origin = web_base_url.rstrip("/")
    if not origin:
        raise ValueError("AGORA_WEB_BASE_URL is required for outbound OAuth")
    return f"{origin}{_EXTERNAL_OAUTH_CALLBACK_PATH}"


def cognito_pool_location(issuer: str) -> tuple[str, str]:
    """Human Cognito issuer에서 (region, user_pool_id)를 파생해요."""
    parsed = urlparse(issuer)
    match = re.fullmatch(
        r"cognito-idp\.([a-z0-9-]+)\.amazonaws\.com(?:\.cn)?",
        parsed.hostname or "",
    )
    pool_id = parsed.path.strip("/").split("/", 1)[0]
    if not match or not pool_id or "_" not in pool_id:
        raise ValueError("AGORA_AUTH_COGNITO_ISSUER 형식이 올바르지 않아요.")
    return match.group(1), pool_id


@dataclass(frozen=True)
class Config:
    """부팅 시 1회 로드되는 불변 설정."""

    stage: str            # "dev" | "prod"
    region: str           # Registry(AgentCore) 리전 = us-east-1
    table_name: str
    bucket_name: str
    registry_id: str | None
    source_region: str    # 소스스토어(S3·DDB) 리전.
    role: str = ""        # "local" | "portal" | "lambda". load_config에서는 명시 필수.
    scan_region: str = _DEFAULT_SOURCE_REGION  # 스캔 도구(agora-tool-*) 배포·열거 리전.
    scan_tools_stage: str | None = None        # 스캔 도구 로그 조회 stage 오버라이드(미지정 시 stage).
    deploy_region: str = _DEFAULT_DEPLOY_REGION    # MCP 배포(AgentCore Runtime/Gateway) 리전 = 서울.
    deploy_gateway_id: str | None = None           # 배포 대상 AgentCore Gateway ID.
    deploy_exec_role_arn: str | None = None        # Lambda 실행롤 ARN(MCP 배포).
    deploy_agent_exec_role_arn: str | None = None  # AgentCore Runtime 실행롤 ARN(agent 배포).
    deploy_agent_shared_policy_arn: str | None = None
    deploy_agent_permissions_boundary_arn: str | None = None
    deploy_builtin_exec_role_arn: str | None = None  # CUSTOM 내장 도구 실행롤 ARN.
    deploy_builtin_recording_bucket: str | None = None  # Browser 녹화 버킷.
    deploy_jobs_table: str | None = None           # 배포 job 상태 DynamoDB 테이블명.
    deploy_ecr_uri: str | None = None              # 컨테이너 빌드 산출물 ECR 리포지토리 URI (§7).
    deploy_codebuild_project: str | None = None    # 소스→아티팩트 빌드 CodeBuild 프로젝트명 (§7).
    deploy_artifact_bucket: str | None = None      # codezip 빌드 산출물 S3 버킷 (§7).
    # [S] 같은 사람 pool의 Runtime workload 좌표. discovery/client/scope/token URL은
    # 기계 client 한 묶음이고, human_client_id는 Runtime 인바운드 허용에만 써요.
    deploy_cognito_discovery_url: str | None = None
    deploy_cognito_client_id: str | None = None
    # 【폐기 예정 · IA-89 ②】 소비자 0건이라 Runtime allowedClients 에서 제거했어요.
    # 로더는 남기지만 아무도 읽지 않아요 — 키 삭제는 Configuration Contract 6단계 재수행(다음 회차).
    deploy_cognito_human_client_id: str | None = None
    deploy_cognito_scope: str | None = None
    deploy_cognito_token_url: str | None = None
    runtime_authorization_url: str | None = None      # Runtime의 tool 호출별 decision endpoint.
    runtime_fail_closed_on_unknown_authorization: bool = False
    runtime_fail_closed_on_unknown_builtin_tools: bool = False
    runtime_fail_closed_on_identity_outbound: bool = True
    runtime_per_agent_roles_enabled: bool = True
    # [E] 미검증 AgentCore trace sampling 요청. 비용 안전을 위해 기본 off.
    runtime_force_trace_sampling_enabled: bool = False
    poller_interval: float = 3.0                   # 배포 job 백그라운드 폴러 주기(초).
    # [E] MCP 도구 목록 드리프트 주기 관측. 외부 MCP endpoint를 주기적으로 두드리므로
    # 기본 off — 폴러를 소유한 프로세스(포털)만 명시적으로 켜요.
    mcp_drift_poll_enabled: bool = False
    mcp_drift_poll_interval: float = 900.0         # [E] 드리프트 관측 주기(초).
    gov_table: str | None = None                   # 거버넌스 콘솔 상태 DynamoDB 테이블명. 설정 시 Dynamo.
    bundle_table: str | None = None                # 플러그인(bundle) 스토어 DynamoDB 테이블명(HP-02). 설정 시 Dynamo.
    connection_table: str | None = None            # repo 연결(connection) 스토어 DynamoDB 테이블명(HP-03). 설정 시 Dynamo.
    monitoring_aggregate_table: str | None = None  # [S] 시간별 span 집계 테이블.
    monitoring_ingest_dlq_url: str | None = None   # [S] 적재 Lambda DLQ.
    monitoring_max_ingest_lag_seconds: int = 900   # [E] freshness 임계값.
    # [E] 감사 GSI의 양끝 불확실 구간을 판정에서 제외하는 시간.
    monitoring_traffic_boundary_margin_seconds: int = 60
    monitoring_ingest_owner: bool = False          # [E] account/region span owner.
    auth_mode: str = "dev"                         # cognito | dev | test.
    auth_cognito_issuer: str | None = None         # 사람용 User Pool issuer.
    auth_cognito_client_id: str | None = None      # 사람용 public app client id.
    dev_principal: str = "dev-user"                # dev 모드 고정 principal.
    dev_roles: tuple[str, ...] = ("user",)         # dev 모드 고정 플랫폼 역할.
    identity_table: str | None = None              # AccessGrant/Delegation/Audit 테이블.
    identity_region: str = _DEFAULT_SOURCE_REGION  # Human Identity 데이터 리전.
    m2_policy_engine_id: str | None = None         # M2 Gateway Policy Engine ID(정책 배포 대상).
    m2_gateway_arn: str | None = None              # M2 Gateway ARN(Cedar resource·reconcile scope).
    # [S] 키 이름은 legacy지만 값은 IA-78부터 통합 사람 pool 좌표예요.
    m2_oauth_user_pool_id: str | None = None       # agent/dev M2M client 발급 pool.
    m2_oauth_scope: str | None = None              # M2 OAuth Gateway invoke scope.
    # [S] Gateway inbound issuer. agent 배포 outbound는 pool ID에서 파생해요.
    m2_oauth_discovery_url: str | None = None
    m2_oauth_token_url: str | None = None          # 사람 pool client_credentials endpoint.
    m2_oauth_gateway_id: str | None = None          # deploy-mode MCP target 배선 Gateway ID.
    m2_oauth_gateway_url: str | None = None        # 배포 agent outbound MCP endpoint.
    m2_oauth_gateway_arn: str | None = None        # OAuthUser Cedar resource scope.
    m2_oauth_policy_engine_arn: str | None = None  # OAuthUser policy 배포 대상.
    m2_oauth_gateway_mode: str | None = None       # [E] LOG_ONLY | ENFORCE. 미관측은 None.
    dev_identity_credential_ttl_days: int = 7
    dev_identity_credential_ttl_max_days: int = 30
    dev_identity_token_ttl_minutes: int = 60
    web_base_url: str | None = None                  # [E] stack-owned public portal origin.
    authorization_mode: str = "agent_policy"       # agent_policy | legacy_delegated. 런타임 tool 인가 모델(IA-19).
    registry_namespace: str = "bedrock-agentcore"  # bedrock-agentcore | agent-registry. Registry 서비스 네임스페이스(CA-05/ADR-0015).


def load_config() -> Config:
    """환경변수에서 설정을 로드해요. 필수 값이 없으면 즉시 실패."""
    # 기본값을 두면 미분류 프로세스가 조용히 local/portal 정책을 상속해 환경 분리가 무력화돼요.
    role = os.environ.get("AGORA_ROLE", "").lower()
    stage = os.environ.get("AGORA_STAGE", "dev").lower()
    region = os.environ.get("AGORA_REGION", _REGISTRY_REGION)
    table_name = os.environ.get("AGORA_TABLE_NAME")
    bucket_name = os.environ.get("AGORA_BUCKET_NAME")
    registry_id = os.environ.get("AGORA_REGISTRY_ID")
    source_region = os.environ.get("AGORA_SOURCE_REGION") or _DEFAULT_SOURCE_REGION
    scan_region = os.environ.get("AGORA_SCAN_REGION") or _DEFAULT_SOURCE_REGION
    scan_tools_stage = os.environ.get("AGORA_SCAN_TOOLS_STAGE") or None
    deploy_region = os.environ.get("AGORA_DEPLOY_REGION") or _DEFAULT_DEPLOY_REGION
    deploy_gateway_id = os.environ.get("AGORA_DEPLOY_GATEWAY_ID")
    deploy_exec_role_arn = os.environ.get("AGORA_DEPLOY_EXEC_ROLE_ARN")
    # agent Runtime 실행롤(bedrock-agentcore trust)은 Lambda 실행롤과 trust가 달라 별도예요.
    # 미지정 시 EXEC_ROLE_ARN으로 폴백(하위호환) — 단 agent 배포는 전용 롤 지정을 권장해요.
    deploy_agent_exec_role_arn = (
        os.environ.get("AGORA_DEPLOY_AGENT_EXEC_ROLE_ARN") or deploy_exec_role_arn)
    deploy_agent_shared_policy_arn = os.environ.get(
        "AGORA_DEPLOY_AGENT_SHARED_POLICY_ARN"
    )
    deploy_agent_permissions_boundary_arn = os.environ.get(
        "AGORA_DEPLOY_AGENT_PERMISSIONS_BOUNDARY_ARN"
    )
    deploy_builtin_exec_role_arn = os.environ.get(
        "AGORA_DEPLOY_BUILTIN_EXEC_ROLE_ARN"
    )
    deploy_builtin_recording_bucket = os.environ.get(
        "AGORA_DEPLOY_BUILTIN_RECORDING_BUCKET"
    )
    deploy_jobs_table = os.environ.get("AGORA_DEPLOY_JOBS_TABLE")
    deploy_ecr_uri = os.environ.get("AGORA_DEPLOY_ECR_URI")
    deploy_codebuild_project = os.environ.get("AGORA_DEPLOY_CODEBUILD_PROJECT")
    deploy_artifact_bucket = os.environ.get("AGORA_DEPLOY_ARTIFACT_BUCKET")
    deploy_cognito_discovery_url = os.environ.get("AGORA_DEPLOY_COGNITO_DISCOVERY_URL")
    deploy_cognito_client_id = os.environ.get("AGORA_DEPLOY_COGNITO_CLIENT_ID")
    deploy_cognito_human_client_id = os.environ.get(
        "AGORA_DEPLOY_COGNITO_HUMAN_CLIENT_ID"
    )
    deploy_cognito_scope = os.environ.get("AGORA_DEPLOY_COGNITO_SCOPE")
    deploy_cognito_token_url = os.environ.get("AGORA_DEPLOY_COGNITO_TOKEN_URL")
    runtime_authorization_url = os.environ.get("AGORA_RUNTIME_AUTHORIZATION_URL")
    unknown_authz_flag = os.environ.get(
        "AGORA_RUNTIME_FAIL_CLOSED_ON_UNKNOWN_AUTHORIZATION",
        "0",
    )
    if unknown_authz_flag not in ("0", "1"):
        raise ValueError(
            "AGORA_RUNTIME_FAIL_CLOSED_ON_UNKNOWN_AUTHORIZATION must be '0' or '1'"
        )
    runtime_fail_closed_on_unknown_authorization = unknown_authz_flag == "1"
    unknown_builtin_flag = os.environ.get(
        "AGORA_RUNTIME_FAIL_CLOSED_ON_UNKNOWN_BUILTIN_TOOLS",
        "0",
    )
    if unknown_builtin_flag not in ("0", "1"):
        raise ValueError(
            "AGORA_RUNTIME_FAIL_CLOSED_ON_UNKNOWN_BUILTIN_TOOLS "
            "must be '0' or '1'"
        )
    runtime_fail_closed_on_unknown_builtin_tools = (
        unknown_builtin_flag == "1"
    )
    identity_outbound_flag = os.environ.get(
        "AGORA_RUNTIME_FAIL_CLOSED_ON_IDENTITY_OUTBOUND",
        "1",
    )
    if identity_outbound_flag not in ("0", "1"):
        raise ValueError(
            "AGORA_RUNTIME_FAIL_CLOSED_ON_IDENTITY_OUTBOUND must be '0' or '1'"
        )
    runtime_fail_closed_on_identity_outbound = identity_outbound_flag == "1"
    per_agent_roles_flag = os.environ.get(
        "AGORA_RUNTIME_PER_AGENT_ROLES_ENABLED", "1"
    )
    if per_agent_roles_flag not in ("0", "1"):
        raise ValueError(
            "AGORA_RUNTIME_PER_AGENT_ROLES_ENABLED must be '0' or '1'"
        )
    runtime_per_agent_roles_enabled = per_agent_roles_flag == "1"
    force_trace_sampling_flag = os.environ.get(
        "AGORA_RUNTIME_FORCE_TRACE_SAMPLING_ENABLED",
        "0",
    )
    if force_trace_sampling_flag not in ("0", "1"):
        raise ValueError(
            "AGORA_RUNTIME_FORCE_TRACE_SAMPLING_ENABLED must be '0' or '1'"
        )
    runtime_force_trace_sampling_enabled = force_trace_sampling_flag == "1"
    poller_interval = float(os.environ.get("AGORA_POLLER_INTERVAL", "3"))
    mcp_drift_flag = os.environ.get("AGORA_MCP_DRIFT_POLL_ENABLED", "0")
    if mcp_drift_flag not in ("0", "1"):
        raise ValueError("AGORA_MCP_DRIFT_POLL_ENABLED must be '0' or '1'")
    mcp_drift_poll_enabled = mcp_drift_flag == "1"
    # 하한 60초 — 더 짧게 두면 남의 MCP 서버를 우리 폴러가 두드려 대요. 값이 이상하면
    # 조용히 기본값으로 넘기지 않고 실패해요(설정 오타가 관측 공백으로 숨지 않게).
    mcp_drift_poll_interval = float(
        os.environ.get("AGORA_MCP_DRIFT_POLL_INTERVAL", "900"))
    if mcp_drift_poll_interval < 60:
        raise ValueError("AGORA_MCP_DRIFT_POLL_INTERVAL must be >= 60 seconds")
    gov_table = os.environ.get("AGORA_GOV_TABLE")
    bundle_table = os.environ.get("AGORA_BUNDLE_TABLE")
    connection_table = os.environ.get("AGORA_CONNECTION_TABLE")
    monitoring_aggregate_table = os.environ.get(
        "AGORA_MONITORING_AGGREGATE_TABLE"
    )
    monitoring_ingest_dlq_url = os.environ.get(
        "AGORA_MONITORING_INGEST_DLQ_URL"
    )
    monitoring_max_ingest_lag_seconds = int(
        os.environ.get("AGORA_MONITORING_MAX_INGEST_LAG_SECONDS", "900")
    )
    monitoring_traffic_boundary_margin_seconds = int(
        os.environ.get(
            "AGORA_MONITORING_TRAFFIC_BOUNDARY_MARGIN_SECONDS",
            "60",
        )
    )
    monitoring_ingest_owner_flag = os.environ.get(
        "AGORA_MONITORING_INGEST_OWNER", "0"
    )
    if monitoring_ingest_owner_flag not in ("0", "1"):
        raise ValueError("AGORA_MONITORING_INGEST_OWNER must be '0' or '1'")
    monitoring_ingest_owner = monitoring_ingest_owner_flag == "1"
    auth_mode = os.environ.get(
        "AGORA_AUTH_MODE", "cognito" if stage == "prod" else "dev").lower()
    auth_cognito_issuer = os.environ.get("AGORA_AUTH_COGNITO_ISSUER")
    auth_cognito_client_id = os.environ.get("AGORA_AUTH_COGNITO_CLIENT_ID")
    dev_principal = os.environ.get("AGORA_DEV_PRINCIPAL", "dev-user").strip()
    dev_roles = tuple(
        role.strip().lower()
        for role in os.environ.get("AGORA_DEV_ROLES", "user").split(",")
        if role.strip()
    )
    identity_table = (
        os.environ.get("AGORA_IDENTITY_TABLE") or f"agora-identity-{stage}"
    )
    identity_region = (
        os.environ.get("AGORA_IDENTITY_REGION") or _DEFAULT_SOURCE_REGION
    )
    m2_policy_engine_id = os.environ.get("AGORA_M2_POLICY_ENGINE_ID")
    m2_gateway_arn = os.environ.get("AGORA_M2_GATEWAY_ARN")
    m2_oauth_user_pool_id = os.environ.get("AGORA_M2_OAUTH_COGNITO_USER_POOL_ID")
    m2_oauth_scope = os.environ.get("AGORA_M2_OAUTH_SCOPE")
    m2_oauth_discovery_url = os.environ.get("AGORA_M2_OAUTH_DISCOVERY_URL")
    m2_oauth_token_url = os.environ.get("AGORA_M2_OAUTH_TOKEN_URL")
    m2_oauth_gateway_id = os.environ.get("AGORA_M2_OAUTH_GATEWAY_ID")
    m2_oauth_gateway_url = os.environ.get("AGORA_M2_OAUTH_GATEWAY_URL")
    m2_oauth_gateway_arn = os.environ.get("AGORA_M2_OAUTH_GATEWAY_ARN")
    m2_oauth_policy_engine_arn = os.environ.get(
        "AGORA_M2_OAUTH_POLICY_ENGINE_ARN"
    )
    m2_oauth_gateway_mode = os.environ.get("AGORA_M2_OAUTH_GATEWAY_MODE")
    if (
        m2_oauth_gateway_mode is not None
        and m2_oauth_gateway_mode not in ("LOG_ONLY", "ENFORCE")
    ):
        raise ValueError(
            "AGORA_M2_OAUTH_GATEWAY_MODE must be LOG_ONLY or ENFORCE"
        )
    dev_identity_credential_ttl_days = int(
        os.environ.get("AGORA_DEV_IDENTITY_CREDENTIAL_TTL_DAYS", "7")
    )
    dev_identity_credential_ttl_max_days = int(
        os.environ.get("AGORA_DEV_IDENTITY_CREDENTIAL_TTL_MAX_DAYS", "30")
    )
    dev_identity_token_ttl_minutes = int(
        os.environ.get("AGORA_DEV_IDENTITY_TOKEN_TTL_MINUTES", "60")
    )
    web_base_url = (
        os.environ.get("AGORA_WEB_BASE_URL", "").rstrip("/") or None
    )
    authorization_mode = os.environ.get(
        "AGORA_AUTHORIZATION_MODE", "agent_policy").lower()
    registry_namespace = os.environ.get(
        "AGORA_REGISTRY_NAMESPACE", _OLD_REGISTRY_NAMESPACE).lower()

    if role not in ("local", "portal", "lambda"):
        raise ValueError(
            "AGORA_ROLE must be explicitly set to 'local', 'portal', or 'lambda'; "
            "use 'local' for api/run-backend.sh, 'portal' for PortalStack, "
            "and 'lambda' for packaged Lambda handlers"
        )
    if stage not in ("dev", "prod"):
        raise ValueError(f"AGORA_STAGE must be 'dev' or 'prod', got: {stage!r}")
    if region != "us-east-1":
        raise ValueError(
            f"AGORA_REGION은 us-east-1 전용이에요(AgentCore GA 리전). got: {region!r}"
        )
    if auth_mode not in ("cognito", "dev", "test"):
        raise ValueError(
            "AGORA_AUTH_MODE must be 'cognito', 'dev', or 'test', "
            f"got: {auth_mode!r}"
        )
    if role == "portal" and auth_mode != "cognito":
        raise ValueError(
            "AGORA_ROLE=portal requires AGORA_AUTH_MODE=cognito; "
            "dev/test authentication would bypass portal login"
        )
    if role == "portal" and runtime_per_agent_roles_enabled:
        missing_agent_role_coordinates = [
            name for name, value in (
                (
                    "AGORA_DEPLOY_AGENT_SHARED_POLICY_ARN",
                    deploy_agent_shared_policy_arn,
                ),
                (
                    "AGORA_DEPLOY_AGENT_PERMISSIONS_BOUNDARY_ARN",
                    deploy_agent_permissions_boundary_arn,
                ),
            )
            if not value
        ]
        if missing_agent_role_coordinates:
            raise ValueError(
                "AGORA_ROLE=portal per-agent roles require IAM coordinates. "
                "Missing: " + ", ".join(missing_agent_role_coordinates)
            )
    if authorization_mode not in ("agent_policy", "legacy_delegated"):
        raise ValueError(
            "AGORA_AUTHORIZATION_MODE must be 'agent_policy' or 'legacy_delegated', "
            f"got: {authorization_mode!r}"
        )
    if role != "local" and authorization_mode == "legacy_delegated":
        raise ValueError(
            "AGORA_AUTHORIZATION_MODE=legacy_delegated is only allowed for "
            "AGORA_ROLE=local regression tests"
        )
    if min(
        dev_identity_credential_ttl_days,
        dev_identity_credential_ttl_max_days,
        dev_identity_token_ttl_minutes,
    ) < 1:
        raise ValueError("dev identity TTL values must be positive")
    if monitoring_max_ingest_lag_seconds < 60:
        raise ValueError(
            "AGORA_MONITORING_MAX_INGEST_LAG_SECONDS must be at least 60"
        )
    if monitoring_traffic_boundary_margin_seconds < 1:
        raise ValueError(
            "AGORA_MONITORING_TRAFFIC_BOUNDARY_MARGIN_SECONDS "
            "must be positive"
        )
    if bool(monitoring_aggregate_table) != bool(monitoring_ingest_dlq_url):
        raise ValueError(
            "AGORA_MONITORING_AGGREGATE_TABLE and "
            "AGORA_MONITORING_INGEST_DLQ_URL must be set together"
        )
    if web_base_url is not None:
        parsed_web_base_url = urlparse(web_base_url)
        if (
            parsed_web_base_url.scheme not in ("http", "https")
            or not parsed_web_base_url.netloc
            or parsed_web_base_url.path not in ("", "/")
        ):
            raise ValueError("AGORA_WEB_BASE_URL must be an http(s) origin")
    if role == "portal":
        if web_base_url is None:
            raise ValueError(
                "AGORA_ROLE=portal requires AGORA_WEB_BASE_URL; "
                "derive it from the PortalStack CloudFront domain"
            )
        if parsed_web_base_url.hostname in {
            "localhost", "127.0.0.1", "::1", "0.0.0.0",
        }:
            raise ValueError(
                "AGORA_ROLE=portal requires a public AGORA_WEB_BASE_URL, "
                "not a loopback address; use the PortalStack CloudFront domain"
            )
    oauth_config = {
        "AGORA_M2_OAUTH_COGNITO_USER_POOL_ID": m2_oauth_user_pool_id,
        "AGORA_M2_OAUTH_SCOPE": m2_oauth_scope,
        "AGORA_M2_OAUTH_DISCOVERY_URL": m2_oauth_discovery_url,
        "AGORA_M2_OAUTH_GATEWAY_ID": m2_oauth_gateway_id,
        "AGORA_M2_OAUTH_GATEWAY_URL": m2_oauth_gateway_url,
        "AGORA_M2_OAUTH_GATEWAY_ARN": m2_oauth_gateway_arn,
        "AGORA_M2_OAUTH_POLICY_ENGINE_ARN": m2_oauth_policy_engine_arn,
    }
    if (
        authorization_mode == "agent_policy"
        and (
            role == "portal"
            or any(oauth_config.values())
            or (auth_mode != "test" and bool(registry_id))
        )
    ):
        missing_oauth = [
            name for name, value in oauth_config.items() if not value
        ]
        if missing_oauth:
            raise ValueError(
                "M2 OAuth configuration is incomplete; set all "
                "AGORA_M2_OAUTH_* values together. Missing: "
                + ", ".join(missing_oauth)
            )
    if registry_namespace not in _REGISTRY_NAMESPACES:
        raise ValueError(
            "AGORA_REGISTRY_NAMESPACE must be 'bedrock-agentcore' or 'agent-registry', "
            f"got: {registry_namespace!r}"
        )
    missing = [
        name for name, val in
        (("AGORA_TABLE_NAME", table_name), ("AGORA_BUCKET_NAME", bucket_name))
        if not val
    ]
    if missing:
        raise ValueError(f"필수 환경변수가 없어요: {', '.join(missing)}")

    return Config(
        stage=stage, role=role, region=region,
        table_name=table_name, bucket_name=bucket_name,
        registry_id=registry_id, source_region=source_region,
        scan_region=scan_region,
        scan_tools_stage=scan_tools_stage,
        deploy_region=deploy_region,
        deploy_gateway_id=deploy_gateway_id,
        deploy_exec_role_arn=deploy_exec_role_arn,
        deploy_agent_exec_role_arn=deploy_agent_exec_role_arn,
        deploy_agent_shared_policy_arn=deploy_agent_shared_policy_arn,
        deploy_agent_permissions_boundary_arn=(
            deploy_agent_permissions_boundary_arn
        ),
        deploy_builtin_exec_role_arn=deploy_builtin_exec_role_arn,
        deploy_builtin_recording_bucket=deploy_builtin_recording_bucket,
        deploy_jobs_table=deploy_jobs_table,
        deploy_ecr_uri=deploy_ecr_uri,
        deploy_codebuild_project=deploy_codebuild_project,
        deploy_artifact_bucket=deploy_artifact_bucket,
        deploy_cognito_discovery_url=deploy_cognito_discovery_url,
        deploy_cognito_client_id=deploy_cognito_client_id,
        deploy_cognito_human_client_id=deploy_cognito_human_client_id,
        deploy_cognito_scope=deploy_cognito_scope,
        deploy_cognito_token_url=deploy_cognito_token_url,
        runtime_authorization_url=runtime_authorization_url,
        runtime_fail_closed_on_unknown_authorization=(
            runtime_fail_closed_on_unknown_authorization
        ),
        runtime_fail_closed_on_unknown_builtin_tools=(
            runtime_fail_closed_on_unknown_builtin_tools
        ),
        runtime_fail_closed_on_identity_outbound=(
            runtime_fail_closed_on_identity_outbound
        ),
        runtime_per_agent_roles_enabled=runtime_per_agent_roles_enabled,
        runtime_force_trace_sampling_enabled=(
            runtime_force_trace_sampling_enabled
        ),
        poller_interval=poller_interval,
        mcp_drift_poll_enabled=mcp_drift_poll_enabled,
        mcp_drift_poll_interval=mcp_drift_poll_interval,
        gov_table=gov_table,
        bundle_table=bundle_table,
        connection_table=connection_table,
        monitoring_aggregate_table=monitoring_aggregate_table,
        monitoring_ingest_dlq_url=monitoring_ingest_dlq_url,
        monitoring_max_ingest_lag_seconds=monitoring_max_ingest_lag_seconds,
        monitoring_traffic_boundary_margin_seconds=(
            monitoring_traffic_boundary_margin_seconds
        ),
        monitoring_ingest_owner=monitoring_ingest_owner,
        auth_mode=auth_mode,
        auth_cognito_issuer=auth_cognito_issuer,
        auth_cognito_client_id=auth_cognito_client_id,
        dev_principal=dev_principal,
        dev_roles=dev_roles,
        identity_table=identity_table,
        identity_region=identity_region,
        m2_policy_engine_id=m2_policy_engine_id,
        m2_gateway_arn=m2_gateway_arn,
        m2_oauth_user_pool_id=m2_oauth_user_pool_id,
        m2_oauth_scope=m2_oauth_scope,
        m2_oauth_discovery_url=m2_oauth_discovery_url,
        m2_oauth_token_url=m2_oauth_token_url,
        m2_oauth_gateway_id=m2_oauth_gateway_id,
        m2_oauth_gateway_url=m2_oauth_gateway_url,
        m2_oauth_gateway_arn=m2_oauth_gateway_arn,
        m2_oauth_policy_engine_arn=m2_oauth_policy_engine_arn,
        m2_oauth_gateway_mode=m2_oauth_gateway_mode,
        dev_identity_credential_ttl_days=dev_identity_credential_ttl_days,
        dev_identity_credential_ttl_max_days=(
            dev_identity_credential_ttl_max_days
        ),
        dev_identity_token_ttl_minutes=dev_identity_token_ttl_minutes,
        web_base_url=web_base_url,
        authorization_mode=authorization_mode,
        registry_namespace=registry_namespace,
    )
