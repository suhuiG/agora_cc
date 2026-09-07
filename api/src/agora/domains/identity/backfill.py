"""Connection.resource 일괄 backfill — 멱등. dry_run은 변경 대상만 반환.

dual-read(normalize)가 안전망이지만, 이 스크립트로 혼재를 한 번에 종료해
(2) policy compiler가 resource를 항상 신뢰할 수 있게 해요.
"""
from __future__ import annotations

from .connection_normalize import normalize_connection
from .resource_ref import CURRENT_SCHEMA_VERSION


def backfill(store, *, dry_run: bool) -> list[str]:
    """모든 Connection의 resource를 채워요. 이미 최신이면 skip(멱등).

    반환: 변경된(또는 dry_run이면 변경될) connection_id 목록.
    """
    changed: list[str] = []
    for conn in store.list_connections():
        if conn.resource is not None and conn.schema_version == CURRENT_SCHEMA_VERSION:
            continue
        changed.append(conn.connection_id)
        if not dry_run:
            store.put_connection(normalize_connection(conn))
    return changed
