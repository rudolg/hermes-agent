import json
from types import SimpleNamespace

import httpx
import pytest

from agent import claude_broker_transport as broker
from run_agent import AIAgent


class Native(httpx.BaseTransport):
    def __init__(self, body):
        self.body = body
        self.requests = []

    def handle_request(self, request):
        self.requests.append(request)
        return httpx.Response(200, content=self.body, request=request)


@pytest.mark.parametrize("historical_text,allowed", [
    ("The repository is /usr/local/PatentVault/LOCAL_Patents_Never_Cloud/PROJECTS. Continue the code task.", True),
    ("The worktrees directory is /usr/local/PatentVault/LOCAL_Patents_Never_Cloud/PROJECTS-worktrees.", True),
    ("Read /usr/local/PatentVault/LOCAL_Patents_Never_Cloud/PROJECTS/patient-records/demo.txt", True),
    ("Read /usr/local/PatentVault/LOCAL_Patents_Never_Cloud/PROJECTS-private/note.txt", True),
    ("Read /usr/local/PatentVault/LOCAL_Patents_Never_Cloud/PROJECTS/../../patient.txt", True),
    ("Read /usr/local/PatentVault/LOCAL_Patents_Never_Cloud/patient.txt", True),
    ("The repository is /usr/local/PatentVault/LOCAL_Patents_Never_Cloud/PROJECTS. Hospital number 0000000.", True),
])
def test_project_history_reaches_provider_but_clinical_history_stays_local(monkeypatch, tmp_path, historical_text, allowed):
    controls = []
    monkeypatch.setattr(broker, "_control", lambda _root, payload: controls.append(payload) or {"ok": True, "lease": "test-lease"})
    transport = broker.ClaudeBrokerTransport(tmp_path)
    native = Native(b"PROVIDER_REACHED")
    transport.native = native
    body = {"model": "claude-opus-5-5", "messages": [
        {"role": "user", "content": historical_text},
        {"role": "assistant", "content": "Acknowledged."},
        {"role": "user", "content": "and?"},
    ]}
    with httpx.Client(transport=transport) as client:
        response = client.post("http://broker.local/v1/messages", json=body)
        if allowed:
            assert response.content == b"PROVIDER_REACHED"
            assert json.loads(native.requests[0].content) == body
        else:
            assert response.json()["id"] == "msg_local_clinic_refusal"
    assert len(native.requests) == int(allowed)
    assert len(controls) == 2 * int(allowed)


def test_native_tool_use_is_returned_unchanged_and_lease_closes(monkeypatch, tmp_path):
    controls = []

    def control(_root, payload):
        controls.append(payload)
        return {"ok": True, "lease": "test-lease"}

    monkeypatch.setattr(broker, "_control", control)
    body = json.dumps({"content": [{"type": "tool_use", "id": "toolu_test",
                                   "name": "research_lookup", "input": {"query": "synthetic"}}]}).encode()
    transport = broker.ClaudeBrokerTransport(tmp_path)
    native = Native(body)
    transport.native = native
    with httpx.Client(transport=transport) as client:
        response = client.post("http://broker.local/v1/messages", json={"model": "claude-sonnet-5"})
        assert response.content == body
    assert [row["op"] for row in controls] == ["register", "close"]
    assert controls[0]["client_kind"] == controls[0]["route"] == "hermes"
    assert controls[1]["lease"] == "test-lease"
    assert native.requests[0].url.path == "/v1/messages"
    assert native.requests[0].headers["x-hermes-broker-session"] == controls[0]["session"]


def test_failed_registration_never_sends_provider_request(monkeypatch, tmp_path):
    def control(_root, _payload):
        raise RuntimeError("Claude Broker: audit_unavailable")

    monkeypatch.setattr(broker, "_control", control)
    transport = broker.ClaudeBrokerTransport(tmp_path)
    native = Native(b"wrong")
    transport.native = native
    with httpx.Client(transport=transport) as client:
        with pytest.raises(RuntimeError, match="audit_unavailable"):
            client.post("http://broker.local/v1/messages", json={"model": "claude-sonnet-5"})
    assert native.requests == []


def test_broker_route_never_reads_local_anthropic_credentials(monkeypatch):
    from agent import agent_init
    from agent import anthropic_credentials

    monkeypatch.setattr(anthropic_credentials, "resolve_anthropic_token",
                        lambda **_kwargs: pytest.fail("local credential was read"))
    monkeypatch.setattr(agent_init, "_print_key_banner", lambda *_args: None)
    agent = SimpleNamespace(provider="anthropic", model="claude-sonnet-5", quiet_mode=True)
    agent_init._init_anthropic_client(agent, None, "claude-broker://local", 30)
    assert agent.api_key == "sk-ant-oat-hermes-broker-placeholder"
    assert agent._is_anthropic_oauth is True
    assert isinstance(agent._anthropic_client._client._transport, broker.ClaudeBrokerTransport)
    agent._anthropic_client.close()


