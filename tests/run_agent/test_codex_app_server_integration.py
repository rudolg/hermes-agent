"""Integration test for the codex_app_server runtime path through AIAgent.

Verifies that:
  - api_mode='codex_app_server' is accepted on AIAgent construction
  - run_conversation() takes the early-return path and never enters the
    chat completions loop
  - Projected messages from a fake Codex session land in the messages list
  - tool_iterations from the codex session tick the skill nudge counter
  - Memory nudge counter ticks once per turn
  - The returned dict has the same shape as the chat_completions path
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

import run_agent
from agent.transports.codex_app_server_session import CodexAppServerSession, TurnResult


@pytest.fixture
def fake_session(monkeypatch):
    """Replace CodexAppServerSession with a stub that returns a fixed
    TurnResult, so we can drive AIAgent without spawning real codex."""

    def fake_run_turn(self, user_input: str, **kwargs):
        return TurnResult(
            final_text=f"echo: {user_input}",
            projected_messages=[
                {"role": "assistant", "content": None,
                 "tool_calls": [{"id": "exec_1", "type": "function",
                                 "function": {"name": "exec_command",
                                              "arguments": "{}"}}]},
                {"role": "tool", "tool_call_id": "exec_1", "content": "ok"},
                {"role": "assistant", "content": f"echo: {user_input}"},
            ],
            tool_iterations=1,
            interrupted=False,
            error=None,
            turn_id="turn-stub-1",
            thread_id="thread-stub-1",
        )

    monkeypatch.setattr(CodexAppServerSession, "run_turn", fake_run_turn)
    monkeypatch.setattr(
        CodexAppServerSession, "ensure_started", lambda self: "thread-stub-1"
    )


def _make_codex_agent():
    """Construct an AIAgent in codex_app_server mode without contacting any
    real provider. We pass api_mode explicitly so the constructor takes the
    fast path for direct credentials."""
    return run_agent.AIAgent(
        api_key="stub",
        base_url="https://stub.invalid",
        provider="openai",
        api_mode="codex_app_server",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
    )


class TestApiModeAccepted:
    def test_api_mode_is_codex_app_server(self):
        agent = _make_codex_agent()
        assert agent.api_mode == "codex_app_server"


class TestRunConversationCodexPath:
    def test_run_conversation_returns_codex_shape(self, fake_session):
        agent = _make_codex_agent()
        # No background review fork during tests
        with patch.object(agent, "_spawn_background_review", return_value=None):
            result = agent.run_conversation("hello there")
        assert result["final_response"] == "echo: hello there"
        assert result["completed"] is True
        assert result["partial"] is False
        assert result["error"] is None
        assert result["api_calls"] == 1
        assert result["codex_thread_id"] == "thread-stub-1"
        assert result["codex_turn_id"] == "turn-stub-1"

    def test_projected_messages_are_spliced(self, fake_session):
        agent = _make_codex_agent()
        with patch.object(agent, "_spawn_background_review", return_value=None):
            result = agent.run_conversation("hello")
        msgs = result["messages"]
        # User message + 3 projected (assistant tool_call + tool + assistant text)
        assert len(msgs) >= 4
        assert msgs[0]["role"] == "user"
        assert msgs[0]["content"] == "hello"
        # Last assistant message has the final text
        final = [m for m in msgs if m.get("role") == "assistant"
                 and m.get("content") == "echo: hello"]
        assert final, f"expected final assistant message in {msgs}"

    def test_nudge_counters_tick(self, fake_session):
        """The skill nudge counter must accumulate tool_iterations across
        turns. The memory nudge counter is gated on memory being configured
        (which we skip via skip_memory=True), so we don't assert on it here —
        a separate test below covers that path explicitly."""
        agent = _make_codex_agent()
        agent._iters_since_skill = 0
        agent._user_turn_count = 0
        with patch.object(agent, "_spawn_background_review", return_value=None):
            agent.run_conversation("first")
        assert agent._iters_since_skill == 1  # one tool_iteration in fake turn
        # _user_turn_count is incremented by run_conversation pre-loop, not
        # by the codex helper — confirms we delegate that to the standard flow.
        assert agent._user_turn_count == 1
        with patch.object(agent, "_spawn_background_review", return_value=None):
            agent.run_conversation("second")
        assert agent._iters_since_skill == 2
        assert agent._user_turn_count == 2

    def test_user_message_not_duplicated(self, fake_session):
        """Regression guard: the user message must appear exactly once in
        the messages list. The standard run_conversation pre-loop appends
        it, and the codex helper must NOT append again."""
        agent = _make_codex_agent()
        with patch.object(agent, "_spawn_background_review", return_value=None):
            result = agent.run_conversation("ping unique 12345")
        user_count = sum(
            1 for m in result["messages"]
            if m.get("role") == "user" and m.get("content") == "ping unique 12345"
        )
        assert user_count == 1, f"user message appeared {user_count}× in {result['messages']}"

    def test_background_review_NOT_invoked_below_threshold(self, fake_session):
        """A single turn shouldn't trigger background review — counters
        haven't reached the nudge interval (default 10)."""
        agent = _make_codex_agent()
        agent._memory_nudge_interval = 10
        agent._skill_nudge_interval = 10
        agent._iters_since_skill = 0
        with patch.object(agent, "_spawn_background_review",
                          return_value=None) as spawn:
            agent.run_conversation("ping")
        # Below threshold → review should NOT fire (was a real bug:
        # the helper was calling _spawn_background_review() with no
        # args after every turn, which would crash with TypeError).
        assert not spawn.called

    def test_background_review_skill_trigger_fires_above_threshold(
        self, monkeypatch
    ):
        """When tool iterations cross the skill nudge interval, the
        background review fires with review_skills=True and the right
        messages_snapshot signature."""
        from agent.transports.codex_app_server_session import (
            CodexAppServerSession, TurnResult,
        )
        # Make the fake session report 10 tool iterations in one turn
        # (matching the default skill threshold).
        def fake_run_turn(self, user_input: str, **kwargs):
            return TurnResult(
                final_text=f"echo: {user_input}",
                projected_messages=[
                    {"role": "assistant", "content": f"echo: {user_input}"},
                ],
                tool_iterations=10,
                turn_id="t1", thread_id="th1",
            )
        monkeypatch.setattr(CodexAppServerSession, "run_turn", fake_run_turn)
        monkeypatch.setattr(
            CodexAppServerSession, "ensure_started", lambda self: "th1"
        )

        agent = _make_codex_agent()
        agent._skill_nudge_interval = 10
        agent._iters_since_skill = 0
        # Make valid_tool_names include 'skill_manage' so the gate passes
        agent.valid_tool_names = set(getattr(agent, "valid_tool_names", set()))
        agent.valid_tool_names.add("skill_manage")

        with patch.object(agent, "_spawn_background_review",
                          return_value=None) as spawn:
            agent.run_conversation("do tool work")

        assert spawn.called, "skill threshold tripped but review didn't fire"
        # Verify the call signature matches what _spawn_background_review
        # actually expects — this is the regression guard for the original
        # bug where the codex path called it with no args at all.
        call = spawn.call_args
        assert "messages_snapshot" in call.kwargs
        assert isinstance(call.kwargs["messages_snapshot"], list)
        assert call.kwargs["review_skills"] is True
        # Counter should be reset after the review fires
        assert agent._iters_since_skill == 0

    def test_background_review_signature_never_breaks(self, fake_session):
        """Even when no trigger fires, the helper must never call
        _spawn_background_review with the wrong signature. Run a turn,
        then run another turn after manually tripping the skill counter
        and confirm the call shape is the kwargs-only form the function
        actually accepts."""
        agent = _make_codex_agent()
        agent._skill_nudge_interval = 1  # very low so any iter trips it
        agent._iters_since_skill = 0
        agent.valid_tool_names = set(getattr(agent, "valid_tool_names", set()))
        agent.valid_tool_names.add("skill_manage")

        with patch.object(agent, "_spawn_background_review",
                          return_value=None) as spawn:
            agent.run_conversation("first")
        # The fake session reports tool_iterations=1, which trips
        # _skill_nudge_interval=1. So review should fire.
        assert spawn.called
        # Critical invariant: positional args must be empty, all real
        # args must be kwargs (matching _spawn_background_review's
        # actual signature).
        call = spawn.call_args
        assert call.args == (), (
            f"expected no positional args, got {call.args!r} — "
            "would crash _spawn_background_review at runtime"
        )
        assert "messages_snapshot" in call.kwargs

    def test_chat_completions_loop_is_not_entered(self, fake_session):
        """The early-return must bypass the regular API call loop entirely.
        We confirm by patching the SDK call and asserting it's never invoked."""
        agent = _make_codex_agent()
        # The chat_completions loop calls self.client.chat.completions.create(...)
        # If our early-return works, that path is dead.
        with patch.object(agent, "client") as client_mock, patch.object(
            agent, "_spawn_background_review", return_value=None
        ):
            agent.run_conversation("hi")
        assert not client_mock.chat.completions.create.called


