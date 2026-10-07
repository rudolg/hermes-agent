"""Exercise the registry -> MCP handler -> provider boundary with real session scopes.

Provider responses are fixtures; these tests do not claim live Desktop or Atlas access.
"""

import asyncio
import json
import threading
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tools.mcp_tool_session_identity import ATLAS_SESSION_META


@pytest.fixture
def dispatch_rig(tmp_path, monkeypatch):
    from agent.secret_scope import set_multiplex_active
    from tools import mcp_tool, mcp_tool_loop
    from tools.mcp_tool_handlers import _make_tool_handler
    from tools.registry import registry
    from tui_gateway import server as gateway

    set_multiplex_active(True)
    monkeypatch.setattr(gateway, "_sessions", {})
    monkeypatch.setattr(gateway, "_hermes_home", tmp_path / "launch")
    for name in ("_servers", "_lazy_server_configs", "_server_tool_scopes", "_server_error_counts",
                 "_server_trust_levels", "_tool_read_only_hints"):
        monkeypatch.setattr(mcp_tool, name, {})
    calls = []

    async def call_tool(name, arguments, **kwargs):
        from mcp.types import CallToolRequestParams
        # The installed SDK must preserve the agreed extension on the JSON wire.
        frame = CallToolRequestParams(name=name, arguments=arguments, _meta=kwargs.get("meta"))
        assert frame.model_dump(by_alias=True, exclude_none=True).get("_meta") == kwargs.get("meta")
        calls.append((name, arguments, kwargs))
        return SimpleNamespace(content=[SimpleNamespace(type="text", text="ok")], isError=False)

    provider = SimpleNamespace(call_tool=call_tool)
    transport = SimpleNamespace(session=provider, _rpc_lock=asyncio.Lock(),
                                _config={"command": "node", "args": ["atlas.js"],
                                         "session_identity": ATLAS_SESSION_META})

    monkeypatch.setattr(mcp_tool, "_mcp_loop", None)
    monkeypatch.setattr(mcp_tool, "_mcp_thread", None)
    mcp_tool_loop._ensure_mcp_loop()
    ready = threading.Event()
    mcp_tool._mcp_loop.call_soon_threadsafe(ready.set)
    assert ready.wait(5)
    registry.register(name="mcp_identity_fixture", toolset="mcp-fixture", schema={},
                      handler=_make_tool_handler("fixture", "probe", 5), check_fn=lambda: True)

    @contextmanager
    def conversation(label="a", *, launch=False):
        from hermes_constants import set_hermes_home_override, reset_hermes_home_override
        from tools.mcp_tool_scope import _server_key

        home = tmp_path / label
        home.mkdir(exist_ok=True)
        sid, ui = f"desktop-session-{label}", f"ui-{label}"
        record = {"agent": SimpleNamespace(session_id=sid), "source": "desktop", "session_key": sid,
                  "profile_home": None if launch else str(home)}
        gateway._sessions[ui] = record
        token = set_hermes_home_override(None if launch else str(home))
        # Use the production Desktop turn binder, not a hand-built ContextVar payload.
        session_tokens = gateway._set_session_context(sid, ui_session_id=ui)
        mcp_tool._servers[_server_key("fixture")] = transport
        try:
            yield SimpleNamespace(sid=sid, ui=ui, record=record,
                                  home=gateway._hermes_home if launch else home)
        finally:
            gateway._clear_session_context(session_tokens)
            reset_hermes_home_override(token)

    yield SimpleNamespace(calls=calls, server=transport, provider=provider, context=conversation,
                          dispatch=lambda args, **kwargs: registry.dispatch("mcp_identity_fixture", args, **kwargs),
                          core=mcp_tool, gateway=gateway)
    registry.deregister("mcp_identity_fixture")
    mcp_tool_loop._stop_mcp_loop()


