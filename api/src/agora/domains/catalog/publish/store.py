"""ConnectionStore — RepoConnection 영속화(BundleStore 패턴). 조직·환경당 단일 레코드."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol, runtime_checkable

from .models import RepoConnection


@runtime_checkable
class ConnectionStore(Protocol):
    def get(self) -> RepoConnection | None: ...
    def set(self, conn: RepoConnection) -> None: ...
    def clear(self) -> None: ...


class JsonConnectionStore:
    def __init__(self, store_path: str | Path) -> None:
        self._store_path = Path(store_path)
        self._conn: RepoConnection | None = None
        if self._store_path.exists():
            raw = self._store_path.read_text().strip()
            if raw:
                self._conn = RepoConnection.from_dict(json.loads(raw))

    def get(self) -> RepoConnection | None:
        return self._conn

    def set(self, conn: RepoConnection) -> None:
        self._conn = conn
        self._store_path.write_text(
            json.dumps(conn.to_dict(), ensure_ascii=False, indent=2))

    def clear(self) -> None:
        self._conn = None
        self._store_path.write_text("")
