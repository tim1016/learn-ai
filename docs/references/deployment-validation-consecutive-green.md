# Deployment Validation Consecutive Green

Status: internal validation strategy, not financial advice and not an alpha claim.

## Rule

- Data: regular-session 1-minute equity bars.
- Start detection: 15 minutes after the session's scheduled open.
- Stop detection: 15 minutes before the session's scheduled close. At that
  barrier, flatten any open position.
- Entry pattern: two consecutive green minute bars, where `green = close > open`.
- Entry order: after the second green bar, submit long entry. Intended Engine Lab run mode is `fill_mode=next_bar_open`, so the entry fills on the following minute bar open.
- Position: one open position at a time via `SetHoldings(symbol, 1.0)`.
- Exit: count bar closes while in position, including the entry-fill bar. Submit `Liquidate(symbol)` on the third held bar, which is the fifth bar in the two-green-bar pattern.
- Re-entry: after the exit fill, reset the detector. Bars from the open trade cannot seed the next pattern. Multiple trades per day are allowed.

## Half-day cutoff contract (#1672)

The 09:45/15:45 ET clocks in earlier revisions of this document were never
meaningful in themselves — they were always intended as "15 minutes after
open" and "15 minutes before close" on a regular NYSE session (09:30–16:00
ET). Because this strategy is an internal validation primitive with no
external reference to preserve byte-for-byte (see "No external golden
fixture" below), there is no tension between that intent and temporal
rigor's calendar-as-source-of-truth rule — the fix is unambiguous.

Both cutoffs are now derived from the day's actual scheduled session window
(`app.lean_sidecar.trading_calendar.session_open_ms_utc` /
`session_close_ms_utc` in Python; `Securities[symbol].Exchange.Hours` in
LEAN) rather than fixed wall-clock literals:

- Detection start = scheduled session open + 15 minutes.
- Stop/flatten = scheduled session close − 15 minutes.

On a regular session this resolves to exactly 09:45/15:45 ET, unchanged from
before. On an NYSE half day (13:00 ET close, e.g. the day after
Thanksgiving) it resolves to 09:45/12:45 ET — previously the stop/flatten
barrier was a fixed 15:45 ET literal that a half-day session never reaches,
so the flatten safety net for any open position silently never fired.
Regression coverage: `tests/engine/test_deployment_validation_strategy.py::
test_on_minute_bar_suppresses_entries_at_half_day_barrier` and
`::test_on_closed_bar_emits_exit_at_half_day_barrier`, both fixtured
against 2024-11-29. Cross-implementation numeric parity (canonical Python,
QC shadow copy, LEAN trusted template) is pinned at tolerance 0 by
`tests/engine/test_deployment_validation_session_window_parity.py` against
`tests/fixtures/golden/deployment-validation-session-window/`.

### Superseded validation evidence — re-validation required before deploy

This fix changed the validated source files (`app/lean_sidecar/trusted_samples/
deployment_validation.py` and `references/qc-shadow/DeploymentValidationAlgorithm.py`,
mirrored to the container-fallback copy at `app/data/qc-shadow/
DeploymentValidationAlgorithm.py`). `app/data/strategy_validation_manifest.json`'s
`deployment_validation` entry pins SHA256 hashes of the pre-fix content plus a
`qc_cloud_backtest_id` bound to a QC Cloud backtest that ran the pre-fix
algorithm — that recorded evidence no longer describes the current code, so
it is **not** re-hashed here to manufacture a "passed" verdict per
`.claude/rules/numerical-rigor.md`'s ban on regenerating evidence to make a
check pass. The manifest's stored hashes are deliberately left stale; the
existing hash-verification gate (`app/services/strategy_validation_manifest.py`)
now correctly reports `deployable: false` for `deployment_validation` with
diagnostic notes explaining the hash mismatch, and the strategy is excluded
from the Alpaca paper-deploy panel until re-validated.

**To restore deployability**: upload the updated
`references/qc-shadow/DeploymentValidationAlgorithm.py` to quantconnect.com,
run a fresh backtest (including at least one NYSE half day in the window to
exercise the new barrier), reconcile it against the Python engine's output,
then update `deployment_validation`'s manifest entry with the new
`qc_cloud_backtest_id`, evidence hashes, and reconciliation diagnostics, and
record a fresh accepted flag event. This is a manual step — see
`docs/architecture/strategy-validation-deploy-rehome-prd.md` for the process
this manifest's evidence chain follows.

## Implementations

- Python canonical: `PythonDataService/app/engine/strategy/algorithms/deployment_validation.py`
- LEAN companion template: `PythonDataService/app/lean_sidecar/trusted_samples/deployment_validation.py`
- LEAN validator template: `PythonDataService/app/lean_sidecar/trusted_samples/deployment_validation.py`
- Legacy deploy binding fixture: `PythonDataService/app/engine/strategy/spec/fixtures/deployment_validation.spec.json` remains only because the current live-runner deploy schema still records `strategy_spec_path`; it is not the strategy validation authority.
- QuantConnect audit copy: `references/qc-shadow/DeploymentValidationAlgorithm.py`

The deployment form requires both deploy artifacts in addition to the strategy
registry entry. Run the committed QuantConnect audit copy on quantconnect.com,
copy that backtest id into the deployment form, and select the same committed
audit copy under `references/qc-shadow/`.

## Validation

- Engine behavior: `PythonDataService/tests/engine/test_deployment_validation_strategy.py`
- Cross-implementation session-window parity (canonical Python, QC shadow copy, LEAN trusted template): `PythonDataService/tests/engine/test_deployment_validation_session_window_parity.py` against `PythonDataService/tests/fixtures/golden/deployment-validation-session-window/`
- Deployment artifacts: `PythonDataService/tests/engine/live/test_deployment_validation_deploy_artifacts.py`
- Engine registry: `PythonDataService/tests/test_engine_strategies_endpoint.py`
- LEAN template shape and registry: `PythonDataService/tests/lean_sidecar/test_deployment_validation_template.py`, `PythonDataService/tests/services/test_lean_sidecar_template_registry.py`

No external golden fixture is required because this is an internal deployment-validation primitive rather than a port from LEAN, TradingView, or a paper — the session-window parity fixture above is an internal self-consistency check (see its attribution.md), not an external oracle comparison. Cross-engine reconciliation fixtures that actually execute the QC shadow copy / LEAN template inside a LEAN sandbox can be added later once this template is included in the parity matrix.
