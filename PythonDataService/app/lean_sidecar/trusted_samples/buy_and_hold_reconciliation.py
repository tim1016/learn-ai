"""Phase 5b — reconciliation-grade buy-and-hold sample.

Identical to ``buy_and_hold.py`` in behavior (buy SPY on first OnData,
hold to end of backtest, record every received bar to
``observations.csv``) but pins the brokerage, account type, fill
model, and fee model explicitly so the run is eligible for
Engine-Lab-vs-LEAN reconciliation per ADR § "Brokerage, fill, and fee
policy" and invariant #11.

The Phase 5a self-reconciler (``app/lean_sidecar/reconciler.py``) will
return a clean fee report for runs of THIS sample, and a "many drift"
report for runs of the default sample — the difference between the
two is the value Phase 5b adds.

What this sample still does NOT do (deferred to Phase 5c+):
- ``SetBenchmark(lambda dt: 100)`` is kept; real SPY daily benchmark
  staging is its own work item. Benchmark mismatches show up in
  LEAN's stats but do not affect fill prices or fees, so they don't
  affect the self-reconciler.
- Quote-bar staging is not added here; the trusted-sample fixture
  still emits the known-noise ``_quote.zip not found`` log line.

Class name MUST stay ``MyAlgorithm`` (LeanConfig.algorithm_type_name's
default) — switching it silently changes which class LEAN loads.
"""

from __future__ import annotations

BUY_AND_HOLD_RECONCILIATION_SOURCE: str = '''\
"""Phase 5b reconciliation-grade sample for the LEAN Sidecar Lab.

Behaviour:
  * Pin Interactive Brokers brokerage + margin account so the
    commission/fill assumptions are explicit. The Phase 5a reconciler
    compares LEAN-recorded ``orderFeeAmount`` against the canonical
    IbkrEquityCommissionModel — runs of THIS sample reconcile clean.
  * On the first OnData call after warmup, set 100% of equity to SPY.
  * Hold to end of backtest.
  * Append every received bar to ``observations.csv`` under the
    output folder.

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
        start = self.GetParameter("start_date") or "2025-01-06"
        end = self.GetParameter("end_date") or "2025-01-10"
        cash = float(self.GetParameter("starting_cash") or "100000")
        sy, sm, sd = (int(x) for x in start.split("-"))
        ey, em, ed = (int(x) for x in end.split("-"))
        self.SetStartDate(sy, sm, sd)
        self.SetEndDate(ey, em, ed)
        self.SetCash(cash)

        # Phase 5b — explicit brokerage + account type. Per ADR
        # § "Brokerage, fill, and fee policy" + invariant #11, any
        # reconciliation-grade run MUST pin these so the manifest's
        # brokerage_policy is unambiguously "interactive_brokers" and
        # the Phase 5a fee reconciler can return a clean report.
        self.SetBrokerageModel(BrokerageName.InteractiveBrokersBrokerage, AccountType.Margin)

        equity = self.AddEquity(
            "SPY",
            Resolution.Minute,
            fillForward=False,
            extendedMarketHours=False,
        )
        equity.SetDataNormalizationMode(DataNormalizationMode.Raw)
        self.symbol = equity.Symbol
        self._invested = False

        # Constant benchmark kept until real benchmark staging lands
        # (Phase 5c). A benchmark difference does not affect fill
        # prices or fees, so the self-reconciler is unaffected.
        self.SetBenchmark(lambda dt: 100)

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
