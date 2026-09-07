/**
 * 폴더 통째 업로드 헬퍼 — publish(등록)와 자산 상세 재배포가 공유해요.
 *
 * publish/page.tsx 안에 있던 것을 옮겼어요. 재배포도 같은 필터·정규화를 써야
 * 하는데(잡파일 제외, 최상위 폴더명 제거), 규칙이 갈라지면 재배포 소스에
 * .venv 같은 게 섞여 들어가요.
 */
import type { SourceFile } from "./api";

const TEXT_EXTS = new Set([
  "md", "txt", "json", "yaml", "yml", "py", "ts", "js", "tsx", "jsx",
  "sh", "toml", "csv", "ini", "cfg", "env", "gitignore",
]);

// 점으로 시작하는 알려진 텍스트 설정 파일(확장자 규칙이 아니라 파일명으로 판정).
// `.env` 는 여기 없어요 — 아래 isCredentialPath 가 텍스트 판정보다 먼저 걸러요.
const TEXT_BASENAMES = new Set([
  ".gitignore", ".dockerignore", ".gitattributes", ".editorconfig",
]);

// example/template 변형 접미사 — 벗겨내고 그 앞 세그먼트를 실제 확장자로 재판정해요.
// (.env.example → env, config.yaml.sample → yaml). 안 그러면 마지막 세그먼트인
// "example"이 확장자로 잡혀 텍스트 파일이 비텍스트로 제외돼요.
const TEMPLATE_SUFFIXES = new Set([
  "example", "sample", "template", "dist", "local", "development", "production",
]);

// 값이 든 dotenv 는 통과시키고, 값이 없는 템플릿은 통과시켜요.
const DOTENV_TEMPLATE_SUFFIXES = new Set(["example", "sample", "template", "dist"]);

// 크리덴셜·비밀키만 담는 홈 디렉터리. `.aws/credentials` 는 확장자가 없어서
// isTextFile 이 텍스트로 판정해 버려요 — 그래서 텍스트 판정보다 먼저 걸러야 해요.
const CREDENTIAL_DIRS = new Set([".aws", ".ssh", ".gnupg"]);

// 디렉터리 없이 파일명만으로도 거르는 이름. 개별 파일 선택은 디렉터리 정보를 주지 않아서
// `~/.aws/credentials` 가 `credentials` 로 납작해져요 — 그러면 위 규칙이 못 봐요.
const CREDENTIAL_BASENAMES = new Set([
  "credentials", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",
]);

/**
 * 크리덴셜 운반체로 알려진 경로인가요?
 *
 * Agent Initializr 가 내려주는 ZIP 의 `.env` 에 7일 유효한 dev 크리덴셜이 구워져 있어요.
 * 받은 폴더를 그대로 "소스 배포"로 올리면 그 크리덴셜이 S3 소스 버킷에 평문으로 남아요.
 *
 * 서버도 같은 판정을 해요(`api .../sourcestore/models.py` `reject_credential_path`).
 * **서버가 권위 있는 쪽이에요** — 여기는 업로드를 헛되게 만들지 않으려는 UX 장치라서,
 * 두 판정이 어긋나면 최악의 결과는 사용자가 422 를 보는 것뿐이에요.
 *
 * 최상위 폴더명을 떼기 **전**의 경로로 판정해요. `~/.aws` 를 통째로 고르면 stripTopFolder
 * 가 `.aws` 를 떼서 `credentials` 만 남고, 그러면 이 검사를 통과해 버려요.
 */
