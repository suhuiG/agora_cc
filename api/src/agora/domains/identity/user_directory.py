"""Cognito human-user directory port and its fake/AWS adapters."""
from __future__ import annotations

import base64
from abc import ABC, abstractmethod
from dataclasses import dataclass, replace
from threading import RLock


class UserDirectoryNotFound(LookupError):
    pass


class UserDirectoryConflict(RuntimeError):
    pass


@dataclass(frozen=True)
class DirectoryUser:
    sub: str
    username: str
    email: str
    name: str
    team: str
    status: str
    enabled: bool
    mfa_enabled: bool = False


@dataclass(frozen=True)
class DirectoryUserPage:
    items: tuple[DirectoryUser, ...]
    next_page: str | None


_ALLOWED_FILTER_ATTRIBUTES = frozenset({"email", "name"})


class CognitoUserDirectoryPort(ABC):
    """User-pool operations used by the admin user-management service."""

    @abstractmethod
    def list_users(
        self, *, query: str, page: str | None, limit: int, attribute: str = "email"
    ) -> DirectoryUserPage:
        raise NotImplementedError

    @abstractmethod
    def get_user(self, sub: str) -> DirectoryUser:
        raise NotImplementedError

    @abstractmethod
    def create_user(self, *, email: str, name: str, team: str) -> DirectoryUser:
        raise NotImplementedError

    @abstractmethod
    def update_user(
        self,
        sub: str,
        *,
        email: str | None,
        name: str | None,
        team: str | None,
    ) -> DirectoryUser:
        raise NotImplementedError

    @abstractmethod
    def list_groups(self, sub: str) -> tuple[str, ...]:
        raise NotImplementedError

    @abstractmethod
    def add_user_to_group(self, sub: str, group: str) -> None:
        raise NotImplementedError

    @abstractmethod
    def remove_user_from_group(self, sub: str, group: str) -> None:
        raise NotImplementedError

    @abstractmethod
    def disable_user(self, sub: str) -> None:
        raise NotImplementedError

    @abstractmethod
    def enable_user(self, sub: str) -> None:
        raise NotImplementedError

    @abstractmethod
    def reset_user_password(self, sub: str) -> None:
        raise NotImplementedError


def _encode_offset(offset: int) -> str:
    return base64.urlsafe_b64encode(str(offset).encode()).rstrip(b"=").decode()


def _decode_offset(value: str | None) -> int:
    if not value:
        return 0
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        return int(raw.decode())
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError("page cursor is invalid") from exc


