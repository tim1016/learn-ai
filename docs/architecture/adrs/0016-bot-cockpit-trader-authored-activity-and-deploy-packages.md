# ADR 0016 — Bot Cockpit trader-authored activity and deploy packages

**Status:** Accepted 2026-06-25. Drafted during the 2026-06-25 Bot Cockpit / Deploy a strategy grilling session.
**Decision drivers:** The Bot Cockpit was leaking operator-control implementation language (`endpoint_snapshot`, raw JSON, `spec_path`, account-source codes) into trader-facing surfaces; trade history for the JUNE-25 bot existed in durable artifacts but did not appear in Activity; broker/evidence tables flashed during refresh; and Deploy a strategy asked traders to assemble raw paths, settings files, account ids, and validation identifiers by hand.
**Related:** ADR 0013 (operator-surface boundary: judgment vs evidence), ADR 0014 (broker-authored operator view, backend-rendered narratives), ADR 0015 (operator notice contract), `CONTEXT.md` (Live operator console glossary).

## Context

The deployed-strategy console has one trader-facing name: **Bot Cockpit**. Its job is to let a trader understand and control a configured bot without learning the implementation vocabulary of the control plane.

The current surfaces violate that shape in several ways:

1. **Activity shows code output.** Evidence rows such as `endpoint_snapshot`, account-position events, account-source fields, and request/callback names appear as primary table text. These are diagnostic facts, not trader language.
2. **Trade history can exist but not render.** The JUNE-25 investigation found `executions.parquet` and `trades.parquet` rows under the run artifact directory while the per-instance broker-activity WAL was absent/stale. The Activity tab therefore had no fill rows even though durable trade evidence existed.
3. **Refresh churn causes visual flashing.** The Activity table can appear to reload from local artifact reads or full projection replacement even when no trader-usable row has changed.
4. **Audit and Configuration duplicate raw facts.** Run identity, runtime configuration, broker API evidence, and deployment metadata are displayed as raw key/value or JSON blobs, sometimes in more than one place.
5. **Deploy asks for implementation details.** The deploy page asks for strategy paths, settings file paths, account ids, backtest validation fields, and action-plan primitives in a way that makes the trader assemble a valid deployment instead of selecting/creating a validated package.

External wording research reinforced the same direction. Interactive Brokers surfaces trader nouns such as account, portfolio, positions, trades, balances, margin, and available-for-trading rather than raw API callback names. UX guidance from Nielsen Norman Group recommends plain language, precise diagnosis, constructive next steps, and hiding/minimizing obscure codes except for diagnostics.

## Decision

### 1. Canonical product language

The trader-facing surface is **Bot Cockpit**. Implementation docs may mention `cockpit-v2`, but trader copy should not use "terminal cockpit" or other implementation-oriented names.

Bot Cockpit rows, panels, badges, and section summaries use trader-facing language. Raw event/type codes may appear only in technical details or audit expansion.

### 2. Backend-authored event narrative registry

Trader-facing explanations for broker activity, audit evidence, incidents, reconciliation states, and deploy validation evidence are backend-authored from structured facts.

The backend owns a closed event narrative registry. Each supported event meaning carries:

- stable code / enum identity;
- severity or attention level;
- short trader label;
- concise explanation;
- optional why-it-matters or suggested next step;
- technical facts for expansion;
- fold identity and fold count when rows are repeated.

Angular renders the authored contract. It may format layout, ET display time, numbers, badges, PrimeNG severity, expansion state, and technical-detail disclosure. It must not infer what an event means by comparing raw JSON, timestamps, or rendered text.

Unsupported event meanings fail visibly as unmapped diagnostics rather than being guessed by the frontend.

### 3. Trade history must surface in Activity

Broker activity remains the primary live Activity authority, per ADR 0014. However, the Bot Cockpit must not show an empty Activity table when durable fill evidence exists.

If the per-instance broker-activity WAL is missing or stale, the backend must automatically repair or supplement the Activity projection from durable sources, in this order:

1. raw broker callback WAL, when present;
2. legacy execution artifacts such as `executions.parquet`;
3. closed-trade artifacts such as `trades.parquet`, when rendering trade-history summaries;
4. diagnostic evidence explaining why no reconstructable row exists.

This repair path is **net-new production wiring**, not a relabel of existing code. `broker_activity_reconstruction.py` already contains authoring logic, but Activity projection must decide when to invoke repair, how to cache/idempotently skip already-repaired runs, and how to avoid scanning Parquet on every poll.