export function isCredentialPath(fullPath: string): boolean {
  const segments = fullPath.split("/");
  const base = (segments.pop() ?? "").toLowerCase();
  if (segments.some((s) => CREDENTIAL_DIRS.has(s.toLowerCase()))) return true;
  if (CREDENTIAL_BASENAMES.has(base)) return true;
  // `.env`·`.envrc`(direnv — `export AWS_SECRET_ACCESS_KEY=…` 를 흔히 담아요)·
  // `prod.env` 처럼 `.env` 로 끝나는 파일까지 같은 부류로 봐요.
  const isDotenvFamily =
    base === ".env" || base === ".envrc" || base.endsWith(".env");
  if (isDotenvFamily) return true;
  if (!base.startsWith(".env.") && !base.startsWith(".envrc.")) return false;
  // 접미사 allowlist — 모르는 접미사(.env.local 등)는 값이 든 것으로 봐요.
  return !DOTENV_TEMPLATE_SUFFIXES.has(base.split(".").pop()!);
}

export function isTextFile(path: string): boolean {
  const base = (path.split("/").pop() ?? path).toLowerCase();
  if (TEXT_BASENAMES.has(base)) return true; // .gitignore 등 (확장자 없는 dotfile)
  if (!base.includes(".")) return true; // 확장자 없는 파일(예: Dockerfile)은 텍스트로 간주
  const parts = base.split(".");
  let ext = parts.pop()!;
  // .env.example 처럼 template 접미사면 그 앞 세그먼트로 확장자를 재판정해요.
  if (TEMPLATE_SUFFIXES.has(ext) && parts.length > 0) {
    ext = parts.pop()!;
  }
  return TEXT_EXTS.has(ext);
}

/** webkitRelativePath의 최상위 폴더명을 떼요 ("my-agent/main.py" → "main.py"). */
export function stripTopFolder(relPath: string): string {
  const idx = relPath.indexOf("/");
  return idx >= 0 ? relPath.slice(idx + 1) : relPath;
}

// 폴더 통째 선택 시 건너뛸 잡파일/디렉터리(VCS·빌드산출물·캐시).
// 배포형 소스에는 .venv·__pycache__ 같은 게 흔히 딸려오니 업로드 전에 걸러요.
export const IGNORE_PATH =
  /(^|\/)(\.venv|venv|\.git|__pycache__|\.pytest_cache|\.mypy_cache|\.ruff_cache|node_modules|\.DS_Store|dist|build|\.next|\.idea|\.vscode)(\/|$)/;

/**
 * FileList → {path, content}[] (텍스트만).
 *  - 최상위 폴더명 제거(stripTopFolder)
 *  - 잡파일/디렉터리(IGNORE_PATH) 제외
 *  - 크리덴셜 파일은 excludedSecrets 로 모아요 — skipped 와 **섞지 않아요**. 이유가
 *    다르면 사용자에게 보여줄 말도 달라야 해요("비텍스트라 건너뜀" vs "크리덴셜이라
 *    빼뒀음"). 조용히 빼면 사용자는 뭐가 안 올라갔는지 몰라요.
 *  - 비텍스트 파일은 skipped 로 모아 비차단 경고에 써요(현재 텍스트 업로드만 지원).
 */
export async function collectFolderFiles(
  fileList: FileList,
): Promise<{ files: SourceFile[]; skipped: string[]; excludedSecrets: string[] }> {
  const out: SourceFile[] = [];
  const skipped: string[] = [];
  const excludedSecrets: string[] = [];
  for (const f of Array.from(fileList)) {
    const full = f.webkitRelativePath || f.name;
    const rel = stripTopFolder(full);
    if (!rel || IGNORE_PATH.test(full)) continue;
    // 텍스트 판정보다 **먼저** 걸러요. `.aws/credentials` 는 확장자가 없어서 텍스트로
    // 판정되고, `.env` 는 확장자 `env` 가 TEXT_EXTS 에 있어서 역시 텍스트예요.
    if (isCredentialPath(full)) {
      excludedSecrets.push(rel);
      continue;
    }
    if (!isTextFile(rel)) {
      skipped.push(rel);
      continue;
    }
    out.push({ path: rel, content: await f.text() });
  }
  return { files: out, skipped, excludedSecrets };
}