class FakeCognitoUserDirectory(CognitoUserDirectoryPort):
    """Hermetic adapter for dev and unit tests."""

    def __init__(self) -> None:
        self._users: dict[str, DirectoryUser] = {}
        self._groups: dict[str, set[str]] = {}
        self._next_sub = 1
        self._lock = RLock()

    def list_users(
        self, *, query: str, page: str | None, limit: int, attribute: str = "email"
    ) -> DirectoryUserPage:
        if attribute not in _ALLOWED_FILTER_ATTRIBUTES:
            raise ValueError(
                f"attribute 는 {sorted(_ALLOWED_FILTER_ATTRIBUTES)} 중 하나여야 해요."
            )
        with self._lock:
            needle = query.strip().casefold()
            users = sorted(self._users.values(), key=lambda item: item.email.casefold())
            if needle:
                if attribute == "name":
                    users = [
                        item
                        for item in users
                        if item.name.casefold().startswith(needle)
                    ]
                else:
                    users = [
                        item
                        for item in users
                        if item.email.casefold().startswith(needle)
                    ]
            offset = _decode_offset(page)
            items = tuple(users[offset : offset + limit])
            following = offset + len(items)
            return DirectoryUserPage(
                items=items,
                next_page=_encode_offset(following) if following < len(users) else None,
            )

    def get_user(self, sub: str) -> DirectoryUser:
        with self._lock:
            try:
                return self._users[sub]
            except KeyError as exc:
                raise UserDirectoryNotFound(sub) from exc

    def create_user(self, *, email: str, name: str, team: str) -> DirectoryUser:
        with self._lock:
            normalized = email.strip().casefold()
            if any(item.email.casefold() == normalized for item in self._users.values()):
                raise UserDirectoryConflict(email)
            sub = f"fake-sub-{self._next_sub}"
            self._next_sub += 1
            user = DirectoryUser(
                sub=sub,
                username=email.strip(),
                email=email.strip(),
                name=name.strip(),
                team=team.strip(),
                status="FORCE_CHANGE_PASSWORD",
                enabled=True,
            )
            self._users[sub] = user
            self._groups[sub] = set()
            return user

    def update_user(
        self,
        sub: str,
        *,
        email: str | None,
        name: str | None,
        team: str | None,
    ) -> DirectoryUser:
        with self._lock:
            current = self.get_user(sub)
            if email is not None:
                normalized = email.strip().casefold()
                if any(
                    item.sub != sub and item.email.casefold() == normalized
                    for item in self._users.values()
                ):
                    raise UserDirectoryConflict(email)
            updated = replace(
                current,
                email=current.email if email is None else email.strip(),
                name=current.name if name is None else name.strip(),
                team=current.team if team is None else team.strip(),
            )
            self._users[sub] = updated
            return updated

    def list_groups(self, sub: str) -> tuple[str, ...]:
        self.get_user(sub)
        with self._lock:
            return tuple(sorted(self._groups.get(sub, set())))

    def add_user_to_group(self, sub: str, group: str) -> None:
        self.get_user(sub)
        with self._lock:
            self._groups.setdefault(sub, set()).add(group)

    def remove_user_from_group(self, sub: str, group: str) -> None:
        self.get_user(sub)
        with self._lock:
            self._groups.setdefault(sub, set()).discard(group)

    def disable_user(self, sub: str) -> None:
        with self._lock:
            self._users[sub] = replace(self.get_user(sub), enabled=False)

    def enable_user(self, sub: str) -> None:
        with self._lock:
            self._users[sub] = replace(self.get_user(sub), enabled=True)

    def reset_user_password(self, sub: str) -> None:
        with self._lock:
            self._users[sub] = replace(
                self.get_user(sub), status="RESET_REQUIRED"
            )


def _attributes(values: list[dict]) -> dict[str, str]:
    return {
        str(item.get("Name", "")): str(item.get("Value", ""))
        for item in values
    }


