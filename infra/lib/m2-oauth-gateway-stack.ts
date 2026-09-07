import * as path from "path";
import * as cdk from "aws-cdk-lib";
import * as bedrockagentcore from "aws-cdk-lib/aws-bedrockagentcore";
import * as cloudwatch from "aws-cdk-lib/aws-cloudwatch";
import * as cognito from "aws-cdk-lib/aws-cognito";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as iam from "aws-cdk-lib/aws-iam";
import * as lambda from "aws-cdk-lib/aws-lambda";
import * as logs from "aws-cdk-lib/aws-logs";
import * as xray from "aws-cdk-lib/aws-xray";
import { Construct } from "constructs";
import { assertM2OAuthPoolConsistency } from "./portal-env";
import { Stage } from "./catalog-storage-stack";
import {
  cognitoDomainPrefix,
  cognitoUserPoolName,
  m2OAuthResourceServerIdentifier,
} from "./cognito-domain";

const TARGET_NAME = "m2-oauth-data";
const APPROVED_TARGETS = new Set<string>([TARGET_NAME]);
const INTERCEPTOR_RESERVED_CONCURRENCY = 50;

const OIDC_DISCOVERY_SUFFIX = "/.well-known/openid-configuration";

/**
 * IA-80: `AuthorizerConfiguration.customJWTAuthorizer.discoveryUrl` 은 required
 * 스칼라이고 점진 이관이 API 형태상 불가능해요. 컷오버 한 번이라 오타 하나가 곧
 * **인바운드 전면 401** 이에요 — 그래서 배포 입력의 형태를 synth 에서 끊어요.
 *
 * 기대값 소유자는 대상(배포 입력)이 아니라 OIDC discovery 규약이에요. 흔한 오입력 셋을
 * 전부 잡아요 — `HumanCognitoIssuer` 를 suffix 없이 붙여넣기, token endpoint
 * (`.../oauth2/token`) 붙여넣기, pool id 만 붙여넣기.
 *
 * Cognito host와 pool ID 정체 대조는 형제 게이트
 * `assertM2OAuthPoolConsistency`가 맡아요. 이 helper는 URL 형태 검사를 좁게 유지하고,
 * stack은 둘을 함께 호출해 required pool/discovery 배포 입력을 원자적으로 검증해요.
 *
 * synth는 live pool의 존재나 계정 소유권을 조회하지 않아요. 또 token endpoint 도메인에는
 * pool ID가 없으므로 그것을 discovery 근거로 추측하지 않아요.
 */
export function validateAuthorizerDiscoveryUrl(discoveryUrl: string): void {
  if (!discoveryUrl.startsWith("https://")) {
    throw new Error(
      "AGORA_M2_OAUTH_DISCOVERY_URL must be an https OIDC discovery URL: "
      + discoveryUrl,
    );
  }
  if (!discoveryUrl.endsWith(OIDC_DISCOVERY_SUFFIX)) {
    throw new Error(
      `AGORA_M2_OAUTH_DISCOVERY_URL must end with ${OIDC_DISCOVERY_SUFFIX}: `
      + discoveryUrl,
    );
  }
}

export function validateScopeNamePrefixes(
  scopeNames: readonly string[],
): void {
  for (let left = 0; left < scopeNames.length; left += 1) {
    for (let right = left + 1; right < scopeNames.length; right += 1) {
      const leftName = scopeNames[left];
      const rightName = scopeNames[right];
      if (leftName.startsWith(rightName) || rightName.startsWith(leftName)) {
        throw new Error(
          "scope names must not have a prefix relationship: " +
            `${leftName}, ${rightName}`,
        );
      }
    }
  }
}