Repair does **not** append to the canonical per-instance `broker_activity.jsonl` during Activity reads. Activity GET is read-only with respect to the broker-activity WAL. Repaired rows are written to a separate derived repair projection/cache with its own lock, artifact signature, provenance, and stable visible-row identity. Activity reads the live broker-activity WAL plus the repair projection. This keeps broker-WAL sequence ownership with the live publisher and avoids racing live subscription writes with historical repair writes.

The trader should see the fill/trade with provenance in expansion. They should not have to know whether the row came from live capture, callback replay, legacy execution reconstruction, or closed-trade summary projection. Closed-trade summaries are a distinct Activity row type/section; they reference their constituent fill rows and must not be counted as additional broker executions.

The next live validation is a normal-market bot run: after a real broker fill, the fill must appear in Bot Cockpit Activity without manual reconstruction. This is an operational confirmation, not a CI merge gate.

### 4. Exchange-time display

All primary Bot Cockpit tables, panels, and audit summaries display time in `America/New_York` (ET), matching the market/session clock for U.S. equities and options. Canonical `int64 ms UTC` remains the wire/storage format and may appear in expandable forensic details.

The backend must not ship preformatted local-time strings. It ships canonical integer milliseconds plus any semantic labels; Angular formats for display at the UI rendering boundary.

### 5. Backend-authored folding and clustering

Repeated Activity rows and repeated Recent Incident panels are folded only when the backend supplies a stable fold identity and count.

The folded row or panel shows a badge with the count. Expansion reveals the individual evidence rows or incidents. Angular does not decide sameness by comparing text, JSON, timestamps, or partial fields.

Structural clustering is separate from duplicate folding. Partial fills or order lifecycle rows may be grouped under a backend-authored `cluster_key` / `cluster_label`; repeated polling/evidence noise is folded under a backend-authored `fold_key` / `fold_count`. Angular must not derive either key from `perm_id`, `exec_id`, text, or JSON.

### 6. Stable activity stream and anti-flash contract

The Activity table is updated through incremental merge semantics, not by wholesale table replacement on every refresh.

The backend supplies one stable `visible_row_id` for every visible Activity row or folded group. The parent Activity tab and table component remain mounted. Row identity, expansion state, and scroll context should be preserved where possible. Visible highlighting or motion occurs only when a usable row is added or a visible fold count changes.

A **usable row** is a trader-relevant update: broker fill, order lifecycle event, trader-relevant broker evidence summary, or incident that changes what the trader can understand or act on. Low-level polling/file-refresh churn is diagnostic evidence, not a reason to redraw the primary table.

### 7. Audit and Configuration boundary

Configuration shows what the bot was intended and configured to run with.

Audit shows evidence of what actually happened and whether the evidence supports the intended configuration.

The same raw fact should not be duplicated as primary content in both places. If needed, one surface may summarize or link to the other as provenance. Runtime configuration should be presented as grouped, human-readable fields, not raw JSON; exact JSON may remain in technical details.

### 8. Deploy uses validated strategy packages

Deploy a strategy owns creating or selecting a **validated strategy package**. Engine Lab is not the package-authoring surface for this workflow, though package creation may select validation artifacts originally produced by Engine Lab, QuantConnect, or committed golden fixtures.

A validated strategy package binds:

- strategy implementation/spec;
- approved settings;
- golden fixture or parity evidence;
- backtest/audit provenance;
- deployable action-plan schema.

Package creation is a backend-owned registry operation. A package has a stable id, display label, strategy key, spec path and hash, audit-copy path and hash, backtest id, validation/golden-fixture references, optional settings schema and values, action-plan schema, validation status, and creation timestamp. The first implementation may store the registry in the repo's existing artifact substrate; the exact persistence mechanism is an implementation detail, but Deploy must consume a package object rather than loose raw fields once the package slice lands.

Packages are immutable by content hash. If any referenced spec, audit copy, fixture, or validation artifact changes or cannot be found at (re)deploy time, Deploy fails closed with a trader-readable package-evidence-drift message. Existing/running bot evidence is not rewritten when a package artifact drifts; the drift blocks new deploy/redeploy until a new package version is created.

Deploy should not ask the trader to assemble raw strategy paths, settings-file paths, backtest ids, and audit-copy paths as normal inputs. Technical paths and hashes remain provenance.

