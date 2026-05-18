# Phase 5c progress (2026-05-17, follow-up PR — synthetic minute-quote staging)


Trusted-sample runs historically logged ``Cannot find file: ...quote.zip``
warnings classified as ``failed_data_requests`` — the last known-noise
category. The launcher's E2E test even special-cased the pattern. Phase
5c eliminates it by writing a synthetic minute-quote zip alongside the
trade zip for every staged trading day.

- **New writer** — ``write_lean_quote_day_zip`` in
  ``app/engine/data/lean_format.py``. Same path/CSV/ms-encoding shape
  as ``write_lean_day_zip`` but with the 11-column minute-quote row
  format (``ms,bid_o,bid_h,bid_l,bid_c,bid_size,ask_o,ask_h,ask_l,ask_c,ask_size``).
- **Zero-spread synthesis.** ``bid = ask = trade_close``, both sizes 0.
  Zero spread is explicit about what we DON'T have (real bid/ask
  information). Operators / auditors reading the manifest sees this
  in the source comment. A real-quote pipeline is a Phase 5d concern
  for algorithms that actually consume quotes.
- **Staging** — new ``stage_quote_bars`` in
  ``app/lean_sidecar/staging.py``, called by
  ``run_trusted_sample`` right after ``stage_minute_bars``. Quote
  zips land in the same ``equity/usa/minute/<sym>/`` dir as trade
  zips (LEAN looks up both by the same path prefix).
- **Manifest** — quote zips are appended to ``staged_data.bar_zips``
  so the reproducibility hash list covers every byte LEAN saw.
- **What this does NOT do.** Does not change the launcher's
  result_classifier or the E2E test's known-noise allow-list — once
  the next E2E run confirms the log goes clean, those special-cases
  become removable in a follow-up. Does not synthesize bid/ask
  depth — the size=0 keeps the synthesis honest.
- **Test surface** — 12 unit tests covering path/CSV/row-format/
  zero-spread/sizes/ms-encoding/DST-stability/co-location/symbol
  re-validation/empty-input. 250 lean_sidecar tests pass (was 238).
