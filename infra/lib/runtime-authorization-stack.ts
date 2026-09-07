import * as fs from "fs";
import * as path from "path";
import { spawnSync } from "child_process";
import * as cdk from "aws-cdk-lib";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as iam from "aws-cdk-lib/aws-iam";
import * as lambda from "aws-cdk-lib/aws-lambda";
import * as s3 from "aws-cdk-lib/aws-s3";
import { Construct } from "constructs";
import { Stage } from "./catalog-storage-stack";

export interface RuntimeAuthorizationStackProps extends cdk.StackProps {
  readonly stage: Stage;
  readonly apiExecutionRole: iam.IRole;
  readonly catalogTable: dynamodb.ITable;
  readonly artifactsBucket: s3.IBucket;
  readonly identityDataTable: dynamodb.ITable;
  readonly registryId: string;
  /** Registry 서비스 네임스페이스(CA-05/ADR-0015). bedrock-agentcore(기본) | agent-registry. */
  readonly registryNamespace: string;
  readonly workloadCognitoDiscoveryUrl: string;
  readonly workloadCognitoClientId: string;
  readonly workloadCognitoScope: string;
  readonly code?: lambda.Code;
}

function runUv(args: string[], cwd: string): void {
  const result = spawnSync("uv", args, { cwd, stdio: "inherit" });
  if (result.error) {
    throw result.error;
  }
  if (result.status !== 0) {
    throw new Error(`uv ${args[0]} failed with status ${result.status}`);
  }
}

/** Docker가 없는 개발 환경에서도 Lambda ARM64 패키지를 재현 가능하게 만들어요. */
export function pythonArm64LocalBundling(sourceDir: string): cdk.ILocalBundling {
  return {
    tryBundle(outputDir: string): boolean {
      const probe = spawnSync("uv", ["--version"], { stdio: "ignore" });
      if (probe.error && (probe.error as NodeJS.ErrnoException).code === "ENOENT") {
        return false;
      }
      if (probe.error || probe.status !== 0) {
        throw probe.error ?? new Error("uv availability check failed");
      }

      const requirements = path.join(outputDir, "requirements.txt");
      runUv([
        "export",
        "--frozen",
        "--no-dev",
        "--extra",
        "aws",
        "--no-emit-project",
        "--no-hashes",
        "--quiet",
        "--output-file",
        requirements,
      ], sourceDir);
      runUv([
        "pip",
        "install",
        "--target",
        outputDir,
        "--python-platform",
        "aarch64-manylinux2014",
        "--python-version",
        "3.12",
        "--only-binary=:all:",
        "--no-progress",
        "--quiet",
        "--requirements",
        requirements,
      ], sourceDir);
      fs.rmSync(requirements);
      fs.cpSync(
        path.join(sourceDir, "src/agora"),
        path.join(outputDir, "agora"),
        {
          recursive: true,
          filter: (source) => (
            !source.includes(`${path.sep}__pycache__`)
            && !source.endsWith(".pyc")
          ),
        },
      );
      return true;
    },
  };
}

/** Runtime의 MCP tool 호출 직전 authorization decision endpoint. */
export class RuntimeAuthorizationStack extends cdk.Stack {
  public readonly authorizationUrl: string;

  constructor(
    scope: Construct,
    id: string,
    props: RuntimeAuthorizationStackProps,
  ) {
    super(scope, id, props);
    if (!props.registryId.trim()) {
      throw new Error("registryId is required for runtime authorization");
    }

    const apiSource = path.join(__dirname, "../../api");
    const code = props.code ?? lambda.Code.fromAsset(
      apiSource,
      {
        exclude: [
          ".venv",
          ".pytest_cache",
          ".ruff_cache",
          "tests",
          "**/__pycache__",
          "*.pyc",
        ],
        bundling: {
          image: lambda.Runtime.PYTHON_3_12.bundlingImage,
          local: pythonArm64LocalBundling(apiSource),
          command: [
            "bash",
            "-c",
            "pip install '.[aws]' -t /asset-output",
          ],
        },
      },
    );

    const authorizer = new lambda.Function(this, "RuntimeAuthorizer", {
      functionName: `agora-runtime-authorizer-${props.stage}`,
      runtime: lambda.Runtime.PYTHON_3_12,
      architecture: lambda.Architecture.ARM_64,
      handler: "agora.runtime_authorizer.handler",
      code,
      role: props.apiExecutionRole,
      memorySize: 512,
      timeout: cdk.Duration.seconds(30),
      environment: {
        AGORA_ROLE: "lambda",
        AGORA_STAGE: props.stage,
        AGORA_REGION: "us-east-1",
        AGORA_REGISTRY_ID: props.registryId,
        // CA-05/ADR-0015: 컷오버 전엔 bedrock-agentcore(기본). 이관 완료 후 agent-registry로
        // 전환하려면 이 env를 바꿔 이 스택을 재배포해요.
        AGORA_REGISTRY_NAMESPACE: props.registryNamespace,
        AGORA_TABLE_NAME: props.catalogTable.tableName,
        AGORA_BUCKET_NAME: props.artifactsBucket.bucketName,
        AGORA_SOURCE_REGION: this.region,
        AGORA_IDENTITY_TABLE: props.identityDataTable.tableName,
        AGORA_IDENTITY_REGION: this.region,
        AGORA_DEPLOY_REGION: this.region,
        AGORA_DEPLOY_COGNITO_DISCOVERY_URL:
          props.workloadCognitoDiscoveryUrl,
        AGORA_DEPLOY_COGNITO_CLIENT_ID: props.workloadCognitoClientId,
        AGORA_DEPLOY_COGNITO_SCOPE: props.workloadCognitoScope,
        AGORA_POLLER_ENABLED: "0",
        // 런타임 tool 인가 모델(IA-19): agent_policy(policy+identity, 기본) | legacy_delegated(3중 체크).
        AGORA_AUTHORIZATION_MODE: "agent_policy",
      },
    });
    const functionUrl = authorizer.addFunctionUrl({
      authType: lambda.FunctionUrlAuthType.AWS_IAM,
      invokeMode: lambda.InvokeMode.BUFFERED,
    });
    // AWS_IAM edge와 account-only resource policy를 함께 요구해요. NONE은 서명된
    // 요청도 anonymous로 취급해 account principal과 매칭되지 않으므로 사용할 수 없어요.
    const urlPermission = new lambda.CfnPermission(
      this,
      "RuntimeAuthorizationInvokeFunctionUrlPermission",
      {
        action: "lambda:InvokeFunctionUrl",
        functionName: authorizer.functionArn,
        principal: this.account,
        functionUrlAuthType: lambda.FunctionUrlAuthType.AWS_IAM,
      },
    );
    const invokePermission = new lambda.CfnPermission(
      this,
      "RuntimeAuthorizationInvokeFunctionPermission",
      {
        action: "lambda:InvokeFunction",
        functionName: authorizer.functionArn,
        principal: this.account,
        invokedViaFunctionUrl: true,
      },
    );
    // logical ID를 바꿔 과거 NONE permission을 강제로 교체해요.
    urlPermission.overrideLogicalId("RuntimeAuthorizationInvokeFunctionUrlV5");
    invokePermission.overrideLogicalId("RuntimeAuthorizationInvokeFunctionV5");
    this.authorizationUrl = `${functionUrl.url}internal/authorization/decide`;

    new cdk.CfnOutput(this, "RuntimeAuthorizationUrl", {
      value: this.authorizationUrl,
    });
  }
}
