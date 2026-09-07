"""Source Store 도메인 모델 — 자산 타입 중립의 값 객체와 예외.

skill·mcp·agent 모두 "파일 트리 + 불변 semver 버전 + 경량 감사"를 공유하므로,
이 모델은 특정 타입에 묶이지 않아요. 타입별 차이는 bindings.py가 담당해요.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum


class VersionStatus(str, Enum):
    """버전 라이프사이클. STAGING은 업로드 중(미노출), PUBLISHED는 확정·불변."""

    STAGING = "STAGING"
    PUBLISHED = "PUBLISHED"
    DEPRECATED = "DEPRECATED"


_ASSET_ID_SEGMENT_RE = re.compile(r"\A[a-z0-9][a-z0-9._-]*\Z")


def validate_asset_id(asset_id: str) -> str:
    r"""네임스페이스 asset_id 검증.

    형식은 정확히 `{owner}/{name}` — 슬래시 1개, 각 세그먼트는 slug-safe
    (`\A[a-z0-9][a-z0-9._-]*\Z` — `\Z`로 trailing-newline 우회 차단). 슬래시는 DDB PK·S3 prefix 모두 합법이라
    리터럴로 유지해요. (서버 레이어에서만 호출 — Store는 asset_id-agnostic.)
    """
    parts = asset_id.split("/")
    if len(parts) != 2:
        raise ValueError(
            f"asset_id must be '{{owner}}/{{name}}' with exactly one '/': {asset_id!r}"
        )
    for seg in parts:
        if not _ASSET_ID_SEGMENT_RE.match(seg):
            raise ValueError(f"asset_id segment not slug-safe: {seg!r} in {asset_id!r}")
    return asset_id


def _validate_rel_path(path: str) -> str:
    """파일 트리 내 상대 경로만 허용 (경로 탈출·절대경로 차단)."""
    if not path or path.startswith("/") or "\\" in path:
        raise ValueError(f"unsafe path: {path!r}")
    parts = path.split("/")
    if ".." in parts or "" in parts:
        raise ValueError(f"unsafe path: {path!r}")
    return path


# 값이 든 dotenv 는 등록을 거부해요. Agora 자신의 Agent Initializr 가 내려주는 ZIP 의
# `.env` 에 7일 유효한 dev 크리덴셜을 구워 넣어요
# (`playground/scaffold.py` `scaffold_with_dev_environment`).
#
# 실제 피해는 **소스 저장소에 평문으로 남는 것**이에요. 등록된 버전은 인증된 사용자 누구나
# 내려받을 수 있고(`catalog/router.py` 의 archive·files 라우트), 크리덴셜은 7일 살아요.
#
# 배포된 agent 가 그 크리덴셜로 호출하는 신원 스왑은 지금은 codezip 빌드의 `cp -r ./* package/`
# 가 dotfile 을 안 옮겨서 막혀 있어요 — 설계가 아니라 glob 동작에 기댄 우연이라, 생성 코드 쪽에
# 별도 방어선을 뒀어요(`scaffold.py` `_deployed_runtime`).
#
# 템플릿 접미사는 통과시켜요 — 값이 없고, 생성물 README 가 `cp .env.example .env` 를
# 안내하거든요. 접미사 allowlist 라서 모르는 접미사(`.env.local` 등)는 거부돼요.
_DOTENV_TEMPLATE_SUFFIXES = frozenset({"example", "sample", "template", "dist"})

# 크리덴셜·비밀키만 담는 홈 디렉터리. agent 소스 트리에 들어올 이유가 없고,
# `.aws/credentials` 는 확장자가 없어서 클라이언트 텍스트 필터도 통과해요(장기 크리덴셜).
_CREDENTIAL_DIRS = frozenset({".aws", ".ssh", ".gnupg"})

# 디렉터리 없이 파일명만으로도 거부하는 이름. 브라우저의 **개별 파일 선택**은 디렉터리
# 정보를 아예 주지 않아서, `~/.aws/credentials` 를 손으로 고르면 경로가 `credentials` 로
# 납작해져요 — 그러면 위 디렉터리 규칙이 못 봐요. 확장자도 없어서 텍스트 필터도 통과해요.
_CREDENTIAL_BASENAMES = frozenset({
    "credentials", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",
})


def _is_value_dotenv(basename: str) -> bool:
    """`.env`·`.env.local` 처럼 값이 든 dotenv 인가요? (`.env.example` 은 아니에요.)

    `.envrc`(direnv)도 같은 부류로 봐요 — `export AWS_SECRET_ACCESS_KEY=…` 를 흔히
    담고, 배포된 agent 는 direnv 를 쓰지 않아요. `prod.env` 처럼 `.env` 로 끝나는
    파일도 dotenv 관례예요.
    """
    lowered = basename.lower()
    if lowered in (".env", ".envrc") or lowered.endswith(".env"):
        return True
    if not (lowered.startswith(".env.") or lowered.startswith(".envrc.")):
        return False
    return lowered.rpartition(".")[2] not in _DOTENV_TEMPLATE_SUFFIXES


def reject_credential_path(path: str) -> str:
    """크리덴셜 운반체로 알려진 경로면 ValueError.

    업로드 티켓 발급 시점(=업로드 **전**)에 거절하는 게 목적이에요. 소스를 다 올린 뒤
    거절하면 사용자가 처음부터 다시 해야 하고, 그때는 이미 S3 에 올라가 있어요.

    경로 기반 검사예요 — 내용을 안 봐요. 서버는 업로드 파일 내용을 아예 읽지 않고
    (`presign_upload` 는 path·size 만 받아요), 파일명을 바꿔 넣은 크리덴셜은 통과해요.
    그 잔여 위험은 `docs/06-risks.md` 에 기록해 뒀어요.
    """
    segments = path.split("/")
    for segment in segments[:-1]:
        if segment.lower() in _CREDENTIAL_DIRS:
            raise ValueError(
                f"{segment}/ 는 크리덴셜 디렉터리라 등록할 수 없어요. "
                f"빼고 다시 올려 주세요: {path}"
            )
    basename = segments[-1]
    if _is_value_dotenv(basename):
        raise ValueError(
            f"{basename} 에는 크리덴셜이 들어 있어 등록할 수 없어요. "
            f"빼고 다시 올려 주세요 (.env.example 은 올릴 수 있어요): {path}"
        )
    if basename.lower() in _CREDENTIAL_BASENAMES:
        raise ValueError(
            f"{basename} 은 크리덴셜 파일 이름이라 등록할 수 없어요. "
            f"빼고 다시 올려 주세요: {path}"
        )
    return path


@dataclass(frozen=True)
class FileSpec:
    """업로드 요청 시 클라이언트가 선언하는 파일 1건 (내용 아님, 메타만).

    크리덴셜 경로 거부를 여기 두는 이유: 업로드 티켓을 발급하는 라우트 3곳
    (`/api/agent/deploy/init`·`/api/mcp/deploy/init`·`/api/source/publish/init`)이 모두
    이 값 객체를 만들고, 셋 다 `ValueError` 를 `HTTPException(422, str(e))` 로 바꿔요.
    라우트마다 헬퍼를 부르면 네 번째 라우트가 생길 때 빠질 수 있어요.
    """

    path: str
    size: int

    def __post_init__(self) -> None:
        _validate_rel_path(self.path)
        reject_credential_path(self.path)
        if self.size < 0:
            raise ValueError("size must be >= 0")


@dataclass(frozen=True)
class ManifestEntry:
    """확정된 버전의 파일 1건 (해시 포함)."""

    path: str
    size: int
    sha256: str


@dataclass(frozen=True)
class Manifest:
    """한 버전의 전체 파일 목록. DDB 버전 아이템에 저장돼요."""

    entries: tuple[ManifestEntry, ...] = ()

    @property
    def total_size(self) -> int:
        return sum(e.size for e in self.entries)

    def paths(self) -> tuple[str, ...]:
        return tuple(e.path for e in self.entries)

    def get(self, path: str) -> ManifestEntry | None:
        for e in self.entries:
            if e.path == path:
                return e
        return None


@dataclass(frozen=True)
class UploadTicket:
    """presign_upload 결과. 파일별 presigned PUT URL + 업로드 식별자."""

    asset_id: str
    version: str
    upload_id: str
    urls: dict = field(default_factory=dict)  # path -> presigned PUT URL


@dataclass(frozen=True)
class VersionRecord:
    """확정·조회되는 버전 1건."""

    asset_id: str
    version: str
    asset_type: str
    s3_prefix: str
    status: VersionStatus
    manifest: Manifest
    published_by: str = ""
    published_at: str = ""
    # 카탈로그 메타(name/description/tags 등)를 init→finalize로 운반하는 opaque dict.
    # Store는 해석하지 않고 STAGING 아이템에 저장·반환만 해요(durable 핸드오프, §11.7).
    meta: dict = field(default_factory=dict)


# ── 예외 ────────────────────────────────────────────────────────────────
class SourceStoreError(Exception):
    """source store 작업 공통 예외 베이스."""


class VersionAlreadyExists(SourceStoreError):
    """이미 PUBLISHED된 semver를 재발행하려 함 (불변성 위반)."""


class IncompleteUpload(SourceStoreError):
    """finalize 시 선언된 파일 중 일부가 S3에 없음."""


class VersionNotFound(SourceStoreError):
    """해당 asset_id/version 버전이 없음."""
