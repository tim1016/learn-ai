# Phase 5b progress (2026-05-17, follow-up PR — reconciliation-grade trusted-sample template)


Phase 5a shipped the reconciler primitive but the bundled trusted
sample still used LEAN's default brokerage, so reconciler reports on
trusted-sample runs surfaced many `commission_drift` rows by design.
Phase 5b closes that with a NEW bundled template that pins IBKR
brokerage explicitly — runs of the reconciliation template come back
clean through the reconciler.

- **New sample** — `app/lean_sidecar/trusted_samples/buy_and_hold_reconciliation.py`
  exports `BUY_AND_HOLD_RECONCILIATION_SOURCE`. Identical to
  `buy_and_hold.py` except for one line:
  `self.SetBrokerageModel(BrokerageName.InteractiveBrokersBrokerage, AccountType.Margin)`.
  Keeps `fillForward=False` (invariant #13) and `DataNormalizationMode.Raw`
  (invariant #14) which were already present in the default sample.
- **API** — `TrustedRunRequestModel.template: Literal["trusted_default", "reconciliation"]`,
  default `"trusted_default"` for back-compat with Phase 4 clients that
  never sent the field. The dataclass `TrustedRunRequest.template` and
  the orchestrator's `_SOURCE_FOR_TEMPLATE` / `_BROKERAGE_POLICY_FOR_TEMPLATE`
  mappings keep template selection in one place (no `if/elif` chains).
- **Manifest** — `brokerage_policy="interactive_brokers"` when
  `template="reconciliation"`, else `"algorithm_default"` as before.
  `manifest.notes` also gets a new `trusted_template=<value>` line so
  an auditor reading the manifest can tell which template was staged
  (the field is set to `"user_provided_no_template"` when the caller
  pasted their own source).
- **UI** — when the "Custom algorithm" toggle is off, a new "Trusted
  sample template" dropdown lets the operator pick default vs
  reconciliation. The field is hidden (not just disabled) when the
  custom toggle is on, and the request omits the template field in
  that case — operator-pasted source picks its own brokerage via
  `SetBrokerageModel`, and sending a contradictory template would
  be a UX lie.
- **What 5b does NOT do** — does not stage real benchmark daily data
  (the reconciliation template keeps the constant `SetBenchmark(lambda dt: 100)`
  for now — benchmark mismatches affect LEAN's stats but not fill
  prices or fees, so the fee reconciler is unaffected). Does not
  stage quote bars (still produces the known-noise `_quote.zip not
  found` log line). Does not add a LEAN-Lab-vs-Engine-Lab trade
  reconciler — that's Phase 5c.
- **Test surface** — 10 new template-selection tests
  (`test_template_selection.py`) cover the dataclass default, the
  policy/source mappings, and source-string invariants (regression
  catches for SetBrokerageModel/fillForward/Raw/class name being
  edited out). 3 new router tests for the Pydantic field. 3 new
  frontend specs for default/reconciliation/omit-when-custom.
