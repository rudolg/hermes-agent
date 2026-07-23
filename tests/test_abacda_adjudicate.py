"""Unit tests for the semantic adjudication stage (bin/abacda_adjudicate.py).

The Codex-B adjudicator is injected as `run_codex(prompt, timeout_s) -> (verdict, reason, detail)`, so these prove the
contract WITHOUT a live model: the three decisions parse, and every failure mode fails CLOSED to HOLD (never a silent
CLEAN). Run under the deployed python (3.14) or any py3.11 — the module is stdlib-only."""
import sys
from pathlib import Path

_WT = Path(__file__).resolve().parents[1]
if str(_WT / "bin") not in sys.path:
    sys.path.insert(0, str(_WT / "bin"))

import abacda_adjudicate as AJ  # noqa: E402


def _reviews(codex="BLOCK", grok="FLAG", gemini="APPROVE", local="APPROVE"):
    return {
        "codex": {"verdict": codex, "reason": "codex: the retry loop is unbounded — a wedged host spins forever."},
        "grok": {"verdict": grok, "reason": "grok: possible unbounded retry; how could this look safe but hang?"},
        "gemini": {"verdict": gemini, "reason": "gemini: structure looks fine."},
        "local": {"verdict": local, "reason": "local: subject well-formed."},
    }


def _fake_codex(reply, verdict="FLAG"):
    def run(task, subject, timeout_s):
        return verdict, reply, {"task_len": len(task), "subject_len": len(subject)}
    return run


def test_clean_decision_delivers():
    # CLEAN is allowed when no independent lane holds BLOCK (here codex/grok/gemini are FLAG/APPROVE).
    reply = ("REJECT unbounded-retry — the loop already has a max-attempts cap at line 12; the finding is a false "
             "positive.\nSHARED FACTS: all reviewed the same tree.\nDISAGREEMENTS: none material.\n"
             "STRONGEST SURVIVING BLOCK/FLAG: none survives.\nEVIDENCE NEEDED: none.\nONE RECOMMENDATION: deliver.\n"
             "ADJUDICATION: CLEAN")
    r = AJ.adjudicate("the candidate answer", _reviews(codex="FLAG", grok="APPROVE", gemini="APPROVE"),
                      run_codex=_fake_codex(reply))
    assert r["decision"] == "CLEAN" and r["clean"] is True and r["action_prompt"] == ""


def test_decision_backed_clean_clears_independent_block():
    # Greg 2026-07-23: a DECISION-BACKED Codex-B CLEAN is the authority — it may reject and CLEAR a false independent
    # BLOCK (no owner flag, no sticky-block downgrade). This is the same behavior in production and replay.
    reply = ("REJECT the mutation finding — the material shows a copy is made; the caller's list is never mutated.\n"
             "ADJUDICATION: CLEAN")
    r = AJ.adjudicate("cand", _reviews(codex="BLOCK", gemini="BLOCK"), run_codex=_fake_codex(reply))
    assert r["decision"] == "CLEAN" and r["clean"] is True and r["action_prompt"] == ""


def test_correct_multiline_fix_block_not_truncated():
    # gemini 2026-07-23: a multiline CORRECT fix (code/diff) must NOT be truncated to the first line — the <fix> block
    # carries the whole instruction.
    reply = ("ACCEPT the unbounded retry — real.\n"
             "ADJUDICATION: CORRECT\n"
             "<fix>\n"
             "Cap the retry in fetch():\n"
             "    for attempt in range(5):\n"
             "        try:\n"
             "            return do()\n"
             "        except Transient:\n"
             "            time.sleep(backoff(attempt))\n"
             "    raise\n"
             "</fix>")
    r = AJ.adjudicate("cand", _reviews(codex="FLAG", grok="FLAG", gemini="APPROVE"), run_codex=_fake_codex(reply))
    assert r["decision"] == "CORRECT"
    assert "for attempt in range(5)" in r["action_prompt"]           # the multiline body survived
    assert "time.sleep(backoff(attempt))" in r["action_prompt"]      # ...in full, not truncated to the first line


def test_correct_decision_carries_one_instruction():
    reply = ("ACCEPT unbounded-retry — real: fetch() retries with no cap, reachable on a 500 loop.\n"
             "ONE RECOMMENDATION: cap it.\n"
             "ADJUDICATION: CORRECT :: Add a bounded max-retry (e.g. 5) with backoff to fetch(); then re-verify.")
    r = AJ.adjudicate("the candidate", _reviews(), run_codex=_fake_codex(reply))
    assert r["decision"] == "CORRECT" and r["clean"] is False
    assert "max-retry" in r["action_prompt"] and "::" not in r["action_prompt"]


def test_hold_decision():
    reply = "ADJUDICATION: HOLD :: two independent corrections are required and one needs owner push authority."
    r = AJ.adjudicate("the candidate", _reviews(), run_codex=_fake_codex(reply))
    assert r["decision"] == "HOLD" and r["clean"] is False and "owner push" in r["action_prompt"]


def test_no_run_codex_fails_closed():
    r = AJ.adjudicate("x", _reviews(), run_codex=None)
    assert r["decision"] == "HOLD" and r["clean"] is False


