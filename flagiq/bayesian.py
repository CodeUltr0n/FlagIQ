"""
FlagIQ — Bayesian Experimentation & Deployment Eligibility Gate

Pure-Python (stdlib math only) Beta-Binomial posterior engine paired with:
  1. Minimum sample-size gate (min_sample_per_arm)
  2. Minimum practical improvement gate (min_practical_lift)
  3. Safety circuit-breakers (max_false_answer_rate, max_false_escalation_rate)
  4. Per-repository breakdown & full-gate Leave-One-Repo-Out (LOO) sensitivity

Outputs one of: SHIP_WINNER | GATHER_MORE_DATA | ROLLBACK_SAFETY
Safety conditions strictly override statistical superiority.
"""
from __future__ import annotations
import math
from collections import defaultdict
from dataclasses import dataclass, field


@dataclass
class BayesianResult:
    n_a: int
    k_a: int
    n_b: int
    k_b: int
    mean_a: float
    mean_b: float
    ci_a: tuple[float, float]
    ci_b: tuple[float, float]
    prob_b_beats_a: float
    prob_a_beats_b: float
    winning_variant: str        # "answer_first" | "clarify_first" | "inconclusive"
    win_probability: float      # max(prob_a_beats_b, prob_b_beats_a)
    relative_lift_pct: float
    absolute_diff_pct: float
    guardrail_status: str       # "PASS" | "WARNING" | "BLOCKED"
    guardrail_reasons: list[str]
    recommendation: str         # "SHIP_WINNER" | "GATHER_MORE_DATA" | "ROLLBACK_SAFETY"
    repo_breakdown: dict[str, dict] = field(default_factory=dict)
    loo_robust: bool = False
    loo_notes: list[str] = field(default_factory=list)
    loo_evaluated: bool = False
    loo_stat_robust: bool = False
    loo_safety_pass: bool = False
    loo_summary: str = ""
    loo_folds: dict[str, dict] = field(default_factory=dict)


def _beta_grid_pmf(alpha: float, beta: float, steps: int = 400) -> list[float]:
    """Numerically stable discrete posterior grid on (0, 1) using log-gamma."""
    log_vals: list[float] = []
    a_m1 = alpha - 1.0
    b_m1 = beta - 1.0
    for i in range(steps):
        x = (i + 0.5) / steps
        lv = a_m1 * math.log(x) + b_m1 * math.log(1.0 - x)
        log_vals.append(lv)
    max_lv = max(log_vals)
    weights = [math.exp(lv - max_lv) for lv in log_vals]
    total = sum(weights)
    return [w / total for w in weights]


def _credible_interval(pmf: list[float], low: float = 0.025, high: float = 0.975) -> tuple[float, float]:
    steps = len(pmf)
    cdf = 0.0
    lo_val = 0.0
    hi_val = 1.0
    found_lo = False
    for i, w in enumerate(pmf):
        cdf += w
        x = (i + 0.5) / steps
        if not found_lo and cdf >= low:
            lo_val = x
            found_lo = True
        if cdf >= high:
            hi_val = x
            break
    return (round(lo_val, 3), round(hi_val, 3))


def _repo_from_row(row: dict) -> str:
    if row.get("repo"):
        return row["repo"]
    ident = row.get("identity", "")
    if ident.startswith("demo/"):
        ident = ident[5:]
    return ident.split("#")[0] if "#" in ident else "unknown"


def _posterior_probs(n_a: int, k_a: int, n_b: int, k_b: int) -> tuple[float, float, list[float], list[float]]:
    pmf_a = _beta_grid_pmf(1.0 + k_a, 1.0 + max(0, n_a - k_a))
    pmf_b = _beta_grid_pmf(1.0 + k_b, 1.0 + max(0, n_b - k_b))
    prob_b_beats_a = 0.0
    cdf_a = 0.0
    for wa, wb in zip(pmf_a, pmf_b):
        prob_b_beats_a += wb * (cdf_a + 0.5 * wa)
        cdf_a += wa
    prob_b_beats_a = min(max(prob_b_beats_a, 0.0), 1.0)
    return prob_b_beats_a, 1.0 - prob_b_beats_a, pmf_a, pmf_b


