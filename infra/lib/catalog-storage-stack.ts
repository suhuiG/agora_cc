import * as cdk from "aws-cdk-lib";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as iam from "aws-cdk-lib/aws-iam";
import { Construct } from "constructs";

export type Stage = "dev" | "prod";

export interface CatalogStorageStackProps extends cdk.StackProps {
  /** dev = teardown 가능(정리용), prod = 운영 보존. 기본 prod (안전 우선). */
  readonly stage?: Stage;
}

/**
 * Agora source store 스토리지 스택.
 *
 * - S3 artifacts 버킷: 버전 트리 콘텐츠.
 * - DynamoDB AgoraCatalog 테이블: 카탈로그 레코드 + 버전·manifest·경량 감사. on-demand + PITR.
 *   GSI1 by-type / GSI2 by-team / GSI3 by-visibility.
 * - API 실행 역할: 버킷·테이블 접근 권한.
 *
 * stage 분기 (ADR-007):
 * - prod : RemovalPolicy.RETAIN + S3 Object Lock(GOVERNANCE 10년). 운영 보존, 함부로 못 지움.
 * - dev  : RemovalPolicy.DESTROY + autoDeleteObjects + Object Lock off. sandbox 정리 가능.
 *
 * 리전: ap-northeast-2 (bin/agora.ts에서 고정 주입).
 */
export class CatalogStorageStack extends cdk.Stack {
  public readonly artifactsBucket: s3.Bucket;
  public readonly catalogTable: dynamodb.Table;
  public readonly bundleTable: dynamodb.Table;
  public readonly connectionTable: dynamodb.Table;
  public readonly apiExecutionRole: iam.Role;

