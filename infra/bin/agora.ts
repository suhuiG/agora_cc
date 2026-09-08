#!/usr/bin/env node
import * as cdk from "aws-cdk-lib";
import { CatalogStorageStack, Stage } from "../lib/catalog-storage-stack";
import { GovernanceScanStack } from "../lib/governance-scan-stack";
import { GovernanceScanToolsStack } from "../lib/scan-tools-stack";
import { RuntimeDeployStack } from "../lib/runtime-deploy-stack";
import {
  IdentityStack,
  identityDataTableName,
} from "../lib/identity-stack";
import { RuntimeAuthorizationStack } from "../lib/runtime-authorization-stack";
import { M2OAuthGatewayStack } from "../lib/m2-oauth-gateway-stack";
import { PortalStack } from "../lib/portal-stack";
import { pickPortalBackendEnv } from "../lib/portal-env";
import { TelemetryArchiveStack } from "../lib/telemetry-archive-stack";
import { MonitoringAggregateStack } from "../lib/monitoring-aggregate-stack";

const app = new cdk.App();

// stage = dev | prod. `-c stage=dev` 로 주입. 기본은 prod (안전 우선).
const stage = (app.node.tryGetContext("stage") as Stage) ?? "prod";
if (stage !== "dev" && stage !== "prod") {
  throw new Error(`invalid stage: ${stage} (dev | prod)`);
}

// 리전은 ap-northeast-2 고정. 계정은 배포 환경에서 주입(CDK_DEFAULT_ACCOUNT).
// 스택명에 stage를 붙여 dev/prod가 한 계정에 공존해도 충돌하지 않게 해요.
const catalogStorageStack = new CatalogStorageStack(app, `AgoraCatalogStorage-${stage}`, {
  stage,
  env: {
    account: process.env.CDK_DEFAULT_ACCOUNT,
    region: "ap-northeast-2",
  },
});

new GovernanceScanStack(app, `AgoraGovernanceScan-${stage}`, {
  stage,
  env: {
    account: process.env.CDK_DEFAULT_ACCOUNT,
    region: "ap-northeast-2",
  },
});

new GovernanceScanToolsStack(app, `AgoraGovernanceScanTools-${stage}`, {
  stage,
  env: { account: process.env.CDK_DEFAULT_ACCOUNT, region: "ap-northeast-2" },
});

// RuntimeDeployStack — MCP "deploy" 모드 인프라. AgentCore Runtime/Gateway는 서울 GA라
// 데이터 플레인과 같은 ap-northeast-2에 배포(cross-region 소스 pull 제거).
// 메타 SoT인 Agent Registry만 서울 미GA라 us-east-1 유지(AGORA_REGION).
const runtimeDeployStack = new RuntimeDeployStack(app, `AgoraRuntimeDeploy-${stage}`, {
  stage,
  apiExecutionRole: catalogStorageStack.apiExecutionRole,
  env: {
    account: process.env.CDK_DEFAULT_ACCOUNT,
    region: process.env.AGORA_DEPLOY_REGION ?? "ap-northeast-2",
  },
});

const monitoringAggregateEnabled =
  app.node.tryGetContext("monitoringAggregate") === "true";
const monitoringSharedSpansOwner =
  app.node.tryGetContext("monitoringSharedSpansOwner") === "true";
const forceTraceSamplingContext = app.node.tryGetContext(
  "runtimeForceTraceSampling",
);
if (
  forceTraceSamplingContext !== undefined
  && forceTraceSamplingContext !== "true"
  && forceTraceSamplingContext !== "false"
) {
  throw new Error("runtimeForceTraceSampling must be true or false");
}
const forceTraceSampling = forceTraceSamplingContext === "true";
if (monitoringSharedSpansOwner && !monitoringAggregateEnabled) {
  throw new Error(
    "monitoringSharedSpansOwner=true requires monitoringAggregate=true",
  );
}
if (monitoringAggregateEnabled) {
  new MonitoringAggregateStack(app, `AgoraMonitoringAggregate-${stage}`, {
    stage,
    apiExecutionRole: catalogStorageStack.apiExecutionRole,
    manageSharedSpans: monitoringSharedSpansOwner,
    env: {
      account: process.env.CDK_DEFAULT_ACCOUNT,
      region: process.env.AGORA_DEPLOY_REGION ?? "ap-northeast-2",
    },
  });
}

const telemetryArchiveEnabled =
  app.node.tryGetContext("telemetryArchive") === "true";
const telemetrySharedSpansOwner =
  app.node.tryGetContext("telemetrySharedSpansOwner") === "true";
