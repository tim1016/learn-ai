# ADR 0054 — Corpus coverage is a stamp on a proven paper account, and a blocker anywhere else

**Status:** Accepted 2026-09-03
**Provenance:** Operator decision in session, 2026-09-03, after a `grill-me` pass. The triggering refusal: *"This instance resolved parameters the golden qualification corpus does not cover."* raised on every paper deploy whose symbol or parameters differed from the registry's single `validated_settings` point.
**Decision drivers:** Paper trading is this repo's strategy-testing surface, and testing means many symbols, many strategies, and many parameter variations. The refusal above lumped that ordinary testing activity in with nineteen genuine build-identity failures under one reason code, and the only sanctioned way past it was to move a program's one blessed `validated_settings` point — which re-fingerprints the corpus and permanently strands every bot sealed against the old root (`docs/references/` and the 2026-09-01 EMA move, PR #1915, are the evidence). Live trading does not yet exist: every execution path refuses a non-paper account (`LIVE_ACCOUNT_REFUSED`), so "warn in paper, block in live" is currently "warn everywhere that can run", which must therefore be made a *fact about the account* rather than an assumption.
**Vocabulary:** `corpus_coverage` — whether the golden corpus behind a program's `golden_trace_root` describes the resolved parameter point and symbol a bot runs. `account_mode` on the Clerk custody snapshot — the environment (`paper` / `live`) the Clerk positively learned from the broker at activation; `None` means unproven. Both are defined where operators read them: `docs/broker-v2-operator-manual.md` § "Corpus coverage".
**Related:** ADR 0043 (the seal and the running-build proof) — **amended in one row**: `parameters_match_validated_settings` stays on the seal but is no longer one of the seal checks that refuses the proof. Issue #1735 / #1828 (wiring drift reported rather than blocking; the verdict a run started under is frozen with it) — the exact shape this decision copies. ADR 0023 (a human validation flag gates live behaviour) — untouched; this ADR adds no flag. PRD `docs/prds/sealed-signal-program-to-governed-alpaca-bot.md` §22.5 item 6 already asked that a deploy outside the validated fixture keep `parameters_match_validated_settings=false` *visible*; this ADR is what makes that run start.

## Context

`prove_running_program_build` (`PythonDataService/app/services/signal_program_admission.py`) proved a build in two steps: a table of seal checks — does the stored seal still name the registered program, version, trace root, account, mode, cadence, and so on — and then a receipt lookup proving the loaded bytes were the ones golden-qualified for that `(program_version, golden_trace_root)`. Any failing row refused the proof as `UNPROVEN`, and `run_admission.py` turned every `UNPROVEN` into `PROGRAM_BUILD_UNPROVEN`.

One row was different in kind from the other nineteen. `parameters_match_validated_settings` asks whether the corpus *covers* this configuration: is the symbol in `validated_symbols`, and does every resolved value equal the one `validated_settings` point. Every other row asks whether the sealed program still *is* the registered one. A bot at an uncovered point runs exactly the bytes that were qualified — the artifact and wiring digests still match their receipt — it simply does so at a parameter point nobody replayed. That is a fact about the *evidence* the run can later claim, not about the *code* it is about to execute.

Three facts made the cost of keeping that row as a blocker concrete:

1. **The refusal was the only way a tester learned anything.** The seal already records every resolved parameter with its origin and the coverage boolean; the panel already displays it. The gate decided only whether the bot could *start* — it created no evidence that did not already exist.
2. **The sanctioned bypass is destructive.** Moving `validated_settings` regenerates the corpus and moves the program's `golden_trace_root`; every instance sealed under the old root fails `golden_trace_root` *and* the receipt lookup and can never Resume again. The registry holds one point per program, so no revert satisfies more than one cohort of bots.
3. **The repo already had the right shape one layer down.** The wiring digest (#1735) reports drift as `wiring: DRIFTED` on a PROVEN fact, with a `next_step`, behind a toggle, and #1828 freezes that verdict into the run's durable evidence so a frozen run replays what it actually started under.

## Decision

### 1. Coverage is a field on the proof, not a reason to withhold it

`ProgramBuildAdmissionFact.corpus_coverage: COVERED | UNCOVERED | NOT_CHECKED` (default `NOT_CHECKED`) sits beside `wiring`. `prove_running_program_build` no longer lists `parameters_match_validated_settings` among the seal checks; it stamps `corpus_coverage` from the seal's stored boolean onto a PROVEN fact, appends `program-corpus-coverage:<value>` to `evidence_refs`, and — when uncovered — appends one shared sentence to the explanation and one shared remedy to `next_step`. A PROVEN fact that reports `UNCOVERED` without a `next_step` is refused by the model validator, exactly as a drifted one is.

The copy lives once, in `app/schemas/run_admission.py` (`CORPUS_UNCOVERED_EXPLANATION`, `CORPUS_UNCOVERED_NEXT_STEP`, `CORPUS_UNCOVERED_ADMITTED_NOTE`), and the live proof and the frozen-run replay compose it through the same two helpers (`proven_build_explanation`, `proven_build_next_step`), so a Start decision and that run's panel afterwards say the same thing.

### 2. Whether an uncovered point may start is the pure policy's decision, keyed off the account environment

`evaluate_run_admission` gains one gate, placed immediately after `PROGRAM_BUILD_UNPROVEN`:

> `corpus_coverage == "UNCOVERED"` and `clerk.account_mode != "paper"` → refuse `PROGRAM_CORPUS_UNCOVERED`.

`ClerkCustodySnapshot.account_mode: paper | live | None` is the carrier. The real SQLite Clerk fills it from the broker account it activated against — the same `read.get_account()` that already refuses a non-paper account — and the synthetic Dry Run authority reports `paper` by construction. `None` is any producer that did not prove it, and the gate treats `None` exactly like `live`: the relaxation can only ever be unlocked by a positive fact, never by the absence of one, and never by the shape of an account id.

The gate runs *after* the canary allowlist check, so on a paper `trade` run the closed `(program, account)` pairing is still required; the relaxation pre-empts nothing that already gated a proven build. It runs for Dry Run too: the synthetic authority is paper, so an uncovered Dry Run starts with the stamp.

### 3. The stamp is written where an operator reads, and frozen with the run

An admitted decision at an uncovered point carries `CORPUS_UNCOVERED_ADMITTED_NOTE` in its `explanation`, which the deploy launch receipt and the bot panel already render. `ProgramBuildRunEvidence` moves to `schema_version` 3 and records `corpus_coverage`; `program_build_view_from_run_evidence` replays it with the same explanation and remedy, and records written before schema 3 replay `NOT_CHECKED` — a true statement about a run whose coverage was never written down. `corpus_coverage` joins `wiring` and `schema_version` in the create-once conflict check's exclusion set: it is an observation about the run, not part of which run it is.

### 4. No configuration flag

The account environment is the single axis. A toggle mirroring `SIGNAL_PROGRAM_WIRING_DIGEST_ENFORCED` was considered and rejected: it would be a second switch to reason about, and a paper run that must be corpus-covered simply deploys at the validated point and reads `COVERED`.

### 5. Not decided here

- **Deploy-time qualification** — regenerating a corpus for the chosen point at seal time and minting a receipt for it. Rejected *for now*: a fingerprint minted with nothing to compare it against proves nothing the sealed parameter record does not already prove; the corpus is valuable because a human reviewed and pinned it and a test detects drift, and a self-minted root has neither property. It would also require a committed price fixture for every symbol before it could be deployed, which is the friction this ADR removes. Revisit when live trading exists.
- **Rescuing bots stranded by an earlier `validated_settings` move.** Not built. With this ADR no future testing needs to move the point, so nothing new is stranded; already-stranded instances are redeployed as new instances. Relaxing the `golden_trace_root` seal check alone would not have rescued them anyway — the receipt lookup keys on the same root.
- **Ranges for `validated_settings`.** Out of scope; the corpus stays one point per program.

## Consequences

- A paper deploy may name any symbol the broker serves and any parameter value the schema accepts; the run starts, its decision and its panel say it is exploratory and not citable as qualification evidence, and its per-run evidence keeps that stamp forever.
- Nineteen of the twenty causes previously folded into `PROGRAM_BUILD_UNPROVEN` are untouched, including code identity (artifact digest), the instance, account and mode checks, and the moved-trace-root check. Edited strategy code with no receipt still cannot start, in paper or anywhere.
- When a live account exists, nothing needs to change for the gate to block: the Clerk will report `live`, and `PROGRAM_CORPUS_UNCOVERED` refuses. Any new custody producer that forgets to state `account_mode` also blocks, loudly, rather than silently admitting.
- Two existing tests that pinned the refusal now pin the stamp; the `admit_lean_parity_settings_for_start_admission` test helper is no longer needed to *admit* a LEAN-parity-point deploy in tests, only to keep such a run's evidence `COVERED`.
- `ClerkCustodySnapshot` is on the wire, so the OpenAPI contract and the generated frontend types gain `account_mode` and `corpus_coverage`; the frontend renders the backend-authored `explanation`, no new UI.