  constructor(scope: Construct, id: string, props?: CatalogStorageStackProps) {
    super(scope, id, props);

    const stage: Stage = props?.stage ?? "prod";
    const isProd = stage === "prod";
    const removalPolicy = isProd
      ? cdk.RemovalPolicy.RETAIN
      : cdk.RemovalPolicy.DESTROY;

    // 브라우저가 presigned URL로 S3에 직접 업로드/다운로드하므로(web publishSource),
    // 버킷에 CORS가 없으면 preflight가 막혀 "failed to fetch"가 나요.
    // dev는 로컬 개발 서버, prod는 실제 포탈 origin만 허용해요.
    // 호스팅 포털(ADR-0030, CloudFront)로 접속하면 그 origin도 있어야 소스 업로드가 돼요.
    // `AGORA_PORTAL_ORIGIN`(배포 시 주입, 예: https://xxxx.cloudfront.net)이 있으면 추가해요.
    const portalOrigin = process.env.AGORA_PORTAL_ORIGIN?.trim();
    const allowedOrigins = [
      ...(isProd
        ? ["https://agora.example.com"] // TODO: prod 포탈 도메인 확정 시 교체
        : ["http://localhost:3000", "http://localhost:3001"]),
      ...(portalOrigin ? [portalOrigin] : []),
    ];

    // ── S3 artifacts 버킷 ──────────────────────────────────────────
    // prod만 Object Lock(불변 보존). dev는 off — 안 그러면 버킷을 영영 못 지워요.
    this.artifactsBucket = new s3.Bucket(this, "ArtifactsBucket", {
      versioned: true,
      objectLockEnabled: isProd,
      objectLockDefaultRetention: isProd
        ? s3.ObjectLockRetention.governance(cdk.Duration.days(3650))
        : undefined,
      encryption: s3.BucketEncryption.S3_MANAGED,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      enforceSSL: true,
      cors: [
        {
          allowedOrigins,
          allowedMethods: [
            s3.HttpMethods.GET,
            s3.HttpMethods.PUT,
            s3.HttpMethods.HEAD,
          ],
          allowedHeaders: ["*"],
          exposedHeaders: ["ETag"],
          maxAge: 3000,
        },
      ],
      lifecycleRules: [{ noncurrentVersionExpiration: cdk.Duration.days(90) }],
      removalPolicy,
      // dev: 스택 삭제 시 객체까지 자동 삭제(teardown 가능). prod: 미설정(보존).
      autoDeleteObjects: !isProd,
    });

    // ── DynamoDB AgoraCatalog 테이블 ───────────────────────────────
    this.catalogTable = new dynamodb.Table(this, "CatalogTable", {
      tableName: "AgoraCatalog",
      partitionKey: { name: "PK", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "SK", type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      removalPolicy,
    });

    this.catalogTable.addGlobalSecondaryIndex({
      indexName: "GSI1-by-type",
      partitionKey: { name: "GSI1PK", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "GSI1SK", type: dynamodb.AttributeType.STRING },
    });
    this.catalogTable.addGlobalSecondaryIndex({
      indexName: "GSI2-by-team",
      partitionKey: { name: "GSI2PK", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "GSI2SK", type: dynamodb.AttributeType.STRING },
    });
    this.catalogTable.addGlobalSecondaryIndex({
      indexName: "GSI3-by-visibility",
      partitionKey: { name: "GSI3PK", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "GSI3SK", type: dynamodb.AttributeType.STRING },
    });

    // ── DynamoDB AgoraBundle 테이블 (HP-02) ─────────────────────────
    // 플러그인(bundle) 스토어. 로컬 JSON은 Fargate 다중 인스턴스·재시작에서 휘발하므로
    // 공유 DynamoBundleStore로 영속화해요(단일 파티션 PK=BUNDLE / SK=bundle_id).
    this.bundleTable = new dynamodb.Table(this, "BundleTable", {
      tableName: `AgoraBundle-${stage}`,
      partitionKey: { name: "PK", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "SK", type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      // authored 데이터(재구성 불가)라 CatalogTable과 동일하게 PITR로 오손·덮어쓰기 복구를 보장해요.
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      removalPolicy,
    });

    // ── DynamoDB AgoraConnection 테이블 (HP-03) ─────────────────────
    // repo 연결(connection) 스토어. 조직·환경당 단일 레코드(PK=CONNECTION / SK=SINGLETON).
    this.connectionTable = new dynamodb.Table(this, "ConnectionTable", {
      tableName: `AgoraConnection-${stage}`,
      partitionKey: { name: "PK", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "SK", type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      // authored 데이터(재구성 불가)라 CatalogTable과 동일하게 PITR로 오손·덮어쓰기 복구를 보장해요.
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      removalPolicy,
    });

    // ── API 실행 역할 ──────────────────────────────────────────────
    // IAM description은 ASCII만 허용해요(한글 금지) — 영문으로.
    // ADR-0030: 포털 백엔드를 ECS Fargate로 호스팅할 때 이 역할을 task role로 재사용해요
    // (백엔드가 필요로 하는 권한이 이미 이 역할에 부착됨). Lambda(기존)와 ECS tasks 둘 다
    // assume하도록 신뢰정책을 넓혀요(추가·가역).
    this.apiExecutionRole = new iam.Role(this, "ApiExecutionRole", {
      assumedBy: new iam.CompositePrincipal(
        new iam.ServicePrincipal("lambda.amazonaws.com"),
        new iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
      ),
      description: "Agora API: source store S3 + DynamoDB access",
      managedPolicies: [
        iam.ManagedPolicy.fromAwsManagedPolicyName(
          "service-role/AWSLambdaBasicExecutionRole",
        ),
      ],
    });
    // TODO: 설계 §6는 prefix-scoped S3 접근 + 별도 presign 서명 역할(타이트 분리)을 명시해요.
    // MVP는 단일 apiRole에 bucket-wide read/write로 단순화 — 프로덕션 전 least-privilege 강화 필요 (backlog).
    this.artifactsBucket.grantReadWrite(this.apiExecutionRole);
    this.catalogTable.grantReadWriteData(this.apiExecutionRole);
    // HP-02·HP-03: 백엔드(포털 task role = 이 역할)가 bundle·connection 스토어를 읽고 써요.
    // 아래 AgoraPortalDynamo statement(dynamodb:* on table/*)가 이미 덮지만, catalogTable과
    // 같은 패턴으로 명시 grant도 둬 의도(이 테이블 read/write)를 드러내요.
    this.bundleTable.grantReadWriteData(this.apiExecutionRole);
    this.connectionTable.grantReadWriteData(this.apiExecutionRole);
    this.apiExecutionRole.addToPolicy(new iam.PolicyStatement({
      actions: ["bedrock-agentcore:GetRegistryRecord"],
      resources: [
        `arn:${cdk.Aws.PARTITION}:bedrock-agentcore:us-east-1:` +
          `${cdk.Aws.ACCOUNT_ID}:registry/*`,
      ],
    }));
    // CA-05 컷오버는 2026-08-15 완료됐고 포털은 agent-registry namespace로 읽고 써요.
    // AWS Service Authorization Reference에는 CreateRegistry/ListRegistries의 resource
    // type이 없으므로 이 두 계정 수준 action만 Resource "*"가 필요해요.
    this.apiExecutionRole.addToPolicy(new iam.PolicyStatement({
      sid: "AgoraPortalAgentRegistryAccount",
      actions: [
        "agent-registry:CreateRegistry",
        "agent-registry:ListRegistries",
      ],
      resources: ["*"],
    }));
    // registry/*는 공식 registry ARN과 그 하위 registry-record ARN
    // (...:registry/{RegistryId}/record/{RecordId})을 모두 포괄해요.
    this.apiExecutionRole.addToPolicy(new iam.PolicyStatement({
      sid: "AgoraPortalAgentRegistryRecords",
      actions: [
        "agent-registry:CreateRegistryRecord",
        "agent-registry:DeleteRegistryRecord",
        "agent-registry:GetRegistryRecord",
        "agent-registry:ListRegistryRecords",
        "agent-registry:SearchDiscoverableRegistryRecords",
        "agent-registry:SubmitRegistryRecordForApproval",
        "agent-registry:UpdateRegistryRecord",
        "agent-registry:UpdateRegistryRecordStatus",
      ],
      resources: [
        `arn:${cdk.Aws.PARTITION}:agent-registry:us-east-1:` +
          `${cdk.Aws.ACCOUNT_ID}:registry/*`,
      ],
    }));

    // ADR-0030(HP-01): 포털을 ECS Fargate로 호스팅하면 백엔드 앱 전체가 이 role로 돌아요.
    // 로컬은 dev IAM 유저(광범위)라 안 드러났지만, Fargate에선 registry list·deploy jobs
    // scan·소스 S3·스캔 SFN·빌드 등 앱이 쓰는 액션이 필요해요. 데모 범위의 넓은 앱
    // 접근을 부여해요(least-privilege 재설계는 후속). 위 read-only registry statement는 authorizer
    // Lambda 등 다른 소비자를 위해 그대로 둬요.
    this.apiExecutionRole.addToPolicy(new iam.PolicyStatement({
      sid: "AgoraPortalRegistryAndAgentCore",
      actions: ["bedrock-agentcore:*"],
      resources: ["*"],
    }));
    // IA-76 / ADR-0101 단계 0: 임의 userId/JWT로 사람의 provider token을 읽는 API와
    // API-key 읽기는 계속 전면 Deny해요. 이 role은 Portal API와 runtime-authorizer Lambda가
    // 공유하므로, IA-93 preflight용 M2M 두 action은 두 주체 모두 아래 NotResource Deny로
    // agora-agent-* identity 안에만 가둬요.
    this.apiExecutionRole.addToPolicy(new iam.PolicyStatement({
      sid: "AgoraPortalDenyUserScopedTokenVault",
      effect: iam.Effect.DENY,
      actions: [
        "bedrock-agentcore:GetWorkloadAccessTokenForUserId",
        "bedrock-agentcore:GetWorkloadAccessTokenForJWT",
        "bedrock-agentcore:GetResourceApiKey",
      ],
      resources: ["*"],
    }));
    this.apiExecutionRole.addToPolicy(new iam.PolicyStatement({
      sid: "AgoraPortalDenyNonAgentIdentityTokens",
      effect: iam.Effect.DENY,
      actions: [
        "bedrock-agentcore:GetWorkloadAccessToken",
        "bedrock-agentcore:GetResourceOauth2Token",
      ],
      notResources: [
        `arn:${this.partition}:bedrock-agentcore:${this.region}:${this.account}:` +
          "token-vault/default",
        `arn:${this.partition}:bedrock-agentcore:${this.region}:${this.account}:` +
          "token-vault/default/oauth2credentialprovider/agora-agent-*",
        `arn:${this.partition}:bedrock-agentcore:${this.region}:${this.account}:` +
          "workload-identity-directory/default",
        `arn:${this.partition}:bedrock-agentcore:${this.region}:${this.account}:` +
          "workload-identity-directory/default/workload-identity/agora-agent-*",
      ],
    }));
    // IA-57 / ADR-0081 M6: 위 `bedrock-agentcore:*` 는 Gateway control-plane까지 열어줘요.
    // 2026-08-29 simulate-principal-policy 실측으로 이 role의 UpdateGateway·UpdatePolicyEngine·
    // DeletePolicyEngine이 전부 allowed였어요. 강제(ENFORCE) 모드를 받는 주체가 그 모드를 끌 수
    // 있으면 그 인가는 자기 자신이 보증하는 셈이라(ADR-0037 §4) 명시 Deny로 권한을 분리해요.
    // Gateway·PolicyEngine의 생성·수정·삭제는 CDK/CloudFormation 소유예요
    // (infra/bin/agora.ts → M2OAuthGatewayStack의 CfnGateway.policyEngineConfiguration.mode).
    // 포털 코드에 gateway 수준 쓰기 호출은 0건이고(aws_adapter의 get_gateway 읽기뿐) Target·Policy·
    // TagResource는 Deny 대상이 아니라, 이 Deny는 동작 중립이에요. 와일드카드를 쓰지 않는 이유도
    // 같아요 — `*PolicyEngine` 같은 패턴은 GetPolicyEngine 읽기까지 막아 과도 차단이 돼요.
    this.apiExecutionRole.addToPolicy(new iam.PolicyStatement({
      sid: "AgoraPortalDenyGatewayControlPlane",
      effect: iam.Effect.DENY,
      actions: [
        "bedrock-agentcore:CreateGateway",
        "bedrock-agentcore:UpdateGateway",
        "bedrock-agentcore:DeleteGateway",
        "bedrock-agentcore:UpdatePolicyEngine",
        "bedrock-agentcore:DeletePolicyEngine",
      ],
      resources: ["*"],
    }));
    // 백엔드가 bedrock-runtime.invoke_model를 직접 호출해요: Initializr 프롬프트 생성
    // (playground/prompt_service), 위협리포트.md(governance/report_service), compute 분류·MCP
    // 민감도 제안. 로컬 dev 유저(admin)엔 있지만 스코프 role엔 없어 배포서버에서 이 기능들이
    // AccessDenied(위협리포트 실패·프롬프트는 템플릿 폴백)로 깨졌어요.
    this.apiExecutionRole.addToPolicy(new iam.PolicyStatement({
      sid: "AgoraPortalBedrockInvoke",
      actions: [
        "bedrock:InvokeModel",
        "bedrock:InvokeModelWithResponseStream",
        "bedrock:Converse",
        "bedrock:ConverseStream",
      ],
      resources: ["*"],
    }));
    // 전수조사(boto3 client 대조): 백엔드가 lambda(배포형 Lambda 생성/수정/삭제·invoke),
    // ecs(fargate 스캐너·배포 probe), secretsmanager(PAT 자격증명 prod)도 써요. 로컬 admin엔
    // 있지만 스코프 role엔 없어 해당 경로가 배포서버에서 AccessDenied가 나요. 데모 범위로 부여.
    this.apiExecutionRole.addToPolicy(new iam.PolicyStatement({
      sid: "AgoraPortalLambdaEcsSecrets",
      actions: [
        "lambda:InvokeFunction",
        "lambda:InvokeFunctionUrl",
        "lambda:CreateFunction",
        "lambda:GetFunction",
        "lambda:UpdateFunctionCode",
        "lambda:UpdateFunctionConfiguration",
        "lambda:DeleteFunction",
        "lambda:AddPermission",
        "lambda:RemovePermission",
        "lambda:CreateFunctionUrlConfig",
        "lambda:GetFunctionUrlConfig",
        "ecs:RunTask",
        "ecs:StopTask",
        "ecs:DescribeTasks",
        "ecs:ListTasks",
        "secretsmanager:CreateSecret",
        "secretsmanager:GetSecretValue",
        "secretsmanager:PutSecretValue",
        "secretsmanager:UpdateSecret",
        "secretsmanager:DeleteSecret",
        "secretsmanager:DescribeSecret",
        "secretsmanager:TagResource",
      ],
      resources: ["*"],
    }));
    this.apiExecutionRole.addToPolicy(new iam.PolicyStatement({
      sid: "AgoraPortalDynamo",
      actions: ["dynamodb:*"],
      resources: [
        `arn:${cdk.Aws.PARTITION}:dynamodb:*:${cdk.Aws.ACCOUNT_ID}:table/*`,
      ],
    }));
    this.apiExecutionRole.addToPolicy(new iam.PolicyStatement({
      sid: "AgoraPortalS3",
      actions: ["s3:*"],
      resources: [`arn:${cdk.Aws.PARTITION}:s3:::*`],
    }));
    this.apiExecutionRole.addToPolicy(new iam.PolicyStatement({
      sid: "AgoraPortalBuildScanEcrLogsAuth",
      actions: [
        "states:StartExecution",
        "states:StopExecution",
        "states:DescribeExecution",
        "states:GetExecutionHistory",
        "codebuild:StartBuild",
        "codebuild:BatchGetBuilds",
        "ecr:GetAuthorizationToken",
        "ecr:BatchCheckLayerAvailability",
        "ecr:GetDownloadUrlForLayer",
        "ecr:BatchGetImage",
        "ecr:PutImage",
        "ecr:InitiateLayerUpload",
        "ecr:UploadLayerPart",
        "ecr:CompleteLayerUpload",
        "ecr:DescribeImages",
        "ecr:DescribeRepositories",
        "logs:CreateLogGroup",
        "logs:CreateLogStream",
        "logs:PutLogEvents",
        "logs:DescribeLogStreams",
        "logs:GetLogEvents",
        "logs:FilterLogEvents",
        "cognito-idp:*",
        "kms:Decrypt",
        "kms:GenerateDataKey",
        "kms:DescribeKey",
      ],
      resources: ["*"],
    }));
    if (!isProd) {
      const demoExternalId = `agora-sts-broker-demo-${stage}`;
      const demoTable = new dynamodb.Table(this, "StsBrokerDemoTable", {
        tableName: `agora-sts-broker-demo-${stage}`,
        partitionKey: {
          name: "id",
          type: dynamodb.AttributeType.STRING,
        },
        billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
        pointInTimeRecoverySpecification: {
          pointInTimeRecoveryEnabled: true,
        },
        removalPolicy: cdk.RemovalPolicy.DESTROY,
      });
      const externalIdCondition = {
        StringEquals: {
          "sts:ExternalId": demoExternalId,
        },
      };
      const demoRole = new iam.Role(
        this,
        "StsBrokerDemoDelegatedRole",
        {
          assumedBy: new iam.AccountPrincipal(cdk.Aws.ACCOUNT_ID)
            .withConditions(externalIdCondition),
          description:
            "Dev-only customer delegated role for Agora STS broker validation",
          maxSessionDuration: cdk.Duration.hours(1),
        },
      );
      // broker가 감사 추적용 SourceIdentity(Cognito sub)를 심으려면 위임 role trust가
      // sts:AssumeRole과 sts:SetSourceIdentity를 함께 허용해야 해요. assumedBy 자동
      // statement는 AssumeRole만 만들므로, 실행역할(apiExecutionRole)엔 두 action을,
      // AccountPrincipal(로컬 검증 자격)엔 SetSourceIdentity를 명시로 보완해요.
      // 실제 고객 위임 role 온보딩에도 동일한 두 action이 필요해요.
      demoRole.assumeRolePolicy?.addStatements(
        new iam.PolicyStatement({
          actions: ["sts:AssumeRole", "sts:SetSourceIdentity"],
          principals: [new iam.ArnPrincipal(this.apiExecutionRole.roleArn)],
          conditions: externalIdCondition,
        }),
        new iam.PolicyStatement({
          actions: ["sts:SetSourceIdentity"],
          principals: [new iam.AccountPrincipal(cdk.Aws.ACCOUNT_ID)],
          conditions: externalIdCondition,
        }),
      );
      demoRole.addToPolicy(new iam.PolicyStatement({
        actions: ["dynamodb:GetItem"],
        resources: [demoTable.tableArn],
      }));
      // broker는 위임 role을 assume하면서 SourceIdentity(Cognito sub)를 심어요. STS는
      // caller의 identity policy에도 sts:SetSourceIdentity를 요구하므로 실행역할에 함께 부여해요
      // (role trust만으로는 부족 — 양쪽 다 있어야 SourceIdentity 지정이 통과해요).
      this.apiExecutionRole.addToPolicy(new iam.PolicyStatement({
        actions: ["sts:AssumeRole", "sts:SetSourceIdentity"],
        resources: [demoRole.roleArn],
      }));

      new cdk.CfnOutput(this, "StsBrokerDemoRoleArn", {
        value: demoRole.roleArn,
      });
      new cdk.CfnOutput(this, "StsBrokerDemoTableName", {
        value: demoTable.tableName,
      });
      new cdk.CfnOutput(this, "StsBrokerDemoExternalId", {
        value: demoExternalId,
      });
    }

    // ── 출력 ───────────────────────────────────────────────────────
    new cdk.CfnOutput(this, "Stage", { value: stage });
    new cdk.CfnOutput(this, "ArtifactsBucketName", {
      value: this.artifactsBucket.bucketName,
    });
    new cdk.CfnOutput(this, "CatalogTableName", {
      value: this.catalogTable.tableName,
    });
    new cdk.CfnOutput(this, "BundleTableName", {
      value: this.bundleTable.tableName,
    });
    new cdk.CfnOutput(this, "ConnectionTableName", {
      value: this.connectionTable.tableName,
    });
  }
}
