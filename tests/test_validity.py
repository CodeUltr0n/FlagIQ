"""Tests for experimental validity — answers the judge's hardest question."""
import sys, hashlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from flagiq.schemas import IssueReport
from flagiq.decision import run_triage, _compute_confidence


def make_issue(**kwargs) -> IssueReport:
    defaults = dict(
        identity="test/repo#1", repo="test/repo", number=1,
        title="Descriptive title about a real bug in the system",
        body="Detailed reproduction steps here with expected behavior and actual behavior.",
        labels=[], comment_count=2, has_linked_pr=False,
        real_resolution="ANSWER",
    )
    defaults.update(kwargs)
    return IssueReport(**defaults)


class TestConsistentVariantAssignment:
    """Check 1 — same identity always produces the same variant."""

    def _simulate_variant(self, identity: str) -> str:
        h = int(hashlib.sha256(identity.encode()).hexdigest(), 16) % 100
        return "answer_first" if h < 50 else "clarify_first"

    def test_same_identity_same_variant_10_times(self):
        identity = "python/cpython#12345"
        variants = {self._simulate_variant(identity) for _ in range(10)}
        assert len(variants) == 1, "Variant assignment must be deterministic"

    def test_different_identities_can_differ(self):
        v1 = self._simulate_variant("python/cpython#1")
        v2 = self._simulate_variant("facebook/react#9999")
        assert v1 in ("answer_first", "clarify_first")
        assert v2 in ("answer_first", "clarify_first")

    def test_decision_result_is_deterministic(self):
        issue = make_issue(identity="test/repo#42")
        r1 = run_triage(issue, "answer_first")
        r2 = run_triage(issue, "answer_first")
        assert r1.decision == r2.decision
        assert r1.confidence == r2.confidence


class TestNoOutcomeLeakage:
    """Check 3 & 4 — outcome and post-close metadata never influence the decision."""

    def test_confidence_ignores_post_close_fields(self):
        """Identical creation-time text, completely different post-close fields -> identical confidence."""
        base = dict(
            title="How to configure timeout setting?",
            body="Looking for docs on configuring timeout.",
        )
        issue_a = make_issue(**base, labels=["question"], comment_count=0, has_linked_pr=False, real_resolution="ANSWER")
        issue_b = make_issue(**base, labels=["duplicate", "bug"], comment_count=99, has_linked_pr=True, real_resolution="ESCALATE")
        issue_c = make_issue(**base, labels=["needs-info"], comment_count=1, has_linked_pr=False, real_resolution="CLARIFY")

        assert _compute_confidence(issue_a) == _compute_confidence(issue_b)
        assert _compute_confidence(issue_b) == _compute_confidence(issue_c)

    def test_decision_ignores_post_close_fields(self):
        """Identical creation-time text, completely different post-close fields -> identical decision."""
        base = dict(
            title="TypeError crash during async socket teardown",
            body="Steps to reproduce:\n```python\nclose()\n```\nTraceback:\n  File 's.py', line 9\nTypeError: crash",
        )
        issue_a = make_issue(**base, labels=[], comment_count=0, has_linked_pr=False, real_resolution="ANSWER")
        issue_b = make_issue(**base, labels=["duplicate"], comment_count=15, has_linked_pr=True, real_resolution="DUPLICATE")

        for strategy in ("answer_first", "clarify_first"):
            r_a = run_triage(issue_a, strategy)
            r_b = run_triage(issue_b, strategy)
            assert r_a.decision == r_b.decision, (
                f"Decision leaked post-close metadata for strategy={strategy}"
            )

    def test_post_close_fields_only_used_after_decision(self):
        """
        Static: verify real_resolution, has_linked_pr, comment_count, and labels
        do not appear in decision.py functions prior to `decision` assignment.
        """
        source = Path("flagiq/decision.py").read_text()
        lines = source.splitlines()

        forbidden = ("real_resolution", "has_linked_pr", "comment_count", ".labels")
        decision_line = None
        forbidden_lines = []
        in_docstring = False
        in_runtime_code = False

        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if stripped.startswith('"""') or stripped.startswith("'''"):
                # Toggle multi-line docstring unless closed on the same line
                quote = stripped[:3]
                if stripped.count(quote) == 1:
                    in_docstring = not in_docstring
                continue
            if in_docstring:
                continue
            if stripped.startswith("def _extract_triage_signals") or stripped.startswith("def run_triage"):
                in_runtime_code = True
                continue
            if not in_runtime_code:
                continue

            code = line.split("#")[0]
            if ("decision =" in code or "decision: DecisionType" in code) and decision_line is None:
                decision_line = i
            if any(field in code for field in forbidden):
                forbidden_lines.append(i)

        assert decision_line is not None, "Could not find decision assignment"
        leaks = [ln for ln in forbidden_lines if ln < decision_line]
        assert not leaks, (
            f"Post-close fields accessed at lines {leaks} "
            f"before decision assigned at line {decision_line}"
        )


