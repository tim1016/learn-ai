"""Engine-side seam onto the IBKR equity-tier commission model.

The canonical implementation lives in
``app.research.parity.ibkr_commission`` (see its module docstring for the
formula and reference). Engine execution code imports from here, so this
one file is the engine's only import of ``app.research.*`` for fees and the
reconciler and the engine share one fee implementation (an identity pinned
by ``test_commission_reexport.py``).

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
