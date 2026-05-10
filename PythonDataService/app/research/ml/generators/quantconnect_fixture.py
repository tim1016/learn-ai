"""Import a QuantConnect precomputed-predictions tutorial export into the
v0.5 prediction-set artifact format.

Phase 1 §A scope: schema validation, symbol filter, timestamp conversion,
manifest + chunk write. Real-fixture parity tests are gated on §B (QC
Cloud capture) — see
``docs/superpowers/specs/2026-05-10-quantconnect-precomputed-predictions-parity.md``.

Wire and storage format for timestamps is ``int64 ms UTC``. The QC export's
raw date strings cross the ingestion boundary in this module and are
converted on the spot; no string timestamps escape downstream.

The ``QcExport`` shape used here is a strawman for the QC export's actual
schema (spec R1). Once §B captures a real export, this model is verified
or adjusted.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class QcCalendarWindow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start: str
    end: str
    tz: str


class QcPredictionRow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbol: str
    date: str
    prediction: float


class QcExport(BaseModel):
    """Closed model of the captured ``qc_export.json``.

    Strawman shape; §B fixture capture confirms the real schema.
    """

    model_config = ConfigDict(extra="forbid")
    tutorial_id: str
    tutorial_url: str
    exported_at: str
    dataset_id: str
    calendar_window: QcCalendarWindow
    versions: dict[str, str]
    predictions: list[QcPredictionRow] = Field(min_length=1)