if (telemetryArchiveEnabled) {
  new TelemetryArchiveStack(app, `AgoraTelemetryArchive-${stage}`, {
    stage,
    manageSharedSpans: telemetrySharedSpansOwner,
    env: {
      account: process.env.CDK_DEFAULT_ACCOUNT,
      region: process.env.AGORA_DEPLOY_REGION ?? "ap-northeast-2",
    },
  });
}

// Agora 의 유일한 도구 Gateway — OAuth/JWT 인바운드 AgentCore Gateway.
// agent client 는 shared invoke scope 로 인바운드 게이팅하고, 정밀 판정은 아래에서
// Identity 원장을 주입받는 REQUEST interceptor 가 담당해요.
//
// **기본이 ENFORCE 예요.** 플래그를 안 넘기고 배포했을 때 강제가 조용히 꺼지면 안 되기
// 때문이에요 — 강제를 끄는 변경은 항상 명시적이어야 해요.
// 이 값은 `PortalStack` 의 `AGORA_M2_OAUTH_GATEWAY_MODE` env 로도 흘러가요 — 기본값이
// 어긋나면 백엔드가 강제 상태를 잘못 알아요.
// `LOG_ONLY` 는 명시적으로 `-c m2OAuthGatewayMode=LOG_ONLY` 를 넘길 때만 되고, Agora 는
// 그 모드를 쓰지 않아요. `LOG_ONLY` 에서 관측한 "거부 0건" 은 강제의 증거가 아니에요.
const m2OAuthGatewayMode =
  (app.node.tryGetContext("m2OAuthGatewayMode") as "LOG_ONLY" | "ENFORCE") ?? "ENFORCE";
// REQUEST interceptor 부착 스위치 — **기본이 on 이에요** (2026-08-29 변경).
//
// 원래 기본이 `off` 였어요. 근거는 "handle 생산자가 0곳" 이었고(발급은 IA-55, 생성 코드가
// 헤더에 싣는 건 IA-61), 켜면 handshake 부터 전면 403 이니까요. **그 조건은 해소됐어요** —
// IA-55·IA-61 이 닫혀서 배포 검증 호출까지 handle 을 실어요
// (`shared/deps.py issue_verify_call_handle`).
//
// 기본을 뒤집는 이유는 형제 플래그와 같아요: 2026-08-29 에 플래그 없이
// `cdk deploy AgoraM2OAuthGateway-dev` 를 돌렸다가 `interceptorConfigurations` 가
// `undefined` 로 덮여 **부착이 지워졌어요.** interceptor 는 유일한 강제 지점이라
// (ADR-0091 · AGENTS.md 하드룰) 그게 "그냥 배포" 로 사라지면 안 돼요. 떼려면 명시해야 해요.
//
// 부착이 없어도 Cedar 굵은 문은 남지만 그건 도구 단위 판정이 아니에요. Cedar 를 두 번째
// 방어선으로 취급하지 마세요(§12).
const m2OAuthInterceptor =
  (app.node.tryGetContext("m2OAuthInterceptor") as "on" | "off") ?? "on";
if (m2OAuthInterceptor !== "on" && m2OAuthInterceptor !== "off") {
  throw new Error(
    `invalid m2OAuthInterceptor: ${m2OAuthInterceptor} (on | off)`,
  );
}
if (m2OAuthGatewayMode !== "LOG_ONLY" && m2OAuthGatewayMode !== "ENFORCE") {
  throw new Error(
    `invalid m2OAuthGatewayMode: ${m2OAuthGatewayMode} (LOG_ONLY | ENFORCE)`,
  );
}

