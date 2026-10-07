"""Per-request Hermes lease on the local Claude Broker's private sockets.

The Broker owns credentials, account choice and the provider audit.  A lease
exists only while the HTTP response is open, so an idle Hermes gateway does
not prevent Broker upgrades.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import socket
import uuid

import httpx

from agent.clinic_wire import (
    CLAUDE_REFUSAL, anthropic_requests_stream, anthropic_user_text, refusal_for,
)


BROKER_ROOT = Path.home() / ".local" / "state" / "claude-broker"
logger = logging.getLogger(__name__)


def _control(root: Path, payload: dict) -> dict:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(15)
        connection.connect(str(root / "broker.sock"))
        connection.sendall(json.dumps(payload, separators=(",", ":")).encode() + b"\n")
        with connection.makefile("rb") as reader:
            reply = reader.readline(65537)
    if not reply or len(reply) > 65536:
        raise RuntimeError("Claude Broker control unavailable")
    result = json.loads(reply)
    if result.get("ok") is not True:
        raise RuntimeError("Claude Broker: " + str(result.get("error") or "unavailable"))
    return result


class _LeasedStream(httpx.SyncByteStream):
    def __init__(self, response: httpx.Response, close_lease):
        self.response = response
        self.close_lease = close_lease
        self.closed = False

    def __iter__(self):
        try:
            yield from self.response.stream
        finally:
            self.close()

    def close(self):
        if not self.closed:
            self.closed = True
            try:
                self.response.close()
            finally:
                self.close_lease()


class ClaudeBrokerTransport(httpx.BaseTransport):
    """Forward native Anthropic requests. Clinic text is answered locally and is not forwarded."""

    def __init__(self, root: Path = BROKER_ROOT):
        self.root = root
        self.native = httpx.HTTPTransport(uds=str(root / "api.sock"))

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        body = request.content
        refusal = refusal_for(anthropic_user_text(body), vendor="claude")
        if refusal:
            logger.info("clinic refused before claude")
            return _refusal_response(request, body, refusal)
        pid = os.getpid()
        session = str(uuid.uuid4())
        conversation = request.headers.get("x-hermes-conversation-id")
        if conversation is None:
            # Stateless auxiliary calls have no owner conversation. Main agent
            # turns send a stable ID from their Hermes session on every call.
            conversation = str(uuid.uuid4())
            request.headers["x-hermes-conversation-id"] = conversation
        else:
            try:
                conversation = str(uuid.UUID(conversation))
            except ValueError as error:
                raise RuntimeError("Claude Broker conversation identity invalid") from error
        result = _control(self.root, {
            "op": "register", "client_kind": "hermes", "route": "hermes",
            "pid": pid, "session": session, "conversation": conversation,
            "audit_context": {"reason_code": "DEFAULT"},
        })
        # A gateway can issue several research tool turns concurrently from one
        # PID. The native socket binds each HTTP request to its own audited lease.
        request.headers["x-hermes-broker-session"] = session

        def close_lease():
            _control(self.root, {"op": "close", "pid": pid,
                                 "session": session, "lease": result["lease"]})

        try:
            response = self.native.handle_request(request)
        except BaseException:
            close_lease()
            raise
        return httpx.Response(
            status_code=response.status_code, headers=response.headers,
            stream=_LeasedStream(response, close_lease),
            extensions=response.extensions, request=request,
        )

    def close(self):
        self.native.close()


def _message(model: str, text: str) -> dict:
    return {
        "id": "msg_local_clinic_refusal",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 0, "output_tokens": 0},
    }


def _sse(model: str, text: str) -> bytes:
    message = _message(model, text)
    message["content"] = []
    message["stop_reason"] = None
    events = [
        ("message_start", {"type": "message_start", "message": message}),
        ("content_block_start", {
            "type": "content_block_start", "index": 0,
            "content_block": {"type": "text", "text": ""},
        }),
        ("content_block_delta", {
            "type": "content_block_delta", "index": 0,
            "delta": {"type": "text_delta", "text": text},
        }),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn", "stop_sequence": None},
            "usage": {"output_tokens": 0},
        }),
        ("message_stop", {"type": "message_stop"}),
    ]
    lines = []
    for name, payload in events:
        lines.append(f"event: {name}\ndata: {json.dumps(payload, separators=(',', ':'))}\n")
    return ("\n".join(lines) + "\n").encode()


def _refusal_response(request: httpx.Request, body: bytes, text: str) -> httpx.Response:
    model, stream = anthropic_requests_stream(body)
    if not text:
        text = CLAUDE_REFUSAL
    if stream:
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"},
            content=_sse(model, text), request=request,
        )
    return httpx.Response(200, json=_message(model, text), request=request)
