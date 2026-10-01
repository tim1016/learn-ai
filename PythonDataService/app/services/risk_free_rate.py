"""The one default annualized risk-free rate for option pricing (#2764).

Option pricing that has no rate of its own uses ``DEFAULT_RISK_FREE_RATE``,
for example a pricing request that omits the field. It is also the rate
``app.services.fred_service.get_risk_free_rate`` returns when FRED cannot
answer, so a caller that omits the rate and a caller whose lookup fails price
at the same number. The live rate comes from FRED; this is only the default.
.NET and Angular never restate it: they omit the field and let Python fill it.

It is not the LEAN-parity Sharpe rate. The Sharpe and Sortino rate in
``app/engine/results/lean_statistics.py`` (0.0 by default, to match LEAN's
statistics) is a different concept and must not use this constant.

The 4.3 % figure has no recorded source. It was first hardcoded on 2026-02-22,
as the Strategy Builder's default, and has been FRED's fallback since
2026-03-05. Treat it as a placeholder, not a measured or dated market rate.

The module has no app imports, so the request models read it without pulling
in the FRED client or the settings it reads.
"""

from __future__ import annotations

from typing import Final

DEFAULT_RISK_FREE_RATE: Final[float] = 0.043
