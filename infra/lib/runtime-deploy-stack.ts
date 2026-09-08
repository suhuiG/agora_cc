import * as cdk from "aws-cdk-lib";
import { Construct } from "constructs";
import * as iam from "aws-cdk-lib/aws-iam";
import * as s3 from "aws-cdk-lib/aws-s3";
import { createBuildPipeline } from "./runtime-deploy/build-pipeline";
import {
  createRuntimeExecutionRoles,
  createBackendRoles,
} from "./runtime-deploy/iam-roles";
import {
  createGatewayCognito,
  createGatewayResource,
} from "./runtime-deploy/gateway";
import {
  createIsolatedAgentPermissionBoundary,
  createRuntimePermissionBoundary,
} from "./runtime-deploy/permission-boundary";
import { createBackendPermissionBoundary } from "./runtime-deploy/backend-permission-boundary";

export type Stage = "dev" | "prod";
export interface RuntimeDeployStackProps extends cdk.StackProps {
  readonly stage?: Stage;
  /** Portal backend role that owns builtin control-plane lifecycle operations. */
  readonly apiExecutionRole?: iam.IRole;
}

/**
 * RuntimeDeployStack — MCP 소스 → AgentCore Runtime 배포 인프라.
 * ECR(container 이미지) + CodeBuild(ARM64 빌드) + artifact S3(codezip) +
 * DeployJobs DDB + Runtime 실행 롤 + 파이프라인 롤.
 * 공용 Gateway(IAM authorizer)는 GA 리전에 별도 프로비저닝(§9 — 실측 후 확정).
 *
 * W0 refactor: 구성물을 세 모듈로 분리했어요(build-pipeline / iam-roles / gateway).
 * 각 헬퍼는 scope로 이 stack을 받아 construct를 stack 직속으로 붙이고, 아래 호출 순서는
 * 분리 전 생성자의 순서를 그대로 따라요. 그래서 construct path·logical ID·synth 출력이
 * 분리 전과 byte 단위로 동일해요(기존 dev 배포 무교체 보장).
 */
export class RuntimeDeployStack extends cdk.Stack {
  public readonly cognitoDiscoveryUrl: string;
  public readonly cognitoClientId: string;
  public readonly cognitoScope: string;
  public readonly builtinToolExecutionRoleArn: string;
  public readonly builtinRecordingBucketName: string;
  public readonly agentRuntimeSharedPolicyArn: string;
  public readonly runtimePermissionBoundaryArn: string;

