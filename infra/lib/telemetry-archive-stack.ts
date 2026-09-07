import * as path from "path";
import * as cdk from "aws-cdk-lib";
import { Construct } from "constructs";
import * as cloudwatch from "aws-cdk-lib/aws-cloudwatch";
import * as cwActions from "aws-cdk-lib/aws-cloudwatch-actions";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as events from "aws-cdk-lib/aws-events";
import * as targets from "aws-cdk-lib/aws-events-targets";
import * as firehose from "aws-cdk-lib/aws-kinesisfirehose";
import * as iam from "aws-cdk-lib/aws-iam";
import * as lambda from "aws-cdk-lib/aws-lambda";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as s3Notifications from "aws-cdk-lib/aws-s3-notifications";
import * as sns from "aws-cdk-lib/aws-sns";
import * as ssm from "aws-cdk-lib/aws-ssm";
import { Stage } from "./catalog-storage-stack";

/**
 * S3 이벤트 알림 prefix 필터를 이벤트 키와 같은 인코딩으로 맞춘다.
 *
 * 2026-08-24 dev/ap-northeast-2 실측: 알림 필터는 **URL 인코딩된 키**에 매칭된다.
 * 리터럴 `stage=dev/` 필터는 실제 archive 객체(`stage=dev/...`)를 하나도 잡지 못해
 * manifest recorder 가 호출 0회였고, 같은 Lambda·같은 버킷에서 `plainprefix/` 는
 * 3초 만에 호출됐다. 필터를 `stage%3Ddev/` 로 바꾸자 즉시 호출됐다.
 *
 * 세그먼트 단위로 인코딩해 `/` 는 그대로 둔다. 공백은 S3 가 `+` 로 인코딩하므로
 * 이 helper 로 표현할 수 없다 — prefix 에 공백을 넣지 않는다.
 */
function eventKeyPrefix(prefix: string): string {
  return prefix.split("/").map(encodeURIComponent).join("/");
}

export interface TelemetryArchiveStackProps extends cdk.StackProps {
  readonly stage: Stage;
  /**
   * aws/spans is account-shared and cannot be attributed to a stage. Exactly
   * one stack per account/region may be designated as its archive owner.
   */
  readonly manageSharedSpans?: boolean;
}

/**
 * Archives owned AgentCore runtime logs and optionally account-shared spans.
 *
 * Runtime ownership comes from AgoraDeployJobs-<stage>; this stack never scans
 * or mutates every group under the AgentCore runtime prefix.
 */
export class TelemetryArchiveStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: TelemetryArchiveStackProps) {
    super(scope, id, props);
    const logGroupArn = (name: string) => this.formatArn({
      service: "logs",
      resource: "log-group",
      resourceName: name,
      arnFormat: cdk.ArnFormat.COLON_RESOURCE_NAME,
    });
    const manageSharedSpans = props.manageSharedSpans === true;
    if (manageSharedSpans) {
      const sharedSpansOwner = new ssm.StringParameter(
        this,
        "SharedSpansOwnerClaim",
        {
          parameterName: "/agora/telemetry-archive/shared-spans-owner",
          stringValue: props.stage,
          description: (
            "Account/region singleton claim for the aws/spans archive owner."
          ),
        },
      );
      sharedSpansOwner.applyRemovalPolicy(cdk.RemovalPolicy.RETAIN);
    }

