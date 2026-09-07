import * as cdk from "aws-cdk-lib";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as ecr from "aws-cdk-lib/aws-ecr";
import * as ecs from "aws-cdk-lib/aws-ecs";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as iam from "aws-cdk-lib/aws-iam";
import { Construct } from "constructs";

export type Stage = "dev" | "prod";

export interface GovernanceScanStackProps extends cdk.StackProps {
  readonly stage?: Stage;
}

/**
 * AgoraGovernanceScan — L2 격리 스캔 실행 인프라 (명세 §1.3).
 *
 * scan-runner 경로만: 격리 VPC(PRIVATE_ISOLATED, IGW/NAT 없음) + S3 Gateway EP(무료)
 * + ECR/Logs Interface EP + ECR repo + ECS Cluster/TaskDef(2vCPU/4GB) + 최소 Task Role
 * + scan-I/O S3 버킷. Firewall·NAT·fetcher·Cognito는 후속 phase.
 *
 * 보안 성립: scan 서브넷에 인터넷 경로(IGW/NAT)가 물리적으로 없어 스캔 대상 코드가
 * 악성이어도 exfiltration 불가. AWS 서비스 평면은 VPC endpoint로만.
 */
export class GovernanceScanStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props?: GovernanceScanStackProps) {
    super(scope, id, props);

    const stage: Stage = props?.stage ?? "prod";
    const isProd = stage === "prod";
    const removalPolicy = isProd ? cdk.RemovalPolicy.RETAIN : cdk.RemovalPolicy.DESTROY;

    // ── 격리 VPC (IGW/NAT 없음 = air-gap 토폴로지) ──────────────────
    const vpc = new ec2.Vpc(this, "ScanVpc", {
      maxAzs: 2,
      natGateways: 0, // NAT 없음
      subnetConfiguration: [
        { name: "isolated", subnetType: ec2.SubnetType.PRIVATE_ISOLATED, cidrMask: 24 },
      ],
      // subnetConfiguration에 PUBLIC이 없으면 CDK는 IGW를 만들지 않아요.
    });

    // ── VPC Endpoints ──────────────────────────────────────────────
    // S3 Gateway(무료) — 소스·결과 I/O.
    vpc.addGatewayEndpoint("S3Gw", { service: ec2.GatewayVpcEndpointAwsService.S3 });
    // ECR(api·dkr)·Logs Interface — 이미지 pull·로그 전송에 필수.
    vpc.addInterfaceEndpoint("EcrApi", { service: ec2.InterfaceVpcEndpointAwsService.ECR });
    vpc.addInterfaceEndpoint("EcrDkr", { service: ec2.InterfaceVpcEndpointAwsService.ECR_DOCKER });
    vpc.addInterfaceEndpoint("Logs", { service: ec2.InterfaceVpcEndpointAwsService.CLOUDWATCH_LOGS });

    // ── ECR repo (scan-runner 이미지) ──────────────────────────────
    const repo = new ecr.Repository(this, "ScanRunnerRepo", {
      repositoryName: `agora-scan-runner-${stage}`,
      removalPolicy,
      emptyOnDelete: !isProd,
    });

    // ── scan-I/O S3 버킷 ───────────────────────────────────────────
    const bucket = new s3.Bucket(this, "ScanIoBucket", {
      encryption: s3.BucketEncryption.S3_MANAGED,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      enforceSSL: true,
      lifecycleRules: [{ expiration: cdk.Duration.days(7) }], // 스캔 I/O는 임시물
      removalPolicy,
      autoDeleteObjects: !isProd,
    });

    // ── ECS Cluster + Fargate TaskDef ──────────────────────────────
    const cluster = new ecs.Cluster(this, "ScanCluster", { vpc });
    const taskDef = new ecs.FargateTaskDefinition(this, "ScanTaskDef", {
      cpu: 2048, // 2 vCPU
      memoryLimitMiB: 4096, // 4 GB
    });
    // 최소권한 Task Role: scan-I/O 버킷 read/write만. (Logs는 execution role.)
    taskDef.taskRole.addToPrincipalPolicy(new iam.PolicyStatement({
      actions: ["s3:GetObject", "s3:PutObject", "s3:ListBucket"],
      resources: [bucket.bucketArn, `${bucket.bucketArn}/*`],
    }));
    taskDef.addContainer("scan-runner", {
      containerName: "scan-runner",
      image: ecs.ContainerImage.fromEcrRepository(repo, "latest"),
      logging: ecs.LogDrivers.awsLogs({
        streamPrefix: "scan-runner",
      }),
    });
    // 이 스택은 현재 계정에 배포되지 않았고 포털은 AGORA_SCANNER=stepfn을 사용해요.
    // 향후 fargate 직접 경로를 켤 때는 이 미배포 스택을 참조하지 말고, 실제 배포된
    // AgoraGovernanceScanTools-<stage> task/execution role ARN에만 iam:PassRole을 부여해요.
    // 조건은 StringEquals iam:PassedToService=ecs-tasks.amazonaws.com 이어야 해요.

    // ── scan task 전용 보안그룹 (egress 필요 — VPC endpoint 통신용) ──
    const sg = new ec2.SecurityGroup(this, "ScanTaskSg", { vpc, allowAllOutbound: true });

    // ── 출력 (어댑터 env로 사용) ───────────────────────────────────
    const isolatedSubnetIds = vpc.selectSubnets({
      subnetType: ec2.SubnetType.PRIVATE_ISOLATED,
    }).subnetIds.join(",");
    new cdk.CfnOutput(this, "ClusterName", { value: cluster.clusterName });
    new cdk.CfnOutput(this, "TaskDefArn", { value: taskDef.taskDefinitionArn });
    new cdk.CfnOutput(this, "IsolatedSubnetIds", { value: isolatedSubnetIds });
    new cdk.CfnOutput(this, "ScanSecurityGroupId", { value: sg.securityGroupId });
    new cdk.CfnOutput(this, "ScanBucketName", { value: bucket.bucketName });
    new cdk.CfnOutput(this, "EcrRepoUri", { value: repo.repositoryUri });
  }
}
