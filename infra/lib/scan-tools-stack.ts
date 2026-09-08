import * as cdk from "aws-cdk-lib";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as ecr from "aws-cdk-lib/aws-ecr";
import * as ecs from "aws-cdk-lib/aws-ecs";
import * as lambda from "aws-cdk-lib/aws-lambda";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as iam from "aws-cdk-lib/aws-iam";
import * as logs from "aws-cdk-lib/aws-logs";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as sfn from "aws-cdk-lib/aws-stepfunctions";
import * as tasks from "aws-cdk-lib/aws-stepfunctions-tasks";
import * as events from "aws-cdk-lib/aws-events";
import * as targets from "aws-cdk-lib/aws-events-targets";
import * as path from "path";
import { Construct } from "constructs";
import { SCAN_TOOLS } from "./scan-tools-catalog";

export type Stage = "dev" | "prod";
export interface GovernanceScanToolsStackProps extends cdk.StackProps {
  readonly stage?: Stage;
}

const SCAN_TOOLS_CTX = path.join(__dirname, "..", "scan-tools"); // 빌드 컨텍스트

/**
 * GovernanceScanToolsStack — 도구당 이미지 실행 유닛 (SP-3).
 * air-gap VPC(PRIVATE_ISOLATED) + scan-I/O 버킷 + 도구별 ECR + compute별 Lambda/Fargate.
 */
export class GovernanceScanToolsStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props?: GovernanceScanToolsStackProps) {
    super(scope, id, props);
    const stage: Stage = props?.stage ?? "prod";
    const isProd = stage === "prod";
    const removalPolicy = isProd ? cdk.RemovalPolicy.RETAIN : cdk.RemovalPolicy.DESTROY;

    // air-gap VPC
    const vpc = new ec2.Vpc(this, "ScanVpc", {
      maxAzs: 2, natGateways: 0,
      subnetConfiguration: [{ name: "isolated", subnetType: ec2.SubnetType.PRIVATE_ISOLATED, cidrMask: 24 }],
    });
    vpc.addGatewayEndpoint("S3Gw", { service: ec2.GatewayVpcEndpointAwsService.S3 });
    vpc.addInterfaceEndpoint("EcrApi", { service: ec2.InterfaceVpcEndpointAwsService.ECR });
    vpc.addInterfaceEndpoint("EcrDkr", { service: ec2.InterfaceVpcEndpointAwsService.ECR_DOCKER });
    vpc.addInterfaceEndpoint("Logs", { service: ec2.InterfaceVpcEndpointAwsService.CLOUDWATCH_LOGS });
    // llm-judge Lambda는 PRIVATE_ISOLATED 서브넷에서 Bedrock을 호출해요 — NAT 없으므로
    // bedrock-runtime VPC interface endpoint가 없으면 런타임에 연결 실패해요.
    vpc.addInterfaceEndpoint("BedrockRuntime", { service: ec2.InterfaceVpcEndpointAwsService.BEDROCK_RUNTIME });

    // scan-I/O 버킷
    const bucket = new s3.Bucket(this, "ScanIoBucket", {
      encryption: s3.BucketEncryption.S3_MANAGED,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      enforceSSL: true,
      lifecycleRules: [{ expiration: cdk.Duration.days(7) }],
      removalPolicy, autoDeleteObjects: !isProd,
    });

    const sg = new ec2.SecurityGroup(this, "ScanTaskSg", { vpc, allowAllOutbound: true });
    const cluster = new ecs.Cluster(this, "ScanCluster", { vpc });
    const fargateTaskRoleArns: string[] = [];