  constructor(scope: Construct, id: string, props?: RuntimeDeployStackProps) {
    super(scope, id, props);
    const stage: Stage = props?.stage ?? "prod";
    const isProd = stage === "prod";
    const removalPolicy = isProd ? cdk.RemovalPolicy.RETAIN : cdk.RemovalPolicy.DESTROY;

    // 1) 빌드 파이프라인 — ECR + artifact S3 + CodeBuild + DeployJobs DDB.
    const { repo, artifactBucket, project, jobsTable } =
      createBuildPipeline(this, stage, removalPolicy, isProd);

    const builtinRecordingBucket = new s3.Bucket(this, "BuiltinRecordingStore", {
      encryption: s3.BucketEncryption.S3_MANAGED,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      enforceSSL: true,
      versioned: true,
      lifecycleRules: [{
        expiration: cdk.Duration.days(30),
        noncurrentVersionExpiration: cdk.Duration.days(7),
      }],
      removalPolicy,
      autoDeleteObjects: !isProd,
    });
    this.builtinRecordingBucketName = builtinRecordingBucket.bucketName;

    // IAM은 리전 무관이라 이름을 고정하지 않는 공통 실행 권한 상한선.
    const permissionsBoundary =
      createRuntimePermissionBoundary(
        this,
        stage,
        repo,
        artifactBucket,
        builtinRecordingBucket,
      );
    const isolatedAgentPermissionsBoundary =
      createIsolatedAgentPermissionBoundary(
        this,
        stage,
        repo,
        artifactBucket,
        builtinRecordingBucket,
      );

    // 2) Runtime 실행 롤 2종 — McpRuntime(container/ECR+MCP) + Agent(codezip A2A).
    const { execRole, agentExecRole, agentSharedPolicy, builtinToolExecRole } =
      createRuntimeExecutionRoles(
        this,
        stage,
        repo,
        artifactBucket,
        builtinRecordingBucket,
        permissionsBoundary,
      );
    this.agentRuntimeSharedPolicyArn = agentSharedPolicy.managedPolicyArn;
    this.runtimePermissionBoundaryArn =
      isolatedAgentPermissionsBoundary.managedPolicyArn;
    this.builtinToolExecutionRoleArn = builtinToolExecRole.roleArn;

    const agentRoleResource =
      `arn:${this.partition}:iam::${this.account}:role/agora/agent/*`;
    props?.apiExecutionRole?.addToPrincipalPolicy(new iam.PolicyStatement({
      sid: "CreateAndConfigureIsolatedAgentRoles",
      actions: ["iam:CreateRole", "iam:PutRolePolicy"],
      resources: [agentRoleResource],
      conditions: {
        StringEquals: {
          "iam:PermissionsBoundary":
            isolatedAgentPermissionsBoundary.managedPolicyArn,
        },
      },
    }));
    props?.apiExecutionRole?.addToPrincipalPolicy(new iam.PolicyStatement({
      sid: "TagAndDeleteIsolatedAgentRoles",
      actions: [
        "iam:TagRole",
        // AwsDeployAdapter only calls GetRole while reconciling or observing
        // roles created under this path, so no account-wide read is needed.
        "iam:GetRole",
        "iam:DeleteRolePolicy",
        "iam:DetachRolePolicy",
        "iam:DeleteRole",
      ],
      resources: [agentRoleResource],
    }));
    props?.apiExecutionRole?.addToPrincipalPolicy(new iam.PolicyStatement({
      sid: "AttachAgentRuntimeSharedPolicy",
      actions: ["iam:AttachRolePolicy"],
      resources: [agentRoleResource],
      conditions: {
        StringEquals: {
          "iam:PolicyARN": agentSharedPolicy.managedPolicyArn,
        },
      },
    }));
    props?.apiExecutionRole?.addToPrincipalPolicy(new iam.PolicyStatement({
      sid: "PassIsolatedAgentRoles",
      actions: ["iam:PassRole"],
      resources: [agentRoleResource],
      conditions: {
        StringEquals: {
          "iam:PassedToService": "bedrock-agentcore.amazonaws.com",
        },
      },
    }));

    props?.apiExecutionRole?.addToPrincipalPolicy(new iam.PolicyStatement({
      sid: "ManageAgentBuiltinResources",
      actions: [
        "bedrock-agentcore:CreateBrowser",
        "bedrock-agentcore:GetBrowser",
        "bedrock-agentcore:DeleteBrowser",
        "bedrock-agentcore:CreateCodeInterpreter",
        "bedrock-agentcore:GetCodeInterpreter",
        "bedrock-agentcore:DeleteCodeInterpreter",
        "bedrock-agentcore:TagResource",
        "bedrock-agentcore:ListTagsForResource",
      ],
      resources: [
        `arn:${this.partition}:bedrock-agentcore:${this.region}:${this.account}:browser/*`,
        `arn:${this.partition}:bedrock-agentcore:${this.region}:${this.account}:code-interpreter/*`,
      ],
      conditions: {
        StringEquals: {
          "aws:RequestedRegion": this.region,
          "aws:PrincipalAccount": this.account,
        },
      },
    }));
    props?.apiExecutionRole?.addToPrincipalPolicy(new iam.PolicyStatement({
      sid: "ListAgentBuiltinResources",
      actions: [
        "bedrock-agentcore:ListBrowsers",
        "bedrock-agentcore:ListCodeInterpreters",
      ],
      // List operations have no resource-level authorization target.
      resources: ["*"],
      conditions: {
        StringEquals: {
          "aws:RequestedRegion": this.region,
          "aws:PrincipalAccount": this.account,
        },
      },
    }));
    props?.apiExecutionRole?.addToPrincipalPolicy(new iam.PolicyStatement({
      sid: "ObserveAgentCoreIdentityQuota",
      actions: ["servicequotas:ListServiceQuotas"],
      resources: ["*"],
      conditions: {
        StringEquals: {
          "aws:RequestedRegion": this.region,
          "aws:PrincipalAccount": this.account,
        },
      },
    }));
    props?.apiExecutionRole?.addToPrincipalPolicy(new iam.PolicyStatement({
      sid: "ObserveGatewayTargetQuota",
      actions: [
        "bedrock-agentcore:ListGatewayTargets",
        "servicequotas:ListServiceQuotas",
        "servicequotas:GetServiceQuota",
      ],
      resources: ["*"],
      conditions: {
        StringEquals: {
          "aws:RequestedRegion": this.region,
          "aws:PrincipalAccount": this.account,
        },
      },
    }));
    props?.apiExecutionRole?.addToPrincipalPolicy(new iam.PolicyStatement({
      sid: "PassBuiltinToolExecutionRole",
      actions: ["iam:PassRole"],
      resources: [builtinToolExecRole.roleArn],
      conditions: {
        StringEquals: {
          "iam:PassedToService": "bedrock-agentcore.amazonaws.com",
        },
      },
    }));
    props?.apiExecutionRole?.addToPrincipalPolicy(new iam.PolicyStatement({
      sid: "ManageBuiltinVendedLogs",
      actions: [
        "logs:CreateLogGroup",
        "logs:DescribeLogGroups",
        "logs:PutDeliveryDestination",
        "logs:PutDeliverySource",
        "logs:DescribeDeliveries",
        "logs:CreateDelivery",
        "logs:TagResource",
        "logs:DeleteDelivery",
        "logs:DeleteDeliverySource",
        // CreateDelivery to a CWL destination makes CloudWatch Logs write an
        // `AWSLogDeliveryWrite20150319` resource policy on the target log group
        // for `delivery.logs.amazonaws.com`. Without these two the caller gets
        // `AccessDeniedException: Access Denied for this Delivery Destination`
        // even though every logs:*Delivery* action is allowed (measured
        // 2026-08-21, job agent-client-cabc219adac3beebad8c206e648453c8).
        // Official policy: AWS-logs-infrastructure-V2-CloudWatchLogs.html
        // (`AllowUpdatesToResourcePolicyCWL`).
        "logs:PutResourcePolicy",
        "logs:DescribeResourcePolicies",
      ],
      // Vended delivery control APIs do not expose a consistent resource ARN.
      resources: ["*"],
      conditions: {
        StringEquals: {
          "aws:RequestedRegion": this.region,
          "aws:PrincipalAccount": this.account,
        },
      },
    }));
    // TRACES delivery targets an XRAY destination, whose auto-created resource
    // policy lives in X-Ray, not CloudWatch Logs. Official policy:
    // AWS-logs-infrastructure-V2-XRayTraces.html
    // (`AllowUpdatesToResourcePolicyXRay`). X-Ray resource-policy APIs are
    // account-scoped and take no resource ARN, so `*` is the documented form.
    props?.apiExecutionRole?.addToPrincipalPolicy(new iam.PolicyStatement({
      sid: "ManageBuiltinTraceDeliveryPolicy",
      actions: [
        "xray:PutResourcePolicy",
        "xray:ListResourcePolicies",
        "xray:GetTraceSegmentDestination",
      ],
      resources: ["*"],
      conditions: {
        StringEquals: {
          "aws:RequestedRegion": this.region,
          "aws:PrincipalAccount": this.account,
        },
      },
    }));

    // 3) Gateway 인바운드 Cognito — M2M(client_credentials) 풀·클라이언트.
    const cognitoParts = createGatewayCognito(this, stage, removalPolicy);
    const { userPool, userPoolClient } = cognitoParts;
    this.cognitoDiscoveryUrl = cognitoParts.cognitoDiscoveryUrl;
    this.cognitoClientId = cognitoParts.cognitoClientId;
    this.cognitoScope = cognitoParts.cognitoScope;

    // 4) 배포 산출물 호출 롤 2종 — McpLambda(배포 MCP 실행) + McpGateway(아웃바운드).
    //
    // 상한선은 agent runtime 쪽과 **분리**해요. 공유 boundary 는 agent 전용 허용으로
    // 5,926/6,144 자를 이미 쓰고 있어서 문장을 더 넣을 수 없고(2026-08-30 실측), 배포된
    // MCP Lambda 는 그 허용을 하나도 안 써요. 근거와 내용은
    // `backend-permission-boundary.ts` 클래스 주석에 있어요.
    const backendPermissionsBoundary =
      createBackendPermissionBoundary(this, stage, artifactBucket);
    const { lambdaExecRole, gatewayExecRole } =
      createBackendRoles(this, stage, artifactBucket, backendPermissionsBoundary);
    // Keep the two legacy Runtime roles for rollback deployments; every target
    // here is stack-owned and passed only to its actual service principal.
    props?.apiExecutionRole?.addToPrincipalPolicy(new iam.PolicyStatement({
      sid: "PassRuntimeAndGatewayExecutionRoles",
      actions: ["iam:PassRole"],
      resources: [
        execRole.roleArn,
        agentExecRole.roleArn,
        gatewayExecRole.roleArn,
      ],
      conditions: {
        StringEquals: {
          "iam:PassedToService": "bedrock-agentcore.amazonaws.com",
        },
      },
    }));
    props?.apiExecutionRole?.addToPrincipalPolicy(new iam.PolicyStatement({
      sid: "PassMcpLambdaExecutionRole",
      actions: ["iam:PassRole"],
      resources: [lambdaExecRole.roleArn],
      conditions: {
        StringEquals: {
          "iam:PassedToService": "lambda.amazonaws.com",
        },
      },
    }));

    // 5) Gateway 리소스 — CUSTOM_JWT authorizer(allowedClients = 위 client id).
    const gateway = createGatewayResource(
      this, stage, gatewayExecRole, this.cognitoDiscoveryUrl, userPoolClient);

    // ── CfnOutputs ────────────────────────────────────────────────────────
    new cdk.CfnOutput(this, "EcrUri", { value: repo.repositoryUri });
    new cdk.CfnOutput(this, "CodeBuildProject", { value: project.projectName });
    new cdk.CfnOutput(this, "DeployJobsTable", { value: jobsTable.tableName });
    // ⚠️ 이름이 가장 일반적인 `ExecRoleArn` 은 **AgentCore Runtime** 롤이에요 —
    // `AGORA_DEPLOY_EXEC_ROLE_ARN` 이 아니에요. 그 env 는 Lambda 실행롤
    // (`LambdaExecRoleArn`) 이고, PassRole grant 가
    // `iam:PassedToService` 로 갈라져 있어서 잘못 매핑하면 MCP(배포형) 배포가
    // `iam:PassRole` AccessDenied 로 죽어요. 의미가 드러나는 별칭을 함께 내보내요.
    new cdk.CfnOutput(this, "ExecRoleArn", {
      value: execRole.roleArn,
      description:
        "AgentCore Runtime execution role (legacy output name). "
        + "Maps to AGORA_DEPLOY_AGENT_EXEC_ROLE_ARN, NOT AGORA_DEPLOY_EXEC_ROLE_ARN.",
    });
    new cdk.CfnOutput(this, "McpRuntimeExecRoleArn", {
      value: execRole.roleArn,
      description:
        "Same value as ExecRoleArn, named for what it is. "
        + "Passed only to bedrock-agentcore.amazonaws.com.",
    });
    new cdk.CfnOutput(this, "ArtifactBucket", { value: artifactBucket.bucketName });
    new cdk.CfnOutput(this, "LambdaExecRoleArn", {
      value: lambdaExecRole.roleArn,
      description:
        "MCP tool-provider Lambda execution role. "
        + "Maps to AGORA_DEPLOY_EXEC_ROLE_ARN.",
    });
    new cdk.CfnOutput(this, "GatewayExecRoleArn", { value: gatewayExecRole.roleArn });
    new cdk.CfnOutput(this, "CognitoUserPoolId", { value: userPool.userPoolId });
    new cdk.CfnOutput(this, "CognitoDiscoveryUrl", { value: this.cognitoDiscoveryUrl });
    new cdk.CfnOutput(this, "CognitoTokenUrl", {
      value: cognitoParts.cognitoTokenUrl,
    });
    // I3: Gateway allowedClients와 매칭되는 앱 client id. MCP 클라이언트가 이 client id로
    // client_credentials 토큰을 발급받아야 Gateway 인바운드 JWT 검증을 통과해요.
    new cdk.CfnOutput(this, "CognitoClientId", { value: this.cognitoClientId });
    // GatewayId/GatewayUrl — CfnGateway attrGatewayIdentifier/attrGatewayUrl로 채워져요.
    // Task 10(실 e2e 게이트)에서 실 배포 후 값을 검증해요.
    new cdk.CfnOutput(this, "GatewayId", { value: gateway.attrGatewayIdentifier });
    new cdk.CfnOutput(this, "GatewayUrl", { value: gateway.attrGatewayUrl });
    // AgentRuntimeExecRoleArn — create_agent_runtime의 roleArn 파라미터로 사용해요.
    // 운영자는 이 ARN 을 `AGORA_DEPLOY_AGENT_EXEC_ROLE_ARN` 에 넣어요.
    // `AGORA_DEPLOY_EXEC_ROLE_ARN` 에 넣으면 안 돼요 — 그건 Lambda 실행롤 자리예요.
    new cdk.CfnOutput(this, "AgentRuntimeExecRoleArn", {
      value: agentExecRole.roleArn,
      description: "Maps to AGORA_DEPLOY_AGENT_EXEC_ROLE_ARN.",
    });
    new cdk.CfnOutput(this, "BuiltinToolExecRoleArn", {
      value: builtinToolExecRole.roleArn,
    });
    new cdk.CfnOutput(this, "BuiltinRecordingBucket", {
      value: builtinRecordingBucket.bucketName,
    });
    new cdk.CfnOutput(this, "PermissionBoundaryArn", {
      value: isolatedAgentPermissionsBoundary.managedPolicyArn,
    });
    new cdk.CfnOutput(this, "AgentRuntimeSharedPolicyArn", {
      value: agentSharedPolicy.managedPolicyArn,
    });
  }
}