def _evaluate_fold_eligibility(
    rows: list[dict],
    variant_a: str,
    variant_b: str,
    win_threshold: float,
    max_false_answer_rate: float,
    max_false_escalation_rate: float,
    min_sample_per_arm: int,
    min_practical_lift: float,
) -> dict:
    """Evaluate complete posterior + practical lift + utility + safety rules on one dataset/fold."""
    group_a = [r for r in rows if r["strategy"] == variant_a]
    group_b = [r for r in rows if r["strategy"] == variant_b]
    n_a, k_a = len(group_a), sum(1 for r in group_a if r["correct"])
    n_b, k_b = len(group_b), sum(1 for r in group_b if r["correct"])

    prob_b_beats_a, prob_a_beats_b, pmf_a, pmf_b = _posterior_probs(n_a, k_a, n_b, k_b)
    mean_a = (k_a / n_a) if n_a else 0.0
    mean_b = (k_b / n_b) if n_b else 0.0
    abs_diff = abs(mean_b - mean_a)

    if prob_b_beats_a >= win_threshold:
        stat_leader = variant_b
        win_prob = prob_b_beats_a
        loser_mean = mean_a if mean_a > 0 else 0.001
        rel_lift = (mean_b - mean_a) / loser_mean * 100.0
    elif prob_a_beats_b >= win_threshold:
        stat_leader = variant_a
        win_prob = prob_a_beats_b
        loser_mean = mean_b if mean_b > 0 else 0.001
        rel_lift = (mean_a - mean_b) / loser_mean * 100.0
    else:
        stat_leader = "inconclusive"
        win_prob = max(prob_a_beats_b, prob_b_beats_a)
        base = mean_a if mean_a > 0 else 0.001
        rel_lift = (mean_b - mean_a) / base * 100.0

    leader = variant_b if prob_b_beats_a >= prob_a_beats_b else variant_a
    leader_group = group_b if leader == variant_b else group_a
    other_group = group_a if leader == variant_b else group_b

    fa_rate = (sum(1 for r in leader_group if r.get("false_answer")) / len(leader_group)) if leader_group else 0.0
    fe_rate = (sum(1 for r in leader_group if r.get("false_escalation")) / len(leader_group)) if leader_group else 0.0
    leader_net = sum(r.get("net_value_usd", 0.0) for r in leader_group)
    other_net = sum(r.get("net_value_usd", 0.0) for r in other_group)

    safety_reasons: list[str] = []
    gate_reasons: list[str] = []

    if fa_rate > max_false_answer_rate:
        safety_reasons.append(f"{leader} false_answer rate {fa_rate:.1%} exceeds {max_false_answer_rate:.0%} safety cap")
    if fe_rate > max_false_escalation_rate:
        safety_reasons.append(f"{leader} false_escalation rate {fe_rate:.1%} exceeds {max_false_escalation_rate:.0%} cost cap")

    if min(n_a, n_b) < min_sample_per_arm:
        gate_reasons.append(f"Sample size (n_a={n_a}, n_b={n_b}) below min_sample_per_arm={min_sample_per_arm}")
    if stat_leader == "inconclusive":
        gate_reasons.append(f"win_probability={win_prob:.1%} below {win_threshold:.0%} threshold")
    elif abs_diff < min_practical_lift:
        gate_reasons.append(f"Absolute accuracy lift {abs_diff:.1%} below min_practical_lift={min_practical_lift:.1%}")
    if leader_group and other_group and (leader_net / len(leader_group)) < (other_net / len(other_group)):
        gate_reasons.append(
            f"{leader} has lower per-ticket net utility (${leader_net/len(leader_group):.2f} vs ${other_net/len(other_group):.2f})"
        )

    if safety_reasons:
        rec = "ROLLBACK_SAFETY"
        winner = "inconclusive"
    elif gate_reasons:
        rec = "GATHER_MORE_DATA"
        winner = "inconclusive"
    else:
        rec = "SHIP_WINNER"
        winner = stat_leader

    return {
        "n_a": n_a, "k_a": k_a, "n_b": n_b, "k_b": k_b,
        "mean_a": mean_a, "mean_b": mean_b,
        "pmf_a": pmf_a, "pmf_b": pmf_b,
        "prob_b_beats_a": prob_b_beats_a, "prob_a_beats_b": prob_a_beats_b,
        "stat_leader": stat_leader, "win_prob": win_prob,
        "rel_lift": rel_lift, "abs_diff": abs_diff,
        "fa_rate": fa_rate, "fe_rate": fe_rate,
        "safety_reasons": safety_reasons, "gate_reasons": gate_reasons,
        "recommendation": rec, "winner": winner,
    }


