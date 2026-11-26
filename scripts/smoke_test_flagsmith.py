"""
FlagIQ — Live Flagsmith Cloud Smoke-Test & Round 04 Rollout Verifier

Run this BEFORE the experiment batch (400-issue target) to confirm:
  1. Flagsmith Cloud receives exposures via `get_experiment_flag()` with valid variation keys.
  2. `track_event("decision_correct", identifier=...)` registers conversions cleanly.
  3. `--verify-rollout` confirms Round 04 (`SHIP_WINNER`) rolled out to 100% without code changes.

Usage:
    python3 scripts/smoke_test_flagsmith.py                 # live 5-identity smoke test
    python3 scripts/smoke_test_flagsmith.py --verify-rollout # confirm Round 04 100% winner rollout
    python3 scripts/smoke_test_flagsmith.py --dry-run        # local check without network
"""
from __future__ import annotations
import argparse
import os
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from rich.console import Console
from rich.table import Table

sys.path.insert(0, str(Path(__file__).parent.parent))
from scripts.run_experiment import (
    FLAG_NAME,
    ENV_KEY,
    VARIANT_A,
    VARIANT_B,
    create_flagsmith_client,
    get_variant_for_identity,
    record_occurrence_event,
)

console = Console()


def run_smoke_test(dry_run: bool = False, verify_rollout: bool = False) -> dict:
    if not dry_run and not ENV_KEY:
        console.print("[red]✗ FLAGSMITH_ENV_KEY not set in .env[/red]")
        sys.exit(1)

    fs = None if dry_run else create_flagsmith_client(ENV_KEY)
    probe_prefix = "smoke/rollout-probe" if verify_rollout else "smoke/check"
    identities = [f"{probe_prefix}#{i}" for i in range(1, 6)]

    table = Table(
        title="FlagIQ — Flagsmith Smoke Test" + (" (Rollout Check)" if verify_rollout else ""),
        show_header=True,
    )
    table.add_column("Identity")
    table.add_column("Assigned Variant", style="cyan")
    table.add_column("Conversion Fired")

    assigned: list[str] = []
    conversions_fired = 0

    for idx, identity in enumerate(identities):
        variant = get_variant_for_identity(fs, identity, idx, dry_run)
        assigned.append(variant)

        # Fire occurrence conversion on 3 of 5 probes during pre-run smoke test
        should_convert = (not verify_rollout) and (idx < 3)
        if should_convert:
            record_occurrence_event(fs, identity, "decision_correct", dry_run)
            conversions_fired += 1

        table.add_row(
            identity,
            variant,
            "decision_correct (1)" if should_convert else "exposure only",
        )

    if fs and hasattr(fs, "flush_events"):
        try:
            fs.flush_events()
        except Exception as exc:
            console.print(f"[yellow]⚠ flush_events warning: {exc}[/yellow]")

    console.print(table)
    unique_variants = set(assigned)

    if verify_rollout:
        if len(unique_variants) == 1:
            winner = next(iter(unique_variants))
            console.print(
                f"\n[green]✓ ROLLOUT VERIFIED — All 5 probe identities served '{winner}' "
                "with zero code changes or server restart.[/green]"
            )
            status = "rollout_verified"
        else:
            console.print(
                f"\n[yellow]⚠ SPLIT STILL ACTIVE ({sorted(unique_variants)}) — "
                "Rollout is not yet 100% on one variant in Flagsmith Cloud.[/yellow]"
            )
            status = "split_active"
    else:
        if len(unique_variants) >= 2:
            console.print(
                f"\n[green]✓ SMOKE TEST PASS — Both variants ({VARIANT_A}, {VARIANT_B}) "
                f"assigned across 5 exposures; {conversions_fired} conversions flushed.[/green]"
            )
            status = "pass"
        else:
            console.print(
                f"\n[yellow]⚠ Only one variant seen ({sorted(unique_variants)}). "
                "Check Flagsmith 50/50 split and variation keys before running the experiment batch.[/yellow]"
            )
            status = "single_variant_warning"

    console.print(
        "[dim]Next: Check Flagsmith Cloud → Experiments → AI Triage Strategy Experiment "
        "for exposures & conversions (allow ~60s for ingestion).[/dim]"
    )
    return {
        "status": status,
        "flag_name": FLAG_NAME,
        "assigned": assigned,
        "conversions_fired": conversions_fired,
    }


def main():
    parser = argparse.ArgumentParser(description="FlagIQ Flagsmith smoke-test & rollout verifier")
    parser.add_argument("--verify-rollout", action="store_true",
                        help="Verify Round 04 winner rollout serves 100% single variant")
    parser.add_argument("--dry-run", action="store_true",
                        help="Simulate locally without hitting Flagsmith Cloud")
    args = parser.parse_args()
    run_smoke_test(dry_run=args.dry_run, verify_rollout=args.verify_rollout)


if __name__ == "__main__":
    main()
