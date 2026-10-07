"""A Desktop helper import must not turn its MCP catalogue into the messaging gateway's."""

import os
from pathlib import Path
import subprocess
import sys


def test_desktop_helper_preserves_enabled_mcp_catalogue(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text(
        "agent:\n  coding_context: off\n"
        "mcp_servers:\n  desktop_fixture:\n"
        "    enabled: true\n    gateway: false\n    command: fixture\n",
        encoding="utf-8",
    )
    env = {k: v for k, v in os.environ.items() if k.upper() in {
        "PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT",
    }}
    for key in ("HOME", "USERPROFILE", "HERMES_HOME", "APPDATA", "LOCALAPPDATA", "TEMP", "TMP"):
        env[key] = str(home)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
    result = subprocess.run([sys.executable, "-c", r'''
import asyncio
from unittest.mock import patch

from tui_gateway import server
from tools.registry import registry
from model_tools import get_tool_definitions, handle_function_call

name = "mcp__desktop_fixture__probe"
registry.register(
    name=name, toolset="mcp-desktop_fixture",
    schema={"name": name, "description": "Fixture read", "parameters": {"type": "object", "properties": {}}},
    handler=lambda args, **kwargs: '{"fixture": "called"}',
    check_fn=lambda: True,
)
registry.register_toolset_alias("desktop_fixture", "mcp-desktop_fixture")

def fresh_desktop_catalogue():
    enabled = server._load_enabled_toolsets("desktop")
    tools = get_tool_definitions(enabled_toolsets=enabled, quiet_mode=True, skip_tool_search_assembly=True)
    return enabled, {tool["function"]["name"] for tool in tools}

before, before_names = fresh_desktop_catalogue()
assert name in before_names, ("fixture absent before helper", before)
# This is a real Desktop session helper: importing GatewayRunner for the shared
# compression signature used to set the messaging-process marker as a side effect.
server._tui_compression_config_signature({})
after, after_names = fresh_desktop_catalogue()
assert name in after_names, ("Desktop helper removed its configured MCP server", before, after)
assert handle_function_call(name, {}, enabled_toolsets=after) == '{"fixture": "called"}'

# Actual messaging startup must still exclude the desktop-only server before
# discovery; stop at its first OS-setup seam, before locks, adapters or network.
from gateway.run import start_gateway
from hermes_cli import resource_limits
class StopBeforeStartup(Exception):
    pass
with patch.object(resource_limits, "apply_nofile_soft_limit", side_effect=StopBeforeStartup):
    try:
        asyncio.run(start_gateway())
    except StopBeforeStartup:
        pass
    else:
        raise AssertionError("startup seam was not reached")
from hermes_cli.config import load_config
from hermes_cli.tools_config import enabled_mcp_server_names
assert "desktop_fixture" not in enabled_mcp_server_names(load_config())
'''], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, result.stdout + result.stderr
