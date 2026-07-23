"""Hermetic tests for the Hermes-native adapter (agent/abacda_review.py).

Proves the orchestration contract with the engine (review) and the model turn (agent.run_conversation) injected:
silent trivia, silent clean, ONE corrective turn on findings, RE-REVIEW of the correction, deliver-corrected vs
report-BLOCKED, fail-closed on engine error / empty correction / still-blocked correction, and both recursion brakes."""
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parents[1]
if str(_HERE / "agent") not in sys.path:
    sys.path.insert(0, str(_HERE / "agent"))

import abacda_review as AR  # noqa: E402


class _FakeAgent:
    def __init__(self, corrected="CORRECTED final answer"):
        self.calls = 0
        self.flag_during_call = None
        self._corrected = corrected

    def run_conversation(self, user_message, conversation_history=None):
        self.calls += 1
        self.flag_during_call = getattr(self, "_abacda_retrying", False)
        self.last_user = user_message
        return {"final_response": self._corrected,
                "messages": list(conversation_history or []) + [{"role": "assistant", "content": self._corrected}]}


def _seq_reviews(*verdicts):
    """Return a `review`-shaped callable that yields the given verdict dicts in order across calls."""
    calls = {"n": 0, "seen": []}

    def review(question, answer, **k):
        v = verdicts[min(calls["n"], len(verdicts) - 1)]
        calls["n"] += 1
        calls["seen"].append(answer)
        return dict(v)
    return calls, review


def test_trivia_delivers_silently(monkeypatch):
    monkeypatch.setattr(AR, "review", lambda q, a, **k: {"reviewed": False, "clean": True, "tier": "T0"})
    ag = _FakeAgent()
    ans, msgs, rev = AR.maybe_review_and_retry(ag, "hi", "Fixed a typo.", [])
    assert ans == "Fixed a typo." and ag.calls == 0


def test_clean_delivers_silently(monkeypatch):
    monkeypatch.setattr(AR, "review", lambda q, a, **k: {"reviewed": True, "clean": True, "clean_aggregate_permitted": True, "tier": "T2"})
    ag = _FakeAgent()
    ans, msgs, rev = AR.maybe_review_and_retry(ag, "q", "a substantive answer", [])
    assert ans == "a substantive answer" and ag.calls == 0


def test_findings_then_correction_reviewed_clean_delivers_corrected(monkeypatch):
    calls, review = _seq_reviews(
        {"reviewed": True, "clean": False, "findings": "BLOCK: unbounded retry"},   # initial review of draft
        {"reviewed": True, "clean": True, "clean_aggregate_permitted": True})                                          # re-review of the correction
    monkeypatch.setattr(AR, "review", review)
    ag = _FakeAgent(corrected="bounded retry with backoff")
    ans, msgs, rev = AR.maybe_review_and_retry(ag, "q", "unbounded retry draft", [{"role": "user", "content": "q"}])
    assert ag.calls == 1                                            # exactly ONE corrective turn
    assert ans == "bounded retry with backoff"                     # deliver the corrected answer
    assert ag.flag_during_call is True                             # corrective turn guarded from re-review
    assert calls["n"] == 2                                         # the correction WAS re-reviewed
    assert calls["seen"][1] == "bounded retry with backoff"        # re-review saw the CORRECTED answer


def test_correction_still_blocked_is_reported_blocked(monkeypatch):
    calls, review = _seq_reviews(
        {"reviewed": True, "clean": False, "findings": "BLOCK: unsafe"},
        {"reviewed": True, "clean": False, "findings": "BLOCK: still unsafe"})       # re-review still fails
    monkeypatch.setattr(AR, "review", review)
    ag = _FakeAgent(corrected="still-bad answer")
    ans, msgs, rev = AR.maybe_review_and_retry(ag, "q", "bad draft", [])
    assert "WITHHELD" in ans and "still-bad answer" not in ans       # reported blocked; body NOT disclosed
    assert ag.calls == 1                                           # only one corrective turn (no loop)


def test_empty_correction_does_not_release_original(monkeypatch):
    monkeypatch.setattr(AR, "review", lambda q, a, **k: {"reviewed": True, "clean": False, "findings": "BLOCK"})
    ag = _FakeAgent(corrected="   ")                               # corrective turn produced nothing
    ans, msgs, rev = AR.maybe_review_and_retry(ag, "q", "the ORIGINAL blocked answer", [])
    assert "WITHHELD" in ans and "the ORIGINAL blocked answer" not in ans  # fail closed; original NOT disclosed