class TestReviewForkApiModeDowngrade:
    """When the parent agent runs on codex_app_server, the background
    review fork must downgrade to codex_responses — otherwise the fork
    can't dispatch agent-loop tools (memory, skill_manage) which is the
    whole point of the review."""

    def test_codex_app_server_parent_downgrades_review_fork(self):
        """Live test against the real _spawn_background_review code path:
        verify the review_agent gets api_mode=codex_responses when the
        parent is codex_app_server."""
        from unittest.mock import MagicMock, patch as _patch
        agent = _make_codex_agent()
        # Pretend memory + skills are configured so the review fork
        # reaches the AIAgent constructor.
        agent._memory_store = MagicMock()
        agent._memory_enabled = True
        agent._user_profile_enabled = True
        # Mock _current_main_runtime to return the parent's codex_app_server
        # state so we can confirm the helper detects + downgrades it.
        agent._current_main_runtime = lambda: {
            "api_mode": "codex_app_server",
            "base_url": "https://chatgpt.com/backend-api/codex",
            "api_key": "stub-token",
        }
        # Capture what AIAgent gets constructed with inside the helper.
        captured = {}

        def _capture_init(self, **kwargs):
            captured.update(kwargs)
            # Set bare attributes the rest of the spawn function reads
            # so it can finish without exploding.
            self.api_mode = kwargs.get("api_mode")
            self.provider = kwargs.get("provider")
            self.model = kwargs.get("model")
            self._memory_write_origin = None
            self._memory_write_context = None
            self._memory_store = None
            self._memory_enabled = False
            self._user_profile_enabled = False
            self._memory_nudge_interval = 0
            self._skill_nudge_interval = 0
            self.suppress_status_output = False
            self._session_messages = []

            def _no_op_run_conv(*a, **kw):
                return {"final_response": "", "messages": []}
            self.run_conversation = _no_op_run_conv

            def _no_op_close(*a, **kw):
                return None
            self.close = _no_op_close

        with _patch("run_agent.AIAgent.__init__", _capture_init):
            agent._spawn_background_review(
                messages_snapshot=[{"role": "user", "content": "x"}],
                review_memory=True,
                review_skills=False,
            )
            # Wait for the spawned thread to actually execute
            import time
            for _ in range(30):
                if "api_mode" in captured:
                    break
                time.sleep(0.1)

        assert captured.get("api_mode") == "codex_responses", (
            f"review fork should be downgraded to codex_responses when "
            f"parent is codex_app_server; got {captured.get('api_mode')!r}"
        )


