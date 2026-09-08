import * as fs from "fs";
import * as path from "path";
import * as cdk from "aws-cdk-lib";
import { Construct } from "constructs";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as iam from "aws-cdk-lib/aws-iam";
import * as lambda from "aws-cdk-lib/aws-lambda";
import * as logs from "aws-cdk-lib/aws-logs";
import * as sqs from "aws-cdk-lib/aws-sqs";
import * as ssm from "aws-cdk-lib/aws-ssm";
import { Stage } from "./catalog-storage-stack";

const deployJobsScanContract = JSON.parse(fs.readFileSync(path.join(
  __dirname,
  "..",
  "..",
  "api",
  "src",
  "agora",
  "domains",
  "monitoring",
  "deploy-jobs-scan-contract.json",
), "utf8"));

export interface MonitoringAggregateStackProps extends cdk.StackProps {
  readonly stage: Stage;
  readonly apiExecutionRole: iam.IRole;
  readonly manageSharedSpans?: boolean;
}

export class MonitoringAggregateStack extends cdk.Stack {
  public readonly aggregateTableName: string;
  public readonly ingestDlqUrl: string;

  constructor(
    scope: Construct,
    id: string,
    props: MonitoringAggregateStackProps,
  ) {
    super(scope, id, props);

    // 형제 스택(catalog-storage·identity)과 같은 stage 분기예요. dev 에서 RETAIN 이면
    // 첫 배포가 중간에 실패했을 때 테이블과 SSM 파라미터가 남고, 재시도가 changeset
    // 단계에서 "already exists" 로 죽어요 — 사람이 수동으로 지워야 다시 배포돼요.
    const removalPolicy = props.stage === "prod"
      ? cdk.RemovalPolicy.RETAIN
      : cdk.RemovalPolicy.DESTROY;

    const table = new dynamodb.Table(this, "AggregateTable", {
      tableName: `AgoraMonitoringAggregate-${props.stage}`,
      partitionKey: { name: "PK", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "SK", type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      timeToLiveAttribute: "expires_at",
      removalPolicy,
    });
    const dlq = new sqs.Queue(this, "IngestDlq", {
      queueName: `agora-monitoring-ingest-dlq-${props.stage}`,
      encryption: sqs.QueueEncryption.SQS_MANAGED,
      retentionPeriod: cdk.Duration.days(14),
    });
    const manageSharedSpans = props.manageSharedSpans === true;
    if (manageSharedSpans) {
      const ownerClaim = new ssm.StringParameter(this, "SharedSpansOwnerClaim", {
        parameterName: "/agora/monitoring-aggregate/shared-spans-owner",
        stringValue: props.stage,
        description: (
          "Account/region singleton claim for the aws/spans aggregate owner."
        ),
      });
      ownerClaim.applyRemovalPolicy(removalPolicy);

      const ingest = new lambda.Function(this, "IngestFunction", {
        functionName: `agora-monitoring-aggregate-${props.stage}`,
        runtime: lambda.Runtime.PYTHON_3_12,
        handler: "agora.domains.monitoring.ingest_lambda.handler",
        code: lambda.Code.fromAsset(
          path.join(__dirname, "..", "..", "api", "src"),
        ),
        timeout: cdk.Duration.seconds(60),
        memorySize: 512,
        retryAttempts: 2,
        deadLetterQueue: dlq,
        environment: {
          AGORA_ROLE: "lambda",
          AGORA_MONITORING_STAGE: props.stage,
          AGORA_MONITORING_AGGREGATE_TABLE: table.tableName,
          AGORA_MONITORING_DEPLOY_JOBS_TABLE: `AgoraDeployJobs-${props.stage}`,
          AGORA_MONITORING_RETENTION_DAYS: "400",
        },
      });
      ingest.addToRolePolicy(new iam.PolicyStatement({
        actions: [
          "dynamodb:GetItem",
          "dynamodb:PutItem",
          "dynamodb:UpdateItem",
        ],
        resources: [table.tableArn],
      }));
      const deployJobs = dynamodb.Table.fromTableName(
        this,
        "DeployJobs",
        `AgoraDeployJobs-${props.stage}`,
      );
      ingest.addToRolePolicy(new iam.PolicyStatement({
        actions: ["dynamodb:Scan"],
        resources: [deployJobs.tableArn],
        conditions: {
          "ForAllValues:StringEquals": {
            "dynamodb:Attributes": [
              ...deployJobsScanContract.tableKeyAttributes,
              ...deployJobsScanContract.projectionAttributes,
            ],
          },
          StringEquals: {
            "dynamodb:Select": deployJobsScanContract.select,
          },
        },
      }));
      const invokePermission = new lambda.CfnPermission(
        this,
        "AllowSharedSpansInvoke",
        {
          action: "lambda:InvokeFunction",
          functionName: ingest.functionName,
          principal: `logs.${this.region}.amazonaws.com`,
          sourceAccount: this.account,
          sourceArn: this.formatArn({
            service: "logs",
            resource: "log-group",
            resourceName: "aws/spans:*",
            arnFormat: cdk.ArnFormat.COLON_RESOURCE_NAME,
          }),
        },
      );
      const subscription = new logs.CfnSubscriptionFilter(
        this,
        "SharedSpansSubscription",
        {
          destinationArn: ingest.functionArn,
          filterName: "agora-monitoring-aggregate-shared-spans",
          filterPattern: "",
          logGroupName: "aws/spans",
        },
      );
      subscription.addResourceDependency(invokePermission);
    }

    table.grantReadData(props.apiExecutionRole);
    dlq.grant(
      props.apiExecutionRole,
      "sqs:GetQueueAttributes",
      "sqs:GetQueueUrl",
    );
    this.aggregateTableName = table.tableName;
    this.ingestDlqUrl = dlq.queueUrl;

    new cdk.CfnOutput(this, "MonitoringAggregateTableName", {
      value: table.tableName,
    });
    new cdk.CfnOutput(this, "MonitoringIngestDlqUrl", {
      value: dlq.queueUrl,
    });
  }
}
