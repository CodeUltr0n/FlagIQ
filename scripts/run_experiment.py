"""
FlagIQ — Experiment Runner
Runs the A/B experiment: reads Flagsmith flag variant per issue,
applies the corresponding strategy, records exposure and decision_correct event.

Flagsmith Experimentation Alignment (Python SDK >= 6.3.0):
  - Initializes Flagsmith with enable_events=True & EventProcessorConfig(max_buffer_items=1)
  - Calls get_experiment_flag(FLAG_NAME, identifier=identity) to evaluate & record exposure
  - Tracks `decision_correct` ONLY when result.correct == True (required for occurrence metrics!)
  - Saves processed.json & results.json per issue in live mode for crash-safe --resume
  - Calls flush_events() before exit

Usage:
    python3 scripts/run_experiment.py --local-preview  # run BOTH arms locally (disagreement + per-repo report)
    python3 scripts/run_experiment.py --dry-run        # test on 5 issues, no events pushed
    python3 scripts/run_experiment.py                  # full live Flagsmith run
    python3 scripts/run_experiment.py --resume         # resume cleanly from processed.json
"""
from __future__ import annotations
import argparse
from collections import defaultdict
import json
import os
import sys
import time
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv():
        return False

try:
    from flagsmith import Flagsmith
except ImportError:
    Flagsmith = None

from rich.console import Console
from rich.table import Table
from tqdm import tqdm

sys.path.insert(0, str(Path(__file__).parent.parent))
from flagiq.schemas import IssueReport
from flagiq.decision import run_triage

load_dotenv()
console = Console()

ISSUES_PATH      = Path("fixtures/real_issues/issues.json")
CALIBRATION_PATH = Path("fixtures/real_issues/calibration_issues.json")
PROCESSED_PATH   = Path("fixtures/real_issues/processed.json")
RESULTS_PATH     = Path("fixtures/real_issues/results.json")

FLAG_NAME     = os.environ.get("FLAGSMITH_FLAG_NAME", "triage_strategy")
ENV_KEY       = os.environ.get("FLAGSMITH_ENV_KEY", "")
VARIANT_A     = os.environ.get("FLAGSMITH_VARIANT_A", "answer_first")
VARIANT_B     = os.environ.get("FLAGSMITH_VARIANT_B", "clarify_first")
API_SLEEP     = float(os.environ.get("API_SLEEP", "0.05"))


def _sample_issues() -> list[dict]:
    return [
        {
            "identity": "python/cpython#101",
            "repo": "python/cpython",
            "number": 101,
            "created_at": "2026-10-01T10:00:00Z",
            "state_reason": "completed",
            "title": "Regression: asyncio event loop crash on shutdown with open sockets",
            "body": (
                "Steps to reproduce on Python 3.12 macOS:\n"
                "1. Open client socket\n2. Close loop.\n"
                "```python\nloop.run_until_complete(main())\n```\n"
                "Traceback (most recent call last):\n"
                "  File \"asyncio/base_events.py\", line 640, in close\n"
                "RuntimeError: Event loop crash during shutdown."
            ),
            "labels": ["bug"],
            "comment_count": 3,
            "has_linked_pr": True,
            "real_resolution": "ESCALATE",
        },
        {
            "identity": "facebook/react#202",
            "repo": "facebook/react",
            "number": 202,
            "created_at": "2026-10-05T12:00:00Z",
            "state_reason": "duplicate",
            "title": "Duplicate: useEffect firing twice in StrictMode",
            "body": "Duplicate of #200. useEffect runs two times in dev mode.",
            "labels": ["duplicate"],
            "comment_count": 2,
            "has_linked_pr": False,
            "real_resolution": "DUPLICATE",
        },
        {
            "identity": "microsoft/vscode#303",
            "repo": "microsoft/vscode",
            "number": 303,
            "created_at": "2026-10-10T14:00:00Z",
            "state_reason": "completed",
            "title": "How to configure python interpreter path in settings.json?",
            "body": "Looking for the right setting option in docs or config guide to specify a venv path in workspace settings.",
            "labels": ["question"],
            "comment_count": 4,
            "has_linked_pr": False,
            "real_resolution": "ANSWER",
        },
        {
            "identity": "vercel/next.js#401",
            "repo": "vercel/next.js",
            "number": 401,
            "created_at": "2026-10-15T16:00:00Z",
            "state_reason": "not_planned",
            "title": "Build broken after update",
            "body": "Our CI pipeline keeps failing since this morning and nothing builds cleanly anymore.",
            "labels": ["needs-info"],
            "comment_count": 1,
            "has_linked_pr": False,
            "real_resolution": "CLARIFY",
        },
        {
            "identity": "flagsmith/flagsmith#505",
            "repo": "flagsmith/flagsmith",
            "number": 505,
            "created_at": "2026-10-20T18:00:00Z",
            "state_reason": "completed",
            "title": "TypeError crash when event processor flushes empty batch",
            "body": (
                "Expected behavior: flush cleanly.\nActual behavior: TypeError.\n"
                "Version: Python 3.11, SDK 6.3.0\n"
                "Traceback (most recent call last):\n"
                "  File \"flagsmith/analytics.py\", line 88, in flush\n"
                "TypeError: cannot serialize NoneType."
            ),
            "labels": ["bug"],
            "comment_count": 5,
            "has_linked_pr": True,
            "real_resolution": "ESCALATE",
        },
    ]


