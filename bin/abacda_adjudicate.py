#!/usr/bin/env python3
"""abacda_adjudicate — the SEMANTIC ADJUDICATION stage of the ABACDA Response Orchestrator (the missing fan-in).

The concurrent arms (codex/grok/gemini/local) already review the same frozen subject and the existing `_synthesize`
MECHANICALLY aggregates their verdicts into a pile of truncated reasons + a rule-based gate. That pile is then handed
back to Claude, so Claude remains the adjudicator AND the courier. This module closes that gap: it runs **Codex-B a
SECOND time as the ADJUDICATOR** — given its own original verdict, the other independent reviews, the frozen subject,
the scope, and the owner's standing rule — and it emits EXACTLY ONE action:

    CLEAN                      -> deliver the candidate as-is (no accepted must-fix finding survives)
    CORRECT :: <instruction>   -> apply this ONE bounded correction, then verify once
    HOLD    :: <reason>        -> cannot be safely corrected in one turn / owner authority needed

It is a bounded STAR, not a debate: the adjudicator does semantic fan-in over the reviews and returns one action prompt.
No new queue, daemon, manifest, or crypto — it reuses the existing Codex lane (injected as `run_codex`).

FAIL CLOSED: the adjudicator can only CLEAR a finding by an EXPLICIT, parseable `ADJUDICATION: CLEAN` line from a
Codex-B pass that actually ran. A missing/unparseable action, an UNAVAILABLE adjudicator, or any surviving ACCEPTED BLOCK
that is not turned into a CORRECT/HOLD -> HOLD. The adjudicator is CODEX (independent of the builder), so its rejection
of a finding is an independent decision, not builder self-green-washing (North-Star-#1 independence preserved).
"""
from __future__ import annotations

import re
from typing import Callable, Optional

# The action verbs the adjudicator must end on. Parsed from the LAST matching line (case-insensitive verb).
_ACTION_RE = re.compile(r"^\s*ADJUDICATION:\s*(CLEAN|CORRECT|HOLD)\b\s*(?:::\s*(.*))?$", re.IGNORECASE)
# a bounded, MULTILINE fix payload (gemini 2026-07-23): a single trailing `:: <line>` truncates a fix that contains code
# or diffs, causing incomplete corrections + false-positive HOLD loops. `<fix>...</fix>` carries the whole instruction.
_FIX_RE = re.compile(r"<fix>\s*(.*?)\s*</fix>", re.IGNORECASE | re.DOTALL)

# The owner's DEFAULT standing rule (the adjudication policy) — IDENTICAL in production and replay. A caller may override
# via `standing_prompt`. It states the policy WITHOUT leaking any per-case answer (no verdict labels, no case hints).
DEFAULT_STANDING_RULE = (
    "A defect that genuinely affects delivery, privacy, or correctness — and is reachable in the material under review — "
    "must be fixed (CORRECT) or, if it cannot be fixed in one bounded step, held (HOLD). Demands for dependency pinning, "
    "per-decision cryptographic audit, fsync durability, split-review conservatism, or reviewer timeout/degradation are "
    "OUT OF SCOPE for a delivery/correctness review and are not grounds to block or to widen scope. Verify every cited "
    "finding against the material before accepting it; if a finding does not hold against the material, say so and do not "
    "act on it. Prefer the smallest change that removes the genuine risk; do not expand scope."
)

_ADJUDICATOR_CONTRACT = (
    "You are the ADJUDICATOR — Codex-B, running a SECOND time. You are INDEPENDENT of the author of the candidate; your "
    "job is semantic fan-in over the reviews below, NOT a fresh from-scratch review. You already reviewed this exact "
    "frozen subject in the first pass (your original verdict is given). Now reconcile ALL the independent reviews into "
    "ONE decision.\n\n"
    "Do this:\n"
    "1. For EACH distinct finding across every review, decide on one line: `ACCEPT <finding> — <why it holds and is "
    "reachable>`, `REJECT <finding> — <why it does not hold: not reachable, contradicted by the material, or outside the "
    "review's scope>`, or `UNVERIFIED <finding> — <the exact evidence that would settle it>`. Verify each cited claim "
    "against the material before accepting it.\n"
    "2. Give the FIVE-PART SYNTHESIS in five short labelled lines: SHARED FACTS · DISAGREEMENTS · STRONGEST SURVIVING "
    "BLOCK/FLAG · EVIDENCE NEEDED · ONE RECOMMENDATION.\n"
    "3. Emit EXACTLY ONE decision as the LAST `ADJUDICATION:` line of your reply, one of:\n"
    "   ADJUDICATION: CLEAN\n"
    "   ADJUDICATION: CORRECT\n"
    "   ADJUDICATION: HOLD :: <why it cannot be safely corrected in one turn, or which owner authority is needed>\n"
    "For CORRECT, put the ONE fix inside a bounded block so it is never truncated — it MAY be multiline and MAY contain "
    "code or a diff:\n"
    "   <fix>\n   ...the single, complete, sufficient correction instruction...\n   </fix>\n"
    "(A short one-line fix may also be written inline as `ADJUDICATION: CORRECT :: <fix>`, but prefer the <fix> block for "
    "anything with code, diffs, or newlines.) Emit CLEAN only if NO accepted must-fix finding survives. Emit CORRECT with "
    "EXACTLY ONE bounded fix (never a menu, never 'choose'). Emit HOLD if more than one independent correction is "
    "required, if owner authority is needed, or if you cannot adjudicate. Do not ask the reader to choose."
)


