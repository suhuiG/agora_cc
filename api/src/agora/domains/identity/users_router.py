"""Admin-only Cognito user-management API."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field

from ...shared.deps import get_user_management_service
from ..governance.authz import require_role
from .user_directory import UserDirectoryConflict, UserDirectoryNotFound
from .users_service import GrantRevocationError

router = APIRouter(
    prefix="/api/admin/identity/users",
    tags=["identity-users"],
    dependencies=[Depends(require_role("admin"))],
)


class UserItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    sub: str
    email: str
    name: str
    team: str
    groups: tuple[str, ...]
    status: str
    mfa_enabled: bool


class UserPage(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    items: tuple[UserItem, ...]
    next_page: str | None


class UserInvite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=3, max_length=320)
    name: str = Field(min_length=1, max_length=128)
    team: str = Field(default="", max_length=128)
    groups: tuple[str, ...] = Field(default=("user",), min_length=1, max_length=2)


class UserPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str | None = Field(default=None, min_length=3, max_length=320)
    name: str | None = Field(default=None, min_length=1, max_length=128)
    team: str | None = Field(default=None, max_length=128)
    groups: tuple[str, ...] | None = Field(default=None, min_length=1, max_length=2)


class BulkInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    csv: str = Field(min_length=1, max_length=1_000_000)


class BulkRowOutput(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    line: int
    email: str
    action: str
    errors: tuple[str, ...]


class BulkOutput(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    total: int
    passed: int
    failed: int
    rows: tuple[BulkRowOutput, ...]


def _service_call(callback):
    try:
        return callback()
    except UserDirectoryNotFound as exc:
        raise HTTPException(404, "사용자를 찾을 수 없어요.") from exc
    except UserDirectoryConflict as exc:
        raise HTTPException(409, "이미 등록된 email이에요.") from exc
    except GrantRevocationError as exc:
        raise HTTPException(
            503,
            "grant 회수에 실패해 계정을 비활성화하지 않았어요. 다시 시도하세요.",
        ) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("", response_model=UserPage)
def list_users(
    q: str = Query(default="", max_length=128),
    group: str | None = Query(default=None),
    page: str | None = Query(default=None, max_length=4096),
):
    return _service_call(
        lambda: get_user_management_service().list_users(
            query=q,
            group=group,
            page=page,
        )
    )


@router.get("/{sub}", response_model=UserItem)
def get_user(sub: str):
    return _service_call(lambda: get_user_management_service().get(sub))


@router.post("", response_model=UserItem, status_code=status.HTTP_201_CREATED)
def invite_user(body: UserInvite):
    return _service_call(
        lambda: get_user_management_service().invite(
            email=body.email,
            name=body.name,
            team=body.team,
            groups=body.groups,
        )
    )


@router.patch("/{sub}", response_model=UserItem)
def update_user(sub: str, body: UserPatch):
    if not body.model_fields_set:
        raise HTTPException(422, "수정할 필드를 하나 이상 지정해야 해요.")
    return _service_call(
        lambda: get_user_management_service().update(
            sub,
            email=body.email,
            name=body.name,
            team=body.team,
            groups=body.groups,
        )
    )


@router.post("/{sub}/disable")
def disable_user(sub: str):
    revoked = _service_call(
        lambda: get_user_management_service().disable_and_revoke(
            sub,
            updated_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        )
    )
    return {"sub": sub, "disabled": True, "revoked_grants": revoked}


@router.post("/bulk:validate", response_model=BulkOutput)
def validate_bulk_users(body: BulkInput):
    return _service_call(
        lambda: get_user_management_service().bulk_validate(body.csv)
    )


@router.post("/bulk", response_model=BulkOutput)
def bulk_users(body: BulkInput):
    return _service_call(
        lambda: get_user_management_service().bulk_commit(body.csv)
    )
