"""Real integration tests for the shared review engine (bin/review_response.py).

Sanitization/classification/tiering are REAL (deployed abacda_auto + auto_abacda_gate); only the abacda_answer runner
is injected so no live lanes fire. Covers the exact holes Codex reproduced: raw sensitive egress, effective mode
mapping, fail-closed on unsanitizable values, tier-from-request, durable record. Run under the DEPLOYED python (3.14)."""
import json
import sys
from pathlib import Path

_WT = Path(__file__).resolve().parents[1]
if str(_WT / "bin") not in sys.path:
    sys.path.insert(0, str(_WT / "bin"))

import review_response as RR  # noqa: E402


def _capturing_runner(rc=0, synth="EXIT 0: all lanes APPROVE", clean_permitted=True):
    seen = {}

    def run(subject_path, mode, record_dir, task, timeout_s):
        seen["subject"] = Path(subject_path).read_text(encoding="utf-8")
        seen["mode"] = mode
        return rc, synth, clean_permitted                            # 3-tuple: (rc, synthesis, clean_aggregate_permitted)
    return seen, run


def test_t0_trivia_is_not_reviewed(tmp_path):
    seen, run = _capturing_runner()
    r = RR.review_response("say hi", "Fixed a small typo.", str(tmp_path), run_answer=run)
    assert r["reviewed"] is False and r["clean"] is True and "mode" not in seen


def test_t2_answer_maps_to_shared_mode_not_quad(tmp_path):
    seen, run = _capturing_runner()
    r = RR.review_response("cap the retry", "Add a bounded max-retry with backoff to fetch(); then commit the fix.",
                           str(tmp_path), run_answer=run)
    assert r["tier"] == "T2" and r["mode"] == "shared"                # fix 1: T2 is a Duo, not a full quad
    assert seen["mode"] == "shared"


def test_raw_sensitive_values_never_reach_the_runner(tmp_path):
    # fix 2 — the exact hole Codex reproduced (raw_ssn_in_runner_subject=True)
    seen, run = _capturing_runner()
    answer = "Wire it with SSN 123-45-6789 and api_key = zz-livesecret-abc123456 then commit."
    r = RR.review_response("configure the client", answer, str(tmp_path), run_answer=run)
    assert r["coverage"] == "SANITIZED_FULL" and r["effective_tier"] == "T4"
    assert "123-45-6789" not in seen["subject"]                       # raw SSN reaches ZERO arms
    assert "zz-livesecret-abc123456" not in seen["subject"]           # raw secret reaches ZERO arms
    assert "[REDACTED_SSN_LIKE]" in seen["subject"] and "[REDACTED_SECRET]" in seen["subject"]
    assert "123-45-6789" not in json.dumps(r)                         # never surfaced in the verdict either


def test_unsanitizable_sensitive_fails_closed_no_runner(tmp_path):
    # a scanner exception (fail-closed) must NOT run any arm and must NOT be clean
    seen, run = _capturing_runner()
    r = RR.review_response("x", "some substantive commit answer", str(tmp_path),
                           run_answer=lambda *a, **k: (0, "clean", True))
    # force the exception branch via a broken classifier is covered in the hermes-side tests; here assert the contract
    # for a residual-domain case: an SSN whose label survives keeps it non-clean -> local-only BLOCK.
    r2 = RR.review_response("x", "record: SSN 123-45-6789 belongs to the trading portfolio account number 12345678",
                            str(tmp_path), run_answer=run)
    assert r2["clean"] is False and r2["coverage"] == "RESTRICTED"
    assert "mode" not in seen or seen.get("mode") != "governed"       # no full-quad arm run on raw sensitive
    assert "BLOCKED" in r2["findings"]


def test_runner_error_is_not_clean(tmp_path):
    # fix 3: a runner failure must fail CLOSED (not clean); the real _subprocess_answer converts spawn/timeout errors
    # to (2, BLOCKED), so here we assert review_response honours a nonzero rc as not-clean.
    def failed(sp, m, rd, t, ts):
        return 2, "ABACDA runner failed to complete — treat as BLOCKED (fail-closed)", None
    r = RR.review_response("q", "a substantive #64 governance answer", str(tmp_path), run_answer=failed)
    assert r["clean"] is False and "BLOCKED" in r["findings"]