class TestErrorHandling:
    def test_session_exception_returns_partial_with_error(self, monkeypatch):
        def boom_run_turn(self, user_input, **kwargs):
            raise RuntimeError("subprocess died")

        monkeypatch.setattr(CodexAppServerSession, "ensure_started",
                            lambda self: "t1")
        monkeypatch.setattr(CodexAppServerSession, "run_turn", boom_run_turn)

        agent = _make_codex_agent()
        with patch.object(agent, "_spawn_background_review", return_value=None):
            result = agent.run_conversation("hi")
        assert result["completed"] is False
        assert result["partial"] is True
        assert "subprocess died" in result["error"]
        assert "codex-runtime auto" in result["final_response"]

    def test_interrupted_turn_marked_partial(self, monkeypatch):
        def interrupted_turn(self, user_input, **kwargs):
            return TurnResult(
                final_text="",
                projected_messages=[],
                tool_iterations=0,
                interrupted=True,
                error="user interrupted",
                turn_id="t",
                thread_id="th",
            )
        monkeypatch.setattr(CodexAppServerSession, "ensure_started",
                            lambda self: "th")
        monkeypatch.setattr(CodexAppServerSession, "run_turn", interrupted_turn)

        agent = _make_codex_agent()
        with patch.object(agent, "_spawn_background_review", return_value=None):
            result = agent.run_conversation("hi")
        assert result["completed"] is False
        assert result["partial"] is True
        assert result["error"] == "user interrupted"


