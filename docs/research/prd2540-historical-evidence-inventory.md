# Historical evidence inventory for #2540

Read-only local inspection on 2026-09-27. This inventory covers the running
`my-postgres` application database and validation manifests available inside
`polygon-data-service`, `alpaca-paper-clerk`, and `alpaca-live-clerk`. It does
not establish the state of other installations or backups. No evidence,
acceptance, account, or runtime configuration was changed.

## Golden validation

A PostgreSQL `BEGIN READ ONLY` transaction against the `postgres` database
found zero rows in `research_validation_golden_runs` and zero rows in
`research_golden_validation_reviews`. Consequently this local database has
no persisted Golden acceptance to classify or invalidate.

## Legacy validation flags

The data-service runtime flag file contains 18 events: 15 validations and
three invalidations. The ordinary read-only manifest loader, combining seed
and runtime events, produced the following current decisions:

| Strategy | Current event | Decision |
| --- | --- | --- |
| `deployment_validation` | `seed-deployment-validation-accepted-for-deploy` | accepted for deployment |
| `ema_crossover_signal` | `c438d6ba08164a469a3553d0ec814c9f` | accepted for deployment |
| `rsi_mean_reversion` | `fc57d1d112cd47ec9907b6b324fbd1d3` | evidence only |
| `sma_crossover` | `828477c6cc8c4bd6954c9f0095a0dd1a` | evidence only |
| `spy_strategy_a` | `bfdc760bb6494032bdb1cec88225e935` | evidence only |
| `spy_strategy_b` | `f1046562842241cdb83ce1ce23d28e0a` | evidence only |
| `spy_strategy_c` | `43c75c96f4e64dc4ac11fd45fd389666` | rejected / needs validation |

Neither Clerk container has a runtime flag file at the loader's default
path. Their manifest projections contain the seeded acceptance events for
`deployment_validation` and `ema_crossover_signal`; they do not contain the
data-service's later runtime events. This is a scoped inventory of the
observed files, not an assertion that all running processes use identical
validation inputs.

These legacy snapshots record artifact references, hashes, and behavioral
decisions, but no explicit data-completeness, statistics-basis, or daily-return
conventions. Their applicability to the fixes in #2445/#2446, #2447, and #2448
is **unknown**. The inspection found no positive provenance establishing an
affected acceptance; absence of such provenance also does not prove that
the old metrics used corrected conventions.

Original decisions, artifacts, and hashes remain unchanged. The admission
receipt now uses the same provenance assessment as Golden validation to
disclose that uncertainty. New Golden runs record producer conventions;
their existing validation authority provides rerun, comparison, and explicit
review, including the deliberate Manual override where required.
