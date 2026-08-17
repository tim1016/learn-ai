# EMA Crossover 2 bps LEAN trade fixture

- Reference engine: QuantConnect LEAN source commit
  `261366a7e26ae942df858ab20df4fef8fa07de67`, executed from image
  `sha256:3dd003372f1ef1981b4e80038e3f1c557f1fe414d1be531f485ef870f81a5771`.
- Source receipt: local LEAN sidecar run
  `companion-pg-e4edc7a8afa84d9ea55a`; algorithm source SHA-256
  `1a85f9d4cc0ca2607077615c78582191a11ec06c1b40b8edf59de3f52d64360b`;
  input snapshot SHA-256
  `6d6df3745cca0cab1b7f930315b171b4fbceb93dc4d139ff943a3be48dd4c88f`.
- Market input: Polygon/Massive raw SPY minute archives from the same pinned
  LEAN input snapshot. The generator verifies every selected archive against
  the SHA-256 values recorded by the source receipt.
- Protocol: SPY raw regular-session minute bars consolidated to 15 minutes,
  $100,000 initial cash, Interactive Brokers equity commission, LEAN
  `SetHoldings` sizing, and default gates `gap_bps=2`, `rsi_min=50`,
  `rsi_max=70`.
- Compact window: 2026-02-17 through 2026-02-26. The input contains all 3,120
  regular-session minute bars. The output contains the six LEAN `Filled`
  order events (three closed trades) copied from the independently executed
  source receipt in that window.
- Generated: 2026-08-17 with
  `PYTHONPATH=. python tests/fixtures/golden/ema-crossover-2-bps-lean/generate.py`
  from the `PythonDataService` directory.
- Tolerance: timestamps, directions, quantities, and fees are exact. Fill
  prices and final equity use absolute tolerance `0.000001` with zero relative
  tolerance; the committed fixture currently reproduces both with zero error.
