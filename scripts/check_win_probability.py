"""
FlagIQ — Round 04 Deployment Eligibility Gate & Reconciliation Checker
Evaluates Bayesian posterior win_probability, practical lift, minimum sample size,
LOO repository robustness, safety circuit-breakers, and local Flagsmith reconciliation.

Usage:
    python3 scripts/check_win_probability.py
"""
from __future__ import annotations
import json
import os
import sys
from pathlib import Path
import yaml

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

sys.path.insert(0, str(Path(__file__).parent.parent))
from flagiq.bayesian import evaluate_experiment

console = Console()

ENV_KEY   = os.environ.get("FLAGSMITH_ENV_KEY", "")
FLAG_NAME = os.environ.get("FLAGSMITH_FLAG_NAME", "triage_strategy")
VARIANT_A = os.environ.get("FLAGSMITH_VARIANT_A", "answer_first")
VARIANT_B = os.environ.get("FLAGSMITH_VARIANT_B", "clarify_first")
POLICY_PATH = Path("config/policy.yaml")


def main():
    policy = yaml.safe_load(POLICY_PATH.read_text())
    gate = policy.get("eligibility_gate", {})
    threshold = float(os.environ.get("WIN_PROBABILITY_THRESHOLD", gate.get("win_probability_threshold", 0.90)))

    console.print(Panel.fit(
        "[bold cyan]FlagIQ — Round 04 Deployment Eligibility Gate[/bold cyan]\n\n"
        "Flagsmith Dashboard:\n"
        "[link]https://app.flagsmith.com[/link]\n"
        "Navigate to: Experiments → AI Triage Strategy Experiment\n\n"
        f"Requirements for SHIP_WINNER:\n"
        f"  1. P(win) ≥ {threshold:.0%}   2. Lift ≥ {float(gate.get('min_practical_lift', 0.03)):.0%}\n"
        f"  3. n ≥ {int(gate.get('min_sample_per_arm', 100))}/arm  4. Guardrails PASS & LOO Robust",
        title="Round 04 — Ship or Hold",
    ))

    results_path = Path("fixtures/real_issues/results.json")
    if not results_path.exists():
        console.print("\n[dim]No fixtures/real_issues/results.json found — run python3 scripts/run_experiment.py first[/dim]")
        return

    results = json.loads(results_path.read_text())
    bayes = evaluate_experiment(
        results,
        variant_a=VARIANT_A,
        variant_b=VARIANT_B,
        win_threshold=threshold,
        max_false_answer_rate=float(gate.get("max_false_answer_rate", 0.20)),
        max_false_escalation_rate=float(gate.get("max_false_escalation_rate", 0.35)),
        min_sample_per_arm=int(gate.get("min_sample_per_arm", 100)),
        min_practical_lift=float(gate.get("min_practical_lift", 0.03)),
        require_loo_robust=bool(gate.get("require_loo_robust", True)),
    )

    a = [r for r in results if r["strategy"] == VARIANT_A]
    b = [r for r in results if r["strategy"] == VARIANT_B]
    net_a = sum(r.get("net_value_usd", 0.0) for r in a)
    net_b = sum(r.get("net_value_usd", 0.0) for r in b)

    console.print("\n[bold]Reconciliation Denominators (verify against Flagsmith UI):[/bold]")
    console.print(f"  {VARIANT_A}: {bayes.k_a}/{bayes.n_a} ({bayes.mean_a:.1%}, 95% CI {bayes.ci_a[0]:.1%}–{bayes.ci_a[1]:.1%}) | net: ${net_a:+,.0f}")
    console.print(f"  {VARIANT_B}: {bayes.k_b}/{bayes.n_b} ({bayes.mean_b:.1%}, 95% CI {bayes.ci_b[0]:.1%}–{bayes.ci_b[1]:.1%}) | net: ${net_b:+,.0f}")
    console.print(f"  P({VARIANT_B} > {VARIANT_A}): [bold]{bayes.prob_b_beats_a:.1%}[/bold] | Abs Diff: [bold]{bayes.absolute_diff_pct:.1f}pp[/bold] | Rel Lift: [bold]{bayes.relative_lift_pct:+.1f}%[/bold]")

    if bayes.repo_breakdown:
        t = Table(title="Per-Repository Accuracy Breakdown", show_header=True)
        t.add_column("Repo")
        t.add_column(VARIANT_A, justify="right")
        t.add_column(VARIANT_B, justify="right")
        for repo, stats in bayes.repo_breakdown.items():
            a_str = f"{stats['acc_a']:.1f}% (n={stats['n_a']})" if stats["acc_a"] is not None else "n/a"
            b_str = f"{stats['acc_b']:.1f}% (n={stats['n_b']})" if stats["acc_b"] is not None else "n/a"
            t.add_row(repo, a_str, b_str)
        console.print(t)

    loo_label = "NOT VERIFIED" if not bayes.loo_evaluated else ("PASS" if bayes.loo_robust else "FAIL")
    console.print(f"  LOO Status: [bold]{loo_label}[/bold] — {bayes.loo_summary}")
    for repo, f in bayes.loo_folds.items():
        console.print(
            f"    - Excluding {repo}: P={f['prob_b_beats_a_pct']:.1f}% "
            f"({'stat ≥90%' if f['stat_pass'] else 'stat <90%'}), "
            f"false_answer={f['false_answer_pct']:.1f}% ({'safety PASS' if f['safety_pass'] else 'safety FAIL'}) "
            f"-> {f['recommendation']}"
        )
    stat_leader = VARIANT_B if bayes.prob_b_beats_a >= threshold else (VARIANT_A if bayes.prob_a_beats_b >= threshold else "inconclusive")
    avg_a = round(net_a / len(a), 2) if a else 0.0
    avg_b = round(net_b / len(b), 2) if b else 0.0
    econ_leader = VARIANT_B if avg_b >= avg_a else VARIANT_A
    diff_per_ticket = abs(avg_b - avg_a)
    console.print(f"  1. Deployment Decision: [bold red]{bayes.recommendation}[/bold red] ({bayes.guardrail_status}) — {'; '.join(bayes.guardrail_reasons)}")
    console.print(f"  2. Statistical Accuracy Winner: [bold]{stat_leader}[/bold] (P={bayes.prob_b_beats_a:.1%}; deployment winner={bayes.winning_variant})")
    console.print(f"  3. Economic Preference (Modeled Only): [bold]{econ_leader}[/bold] (+${diff_per_ticket:.2f}/ticket = ${round(diff_per_ticket * 2000):,}/month @ 2,000/mo projected under cost assumptions; NOT verified savings, blocked from deploy)")


if __name__ == "__main__":
    main()