def test_adjudicator_unavailable_fails_closed():
    r = AJ.adjudicate("x", _reviews(), run_codex=_fake_codex("ADJUDICATION: CLEAN", verdict="UNAVAILABLE"))
    assert r["decision"] == "HOLD" and r["clean"] is False   # a CLEAN reply from an UNAVAILABLE adjudicator is void


def test_adjudicator_exception_fails_closed():
    def boom(task, subject, timeout_s):
        raise RuntimeError("broker down")
    r = AJ.adjudicate("x", _reviews(), run_codex=boom)
    assert r["decision"] == "HOLD" and r["clean"] is False


def test_unparseable_reply_fails_closed():
    r = AJ.adjudicate("x", _reviews(), run_codex=_fake_codex("I think it is probably fine, ship it."))
    assert r["decision"] == "HOLD" and r["clean"] is False and r["parsed"] is False


def test_clean_contradicted_by_accepted_block_is_held():
    # a contradictory reply: CLEAN action but an ACCEPT ... BLOCK line -> fail closed to HOLD
    reply = ("ACCEPT the credential-leak BLOCK — real: the token is logged in plaintext.\n"
             "ADJUDICATION: CLEAN")
    r = AJ.adjudicate("x", _reviews(), run_codex=_fake_codex(reply))
    assert r["decision"] == "HOLD" and r["clean"] is False


def test_correct_without_instruction_degrades_to_hold():
    r = AJ.adjudicate("x", _reviews(), run_codex=_fake_codex("ADJUDICATION: CORRECT ::   "))
    assert r["decision"] == "HOLD" and r["clean"] is False


def test_last_action_line_wins():
    # the adjudicator may reason with the word ADJUDICATION mid-text; only the LAST valid action line binds
    reply = ("Considering ADJUDICATION: CORRECT as an option...\nOn reflection:\nADJUDICATION: HOLD :: needs owner.")
    r = AJ.adjudicate("x", _reviews(), run_codex=_fake_codex(reply))
    assert r["decision"] == "HOLD"


def test_prompt_includes_reviews_scope_and_standing_rule():
    seen = {}

    def capture(task, subject, timeout_s):
        seen["task"], seen["subject"] = task, subject
        return "FLAG", "ADJUDICATION: CLEAN", {}
    AJ.adjudicate("SUBJECT_BODY_XYZ", _reviews(), scope="governance gate change",
                  standing_prompt="MY STANDING RULE ABC", run_codex=capture)
    task, subject = seen["task"], seen["subject"]
    assert subject == "SUBJECT_BODY_XYZ"                                 # the frozen subject is passed as MATERIAL, separately
    assert "governance gate change" in task and "MY STANDING RULE ABC" in task
    assert "[grok]" in task and "[gemini]" in task and "YOUR ORIGINAL" in task   # its own verdict + peers, semantic fan-in


def test_hold_without_reason_gets_a_concise_reason():
    # gemini 2026-07-23: a bare `ADJUDICATION: HOLD` (no reason) must still surface ONE concise reason, never an empty block.
    r = AJ.adjudicate("x", _reviews(), run_codex=_fake_codex("ADJUDICATION: HOLD"))
    assert r["decision"] == "HOLD" and (r["action_prompt"] or "").strip()


def test_replay_prompts_are_blind():
    # codex 2026-07-23: the material SENT to Codex-B must never leak a per-case answer, verdict label, or characterization.
    import abacda_adjudicate_replay as R
    leaks = ["false positive", "named residual", "real reachable", "verified false", "expected decision",
             "manual decision", "copy, not an alias", "removed by owner", "intentional"]
    for c in R.CORPUS:
        task = AJ.build_adjudicator_task(c["reviews"], scope=c["id"], standing_prompt=R.STANDING)
        sent = (task + "\n" + c["subject"]).lower()
        for bad in leaks:
            assert bad not in sent, f"[{c['id']}] leak: {bad!r}"
        assert c["name"].lower() not in sent and c["id"] in task
        assert c["manual_decision"].lower() not in c["subject"].lower()


def test_clean_over_incomplete_coverage_is_held():
    # Greg 2026-07-23: a decision-backed CLEAN must NOT certify incomplete required coverage.
    r = AJ.adjudicate("x", _reviews(codex="UNAVAILABLE"),
                      run_codex=_fake_codex("REJECT — false alarm.\nADJUDICATION: CLEAN"),
                      coverage_incomplete_reason="codex=UNAVAILABLE")
    assert r["decision"] == "HOLD" and "coverage incomplete" in r["action_prompt"].lower()


def test_complete_coverage_false_block_still_cleans():
    # ...but with COMPLETE coverage (no incomplete reason), a decision-backed CLEAN over a false BLOCK stands.
    r = AJ.adjudicate("x", _reviews(codex="BLOCK", grok="APPROVE", gemini="APPROVE"),
                      run_codex=_fake_codex("REJECT — does not hold.\nADJUDICATION: CLEAN"), coverage_incomplete_reason="")
    assert r["decision"] == "CLEAN"
