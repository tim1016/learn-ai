# ADR 0039: An ADR's Status states the decision's standing, not the code's conformance

- **Status:** Accepted
- **Date:** 2026-08-18
- **Context:** Wayfinder map [#1588](https://github.com/tim1016/learn-ai/issues/1588),
  decision ticket [#1594](https://github.com/tim1016/learn-ai/issues/1594).
  Grilling session: `grill-with-docs` + `domain-modeling`, 2026-08-18.

## What the corpus actually looks like

Swept all 38 ADRs at `9d6fe9c65`:

- **Ten read `Proposed` while governing shipped behaviour**: 0009, 0010, 0011,
  0012, 0017, 0018, 0019, 0020, 0021, 0023. ADR 0019's Status line contains the
  phrase *"pruned 2026-07-04 after shipping in PR #910"* — it announces its own
  shipping inside the field that claims it is unproposed.
- **Nine of those ten have a Status line longer than 200 characters.** ADR 0018's
  is 882, holding the drafting session plus a paragraph of live-verified socket
  claims. ADR 0009's is 824. The correlation is near-total and is the root cause:
  once the field held a *narrative*, it stopped being a *state*, and nobody
  updates a history.
- **Three incompatible formats**: `- **Status:**` (10 files), `**Status:**` (23),
  `**Status**:` (5). No grep can check status today.
- **An ADR can hold several simultaneous statuses.** ADR 0008 has three Status
  lines — the header plus two inside amendment blocks — and one of them uses
  `Shipped`, a value that appears in no vocabulary.
- **`doc-authority.md` has no Status column.** Its ADR table is `| ADR | Decision |`.
  The word "proposed" appears in exactly two rows (0028, 0035) as an adjective
  typed inside a prose description. There is no competing status field, so the
  "index versus ADR file" conflict this ticket was opened on does not exist —
  there are two stale words in a description column.

The decisive counter-example for what `Accepted` can promise is ADR 0026. It is
`Accepted`, and its §4 — a pure `f(evidence) → phase` with a drift flag on read —
was **never built** (ADR 0038). `Accepted` therefore does not currently assert
that the code conforms, and has not for some time.

## Decision

**1. Status states the decision's standing. It says nothing about the code.**

An `Accepted` ADR is binding intent: an agent follows it. Where the code
disagrees, the code is wrong — that divergence is an open defect and belongs in
`docs/known-gaps.md`, which is already this repo's only durable home for open
defects. It is not a reason to doubt or downgrade the ADR.

**2. The ADR file is the sole authority on status.**

`doc-authority.md` indexes decisions; it has never had a status field. The two
stale "proposed" adjectives are **deleted**, not corrected — adding status to the
index would create the second authority this decision exists to prevent.

**3. Status is one closed value on one line. The narrative moves out.**

The field holds a value and, at most, a date. Everything the Status lines
currently carry — drafting sessions, verified claims, superseding relationships,
PR references — moves to its own `Provenance:` line or into Context. This is the
fix for the root cause: a one-word field is cheap to revisit, and an 882-character
one is not.

**4. One status per ADR, in the header. Amendment blocks may not carry Status.**

An amendment changes the decision; it does not acquire a standing of its own. An
ADR whose amendments changed its standing states that once, at the top.

**5. The closed vocabulary is `Accepted`, `Proposed`, `Superseded`, `Retired`.**

- `Proposed` — drafted, not yet binding. An agent may read it for context and
  must not treat it as a rule.
- `Accepted` — binding. Follow it.
- `Superseded` — replaced by a named later decision (ADR 0013's shape).
- `Retired` — abandoned without ever being adopted (ADR 0028's shape).

`Superseded` and `Retired` are genuinely distinct and both stay. `Shipped` is
**not** a status: under Decision 1 it answers the conformance question, which
Status does not ask.

**6. Status becomes mechanically checkable.**

One format, one closed value, one occurrence per file — a CI grep gate, sibling
to the temporal-rigor ban list. This is what obliges the field to stay current;
nothing did before, for the whole life of the corpus.

## Considered and rejected

- **`Accepted` means decided *and* conformant.** The stronger promise, and the
  one a reader probably assumes. Rejected on cost and honesty: promoting the ten
  would require verifying each against the code first, ADR 0026 would have to
  leave `Accepted` today — making the corpus look worse before better — and every
  future ADR would cost more to land. It also conflates two facts that genuinely
  came apart in ADR 0026, rather than letting each be stated.
- **Two axes: decision standing plus implementation state** (`Accepted / Partial`).
  The most honest option, and the only one where the ADR-0026 failure cannot hide.
  Rejected because it asks the repo to keep two fields current when one already
  went stale for 38 files — and `known-gaps.md` already carries divergence, so the
  second axis would duplicate a register that exists. Revisit if divergence keeps
  going unrecorded.
- **Give `doc-authority.md` a Status column and generate it from the ADR files**,
  following the `vocabulary.snapshot.json` precedent. Rejected as premature: a
  generated view is worth building when the source is trustworthy, and the source
  is currently three formats and five values. Decision 6's gate is the
  prerequisite, not the alternative.

## Consequences

These are **not implemented**. This ADR is a decision; the corrections are
register work.

1. **Ten ADRs need a read-through and promotion**: 0009, 0010, 0011, 0012, 0017,
   0018, 0019, 0020, 0021, 0023. Under Decision 1 this confirms only that the
   decision still stands — **no code verification**, and none should be implied.
   Any that no longer stand become `Superseded` or `Retired` instead.
2. **Three Status formats to normalize** across 38 files, to whichever single form
   the gate checks.
3. **ADR 0008's two amendment Status lines fold into the header**, and its
   `Shipped` value disappears with them.
4. **`doc-authority.md` rows 0028 and 0035 lose the word "proposed."** Deletion,
   not correction.
5. **The provenance now inside Status lines must be preserved when moved.** ADR
   0018's 882 characters include live-verified socket evidence that exists nowhere
   else; this is a move, never a trim.
6. **ADR 0026's never-built §4 becomes a `known-gaps.md` entry** under Decision 1.
   Already scheduled — [#1610](https://github.com/tim1016/learn-ai/issues/1610)
   item 2 marks the ADR superseded for Alpaca and flags the contradiction.
7. **A CI gate is owed** (Decision 6). Until it exists, this ADR's own rules are
   as unenforced as the ones that produced the drift.
8. **No `CONTEXT.md` entry.** ADR status is repo process, not trading-domain
   language, and `domain-modeling` holds that the glossary is a glossary and
   nothing else. That boundary — what `CONTEXT.md` is a glossary *of* — is itself
   the open question in [#1595](https://github.com/tim1016/learn-ai/issues/1595);
   this ADR assumes the narrow reading and should be revisited if #1595 widens it.