    const objectLockDays = new cdk.CfnParameter(
      this,
      "TelemetryArchiveObjectLockDays",
      {
        type: "Number",
        minValue: 1,
        description: (
          "Approved GOVERNANCE Object Lock duration for telemetry objects."
        ),
      },
    );
    const iaStorageClass = new cdk.CfnParameter(
      this,
      "TelemetryArchiveIaStorageClass",
      {
        type: "String",
        allowedValues: ["STANDARD_IA"],
        description: (
          "Approved multi-AZ infrequent-access class. ONEZONE_IA is forbidden."
        ),
      },
    );
    const iaTransitionDays = new cdk.CfnParameter(
      this,
      "TelemetryArchiveIaTransitionDays",
      {
        type: "Number",
        minValue: 30,
        description: "Days after S3 arrival before the IA transition.",
      },
    );
    const glacierStorageClass = new cdk.CfnParameter(
      this,
      "TelemetryArchiveGlacierStorageClass",
      {
        type: "String",
        allowedValues: ["GLACIER_IR", "GLACIER", "DEEP_ARCHIVE"],
        description: "Approved Glacier-family class for archived telemetry.",
      },
    );
    const glacierTransitionDays = new cdk.CfnParameter(
      this,
      "TelemetryArchiveGlacierTransitionDays",
      {
        type: "Number",
        minValue: 30,
        description: "Days after S3 arrival before the Glacier transition.",
      },
    );
    const currentExpirationDays = new cdk.CfnParameter(
      this,
      "TelemetryArchiveExpirationDays",
      {
        type: "Number",
        default: 0,
        minValue: 0,
        description: (
          "Approved current-object expiry in days; 0 keeps expiry disabled."
        ),
      },
    );
    const noncurrentExpirationDays = new cdk.CfnParameter(
      this,
      "TelemetryArchiveNoncurrentExpirationDays",
      {
        type: "Number",
        default: 0,
        minValue: 0,
        description: (
          "Approved noncurrent-version expiry in days; 0 keeps it disabled."
        ),
      },
    );
    const maxIngestLagSeconds = new cdk.CfnParameter(
      this,
      "TelemetryMaxIngestLagSeconds",
      {
        type: "Number",
        minValue: 300,
        description: "Maximum approved Firehose-to-S3 ingest lag.",
      },
    );
    const maxIncomingBytesPerHour = new cdk.CfnParameter(
      this,
      "TelemetryMaxIncomingBytesPerHour",
      {
        type: "Number",
        minValue: 1,
        description: "Approved hourly Firehose ingress ceiling in bytes.",
      },
    );
    const maxArchiveBytes = new cdk.CfnParameter(
      this,
      "TelemetryMaxArchiveBytes",
      {
        type: "Number",
        minValue: 1,
        description: "Approved total archive storage ceiling in bytes.",
      },
    );
    const alarmTopicArn = new cdk.CfnParameter(
      this,
      "TelemetryAlarmTopicArn",
      {
        type: "String",
        allowedPattern: "^arn:[^:]+:sns:[^:]+:[0-9]{12}:.+$",
        description: "Existing operated SNS topic that receives every alarm.",
      },
    );

    const archiveBucket = new s3.Bucket(this, "ArchiveBucket", {
      encryption: s3.BucketEncryption.S3_MANAGED,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      enforceSSL: true,
      versioned: true,
      objectLockEnabled: true,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
      autoDeleteObjects: false,
    });
    const cfnBucket = archiveBucket.node.defaultChild as s3.CfnBucket;
    cfnBucket.objectLockConfiguration = {
      objectLockEnabled: "Enabled",
      rule: {
        defaultRetention: {
          mode: "GOVERNANCE",
          days: objectLockDays.valueAsNumber,
        },
      },
    };
    const expireCurrent = new cdk.CfnCondition(
      this,
      "ExpireTelemetryCurrentObjects",
      {
        expression: cdk.Fn.conditionNot(
          cdk.Fn.conditionEquals(currentExpirationDays.value, 0),
        ),
      },
    );
    const expireNoncurrent = new cdk.CfnCondition(
      this,
      "ExpireTelemetryNoncurrentObjects",
      {
        expression: cdk.Fn.conditionNot(
          cdk.Fn.conditionEquals(noncurrentExpirationDays.value, 0),
        ),
      },
    );
    cfnBucket.lifecycleConfiguration = {
      rules: [{
        id: "TierArchivedTelemetry",
        status: "Enabled",
        transitions: [
          {
            storageClass: iaStorageClass.valueAsString,
            transitionInDays: iaTransitionDays.valueAsNumber,
          },
          {
            storageClass: glacierStorageClass.valueAsString,
            transitionInDays: glacierTransitionDays.valueAsNumber,
          },
        ],
        expirationInDays: cdk.Fn.conditionIf(
          expireCurrent.logicalId,
          currentExpirationDays.valueAsNumber,
          cdk.Aws.NO_VALUE,
        ),
        noncurrentVersionExpiration: cdk.Fn.conditionIf(
          expireNoncurrent.logicalId,
          { NoncurrentDays: noncurrentExpirationDays.valueAsNumber },
          cdk.Aws.NO_VALUE,
        ),
      }],
    } as any;

