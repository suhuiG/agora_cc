"""Independent Cognito scope observation for shared Gateway policies."""
from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Literal


class ScopeNameConflict(ValueError):
    """A scope cannot be registered when either name prefixes the other."""


# `danger_scope_for_invoke_scope` 를 지웠어요 (ADR-0099 결정 8).
#
# `<identifier>/danger` scope 를 파생하던 함수예요. Cedar danger 백스톱이 없어져서 그 scope 를
# 쓰는 곳이 없어졌고, 애초에 **발급되지 않던 scope**를 백스톱이 요구한 것이 IH-130 의 원인이었어요
# (M2M pool app client 10개 중 `/danger` 허용 0개, 2026-08-31 전수 확인).
#
# `validate_scope_name_prefixes` 는 남아요 — ADR-0085 결정 3(접두어 충돌 금지)은 백스톱과
# 무관하게 유효해요. `…tools.danger` 를 `…tools.dangerous-extra` 로 우회한 사고가 있었고,
# 그 규약은 코드가 강제해야 성립해요.


def validate_scope_name_prefixes(
    scope_names: Iterable[str],
) -> tuple[str, ...]:
    """Return normalized names after bidirectional prefix validation."""
    names = tuple(scope.strip() for scope in scope_names)
    if any(not scope for scope in names):
        raise ScopeNameConflict("scope names must not be empty")
    for index, scope in enumerate(names):
        related = next(
            (
                other
                for other in names[index + 1 :]
                if scope.startswith(other) or other.startswith(scope)
            ),
            None,
        )
        if related is not None:
            raise ScopeNameConflict(
                "scope names must not have a prefix relationship: "
                f"{scope!r}, {related!r}"
            )
    return names


@dataclass(frozen=True)
class CognitoScopeObservation:
    status: Literal["observed", "unknown"]
    scope_names: tuple[str, ...] = ()
    source: str = "cognito-idp:list_resource_servers"
    reason: str = ""


class CognitoScopeObserver:
    """Read the complete resource-server scope inventory for one user pool."""

    def __init__(
        self,
        cognito_client,
        *,
        user_pool_id: str,
        client_factory: Callable[[], object] | None = None,
    ) -> None:
        self._cognito = cognito_client
        self._user_pool_id = user_pool_id.strip()
        self._client_factory = client_factory

    def observe(self) -> CognitoScopeObservation:
        if (
            self._cognito is None
            and self._client_factory is None
        ) or not self._user_pool_id:
            return CognitoScopeObservation(
                status="unknown",
                reason="Cognito scope observer is not configured",
            )
        try:
            if self._cognito is None:
                self._cognito = self._client_factory()
            paginator = self._cognito.get_paginator(
                "list_resource_servers"
            )
            scope_names: set[str] = set()
            for page in paginator.paginate(
                UserPoolId=self._user_pool_id,
                MaxResults=50,
            ):
                servers = page.get("ResourceServers", ())
                if not isinstance(servers, (list, tuple)):
                    raise ValueError(
                        "ResourceServers is not a list"
                    )
                for server in servers:
                    if not isinstance(server, dict):
                        raise ValueError(
                            "ResourceServers contains a non-object"
                        )
                    identifier = server.get("Identifier")
                    if not isinstance(identifier, str) or not identifier:
                        raise ValueError(
                            "resource server Identifier is missing"
                        )
                    scopes = server.get("Scopes", ())
                    if not isinstance(scopes, (list, tuple)):
                        raise ValueError("Scopes is not a list")
                    for scope in scopes:
                        if not isinstance(scope, dict):
                            raise ValueError(
                                "Scopes contains a non-object"
                            )
                        scope_name = scope.get("ScopeName")
                        if (
                            not isinstance(scope_name, str)
                            or not scope_name
                        ):
                            raise ValueError("ScopeName is missing")
                        scope_names.add(
                            f"{identifier}/{scope_name}"
                        )
        except Exception as exc:  # noqa: BLE001 - unknown is explicit
            return CognitoScopeObservation(
                status="unknown",
                reason=f"{type(exc).__name__}: {str(exc)[:800]}",
            )
        return CognitoScopeObservation(
            status="observed",
            scope_names=tuple(sorted(scope_names)),
        )
