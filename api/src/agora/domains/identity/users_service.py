"""Application service for Cognito-backed admin user management."""
from __future__ import annotations

import csv
import io
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from threading import RLock

from .models import GrantStatus
from .user_directory import (
    CognitoUserDirectoryPort,
    DirectoryUser,
    UserDirectoryConflict,
)

ALLOWED_GROUPS = frozenset({"user", "admin"})
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

#: `GET /api/admin/identity/users` 한 페이지 크기 (IH-164).
#:
#: 웹의 전수 walk(`listAllCognitoUsers`)는 `next_page` 커서를 따라가는 **순차** 루프라
#: HTTP 왕복이 `ceil(N/이 값)` 번이에요. 25 → 60 이면 왕복이 약 58% 줄어요.
#:
#: 60 은 Cognito `ListUsers` 의 **하드 상한**이에요 — botocore 서비스 모델의
#: `QueryLimitType` 이 `max: 60` 이고, 이 저장소도 같은 값을 독립적으로 쓰고 있어요
#: (`access_router._DIRECTORY_PAGE_SIZE`). ⚠️ 아무도 클램프하지 않아서
#: (`user_directory.AwsCognitoUserDirectory.list_users` 가 `Limit` 을 그대로 넘겨요)
#: 이 값을 60 보다 크게 올리면 **실 AWS 만** `InvalidParameterException` 으로 터지고
#: Fake 어댑터는 그냥 슬라이스해서 통과해요 — 유닛테스트로는 안 잡혀요.
_LIST_PAGE_SIZE = 60

#: 페이지 안의 사용자별 enrich 동시 실행 상한.
#:
#: 페이지 크기와 **일부러 분리했어요.** `_enrich` 는 사용자 1명당 Cognito
#: `AdminGetUser` 1회 + (캐시 미스면) `AdminListGroupsForUser` 1회예요. 상한을
#: 페이지 크기에 묶어 두면 페이지를 25 → 60 으로 올리는 순간 한 요청의 Cognito
#: 동시 호출도 2.4배가 되는데, 그건 공유 쿼터를 건드리는 «다른 축»이고 측정한 근거가
#: 없어요. 그래서 오늘의 상한을 그대로 유지해요 — 전체 enrich 호출 수는 어차피 N 이라
#: 바뀌지 않고, 줄어드는 건 순차 HTTP 왕복 수뿐이에요.
_ENRICH_MAX_WORKERS = 25


class GrantRevocationError(RuntimeError):
    def __init__(self, failed_grant_ids: tuple[str, ...]) -> None:
        self.failed_grant_ids = failed_grant_ids
        super().__init__(
            f"grant 회수에 실패했어요: {', '.join(failed_grant_ids)}"
        )


@dataclass(frozen=True)
class AdminUser:
    sub: str
    email: str
    name: str
    team: str
    groups: tuple[str, ...]
    status: str
    mfa_enabled: bool


@dataclass(frozen=True)
class AdminUserPage:
    items: tuple[AdminUser, ...]
    next_page: str | None


@dataclass(frozen=True)
class BulkRow:
    line: int
    email: str
    action: str
    errors: tuple[str, ...]


@dataclass(frozen=True)
class BulkResult:
    total: int
    passed: int
    failed: int
    rows: tuple[BulkRow, ...]


@dataclass(frozen=True)
class _BulkCandidate:
    line: int
    email: str
    name: str
    team: str
    groups: tuple[str, ...]
    errors: tuple[str, ...]


