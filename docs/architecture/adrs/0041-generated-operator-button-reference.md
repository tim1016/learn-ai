# ADR 0041: The operator manual's Button Reference is generated from the backend copy map

**Status:** Accepted

- **Date:** 2026-08-18
- **Context:** Wayfinder map [#1588](https://github.com/tim1016/learn-ai/issues/1588),
  decision ticket [#1599](https://github.com/tim1016/learn-ai/issues/1599); the
  rendered-surface evidence in
  `docs/audits/live-operator-surface-inventory-2026-08-18.md`.
  Grilling session: `grill-with-docs` + `domain-modeling`, 2026-08-18.
- **Vocabulary:** none owed — this decision concerns how a document is produced,
  not domain language. Per ADR 0040 Decision 4.

## What the evidence showed

Three measurements, all against `9d6fe9c65`.

**1. Two vocabularies diverged, and only one badly.** The manual documents nine
closed vocabularies. **Seven** — station ids, station states, hold reasons,
reconciliation verdicts, channel states, phases, duty outcomes — are **fully
documented**, every code present. `DesiredState` misses one value (`PAUSED`).
`ActionId` misses nine.

*(An earlier draft called this "eight of nine fully documented" while naming
`DesiredState`'s gap in the same sentence. Seven, one small gap, one large one.)*

So it is not true that nothing kept the manual honest. Something did, for seven
of nine, and nearly so for the eighth. What failed is the list that **grew**: the
custody and recovery actions were added after the manual was written, and nothing
brought them in.

**2. The backend already authors all of it, correctly.** Every one of the 19
`ActionId` values has a `label` and an `explanation` in `OPERATOR_COPY`,
published to `broker/v2panel/vocabulary.snapshot.json`:

> `resume` → *"Create a new run of this unchanged strategy instance after backend admission."*
> `continue` → *"Let this paused live run evaluate bars again without changing its run ID."*

The Continue-versus-Resume distinction the manual omits entirely — the pair
`CONTEXT.md` went to trouble to disambiguate — is **already written, in the
backend, and correct**. The manual is hand-maintaining nine entries that
duplicate a complete and accurate generated artifact, and getting fewer of them
right.

**3. The coverage gate already exists and is already green.** A copy-coverage
contract test (`tests/broker/v2panel/test_vocabulary_snapshot.py`) fails when a
code lacks operator copy. An action therefore *cannot* enter the enum
undocumented. The machinery to keep a document honest was built, is passing, and
the manual simply never consumed it.

And from the live walk: the documented `start` action **does not exist in the
enum**, while its stated condition — *"when the bot's desired state is STOPPED and
it is OFF_DUTY"* — describes exactly the bot observed, whose rendered button is
**Resume**. The manual names the right situation and the wrong control, on the
state an operator is most likely to be reading about.

## Decision

**1. The Button Reference is generated from `OPERATOR_COPY`, not hand-written.**

Every `ActionId`, with its backend `label` and `explanation`. The documented set
*is* the enum, so it is consistent with what exists by construction: the phantom
`start` disappears because nothing generates it, and the eleven missing actions
appear because the enum contains them.

**2. The "When available" prose is dropped, not regenerated.**

The backend does not author availability — that is gate logic, evaluated per
request. It is not being replaced by a worse generated version; it is being
replaced by something better that already ships. The live walk found blocked
actions render **with their own runtime reason** — *"No attributed exposure
requires a flatten plan."*, *"No working order has both a durable Clerk reference
and broker identity."* — on a healthy, idle bot, under "Active command gates ·
2 ready · 5 blocked".

A condition computed at the moment of asking beats a prose condition that can
rot, and the rot is demonstrated: the hand-written "When available" half is
exactly where the `start` error lives.

**3. The same generation extends to the Glossary tables.**

They are already `Code | Label | Meaning` — the shape of `OPERATOR_COPY` — and
they are already correct. Generating them costs nothing extra, removes a second
hand-copy, and closes the `DesiredState`/`PAUSED` gap as a side effect. Correct
today is not the same as protected.

**4. One artifact, two renderings — and today there are two artifacts.**

`docs/broker-v2-operator-manual.md` is the source. The in-app view at
`/brokers/:broker/manual` must render **that** file, and the generation gate must
cover whatever the app actually loads.

This is not hypothetical housekeeping. `broker-v2-manual-page.component.ts:69`
loads `/assets/docs/broker-v2-operator-manual.md`, a **second committed copy**,
and the two have already drifted: the served copy documents 11 actions, the
repo-root copy 9. A root-only generator would leave CI reporting the canonical
file clean while operators read a stale page — the exact failure this ADR exists
to make impossible, reintroduced through the back door. An earlier draft asserted
the two agreed; the live walk did not check, and they do not.

So the contract covers both paths: the generator writes the canonical file, the
Frontend asset is produced from it (copied at build time or regenerated in the
same step), and Decision 5's `git diff --exit-code` gate runs over **both**. The
two copies are reconciled once, before the gate is switched on — the gate cannot
be the thing that discovers the drift.

**5. CI fails on a hand-edited generated block**, using the pattern the repo
already runs for the OpenAPI and GraphQL snapshots: regenerate, then
`git diff --exit-code`. Divergence of *content* becomes impossible; the gate
catches divergence of *process*.

**6. Generation is category-aware, and `ActionId` is pinned to what the gate iterates.**

Two seams make a naive "dump `OPERATOR_COPY` into a table" generator wrong:

- `OPERATOR_COPY` is not code-for-code. The emitted `DutyOutcomeKind` is
  `STOPPED`, but its copy is stored under the synthetic key `STOPPED_OUTCOME`
  (`vocabulary.py:167`) so it cannot collide with the desired-state sense of
  `STOPPED`; `duty_outcome_copy_key` (`:352-354`) translates. A direct dump would
  document `STOPPED_OUTCOME` as an emitted code, or attach desired-state copy to
  the duty outcome. The generator renders **emitted** codes, resolving copy
  through the same helper the projection uses.
- `ActionId` (a `Literal`, `vocabulary.py:83`) and `ACTION_IDS` (a tuple, `:104`)
  are maintained separately, and `ALL_VOCABULARY_CODES` unions only the tuple
  (`:338`). Adding a member to the `Literal` alone makes the request schema accept
  it while the copy test stays green and the generator omits it — recreating this
  ADR's exact drift, one level down. A parity assertion is owed
  (`set(get_args(ActionId)) == set(ACTION_IDS)`), or the tuple derives from the
  type alias. Measurement 3's "an action cannot enter the enum undocumented" holds
  only once that exists.

## Considered and rejected

- **Generate "what it does", keep hand-writing "when available".** Preserves a
  browsable conditions list, which is a real loss under Decision 2. Rejected
  because the surviving half is precisely the half with the demonstrated failure
  mode — `start` — and nothing in the hybrid prevents a recurrence unless the
  gate also checks prose, which it cannot.
- **Keep the manual hand-written; add a divergence gate.** Cheapest change to the
  artifact and it would have caught every problem found here. Rejected because it
  leaves the duplication in place: the same sentence is authored twice and can
  disagree in wording while both exist. It also does nothing about
  Continue/Resume being correctly described in the backend and absent from the
  document.
- **Extend a hand-written contract to the other eight vocabularies.** They are
  already complete, so this would have been work with no defect to fix. Decision 3
  reaches the same protection through generation instead.

## Consequences

These are **not implemented**. This ADR is a decision; the work is a follow-up.

1. **A generator is owed** — `OPERATOR_COPY` → a fenced block in
   `docs/broker-v2-operator-manual.md`, plus the CI regenerate-and-diff step.
   Nothing in this repo generates prose into a Markdown file yet, so this is new
   machinery, not a new caller of existing machinery.
2. **The manual loses its browsable "when can I use this" list.** That is a real
   cost of Decision 2 and should be stated in the manual itself, pointing the
   reader at the panel's command-gate table.
3. **Nine hand-written entries are deleted**, `start` among them. Their "what it
   does" text should be diffed against `OPERATOR_COPY` before deletion — where the
   hand-written sentence is better, improve the backend copy rather than lose it.
4. **`DesiredState`'s missing `PAUSED` closes** under Decision 3 without separate
   work.
5. **The in-app renderer must handle the generated block** unchanged; it renders
   the markdown, so this should be free, but it is untested.
6. **`clear_hold` documents a retiring path.** The manual scopes it to *"Legacy /
   unactivated JSONL accounts only"* — the authority ADR 0037 removes. Generation
   will keep emitting it while it remains in the enum; removing it is ADR 0037's
   retirement work, not this ADR's.
7. **`rebuild_from_mirror` and `reset_authority` will be documented before they
   are located.** The inventory found no rendering site for either. Generation
   documents what exists in the enum, which is correct — but an operator reading
   about an action they cannot find is a new, smaller gap worth tracking.