    const coverageTable = new dynamodb.Table(this, "CoverageLedger", {
      tableName: `AgoraTelemetryCoverage-${props.stage}`,
      partitionKey: {
        name: "log_group_name",
        type: dynamodb.AttributeType.STRING,
      },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecoverySpecification: {
        pointInTimeRecoveryEnabled: true,
      },
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });
    const deployJobsTable = dynamodb.Table.fromTableName(
      this,
      "DeployJobsInventory",
      `AgoraDeployJobs-${props.stage}`,
    );

    const cloudWatchLogsPrincipal = new iam.ServicePrincipal(
      `logs.${this.region}.amazonaws.com`,
    );
    archiveBucket.addToResourcePolicy(new iam.PolicyStatement({
      sid: "AllowCloudWatchLogsBackfillBucketCheck",
      principals: [cloudWatchLogsPrincipal],
      actions: ["s3:GetBucketAcl"],
      resources: [archiveBucket.bucketArn],
      conditions: {
        StringEquals: { "aws:SourceAccount": this.account },
        ArnLike: { "aws:SourceArn": logGroupArn("*") },
      },
    }));
    archiveBucket.addToResourcePolicy(new iam.PolicyStatement({
      sid: "AllowCloudWatchLogsBackfillWrite",
      principals: [cloudWatchLogsPrincipal],
      actions: ["s3:PutObject"],
      resources: [archiveBucket.arnForObjects("backfill/*")],
      conditions: {
        StringEquals: {
          "aws:SourceAccount": this.account,
          "s3:x-amz-acl": "bucket-owner-full-control",
        },
        ArnLike: {
          // 2026-08-24 dev 실측: runtime 패턴은 끝의 `*` 가 `:*` 접미사까지 삼켜
          // 77개 그룹의 export 가 통과했지만, 정확 매칭인 `aws/spans` 는 CreateExportTask
          // 가 계속 AccessDenied 였다(CloudTrail 메시지는 "An unknown error occurred"
          // 로 사유를 숨긴다). 두 형태를 모두 허용한다.
          "aws:SourceArn": [
            logGroupArn("/aws/bedrock-agentcore/runtimes/*"),
            ...(manageSharedSpans
              ? [
                logGroupArn("aws/spans"),
                `${logGroupArn("aws/spans")}:*`,
              ]
              : []),
          ],
        },
      },
    }));

    const firehoseRole = new iam.Role(this, "FirehoseRole", {
      assumedBy: new iam.ServicePrincipal("firehose.amazonaws.com", {
        conditions: {
          StringEquals: { "aws:SourceAccount": this.account },
        },
      }),
    });
    firehoseRole.addToPolicy(new iam.PolicyStatement({
      actions: [
        "s3:AbortMultipartUpload",
        "s3:GetBucketLocation",
        "s3:ListBucket",
        "s3:ListBucketMultipartUploads",
        "s3:PutObject",
      ],
      resources: [archiveBucket.bucketArn, archiveBucket.arnForObjects("*")],
    }));

    const deliveryStreamName = `agora-telemetry-archive-${props.stage}`;
    const deliveryStream = new firehose.CfnDeliveryStream(
      this,
      "ArchiveDeliveryStream",
      {
        deliveryStreamName,
        deliveryStreamType: "DirectPut",
        extendedS3DestinationConfiguration: {
          bucketArn: archiveBucket.bucketArn,
          roleArn: firehoseRole.roleArn,
          compressionFormat: "UNCOMPRESSED",
          bufferingHints: {
            intervalInSeconds: 300,
            sizeInMBs: 5,
          },
          prefix: (
            `stage=${props.stage}/year=!{timestamp:yyyy}/`
            + "month=!{timestamp:MM}/day=!{timestamp:dd}/hour=!{timestamp:HH}/"
          ),
          errorOutputPrefix: (
            `errors/stage=${props.stage}/type=!{firehose:error-output-type}/`
            + "year=!{timestamp:yyyy}/month=!{timestamp:MM}/day=!{timestamp:dd}/"
          ),
        },
      },
    );
    deliveryStream.addResourceDependency(cfnBucket);
    const deliveryStreamArn = this.formatArn({
      service: "firehose",
      resource: "deliverystream",
      resourceName: deliveryStreamName,
    });
    const logsDeliveryRole = new iam.Role(this, "CloudWatchLogsDeliveryRole", {
      assumedBy: new iam.ServicePrincipal(`logs.${this.region}.amazonaws.com`, {
        conditions: {
          StringEquals: { "aws:SourceAccount": this.account },
          ArnLike: {
            "aws:SourceArn":
              `arn:${this.partition}:logs:${this.region}:${this.account}:*`,
          },
        },
      }),
    });
    logsDeliveryRole.addToPolicy(new iam.PolicyStatement({
      actions: ["firehose:PutRecord", "firehose:PutRecordBatch"],
      resources: [deliveryStreamArn],
    }));

