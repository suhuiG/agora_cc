"""Connection.resource 정규화 — 두 store와 쓰기 경로가 공유해요.

- normalize_connection: 역직렬화·쓰기 시 resource가 비면 target에서 채워요(dual-read).
- rederive_for_target: target이 바뀌면 resource를 강제 재파생해 drift를 막아요.
"""
from __future__ import annotations

from dataclasses import replace

from .models import Connection
from .resource_ref import CURRENT_SCHEMA_VERSION, parse_target


def normalize_connection(conn: Connection) -> Connection:
    if conn.resource is None:
        return replace(
            conn,
            resource=parse_target(conn.target),
            schema_version=CURRENT_SCHEMA_VERSION,
        )
    if conn.schema_version != CURRENT_SCHEMA_VERSION:
        return replace(conn, schema_version=CURRENT_SCHEMA_VERSION)
    return conn


def rederive_for_target(conn: Connection, new_target: str) -> Connection:
    return replace(
        conn,
        target=new_target,
        resource=parse_target(new_target),
        schema_version=CURRENT_SCHEMA_VERSION,
    )
