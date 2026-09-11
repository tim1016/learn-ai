# ADR 0061 — Golden Validation is a scoped human promotion over immutable run evidence

**Status:** Accepted 2026-09-11
**Provenance:** Explicit product decision by the repository owner, 2026-09-11. The owner chose a research pipeline in which a reviewer can advance a strategy configuration after inspecting engine evidence, including documented differences and explicit overrides, so that Paper and Live experimentation are not stalled by non-material parity deviations. This is not a decision to build grid search or forward optimization; those may later nominate runs for this workflow.
**Vocabulary:** `CONTEXT.md` § "Golden Validation (resolved 2026-09-11)".
**Amends / supersedes:** This ADR supersedes ADR 0020 Decision 1 and its anti-pattern against per-symbol validation **only for the strategy-validation gate**: Golden Validation is configuration-scoped, not a strategy-wide claim. It amends ADR 0023 Decision 1 and ADR 0020 Decision 5 **only where they make the v1 strategy-level `validated` / `accepted_for_deploy` projection the definition of that gate**. Their existing events and projections retain their original meanings. It does not amend ADR 0044's two categories, ADR 0054's corpus coverage and account-mode policy, or ADR 0058's parity-verdict semantics; it recognizes the later v3 parity payload as additional evidence, without reinterpreting historical v2 verdicts.

## Context

The current vocabulary conflates three different uses of “golden” and two different facts called validation:

- A **golden fixture** proves a mathematical port against a pinned reference.
- A Signal Program's **golden qualification corpus** proves that specific program bytes were tested at its registered corpus point.
- A researcher wants to choose one completed Strategy Lab history run as the baseline they are prepared to advance for a concrete configuration.

The first two are reproducibility and program-build concepts. The third is a human promotion decision. Treating it as another fixture would imply that selecting a run certifies the port, force a change to `validated_settings` or `golden_trace_root`, and violate ADR 0054's rule that ordinary Paper experimentation must remain possible without a qualification certificate.

Likewise, the computed comparison and the reviewer's decision are not the same fact. A paired Python/LEAN history run might produce 63 and 64 trades. That divergence is valuable, inspectable evidence; it is not an instruction to stop all research. Conversely, an unavailable, incomplete, or corrupt comparison must never be displayed as engine agreement merely because a human permits progress. The platform needs to state both facts honestly.

ADR 0020 and ADR 0023 model a v1 strategy-level validation event, originally anchored to QuantConnect evidence. ADR 0058 models parity verdicts for particular pairs of persisted backtest runs. Historical v2 verdicts retain their original payload and meaning; newer v3 verdicts add explicit receipts for exact resolved parameters, data identity/window, entry and exit timestamps, and synthetic-exit treatment. Neither a v1 event nor either parity-verdict version is a reviewer-selected, exact-configuration promotion record, and none may be silently reinterpreted as one.

## Decision

### 1. A Validation Golden Run is a selected completed history run, not a claim of optimality

A **Validation Golden Run** is a completed Strategy Lab history run that a user designates as the baseline for a proposed promotion. “Golden” means *user-selected baseline*; it does **not** mean best-performing, profitable, optimal, statistically sufficient, or universally validated.

The designation is always bound to one exact **Golden Validation configuration**:

- strategy / Signal Program identity and exact program or strategy version;
- signal ticker;
- fully resolved parameters;
- historical data identity and time window; and
- execution assumptions used by the run.

The configuration is an identity, not a similarity score. A changed ticker, program version, resolved parameter, data/window, or execution assumption needs its own applicable Golden Validation record. A prior record remains immutable history; it may later be shown as inapplicable or stale, but is never edited to pretend it covered the new configuration.

New run-history records capture `program_version` when they are persisted. A historical run whose version is null cannot prove the exact program-version member of this configuration and therefore can advance only through a **Manual override**. No version is inferred from a strategy name, a later registry entry, or a reviewer’s recollection.
To make such an override applicable rather than merely archival, the reviewer must explicitly name the exact program version being authorized; that value is retained on the immutable review and remains visibly an override.

### 2. Computed evidence and human promotion are separate immutable facts

The comparison attached to a selected run records one **engine evidence result**:

