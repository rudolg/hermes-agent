"""Per-request Hermes lease on the local Claude Broker's private sockets.

The Broker owns credentials, account choice and the provider audit.  A lease
exists only while the HTTP response is open, so an idle Hermes gateway does
not prevent Broker upgrades.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import uuid

import httpx


BROKER_ROOT = Path.home() / ".local" / "state" / "claude-broker"


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
    """Forward native Anthropic requests; no content transformation or token access."""

    def __init__(self, root: Path = BROKER_ROOT):
        self.root = root
        self.native = httpx.HTTPTransport(uds=str(root / "api.sock"))

    def handle_request(self, request: httpx.Request) -> httpx.Response:
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
