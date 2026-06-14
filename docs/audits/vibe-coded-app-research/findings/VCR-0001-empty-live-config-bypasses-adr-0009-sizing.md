---
id: VCR-0001
severity: P0
status: remediated
area: live-sizing
canonical_file: PythonDataService/app/engine/live/live_engine.py:411
reference: docs/architecture/adrs/0009-live-sizing-authority-and-provenance.md
first_seen: 2026-06-14
last_seen: 2026-06-14
remediated_in: "#493 — Phase 1 — Sizing policy required at deploy boundary"
lens: live-sizing-adr-0009
dedupe_with_F: none
confidence: high
---

## Remediation (#493 / Phase 1)

Closed by issue #493. The deploy boundary now refuses empty / sizing-less
``live_config`` payloads at three layers, all mirrored against
``LIVE_CONFIG_LEDGER_KEYS`` so any new sibling has to opt in deliberately:

1. **Schema layer** — ``HostRunnerDeployRequest._validate_sizing``
   (``app/schemas/live_runs.py``) raises ``ValidationError`` when ``sizing`` is
   absent or a sibling is unknown. API requests fail with 422 before reaching
   the daemon.
2. **Deploy seam** — ``_enforce_sizing_policy_present`` in
   ``app/engine/live/deploy.py`` raises ``SizingPolicyMissingError`` /
   ``UnknownLiveConfigKeyError`` so the CLI (``run init-ledger``) and any
   direct ``deploy_run`` caller is gated too. Canonicalizes ``sizing`` through
   ``policy_to_ledger_dict`` so the CLI path produces the same hashed
   ``run_id`` as the API path.
3. **Start-time runtime gate** — ``cmd_start`` in ``app/engine/live/run.py``
   refuses to bring up a ledger whose ``live_config.sizing`` is ``None`` with
   exit code 2 and a redeploy-required error naming sizing. No
   ``--allow-pre-policy-sizing`` flag: ``live_config`` is hashed into
   ``run_id``, so a start-time effective-sizing change would make the
   identity fingerprint dishonest.

Read-only paths stay open: ``_live_config_from_ledger`` still hydrates
pre-policy ledgers for cockpit / Sizing-card inspection
(``LiveConfig(sizing=None)``). ``check_sizing_policy_present`` is registered
in ``app/engine/live/pre_flight.py`` for the manual pre-flight subcommand.

Regression tests:

- ``tests/schemas/test_host_runner_deploy_request_sizing.py`` —
  ``test_empty_live_config_rejected``,
  ``test_live_config_without_sizing_key_rejected``,
  ``test_unknown_sibling_key_rejected_at_schema_boundary``,
  ``test_live_config_with_only_known_siblings_accepted``.
- ``tests/engine/live/test_deploy.py`` —
  ``test_deploy_run_rejects_empty_live_config``,
  ``test_deploy_run_rejects_live_config_without_sizing``,
  ``test_deploy_run_rejects_unknown_live_config_sibling_key``,
  ``test_deploy_run_canonicalizes_sizing_dict``.
- ``tests/engine/live/test_run_cli.py`` —
  ``test_init_ledger_refuses_default_empty_live_config``,
  ``test_init_ledger_refuses_unknown_live_config_sibling``,
  ``test_start_refuses_legacy_ledger_without_sizing``,
  ``test_start_refuses_ledger_with_sibling_keys_but_no_sizing``,
  ``test_live_config_from_ledger_legacy_path_still_loads_for_inspection``.
- ``tests/engine/live/test_pre_flight.py`` —
  ``test_sizing_policy_present_passes_for_safe_canary``,
  ``test_sizing_policy_present_fails_for_legacy_ledger``,
  ``test_sizing_policy_present_fails_when_sibling_keys_only``.

---

## What

ADR 0009 was built specifically to prevent the documented $250k surprise where `deployment_validation` sized with `set_holdings(SPY, 1.0)` and bought the entire paper account. PR1–PR7 ship the policy machinery (`OrderSizer`, four-kind discriminated union, allow-list, sizing card, deploy-form 3-option radio) but leave one load-bearing back-door open:

`HostRunnerDeployRequest.live_config` is typed as an open `dict` (schemas/live_runs.py:326). The `_validate_sizing` field validator only validates the `sizing` sub-key when present — it does **not** require it. A deploy request with `live_config = {}` (or with no `sizing` key) passes Pydantic, persists into the ledger, and at start time:

1. `_live_config_from_ledger` accepts the empty payload and builds `LiveConfig(sizing=None)`.
2. `LiveEngine.run` constructs `LivePortfolio(self._broker)` and **only constructs an `OrderSizer` when `self._config.sizing is not None`** (live_engine.py:411).
3. When the gate is False, the portfolio retains `LivePortfolio.sizing_model: SizingModel = field(default_factory=SimpleFloorSizing)` (live_portfolio.py default).
4. The legacy `SimpleFloorSizing` then governs `set_holdings(symbol, Decimal('1.0'))` → `int(portfolio_value * 1.0 / price)` → the entire account.

The code comment at live_engine.py:406-408 is candid about the back-door: *"callers (no policy in live_config) keep the prior SimpleFloorSizing path."* This is documented behavior, not a bug per se — but it means the ADR 0009 safety guarantee is **conditional on every caller remembering to send a `sizing` key**, and there is no server-side check that the caller did so.