def build_adjudicator_task(reviews: dict, *, scope: str = "", standing_prompt: str = "",
                           synthesis: Optional[dict] = None) -> str:
    """Compose the Codex-B adjudicator TASK (instructions + the independent reviews to adjudicate). The frozen SUBJECT
    (the candidate answer + evidence the arms saw) is delivered SEPARATELY as the review material — this matches the
    codex broker's (task, subject_path) shape. `reviews` is {lane: {"verdict": .., "reason": ..}} incl. codex's own
    first-pass verdict."""
    codex = reviews.get("codex") or {}
    others = [(ln, r) for ln, r in reviews.items() if ln != "codex"]
    parts = [_ADJUDICATOR_CONTRACT, ""]
    parts.append(f"STANDING RULE (owner adjudication policy):\n{standing_prompt or DEFAULT_STANDING_RULE}")
    if scope:
        parts.append(f"\nSCOPE OF THE CHANGE UNDER REVIEW:\n{scope}")
    parts.append(f"\nYOUR ORIGINAL (first-pass) VERDICT: {str(codex.get('verdict') or 'NONE')}\n"
                 f"{(codex.get('reason') or '').strip()}")
    parts.append("\nTHE OTHER INDEPENDENT REVIEWS OF THE SAME FROZEN SUBJECT:")
    for ln, r in others:
        parts.append(f"\n[{ln}] {str(r.get('verdict') or 'NONE')}:\n{(r.get('reason') or '').strip()}")
    if synthesis and isinstance(synthesis, dict):
        sf = synthesis.get("shared_facts") or {}
        parts.append(f"\nMECHANICAL SHARED FACTS (for reference; do not just repeat): "
                     f"subject_sha256={sf.get('subject_sha256')}, lanes_run={sf.get('lanes_run')}, "
                     f"subject_complete={sf.get('subject_complete')}")
    parts.append("\nThe FROZEN SUBJECT under review (the candidate answer + the evidence the arms saw) is provided to you "
                 "as the review MATERIAL. Verify each cited finding against it before accepting.")
    return "\n".join(parts)


def parse_adjudication(text: str) -> dict:
    """Extract the single trailing ADJUDICATION action from the adjudicator's reply. Returns
    {decision, instruction, body}. FAIL CLOSED: no parseable action line -> decision 'HOLD'."""
    body = text or ""
    decision, instruction = None, ""
    for line in body.splitlines():                                   # take the LAST valid action line
        m = _ACTION_RE.match(line)
        if m:
            decision = m.group(1).upper()
            instruction = (m.group(2) or "").strip()
    if decision is None:
        return {"decision": "HOLD", "instruction": "adjudicator emitted no parseable ADJUDICATION action line",
                "body": body, "parsed": False}
    if decision == "CORRECT":
        # prefer the MULTILINE-safe <fix>...</fix> block (the LAST one) over the truncated inline `:: <line>` (gemini)
        fixes = _FIX_RE.findall(body)
        if fixes and fixes[-1].strip():
            instruction = fixes[-1].strip()
    return {"decision": decision, "instruction": instruction, "body": body, "parsed": True}


def _accepted_blocks_survive(body: str) -> bool:
    """True if the adjudicator's ACCEPT lines include a BLOCK-class finding — a cross-check so a CLEAN can never stand
    while the adjudicator itself accepted a blocking defect (belt-and-braces against a contradictory reply)."""
    for line in (body or "").splitlines():
        s = line.strip().upper()
        if s.startswith("ACCEPT") and "BLOCK" in s and "REJECT" not in s and "UNVERIFIED" not in s:
            return True
    return False


