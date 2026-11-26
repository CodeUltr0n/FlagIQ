"""
FlagIQ — GitHub Issue Scraper
Pulls real closed GitHub issues from 5 repositories, derives high-precision
ground-truth resolutions, quarantines ambiguous/bot/stale closures as UNKNOWN
(never silently defaulting them to ANSWER), and exports a 50-issue audit sample.

Usage:
    python3 scripts/scrape_github_issues.py
    python3 scripts/scrape_github_issues.py --calibration   # pull 60 older dev issues

Outputs:
    fixtures/real_issues/issues.json            — verified benchmark issues (sorted chronologically)
    fixtures/real_issues/excluded_unknown.json  — quarantined ambiguous/stale/bot closures
    fixtures/real_issues/audit_sample_50.json   — random 50-issue sample for human ground-truth audit
"""
from __future__ import annotations
import argparse
import json
import os
import random
import sys
import time
from collections import Counter
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv():
        return False

import requests
from rich.console import Console
from tqdm import tqdm

load_dotenv()
console = Console()

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
HEADERS = {
    "Authorization": f"token {GITHUB_TOKEN}" if GITHUB_TOKEN else "",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}

REPOS = [
    ("python/cpython",      100),
    ("facebook/react",       80),
    ("microsoft/vscode",     80),
    ("vercel/next.js",       80),
    ("flagsmith/flagsmith",  60),
]

OUT_PATH = Path("fixtures/real_issues/issues.json")
EXCLUDED_PATH = Path("fixtures/real_issues/excluded_unknown.json")
AUDIT_PATH = Path("fixtures/real_issues/audit_sample_50.json")
CALIBRATION_PATH = Path("fixtures/real_issues/calibration_issues.json")
OUT_PATH.parent.mkdir(parents=True, exist_ok=True)

_STALE_MARKERS = (
    "stale", "no-response", "no response", "abandoned",
    "inactive", "expired", "auto-closed",
)


def _is_bot_user(user: dict | None) -> bool:
    if not user:
        return False
    login = (user.get("login") or "").lower()
    return user.get("type") == "Bot" or login.endswith("[bot]") or "stale" in login


def _has_solution_comment(repo: str, number: int, issue_author: str) -> bool:
    """
    Require substantive non-bot, non-author solution evidence in comments
    (code block, docs/commit/PR explanation, or >= 40 char helpful guidance).
    """
    try:
        url = f"https://api.github.com/repos/{repo}/issues/{number}/comments"
        r = requests.get(url, headers=HEADERS, timeout=10)
        if r.status_code != 200:
            return False
        for comment in r.json():
            user = comment.get("user") or {}
            login = (user.get("login") or "").lower()
            if _is_bot_user(user) or (issue_author and login == issue_author.lower()):
                continue
            body = (comment.get("body") or "").strip()
            if len(body) < 35:
                continue
            lower = body.lower()
            if "stale" in lower or "closing this issue" in lower or "please fill out the template" in lower:
                continue
            if "```" in body or "http" in body or len(body) >= 80:
                return True
    except Exception:
        return False
    return False


def _get_merged_pr(repo: str, number: int) -> bool:
    """
    Verify an actually MERGED PR or closing commit via timeline.
    Closed-unmerged (rejected/abandoned) PRs do NOT count as ESCALATE.
    """
    try:
        url = f"https://api.github.com/repos/{repo}/issues/{number}/timeline"
        r = requests.get(
            url,
            headers={**HEADERS, "Accept": "application/vnd.github.mockingbird-preview+json"},
            timeout=10,
        )
        if r.status_code != 200:
            return False
        for event in r.json():
            ev_type = event.get("event")
            if ev_type == "closed" and event.get("commit_id"):
                return True
            if ev_type in ("cross-referenced", "connected"):
                source = event.get("source", {})
                src_issue = source.get("issue", {})
                pr = src_issue.get("pull_request")
                if pr and pr.get("merged_at"):
                    return True
    except Exception:
        return False
    return False