- **agreement** — both engines completed comparable evidence and the computed result agrees under its declared comparison;
- **divergence** — both engines completed comparable evidence and the computed result differs;
- **unavailable** — no comparison result is available;
- **incomplete** — a required run, input, or comparison is unfinished or missing; or
- **corrupt** — evidence is unreadable, contradictory, or fails integrity checks.

This result is computed evidence. It is frozen with the run evidence and is never rewritten by review. In particular, a 63-versus-64-trade result remains a divergence after acceptance, and corrupt or missing evidence remains corrupt or missing.

A reviewer separately makes a **human promotion decision**. A decision that accepts a configuration is a **Golden Validation record** and must contain the immutable evidence result, the reviewer identity, time, exact configuration, selected runs, and a required explanatory note. The accepted classification is exactly one of:

- **Engine agreement** — the reviewer accepts computed agreement.
- **Reviewed deviations** — the reviewer accepts a computed divergence and explains why it is acceptable for this baseline.
- **Manual override** — the reviewer explicitly accepts progression despite unavailable, incomplete, or corrupt evidence, or despite a Python-only run whose parity is absent.

`Engine agreement` and `Reviewed deviations` require the corresponding computable result. `Manual override` is the only acceptance classification for unavailable, incomplete, corrupt, or Python-only evidence. It is a conspicuous decision, not a relabeling of the evidence as valid.

### 3. The MVP begins with paired history; Python-only designation is an explicit exception

The normal MVP input is a user-selected paired Python+LEAN history run for the same Golden Validation configuration. Its comparison supplies the engine evidence result.

A completed Python-only history run may also be designated. Its record must disclose parity as missing or unavailable and may advance only through an explicit **Manual override** until a paired attempt exists. A later paired attempt supplies new evidence for review; it does not mutate the Python-only result, its override, or any prior record. A reviewer must make a new Golden Validation decision to rely on the paired evidence.

QuantConnect Cloud backtest ID is optional supplementary evidence. It may enrich review where present, but it is neither required for a Golden Validation record nor a substitute for the selected history evidence.

### 4. An accepted Golden Validation record satisfies one scoped gate, not all safety gates

Any of the three accepted classifications may satisfy the **strategy-validation gate** for its exact Golden Validation configuration in Paper or Live. This is the permission the research pipeline needs to keep moving; it is not a general waiver.

An accepted record does not bypass any other admission condition, including broker authority, account mode and custody, arming, risk-envelope, data/corpus coverage, execution safety, or the permanent Live exclusion for an ADR 0044 operational validation harness. ADR 0054 remains unchanged: `corpus_coverage` and account mode are separate build/admission facts, and an exploratory Paper run does not become corpus qualification because it is Golden Validated.

The applicable-record check is deliberately exact. A record may be stale or inapplicable when its configuration no longer matches or a declared freshness policy says it no longer applies. That applicability projection is not an edit to the record, its engine evidence result, or its human decision; a Manual override remains visibly an override rather than being silently converted into a different result.

Paper/Live admission projects that exact case onto the facts a future-running bot can truthfully carry: strategy identity, exact program version, signal ticker, and the fully resolved parameter set. The selected case's historical data identity/window and backtest execution assumptions remain frozen provenance on the Golden Validation record; admission does not manufacture equality by pretending that a future live run has the same historical window or simulated fill model. A fresh Start may select the newest applicable record for those four deployable facts. Resume is pinned to the exact Golden review named by its immutable Signal Program seal and fails closed if that review is later stale, rejected, unreadable, or inapplicable.

### 5. Golden Validation is distinct from fixtures, corpus qualification, and existing validation evidence

The following concepts remain separate:

- A **golden fixture** remains a deterministic reference-derived mathematical test. It is not selected by an operator and does not grant Paper or Live eligibility.
- A **Signal Program corpus qualification** remains the build/corpus receipt keyed by program version and `golden_trace_root`. A Golden Validation record never moves `validated_settings`, `golden_trace_root`, or the registered corpus point.
- A **v1 strategy-validation event** remains the strategy-level human flag and behavioral-equivalence evidence defined by ADR 0023. Its existing `validated`, `invalidated`, and `accepted_for_deploy` semantics do not change.
- A **historical v2 parity verdict** remains the computed comparison of its persisted run pair under ADR 0058. Its payload, statuses, and lifecycle do not change.
- A **v3 parity verdict** remains the same kind of computed pair evidence, with added exact parameter, data/window, entry/exit timestamp, and synthetic-exit checks. Those checks add evidence for newly written v3 verdicts; they do not backfill or revise v2 history.