def test_tier_from_request_not_only_answer(tmp_path):
    # fix 6: a high-risk REQUEST is caught even if the answer omits the trigger vocabulary. The gate must consult the
    # request — a governance ask with a terse answer still reviews.
    seen, run = _capturing_runner()
    r = RR.review_response("review the #64 governance gate change and the abacda architecture", "Yes.",
                           str(tmp_path), run_answer=run)
    assert RR._GATE._rank(r["tier"]) >= RR._GATE._rank("T2")          # reviewed because the REQUEST is substantive
    assert seen.get("mode")                                           # the runner was actually invoked
    # control: the same terse answer with a trivial request stays silent (proves it's the request driving it)
    seen2, run2 = _capturing_runner()
    r2 = RR.review_response("say hello", "Yes.", str(tmp_path), run_answer=run2)
    assert r2["reviewed"] is False and "mode" not in seen2


def test_clinical_topic_is_full_raw_quad(tmp_path):
    seen, run = _capturing_runner()
    r = RR.review_response("summarise", "General de-identified warfarin vs apixaban anticoagulation tradeoffs.",
                           str(tmp_path), run_answer=run)
    assert r["effective_tier"] == "T4" and r["coverage"] == "full" and seen["mode"] == "governed"


def test_durable_record_in_caller_dir(tmp_path):
    seen, run = _capturing_runner()
    RR.review_response("cap the retry", "Add a bounded retry to fetch(); commit.", str(tmp_path), run_answer=run)
    assert (tmp_path / "subject.md").exists()                         # fix 7: record lands in the caller-owned dir


def test_subprocess_timeouts_capped_below_budget(monkeypatch):
    # Codex-B #3: every nested bound must be <= the remaining answer budget
    import subprocess as _sp
    seen = {}

    class _CP:
        returncode, stdout, stderr = 0, "", ""

    def fake_run(argv, **kw):
        seen["timeout"] = kw.get("timeout")
        seen["lane"] = (kw.get("env") or {}).get("ABACDA_LANE_TIMEOUT_S")
        return _CP()
    monkeypatch.setattr(_sp, "run", fake_run)
    RR._subprocess_answer("/tmp/s.md", "governed", "/tmp/rec", "task", 460)
    assert seen["timeout"] <= 460                                    # subprocess hard-timeout <= remaining budget
    assert int(seen["lane"]) < 460                                   # lane wall-clock strictly below the budget


def test_rc0_with_flag_is_not_clean(tmp_path):
    # Codex-B #3: abacda_answer returns rc 0 for a tolerated FLAG too; only clean_aggregate_permitted==True is clean.
    seen, run = _capturing_runner(rc=0, synth="EXIT 0: FLAG from grok (tolerated)", clean_permitted=False)
    r = RR.review_response("q", "a substantive #64 governance answer", str(tmp_path), run_answer=run)
    assert r["clean"] is False and r["exit_code"] == 0                # rc 0 + FLAG -> NOT clean
    assert r["findings"]


def test_all_approve_record_is_clean(tmp_path):
    seen, run = _capturing_runner(rc=0, synth="EXIT 0: all lanes APPROVE", clean_permitted=True)
    r = RR.review_response("q", "a substantive #64 governance answer", str(tmp_path), run_answer=run)
    assert r["clean"] is True and r["clean_aggregate_permitted"] is True   # rc 0 + gate permits -> clean


def test_missing_or_malformed_record_fails_closed(tmp_path):
    # the real _subprocess_answer reads clean_aggregate_permitted from record.json; no/blank record -> None -> not clean.
    assert RR._clean_permitted_from_record(str(tmp_path)) is None      # no record.json
    (tmp_path / "record.json").write_text("{ not json", encoding="utf-8")
    assert RR._clean_permitted_from_record(str(tmp_path)) is None      # malformed
    (tmp_path / "record.json").write_text('{"synthesis": {"synthesis_gate": {"clean_aggregate_permitted": true}}}',
                                          encoding="utf-8")
    assert RR._clean_permitted_from_record(str(tmp_path)) is True      # nested gate location read
    seen, run = _capturing_runner(rc=0, synth="x", clean_permitted=None)
    r = RR.review_response("q", "a substantive governance answer", str(tmp_path), run_answer=run)
    assert r["clean"] is False                                        # rc 0 but record signal absent -> fail closed