    const filterName = `agora-telemetry-archive-${props.stage}`;
    const lambdaEnvironment = {
      AGORA_ROLE: "lambda",
      AGORA_TELEMETRY_STAGE: props.stage,
      AGORA_TELEMETRY_RUNTIME_LOG_GROUP_PREFIX:
        "/aws/bedrock-agentcore/runtimes/",
      AGORA_TELEMETRY_SHARED_SPAN_LOG_GROUP: "aws/spans",
      AGORA_TELEMETRY_MANAGE_SHARED_SPANS: String(manageSharedSpans),
      AGORA_TELEMETRY_DESTINATION_ARN: deliveryStreamArn,
      AGORA_TELEMETRY_SUBSCRIPTION_ROLE_ARN: logsDeliveryRole.roleArn,
      AGORA_TELEMETRY_SUBSCRIPTION_FILTER_NAME: filterName,
      AGORA_TELEMETRY_SHARED_SPAN_FILTER_NAME:
        "agora-telemetry-archive-shared-spans",
      AGORA_TELEMETRY_DEPLOY_JOBS_TABLE: deployJobsTable.tableName,
      AGORA_TELEMETRY_COVERAGE_TABLE: coverageTable.tableName,
      AGORA_TELEMETRY_ARCHIVE_BUCKET: archiveBucket.bucketName,
      AGORA_TELEMETRY_MAX_INGEST_LAG_SECONDS:
        maxIngestLagSeconds.valueAsString,
    };
    const code = lambda.Code.fromAsset(
      path.join(__dirname, "..", "lambda", "telemetry-archive-reconciler"),
      { exclude: ["test_*.py", "__pycache__"] },
    );
    const reconciler = new lambda.Function(this, "LogPolicyReconciler", {
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: "index.handler",
      code,
      timeout: cdk.Duration.minutes(10),
      memorySize: 256,
      environment: lambdaEnvironment,
    });
    deployJobsTable.grantReadData(reconciler);
    coverageTable.grantReadWriteData(reconciler);
    reconciler.addToRolePolicy(new iam.PolicyStatement({
      actions: ["logs:DescribeLogGroups", "logs:DescribeExportTasks"],
      resources: ["*"],
    }));
    // 2026-08-24 dev/ap-northeast-2 실측: CloudWatch Logs 는 PutSubscriptionFilter 를
    // 접미사 `:*` 가 **없는** 로그 그룹 ARN 으로 인가한다. CloudTrail AccessDenied 메시지가
    // `log-group:/aws/bedrock-agentcore/runtimes/<name>`(suffix 없음)을 그대로 찍었고
    // `:*` 만 있던 정책은 78개 그룹 전부를 거부했다. API 마다 어느 형태로 평가하는지가
    // 달라 두 형태를 모두 허용한다(그룹 단위 action 이므로 stream 확장은 없다).
    const subscriptionResources = [
      logGroupArn("/aws/bedrock-agentcore/runtimes/*"),
      `${logGroupArn("/aws/bedrock-agentcore/runtimes/*")}:*`,
      ...(manageSharedSpans
        ? [logGroupArn("aws/spans"), `${logGroupArn("aws/spans")}:*`]
        : []),
    ];
    reconciler.addToRolePolicy(new iam.PolicyStatement({
      actions: [
        "logs:DescribeSubscriptionFilters",
        "logs:PutSubscriptionFilter",
        "logs:CreateExportTask",
      ],
      resources: subscriptionResources,
    }));
    // 2026-08-24 dev 실측: 지역화 주체(`logs.<region>.amazonaws.com`)만 허용하면
    // PutSubscriptionFilter 의 PassRole 검사가 CloudTrail 에 `iam:PassRole` 거부로 남는다.
    // 실제 요청의 `iam:PassedToService` 는 전역 `logs.amazonaws.com` 이다. 로그 그룹
    // 신뢰 정책은 지역화 주체를 쓰므로 두 형태를 모두 허용해 조건을 유지한다.
    reconciler.addToRolePolicy(new iam.PolicyStatement({
      actions: ["iam:PassRole"],
      resources: [logsDeliveryRole.roleArn],
      conditions: {
        StringEquals: {
          "iam:PassedToService": [
            "logs.amazonaws.com",
            `logs.${this.region}.amazonaws.com`,
          ],
        },
      },
    }));
    reconciler.addToRolePolicy(new iam.PolicyStatement({
      actions: ["cloudwatch:PutMetricData"],
      resources: ["*"],
      conditions: {
        StringEquals: {
          "cloudwatch:namespace": "Agora/TelemetryArchive",
        },
      },
    }));

