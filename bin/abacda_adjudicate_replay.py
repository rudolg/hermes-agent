#!/usr/bin/env python3
"""Replay harness for the semantic adjudication stage — the BINDING proof (docs ABACDA_RESPONSE_ORCHESTRATOR.md:29).

BLIND replay: a FROZEN set of historical ABACDA review cases (real arm verdicts+reasons + a FACTUAL code excerpt) runs
through the adjudicator under the EXACT production configuration, and an ORACLE checks it reproduces the manual decision
with NO decision-changing miss and ZERO courier work. The prompt sent to Codex-B carries ONLY a neutral case id, the
factual code, and the real reviewer text — never the expected decision, the case's characterization, or verdict labels.
The `manual_decision` + `anchors` are oracle-only (never sent).

Usage:
  abacda_adjudicate_replay.py            # DRY structural run (deterministic fake adjudicator; proves the harness + oracle)
  abacda_adjudicate_replay.py --live     # LIVE run: the real Codex-B adjudicator via the production broker transport
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_WT = Path(__file__).resolve().parents[1]
if str(_WT / "bin") not in sys.path:
    sys.path.insert(0, str(_WT / "bin"))

import abacda_adjudicate as AJ  # noqa: E402

STANDING = AJ.DEFAULT_STANDING_RULE               # IDENTICAL policy in production and replay

# ---- FROZEN historical corpus. `subject`/`reviews`/`scope-id` are SENT; `manual_decision`/`anchors`/`name` are ORACLE-ONLY.
CORPUS = [
    {
        "id": "case-01",
        "name": "return-code (correctness)",
        "manual_decision": "CORRECT",
        "anchors": ["returncode", "return code", "nonzero", "exit"],
        "subject": (
            "agent/abacda_review.py — review() (the caller), current state:\n"
            "    cp = subprocess.run(argv, input=..., capture_output=True, text=True, env=env, timeout=max(1, timeout_s))\n"
            "    return json.loads((cp.stdout or '').strip().splitlines()[-1])\n"
            "cp.returncode is never inspected; review() returns whatever the LAST stdout line parses to.\n\n"
            "The invoked engine (bin/review_response.py, run as a subprocess) exits NONZERO as its authoritative "
            "not-clean signal: exit 2 for a BLOCK/non-clean review, exit 3 for an incomplete/degraded review. Its stdout "
            "is not guaranteed to end with a well-formed non-clean record on those paths (a partial write, a trailing "
            "warning line, or an earlier clean-looking line can be the last line). So a nonzero exit can co-occur with a "
            "last stdout line that parses as clean, and review() would return it.\n"),
        "reviews": {
            "codex": {"verdict": "BLOCK", "reason": "review() parses the stdout JSON and returns it even when "
                      "cp.returncode != 0. A subprocess that exits nonzero while emitting {\"clean\":true} is returned "
                      "as certified clean. Require returncode == 0 for any clean disposition."},
            "grok": {"verdict": "FLAG", "reason": "how could a nonzero exit still look clean? the JSON is trusted alone."},
            "gemini": {"verdict": "APPROVE", "reason": "fallbacks look robust."},
            "local": {"verdict": "APPROVE", "reason": "subject well-formed."},
        },
    },
    {
        "id": "case-02",
        "name": "messages handling (privacy)",
        "manual_decision": "CLEAN",
        "anchors": [],
        "subject": (
            "agent/conversation_loop.py — run_conversation():\n"
            "    messages = list(conversation_history) if conversation_history else []\n"
            "    ...\n"
            "    final_msg = agent._build_assistant_message(assistant_message, finish_reason)\n"
            "    messages.append(final_msg)\n"
            "    ...\n"
            "    final_response, messages, _abacda_rev = maybe_review_and_retry(agent, user_message, final_response, messages)\n"
            "    ...\n"
            "    return {'final_response': final_response, 'messages': messages, ...}\n"),
        "reviews": {
            "codex": {"verdict": "UNAVAILABLE", "reason": "no decision-backed verdict (infra returncode=1)."},
            "grok": {"verdict": "DEGRADED", "reason": "exit 124 max-turns."},
            "gemini": {"verdict": "BLOCK", "reason": "In-place mutation failure: the standard loop rebinds the local "
                       "`messages` variable instead of mutating in place; a caller holding a reference to the original "
                       "conversation_history list retains the unmodified list containing the uncertified draft."},
            "local": {"verdict": "APPROVE", "reason": "subject well-formed."},
        },
    },
    {
        "id": "case-03",
        "name": "deployed dependency (scope)",
        "manual_decision": "CLEAN",
        "anchors": [],
        "subject": (
            "bin/review_response.py imports the deployed sanitizer/tier modules and shells the deployed runner:\n"
            "    import abacda_auto as _AH          # resolved from $ABACDA_BIN/../hooks/claude\n"
            "    import auto_abacda_gate as _GATE   # resolved from $ABACDA_BIN\n"
            "    # _subprocess_answer(...) runs $ABACDA_BIN/abacda_answer.py under the deployed python\n"
            "No dependency-hash manifest is present in the reviewed tree; abacda_auto / auto_abacda_gate / abacda_answer "
            "are imported/executed from the deployed ABACDA_BIN location.\n"),
        "reviews": {
            "codex": {"verdict": "BLOCK", "reason": "The trusted-manifest/TOCTOU pin is a release blocker: the deployed "
                      "deps are not pinned against a trusted expected-hash manifest, and abacda_answer.py is not proven "
                      "to execute the bytes that were hashed. Add a manifest pin + per-decision cryptographic audit."},
            "grok": {"verdict": "DEGRADED", "reason": "exit 124."},
            "gemini": {"verdict": "APPROVE", "reason": "delivery fixes correct."},
            "local": {"verdict": "APPROVE", "reason": "subject well-formed."},
        },
    },
    {
        "id": "case-04",
        "name": "runtime side-effect ordering (privacy)",
        "manual_decision": "CORRECT",
        "anchors": ["external memory", "_sync_external_memory", "background review", "before review"],
        "subject": (
            "agent/codex_runtime.py — the statement ORDER for a codex-runtime turn:\n"
            "    messages.extend(turn.projected_messages)\n"
            "    ...\n"
            "    agent._sync_external_memory_for_turn(original_user_message=..., final_response=turn.final_text, interrupted=False)\n"
            "    ...\n"
            "    agent._spawn_background_review(messages_snapshot=list(messages), review_memory=..., review_skills=...)\n"
            "The Auto-ABACDA pre-delivery review of the answer is invoked AFTER these three statements.\n"),
        "reviews": {
            "codex": {"verdict": "BLOCK", "reason": "The raw un-reviewed draft flows to _sync_external_memory_for_turn "
                      "and _spawn_background_review before the pre-delivery review; move the review before both side "
                      "effects and strip the draft from messages."},
            "grok": {"verdict": "FLAG", "reason": "draft may persist to memory before review."},
            "gemini": {"verdict": "BLOCK", "reason": "uncertified draft leaks to external memory and returned messages."},
            "local": {"verdict": "APPROVE", "reason": "subject well-formed."},
        },
    },
]


def _fake_adjudicator(case):
    """A DETERMINISTIC stand-in emulating a competent adjudicator — for the dry run so the harness + oracle are proven
    without a live model. The LIVE run replaces this with the real Codex-B (AJ.broker_run_codex)."""
    md = case["manual_decision"]
    if md == "CLEAN":
        reply = "REJECT the cited finding — it does not hold against the material.\nADJUDICATION: CLEAN"
    else:
        anchor = (case["anchors"] or ["the defect"])[0]
        reply = f"ACCEPT {anchor} — it holds and is reachable.\nADJUDICATION: CORRECT\n<fix>\nFix {anchor}.\n</fix>"

    def run(task, subject, timeout_s):
        return "FLAG", reply, {}
    return run


def _oracle(case, result) -> tuple[bool, str]:
    """NO decision-changing MISS: manual CLEAN must stay CLEAN (no false-red); a manual CORRECT/HOLD must NOT become CLEAN
    (no false-green) and the action must name the finding's MECHANISM (an anchor token)."""
    md, dec = case["manual_decision"], result["decision"]
    if md == "CLEAN":
        return (dec == "CLEAN", f"manual CLEAN vs adjudicator {dec}")
    if dec == "CLEAN":
        return (False, f"FALSE-GREEN MISS: manual {md} but adjudicator CLEAN")
    anchors = [a.lower() for a in (case.get("anchors") or []) if a]
    text = (result.get("action_prompt", "") + " " + result.get("body", "")).lower()
    named = (not anchors) or any(a in text for a in anchors)
    return (named, f"manual {md}, adjudicator {dec}" + ("" if named else " but missed the finding mechanism"))