export interface M2OAuthGatewayStackProps extends cdk.StackProps {
  readonly stage: Stage;
  readonly apiExecutionRole?: iam.IRole;
  readonly identityDataTableName: string;
  /**
   * `[S]` Cognito human web-client IDs accepted by the REQUEST interceptor,
   * serialized as the `AGORA_GATEWAY_HUMAN_CLIENT_IDS` CSV.
   *
   * `bin/agora.ts` deliberately receives this through required `process.env`
   * deployment input and rejects an empty CSV at synthesis. The deployment
   * procedure populates that input from IdentityStack's
   * `HumanCognitoClientId` output. A direct
   * `identityStack.userPoolClient.userPoolClientId` Ref was synthesized during
   * IA-88 and CDK rejected the cycle
   * Identity -> CatalogStorage -> M2OAuthGateway -> Identity.
   *
   * `humanCognitoUserPoolId`와 `humanCognitoDiscoveryUrl`도 같은 required
   * deployment-input boundary를 사용해요. 직접 M2OAuthGateway -> Identity Ref를
   * 추가하지 마세요.
   */
  readonly humanCognitoClientIds: string[];
  /**
   * `[S]` Credential-provider client를 발급한 Cognito pool ID.
   * Gateway inbound issuer와 같아야 하며 stack이 discovery URL에서 독립 파싱해 대조해요.
   */
  readonly humanCognitoUserPoolId: string;
  /**
   * `[S]` Gateway 인바운드 JWT 발급자의 OIDC discovery URL, 필수 배포 입력
   * `AGORA_M2_OAUTH_DISCOVERY_URL` 로 받아요.
   *
   * `bin/agora.ts` 가 `process.env` 에서 읽어 넘겨요. 형제인
   * `humanCognitoClientIds` 와 같은 배포-입력 경계이고, 이유도 같아요 — 직접
   * `identityStack` Ref 는 IA-88 이 시도했고 CDK 가
   * `Identity -> CatalogStorage -> M2OAuthGateway -> Identity` 사이클로 거부했어요.
   * 운영 절차가 `HumanCognitoIssuer` output 에서 값을 가져와요.
   *
   * 자동 bot-pool 폴백은 없어요. 롤백하려면 pool ID와 discovery URL을 bot pool 좌표로
   * 함께 명시해야 하며, bot pool 자체와 그 discovery output은 계속 보존해요.
   */
  readonly humanCognitoDiscoveryUrl: string;
  /**
   * Policy Engine 연결 모드. `bin/agora.ts` 의 기본값은 **`ENFORCE`** 예요(2026-08-29 변경).
   *
   * Agora 는 `LOG_ONLY` 를 쓰지 않아요 — 그 모드에서 본 "거부 0건" 은 강제의 증거가 아니에요.
   */
  readonly policyEngineMode?: "LOG_ONLY" | "ENFORCE";
  /**
   * REQUEST interceptor 를 Gateway 에 실제로 부착할지. `bin/agora.ts` 의 기본값은
   * **`true`** 예요(2026-08-29 변경).
   *
   * 원래 기본이 미부착이었어요. 근거는 "`X-Agora-Call` handle 생산자가 0곳" 이었는데
   * IA-55·IA-61 로 해소됐고, 그날 context 없이 배포했다가 라이브 `interceptorConfigurations`
   * 가 `undefined` 로 덮여 **유일한 강제 지점이 지워졌어요**(ADR-0091).
   *
   * Lambda·alias·alarm 은 이 값과 무관하게 항상 만들어요 — 부착 여부만 스위치예요.
   */
  readonly attachRequestInterceptor?: boolean;
}

/**
 * IA-15a M2 OAuth 병행 Gateway.
 *
 * agent별 app client 발급과 OAuthUser Cedar policy는 후속 작업이 소유한다.
 * 이 스택은 공유 invoke scope로 인바운드를 제한하고 빈 Policy Engine을 제공한다.
 */
export class M2OAuthGatewayStack extends cdk.Stack {
  public readonly policyEngine: bedrockagentcore.CfnPolicyEngine;
  public readonly gateway: bedrockagentcore.CfnGateway;
  public readonly gatewayTarget: bedrockagentcore.CfnGatewayTarget;
  public readonly userPool: cognito.UserPool;
  /**
   * Gateway authorizer 가 실제로 쓰는 required 배포 입력 discoveryUrl.
   *
   * 옛 이름은 `discoveryUrl` 이었고 항상 봇 pool 이었어요. IA-80 이 두 값을 갈라놓아서
   * 이름을 바꿨어요 — 소비자가 「실효값」과 「봇 pool 좌표」 중 하나를 **고르도록**
   * 강제해야 컷오버 뒤 조용한 의미 드리프트가 안 생겨요.
   */
  public readonly authorizerDiscoveryUrl: string;
  /** 이 스택 자신의 봇 pool 에서 파생한 discoveryUrl. 되돌리기 좌표예요. */
  public readonly botPoolDiscoveryUrl: string;
  public readonly invokeScope: string;