    const manifestRecorder = new lambda.Function(
      this,
      "ArchiveManifestRecorder",
      {
        runtime: lambda.Runtime.PYTHON_3_12,
        handler: "manifest.handler",
        code,
        timeout: cdk.Duration.minutes(5),
        memorySize: 512,
        environment: lambdaEnvironment,
      },
    );
    archiveBucket.grantRead(manifestRecorder);
    archiveBucket.grantWrite(manifestRecorder, "manifests/*");
    coverageTable.grantReadWriteData(manifestRecorder);
    archiveBucket.addEventNotification(
      s3.EventType.OBJECT_CREATED,
      new s3Notifications.LambdaDestination(manifestRecorder),
      { prefix: eventKeyPrefix(`stage=${props.stage}/`) },
    );
    archiveBucket.addEventNotification(
      s3.EventType.OBJECT_CREATED,
      new s3Notifications.LambdaDestination(manifestRecorder),
      { prefix: eventKeyPrefix(`backfill/stage=${props.stage}/`) },
    );

    const schedule = new events.Rule(this, "ReconcileSchedule", {
      schedule: events.Schedule.rate(cdk.Duration.minutes(5)),
    });
    schedule.addTarget(new targets.LambdaFunction(reconciler));

    const alarmTopic = sns.Topic.fromTopicArn(
      this,
      "TelemetryAlarmReceiver",
      alarmTopicArn.valueAsString,
    );
    const alarms: cloudwatch.Alarm[] = [];
    alarms.push(new cloudwatch.Alarm(this, "ReconcilerErrors", {
      metric: reconciler.metricErrors({ period: cdk.Duration.minutes(5) }),
      threshold: 1,
      evaluationPeriods: 1,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    }));
    alarms.push(new cloudwatch.Alarm(this, "ManifestRecorderErrors", {
      metric: manifestRecorder.metricErrors({
        period: cdk.Duration.minutes(5),
      }),
      threshold: 1,
      evaluationPeriods: 1,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    }));
    alarms.push(new cloudwatch.Alarm(this, "CoverageIncomplete", {
      metric: new cloudwatch.Metric({
        namespace: "Agora/TelemetryArchive",
        metricName: "CoverageIncomplete",
        dimensionsMap: { Stage: props.stage },
        statistic: "Maximum",
        period: cdk.Duration.minutes(5),
      }),
      threshold: 1,
      evaluationPeriods: 1,
      treatMissingData: cloudwatch.TreatMissingData.BREACHING,
    }));
    alarms.push(new cloudwatch.Alarm(this, "SubscriptionDrift", {
      metric: new cloudwatch.Metric({
        namespace: "Agora/TelemetryArchive",
        metricName: "SubscriptionDrift",
        dimensionsMap: { Stage: props.stage },
        statistic: "Maximum",
        period: cdk.Duration.minutes(5),
      }),
      threshold: 1,
      evaluationPeriods: 1,
      treatMissingData: cloudwatch.TreatMissingData.BREACHING,
    }));
    alarms.push(new cloudwatch.Alarm(this, "FirehoseDeliveryFailure", {
      metric: new cloudwatch.Metric({
        namespace: "AWS/Firehose",
        metricName: "DeliveryToS3.Success",
        dimensionsMap: { DeliveryStreamName: deliveryStreamName },
        statistic: "Average",
        period: cdk.Duration.minutes(5),
      }),
      // 2026-08-24 dev 실측: `DeliveryToS3.Success` 는 백분율이 아니라 성공 비율
      // (0~1)이다. 정상 배달의 실측 datapoint 가 `1.0` 이었고 임계값 `99` 와 비교하니
      // **성공할 때마다** ALARM 이 됐다("1 datapoint [1.0] was less than the threshold
      // (99.0)"). 상시 울리는 알람은 진짜 실패를 덮으므로 비율 임계로 고친다.
      threshold: 0.99,
      comparisonOperator:
        cloudwatch.ComparisonOperator.LESS_THAN_THRESHOLD,
      evaluationPeriods: 1,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    }));
    alarms.push(new cloudwatch.Alarm(this, "FirehoseIngestLag", {
      metric: new cloudwatch.Metric({
        namespace: "AWS/Firehose",
        metricName: "DeliveryToS3.DataFreshness",
        dimensionsMap: { DeliveryStreamName: deliveryStreamName },
        statistic: "Maximum",
        period: cdk.Duration.minutes(5),
      }),
      threshold: maxIngestLagSeconds.valueAsNumber,
      evaluationPeriods: 1,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    }));
    alarms.push(new cloudwatch.Alarm(this, "FirehoseHourlyIngress", {
      metric: new cloudwatch.Metric({
        namespace: "AWS/Firehose",
        metricName: "IncomingBytes",
        dimensionsMap: { DeliveryStreamName: deliveryStreamName },
        statistic: "Sum",
        period: cdk.Duration.hours(1),
      }),
      threshold: maxIncomingBytesPerHour.valueAsNumber,
      evaluationPeriods: 1,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    }));
    const archiveStorageTypes = [
      "StandardStorage",
      "StandardIAStorage",
      "StandardIASizeOverhead",
      "GlacierInstantRetrievalStorage",
      "GlacierStorage",
      "GlacierS3ObjectOverhead",
      "GlacierObjectOverhead",
      "DeepArchiveStorage",
      "DeepArchiveS3ObjectOverhead",
      "DeepArchiveObjectOverhead",
    ];
    const archiveStorageMetrics = Object.fromEntries(
      archiveStorageTypes.map((storageType, index) => [
        `storage${index}`,
        new cloudwatch.Metric({
          namespace: "AWS/S3",
          metricName: "BucketSizeBytes",
          dimensionsMap: {
            BucketName: archiveBucket.bucketName,
            StorageType: storageType,
          },
          statistic: "Average",
          period: cdk.Duration.days(1),
        }),
      ]),
    );
    alarms.push(new cloudwatch.Alarm(this, "ArchiveStorageSize", {
      metric: new cloudwatch.MathExpression({
        expression: Object.keys(archiveStorageMetrics).join(" + "),
        usingMetrics: archiveStorageMetrics,
        period: cdk.Duration.days(1),
      }),
      threshold: maxArchiveBytes.valueAsNumber,
      evaluationPeriods: 1,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    }));
    for (const alarm of alarms) {
      alarm.addAlarmAction(new cwActions.SnsAction(alarmTopic));
      alarm.addOkAction(new cwActions.SnsAction(alarmTopic));
    }

    new cdk.CfnOutput(this, "TelemetryArchiveBucket", {
      value: archiveBucket.bucketName,
    });
    new cdk.CfnOutput(this, "TelemetryCoverageTable", {
      value: coverageTable.tableName,
    });
    new cdk.CfnOutput(this, "TelemetryArchiveDeliveryStreamArn", {
      value: deliveryStreamArn,
    });
    new cdk.CfnOutput(this, "SharedSpansManagedByThisStack", {
      value: String(manageSharedSpans),
    });
  }
}