def test_engine_error_on_substantive_fails_closed(monkeypatch):
    monkeypatch.setattr(AR, "review", lambda q, a, **k: {"reviewed": False, "engine_error": "TimeoutExpired", "clean": False})
    ag = _FakeAgent()
    ans, msgs, rev = AR.maybe_review_and_retry(ag, "q", "a substantive answer", [])
    assert "BLOCKED" in ans and "did not complete" in ans and ag.calls == 0   # fix 3: no silent release on engine failure


def test_retry_flag_blocks_reentry(monkeypatch):
    monkeypatch.setattr(AR, "review", lambda q, a, **k: (_ for _ in ()).throw(AssertionError("must not review during retry")))
    ag = _FakeAgent()
    ag._abacda_retrying = True
    ans, msgs, rev = AR.maybe_review_and_retry(ag, "q", "an answer", [])
    assert ans == "an answer" and ag.calls == 0 and rev is None


def test_sentinel_env_blocks_review(monkeypatch):
    monkeypatch.setenv("HERMES_ABACDA_INTERNAL", "1")
    monkeypatch.setattr(AR, "review", lambda q, a, **k: (_ for _ in ()).throw(AssertionError("sentinel must skip review")))
    ag = _FakeAgent()
    ans, msgs, rev = AR.maybe_review_and_retry(ag, "q", "an answer", [])
    assert ans == "an answer" and ag.calls == 0


# --- fix 4: the stream buffer holds the un-reviewed draft and emits only the FINAL answer once ---

class _StreamAgent:
    def __init__(self):
        self.delivered = []
        self.stream_delta_callback = self.delivered.append
        self._stream_callback = None


def test_stream_buffer_holds_draft_and_emits_final_once():
    ag = _StreamAgent()
    real = ag.stream_delta_callback
    tok = AR.install_answer_buffer(ag)
    assert tok is not None and ag.stream_delta_callback is not real     # sink swapped for the buffer
    ag.stream_delta_callback("draft token 1")
    ag.stream_delta_callback("draft token 2")
    assert ag.delivered == []                                           # the DRAFT never reached the user
    AR.flush_answer(ag, tok, "FINAL corrected answer")
    assert ag.delivered == ["FINAL corrected answer"]                   # only the final answer, once
    assert ag.stream_delta_callback is real                            # real sink restored


def test_stream_buffer_noop_without_consumer():
    class _Ag:
        stream_delta_callback = None
        _stream_callback = None
    assert AR.install_answer_buffer(_Ag()) is None                      # oneshot/cron: nothing streams -> nothing to buffer


def test_stream_buffer_noop_during_corrective_turn():
    ag = _StreamAgent()
    ag._abacda_retrying = True
    assert AR.install_answer_buffer(ag) is None                         # nested corrective turn must not re-buffer


# --- Codex-B round-2 hardening: fail-closed on raises, strict re-review, no raw staging, flush dedup/failure ---

def test_corrective_turn_raising_fails_closed(monkeypatch):
    monkeypatch.setattr(AR, "review", lambda q, a, **k: {"reviewed": True, "clean": False, "findings": "BLOCK: unsafe"})

    class _Ag:
        calls = 0

        def run_conversation(self, *a, **k):
            raise RuntimeError("model provider down")
    ag = _Ag()
    ans, msgs, rev = AR.maybe_review_and_retry(ag, "q", "the ORIGINAL unreviewed answer", [])
    assert "WITHHELD" in ans and "ORIGINAL unreviewed answer" not in ans  # #1: withheld, body NOT disclosed


def test_rereview_malformed_result_fails_closed(monkeypatch):
    calls, review = _seq_reviews(
        {"reviewed": True, "clean": False, "findings": "BLOCK"},
        {"reviewed": False, "clean": False})                            # malformed re-review: no engine_error, not clean
    monkeypatch.setattr(AR, "review", review)
    ag = _FakeAgent(corrected="corrected-but-unverifiable")
    ans, msgs, rev = AR.maybe_review_and_retry(ag, "q", "draft", [])
    assert "WITHHELD" in ans and "corrected-but-unverifiable" not in ans  # #7: unknown state -> withheld, no disclosure


