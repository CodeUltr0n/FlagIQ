# AGENTS.md — FlagIQ × Toggle Wars 2026

## Project
FlagIQ — AI decision strategy A/B testing platform  
Hackathon: Toggle Wars 2026 (WeMakeDevs × Flagsmith)  
Track: Track 1 — Test Something Real  
Window: Nov 26–29, 2026

## Working directory
/Users/chokkaraketankumar/Desktop/Toggle

## Execution Rules for Coding Agents

- **Core decision logic (`flagiq/decision.py`):** Must remain strictly blind to post-close metadata (`real_resolution`, `has_linked_pr`, `comment_count`, `labels`) prior to assigning `decision`. Only edit thresholds in `config/policy.yaml` before Phase 4 starts; never edit `config/policy.yaml` mid-experiment.
- **Flagsmith SDK contract (`scripts/run_experiment.py`):** Always evaluate via `get_experiment_flag(FLAG_NAME, identifier=identity)` and emit `decision_correct` ONLY when `result.correct` is `True` (because the metric uses Flagsmith `occurrence` aggregation).
- **Idempotency:** Do NOT delete `fixtures/real_issues/processed.json` mid-run when using `--resume`.
- **Web UI (`app.py`):** Serves local FlagIQ Workbench at `http://127.0.0.1:8501` using Python stdlib `http.server` with zero external UI dependencies.
- **Hard rules:**
  - Never commit `.env`, `GITHUB_TOKEN`, or `FLAGSMITH_ENV_KEY`.
  - Never fabricate synthetic issues — always use real GitHub issues via `scripts/scrape_github_issues.py`.
  - Run `python3 scripts/validate_experiment.py` and `pytest tests/` after any code edit.
  - Stop adding features after Phase 4 and invest remaining time in video rehearsal.

## Phase Gates
1. `fixtures/real_issues/issues.json` exists with 400 real closed GitHub issues.
2. Flagsmith flag `triage_strategy` & experiment active in Flagsmith Cloud.
3. `python3 scripts/run_experiment.py --dry-run` and `python3 scripts/validate_experiment.py` pass cleanly.
4. All 400 issues processed; exposures and `decision_correct` conversions visible in Flagsmith.
5. `assets/win_probability.png` captured from Flagsmith dashboard + `app.py` verified for demo.
6. 3-minute demo video recorded and uploaded.
