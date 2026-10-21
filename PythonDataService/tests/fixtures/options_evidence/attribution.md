# Options workspace retained read fixtures

Captured 2026-10-20 (America/Chicago) from the owner's local application for
[#3395](https://github.com/tim1016/learn-ai/issues/3395) through its read API and
repository. Implementation was developed against master `a3bd3a6f`;
runtime scientific identities remain in the receipts. These are retained application results, not independently
reproduced backtests or golden numerical validation.

`run76.json` and `run69.json` are unchanged JSON reads of
`GET /api/research/options-backtest/runs/{id}`. `golden76.json` comes from
`GET /api/research/golden-option-setups/ogs_dbe79f85399048f4a4decc3c86e1f3ff`;
only the bulky execution-proof payload is omitted (replaced by `{}`). Its
immutable identity, historical evidence and current proof status are retained.
`study-record.json` is the retained row read by the existing study repository
for `os_d7b4752086147e437a3a0a56ecc980f33d3c6055`, including frozen protocol,
development/forward projections and snapshot inventory. No final test was opened.

`development-evidence.json` holds the baseline development evaluation row and
its verified retained blob, read through `repository.get_evaluation` and
`stages.default_blob_store().get`. Its payload is 239,437 bytes; the blob digest
is retained in `record.result.evidence_blob`. The four observed sessions include
599 required held marks and two absent valuations at 1730925480000 and
1730925600000. A completed trade does not repair those missing intermediate
marks. The remaining 369 scheduled sessions were never computed.
`candidate-development.json`, `candidate-session.json` and `study-summary.json`
are the bounded read projections derived offline from those same bytes for the
browser journey; they are display fixtures, not a second numerical authority.
`failure-development.json` is the read-only diagnostic projection of the same
retained development evidence. Its input paths and digests come from the
recorded session pins; missing-mark causes remain unknown until a separate
targeted inspection verifies a specific archived input.

`lineage-receipts.json` was read 2026-10-20 from the same study's durable SQL
ledgers: evaluation counts (27 keys, 27 completed, 27 physical starts), 28 trial
events (21 evaluated, 7 cache hits, zero invalid), and one development exposure.
These are distinct accounting measures, not 28 independent strategies. Only
the first 51 trial receipts and first 201 exposure receipts can be captured;
this study has 28 and one respectively. No evaluation payloads or credentials
are included. `capture_lineage.py` reproduces this bounded read into a new file.
`lineage-read.json` and `lineage-trials.json` are deterministic display projections
of this fixture and `study-record.json`, generated offline by
`options_research_lineage.project` and `trial_page` for UI/browser checks.

`comparison76-69.json` is the unchanged existing ordinary-run comparison GET
for `a=76&b=69`: 499 shared sessions and no sessions unique to either run.
`neighbor-summaries.json` contains only the three development scalar receipts
returned by `repository.evaluation_summaries`, read 2026-10-20 in a read-only
repeatable-read transaction in the configured data-service container. Its keys
come from the frozen protocol and canonical `materialize`/`resolve` functions.
`neighbors-development.json` is the bounded projection of those receipts and
the retained study; all three declared points have partial diagnostic evidence.
`robustness-development.json` copies canonical metrics and capital evidence
verified from `development-evidence.json`. It exposes no final outcome.
The new display projections are reproduced by `capture.py` in a configured
application context, using existing read repositories and retained bytes.

These fixtures test identity and display projections. Canonical numerical
calculations remain covered by their existing engine fixtures/tests. No vendor
capture, replay, approval or configuration command was issued to capture them.
Regeneration requires the same source records and explicitly reviewing any
changed evidence. `capture.py` writes a new capture to a chosen directory;
it never overwrites these checked-in fixtures implicitly.
