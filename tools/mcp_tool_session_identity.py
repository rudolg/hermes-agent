"""Per-call Desktop identity for an explicitly selected local Atlas connection.

This is client attribution, not permission: Atlas separately verifies its native
parent and an owner's exact conversation grant. Pool membership grants nothing.
"""

from __future__ import annotations

import sys
from pathlib import Path

from tools.mcp_tool_common import _core

ATLAS_SESSION_META = "atlas/hermes-session/v1"


def validate_session_identity_config(config: dict) -> bool:
    """Return whether attribution was requested; reject unsupported destinations."""
    if "session_identity" not in config:
        return False
    if config["session_identity"] != ATLAS_SESSION_META:
        raise ValueError("Unsupported MCP session_identity; expected atlas/hermes-session/v1")
    if ("url" in config or config.get("transport") not in (None, "stdio")
            or not isinstance(config.get("command"), str) or not config["command"].strip()):
        raise ValueError("MCP session_identity is available only for a local stdio Atlas server")
    return True


def _bound_text(var, label: str, *, limit: int = 128) -> str:
    value = var.get()
    if (not isinstance(value, str) or not 1 <= len(value) <= limit
            or value != value.strip() or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise ValueError(f"Atlas requires an explicitly bound {label}")
    return value


def _desktop_principal(framework_session_id: str | None) -> dict:
    from agent.delegation_context import is_delegated_child_process_context
    from gateway import session_context as context
    from hermes_constants import get_hermes_home_override

    if is_delegated_child_process_context():
        raise ValueError("Atlas Desktop access is not inherited by delegated agents")
    session_id = _bound_text(context._SESSION_ID, "session id")
    ui_session_id = _bound_text(context._SESSION_UI_SESSION_ID, "UI session id")
    session_key = _bound_text(context._SESSION_KEY, "session key")
    if (_bound_text(context._SESSION_SOURCE, "session source") != "desktop"
            or framework_session_id != session_id):
        raise ValueError("Atlas requires the calling Desktop agent's exact session identity")

    # Do not import/start a gateway to manufacture a live session for a CLI caller.
    gateway = sys.modules.get("tui_gateway.server")
    if gateway is None:
        raise ValueError("Atlas requires a live Hermes Desktop conversation")
    with gateway._sessions_lock:
        live = gateway._sessions.get(ui_session_id)
        if (not isinstance(live, dict) or live.get("_finalized") or live.get("source") != "desktop"
                or live.get("session_key") != session_key
                or getattr(live.get("agent"), "session_id", None) != session_id):
            raise ValueError("Atlas Desktop identity does not match the live main conversation")
        # A launch-profile record deliberately stores None. Its captured gateway home
        # is authoritative; reading get_hermes_home()/os.environ here could borrow a
        # different conversation's ambient profile.
        recorded_home = live.get("profile_home")
        launch_home = gateway._hermes_home
        home = Path(recorded_home or launch_home).resolve()
        override = get_hermes_home_override()
        if (recorded_home and not override) or Path(override or launch_home).resolve() != home:
            raise ValueError("Atlas Desktop identity does not match the bound profile")
    return {"session_id": session_id, "ui_session_id": ui_session_id,
            "profile_home": str(home), "source": "desktop", "delegated": False}


def session_identity_meta(server_name: str, arguments: dict, framework_session_id: str | None) -> dict | None:
    """Validate before lazy connection/revival; arguments never supply identity."""
    from tools.mcp_tool_scope import _resolve_server_key

    with _core._lock:
        key = _resolve_server_key(server_name)
        server = _core._servers.get(key)
        config = _core._lazy_server_configs.get(key)
        if config is None:
            config = getattr(server, "_config", None)
    if not isinstance(config, dict) or not validate_session_identity_config(config):
        return None
    if "_meta" in arguments or ATLAS_SESSION_META in arguments:
        raise ValueError("Atlas session metadata cannot be supplied in tool arguments")
    return {ATLAS_SESSION_META: _desktop_principal(framework_session_id)}