def test_review_passes_material_over_stdin_no_raw_staging(monkeypatch, tmp_path):
    import subprocess as _sp
    captured = {}

    class _CP:
        returncode, stdout, stderr = 0, '{"reviewed": false, "clean": true}', ""

    def fake_run(argv, **kw):
        captured["argv"], captured["input"] = argv, kw.get("input")
        return _CP()
    monkeypatch.setattr(_sp, "run", fake_run)
    monkeypatch.setattr(AR, "_RECORDS_BASE", tmp_path)
    AR.review("configure the client with SSN 123-45-6789", "api_key = zz-livesecret-abc123")
    assert "--stdin" in captured["argv"]                               # #3: material over stdin
    assert "--question-file" not in captured["argv"] and "--answer-file" not in captured["argv"]
    assert "123-45-6789" in (captured["input"] or "")                  # raw goes to stdin, not disk
    staged = [p for p in tmp_path.rglob("*")
              if p.is_file() and "123-45-6789" in p.read_text(encoding="utf-8", errors="replace")]
    assert staged == []                                               # #3: raw sensitive NEVER written to disk by adapter


def test_flush_dedup_same_consumer_via_both_callbacks():
    delivered = []

    class _Ag:
        pass
    ag = _Ag()
    same = delivered.append
    ag.stream_delta_callback = same
    ag._stream_callback = same                                        # one consumer, two paths (same object)
    tok = AR.install_answer_buffer(ag)
    AR.flush_answer(ag, tok, "FINAL")
    assert delivered == ["FINAL"]                                      # #4: emitted ONCE, not twice


def test_flush_dedup_distinct_bound_methods_same_receiver():
    # Codex #4: two DISTINCT bound-method objects for the same receiver.method must dedup (id() alone would not).
    class _Sink:
        def __init__(self):
            self.got = []

        def send(self, t):
            self.got.append(t)

    class _Ag:
        pass
    s = _Sink()
    ag = _Ag()
    ag.stream_delta_callback = s.send                                 # bound-method object #1
    ag._stream_callback = s.send                                      # bound-method object #2 (different id!)
    assert ag.stream_delta_callback is not ag._stream_callback        # they really are distinct objects
    tok = AR.install_answer_buffer(ag)
    AR.flush_answer(ag, tok, "FINAL")
    assert s.got == ["FINAL"]                                         # emitted ONCE despite two distinct bound methods


def test_flush_reports_callback_failure():
    class _Ag:
        def __init__(self):
            self.stream_delta_callback = self._boom
            self._stream_callback = None

        def _boom(self, t):
            raise RuntimeError("sink dead")
    ag = _Ag()
    tok = AR.install_answer_buffer(ag)
    assert AR.flush_answer(ag, tok, "FINAL") is False                  # #6: silent delivery loss is surfaced


def test_install_failure_suppresses_draft():
    class _Ag:
        _stream_callback = None

        def __init__(self):
            self._sd = lambda t: None

        @property
        def stream_delta_callback(self):
            return self._sd

        @stream_delta_callback.setter
        def stream_delta_callback(self, v):
            if v is not None:
                raise RuntimeError("cannot install buffer")
            self._sd = None
    ag = _Ag()
    tok = AR.install_answer_buffer(ag)
    assert tok is not None and tok.get("degraded") is True             # #5: swap failure -> draft suppressed (fail closed)
    assert ag._sd is None                                              # the live sink was nulled, no draft leaks


# --- Codex-B round-3 hardening: strict trivia disposition, partial-swap degraded ---

def test_malformed_review_no_tier_fails_closed(monkeypatch):
    # reviewed=False, no engine_error, no tier -> NOT trivia (absence of error != certification) -> fail closed (#3)
    monkeypatch.setattr(AR, "review", lambda q, a, **k: {"reviewed": False, "clean": False})
    ag = _FakeAgent()
    ans, msgs, rev = AR.maybe_review_and_retry(ag, "q", "a substantive UNCERTIFIED answer", [])
    assert "WITHHELD" in ans and "a substantive UNCERTIFIED answer" not in ans and ag.calls == 0


def test_explicit_trivia_tier_delivers(monkeypatch):
    monkeypatch.setattr(AR, "review", lambda q, a, **k: {"reviewed": False, "clean": True, "tier": "T0"})
    ag = _FakeAgent()
    ans, msgs, rev = AR.maybe_review_and_retry(ag, "q", "Fixed a typo.", [])
    assert ans == "Fixed a typo." and ag.calls == 0                  # EXPLICIT below-Duo disposition -> deliver


