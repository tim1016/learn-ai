# ADR 0021 — The deploy flow defaults to one-click paper execution (paper orders on, start-immediately loud, 2000/day), replacing read-only-first, behind a hard guardrail envelope

**Status:** Proposed 2026-07-05. Drafted during the 2026-07-05 `grill-with-docs` session (deploy-page redesign); vocabulary in `CONTEXT.md` § "Strategy validation & signal stream (sharpened 2026-07-05)" → "Launch-default posture (deploy)". Inverts the launch default that `Frontend/src/app/components/broker/broker-deploy-form/` ships today.

**Decision drivers:** The deploy form defaults to the *most cautious* posture and puts friction on the common case. Today: execution capability defaults to `READ-ONLY OBSERVATION` (`readonlyFlag = true`), "Start trading immediately" defaults **off** (launch options apply only when starting now), the daily order limit defaults to `DEFAULT_MAX_ORDERS_PER_DAY = 2`, and switching to paper orders pops a confirm modal ("Enable paper order submission?"). But this is a **paper-only research tool** — the broker identity is paper-verified and `UNSAFE`/live is fail-closed (ADR 0011). For that workflow, read-only-first + a modal + a 2-order canary means every routine paper deploy is a five-click ceremony that observes nothing. The operator wants the routine case to be one click, with the *danger* — not the routine — carrying the friction.

**Related:** ADR 0009 (live sizing authority & provenance — Safe-canary 1-share sizing is the guardrail that makes this safe), ADR 0011 (broker safety verdict, fail-closed reactive halt — the `UNSAFE`/live hard block this ADR leans on), ADR 0010 (operator action contract — flatten/pause/stop stay canonical, not surfaced on deploy), the 3-axis execution-posture model (`identity × submission_capability → effective_posture`). `CONTEXT.md` §§ "Launch-default posture (deploy)", "Actionable readiness gate", "Readiness gate".

## Context

The current default posture was correct when Deploy was the *only* surface and its safety story was "observe by default, opt into orders." Two things changed. First, `UNSAFE`/live identity is now a hard, fail-closed block independent of the deploy form (ADR 0011): paper-order submission cannot silently become real-money trading. Second, deploy readiness became an **actionable** surface (this session): Account `NOT_PROVEN` can be cleared in-place by reusing `reconcileAccount()`, and the gate re-evaluates server-side. The safety no longer needs to live in a defensive *default* — it lives in a **guardrail envelope** that holds regardless of the default.

Grilling settled the shape: flip the defaults, but make "start immediately" **loud**, keep the real guardrails hard, and move the one remaining hard confirm to where the actual danger is.

## Decision

### 1. Paper orders enabled is the default execution capability

The execution-capability default flips from `READ-ONLY OBSERVATION` to `PAPER ORDERS ENABLED` (`readonly_at_start: false`, `submission_capability: PAPER_ORDERS_ENABLED`). Read-only observation remains available as an explicit choice.

### 2. Start-immediately is default-on, rendered *loud*

Deploy auto-starts. Because a one-click deploy now creates paper fills immediately, the UI renders this **loudly** — a prominent launch banner stating plainly "this starts the bot now and submits paper orders to `<account>`," with the primary action reading **"Deploy & start."** Loudness is the mechanism; it is not optional chrome.

### 3. Daily order limit defaults to 2000

The daily order-limit default moves from `2` to **2000** — a practically-unthrottled ceiling. The per-day order count is explicitly *not* the safety canary; sizing is (§5).

### 4. The standalone paper-confirm modal is removed

The "Enable paper order submission?" modal existed because read-only was the default and paper was the elevated choice. With paper-orders-as-default, that friction is wrong. The **loud start treatment (§2) is the confirmation** for the normal paper case; there is no separate modal on a routine paper start.

### 5. A hard confirm/block is reserved for elevated conditions only

Friction is relocated, not deleted. A hard confirm or block fires only when the danger is real:

- **Broker identity `UNSAFE` / live-detected** → hard block (ADR 0011; never a mere warning).
- **Account `NOT_PROVEN`** → **hard-block the *start***, but still allow deploy-*without*-start (create the instance, don't submit). This is clearable in-place via the actionable readiness gate (`reconcileAccount()`), after which start unblocks.
- **Engine `UNREACHABLE` / Broker `HARD_DOWN` / Fleet `CONTAMINATED (blocks starts)`** → hard-block the start.
- `STALE_CODE`, `RECONNECTING`, `LINK_INTERRUPTED` → warn, do not block.

### 6. The guardrail envelope is the invariant, not the default

This default is safe **only** while three guardrails stay hard, and they are the load-bearing part of this ADR:

1. **Safe-canary 1-share sizing remains the default** (ADR 0009) — the real exposure guardrail. A 2000/day limit with 1-share sizing is a bounded, low-stakes canary. Changing the sizing default is out of scope here and would reopen this ADR's risk analysis.
2. **`UNSAFE`/live identity is a hard block** (ADR 0011).
3. **Account readiness gates the *start*** (§5), with in-place clearing.

If any guardrail is weakened, this default must be re-evaluated.

## Consequences

### Positive

- The routine paper deploy becomes one click; friction concentrates on genuine danger (live identity, unproven account).
- The default now actually *does something* (submits paper orders and starts), matching how the tool is used for research, instead of defaulting to a silent observer.
- No behavioral safety is lost: the fail-closed `UNSAFE` block and the account-readiness start-gate are unchanged and remain hard.

### Negative / costs

- A real posture inversion: muscle memory and any docs/screenshots that assume read-only-first are now stale.
- "Loud" is a design obligation, not a checkbox — if the loud treatment is weak, a one-click deploy can surprise a user with immediate paper fills. The banner is load-bearing.
- The removed modal means the *only* remaining hard confirm is the elevated-condition path; that path must be correct (especially the Account `NOT_PROVEN` start-block).

### Non-consequences

- Does not touch real-money/live trading — broker identity remains paper-verified and `UNSAFE` stays fail-closed.
- Does not change sizing authority (ADR 0009) or the operator action contract (ADR 0010); lifecycle actions keep their canonical Bot-Cockpit render site and are not surfaced on the deploy readiness strip.
- Does not change the order-execution or commission models.

## Anti-patterns this ADR rejects

- Treating "paper orders" as unconditionally safe — paper fills still mutate the paper account, so Account `NOT_PROVEN` must gate the start.
- Keeping read-only-first "to be safe" while the real guardrails already live elsewhere (defensive defaults masquerading as safety).
- A silent one-click start — start-immediately must be loud.
- Restoring a routine-case modal — friction belongs on elevated conditions, not every deploy.

## References

- `CONTEXT.md` §§ "Launch-default posture (deploy)", "Actionable readiness gate", "Readiness gate".
- `Frontend/src/app/components/broker/broker-deploy-form/broker-deploy-form.component.html` (execution-capability fieldset, daily-order-limit input, `showLiveConfirm` modal) — the surface this ADR inverts.
- `Frontend/src/app/api/live-runs.types.ts` (`DEFAULT_MAX_ORDERS_PER_DAY`) — the constant that moves 2 → 2000.
- `Frontend/src/app/services/broker.service.ts` (`reconcileAccount`) — the in-place gate-clearing mutation the `NOT_PROVEN` start-block relies on.
- ADR 0009, ADR 0010, ADR 0011; the 3-axis execution-posture model in `app/engine/live/runtime_producer.py`.
