"""``mcp_servers.<name>.gateway: false`` keeps an entry out of the messaging gateway only.

The gateway and the desktop backend load every enabled entry through the same reader, so an entry that
owns an exclusive resource (a TradingView chart lane: ONE controller claim per lane) was spawned twice and
the claim went to whichever process connected first (measured 2026-10-01: the gateway took `hermes-atlas`
before the desktop had loaded the entry). The gateway identifies itself with ``_HERMES_GATEWAY=1``; every
other process keeps the entry exactly as ``enabled`` says.
"""
from __future__ import annotations

import pytest

from tools.mcp_tool_common import mcp_server_enabled, mcp_server_scoped_out_of_gateway


@pytest.fixture
def gateway_process(monkeypatch):
    monkeypatch.setenv("_HERMES_GATEWAY", "1")


@pytest.fixture
def other_process(monkeypatch):
    monkeypatch.delenv("_HERMES_GATEWAY", raising=False)


@pytest.mark.parametrize("value", [False, "false", "no", "off", 0])
def test_gateway_false_is_off_inside_the_gateway(gateway_process, value):
    cfg = {"enabled": True, "gateway": value}
    assert mcp_server_scoped_out_of_gateway(cfg) is True
    assert mcp_server_enabled(cfg) is False


@pytest.mark.parametrize("value", [False, "false", 0])
def test_gateway_false_changes_nothing_outside_the_gateway(other_process, value):
    cfg = {"enabled": True, "gateway": value}
    assert mcp_server_scoped_out_of_gateway(cfg) is False
    assert mcp_server_enabled(cfg) is True


@pytest.mark.parametrize("cfg", [{}, {"gateway": None}, {"gateway": True}, {"gateway": "yes"}, {"gateway": "nonsense"}])
def test_absent_or_true_or_unparseable_gateway_keeps_the_entry_in_scope(gateway_process, cfg):
    assert mcp_server_scoped_out_of_gateway(cfg) is False
    assert mcp_server_enabled(cfg) is True


def test_enabled_false_stays_off_everywhere(gateway_process):
    assert mcp_server_enabled({"enabled": False, "gateway": True}) is False
    assert mcp_server_enabled({"enabled": False}) is False


def test_negative_control_the_marker_value_must_be_exactly_1(monkeypatch):
    monkeypatch.setenv("_HERMES_GATEWAY", "true")
    assert mcp_server_scoped_out_of_gateway({"gateway": False}) is False