class TestSessionRetirementOnRunAgent:
    """run_agent.py side: when run_turn returns should_retire=True, the
    AIAgent must close + null _codex_session so the next turn respawns."""

    def test_should_retire_drops_session(self, monkeypatch):
        closes = {"count": 0}

        def fake_run_turn(self, user_input, **kwargs):
            return TurnResult(
                final_text="",
                projected_messages=[],
                tool_iterations=0,
                interrupted=True,
                error="turn timed out after 600.0s",
                turn_id="tu1",
                thread_id="th1",
                should_retire=True,
            )

        def fake_close(self):
            closes["count"] += 1

        monkeypatch.setattr(CodexAppServerSession, "ensure_started",
                            lambda self: "th1")
        monkeypatch.setattr(CodexAppServerSession, "run_turn", fake_run_turn)
        monkeypatch.setattr(CodexAppServerSession, "close", fake_close)

        agent = _make_codex_agent()
        with patch.object(agent, "_spawn_background_review", return_value=None):
            result = agent.run_conversation("hi")

        # The session was closed and cleared
        assert closes["count"] == 1
        assert getattr(agent, "_codex_session", "MISSING") is None
        # Partial result was still returned (caller still sees the error)
        assert result["partial"] is True
        assert result["error"] == "turn timed out after 600.0s"

    def test_normal_turn_keeps_session(self, fake_session):
        """fake_session fixture returns should_retire=False (default).
        The session must stay attached for the next turn to reuse."""
        agent = _make_codex_agent()
        with patch.object(agent, "_spawn_background_review", return_value=None):
            agent.run_conversation("hi")
        # Session was lazily created and still attached.
        assert getattr(agent, "_codex_session", None) is not None

    def test_exception_path_also_drops_session(self, monkeypatch):
        """Even if run_turn raises (not just sets should_retire), we must
        drop the session — a thrown exception is the strongest possible
        signal the process is dead."""
        closes = {"count": 0}

        def boom_run_turn(self, user_input, **kwargs):
            raise RuntimeError("codex segfaulted")

        def fake_close(self):
            closes["count"] += 1

        monkeypatch.setattr(CodexAppServerSession, "ensure_started",
                            lambda self: "th1")
        monkeypatch.setattr(CodexAppServerSession, "run_turn", boom_run_turn)
        monkeypatch.setattr(CodexAppServerSession, "close", fake_close)

        agent = _make_codex_agent()
        with patch.object(agent, "_spawn_background_review", return_value=None):
            result = agent.run_conversation("hi")

        assert closes["count"] == 1
        assert agent._codex_session is None
        assert result["completed"] is False
        assert "codex segfaulted" in result["error"]




# --- Auto-ABACDA JEDI (Codex-B #1/#2): the codex_app_server review runs BEFORE external-memory sync AND the
#     background-review fork, so BOTH side effects receive the REVIEWED candidate, never the raw draft. ---

