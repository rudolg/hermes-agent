"""Hermes-native Auto-ABACDA adapter (JEDI).

Thin per-runtime adapter over the ONE shared review engine (`bin/review_response.py`). At Hermes's pre-delivery
boundary it reviews the proposed answer through the shared engine (tiering + deterministic sanitization + canonical
concurrent arms + five-part record — all under the deployed python), feeds genuine findings back for exactly ONE
corrective turn, RE-REVIEWS the correction once, and delivers the corrected answer OR reports BLOCKED. It never runs the
arms itself and never STAGES raw sensitive bytes to disk — material is passed to the engine over stdin.

FAIL CLOSED for a reviewed answer: an engine failure, a missing required lane, an unsanitizable sensitive value, a
corrective turn that errors or yields nothing, or a correction that does not pass a CLEAN re-review must NOT release the
original/uncertified answer as done — it is wrapped in a BLOCKED banner. Only a genuinely clean review (or below-Duo
trivia) delivers silently. ``maybe_review_and_retry`` never raises: every internal failure resolves to BLOCKED.

Two recursion brakes: the per-agent ``_abacda_retrying`` flag stops the corrective turn from being reviewed, and the
``HERMES_ABACDA_INTERNAL`` subprocess env stops anything the engine spawns from re-entering.

NAMED RESIDUALS (Codex-B 2026-07-22 #5 — accepted, not closed here):
  * EXTERNAL DEPLOYED ABACDA DEPENDENCY — the review engine is the DEPLOYED hermes-master-live ``bin`` run under a
    separate py3.14 (``ABACDA_BIN``/``ABACDA_PYTHON``), because the Hermes venv (py3.11) cannot import it. If that
    deployment moves/breaks, review fails to complete and the answer fails CLOSED (BLOCKED), never released uncertified.
  * OPERATOR-LEVEL SENTINEL — ``HERMES_ABACDA_INTERNAL`` and ``_abacda_retrying`` are operator/process-level recursion
    brakes, NOT a defense against a model deliberately spoofing them; the model does not control this process's env or
    the agent's private attributes, so this is adequate for the recursion concern but is not an anti-adversary control.
  * COOPERATIVE DEADLINE — the corrective-turn bound is cooperative interruption (see ``_interrupt_at_deadline``), not a
    hard wall-clock kill; a turn wedged in one uninterruptible blocking call can overrun the answer-level deadline.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
import time
from pathlib import Path

_HERMES_ROOT = Path(__file__).resolve().parents[1]
_ENGINE = _HERMES_ROOT / "bin" / "review_response.py"
_DEPLOYED_BIN = os.environ.get("ABACDA_BIN") or "/Users/spinec/hermes-workforce-worktrees/hermes-master-live/bin"
_PYTHON = os.environ.get("ABACDA_PYTHON") or "/opt/homebrew/bin/python3"   # engine needs py3.14, not the Hermes venv
_SENTINEL = "HERMES_ABACDA_INTERNAL"
_RETRY_FLAG = "_abacda_retrying"
_RECORDS_BASE = Path(os.environ.get("HERMES_ABACDA_RECORDS") or (Path.home() / ".hermes" / "abacda_records"))
# ONE answer-level review budget (Codex-B 2026-07-22 #3): the initial review, correction, and re-review SHARE this
# monotonic deadline — not per-review 460+120s each. Below _MIN_REVIEW_S remaining, a review can't run -> fail closed.
_ANSWER_DEADLINE_S = int(os.environ.get("HERMES_ABACDA_DEADLINE_S", "480"))
_MIN_REVIEW_S = 30


def _record_dir() -> str:
    # fix 7: a durable record dir under ~/.hermes, NOT disposable /tmp-only state. (Raw material is NOT staged here —
    # it is streamed to the engine over stdin; the engine only ever writes the SANITIZED subject.)
    _RECORDS_BASE.mkdir(parents=True, exist_ok=True)
    return tempfile.mkdtemp(prefix=f"resp-{int(time.time())}-", dir=str(_RECORDS_BASE))


def review(question: str, answer: str, *, runner=None, timeout_s: int = 460) -> dict:
    """Call the shared review engine on (question, answer) — material passed over STDIN so raw bytes never hit disk.
    Returns the engine dict (tier/clean/findings/coverage/record_dir/...). On any operational failure returns
    {reviewed:False, engine_error:...}; the caller fails closed."""
    if runner is not None:
        return runner(question, answer)
    rec = _record_dir()
    env = dict(os.environ)
    env[_SENTINEL] = "1"
    env["ABACDA_BIN"] = _DEPLOYED_BIN
    argv = [_PYTHON, str(_ENGINE), "--stdin", "--record-dir", rec, "--timeout", str(timeout_s)]
    try:
        # the engine CLI is hard-capped at EXACTLY the remaining budget it is given (Codex-B #3, no floor overshoot) — it
        # caps its own nested lanes below that, so no layer exceeds the answer-level deadline.
        cp = subprocess.run(argv, input=json.dumps({"question": question or "", "answer": answer or ""}),
                            capture_output=True, text=True, env=env, timeout=max(1, timeout_s))
        result = json.loads((cp.stdout or "").strip().splitlines()[-1])
        # FAIL CLOSED on a NONZERO engine exit (codex 2026-07-23): the engine exits 0 ONLY for a certified-clean review;
        # a nonzero exit with clean-looking JSON is a contradiction and must NEVER be released as clean. Force not-clean.
        if isinstance(result, dict) and cp.returncode != 0:
            result["clean"] = False
            result["clean_aggregate_permitted"] = False
            result.setdefault("findings", f"engine exited {cp.returncode} — not certified clean")
            result["engine_exit"] = cp.returncode
        return result
    except Exception as e:  # noqa: BLE001 — engine failure is a fail-closed signal, decided by the caller per tier
        return {"reviewed": False, "clean": False, "engine_error": f"{type(e).__name__}: {e}", "record_dir": rec}


# ------------------------------------------------------------------- stream buffering (fix 4): hold the draft
def install_answer_buffer(agent):
    """Hold FINAL-ANSWER stream deltas so the un-reviewed draft never reaches the user while review runs. Swaps ONLY the
    final-text delta sinks (stream_delta_callback / _stream_callback) for a buffer; tool/status output uses a different
    path and keeps flowing. Returns a restore token, or None when there is no live consumer / this is a nested corrective
    turn. If the swap itself FAILS on a live consumer it FAILS CLOSED (nulls the sinks so no draft leaks) and still
    returns a token so flush restores + emits once."""
    if getattr(agent, _RETRY_FLAG, False) or os.environ.get(_SENTINEL):
        return None
    saved = (getattr(agent, "stream_delta_callback", None), getattr(agent, "_stream_callback", None))
    if saved[0] is None and saved[1] is None:
        return None                                                  # oneshot/cron: nothing streams -> nothing to hold
    buf: list = []

    def _sink(text):
        if text:
            buf.append(text)
    # Swap each live sink INDEPENDENTLY; a partial failure must never leave a LIVE un-buffered sink.
    swapped_ok = True
    for _attr in ("stream_delta_callback", "_stream_callback"):
        if getattr(agent, _attr, None) is None:
            continue
        try:
            setattr(agent, _attr, _sink)
        except Exception:  # noqa: BLE001
            swapped_ok = False
    if swapped_ok:
        return {"saved": saved, "buffer": buf}
    # a swap failed -> force-SUPPRESS every sink we can (independently) and mark DEGRADED so the caller fails closed
    # (withholds) rather than trust a possibly-live sink.
    for _attr in ("stream_delta_callback", "_stream_callback"):
        try:
            setattr(agent, _attr, None)
        except Exception:  # noqa: BLE001
            pass
    return {"saved": saved, "buffer": buf, "degraded": True}


def flush_answer(agent, token, final_text: str) -> bool:
    """Restore the real delta sinks FIRST, then emit the FINAL answer ONCE per DISTINCT sink (dedup by identity so a
    consumer reachable via both callbacks is not double-delivered). Returns True iff restore + every emit succeeded, so
    the caller can surface a failure (a silent restore/emit loss must be observable)."""
    if not token:
        return True
    ok = True
    try:
        agent.stream_delta_callback, agent._stream_callback = token["saved"]   # restore FIRST
    except Exception:  # noqa: BLE001
        ok = False
    seen = set()
    for cb in token.get("saved", ()):
        if cb is None:
            continue
        # dedup by (receiver, function) so two DISTINCT bound-method objects for the same consumer.method are emitted
        # once; fall back to identity for plain functions/partials.
        key = (id(getattr(cb, "__self__", cb)), getattr(cb, "__func__", None) or id(cb))
        if key in seen:
            continue
        seen.add(key)
        if final_text:
            try:
                cb(final_text)
            except Exception:  # noqa: BLE001
                ok = False
    return ok


# ------------------------------------------------------------------- pre-delivery orchestration
def feedback_message(findings: str) -> str:
    return ("[AUTO-ABACDA REVIEW OF YOUR PROPOSED ANSWER — independent arms, before delivery]\n\n"
            f"{(findings or '').strip()[:6000]}\n\n"
            "Consume these findings semantically: fix the ones that are genuinely correct, and explicitly reject any that "
            "are wrong (say why). Then give your single corrected final answer. Do not mention this review.")


def message_text(content) -> str:
    """Concatenate the TEXT of a message's content whether it is a plain string OR a list of content blocks (dicts with
    a 'text'/'content' field, or bare strings), so a draft SPLIT ACROSS blocks (e.g. [{'text':'echo: '},{'text':'do it'}])
    is matched as one contiguous string (codex/gemini 2026-07-23 — `str(content)` failed because block-list repr syntax
    separates the chunks)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, str):
                parts.append(b)
            elif isinstance(b, dict):
                t = b.get("text")
                if not isinstance(t, str):
                    t = b.get("content")
                if isinstance(t, str):
                    parts.append(t)
        return "".join(parts)
    return ""


