"""Chat-menu rows for the local Claude broker and Codex bunker.

The desktop chat menu sorts provider groups by display name. These names are
chosen so that sort puts the broker first and the bunker second, then the
direct ChatGPT subscription, then every other source. Inside each door the
rows follow the shelf, then the newest version: Fable, Opus, Sonnet, Haiku,
and Astra, Sol, Luna, Terra.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

_BROKER_SLUG = "claude-broker"
_BUNKER_SLUG = "codex-bunker"
_BROKER_NAME = "Anthropic broker (Claude subscription)"
_BUNKER_NAME = "Bunker Codex (bunker accounts)"
_SUBSCRIPTION_NAME = "ChatGPT subscription (direct login)"
_BROKER_ALIASES = ("fable", "opus", "sonnet", "haiku")
_BUNKER_ALIASES = ("astra", "sol", "luna", "terra")
_BROKER_RANK = {name: index for index, name in enumerate(_BROKER_ALIASES)}
_BUNKER_RANK = {name: index for index, name in enumerate(_BUNKER_ALIASES)}
_CLAUDE_FAMILY = re.compile(
    r"claude-(?:(?:opus|sonnet|haiku|fable)-[0-9]{1,8}(?:-[0-9]{1,8}){0,4}"
    r"|[0-9]-[0-9]-(?:opus|sonnet|haiku)-[0-9]{8})\Z"
)
_MODERN_CLAUDE = re.compile(r"claude-(opus|sonnet|haiku|fable)-(\d+(?:-\d+){0,4})\Z")
_LEGACY_CLAUDE = re.compile(r"claude-(\d+)-(\d+)-(opus|sonnet|haiku)-(\d{8})\Z")
_CODEX_FAMILY = re.compile(r"gpt-(\d+)(?:\.(\d+))?-(sol|luna|astra|terra)\Z")
_DATE_TAIL = re.compile(r"-\d{8}\Z")
_DOT_VERSION = re.compile(r"(?<=\d)\.(?=\d)")
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


def _claude_candidate(model: str) -> tuple[str, tuple[int, ...], bool, str] | None:
    """Family, version, undated flag, and the id to list. A date pin is not a version."""
    name = _DOT_VERSION.sub("-", str(model).strip())
    modern = _MODERN_CLAUDE.fullmatch(name)
    if modern:
        parts = [int(part) for part in modern.group(2).split("-")]
        dated = len(parts) >= 2 and len(str(parts[-1])) == 8
        version = tuple(parts[:-1] if dated else parts)
        if not version:
            return None
        return modern.group(1), version, not dated, name
    legacy = _LEGACY_CLAUDE.fullmatch(name)
    if legacy is None:
        return None
    return legacy.group(3), (int(legacy.group(1)), int(legacy.group(2))), False, name


def _sorted_versions(
    chosen: dict[tuple[str, tuple[int, ...]], str], rank: dict[str, int],
) -> list[str]:
    """Shelf order, then newest version. Opus 5.5 comes before Opus 5 and Opus 4.8."""
    def key(item: tuple[str, tuple[int, ...]]) -> tuple[int, tuple[int, ...]]:
        version = item[1] + (0,) * (6 - len(item[1]))
        return rank.get(item[0], len(rank)), tuple(-part for part in version)

    return [chosen[item] for item in sorted(chosen, key=key)]


def broker_menu_models(claude_ids: list[str] | None = None) -> list[str]:
    """Every concrete Claude version, by name then version. No bare alias beside them."""
    chosen: dict[tuple[str, tuple[int, ...]], tuple[bool, str]] = {}
    for model in claude_ids or []:
        parsed = _claude_candidate(str(model))
        if parsed is None:
            continue
        family, version, undated, emit = parsed
        key = (family, version)
        current = chosen.get(key)
        if current is None or (undated and not current[0]):
            chosen[key] = (undated, emit)
    if not chosen:
        return list(_BROKER_ALIASES)
    return _sorted_versions({key: emit for key, (_, emit) in chosen.items()}, _BROKER_RANK)


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


def _codex_identity(model: str) -> str | None:
    """Undated family id, or None for spark, 900k, pro, and anything else the bunker does not serve."""
    name = _DATE_TAIL.sub("", str(model).strip())
    return name if _CODEX_FAMILY.fullmatch(name) else None


def bunker_menu_models(subscription_ids: list[str] | None = None) -> list[str]:
    """Every bunker family version, by name then version. Subscription-only ids such as gpt-6.1-sol join this door."""
    chosen: dict[tuple[str, tuple[int, int]], str] = {}
    for model in [*bunker_concrete_models(), *(subscription_ids or [])]:
        ident = _codex_identity(str(model))
        if ident is None:
            continue
        match = _CODEX_FAMILY.fullmatch(ident)
        if match is None:
            continue
        chosen.setdefault((match.group(3), (int(match.group(1)), int(match.group(2) or 0))), ident)
    if not chosen:
        return list(_BUNKER_ALIASES)
    return _sorted_versions(chosen, _BUNKER_RANK)


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
        row["name"] = _SUBSCRIPTION_NAME
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
    subscription_ids: list[str] = []
    for row in rows:
        slug = str(row.get("slug") or "").strip().lower()
        models = [str(model) for model in (row.get("models") or [])]
        if slug == "anthropic":
            claude_ids = models
        elif slug == "openai-codex" and not subscription_ids:
            subscription_ids = models

    seat: list[dict] = []
    if seat_enabled(_BROKER_SLUG) and is_provider_explicitly_configured(_BROKER_SLUG):
        seat.append(_seat_row(
            _BROKER_SLUG, _BROKER_NAME, broker_menu_models(claude_ids), current, "local_broker"))
    if seat_enabled(_BUNKER_SLUG) and is_provider_explicitly_configured(_BUNKER_SLUG):
        seat.append(_seat_row(
            _BUNKER_SLUG, _BUNKER_NAME, bunker_menu_models(subscription_ids), current, "local_bunker"))
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
