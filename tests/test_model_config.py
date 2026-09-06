from model_config import model_spec, resolve_agent_model


def test_model_spec_accepts_legacy_string():
    assert model_spec("openai/gpt-5.4") == ("openai/gpt-5.4", [])


def test_model_spec_accepts_structured_setting():
    assert model_spec(
        {
            "primary": "openai/gpt-5.6-sol",
            "fallbacks": ["openai/gpt-5.5"],
        }
    ) == ("openai/gpt-5.6-sol", ["openai/gpt-5.5"])


def test_agent_specific_model_wins():
    config = {
        "agents": {
            "defaults": {"model": "openai/gpt-5.5"},
            "list": [
                {
                    "id": "briefing",
                    "model": {
                        "primary": "openai/gpt-5.6-luna",
                        "fallbacks": ["deepseek/deepseek-v4-pro"],
                    },
                }
            ],
        }
    }
    assert resolve_agent_model(config, "briefing") == (
        "openai/gpt-5.6-luna",
        ["deepseek/deepseek-v4-pro"],
    )


def test_missing_agent_falls_back_to_structured_default():
    config = {
        "agents": {
            "defaults": {
                "model": {
                    "primary": "openai/gpt-5.6-sol",
                    "fallbacks": ["openai/gpt-5.5"],
                }
            },
            "list": [],
        }
    }
    assert resolve_agent_model(config, "briefing") == (
        "openai/gpt-5.6-sol",
        ["openai/gpt-5.5"],
    )