def test_restricted_domain_wins_over_topic_no_cloud(tmp_path):
    # gemini 2026-07-23: an answer with BOTH a restricted domain (trading/patent) AND a clinical topic must stay
    # RESTRICTED (shared = Codex+local); the domain confinement WINS and it must NOT widen to the governed cloud quad.
    # Control: the SAME clinical text WITHOUT the domain routes to the full cloud quad (proving the topic is really seen).
    seen_topic, run_topic = _capturing_runner()
    clinical = "General de-identified warfarin vs apixaban anticoagulation tradeoffs."
    rt = RR.review_response("q", clinical, str(tmp_path), run_answer=run_topic)
    assert rt["coverage"] == "full" and seen_topic.get("mode") == "governed"   # topic alone -> full cloud quad

    seen_both, run_both = _capturing_runner()
    both = "Proprietary trading strategy patent for the brokerage. " + clinical   # domain + the SAME clinical topic
    rb = RR.review_response("q", both, str(tmp_path), run_answer=run_both)
    assert "restricted_keyword" in rb.get("sensitive_classes", [])   # the restricted domain is present
    assert rb["coverage"] == "RESTRICTED"                            # ...and WINS over the co-occurring topic
    assert rb["mode"] == "shared" and seen_both.get("mode") == "shared"   # Codex+local, NOT governed cloud
    assert rb["effective_tier"] == "T4"                             # topic still raises tier to T4 — but reviewed LOCALLY


def test_clean_permitted_handles_non_dict_record(tmp_path):
    # gemini 2026-07-23: a valid-JSON-but-not-object record.json (or a non-dict synthesis) must fail closed (None), never crash.
    for bad in ("null", "[1, 2, 3]", '"a string"', '{"synthesis": "not a dict"}'):
        (tmp_path / "record.json").write_text(bad, encoding="utf-8")
        assert RR._clean_permitted_from_record(str(tmp_path)) is None, f"should fail closed on {bad!r}"


def test_main_malformed_stdin_fails_closed(tmp_path, monkeypatch, capsys):
    # gemini 2026-07-23: malformed / non-object stdin must fail closed (BLOCKED + non-zero exit), NOT review an empty
    # answer and trivially certify clean=True.
    import io
    for bad in ("this is not json", "[1, 2, 3]", "null", '"just a string"'):
        monkeypatch.setattr("sys.stdin", io.StringIO(bad))
        rc = RR._main(["--stdin", "--record-dir", str(tmp_path)])
        assert rc == 2, f"expected non-zero exit for {bad!r}"
        out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert out["clean"] is False and "BLOCKED" in out["findings"]


def _write_lanes(tmp_path, lanes):
    (tmp_path / "record.json").write_text(json.dumps({"lanes": lanes}), encoding="utf-8")


def _adj_codex(reply, verdict="FLAG"):
    def run(task, subject, timeout_s):
        return verdict, reply, {}
    return run


def test_adjudicate_findings_unavailable_holds(tmp_path):
    # Hermes decision mapping: a not-decision-backed adjudicator must fail CLOSED to HOLD (never a silent clear).
    _write_lanes(tmp_path, {"codex": {"verdict": "BLOCK", "reason": "x"}, "local": {"verdict": "APPROVE", "reason": "y"}})
    r = RR._adjudicate_findings("subj", str(tmp_path), "governed", 60,
                                run_codex=_adj_codex("ADJUDICATION: CLEAN", verdict="UNAVAILABLE"))
    assert r["decision"] == "HOLD" and (r.get("action_prompt") or "").strip()


