"""capability catalog 프리셋 시드 — 멱등. CLI와 콘솔 버튼이 공유해요.

도메인 무관 일반 CRUD 권한이에요. data.read/create/update/delete capability를 누적
3단계 그룹(조회 전용 → 편집 → 전체 관리)으로 묶어, 어떤 MCP tool 에도 붙일 수 있어요.
이미 있는 connection_id는 건너뛰어 여러 번 실행해도 안전해요.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from .models import (
    Connection,
    ConnectionCapability,
    ConnectionStatus,
)
from .store import IdentityRecordNotFound, IdentityStore


@dataclass(frozen=True)
class _Preset:
    connection_id: str
    name: str
    ceiling: tuple[str, ...]
    capabilities: tuple[ConnectionCapability, ...]


_READ = ConnectionCapability(name="data.read", description="조회·목록", operations=())
_CREATE = ConnectionCapability(name="data.create", description="생성", operations=())
_UPDATE = ConnectionCapability(name="data.update", description="수정", operations=())
_DELETE = ConnectionCapability(name="data.delete", description="삭제", operations=())


CATALOG_PRESETS: tuple[_Preset, ...] = (
    _Preset(
        connection_id="preset-viewer",
        name="조회 전용",
        ceiling=("data.read",),
        capabilities=(_READ,),
    ),
    _Preset(
        connection_id="preset-editor",
        name="편집",
        ceiling=("data.read", "data.create", "data.update"),
        capabilities=(_READ, _CREATE, _UPDATE),
    ),
    _Preset(
        connection_id="preset-manager",
        name="전체 관리",
        ceiling=("data.read", "data.create", "data.update", "data.delete"),
        capabilities=(_READ, _CREATE, _UPDATE, _DELETE),
    ),
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def seed_capability_catalog(store: IdentityStore) -> dict:
    """프리셋 권한 그룹을 멱등하게 시드해요. 이미 있으면 건너뛰어요."""
    created: list[str] = []
    skipped: list[str] = []
    now = _now_iso()
    for preset in CATALOG_PRESETS:
        try:
            store.get_connection(preset.connection_id)
            skipped.append(preset.connection_id)
            continue
        except IdentityRecordNotFound:
            pass
        store.put_connection(Connection(
            connection_id=preset.connection_id,
            name=preset.name,
            kind="capability_set",
            target=preset.name,
            credential_mode="mcp",
            ceiling=preset.ceiling,
            status=ConnectionStatus.ACTIVE,
            enforcement="fine_grained",
            created_by="catalog-seed",
            created_at=now,
            updated_at=now,
        ))
        # 주의: 아래 두 쓰기(put_connection → put_connection_capabilities)는 원자적이지 않아요.
        # 그 사이에 크래시가 나면 capability 없는 그룹이 남고, 재실행 시 get_connection이
        # 성공해 건너뛰므로 복구가 안 돼요(admin이 해당 connection을 삭제 후 재시드해야 해요).
        store.put_connection_capabilities(
            preset.connection_id, list(preset.capabilities))
        created.append(preset.connection_id)
    return {"created": created, "skipped": skipped}
