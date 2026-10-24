# ADR 0085: A study run is recorded where it ends, in the artifact store, and a stored run is opened, never recomputed

Date: 2026-10-24

**Status:** Accepted 2026-10-24

Extends ADR 0084 (the History's addresses) and ADR 0072 (the research artifact store). Contexts: PRD #3490 R26 and slice #3520.

## Decision

1. **Five studies keep a record of every run:** Feature, Signal, Cross-sectional, Indicator reliability and Realized vs IV. Until now the first three held their result in the job store for 24 hours, and the other two held nothing.
2. **The record lives in the research artifact store** (`artifacts/runs/<study>/<record_id>/config.json` and `result.json`, through `ArtifactStore`), beside the walk-forwards and Monte Carlos. No table is added. The file store already serves a list that is read whole and filtered, and research DDL runs only from the data plane while the clerks hold SELECT only.
3. **A run is recorded once, where it ends, by one call at the job or route boundary** (`app/research/study_records/recorder.py`). It stores the inputs the request validated, the result exactly as delivered, how the run ended (`completed`, `partial`, `failed`, `cancelled`), the result's own `data_source` block when it carries one, and the record it was prefilled from. A study's computation is not changed and does not know. A result with no `data_source` block is stored with none, and the History reads "not recorded".
4. **A record is never changed, backfilled or invented.** Runs from before this decision stay as they were: the 24-hour cache entries lapse unrecorded, and the old `ResearchExperiments` and `SignalExperiments` rows stay readable under their own types (`feature-experiment`, `signal-report`). The new types are different stores, never the same type.
5. **A result served from the 24-hour result cache is not recorded again.** It is a copy of a run that has its own record.
6. **`partial` means the run finished and part of its work did not.** For Cross-sectional it is a run that evaluated fewer members than it was asked, with the counts and reasons in the status detail.
7. **Opening a record reads it.** Nothing between the History and a stored run starts a job, fetches bars or computes. **Rerun** is a separate, explicit action: it opens the study's page with the captured inputs filled in and runs nothing until Run study is pressed. The new run is a new record that names the one it came from, and its data identity is whatever the data is then.
8. **A record that cannot be written never fails the study.** The failure is logged with its reason, and a job says so in its own log.

## Why

- A list of "every study I have run" is only true if every study leaves something, whichever way it ends. A failed or cancelled run is the one the owner most needs to find again.
- Writing at the end keeps the hook to one call and keeps the studies free of persistence, which matters while their data sources are being moved to the lake on other branches: those branches add a `data_source` block to the result, and the record keeps it without any change here.
- A reload that recomputes can return different numbers from the ones the owner decided on. A stored run is evidence.

## Considered options

- **A Postgres table per study.** Rejected: DDL only from the data plane, a list that needs no query beyond filter and sort, and one more thing to roll back.
- **One universal schema for all stores.** Rejected by PRD R26: each store keeps its own models.
- **Record at the start and update at the end.** Rejected: it needs a second write and a way to recognise a run the process lost; a run that never ends is recorded as nothing, as before.

## Consequences

- Rolling back leaves the new directories in place. No older reader looks at them, and a record written by a newer build (a `schema_version` this build does not know) is counted as unreadable on the page, never shown as another record.
- A Realized vs IV run whose bars fail to load in the browser never reaches the server, so it leaves no record. The run is recorded from the request the series route receives. Moving that study's data to the server (#3525) closes this.
- History addresses: `/research/history/(feature-study|signal-study|cross-sectional-study|indicator-reliability|realized-vs-iv)/:id`.