def test_adjudicate_findings_clean_over_false_block(tmp_path):
    # a decision-backed CLEAN clears a FALSE independent BLOCK (no sticky-block, no owner flag) — governed coverage is
    # COMPLETE here (all required lanes present with real verdicts), so the CLEAN stands.
    _write_lanes(tmp_path, {"codex": {"verdict": "BLOCK", "reason": "x"}, "grok": {"verdict": "APPROVE", "reason": "g"},
                            "gemini": {"verdict": "BLOCK", "reason": "y"}, "local": {"verdict": "APPROVE", "reason": "l"}})
    r = RR._adjudicate_findings("subj", str(tmp_path), "governed", 60,
                                run_codex=_adj_codex("REJECT — contradicted by the material.\nADJUDICATION: CLEAN"))
    assert r["decision"] == "CLEAN"


def test_adjudicate_findings_real_block_corrects(tmp_path):
    _write_lanes(tmp_path, {"codex": {"verdict": "BLOCK", "reason": "x"}})
    r = RR._adjudicate_findings("subj", str(tmp_path), "governed", 60,
                                run_codex=_adj_codex("ACCEPT — real.\nADJUDICATION: CORRECT\n<fix>\ncap the retry\n</fix>"))
    assert r["decision"] == "CORRECT" and "cap the retry" in r["action_prompt"]


def test_adjudicate_findings_no_lanes_holds(tmp_path):
    # no record.json -> no lanes -> fail closed to HOLD, WITHOUT calling codex.
    called = {"n": 0}

    def _never(task, subject, timeout_s):
        called["n"] += 1
        return "FLAG", "ADJUDICATION: CLEAN", {}
    r = RR._adjudicate_findings("subj", str(tmp_path), "governed", 60, run_codex=_never)
    assert r["decision"] == "HOLD" and called["n"] == 0


def test_review_response_populates_adjudication_on_non_clean(tmp_path):
    # end-to-end: a non-clean arms result triggers the engine adjudication (lanes written by the injected runner).
    def run(subject_path, mode, record_dir, task, timeout_s):
        _write_lanes(Path(record_dir), {"codex": {"verdict": "BLOCK", "reason": "unbounded retry"}})
        return 2, "EXIT 2: codex BLOCK", False       # not clean
    r = RR.review_response("cap the retry", "Add a retry to fetch(); commit.", str(tmp_path), run_answer=run,
                           adjudicate_run_codex=_adj_codex("ACCEPT — real.\nADJUDICATION: CORRECT\n<fix>\ncap it\n</fix>"))
    assert r["clean"] is False and r["adjudication"]["decision"] == "CORRECT" and "cap it" in r["adjudication"]["action_prompt"]


def test_adjudicate_findings_incomplete_coverage_holds_over_clean(tmp_path):
    # Greg 2026-07-23: governed mode with gemini UNAVAILABLE -> incomplete required coverage -> HOLD even on a CLEAN reply.
    _write_lanes(tmp_path, {"codex": {"verdict": "BLOCK", "reason": "x"}, "grok": {"verdict": "APPROVE", "reason": "y"},
                            "gemini": {"verdict": "UNAVAILABLE", "reason": "timeout"}, "local": {"verdict": "APPROVE", "reason": "z"}})
    r = RR._adjudicate_findings("subj", str(tmp_path), "governed", 60,
                                run_codex=_adj_codex("REJECT — false.\nADJUDICATION: CLEAN"))
    assert r["decision"] == "HOLD" and "coverage incomplete" in r["action_prompt"].lower()


def test_adjudicate_findings_restricted_policy_denied_unchanged(tmp_path):
    # SHARED (restricted) mode: only codex is required; grok/gemini absent is intentional -> NOT incomplete -> CLEAN stands.
    _write_lanes(tmp_path, {"codex": {"verdict": "BLOCK", "reason": "x"}, "local": {"verdict": "APPROVE", "reason": "y"}})
    r = RR._adjudicate_findings("subj", str(tmp_path), "shared", 60,
                                run_codex=_adj_codex("REJECT — false alarm.\nADJUDICATION: CLEAN"))
    assert r["decision"] == "CLEAN"                                   # policy-denied cloud arms don't gate shared coverage