def test_reviewed_not_clean_goes_to_findings(monkeypatch):
    # reviewed=True but clean missing/False must NOT deliver as clean -> enters the corrective path
    calls, review = _seq_reviews({"reviewed": True, "clean": False, "findings": "BLOCK"}, {"reviewed": True, "clean": True, "clean_aggregate_permitted": True})
    monkeypatch.setattr(AR, "review", review)
    ag = _FakeAgent(corrected="fixed")
    ans, msgs, rev = AR.maybe_review_and_retry(ag, "q", "draft", [])
    assert ans == "fixed" and ag.calls == 1                          # not released as clean; corrected + re-reviewed


def test_install_partial_swap_is_degraded_and_fully_suppressed():
    # Codex #1/#2: if one sink swap fails, the buffer install must not leave a LIVE sink — it degrades + suppresses both.
    class _Ag:
        def __init__(self):
            self.stream_delta_callback = lambda t: None              # plain settable sink
            self._sc = lambda t: None

        @property
        def _stream_callback(self):
            return self._sc

        @_stream_callback.setter
        def _stream_callback(self, v):
            if v is not None:
                raise RuntimeError("cannot swap second sink")
            self._sc = None
    ag = _Ag()
    tok = AR.install_answer_buffer(ag)
    assert tok is not None and tok.get("degraded") is True           # partial swap -> degraded (caller withholds)
    assert ag.stream_delta_callback is None and ag._sc is None       # NO live un-buffered sink remains


# --- Codex-B round-6 #3: ONE shared answer-level monotonic deadline (fake clock) ---