def _without_draft_and_feedback(msgs, draft, feedback):
    """Return `msgs` with the UNCERTIFIED draft assistant message(s) and the internal review-feedback user message
    removed, so a rejected/uncertified draft never persists in returned/persisted messages, the external-memory sync, or
    the background-review snapshot (Codex-B 2026-07-22 #1). `draft` is a single string OR an iterable of strings — on a
    non-clean post-correction path BOTH the original draft AND the corrected draft are passed so neither survives
    (Codex-B 2026-07-22 #2). Matches on the EXTRACTED text of the content (handles structured/segmented assistant
    content — codex/gemini 2026-07-23), by EXACT equality so a kept corrected answer is never over-stripped."""
    drafts = {draft} if isinstance(draft, str) else {d for d in (draft or ()) if isinstance(d, str)}
    drafts.discard("")
    out = []
    for m in (msgs or []):
        c = m.get("content") if isinstance(m, dict) else None
        role = m.get("role") if isinstance(m, dict) else None
        if role == "assistant" and message_text(c) in drafts:
            continue
        if role == "user" and message_text(c) == feedback and feedback:
            continue
        out.append(m)
    return out


# The corrective turn must be PRIVATE (Codex-B 2026-07-22 #1): while it runs, the nested run_conversation must NOT
# persist, write a trajectory, sync external memory, or fork a background review — only the in-memory candidate returns
# to the outer turn, which persists and delivers ONCE after a clean re-review. These agent methods are the side-effect
# surface; they are temporarily replaced with no-ops around the nested call and restored immediately after.
_SIDE_EFFECT_METHODS = (
    "_persist_session", "_save_trajectory", "_sync_external_memory_for_turn", "_spawn_background_review",
)