def adjudicate(subject: str, reviews: dict, *, scope: str = "", standing_prompt: str = "",
               synthesis: Optional[dict] = None, run_codex: Optional[Callable] = None,
               timeout_s: int = 300, coverage_incomplete_reason: str = "") -> dict:
    """Run the semantic adjudication stage. `run_codex(task, subject, timeout_s) -> (verdict, reason, detail)` is the
    SAME Codex lane (via codex_broker_queue), reused for the second (adjudicator) pass — injected so this stage adds no
    new transport: `task` is the adjudicator prompt, `subject` the frozen candidate material.

    A DECISION-BACKED Codex-B pass is the authority: its CLEAN may reject and CLEAR false substantive BLOCKs (owner's
    standing instruction — Codex-B accepts genuine findings and rejects hallucinations). BUT a CLEAN may NEVER certify
    INCOMPLETE required coverage: the caller passes `coverage_incomplete_reason` (non-empty) when a required first-pass
    lane was missing / UNAVAILABLE / DEGRADED, and a CLEAN is then forced to HOLD (a reviewer failure is not approval;
    intentional POLICY_DENIED restricted coverage is NOT incomplete and is not passed here). Returns
    {decision: CLEAN|CORRECT|HOLD, instruction, adjudicator_verdict, body, parsed, action_prompt}.
    FAIL CLOSED when the second Codex call is NOT decision-backed OR coverage is incomplete: no run_codex, an
    UNAVAILABLE/DEGRADED/empty adjudicator verdict, an unparseable action, a CLEAN that contradicts the adjudicator's OWN
    accepted BLOCK, a CORRECT with an empty instruction, or a CLEAN over incomplete required coverage -> HOLD."""
    out = {"decision": "HOLD", "instruction": "", "adjudicator_verdict": None, "body": "", "parsed": False,
           "_coverage_incomplete": (coverage_incomplete_reason or "").strip()}
    if run_codex is None:
        out["instruction"] = "no adjudicator available (run_codex not provided) — fail closed"
        return _finalize(out)
    task = build_adjudicator_task(reviews, scope=scope, standing_prompt=standing_prompt, synthesis=synthesis)
    try:
        verdict, reason, _detail = run_codex(task, subject, timeout_s)
    except Exception as e:  # noqa: BLE001 — an adjudicator failure is fail-closed HOLD, never a silent CLEAN
        out["instruction"] = f"adjudicator call failed ({type(e).__name__}: {e}) — fail closed"
        return _finalize(out)
    out["adjudicator_verdict"] = str(verdict or "").upper()
    if out["adjudicator_verdict"] in ("UNAVAILABLE", "DEGRADED", ""):
        out["instruction"] = f"adjudicator {out['adjudicator_verdict'] or 'EMPTY'} — cannot certify, fail closed"
        return _finalize(out)
    parsed = parse_adjudication(reason or "")
    out.update(decision=parsed["decision"], instruction=parsed["instruction"], body=parsed["body"],
               parsed=parsed["parsed"])
    return _finalize(out)


def _finalize(out: dict) -> dict:
    """Apply the fail-closed cross-checks and stamp `action_prompt`. A decision-backed CLEAN is honoured (it may clear
    false independent BLOCKs) EXCEPT when the reply is self-contradictory (CLEAN while the adjudicator itself ACCEPTed a
    BLOCK). A CORRECT needs a non-empty instruction, else it degrades to HOLD."""
    decision = out.get("decision") or "HOLD"
    if decision == "CLEAN" and _accepted_blocks_survive(out.get("body", "")):
        decision, out["instruction"] = "HOLD", "adjudicator returned CLEAN but accepted a BLOCK finding — fail closed"
    if decision == "CLEAN" and out.get("_coverage_incomplete"):
        # a decision-backed CLEAN may clear a false substantive BLOCK, but it may NOT convert INCOMPLETE required coverage
        # into approval (Greg 2026-07-23): a missing/UNAVAILABLE/DEGRADED required first-pass lane keeps the outcome HOLD.
        decision, out["instruction"] = "HOLD", f"required reviewer coverage incomplete — {out['_coverage_incomplete']}"
    if decision == "CORRECT" and not (out.get("instruction") or "").strip():
        decision, out["instruction"] = "HOLD", "adjudicator returned CORRECT with no instruction — fail closed"
    if decision not in ("CLEAN", "CORRECT", "HOLD"):
        decision, out["instruction"] = "HOLD", f"unrecognized adjudication decision {decision!r} — fail closed"
    # HOLD must carry ONE concise reason, never an empty block (gemini 2026-07-23): a bare `ADJUDICATION: HOLD` with no
    # `:: reason` would otherwise surface as a silent UI block.
    if decision == "HOLD" and not (out.get("instruction") or "").strip():
        out["instruction"] = "held by the Codex-B adjudicator (no specific reason provided) — fail closed"
    out["decision"] = decision
    out["clean"] = (decision == "CLEAN")
    out["action_prompt"] = ("" if decision == "CLEAN" else (out.get("instruction") or "").strip())
    out.pop("_coverage_incomplete", None)                            # internal input — not part of the public result
    return out

