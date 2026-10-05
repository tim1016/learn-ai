# EMA Previous Crossover LEAN trade fixture

- Reference engine: QuantConnect LEAN source commit
  `261366a7e26ae942df858ab20df4fef8fa07de67`, executed from image
  `sha256:3dd003372f1ef1981b4e80038e3f1c557f1fe414d1be531f485ef870f81a5771`.
- What the reference is: the rule itself is the owner's (#2902, 2026-10-05)
  and has no outside source. The oracle is LEAN's own execution of the trusted
  template `ema_previous_crossover`
  (`app/lean_sidecar/trusted_samples/ema_previous_crossover.py`, source SHA-256
  recorded in `output.json`). LEAN supplies the 15-minute consolidation, the
  EMAs, Wilders RSI(14), `SetHoldings` sizing, Interactive Brokers fees and the
  fills.
- Market input: the committed cross-engine capture
  (`tests/fixtures/golden/cross-engine-studies/_lean_data_capture/`), raw
  Polygon/Massive minute bars, regular session. Nothing is copied into this
  fixture: `input.json` names the two cells and the SHA-256 of each cell's
  committed `lean/observations.csv`. The generator refuses to write unless the
  minute bars LEAN reports receiving are byte-identical to that file, so the
  parity test replays exactly what LEAN consumed.
- Cells: `SPY_W6mo_2025-11-03_to_2026-04-30` (178 fills) and
  `QQQ_W6mo_2025-11-03_to_2026-04-30` (174 fills), each a full six-month run
  from the cell's own start date, so warmup is bar-for-bar identical on both
  sides.
- Protocol: $100,000 initial cash and the registered default settings, passed
  as runtime parameters: `gap=0.2`, `gap_bps=0.0`, `rsi_min=50.0`,
  `rsi_max=70.0`, `fast_period=5`, `slow_period=10`, `hold_bars=5`.
- Dividends: in raw mode LEAN pays a cash dividend on a position held over an
  ex-date. The generator derives each one from the capture's factor file with
  LEAN's formula (`Math.Round(close - close * pf_i / pf_i+1, 2)`,
  `Common/Data/Market/Dividend.cs`) and records it under `dividends`. The SPY
  run held 157 shares over the 2026-03-20 ex-date: $1.80 a share, $282.60.
  QQQ collected none. `final_equity` is fills plus dividends; the generator
  refuses to write unless it equals LEAN's reported end equity
  (`lean_reported_end_equity`) to the cent. The Python engine pays no
  dividends (#2903), so the parity test credits the recorded amount.
- Generated: 2026-10-05 with
  `PYTHONPATH=. python tests/fixtures/golden/ema-previous-crossover-lean/generate.py`
  from the `PythonDataService` directory (needs podman and the pinned image).
- Tolerance: timestamps, directions, quantities and fees are exact. Fill
  prices and final equity use absolute tolerance `0.000001` with zero relative
  tolerance; the committed fixture reproduces both with zero error.
