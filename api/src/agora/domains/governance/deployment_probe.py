"""AWS 실배포 도구 열거 — best-effort (AWS SoT 설계).

Lambda list_functions + ECS list_task_definition_families에서 명명 규칙
agora-tool-{tool_id}-{stage}에 맞는 리소스를 찾아 배포된 tool_id 집합을 돌려줘요.
AWS 조회가 실패하면(권한 없음·mock·네트워크) 빈 집합을 반환하고 절대 크래시하지
않아요(전부 미배포로 표시). 결과는 (region, stage)별로 60초 캐시해요.
"""
from __future__ import annotations

import time

_PREFIX = "agora-tool-"
_TTL_SECONDS = 60.0

# (region, stage) -> (expires_at_monotonic, tool_ids)
_cache: dict[tuple[str, str], tuple[float, set[str]]] = {}


def _reset_cache() -> None:
    _cache.clear()


def _tool_id_from_name(name: str, stage: str) -> str | None:
    """agora-tool-{tool_id}-{stage} → tool_id. 규칙 불일치면 None.

    tool_id에 하이픈이 있어도(snyk-agent-scan) 접두/접미를 정확 문자열로 벗겨 안전해요.
    """
    suffix = f"-{stage}"
    if not name.startswith(_PREFIX) or not name.endswith(suffix):
        return None
    tool_id = name[len(_PREFIX):-len(suffix)]
    return tool_id or None


def _probe_lambda(client, stage: str) -> set[str]:
    ids: set[str] = set()
    for page in client.get_paginator("list_functions").paginate():
        for fn in page.get("Functions", []):
            tid = _tool_id_from_name(fn.get("FunctionName", ""), stage)
            if tid:
                ids.add(tid)
    return ids


def _probe_ecs(client, stage: str) -> set[str]:
    ids: set[str] = set()
    # list_task_definitions의 familyPrefix는 완전일치만 하므로 접두 매칭이 안 돼요.
    # list_task_definition_families는 실제 접두 매칭을 하고 family NAME을 직접 돌려줘요.
    for page in client.get_paginator("list_task_definition_families").paginate(
        familyPrefix=_PREFIX, status="ACTIVE"
    ):
        for family in page.get("families", []):
            tid = _tool_id_from_name(family, stage)
            if tid:
                ids.add(tid)
    return ids


def list_deployed_tool_ids(
    region: str, stage: str, *, lambda_client=None, ecs_client=None, now: float | None = None,
) -> set[str]:
    """배포된 tool_id 집합(best-effort, 60초 캐시). AWS 실패 시 빈 집합."""
    ts = time.monotonic() if now is None else now
    key = (region, stage)
    cached = _cache.get(key)
    if cached is not None and ts < cached[0]:
        return set(cached[1])

    ids: set[str] = set()
    try:
        lam = lambda_client
        ecs = ecs_client
        if lam is None or ecs is None:
            import boto3
            lam = lam or boto3.client("lambda", region_name=region)
            ecs = ecs or boto3.client("ecs", region_name=region)
        ids |= _probe_lambda(lam, stage)
        ids |= _probe_ecs(ecs, stage)
    except Exception:
        # best-effort: 권한 없음·mock·네트워크 → 전부 미배포로 표시(크래시 금지).
        ids = set()

    _cache[key] = (ts + _TTL_SECONDS, set(ids))
    return ids
