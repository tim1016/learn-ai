# Deployment Validation Consecutive Green

Status: internal validation strategy, not financial advice and not an alpha claim.

## Rule

- Data: regular-session 1-minute equity bars.
- Start detection: first bar whose close timestamp is 09:45 ET.
- Stop detection: 15:45 ET. At that barrier, flatten any open position.
- Entry pattern: two consecutive green minute bars, where `green = close > open`.
- Entry order: after the second green bar, submit long entry. Intended Engine Lab run mode is `fill_mode=next_bar_open`, so the entry fills on the following minute bar open.
- Position: one open position at a time via `SetHoldings(symbol, 1.0)`.
- Exit: count bar closes while in position, including the entry-fill bar. Submit `Liquidate(symbol)` on the third held bar, which is the fifth bar in the two-green-bar pattern.
- Re-entry: after the exit fill, reset the detector. Bars from the open trade cannot seed the next pattern. Multiple trades per day are allowed.

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
- Deployment artifacts: `PythonDataService/tests/engine/live/test_deployment_validation_deploy_artifacts.py`
- Engine registry: `PythonDataService/tests/test_engine_strategies_endpoint.py`
- LEAN template shape and registry: `PythonDataService/tests/lean_sidecar/test_deployment_validation_template.py`, `PythonDataService/tests/services/test_lean_sidecar_template_registry.py`

No external golden fixture is required because this is an internal deployment-validation primitive rather than a port from LEAN, TradingView, or a paper. Cross-engine reconciliation fixtures can be added later once this template is included in the parity matrix.
