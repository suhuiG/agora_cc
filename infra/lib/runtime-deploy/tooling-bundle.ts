import * as crypto from "crypto";
import * as fs from "fs";
import * as os from "os";
import * as path from "path";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as s3deploy from "aws-cdk-lib/aws-s3-deployment";
import { Construct } from "constructs";

/**
 * CA-18 — 배포형 MCP 빌드용 tooling 번들 자동 갱신.
 *
 * CodeBuild(build-pipeline.ts)는 업로드된 MCP 소스에서 tool 스키마를 추출하고
 * (`tool_extract`) Lambda 하네스를 주입하는데, 그 코드는 리포가 아니라 artifact
 * 버킷의 `_tooling/` 프리픽스에서 와요. 예전엔 이 번들을 손으로 `aws s3 cp` 했기에
 * 백엔드 코드가 바뀌어도 번들이 stale하면 빌드가 옛 코드로 tools.json을 산출해
 * 태그 누락 같은 조용한 불일치가 났어요(2026-08-16 IA-22 실측).
 *
 * 이 모듈은 synth 시점에 api/src에서 번들 파일을 골라 staging 디렉터리로 모으고
 * BucketDeployment로 `_tooling/`에 올려요. 그래서 `cdk deploy` 한 번이면 번들이
 * 최신 코드로 갱신돼요. 추가로 내용 해시 VERSION 스탬프를 심어, 빌드가 S3 번들이
 * 이 스택 배포 시점 코드와 일치하는지 확인(불일치면 실패)할 수 있게 해요.
 */

/**
 * 번들에 담을 파일 목록 — `api/src` 기준 상대 경로. 이 목록이 번들의 단일 출처(SoT)예요.
 *
 * `python -m agora.domains.runtime.deploy.tool_extract`의 import 체인 전체예요:
 *   - agora / domains / runtime / deploy 패키지 __init__ 체인
 *   - deploy/__init__ 가 import 하는 models.py
 *   - tool_extract.py 와 그것이 import 하는 shared/mcp_sensitivity.py
 *   - Lambda 하네스 소스 lambda_handler_template.py (build가 agora_mcp_handler.py로 복사)
 * 전부 표준 라이브러리 + 내부 의존만이라 격리 python으로도 import돼요.
 *
 * ⚠️ import 체인이 바뀌면(새 import 추가·파일 이동) 이 목록과 memory 노트
 * (`mcp-build-tooling-bundle-stale`)를 함께 갱신하세요. 파일이 없으면 synth가
 * 명확한 에러로 실패해 stale/누락을 조기에 잡아요.
 */
export const TOOLING_BUNDLE_FILES: readonly string[] = [
  "agora/__init__.py",
  "agora/domains/__init__.py",
  "agora/domains/runtime/__init__.py",
  "agora/domains/runtime/deploy/__init__.py",
  "agora/domains/runtime/deploy/models.py",
  "agora/domains/runtime/deploy/tool_extract.py",
  "agora/domains/runtime/deploy/lambda_handler_template.py",
  "agora/shared/__init__.py",
  "agora/shared/mcp_sensitivity.py",
];

/** artifact 버킷에서 번들이 사는 프리픽스. 빌드가 여기서 recursive cp로 받아요. */
export const TOOLING_BUNDLE_PREFIX = "_tooling";

/** VERSION 스탬프 파일명(프리픽스 아래). 빌드가 첫 줄(해시)을 읽어 stale을 감지해요. */
export const TOOLING_VERSION_FILE = "VERSION";

function apiSrcDir(): string {
  // __dirname = infra/lib/runtime-deploy → 리포 루트/api/src
  return path.resolve(__dirname, "..", "..", "..", "api", "src");
}

export interface AssembledBundle {
  /** BucketDeployment의 Source.asset가 가리키는 staging 디렉터리(agora/ 레이아웃 + VERSION). */
  readonly stagingDir: string;
  /** 번들 파일 내용의 sha256 앞 16헥사 — VERSION 첫 줄이자 CodeBuild 기대값. */
  readonly version: string;
}

/** VERSION 첫 줄이 `version`과 같고 번들 파일이 모두 있으면 완성된 디렉터리예요. */
function isPublishedBundle(dir: string, version: string): boolean {
  try {
    const stamp = fs.readFileSync(path.join(dir, TOOLING_VERSION_FILE), "utf8");
    if (stamp.split("\n", 1)[0] !== version) return false;
  } catch {
    return false;
  }
  return TOOLING_BUNDLE_FILES.every((rel) =>
    fs.existsSync(path.join(dir, rel)),
  );
}