  constructor(scope: Construct, id: string, props: M2OAuthGatewayStackProps) {
    super(scope, id, props);

    if (!APPROVED_TARGETS.has(TARGET_NAME)) {
      throw new Error(`target ${TARGET_NAME} is not in the approved allowlist`);
    }

    const engineMode = props.policyEngineMode ?? "LOG_ONLY";
    const humanCognitoClientIds = [...new Set(
      props.humanCognitoClientIds
        .map((clientId) => clientId.trim())
        .filter(Boolean),
    )];
    const removalPolicy = props.stage === "prod"
      ? cdk.RemovalPolicy.RETAIN
      : cdk.RemovalPolicy.DESTROY;
    const identityDataTable = dynamodb.Table.fromTableName(
      this,
      "GatewayInterceptorIdentityData",
      props.identityDataTableName,
    );
    const resourceServerIdentifier = m2OAuthResourceServerIdentifier(
      props.stage,
    );
    const invokeScope = `${resourceServerIdentifier}/invoke`;
    // scope 는 `/invoke` 하나예요. `/danger` scope 는 ADR-0099 결정 8 로 없앴어요 —
    // 그 scope 를 발급하는 경로가 처음부터 없어서(app client 는 `[invoke]` 만 받아요)
    // 그걸 조건으로 쓴 Cedar danger 백스톱이 조건부 게이트가 아니라 영구 차단이었어요
    // (IH-130). 도구를 막는 것은 REQUEST interceptor 하나예요.
    //
    // 접두어 검증은 scope 가 하나여도 남겨요 — 새 scope 를 더하는 사람이 배열에 넣기만
    // 하면 ADR-0085 결정 3(접두어 충돌 금지)이 그대로 걸려요.
    validateScopeNamePrefixes([invokeScope]);
    this.invokeScope = invokeScope;

    this.userPool = new cognito.UserPool(this, "M2OAuthUserPool", {
      userPoolName: cognitoUserPoolName(props.stage, "m2-oauth"),
      selfSignUpEnabled: false,
      removalPolicy,
    });
    const domainPrefix = cognitoDomainPrefix(this, props.stage, "m2-oauth");
    this.userPool.addDomain("M2OAuthDomain", {
      cognitoDomain: { domainPrefix },
    });
    // HP-13's AGORA_M2_OAUTH_TOKEN_URL remains explicitly configured; this
    // domain reconciliation does not derive or overwrite that environment value.

    // Resource server name is deliberately not a URL: Cognito restricts it to
    // [\w\s+=,.@-]+ even though the identifier permits URL characters.
    const resourceServer = this.userPool.addResourceServer(
      "M2OAuthResourceServer",
      {
        userPoolResourceServerName: `agora-m2-oauth-${props.stage}`,
        identifier: resourceServerIdentifier,
        scopes: [
          {
            scopeName: "invoke",
            scopeDescription: "Invoke MCP tool via M2 OAuth Gateway",
          },
        ],
      },
    );

    // Runtime agent clients are provisioned by IA-15b. This single client is
    // retained only for stack validation and does not gate Gateway admission.
    new cognito.UserPoolClient(this, "M2OAuthBootstrapClient", {
      userPool: this.userPool,
      userPoolClientName: `agora-m2-oauth-bootstrap-${props.stage}`,
      generateSecret: true,
      oAuth: {
        flows: { clientCredentials: true },
        scopes: [
          cognito.OAuthScope.resourceServer(resourceServer, {
            scopeName: "invoke",
            scopeDescription: "Invoke MCP tool via M2 OAuth Gateway",
          }),
        ],
      },
    });

    // 봇 pool 은 되돌리기 자원이라 생성을 끄지 않아요 — dev 의 removalPolicy 가 DESTROY 라
    // props 로 생성을 분기하면 그 순간 pool·resource server·app client 11개가 사라져요
    // (ADR-0102 배포 순서 말미). **참조만 바꿔요.**
    const botPoolDiscoveryUrl =
      `https://cognito-idp.${this.region}.amazonaws.com/${this.userPool.userPoolId}` +
      OIDC_DISCOVERY_SUFFIX;
    this.botPoolDiscoveryUrl = botPoolDiscoveryUrl;

    const suppliedPoolId = props.humanCognitoUserPoolId.trim();
    const suppliedDiscoveryUrl = props.humanCognitoDiscoveryUrl.trim();
    if (!suppliedPoolId || !suppliedDiscoveryUrl) {
      throw new Error(
        "AGORA_M2_OAUTH_COGNITO_USER_POOL_ID and "
        + "AGORA_M2_OAUTH_DISCOVERY_URL are required for synthesis",
      );
    }
    validateAuthorizerDiscoveryUrl(suppliedDiscoveryUrl);
    assertM2OAuthPoolConsistency({
      AGORA_M2_OAUTH_COGNITO_USER_POOL_ID: suppliedPoolId,
      AGORA_M2_OAUTH_DISCOVERY_URL: suppliedDiscoveryUrl,
    });
    const authorizerDiscoveryUrl = suppliedDiscoveryUrl;
    this.authorizerDiscoveryUrl = authorizerDiscoveryUrl;

    props.apiExecutionRole?.addToPrincipalPolicy(new iam.PolicyStatement({
      actions: [
        "cognito-idp:CreateUserPoolClient",
        "cognito-idp:DeleteUserPoolClient",
        "cognito-idp:DescribeUserPoolClient",
        "cognito-idp:ListUserPoolClients",
      ],
      resources: [this.userPool.userPoolArn],
    }));

    const userTable = new dynamodb.Table(this, "M2OAuthUserTable", {
      tableName: `agora-m2-oauth-user-${props.stage}`,
      partitionKey: { name: "user_id", type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      removalPolicy,
    });
    const itemTable = new dynamodb.Table(this, "M2OAuthItemTable", {
      tableName: `agora-m2-oauth-item-${props.stage}`,
      partitionKey: { name: "user_id", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "order_id", type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      removalPolicy,
    });
    const target = new lambda.Function(this, "M2OAuthDataTarget", {
      functionName: `agora-m2-oauth-data-${props.stage}`,
      runtime: lambda.Runtime.PYTHON_3_12,
      architecture: lambda.Architecture.ARM_64,
      handler: "index.lambda_handler",
      code: lambda.Code.fromAsset(
        path.join(__dirname, "../gateway-targets/data-target"),
        { exclude: ["**/__pycache__", "**/*.pyc"] },
      ),
      memorySize: 256,
      timeout: cdk.Duration.seconds(10),
      environment: {
        USER_TABLE_NAME: userTable.tableName,
        ITEM_TABLE_NAME: itemTable.tableName,
      },
    });
    userTable.grant(target, "dynamodb:GetItem", "dynamodb:Scan");
    itemTable.grant(target, "dynamodb:GetItem", "dynamodb:Query", "dynamodb:Scan");

    const stageName = props.stage[0].toUpperCase() + props.stage.slice(1);
    this.policyEngine = new bedrockagentcore.CfnPolicyEngine(
      this,
      "M2OAuthPolicyEngine",
      {
        name: `AgoraM2OAuthGateway${stageName}`,
        description:
          "M2 OAuth Gateway policy engine; policies owned by AgentPolicyDeployer.",
      },
    );
    props.apiExecutionRole?.addToPrincipalPolicy(new iam.PolicyStatement({
      actions: ["bedrock-agentcore:TagResource"],
      resources: [
        this.policyEngine.attrPolicyEngineArn,
        `${this.policyEngine.attrPolicyEngineArn}/policy/*`,
      ],
    }));

    const interceptorLogGroup = new logs.LogGroup(
      this,
      "GatewayRequestInterceptorLogs",
      {
        logGroupName:
          `/aws/lambda/agora-gateway-request-interceptor-${props.stage}`,
        retention: props.stage === "prod"
          ? logs.RetentionDays.THREE_MONTHS
          : logs.RetentionDays.ONE_MONTH,
        removalPolicy,
      },
    );
    const requestInterceptor = new lambda.Function(
      this,
      "GatewayRequestInterceptor",
      {
        functionName: `agora-gateway-request-interceptor-${props.stage}`,
        runtime: lambda.Runtime.PYTHON_3_12,
        architecture: lambda.Architecture.ARM_64,
        handler: "agora.gateway_request_interceptor.handler",
        code: lambda.Code.fromAsset(path.join(__dirname, "../../api/src"), {
          exclude: ["**/__pycache__", "**/*.pyc"],
        }),
        memorySize: 512,
        timeout: cdk.Duration.seconds(5),
        reservedConcurrentExecutions: INTERCEPTOR_RESERVED_CONCURRENCY,
        logGroup: interceptorLogGroup,
        environment: {
          AGORA_ROLE: "lambda",
          AGORA_GATEWAY_HUMAN_CLIENT_IDS: humanCognitoClientIds.join(","),
          AGORA_IDENTITY_TABLE: identityDataTable.tableName,
          AGORA_IDENTITY_REGION: this.region,
        },
      },
    );
    identityDataTable.grant(
      requestInterceptor,
      "dynamodb:GetItem",
      "dynamodb:Query",
    );
    const requestInterceptorAlias = new lambda.Alias(
      this,
      "GatewayRequestInterceptorLive",
      {
        aliasName: "live",
        version: requestInterceptor.currentVersion,
      },
    );

    const gatewayRole = new iam.Role(this, "M2OAuthGatewayRole", {
      assumedBy: new iam.ServicePrincipal(
        "bedrock-agentcore.amazonaws.com",
        {
          conditions: {
            StringEquals: { "aws:SourceAccount": this.account },
            ArnLike: {
              "aws:SourceArn":
                `arn:${cdk.Aws.PARTITION}:bedrock-agentcore:${this.region}:` +
                `${this.account}:*`,
            },
          },
        },
      ),
      description: "Execution role for the M2 OAuth Gateway.",
    });
    gatewayRole.addToPolicy(new iam.PolicyStatement({
      actions: ["lambda:InvokeFunction"],
      // Deploy-mode MCP Lambda names are user names or job IDs, so match the
      // established deploy Gateway role's account/region scope.
      resources: [
        `arn:${cdk.Aws.PARTITION}:lambda:${this.region}:${this.account}:function:*`,
      ],
    }));
    gatewayRole.addToPolicy(new iam.PolicyStatement({
      actions: ["bedrock-agentcore:GetPolicyEngine"],
      resources: [this.policyEngine.attrPolicyEngineArn],
    }));
    gatewayRole.addToPolicy(new iam.PolicyStatement({
      actions: [
        "bedrock-agentcore:AuthorizeAction",
        "bedrock-agentcore:PartiallyAuthorizeActions",
      ],
      resources: [
        this.policyEngine.attrPolicyEngineArn,
        `arn:${cdk.Aws.PARTITION}:bedrock-agentcore:${this.region}:` +
          `${this.account}:gateway/*`,
      ],
    }));
    requestInterceptorAlias.grantInvoke(gatewayRole);

    this.gateway = new bedrockagentcore.CfnGateway(this, "M2OAuthGateway", {
      name: `agora-m2-oauth-${props.stage}`,
      description:
        "M2 OAuth Gateway (JWT inbound; ledger-backed REQUEST authorization).",
      roleArn: gatewayRole.roleArn,
      protocolType: "MCP",
      protocolConfiguration: {
        mcp: { supportedVersions: ["2025-06-18", "2025-03-26"] },
      },
      authorizerType: "CUSTOM_JWT",
      authorizerConfiguration: {
        customJwtAuthorizer: {
          discoveryUrl: authorizerDiscoveryUrl,
          // Agent clients are created dynamically, so a static client allowlist
          // cannot represent the fleet. The REQUEST interceptor resolves each
          // validated client_id through the Identity ledger.
          allowedScopes: [invokeScope],
        },
      },
      policyEngineConfiguration: {
        arn: this.policyEngine.attrPolicyEngineArn,
        mode: engineMode,
      },
      // 부착은 명시적 opt-in 이에요 (`-c m2OAuthInterceptor=on`). 근거는 props 주석.
      interceptorConfigurations: props.attachRequestInterceptor
        ? [
          {
            inputConfiguration: { passRequestHeaders: true },
            interceptionPoints: ["REQUEST"],
            interceptor: {
              lambda: { arn: requestInterceptorAlias.functionArn },
            },
          },
        ]
        : undefined,
    });
    this.gateway.node.addDependency(gatewayRole);

    const traceSourceName = `agora-m2-oauth-gateway-traces-${props.stage}`;
    const traceDestination = new logs.CfnDeliveryDestination(
      this,
      "M2OAuthGatewayTraceDestination",
      {
        name: `agora-m2-oauth-gateway-xray-${props.stage}`,
        deliveryDestinationType: "XRAY",
      },
    );
    const traceSource = new logs.CfnDeliverySource(
      this,
      "M2OAuthGatewayTraceSource",
      {
        name: traceSourceName,
        logType: "TRACES",
        resourceArn: this.gateway.attrGatewayArn,
      },
    );
    traceSource.addResourceDependency(this.gateway);

    // CloudWatch vended trace delivery writes to X-Ray. Transaction Search,
    // already enabled at account level, then exposes those spans in aws/spans.
    const traceResourcePolicy = new xray.CfnResourcePolicy(
      this,
      "M2OAuthGatewayTraceResourcePolicy",
      {
        policyName: `agora-m2-oauth-gateway-traces-${props.stage}`,
        policyDocument: cdk.Fn.sub(JSON.stringify({
          Version: "2012-10-17",
          Statement: [{
            Sid: "AWSLogDeliveryWrite",
            Effect: "Allow",
            Principal: { Service: "delivery.logs.amazonaws.com" },
            Action: "xray:PutTraceSegments",
            Resource: "*",
            Condition: {
              StringEquals: {
                "aws:SourceAccount": "${AWS::AccountId}",
              },
              "ForAllValues:ArnLike": {
                "logs:LogGeneratingResourceArns":
                  "arn:${AWS::Partition}:bedrock-agentcore:${AWS::Region}:" +
                  "${AWS::AccountId}:gateway/*",
              },
              ArnLike: {
                "aws:SourceArn":
                  "arn:${AWS::Partition}:logs:${AWS::Region}:" +
                  "${AWS::AccountId}:delivery-source:" + traceSourceName,
              },
            },
          }],
        })),
      },
    );
    const traceDelivery = new logs.CfnDelivery(
      this,
      "M2OAuthGatewayTraceDelivery",
      {
        deliverySourceName: traceSourceName,
        deliveryDestinationArn: traceDestination.attrArn,
      },
    );
    traceDelivery.addResourceDependency(traceSource);
    traceDelivery.addResourceDependency(traceDestination);
    traceDelivery.addResourceDependency(traceResourcePolicy);

    const invokePermission = new lambda.CfnPermission(
      this,
      "M2OAuthGatewayInvokePermission",
      {
        action: "lambda:InvokeFunction",
        functionName: target.functionArn,
        principal: "bedrock-agentcore.amazonaws.com",
        sourceAccount: this.account,
        sourceArn: this.gateway.attrGatewayArn,
      },
    );
    new lambda.CfnPermission(
      this,
      "M2OAuthGatewayInterceptorInvokePermission",
      {
        action: "lambda:InvokeFunction",
        functionName: requestInterceptorAlias.functionArn,
        principal: "bedrock-agentcore.amazonaws.com",
        sourceAccount: this.account,
        sourceArn: this.gateway.attrGatewayArn,
      },
    );
    this.gatewayTarget = new bedrockagentcore.CfnGatewayTarget(
      this,
      "M2OAuthGatewayTarget",
      {
        gatewayIdentifier: this.gateway.attrGatewayIdentifier,
        name: TARGET_NAME,
        description: "Read-only USER and ITEM demo target for M2 OAuth validation.",
        credentialProviderConfigurations: [
          { credentialProviderType: "GATEWAY_IAM_ROLE" },
        ],
        targetConfiguration: {
          mcp: {
            lambda: {
              lambdaArn: target.functionArn,
              toolSchema: { inlinePayload: m2OAuthToolSchema() },
            },
          },
        },
      },
    );
    this.gatewayTarget.addResourceDependency(invokePermission);

    const alarmPeriod = cdk.Duration.minutes(1);
    new cloudwatch.Alarm(this, "GatewayRequestInterceptorErrors", {
      alarmName: `agora-gateway-request-interceptor-errors-${props.stage}`,
      metric: requestInterceptorAlias.metricErrors({
        period: alarmPeriod,
        statistic: "Sum",
      }),
      threshold: 1,
      evaluationPeriods: 1,
      comparisonOperator:
        cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    new cloudwatch.Alarm(this, "GatewayRequestInterceptorThrottles", {
      alarmName: `agora-gateway-request-interceptor-throttles-${props.stage}`,
      metric: requestInterceptorAlias.metricThrottles({
        period: alarmPeriod,
        statistic: "Sum",
      }),
      threshold: 1,
      evaluationPeriods: 1,
      comparisonOperator:
        cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    new cloudwatch.Alarm(this, "GatewayRequestInterceptorDuration", {
      alarmName: `agora-gateway-request-interceptor-duration-${props.stage}`,
      metric: requestInterceptorAlias.metricDuration({
        period: alarmPeriod,
        statistic: "Maximum",
      }),
      threshold: 2_000,
      evaluationPeriods: 1,
      comparisonOperator:
        cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });

    new cdk.CfnOutput(this, "M2OAuthGatewayId", {
      value: this.gateway.attrGatewayIdentifier,
    });
    new cdk.CfnOutput(this, "M2OAuthGatewayUrl", {
      value: this.gateway.attrGatewayUrl,
    });
    new cdk.CfnOutput(this, "M2OAuthGatewayArn", {
      value: this.gateway.attrGatewayArn,
    });
    new cdk.CfnOutput(this, "M2OAuthPolicyEngineArn", {
      value: this.policyEngine.attrPolicyEngineArn,
    });
    new cdk.CfnOutput(this, "M2OAuthCognitoUserPoolId", {
      value: this.userPool.userPoolId,
    });
    // 실효 발급자예요 — 옛날에는 이 output 이 항상 봇 pool 이라 「어느 발급자로 배포됐는가」를
    // 가렸어요(IA-80 백로그 작업 ②). required 배포 입력의 실효값과 출처를 함께 내보내고,
    // 봇 pool 좌표는 명시적 롤백용 별 output 으로 갈라 남겨요.
    new cdk.CfnOutput(this, "M2OAuthDiscoveryUrl", {
      value: authorizerDiscoveryUrl,
    });
    new cdk.CfnOutput(this, "M2OAuthAuthorizerIssuerSource", {
      value: "deployment-input",
      description:
        "The Gateway authorizer discoveryUrl is a required deployment input. "
        + "Synthesis verifies its pool ID matches "
        + "AGORA_M2_OAUTH_COGNITO_USER_POOL_ID.",
    });
    new cdk.CfnOutput(this, "M2OAuthBotPoolDiscoveryUrl", {
      value: botPoolDiscoveryUrl,
      description:
        "Explicit rollback coordinate for this stack's own M2M pool. "
        + "Rollback must supply the matching pool ID and discovery URL.",
    });
    new cdk.CfnOutput(this, "M2OAuthScope", {
      value: invokeScope,
    });
    new cdk.CfnOutput(this, "M2OAuthGatewayMode", {
      value: engineMode,
    });
  }
}

function m2OAuthToolSchema() {
  return [
    toolDef("list_users", "List users from the USER table.", {
      properties: {
        limit: { type: "number", description: "Maximum number of users." },
      },
      required: [],
    }),
    toolDef("get_user", "Read one user record.", {
      properties: {
        user_id: { type: "string", description: "Customer identifier." },
      },
      required: ["user_id"],
    }),
    toolDef("list_items", "List items, optionally filtered by user.", {
      properties: {
        user_id: { type: "string", description: "Customer identifier." },
        limit: { type: "number", description: "Maximum number of items." },
      },
      required: [],
    }),
    toolDef("get_item", "Read one item record.", {
      properties: {
        user_id: { type: "string", description: "Customer identifier." },
        order_id: { type: "string", description: "Order identifier." },
      },
      required: ["user_id", "order_id"],
    }),
  ];
}

function toolDef(
  name: string,
  description: string,
  schema: {
    properties: Record<string, { type: string; description: string }>;
    required: string[];
  },
) {
  return {
    name,
    description,
    inputSchema: {
      type: "object",
      properties: schema.properties,
      required: schema.required,
      additionalProperties: false,
    },
  };
}