class UserManagementService:
    def __init__(
        self,
        directory: CognitoUserDirectoryPort,
        identity_store,
        *,
        clock=time.monotonic,
        group_cache_ttl: float = 30.0,
    ) -> None:
        self.directory = directory
        self.identity_store = identity_store
        self._clock = clock
        self._group_cache_ttl = group_cache_ttl
        self._group_cache: dict[str, tuple[float, tuple[str, ...]]] = {}
        self._cache_lock = RLock()

    def _groups(self, sub: str) -> tuple[str, ...]:
        now = self._clock()
        with self._cache_lock:
            cached = self._group_cache.get(sub)
            if cached and cached[0] > now:
                return cached[1]
        groups = self.directory.list_groups(sub)
        self._cache_groups(sub, groups)
        return groups

    def _cache_groups(self, sub: str, groups: tuple[str, ...]) -> None:
        with self._cache_lock:
            self._group_cache[sub] = (
                self._clock() + self._group_cache_ttl,
                tuple(sorted(groups)),
            )

    def _admin_user(self, user: DirectoryUser, groups: tuple[str, ...]) -> AdminUser:
        return AdminUser(
            sub=user.sub,
            email=user.email,
            name=user.name,
            team=user.team,
            groups=groups,
            status="DISABLED" if not user.enabled else user.status,
            mfa_enabled=user.mfa_enabled,
        )

    def _enrich(self, user: DirectoryUser) -> AdminUser:
        # `mfa_enabled` 는 `AdminGetUser` 만 줘요 — `ListUsers` 가 돌려주는 `UserType` 에는
        # `UserMFASettingList` 가 **없어요**(botocore 서비스 모델 실측, 2026-09-05). `groups`
        # 는 어느 쪽도 안 줘서 `AdminListGroupsForUser` 가 따로 필요해요. 반면 `status`·
        # `enabled` 는 `ListUsers` 에도 실려 있어요 — 즉 이 호출을 목록 경로에서 빼면
        # `status` 는 살지만 `mfa_enabled` 가 «조용히» 전원 False 가 되고(`/admin/users` 의
        # MFA 열이 거짓말을 해요) `groups` 는 비어서 그룹 필터가 0건을 돌려줘요. IH-164 가
        # 페이지 크기를 올리는 쪽을 고른 이유예요.
        fresh = self.directory.get_user(user.sub)
        return self._admin_user(fresh, self._groups(user.sub))

    def list_users(
        self,
        *,
        query: str,
        group: str | None,
        page: str | None,
        limit: int = _LIST_PAGE_SIZE,
        attribute: str = "email",
    ) -> AdminUserPage:
        if group and group not in ALLOWED_GROUPS:
            raise ValueError("group must be user or admin")
        raw = self.directory.list_users(query=query, page=page, limit=limit, attribute=attribute)
        if not raw.items:
            return AdminUserPage(items=(), next_page=raw.next_page)
        workers = min(_ENRICH_MAX_WORKERS, len(raw.items))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            items = tuple(executor.map(self._enrich, raw.items))
        if group:
            # Cognito cursor를 보존하려고 그룹 필터는 수신한 페이지에만 적용해요.
            # 따라서 현재 페이지가 비어도 next_page가 있으면 다음 페이지로 이동할 수 있어요.
            items = tuple(item for item in items if group in item.groups)
        return AdminUserPage(items=items, next_page=raw.next_page)

    def get(self, sub: str) -> AdminUser:
        return self._enrich(self.directory.get_user(sub))

    def invite(
        self,
        *,
        email: str,
        name: str,
        team: str,
        groups: tuple[str, ...],
    ) -> AdminUser:
        normalized_groups = _validate_groups(groups)
        _validate_identity_fields(email, name)
        user = self.directory.create_user(email=email, name=name, team=team)
        try:
            for group in normalized_groups:
                self.directory.add_user_to_group(user.sub, group)
        except Exception:
            self.directory.disable_user(user.sub)
            raise
        self._cache_groups(user.sub, normalized_groups)
        return self._admin_user(self.directory.get_user(user.sub), normalized_groups)

    def update(
        self,
        sub: str,
        *,
        email: str | None,
        name: str | None,
        team: str | None,
        groups: tuple[str, ...] | None,
    ) -> AdminUser:
        if email is not None and not _EMAIL.fullmatch(email.strip()):
            raise ValueError("email 형식이 올바르지 않아요.")
        if name is not None and not name.strip():
            raise ValueError("name은 비어 있을 수 없어요.")
        user = self.directory.update_user(
            sub,
            email=email,
            name=name,
            team=team,
        )
        if groups is None:
            current_groups = self._groups(sub)
        else:
            desired = set(_validate_groups(groups))
            current = set(self._groups(sub))
            for group in sorted(desired - current):
                self.directory.add_user_to_group(sub, group)
            for group in sorted(current - desired):
                self.directory.remove_user_from_group(sub, group)
            current_groups = tuple(sorted(desired))
            self._cache_groups(sub, current_groups)
        return self._admin_user(self.directory.get_user(user.sub), current_groups)

    def disable_and_revoke(self, sub: str, *, updated_at: str) -> int:
        revoked = 0
        failures: list[tuple[str, Exception]] = []
        for grant in self.identity_store.list_grants(principal_id=sub):
            if grant.status is not GrantStatus.ACTIVE:
                continue
            try:
                self.identity_store.put_grant(
                    replace(
                        grant,
                        status=GrantStatus.REVOKED,
                        version=grant.version + 1,
                        updated_at=updated_at,
                    )
                )
            except Exception as exc:
                failures.append((grant.grant_id, exc))
            else:
                revoked += 1
        if failures:
            raise GrantRevocationError(
                tuple(grant_id for grant_id, _ in failures)
            ) from failures[0][1]
        self.directory.disable_user(sub)
        return revoked

    def bulk_validate(self, content: str) -> BulkResult:
        candidates = _parse_bulk_csv(content)
        valid = [item for item in candidates if not item.errors]
        existing: set[str] = set()
        if valid:
            with ThreadPoolExecutor(max_workers=min(10, len(valid))) as executor:
                for email, found in executor.map(
                    lambda item: (
                        item.email,
                        any(
                            user.email.casefold() == item.email.casefold()
                            for user in self.directory.list_users(
                                query=item.email,
                                page=None,
                                limit=1,
                            ).items
                        ),
                    ),
                    valid,
                ):
                    if found:
                        existing.add(email.casefold())
        return _bulk_result(
            BulkRow(
                line=item.line,
                email=item.email,
                action="create_and_grant",
                errors=(
                    (*item.errors, "이미 등록된 email이에요.")
                    if item.email.casefold() in existing
                    else item.errors
                ),
            )
            for item in candidates
        )

    def bulk_commit(self, content: str) -> BulkResult:
        rows: list[BulkRow] = []
        for item in _parse_bulk_csv(content):
            errors = list(item.errors)
            if not errors:
                try:
                    self.invite(
                        email=item.email,
                        name=item.name,
                        team=item.team,
                        groups=item.groups,
                    )
                except UserDirectoryConflict:
                    errors.append("이미 등록된 email이에요.")
                except Exception as exc:
                    errors.append(f"등록 실패: {type(exc).__name__}")
            rows.append(
                BulkRow(
                    line=item.line,
                    email=item.email,
                    action="create_and_grant",
                    errors=tuple(errors),
                )
            )
        return _bulk_result(rows)