def test_broker_route_does_not_refresh_from_claude_profile(monkeypatch):
    from agent.client_lifecycle import ClientLifecycleMixin
    from agent import anthropic_credentials

    monkeypatch.setattr(anthropic_credentials, "resolve_anthropic_token",
                        lambda **_kwargs: pytest.fail("local credential was read"))
    agent = SimpleNamespace(api_mode="anthropic_messages", provider="anthropic",
                            _anthropic_base_url="claude-broker://local",
                            _anthropic_api_key="sk-ant-oat-hermes-broker-placeholder")
    assert ClientLifecycleMixin._try_refresh_anthropic_client_credentials(agent) is False


def test_broker_route_preserves_native_signed_thinking_and_tools():
    from agent.anthropic_message_convert import convert_messages_to_anthropic
    from agent.anthropic_endpoints import _is_third_party_anthropic_endpoint

    assert not _is_third_party_anthropic_endpoint("claude-broker://local")
    messages = [{"role": "user", "content": "research"},
        {"role": "assistant", "content": "call", "tool_calls": [{
        "id": "toolu_1", "type": "function",
        "function": {"name": "research_lookup", "arguments": '{"query":"synthetic"}'},
    }], "anthropic_content_blocks": [
        {"type": "thinking", "thinking": "reason", "signature": "sig-test"},
        {"type": "text", "text": "call"},
        {"type": "tool_use", "id": "toolu_1", "name": "research_lookup",
         "input": {"query": "synthetic"}},
    ]}, {"role": "tool", "tool_call_id": "toolu_1",
         "name": "research_lookup", "content": "found"}]
    _system, converted = convert_messages_to_anthropic(
        messages, base_url="claude-broker://local", model="claude-sonnet-5")
    blocks = next(m["content"] for m in converted if m["role"] == "assistant")
    assert any(b.get("type") == "thinking" and b.get("signature") == "sig-test" for b in blocks)
    assert any(b.get("type") == "tool_use" and b.get("name") == "research_lookup" for b in blocks)


def test_broker_is_a_resolvable_native_hermes_provider():
    from hermes_cli.providers import get_provider
    from hermes_cli.runtime_provider import resolve_runtime_provider

    provider = get_provider("claude-broker", allow_network=False)
    runtime = resolve_runtime_provider(requested="claude-broker",
                                       target_model="claude-sonnet-5-5")
    assert provider.transport == "anthropic_messages"
    assert runtime["api_mode"] == "anthropic_messages"
    assert runtime["base_url"] == "claude-broker://local"
    assert runtime["api_key"] == "sk-ant-oat-hermes-broker-placeholder"


def test_hermes_agent_executes_tool_and_returns_result_over_broker_wire(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HERMES_RUNTIME_DIR", str(tmp_path / "runtime"))
    sample = tmp_path / "sample.txt"
    sample.write_text("HERMES_TOOL_CYCLE_OK\n")
    requests, controls = [], []

    def control(_root, payload):
        controls.append(payload)
        return {"ok": True, "lease": "synthetic-lease"}

    class Native(httpx.BaseTransport):
        def handle_request(self, request):
            body = json.loads(request.content)
            requests.append(body)
            if len(requests) == 1:
                content = [{"type": "tool_use", "id": "toolu_synthetic",
                            "name": "mcp__read_file", "input": {"path": str(sample)}}]
                stop = "tool_use"
            else:
                content = [{"type": "text", "text": "HERMES_TOOL_CYCLE_FINISHED"}]
                stop = "end_turn"
            return httpx.Response(200, json={
                "id": "msg_synthetic", "type": "message", "role": "assistant",
                "model": "claude-sonnet-5", "content": content,
                "stop_reason": stop, "stop_sequence": None,
                "usage": {"input_tokens": 20, "output_tokens": 10},
            }, request=request)

    monkeypatch.setattr(broker, "_control", control)
    monkeypatch.setattr(broker.httpx, "HTTPTransport", lambda **_kwargs: Native())
    agent = AIAgent(model="claude-sonnet-5", provider="claude-broker",
                    base_url="claude-broker://local",
                    api_key="", quiet_mode=True, skip_context_files=True,
                    skip_memory=True, save_trajectories=False, max_iterations=3)
    assert agent.api_mode == "anthropic_messages"
    agent._disable_streaming = True
    result = agent.run_conversation("Read the synthetic file and respond")
    assert result["final_response"] == "HERMES_TOOL_CYCLE_FINISHED"
    assert result.get("failed") is False
    assert len(requests) == 2
    assert any(tool["name"] == "mcp__read_file" for tool in requests[0]["tools"])
    assert "HERMES_TOOL_CYCLE_OK" in json.dumps(requests[1]["messages"])
    assert [row["op"] for row in controls] == ["register", "close", "register", "close"]
    assert controls[0]["conversation"] == controls[2]["conversation"]
