"""Trusted Phase 1 sample: buy-and-hold SPY for one day.

This is the *source* the sidecar copies into ``workspace/project/main.py``
before launching LEAN. It is intentionally trivial so the Phase 1 spike
is testing the runner and the data-folder contract, not strategy logic.

Invariants this sample preserves so reconciliation-grade callers can
re-use it:

- Class name is ``MyAlgorithm`` — the default ``algorithm_type_name``
  recorded in the manifest.
- ``SetCash`` is explicit so starting capital is pinned in the manifest.
- Subscription requests ``fillForward=False`` per the ADR's
  fill-forward policy.
- ``DataNormalizationMode.Raw`` is set so adjusted-vs-raw cannot drift
  silently between the staged data and what the algorithm sees.
- Every received bar is recorded by ``(timestamp_unix_ms, close)`` so
  the round-trip fidelity test can assert what the algorithm actually
  saw against what was written.

The file is shipped as a Python source string rather than a separately
importable module so callers can copy it byte-for-byte into the LEAN
container's workspace without dealing with import-path semantics inside
the container.
"""

from __future__ import annotations

# The actual algorithm source. The hash of this constant becomes the
# manifest's ``algorithm_source_sha256``.
BUY_AND_HOLD_SOURCE: str = '''\
"""Trusted Phase 1 sample for the LEAN Sidecar Lab.

Behaviour:
  * On the first OnData call after warmup, set 100% of equity to SPY.
  * Hold to end of backtest.
  * Append every received bar to ``observations.csv`` under the
    output folder so the round-trip fidelity test can reconcile what
    the algorithm actually saw against what was written to disk.

The class name MUST be ``MyAlgorithm`` — Phase 1 launches use it as the
default ``algorithm_type_name``.
"""

from AlgorithmImports import *


class MyAlgorithm(QCAlgorithm):
    def Initialize(self):
        # Window is configured via parameters so the launcher can pin
        # the requested window without editing source. Defaults match
        # the trusted-sample fixture so out-of-the-box runs work.
        start = self.GetParameter("start_date") or "2025-01-06"
        end = self.GetParameter("end_date") or "2025-01-10"
        cash = float(self.GetParameter("starting_cash") or "100000")
        sy, sm, sd = (int(x) for x in start.split("-"))
        ey, em, ed = (int(x) for x in end.split("-"))
        self.SetStartDate(sy, sm, sd)
        self.SetEndDate(ey, em, ed)
        self.SetCash(cash)

        equity = self.AddEquity(
            "SPY",
            Resolution.Minute,
            fillForward=False,
            extendedMarketHours=False,
        )
        equity.SetDataNormalizationMode(DataNormalizationMode.Raw)
        self.symbol = equity.Symbol
        self._invested = False

        # Pre-create the observations file so the test can detect
        # zero-bars-consumed without ambiguity.
        path = self.ObjectStore.GetFilePath("observations.csv")
        with open(path, "w") as f:
            f.write("ms_utc,close\\n")
        self._obs_path = path

    def OnData(self, slice):
        bar = slice.Bars.get(self.symbol)
        if bar is None:
            return
        # bar.EndTime is the bar close in algorithm timezone (ET);
        # convert to int64 ms UTC for the manifest boundary.
        end_utc_ms = int(bar.EndTime.ToUniversalTime().timestamp() * 1000)
        with open(self._obs_path, "a") as f:
            f.write(f"{end_utc_ms},{bar.Close}\\n")
        if not self._invested:
            self.SetHoldings(self.symbol, 1.0)
            self._invested = True
'''
