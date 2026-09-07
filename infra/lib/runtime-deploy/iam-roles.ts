import * as ecr from "aws-cdk-lib/aws-ecr";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as iam from "aws-cdk-lib/aws-iam";
import { Stack } from "aws-cdk-lib";
import { Construct } from "constructs";
import { mcptestTableArns } from "./mcptest-tables";

/**
 * RuntimeDeploy IAM 역할 4종 — 두 함수로 나뉘어요.
 *   createRuntimeExecutionRoles: AgentCore Runtime이 assume하는 실행 롤 2종
 *     (McpRuntime=container/ECR+MCP, Agent=codezip A2A).
 *   createBackendRoles: 배포 산출물을 호출하는 롤 2종
 *     (McpLambda=배포된 MCP Lambda 실행, McpGateway=Gateway→Lambda 아웃바운드).
 * 각 역할을 분리해 경로별 권한을 독립 감사해요.
 *
 * W0 refactor: RuntimeDeployStack 생성자에서 추출했어요. construct들은 여전히
 * stack(scope) 직속으로 붙고, 스택은 원본과 동일한 순서로 헬퍼를 호출하므로
 * construct path·logical ID·synth 출력이 분리 전과 100% 동일해요(무교체 보장).
 */
export interface RuntimeExecutionRoles {
  readonly execRole: iam.Role;
  readonly agentExecRole: iam.Role;
  readonly agentSharedPolicy: iam.ManagedPolicy;
  readonly builtinToolExecRole: iam.Role;
}

export interface BackendRoles {
  readonly lambdaExecRole: iam.Role;
  readonly gatewayExecRole: iam.Role;
}

