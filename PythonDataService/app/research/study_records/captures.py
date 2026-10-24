"""What a request says, for the study whose request carries an input too large to copy into every record.

Realized vs IV may be handed an IV history in its request body: thousands of points a caller measured elsewhere. Its
record keeps whether one was supplied, not the points: the run's ``data_source`` already names the IV it used by
count and digest, and the inputs stay small enough for the History to read for every record.
"""

from __future__ import annotations

from pydantic import BaseModel

from app.research.study_records.recorder import RunCapture, capture_request

_LEFT_OUT = {"job_id", "force", "rerun_of", "iv_series"}


def capture_realized_vs_iv(request: BaseModel) -> RunCapture:
    """The capture of a Realized vs IV job request (``RealizedVsIvJobRequest``): its settings, and whether it brought its own IV."""
    kept = capture_request(request)
    inputs = {
        **{name: value for name, value in kept.inputs.items() if name not in _LEFT_OUT},
        "iv_series_supplied": bool(getattr(request, "iv_series", None)),
    }
    return RunCapture(inputs=inputs, job_id=kept.job_id, rerun_of=kept.rerun_of)
