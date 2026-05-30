"""Normalize a QuantConnect backtest-result export into the canonical orders
fixture ``qc_reconciler._parse_qc_orders`` accepts.

QC's backtest result JSON stores ``orders`` as a dict keyed by order id, with
the fill price/quantity/time on the order itself (market orders fill fully)
and a nested ``symbol`` object. The reconciler wants:

    {"orders": [{"id", "symbol": str, "type": int,
                 "events": [{"time", "fillQuantity", "fillPrice",
                             "direction", "orderFeeAmount"}]}]}

Fees are not in the backtest-result export (QC carries them on a separate
order-events stream), so ``orderFeeAmount`` is ``null`` — Branch-B, commission
non-gating per the numerical-rigor reconciliation taxonomy.

Usage:
    python normalize_orders.py <qc_export.json> <out_qc_orders.json>
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def normalize(export: dict) -> dict:
    raw = export["orders"]
    items = list(raw.values()) if isinstance(raw, dict) else raw
    items.sort(key=lambda o: int(o["id"]))
    orders = []
    for o in items:
        sym = o["symbol"]
        symbol = sym["value"] if isinstance(sym, dict) else str(sym)
        orders.append(
            {
                "id": int(o["id"]),
                "symbol": symbol,
                "type": int(o.get("type", 0)),
                "events": [
                    {
                        "time": o.get("lastFillTime") or o["time"],
                        "fillQuantity": int(o["quantity"]),  # signed
                        "fillPrice": float(o["price"]),
                        "direction": int(o.get("direction", 0)),
                        "orderFeeAmount": None,  # not in backtest export → Branch-B
                    }
                ],
            }
        )
    return {"orders": orders}


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__)
        return 2
    export = json.loads(Path(argv[1]).read_text())
    out = normalize(export)
    Path(argv[2]).write_text(json.dumps(out, indent=2) + "\n")
    print(f"wrote {len(out['orders'])} orders → {argv[2]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