/** AgentCore Runtime 실행 롤 2종(McpRuntime + Agent). */
export function createRuntimeExecutionRoles(
  scope: Construct,
  stage: string,
  repo: ecr.Repository,
  artifactBucket: s3.Bucket,
  builtinRecordingBucket: s3.Bucket,
  permissionsBoundary: iam.IManagedPolicy,
): RuntimeExecutionRoles {
  const stack = Stack.of(scope);

  // Runtime 실행 롤 — bedrock-agentcore가 assume.
  const execRole = new iam.Role(scope, "McpRuntimeExecRole", {
    // roleName 미지정 — IAM은 리전 무관이라 고정 이름이면 us-east-1↔서울 스택이 충돌해요.
    // CDK 자동 생성 이름을 쓰고, 참조는 CfnOutput의 ARN으로 해요.
    assumedBy: new iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
    permissionsBoundary,
  });
  repo.grantPull(execRole);
  // 로그는 계정·리전 log group, Bedrock은 모델 리소스 유형으로 한정해요.
  execRole.addToPolicy(new iam.PolicyStatement({
    actions: ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
    resources: [
      `arn:${stack.partition}:logs:${stack.region}:${stack.account}:log-group:*`,
    ],
  }));
  execRole.addToPolicy(new iam.PolicyStatement({
    actions: ["xray:PutTraceSegments", "xray:PutTelemetryRecords"],
    // X-Ray write APIs do not support resource-level permissions.
    resources: ["*"],
    conditions: {
      StringEquals: {
        "aws:RequestedRegion": stack.region,
        "aws:PrincipalAccount": stack.account,
      },
    },
  }));
  // inference profile은 이 region 엔드포인트로 호출돼요 → RequestedRegion 유지.
  execRole.addToPolicy(new iam.PolicyStatement({
    actions: ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
    resources: [
      `arn:${stack.partition}:bedrock:${stack.region}:${stack.account}:inference-profile/*`,
      `arn:${stack.partition}:bedrock:${stack.region}:${stack.account}:application-inference-profile/*`,
    ],
    conditions: {
      StringEquals: {
        "aws:RequestedRegion": stack.region,
        "aws:PrincipalAccount": stack.account,
      },
    },
  }));
  // foundation-model: global/cross-region inference profile 라우팅용 — RequestedRegion 미적용
  // (목적지 region이 호출 region과 달라요). account 스코프(PrincipalAccount)만 유지.
  execRole.addToPolicy(new iam.PolicyStatement({
    actions: ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
    resources: [`arn:${stack.partition}:bedrock:*::foundation-model/*`],
    conditions: { StringEquals: { "aws:PrincipalAccount": stack.account } },
  }));
  // Lambda가 S3에서 artifact 읽기(CodeBuild 시 create_function S3Key 참조용).
  artifactBucket.grantRead(execRole);

  // ── Agent Runtime 실행 롤 (codezip A2A) ─────────────────────────────────
  // McpRuntimeExecRole(container/ECR+MCP)과 분리한 전용 롤이에요.
  // 이유: codezip agent는 ECR pull이 필요 없으므로 McpRuntimeExecRole의 ECR 권한은
  //   과잉 부여(least-privilege 위반)가 돼요. 역할 분리로 agent 경로 권한을 독립적으로
  //   감사할 수 있어요(McpRuntime/McpLambda/McpGateway 분리와 같은 맥락).
  const agentExecRole = new iam.Role(scope, "AgentRuntimeExecRole", {
    // roleName 미지정 (리전 무관 충돌 방지 — 위 McpRuntimeExecRole 주석 참조).
    assumedBy: new iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
    permissionsBoundary,
    description: "Execution role for AgentCore Runtime (codezip A2A agent). " +
      "Distinct from McpRuntimeExecRole (container/ECR+MCP): no ECR pull, codezip only.",
  });
  // Deprecated compatibility role for runtimes deployed before IH-71. New and
  // redeployed agents use backend-created /agora/agent/* roles. Adding the tag
  // condition here would immediately break the existing untagged fleet.
  agentExecRole.addToPolicy(new iam.PolicyStatement({
    actions: ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
    resources: [
      `arn:${stack.partition}:logs:${stack.region}:${stack.account}:log-group:*`,
    ],
  }));
  agentExecRole.addToPolicy(new iam.PolicyStatement({
    actions: [
      "bedrock-agentcore:StartBrowserSession",
      "bedrock-agentcore:ConnectBrowserAutomationStream",
      "bedrock-agentcore:StopBrowserSession",
      "bedrock-agentcore:StartCodeInterpreterSession",
      "bedrock-agentcore:InvokeCodeInterpreter",
      "bedrock-agentcore:StopCodeInterpreterSession",
    ],
    // CUSTOM built-in tool ARNs use the `-custom` resource type, not the plain
    // one. Measured 2026-08-21 (dev/ap-northeast-2):
    //   arn:aws:bedrock-agentcore:…:browser-custom/weather_c3_sum_br_brows_…
    //   arn:aws:bedrock-agentcore:…:code-interpreter-custom/weather_c2h_sem_ci_code__…
    // Granting only `browser/*` + `code-interpreter/*` means an agent can never
    // start a session on the CUSTOM resource Agora just created for it — the
    // agent replied "StartBrowserSession 권한이 없어요" while the deploy reported
    // the tool as wired. Both forms are granted: `-custom` for per-agent
    // resources and the plain form for AWS-managed defaults.
    resources: [
      `arn:${stack.partition}:bedrock-agentcore:${stack.region}:${stack.account}:browser/*`,
      `arn:${stack.partition}:bedrock-agentcore:${stack.region}:${stack.account}:browser-custom/*`,
      `arn:${stack.partition}:bedrock-agentcore:${stack.region}:${stack.account}:code-interpreter/*`,
      `arn:${stack.partition}:bedrock-agentcore:${stack.region}:${stack.account}:code-interpreter-custom/*`,
    ],
    conditions: {
      StringEquals: {
        "aws:RequestedRegion": stack.region,
        "aws:PrincipalAccount": stack.account,
      },
    },
  }));
  agentExecRole.addToPolicy(new iam.PolicyStatement({
    actions: ["xray:PutTraceSegments", "xray:PutTelemetryRecords"],
    // X-Ray write APIs do not support resource-level permissions.
    resources: ["*"],
    conditions: {
      StringEquals: {
        "aws:RequestedRegion": stack.region,
        "aws:PrincipalAccount": stack.account,
      },
    },
  }));
  // inference profile은 이 region 엔드포인트로 호출돼요 → RequestedRegion 유지.
  agentExecRole.addToPolicy(new iam.PolicyStatement({
    actions: ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
    resources: [
      `arn:${stack.partition}:bedrock:${stack.region}:${stack.account}:inference-profile/*`,
      `arn:${stack.partition}:bedrock:${stack.region}:${stack.account}:application-inference-profile/*`,
    ],
    conditions: {
      StringEquals: {
        "aws:RequestedRegion": stack.region,
        "aws:PrincipalAccount": stack.account,
      },
    },
  }));
  // foundation-model: global/cross-region inference profile은 목적지 region으로 라우팅해
  // foundation-model을 호출하므로 aws:RequestedRegion(=호출 region)을 걸면 목적지≠호출
  // region일 때 거부돼요. AWS-owned 리소스라 account 스코프(PrincipalAccount)만 유지해요.
  agentExecRole.addToPolicy(new iam.PolicyStatement({
    actions: ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
    resources: [`arn:${stack.partition}:bedrock:*::foundation-model/*`],
    conditions: { StringEquals: { "aws:PrincipalAccount": stack.account } },
  }));
  agentExecRole.addToPolicy(new iam.PolicyStatement({
    // Strands 1.51 ordinary turns use these three actions. GetEvent is confined
    // to read_message(), which that flow does not call. DeleteEvent is confined
    // to unsupported guardrail redaction and legacy Memory migration paths.
    actions: [
      "bedrock-agentcore:CreateEvent",
      "bedrock-agentcore:ListEvents",
      "bedrock-agentcore:RetrieveMemoryRecords",
    ],
    resources: [
      `arn:${stack.partition}:bedrock-agentcore:${stack.region}:${stack.account}:memory/*`,
    ],
    conditions: {
      StringEquals: {
        "aws:RequestedRegion": stack.region,
        "aws:PrincipalAccount": stack.account,
      },
    },
  }));
  // AgentCore Runtime이 S3에서 codezip artifact를 읽어요.
  artifactBucket.grantRead(agentExecRole);
  const runtimeAuthorizerArn =
    `arn:aws:lambda:${stack.region}:${stack.account}:` +
    `function:agora-runtime-authorizer-${stage}`;
  // Runtime authorization URL은 account principal로 닫혀 있어 IAM 서명이 필수예요.
  // 실행 역할에는 이 stage의 authorizer Function URL 호출만 허용합니다.
  agentExecRole.addToPolicy(new iam.PolicyStatement({
    actions: ["lambda:InvokeFunctionUrl"],
    resources: [runtimeAuthorizerArn],
    conditions: {
      StringEquals: { "lambda:FunctionUrlAuthType": "AWS_IAM" },
    },
  }));
  agentExecRole.addToPolicy(new iam.PolicyStatement({
    actions: ["lambda:InvokeFunction"],
    resources: [runtimeAuthorizerArn],
    conditions: {
      Bool: { "lambda:InvokedViaFunctionUrl": "true" },
    },
  }));

  // ── Identity P2: 배포 agent의 outbound Gateway 호출 토큰 ────────────────
  // 배포된 agent가 Cognito 보호 MCP Gateway를 부르려면 Bearer가 필요해요. secret을
  // 컨테이너에 두지 않고 Token Vault에 맡기는 방식이라, 컨테이너는 두 API만 호출해요:
  //   GetWorkloadAccessToken(우리 소유 workload identity) → GetResourceOauth2Token(M2M).
  // workload identity를 따로 만드는 이유: Runtime이 자동 생성하는 identity는
  // service-linked라 GetWorkloadAccessToken이 ValidationException이에요(실측 2026-07-27).
  agentExecRole.addToPolicy(new iam.PolicyStatement({
    actions: [
      "bedrock-agentcore:GetResourceOauth2Token",
      "bedrock-agentcore:GetWorkloadAccessToken",
    ],
    resources: [
      `arn:aws:bedrock-agentcore:${stack.region}:${stack.account}:token-vault/default`,
      `arn:aws:bedrock-agentcore:${stack.region}:${stack.account}:token-vault/default/oauth2credentialprovider/agora-agent-*`,
      `arn:aws:bedrock-agentcore:${stack.region}:${stack.account}:workload-identity-directory/default`,
      `arn:aws:bedrock-agentcore:${stack.region}:${stack.account}:workload-identity-directory/default/workload-identity/agora-agent-*`,
    ],
  }));
  // Token Vault가 provider secret을 Secrets Manager에 두므로 읽기 권한이 필요해요.
  agentExecRole.addToPolicy(new iam.PolicyStatement({
    actions: ["secretsmanager:GetSecretValue"],
    resources: [
      `arn:aws:secretsmanager:${stack.region}:${stack.account}:secret:bedrock-agentcore-identity!default/oauth2/agora-agent-*`,
    ],
  }));

  const isolatedResourceCondition = {
    StringEquals: {
      // IAM policy variable: keep this as an ordinary string so TypeScript does
      // not interpolate it. Access Analyzer currently reports this condition as
      // unsupported, but P2/P3/P5/P6 live probes proved service enforcement.
      "aws:ResourceTag/agora:record-id":
        "${aws:PrincipalTag/agora:record-id}",
      "aws:RequestedRegion": stack.region,
      "aws:PrincipalAccount": stack.account,
    },
  };
  const regionalAccountCondition = {
    StringEquals: {
      "aws:RequestedRegion": stack.region,
      "aws:PrincipalAccount": stack.account,
    },
  };
  const agentSharedPolicy = new iam.ManagedPolicy(
    scope,
    "AgentRuntimeSharedPolicy",
    {
      description:
        `Shared least-privilege policy for isolated Agora agent roles (${stage}).`,
      document: new iam.PolicyDocument({
        statements: [
          new iam.PolicyStatement({
            sid: "RuntimeLogs",
            actions: [
              "logs:CreateLogGroup",
              "logs:CreateLogStream",
              "logs:PutLogEvents",
            ],
            resources: [
              `arn:${stack.partition}:logs:${stack.region}:${stack.account}:log-group:*`,
            ],
          }),
          new iam.PolicyStatement({
            sid: "AgentOwnedBuiltinTools",
            actions: [
              "bedrock-agentcore:StartBrowserSession",
              "bedrock-agentcore:ConnectBrowserAutomationStream",
              "bedrock-agentcore:StopBrowserSession",
              "bedrock-agentcore:StartCodeInterpreterSession",
              "bedrock-agentcore:InvokeCodeInterpreter",
              "bedrock-agentcore:StopCodeInterpreterSession",
            ],
            resources: [
              `arn:${stack.partition}:bedrock-agentcore:${stack.region}:${stack.account}:browser-custom/*`,
              `arn:${stack.partition}:bedrock-agentcore:${stack.region}:${stack.account}:code-interpreter-custom/*`,
            ],
            conditions: isolatedResourceCondition,
          }),
          new iam.PolicyStatement({
            sid: "RuntimeTelemetry",
            actions: ["xray:PutTraceSegments", "xray:PutTelemetryRecords"],
            resources: ["*"],
            conditions: regionalAccountCondition,
          }),
          new iam.PolicyStatement({
            sid: "BedrockInferenceProfiles",
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
            sid: "BedrockFoundationModels",
            actions: [
              "bedrock:InvokeModel",
              "bedrock:InvokeModelWithResponseStream",
            ],
            resources: [
              `arn:${stack.partition}:bedrock:*::foundation-model/*`,
            ],
            conditions: {
              StringEquals: { "aws:PrincipalAccount": stack.account },
            },
          }),
          new iam.PolicyStatement({
            sid: "AgentOwnedMemory",
            actions: [
              "bedrock-agentcore:CreateEvent",
              "bedrock-agentcore:ListEvents",
              "bedrock-agentcore:RetrieveMemoryRecords",
            ],
            resources: [
              `arn:${stack.partition}:bedrock-agentcore:${stack.region}:${stack.account}:memory/*`,
            ],
            conditions: isolatedResourceCondition,
          }),
          new iam.PolicyStatement({
            sid: "ArtifactRead",
            actions: ["s3:GetObject*", "s3:GetBucket*", "s3:List*"],
            resources: [artifactBucket.bucketArn, `${artifactBucket.bucketArn}/*`],
            conditions: regionalAccountCondition,
          }),
          new iam.PolicyStatement({
            sid: "RuntimeAuthorizerUrl",
            actions: ["lambda:InvokeFunctionUrl"],
            resources: [runtimeAuthorizerArn],
            conditions: {
              StringEquals: { "lambda:FunctionUrlAuthType": "AWS_IAM" },
            },
          }),
          new iam.PolicyStatement({
            sid: "RuntimeAuthorizerFunction",
            actions: ["lambda:InvokeFunction"],
            resources: [runtimeAuthorizerArn],
            conditions: {
              Bool: { "lambda:InvokedViaFunctionUrl": "true" },
            },
          }),
        ],
      }),
    },
  );

  const builtinToolExecRole = new iam.Role(scope, "BuiltinToolExecRole", {
    assumedBy: new iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
    permissionsBoundary,
    description: "Execution role for per-agent Browser recording and builtin tool logs.",
  });
  builtinToolExecRole.addToPolicy(new iam.PolicyStatement({
    actions: [
      "s3:GetObject",
      "s3:PutObject",
      "s3:AbortMultipartUpload",
      "s3:ListMultipartUploadParts",
    ],
    resources: [`${builtinRecordingBucket.bucketArn}/*`],
  }));
  builtinToolExecRole.addToPolicy(new iam.PolicyStatement({
    actions: ["s3:ListBucket", "s3:ListBucketMultipartUploads"],
    resources: [builtinRecordingBucket.bucketArn],
  }));
  builtinToolExecRole.addToPolicy(new iam.PolicyStatement({
    actions: ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
    resources: [
      `arn:${stack.partition}:logs:${stack.region}:${stack.account}:log-group:*`,
    ],
  }));

  return { execRole, agentExecRole, agentSharedPolicy, builtinToolExecRole };
}