def evaluate_experiment(
    results: list[dict],
    variant_a: str = "answer_first",
    variant_b: str = "clarify_first",
    win_threshold: float = 0.90,
    max_false_answer_rate: float = 0.20,
    max_false_escalation_rate: float = 0.35,
    min_sample_per_arm: int = 100,
    min_practical_lift: float = 0.03,
    require_loo_robust: bool = True,
) -> BayesianResult:
    """
    Compute Beta-Binomial win probabilities, per-repo full-gate LOO robustness,
    and Deployment Eligibility Gate decision. Excludes UNKNOWN issues defensively.
    """
    clean_results = [r for r in results if r.get("real_resolution") != "UNKNOWN"]

    full = _evaluate_fold_eligibility(
        clean_results,
        variant_a=variant_a,
        variant_b=variant_b,
        win_threshold=win_threshold,
        max_false_answer_rate=max_false_answer_rate,
        max_false_escalation_rate=max_false_escalation_rate,
        min_sample_per_arm=min_sample_per_arm,
        min_practical_lift=min_practical_lift,
    )

    by_repo: dict[str, list[dict]] = defaultdict(list)
    for row in clean_results:
        by_repo[_repo_from_row(row)].append(row)

    repo_breakdown: dict[str, dict] = {}
    for repo, r_rows in sorted(by_repo.items()):
        ra = [r for r in r_rows if r["strategy"] == variant_a]
        rb = [r for r in r_rows if r["strategy"] == variant_b]
        acc_a = round(sum(1 for r in ra if r["correct"]) / len(ra) * 100, 1) if ra else None
        acc_b = round(sum(1 for r in rb if r["correct"]) / len(rb) * 100, 1) if rb else None
        repo_breakdown[repo] = {"n_a": len(ra), "acc_a": acc_a, "n_b": len(rb), "acc_b": acc_b}

    # Leave-One-Repo-Out always evaluates all K folds independently whenever >=2 repos exist,
    # separating statistical preference robustness from operational safety eligibility.
    loo_evaluated = len(by_repo) >= 2
    loo_robust = False
    loo_stat_robust = False
    loo_safety_pass = False
    loo_folds: dict[str, dict] = {}
    loo_notes: list[str] = []

    if loo_evaluated:
        # Proportional sample requirement for (K-1)/K folds
        fold_min_n = int(min_sample_per_arm * max(0.5, (len(by_repo) - 1) / len(by_repo)))
        all_stat_pass = True
        all_safety_pass = True
        all_full_ship = full["recommendation"] == "SHIP_WINNER"

        for held_out_repo in sorted(by_repo.keys()):
            sub = [r for r in clean_results if _repo_from_row(r) != held_out_repo]
            fold = _evaluate_fold_eligibility(
                sub,
                variant_a=variant_a,
                variant_b=variant_b,
                win_threshold=win_threshold,
                max_false_answer_rate=max_false_answer_rate,
                max_false_escalation_rate=max_false_escalation_rate,
                min_sample_per_arm=fold_min_n,
                min_practical_lift=min_practical_lift,
            )
            stat_pass = (
                full["stat_leader"] != "inconclusive"
                and fold["stat_leader"] == full["stat_leader"]
                and fold["abs_diff"] >= min_practical_lift
                and min(fold["n_a"], fold["n_b"]) >= fold_min_n
            )
            safety_pass = len(fold["safety_reasons"]) == 0
            full_pass = (
                full["recommendation"] == "SHIP_WINNER"
                and fold["recommendation"] == "SHIP_WINNER"
                and fold["winner"] == full["winner"]
            )
            if not stat_pass:
                all_stat_pass = False
            if not safety_pass:
                all_safety_pass = False
            if not full_pass:
                all_full_ship = False

            loo_folds[held_out_repo] = {
                "n_a": fold["n_a"],
                "n_b": fold["n_b"],
                "prob_b_beats_a_pct": round(fold["prob_b_beats_a"] * 100.0, 1),
                "win_probability_pct": round(fold["win_prob"] * 100.0, 1),
                "abs_diff_pct": round(fold["abs_diff"] * 100.0, 1),
                "false_answer_pct": round(fold["fa_rate"] * 100.0, 1),
                "false_escalation_pct": round(fold["fe_rate"] * 100.0, 1),
                "stat_leader": fold["stat_leader"],
                "stat_pass": stat_pass,
                "safety_pass": safety_pass,
                "recommendation": fold["recommendation"],
            }
            if not full_pass:
                detail = "; ".join(fold["safety_reasons"] + fold["gate_reasons"])
                loo_notes.append(
                    f"Dropping '{held_out_repo}': P={fold['win_prob']:.1%}, false_answer={fold['fa_rate']:.1%}, "
                    f"eligibility={fold['recommendation']}" + (f" ({detail})" if detail else "")
                )

        loo_stat_robust = all_stat_pass
        loo_safety_pass = all_safety_pass
        loo_robust = all_full_ship
        if loo_robust:
            loo_summary = (
                f"PASS: All {len(by_repo)} folds satisfy {win_threshold:.0%} threshold, "
                "practical lift, utility, and safety guardrails."
            )
            loo_notes.append(
                f"Full eligibility gate ('{full['winner']}', lift, utility & safety) holds across all {len(by_repo)} LOO folds."
            )
        else:
            parts = [f"LOO: {len(by_repo)} folds evaluated independently."]
            if loo_stat_robust:
                parts.append(f"Statistical preference holds at {win_threshold:.0%} in every fold.")
            else:
                parts.append(
                    f"Statistical preference is not robust at the configured {win_threshold:.0%} threshold in every fold."
                )
            if loo_safety_pass:
                parts.append("Safety eligibility passes across folds.")
            else:
                parts.append("Safety eligibility fails across folds.")
            loo_summary = " ".join(parts)
    else:
        loo_summary = "NOT VERIFIED (<2 repositories present; cross-repo LOO folds unavailable)."
        loo_notes.append(loo_summary)

    safety_reasons = list(full["safety_reasons"])
    gate_reasons = list(full["gate_reasons"])
    if require_loo_robust and loo_evaluated and not loo_robust:
        gate_reasons.append("Failed Leave-One-Repo-Out full-gate robustness check")

    if safety_reasons:
        guardrail_status = "BLOCKED"
        recommendation = "ROLLBACK_SAFETY"
        winner = "inconclusive"
        reasons = safety_reasons + gate_reasons
    elif gate_reasons:
        guardrail_status = "WARNING" if (loo_evaluated and not loo_robust) else "PASS"
        recommendation = "GATHER_MORE_DATA"
        winner = "inconclusive"
        reasons = gate_reasons
    else:
        guardrail_status = "PASS"
        recommendation = "SHIP_WINNER"
        winner = full["winner"]
        loo_tag = "PASS" if loo_robust else "NOT VERIFIED"
        reasons = [
            f"Eligible to ship '{winner}': P={full['win_prob']:.1%}, lift={full['abs_diff']:+.1%}, "
            f"false_answer={full['fa_rate']:.1%}, false_escalation={full['fe_rate']:.1%}, LOO={loo_tag}"
        ]

    return BayesianResult(
        n_a=full["n_a"],
        k_a=full["k_a"],
        n_b=full["n_b"],
        k_b=full["k_b"],
        mean_a=round(full["mean_a"], 4),
        mean_b=round(full["mean_b"], 4),
        ci_a=_credible_interval(full["pmf_a"]),
        ci_b=_credible_interval(full["pmf_b"]),
        prob_b_beats_a=round(full["prob_b_beats_a"], 4),
        prob_a_beats_b=round(full["prob_a_beats_b"], 4),
        winning_variant=winner,
        win_probability=round(full["win_prob"], 4),
        relative_lift_pct=round(full["rel_lift"], 1),
        absolute_diff_pct=round(full["abs_diff"] * 100.0, 1),
        guardrail_status=guardrail_status,
        guardrail_reasons=reasons,
        recommendation=recommendation,
        repo_breakdown=repo_breakdown,
        loo_robust=loo_robust,
        loo_notes=loo_notes,
        loo_evaluated=loo_evaluated,
        loo_stat_robust=loo_stat_robust,
        loo_safety_pass=loo_safety_pass,
        loo_summary=loo_summary,
        loo_folds=loo_folds,
    )