Strategy settings are shown only when the selected package requires tunable settings. They should be rendered as named, human-readable controls, not a raw settings-file path.

### 9. Bot name is the strategy instance identity

Deploy may prefill a random, trader-editable bot name. The final bot name is lifetime-unique, system-safe, and becomes the durable `strategy_instance_id`.

There is no separate display-only bot-name variable. The value is used for Bot Cockpit identity, paths, ownership, and broker attribution, subject to the existing strategy-instance and order-ref constraints. A stopped or retired bot name is not reusable because its paths and broker `order_ref` namespace remain durable evidence.

### 10. Broker account is display-only deploy evidence

Deploy displays the currently connected broker account as read-only evidence. Traders do not type broker account identifiers.

If the connected account is unavailable or ambiguous, deploy fails closed and explains the issue in trader language. This intentionally removes the current manual-account escape hatch; a broker outage blocks new deploy/redeploy until the connected account can be observed.

### 11. Trader-readable action-plan instruments

Deploy action plans use rich instrument pickers rather than raw stock/option entry rows.

Stocks should surface recognizable symbol/company/exchange context when available. Options should surface underlying, expiry, strike, call/put, multiplier, and quote context when available. Raw contract identifiers remain technical details.

### 12. PrimeNG-first, token-themed UI

Bot Cockpit and Deploy UI should prefer PrimeNG components for tables, accordions, badges, panels, forms, dropdowns, pickers, and dialogs. Apache ECharts remains appropriate for charting.

Existing bespoke controls should be replaced with PrimeNG when the replacement is straightforward and preserves behavior. Custom CSS is limited to layout and app-theme glue.

Broker/audit evidence surfaces use the app's theme tokens for contrast, severity, spacing, and emphasis. One-off hard-coded colors are avoided; PrimeNG components should be styled through the app theme/token layer.

## Consequences

**Positive:**
- Traders read Bot Cockpit in trading language rather than API/control-plane language.
- Activity cannot silently hide durable trade evidence merely because the preferred broker-activity projection is missing.
- Folding and refresh behavior become testable backend contracts instead of fragile frontend inference.
- Audit and Configuration stop competing as raw artifact viewers.
- Deploy becomes a controlled strategy-package workflow, reducing path/ID assembly mistakes.
- PrimeNG and theme tokens reduce bespoke UI code and contrast drift.

**Negative:**
- Backend work is required before UI polish: narrative registry, fold keys/counts, repair/reconstruction paths, and deploy package contracts.
- Repair/reconstruction is not wired into production Activity today; the first implementation slice must add the trigger, idempotency, and cost controls.
- Existing frontend components that currently format raw fields will need migration rather than superficial relabeling.
- The single bot-name/strategy-instance identity is simpler, but stricter: generated names and user edits must pass uniqueness, path-safety, and broker `order_ref` constraints.
- Incremental merge behavior requires stable row/fold identities and tests; simple full-response replacement is no longer acceptable for Activity.

**Non-consequences:**
- `int64 ms UTC` remains the storage and wire timestamp format.
- Angular may still format ET display time, currency, numbers, and component state.
- ECharts remains valid for charts.
- ADR 0013 and ADR 0014 remain in force: frontend does not derive operational verdicts or broker narratives.

## References

- `CONTEXT.md` — glossary entries created during the 2026-06-25 grilling session.
- `docs/architecture/adrs/0013-operator-surface-judgment-vs-evidence.md` — frontend/backend judgment boundary.
- `docs/architecture/adrs/0014-broker-authored-operator-view-backend-rendered-narratives.md` — backend-authored broker activity narratives.
- `PythonDataService/app/services/broker_activity_reconstruction.py` — existing repair path for missing broker-activity rows from raw callbacks or legacy execution artifacts.
- `PythonDataService/app/routers/live_instances.py` — current Activity projection and evidence-row construction.
- `Frontend/src/app/components/broker/cockpit-v2/tabs/activity-tab.component.ts` — current Activity projection consumer.
- `Frontend/src/app/components/broker/cockpit-v2/reused/broker-activity-table/` — current Activity table rendering.
- `Frontend/src/app/components/broker/broker-deploy-form/` — current Deploy a strategy form.
- Interactive Brokers Client Portal and Account Window documentation — trader-facing account/portfolio/positions/trades language.
- Nielsen Norman Group error-message and usability-heuristic guidance — plain language, precise diagnosis, and minimizing obscure codes.
