"""Helpers for reading OpenClaw's string and structured model settings."""


def model_spec(value):
    """Return ``(primary, fallbacks)`` from an OpenClaw model setting."""
    if isinstance(value, str):
        return value, []
    if not isinstance(value, dict):
        return "", []

    primary = value.get("primary", "")
    if not isinstance(primary, str):
        primary = ""
    fallbacks = value.get("fallbacks", [])
    if not isinstance(fallbacks, list):
        fallbacks = []
    return primary, [item for item in fallbacks if isinstance(item, str)]


def resolve_agent_model(config, agent_id):
    """Resolve an agent model, falling back to ``agents.defaults.model``."""
    agents = config.get("agents", {})
    default_spec = model_spec(agents.get("defaults", {}).get("model"))
    agent = next(
        (
            item
            for item in agents.get("list", [])
            if isinstance(item, dict) and item.get("id") == agent_id
        ),
        None,
    )
    if not agent or "model" not in agent:
        return default_spec

    primary, fallbacks = model_spec(agent.get("model"))
    return (primary, fallbacks) if primary else default_spec