def _validate_identity_fields(email: str, name: str) -> None:
    if not _EMAIL.fullmatch(email.strip()):
        raise ValueError("email 형식이 올바르지 않아요.")
    if not name.strip():
        raise ValueError("name은 비어 있을 수 없어요.")


def _validate_groups(groups: tuple[str, ...]) -> tuple[str, ...]:
    normalized = tuple(sorted(set(group.strip().lower() for group in groups if group.strip())))
    invalid = set(normalized) - ALLOWED_GROUPS
    if invalid:
        raise ValueError(f"지원하지 않는 group이에요: {sorted(invalid)[0]}")
    if not normalized:
        raise ValueError("group을 하나 이상 지정해야 해요.")
    return normalized


def _parse_bulk_csv(content: str) -> tuple[_BulkCandidate, ...]:
    if len(content.encode("utf-8")) > 1_000_000:
        raise ValueError("CSV는 1MB 이하여야 해요.")
    try:
        reader = csv.DictReader(io.StringIO(content.lstrip("\ufeff")))
    except csv.Error as exc:
        raise ValueError("CSV 형식이 올바르지 않아요.") from exc
    if reader.fieldnames is None:
        raise ValueError("CSV header가 필요해요.")
    headers = {name.strip().lower() for name in reader.fieldnames if name}
    if not {"email", "name"}.issubset(headers):
        raise ValueError("CSV에 email,name header가 필요해요.")

    results: list[_BulkCandidate] = []
    seen: set[str] = set()
    try:
        for line, raw in enumerate(reader, start=2):
            if len(results) >= 500:
                raise ValueError("CSV는 최대 500행까지 처리할 수 있어요.")
            if None in raw:
                raise ValueError(
                    f"CSV {line}행의 열 개수가 header와 맞지 않아요."
                )
            row = {
                (key or "").strip().lower(): (value or "").strip()
                for key, value in raw.items()
            }
            email = row.get("email", "")
            name = row.get("name", "")
            team = row.get("team", "")
            group_text = row.get("groups", row.get("group", "user"))
            groups = tuple(
                part.strip().lower()
                for part in re.split(r"[|,;]", group_text)
                if part.strip()
            )
            errors: list[str] = []
            try:
                _validate_identity_fields(email, name)
            except ValueError as exc:
                errors.append(str(exc))
            try:
                groups = _validate_groups(groups)
            except ValueError as exc:
                errors.append(str(exc))
            normalized_email = email.casefold()
            if normalized_email in seen:
                errors.append("CSV 안에서 email이 중복됐어요.")
            seen.add(normalized_email)
            results.append(
                _BulkCandidate(
                    line=line,
                    email=email,
                    name=name,
                    team=team,
                    groups=groups,
                    errors=tuple(errors),
                )
            )
    except csv.Error as exc:
        raise ValueError("CSV 형식이 올바르지 않아요.") from exc
    return tuple(results)


def _bulk_result(rows) -> BulkResult:
    items = tuple(rows)
    failed = sum(bool(item.errors) for item in items)
    return BulkResult(
        total=len(items),
        passed=len(items) - failed,
        failed=failed,
        rows=items,
    )