def test_identity_follows_the_calling_main_conversation_and_stays_local(dispatch_rig):
    from tools.mcp_tool_registration import _connection_identity
    rig = dispatch_rig
    config = dict(rig.server._config)
    for label in ("a", "b", "a"):
        with rig.context(label) as caller:
            assert "error" not in rig.dispatch({"symbol": "TEST"}, session_id=caller.sid)
            _, arguments, kwargs = rig.calls[-1]
            assert arguments == {"symbol": "TEST"}
            assert kwargs == {"meta": {ATLAS_SESSION_META: {
                "session_id": caller.sid, "ui_session_id": caller.ui,
                "profile_home": str(caller.home.resolve()), "source": "desktop", "delegated": False}}}
    assert rig.calls[0][2] == rig.calls[2][2] != rig.calls[1][2]
    with rig.context("launch", launch=True) as caller:
        assert "error" not in rig.dispatch({}, session_id=caller.sid)
        assert rig.calls[-1][2]["meta"][ATLAS_SESSION_META]["profile_home"] == str(caller.home.resolve())
    rig.server._config = {"url": "https://mcp.tradingview.com/mcp"}
    rig.core._servers["fixture"] = rig.server
    assert "error" not in rig.dispatch({}, session_id="unbound")
    assert rig.calls[-1][2] == {}  # No Desktop identifiers leave via native HTTP or unrelated MCPs.
    unmarked = {key: value for key, value in config.items() if key != "session_identity"}
    assert _connection_identity(config) != _connection_identity(unmarked)


@pytest.mark.parametrize("case", [
    "child", "missing_framework", "wrong_framework", "missing_context", "env_only", "wrong_source",
    "wrong_main", "wrong_ui", "wrong_key", "wrong_profile", "finalized", "forged_meta", "forged_key",
    "unknown_contract", "non_string_contract", "null_contract", "http_contract", "sse_contract", "lazy_child",
    "oversize_id", "del_id",
])
def test_unattributable_calls_never_reach_or_start_transport(dispatch_rig, monkeypatch, case):
    from agent.delegation_context import delegated_child_context
    from gateway import session_context as context
    from tools import mcp_tool_handlers
    from tools.mcp_tool_scope import _server_key
    from tools.mcp_tool_transport import _connect_inputs

    rig = dispatch_rig
    acquire = AsyncMock()  # It must never be used, including lazy connection attempts.
    monkeypatch.setattr(mcp_tool_handlers, "_acquire_call_server", acquire)
    with rig.context() as caller:
        args, kwargs = {}, {"session_id": caller.sid}
        if case == "missing_framework":
            kwargs = {}
        elif case == "wrong_framework":
            kwargs["session_id"] = "another-agent"
        elif case in {"oversize_id", "del_id"}:
            value = "x" * 129 if case == "oversize_id" else caller.sid + "\x7f"
            context._SESSION_ID.set(value)
            kwargs["session_id"] = value
        elif case == "missing_context":
            context._SESSION_UI_SESSION_ID.set("")
        elif case == "env_only":
            for var, value in ((context._SESSION_ID, caller.sid), (context._SESSION_UI_SESSION_ID, caller.ui)):
                monkeypatch.setenv(var.name, value)
                var.set(context._UNSET)
            args = {"session_id": caller.sid, "ui_session_id": caller.ui, "profile_home": str(caller.home)}
        elif case == "wrong_source":
            caller.record["source"] = "tui"
        elif case == "wrong_main":
            caller.record["agent"].session_id = "child-agent"
        elif case == "wrong_ui":
            context._SESSION_UI_SESSION_ID.set("unknown-ui")
        elif case == "wrong_key":
            context._SESSION_KEY.set("another-key")
        elif case == "wrong_profile":
            caller.record["profile_home"] = str(caller.home / "different")
        elif case == "finalized":
            caller.record["_finalized"] = True
        elif case == "forged_meta":
            args = {"_meta": {ATLAS_SESSION_META: {"session_id": caller.sid}}}
        elif case == "forged_key":
            args = {ATLAS_SESSION_META: {"session_id": caller.sid}}
        elif case in {"unknown_contract", "non_string_contract", "null_contract"}:
            rig.server._config["session_identity"] = {"unknown_contract": "unknown", "non_string_contract": {},
                                                      "null_contract": None}[case]
        elif case == "http_contract":
            rig.server._config["url"] = "https://mcp.tradingview.com/mcp"
        elif case == "sse_contract":
            rig.server._config["transport"] = "sse"
        if case in {"child", "lazy_child"}:
            if case == "lazy_child":
                key = _server_key("fixture")
                rig.core._lazy_server_configs[key] = rig.server._config
                rig.core._servers.pop(key)
            with delegated_child_context(caller.sid):
                result = rig.dispatch(args, **kwargs)
        else:
            result = rig.dispatch(args, **kwargs)
        assert "error" in json.loads(result)
        assert rig.calls == []
        acquire.assert_not_called()
        if case.endswith("contract"):
            with pytest.raises(ValueError):
                _connect_inputs("fixture", rig.server._config)