def run_replay(run_codex_factory, *, timeout_s=300) -> dict:
    """Run every case under the EXACT production configuration (no owner flag — a decision-backed CLEAN clears). The
    prompt to Codex carries ONLY the neutral case id as scope + the factual subject + the real reviews."""
    rows, misses = [], 0
    for case in CORPUS:
        rc = run_codex_factory(case)
        res = AJ.adjudicate(case["subject"], case["reviews"], scope=case["id"], standing_prompt=STANDING,
                            run_codex=rc, timeout_s=timeout_s)
        ok, note = _oracle(case, res)
        if not ok:
            misses += 1
        rows.append({"case": case["name"], "scope_sent": case["id"], "manual": case["manual_decision"],
                     "adjudicator": res["decision"], "match": ok, "note": note,
                     "action_prompt": (res.get("action_prompt", "") or "")[:160],
                     "run_id": (res.get("body", "") and _run_id_from(res))})
    return {"total": len(CORPUS), "decision_changing_misses": misses, "zero_miss": misses == 0, "rows": rows}


def _run_id_from(res) -> str:
    # best-effort: the GOV_STRUCTURED_VERDICT nonce carries the duo run_id in a live reply
    import re
    m = re.search(r"nonce=(duo-[0-9a-f]+)", res.get("body", "") or "")
    return m.group(1) if m else ""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="use the real Codex-B adjudicator (default: deterministic dry run)")
    ap.add_argument("--timeout", type=int, default=300)
    args = ap.parse_args(argv)
    factory = (lambda case: AJ.broker_run_codex) if args.live else _fake_adjudicator
    report = run_replay(factory, timeout_s=args.timeout)
    print(json.dumps(report, indent=2))
    print(f"\nREPLAY: {report['total']} cases · decision-changing misses = {report['decision_changing_misses']} · "
          f"{'PASS (zero-miss)' if report['zero_miss'] else 'FAIL'}")
    return 0 if report["zero_miss"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
