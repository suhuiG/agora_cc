import * as cdk from "aws-cdk-lib";
import * as ecr from "aws-cdk-lib/aws-ecr";
import * as codebuild from "aws-cdk-lib/aws-codebuild";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as iam from "aws-cdk-lib/aws-iam";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import { Construct } from "constructs";
import { addToolingBundle, TOOLING_BUNDLE_PREFIX } from "./tooling-bundle";

const BEDROCK_AGENTCORE_QUALIFIED_VERSIONS = ["1.22.0"] as const;
const BEDROCK_AGENTCORE_QUALIFIED_VERSION_LIST =
  BEDROCK_AGENTCORE_QUALIFIED_VERSIONS.join(",");
const BEDROCK_AGENTCORE_VERSION_GATE = [
  `echo "bedrock-agentcore build gate actual=$BEDROCK_AGENTCORE_VERSION qualified=${BEDROCK_AGENTCORE_QUALIFIED_VERSION_LIST}";`,
  "python -c \"import sys;",
  `qualified={${BEDROCK_AGENTCORE_QUALIFIED_VERSIONS.map((version) => `'${version}'`).join(",")}};`,
  "(sys.argv[1] in qualified) or sys.exit(1)\" \"$BEDROCK_AGENTCORE_VERSION\"",
].join(" ");

/**
 * 빌드 파이프라인 구성물 — ECR(container 이미지) + artifact S3(codezip) +
 * CodeBuild(ARM64 빌드) + DeployJobs DDB.
 *
 * W0 refactor: RuntimeDeployStack 생성자에서 추출했어요. construct들은 여전히
 * stack(scope) 직속으로 붙어 construct path·logical ID·synth가 100% 동일해요
 * (별도 Construct로 감싸지 않는 이유 — logical ID 보존 = 기존 배포 무교체).
 */
export interface BuildPipeline {
  readonly repo: ecr.Repository;
  readonly artifactBucket: s3.Bucket;
  readonly project: codebuild.Project;
  readonly jobsTable: dynamodb.Table;
}

