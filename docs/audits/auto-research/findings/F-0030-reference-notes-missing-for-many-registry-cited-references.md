---
id: F-0030
severity: P2
status: open
area: documentation
canonical_file: docs/references/
reference: docs/math-sources-of-truth.md (multiple rows)
first_seen: 2026-05-06
last_seen: 2026-05-06
phase: 10
---

## What

Phase 10 cross-check between registry's `Reference` column and `docs/references/<name>.md` files. Indicator reference notes are well-covered (24 of 24 rows have a corresponding doc). **Strategy and statistic reference notes are mostly missing.**

## Where

### Reference notes that exist (well-covered subset)

```
docs/references/{ad,adx,alma,cmf,dema,donchian,fisher,hma,kc,macd,mfi,mom,natr,
                 rma,roc,rsi,sma,supertrend,tema,vwap,willr,wma,zlma}.md
docs/references/options-bs-greeks-2026-04-24.md
docs/references/iv-rv-basis-alignment.md
docs/references/sharpe-ci-and-deflated-sharpe.md
docs/references/strategy-spec-layer.md
docs/references/polygon-throttle.md
docs/references/reconciliations/data-lab-spy-2026-04-17-to-2026-04-24.md
```

### Registry references with NO `docs/references/<name>.md`

| Registry concept | Reference cited | Note status |
|---|---|---|
| Bollinger Bands | pandas-ta (external) | no doc |
| IV term-structure | `docs/math-rigor.md` Upgrade 1 | no specific note |
| Risk-free rate | `docs/math-rigor.md` Upgrade 4 | no specific note |
| Bar consolidation, event replay, fill models | LEAN Engine | no doc |
| Max drawdown | Bacon, Practical Portfolio Performance Measurement (2e), §8.2 | no doc |
| Replay determinism | Internal invariant | no doc (acceptable) |
| SPY EMA Crossover | LEAN; TV parity (`docs/validation/*.pine`) | no `docs/references/` doc; `docs/validation/` is a different surface |
| SPY ORB | TV `docs/validation/*` | same |
| QQQ ORB | TV `docs/validation/*` | same |
| RSI Mean Reversion | LEAN | no doc |
| SMA Crossover | LEAN | no doc |
| Momentum RSI/Stochastic | LEAN (verify) | no doc; reference itself unverified |
| RSI Reversal | LEAN (verify) | no doc; reference itself unverified |
| Position valuation | Internal | no doc (acceptable) |
| Portfolio reconciliation | Internal | no doc (acceptable) |
| Portfolio scenario | Hull §19 | no doc |
| Portfolio live Greeks | Hull §19 | no doc |
| Bar divergence | `docs/tv-polygon-validation-gotchas.md` | doc exists at different path |
| Trade divergence | Internal | no doc |
| Dividend adjustment | "CRSP methodology (or similar — verify)" | no doc; reference itself unverified |
| Indicator reliability | `docs/indicator-reliability-methodology.md` | doc exists at different path |

## Why this severity

P2 — Reference notes are not the math itself; they're the audit trail that lets a reviewer confirm the math matches what was claimed. Indicator notes are well-covered; what's missing falls into three groups:

1. **Internal-only references** (Position valuation, Portfolio reconciliation, Replay determinism, Trade divergence) — arguably acceptable; "Internal" is honest provenance.
2. **External textbook references with no extracted note** (Hull §19 for Greeks/scenario/live-Greeks; Bacon for max drawdown) — moderate-effort to add; useful for reviewers.
3. **Unverified references** (Momentum RSI/Stochastic, RSI Reversal, Dividend adjustment) — flagged as `(verify)` in registry; per F-0030 these need either a confirmed reference + doc, or honest demotion to "external-unvalidated".

Plus two existing docs at non-canonical paths (`tv-polygon-validation-gotchas.md`, `indicator-reliability-methodology.md`) — could be cross-linked from `docs/references/<name>.md` aliases for discoverability.

## Reproduction

```
ls docs/references/*.md | wc -l                                            # 28 files
grep -E '^\| [A-Z]' docs/math-sources-of-truth.md | wc -l                  # ~30 registry rows
# Diff conceptually
```

## Suggested resolution (NOT auto-applied)

For the §6 hardening gate ("Reference notes (`docs/references/<name>.md`) exist for every reconciled port"):

1. **Cheap (one PR):** for the 4 "Internal" cases, add 2-line notes confirming "internal — no external reference" so the gate is closeable.
2. **Medium:** add Hull-citation extracts for portfolio scenario / live Greeks / Greeks rows. These can share one `hull-greeks.md` rather than per-row.
3. **Harder (research):** for the 3 `(verify)` references (Momentum RSI/Stoch, RSI Reversal, Dividend adjustment), either confirm + write the note, or change registry status to `external-unvalidated`.
4. **Cross-link:** add `tv-polygon-validation-gotchas.md` + `indicator-reliability-methodology.md` references in the registry so they're discoverable.

## Provenance of the finding itself

Phase 10 / cursor: `Glob("docs/references/*.md")` listed 28 files. Cross-checked against registry rows in `docs/math-sources-of-truth.md`.