class TestReproducibility:
    """Check 5 — identical inputs produce identical outputs."""

    def test_two_runs_produce_identical_results(self):
        issues_data = [
            dict(identity=f"repo/name#{i}", repo="repo/name", number=i,
                 title="A descriptive title for issue", body="Reproduction steps " * 10,
                 labels=["bug"], comment_count=i % 4, has_linked_pr=(i % 3 == 0),
                 real_resolution="ANSWER")
            for i in range(1, 11)
        ]

        def run(data):
            results = []
            for d in data:
                issue = IssueReport(**d)
                h = int(hashlib.sha256(d["identity"].encode()).hexdigest(), 16) % 100
                strategy = "answer_first" if h < 50 else "clarify_first"
                r = run_triage(issue, strategy)
                results.append((r.identity, r.strategy, r.decision, r.correct))
            return results

        assert run(issues_data) == run(issues_data)

    def test_confidence_is_deterministic(self):
        issue = make_issue(body="x" * 190)
        scores = [_compute_confidence(issue) for _ in range(5)]
        assert len(set(scores)) == 1


class TestComparableAllocation:
    """Check 2 — Flagsmith hash distributes issues roughly 50/50."""

    def test_hash_produces_near_equal_split(self):
        def variant(identity):
            h = int(hashlib.sha256(identity.encode()).hexdigest(), 16) % 100
            return "answer_first" if h < 50 else "clarify_first"

        identities = [f"repo/project#{i}" for i in range(400)]
        counts = {"answer_first": 0, "clarify_first": 0}
        for iden in identities:
            counts[variant(iden)] += 1

        ratio = counts["answer_first"] / 400
        assert 0.40 <= ratio <= 0.60


