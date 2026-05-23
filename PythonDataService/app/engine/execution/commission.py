"""Engine-side seam onto the IBKR equity-tier commission model.

The canonical implementation lives in
``app.research.parity.ibkr_commission`` (see its module docstring for the
formula and reference). Engine execution code imports from here so the
``app.engine.*`` package has no inbound dependency on ``app.research.*``;
the reconciler and the engine therefore share one fee implementation
without crossing the layer boundary in the wrong direction.

Provenance:
  Formula: see app/research/parity/ibkr_commission.py
  Reference: QuantConnect InteractiveBrokersBrokerage equity tier
  Canonical implementation: app/research/parity/ibkr_commission.py
  Validated against: tests/research/parity/test_ibkr_commission.py
                     tests/engine/test_commission_reexport.py
"""

from __future__ import annotations

from app.research.parity.ibkr_commission import IbkrEquityCommissionModel

__all__ = ["IbkrEquityCommissionModel"]
