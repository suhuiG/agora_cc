"""publish 도메인 값객체 — 연결·결과 dataclass. PAT 원문은 여기 담지 않아요(credential_ref만)."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ConnectionStatus(str, Enum):
    CONNECTED = "CONNECTED"
    ACCESS_FAILED = "ACCESS_FAILED"
    TOKEN_EXPIRED = "TOKEN_EXPIRED"


@dataclass
class RepoConnection:
    """사용자 repo 연결 1건. credential_ref는 PAT 저장소 키(원문 아님)."""

    provider: str
    repo_url: str
    credential_ref: str
    status: str
    connected_at: str = ""
    connected_by: str = ""

    def to_dict(self) -> dict:
        return {
            "provider": self.provider, "repo_url": self.repo_url,
            "credential_ref": self.credential_ref, "status": self.status,
            "connected_at": self.connected_at, "connected_by": self.connected_by,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "RepoConnection":
        return cls(
            provider=d["provider"], repo_url=d["repo_url"],
            credential_ref=d["credential_ref"], status=d["status"],
            connected_at=d.get("connected_at", ""),
            connected_by=d.get("connected_by", ""),
        )


@dataclass
class RepoAccessResult:
    """verify_access 결과. reason ∈ ''|not_found|no_write|invalid_token.

    is_private: 조직 marketplace는 private/internal 필수예요. public이면 콘솔이
    거부하므로 관리자에게 경고해요(연결 자체는 막지 않아요 — 3P는 public도 허용).
    """

    ok: bool
    reason: str = ""
    is_private: bool = False


@dataclass
class PushResult:
    """push_files 결과."""

    commit_sha: str
    files_written: int


@dataclass
class PullRequestResult:
    """open_pull_request 결과."""

    pr_url: str
    pr_number: int
    commit_sha: str
    files_written: int