class _NoopSink:
    """Any attribute access returns a no-op callable — a safe stand-in for _session_db so the private turn's token/
    message persistence calls resolve to no-ops instead of raising on a None."""
    def __getattr__(self, _):
        return lambda *a, **k: None


def _suppress_side_effects(agent):
    """Replace the agent's persistence/trajectory/sync/background-review surface (and its session DB) with no-ops for the
    duration of the private corrective turn. Returns (saved, ok): `saved` is the restore token; `ok` is False if ANY
    present side-effect method could not be replaced. FAIL CLOSED (codex/gemini 2026-07-23 #3): a caller that gets ok=False
    must ABORT the corrective turn (an unsuppressed persist/sync would leak the uncertified corrective draft), because the
    main loop's persistence/trajectory calls are NOT individually guarded by _abacda_retrying — the no-op swap is the
    guarantee. Never raises."""
    saved = {}
    ok = True
    _noop = lambda *a, **k: None                                     # noqa: E731
    for m in _SIDE_EFFECT_METHODS:
        if hasattr(agent, m):
            saved[m] = getattr(agent, m)
            try:
                setattr(agent, m, _noop)
                if getattr(agent, m, None) is not _noop:            # verify the swap actually took (no property shadow)
                    ok = False
            except Exception:  # noqa: BLE001 — could not neutralize a live side-effect surface -> fail closed
                saved.pop(m, None)
                ok = False
    for attr in ("_session_db", "session_db"):
        if getattr(agent, attr, None) is not None:
            saved[attr] = getattr(agent, attr)
            try:
                _sink = _NoopSink()
                setattr(agent, attr, _sink)
                if getattr(agent, attr, None) is not _sink:
                    ok = False
            except Exception:  # noqa: BLE001
                saved.pop(attr, None)
                ok = False
    return saved, ok


