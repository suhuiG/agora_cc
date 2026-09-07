"""ConnectionService — repo 연결·검증 오케스트레이션.

connect는 verify_access로 권한을 확인한 뒤에만 PAT을 credential 스토어에 저장하고
RepoConnection을 영속화해요. PAT 원문은 RepoConnection에 절대 담기지 않아요.
"""
from __future__ import annotations

from .models import ConnectionStatus, RepoAccessResult, RepoConnection


class ConnectionService:
    def __init__(self, *, store, credentials, publisher, now) -> None:
        self.store = store
        self.credentials = credentials
        self.publisher = publisher
        self.now = now

    def _ref(self, provider: str) -> str:
        return f"agora/publish/{provider}"

    def connect(self, provider: str, repo_url: str, token: str, *,
                principal: str) -> RepoConnection:
        access = self.publisher.verify_access(repo_url, token)
        if not access.ok:
            raise ConnectionError(f"repo 접근 실패: {access.reason}")
        ref = self._ref(provider)
        self.credentials.put(ref, token)
        conn = RepoConnection(
            provider=provider, repo_url=repo_url, credential_ref=ref,
            status=ConnectionStatus.CONNECTED.value,
            connected_at=self.now(), connected_by=principal,
        )
        self.store.set(conn)
        return conn

    def get(self) -> RepoConnection | None:
        return self.store.get()

    def token_for(self, conn: RepoConnection) -> str:
        return self.credentials.get(conn.credential_ref)

    def verify_current(self) -> RepoAccessResult:
        conn = self.store.get()
        if conn is None:
            raise LookupError("연결된 repo가 없어요")
        return self.publisher.verify_access(conn.repo_url, self.token_for(conn))
