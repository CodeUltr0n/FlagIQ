#!/bin/bash
# FlagIQ — One-command setup
# Usage: bash setup.sh

set -e
echo ""
echo "🚩 FlagIQ Setup"
echo "──────────────────────────────────────"

python3 --version || { echo "❌ Python 3 required"; exit 1; }

if [ ! -d ".venv" ]; then
    echo "→ Creating virtual environment..."
    python3 -m venv .venv
fi

source .venv/bin/activate
echo "→ Installing dependencies..."
pip install --upgrade pip -q
pip install -r requirements.txt -q
echo "✓ Dependencies installed"

if [ ! -f ".env" ]; then
    cp .env.example .env
    echo "✓ .env created from .env.example (fill GITHUB_TOKEN and FLAGSMITH_ENV_KEY)"
else
    echo "✓ .env already exists"
fi

mkdir -p fixtures/real_issues assets
echo "✓ directories ready"

echo ""
echo "→ Running test suite..."
python -m pytest tests/ -v --tb=short
echo ""
echo "──────────────────────────────────────"
echo "✅ FlagIQ setup complete!"
echo ""
echo "Recommended launch sequence:"
echo "  1. python3 scripts/smoke_test_flagsmith.py        # verify 5 live Flagsmith exposures"
echo "  2. python3 scripts/scrape_github_issues.py        # pull 400 verified closed issues + 50-issue audit"
echo "  3. python3 scripts/run_experiment.py --local-preview  # paired pre-lock strategy check"
echo "  4. python3 scripts/validate_experiment.py         # verify zero leakage & temporal isolation"
echo "  5. python3 scripts/run_experiment.py              # run live 400-issue Flagsmith experiment"
echo "  6. python3 scripts/validate_experiment.py --post  # stratified Q1-Q4 & LOO sensitivity"
echo "  7. python3 scripts/check_win_probability.py       # Deployment Eligibility Gate check"
echo "  8. python3 app.py                                 # launch interactive workbench (:8501)"
echo ""
