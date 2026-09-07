import * as iam from "aws-cdk-lib/aws-iam";
import * as s3 from "aws-cdk-lib/aws-s3";
import { Stack } from "aws-cdk-lib";
import { Construct } from "constructs";
import { mcptestTableArnPattern } from "./mcptest-tables";

/**
 * 배포 산출물 호출 롤 2종(McpLambda 실행 + McpGateway 아웃바운드)의 권한 상한선.
 *
 * ## 왜 `RuntimePermissionBoundary` 를 같이 쓰지 않나
 *
 * 원래 네 역할이 한 boundary 를 공유했어요. 그런데 IAM managed policy 는 **6,144자**가
 * 상한이고, 공유 boundary 는 2026-08-30 실측으로 이미 **5,926자**였어요. 남은 218자로는
 * 문장 하나도 못 넣어서, mcptest 테이블 권한을 추가하자 배포가
 * `ServiceLimitExceeded: Cannot exceed quota for PolicySize: 6144` 로 깨졌어요.
 *
 * 공유 boundary 가 큰 이유는 **agent runtime 전용** 허용이 대부분이라서예요 — Bedrock
 * 추론, AgentCore Memory 데이터플레인, browser·code-interpreter 세션, 녹화 버킷,
 * AgentCore Identity 토큰. 배포된 MCP Lambda 는 그중 아무것도 안 써요 — `sample/` 아래
 * 세 MCP 의 boto3 클라이언트는 `dynamodb` 와 `ssm` 뿐이에요.
 *
 * 그래서 상한선을 경로별로 갈라요. 이건 바이트를 아끼려는 우회가 아니라, AGENTS.md 의
 * "각 역할을 분리해 경로별 권한을 독립 감사해요" 를 실제로 지키는 형태예요. agent 쪽
 * boundary 두 개는 **손대지 않아요** — 보안에 민감한 공유 코드의 diff 를 0 으로 유지해요.
 *
 * ## DynamoDB 삭제를 어떻게 다루나
 *
 * 공유 boundary 는 `dynamodb:Delete*` 를 리소스 무관하게 거부해요. 의도는 인프라 파괴
 * 금지인데(형제 항목이 전부 control-plane), 와일드카드가 **데이터 행 삭제**까지 삼켜서
 * 배포형 MCP 가 delete 도구를 영구히 제공할 수 없었어요. 여기서는 파괴형 이름만 열거해요.
 * `DeleteItem` 은 아래 `AllowMcpTestTables` 가 가리키는 테이블에서만 실제로 허용돼요
 * (boundary 는 천장이지 부여가 아니에요 — 부여는 `iam-roles.ts` 인라인 정책이에요).
 */
export function createBackendPermissionBoundary(
  scope: Construct,
  stage: string,
  artifactBucket: s3.Bucket,
): iam.ManagedPolicy {
  const stack = Stack.of(scope);
  const regionalAccountCondition = {
    StringEquals: {
      "aws:RequestedRegion": stack.region,
      "aws:PrincipalAccount": stack.account,
    },
  };

  return new iam.ManagedPolicy(scope, "BackendPermissionBoundary", {
    // roleName 과 같은 이유로 물리 이름을 지정하지 않아요(계정 전역 유일 제약).
    description:
      `Maximum permissions for Agora deployed-artifact roles (${stage}).`,
    document: new iam.PolicyDocument({
      statements: [
        new iam.PolicyStatement({
          sid: "DenyPrivilegeEscalationAndDestruction",
          effect: iam.Effect.DENY,
          actions: [
            "iam:*",
            "organizations:*",
            "sts:AssumeRole",
            // IAM action 문법은 서비스 네임스페이스 와일드카드를 허용하지 않아요
            // ("*:Delete*" 는 무효), 그래서 파괴 계열을 서비스별로 열거해요.
            "bedrock:Delete*",
            "bedrock-agentcore:Delete*",
            "cloudformation:Delete*",
            "codebuild:Delete*",
            "cognito-idp:Delete*",
            // dynamodb 만 control-plane 이름을 열거해요 — 위 클래스 주석 참고.
            "dynamodb:DeleteTable",
            "dynamodb:DeleteTableReplica",
            "dynamodb:DeleteBackup",
            "dynamodb:DeleteResourcePolicy",
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
          // Lambda active tracing 이 켜지면 실행 롤로 세그먼트를 보내요. 리소스 단위
          // 권한을 지원하지 않는 API 라 `*` 예요(공유 boundary 와 같은 이유).
          sid: "AllowXRayTracing",
          actions: ["xray:PutTraceSegments", "xray:PutTelemetryRecords"],
          resources: ["*"],
          conditions: regionalAccountCondition,
        }),
        new iam.PolicyStatement({
          sid: "AllowArtifactRead",
          actions: ["s3:GetObject*", "s3:GetBucket*", "s3:List*"],
          resources: [artifactBucket.bucketArn, `${artifactBucket.bucketArn}/*`],
          conditions: regionalAccountCondition,
        }),
        new iam.PolicyStatement({
          // McpGatewayExecRole 이 Gateway → 백엔드 Lambda 아웃바운드에 써요.
          sid: "AllowRuntimeLambdaInvocation",
          actions: ["lambda:InvokeFunction", "lambda:InvokeFunctionUrl"],
          resources: [
            `arn:${stack.partition}:lambda:${stack.region}:${stack.account}:function:*`,
          ],
          conditions: regionalAccountCondition,
        }),
        new iam.PolicyStatement({
          // 모델 C(docs/agent-governance-05-credential-brokerage.md) — 배포된 MCP 가
          // 사용자 키 대신 이 롤로 카탈로그를 읽어요. 공용 롤이라 읽기만 줘요.
          sid: "AllowCatalogRead",
          actions: ["dynamodb:Scan", "dynamodb:GetItem", "dynamodb:Query"],
          resources: [
            `arn:${stack.partition}:dynamodb:${stack.region}:${stack.account}:table/AgoraCatalog`,
            `arn:${stack.partition}:dynamodb:${stack.region}:${stack.account}:table/AgoraCatalog/index/*`,
          ],
          conditions: regionalAccountCondition,
        }),
        new iam.PolicyStatement({
          // CS 핸즈온 데모 MCP 세 개의 데이터 테이블. 카탈로그와 달리 쓰기까지 올려요 —
          // 도구에 create/update/delete 가 있어요. 대상은 ARN 하나로 두고(천장이라 거칠어도
          // 안전해요), 정확한 열거는 `iam-roles.ts` 의 인라인 정책이 담당해요.
          sid: "AllowMcpTestTables",
          actions: [
            "dynamodb:Scan",
            "dynamodb:GetItem",
            "dynamodb:Query",
            "dynamodb:PutItem",
            "dynamodb:UpdateItem",
            "dynamodb:DeleteItem",
          ],
          resources: [mcptestTableArnPattern(stack, stage)],
          conditions: regionalAccountCondition,
        }),
        new iam.PolicyStatement({
          // CA-05 이후 자산 core 필드의 SoT. registry 는 us-east-1 고정이라
          // aws:RequestedRegion 을 걸 수 없어요 — ARN 으로 리전을 못박고 account 만 조건.
          sid: "AllowRegistryRead",
          actions: [
            "agent-registry:ListRegistryRecords",
            "agent-registry:GetRegistryRecord",
          ],
          resources: [
            `arn:${stack.partition}:agent-registry:us-east-1:${stack.account}:registry/*`,
          ],
          conditions: { StringEquals: { "aws:PrincipalAccount": stack.account } },
        }),
      ],
    }),
  });
}
