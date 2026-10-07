"""OpenAI-shaped Hermes adapter for Bunker's audited native tool-call bridge.

The bridge owns a Codex thread and account rotation. Hermes owns tool execution;
one native ``item/tool/call`` is returned to Hermes, then its exact result is fed
back to the same Codex turn. Neither prompts nor credentials enter argv.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
import select
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
import uuid

from agent.clinic_wire import content_text, refusal_for

logger = logging.getLogger(__name__)


MAX_LINE = 16 * 1024 * 1024
BRIDGE_REPLY_TIMEOUT = 900
MANIFEST = Path.home() / ".local/lib/codex-bunker/current.json"


def _bridge_path(manifest=MANIFEST):
    row = json.loads(Path(manifest).read_text())
    release = Path(row["runtime"]).resolve(strict=True)
    if release.parent != Path.home() / ".local/lib/codex-bunker/releases":
        raise RuntimeError("Codex Bunker runtime path invalid")
    path = release / "codex_bunker_hermes.py"
    if not path.is_file() or path.is_symlink():
        raise RuntimeError("Codex Bunker Hermes bridge is not installed")
    digest = (row.get("sha256") or {}).get(path.name)
    if not isinstance(digest, str) or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
        raise RuntimeError("Codex Bunker Hermes bridge digest mismatch")
    return path


def _readline_bounded(stream, timeout=BRIDGE_REPLY_TIMEOUT):
    deadline = time.monotonic() + timeout
    data = bytearray()
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([stream], [], [], remaining)[0]:
            raise TimeoutError("Codex Bunker Hermes bridge reply timed out")
        chunk = os.read(stream.fileno(), 65536)
        if not chunk:
            raise RuntimeError("Codex Bunker Hermes bridge exited")
        data.extend(chunk)
        if len(data) > MAX_LINE:
            raise RuntimeError("Codex Bunker Hermes bridge response too large")
        if b"\n" in data:
            line, _, tail = data.partition(b"\n")
            if tail:
                # One request is outstanding at a time; unsolicited extra rows
                # indicate a broken protocol and must never be consumed later.
                raise RuntimeError("Codex Bunker Hermes bridge sent extra output")
            return json.loads(line)


def _tool_specs(tools):
    result = []
    for row in tools or []:
        fn = row.get("function") if isinstance(row, dict) else None
        if not isinstance(fn, dict):
            raise RuntimeError("Codex Bunker tool schema invalid")
        result.append({"name": fn["name"], "description": fn.get("description", ""),
                       "input_schema": fn.get("parameters") or {"type": "object"}})
    return result


def _text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list) and all(isinstance(x, dict) and x.get("type") == "text"
                                       and isinstance(x.get("text"), str) for x in value):
        return "\n".join(x["text"] for x in value)
    raise RuntimeError("Codex Bunker Hermes bridge accepts text turns only")


def _completion(result, model):
    kind = result.get("type")
    if kind == "tool_call":
        name, call_id, arguments = result.get("name"), result.get("call_id"), result.get("arguments")
        if not isinstance(name, str) or not isinstance(call_id, str):
            raise RuntimeError("Codex Bunker tool call malformed")
        if not isinstance(arguments, str):
            arguments = json.dumps(arguments if arguments is not None else {}, ensure_ascii=False)
        call = SimpleNamespace(id=call_id, type="function",
                               function=SimpleNamespace(name=name, arguments=arguments))
        message = SimpleNamespace(role="assistant", content=None, tool_calls=[call])
        reason = "tool_calls"
    elif kind == "final" and isinstance(result.get("content"), str):
        message = SimpleNamespace(role="assistant", content=result["content"], tool_calls=None)
        reason = "stop"
    else:
        raise RuntimeError("Codex Bunker Hermes bridge result malformed")
    return SimpleNamespace(id="bunker-" + uuid.uuid4().hex, model=model, usage=None,
                           choices=[SimpleNamespace(index=0, message=message, finish_reason=reason)])


class CodexBunkerClient:
    """The shared facade for one Hermes agent session; request-local clients are forbidden."""

    def __init__(self, agent, *, process_factory=subprocess.Popen, bridge_path=None):
        self.agent = agent
        self.process_factory = process_factory
        self.bridge_path = bridge_path
        self.proc = None
        self.session = None
        self.model = None
        self.tool_specs = None
        self.pending_call = None
        self.lock = threading.RLock()
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def _request(self, row):
        proc = self.proc
        if proc is None or proc.poll() is not None:
            raise RuntimeError("Codex Bunker Hermes bridge unavailable")
        ident = uuid.uuid4().hex
        proc.stdin.write((json.dumps({"id": ident, **row}, ensure_ascii=False) + "\n").encode())
        proc.stdin.flush()
        answer = _readline_bounded(proc.stdout)
        if not isinstance(answer, dict) or answer.get("id") != ident:
            raise RuntimeError("Codex Bunker Hermes bridge reply identity mismatch")
        if answer.get("ok") is not True:
            raise RuntimeError("Codex Bunker: " + str(answer.get("error_code") or "unavailable"))
        return answer

    def _start(self, kwargs):
        from agent.runtime_cwd import resolve_agent_cwd
        from agent.codex_runtime_history_seed import render_history_seed
        path = self.bridge_path or _bridge_path()
        interpreter = "/opt/homebrew/bin/python3" if Path("/opt/homebrew/bin/python3").is_file() else sys.executable
        self.proc = self.process_factory([interpreter, str(path)], stdin=subprocess.PIPE,
                                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
        self.tool_specs = _tool_specs(kwargs.get("tools"))
        messages = kwargs.get("messages") or []
        instructions = "\n\n".join(part for part in (
            *(_text(row.get("content")) for row in messages
              if isinstance(row, dict) and row.get("role") in {"system", "developer"}),
            render_history_seed(messages),
        ) if part)
        try:
            from agent.model_metadata import strip_codex_context_variant_suffix
            # ``-900k`` is a Hermes context opt-in. The bunker routes the base slug;
            # the session keeps the suffixed id so the larger window still applies.
            row = self._request({"op": "open", "cwd": str(getattr(self.agent, "session_cwd", None)
                                                       or resolve_agent_cwd()),
                                 "model": strip_codex_context_variant_suffix(kwargs["model"]),
                                 "tools": self.tool_specs,
                                 "reason_code": "DEFAULT",
                                 "job_id": str(getattr(self.agent, "session_id", None) or uuid.uuid4()),
                                 "developer_instructions": instructions})
            self.session = row["session"]
            self.model = kwargs["model"]
        except BaseException:
            self.close()
            raise

    def create(self, **kwargs):
        with self.lock:
            messages = kwargs.get("messages") or []
            refusal = _clinic_refusal(messages)
            if refusal:
                logger.info("clinic refused before codex")
                if self.proc is not None:
                    self.close()
                return _completion({"type": "final", "content": refusal}, kwargs.get("model") or "")
            if self.proc is None:
                self._start(kwargs)
            elif (_tool_specs(kwargs.get("tools")) != self.tool_specs
                  or kwargs["model"] != self.model):
                if self.pending_call is not None:
                    raise RuntimeError("Codex Bunker Hermes model or tool set changed during a native turn")
                self.close()
                self._start(kwargs)
            messages = kwargs.get("messages") or []
            if not isinstance(messages, list) or not messages:
                raise RuntimeError("Codex Bunker Hermes request has no messages")
            if self.pending_call is not None:
                last = messages[-1]
                if (not isinstance(last, dict) or last.get("role") != "tool"
                        or last.get("tool_call_id") != self.pending_call):
                    raise RuntimeError("Codex Bunker Hermes tool result identity mismatch")
                result = self._request({"op": "result", "session": self.session,
                                        "call_id": self.pending_call,
                                        "success": last.get("is_error") is not True,
                                        "content": _text(last.get("content"))})
                self.pending_call = None
            else:
                user_rows = [row for row in messages if isinstance(row, dict) and row.get("role") == "user"]
                if not user_rows:
                    raise RuntimeError("Codex Bunker Hermes request has no user turn")
                prompt = _text(user_rows[-1].get("content"))
                if not self.session:
                    raise RuntimeError("Codex Bunker Hermes session unavailable")
                result = self._request({"op": "send", "session": self.session, "prompt": prompt})
            if result.get("type") == "tool_call":
                self.pending_call = result.get("call_id")
            return _completion(result, kwargs["model"])

    def close(self):
        with self.lock:
            proc, self.proc = self.proc, None
            if proc is None:
                return
            if self.session and proc.poll() is None and self.pending_call is None:
                try:
                    self.proc = proc
                    self._request({"op": "close", "session": self.session})
                except (OSError, RuntimeError, TimeoutError, ValueError):
                    pass
                finally:
                    self.proc = None
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=3)
            self.session = None
            self.model = None
            self.tool_specs = None
            self.pending_call = None


def _clinic_refusal(messages) -> str:
    if not isinstance(messages, list):
        return ""
    parts = []
    for row in messages:
        if not isinstance(row, dict) or row.get("role") not in {"user", "tool"}:
            continue
        parts.append(content_text(row.get("content")))
    return refusal_for("\n".join(parts), vendor="codex")
