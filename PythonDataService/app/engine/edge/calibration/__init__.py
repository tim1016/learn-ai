"""Calibration utilities for the edge pipeline.

Calibration code is offline / batch — it consumes signal logs and
returns metrics or fitted parameters. Production runtime modules
(``confidence``, ``vrp``, ``regime_*``) must not import from here.
"""
