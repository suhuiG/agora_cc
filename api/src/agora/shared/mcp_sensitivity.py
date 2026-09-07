"""MCP tool sensitivity heuristics shared by catalog registration and deploy builds.

The first word of a snake_case, kebab-case, or camelCase tool name is matched against:

* READ: get, list, search, fetch, read, describe, query, find
* CREATE: create, add, new, insert, register
* UPDATE: update, set, modify, edit, patch, rename, move, put
* DELETE: delete, remove, drop, destroy, purge, revoke

``put_new_*`` is CREATE, while bare ``put`` and every other ``put`` are UPDATE: without
enough context to prove creation, the more sensitive interpretation avoids under-tagging.
Unknown operations default to UPDATE; DELETE is reserved for an explicit destructive word
in the name, description, or schema.
"""
from __future__ import annotations

import json
import re
from typing import Any

_WORDS = re.compile(
    r"[A-Z]+(?=[A-Z][a-z]|[0-9]|$)|[A-Z]?[a-z]+|[A-Z]+|[0-9]+"
)

VERB_SENSITIVITY: dict[str, str] = {
    "get": "READ",
    "list": "READ",
    "search": "READ",
    "fetch": "READ",
    "read": "READ",
    "describe": "READ",
    "query": "READ",
    "find": "READ",
    "create": "CREATE",
    "add": "CREATE",
    "new": "CREATE",
    "insert": "CREATE",
    "register": "CREATE",
    "update": "UPDATE",
    "set": "UPDATE",
    "modify": "UPDATE",
    "edit": "UPDATE",
    "patch": "UPDATE",
    "rename": "UPDATE",
    "move": "UPDATE",
    "put": "UPDATE",
    "delete": "DELETE",
    "remove": "DELETE",
    "drop": "DELETE",
    "destroy": "DELETE",
    "purge": "DELETE",
    "revoke": "DELETE",
}

_DESTRUCTIVE_SIGNALS = {
    "delete",
    "remove",
    "drop",
    "destroy",
    "purge",
    "revoke",
    "erase",
    "wipe",
    "terminate",
}


def _words(value: str) -> list[str]:
    return [match.group(0).lower() for match in _WORDS.finditer(value)]


def leading_verb_sensitivity(tool_name: str) -> tuple[str, str] | None:
    """Return a Tier-1 suggestion when the tool name starts with a known verb."""
    words = _words(tool_name)
    if not words:
        return None
    verb = words[0]
    sensitivity = VERB_SENSITIVITY.get(verb)
    if sensitivity is None:
        return None
    if verb == "put" and len(words) > 1 and words[1] in {
        "create",
        "add",
        "new",
        "insert",
        "register",
    }:
        return "CREATE", f"Tier 1: qualified verb 'put {words[1]}' maps to CREATE."
    qualifier = " (ambiguous put uses the higher sensitivity)" if verb == "put" else ""
    return sensitivity, f"Tier 1: leading verb '{verb}' maps to {sensitivity}{qualifier}."


def conservative_sensitivity(
    tool_name: str,
    description: str,
    input_schema: dict[str, Any],
) -> tuple[str, str]:
    """Return the fail-safe suggestion used when classification is inconclusive."""
    schema_text = json.dumps(input_schema, ensure_ascii=False, sort_keys=True)
    signals = set(_words(f"{tool_name} {description} {schema_text}"))
    destructive = sorted(signals.intersection(_DESTRUCTIVE_SIGNALS))
    if destructive:
        return (
            "DELETE",
            f"Conservative default: destructive signal '{destructive[0]}' requires DELETE.",
        )
    return (
        "UPDATE",
        "Conservative default: unknown operations use UPDATE to avoid under-tagging mutations.",
    )


def heuristic_sensitivity(
    tool_name: str,
    description: str,
    input_schema: dict[str, Any],
) -> tuple[str, str]:
    """Return Tier 1 when conclusive, otherwise the conservative default."""
    return leading_verb_sensitivity(tool_name) or conservative_sensitivity(
        tool_name, description, input_schema
    )