def _restore_side_effects(agent, saved):
    """Restore every method/attr `_suppress_side_effects` replaced. Never raises."""
    for k, v in (saved or {}).items():
        try:
            setattr(agent, k, v)
        except Exception:  # noqa: BLE001
            pass


def _blocked(reason: str, answer: str = "", *, record_dir: str = "") -> str:
    """A BLOCKED outcome WITHHOLDS the uncertified answer body — echoing it would release exactly the content review
    refused to certify (and would leak an unsanitizable sensitive answer to the user). Report only the reason, the
    withheld length, and the review-record path (whose subject is sanitized)."""
    ref = f"\nReview record (sanitized): {record_dir}" if record_dir else ""
    return ("[AUTO-ABACDA: BLOCKED — the proposed answer did not pass independent review and is WITHHELD, not presented "
            "as done.]\n"
            f"Reason: {reason}\n"
            f"Withheld answer length: {len(answer or '')} chars.{ref}\n"
            "The uncertified answer is not shown; adjust the request or re-ask.")


def _certified_clean(rev) -> bool:
    """A REVIEWED answer is releasable ONLY when the canonical record's clean_aggregate_permitted is EXACTLY True AND the
    engine's derived clean flag is True (codex/grok 2026-07-23). `clean` alone is a derived boolean a stale or mocked
    engine could set without the canonical gate; requiring clean_aggregate_permitted binds the delivery boundary to the
    record so a review that did not actually produce an all-APPROVE gate can never be released as done."""
    return isinstance(rev, dict) and rev.get("clean") is True and rev.get("clean_aggregate_permitted") is True


def _blocked_msgs(base_msgs, drafts, feedback, banner):
    """Strip the uncertified draft(s) + internal feedback, THEN append the BLOCKED notice as the assistant turn so the
    persisted conversation carries the notice — not the draft, and not an empty/answer-less turn (codex 2026-07-23 #4)."""
    out = _without_draft_and_feedback(base_msgs, drafts, feedback)
    out.append({"role": "assistant", "content": banner})
    return out


