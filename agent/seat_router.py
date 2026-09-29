"""Choose the seat before the mouth speaks.

Short ordinary chat stays on the saved mouth. A long desktop or CLI turn asks
Sonnet 5.5 first. Trading and code go to Codex Sol for that turn only. Clinic
text is not sent to Sonnet; the vendor wire refuses it.
"""

from __future__ import annotations

import logging
import os
import subprocess

from agent.clinic_wire import refusal_for

logger = logging.getLogger(__name__)

SCALE = "/Users/spinec/bin/hermes-scale"
ORDINARY_LIMIT = 280
SOL_MODEL = "gpt-6-sol"
PHONES = {
    "telegram", "discord", "slack", "whatsapp", "matrix", "mattermost",
    "email", "cron", "signal", "homeassistant",
}
DROP_ENV = {
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_UNIX_SOCKET",
    "CLAUDE_BROKER_CONFIG_DIR",
    "CLAUDE_BROKER_MANAGED",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CONFIG_DIR",
    "CODEX_HOME",
}
SNAPSHOT_FIELDS = (
    "provider", "requested_provider", "model", "api_mode", "base_url", "api_key",
    "client", "_client_kwargs", "_disable_streaming",
    "_anthropic_client", "_anthropic_api_key", "_anthropic_base_url", "_is_anthropic_oauth",
    "_use_prompt_caching", "_use_native_cache_layout",
    "_fallback_activated", "_provider_fallback_active",
)


def user_text(message):
    if isinstance(message, str):
        return message
    if isinstance(message, dict):
        return user_text(message.get("content"))
    if not isinstance(message, list):
        return None
    parts = []
    for item in message:
        if isinstance(item, str):
            parts.append(item)
            continue
        if not isinstance(item, dict):
            return None
        if item.get("type") in {"image", "image_url", "input_image"}:
            return None
        text = item.get("text")
        if isinstance(text, str):
            parts.append(text)
            continue
        return None
    return "\n".join(parts)


def _classifier_env():
    env = os.environ.copy()
    for key in list(env):
        if key in DROP_ENV or key.startswith("CODEX_BUNKER_"):
            env.pop(key, None)
    return env


def classify(text: str) -> str:
    try:
        done = subprocess.run(
            [SCALE, "route"],
            input=text,
            text=True,
            capture_output=True,
            timeout=170,
            env=_classifier_env(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return "stay"
    if done.returncode != 0:
        return "stay"
    word = (done.stdout or "").strip().split()
    if not word:
        return "stay"
    token = word[0].strip(".,:;").lower()
    if token in {"ordinary", "stay", "move", "clinical"}:
        return token
    return "stay"


def _snapshot(agent):
    snap = {}
    for name in SNAPSHOT_FIELDS:
        if not hasattr(agent, name):
            continue
        value = getattr(agent, name)
        snap[name] = dict(value) if isinstance(value, dict) else value
    return snap


def _swap_to_sol(agent):
    snap = _snapshot(agent)
    try:
        from agent.codex_bunker_adapter import CodexBunkerClient
        agent.provider = "codex-bunker"
        agent.requested_provider = "codex-bunker"
        agent.model = SOL_MODEL
        agent.api_mode = "chat_completions"
        agent.base_url = "codex-bunker://local"
        agent.api_key = ""
        agent._disable_streaming = True
        agent.client = CodexBunkerClient(agent)
    except Exception:
        restore_seat(agent, snap)
        raise
    logger.info("seat route move: %s -> %s", snap.get("model"), SOL_MODEL)
    return snap


def handoff_to_sol(agent, user_message):
    """Return a restore snapshot when this turn should speak as Sol. None keeps the mouth."""
    try:
        if getattr(agent, "provider", None) != "claude-broker":
            return None
        platform = str(getattr(agent, "platform", "") or "")
        if platform in PHONES:
            return None
        text = user_text(user_message)
        if text is None:
            return None
        stripped = text.strip()
        if not stripped or stripped.lower().startswith("quad:"):
            return None
        if refusal_for(stripped, vendor="claude"):
            return None
        low = stripped.lower()
        explicit = low.startswith(("scale move:", "scale stay:", "scale clinical:"))
        if not explicit and len(stripped) < ORDINARY_LIMIT:
            return None
        route = classify(stripped)
        if route != "move":
            logger.info("seat route %s: mouth stays", route)
            return None
        return _swap_to_sol(agent)
    except Exception:
        logger.warning("seat route skipped", exc_info=True)
        return None


def restore_seat(agent, snapshot) -> None:
    if not isinstance(snapshot, dict):
        return
    current = getattr(agent, "client", None)
    for name, value in snapshot.items():
        setattr(agent, name, value)
    if current is not None and current is not snapshot.get("client"):
        close = getattr(current, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                logger.debug("seat route close failed", exc_info=True)