// ── 포털 호스팅 (ADR-0030) ─────────────────────────────────────────────
// 웹 SSR + FastAPI 백엔드를 App Runner 컨테이너로 띄우고 웹 앞에 CloudFront를 둬요.
// DockerImageAsset 빌드가 무거워 `-c portal=true`일 때만 스택을 만들어요(다른 배포엔 영향 없음).
// SHARED 좌표만 개발자 env에서 선택하고, PER-ENV 운영값은 PortalStack이 결정해요.
let portalStack: PortalStack | undefined;
if (app.node.tryGetContext("portal") === "true") {
  const pick = (keys: string[]): Record<string, string> => {
    const out: Record<string, string> = {};
    for (const k of keys) {
      const v = process.env[k];
      if (typeof v === "string" && v.length > 0) out[k] = v;
    }
    return out;
  };
  // 포털은 데이터와 같은 서울(ap-northeast-2)에 ECS Fargate로 배포해요(App Runner는 서울 미지원).
  portalStack = new PortalStack(app, `AgoraPortal-${stage}`, {
    stage,
    backendTaskRole: catalogStorageStack.apiExecutionRole,
    sessionTableName: process.env.AGORA_AUTH_SESSION_TABLE ?? "",
    builtinExecutionRoleArn: runtimeDeployStack.builtinToolExecutionRoleArn,
    builtinRecordingBucket: runtimeDeployStack.builtinRecordingBucketName,
    agentSharedPolicyArn: runtimeDeployStack.agentRuntimeSharedPolicyArn,
    agentPermissionsBoundaryArn:
      runtimeDeployStack.runtimePermissionBoundaryArn,
    m2OAuthGatewayMode,
    monitoringIngestOwner: monitoringSharedSpansOwner,
    forceTraceSampling,
    backendEnv: pickPortalBackendEnv(),
    webEnv: pick([
      "AGORA_WEB_AUTH_MODE",
      "AGORA_AUTH_REGION",
      "AGORA_AUTH_COGNITO_CLIENT_ID",
      "AGORA_AUTH_COGNITO_USER_POOL_ID",
      "AGORA_AUTH_COGNITO_DOMAIN",
      "AGORA_AUTH_SESSION_TABLE",
      "NEXT_PUBLIC_API_URL",
    ]),
    env: { account: process.env.CDK_DEFAULT_ACCOUNT, region: "ap-northeast-2" },
  });
}

const identityStack = new IdentityStack(app, `AgoraIdentity-${stage}`, {
  stage,
  // PortalStack이 있으면 동일 CloudFront 토큰을 Cognito callback에도 직접 연결해요.
  // 포털 없는 독립 Identity 배포만 명시적인 외부 origin을 process env로 받아요.
  webBaseUrl: portalStack?.webBaseUrl ?? process.env.AGORA_WEB_BASE_URL,
  apiExecutionRole: catalogStorageStack.apiExecutionRole,
  env: {
    account: process.env.CDK_DEFAULT_ACCOUNT,
    region: "ap-northeast-2",
  },
});

const gatewayHumanClientIds = Array.from(new Set(
  (process.env.AGORA_GATEWAY_HUMAN_CLIENT_IDS ?? "")
    .split(",")
    .map((clientId) => clientId.trim())
    .filter(Boolean),
));
if (gatewayHumanClientIds.length === 0) {
  throw new Error(
    "AGORA_GATEWAY_HUMAN_CLIENT_IDS is required for synthesis; "
    + "supply the IdentityStack HumanCognitoClientId output through the "
    + "deployment environment.",
  );
}

// IA-93: provider client 발급 pool과 Gateway의 단일 inbound issuer는 하나의 불변식이에요.
// 둘 중 하나가 없거나 pool ID가 다르면 synth를 실패시켜 부분 컷오버를 표현할 수 없게 해요.
// AGORA_M2_OAUTH_TOKEN_URL은 domain 기반이라 pool ID를 담지 않으므로 여기서 추측하지 않아요.
const gatewayAuthorizerPoolId =
  process.env.AGORA_M2_OAUTH_COGNITO_USER_POOL_ID?.trim() || "";
const gatewayAuthorizerDiscoveryUrl =
  process.env.AGORA_M2_OAUTH_DISCOVERY_URL?.trim() || "";
if (!gatewayAuthorizerPoolId || !gatewayAuthorizerDiscoveryUrl) {
  throw new Error(
    "AGORA_M2_OAUTH_COGNITO_USER_POOL_ID and "
    + "AGORA_M2_OAUTH_DISCOVERY_URL are required for synthesis",
  );
}

const m2OAuthGatewayStack = new M2OAuthGatewayStack(
  app,
  `AgoraM2OAuthGateway-${stage}`,
  {
    stage,
    apiExecutionRole: catalogStorageStack.apiExecutionRole,
    // This stage-qualified [S] coordinate avoids a Catalog -> M2 -> Identity
    // cycle while remaining identical to IdentityStack's physical table name.
    identityDataTableName: identityDataTableName(stage),
    // [S] deploy input. The deployment procedure sources it from the
    // HumanCognitoClientId output; a direct IdentityStack Ref closes the cycle
    // documented on M2OAuthGatewayStackProps.humanCognitoClientIds.
    humanCognitoClientIds: gatewayHumanClientIds,
    // [S] deploy input, same process-env boundary and the same measured cycle
    // (Identity -> CatalogStorage -> M2OAuthGateway -> Identity).
    humanCognitoUserPoolId: gatewayAuthorizerPoolId,
    humanCognitoDiscoveryUrl: gatewayAuthorizerDiscoveryUrl,
    policyEngineMode: m2OAuthGatewayMode,
    attachRequestInterceptor: m2OAuthInterceptor === "on",
    env: { account: process.env.CDK_DEFAULT_ACCOUNT, region: "ap-northeast-2" },
  },
);