    // 도구별 실행 유닛
    for (const tool of SCAN_TOOLS) {
      const idBase = tool.toolId.replace(/[^a-zA-Z0-9]/g, "");

      if (tool.compute === "lambda") {
        // lambda 도구는 이미지 에셋(CDK bootstrap 에셋 repo)을 사용하므로
        // per-tool ECR repo를 만들지 않아요 — 만들면 push/참조 안 되는 고아 리소스가 돼요.
        const fn = new lambda.DockerImageFunction(this, `Fn${idBase}`, {
          functionName: `agora-tool-${tool.toolId}-${stage}`,
          // Lambda는 x86_64. 빌드 호스트(arm64 Mac 등)와 무관하게 amd64 이미지를 굽도록
          // platform을 명시 — 안 하면 arm64 이미지가 x86_64 Lambda에 올라가 InvalidEntrypoint.
          architecture: lambda.Architecture.X86_64,
          code: lambda.DockerImageCode.fromImageAsset(SCAN_TOOLS_CTX, {
            file: `${tool.imageDir}/Dockerfile`,
            platform: cdk.aws_ecr_assets.Platform.LINUX_AMD64,
          }),
          memorySize: tool.memoryMiB,
          timeout: cdk.Duration.seconds(tool.timeoutSec),
          vpc, vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_ISOLATED },
          securityGroups: [sg],
          // trivy는 번들 DB를 /opt/trivy-cache에서 읽어요(빌드 시 pre-download).
          // 다른 lambda 도구(gitleaks)엔 무해 — handler가 안 읽어요.
          environment: { TRIVY_CACHE_DIR: "/opt/trivy-cache" },
        });
        bucket.grantReadWrite(fn);
        if (tool.toolId === "llm-judge") {
          // Global inference profile은 계정 리소스(profile)와 대상 foundation model 권한이
          // 모두 필요해요. **두 자리의 조건이 달라요.**
          //
          // profile 은 호출 endpoint 리전에서 평가되니 `aws:RequestedRegion` 을 고정할 수
          // 있어요. 반면 foundation model 인가는 global profile 이 **라우팅한 리전**에서
          // 평가돼요 — 그래서 model 쪽에도 같은 리전 조건을 걸면
          // `bedrock:InvokeModel on arn:aws:bedrock:::foundation-model/…` 이 implicitDeny 로
          // 떨어져요.
          //
          // 리전 고정 모델 ID 로 우회할 수도 없어요. `anthropic.claude-sonnet-4-6` 은
          // ap-northeast-2 에서 `INFERENCE_PROFILE` 만 지원하고, 그 리전에 존재하는 profile
          // 이 `global.…` 하나예요. 그래서 model statement 에는 계정 조건만 걸어요.
          fn.addToRolePolicy(new iam.PolicyStatement({
            sid: "InvokeInferenceProfileInRegion",
            actions: ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
            resources: [
              `arn:${this.partition}:bedrock:${this.region}:${this.account}:inference-profile/*`,
              `arn:${this.partition}:bedrock:${this.region}:${this.account}:application-inference-profile/*`,
            ],
            conditions: {
              StringEquals: {
                "aws:RequestedRegion": this.region,
                "aws:PrincipalAccount": this.account,
              },
            },
          }));
          fn.addToRolePolicy(new iam.PolicyStatement({
            sid: "InvokeRoutedFoundationModel",
            actions: ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
            resources: [
              `arn:${this.partition}:bedrock:*::foundation-model/*`,
            ],
            conditions: {
              StringEquals: { "aws:PrincipalAccount": this.account },
            },
          }));
        }
        new cdk.CfnOutput(this, `Tool${idBase}FunctionArn`, { value: fn.functionArn });
      } else {
        // fargate 도구만 per-tool ECR repo를 소비해요 (수동 push → ContainerImage.fromEcrRepository).
        const repo = new ecr.Repository(this, `Repo${idBase}`, {
          repositoryName: `agora-tool-${tool.toolId}-${stage}`,
          removalPolicy, emptyOnDelete: !isProd,
        });
        const taskDef = new ecs.FargateTaskDefinition(this, `Task${idBase}`, {
          // family를 명명 규칙으로 고정 — dispatcher가 taskDefinition=agora-tool-{tool}-{stage}로
          // run_task를 부르므로, family가 자동생성 ID면 TaskDefinition not found로 실패해요.
          family: `agora-tool-${tool.toolId}-${stage}`,
          cpu: 2048, memoryLimitMiB: tool.memoryMiB,
        });
        taskDef.taskRole.addToPrincipalPolicy(new iam.PolicyStatement({
          actions: ["s3:GetObject", "s3:PutObject", "s3:ListBucket"],
          resources: [bucket.bucketArn, `${bucket.bucketArn}/*`],
        }));
        const toolLogGroupName = `/agora/scan-tool/${tool.toolId}-${stage}`;
        const toolLogGroupResource = new logs.CfnLogGroup(
          this,
          `ToolLog${tool.toolId}`,
          { logGroupName: toolLogGroupName },
        );
        toolLogGroupResource.applyRemovalPolicy(removalPolicy);
        const toolLogGroup = logs.LogGroup.fromLogGroupName(
          this,
          `ToolLogReference${tool.toolId}`,
          toolLogGroupName,
        );
        taskDef.addContainer("scanner", {
          containerName: "scanner",
          image: ecs.ContainerImage.fromEcrRepository(repo, "latest"),
          logging: ecs.LogDrivers.awsLogs({
            streamPrefix: `tool-${tool.toolId}`,
            logGroup: toolLogGroup,
          }),
        });
        const taskExecutionRole = taskDef.executionRole;
        if (!taskExecutionRole) {
          throw new Error(`${tool.toolId} task execution role was not created`);
        }
        fargateTaskRoleArns.push(
          taskDef.taskRole.roleArn,
          taskExecutionRole.roleArn,
        );
        new cdk.CfnOutput(this, `Tool${idBase}TaskDefArn`, { value: taskDef.taskDefinitionArn });
      }
    }

    // ── 오케스트레이션: aggregate Lambda + Step Functions 상태머신 (SP-4) ──
    const aggregateFn = new lambda.Function(this, "AggregateFn", {
      functionName: `agora-scan-aggregate-${stage}`,
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: "aggregate_handler.handler",
      code: lambda.Code.fromAsset(path.join(SCAN_TOOLS_CTX, "orchestrator")),
      timeout: cdk.Duration.seconds(30),
    });

    // 거버넌스 콘솔 상태 공유 저장소(멀티인스턴스). PK/SK 단일 테이블.
    const govTable = new dynamodb.Table(this, "GovTable", {
      tableName: `AgoraGov-${stage}`,
      partitionKey: { name: "PK", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "SK", type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      removalPolicy,
    });
    govTable.grantWriteData(aggregateFn);
    aggregateFn.addEnvironment("AGORA_GOV_TABLE", govTable.tableName);
    new cdk.CfnOutput(this, "GovTableName", { value: govTable.tableName });

    // dispatcher Lambda — Map 브랜치가 호출. item의 compute·image_ref 보고 실제 도구 실행.
    const dispatchFn = new lambda.Function(this, "DispatchFn", {
      functionName: `agora-scan-dispatch-${stage}`,
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: "dispatch_handler.handler",
      code: lambda.Code.fromAsset(path.join(SCAN_TOOLS_CTX, "orchestrator")),
      timeout: cdk.Duration.minutes(15),
      environment: {
        AGORA_STAGE: stage,
        SCAN_CLUSTER: cluster.clusterName,
        SCAN_SUBNETS: vpc.selectSubnets({ subnetType: ec2.SubnetType.PRIVATE_ISOLATED }).subnetIds.join(","),
        SCAN_SG: sg.securityGroupId,
      },
    });
    bucket.grantReadWrite(dispatchFn);
    // 최소권한: 도구 Lambda invoke + Fargate run/describe + PassRole(도구 TaskDef role).
    dispatchFn.addToRolePolicy(new iam.PolicyStatement({
      actions: ["lambda:InvokeFunction"],
      resources: [`arn:aws:lambda:${this.region}:${this.account}:function:agora-tool-*`],
    }));
    dispatchFn.addToRolePolicy(new iam.PolicyStatement({
      actions: ["ecs:RunTask", "ecs:DescribeTasks"],
      resources: ["*"],  // ECS run_task는 taskDef ARN 리소스 스코프가 계정/리전 한정이라 조건으로 좁힘
      conditions: { ArnEquals: { "ecs:cluster": cluster.clusterArn } },
    }));
    dispatchFn.addToRolePolicy(new iam.PolicyStatement({
      actions: ["iam:PassRole"],
      resources: fargateTaskRoleArns,
      conditions: { StringEquals: { "iam:PassedToService": "ecs-tasks.amazonaws.com" } },
    }));

    // Map: execution input.tools 를 병렬 순회. 각 item을 dispatcher Lambda로 라우팅해
    // (compute·image_ref 보고 실제 도구 실행), Map 결과를 aggregate Lambda로 fan-in.
    const dispatchTask = new tasks.LambdaInvoke(this, "DispatchTool", {
      lambdaFunction: dispatchFn,
      payload: sfn.TaskInput.fromObject({
        "tool_id.$": "$.tool_id", "area.$": "$.area", "compute.$": "$.compute",
        "image_ref.$": "$.image_ref", "scan_id.$": "$.scan_id", "bucket.$": "$.bucket",
        "input_prefix.$": "$.input_prefix", "output_prefix.$": "$.output_prefix",
        "model_alias.$": "$.model_alias",
      }),
      outputPath: "$.Payload",
    });
    const mapState = new sfn.Map(this, "ScanToolsMap", {
      itemsPath: sfn.JsonPath.stringAt("$.tools"),
      itemSelector: {
        "tool_id.$": "$$.Map.Item.Value.tool_id",
        "area.$": "$$.Map.Item.Value.area",
        "compute.$": "$$.Map.Item.Value.compute",
        "image_ref.$": "$$.Map.Item.Value.image_ref",
        "model_alias.$": "$$.Map.Item.Value.model_alias",
        "scan_id.$": "$.scan_id", "bucket.$": "$.bucket",
        "input_prefix.$": "$.input_prefix", "output_prefix.$": "$.output_prefix",
      },
      resultPath: "$.results",
      maxConcurrency: 10,
    });
    mapState.itemProcessor(dispatchTask);
    const aggregateTask = new tasks.LambdaInvoke(this, "Aggregate", {
      lambdaFunction: aggregateFn,
      payload: sfn.TaskInput.fromObject({
        // Map(resultPath:$.results)이 top-level $.scan_id·$.record_id를 보존하므로 그대로 읽어요.
        // record_id로 aggregate Lambda가 DynamoGovStore SCAN#{record_id}에 결과를 write해요.
        "scan_id.$": "$.scan_id", "record_id.$": "$.record_id", "results.$": "$.results",
      }),
      outputPath: "$.Payload",
    });
    const definition = mapState.next(aggregateTask);
    const stateMachine = new sfn.StateMachine(this, "ScanStateMachine", {
      stateMachineName: `agora-scan-orchestration-${stage}`,
      definitionBody: sfn.DefinitionBody.fromChainable(definition),
      timeout: cdk.Duration.minutes(30),
    });
    bucket.grantReadWrite(aggregateFn);
    new cdk.CfnOutput(this, "StateMachineArn", { value: stateMachine.stateMachineArn });

    // ── EventBridge 트리거: ScanRequested 이벤트 → SF StartExecution (완전 비동기 진입) ──
    const rule = new events.Rule(this, "ScanRequestedRule", {
      ruleName: `agora-scan-requested-${stage}`,
      eventPattern: {
        source: ["agora.governance"],
        detailType: ["ScanRequested"],
      },
    });
    // 이벤트 envelope의 detail을 상태머신 top-level 입력으로 매핑해요. 매핑이 없으면
    // 전체 envelope가 전달돼 SM 입력이 $.detail.* 아래로 들어가고, SM은 top-level
    // $.tools/$.scan_id 를 읽으므로 Map이 실패해요(잘못된 payload shape). detail만 벗겨
    // 전달하면 {scan_id, tools, ...} 가 top-level로 매칭돼요.
    rule.addTarget(new targets.SfnStateMachine(stateMachine, {
      input: events.RuleTargetInput.fromEventPath("$.detail"),
    }));

    const isolatedSubnetIds = vpc.selectSubnets({ subnetType: ec2.SubnetType.PRIVATE_ISOLATED }).subnetIds.join(",");
    new cdk.CfnOutput(this, "ScanBucketName", { value: bucket.bucketName });
    new cdk.CfnOutput(this, "IsolatedSubnetIds", { value: isolatedSubnetIds });
    new cdk.CfnOutput(this, "ScanSecurityGroupId", { value: sg.securityGroupId });
    new cdk.CfnOutput(this, "ClusterName", { value: cluster.clusterName });
  }
}