/**
 * synth 시점에 api/src에서 번들 파일을 staging 디렉터리(agora/ 레이아웃)로 복사하고
 * 내용 해시(VERSION)를 계산해요.
 *
 * 결정적이에요: 파일 경로·내용만으로 해시를 만들고 타임스탬프를 안 넣어요. 그래서
 * 코드가 안 바뀌면 asset 해시도 그대로라 불필요한 재업로드가 없고, 코드가 바뀌면
 * 해시가 바뀌어 BucketDeployment가 새 번들을 올려요.
 *
 * ⚠️ staging 디렉터리는 **내용 주소(`…-<version>`)** 이고, 이미 있으면 절대 지우지
 * 않아요. 예전 구현은 고정 경로 `os.tmpdir()/agora-tooling-bundle`을 매번 `rmSync`로
 * 밀고 다시 썼는데, 이 디렉터리는 `Source.asset()`이 **app 구성이 끝난 뒤** 읽어요.
 * 그래서 같은 리포에서 `cdk synth`가 둘 이상 겹치면(병렬 jest, 다른 세션의 synth·diff,
 * 수동 synth) 한쪽의 `rmSync`가 다른 쪽이 복사 중인 트리를 지워
 * `ENOENT: copyfile … /agora-tooling-bundle/agora/domains/runtime/deploy/tool_extract.py`
 * 로 synth 전체가 죽어요(2026-08-30 실측 — `test/agora-app.test.ts`·
 * `test/iam-escalation-guard.test.ts` 의 synth 4건이 이렇게 실패했어요).
 *
 * 고유 scratch 디렉터리에 다 쓴 뒤 `rename`으로 원자적으로 게시하니, 게시된 경로는
 * 언제나 완성 상태예요. 내용이 같으면 경로도 같아 asset 해시는 그대로고, 내용이 바뀌면
 * 경로가 갈라져 stale을 재사용할 수 없어요. 옛 버전 디렉터리는 다른 프로세스가 읽고
 * 있을 수 있어 지우지 않고 OS의 tmp 청소에 맡겨요.
 */
export function assembleToolingBundle(): AssembledBundle {
  const srcRoot = apiSrcDir();
  const hash = crypto.createHash("sha256");
  const files: { rel: string; body: Buffer }[] = [];

  // 정렬해 해시를 경로 순서와 무관하게 결정적으로.
  for (const rel of [...TOOLING_BUNDLE_FILES].sort()) {
    const abs = path.join(srcRoot, rel);
    let body: Buffer;
    try {
      body = fs.readFileSync(abs);
    } catch {
      throw new Error(
        `CA-18 tooling bundle: 소스 파일을 못 찾았어요 — ${rel} (${abs}). ` +
          "파일이 이동·개명됐다면 TOOLING_BUNDLE_FILES와 memory 노트를 갱신하세요.",
      );
    }
    hash.update(rel, "utf8");
    hash.update("\0");
    hash.update(body);
    hash.update("\0");
    files.push({ rel, body });
  }
  const version = hash.digest("hex").slice(0, 16);

  const stagingDir = path.join(os.tmpdir(), `agora-tooling-bundle-${version}`);
  if (isPublishedBundle(stagingDir, version)) return { stagingDir, version };

  // VERSION: 1줄=해시(빌드가 head -n1로 읽음), 이후=사람이 읽는 매니페스트. 타임스탬프 없음(결정적).
  const manifest = [
    version,
    "# CA-18 agora tooling bundle — content hash of the files below.",
    ...[...TOOLING_BUNDLE_FILES].sort().map((f) => `# ${f}`),
    "",
  ].join("\n");

  const scratch = fs.mkdtempSync(
    path.join(os.tmpdir(), "agora-tooling-scratch-"),
  );
  try {
    for (const { rel, body } of files) {
      const dest = path.join(scratch, rel);
      fs.mkdirSync(path.dirname(dest), { recursive: true });
      fs.writeFileSync(dest, body);
    }
    fs.writeFileSync(path.join(scratch, TOOLING_VERSION_FILE), manifest);
    fs.renameSync(scratch, stagingDir);
  } catch (err) {
    // 경쟁하는 synth가 먼저 같은 내용을 게시했으면(rename이 EEXIST/ENOTEMPTY) 그쪽을
    // 쓰면 돼요. 그게 아니면 진짜 실패라 그대로 올려요.
    fs.rmSync(scratch, { recursive: true, force: true });
    if (!isPublishedBundle(stagingDir, version)) throw err;
  }

  return { stagingDir, version };
}

/**
 * tooling 번들을 artifact 버킷의 `_tooling/` 프리픽스에 배포하고, 번들 버전을 반환해요.
 * 반환한 버전을 CodeBuild `AGORA_TOOLING_VERSION` env로 배선하면 빌드가 stale을 감지해요.
 *
 * BucketDeployment는 CDK가 제공하는 배포 핸들러(Lambda + role)를 스택에 추가해요.
 */
export function addToolingBundle(
  scope: Construct,
  artifactBucket: s3.Bucket,
): string {
  const { stagingDir, version } = assembleToolingBundle();
  new s3deploy.BucketDeployment(scope, "ToolingBundle", {
    sources: [s3deploy.Source.asset(stagingDir)],
    destinationBucket: artifactBucket,
    destinationKeyPrefix: TOOLING_BUNDLE_PREFIX,
    // `_tooling/` 프리픽스는 이 번들 전용이라 prune으로 옛 파일(수동 tar 포함)을 청소해요.
    prune: true,
  });
  return version;
}
