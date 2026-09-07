"""PublisherPort — Agora가 사용자 git repo에 파일을 쓰는 단일 인터페이스.

sourcestore의 SourceStorePort와 형제. GitHub/GitLab 어댑터가 이 Port를 구현하고,
레이어 코드는 provider를 몰라요.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from .models import PullRequestResult, PushResult, RepoAccessResult, RepoConnection


@runtime_checkable
class PublisherPort(Protocol):
    """git repo 배포 포트."""

    def verify_access(self, repo_url: str, token: str) -> RepoAccessResult:
        """repo 존재 + 쓰기권한 확인. 실패 시 reason으로 사유를 알려요."""
        ...

    def push_files(
        self, conn: RepoConnection, files: dict[str, bytes], *,
        message: str, token: str,
    ) -> PushResult:
        """파일트리(경로→바이트)를 main에 단일 커밋으로 push. 반환: commit SHA."""
        ...

    def delete_paths(
        self, conn: RepoConnection, prefixes: list[str], *,
        message: str, token: str,
    ) -> PushResult:
        """prefix(디렉터리·파일)에 걸리는 blob을 main에서 단일 커밋으로 삭제.

        지울 게 없으면 no-op(commit_sha="" / files_written=0)으로 돌아와요.
        """
        ...

    def read_file(
        self, conn: RepoConnection, path: str, *, token: str,
    ) -> bytes | None:
        """repo의 파일 1개 내용을 읽어요. 없으면(404) None."""
        ...

    def open_pull_request(
        self, conn: RepoConnection, files: dict[str, bytes], *,
        branch: str, title: str, body: str, token: str,
    ) -> PullRequestResult:
        """파일트리를 새 브랜치에 단일 커밋으로 올리고 default branch로 PR을 열어요.

        managed 콘솔의 자동 sync는 version bump를 포함한 PR 머지에만 걸려요
        (직접 push는 트리거되지 않아요). 같은 이름 브랜치가 있으면 force 갱신해요.
        """
        ...