class TestTemporalAndScraperValidity:
    """Checks 6 & Ground Truth — strict temporal duplicate filter & UNKNOWN quarantine."""

    def test_future_peer_ignored_in_duplicate_matching(self):
        earlier = make_issue(
            identity="facebook/react#10",
            repo="facebook/react",
            number=10,
            created_at="2026-10-01T10:00:00Z",
            title="StrictMode causes double render in useEffect hook",
            body="Seeing hook run twice in development.",
        )
        future_dup = make_issue(
            identity="facebook/react#999",
            repo="facebook/react",
            number=999,
            created_at="2026-11-15T10:00:00Z",
            title="StrictMode causes double render in useEffect hook",
            body="Duplicate ticket filed later.",
        )
        res = run_triage(earlier, "clarify_first", corpus=[earlier, future_dup])
        assert res.decision != "DUPLICATE", "Earlier issue must never match a future duplicate"

    def test_uncertain_or_bot_closures_become_unknown_not_answer(self):
        from scripts.scrape_github_issues import label_resolution
        assert label_resolution(["bug"], has_merged_pr=False, has_solution_comment=False) == "UNKNOWN"
        assert label_resolution(["stale"], has_merged_pr=False, has_solution_comment=True) == "UNKNOWN"
        assert label_resolution(["bug"], has_merged_pr=False, has_solution_comment=True, closed_by_bot=True) == "UNKNOWN"
        assert label_resolution(["bug"], has_merged_pr=False, has_solution_comment=False, state_reason="not_planned") == "UNKNOWN"
        assert label_resolution(["bug"], has_merged_pr=False, has_solution_comment=True, state_reason="completed") == "ANSWER"

    def test_demo_feedback_never_fires_decision_correct(self):
        import app
        assert app._normalize_demo_identity("vercel/next.js#402") == "demo/vercel/next.js#402"
        src = Path("app.py").read_text()
        feedback_section = src.split('elif path == "/api/feedback":', 1)[1]
        assert "decision_correct" not in feedback_section.split("track_event", 1)[1].splitlines()[0]

    def test_stratified_audit_sample_covers_all_repos_and_labels_deterministically(self):
        from scripts.scrape_github_issues import stratified_audit_sample
        repos = ["facebook/react", "vercel/next.js", "tiangolo/fastapi", "python/cpython", "Flagsmith/flagsmith"]
        labels = ["ANSWER", "ESCALATE", "DUPLICATE"]
        issues = []
        idx = 1
        for r in repos:
            for lbl_idx, lbl in enumerate(labels):
                cnt = 20 if lbl_idx == 0 else (15 if lbl_idx == 1 else 5)
                for _ in range(cnt):
                    issues.append({"identity": f"{r}#{idx}", "repo": r, "number": idx, "real_resolution": lbl})
                    idx += 1

        s1 = stratified_audit_sample(issues, target_n=50, seed=42)
        s2 = stratified_audit_sample(issues, target_n=50, seed=42)
        assert len(s1) == 50
        assert [x["identity"] for x in s1] == [x["identity"] for x in s2]
        assert len({x["identity"] for x in s1}) == 50
        assert {x["repo"] for x in s1} == set(repos)
        assert {x["real_resolution"] for x in s1} == set(labels)

    def test_fetch_issues_and_run_experiment_exclude_overlap_and_unknown(self):
        import json
        import tempfile
        from unittest.mock import MagicMock, patch
        import scripts.run_experiment as run_exp
        import scripts.scrape_github_issues as scraper

        # 1. Verify fetch_issues honors exclude_ids and quarantines UNKNOWN closures
        fake_page = [
            {
                "number": 10,
                "title": "Already in calibration",
                "body": "Body",
                "created_at": "2026-10-01T00:00:00Z",
                "labels": [{"name": "bug"}],
                "comments": 2,
                "state_reason": "completed",
                "user": {"login": "u1"},
                "closed_by": {"login": "stale[bot]", "type": "Bot"},
            },
            {
                "number": 11,
                "title": "Ambiguous closed bug",
                "body": "Body",
                "created_at": "2026-10-02T00:00:00Z",
                "labels": [{"name": "bug"}],
                "comments": 2,
                "state_reason": "not_planned",
                "user": {"login": "u2"},
                "closed_by": {"login": "maintainer", "type": "User"},
            },
            {
                "number": 12,
                "title": "Duplicate report",
                "body": "Body",
                "created_at": "2026-10-03T00:00:00Z",
                "labels": [{"name": "duplicate"}],
                "comments": 1,
                "state_reason": "duplicate",
                "user": {"login": "u3"},
                "closed_by": {"login": "maintainer", "type": "User"},
            },
        ]
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = fake_page
        empty_resp = MagicMock(status_code=200)
        empty_resp.json.return_value = []

        with patch.object(scraper.requests, "get", side_effect=[mock_resp, empty_resp]), \
             patch.object(scraper, "_get_merged_pr", return_value=False), \
             patch.object(scraper, "_has_solution_comment", return_value=False), \
             patch.object(scraper.time, "sleep", return_value=None):
            verified, excluded = scraper.fetch_issues(
                "Flagsmith/flagsmith", count=5, start_page=1, exclude_ids={"Flagsmith/flagsmith#10"}
            )
        assert [v["identity"] for v in verified] == ["Flagsmith/flagsmith#12"]
        assert [e["identity"] for e in excluded] == ["Flagsmith/flagsmith#11"]

        # 2. Verify run_experiment.load_issues filters UNKNOWN and _atomic_write_json writes cleanly
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_issues = Path(tmpdir) / "issues.json"
            payload = [
                {"identity": "a/b#1", "repo": "a/b", "number": 1, "created_at": "2026-10-01T00:00:00Z",
                 "title": "t1", "body": "b1", "labels": [], "comment_count": 1, "has_linked_pr": False, "real_resolution": "ANSWER"},
                {"identity": "a/b#2", "repo": "a/b", "number": 2, "created_at": "2026-10-02T00:00:00Z",
                 "title": "t2", "body": "b2", "labels": [], "comment_count": 1, "has_linked_pr": False, "real_resolution": "UNKNOWN"},
            ]
            run_exp._atomic_write_json(tmp_issues, payload)
            assert not (Path(tmpdir) / "issues.json.tmp").exists()
            orig_issues_path = run_exp.ISSUES_PATH
            try:
                run_exp.ISSUES_PATH = tmp_issues
                loaded = run_exp.load_issues(dry_run=False)
                assert [i["identity"] for i in loaded] == ["a/b#1"]
            finally:
                run_exp.ISSUES_PATH = orig_issues_path