def load_issues(dry_run: bool, use_calibration: bool = False) -> list[dict]:
    path = CALIBRATION_PATH if (use_calibration and CALIBRATION_PATH.exists()) else ISSUES_PATH
    if not path.exists():
        if dry_run:
            console.print("[dim]ℹ  issues.json not found yet — using 5 built-in sample issues for dry-run verification.[/dim]")
            return _sample_issues()
        console.print(f"[red]✗ {path} not found. Run scripts/scrape_github_issues.py first.[/red]")
        sys.exit(1)
    with open(path) as f:
        raw = json.load(f)
    # Exclude UNKNOWN ground truth from all experimental denominators & evaluations
    issues = [i for i in raw if i.get("real_resolution") != "UNKNOWN"]
    issues.sort(key=lambda x: (x.get("created_at", ""), x.get("number", 0)))
    return issues[:5] if dry_run else issues


def _atomic_write_json(path: Path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w") as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp, path)


def load_processed() -> set[str]:
    if PROCESSED_PATH.exists():
        with open(PROCESSED_PATH) as f:
            return set(json.load(f))
    return set()


def save_processed(processed: set[str]):
    _atomic_write_json(PROCESSED_PATH, sorted(processed))


def create_flagsmith_client(env_key: str):
    if Flagsmith is None:
        console.print("[red]✗ flagsmith package not installed. Run: pip install -r requirements.txt[/red]")
        sys.exit(1)
    init_kwargs: dict = {"environment_key": env_key}
    try:
        from flagsmith.models import EventProcessorConfig
        init_kwargs["enable_events"] = True
        init_kwargs["event_processor_config"] = EventProcessorConfig(max_buffer_items=1)
    except (ImportError, TypeError):
        try:
            return Flagsmith(environment_key=env_key, enable_events=True)
        except TypeError:
            pass
    return Flagsmith(**init_kwargs)


def get_variant_for_identity(fs, identity: str, index: int, dry_run: bool) -> str:
    if dry_run:
        return VARIANT_A if index % 2 == 0 else VARIANT_B
    if hasattr(fs, "get_experiment_flag"):
        flag = fs.get_experiment_flag(FLAG_NAME, identifier=identity)
        val = getattr(flag, "value", None) if flag else None
    else:
        flags = fs.get_identity_flags(identity)
        if hasattr(fs, "track_exposure"):
            fs.track_exposure(identifier=identity, feature_name=FLAG_NAME)
        val = flags.get_value(FLAG_NAME)

    if not val or val not in (VARIANT_A, VARIANT_B):
        raise RuntimeError(
            f"Flagsmith returned invalid/empty value {val!r} for flag '{FLAG_NAME}' "
            f"(identity='{identity}'). Verify flag enabled state & variation keys."
        )
    return val


def record_occurrence_event(fs, identity: str, event_name: str, dry_run: bool):
    """
    Push an occurrence conversion event to Flagsmith.
    IMPORTANT: For Flagsmith 'occurrence' metrics, only invoke this when the
    event ACTUALLY succeeded (never send value=0 for failures).
    """
    if dry_run:
        console.print(f"  [dim][dry-run] track_event('{event_name}', identifier='{identity}')[/dim]")
        return
    if hasattr(fs, "track_event"):
        try:
            fs.track_event(event_name, identifier=identity)
            return
        except TypeError:
            fs.track_event(event_name=event_name, identifier=identity)
            return
    if hasattr(fs, "identify"):
        fs.identify(identity).track_event(event_name, 1)


def run_local_preview(issues: list[dict]):
    """
    Evaluate BOTH strategies across every issue locally (paired comparison) before locking policy.yaml.
    Reports class distribution, disagreement rate, per-repo accuracy, and cost-weighted utility.
    """
    corpus = [IssueReport(**d) for d in issues]
    a_res = [run_triage(IssueReport(**d), VARIANT_A, corpus=corpus) for d in issues]
    b_res = [run_triage(IssueReport(**d), VARIANT_B, corpus=corpus) for d in issues]

    disagree = sum(1 for a, b in zip(a_res, b_res) if a.decision != b.decision)
    disagree_pct = (disagree / len(issues) * 100.0) if issues else 0.0

    console.print("\n[bold cyan]Paired Local Pre-Experiment Report (Both Policies on Same Issues)[/bold cyan]")
    console.print(f"Issues evaluated: {len(issues)} | Strategy disagreement: [bold]{disagree}/{len(issues)} ({disagree_pct:.1f}%)[/bold]\n")

    def summarize_rows(rows):
        n = len(rows) or 1
        return (
            sum(r.correct for r in rows) / n,
            sum(r.false_escalation for r in rows) / n,
            sum(r.false_answer for r in rows) / n,
            sum(r.net_value_usd for r in rows),
        )

    aa = summarize_rows(a_res)
    bb = summarize_rows(b_res)

    t = Table(title="Paired Strategy Comparison", show_header=True)
    t.add_column("Metric")
    t.add_column(VARIANT_A, style="cyan")
    t.add_column(VARIANT_B, style="magenta")
    t.add_row("Accuracy", f"{aa[0]:.1%}", f"{bb[0]:.1%}")
    t.add_row("False escalation rate", f"{aa[1]:.1%}", f"{bb[1]:.1%}")
    t.add_row("False answer rate", f"{aa[2]:.1%}", f"{bb[2]:.1%}")
    t.add_row("Cost-weighted utility", f"${aa[3]:+,.0f}", f"${bb[3]:+,.0f}")
    console.print(t)

    # Per-repo accuracy
    by_repo_a = defaultdict(list)
    by_repo_b = defaultdict(list)
    for d, ra, rb in zip(issues, a_res, b_res):
        by_repo_a[d["repo"]].append(ra.correct)
        by_repo_b[d["repo"]].append(rb.correct)

    rt = Table(title="Per-Repository Accuracy", show_header=True)
    rt.add_column("Repository")
    rt.add_column("n", justify="right")
    rt.add_column(VARIANT_A, justify="right")
    rt.add_column(VARIANT_B, justify="right")
    for repo in sorted(by_repo_a):
        ga = by_repo_a[repo]
        gb = by_repo_b[repo]
        rt.add_row(repo, str(len(ga)), f"{sum(ga)/len(ga):.1%}", f"{sum(gb)/len(gb):.1%}")
    console.print(rt)


def print_summary(results: list[dict]):
    a = [r for r in results if r["strategy"] == VARIANT_A]
    b = [r for r in results if r["strategy"] == VARIANT_B]

    def rate(group, key): return sum(r[key] for r in group) / len(group) if group else 0.0
    def net(group): return sum(r.get("net_value_usd", 0.0) for r in group)

    table = Table(title="FlagIQ Experiment Results (All Exposed Denominator)", show_header=True)
    table.add_column("Metric")
    table.add_column(f"{VARIANT_A} (A)", style="cyan")
    table.add_column(f"{VARIANT_B} (B)", style="magenta")

    table.add_row("Exposed issues (denominator)", str(len(a)), str(len(b)))
    table.add_row("Decision accuracy", f"{rate(a, 'correct'):.1%}", f"{rate(b, 'correct'):.1%}")
    table.add_row("False escalation rate", f"{rate(a, 'false_escalation'):.1%}", f"{rate(b, 'false_escalation'):.1%}")
    table.add_row("False answer rate", f"{rate(a, 'false_answer'):.1%}", f"{rate(b, 'false_answer'):.1%}")
    table.add_row("Net engineering value", f"${net(a):+,.0f}", f"${net(b):+,.0f}")

    console.print(table)
    conv_a = sum(1 for r in a if r["correct"])
    conv_b = sum(1 for r in b if r["correct"])
    console.print(
        f"[bold]Flagsmith Reconciliation Target:[/bold] "
        f"Total exposures={len(results)} ({len(a)} A, {len(b)} B) | "
        f"Total '{FLAG_NAME}' -> 'decision_correct' conversions={conv_a + conv_b} ({conv_a} A, {conv_b} B)"
    )
    console.print("[dim]Verify Flagsmith Cloud → Experiments → AI Triage Strategy Experiment matches these totals.[/dim]\n")


def main():
    parser = argparse.ArgumentParser(description="FlagIQ experiment runner")
    parser.add_argument("--dry-run", action="store_true",
                        help="Run on 5 issues without pushing events to Flagsmith")
    parser.add_argument("--resume", action="store_true",
                        help="Skip already-processed issues (idempotent re-run)")
    parser.add_argument("--local-preview", action="store_true",
                        help="Run paired local comparison on calibration/issues dataset without Flagsmith")
    args = parser.parse_args()

    if args.local_preview:
        issues = load_issues(dry_run=not ISSUES_PATH.exists() and not CALIBRATION_PATH.exists(), use_calibration=True)
        run_local_preview(issues)
        return

    if not ENV_KEY and not args.dry_run:
        console.print("[red]✗ FLAGSMITH_ENV_KEY not set. Add it to .env[/red]")
        sys.exit(1)

    if not args.dry_run and not args.resume and PROCESSED_PATH.exists():
        console.print("[yellow]⚠ Existing processed.json found without --resume; pass --resume to skip already-exposed issues.[/yellow]")

    fs = create_flagsmith_client(ENV_KEY) if not args.dry_run else None
    issues = load_issues(args.dry_run)
    corpus = [IssueReport(**d) for d in issues]
    processed = load_processed() if args.resume else set()

    results = []
    if RESULTS_PATH.exists() and args.resume:
        with open(RESULTS_PATH) as f:
            results = json.load(f)
        # Reconcile processed set with saved results to avoid duplicate Flagsmith calls
        seen_results = {r["identity"] for r in results}
        processed |= seen_results

    console.print("\n[bold]FlagIQ Experiment Runner[/bold]")
    console.print(f"Issues: {len(issues)} | Dry-run: {args.dry_run} | Resume: {args.resume}\n")

    try:
        for idx, issue_dict in enumerate(tqdm(issues, unit="issue")):
            identity = issue_dict["identity"]
            if identity in processed:
                continue

            issue = IssueReport(**issue_dict)
            strategy = get_variant_for_identity(fs, identity, idx, args.dry_run)
            result = run_triage(issue, strategy, corpus=corpus)

            if result.correct:
                record_occurrence_event(fs, identity, "decision_correct", args.dry_run)
            if result.false_escalation:
                record_occurrence_event(fs, identity, "false_escalation", args.dry_run)
            if result.false_answer:
                record_occurrence_event(fs, identity, "false_answer", args.dry_run)

            results.append({
                "identity": result.identity,
                "repo": issue.repo,
                "strategy": result.strategy,
                "decision": result.decision,
                "confidence": result.confidence,
                "real_resolution": result.real_resolution,
                "correct": result.correct,
                "false_escalation": result.false_escalation,
                "false_answer": result.false_answer,
                "evidence": result.evidence,
                "net_value_usd": result.net_value_usd,
            })
            processed.add(identity)

            # Write results atomically before processed.json so crashes never leave orphan records
            if not args.dry_run:
                _atomic_write_json(RESULTS_PATH, results)
                save_processed(processed)

            time.sleep(API_SLEEP)
    finally:
        if fs and hasattr(fs, "flush_events"):
            try:
                fs.flush_events()
            except Exception as e:
                console.print(f"[yellow]⚠ flush_events warning: {e}[/yellow]")

    console.print(f"\n[green]✅ Experiment complete — {len(results)} issues processed[/green]")
    print_summary(results)


if __name__ == "__main__":
    main()