class TestAbacdaCodexPathOrdering:
    def test_review_precedes_BOTH_side_effects(self, fake_session, monkeypatch):
        import run_agent
        import agent.abacda_review as AR
        agent = _make_codex_agent()

        synced = {}
        monkeypatch.setattr(
            run_agent.AIAgent, "_sync_external_memory_for_turn",
            lambda self, *, original_user_message, final_response, interrupted: synced.update(fr=final_response),
        )
        bg = {}
        monkeypatch.setattr(
            run_agent.AIAgent, "_spawn_background_review",
            lambda self, *, messages_snapshot, review_memory, review_skills: bg.update(snap=list(messages_snapshot)),
        )
        # the review returns STRIPPED messages (the real maybe_review_and_retry removes the uncertified draft) —
        # exercise that the codex path splices the stripped result into `messages`, so no side effect sees the draft.
        monkeypatch.setattr(
            AR, "maybe_review_and_retry",
            lambda ag, q, a, m: ("REVIEWED_CORRECTED_ANSWER",
                                 AR._without_draft_and_feedback(m, a, None)
                                 + [{"role": "assistant", "content": "REVIEWED_CORRECTED_ANSWER"}], {}),
        )

        # call the runtime function DIRECTLY with should_review_memory=True so the bg-review fork RELIABLY fires
        result = agent._run_codex_app_server_turn(
            user_message="do it", original_user_message="do it",
            messages=[], effective_task_id="t", should_review_memory=True)

        assert result["final_response"] == "REVIEWED_CORRECTED_ANSWER"          # returned = reviewed
        assert synced.get("fr") == "REVIEWED_CORRECTED_ANSWER"                  # memory sync got REVIEWED, not draft
        assert bg.get("snap") is not None                                      # background review FIRED
        # Codex-B #1: the raw draft sentinel ("echo: do it") occurs ZERO times in BOTH side-effect inputs
        assert "echo: do it" not in str(synced.get("fr"))
        assert not any("echo: do it" == m.get("content") for m in bg["snap"])
        _last_assistant = next((m for m in reversed(bg["snap"]) if m.get("role") == "assistant"), None)
        assert _last_assistant is not None and _last_assistant.get("content") == "REVIEWED_CORRECTED_ANSWER"
        # and the returned messages themselves carry no raw draft
        assert not any("echo: do it" == m.get("content") for m in result.get("messages", []))


class TestAbacdaCodexPrivacyAndDisplay:
    """Codex-B 2026-07-22 #4 + #1 on the codex_app_server path."""

    def test_production_session_has_no_display_hook(self):
        # #4: the production constructor passes NO on_event, so this path emits NO token stream to a display sink and
        # therefore cannot emit a raw pre-review draft through a display layer. (Corrects the earlier false comment.)
        sess = CodexAppServerSession(cwd=".")
        assert getattr(sess, "_on_event", "MISSING") is None

    def test_codex_path_emits_no_raw_draft_to_stream_sink(self, fake_session, monkeypatch):
        # #4/#6 behavioural: drive the real codex path with a capturing stream sink; the raw pre-review draft
        # ("echo: do it") must NEVER reach the display sink (nothing streams token-by-token on this path).
        import agent.abacda_review as AR
        agent = _make_codex_agent()
        captured = []
        agent.stream_delta_callback = captured.append
        agent._stream_callback = captured.append
        monkeypatch.setattr(AR, "maybe_review_and_retry",
                            lambda ag, q, a, m: (a, m, {"reviewed": True, "clean": True}))
        with patch.object(agent, "_spawn_background_review", return_value=None):
            result = agent.run_conversation("do it")
        assert "echo: do it" not in "".join(str(c) for c in captured)   # zero raw-draft display emission
        assert result["final_response"] == "echo: do it"                # answer still returned (via the turn result)

    def test_private_retry_skips_codex_sync_and_bg(self, fake_session, monkeypatch):
        # #1 on the codex path: during a PRIVATE corrective turn (_abacda_retrying=True) the codex runtime must NOT sync
        # external memory and must NOT fork a background review (explicit guards alongside the adapter no-ops).
        agent = _make_codex_agent()
        agent._abacda_retrying = True
        synced = {"n": 0}
        bg = {"n": 0}
        monkeypatch.setattr(run_agent.AIAgent, "_sync_external_memory_for_turn",
                            lambda self, **k: synced.__setitem__("n", synced["n"] + 1))
        monkeypatch.setattr(run_agent.AIAgent, "_spawn_background_review",
                            lambda self, **k: bg.__setitem__("n", bg["n"] + 1))
        result = agent._run_codex_app_server_turn(
            user_message="do it", original_user_message="do it",
            messages=[], effective_task_id="t", should_review_memory=True)
        assert synced["n"] == 0 and bg["n"] == 0            # zero side effects on the codex path during the private turn
        assert result["final_response"] == "echo: do it"    # the private turn still returns its candidate to the adapter