def label_resolution(
    labels: list[str],
    has_merged_pr: bool,
    has_solution_comment: bool,
    state_reason: str = "",
    closed_by_bot: bool = False,
) -> str:
    """
    Deterministic high-precision ground-truth labeling from GitHub signals.
    Uncertain, stale, bot-closed, or not_planned tickets return 'UNKNOWN'
    and are quarantined instead of contaminating ANSWER.
    """
    label_names = [l.lower() for l in labels]

    if "duplicate" in label_names or state_reason == "duplicate":
        return "DUPLICATE"
    if any(any(marker in lbl for marker in _STALE_MARKERS) for lbl in label_names):
        return "UNKNOWN"
    if closed_by_bot:
        return "UNKNOWN"
    if has_merged_pr:
        return "ESCALATE"
    if any(l in label_names for l in ["needs-info", "needs info", "needs-repro", "wontfix", "won't fix", "invalid"]):
        return "CLARIFY"
    if state_reason == "not_planned":
        return "UNKNOWN"
    if has_solution_comment:
        return "ANSWER"
    return "UNKNOWN"


def stratified_audit_sample(issues: list[dict], target_n: int = 50, seed: int = 42) -> list[dict]:
    """
    Draw a deterministic, proportional stratified sample across (repo, real_resolution)
    strata so every repo and label class is represented in the human audit.
    """
    if len(issues) <= target_n:
        return list(issues)
    rng = random.Random(seed)
    by_stratum: dict[tuple[str, str], list[dict]] = {}
    for item in issues:
        key = (item.get("repo", ""), item.get("real_resolution", ""))
        by_stratum.setdefault(key, []).append(item)

    selected: list[dict] = []
    remainders: list[dict] = []

    # Proportional quota per stratum (at least 1 when stratum is non-empty and target permits)
    quotas: list[tuple[tuple[str, str], int, float]] = []
    total = len(issues)
    for key in sorted(by_stratum):
        exact = len(by_stratum[key]) * target_n / total
        base = int(exact)
        quotas.append((key, base, exact - base))

    # Give largest fractional remainders extra slots up to target_n
    allocated = sum(b for _, b, _ in quotas)
    quotas.sort(key=lambda x: (-x[2], x[0]))
    final_counts: dict[tuple[str, str], int] = {}
    for idx, (key, base, _) in enumerate(quotas):
        extra = 1 if idx < (target_n - allocated) else 0
        final_counts[key] = min(len(by_stratum[key]), base + extra)

    for key in sorted(by_stratum):
        pool = list(by_stratum[key])
        rng.shuffle(pool)
        take = final_counts[key]
        selected.extend(pool[:take])
        remainders.extend(pool[take:])

    if len(selected) < target_n and remainders:
        rng.shuffle(remainders)
        selected.extend(remainders[: target_n - len(selected)])
    return selected[:target_n]


def fetch_issues(
    repo: str,
    count: int,
    start_page: int = 1,
    exclude_ids: set[str] | None = None,
) -> tuple[list[dict], list[dict]]:
    """Fetch `count` verified closed issues from `repo` (skipping exclude_ids & UNKNOWNs)."""
    verified: list[dict] = []
    excluded: list[dict] = []
    skip_set = exclude_ids or set()
    page = start_page
    per_page = 100

    with tqdm(total=count, desc=f"{repo}", unit="issue") as pbar:
        while len(verified) < count and page <= start_page + 18:
            url = (
                f"https://api.github.com/repos/{repo}/issues"
                f"?state=closed&per_page={per_page}&page={page}"
                f"&sort=created&direction=desc"
            )
            try:
                r = requests.get(url, headers=HEADERS, timeout=15)
            except Exception as e:
                console.print(f"[yellow]Network hiccup on {repo} p{page}: {e}. Retrying in 2s...[/yellow]")
                time.sleep(2)
                continue

            if r.status_code == 403:
                console.print(f"[red]Rate limit hit on {repo}. Confirm GITHUB_TOKEN in .env[/red]")
                sys.exit(1)
            if r.status_code != 200:
                console.print(f"[yellow]Warning: {r.status_code} on {repo} p{page}[/yellow]")
                break

            issues = [i for i in r.json() if "pull_request" not in i]
            if not issues:
                break

            for issue in issues:
                if len(verified) >= count:
                    break

                number = issue["number"]
                identity = f"{repo}#{number}"
                if identity in skip_set:
                    continue

                labels = [l["name"] for l in issue.get("labels", [])]
                comment_count = issue.get("comments", 0)
                state_reason = (issue.get("state_reason") or "").lower()
                issue_author = (issue.get("user") or {}).get("login", "")
                closed_by_bot = _is_bot_user(issue.get("closed_by"))

                if comment_count == 0 and not labels and state_reason != "duplicate":
                    continue

                has_pr = _get_merged_pr(repo, number)
                has_sol = _has_solution_comment(repo, number, issue_author) if not has_pr else False
                resolution = label_resolution(
                    labels=labels,
                    has_merged_pr=has_pr,
                    has_solution_comment=has_sol,
                    state_reason=state_reason,
                    closed_by_bot=closed_by_bot,
                )

                entry = {
                    "identity": identity,
                    "repo": repo,
                    "number": number,
                    "created_at": issue.get("created_at", ""),
                    "state_reason": state_reason,
                    "title": issue.get("title", ""),
                    "body": (issue.get("body") or "")[:2000],
                    "labels": labels,
                    "comment_count": comment_count,
                    "has_linked_pr": has_pr,
                    "real_resolution": resolution,
                }

                if resolution == "UNKNOWN":
                    excluded.append(entry)
                else:
                    verified.append(entry)
                    skip_set.add(identity)
                    pbar.update(1)
                time.sleep(0.2)

            page += 1

    return verified, excluded