class AwsCognitoUserDirectory(CognitoUserDirectoryPort):
    """Boto3 Cognito Identity Provider adapter."""

    def __init__(
        self,
        *,
        user_pool_id: str,
        region: str,
        client=None,
    ) -> None:
        self._user_pool_id = user_pool_id
        self._region = region
        self._client = client
        self._usernames: dict[str, str] = {}
        self._lock = RLock()

    def _cognito(self):
        if self._client is None:
            import boto3

            self._client = boto3.client(
                "cognito-idp",
                region_name=self._region,
            )
        return self._client

    def _remember(self, user: DirectoryUser) -> DirectoryUser:
        with self._lock:
            self._usernames[user.sub] = user.username
        return user

    def _from_aws(self, data: dict) -> DirectoryUser:
        attributes = _attributes(data.get("Attributes", data.get("UserAttributes", [])))
        username = str(data.get("Username", ""))
        sub = attributes.get("sub", username)
        return self._remember(
            DirectoryUser(
                sub=sub,
                username=username,
                email=attributes.get("email", ""),
                name=attributes.get("name", ""),
                team=attributes.get("custom:team", ""),
                status=str(data.get("UserStatus", "UNKNOWN")),
                enabled=bool(data.get("Enabled", True)),
                mfa_enabled=bool(data.get("UserMFASettingList")),
            )
        )

    def _username(self, sub: str) -> str:
        with self._lock:
            cached = self._usernames.get(sub)
        if cached:
            return cached
        response = self._cognito().list_users(
            UserPoolId=self._user_pool_id,
            Filter=f'sub = "{_filter_value(sub)}"',
            Limit=1,
        )
        users = response.get("Users", [])
        if not users:
            raise UserDirectoryNotFound(sub)
        return self._from_aws(users[0]).username

    def _call(self, operation: str, **kwargs):
        try:
            return getattr(self._cognito(), operation)(**kwargs)
        except Exception as exc:
            from botocore.exceptions import ClientError

            if not isinstance(exc, ClientError):
                raise
            code = exc.response.get("Error", {}).get("Code", "")
            if code in ("UserNotFoundException", "ResourceNotFoundException"):
                raise UserDirectoryNotFound(kwargs.get("Username", "")) from exc
            if code in (
                "UsernameExistsException",
                "AliasExistsException",
                "GroupExistsException",
            ):
                raise UserDirectoryConflict(kwargs.get("Username", "")) from exc
            raise

    def list_users(
        self, *, query: str, page: str | None, limit: int, attribute: str = "email"
    ) -> DirectoryUserPage:
        if attribute not in _ALLOWED_FILTER_ATTRIBUTES:
            raise ValueError(
                f"attribute 는 {sorted(_ALLOWED_FILTER_ATTRIBUTES)} 중 하나여야 해요."
            )
        kwargs: dict = {
            "UserPoolId": self._user_pool_id,
            "Limit": limit,
        }
        if query.strip():
            kwargs["Filter"] = f'{attribute} ^= "{_filter_value(query.strip())}"'
        if page:
            kwargs["PaginationToken"] = page
        response = self._call("list_users", **kwargs)
        return DirectoryUserPage(
            items=tuple(self._from_aws(item) for item in response.get("Users", [])),
            next_page=response.get("PaginationToken"),
        )

    def get_user(self, sub: str) -> DirectoryUser:
        response = self._call(
            "admin_get_user",
            UserPoolId=self._user_pool_id,
            Username=self._username(sub),
        )
        return self._from_aws(response)

    def create_user(self, *, email: str, name: str, team: str) -> DirectoryUser:
        attributes = [
            {"Name": "email", "Value": email},
            {"Name": "email_verified", "Value": "true"},
            {"Name": "name", "Value": name},
        ]
        if team:
            attributes.append({"Name": "custom:team", "Value": team})
        response = self._call(
            "admin_create_user",
            UserPoolId=self._user_pool_id,
            Username=email,
            UserAttributes=attributes,
            DesiredDeliveryMediums=["EMAIL"],
        )
        return self._from_aws(response["User"])

    def update_user(
        self,
        sub: str,
        *,
        email: str | None,
        name: str | None,
        team: str | None,
    ) -> DirectoryUser:
        attributes = []
        if email is not None:
            attributes.extend(
                [
                    {"Name": "email", "Value": email},
                    {"Name": "email_verified", "Value": "true"},
                ]
            )
        if name is not None:
            attributes.append({"Name": "name", "Value": name})
        if team is not None:
            attributes.append({"Name": "custom:team", "Value": team})
        if attributes:
            self._call(
                "admin_update_user_attributes",
                UserPoolId=self._user_pool_id,
                Username=self._username(sub),
                UserAttributes=attributes,
            )
        return self.get_user(sub)

    def list_groups(self, sub: str) -> tuple[str, ...]:
        kwargs: dict = {
            "UserPoolId": self._user_pool_id,
            "Username": self._username(sub),
            "Limit": 60,
        }
        groups: set[str] = set()
        while True:
            response = self._call("admin_list_groups_for_user", **kwargs)
            groups.update(
                str(item["GroupName"])
                for item in response.get("Groups", [])
                if item.get("GroupName")
            )
            next_token = response.get("NextToken")
            if not next_token:
                return tuple(sorted(groups))
            kwargs["NextToken"] = next_token

    def add_user_to_group(self, sub: str, group: str) -> None:
        self._call(
            "admin_add_user_to_group",
            UserPoolId=self._user_pool_id,
            Username=self._username(sub),
            GroupName=group,
        )

    def remove_user_from_group(self, sub: str, group: str) -> None:
        self._call(
            "admin_remove_user_from_group",
            UserPoolId=self._user_pool_id,
            Username=self._username(sub),
            GroupName=group,
        )

    def disable_user(self, sub: str) -> None:
        self._call(
            "admin_disable_user",
            UserPoolId=self._user_pool_id,
            Username=self._username(sub),
        )

    def enable_user(self, sub: str) -> None:
        self._call(
            "admin_enable_user",
            UserPoolId=self._user_pool_id,
            Username=self._username(sub),
        )

    def reset_user_password(self, sub: str) -> None:
        self._call(
            "admin_reset_user_password",
            UserPoolId=self._user_pool_id,
            Username=self._username(sub),
        )


def _filter_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')
