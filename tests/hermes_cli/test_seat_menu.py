"""The chat menu lists the broker and the bunker above every other source."""

from unittest.mock import patch

from hermes_cli.inventory import ConfigContext, build_models_payload
from hermes_cli.models_validate import validate_requested_model
from hermes_cli.seat_menu import broker_menu_models, bunker_menu_models, seat_accepts


def _ctx():
    return ConfigContext(
        current_provider="claude-broker",
        current_model="opus",
        current_base_url="",
        user_providers={},
        custom_providers=[],
    )


def _row(slug, name, models, **extra):
    return {
        "slug": slug,
        "name": name,
        "models": models,
        "total_models": len(models),
        "is_current": False,
        "is_user_defined": extra.pop("is_user_defined", False),
        "source": extra.pop("source", "hermes"),
        **extra,
    }


def test_chat_menu_puts_broker_then_bunker_above_other_sources():
    rows = [
        _row("anthropic", "Anthropic", ["claude-opus-4-6", "claude-sonnet-4-6", "not-claude"]),
        _row(
            "openai-codex",
            "ChatGPT or Codex Subscription",
            [
                "gpt-6-sol",
                "gpt-6.1-sol",
                "gpt-6.1-sol-20260929",
                "gpt-6.1-sol-pro",
                "gpt-6-sol-900k",
                "gpt-5.3-codex-spark",
            ],
        ),
        _row(
            "custom",
            "Codex Sol",
            ["gpt-6-sol"],
            is_user_defined=True,
            source="user-config",
            api_url="http://127.0.0.1:8765/v1",
        ),
        _row("nous", "Nous Portal", ["openai/gpt-6-sol"]),
    ]
    ctx = _ctx()

    def configured(slug):
        return slug in {"claude-broker", "codex-bunker", "openai-codex", "nous"}

    with (
        patch("hermes_cli.model_switch.list_authenticated_providers", return_value=rows),
        patch("hermes_cli.config.read_raw_config", return_value={}),
        patch("hermes_cli.auth.is_provider_explicitly_configured", side_effect=configured),
        patch(
            "hermes_cli.seat_menu.bunker_concrete_models",
            return_value=["gpt-5.6-luna", "gpt-6-sol", "gpt-6-sol-900k", "gpt-5.3-codex-spark"],
        ),
    ):
        payload = build_models_payload(ctx, explicit_only=True)

    providers = payload["providers"]
    assert [row["slug"] for row in providers[:2]] == ["claude-broker", "codex-bunker"]
    assert "anthropic" not in [row["slug"] for row in providers]

    broker = providers[0]
    assert broker["name"] == "Anthropic broker (Claude subscription)"
    assert broker["is_current"] is True
    assert broker["models"] == ["claude-opus-4-6", "claude-sonnet-4-6"]
    assert "opus" not in broker["models"]
    assert "fable" not in broker["models"]
    assert "not-claude" not in broker["models"]

    bunker = providers[1]
    assert bunker["name"] == "Bunker Codex (bunker accounts)"
    assert bunker["models"] == ["gpt-5.6-luna", "gpt-6.1-sol", "gpt-6-sol"]
    assert "sol" not in bunker["models"]
    assert "astra" not in bunker["models"]
    assert "gpt-6-sol-900k" not in bunker["models"]
    assert "gpt-5.3-codex-spark" not in bunker["models"]
    assert "gpt-6.1-sol-pro" not in bunker["models"]

    names = {row["slug"]: row["name"] for row in providers}
    assert names["openai-codex"] == "ChatGPT subscription (direct login)"
    assert names["custom"] == "Codex loopback"
    assert names["nous"] == "Nous Portal"
    assert [row["slug"] for row in providers] == ["claude-broker", "codex-bunker", "openai-codex", "custom", "nous"]