def main():
    parser = argparse.ArgumentParser(description="FlagIQ GitHub Issue Scraper")
    parser.add_argument(
        "--calibration",
        action="store_true",
        help="Pull a disjoint older dev split (page 20+) to calibrate policy thresholds without touching test set",
    )
    args = parser.parse_args()

    if not GITHUB_TOKEN:
        console.print("[bold yellow]⚠  GITHUB_TOKEN not set — using unauthenticated (60 req/h).[/bold yellow]")

    # Enforce strict zero overlap between calibration and held-out sets
    exclude_ids: set[str] = set()
    other_path = OUT_PATH if args.calibration else CALIBRATION_PATH
    if other_path.exists():
        with open(other_path) as f:
            exclude_ids = {item["identity"] for item in json.load(f)}
        console.print(f"[dim]Loaded {len(exclude_ids)} existing IDs from {other_path} to enforce disjoint splits.[/dim]")

    all_verified: list[dict] = []
    all_excluded: list[dict] = []

    repo_targets = [(r, max(10, c // 5)) for r, c in REPOS] if args.calibration else REPOS
    start_page = 20 if args.calibration else 1

    for repo, count in repo_targets:
        console.print(f"\n[bold cyan]Scraping {repo} ({count} verified issues, start_page={start_page})…[/bold cyan]")
        v, ex = fetch_issues(repo, count, start_page=start_page, exclude_ids=exclude_ids)
        all_verified.extend(v)
        all_excluded.extend(ex)
        console.print(f"  [green]✓ {len(v)} verified[/green] | [dim]{len(ex)} quarantined as UNKNOWN[/dim]")

    # Sort chronologically to guarantee temporal order across benchmark runs
    all_verified.sort(key=lambda x: (x.get("created_at", ""), x.get("number", 0)))

    out_file = CALIBRATION_PATH if args.calibration else OUT_PATH
    with open(out_file, "w") as f:
        json.dump(all_verified, f, indent=2)
    if not args.calibration:
        with open(EXCLUDED_PATH, "w") as f:
            json.dump(all_excluded, f, indent=2)

        audit_sample = stratified_audit_sample(all_verified, target_n=50, seed=42)
        with open(AUDIT_PATH, "w") as f:
            json.dump(
                [
                    {
                        "identity": i["identity"],
                        "repo": i["repo"],
                        "url": f"https://github.com/{i['repo']}/issues/{i['number']}",
                        "title": i["title"],
                        "labels": i["labels"],
                        "state_reason": i["state_reason"],
                        "has_linked_pr": i["has_linked_pr"],
                        "assigned_ground_truth": i["real_resolution"],
                        "body_edit_marker": any(
                            m in (i.get("body") or "").lower()
                            for m in ("edit:", "update:", "fixed in", "solved by")
                        ),
                        "human_verified_correct": None,
                    }
                    for i in audit_sample
                ],
                f,
                indent=2,
            )

    console.print(f"\n[bold green]✅ Done — {len(all_verified)} verified issues saved to {out_file}[/bold green]")
    if not args.calibration:
        console.print(f"[dim]Quarantined UNKNOWN issues: {len(all_excluded)} ({EXCLUDED_PATH})[/dim]")
        console.print(f"[dim]Human audit sample (n={min(50, len(all_verified))}): {AUDIT_PATH}[/dim]")

    dist = Counter(i["real_resolution"] for i in all_verified)
    console.print("\n[bold]Verified Label Distribution:[/bold]")
    for label, cnt in sorted(dist.items()):
        pct = (cnt / len(all_verified) * 100) if all_verified else 0.0
        console.print(f"  {label}: {cnt} ({pct:.1f}%)")


if __name__ == "__main__":
    main()
