"""A native Bunker tool call must reach Hermes and return to the same turn."""

import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

from agent.codex_bunker_adapter import CodexBunkerClient
from agent.transports.chat_completions import ChatCompletionsTransport
from run_agent import AIAgent


BRIDGE = r'''
import json, sys
for line in sys.stdin:
    row = json.loads(line)
    op = row["op"]
    if op == "open":
        result = {"session": "same-native-thread", "thread_id": "thread-1"}
    elif op == "send":
        result = {"type": "tool_call", "name": "research_lookup",
                  "call_id": "call-1", "arguments": {"query": "sample"}}
    elif op == "result":
        assert row["session"] == "same-native-thread"
        assert row["call_id"] == "call-1"
        assert row["content"] == "found source"
        result = {"type": "final", "content": "The source was found."}
    elif op == "close":
        result = {"closed": True}
    else:
        result = {"error_code": "invalid_request"}
    print(json.dumps({"id": row["id"], "ok": True, **result}), flush=True)
'''


def test_tool_call_round_trip_uses_one_native_bunker_thread(tmp_path):
    script = tmp_path / "bridge.py"
    script.write_text(BRIDGE)
    agent = SimpleNamespace(session_cwd=str(tmp_path), session_id="hermes-test")

    def process_factory(_command, **kwargs):
        return subprocess.Popen([sys.executable, str(script)], **kwargs)

    client = CodexBunkerClient(agent, process_factory=process_factory, bridge_path=script)
    tools = [{"type": "function", "function": {"name": "research_lookup",
              "description": "Look up a source", "parameters": {"type": "object"}}}]
    try:
        first = client.create(model="gpt-6-sol", tools=tools, messages=[
            {"role": "system", "content": "Research carefully."},
            {"role": "user", "content": "Find a source."},
        ])
        normalized = ChatCompletionsTransport().normalize_response(first)
        assert normalized.tool_calls[0].name == "research_lookup"
        assert json.loads(normalized.tool_calls[0].arguments) == {"query": "sample"}
        final = client.create(model="gpt-6-sol", tools=tools, messages=[
            {"role": "user", "content": "Find a source."},
            {"role": "assistant", "tool_calls": []},
            {"role": "tool", "tool_call_id": "call-1", "content": "found source"},
        ])
        assert ChatCompletionsTransport().normalize_response(final).content == "The source was found."
    finally:
        client.close()


def test_900k_pick_opens_the_base_and_keeps_one_session(tmp_path):
    opened = tmp_path / "opened.txt"
    script = tmp_path / "bridge.py"
    script.write_text(
        "import json, sys\n"
        "for line in sys.stdin:\n"
        "    row = json.loads(line)\n"
        "    if row['op'] == 'open':\n"
        f"        open({json.dumps(str(opened))}, 'a').write(row['model'] + '\\n')\n"
        "        result = {'session': 'wide', 'thread_id': 't'}\n"
        "    elif row['op'] == 'send':\n"
        "        result = {'type': 'final', 'content': 'ok'}\n"
        "    else:\n"
        "        result = {'closed': True}\n"
        "    print(json.dumps({'id': row['id'], 'ok': True, **result}), flush=True)\n"
    )
    agent = SimpleNamespace(session_cwd=str(tmp_path), session_id="hermes-test")
    client = CodexBunkerClient(
        agent,
        process_factory=lambda _cmd, **kw: subprocess.Popen([sys.executable, str(script)], **kw),
        bridge_path=script,
    )
    try:
        client.create(model="gpt-6-astra-900k", tools=[], messages=[{"role": "user", "content": "wide"}])
        client.create(model="gpt-6-astra-900k", tools=[], messages=[{"role": "user", "content": "again"}])
        client.close()
        client.create(model="gpt-6.1-sol-900k", tools=[], messages=[{"role": "user", "content": "no bump"}])
    finally:
        client.close()
    assert opened.read_text().splitlines() == ["gpt-6-astra", "gpt-6.1-sol-900k"]


def test_bunker_context_window_follows_the_900k_opt_in():
    from agent.model_metadata import get_model_context_length

    assert get_model_context_length(
        "gpt-6-astra", provider="codex-bunker", base_url="codex-bunker://local") == 272_000
    assert get_model_context_length(
        "gpt-6-astra-900k", provider="codex-bunker", base_url="codex-bunker://local") == 900_000
    assert get_model_context_length(
        "gpt-6-sol-900k", provider="codex-bunker", base_url="codex-bunker://local") == 900_000
    assert get_model_context_length(
        "gpt-6.1-sol", provider="codex-bunker", base_url="codex-bunker://local") == 272_000


def test_wrong_tool_result_never_reaches_bunker(tmp_path):
    script = tmp_path / "bridge.py"
    script.write_text(BRIDGE)
    agent = SimpleNamespace(session_cwd=str(tmp_path), session_id="hermes-test")
    client = CodexBunkerClient(agent, process_factory=lambda _cmd, **kw:
                               subprocess.Popen([sys.executable, str(script)], **kw), bridge_path=script)
    try:
        client.create(model="gpt-6-sol", tools=[], messages=[{"role": "user", "content": "Find it."}])
        try:
            client.create(model="gpt-6-sol", tools=[], messages=[
                {"role": "tool", "tool_call_id": "different-call", "content": "forged"}])
        except RuntimeError as error:
            assert "identity mismatch" in str(error)
        else:
            raise AssertionError("wrong tool result was accepted")
    finally:
        client.close()


def test_hermes_agent_executes_native_bunker_tool_call(monkeypatch, tmp_path):
    from agent import codex_bunker_adapter

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HERMES_RUNTIME_DIR", str(tmp_path / "runtime"))
    source = tmp_path / "source.txt"
    source.write_text("HERMES_BUNKER_TOOL_OK\n")
    script = tmp_path / "bridge.py"
    tool_names = tmp_path / "tool-names.json"
    script.write_text('''
import json, sys
for line in sys.stdin:
    row = json.loads(line)
    if row["op"] == "open":
        open(''' + json.dumps(str(tool_names)) + ''', "w").write(json.dumps([t["name"] for t in row["tools"]]))
        result = {"session": "native-session", "thread_id": "native-thread"}
    elif row["op"] == "send":
        result = {"type": "tool_call", "name": "read_file", "call_id": "call-read",
                  "arguments": {"path": ''' + json.dumps(str(source)) + '''}}
    elif row["op"] == "result":
        assert row["call_id"] == "call-read"
        assert "HERMES_BUNKER_TOOL_OK" in row["content"]
        result = {"type": "final", "content": "BUNKER_HERMES_TOOL_CYCLE_FINISHED"}
    else:
        result = {"closed": True}
    print(json.dumps({"id": row["id"], "ok": True, **result}), flush=True)
''')
    monkeypatch.setattr(codex_bunker_adapter, "_bridge_path", lambda: script)
    agent = AIAgent(model="gpt-6-sol", provider="codex-bunker",
                    base_url="codex-bunker://local", api_key="", quiet_mode=True,
                    skip_context_files=True, skip_memory=True,
                    save_trajectories=False, max_iterations=3)
    assert agent.api_mode == "chat_completions"
    result = agent.run_conversation("Read the synthetic file and respond")
    assert result["final_response"] == "BUNKER_HERMES_TOOL_CYCLE_FINISHED"
    assert result.get("failed") is False
    assert "read_file" in json.loads(tool_names.read_text())
