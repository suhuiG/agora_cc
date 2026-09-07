import * as path from "path";
import * as cdk from "aws-cdk-lib";
import { Construct } from "constructs";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as ecs from "aws-cdk-lib/aws-ecs";
import * as elbv2 from "aws-cdk-lib/aws-elasticloadbalancingv2";
import * as iam from "aws-cdk-lib/aws-iam";
import * as ecrAssets from "aws-cdk-lib/aws-ecr-assets";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as logs from "aws-cdk-lib/aws-logs";
import * as cloudfront from "aws-cdk-lib/aws-cloudfront";
import * as origins from "aws-cdk-lib/aws-cloudfront-origins";
import { Stage } from "./catalog-storage-stack";
import { pickPortalBackendEnv } from "./portal-env";

/**
 * Agora 포털 호스팅 스택 (ADR-0030).
 *
 * 포털(웹 SSR + FastAPI 백엔드)을 서울(ap-northeast-2) ECS Fargate 컨테이너 2개로 띄우고,
 * 웹 앞에 ALB + CloudFront를 둬서 CloudFront URL로 접속하게 해요. App Runner가 서울에 없어
 * Fargate를 써요(전부 in-region). 흐름: 브라우저 → CloudFront → ALB → 웹(Next.js SSR) →
 * BFF proxy → 백엔드(FastAPI, Cloud Map 내부 DNS) → 실 AWS.
 *
 * - Graviton(ARM64) 태스크 + ARM64 이미지 → arm64 개발기에서 네이티브 빌드(QEMU 불필요).
 * - VPC는 퍼블릭 서브넷만(NAT 비용 회피). 태스크는 퍼블릭 IP로 AWS API/인터넷에 붙고, 인바운드는
 *   SG로 ALB(웹)·웹(백엔드)만 허용.
 * - 백엔드 task role = 공유 apiExecutionRole(신뢰정책에 ecs-tasks 추가), 웹은 세션 DynamoDB
 *   쓰기 전용 role.
 * - DockerImageAsset 빌드가 무거워 `bin/agora.ts`에서 `-c portal=true`일 때만 인스턴스화해요.
 */
export interface PortalStackProps extends cdk.StackProps {
  readonly stage: Stage;
  /** 백엔드 task role(공유 apiExecutionRole; 신뢰정책에 ecs-tasks.amazonaws.com 추가됨). */
  readonly backendTaskRole: iam.IRole;
  /** 웹 SSR이 쓰는 세션 테이블 이름(이 스택과 같은 리전 = 서울). */
  readonly sessionTableName: string;
  /** RuntimeDeployStack-owned execution role used by CUSTOM builtin resources. */
  readonly builtinExecutionRoleArn: string;
  /** RuntimeDeployStack-owned Browser recording bucket. */
  readonly builtinRecordingBucket: string;
  readonly agentSharedPolicyArn: string;
  readonly agentPermissionsBoundaryArn: string;
  /** [E] Actual M2 OAuth Gateway policy mode selected by the CDK app context. */
  readonly m2OAuthGatewayMode: "LOG_ONLY" | "ENFORCE";
  /** [E] This stage owns the account/region shared aws/spans subscription. */
  readonly monitoringIngestOwner: boolean;
  /** [E] Request Sampled=1 for supported Runtime invocation paths. */
  readonly forceTraceSampling: boolean;
  /** 백엔드 SHARED 좌표. PER-ENV 운영값은 이 스택이 결정적으로 주입해요. */
  readonly backendEnv: Record<string, string>;
  /** 웹 런타임 env. AGORA_API_URL은 백엔드 내부 DNS로 스택이 덮어써요. */
  readonly webEnv: Record<string, string>;
}

const NAMESPACE = "agora.portal";
const BACKEND_DNS = `backend.${NAMESPACE}`;
const BACKEND_PORT = 9100;
const WEB_PORT = 3000;

export class PortalStack extends cdk.Stack {
  public readonly webBaseUrl: string;