def test_shared_deadline_propagates_and_caps_each_phase(monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr(AR.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(AR, "_ANSWER_DEADLINE_S", 480)
    seen = []

    def fake_review(q, a, *, timeout_s=None, **k):
        seen.append(timeout_s)
        clock["t"] += 200                                             # each review burns 200s of the SHARED budget
        return ({"reviewed": True, "clean": False, "findings": "BLOCK", "tier": "T4", "record_dir": ""}
                if "DRAFT" in a else {"reviewed": True, "clean": True, "clean_aggregate_permitted": True, "tier": "T4", "record_dir": ""})
    monkeypatch.setattr(AR, "review", fake_review)
    ag = _FakeAgent(corrected="fixed")
    ans, msgs, rev = AR.maybe_review_and_retry(ag, "q", "DRAFT answer", [])
    assert seen[0] == 480                                             # initial review: full budget
    assert 0 < seen[1] <= 280                                        # re-review: capped by the SAME remaining deadline
    assert ans == "fixed"


def test_deadline_exhausted_before_rereview_fails_closed(monkeypatch):
    clock = {"t": 0.0}
    monkeypatch.setattr(AR.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(AR, "_ANSWER_DEADLINE_S", 300)
    n = {"c": 0}

    def fake_review(q, a, *, timeout_s=None, **k):
        n["c"] += 1
        clock["t"] += 290                                            # initial review nearly exhausts the budget
        return {"reviewed": True, "clean": False, "findings": "BLOCK", "tier": "T4", "record_dir": ""}
    monkeypatch.setattr(AR, "review", fake_review)
    ag = _FakeAgent(corrected="fixed")
    ans, msgs, rev = AR.maybe_review_and_retry(ag, "q", "DRAFT", [])
    assert "WITHHELD" in ans and "deadline" in ans.lower()           # exhausted before re-review -> fail closed
    assert n["c"] == 1                                               # re-review SKIPPED (no budget), not run past deadline


# --- Codex-B round-7: uncertified DRAFT removed from all returned paths + corrective-turn deadline ---

def test_helper_strips_draft_and_feedback():
    draft = "RAW_DRAFT_SENTINEL text"
    fb = AR.feedback_message("BLOCK: bad")
    msgs = [{"role": "user", "content": "q"},
            {"role": "assistant", "content": draft},
            {"role": "user", "content": fb},
            {"role": "assistant", "content": "CORRECTED"}]
    out = AR._without_draft_and_feedback(msgs, draft, fb)
    assert not any(m.get("content") == draft for m in out)          # draft gone
    assert not any(m.get("content") == fb for m in out)             # internal feedback gone
    assert any(m.get("content") == "CORRECTED" for m in out)        # correction kept


def test_corrected_return_excludes_the_raw_draft(monkeypatch):
    calls, review = _seq_reviews({"reviewed": True, "clean": False, "findings": "BLOCK", "record_dir": ""},
                                 {"reviewed": True, "clean": True, "clean_aggregate_permitted": True})
    monkeypatch.setattr(AR, "review", review)

    class _Ag:
        calls = 0

        def run_conversation(self, user_message, conversation_history=None):
            self.calls += 1
            return {"final_response": "CORRECTED",
                    "messages": list(conversation_history or []) + [{"role": "assistant", "content": "CORRECTED"}]}
    ag = _Ag()
    draft = "RAW_DRAFT_SENTINEL answer"
    msgs = [{"role": "user", "content": "q"}, {"role": "assistant", "content": draft}]   # draft in history
    ans, out, rev = AR.maybe_review_and_retry(ag, "q", draft, msgs)
    assert ans == "CORRECTED"
    assert not any(m.get("content") == draft for m in out)          # #1: raw draft ZERO in returned messages
    assert "RAW_DRAFT_SENTINEL" not in " ".join(str(m.get("content")) for m in out)


def test_blocked_return_excludes_the_raw_draft(monkeypatch):
    monkeypatch.setattr(AR, "review", lambda q, a, **k: {"reviewed": True, "clean": False, "findings": "BLOCK",
                                                         "record_dir": ""})

    class _Ag:
        def run_conversation(self, *a, **k):
            return {"final_response": "", "messages": []}            # empty correction -> BLOCKED
    ag = _Ag()
    draft = "RAW_DRAFT_SENTINEL"
    ans, out, rev = AR.maybe_review_and_retry(ag, "q", draft, [{"role": "assistant", "content": draft}])
    assert "WITHHELD" in ans
    assert not any(m.get("content") == draft for m in out)          # draft removed even on the BLOCKED path


def test_corrective_turn_interrupt_fails_closed(monkeypatch):
    monkeypatch.setattr(AR, "review", lambda q, a, **k: {"reviewed": True, "clean": False, "findings": "BLOCK",
                                                         "record_dir": ""})

    class _Ag:
        def interrupt(self, msg=None):
            pass

        def clear_interrupt(self):
            pass

        def run_conversation(self, user_message, conversation_history=None):
            return {"final_response": "", "messages": list(conversation_history or []), "interrupted": True}
    ag = _Ag()
    ans, out, rev = AR.maybe_review_and_retry(ag, "q", "DRAFT answer", [])
    assert "WITHHELD" in ans and "deadline" in ans.lower()          # #2: interrupted corrective turn -> fail closed


def test_corrective_turn_timer_bounded_by_remaining(monkeypatch):
    clock = {"t": 0.0}
    monkeypatch.setattr(AR.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(AR, "_ANSWER_DEADLINE_S", 400)

    def fake_review(q, a, *, timeout_s=None, **k):
        clock["t"] += 100                                           # initial review burns 100s of the budget
        return ({"reviewed": True, "clean": False, "findings": "BLOCK", "record_dir": ""} if "DRAFT" in a
                else {"reviewed": True, "clean": True, "clean_aggregate_permitted": True})
    monkeypatch.setattr(AR, "review", fake_review)
    delays = []
    _RealTimer = AR.threading.Timer

    def fake_timer(delay, fn, args=()):
        delays.append(delay)
        return _RealTimer(delay, fn, args)
    monkeypatch.setattr(AR.threading, "Timer", fake_timer)
    ag = _FakeAgent(corrected="fixed")
    AR.maybe_review_and_retry(ag, "q", "DRAFT answer", [])
    assert delays and delays[0] == 300                             # corrective-turn deadline == remaining (400-100)


def test_private_corrective_turn_has_zero_side_effects(monkeypatch):
    # Codex-B 2026-07-22 #1: while _abacda_retrying is true the nested turn must persist NOTHING, sync NO external memory,
    # and fork NO background review. The adapter no-ops those methods around the nested call and restores them after.
    real = {"persist": 0, "sync": 0, "bg": 0}
    seen = {"retrying": None, "persist_is_noop": None, "sync_is_noop": None, "bg_is_noop": None}
    calls, review = _seq_reviews({"reviewed": True, "clean": False, "findings": "BLOCK", "record_dir": ""},
                                 {"reviewed": True, "clean": True, "clean_aggregate_permitted": True})
    monkeypatch.setattr(AR, "review", review)

    class _Ag:
        def _persist_session(self, *a, **k):
            real["persist"] += 1

        def _save_trajectory(self, *a, **k):
            pass

        def _sync_external_memory_for_turn(self, *a, **k):
            real["sync"] += 1

        def _spawn_background_review(self, *a, **k):
            real["bg"] += 1

        def clear_interrupt(self):
            pass

        def run_conversation(self, user_message, conversation_history=None):
            # observe state DURING the private turn: the flag is set and the side-effect methods are the no-ops
            seen["retrying"] = getattr(self, "_abacda_retrying", False)
            seen["persist_is_noop"] = getattr(self._persist_session, "__name__", "") != "_persist_session"
            seen["sync_is_noop"] = getattr(self._sync_external_memory_for_turn, "__name__", "") != "_sync_external_memory_for_turn"
            seen["bg_is_noop"] = getattr(self._spawn_background_review, "__name__", "") != "_spawn_background_review"
            # a private turn that TRIES to fire side effects must hit no-ops (counters stay 0)
            self._persist_session([], [])
            self._sync_external_memory_for_turn(final_response="CORRECTED")
            self._spawn_background_review(messages_snapshot=[])
            return {"final_response": "CORRECTED",
                    "messages": list(conversation_history or []) + [{"role": "assistant", "content": "CORRECTED"}]}

    ag = _Ag()
    ans, out, rev = AR.maybe_review_and_retry(ag, "q", "DRAFT", [{"role": "assistant", "content": "DRAFT"}])
    assert ans == "CORRECTED"
    assert seen["retrying"] is True                                   # the corrective turn ran under the retry flag
    assert seen["persist_is_noop"] and seen["sync_is_noop"] and seen["bg_is_noop"]   # methods were suppressed
    assert real == {"persist": 0, "sync": 0, "bg": 0}                 # ZERO real side effects during the private turn
    # restored afterward: the outer turn regains its real persistence/sync surface
    ag._persist_session([], []); ag._sync_external_memory_for_turn(final_response="x")
    assert real["persist"] == 1 and real["sync"] == 1


def test_failed_re_review_strips_both_original_and_corrected(monkeypatch):
    # Codex-B 2026-07-22 #2: when the correction ALSO fails re-review, BOTH the original draft and the corrected draft
    # must be removed from the returned/persisted messages — only the BLOCKED notice remains.
    calls, review = _seq_reviews({"reviewed": True, "clean": False, "findings": "BLOCK original", "record_dir": ""},
                                 {"reviewed": True, "clean": False, "findings": "STILL bad", "record_dir": ""})
    monkeypatch.setattr(AR, "review", review)

    class _Ag:
        def clear_interrupt(self):
            pass

        def run_conversation(self, user_message, conversation_history=None):
            return {"final_response": "CORRECTED_SENTINEL",
                    "messages": list(conversation_history or []) +
                    [{"role": "assistant", "content": "CORRECTED_SENTINEL"}]}

    ag = _Ag()
    orig = "ORIGINAL_DRAFT_SENTINEL"
    ans, out, rev = AR.maybe_review_and_retry(ag, "q", orig, [{"role": "assistant", "content": orig}])
    assert "WITHHELD" in ans                                          # re-review failed -> BLOCKED, body withheld
    joined = " ".join(str(m.get("content")) for m in out)
    assert "ORIGINAL_DRAFT_SENTINEL" not in joined                   # original draft stripped
    assert "CORRECTED_SENTINEL" not in joined                        # corrected draft ALSO stripped (the #2 fix)


def test_clean_without_aggregate_permitted_is_not_released(monkeypatch):
    # codex/grok 2026-07-23 #1: reviewed+clean but WITHOUT clean_aggregate_permitted must NOT be released — a stale or
    # mocked engine could set clean=True without the canonical gate. The delivery boundary requires the gate to be True.
    monkeypatch.setattr(AR, "review", lambda q, a, **k: {"reviewed": True, "clean": True})   # NO clean_aggregate_permitted

    class _Ag:
        def clear_interrupt(self):
            pass

        def run_conversation(self, *a, **k):
            return {"final_response": "STILL_UNCERTIFIED", "messages": []}

    ag = _Ag()
    ans, out, rev = AR.maybe_review_and_retry(ag, "q", "a substantive uncertified answer", [])
    assert "WITHHELD" in ans                                          # clean-without-gate never released as done


def test_below_duo_missing_clean_fails_closed(monkeypatch):
    # codex 2026-07-23 #2: a below-Duo disposition with clean MISSING must not be treated as permission (`is not False`
    # would have released it); require an explicit clean True, else BLOCK.
    monkeypatch.setattr(AR, "review", lambda q, a, **k: {"reviewed": False, "tier": "T0"})   # no clean field

    class _Ag:
        def run_conversation(self, *a, **k):
            return {"final_response": "x", "messages": []}

    ans, out, rev = AR.maybe_review_and_retry(_Ag(), "q", "a substantive answer", [])
    assert "WITHHELD" in ans                                          # unrecognized disposition -> fail closed


def test_suppression_failure_aborts_corrective_turn(monkeypatch):
    # codex/gemini 2026-07-23 #3: if a side-effect surface cannot be neutralized, the corrective turn must NOT run.
    monkeypatch.setattr(AR, "review", lambda q, a, **k: {"reviewed": True, "clean": False, "findings": "BLOCK"})
    ran = {"n": 0}

    class _Ag:
        @property
        def _persist_session(self):                                  # read-only property -> setattr(no-op) raises
            return lambda *a, **k: None

        def clear_interrupt(self):
            pass

        def run_conversation(self, *a, **k):
            ran["n"] += 1
            return {"final_response": "CORRECTED", "messages": []}

    ag = _Ag()
    ans, out, rev = AR.maybe_review_and_retry(ag, "q", "DRAFT", [])
    assert ran["n"] == 0                                             # corrective turn NEVER ran (fail closed)
    assert "WITHHELD" in ans


def test_blocked_path_persists_the_notice_as_assistant_message(monkeypatch):
    # codex 2026-07-23 #4: the BLOCKED notice must be APPENDED to the returned/persisted messages (not an answer-less
    # turn, not the draft). The persisted assistant turn equals the returned banner.
    monkeypatch.setattr(AR, "review", lambda q, a, **k: {"reviewed": True, "clean": False, "findings": "BLOCK"})

    class _Ag:
        def clear_interrupt(self):
            pass

        def run_conversation(self, *a, **k):
            return {"final_response": "", "messages": [{"role": "assistant", "content": "DRAFT"}]}

    ag = _Ag()
    ans, out, rev = AR.maybe_review_and_retry(ag, "q", "DRAFT", [{"role": "assistant", "content": "DRAFT"}])
    assert "WITHHELD" in ans
    assert out and out[-1]["role"] == "assistant" and out[-1]["content"] == ans   # notice persisted == returned banner
    assert not any(m.get("content") == "DRAFT" for m in out)         # no uncertified draft persisted


def test_suppression_setup_exception_clears_flag_and_blocks(monkeypatch):
    # codex 2026-07-23 C4: if suppression SETUP raises (e.g. a hostile attribute) AFTER the retry flag is set, the outer
    # finally must still restore the agent and CLEAR _abacda_retrying — else the agent bypasses review/persistence forever.
    monkeypatch.setattr(AR, "review", lambda q, a, **k: {"reviewed": True, "clean": False, "findings": "BLOCK"})
    ran = {"n": 0}

    class _Ag:
        @property
        def _persist_session(self):
            raise RuntimeError("hostile attribute during suppression discovery")

        def clear_interrupt(self):
            pass

        def run_conversation(self, *a, **k):
            ran["n"] += 1
            return {"final_response": "CORRECTED", "messages": []}

    ag = _Ag()
    ans, out, rev = AR.maybe_review_and_retry(ag, "q", "DRAFT", [])
    assert ran["n"] == 0                                              # corrective turn never ran (setup blew up)
    assert "WITHHELD" in ans                                         # fail closed
    assert getattr(ag, "_abacda_retrying", False) is False           # flag CLEARED despite the setup exception (C4)


def test_scrub_handles_structured_and_segmented_content():
    # codex/gemini 2026-07-23 F1/F2: _without_draft_and_feedback (the NORMAL corrective/blocked path) must strip a draft
    # even when the assistant content is a LIST of blocks and the draft is SPLIT ACROSS blocks — extracted-text match,
    # not str(content). A kept unrelated answer (exact-match, not substring) must survive.
    draft = "echo: do it"
    msgs = [
        {"role": "assistant", "content": [{"type": "text", "text": "echo: "}, {"type": "text", "text": "do it"}]},
        {"role": "assistant", "content": "an unrelated kept answer"},
    ]
    assert AR.message_text(msgs[0]["content"]) == draft                # extractor concatenates segmented blocks
    out = AR._without_draft_and_feedback(msgs, draft, None)
    assert not any(AR.message_text(m.get("content")) == draft for m in out)   # segmented draft stripped
    assert any(m.get("content") == "an unrelated kept answer" for m in out)   # unrelated kept (no over-strip)


def test_scrub_keeps_corrected_when_stripping_only_original():
    # clean path passes drafts={original}; a corrected answer that merely CONTAINS the original as a substring must be
    # KEPT (exact extracted-text match, never substring) so the delivered answer is not over-stripped.
    original = "do it"
    msgs = [
        {"role": "assistant", "content": "do it"},                    # the original draft -> stripped
        {"role": "assistant", "content": "do it carefully with a bounded retry"},   # corrected -> kept (superset)
    ]
    out = AR._without_draft_and_feedback(msgs, original, None)
    assert not any(m.get("content") == "do it" for m in out)
    assert any(m.get("content") == "do it carefully with a bounded retry" for m in out)


def test_review_nonzero_engine_exit_is_not_clean(monkeypatch):
    # codex 2026-07-23: review() must NOT release as clean when the engine subprocess exits NONZERO, even if stdout JSON
    # claims clean=True/clean_aggregate_permitted=True. A contradictory exit/status must fail closed.
    import subprocess as _sp

    class _CP:
        returncode = 2
        stdout = '{"reviewed": true, "clean": true, "clean_aggregate_permitted": true}'
        stderr = ""
    monkeypatch.setattr(_sp, "run", lambda *a, **k: _CP())
    r = AR.review("q", "a substantive answer")                       # real subprocess path (runner=None)
    assert r["clean"] is False and r["clean_aggregate_permitted"] is False   # nonzero exit -> not clean
    assert r.get("engine_exit") == 2


# --- Semantic adjudication mapping (Greg 2026-07-23): the engine's Codex-B adjudication maps to delivery behavior, and
#     the raw findings pile NEVER reaches the user (only the ONE action prompt / HOLD reason). ---
def test_adjudication_clean_delivers_without_correction(monkeypatch):
    ran = {"n": 0}

    class _Ag:
        def clear_interrupt(self):
            pass

        def run_conversation(self, *a, **k):
            ran["n"] += 1
            return {"final_response": "SHOULD_NOT_RUN", "messages": []}
    monkeypatch.setattr(AR, "review", lambda q, a, **k: {
        "reviewed": True, "clean": False, "findings": "[codex] BLOCK: RAW_PILE_SENTINEL", "record_dir": "",
        "adjudication": {"decision": "CLEAN"}})
    ans, msgs, rev = AR.maybe_review_and_retry(_Ag(), "q", "the substantive answer", [])
    assert ans == "the substantive answer" and ran["n"] == 0          # adjudicator cleared it -> deliver, no correction


def test_adjudication_hold_withholds_no_raw_pile(monkeypatch):
    monkeypatch.setattr(AR, "review", lambda q, a, **k: {
        "reviewed": True, "clean": False, "findings": "[codex] BLOCK: RAW_PILE_SENTINEL\n[grok] FLAG: ...", "record_dir": "",
        "adjudication": {"decision": "HOLD", "action_prompt": "needs owner push authority"}})

    class _Ag:
        def clear_interrupt(self):
            pass

        def run_conversation(self, *a, **k):
            return {"final_response": "x", "messages": []}
    ans, out, rev = AR.maybe_review_and_retry(_Ag(), "q", "DRAFT", [])
    assert "WITHHELD" in ans and "needs owner push authority" in ans
    assert "RAW_PILE_SENTINEL" not in ans                             # the raw pile is NOT disclosed
    assert not any("RAW_PILE_SENTINEL" in str(m.get("content")) for m in out)


def test_adjudication_correct_feeds_action_prompt_not_pile(monkeypatch):
    seen = {}
    calls, review = _seq_reviews(
        {"reviewed": True, "clean": False, "findings": "[codex] BLOCK: RAW_PILE_SENTINEL", "record_dir": "",
         "adjudication": {"decision": "CORRECT", "action_prompt": "ACTION_PROMPT_SENTINEL: add a bounded max-retry"}},
        {"reviewed": True, "clean": True, "clean_aggregate_permitted": True})
    monkeypatch.setattr(AR, "review", review)

    class _Ag:
        def clear_interrupt(self):
            pass

        def run_conversation(self, user_message, conversation_history=None):
            seen["fb"] = user_message
            return {"final_response": "CORRECTED", "messages": list(conversation_history or []) +
                    [{"role": "assistant", "content": "CORRECTED"}]}
    ans, out, rev = AR.maybe_review_and_retry(_Ag(), "q", "DRAFT", [])
    assert ans == "CORRECTED"
    assert "ACTION_PROMPT_SENTINEL" in seen["fb"]                     # the corrective turn got the ONE action prompt
    assert "RAW_PILE_SENTINEL" not in seen["fb"]                      # NOT the raw four-arm pile