def test_versions_sort_by_name_then_newest_without_the_bare_alias():
    broker = broker_menu_models([
        "claude-opus-5",
        "claude-opus-5-5",
        "claude-opus-4-8",
        "claude-opus-4-5",
        "claude-opus-4-5-20251101",
        "claude-fable-5",
        "claude-fable-5-1",
        "claude-fable-5.1",
        "claude-sonnet-4-6",
        "claude-3-7-sonnet-20250219",
        "claude-haiku-4-5-20251001",
        "not-claude",
        "opus",
    ])
    assert broker == [
        "claude-fable-5-1",
        "claude-fable-5",
        "claude-haiku-4-5-20251001",
        "claude-opus-5-5",
        "claude-opus-5",
        "claude-opus-4-8",
        "claude-opus-4-5",
        "claude-sonnet-4-6",
        "claude-3-7-sonnet-20250219",
    ]
    assert "opus" not in broker
    assert broker.count("claude-fable-5-1") == 1

    with patch(
        "hermes_cli.seat_menu.bunker_concrete_models",
        return_value=["gpt-6-sol", "gpt-6-astra", "gpt-5.6-luna", "gpt-5.6-sol", "gpt-6-sol-900k"],
    ):
        bunker = bunker_menu_models([
            "gpt-6.1-sol",
            "gpt-6.1-sol-pro",
            "gpt-6-luna-20260929",
            "gpt-5.3-codex-spark",
            "sol",
        ])
    assert bunker == [
        "gpt-6-astra",
        "gpt-6-luna",
        "gpt-5.6-luna",
        "gpt-6.1-sol",
        "gpt-6-sol",
        "gpt-5.6-sol",
    ]
    assert "sol" not in bunker


def test_seat_accepts_broker_and_bunker_ids_only():
    assert seat_accepts("claude-broker", "opus")
    assert seat_accepts("claude-broker", "claude-opus-4-6")
    assert seat_accepts("claude-broker", "claude-fable-5-1")
    assert seat_accepts("claude-broker", "claude-3-7-sonnet-20250219")
    assert not seat_accepts("claude-broker", "claude-opus-4-bogus")
    assert not seat_accepts("claude-broker", "gpt-6-sol")
    assert seat_accepts("codex-bunker", "sol")
    assert seat_accepts("codex-bunker", "gpt-6-sol")
    assert seat_accepts("codex-bunker", "gpt-6.1-sol")
    assert seat_accepts("codex-bunker", "gpt-5.6-terra")
    assert not seat_accepts("codex-bunker", "gpt-6.1-sol-pro")
    assert not seat_accepts("codex-bunker", "gpt-6-sol-900k")
    assert not seat_accepts("codex-bunker", "gpt-5.3-codex-spark")
    assert not seat_accepts("openai-codex", "gpt-6-sol")


def test_disabled_or_excluded_local_seats_do_not_reappear():
    from hermes_cli.seat_menu import install_seat_menu

    with patch("hermes_cli.auth.is_provider_explicitly_configured", return_value=True):
        rows = install_seat_menu(
            [_row("anthropic", "Anthropic", ["claude-opus-5"])],
            excluded_providers=["claude-broker"],
            user_providers={"codex-bunker": {"enabled": False}},
        )
    assert [row["slug"] for row in rows] == ["anthropic"]


def test_local_seat_switch_accepts_without_a_network_probe():
    broker = validate_requested_model("opus", "claude-broker", api_mode="anthropic_messages", base_url="claude-broker://local")
    bunker = validate_requested_model("gpt-6-sol", "codex-bunker", base_url="codex-bunker://local")
    newer = validate_requested_model("gpt-6.1-sol", "codex-bunker", base_url="codex-bunker://local")
    rejected = validate_requested_model("gpt-6-sol-900k", "codex-bunker", base_url="codex-bunker://local")
    assert broker["accepted"] is True
    assert broker["message"] in (None, "")
    assert bunker["accepted"] is True
    assert newer["accepted"] is True
    assert rejected["accepted"] is False
    assert "Bunker Codex" in rejected["message"]
