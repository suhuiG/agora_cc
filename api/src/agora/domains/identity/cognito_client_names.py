"""Agora-owned Cognito app-client name contracts."""

HUMAN_POOL_CLIENT_PREFIX = "agora-human-"
HUMAN_POOL_AGENT_CLIENT_PREFIX = f"{HUMAN_POOL_CLIENT_PREFIX}agent-"
HUMAN_POOL_DEV_CLIENT_PREFIX = f"{HUMAN_POOL_CLIENT_PREFIX}dev-"
HUMAN_POOL_WORKLOAD_CLIENT_NAME_TEMPLATE = (
    f"{HUMAN_POOL_CLIENT_PREFIX}workload-{{stage}}"
)
# IA-78 left legacy bot-pool clients in place for rollback. Inventory ownership must
# recognize both generations until that migration is explicitly completed.
MANAGED_AGENT_CLIENT_PREFIXES = (
    "agora-agent-",
    HUMAN_POOL_AGENT_CLIENT_PREFIX,
)


def human_pool_workload_client_name(stage: str) -> str:
    """Return the operator-created shared workload client name for a stage."""
    normalized = stage.strip().lower()
    if not normalized:
        raise ValueError("stage is required for the human-pool workload client")
    return HUMAN_POOL_WORKLOAD_CLIENT_NAME_TEMPLATE.format(stage=normalized)
