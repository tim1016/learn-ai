"""Explicit operator terms for run/custody fixtures unrelated to pricing policy."""
from app.schemas.exit_terms import ExitTermsInput

DEPLOY_EXIT_TERMS = ExitTermsInput(exit_allowance_bps=20, band_multiple=2, spread_cap_bps=50).seal()
