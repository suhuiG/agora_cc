import * as ecr from "aws-cdk-lib/aws-ecr";
import * as iam from "aws-cdk-lib/aws-iam";
import * as s3 from "aws-cdk-lib/aws-s3";
import { Stack } from "aws-cdk-lib";
import { Construct } from "constructs";

/**
 * Runtime 역할의 최대 권한 상한선.
 *
 * IAM managed policy 이름은 계정 전체에서 유일해야 하므로 명시하지 않아요. 같은 stage를
 * 다른 리전에 배포해도 충돌하지 않게 CloudFormation 생성 이름을 사용하고 ARN을 output으로
 * 노출합니다. stack 이름에 stage가 포함돼 생성 이름도 stage별로 구분돼요.
 */
export function createRuntimePermissionBoundary(
  scope: Construct,
  stage: string,
  repo: ecr.Repository,
  artifactBucket: s3.Bucket,
  builtinRecordingBucket: s3.Bucket,
): iam.ManagedPolicy {
  return createPermissionBoundary(
    scope,
    "RuntimePermissionBoundary",
    `Maximum permissions for Agora runtime execution roles (${stage}).`,
    repo,
    artifactBucket,
    builtinRecordingBucket,
    false,
  );
}

export function createIsolatedAgentPermissionBoundary(
  scope: Construct,
  stage: string,
  repo: ecr.Repository,
  artifactBucket: s3.Bucket,
  builtinRecordingBucket: s3.Bucket,
): iam.ManagedPolicy {
  return createPermissionBoundary(
    scope,
    "IsolatedAgentPermissionBoundary",
    `Maximum permissions for isolated Agora agent roles (${stage}).`,
    repo,
    artifactBucket,
    builtinRecordingBucket,
    true,
  );
}

