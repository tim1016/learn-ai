"""Primary-source Sharpe fixtures; independent generation and pinned tolerances."""

import json
from pathlib import Path

import numpy as np

from app.research.signal.sharpe import (
    compute_deflated_sharpe,
    compute_sharpe_ci,
    deflated_sharpe_from_moments,
)

GOLDEN = Path(__file__).parent / "golden" / "research-primitives"


def _read(fixture: str, name: str) -> dict:
    return json.loads((GOLDEN / fixture / "v1" / f"{name}.json").read_text())


class TestRP007LoHac:
    def test_joint_moment_covariance_matches_decimal_influence_oracle(self):
        cases = _read("RP-007", "input")["cases"]
        expected = _read("RP-007", "output")["cases"]
        for case, oracle in zip(cases, expected, strict=True):
            result = compute_sharpe_ci(
                np.asarray(case["returns"]),
                bars_per_year=case["bars_per_year"],
                hac_lag=case["hac_lag"],
                segment_lengths=case["segment_lengths"],
            )
            assert result.valid
            for key, value in oracle.items():
                # 60-digit Decimal oracle versus double-precision 2x2 algebra.
                np.testing.assert_allclose(getattr(result, key), value, atol=1e-12, rtol=0)


class TestRP008DeflatedSharpe:
    def test_published_example(self):
        cases = _read("RP-008", "input")["published"]
        outputs = _read("RP-008", "output")
        for case, published in zip(cases, outputs["published_dsr"], strict=True):
            maximum, probability = deflated_sharpe_from_moments(**case)
            # Authors publish four decimal places: half-unit rounding interval.
            np.testing.assert_allclose(probability, published, atol=0.00005, rtol=0)
            if case["effective_trials"] == 100:
                np.testing.assert_allclose(maximum, outputs["published_null_max_100"], atol=0.00005, rtol=0)

    def test_measured_grid_moments_and_correlation(self):
        inputs = _read("RP-008", "input")
        expected = _read("RP-008", "output")["grid"]
        result = compute_deflated_sharpe(np.asarray(inputs["grid"]), inputs["selected_index"], bars_per_year=1)
        assert result.valid
        for key, value in expected.items():
            np.testing.assert_allclose(getattr(result, key), value, atol=1e-12, rtol=0)