/** 배포 산출물 호출 롤 2종(McpLambda 실행 + McpGateway 아웃바운드). */
export function createBackendRoles(
  scope: Construct,
  stage: string,
  artifactBucket: s3.Bucket,
  permissionsBoundary: iam.IManagedPolicy,
): BackendRoles {
  const stack = Stack.of(scope);

  // ── Lambda 실행 롤 ────────────────────────────────────────────────────
  // 배포된 MCP Lambda 함수가 사용하는 롤이에요.
  // 주의: McpRuntimeExecRole(bedrock-agentcore trust)과는 별개예요.
  //   - 이 롤(LambdaExecRole)은 lambda.amazonaws.com이 assume.
  //   - Lambda create_function/delete_function 등 관리 권한은 FastAPI 실행
  //     자격(AwsDeployAdapter가 사용하는 자격)에 부여해야 해요. 여기선
  //     롤 ARN을 CfnOutput으로만 노출하고 문서화해요(Task 10 게이트).
  const lambdaExecRole = new iam.Role(scope, "McpLambdaExecRole", {
    // roleName 미지정 (리전 무관 충돌 방지).
    assumedBy: new iam.ServicePrincipal("lambda.amazonaws.com"),
    managedPolicies: [
      iam.ManagedPolicy.fromAwsManagedPolicyName("service-role/AWSLambdaBasicExecutionRole"),
    ],
    permissionsBoundary,
    description: "Execution role for deployed MCP Lambda functions (logs only at MVP; scoped perms added per source)",
  });
  // 필요 시 Lambda가 artifact 버킷에서 코드를 읽을 수 있어요.
  artifactBucket.grantRead(lambdaExecRole);

  // ── AgoraCatalog 읽기 (모델 C: 자격증명을 코드에 담지 않기) ─────────────
  // 배포된 MCP가 AWS 리소스에 접근할 때 사용자 키를 받지 않고 이 롤로 해결해요
  // (docs/agent-governance-05-credential-brokerage.md 모델 C).
  // 계기: sample/catalog-ddb-mcp가 하드코딩 더미 키로 DynamoDB를 호출하다
  // THREAT_UNSAFE_CREDENTIAL 판정을 받았어요. 키를 제거한 뒤 boto3 표준
  // 자격증명 체인을 쓰므로, 배포 환경에서는 이 롤이 권한을 제공해야 해요.
  //
  // 한계(의도적): 롤은 자산 단위가 아니라 공용이라, 배포된 모든 MCP가 카탈로그를
  // 읽을 수 있어요. 자산별 권한 분리는 후속 과제로 같은 문서 §6에 남겨뒀어요.
  // 그래서 읽기(Scan/GetItem/Query)만 주고 쓰기는 주지 않아요.
  lambdaExecRole.addToPolicy(new iam.PolicyStatement({
    actions: ["dynamodb:Scan", "dynamodb:GetItem", "dynamodb:Query"],
    resources: [
      `arn:aws:dynamodb:${stack.region}:${stack.account}:table/AgoraCatalog`,
      `arn:aws:dynamodb:${stack.region}:${stack.account}:table/AgoraCatalog/index/*`,
    ],
  }));

  // ── CS 핸즈온 mcptest 테이블 CRUD (모델 C) ─────────────────────────────
  //
  // `sample/user-table-mcp`·`item-table-mcp`·`order-table-mcp` 가 이 테이블을 읽고 써요.
  // 권한이 없어서 도구 호출이 `AccessDeniedException` 으로 죽었어요(2026-08-30 실측:
  // 인가·전달은 전부 통과했는데 마지막에 DynamoDB 에서 막혔어요).
  //
  // **읽기만으로는 부족해요** — 세 MCP 는 create/update/delete 도구를 갖고 있어요. 대신
  // 테이블은 세 개로 **정확히 열거**하고 와일드카드를 쓰지 않아요(`mcptest-tables.ts`).
  //
  // 이것만으로는 부족해요 — 유효 권한은 **정책 ∩ boundary** 라서 boundary 쪽
  // `AllowMcpTestTables` 도 같이 있어야 해요. 인라인만 추가하고 배포했더니 여전히
  // `AccessDeniedException` 이 났어요(같은 날 실측).
  //
  // 한계(위 AgoraCatalog 와 같아요): 이 롤은 자산 단위가 아니라 공용이라, 배포된 **모든**
  // MCP 가 이 테이블을 쓸 수 있어요. 자산별 분리는
  // `docs/agent-governance-05-credential-brokerage.md` §6 의 후속 과제예요.
  lambdaExecRole.addToPolicy(new iam.PolicyStatement({
    sid: "McpTestTablesCrud",
    actions: [
      "dynamodb:Scan",
      "dynamodb:GetItem",
      "dynamodb:Query",
      "dynamodb:PutItem",
      "dynamodb:UpdateItem",
      "dynamodb:DeleteItem",
    ],
    resources: mcptestTableArns(stack, stage),
  }));

  // ── AWS Agent Registry 읽기 (모델 C, CA-05 이후 카탈로그 SoT) ──────────
  // CA-05 마이그레이션으로 자산 core 필드(name/type/status)의 SoT가 AgoraCatalog DDB →
  // AWS Agent Registry로 이동했어요. sample/catalog-ddb-mcp가 registry를 직접 조회하므로
  // (ADR-0021 후속), 배포된 MCP Lambda 롤에 read-only(List/Get)를 부여해요. registry는
  // us-east-1(AgentCore GA 리전)이라 리전을 ARN으로 고정해요(catalog-storage-stack의 API 롤과 동일).
  lambdaExecRole.addToPolicy(new iam.PolicyStatement({
    actions: [
      "agent-registry:ListRegistryRecords",
      "agent-registry:GetRegistryRecord",
    ],
    resources: [
      // partition을 boundary(permission-boundary.ts)와 일치시켜요 — 안 그러면 비표준 파티션
      // (aws-cn/aws-us-gov)에서 정책 ∩ boundary 교집합이 비어요(리뷰 지적).
      `arn:${stack.partition}:agent-registry:us-east-1:${stack.account}:registry/*`,
    ],
  }));

  // ── Gateway 전용 실행 롤 ─────────────────────────────────────────────
  // BedrockAgentCore Gateway가 아웃바운드로 백엔드 Lambda를 호출할 때
  // assume하는 롤이에요. McpRuntimeExecRole(container-runtime 권한)과
  // 책임을 분리해요.
  //
  // Task-10 검증 전제 (2가지):
  //   (a) 서비스 프린시팔: "bedrock-agentcore.amazonaws.com" — Gateway는
  //       AgentCore 제품군의 일부이므로 동일 프린시팔을 사용해요.
  //       실제 CloudFormation assume 실패 시 Task 10 게이트에서 수정해요.
  //   (b) Lambda ARN 패턴: jobs.py의 create_lambda 호출은 name = job.meta["name"]
  //       또는 job_id를 사용하므로 신뢰할 수 있는 고정 접두어가 없어요. 사용자 지정
  //       이름을 깨지 않도록 계정·리전 스코프를 유지하고 permission boundary가 IAM,
  //       조직 변경, AssumeRole, 삭제 작업을 상한선에서 차단해요.
  const gatewayExecRole = new iam.Role(scope, "McpGatewayExecRole", {
    // roleName 미지정 (리전 무관 충돌 방지).
    assumedBy: new iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
    permissionsBoundary,
    description: "Gateway execution role: assumed by BedrockAgentCore Gateway to invoke backend Lambda targets",
  });
  gatewayExecRole.addToPolicy(new iam.PolicyStatement({
    // Lambda 함수 이름은 job_id(UUID) 또는 meta.name(사용자 지정)이어서 prefix가 없어요.
    // 계정·리전 ARN과 boundary를 결합하는 것이 현재 naming contract의 최소 범위예요.
    actions: ["lambda:InvokeFunction"],
    resources: [`arn:aws:lambda:${stack.region}:${stack.account}:function:*`],
  }));

  return { lambdaExecRole, gatewayExecRole };
}