function createPermissionBoundary(
  scope: Construct,
  id: string,
  description: string,
  repo: ecr.Repository,
  artifactBucket: s3.Bucket,
  builtinRecordingBucket: s3.Bucket,
  isolateAgentResources: boolean,
): iam.ManagedPolicy {
  const stack = Stack.of(scope);
  const regionalAccountCondition = {
    StringEquals: {
      "aws:RequestedRegion": stack.region,
      "aws:PrincipalAccount": stack.account,
    },
  };
  const isolatedResourceCondition = {
    StringEquals: {
      "aws:ResourceTag/agora:record-id":
        "${aws:PrincipalTag/agora:record-id}",
      "aws:RequestedRegion": stack.region,
      "aws:PrincipalAccount": stack.account,
    },
  };
  const agentDataPlaneCondition = isolateAgentResources
    ? isolatedResourceCondition
    : regionalAccountCondition;

  return new iam.ManagedPolicy(scope, id, {
    description,
    document: new iam.PolicyDocument({
      statements: [
        new iam.PolicyStatement({
          sid: "DenyPrivilegeEscalationAndDestruction",
          effect: iam.Effect.DENY,
          actions: [
            "iam:*",
            "organizations:*",
            "sts:AssumeRole",
            // IAM action grammar does not permit a wildcard service namespace
            // ("*:Delete*" is invalid), so destructive families are enumerated.
            "bedrock:Delete*",
            "bedrock-agentcore:Delete*",
            "cloudformation:Delete*",
            "codebuild:Delete*",
            "cognito-idp:Delete*",
            "dynamodb:Delete*",
            "ec2:Delete*",
            "ecr:Delete*",
            "ecs:Delete*",
            "events:Delete*",
            "lambda:Delete*",
            "logs:Delete*",
            "s3:Delete*",
            "secretsmanager:Delete*",
            "states:Delete*",
            "kms:ScheduleKeyDeletion",
          ],
          resources: ["*"],
        }),
        new iam.PolicyStatement({
          sid: "AllowRuntimeLogs",
          actions: [
            "logs:CreateLogGroup",
            "logs:CreateLogStream",
            "logs:PutLogEvents",
          ],
          resources: [
            `arn:${stack.partition}:logs:${stack.region}:${stack.account}:log-group:*`,
          ],
          conditions: regionalAccountCondition,
        }),
        new iam.PolicyStatement({
          sid: "AllowRuntimeEcrPull",
          actions: [
            "ecr:BatchCheckLayerAvailability",
            "ecr:GetDownloadUrlForLayer",
            "ecr:BatchGetImage",
          ],
          resources: [repo.repositoryArn],
          conditions: regionalAccountCondition,
        }),
        new iam.PolicyStatement({
          sid: "AllowResourceIndependentRegionalCalls",
          actions: [
            "ecr:GetAuthorizationToken",
            "xray:PutTraceSegments",
            "xray:PutTelemetryRecords",
          ],
          // These APIs do not support resource-level permissions.
          resources: ["*"],
          conditions: regionalAccountCondition,
        }),
        new iam.PolicyStatement({
          sid: "AllowBedrockInferenceProfile",
          actions: [
            "bedrock:InvokeModel",
            "bedrock:InvokeModelWithResponseStream",
          ],
          resources: [
            `arn:${stack.partition}:bedrock:${stack.region}:${stack.account}:inference-profile/*`,
            `arn:${stack.partition}:bedrock:${stack.region}:${stack.account}:application-inference-profile/*`,
          ],
          conditions: regionalAccountCondition,
        }),
        new iam.PolicyStatement({
          // global/cross-region inference profile은 목적지 region으로 foundation-model을
          // 호출하므로 aws:RequestedRegion(=호출 region)을 걸면 안 돼요. account 스코프만 유지.
          sid: "AllowBedrockFoundationModel",
          actions: [
            "bedrock:InvokeModel",
            "bedrock:InvokeModelWithResponseStream",
          ],
          resources: [`arn:${stack.partition}:bedrock:*::foundation-model/*`],
          conditions: { StringEquals: { "aws:PrincipalAccount": stack.account } },
        }),
        new iam.PolicyStatement({
          sid: "AllowAgentCoreMemoryDataPlane",
          // GetEvent is not used by ordinary Strands 1.51 turns. DeleteEvent is
          // intentionally denied above; guardrail redaction and legacy Memory
          // migration remain unsupported until a human approves a narrow carveout.
          actions: [
            "bedrock-agentcore:CreateEvent",
            "bedrock-agentcore:ListEvents",
            "bedrock-agentcore:RetrieveMemoryRecords",
          ],
          resources: [
            `arn:${stack.partition}:bedrock-agentcore:${stack.region}:${stack.account}:memory/*`,
          ],
          conditions: agentDataPlaneCondition,
        }),
        new iam.PolicyStatement({
          sid: "AllowAgentCoreBuiltinDataPlane",
          actions: [
            "bedrock-agentcore:StartBrowserSession",
            "bedrock-agentcore:ConnectBrowserAutomationStream",
            "bedrock-agentcore:StopBrowserSession",
            "bedrock-agentcore:StartCodeInterpreterSession",
            "bedrock-agentcore:InvokeCodeInterpreter",
            "bedrock-agentcore:StopCodeInterpreterSession",
          ],
          // CUSTOM 내장 도구 ARN 은 `-custom` 리소스 타입이에요(실측 2026-08-21).
          // boundary 가 plain 형태만 허용하면 실행롤을 고쳐도 여전히 막혀요.
          resources: [
            `arn:${stack.partition}:bedrock-agentcore:${stack.region}:${stack.account}:browser/*`,
            `arn:${stack.partition}:bedrock-agentcore:${stack.region}:${stack.account}:browser-custom/*`,
            `arn:${stack.partition}:bedrock-agentcore:${stack.region}:${stack.account}:code-interpreter/*`,
            `arn:${stack.partition}:bedrock-agentcore:${stack.region}:${stack.account}:code-interpreter-custom/*`,
          ],
          conditions: agentDataPlaneCondition,
        }),
        new iam.PolicyStatement({
          sid: "AllowArtifactRead",
          actions: ["s3:GetObject*", "s3:GetBucket*", "s3:List*"],
          resources: [artifactBucket.bucketArn, `${artifactBucket.bucketArn}/*`],
          conditions: regionalAccountCondition,
        }),
        new iam.PolicyStatement({
          sid: "AllowBuiltinRecordingBucket",
          actions: [
            "s3:GetObject",
            "s3:PutObject",
            "s3:AbortMultipartUpload",
            "s3:ListMultipartUploadParts",
            "s3:ListBucket",
            "s3:ListBucketMultipartUploads",
          ],
          resources: [
            builtinRecordingBucket.bucketArn,
            `${builtinRecordingBucket.bucketArn}/*`,
          ],
          conditions: regionalAccountCondition,
        }),
        new iam.PolicyStatement({
          sid: "AllowRuntimeLambdaInvocation",
          actions: ["lambda:InvokeFunction", "lambda:InvokeFunctionUrl"],
          resources: [
            `arn:${stack.partition}:lambda:${stack.region}:${stack.account}:function:*`,
          ],
          conditions: regionalAccountCondition,
        }),
        new iam.PolicyStatement({
          sid: "AllowCatalogRead",
          actions: ["dynamodb:Scan", "dynamodb:GetItem", "dynamodb:Query"],
          resources: [
            `arn:${stack.partition}:dynamodb:${stack.region}:${stack.account}:table/AgoraCatalog`,
            `arn:${stack.partition}:dynamodb:${stack.region}:${stack.account}:table/AgoraCatalog/index/*`,
          ],
          conditions: regionalAccountCondition,
        }),
        // AWS Agent Registry 읽기(CA-05 이후 카탈로그 SoT, ADR-0021). registry는 us-east-1이라
        // regionalAccountCondition(ap-northeast-2)을 쓸 수 없어요 — 리소스 ARN으로 us-east-1을
        // 고정하고 account 조건만 걸어요(read-only List/Get). boundary가 이걸 허용해야 역할
        // 정책의 agent-registry read가 유효해져요(effective = 정책 ∩ boundary).
        new iam.PolicyStatement({
          sid: "AllowRegistryRead",
          actions: [
            "agent-registry:ListRegistryRecords",
            "agent-registry:GetRegistryRecord",
          ],
          resources: [
            `arn:${stack.partition}:agent-registry:us-east-1:${stack.account}:registry/*`,
          ],
          conditions: {
            StringEquals: { "aws:PrincipalAccount": stack.account },
          },
        }),
        new iam.PolicyStatement({
          sid: "AllowAgentCoreIdentityTokens",
          actions: [
            "bedrock-agentcore:GetResourceOauth2Token",
            "bedrock-agentcore:GetWorkloadAccessToken",
          ],
          resources: [
            `arn:${stack.partition}:bedrock-agentcore:${stack.region}:${stack.account}:token-vault/default`,
            `arn:${stack.partition}:bedrock-agentcore:${stack.region}:${stack.account}:token-vault/default/oauth2credentialprovider/agora-agent-*`,
            `arn:${stack.partition}:bedrock-agentcore:${stack.region}:${stack.account}:workload-identity-directory/default`,
            `arn:${stack.partition}:bedrock-agentcore:${stack.region}:${stack.account}:workload-identity-directory/default/workload-identity/agora-agent-*`,
          ],
          conditions: regionalAccountCondition,
        }),
        new iam.PolicyStatement({
          sid: "AllowAgentCoreIdentitySecretRead",
          actions: ["secretsmanager:GetSecretValue"],
          resources: [
            `arn:${stack.partition}:secretsmanager:${stack.region}:${stack.account}:secret:bedrock-agentcore-identity!default/oauth2/agora-agent-*`,
          ],
          conditions: regionalAccountCondition,
        }),
      ],
    }),
  });
}