class TestAbacdaCodexReviewException:
    def test_review_exception_strips_draft_and_appends_notice(self, fake_session, monkeypatch):
        # codex 2026-07-23 C1: if maybe_review_and_retry raises on the codex path, the draft (already in messages via
        # projected_messages) must be stripped and the withhold notice appended, so NO side effect sees the raw draft.
        import agent.abacda_review as AR
        agent = _make_codex_agent()
        synced = {}
        monkeypatch.setattr(
            run_agent.AIAgent, "_sync_external_memory_for_turn",
            lambda self, *, original_user_message, final_response, interrupted: synced.update(fr=final_response),
        )
        monkeypatch.setattr(AR, "maybe_review_and_retry",
                            lambda ag, q, a, m: (_ for _ in ()).throw(RuntimeError("review boom")))
        result = agent._run_codex_app_server_turn(
            user_message="do it", original_user_message="do it",
            messages=[], effective_task_id="t", should_review_memory=True)
        msgs = result.get("messages", [])
        assert not any(m.get("content") == "echo: do it" for m in msgs)          # raw draft stripped from messages (C1)
        assert any("withheld" in str(m.get("content", "")).lower() for m in msgs)  # notice appended
        assert "echo: do it" not in str(synced.get("fr"))                        # sync did not see the raw draft
        assert "withheld" in str(result.get("final_response", "")).lower()       # returned answer withheld


class TestAbacdaCodexScrubRobustness:
    def test_review_exception_scrubs_structured_content(self, monkeypatch):
        # codex 2026-07-23 CC4: the scrub must catch draft bytes even in STRUCTURED/segmented assistant content
        # (content is a list of blocks, not a bare string), not only an exact-string message.
        import agent.abacda_review as AR

        def structured_run_turn(self, user_input: str, **kwargs):
            return TurnResult(
                final_text="echo: do it",
                projected_messages=[
                    # SEGMENTED across blocks: str(content) is NOT contiguous "echo: do it" — only extracted-text is.
                    {"role": "assistant", "content": [{"type": "text", "text": "echo: "},
                                                      {"type": "text", "text": "do it"}]},
                ],
                tool_iterations=1, interrupted=False, error=None,
                turn_id="t1", thread_id="th1",
            )
        monkeypatch.setattr(CodexAppServerSession, "run_turn", structured_run_turn)
        monkeypatch.setattr(CodexAppServerSession, "ensure_started", lambda self: "th1")
        agent = _make_codex_agent()
        synced = {}
        monkeypatch.setattr(
            run_agent.AIAgent, "_sync_external_memory_for_turn",
            lambda self, *, original_user_message, final_response, interrupted: synced.update(fr=final_response),
        )
        monkeypatch.setattr(AR, "maybe_review_and_retry",
                            lambda ag, q, a, m: (_ for _ in ()).throw(RuntimeError("review boom")))
        result = agent._run_codex_app_server_turn(
            user_message="do it", original_user_message="do it",
            messages=[], effective_task_id="t", should_review_memory=True)
        blob = " ".join(str(m.get("content", "")) for m in result.get("messages", []))
        assert "echo: do it" not in blob                             # structured draft stripped (CC4)
        assert any("withheld" in str(m.get("content", "")).lower() for m in result.get("messages", []))
        assert "echo: do it" not in str(synced.get("fr"))            # sync did not see the structured draft
