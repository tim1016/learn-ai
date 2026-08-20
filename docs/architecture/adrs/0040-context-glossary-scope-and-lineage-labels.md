# ADR 0040: `CONTEXT.md` is one glossary of the live trading domain, and every entry declares its lineage

**Status:** Accepted

- **Date:** 2026-08-18
- **Context:** Wayfinder map [#1588](https://github.com/tim1016/learn-ai/issues/1588),
  decision ticket [#1595](https://github.com/tim1016/learn-ai/issues/1595).
  Grilling session: `grill-with-docs` + `domain-modeling`, 2026-08-18.
- **Vocabulary:** none owed — this decision is about the glossary's own scope and
  form, not about domain language. See Decision 4.

## What the file actually is

Measured at `9d6fe9c65`: 1533 lines, 26 sections, dated 2026-05-30 to 2026-08-17.

The ticket's premise was that `CONTEXT.md` describes a retired surface — its
header scopes it to *"the deployed-strategy operator console (the 'Paper Run'
page and its backend)"*, and every `broker/paper-run` route now redirects to
`brokers/alpaca/bots`. That is true of the header. It is **not** true of the
content, and the real problem is worse.

Searching for the word `IBKR` finds it in 8 of 26 sections. Searching for the
**artifacts ADR 0038 retires** — the ten identifiers `run_ledger`, `cmd_resume`,
`cmd_start`, `operator_surface`, `host_daemon`, `account_binding`,
`lifecycle_dispositions`, `bot_lifecycle_evaluator`, `desired_state.json`,
`process registry` — finds them in **13 sections spanning 1032 of 1532 lines**.

*(Method, so this is reproducible: split `CONTEXT.md` on its 26 `^## ` headings;
a section counts as a hit if any identifier appears anywhere in its body. An
earlier draft reported "2 of 26" for `IBKR` and a 1533-line file; both were
wrong. The 13-section / 1032-line figure reproduces exactly — a review that
re-ran it against only six of the ten identifiers got 8, which is what dropping
four terms costs.)*

The gap between those two numbers is the finding: keyword search misses five
sections outright and understates the affected span by hundreds of lines. "Sizing authority" is 178 lines
and reads as broker-neutral; its canonical term is
`run_ledger.live_config.sizing`, and `run_ledger.json` is one of the four deploy
artifact families ADR 0038 retires. Nothing in the section says so.

**So two-thirds of the glossary is rooted in machinery that is going away, and a
reader cannot tell which entries those are.** Looking up a term returns a
confident answer with no signal that it describes a dead system. That is a
glossary failing at the one job it has.

Two further facts, both established by reading the deleted file out of git:

- `CONTEXT.md:6` defers *"the full identity/control-plane term list"* to the
  retired paper-deployment plan's **§16.4** (available in Git history). That section is titled
  **"Cross-references"** and contains a PR-queue file map — ADR links and a list
  of code surfaces affected by PRs A–K. It never held a term list. The deferral
  was **wrong when it was written**, roughly three months before the file was
  deleted in the 2026-07-04 prune.
- Two more citations name *"§16.4 Resolution 5"* and *"§16.4 Resolution 7"*. The
  Resolutions live in **§16.1**. Those section numbers were wrong too.

Nobody noticed any of this because nothing ever checked. The newest section
before this map's work was dated 2026-07-27; ADR 0034, ADR 0035, the SQLite
execution-ledger expansion, the Broker Desk lens split, the Bot Gallery, and the
Market Scope shell all landed after it and contributed no vocabulary.

## Decision

**1. One glossary. Every section declares its lineage.**

`CONTEXT.md` stays a single file, and each section states which system its terms
describe:

- **live** — the current Alpaca Broker V2 ecosystem.
- **retiring (ADR 0038)** — machinery the IBKR bot-control retirement removes.
- **retiring (ADR 0037)** — machinery the Alpaca legacy-JSONL custody cutover
  removes.
- **neutral** — operator/trading vocabulary that survives a broker change.

**The two retirements are independent and must not share a label.** ADR 0037
governs an Alpaca custody cutover; ADR 0038 governs the IBKR bot-control surface.
Neither is implemented, and either may land first. A single `retiring` label plus
Consequence 8's "when the IBKR lineage is deleted, everything labelled retiring
goes with it" would archive the glossary for still-running Alpaca custody code if
the IBKR removal happened first. The label names its trigger, and a section
archives only when *its own* trigger fires.

A reader looking up *"live sizing policy"* must see immediately that it is rooted
in a retiring artifact.

**2. The glossary's scope is the live trading and operator domain — not repo process.**

The header's "Paper Run page and its backend" is replaced. Repo-process
vocabulary (ADR status values, lint rules, CI gates) stays out; ADR 0039's
deliberate abstention from a `CONTEXT.md` entry was correct and is confirmed here.

**3. The §16.4 deferral is deleted — but §16.3 next door is audited first.**

§16.4 really is a PR-queue cross-reference table, so the four citations go and the
header stops deferring authority it never successfully delegated.

**The pointer was off by one section, not simply wrong.** The retired
paper-deployment plan's § "16.3. Term Lock (deployment-specific glossary)"
sits immediately above §16.4 and *is* a 12-term table. Seven of those terms appear
nowhere in `CONTEXT.md`: `submit_mode`, `execution_source`, `Layer A divergence`,
`Layer B divergence`, `shadow_sim`, `NoSubmitBrokerAdapter`, and `(T3) topology`.
An earlier draft of this ADR asserted there was no term list to recover; that was
false, and deleting the deferral without looking would have discarded the pointer
to a glossary the header was one section away from naming correctly.

So: audit §16.3 against `CONTEXT.md` before the deletion lands. The likely outcome
is that most of the seven are shadow-mode / IBKR-divergence vocabulary and migrate
as **retiring (ADR 0038)** or not at all — but that has to be *concluded*, not
assumed. `CONTEXT.md`'s Identity ladder remains the identity term list.

**4. Every newly accepted ADR carries a `Vocabulary:` line.**

Unconditionally — naming the `CONTEXT.md` section it added, or stating that none
is owed. The condition is *not* "if it introduces domain language": no grep can
decide that predicate, and Decision 4 is only worth making if ADR 0039's gate can
check it. The author decides whether vocabulary is owed; the gate checks only that
the line exists. Existing ADRs are **not** back-filled — the rule applies from the
next accepted ADR forward, so the corpus is not rejected wholesale. This is not
a new invention — ADRs 0036, 0037, and 0038 each carry one, and ADR 0039 states
why it owes none. It is the observed working practice made into a rule, and it is
the obligation that was missing: for three weeks the glossary went unupdated
because nothing but a grilling session ever triggered an update, and feature work
never did.

It is also grep-checkable, so ADR 0039's CI gate can carry it.

## Considered and rejected

- **Archive the retiring sections now**, into a historical file — there is
  precedent, since `doc-authority.md` already lists the old IBKR operator manual
  as a "Historical IBKR operator record". Rejected on timing, not principle: the
  IBKR code has **not** been deleted yet, so archiving now would leave running
  code with its vocabulary filed under history. Anyone sent to work on it would
  find the terms archived. Decision 1 makes this a mechanical follow-up once the
  code goes, which is the right order.
- **Split into per-lineage contexts** — a root `CONTEXT-MAP.md` with separate
  `CONTEXT.md` files, the documented alternative in the `domain-modeling` skill.
  Rejected on two counts. The lineages are interleaved through the same
  directories rather than living in separate trees, so there is no seam to split
  on — the split would have to be drawn term by term, which is Decision 1 with
  extra files. And it would institutionalise a two-context structure at the exact
  moment one of the two is being deleted: a second context born to die.
- **Rewrite only the header.** The cheapest fix, and it addresses the stated
  premise. Rejected because the premise was the smaller problem: a corrected
  header still leaves 1032 lines a reader cannot date.

## Consequences

These are **not implemented**. This ADR is a decision; the corrections are
follow-up work.

1. **26 sections to classify, and mixed sections are split, not judged.** One
   label per section is the whole mechanism; a section carrying both lineages
   under a single label forces one of them to be mislabelled, and the reader is
   back to guessing — which is what this ADR exists to end. "Broker-facing
   identity" (155 lines) carries both and is split. An earlier draft allowed "a
   judgement call recorded in place"; that loophole is closed.
2. **One mixed section is load-bearing for other open work.** "Continue / Pause /
   Stop guards — shared resolver (legacy Resume naming)" is rooted in `cmd_resume`
   and `operator_surface` (**retiring**), but it also carries the **Continue vs
   Resume** distinction, which is **live** and which
   [#1593](https://github.com/tim1016/learn-ai/issues/1593) /
   [#1599](https://github.com/tim1016/learn-ai/issues/1599) depend on — the
   operator manual omits exactly that pair. Split this one deliberately.
3. **The header is rewritten** to Decision 2's scope; the "Paper Run page"
   framing goes.
4. **Four §16.4 citations deleted** — `CONTEXT.md:6`, `:9`, `:494`, `:548`. Two of
   them additionally cite the wrong section number (`Resolution 5` / `Resolution 7`
   are in §16.1). Delete rather than repoint.
5. **Missing vocabulary to add**: ADR 0034 (immutable strategy instances), ADR
   0035 (SQLite clerk authority), the SQLite execution-ledger expansion, the
   Broker Desk lens split, the Bot Gallery, and the Market Scope shell.
6. **ADR 0038's deploy-state naming lands here.** "Deploy state" names four
   artifact families; two retire, and the two survivors — SQLite registration/run
   folds, and runner JSON instance/run records — need two distinct names. ADR 0038
   consequence 6 handed this decision to #1595; it is now owed as vocabulary.
7. **The `Vocabulary:` line becomes checkable** by ADR 0039's gate. Until that
   gate exists, Decision 4 is as unenforced as the practice it formalises.
8. **Archival is a follow-up, not a non-decision — and it is per-trigger.** When
   the IBKR bot-control surface is deleted, sections labelled **retiring (ADR
   0038)** move to the historical record; when the legacy-JSONL custody cutover
   completes, **retiring (ADR 0037)** sections do. Neither pass touches the other's
   label. Decision 1 exists partly to make each pass safe.