def maybe_review_and_retry(agent, question: str, answer: str, messages: list):
    """Pre-delivery orchestration; NEVER raises. Returns (final_answer, messages, review_dict).

    review -> clean? deliver silently | trivia? deliver silently | engine error on a reviewed answer? BLOCKED |
    findings? ONE corrective turn -> RE-REVIEW once -> deliver corrected ONLY on an explicit clean re-review, else BLOCKED.
    A corrective turn / re-review that errors fails CLOSED to BLOCKED — the original is never released as done."""
    if not (answer or "").strip() or getattr(agent, _RETRY_FLAG, False) or os.environ.get(_SENTINEL):
        return answer, messages, None

    _start = time.monotonic()
    _deadline = _start + _ANSWER_DEADLINE_S

    def _remaining():
        return int(_deadline - time.monotonic())

    def _stamp(d):                                                   # record actual elapsed on every outcome (Codex #3)
        if isinstance(d, dict):
            d["review_elapsed_s"] = round(time.monotonic() - _start, 1)
            d["review_budget_s"] = _ANSWER_DEADLINE_S
        return d

    # No floor OVERSHOOT (Codex-B #3): review only with the EXACT remaining budget; if it's below the minimum, fail
    # closed rather than run for a floor that exceeds the deadline.
    if _remaining() < _MIN_REVIEW_S:
        _b = _blocked("no review budget for this answer (deadline too small)", answer)
        return _b, _blocked_msgs(messages, answer, None, _b), None
    rev = _stamp(review(question, answer, timeout_s=_remaining()))
    # A NON-reviewed result may deliver as-is ONLY on an EXPLICIT below-Duo disposition: tier T0/T1 AND clean EXACTLY True
    # (codex 2026-07-23 #2 — the old `clean is not False` treated a MISSING clean as permission; require an explicit
    # True). Absence of an error is NOT certification: any malformed/unknown result fails CLOSED (Codex-B round 3 #3).
    if not rev.get("reviewed"):
        if rev.get("engine_error"):
            _b = _blocked(f"review engine did not complete: {rev['engine_error']}", answer, record_dir=rev.get("record_dir", ""))
            return _b, _blocked_msgs(messages, answer, None, _b), rev
        if str(rev.get("tier", "")).upper() in ("T0", "T1") and rev.get("clean") is True:
            return answer, messages, rev                            # genuine below-Duo trivia -> silent, keep as-is
        _b = _blocked("review returned an unrecognized disposition (not certified below-Duo)", answer, record_dir=rev.get("record_dir", ""))
        return _b, _blocked_msgs(messages, answer, None, _b), rev
    if _certified_clean(rev):
        return answer, messages, rev                                # arms all APPROVE -> silent deliver

    # SEMANTIC FAN-IN: the arms did not cleanly approve. The engine ran a Codex-B second pass that ADJUDICATED all findings
    # into ONE decision (in rev["adjudication"]). CLEAN -> the adjudicator cleared the finding(s), deliver. HOLD -> withhold.
    # CORRECT -> ONE bounded correction, not the raw pile. (No adjudication present -> fall back to the findings pile.)
    _adj = rev.get("adjudication") or {}
    _adj_dec = str(_adj.get("decision") or "").upper()
    if _adj_dec == "CLEAN":
        return answer, messages, _stamp(rev)                        # decision-backed CLEAN clears the finding -> deliver
    if _adj_dec == "HOLD":
        _b = _blocked("Codex-B adjudicated HOLD: "
                      + (_adj.get("action_prompt") or _adj.get("instruction") or "cannot certify"),
                      answer, record_dir=rev.get("record_dir", ""))
        return _b, _blocked_msgs(messages, answer, None, _b), _stamp(rev)
    # CORRECT -> feed only the ONE action prompt to the corrective turn; else (no adjudication) the findings pile.
    fb = feedback_message((_adj.get("action_prompt") or "") if _adj_dec == "CORRECT" else (rev.get("findings") or ""))
    # State that MUST be cleaned up no matter how the corrective section exits (codex 2026-07-23 C4): if
    # _suppress_side_effects / threading.Timer / any setup step raises AFTER the retry flag is set, the outer `finally`
    # still restores the side-effect surface and clears the flag — otherwise a setup exception would leave the agent
    # permanently retry-flagged (bypassing review) with no-op'd persistence.
    _saved_side_effects = None
    _flag_set = False
    _timer = None
    try:
        # Bound the corrective turn by the SAME remaining monotonic deadline via COOPERATIVE interruption (Codex-B #2/#5,
        # NOT a hard wall-clock kill): a timer trips agent.interrupt() at the deadline, which sets _interrupt_requested;
        # run_conversation observes that flag at its next checkpoint and returns. A turn wedged in a single uninterruptible
        # blocking call can therefore overrun the deadline — that residual is accepted (there is no safe hard-kill of an
        # in-process turn).
        _budget = _remaining()
        _timer = threading.Timer(max(1, _budget), _interrupt_at_deadline, args=(agent,))
        _timer.daemon = True
        setattr(agent, _RETRY_FLAG, True)                           # guard the corrective turn from re-review
        _flag_set = True
        _saved_side_effects, _suppress_ok = _suppress_side_effects(agent)   # (Codex-B #1) private turn: no persist/sync/bg
        if not _suppress_ok:
            # FAIL CLOSED (codex/gemini 2026-07-23 #3): a side-effect surface could not be neutralized, so a corrective
            # turn would risk leaking its uncertified draft through an unsuppressed persist/sync. Do NOT run it — the
            # outer `finally` restores + clears; here just BLOCK (withhold).
            _b = _blocked("private corrective turn could not be isolated (side-effect suppression failed)", answer,
                          record_dir=rev.get("record_dir", ""))
            return _b, _blocked_msgs(messages, answer, fb, _b), rev
        _timer.start()
        nested = agent.run_conversation(fb, conversation_history=list(messages))
        corrected = ((nested or {}).get("final_response") or "").strip()
        base_msgs = (nested or {}).get("messages") or messages
        # CLEAN path keeps the corrected draft (it is delivered); every NON-clean path strips BOTH drafts (Codex-B #2) AND
        # appends the BLOCKED notice so the persisted conversation carries the notice, not an answer-less turn (#4).
        clean_msgs = _without_draft_and_feedback(base_msgs, answer, fb)
        if (nested or {}).get("interrupted") or not corrected:      # deadline-interrupted or empty -> fail closed
            _b = _blocked("the corrective turn produced no answer within the review deadline", answer,
                          record_dir=rev.get("record_dir", ""))
            return _b, _blocked_msgs(base_msgs, [answer, corrected], fb, _b), rev

        if _remaining() < _MIN_REVIEW_S:                            # shared deadline exhausted before the re-review
            _b = _blocked(f"answer-level review deadline ({_ANSWER_DEADLINE_S}s) exhausted before re-review "
                          f"(elapsed {round(time.monotonic() - _start, 1)}s)", corrected, record_dir=rev.get("record_dir", ""))
            return _b, _blocked_msgs(base_msgs, [answer, corrected], fb, _b), _stamp(rev)
        rev2 = _stamp(review(question, corrected, timeout_s=_remaining()))   # re-review shares the SAME deadline
        # FINAL certification boundary: release ONLY on a CANONICALLY clean re-review (clean_aggregate_permitted True);
        # every other/unknown state closes (codex/grok 2026-07-23 #1).
        if rev2.get("reviewed") is True and _certified_clean(rev2):
            return corrected, clean_msgs, rev2
        reason = ("the corrected answer did not pass a clean re-review"
                  + (f": {rev2.get('engine_error')}" if rev2.get("engine_error") else " (findings persist or review incomplete)"))
        _b = _blocked(reason, corrected, record_dir=rev2.get("record_dir", ""))
        return _b, _blocked_msgs(base_msgs, [answer, corrected], fb, _b), rev2
    except Exception as e:  # noqa: BLE001 — a corrective/re-review failure fails CLOSED, never releases the original
        _b = _blocked(f"review pipeline error ({type(e).__name__})", answer, record_dir=rev.get("record_dir", ""))
        return _b, _blocked_msgs(messages, answer, fb, _b), rev
    finally:
        # ALWAYS restore the agent (codex 2026-07-23 C4): cancel the timer, restore the side-effect surface, clear the
        # retry flag, clear the interrupt — on EVERY exit (success, early BLOCK return, or a setup exception before the
        # inner run). Otherwise a setup exception would leave the agent retry-flagged with no-op'd persistence forever.
        if _timer is not None:
            try:
                _timer.cancel()
            except Exception:  # noqa: BLE001
                pass
        _restore_side_effects(agent, _saved_side_effects)
        if _flag_set:
            try:
                setattr(agent, _RETRY_FLAG, False)
            except Exception:  # noqa: BLE001
                pass
        try:
            agent.clear_interrupt()
        except Exception:  # noqa: BLE001
            pass


def _interrupt_at_deadline(agent):
    # COOPERATIVE cancellation (Codex-B #5): request an interrupt the turn observes at its next checkpoint; this is NOT a
    # hard wall-clock kill. If the turn is parked in one uninterruptible blocking call it may overrun — accepted residual.
    try:
        agent.interrupt("[AUTO-ABACDA] answer-level review deadline reached")
    except Exception:  # noqa: BLE001 — a timer failure must never crash the review
        pass
