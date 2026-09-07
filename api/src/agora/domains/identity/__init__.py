"""Agora 사용자 신원 경계."""

from typing import Any

from .models import Principal

__all__ = ["Principal", "current_principal"]


def __getattr__(name: str) -> Any:
    if name == "current_principal":
        from .context import current_principal

        return current_principal
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