The symbol-scoped coexistence guard does **not** close this gap because `_is_set_holdings_full(policy=None)` returns False — it only blocks an *explicit* `SetHoldings(1.0)` policy. An absent policy is treated as legacy, which is exactly the path the ADR's Decision 14 documents as `SimpleFloorSizing` behavior.

## Where

- `PythonDataService/app/engine/live/live_engine.py:402-418` — the `if self._config.sizing is not None:` gate around `OrderSizer` construction; comment 406-408 documents the SimpleFloor fallback.
- `PythonDataService/app/engine/live/live_portfolio.py` (`sizing_model` field default factory `SimpleFloorSizing`).
- `PythonDataService/app/schemas/live_runs.py:326` — `live_config: dict = Field(default_factory=dict)` accepts empty dict.
- `PythonDataService/app/schemas/live_runs.py:332-357` — `_validate_sizing` only validates `sizing` sub-key when present, never requires it.
- `PythonDataService/app/engine/live/run.py:565-578` — `_live_config_from_ledger` allow-list lets an empty payload through.
- `PythonDataService/app/engine/live/pre_flight.py` — `check_all_in_coexistence` only triggers on `SetHoldings(1.0)`, not on absent policy.

## Why this severity

PRD §7 P0: "can silently corrupt live/paper trading, position sizing, fills, P&L". A deploy request that omits the `sizing` key (intentionally or via a stale CLI / API caller that pre-dates PR1) results in 100% of equity being purchased on the first signal, with no UI warning, no pre-flight refusal, and a "Pre-policy run" banner on the Sizing card that explicitly tells the operator this is acceptable legacy behavior. The exact $250k surprise the ADR was built to prevent can recur **silently** from a config-only mistake.

## Trading impact

- **Paper account today**: a fresh deploy via the Angular form is safe (the form emits a `sizing` block per its 3-option radio default of `safe_canary` = `FixedShares(1)`). But:
  - A stale frontend (pre-PR1 cache) submits without the field → SimpleFloor.
  - A CLI / curl caller that omits the field → SimpleFloor.
  - An automated test fixture or replay tool that constructs a `HostRunnerDeployRequest` directly → SimpleFloor.
  - A redeploy from a legacy ledger that loads `live_config={}` from disk → SimpleFloor (the Sizing card honestly shows "Pre-policy run", but the trading behavior is the all-in path).
- **If extended to live money**: the same code paths would route a real-money $250k surprise.

The blast radius is bounded today by paper-only enforcement (DU-prefix sentinel, port check, IBKR_MODE=paper). But the ADR's own framing is that paper validation is the dress rehearsal — the contract carries forward.

## Reproduction

Static trace, confirmed by direct read:

```bash
# 1. Show the gate that admits SimpleFloor when no policy is present:
sed -n '402,418p' PythonDataService/app/engine/live/live_engine.py

# 2. Show the schema accepts empty live_config:
grep -n "live_config: dict = Field" PythonDataService/app/schemas/live_runs.py

# 3. Show the validator does not require 'sizing':
sed -n '332,358p' PythonDataService/app/schemas/live_runs.py

# 4. Show the start-time allow-list lets the empty dict through:
sed -n '565,578p' PythonDataService/app/engine/live/run.py
```

To exercise dynamically (do not run as part of this audit):

```bash
curl -X POST http://localhost:8090/deploy \
  -H "X-Live-Runner-Token: $TOKEN" \
  -d '{ ... usual fields ..., "live_config": {} }'
# → 200 OK, ledger written; start it → SimpleFloorSizing governs set_holdings.
```

## Suggested resolution (NOT auto-applied)

Make the `sizing` key **required** at the server-side boundary. Two reasonable shapes:

1. **Require at the schema level**: change `live_config: dict = Field(default_factory=dict)` to typed `LiveConfigModel` with `sizing: SizingPolicy` required. Existing legacy ledgers continue to load via `_live_config_from_ledger`'s legacy branch (absence ⇒ pre-policy), so on-disk compatibility is preserved; only new deploys are blocked from omitting it. This honors ADR 0009 Decision 7 ("Every new deploy always writes an explicit `sizing` block").
2. **Or refuse to construct `LivePortfolio` without a policy** for any run that is post-ADR (e.g., gated by ledger schema version): live_engine.py:411 becomes a `raise PolicyMissingError("ADR 0009 requires live_config.sizing; legacy fallback is for pre-policy ledgers only and these must be flagged at deploy")`.

Pair either fix with:

- A pre-flight gate `check_sizing_policy_present` that fails closed for new runs with absent policy.
- A test: deploy with `live_config={}` should 400 at the API boundary, not 200-then-silently-Simple-Floor.
- A test: redeploy from a legacy ledger (where absence is genuine) loads a `Pre-policy run` Sizing card AND refuses to start unless the operator confirms via a separate UI gate.

## Provenance of the finding

Lens: `live-sizing-adr-0009` (workflow `wf_def78013-ce4`, 2026-06-13/14). Lens identified the path in its summary; main-loop verified by direct read of live_engine.py:402-418, live_runs.py:326-357, run.py:565-578. The audit comment at live_engine.py:406-408 is the smoking gun.
