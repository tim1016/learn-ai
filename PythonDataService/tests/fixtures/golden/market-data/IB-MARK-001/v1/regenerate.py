"""Generate marks from the installed reference, never the application helper."""
from __future__ import annotations

import hashlib
import inspect
import json
import math
from importlib.metadata import version
from pathlib import Path

from ib_async import Ticker


def regenerate() -> None:
    assert version("ib_async") == "2.1.0"
    root = Path(__file__).parent
    cases = json.loads((root / "input.json").read_text())
    expected = []
    for values in cases:
        ticker = Ticker()
        for name, value in values.items():
            setattr(ticker, name, float("nan") if value is None else value)
        result = ticker.marketPrice()
        expected.append(result if math.isfinite(result) else None)
    source = "".join(inspect.getsource(f) for f in (Ticker.marketPrice, Ticker.hasBidAsk, Ticker.midpoint, Ticker.isUnset))
    (root / "output.json").write_text(json.dumps({"ib_async": version("ib_async"), "source_sha256": hashlib.sha256(source.encode()).hexdigest(), "marks": expected}, indent=2) + "\n")


if __name__ == "__main__":
    regenerate()