  constructor(scope: Construct, id: string, props: PortalStackProps) {
    super(scope, id, props);
    if (
      !props.agentSharedPolicyArn
      || !props.agentPermissionsBoundaryArn
    ) {
      throw new Error(
        "per-agent roles require agentSharedPolicyArn and "
        + "agentPermissionsBoundaryArn",
      );
    }

    const arm = {
      cpuArchitecture: ecs.CpuArchitecture.ARM64,
      operatingSystemFamily: ecs.OperatingSystemFamily.LINUX,
    };
    const platform = ecrAssets.Platform.LINUX_ARM64;

    const vpc = new ec2.Vpc(this, "PortalVpc", {
      maxAzs: 2,
      natGateways: 0,
      subnetConfiguration: [
        { name: "public", subnetType: ec2.SubnetType.PUBLIC },
      ],
    });

    const cluster = new ecs.Cluster(this, "PortalCluster", { vpc });
    cluster.addDefaultCloudMapNamespace({ name: NAMESPACE });

    const logGroup = (lid: string) =>
      new logs.LogGroup(this, lid, {
        retention: logs.RetentionDays.ONE_WEEK,
        removalPolicy: cdk.RemovalPolicy.DESTROY,
      });

    // ── 백엔드 (FastAPI) — Cloud Map 내부 DNS, ALB 없음 ──────────────────
    const backendImage = new ecrAssets.DockerImageAsset(this, "BackendImage", {
      directory: path.join(__dirname, "..", "..", "api"),
      platform,
    });
    const backendTaskDef = new ecs.FargateTaskDefinition(this, "BackendTask", {
      cpu: 1024,
      memoryLimitMiB: 2048,
      runtimePlatform: arm,
      taskRole: props.backendTaskRole,
    });
    const backendContainer = backendTaskDef.addContainer("backend", {
      image: ecs.ContainerImage.fromDockerImageAsset(backendImage),
      environment: {
        ...pickPortalBackendEnv(props.backendEnv),
        // HP-08: 개발자 .env가 포털 운영 정책을 바꾸지 못하게 스택이 소유해요.
        AGORA_ROLE: "portal",
        AGORA_POLLER_ENABLED: "1",
        // LC-03: MCP 도구 목록 드리프트 관측. 외부(우리 통제 밖) MCP endpoint 로 나가는
        // 트래픽이라 소유 프로세스를 스택이 못박아요 — 개발자 셸이 켜고 끄지 못하게요.
        AGORA_MCP_DRIFT_POLL_ENABLED: "1",
        AGORA_MCP_DRIFT_POLL_INTERVAL: "900",
        AGORA_AUTH_MODE: "cognito",
        AGORA_AUTHORIZATION_MODE: "agent_policy",
        AGORA_M2_OAUTH_GATEWAY_MODE: props.m2OAuthGatewayMode,
        AGORA_MONITORING_MAX_INGEST_LAG_SECONDS: "900",
        AGORA_MONITORING_TRAFFIC_BOUNDARY_MARGIN_SECONDS: "60",
        AGORA_MONITORING_INGEST_OWNER:
          props.monitoringIngestOwner ? "1" : "0",
        AGORA_RUNTIME_FAIL_CLOSED_ON_UNKNOWN_AUTHORIZATION: "0",
        AGORA_RUNTIME_FAIL_CLOSED_ON_UNKNOWN_BUILTIN_TOOLS: "0",
        AGORA_RUNTIME_FAIL_CLOSED_ON_IDENTITY_OUTBOUND: "1",
        AGORA_RUNTIME_PER_AGENT_ROLES_ENABLED: "1",
        AGORA_RUNTIME_FORCE_TRACE_SAMPLING_ENABLED:
          props.forceTraceSampling ? "1" : "0",
        AGORA_SCANNER: "stepfn",
        // 호스팅 백엔드는 공유 DynamoGovStore(스캔 파이프라인 aggregate Lambda가 쓰는 AgoraGov-{stage})를
        // 봐야 해요. 안 그러면 stage=prod라도 gov_table 없이 컨테이너 로컬 JSON store로 떨어져
        // auto_scan=False(기본)로 자동스캔이 안 돌고, 스캔 결과도 안 보여요(로컬 JSON≠aggregate가 쓴 DDB).
        // 테이블명은 scan-tools-stack의 `AgoraGov-${stage}`로 결정적이에요.
        AGORA_GOV_TABLE: `AgoraGov-${props.stage}`,
        // HP-02·HP-03·HP-04: bundle·connection 스토어도 공유 DynamoDB로. 이 env가 없으면
        // 백엔드가 컨테이너 로컬 JSON으로 떨어져 Fargate 재시작·다중 인스턴스에서 휘발해요.
        // 테이블명은 catalog-storage-stack의 `AgoraBundle-${stage}`·`AgoraConnection-${stage}`로 결정적.
        AGORA_BUNDLE_TABLE: `AgoraBundle-${props.stage}`,
        AGORA_CONNECTION_TABLE: `AgoraConnection-${props.stage}`,
        AGORA_DEPLOY_BUILTIN_EXEC_ROLE_ARN: props.builtinExecutionRoleArn,
        AGORA_DEPLOY_BUILTIN_RECORDING_BUCKET: props.builtinRecordingBucket,
        AGORA_DEPLOY_AGENT_SHARED_POLICY_ARN:
          props.agentSharedPolicyArn,
        AGORA_DEPLOY_AGENT_PERMISSIONS_BOUNDARY_ARN:
          props.agentPermissionsBoundaryArn,
        AGORA_DEV_IDENTITY_CREDENTIAL_TTL_DAYS:
          props.backendEnv.AGORA_DEV_IDENTITY_CREDENTIAL_TTL_DAYS ?? "7",
        AGORA_DEV_IDENTITY_CREDENTIAL_TTL_MAX_DAYS:
          props.backendEnv.AGORA_DEV_IDENTITY_CREDENTIAL_TTL_MAX_DAYS ?? "30",
        AGORA_DEV_IDENTITY_TOKEN_TTL_MINUTES:
          props.backendEnv.AGORA_DEV_IDENTITY_TOKEN_TTL_MINUTES ?? "60",
      },
      portMappings: [{ containerPort: BACKEND_PORT }],
      logging: ecs.LogDrivers.awsLogs({
        streamPrefix: "backend",
        logGroup: logGroup("BackendLogs"),
      }),
    });
    const backendSg = new ec2.SecurityGroup(this, "BackendSg", {
      vpc,
      description: "Agora portal backend (Fargate)",
    });
    const backendService = new ecs.FargateService(this, "BackendService", {
      cluster,
      taskDefinition: backendTaskDef,
      desiredCount: 1,
      assignPublicIp: true, // NAT 없이 AWS API/인터넷 접근
      vpcSubnets: { subnetType: ec2.SubnetType.PUBLIC },
      securityGroups: [backendSg],
      cloudMapOptions: { name: "backend" }, // → backend.agora.portal
    });

    // ── 웹 (Next.js SSR + BFF) — ALB 뒤 ─────────────────────────────────
    const webImage = new ecrAssets.DockerImageAsset(this, "WebImage", {
      directory: path.join(__dirname, "..", "..", "web"),
      platform,
    });
    // 세션 DynamoDB 쓰기 전용 task role(자격증명 env 없이 SigV4).
    const webRole = new iam.Role(this, "WebTaskRole", {
      assumedBy: new iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
      description: "Agora web SSR (Fargate) - session DynamoDB write",
    });
    dynamodb.Table.fromTableName(
      this,
      "SessionTable",
      props.sessionTableName,
    ).grantReadWriteData(webRole);

    const webTaskDef = new ecs.FargateTaskDefinition(this, "WebTask", {
      cpu: 512,
      memoryLimitMiB: 1024,
      runtimePlatform: arm,
      taskRole: webRole,
    });
    const portalWebEnv = { ...props.webEnv };
    delete portalWebEnv.AGORA_WEB_BASE_URL;
    const webContainer = webTaskDef.addContainer("web", {
      image: ecs.ContainerImage.fromDockerImageAsset(webImage),
      environment: {
        ...portalWebEnv,
        // BFF가 백엔드로 프록시할 내부 DNS(같은 VPC, Cloud Map).
        AGORA_API_URL: `http://${BACKEND_DNS}:${BACKEND_PORT}`,
      },
      portMappings: [{ containerPort: WEB_PORT }],
      logging: ecs.LogDrivers.awsLogs({
        streamPrefix: "web",
        logGroup: logGroup("WebLogs"),
      }),
    });
    const webSg = new ec2.SecurityGroup(this, "WebSg", {
      vpc,
      description: "Agora portal web (Fargate)",
    });
    const webService = new ecs.FargateService(this, "WebService", {
      cluster,
      taskDefinition: webTaskDef,
      desiredCount: 1,
      assignPublicIp: true,
      vpcSubnets: { subnetType: ec2.SubnetType.PUBLIC },
      securityGroups: [webSg],
    });

    // 웹 → 백엔드(9100) 인바운드만 허용.
    backendSg.addIngressRule(webSg, ec2.Port.tcp(BACKEND_PORT), "web to backend");

    // ── ALB (웹 앞) ─────────────────────────────────────────────────────
    // ── 긴 Playground 턴을 위한 타임아웃 (IH-187) ────────────────────────
    //
    // 배포 agent 한 턴이 도구를 여러 번 부르면 **분 단위**가 나와요. 실측 2026-09-07
    // (`cs-assistant`, VIP 11명 주문 조회): 한 턴이 **93초** 걸렸고 백엔드는 완주했는데
    // (`INVOKE_TIMEOUT_SECONDS = 300`), CloudFront 기본 응답 타임아웃 30초가 먼저 끊어서
    // Playground 에는 **빈 말풍선**만 남고 활동 트리에는 정확히 `30.0s ✕` 세 건이 찍혔어요.
    //
    // 두 값을 함께 올려요. **안쪽(ALB)이 바깥쪽(CloudFront)보다 길어야** 해요 — 반대면
    // ALB 가 먼저 연결을 끊어서 CloudFront 가 502 를 만들고, 타임아웃 판정의 주인이
    // 바뀌면서 화면에 나가는 이유도 달라져요.
    const ORIGIN_RESPONSE_TIMEOUT = cdk.Duration.seconds(120);
    const ALB_IDLE_TIMEOUT = cdk.Duration.seconds(180);

    const alb = new elbv2.ApplicationLoadBalancer(this, "PortalAlb", {
      vpc,
      internetFacing: true,
      idleTimeout: ALB_IDLE_TIMEOUT,
    });
    const listener = alb.addListener("Http", { port: 80, open: true });
    listener.addTargets("WebTarget", {
      port: WEB_PORT,
      protocol: elbv2.ApplicationProtocol.HTTP,
      targets: [webService],
      healthCheck: {
        path: "/",
        healthyHttpCodes: "200-399",
        interval: cdk.Duration.seconds(30),
        timeout: cdk.Duration.seconds(10),
      },
    });

    // ── CloudFront → ALB ────────────────────────────────────────────────
    // ALB는 임의 Host를 받으므로 전 헤더·쿠키·쿼리를 전달(ALL_VIEWER), SSR/인증용. 캐시는 꺼요.
    const distribution = new cloudfront.Distribution(this, "PortalCdn", {
      comment: `Agora portal ${props.stage}`,
      defaultBehavior: {
        origin: new origins.HttpOrigin(alb.loadBalancerDnsName, {
          protocolPolicy: cloudfront.OriginProtocolPolicy.HTTP_ONLY,
          // ⚠️ 이 값은 계정 **quota** 를 소비해요. CloudFront 의 「Response timeout
          // (custom origins)」 기본 상한이 30초라, 상향 승인이 없는 계정에서는 배포가
          // `InvalidArgument` 로 거부돼요. 배포가 그 오류로 실패하면 값을 되돌리기 전에
          // quota 상향을 먼저 확인해요 — 30초로 되돌리면 IH-187 이 그대로 재발해요.
          readTimeout: ORIGIN_RESPONSE_TIMEOUT,
        }),
        viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        allowedMethods: cloudfront.AllowedMethods.ALLOW_ALL,
        cachePolicy: cloudfront.CachePolicy.CACHING_DISABLED,
        originRequestPolicy: cloudfront.OriginRequestPolicy.ALL_VIEWER,
      },
    });
    // CloudFront가 공개 origin의 생성자이므로 API와 Web이 같은 값을 직접 받아요.
    this.webBaseUrl = `https://${distribution.distributionDomainName}`;
    backendContainer.addEnvironment("AGORA_WEB_BASE_URL", this.webBaseUrl);
    webContainer.addEnvironment("AGORA_WEB_BASE_URL", this.webBaseUrl);

    // ── 출력 ────────────────────────────────────────────────────────────
    new cdk.CfnOutput(this, "CloudFrontUrl", {
      value: this.webBaseUrl,
      description: "포털 접속 URL. API, Web, Cognito callback이 이 값에서 파생돼요.",
    });
    new cdk.CfnOutput(this, "AlbDnsName", { value: alb.loadBalancerDnsName });
    new cdk.CfnOutput(this, "BackendInternalUrl", {
      value: `http://${BACKEND_DNS}:${BACKEND_PORT}`,
    });
  }
}
