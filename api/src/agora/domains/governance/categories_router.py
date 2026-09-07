"""자산 카테고리 마스터와 기존 자유입력 값 현황 API."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from ...shared.deps import get_gov_store, get_registry, get_registry_id
from ..identity.context import current_principal
from .authz import ADMIN_ONLY, CONSOLE_ROLES, require_role

router = APIRouter(tags=["governance-categories"])
_log = logging.getLogger(__name__)


def _in_use_unlisted(listed: list[str]) -> list[str]:
    """자산이 쓰지만 목록에는 없는 값. Registry 조회 실패는 빈 목록으로 degrade해요."""
    try:
        records = get_registry().list_records(get_registry_id())
    except Exception:
        _log.warning("카테고리 사용 현황 조회에 실패했어요.", exc_info=True)
        return []
    known = set(listed)
    found: set[str] = set()
    for record in records:
        value = (getattr(record, "category", "") or "").strip()
        if value and value not in known:
            found.add(value)
    return sorted(found)


@router.get(
    "/api/governance/categories",
    dependencies=[Depends(require_role(*CONSOLE_ROLES))],
)
def list_categories():
    """카테고리 목록과 사용 중인 미등록 값을 반환해요."""
    listed = get_gov_store().get_categories()
    return {
        "items": listed,
        "in_use_unlisted": _in_use_unlisted(listed),
    }


@router.put(
    "/api/governance/categories",
    dependencies=[Depends(require_role(*ADMIN_ONLY))],
)
def put_categories(body: dict, request: Request):
    """목록을 통째로 교체해요. 공백·중복 정규화는 스토어가 담당해요.

    **`items`가 리스트가 아니면 422로 거부해요.** 빈 리스트로 대체하면 키 오타(`categories`
    등) 하나로 200 OK와 함께 카테고리 마스터 전체가 조용히 지워져요 — 등록 폼이 자유입력으로
    전락하고 관리자는 성공 응답을 받아요. 의도적으로 비우려면 `{"items": []}`를 명시해요.
    """
    principal = current_principal(request)
    items = body.get("items")
    if not isinstance(items, list):
        raise HTTPException(
            422,
            {
                "message": "items는 문자열 리스트여야 해요.",
                "remediation": (
                    "목록을 비우려면 {\"items\": []}를 명시해 주세요."
                ),
            },
        )
    store = get_gov_store()
    store.put_categories(items)
    saved = store.get_categories()
    _log.info(
        "카테고리 목록을 갱신했어요.",
        extra={"principal": principal.principal_id, "count": len(saved)},
    )
    return {"items": saved}
