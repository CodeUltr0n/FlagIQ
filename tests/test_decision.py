"""Tests for the FlagIQ evidence-gated decision engine."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from flagiq.schemas import IssueReport
from flagiq.decision import run_triage, _compute_confidence
from flagiq.bayesian import evaluate_experiment


def make_issue(**kwargs) -> IssueReport:
    defaults = dict(
        identity="test/repo#1",
        repo="test/repo",
        number=1,
        title="How to configure python interpreter path in workspace settings?",
        body="Looking for the right config option in docs or guide to set venv path in project.",
        labels=[],
        comment_count=0,
        has_linked_pr=False,
        real_resolution="ANSWER",
    )
    defaults.update(kwargs)
    return IssueReport(**defaults)


class TestConfidenceScoring:
    def test_high_confidence_with_repro_and_trace(self):
        issue = make_issue(
            title="Regression crash in asyncio event loop shutdown",
            body=(
                "Steps to reproduce on Python 3.12:\n"
                "```python\nloop.close()\n```\n"
                "Traceback (most recent call last):\n"
                "  File 'asyncio/base_events.py', line 640, in close\n"
                + ("x" * 180)
            ),
        )
        score = _compute_confidence(issue)
        assert score >= 0.70

    def test_low_confidence_minimal_info(self):
        issue = make_issue(title="bug", body="broken", comment_count=0, labels=[])
        score = _compute_confidence(issue)
        assert score <= 0.25

    def test_confidence_capped_at_1(self):
        issue = make_issue(
            title="Very detailed bug title with many words",
            body="Steps to reproduce:\n```python\n1/0\n```\nTraceback:\n" + ("x" * 300),
        )
        score = _compute_confidence(issue)
        assert score <= 1.0


class TestStrategies:
    def test_usage_question_answers_in_both(self):
        issue = make_issue(real_resolution="ANSWER")
        result_a = run_triage(issue, "answer_first")
        result_b = run_triage(issue, "clarify_first")
        assert result_a.decision == "ANSWER"
        assert result_b.decision == "ANSWER"

    def test_duplicate_text_gives_duplicate(self):
        issue = make_issue(
            title="Duplicate: useEffect runs twice",
            body="Duplicate of #123 in this repo.",
            real_resolution="DUPLICATE",
        )
        result = run_triage(issue, "clarify_first")
        assert result.decision == "DUPLICATE"
        assert result.correct is True

    def test_peer_corpus_duplicate_detection(self):
        peer = make_issue(
            identity="facebook/react#10",
            repo="facebook/react",
            number=10,
            title="StrictMode causes double render in useEffect hook",
        )
        issue = make_issue(
            identity="facebook/react#25",
            repo="facebook/react",
            number=25,
            title="StrictMode double render inside useEffect hook",
            body="Seeing hook run twice during development.",
            real_resolution="DUPLICATE",
        )
        result = run_triage(issue, "clarify_first", corpus=[peer, issue])
        assert result.decision == "DUPLICATE"

    def test_verified_bug_with_trace_escalates_under_clarify_first(self):
        issue = make_issue(
            title="Segfault crash during async socket teardown",
            body=(
                "Expected behavior: clean close.\nActual behavior: crash.\n"
                "```python\nawait server.close()\n```\n"
                "Traceback (most recent call last):\n"
                "  File 'server.py', line 44, in close\nRuntimeError: crash"
            ),
            real_resolution="ESCALATE",
        )
        result = run_triage(issue, "clarify_first")
        assert result.decision == "ESCALATE"
        assert result.correct is True

    def test_minimal_vague_issue_gets_clarify(self):
        issue = make_issue(
            title="Build broken",
            body="fails on my machine",
            real_resolution="CLARIFY",
        )
        result = run_triage(issue, "clarify_first")
        assert result.decision == "CLARIFY"
        assert result.correct is True


class TestDecisionMetrics:
    def test_false_escalation_detected(self):
        issue = make_issue(
            title="Crash traceback on config setting",
            body=(
                "Steps to reproduce:\n```python\nx=1\n```\n"
                "Traceback:\n  File 'a.py', line 1, in <module>\nRuntimeError: crash"
            ),
            real_resolution="ANSWER",
        )
        result = run_triage(issue, "clarify_first")
        assert result.decision == "ESCALATE"
        assert result.false_escalation is True
        assert result.net_value_usd < 0


class TestBayesianAndGuardrails:
    def test_clear_winner_passes_and_recommends_ship(self):
        rows = (
            [{"strategy": "answer_first", "correct": i < 110, "false_answer": False, "false_escalation": False} for i in range(200)]
            + [{"strategy": "clarify_first", "correct": i < 155, "false_answer": False, "false_escalation": False} for i in range(200)]
        )
        res = evaluate_experiment(rows)
        assert res.winning_variant == "clarify_first"
        assert res.win_probability >= 0.90
        assert res.guardrail_status == "PASS"
        assert res.recommendation == "SHIP_WINNER"

    def test_high_false_answer_rate_triggers_safety_rollback(self):
        rows = (
            [{"strategy": "answer_first", "correct": i < 100, "false_answer": False, "false_escalation": False} for i in range(200)]
            + [{"strategy": "clarify_first", "correct": i < 160, "false_answer": i < 60, "false_escalation": False} for i in range(200)]
        )
        res = evaluate_experiment(rows)
        assert res.guardrail_status == "BLOCKED"
        assert res.recommendation == "ROLLBACK_SAFETY"

    def test_small_sample_or_tiny_lift_blocks_rollout(self):
        small_rows = (
            [{"strategy": "answer_first", "correct": False, "false_answer": False, "false_escalation": False} for _ in range(5)]
            + [{"strategy": "clarify_first", "correct": True, "false_answer": False, "false_escalation": False} for _ in range(5)]
        )
        res_small = evaluate_experiment(small_rows, min_sample_per_arm=100)
        assert res_small.recommendation == "GATHER_MORE_DATA"

        tiny_lift_rows = (
            [{"strategy": "answer_first", "correct": i < 140, "false_answer": False, "false_escalation": False} for i in range(200)]
            + [{"strategy": "clarify_first", "correct": i < 143, "false_answer": False, "false_escalation": False} for i in range(200)]
        )
        res_tiny = evaluate_experiment(tiny_lift_rows, min_practical_lift=0.03)
        assert res_tiny.recommendation == "GATHER_MORE_DATA"

    def test_loo_fragility_prevents_shipping(self):
        # Single dominant repo drives overall lift while another flips direction
        rows = (
            [{"repo": "repo/a", "strategy": "answer_first", "correct": False} for _ in range(90)]
            + [{"repo": "repo/a", "strategy": "clarify_first", "correct": True} for _ in range(90)]
            + [{"repo": "repo/b", "strategy": "answer_first", "correct": True} for _ in range(50)]
            + [{"repo": "repo/b", "strategy": "clarify_first", "correct": i < 5} for i in range(50)]
        )
        res = evaluate_experiment(rows, min_sample_per_arm=50, require_loo_robust=True)
        assert res.loo_robust is False
        assert res.recommendation == "GATHER_MORE_DATA"

    def test_loo_full_gate_checks_lift_and_safety_not_only_posterior(self):
        # Case A: Dropping repo/a still yields P(win) > 0.90 on repo/b,
        # but practical lift drops below min_practical_lift (2.5pp < 3.0pp).
        rows_lift = (
            [{"repo": "repo/a", "strategy": "answer_first", "correct": i < 100} for i in range(200)]
            + [{"repo": "repo/a", "strategy": "clarify_first", "correct": i < 160} for i in range(200)]
            + [{"repo": "repo/b", "strategy": "answer_first", "correct": i < 1400} for i in range(2000)]
            + [{"repo": "repo/b", "strategy": "clarify_first", "correct": i < 1450} for i in range(2000)]
        )
        res_lift = evaluate_experiment(
            rows_lift, win_threshold=0.90, min_sample_per_arm=100, min_practical_lift=0.03, require_loo_robust=True
        )
        assert res_lift.loo_robust is False
        assert res_lift.recommendation == "GATHER_MORE_DATA"

        # Case B: Aggregate false_answer <= 20%, but dropping repo/a leaves repo/b above 20% safety cap.
        rows_safety = (
            [{"repo": "repo/a", "strategy": "answer_first", "correct": i < 90, "false_answer": False} for i in range(150)]
            + [{"repo": "repo/a", "strategy": "clarify_first", "correct": i < 135, "false_answer": False} for i in range(150)]
            + [{"repo": "repo/b", "strategy": "answer_first", "correct": i < 90, "false_answer": False} for i in range(150)]
            + [{"repo": "repo/b", "strategy": "clarify_first", "correct": i < 135, "false_answer": i < 36} for i in range(150)]
        )
        res_safety = evaluate_experiment(
            rows_safety, win_threshold=0.90, min_sample_per_arm=100, max_false_answer_rate=0.20, require_loo_robust=True
        )
        assert res_safety.loo_robust is False
        assert res_safety.recommendation == "GATHER_MORE_DATA"
        assert "ROLLBACK_SAFETY" in "; ".join(res_safety.loo_notes)

    def test_safety_rollback_blocks_even_when_winning_and_marks_blocked(self):
        # High false_escalation triggers ROLLBACK_SAFETY + BLOCKED even with 100% accuracy
        fe_rows = (
            [{"strategy": "answer_first", "correct": False, "false_answer": False, "false_escalation": False} for _ in range(120)]
            + [{"strategy": "clarify_first", "correct": True, "false_answer": False, "false_escalation": True} for _ in range(120)]
        )
        res = evaluate_experiment(fe_rows, min_sample_per_arm=100, max_false_escalation_rate=0.35)
        assert res.recommendation == "ROLLBACK_SAFETY"
        assert res.guardrail_status == "BLOCKED"
        assert res.winning_variant == "inconclusive"

    def test_unknown_excluded_from_evaluate_experiment(self):
        rows = (
            [{"strategy": "answer_first", "correct": False, "real_resolution": "CLARIFY"} for _ in range(110)]
            + [{"strategy": "clarify_first", "correct": True, "real_resolution": "CLARIFY"} for _ in range(110)]
            + [{"strategy": "clarify_first", "correct": False, "false_answer": True, "real_resolution": "UNKNOWN"} for _ in range(40)]
        )
        res = evaluate_experiment(rows, min_sample_per_arm=100)
        assert res.n_a == 110
        assert res.n_b == 110
        assert res.k_b == 110
        assert res.recommendation == "SHIP_WINNER"
        assert res.loo_evaluated is False
        assert "NOT VERIFIED" in res.loo_summary

    def test_loo_runs_independently_even_when_aggregate_triggers_rollback_safety(self):
        # Even when aggregate triggers ROLLBACK_SAFETY, all repo-exclusion folds must run
        # and distinguish statistical fragility from safety failure rather than reporting PASS.
        rows = (
            [{"repo": "repo/a", "strategy": "answer_first", "correct": False, "false_answer": False} for _ in range(60)]
            + [{"repo": "repo/a", "strategy": "clarify_first", "correct": True, "false_answer": True} for _ in range(60)]
            + [{"repo": "repo/b", "strategy": "answer_first", "correct": i < 30, "false_answer": False} for i in range(60)]
            + [{"repo": "repo/b", "strategy": "clarify_first", "correct": i < 32, "false_answer": True} for i in range(60)]
        )
        res = evaluate_experiment(rows, min_sample_per_arm=50, win_threshold=0.90)
        assert res.recommendation == "ROLLBACK_SAFETY"
        assert res.loo_evaluated is True
        assert res.loo_robust is False
        assert res.loo_stat_robust is False
        assert res.loo_safety_pass is False
        assert len(res.loo_folds) == 2
        assert "NOT" not in res.loo_summary.upper() or "not robust" in res.loo_summary
        assert "Safety eligibility fails" in res.loo_summary


class TestJudgeScenarios:
    def test_ambiguous_report_triggers_refusal_guardrail(self):
        issue = make_issue(
            identity="demo/vercel/next.js#402",
            title="Build broken after update",
            body="Our CI pipeline keeps failing since this morning and nothing builds cleanly anymore.",
            real_resolution="CLARIFY",
        )
        res = run_triage(issue, "clarify_first")
        assert res.decision == "CLARIFY"
        assert any("Guardrail triggered" in item for item in res.evidence)

    def test_sigsegv_corpus_duplicate_and_signatures(self):
        peer = make_issue(
            identity="python/cpython#101",
            repo="python/cpython",
            number=101,
            title="Regression: asyncio event loop crash on shutdown with open sockets",
        )
        dup_issue = make_issue(
            identity="python/cpython#9991",
            repo="python/cpython",
            number=9991,
            title="Regression: asyncio event loop crash on shutdown with open sockets",
            body="Seeing RuntimeError and SIGSEGV with HTTP 503 during shutdown.",
            real_resolution="DUPLICATE",
        )
        res = run_triage(dup_issue, "clarify_first", corpus=[peer, dup_issue])
        assert res.decision == "DUPLICATE"
        assert any("Normalized error signatures" in item for item in res.evidence)
        assert any("Similar prior issue python/cpython#101" in item for item in res.evidence)
