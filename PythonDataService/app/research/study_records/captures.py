"""What a request says, for the study whose request carries its data.

Realized vs IV is handed its bars in the request body, so its record keeps what the run saw: the bars (the chart
draws them, and a stored result must show without fetching anything), a digest of them and of any IV history that came
with the request (the run's data identity: a rerun on different bars has a different one), and a summary of both in
the inputs, which stay small enough for the History to read for every record. The stored result is the series response
with the bars added under ``bars``.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from app.research.runs.hashing import hash_payload
from app.research.study_records.recorder import RunCapture

_NOT_AN_INPUT = {"bars", "iv_series", "rerun_of"}


def capture_realized_vs_iv(request: BaseModel) -> RunCapture:
    """The capture of a Realized vs IV request (``RealizedVsIvSeriesRequest``): the bars in time order, as the series is."""
    bars: list[dict[str, Any]] = sorted((bar.model_dump(mode="json") for bar in request.bars), key=lambda bar: bar["ts"])  # type: ignore[attr-defined]
    iv_series = request.iv_series  # type: ignore[attr-defined]
    inputs: dict[str, Any] = {
        **request.model_dump(mode="json", exclude=_NOT_AN_INPUT),
        "iv_series_supplied": bool(iv_series),
        "bars": {
            "count": len(bars),
            "first_ts_ms": bars[0]["ts"] if bars else None,
            "last_ts_ms": bars[-1]["ts"] if bars else None,
        },
    }
    return RunCapture(
        inputs=inputs,
        rerun_of=getattr(request, "rerun_of", None),
        data_identity=hash_payload({"bars": bars, "iv_series": iv_series}) if bars else None,
        package=lambda series: {**series, "bars": bars},
    )
