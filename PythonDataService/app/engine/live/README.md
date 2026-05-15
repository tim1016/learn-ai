# IBKR Paper Live Runtime

This package hosts the paper-trading runtime for LEAN-ported strategies.
Phases 1-7 build the fake-broker replay path and stop before real IBKR
configuration or paper-week execution.

## Indicator state persistence

`SpyEmaCrossoverAlgorithm`'s indicators (EMA5, EMA10, RSI14) persist
across runs so the operator doesn't pay the ~3 h 45 m warmup cost every
morning.

**Files:**
- Stable sidecar: `PythonDataService/artifacts/live_state/spy_ema_crossover/SPY_15m.json`
- Per-run hydration receipt: `<run_dir>/indicator_state_hydration.json`

**Modules:**
- `indicator_state.py` — envelope, payload, policy enum, repo, validation ladder, hydrate() and maybe_write()
- `nyse_calendar.py` — previous-completed-session lookup (staleness check)

**Policy tri-state on `start`:**
- `require` (default) — exit 4 on any validation failure
- `optional` — cold-start on failure; useful for seed day
- `disabled` (alias `--allow-cold-start`) — never read; still write

**Write triggers:**
- Force-flat completion (15:55 ET) — first checkpoint; canonical
- Graceful-shutdown `finally` — "newer" check refuses to overwrite force-flat with earlier-Ctrl-C state

**Design doc:** `docs/superpowers/specs/2026-05-15-spy-ema-paper-dry-run-design.md`

