#!/usr/bin/env python3
"""review_response — the ONE shared ABACDA review engine (JEDI unification).

Composes the deployed sanitization (abacda_auto.classify_material / sanitize_material) IN FRONT of the canonical runner
(abacda_answer.py) so that:
  * raw sensitive VALUES reach ZERO model arms — a deterministic sanitized copy is reviewed, or (if it can't be made
    clean) the review is local-only and T2+ is BLOCKED (never a silent pass) — fixes the raw-egress + fail-open holes;
  * the computed T2/T3/T4 tier maps to an EXPLICIT runner mode (shared / failure_mode_review / governed), so a T2
    answer is a Duo, not a full quad, and the task text can no longer mis-route via `--mode auto`;
  * one durable record.json + five-part synthesis.md land in a CALLER-OWNED record dir (not disposable temp state).

Runs under the DEPLOYED python (3.14) — it imports abacda_auto + auto_abacda_gate, which the Hermes venv (3.11) cannot.
Adapters call it: Hermes shells out to the CLI (JSON on stdout); the Claude hook can import review_response() directly
when it is later refactored onto this engine. This module never runs the arms itself — abacda_answer owns lane execution,
required-lane fatality, and the five-part synthesis.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

_DEPLOYED_BIN = os.environ.get("ABACDA_BIN") or "/Users/spinec/hermes-workforce-worktrees/hermes-master-live/bin"
# the tier gate (auto_abacda_gate) lives in bin/; the sanitizer (abacda_auto) lives in hooks/claude/ — add both.
_HOOKS_CLAUDE = str(Path(_DEPLOYED_BIN).parent / "hooks" / "claude")
for _p in (_DEPLOYED_BIN, _HOOKS_CLAUDE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import abacda_auto as _AH          # noqa: E402 — deployed sanitizer/classifier (values/topic/domain + redaction)
import auto_abacda_gate as _GATE   # noqa: E402 — deployed text tier gate (_semantic_floor_from_text / _rank)

# ACCEPTED T2/T4 LADDER ONLY (Greg/Codex-B 2026-07-22): the deployed text gate emits T0/T2/T4 — there is NO T3 in this
# slice, and a clinical/security-topic or sensitive-value answer is raised to T4. tier (arm WEIGHT) -> canonical runner
# MODE (concrete lane-set; NEVER 'auto', so task text can't mis-route):
#   T2 Duo  -> shared     (codex + local)
#   T4 Quad -> governed   (codex + grok + gemini + local)
# both are degraded_fatal in abacda_answer.MODES, so a missing REQUIRED lane is itself a BLOCK. (No T3/failure_mode_review
# is claimed; an unexpected tier falls back to governed — the stricter set.)
_TIER_MODE = {"T2": "shared", "T4": "governed"}
_REVIEW_TASK = ("Independently review this proposed final answer to the request. Lead with: how could this look right "
                "but be wrong? Reply APPROVE / FLAG / BLOCK with the strongest concrete, actionable finding.")


def _tier(question: str, answer: str) -> str:
    # fix 6: tier from the REQUEST + the answer, so a high-risk ask isn't missed when the answer omits the vocabulary.
    return str(_GATE._semantic_floor_from_text("auto", question or "", answer or "", [])).upper()


def _classify(material: str) -> dict:
    return _AH.classify_material(material)


# NOTE: the deployed ABACDA engine (abacda_auto sanitizer, auto_abacda_gate tier, abacda_answer runner) is an INTENTIONAL
# named trusted RUNTIME dependency of this consumption slice — it is not re-verified here. Dependency-integrity binding (a
# trusted expected-hash manifest, TOCTOU-proof execution, per-decision cryptographic audit records) is a DEPLOYED-ENGINE
# release-governance concern and an accepted NAMED RESIDUAL, deliberately NOT built inside Hermes (Greg 2026-07-23).


def _persistent_dir(record_dir: str) -> Path:
    d = Path(record_dir).expanduser()
    d.mkdir(parents=True, exist_ok=True)
    return d


def review_response(question: str, answer: str, record_dir: str, *, timeout_s: int = 460,
                    run_answer=None, adjudicate_run_codex=None) -> dict:
    """Review a proposed answer. Returns a dict (also the CLI's JSON):
        {tier, effective_tier, mode, coverage, reviewed, clean, findings, record_dir, exit_code,
         sensitive_classes, raw_sha256, subject_sha256}
    clean=True  -> deliver as-is (silent). clean=False -> `findings` (five-part synthesis or a fail-closed reason) must
    be fed back. reviewed=False + clean=True ONLY for below-Duo trivia. `run_answer` is injectable for tests
    (default: subprocess abacda_answer.py); it takes (subject_path, mode, record_dir, task, timeout_s) -> (rc, synthesis)."""
    q, a = question or "", answer or ""
    material = f"# Proposed final answer under Auto-ABACDA review\n\n## Request\n{q}\n\n## Answer\n{a}\n"
    raw_sha = hashlib.sha256(material.encode("utf-8")).hexdigest()
    out: dict = {"tier": "T0", "effective_tier": "T0", "mode": "", "coverage": "none", "reviewed": False,
                 "clean": True, "findings": "", "record_dir": "", "exit_code": None, "sensitive_classes": [],
                 "raw_sha256": raw_sha, "subject_sha256": ""}

    # CLASSIFY FIRST, then gate: sensitive VALUES / clinical|security TOPIC / a scanner EXCEPTION RAISE scrutiny to the
    # quad tier even when the keyword tier is trivial (matches the deployed decide_hook). Only a truly trivial AND
    # non-sensitive answer stays silent.
    base_tier = _tier(q, a)
    cls = _classify(material)
    values, topic = cls.get("values") or [], (cls.get("clinical_topic") or []) + (cls.get("security_topic") or [])
    domain, exc = cls.get("restricted_domain") or [], cls.get("exception") or ""
    out["sensitive_classes"] = sorted(set(values) | set(domain))

    tier = "T4" if (values or topic or exc) else base_tier
    out["tier"], out["effective_tier"] = base_tier, tier
    if _GATE._rank(tier) < _GATE._rank("T2"):
        return out                                                    # trivial AND non-sensitive -> silent, no runner, no files

    # --- coverage decision (mirrors abacda_auto.review's policy) -> (subject bytes, coverage, effective tier, mode)
    subject, coverage = material, "full"
    if values or exc:
        # raw sensitive VALUES (or a fail-closed scanner exception): raw must reach ZERO arms. Try the deterministic
        # sanitized copy; only a CLEAN re-scan may be reviewed by the full quad. Otherwise local-only -> BLOCK for T2+.
        sanitized_ok = False
        if values and not exc:
            try:
                cand = _AH.sanitize_material(material)
                recls = _classify(cand)
                sanitized_ok = not (recls.get("values") or recls.get("restricted_domain") or recls.get("exception"))
                if sanitized_ok:
                    subject = cand
            except Exception:  # noqa: BLE001 — sanitization failure fails CLOSED to local-only
                sanitized_ok = False
        if sanitized_ok:
            coverage, tier = "SANITIZED_FULL", "T4"
        else:
            # FAIL CLOSED: no model arm may see raw sensitive values. T2+ cannot certify -> BLOCK, not silent pass.
            out.update(effective_tier="T4", mode="local-only", coverage="RESTRICTED",
                       reviewed=True, clean=False, record_dir=str(_persistent_dir(record_dir)),
                       findings=("BLOCKED (Auto-ABACDA fail-closed): the answer carries sensitive VALUES "
                                 f"(classes={out['sensitive_classes']}) that deterministic redaction could not fully "
                                 "remove (or the scanner errored), so raw bytes were sent to ZERO model arms. Coverage is "
                                 "local-only RESTRICTED; a T2+ answer cannot be certified. Remove/replace the sensitive "
                                 "values, or re-run once they are out of the answer."))
            return out
    elif domain:
        # Restricted DOMAIN (patent/trading) is checked BEFORE topic (gemini 2026-07-23): a restricted domain FORBIDS
        # cloud egress, and that confinement must WIN over a co-occurring clinical/security topic — otherwise an answer
        # that is both (e.g. a trading algo discussed with a clinical aside) would match `topic`, widen to `full`, and be
        # sent to the cloud quad. RESTRICTED coverage forces `shared` mode below (Codex + LOCAL arms only; the CLOUD arms
        # grok/gemini are NEVER invoked). It is a confinement, not a downgrade. A restricted domain that ALSO carries a
        # sensitive topic still reviews at T4 — but locally. (Raw sensitive VALUES take the fail-closed branch above.)
        coverage = "RESTRICTED"
        if topic:
            tier = "T4"                                              # sensitive-topic restricted domain -> thorough, LOCAL
    elif topic:
        coverage, tier = "full", "T4"                                # clinical/security TOPIC (no restricted domain) -> full raw quad

    effective = "T4" if tier == "T4" else tier
    # RESTRICTED coverage -> `shared` (Codex + local, no cloud arms), overriding the tier->mode map so a restricted
    # domain is never widened to the full cloud quad. Non-restricted answers use the honest T2->shared / T4->governed map.
    mode = "shared" if coverage == "RESTRICTED" else _TIER_MODE.get(effective, "governed")
    out["effective_tier"], out["mode"], out["coverage"] = effective, mode, coverage

    rec = _persistent_dir(record_dir)                                # T2/T4 only: create the record dir for the subject
    subj_path = rec / "subject.md"
    subj_path.write_text(subject, encoding="utf-8")
    out["subject_sha256"] = hashlib.sha256(subject.encode("utf-8")).hexdigest()
    out["record_dir"] = str(rec)

    # Reserve a bounded slice of the answer budget for the Codex-B adjudicator (a real runner only — an injected test
    # runner does no subprocess). The arms get the rest; the two together stay within `timeout_s` so the outer review
    # subprocess is never killed mid-adjudication (preserves the no-overshoot deadline architecture).
    _adj_reserve = 0 if run_answer is not None else max(60, min(int(timeout_s) // 3, 180))
    _arms_budget = max(1, int(timeout_s) - _adj_reserve)
    runner = run_answer or _subprocess_answer
    rc, synthesis, clean_permitted = runner(str(subj_path), mode, str(rec), _REVIEW_TASK, _arms_budget)
    out["exit_code"] = rc
    out["reviewed"] = True
    out["clean_aggregate_permitted"] = clean_permitted
    # CLEAN requires rc==0 AND the canonical record's clean_aggregate_permitted is EXACTLY True (Codex-B #3): abacda_answer
    # returns rc 0 for all-APPROVE OR a tolerated FLAG — only clean_aggregate_permitted distinguishes all-APPROVE. A rc-0
    # FLAG, a missing/malformed record, a missing required lane, or aggregate False stays NON-clean with findings kept.
    out["clean"] = (rc == 0 and clean_permitted is True)
    out["findings"] = "" if out["clean"] else (
        synthesis or f"ABACDA runner exit {rc}, clean_aggregate_permitted={clean_permitted!r} — not certified clean")
    # SEMANTIC FAN-IN (Codex-B second pass): when the arms did not cleanly approve, adjudicate their per-lane findings
    # into ONE decision so the caller acts on an action prompt, not a raw pile. The sanitized `subject` is exactly what
    # the arms saw (egress-safe). A not-decision-backed adjudicator fails CLOSED to HOLD.
    if not out["clean"]:
        out["adjudication"] = _adjudicate_findings(subject, str(rec), mode, max(1, _adj_reserve or 180),
                                                   run_codex=adjudicate_run_codex)
    return out


def _lanes_from_record(record_dir: str) -> dict:
    """Read the per-lane {verdict, reason} from the canonical record.json written by abacda_answer. Missing/malformed -> {}."""
    try:
        rec = json.loads(Path(record_dir, "record.json").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    lanes = (rec.get("lanes") if isinstance(rec, dict) else None) or {}
    out = {}
    if isinstance(lanes, dict):
        for ln, d in lanes.items():
            if isinstance(d, dict):
                out[ln] = {"verdict": d.get("verdict"), "reason": d.get("reason")}
    return out


# the REQUIRED independent reviewers per mode (shared Duo = Codex; governed Quad = Codex+Grok+Gemini). local is
# dependent; a POLICY_DENIED lane is intentional restricted coverage, NOT "incomplete".
_MODE_REQUIRED_LANES = {"shared": ("codex",), "governed": ("codex", "grok", "gemini")}


def _coverage_incomplete_reason(lanes: dict, mode: str) -> str:
    """Return a concise reason if a REQUIRED lane for `mode` is missing / UNAVAILABLE / DEGRADED (POLICY_DENIED excluded);
    "" when required coverage is complete. A CLEAN adjudication may NOT certify incomplete coverage (Greg 2026-07-23)."""
    failed = []
    for a in _MODE_REQUIRED_LANES.get(mode, ("codex",)):
        d = lanes.get(a)
        v = str((d or {}).get("verdict") or "").upper()
        if d is None:
            failed.append(f"{a}=MISSING")
        elif v in ("UNAVAILABLE", "DEGRADED"):
            failed.append(f"{a}={v}")
    return ("required reviewer(s) did not complete: " + ", ".join(failed)) if failed else ""


def _adjudicate_findings(subject: str, record_dir: str, mode: str, timeout_s: int, *, run_codex=None) -> dict:
    """Run the Codex-B adjudicator on the arms' lane verdicts -> {decision, action_prompt, ...}. FAIL CLOSED to HOLD on
    a missing record, an import failure, or a not-decision-backed adjudicator. A CLEAN over INCOMPLETE required coverage
    is forced to HOLD (Greg 2026-07-23). `run_codex` is injectable for tests."""
    lanes = _lanes_from_record(record_dir)
    if not lanes:
        return {"decision": "HOLD", "action_prompt": "no lane verdicts to adjudicate — fail closed", "instruction": "",
                "adjudicator_verdict": None}
    try:
        import abacda_adjudicate as _AJ
        rc = run_codex or _AJ.broker_run_codex
        return _AJ.adjudicate(subject, lanes, scope=mode, run_codex=rc, timeout_s=max(1, min(int(timeout_s), 180)),
                              coverage_incomplete_reason=_coverage_incomplete_reason(lanes, mode))
    except Exception as e:  # noqa: BLE001 — fail closed, never a silent clear
        return {"decision": "HOLD", "action_prompt": f"adjudicator error ({type(e).__name__}) — fail closed",
                "instruction": "", "adjudicator_verdict": None}


def _subprocess_answer(subject_path: str, mode: str, record_dir: str, task: str, timeout_s: int):
    """Invoke the canonical abacda_answer.py under the deployed python. Returns (exit_code, synthesis_text,
    clean_aggregate_permitted). Every nested bound is <= the remaining budget with NO floor overshoot (Codex-B #3): the
    subprocess hard-timeout is EXACTLY the remaining budget; the lane wall-clock is strictly below it. clean_permitted is
    read from the canonical record.json; a missing/malformed record fails CLOSED (None)."""
    env = dict(os.environ)
    env["HERMES_ABACDA_INTERNAL"] = "1"          # brake: nothing the runner spawns re-enters a review
    proc_s = max(1, timeout_s)
    lane_s = max(1, timeout_s - 15)
    env["ABACDA_LANE_TIMEOUT_S"] = str(lane_s)
    argv = [sys.executable, str(Path(_DEPLOYED_BIN) / "abacda_answer.py"),
            "--subject", subject_path, "--mode", mode, "--record-dir", record_dir, "--task", task]
    try:
        cp = subprocess.run(argv, capture_output=True, text=True, env=env,
                            timeout=proc_s, stdin=subprocess.DEVNULL)
        rc = cp.returncode
    except Exception as e:  # noqa: BLE001 — a spawn/timeout failure is a FAIL-CLOSED review outcome, never clean
        return 2, f"ABACDA runner failed to complete ({type(e).__name__}: {e}) — treat as BLOCKED (fail-closed)", None
    syn = Path(record_dir, "synthesis.md")
    synthesis = syn.read_text(encoding="utf-8", errors="replace") if syn.exists() else (cp.stdout or "")[:4000]
    clean_permitted = _clean_permitted_from_record(record_dir)
    return rc, synthesis, clean_permitted


def _clean_permitted_from_record(record_dir: str):
    """Read clean_aggregate_permitted from the canonical record.json. Returns True/False, or None on a missing/malformed
    record (fail CLOSED). Handles the top-level field and the nested synthesis.synthesis_gate location."""
    try:
        rec = json.loads(Path(record_dir, "record.json").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — no readable record -> fail closed
        return None
    if not isinstance(rec, dict):                # valid JSON but not an object (null/list/str) -> fail closed (gemini 2026-07-23)
        return None
    if "clean_aggregate_permitted" in rec:
        return rec["clean_aggregate_permitted"] is True
    syn = rec.get("synthesis")
    gate = (syn if isinstance(syn, dict) else {}).get("synthesis_gate") or rec.get("synthesis_gate") or {}
    if isinstance(gate, dict) and "clean_aggregate_permitted" in gate:
        return gate["clean_aggregate_permitted"] is True
    return None                                  # required signal absent -> fail closed


def _main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="review_response.py")
    ap.add_argument("--stdin", action="store_true",
                    help="read {question, answer} as JSON from stdin (no raw bytes staged to disk)")
    ap.add_argument("--question-file", default="")
    ap.add_argument("--answer-file", default="")
    ap.add_argument("--record-dir", required=True)
    ap.add_argument("--timeout", type=int, default=460)
    args = ap.parse_args(argv)
    if args.stdin:                                                    # preferred: raw material never touches disk
        raw = sys.stdin.read()
        try:
            payload = json.loads(raw or "{}")
        except Exception:  # noqa: BLE001
            payload = None
        # FAIL CLOSED on malformed / non-object stdin (gemini 2026-07-23): defaulting to an empty {} would review an
        # empty answer and trivially certify clean=True for the caller's real (unparseable) payload. Emit a BLOCKED
        # result and a NON-ZERO exit so the adapter fails closed instead of releasing.
        if not isinstance(payload, dict):
            print(json.dumps({"reviewed": True, "clean": False, "clean_aggregate_permitted": False, "tier": "T4",
                              "coverage": "RESTRICTED", "findings": "BLOCKED (fail-closed): malformed or non-object "
                              "review payload on stdin — the answer could not be parsed, so it cannot be certified."}))
            return 2
        q, a = str(payload.get("question") or ""), str(payload.get("answer") or "")
    else:
        q = Path(args.question_file).read_text(encoding="utf-8", errors="replace")
        a = Path(args.answer_file).read_text(encoding="utf-8", errors="replace")
    result = review_response(q, a, args.record_dir, timeout_s=args.timeout)
    print(json.dumps(result))
    return 0 if result.get("clean") else 2                            # non-zero exit whenever not certified clean


if __name__ == "__main__":
    raise SystemExit(_main())
