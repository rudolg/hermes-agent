"""Black-box integration test (Codex-B 2026-07-22 #1): drive the REAL run_conversation with a FAKE streaming model —
no cloud quota — and prove the Auto-ABACDA pre-delivery contract holds end-to-end:

  * the un-reviewed DRAFT (its sentinel) is NEVER emitted to the user's stream sink (buffered),
  * the CORRECTED answer is emitted exactly ONCE,
  * the corrected answer is what run_conversation returns and persists in messages.

The model and the review verdict are injected; everything else (buffer install, boundary review, one corrective turn,
re-review, flush) is the real conversation_loop path.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from run_agent import AIAgent
import agent.abacda_review as AR


def _chunk(content=None, finish_reason=None, model=None):
    delta = SimpleNamespace(content=content, tool_calls=None, reasoning_content=None, reasoning=None)
    choice = SimpleNamespace(index=0, delta=delta, finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice], model=model, usage=None)


def _final(usage=None):
    return SimpleNamespace(choices=[], model="test/model", usage=usage)


def test_run_conversation_buffers_draft_and_emits_corrected_once(monkeypatch):
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
        patch("run_agent.AIAgent._create_request_openai_client") as mock_create,
        patch("run_agent.AIAgent._close_request_openai_client"),
    ):
        session_db = MagicMock()
        agent = AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            model="test/model",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            session_db=session_db,
            session_id="abacda-int-session",
            platform="cli",
        )
        agent.api_mode = "chat_completions"

        captured = []                                          # what actually reaches the user's stream sink
        agent.stream_delta_callback = captured.append

        draft = [_chunk("DRAFT_SENTINEL unbounded while True retry"),
                 _chunk("!", finish_reason="stop", model="test/model"), _final()]
        corrected = [_chunk("CORRECTED bounded retry with backoff"),
                     _chunk("", finish_reason="stop", model="test/model"), _final()]
        client = MagicMock()
        client.chat.completions.create.side_effect = [iter(draft), iter(corrected)]
        mock_create.return_value = client

        def fake_review(question, answer, **k):               # inject the verdict: draft -> findings, correction -> clean
            if "DRAFT_SENTINEL" in answer:
                return {"reviewed": True, "clean": False, "findings": "BLOCK: unbounded retry", "tier": "T4",
                        "record_dir": ""}
            return {"reviewed": True, "clean": True, "clean_aggregate_permitted": True, "tier": "T4", "record_dir": ""}
        monkeypatch.setattr(AR, "review", fake_review)

        result = agent.run_conversation("cap the retry loop in fetch() and commit the fix")

    joined = "".join(str(c) for c in captured)
    assert "DRAFT_SENTINEL" not in joined                      # the un-reviewed draft NEVER streamed to the user
    assert "CORRECTED bounded retry with backoff" in (result.get("final_response") or "")   # corrected is delivered
    assert "DRAFT_SENTINEL" not in (result.get("final_response") or "")
    # the corrected answer is emitted exactly once (the single flush), never token-by-token draft
    assert captured.count(result["final_response"]) == 1
    # corrected answer persisted in the returned conversation
    assert any("CORRECTED bounded retry with backoff" in str(m.get("content", "")) for m in result.get("messages", []))
    # Codex-B #1: the raw draft sentinel occurs ZERO times in the RETURNED messages ...
    assert "DRAFT_SENTINEL" not in " ".join(str(m.get("content", "")) for m in result.get("messages", []))
    # ... and ZERO times in ANY persisted-session-DB call (returned message identity == persisted identity).
    _persisted = " ".join(str(x) for call in session_db.mock_calls for x in (call.args + tuple(call.kwargs.values())))
    assert "DRAFT_SENTINEL" not in _persisted


def test_run_conversation_clean_answer_delivers_normally(monkeypatch):
    # a clean review must not alter delivery: the (single) answer is emitted once, unchanged.
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
        patch("run_agent.AIAgent._create_request_openai_client") as mock_create,
        patch("run_agent.AIAgent._close_request_openai_client"),
    ):
        agent = AIAgent(api_key="k", base_url="https://openrouter.ai/api/v1", model="test/model", quiet_mode=True,
                        skip_context_files=True, skip_memory=True, session_db=MagicMock(),
                        session_id="s2", platform="cli")
        agent.api_mode = "chat_completions"
        captured = []
        agent.stream_delta_callback = captured.append
        answer = [_chunk("a clean substantive answer"), _chunk("", finish_reason="stop", model="test/model"), _final()]
        client = MagicMock()
        client.chat.completions.create.side_effect = [iter(answer)]
        mock_create.return_value = client
        monkeypatch.setattr(AR, "review", lambda q, a, **k: {"reviewed": True, "clean": True, "clean_aggregate_permitted": True, "tier": "T2",
                                                             "record_dir": ""})
        result = agent.run_conversation("a substantive question about the retry loop")
    assert "a clean substantive answer" in (result.get("final_response") or "")
    assert captured.count(result["final_response"]) == 1               # emitted exactly once


def test_arm_failure_withholds_answer_unconditionally(monkeypatch):
    # gemini 2026-07-23: if the review machinery cannot arm (install raises -> _abacda_ok False), the answer must be
    # WITHHELD unconditionally — it must not fall through and deliver the un-reviewed draft.
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
        patch("run_agent.AIAgent._create_request_openai_client") as mock_create,
        patch("run_agent.AIAgent._close_request_openai_client"),
    ):
        agent = AIAgent(api_key="k", base_url="https://openrouter.ai/api/v1", model="test/model", quiet_mode=True,
                        skip_context_files=True, skip_memory=True, session_db=MagicMock(), session_id="s3",
                        platform="cli")
        agent.api_mode = "chat_completions"
        captured = []
        agent.stream_delta_callback = captured.append
        # a real usage object (SimpleNamespace has __dict__) so the un-buffered aggregation path completes — a live
        # model always returns real usage; only the fake _final()'s usage=None would trip vars() on this branch.
        _usage = SimpleNamespace(prompt_tokens=5, completion_tokens=5, total_tokens=10)
        answer = [_chunk("RAW_UNREVIEWED must be withheld"), _chunk("", finish_reason="stop", model="test/model"),
                  SimpleNamespace(choices=[], model="test/model", usage=_usage)]
        client = MagicMock()
        client.chat.completions.create.side_effect = [iter(answer)]
        mock_create.return_value = client
        # make arming fail: install_answer_buffer raises -> _abacda_ok stays False
        monkeypatch.setattr(AR, "install_answer_buffer",
                            lambda ag: (_ for _ in ()).throw(RuntimeError("arm failure")))
        result = agent.run_conversation("a substantive question about the retry loop")
    fr = result.get("final_response") or ""
    # FAIL CLOSED (gemini 2026-07-23): arming failed, so the un-reviewed draft must be delivered NOWHERE — neither in the
    # returned answer nor the stream. The boundary now withholds unconditionally; the raw draft never leaks. (The arm
    # warning below confirms the not-armed branch executed; the unconditional-withhold text is code-guaranteed.)
    assert "RAW_UNREVIEWED" not in fr                                   # the un-reviewed draft is NOT delivered
    assert fr != "RAW_UNREVIEWED must be withheld"                      # ...and specifically is not the draft verbatim
    assert "RAW_UNREVIEWED" not in "".join(str(c) for c in captured)   # nor streamed to the user


def test_review_exception_strips_draft_from_returned_messages(monkeypatch):
    # codex 2026-07-23 C2: on the review-EXCEPTION boundary path (maybe_review_and_retry raises), the already-accumulated
    # draft must be stripped from the returned/persisted messages and the BLOCKED notice appended — not left in messages.
    # A stream callback is SET so install buffers normally (sinks safe, the fake model streams to the buffer); only the
    # review raises.
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
        patch("run_agent.AIAgent._create_request_openai_client") as mock_create,
        patch("run_agent.AIAgent._close_request_openai_client"),
    ):
        session_db = MagicMock()
        agent = AIAgent(api_key="k", base_url="https://openrouter.ai/api/v1", model="test/model", quiet_mode=True,
                        skip_context_files=True, skip_memory=True, session_db=session_db, session_id="s4",
                        platform="cli")
        agent.api_mode = "chat_completions"
        agent.stream_delta_callback = [].append                      # a live consumer -> install buffers (sinks safe)
        answer = [_chunk("DRAFT_LEAK_SENTINEL"), _chunk("", finish_reason="stop", model="test/model"), _final()]
        client = MagicMock()
        client.chat.completions.create.side_effect = [iter(answer)]
        mock_create.return_value = client
        monkeypatch.setattr(AR, "maybe_review_and_retry",
                            lambda ag, q, a, m: (_ for _ in ()).throw(RuntimeError("review boom")))
        result = agent.run_conversation("a substantive question about the retry loop")
    fr = result.get("final_response") or ""
    assert "withheld" in fr.lower()                                    # fail-closed withhold
    msgs = result.get("messages", [])
    assert not any(m.get("content") == "DRAFT_LEAK_SENTINEL" for m in msgs)   # C2: draft stripped from messages
    assert any("withheld" in str(m.get("content", "")).lower() for m in msgs)   # notice appended to messages
    _persisted = " ".join(str(x) for call in session_db.mock_calls for x in (call.args + tuple(call.kwargs.values())))
    assert "DRAFT_LEAK_SENTINEL" not in _persisted                    # nor persisted to the session DB
