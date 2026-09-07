"""AgentCore Memory strategy contract owned by Agora."""

SUPPORTED_MEMORY_STRATEGIES = ("SEMANTIC", "SUMMARIZATION")

MEMORY_STRATEGY_NAMES = {
    "SEMANTIC": "SemanticMemory",
    "SUMMARIZATION": "SummarizationMemory",
}

MEMORY_NAMESPACE_TEMPLATES = {
    "SEMANTIC": "users/{actorId}/facts",
    "SUMMARIZATION": "users/{actorId}/summaries/{sessionId}",
}

# Five results bounds prompt growth while retaining several independent facts.
# The SDK default threshold (0.2) is explicit so upgrades cannot change recall.
MEMORY_RETRIEVAL_TOP_K = 5
MEMORY_RETRIEVAL_RELEVANCE_SCORE = 0.2


def configured_memory_namespaces(memory: object) -> tuple[str, ...] | None:
    """Read ledger-owned namespaces from ``strategy_configs``.

    ``None`` means the MANAGED declaration is incomplete or malformed. Callers
    must retain that as unknown rather than treating it as an empty expectation.
    """
    if not isinstance(memory, dict):
        return None
    mode = str(memory.get("mode") or "").upper()
    if mode == "DISABLED":
        return ()
    strategies = memory.get("strategies")
    configs = memory.get("strategy_configs")
    if (
        mode != "MANAGED"
        or not isinstance(strategies, (list, tuple))
        or not strategies
        or not isinstance(configs, dict)
    ):
        return None
    namespaces: list[str] = []
    for strategy in strategies:
        config = configs.get(strategy)
        configured = (
            config.get("namespaces") if isinstance(config, dict) else None
        )
        if (
            not isinstance(configured, (list, tuple))
            or not configured
            or any(
                not isinstance(namespace, str) or not namespace.strip()
                for namespace in configured
            )
        ):
            return None
        namespaces.extend(configured)
    return tuple(sorted(namespaces))


class UnsupportedMemoryStrategyError(ValueError):
    """A persisted or requested Memory strategy is no longer supported."""


def require_supported_memory_strategies(strategies: object) -> None:
    """Reject named unsupported strategies with an actionable stable message."""
    if not isinstance(strategies, (list, tuple)):
        return
    unsupported = tuple(
        dict.fromkeys(
            strategy
            for strategy in strategies
            if isinstance(strategy, str)
            and strategy not in SUPPORTED_MEMORY_STRATEGIES
        )
    )
    if unsupported:
        raise UnsupportedMemoryStrategyError(
            "지원하지 않는 AgentCore Memory 전략이에요: "
            + ", ".join(unsupported)
            + ". 지원 전략: "
            + ", ".join(SUPPORTED_MEMORY_STRATEGIES)
            + "."
        )
