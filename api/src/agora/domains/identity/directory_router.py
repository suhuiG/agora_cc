"""회원 검색 (등록자용 최소 투영) — 담당자 지정에만 쓰는 좁은 디렉터리 조회 (CA-29·ADR-0071).

왜 별 라우터인가: 등록 폼의 2차 담당자는 **자유 입력이 아니라 회원 검색**으로 고르게 해야
계약(유효한 email·본인과 다른 사람)을 만족시킬 수 있어요. 그런데 기존 관리자 API
(`/api/admin/identity/users`)는 `sub`·groups·status·MFA 활성 여부까지 돌려주고 admin 전용
`require_role("admin")` 이 걸려 있어요. 그걸 일반 등록자에게 열면 **관리자 화면의 데이터를
전부 여는 것**이 돼요.

그래서 목적에 맞게 좁힌 별 엔드포인트를 둬요.

* 필드는 `email`·`name` 두 개뿐 — `sub`(인가 주체 식별자)·groups·status·MFA 는 안 나가요.
* **검색어 없이 전체 목록을 못 받아요**(최소 2자) — 디렉터리 덤프 방지.
* 한 번에 최대 10건.
* **호출자 자신은 결과에서 제외**해요 — 계약의 `distinct_escalation_contact_required` 와
  같은 규칙이라, 화면이 고를 수 없게 만드는 편이 정직해요.
* 인증은 필요해요(익명 조회 아님). 역할은 `user`·`admin` 모두 허용 — 등록은 일반 사용자가
  하는 일이에요.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from ...shared.deps import get_user_management_service
from ..governance.authz import require_role
from .context import current_principal
from .user_directory import UserDirectoryNotFound

router = APIRouter(
    prefix="/api/directory",
    tags=["identity-directory"],
    # 등록 폼에서 쓰니 일반 사용자도 호출할 수 있어야 해요. 익명은 안 돼요.
    dependencies=[Depends(require_role("user", "admin"))],
)

_log = logging.getLogger(__name__)

_MIN_QUERY = 2
_MAX_ITEMS = 10


class DirectoryMember(BaseModel):
    """담당자 지정에 필요한 최소 정보. 인가 주체 식별자(`sub`)는 싣지 않아요."""

    email: str
    name: str


class DirectoryPage(BaseModel):
    items: tuple[DirectoryMember, ...]
    # 상한(10건)에 걸려 잘렸는지. True 면 화면이 "검색어를 더 좁혀 주세요"를 보여줘요 —
    # 잘린 목록을 전체처럼 보여주면 사용자가 없는 사람이라고 오해해요.
    truncated: bool


@router.get("/members", response_model=DirectoryPage)
def search_members(request: Request, q: str = Query(default="", max_length=128)):
    """이름·email 로 회원을 검색해요. 담당자 지정 전용 최소 투영이에요."""
    query = q.strip()
    if len(query) < _MIN_QUERY:
        raise HTTPException(
            422,
            f"검색어를 {_MIN_QUERY}자 이상 입력해 주세요 — 전체 목록은 내려주지 않아요.",
        )
    try:
        me = (current_principal(request).email or "").strip().lower()
    except Exception:
        me = ""

    svc = get_user_management_service()
    fetch_limit = _MAX_ITEMS + 1

    try:
        email_page = svc.list_users(
            query=query, group=None, page=None, limit=fetch_limit, attribute="email",
        )
        name_page = svc.list_users(
            query=query, group=None, page=None, limit=fetch_limit, attribute="name",
        )
    except UserDirectoryNotFound:
        return DirectoryPage(items=(), truncated=False)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc

    # email 기준 dedup 합집합 (email·name 양쪽에서 매칭되어도 1건)
    seen_emails: dict[str, DirectoryMember] = {}
    for item in (*email_page.items, *name_page.items):
        email = (getattr(item, "email", "") or "").strip()
        if not email:
            # email 이 없는 계정은 담당자로 지정할 수 없어요.
            continue
        key = email.lower()
        if key not in seen_emails:
            seen_emails[key] = DirectoryMember(
                email=email, name=getattr(item, "name", "") or ""
            )

    # truncated 판정: 제외 이전 합집합 크기 + 어느 축이든 next_page 있으면 True
    union_size = len(seen_emails)
    truncated = not (
        email_page.next_page is None
        and name_page.next_page is None
        and union_size <= _MAX_ITEMS
    )

    # 본인 제외 (distinct_escalation_contact_required) — 표시에서만 제외해요
    members = sorted(seen_emails.values(), key=lambda m: m.email.casefold())
    members = [m for m in members if not (me and m.email.lower() == me)]

    return DirectoryPage(items=tuple(members[:_MAX_ITEMS]), truncated=truncated)
