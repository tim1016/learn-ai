# Kill list — ADRs and architecture docs (#2712)

Part of map #2700. Read at master `87b8e261021ec673c2e7c80448c0973bccd45378`
(the map's charting SHA). Plan only: nothing is deleted from this ticket.

## How this was judged

- **ADRs.** ADR 0039 makes Status the decision's standing. A `Superseded` or
  `Retired` ADR goes. An `Accepted` ADR stays, even when partly amended, if any
  of its decisions still governs something that runs. ADR 0039 Decision 5 also
  counts *a named removal* as a valid displacer. So an `Accepted` ADR whose
  whole subject has been deleted is superseded in fact, and only its Status
  line is out of date. ADR 0009 got exactly this correction on 2026-09-30
  (`0009:3-4`). Such ADRs are cut here too, and the displacer is named for each.
- **Architecture docs.** A doc stays only if something live links to it (code,
  CLAUDE.md, a rule, a skill, a kept doc) *and* what it describes still runs.
  A catalogue row in `docs/doc-authority.md` alone does not count, because the
  index lists every doc that exists. This affects one row (`sse-job-streams.md`),
  and the question is raised for the map below.
- **Evidence.** Two things. First, every ADR's Status line. Second, a census of
  inbound references for each of the 84 files, made with `git grep -P` over
  tracked files and matching the filename stem, `ADR 00NN` / `ADR-00NN`, and
  `ADRs 00AA/00NN`. A second pass listed every markdown link into each cut doc
  and checked it against `scripts/check_documentation_contract.py`'s
  link-checked classes. Bodies were read only where status and links did not
  settle it.
- **Money path: when unsure, keep.** ADRs touching order identity, custody,
  flatten, the stop latch or the IBKR feed stay unless their subject is
  provably gone.

## Kill list — ADRs (14)

| Path | Kind | Displaced by | Evidence |
|---|---|---|---|
| `docs/architecture/adrs/0003-operational-topology-host-venv.md` | ADR (Accepted, out of date) | Host-daemon removal + ADR 0062 (clerk agents in containers own the read-only IBKR sessions) | Its decision is a host venv "managed by `host_daemon.py`" (`0003:24`), with containers that own no IBKR session (`0003:26`). `PythonDataService/app/engine/live/host_daemon.py` is gone, and CLAUDE.md:7-11 puts the IBKR connections on clerk agents. Inbound: "Related" prose only, in `0001:5` and `0002:5`. |
| `docs/architecture/adrs/0005-engine-authored-readiness-two-altitude-broker-ownership.md` | ADR (Accepted, out of date) | ADR 0038 + #1583 (IBKR runtime removal) | Its subject is `pre_flight.py` / `live_engine.py` readiness (`0005:11-12`). Both files are gone. CONTEXT.md:680-682 ("Readiness gate") and :735-737 mark it historical. Inbound: prose in `0004:11,54` and `0008:5`. |
| `docs/architecture/adrs/0006-deploy-control-plane-host-daemon-init-ledger.md` | ADR (Accepted, out of date) | Host-daemon removal; deploy now runs through the clerk (ADRs 0034, 0038, 0042) | Its deploy pipeline is `python -m app.engine.live.run init-ledger` plus the host daemon (`0006:14`). `app/engine/live/run.py` and `host_daemon.py` are gone. CONTEXT.md:1411-1413 ("Deploy binding and launch posture") is historical. Inbound: prose in `0011:12` and `0012:8`. |
| `docs/architecture/adrs/0007-host-daemon-shared-secret-auth.md` | ADR (Accepted, out of date) | Host-daemon removal | Code calls its token retired: `PythonDataService/app/installation_migration/contents.py:131` reads "Retired host-daemon token (ADR 0007); nothing reads it." The skill hits (`improve-codebase-architecture/SKILL.md:56`, `setup-matt-pocock-skills/domain.md:51`) are generic examples, not references. |
| `docs/architecture/adrs/0009-live-sizing-authority-and-provenance.md` | ADR (Superseded 2026-09-30) | Removal of everything it decided (#1583) | `0009:3-4`. |
| `docs/architecture/adrs/0010-operator-action-contract-flatten-pause-stop.md` | ADR (Superseded 2026-08-24) | ADR 0045 | `0010:8-9`. |
| `docs/architecture/adrs/0013-operator-surface-judgment-vs-evidence.md` | ADR (Superseded 2026-08-06) | Removal of the IBKR Bot Control surface. ADR 0036 restores the no-frontend-verdict rule for numeric boundaries | `0013:3-4`; `0036:10-13`. |
| `docs/architecture/adrs/0016-bot-control-trader-authored-activity-and-deploy-packages.md` | ADR (Accepted, out of date) | IBKR Bot Control removal; deploy selection now governed by ADRs 0042/0043/0061 | Its subject is the IBKR "Bot Control" console (`0016:10`). `broker/bots/:id` now redirects to `brokers/alpaca` (`Frontend/src/app/app.routes.ts:60`). §8's "validated strategy package" registry does not exist (no hits for `strategy_package`/`StrategyPackage` in `app/` or `Frontend/src/app`). Inbound: prose in `0020:10,87`. |
| `docs/architecture/adrs/0017-per-bot-lifecycle-workbench-node-explains-not-gates.md` | ADR (Accepted, out of date) | IBKR Bot Control removal | Its subject is the `broker/bots/:id` workbench (`0017:5`), which is now only a redirect (`app.routes.ts:60`). Zero inbound references outside one ADR. |
| `docs/architecture/adrs/0019-daemon-diagnostics-composed-control-plane-authority.md` | ADR (Accepted, out of date) | ADR 0038 (the evaluator plane retired) + host-daemon removal | Its subject is `app/engine/live/host_daemon.py` diagnostics (`0019:6`), and that file is gone. CONTEXT.md:1105,1115 says "Daemon diagnostics — historical evaluator plane (retired 2026-08-18)". Inbound: prose in `0026:61,271,325,340` and `0039:15`. |
| `docs/architecture/adrs/0024-bot-event-stream-narrated-gate-pipeline.md` | ADR (Accepted, out of date) | #1583 (producer and control surface removed) | Its subject is the `LivePortfolio` / IBKR `errorEvent` order path (`0024:13`). `LivePortfolio` has no hits in `app/`. CONTEXT.md:1448-1450 says "Historical bot event stream … producer and control surface removed by #1583". Inbound: the comment at `app/operator/notices/schema.py:53` (a reserved code). |
| `docs/architecture/adrs/0025-single-dominant-headline-notice-placement.md` | ADR (Accepted, out of date) | IBKR Bot Control removal (same displacer as 0013) | It arbitrates the IBKR bot-control page's "four independent banner sources" (`0025:9`). No live code does banner arbitration: no hits for `banner_winner`, `more critical`, `attention dropdown` or `placement`. The one notice renderer, `Frontend/src/app/components/operator-notice/operator-notice.component.ts`, has no consumer. Inbound: prose in `0015:204,236`, `0026:64,286,338`, `0027:5,20`; CONTEXT.md:1584,1606. |
| `docs/architecture/adrs/0028-bot-cockpit-operator-plane-authority-and-channel-contracts.md` | ADR (Retired 2026-08-06) | Never adopted; ADR 0038 is the control-plane authority | `0028:3-6`. |
| `docs/architecture/adrs/0041-generated-operator-button-reference.md` | ADR (Retired 2026-09-14) | #2060 (manual and generator deleted) | `0041:3-11`. **But** `0041:13-17` says "Decision 6 survives and is still enforced" through `PythonDataService/tests/broker/v2panel/test_vocabulary_snapshot.py:171,202` and the `broker-v2-vocabulary-contract` CI job. The test states the rule itself, so it does not need the ADR (see hazard 6). |

## Kill list — architecture docs (3)

| Path | Kind | Evidence |
|---|---|---|
| `docs/architecture/cross-stack-contract-inventory.md` | doc (inventory, out of date) | Zero inbound references. It is "Current as of issue #1126 (2026-07-20)" (`:2`), and it names `backtest-runs.query.ts` (`:25`), which no longer exists. `Frontend/src/app/graphql/` holds no `.graphql` documents, though `:56` says it does. The decision itself lives in ADR 0031, which does not link here. |
| `docs/architecture/sse-job-streams.md` | doc (design note) | The only inbound reference is the catalogue row `docs/doc-authority.md:187`. Neither the code it describes (`Backend/Jobs/JobsApi.cs:71`, `PythonDataService/app/jobs/phases.py`) nor any doc links to it. Last touched 2026-05-01. The cut turns on the index-row question below. |
| `docs/architecture/strategy-validation-deploy-rehome-prd.md` | doc (shipped PRD) | It shipped (last commit `0cacca4d` "fix: finish strategy validation deploy rehome", 2026-07-05), and ADR 0023 is its decision record (`:4`). It describes the retired `broker/bots/:id` Bot Control (`:2`). Inbound references are backtick paths only (`0020:6`, `0023:4,96`, `docs/references/deployment-validation-consecutive-green.md:72`), with no markdown links. |

## Kept (with the one line that keeps each)

**Superseded/Retired: none kept.** **Accepted ADRs kept (52):** 0001 (0049/0060
cite it as standing), 0002 (Alpaca `shadow_broker.py:112,703`,
`fill_models.py:23`), 0004 (amended by live ADR 0034; 0038's 09-27 amendment
names it; stop latch `desired_state.py`), 0008 (order identity —
`alpaca/clerk/fills.py:162`, `engine/live/order_identity.py:1`; money path),
0011 (extended by 0059; `alpaca-live-verdict.service.ts:83`), 0012
(`schemas/action_plan.py:8`, `engine/action_plan/parity.py:10`), 0014 (extended
by live 0015), 0015 (`app/operator/notices` imported by
`operator/incidents/store.py`, `schemas/live_runs.py`), 0018 (IBKR Gateway
plumbing, sacred — `ibkr/recovery_state_machine.py:3`), 0020/0021/0023
(amended by 0059/0061, still the validation/deploy rules), 0022 (CLAUDE.md:23),
0026 (§2/§3/§6 and the `STOPPED` latch still bind per its own banner
`0026:18-27`), 0027 (`broker_v2_panel/panel_projection_service.py:604`), 0029
(`app/services/session_authority.py:26` `ibkr_capability` source, used by
`alpaca/clerk/sqlite/runtime.py:176`), 0030–0040, 0042–0047, 0049–0067.
**Proposed kept:** 0048 (see "For the map").

**Architecture docs kept (14):** `alpaca-clerk-sqlite-pinned-contracts.md` (7
code refs), `alpaca-configuration-ownership-inventory.md`
(`broker_configuration/envelope.py:12`, ADR 0060:35),
`broker-configuration-profile-contract.md` (7 code refs),
`build-alpha-style-features-1-8-research-spec.md` (`research/runs/__init__.py:11`,
`auto-research-tick` skill :360), `edge-feature-design.md` (`engine/edge/*`,
`routers/edge.py:3`), `engine-authority-map.md` (CLAUDE.md, protected-canonical),
`ibkr-integration-tdd.md` (`docs/ibkr-integration-authority.md:151`; IBKR feed
rationale), `iv-ownership-research.md` (`services/iv_recorder.py:8`,
`Backend/Configuration/IvRecorderOptions.cs:6`), `lean-sidecar-lab.md` (22 code
refs), `lean-sidecar-mission-critical.md` (`routers/lean_sidecar.py:1334,1460`,
`lean_sidecar/cross_reconciler.py:15`, OpenAPI description),
`numerical-authority-migration-plan.md` (CLAUDE.md, protected-canonical),
`options-math-authorities.md`, `options-research.md`
(`services/past-chain.service.ts:6`), `research-artifact-seam.md` (12 code refs).

## What the cuts orphan

- **ADR index rows** in `docs/doc-authority.md`: the 14 rows at :89, :91, :92,
  :93, :95, :96, :99, :102, :103, :105, :110, :111, :114 and :127. Also the
  supporting-doc row :187 (`sse-job-streams.md`).
- **One CI-checked markdown link:** `docs/architecture/adrs/0045-exposure-lifecycle-closure.md:4`
  links to ADR 0010.
- **Two served-doc GitHub links:** `docs/architecture-manual.md:620` (ADR 0013)
  and `:625` (ADR 0009), plus the identical lines in
  `Frontend/src/assets/docs/architecture-manual.md`.
- **Prose mentions in kept ADRs.** These are not links, so CI stays green, but
  each loses its target:
  - 0003 is named by 0001:5 and 0002:5.
  - 0005 by 0004:11,54 and 0008:5.
  - 0006 by 0011:12 and 0012:8.
  - 0009 by 0011, 0012, 0020, 0021, 0023 and 0039:20.
  - 0010 by 0011, 0018, 0021, 0026, 0038:17 and 0045:6,20.
  - 0013 by 0012, 0014, 0015:275, 0018, 0027, 0036:10,13, 0039:74,80, 0047:6 and 0051:47.
  - 0016 by 0020.
  - 0019 by 0026 and 0039:15.
  - 0025 by 0015, 0026 and 0027.
  - 0028 by 0039:76 and 0046:6.
  - 0041 by 0045:6, 0047:6,31,50 and 0051:7.
  - The rehome PRD by 0020:6 and 0023:4,96.

  Leave these as history, or drop the citation. Never renumber: code comments
  cite ADRs by number.
- **Code and test comments that cite a cut ADR:**
  - 0007: `installation_migration/contents.py:99,131`.
  - 0009: `engine/live/intent_events.py:46,67,115`, `engine/live/intent_ledger.py:37,145`,
    `engine/live/live_state_sidecar.py:93` and `tests/engine/live/test_intent_ledger.py:158`.
  - 0024: `operator/notices/schema.py:53`.
  - 0028: `Frontend/src/app/services/versioned-snapshot-stream.ts:25`,
    `broker/ibkr/config.py:129` and `tests/services/test_surface_hub.py:1`.
  - 0041: `tests/broker/v2panel/test_vocabulary_snapshot.py:171,202`,
    `scripts/check_adr_status.py:59` and `scripts/test_check_adr_status.py:58`.
- **No fixtures, helpers, conftest or config** depend on any cut file. No
  non-markdown file names a cut doc by path.

## Hazards the cutting PR must carry

1. **ADR index parity.** `scripts/check_documentation_contract.py:208-226`
   (`_validate_adr_index`) fails "ADR index has no matching file" unless each
   deleted ADR's row leaves `docs/doc-authority.md` in the same commit. It runs
   in CI through
   `PythonDataService/tests/contracts/test_documentation_contract.py::test_validate_repository_current_repository_passes`.
2. **Link check on canonical docs.** ADRs are `canonical` (`:109-110`), and every
   local link in them is resolved (`:149-176`). Edit `0045:4` before deleting
   0010.
3. **Served copies.** `architecture-manual.md` is a byte-for-byte served doc
   (`:59`), and its GitHub `blob/master` links must name existing paths. Edit
   :620 and :625 in **both** copies identically. Point :620 at ADR 0036.
   Fact-check :625 rather than simply re-pointing it: it states as current a
   sizing rule whose ADR was superseded "by the removal of everything it
   decided". The manual belongs to #2713, so coordinate.
4. **`adr-status-guard`** (`.github/workflows/ci.yml:27-40`) loops over whatever
   ADRs exist, so deletions need no change. `check_adr_status.py:59`'s comment
   cites 0041 as an example, so update it or leave it.
5. **CONTEXT.md lineage labels** cite ADRs 0037/0038/0040, and none of those is
   cut. CONTEXT.md does name cut ADRs as authority (:82, :944, :1040, :1020,
   :1602, :1627, :1584, :1606 — e.g. "Authority: ADR-0025"). It is outside the
   link checker, so nothing fails, but #2713 should drop or re-home those lines.
6. **ADR 0041 Decision 6 is still enforced.** Before the ADR goes, reword the
   two citations in `test_vocabulary_snapshot.py` so the Literal↔collection
   parity rule reads on its own. The `broker-v2-vocabulary-contract` job stays
   (#2716 judges it).
7. **IBKR feed (sacred).** ADR 0003 is the only written record of the IBKR
   error-420 / Trusted-IPs co-location constraint. No code handles 420 today,
   and there are no hits under `app/broker/ibkr` or in
   `docs/ibkr-integration-authority.md`. If the fact still holds for Gateway,
   move one sentence into `ibkr-integration-authority.md` (#2713) before
   deleting 0003. Do not touch ADR 0018 or 0067.
8. **Optional resurrection guard.** Adding the three architecture docs to
   `RETIRED_DOCUMENTS` (`check_documentation_contract.py:40-47`) follows the
   existing pattern. ADRs do not need it: the index check already catches a
   revived file.
9. **Kill lists age.** Re-run the inbound census at the cutting SHA. After any
   docs edit, run `pytest PythonDataService/tests/contracts`.

## Pointers (cuttable things outside this area)

- **#2713 (authority docs, glossary):**
  - CONTEXT.md historical sections citing cut ADRs (above).
  - `docs/architecture-manual.md:620,625` and its served copy.
  - `docs/doc-authority.md` rows.
- **#2711 (one-off docs):** these supporting docs name cut ADRs as their subject:
  - `docs/audits/non-numeric-operator-verdict-census-2026-08-18.md` (0013/0041)
  - `docs/audits/contract-surface-drift-2026-08-18.md` (0041)
  - `docs/audits/live-operator-surface-inventory-2026-08-18.md` (0041)
  - `docs/audits/strategy-execution-research-directions-2026-08-24.md:143` (0010)
  - `docs/prds/alpaca-account-clerk-sqlite-control-plane.md:14,665` (0028)
  - `docs/superpowers/specs/2026-07-12-engine-lab-overhaul-design.md:13`
- **#2714 (math references):** `docs/references/lean-set-holdings.md:108` and
  `docs/references/two-bots-one-symbol-2469.md:418,478` cite superseded ADR 0009.
  `docs/references/deployment-validation-consecutive-green.md:72` points at the
  cut rehome PRD.
- **#2704 (engine dead code):** `engine/live/intent_events.py`,
  `intent_ledger.py` and `live_state_sidecar.py` cite superseded 0009. Check
  whether those IBKR-era modules are still reachable. `engine/live/exit_taxonomy.py:23-26`
  still names `host_daemon.*` sources.
- **#2702 (IBKR/migration dead code):**
  - `broker/ibkr/config.py:129` configures polling of "the daemon's batched
    `/instances` snapshot", but the daemon is retired.
  - `installation_migration/contents.py:131` keeps a retired-token entry.
- **#2703 / #2725:** `app/services` surface hub ("ADR-0028 Stage 2/3C") and
  `tests/services/test_surface_hub.py` implement a never-adopted ADR. Check
  reachability.
- **#2709 (Frontend dead code):**
  - `components/operator-notice/operator-notice.component.ts` has no consumer.
  - `services/versioned-snapshot-stream.ts` (ADR-0028).
- **#2716 (CI checks):** `adr-status-guard` and `check_documentation_contract.py`
  are judged there. Neither is lint, a build or a money/math/contract guard on
  its face.

## Not reviewed

- **Kept ADR bodies.** These were not checked clause by clause against the
  code. ADR 0039 makes Status the standing, and a partly amended ADR stays.
  Several kept IBKR-era ADRs (0004, 0008, 0011, 0012, 0014, 0018, 0020, 0021,
  0026) mostly describe retired surfaces and could be slimmed, but slimming is
  outside a kill list.
- **Accuracy of the 14 kept architecture docs** was judged only on live links
  and on the surface existing. `engine-authority-map.md` and
  `numerical-authority-migration-plan.md` are protected-canonical and were not
  read.
- **The census's blind spots.** It matched `ADR 00NN` forms, filename stems and
  `ADRs 00AA/00NN` lists. It misses en-dash ranges written without the `ADR`
  prefix and references spelled without leading zeros.

## For the map

- **Out-of-date `Accepted` statuses.** Nine of the 14 ADR cuts (0003, 0005,
  0006, 0007, 0016, 0017, 0019, 0024, 0025) still read `Accepted`, although
  their whole subject has been deleted. ADR 0039 Decision 5 already names a
  removal as a valid displacer, so this applies the locked rule rather than
  bending it. The cutting PR deletes the files outright; there is no need to
  first change their Status to `Superseded`.
- **Status contradicts content.** Two kept or cut ADRs say one thing in the
  Status line and another in the body:
  - **ADR 0041** reads `Retired`, but ADR 0039 says `Retired` means "never
    adopted", and 0041 says its Decision 6 still binds. It is cut anyway, with
    hazard 6.
  - **ADR 0048** reads `Proposed`, yet 26 code lines and 13 test lines in the
    clerk's SQLite custody build on it, e.g.
    `alpaca/clerk/sqlite/hold_migration.py:3,25`. It stays (money path). The
    fix under ADR 0039 is to promote it to `Accepted`, which is a one-line
    register edit, not a cut.
- **Rule question for the owner: does a catalogue row count as a live link?**
  A row in `docs/doc-authority.md` names a doc without anyone reading it. This
  ticket assumed it does not count, which cuts only `sse-job-streams.md` here.
  #2711 and #2713 will likely hit the same question.
