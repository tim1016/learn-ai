"""Trusted Phase 1 sample: buy-and-hold SPY for the trusted-sample window.

**Not reconciliation-grade.** This sample exists to exercise the
sidecar plumbing (runner, manifest, workspace contract, LEAN data-folder
fidelity, ObjectStore visibility), not to produce numbers that can be
compared to Engine Lab. Specifically:

* ``SetBenchmark(lambda dt: 100)`` pins the benchmark to a constant so
  the post-run ResultsAnalyzer does not try to read SPY daily data the
  trusted-sample window does not stage. A reconciliation-grade run
  must stage real daily benchmark bars.
* Brokerage / fill / commission models are LEAN defaults. A
  reconciliation-grade run must pin Interactive Brokers semantics per
  ADR §"Brokerage, fill, and fee policy".
* The fixture only stages five trading days of synthetic minute bars;
  no factor or map files. Any algorithm that touches corporate-action
  windows is out of scope.

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

from datetime import datetime
from zoneinfo import ZoneInfo

from AlgorithmImports import *


_ET = ZoneInfo("America/New_York")


def _to_ms_utc(dt):
    """Normalize a QC-supplied Python datetime to int64 ms UTC.

    QC's Python bridge passes ``bar.EndTime`` as a naive
    ``datetime.datetime`` in the algorithm timezone (ET for US
    equities). Attaching the ET zone before ``.timestamp()`` is the
    only safe way to convert to a UTC epoch.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_ET)
    return int(dt.timestamp() * 1000)


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

        # LEAN's default benchmark is SPY daily, which would require
        # staging daily bars in addition to minute. The trusted-sample
        # is reconciliation-eligible without a benchmark; pin it to a
        # constant so the post-run ResultsAnalyzer does not fail
        # looking for unstaged daily data.
        self.SetBenchmark(lambda dt: 100)

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
        with open(self._obs_path, "a") as f:
            f.write(f"{_to_ms_utc(bar.EndTime)},{bar.Close}\\n")
        if not self._invested:
            self.SetHoldings(self.symbol, 1.0)
            self._invested = True
'''
