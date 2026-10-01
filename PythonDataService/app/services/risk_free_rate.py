"""The one default annualized risk-free rate (#2764).

Every Python surface that takes a risk-free rate and is not handed one uses
``DEFAULT_RISK_FREE_RATE``: request-model fields, router and pricer
signatures, and the research and volatility helpers. It is also
``app.services.fred_service.FALLBACK_RATE``, the rate a FRED lookup returns
when FRED cannot answer, so a caller that omits the rate and a caller whose
lookup fails price at the same number. The live rate comes from
``app.services.fred_service.get_risk_free_rate``; this is only the default.

.NET and Angular never restate it: they omit the field and let Python fill it.

The module has no app imports, so the volatility package and the request
models read it without pulling in the FRED client or the settings it reads.
"""

from __future__ import annotations

from typing import Final

DEFAULT_RISK_FREE_RATE: Final[float] = 0.043
