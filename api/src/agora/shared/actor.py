"""Stable, non-PII actor identifiers derived from verified principals."""

from __future__ import annotations

import hashlib
import re


AGENTCORE_ACTOR_ID_PATTERN = (
    r"[a-zA-Z0-9][a-zA-Z0-9-_/]*"
    r"(?::[a-zA-Z0-9-_/]+)*[a-zA-Z0-9-_/]*"
)
# Conservative Agora-owned ceiling; the AgentCore service limit is not documented.
AGENTCORE_ACTOR_ID_MAX_LENGTH = 255
_AGENTCORE_ACTOR_ID = re.compile(AGENTCORE_ACTOR_ID_PATTERN)


def derive_actor_id(principal: str) -> str:
    """Return the shared principal pseudonym used by invocation surfaces."""
    if not principal.strip():
        raise ValueError("authenticated principal is required")
    digest = hashlib.sha256(principal.encode("utf-8")).hexdigest()
    return f"agora-{digest[:32]}"


def derive_scoped_actor_id(agent_id: str, principal: str) -> str:
    """Scope a verified principal pseudonym to one deployed agent."""
    if not agent_id.strip():
        raise ValueError("agent_id is required")
    candidate = f"{agent_id}:{derive_actor_id(principal)}"
    if (
        len(candidate) <= AGENTCORE_ACTOR_ID_MAX_LENGTH
        and _AGENTCORE_ACTOR_ID.fullmatch(candidate)
    ):
        return candidate
    digest = hashlib.sha256(candidate.encode("utf-8")).hexdigest()
    return f"agora-invalid-scope-{digest[:32]}"