# --------------------------------------------------------------------------- production Codex-B transport (reused)
def _codex_reply_text(res: dict) -> str:
    """Extract the FULL final agent message of a decision-backed broker run. The relay result dict carries only
    verdict/run_id/packet paths — NOT the reply text; the reply lives in the runtime-captured raw files the packet cites:
    prefer `<base>.last_message.txt`, fall back to the agent_message item in `<base>.stdout.jsonl`."""
    import json as _json
    import os as _os
    import re as _re
    pk = res.get("packet") or res.get("packet_path") or ""
    stdout_path = ""
    try:
        m = _re.search(r"raw stdout: `([^`]+\.stdout\.jsonl)`", open(pk, encoding="utf-8").read())
        if m:
            stdout_path = m.group(1)
    except Exception:  # noqa: BLE001
        pass
    if stdout_path:
        lm = stdout_path.replace(".stdout.jsonl", ".last_message.txt")
        try:
            if _os.path.exists(lm):
                return open(lm, encoding="utf-8").read()
        except Exception:  # noqa: BLE001
            pass
        try:
            for ln in open(stdout_path, encoding="utf-8"):
                d = _json.loads(ln)
                item = d.get("item") or {}
                if item.get("type") == "agent_message" and item.get("text"):
                    return item["text"]
        except Exception:  # noqa: BLE001
            pass
    return ""


def broker_run_codex(task: str, subject: str, timeout_s: int):
    """The PRODUCTION Codex-B adjudicator transport: the SAME codex lane via `codex_broker_queue.submit_and_wait` — NO new
    transport, no daemon (self-serves the queue if idle). Returns (verdict, reply_text, detail); reply_text is the FULL
    final agent message (the ADJUDICATION line lives there). FAIL CLOSED to UNAVAILABLE (the caller HOLDs) on a non-DONE /
    not-decision-backed / unreadable-reply result. codex_broker_queue is imported lazily so this module stays importable
    (stdlib-only) under the Hermes py3.11 for the adapter tests."""
    import hashlib
    import os
    import sys
    import tempfile
    try:
        import codex_broker_queue as CBQ
    except ModuleNotFoundError:
        # locate the deployed bin (where codex_broker_queue lives) — resilient to callers that did not pre-set the path
        _bin = os.environ.get("ABACDA_BIN") or "/Users/spinec/hermes-workforce-worktrees/hermes-master-live/bin"
        if _bin not in sys.path:
            sys.path.insert(0, _bin)
        import codex_broker_queue as CBQ
    sha = hashlib.sha256((subject or "").encode("utf-8")).hexdigest()
    d = tempfile.mkdtemp(prefix="adjudicate-")
    sp = os.path.join(d, "subject.txt")
    with open(sp, "w", encoding="utf-8") as fh:
        fh.write(subject or "")
    res = CBQ.submit_and_wait(task, sp, subject_sha=sha, out_dir=d, timeout=timeout_s,
                              serve_if_idle=True, submitter="abacda_adjudicate")
    if not isinstance(res, dict):
        return "UNAVAILABLE", f"broker returned non-dict: {type(res).__name__}", {"raw": str(res)}
    status = str(res.get("status") or "").upper()
    if status != "DONE" or not res.get("decision_backed"):
        return "UNAVAILABLE", (res.get("fail_reason") or f"adjudicator not decision-backed (status={status})"), res
    reply = _codex_reply_text(res)
    if not reply.strip():
        return "UNAVAILABLE", "decision-backed run but the captured reply text is unreadable — fail closed", res
    return str(res.get("verdict") or "").upper(), reply, res