// API runtime configuration contract. Deployment automation reads these stable
// outputs and maps them to the matching AGORA_M2_OAUTH_* environment variables.
// The token URL is configured explicitly because the deployed Cognito domain
// may predate and differ from the domain prefix synthesized by this app.
new cdk.CfnOutput(catalogStorageStack, "M2OAuthCognitoUserPoolId", {
  value: m2OAuthGatewayStack.userPool.userPoolId,
});
// `M2OAuthDangerScope` output 은 ADR-0099 결정 8 로 없앴어요. `/danger` scope 자체가
// 사라졌고(발급 경로가 0곳이었어요), 그걸 읽던 Cedar danger 백스톱도 없어요.
new cdk.CfnOutput(catalogStorageStack, "M2OAuthScope", {
  value: m2OAuthGatewayStack.invokeScope,
});
// IA-80: **실효** 발급자예요. 예전에는 항상 봇 pool 이라 컷오버 후 「어느 발급자로
// 배포됐는가」를 가렸어요. 봇 pool 좌표는 M2 스택의 `M2OAuthBotPoolDiscoveryUrl` 로
// 갈라져 있어요. `M2OAuthCognitoUserPoolId` 는 그대로 이 스택 **자신의 봇 pool** 이에요 —
// 발급 대상 pool 좌표(`AGORA_M2_OAUTH_COGNITO_USER_POOL_ID`)의 이전은 IA-78 소관이라
// 여기서 중복 변경하지 않아요.
new cdk.CfnOutput(catalogStorageStack, "M2OAuthDiscoveryUrl", {
  value: m2OAuthGatewayStack.authorizerDiscoveryUrl,
});
new cdk.CfnOutput(catalogStorageStack, "M2OAuthGatewayId", {
  value: m2OAuthGatewayStack.gateway.attrGatewayIdentifier,
});
new cdk.CfnOutput(catalogStorageStack, "M2OAuthGatewayUrl", {
  value: m2OAuthGatewayStack.gateway.attrGatewayUrl,
});
new cdk.CfnOutput(catalogStorageStack, "M2OAuthGatewayArn", {
  value: m2OAuthGatewayStack.gateway.attrGatewayArn,
});
new cdk.CfnOutput(catalogStorageStack, "M2OAuthPolicyEngineArn", {
  value: m2OAuthGatewayStack.policyEngine.attrPolicyEngineArn,
});

new RuntimeAuthorizationStack(app, `AgoraRuntimeAuthorization-${stage}`, {
  stage,
  apiExecutionRole: catalogStorageStack.apiExecutionRole,
  catalogTable: catalogStorageStack.catalogTable,
  artifactsBucket: catalogStorageStack.artifactsBucket,
  identityDataTable: identityStack.identityDataTable,
  registryId: process.env.AGORA_REGISTRY_ID ?? "",
  // CA-05/ADR-0015: 컷오버가 끝나서 agent-registry 가 기본이에요. 이 스택의 registry IAM
  // grant(catalog-storage-stack)도 `agent-registry:*` 만 부여해요. 구 bedrock-agentcore 는
  // 신규 계정에서 IAM 이 거부하므로(Admin 도 AccessDenied) rollback 용으로만 남겨요.
  registryNamespace: process.env.AGORA_REGISTRY_NAMESPACE ?? "agent-registry",
  // IA-79: Portal과 Runtime authorizer Lambda가 같은 명시적 사람-pool 좌표를 읽어요.
  // 값이 없는 독립 synth는 기존 RuntimeDeploy 출력으로 유지하고, 컷오버 배포는
  // AGORA_DEPLOY_COGNITO_* 네 좌표를 한 세트로 공급합니다.
  workloadCognitoDiscoveryUrl:
    process.env.AGORA_DEPLOY_COGNITO_DISCOVERY_URL
    ?? runtimeDeployStack.cognitoDiscoveryUrl,
  workloadCognitoClientId:
    process.env.AGORA_DEPLOY_COGNITO_CLIENT_ID
    ?? runtimeDeployStack.cognitoClientId,
  workloadCognitoScope:
    process.env.AGORA_DEPLOY_COGNITO_SCOPE
    ?? runtimeDeployStack.cognitoScope,
  env: {
    account: process.env.CDK_DEFAULT_ACCOUNT,
    region: process.env.AGORA_DEPLOY_REGION ?? "ap-northeast-2",
  },
});
