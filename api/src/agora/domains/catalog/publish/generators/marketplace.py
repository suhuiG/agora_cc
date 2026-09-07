"""marketplace.json 병합·정리 — 순수 함수 단일 출처.

managed 콘솔의 sync는 "repo 현재 상태로 marketplace의 plugin 전체를 교체"해요. 그래서
배포마다 자기 항목만 담은 파일을 덮어쓰면 다른 bundle의 plugin이 조직 카탈로그에서
사라져요. merge_marketplace가 기존 항목을 보존하고 자기 항목만 갱신해요.
"""
from __future__ import annotations

import json

# repo 루트 — managed 콘솔은 owner/repo만 받고 subdirectory path 옵션이 없어요.
MARKETPLACE_PATH = ".claude-plugin/marketplace.json"
# M5에서 쓰던 옛 위치. 마이그레이션 정리 대상이에요.
LEGACY_MARKETPLACE_PATH = "ClaudeCode/.claude-plugin/marketplace.json"

# prune_marketplace가 "마지막 항목이라 파일째 삭제해야 함"을 알리는 센티널.
MARKETPLACE_EMPTY = object()


def _load(existing: bytes | None) -> dict | None:
    if not existing:
        return None
    try:
        data = json.loads(existing)
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def _dump(data: dict) -> bytes:
    return json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")


def merge_marketplace(existing: bytes | None, entry: dict, *,
                      marketplace_name: str, owner_name: str) -> bytes:
    """기존 marketplace.json에 entry를 병합해요(같은 name/source는 제자리 갱신).

    파싱 실패한 기존 파일은 덮어쓰지 않고 ValueError를 올려요 — 조직 카탈로그를
    날리는 것보다 배포 실패가 나아요.
    """
    if existing:
        data = _load(existing)
        if data is None:
            raise ValueError(
                "기존 marketplace.json을 읽을 수 없어요. 배포를 중단했어요"
                " (덮어쓰면 다른 plugin이 사라져요)")
    else:
        data = {"name": marketplace_name, "owner": {"name": owner_name},
                "plugins": []}

    data.setdefault("name", marketplace_name)
    data.setdefault("owner", {"name": owner_name})
    plugins = data.get("plugins")
    if not isinstance(plugins, list):
        plugins = []

    slug = entry.get("name")
    source = entry.get("source")
    replaced = False
    merged = []
    for p in plugins:
        if isinstance(p, dict) and (p.get("name") == slug or p.get("source") == source):
            merged.append(entry)          # 제자리 갱신 — 순서 유지
            replaced = True
        else:
            merged.append(p)
    if not replaced:
        merged.append(entry)

    data["plugins"] = merged
    return _dump(data)


def prune_marketplace(existing: bytes | None, slug: str):
    """marketplace.json에서 slug 항목을 제거한 결과를 돌려줘요.

    반환:
      None             — 파일 없음 / 파싱 실패 / 매칭 항목 없음 (변경 불필요)
      MARKETPLACE_EMPTY — 이 항목이 마지막이라 파일을 삭제해야 함
      bytes            — 항목을 뺀 새 내용 (재작성용)
    """
    data = _load(existing)
    if data is None:
        return None
    plugins = data.get("plugins")
    if not isinstance(plugins, list):
        return None
    source = f"./plugins/{slug}"
    kept = [p for p in plugins
            if not (isinstance(p, dict)
                    and (p.get("source") == source or p.get("name") == slug))]
    if len(kept) == len(plugins):
        return None
    if not kept:
        return MARKETPLACE_EMPTY
    data["plugins"] = kept
    return _dump(data)