export function createBuildPipeline(
  scope: Construct,
  stage: string,
  removalPolicy: cdk.RemovalPolicy,
  isProd: boolean,
): BuildPipeline {
  // 배포된 MCP 이미지 저장소
  const repo = new ecr.Repository(scope, "McpImages", {
    repositoryName: `agora-mcp-runtime-${stage}`,
    removalPolicy, emptyOnDelete: !isProd,
  });

  // codezip 아티팩트 버킷
  const artifactBucket = new s3.Bucket(scope, "McpArtifacts", {
    encryption: s3.BucketEncryption.S3_MANAGED,
    blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
    enforceSSL: true,
    lifecycleRules: [{ expiration: cdk.Duration.days(30) }],
    removalPolicy, autoDeleteObjects: !isProd,
  });

  // CA-18: tool_extract·하네스 tooling 번들 **전용 버킷**. artifact 버킷과 분리한 이유는
  // artifact 버킷의 30일 lifecycle이 _tooling/ 번들까지 만료시켜(BucketDeployment는 콘텐츠
  // 변경 시에만 재업로드→self-heal 안 됨) 30일 후 모든 MCP 빌드가 깨지기 때문이에요(적대적
  // 리뷰 HIGH). 이 버킷은 lifecycle이 없어 번들이 영구 보존돼요.
  const toolingBucket = new s3.Bucket(scope, "ToolingBundleBucket", {
    encryption: s3.BucketEncryption.S3_MANAGED,
    blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
    enforceSSL: true,
    removalPolicy, autoDeleteObjects: !isProd,
  });
  // cdk deploy 마다 최신 api/src 코드로 번들을 갱신하고(BucketDeployment 콘텐츠 해시),
  // 반환된 버전 해시를 CodeBuild env로 배선해 빌드가 번들 무결성을 확인하게 해요.
  const toolingVersion = addToolingBundle(scope, toolingBucket);

  // ARM64 빌드 프로젝트 — Dockerfile 있으면 docker build→ECR push, 없으면 zip→S3.
  const project = new codebuild.Project(scope, "McpBuild", {
    projectName: `agora-mcp-build-${stage}`,
    environment: {
      // AL2023 aarch64 이미지 — Python 3.12 제공. AL2 standard 3.0은 최대 3.11이라
      // Lambda(python3.12)용 네이티브 wheel(pydantic_core cp312)을 못 만들어요
      // (실측: 2026-07-16 cp311 .so → Lambda ModuleNotFoundError). 빌드=런타임 3.12 정합.
      buildImage: codebuild.LinuxArmBuildImage.AMAZON_LINUX_2023_STANDARD_3_0,
      privileged: true,  // docker build용
      environmentVariables: {
        // container 분기에서 ECR push URI로 사용해요.
        ECR_URI: { value: repo.repositoryUri },
        // 하네스·agora tool_extract 모듈이 담긴 tooling 번들 S3 프리픽스(CA-18).
        // 번들엔 tool_extract·lambda_handler_template·models와 agora/ 패키지 __init__
        // 체인이 들어가요(전부 표준 라이브러리 의존 — agora 내부 무의존, 실측 확인).
        // addToolingBundle의 BucketDeployment가 cdk deploy 마다 이 프리픽스(_tooling/)에
        // 최신 번들을 언팩 상태로 올려요. install 단계가 여기서 recursive cp 로 받아요.
        AGORA_TOOLING_S3: {
          value: toolingBucket.s3UrlForObject(TOOLING_BUNDLE_PREFIX),
        },
        // 이 프로젝트가 배포된 시점의 번들 내용 해시(CA-18 무결성 확인). 빌드가 S3의
        // _tooling/VERSION 첫 줄과 비교해 불일치하면 옛 코드로 산출하기 전에 실패해요.
        AGORA_TOOLING_VERSION: { value: toolingVersion },
      },
    },
    buildSpec: codebuild.BuildSpec.fromObject({
      version: "0.2",
      env: {
        // ARTIFACT_KEY, TOOLS_S3_KEY, MCP_MODULE, OTEL_ENTRYPOINT_ENABLED는
        // aws_adapter.py get_build_status()가
        // exportedEnvironmentVariables에서 읽어요.
        //   - ARTIFACT_KEY : artifact.zip S3 key (Lambda Code S3Key).
        //   - TOOLS_S3_KEY : tools.json S3 key (I2 사이드카 — 어댑터가 S3에서 fetch).
        //                    TOOLS_INLINE(5120자 한계)를 대체해요.
        //   - MCP_MODULE   : FastMCP 인스턴스 모듈명 (C1 — Lambda AGORA_MCP_MODULE로 배선).
        "exported-variables": [
          "ARTIFACT_KEY",
          "TOOLS_S3_KEY",
          "MCP_MODULE",
          "OTEL_ENTRYPOINT_ENABLED",
        ],
      },
      phases: {
        install: {
          // 빌드 파이썬을 Lambda 런타임과 동일한 3.12로 고정 — 네이티브 wheel(cp312)이
          // Lambda에서 import 되게 해요(실측: 버전 불일치 시 pydantic_core ModuleNotFound).
          "runtime-versions": { python: "3.12" },
          commands: [
            // 하네스·tool_extract 주입 규약(C2):
            // agora_mcp_handler.py(하네스)와 agora/ 패키지(tool_extract 실행용)는
            // agora API 리포에 있어요. 이걸 빌드 샌드박스에 넣는 경로는 두 가지예요.
            //   (1) AGORA_TOOLING_S3(=_tooling/ 프리픽스)가 설정돼 있으면 그 프리픽스를
            //       /tmp/agora-tooling 로 recursive cp 해요(CA-18: BucketDeployment가 올린
            //       agora/ 레이아웃 + VERSION 을 언팩 상태로 받음).
            //   (2) CodeBuild secondary source(sourceIdentifier=agora)를 쓰면
            //       $CODEBUILD_SRC_DIR_agora 에 리포가 체크아웃돼요.
            // 둘 중 존재하는 쪽을 AGORA_TOOLING_DIR로 잡아요.
            "mkdir -p /tmp/agora-tooling",
            "if [ -n \"$AGORA_TOOLING_S3\" ]; then aws s3 cp \"$AGORA_TOOLING_S3/\" /tmp/agora-tooling/ --recursive; fi",
            // 업로드된 MCP 소스를 sourcestore(서울 S3)에서 작업 디렉터리로 내려받아요.
            // CodeBuild 프로젝트는 NO_SOURCE라 $CODEBUILD_SRC_DIR가 비어 있어요. sourcestore는
            // 서울(SOURCE_REGION)일 수 있어 --region을 명시해요(cross-region).
            "if [ -n \"$SOURCE_BUCKET\" ]; then aws s3 cp \"s3://$SOURCE_BUCKET/$SOURCE_S3_PREFIX\" \"$CODEBUILD_SRC_DIR/\" --recursive --region \"${SOURCE_REGION:-$AWS_DEFAULT_REGION}\"; fi",
            "echo source_downloaded to $CODEBUILD_SRC_DIR; ls -la \"$CODEBUILD_SRC_DIR\" | head",
          ],
        },
        pre_build: {
          commands: [
            "echo build_type=$BUILD_TYPE asset=$ASSET_ID version=$VERSION",
          ],
        },
        build: {
          // ⚠️ CodeBuild는 commands 배열의 각 항목을 개별 셸로 실행해요. 그래서 if 블록·
          //    export가 항목 경계를 넘으면 깨져요(실측: 2026-07-16 "if...then" line 9 syntax
          //    error). 전체 빌드를 하나의 항목(단일 bash 스크립트)으로 실행해 제어흐름·변수를
          //    보존하고, exported-variables는 $CODEBUILD_SRC_DIR/exports.env에 써서 마지막에
          //    한 번에 export해요(단일 셸이라 export가 다음 항목으로 안 새는 것도 방지).
          commands: [
            [
              "set -e",
              "EXPORTS=$CODEBUILD_SRC_DIR/exports.env",
              "> \"$EXPORTS\"",
              // 툴링 디렉터리 재설정 (install의 export는 이 단일 스크립트로 안 넘어와요).
              "if [ -n \"$CODEBUILD_SRC_DIR_agora\" ]; then export AGORA_TOOLING_DIR=\"$CODEBUILD_SRC_DIR_agora/api/src\"; else export AGORA_TOOLING_DIR=/tmp/agora-tooling; fi",
              // CA-18 번들 무결성 확인: 배포된 S3 번들(_tooling/VERSION)이 이 프로젝트가
              // 배포한 버전과 일치하는지 확인해요(불일치=부분/변조 업로드 방어). anti-staleness
              // 자체는 전용 버킷 + BucketDeployment 자동 갱신이 보장하므로(같은 synth의 두 값
              // 비교라 drift 감지는 아님 — 리뷰 MED), 이 게이트는 무결성 방어선이에요.
              // secondary source 경로(리포 직접)엔 VERSION이 없어 건너뛰어요.
              "if [ -f \"$AGORA_TOOLING_DIR/VERSION\" ]; then BUNDLE_VER=$(head -n1 \"$AGORA_TOOLING_DIR/VERSION\"); echo \"tooling bundle version=$BUNDLE_VER expected=$AGORA_TOOLING_VERSION\"; if [ -n \"$AGORA_TOOLING_VERSION\" ] && [ \"$BUNDLE_VER\" != \"$AGORA_TOOLING_VERSION\" ]; then echo \"ERROR: tooling bundle is stale (s3=$BUNDLE_VER != expected=$AGORA_TOOLING_VERSION). Re-deploy AgoraRuntimeDeploy-<stage> to refresh _tooling/.\" >&2; exit 1; fi; fi",
              // ── container 분기 (Dockerfile 있는 소스) ──────────────────
              "if [ -f Dockerfile ]; then",
              "  echo 'container build'",
              "  aws ecr get-login-password --region $AWS_DEFAULT_REGION | docker login --username AWS --password-stdin $ECR_URI",
              "  docker build -t $ECR_URI:$ASSET_ID-$VERSION .",
              "  if [ \"$ASSET_TYPE\" = \"agent\" ]; then",
              "    BEDROCK_AGENTCORE_VERSION=$(docker run --rm --entrypoint python \"$ECR_URI:$ASSET_ID-$VERSION\" -c \"from importlib.metadata import version; print(version('bedrock-agentcore'))\")",
              `    ${BEDROCK_AGENTCORE_VERSION_GATE}`,
              "  fi",
              "  docker push $ECR_URI:$ASSET_ID-$VERSION",
              "  printf '{\"tools\":[]}' > tools.json",
              "  aws s3 cp tools.json s3://$ARTIFACT_BUCKET/$ASSET_ID/$VERSION/tools.json",
              "  { echo ARTIFACT_KEY=$ECR_URI:$ASSET_ID-$VERSION; echo TOOLS_S3_KEY=$ASSET_ID/$VERSION/tools.json; echo MCP_MODULE=; echo OTEL_ENTRYPOINT_ENABLED=false; } > \"$EXPORTS\"",
              "  exit 0",
              "fi",
              // ── agent 분기 (A2A codezip 소스) ─────────────────────────
              // agent는 FastMCP가 없어요(A2A). tool 추출·Lambda 하네스 주입을 건너뛰고
              // 소스+의존성만 zip해요. AgentCore Runtime이 entryPoint(main.py)로 직접 실행해요.
              // Lambda arm64/py3.12 wheel 호환을 위해 현재 플랫폼 wheel을 그대로 받아요
              // (빌드 이미지가 AL2023 aarch64라 Runtime arm64와 일치).
              "if [ \"$ASSET_TYPE\" = \"agent\" ]; then",
              "  echo 'agent codezip build'",
              "  mkdir -p package",
              "  cp -r ./* package/ 2>/dev/null || true",
              "  if [ -f requirements.txt ]; then pip install -r requirements.txt -t ./package; " +
                "elif [ -f pyproject.toml ]; then " +
                "python -c \"import tomllib; d=tomllib.load(open('pyproject.toml','rb')); " +
                "print('\\n'.join(d.get('project',{}).get('dependencies',[])))\" > /tmp/_deps.txt; " +
                "if [ -s /tmp/_deps.txt ]; then pip install -r /tmp/_deps.txt -t ./package; fi; fi",
              // 선언 requirements가 아니라 ./package의 실제 dist-info를 읽어요.
              "  BEDROCK_AGENTCORE_VERSION=$(python -c \"from importlib.metadata import distributions; versions=[d.version for d in distributions(path=['./package']) if (d.metadata.get('Name') or '').lower().replace('_','-') == 'bedrock-agentcore']; assert len(versions) == 1, f'expected one installed bedrock-agentcore distribution, found {versions}'; print(versions[0])\")",
              `  ${BEDROCK_AGENTCORE_VERSION_GATE}`,
              // source 종류가 아니라 완성 package의 dist-info와 console script를 함께
              // 확인해요. 둘 중 하나라도 없으면 main.py 진입점을 보존합니다.
              "  OTEL_ENTRYPOINT_ENABLED=$(python -c \"from importlib.metadata import distributions; from pathlib import Path; names={(d.metadata.get('Name') or '').lower().replace('_','-') for d in distributions(path=['./package'])}; print('true' if 'aws-opentelemetry-distro' in names and Path('./package/bin/opentelemetry-instrument').is_file() else 'false')\")",
              "  echo \"otel entrypoint artifact gate enabled=$OTEL_ENTRYPOINT_ENABLED\"",
              // AgentCore Runtime은 타깃 런타임과 다른 파이썬으로 생성된 .pyc/__pycache__를
              // 거부해요(실측 2026-07-17: "artifact contains Python cache files"). zip 전에
              // 모든 바이트코드 캐시를 제거해 소스만 담아요.
              "  find package -type d -name '__pycache__' -prune -exec rm -rf {} + 2>/dev/null || true",
              "  find package -type f \\( -name '*.pyc' -o -name '*.pyo' \\) -delete 2>/dev/null || true",
              "  UNZIPPED_BYTES=$(du -sb package | cut -f1)",
              "  echo \"agent artifact size unzipped_bytes=$UNZIPPED_BYTES limit=786432000\"",
              "  if [ \"$UNZIPPED_BYTES\" -gt 786432000 ]; then echo \"ERROR: agent artifact exceeds 750 MiB uncompressed limit\" >&2; exit 1; fi",
              "  cd package && zip -r ../artifact.zip . && cd ..",
              "  ZIP_BYTES=$(stat -c%s artifact.zip)",
              "  echo \"agent artifact size zip_bytes=$ZIP_BYTES limit=262144000\"",
              "  if [ \"$ZIP_BYTES\" -gt 262144000 ]; then echo \"ERROR: agent artifact exceeds 250 MiB compressed limit\" >&2; exit 1; fi",
              "  aws s3 cp artifact.zip s3://$ARTIFACT_BUCKET/$ASSET_ID/$VERSION/artifact.zip",
              "  printf '{\"tools\":[]}' > tools.json",
              "  aws s3 cp tools.json s3://$ARTIFACT_BUCKET/$ASSET_ID/$VERSION/tools.json",
              "  { echo ARTIFACT_KEY=$ASSET_ID/$VERSION/artifact.zip; echo TOOLS_S3_KEY=$ASSET_ID/$VERSION/tools.json; echo MCP_MODULE=; echo OTEL_ENTRYPOINT_ENABLED=$OTEL_ENTRYPOINT_ENABLED; } > \"$EXPORTS\"",
              "  exit 0",
              "fi",
              // ── codezip 분기 (FastMCP Python 소스) ────────────────────
              "export PYTHONPATH=\"$AGORA_TOOLING_DIR:$PYTHONPATH\"",
              "export AGORA_SOURCE_ROOT=.",
              // 1) MCP 모듈명·진입점 탐지 (C1)
              "MCP_MODULE=$(python -m agora.domains.runtime.deploy.tool_extract --discover-module)",
              "SRC_ENTRY=$(python -m agora.domains.runtime.deploy.tool_extract --discover-path)",
              "echo discovered module=$MCP_MODULE entry=$SRC_ENTRY",
              // 2) 의존성 설치 → ./package.
              //    CodeBuild가 arm64/Amazon Linux(aarch64)라 여기서 받은 네이티브 wheel은
              //    Lambda arm64/python3.12와 호환돼요. --platform을 명시하면 cross-platform
              //    wheel이 돼 빌드 인터프리터가 import 못 해(tool 추출 실패) — 명시 안 하고
              //    현재 플랫폼 wheel을 받아요. 프로젝트 의존성만 설치(코드는 3에서 복사).
              "mkdir -p package",
              "if [ -f requirements.txt ]; then pip install -r requirements.txt -t ./package; " +
                "elif [ -f pyproject.toml ]; then " +
                "python -c \"import tomllib; d=tomllib.load(open('pyproject.toml','rb')); " +
                "print('\\n'.join(d.get('project',{}).get('dependencies',[])))\" > /tmp/_deps.txt; " +
                "if [ -s /tmp/_deps.txt ]; then pip install -r /tmp/_deps.txt -t ./package; fi; fi",
              // 3) MCP 소스 복사 (진입점 내용을 package/ 루트로)
              "cp -r \"$SRC_ENTRY\"/* package/ 2>/dev/null || true",
              // 4) Lambda 하네스 주입
              "cp \"$AGORA_TOOLING_DIR/agora_mcp_handler.py\" package/agora_mcp_handler.py 2>/dev/null || cp \"$AGORA_TOOLING_DIR/agora/domains/runtime/deploy/lambda_handler_template.py\" package/agora_mcp_handler.py",
              // 5) tool 스키마 추출 → tools.json.
              //    소스 모듈이 `from mcp... import`를 하므로 의존성 설치처(./package)도
              //    PYTHONPATH에 넣어야 import 성공해요(실측: 없으면 ModuleNotFoundError: mcp).
              "AGORA_MCP_MODULE=$MCP_MODULE PYTHONPATH=\"$SRC_ENTRY:$PWD/package:$PYTHONPATH\" python -m agora.domains.runtime.deploy.tool_extract > tools.json",
              // 6) zip 패키징 → S3 업로드
              "cd package && zip -r ../artifact.zip . && cd ..",
              "aws s3 cp artifact.zip s3://$ARTIFACT_BUCKET/$ASSET_ID/$VERSION/artifact.zip",
              "aws s3 cp tools.json s3://$ARTIFACT_BUCKET/$ASSET_ID/$VERSION/tools.json",
              // 7) exported-variables를 파일에 기록
              "{ echo ARTIFACT_KEY=$ASSET_ID/$VERSION/artifact.zip; echo TOOLS_S3_KEY=$ASSET_ID/$VERSION/tools.json; echo MCP_MODULE=$MCP_MODULE; echo OTEL_ENTRYPOINT_ENABLED=false; } > \"$EXPORTS\"",
            ].join("\n"),
            // 단일 셸에서 export한 변수는 다음 항목에 안 남으므로, 파일에서 읽어 CodeBuild
            // exported-variables로 승격해요(aws_adapter.py get_build_status가 읽음).
            "export $(cat $CODEBUILD_SRC_DIR/exports.env | xargs)",
          ],
        },
      },
    }),
  });
  repo.grantPullPush(project);
  artifactBucket.grantReadWrite(project);
  toolingBucket.grantRead(project);  // CodeBuild가 _tooling/ 번들을 읽어요(전용 버킷).
  // CodeBuild가 sourcestore(서울, 별도 스택 소유 버킷)에서 업로드 소스를 내려받아요.
  // 버킷명은 -c sourceBucket=... 로 주입(미지정 시 계정 내 agoracatalogstorage-* 패턴).
  const sourceBucketName =
    (scope.node.tryGetContext("sourceBucket") as string) ||
    `agoracatalogstorage-${stage}-*`;
  project.addToRolePolicy(new iam.PolicyStatement({
    actions: ["s3:GetObject", "s3:ListBucket"],
    resources: [
      `arn:aws:s3:::${sourceBucketName}`,
      `arn:aws:s3:::${sourceBucketName}/*`,
    ],
  }));

  // 배포 job 상태 테이블
  const jobsTable = new dynamodb.Table(scope, "DeployJobs", {
    tableName: `AgoraDeployJobs-${stage}`,
    partitionKey: { name: "PK", type: dynamodb.AttributeType.STRING },
    billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
    timeToLiveAttribute: "expires_at",
    removalPolicy,
  });
  jobsTable.addGlobalSecondaryIndex({
    indexName: "PrincipalRequestIndex",
    partitionKey: {
      name: "principal",
      type: dynamodb.AttributeType.STRING,
    },
    sortKey: {
      name: "PK",
      type: dynamodb.AttributeType.STRING,
    },
    projectionType: dynamodb.ProjectionType.INCLUDE,
    nonKeyAttributes: [
      "request_id",
      "kind",
      "status",
      "title",
      "created_at",
      "updated_at",
      "record_id",
      "job_id",
      "error",
      "phase",
      "phase_detail",
    ],
  });

  return { repo, artifactBucket, project, jobsTable };
}