Golden Validation may cite any of those as evidence, but it creates no backfilled fixture, corpus receipt, v1 event, or v2 parity verdict. Conversely, none of those facts automatically creates a Golden Validation record.

### 6. Lifecycle and migration preserve history rather than rewriting it

The lifecycle is:

1. A researcher completes one history run, normally a paired Python+LEAN run, for an exact configuration.
2. The user selects it as a Validation Golden Run; the system captures the available engine evidence result and warnings.
3. A reviewer accepts it under one of the three classifications with a required note, creating an immutable Golden Validation record, or declines promotion.
4. Deployment evaluates whether that record is applicable to the requested exact configuration and then evaluates every other independent Paper or Live gate.
5. Subsequent runs, changed configurations, newly available evidence, or a paired attempt create new candidate evidence and, if accepted, new records. They never overwrite history.

There is no automatic migration from existing v1 strategy-validation events or parity verdicts of any version to Golden Validation records: neither contains the owner-selected baseline and required promotion note. A migration may surface those facts as selectable evidence, but only a new human decision may create a Golden Validation record. Existing records, events, and verdicts retain their original timestamps, wording, classification, and semantics.

## Consequences

- Reviewers can document a small, understood engine deviation and continue a research configuration into Paper or Live without falsifying the comparison result.
- Missing, incomplete, or corrupt evidence is visible as a warning and cannot quietly qualify as a valid run. Continuing in that state requires a discoverable Manual override and reason.
- A review is traceable to exact program/version, ticker, parameters, data/window, and execution assumptions. A new ticker or configuration does not accidentally inherit authority from a superficially similar one.
- New v3 parity evidence makes exact parameter, data/window, timestamp, and synthetic-exit differences inspectable before review; a historical run with no persisted program version remains an explicit Manual override case rather than a guessed match.
- Grid search and forward optimization remain outside this decision. Their future output can nominate history runs for the same Golden Validation lifecycle without changing what a record means.
- The new gate narrows the old strategy-wide promotion assumption. Existing v1 flags remain valuable evidence and UI history, but no longer stand in for configuration-specific Golden Validation where this gate applies.
- The owner accepts the operational cost of immutable review records, reviewer notes, explicit applicability checks, and clear presentation of evidence result beside human decision.

## Anti-patterns rejected

- Calling a reviewed 63-versus-64-trade divergence “engine agreement.”
- Calling missing, incomplete, or corrupt evidence a valid run merely because a reviewer permits advancement.
- Replacing a prior evidence result, reviewer note, or record when a better paired run later appears.
- Treating a Golden Validation record as permission for a different ticker, parameter set, window, execution model, or program version.
- Moving `validated_settings` or `golden_trace_root` to make a selected run look qualified.
- Treating a Golden Validation record as a golden fixture, a Signal Program corpus receipt, a v1 strategy-validation event, or a parity verdict of any version.
- Treating a null historical `program_version` as if a strategy name or later registry lookup proved the run's exact version.
- Letting a Python-only designation display as paired parity or as anything other than Manual override while no paired attempt exists.
- Letting any Golden Validation classification bypass broker, custody, arming, risk, corpus, or other Paper/Live safety gates.
- Inferring that “golden” means the highest-returning or otherwise optimal configuration.

## Unresolved contradiction surfaced by this ADR

ADR 0020's original “validate once per strategy, never per symbol” language and ADR 0023's strategy-wide `accepted_for_deploy` projection conflict with the owner’s exact-configuration gate. This ADR resolves the conflict narrowly by superseding those clauses for Golden Validation while preserving the old facts and their semantics. The remaining implementation question is how the existing deployment surface transitions from the v1 strategy-wide projection to the new applicable-record check; no code or migration is authorized by this documentation decision alone.

## References

- ADR 0020 — original strategy-level validation decision, amended as stated above.
- ADR 0023 — v1 human flag and behavioral-equivalence event, amended as stated above.
- ADR 0044 — production candidate versus operational validation harness.
- ADR 0054 — corpus coverage remains independent of Golden Validation.
- ADR 0058 — Python-owned backtest runs and parity verdict semantics, including preserved v2 history and later v3 evidence.
- `CONTEXT.md` § "Golden Validation (resolved 2026-09-11)" — domain terminology.
