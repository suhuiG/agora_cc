import * as cdk from "aws-cdk-lib";
import * as iam from "aws-cdk-lib/aws-iam";
import * as cognito from "aws-cdk-lib/aws-cognito";
import * as bedrockagentcore from "aws-cdk-lib/aws-bedrockagentcore";
import { Stack } from "aws-cdk-lib";
import { Construct } from "constructs";
import {
  cognitoDomainPrefix,
  cognitoUserPoolName,
} from "../cognito-domain";

/**
 * MCP Gateway 인바운드 구성물 — 두 함수로 나뉘어요(원본 생성 순서 보존).
 *   createGatewayCognito: Cognito UserPool/Domain/ResourceServer/Client(M2M
 *     client_credentials). 스택은 이 뒤에 backend 롤을 만든 다음 Gateway를 만들어요.
 *   createGatewayResource: BedrockAgentCore CfnGateway(CUSTOM_JWT authorizer).
 *
 * W0 refactor: RuntimeDeployStack 생성자에서 추출했어요. construct들은 여전히
 * stack(scope) 직속으로 붙어 construct path·logical ID·synth가 100% 동일해요.
 */
export interface GatewayCognito {
  readonly userPool: cognito.UserPool;
  readonly userPoolClient: cognito.UserPoolClient;
  readonly cognitoDiscoveryUrl: string;
  readonly cognitoClientId: string;
  readonly cognitoScope: string;
  readonly cognitoTokenUrl: string;
}

/** Cognito M2M(client_credentials) 풀·클라이언트·리소스 서버. */
export function createGatewayCognito(
  scope: Construct,
  stage: string,
  removalPolicy: cdk.RemovalPolicy,
): GatewayCognito {
  const stack = Stack.of(scope);

  // ── Cognito UserPool (Gateway 인바운드 CUSTOM_JWT 인증용) ───────────────
  // Machine-to-machine 흐름: MCP 클라이언트가 Cognito token endpoint에서
  // client_credentials grant로 JWT를 발급받아 Gateway에 제출해요.
  const userPool = new cognito.UserPool(scope, "McpGatewayUserPool", {
    userPoolName: cognitoUserPoolName(stage, "mcp-gateway"),
    selfSignUpEnabled: false,
    removalPolicy,
  });

  // OAuth domain — client_credentials token endpoint에 필요해요.
  const domainPrefix = cognitoDomainPrefix(stack, stage, "mcp-gateway");
  userPool.addDomain("McpGatewayDomain", {
    cognitoDomain: { domainPrefix },
  });

  // Resource server + custom scope (M2M 최소권한).
  // ⚠️ name과 identifier는 별개예요. identifier는 URL 허용이지만 name은 정규식
  //    [\w\s+=,.@-]+ 만 허용해 슬래시·콜론 불가 — URL을 name으로 재사용하면 배포 실패해요
  //    (실측: 2026-07-16 cdk deploy 롤백). name은 별도 유효 문자열로 명시해요.
  const resourceServer = userPool.addResourceServer("McpGatewayResourceServer", {
    userPoolResourceServerName: `agora-mcp-${stage}`,
    identifier: `https://agora-mcp-${stage}`,
    scopes: [
      { scopeName: "invoke", scopeDescription: "Invoke MCP tool via Gateway" },
    ],
  });

  // M2M 클라이언트 (client_credentials flow).
  const userPoolClient = new cognito.UserPoolClient(scope, "McpGatewayClient", {
    userPool,
    userPoolClientName: `agora-mcp-gateway-client-${stage}`,
    generateSecret: true,
    oAuth: {
      flows: { clientCredentials: true },
      scopes: [
        cognito.OAuthScope.resourceServer(resourceServer,
          { scopeName: "invoke", scopeDescription: "Invoke MCP tool via Gateway" }),
      ],
    },
  });

  // Cognito discovery URL (Gateway의 CUSTOM_JWT authorizerConfiguration.discoveryUrl).
  const cognitoDiscoveryUrl =
    `https://cognito-idp.${stack.region}.amazonaws.com/${userPool.userPoolId}` +
    `/.well-known/openid-configuration`;

  return {
    userPool,
    userPoolClient,
    cognitoDiscoveryUrl,
    cognitoClientId: userPoolClient.userPoolClientId,
    cognitoScope: `https://agora-mcp-${stage}/invoke`,
    cognitoTokenUrl:
      `https://${domainPrefix}.auth.${stack.region}.amazoncognito.com/oauth2/token`,
  };
}

/** BedrockAgentCore Gateway(CUSTOM_JWT authorizer, MCP 프로토콜). */
export function createGatewayResource(
  scope: Construct,
  stage: string,
  gatewayExecRole: iam.Role,
  cognitoDiscoveryUrl: string,
  userPoolClient: cognito.UserPoolClient,
): bedrockagentcore.CfnGateway {
  // ── Bedrock AgentCore Gateway (CUSTOM_JWT authorizer, MCP 프로토콜) ──
  // CfnGateway L1은 aws-cdk-lib aws-bedrockagentcore 모듈에 포함돼 있어요
  // (CDK v2.170+, 2026-07 확인). 기존 계획의 부트스트랩 스크립트 대신
  // CFN 리소스로 직접 프로비저닝해요.
  //
  // 참고: Gateway는 서울(ap-northeast-2, 서울 GA) — bin/agora.ts의 AGORA_DEPLOY_REGION 기본값 —
  // 에 배포돼요. 데이터 플레인과 동일 리전이라 cross-region 소스 pull이 없어요.
  return new bedrockagentcore.CfnGateway(scope, "McpGateway", {
    name: `agora-mcp-gateway-${stage}`,
    roleArn: gatewayExecRole.roleArn,  // Gateway 전용 롤 (lambda:InvokeFunction 포함)
    authorizerType: "CUSTOM_JWT",
    authorizerConfiguration: {
      customJwtAuthorizer: {
        discoveryUrl: cognitoDiscoveryUrl,
        // I3: Cognito M2M(client_credentials) access token은 `aud`(audience) 클레임을
        // 담지 않아요 — 대신 발급 대상 앱 client id를 `client_id` 클레임에 실어요.
        // 따라서 resource-server identifier를 allowedAudience로 두면 모든 인바운드 JWT가
        // 401로 거절돼요. CustomJWT authorizer의 allowedClients(= client_id 매칭)에
        // 앱 client id를 넣는 게 올바른 계약이에요.
        //   근거: AWS Cognito 문서 — client_credentials 토큰엔 aud 없음, client_id 존재.
        //   allowedClients와 allowedAudience는 상호배타(둘 중 하나만).
        // ⚠️ Task 10(실 토큰 검증): 발급된 access token을 디코드해 client_id 클레임이
        //    userPoolClient.userPoolClientId와 일치하는지 실측 확인 필요.
        allowedClients: [userPoolClient.userPoolClientId],
      },
    },
    protocolType: "MCP",
    protocolConfiguration: {
      mcp: {
        // AgentCore가 받는 MCP 버전은 [2025-11-25, 2025-06-18, 2025-03-26] 뿐이에요
        // (실측: 2026-07-16 cdk deploy — 구 2024-11-05은 400 InvalidRequest로 거부).
        supportedVersions: ["2025-06-18", "2025-03-26"],
      },
    },
  });
}
