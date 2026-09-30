"""Chat-menu rows for the local Claude broker and Codex bunker.

The desktop chat menu sorts provider groups by display name. These names are
chosen so that sort puts the broker first and the bunker second, above every
other source: "Anthropic broker", then "Bunker Codex", then "ChatGPT
subscription" and the rest.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

_BROKER_SLUG = "claude-broker"
_BUNKER_SLUG = "codex-bunker"
_BROKER_NAME = "Anthropic broker"
_BUNKER_NAME = "Bunker Codex"
_BROKER_ALIASES = ("opus", "fable", "sonnet", "haiku")
_BUNKER_ALIASES = ("sol", "astra", "luna", "terra")
_FAMILY_ORDER = {"sol": 0, "astra": 1, "luna": 2, "terra": 3}
_CLAUDE_FAMILY = re.compile(
    r"claude-(?:(?:opus|sonnet|haiku|fable)-[0-9]{1,8}(?:-[0-9]{1,8}){0,4}"
    r"|[0-9]-[0-9]-(?:opus|sonnet|haiku)-[0-9]{8})\Z"
)
_CODEX_FAMILY = re.compile(r"gpt-(\d+)(?:\.(\d+))?-(sol|luna|astra|terra)\Z")
_MONITORED_MODELS = re.compile(r"^MONITORED_MODEL_IDS = \((.*?)\)", re.M | re.S)
_QUOTED = re.compile(r"""['"]([^'"]+)['"]""")
_FALLBACK_BUNKER_MODELS = (
    "gpt-6-sol",
    "gpt-6-astra",
    "gpt-6-luna",
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "gpt-5.6-luna",
)


def seat_accepts(provider: str, model: str) -> bool:
    """True when this id is served by the broker or the bunker, not by another login."""
    slug = (provider or "").strip().lower()
    name = (model or "").strip()
    if slug == _BROKER_SLUG:
        return name in _BROKER_ALIASES or _CLAUDE_FAMILY.fullmatch(name) is not None
    if slug == _BUNKER_SLUG:
        return name in _BUNKER_ALIASES or _CODEX_FAMILY.fullmatch(name) is not None
    return False


def broker_menu_models(claude_ids: list[str] | None = None) -> list[str]:
    """Family aliases first (newest of that class), then exact Claude ids."""
    models = list(_BROKER_ALIASES)
    seen = set(models)
    for model in claude_ids or []:
        if model in seen or _CLAUDE_FAMILY.fullmatch(str(model)) is None:
            continue
        models.append(str(model))
        seen.add(model)
    return models


def bunker_concrete_models() -> list[str]:
    """Model ids the live bunker release monitors. File read only; the service is not imported."""
    try:
        meta = json.loads((Path.home() / ".local/lib/codex-bunker/current.json").read_text())
        text = (Path(str(meta.get("runtime") or "")) / "codex_accounts.py").read_text(encoding="utf-8")
    except (OSError, json.JSONDecodeError, TypeError, UnicodeError):
        return list(_FALLBACK_BUNKER_MODELS)
    match = _MONITORED_MODELS.search(text)
    if match is None:
        return list(_FALLBACK_BUNKER_MODELS)
    found = [model for model in _QUOTED.findall(match.group(1)) if _CODEX_FAMILY.fullmatch(model)]
    return found or list(_FALLBACK_BUNKER_MODELS)


def _newest_first(models: list[str]) -> list[str]:
    def key(model: str) -> tuple[int, int, int, str]:
        match = _CODEX_FAMILY.fullmatch(model)
        if match is None:
            return (0, 0, 9, model)
        return (
            -int(match.group(1)),
            -int(match.group(2) or 0),
            _FAMILY_ORDER.get(match.group(3), 9),
            model,
        )

    return sorted(models, key=key)


def bunker_menu_models() -> list[str]:
    """Class aliases first, then the bunker's own concrete ids, newest first."""
    concrete = _newest_first([model for model in bunker_concrete_models() if _CODEX_FAMILY.fullmatch(model)])
    models = list(_BUNKER_ALIASES)
    seen = set(models)
    for model in concrete:
        if model in seen:
            continue
        models.append(model)
        seen.add(model)
    return models


def _seat_row(slug: str, name: str, models: list[str], current: str, auth_type: str) -> dict:
    return {
        "slug": slug,
        "name": name,
        "is_current": current == slug,
        "is_user_defined": False,
        "models": list(models),
        "total_models": len(models),
        "source": "hermes",
        "authenticated": True,
        "auth_type": auth_type,
    }


def _relabel_other_sources(row: dict) -> None:
    slug = str(row.get("slug") or "").strip().lower()
    if slug == "openai-codex":
        row["name"] = "ChatGPT subscription"
        return
    url = str(row.get("api_url") or "")
    if ":8765" in url and ("127.0.0.1" in url or "localhost" in url):
        row["name"] = "Codex loopback"


def install_seat_menu(
    rows: list[dict], current_provider: str = "", *,
    excluded_providers: list[str] | None = None, user_providers: dict | None = None,
) -> list[dict]:
    """Put the broker and bunker above every other source, and name the other doors for what they are.

    The direct Anthropic catalog row is dropped once the broker row is present: those Claude ids
    are the broker list. The ChatGPT subscription row and the port-8765 loopback stay, under names
    that do not say they are the bunker.
    """
    from hermes_cli.auth import is_provider_explicitly_configured
    from hermes_cli.config import is_provider_enabled

    current = (current_provider or "").strip().lower()
    excluded = {str(slug).strip().lower() for slug in (excluded_providers or [])}
    configured = user_providers if isinstance(user_providers, dict) else {}

    def seat_enabled(slug: str) -> bool:
        entry = configured.get(slug)
        return slug not in excluded and (
            not isinstance(entry, dict) or is_provider_enabled(entry))

    claude_ids: list[str] = []
    for row in rows:
        if str(row.get("slug") or "").strip().lower() == "anthropic":
            claude_ids = [str(model) for model in (row.get("models") or [])]
            break

    seat: list[dict] = []
    if seat_enabled(_BROKER_SLUG) and is_provider_explicitly_configured(_BROKER_SLUG):
        seat.append(_seat_row(
            _BROKER_SLUG, _BROKER_NAME, broker_menu_models(claude_ids), current, "local_broker"))
    if seat_enabled(_BUNKER_SLUG) and is_provider_explicitly_configured(_BUNKER_SLUG):
        seat.append(_seat_row(
            _BUNKER_SLUG, _BUNKER_NAME, bunker_menu_models(), current, "local_bunker"))
    if not seat:
        return rows

    hidden = {_BROKER_SLUG, _BUNKER_SLUG}
    if any(row["slug"] == _BROKER_SLUG for row in seat):
        hidden.add("anthropic")
    kept: list[dict] = []
    for row in rows:
        if str(row.get("slug") or "").strip().lower() in hidden:
            continue
        _relabel_other_sources(row)
        kept.append(row)
    return seat + kept
