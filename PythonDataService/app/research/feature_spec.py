"""Per-feature validation contracts.

Different features answer different questions, but the original Feature
Runner pipeline tested every feature against the same target with the
same kill conditions. That conflated "the feature is bad" with "the
test was the wrong question for this feature."

Each ``FeatureValidationSpec`` documents, per built-in feature:

* The default forward-return target the IC is measured against.
* The expected feature → return mapping shape (monotonic / U-shaped /
  tail-only / none).
* Whether stationarity should be a hard kill or a diagnostic.
* The minimum effective sample size for the screens to be meaningful.
* Free-text intent so the UI can surface "why are we running this".

The spec is consumed by ``runner.run_feature_research`` to soft-gate
stationarity and monotonicity, and by the UI to render an
"intent vs. result" disclosure near the headline.

This file is the **authority** for the per-feature contracts. Adding
a new feature requires adding an entry; the runner falls back to a
generic default for anything missing, but the validation page
explicitly flags spec-less features as "no validation contract."
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

ExpectedShape = Literal[
    "monotonic_increasing",
    "monotonic_decreasing",
    "u_shaped",
    "inverted_u",
    "tail_only",
    "none",
]
"""How the feature is expected to map to the target.

* ``monotonic_*`` — classic rank signal, higher feature → higher (or
  lower) forward return uniformly.
* ``u_shaped`` — both tails of the feature predict in the same
  direction (e.g. extreme RSI predicting reversal).
* ``inverted_u`` — middle of the distribution dominates.
* ``tail_only`` — only the extreme quantile carries the signal.
* ``none`` — no shape claim; signal is entirely about magnitude or
  direction without a quantile structure.
"""

ExpectedDirection = Literal["positive", "negative", "two_sided", "unknown"]
"""Sign expectation for the headline IC.

* ``positive`` — feature is a momentum-style predictor.
* ``negative`` — feature is a mean-reversion-style predictor (RSI on
  most assets is the canonical example).
* ``two_sided`` — sign is regime-dependent and the headline IC alone
  isn't the right summary (volatility-level features fall here).
* ``unknown`` — no prior; treat the test as exploratory.
"""


@dataclass(frozen=True)
class FeatureValidationSpec:
    """The contract a feature is being tested against."""

    feature_name: str

    # Target & shape
    default_target: str = "forward_log_return_15m"
    """Identifier for the forward-return target. The runner currently
    always computes ``forward_log_return_15m``; specs that name a
    different target are flagged in the UI as "spec mismatch — using
    default target." Future work: feature-aware target dispatch."""

    expected_direction: ExpectedDirection = "unknown"
    expected_shape: ExpectedShape = "none"

    # Validation gating
    stationarity_required: bool = False
    """When False, ADF/KPSS is computed and reported but is **not** a
    kill criterion. Many useful features are non-stationary by
    construction (volatility levels, regime indicators)."""

    monotonicity_required: bool = False
    """When False, the monotonicity ratio is reported as a diagnostic
    only. Set to True for clean rank signals where a non-monotonic
    quantile chart is genuinely a kill."""

    min_effective_n_for_stage1: int = 60
    min_effective_n_for_stage2: int = 100
    min_effective_n_for_stage3: int = 180

    intent: str = ""
    """Plain-English description of what the feature is measuring and
    why we'd expect it to predict. Surfaced in the UI tooltip."""

    notes: tuple[str, ...] = field(default_factory=tuple)
    """Extra caveats — known issues, rough edges, things the user
    should keep in mind. Surfaced as a bulleted list under the
    feature-spec disclosure."""


# ─── Built-in feature specs ────────────────────────────────────────────────


_BUILTIN_SPECS: dict[str, FeatureValidationSpec] = {
    "rsi_14": FeatureValidationSpec(
        feature_name="rsi_14",
        expected_direction="negative",
        expected_shape="monotonic_decreasing",
        stationarity_required=True,
        monotonicity_required=True,
        intent=(
            "Classic mean-reversion oscillator. Low RSI is oversold and "
            "we expect positive forward returns; high RSI is overbought "
            "and we expect negative forward returns."
        ),
        notes=(
            "May actually be U-shaped at the extreme tails. "
            "Stationarity required because RSI is bounded [0, 100].",
        ),
    ),
    "momentum_5m": FeatureValidationSpec(
        feature_name="momentum_5m",
        expected_direction="positive",
        expected_shape="monotonic_increasing",
        stationarity_required=True,
        monotonicity_required=True,
        intent=(
            "Short-horizon price momentum. Positive 5-minute momentum is "
            "expected to predict positive 15-minute forward return at "
            "intraday horizons."
        ),
    ),
    "macd_signal": FeatureValidationSpec(
        feature_name="macd_signal",
        expected_direction="positive",
        expected_shape="monotonic_increasing",
        stationarity_required=False,  # MACD is a price difference; not strictly stationary.
        monotonicity_required=False,
        intent=(
            "MACD crossover signal. Positive signal-line crossings are "
            "expected to predict short-horizon positive return, but the "
            "predictive content is concentrated at crossover events, not "
            "uniformly across the feature distribution."
        ),
        notes=(
            "Predictive content concentrated at sign-change events. "
            "A non-monotonic quantile chart is consistent with the "
            "feature's actual mechanism and is not necessarily a kill.",
        ),
    ),
    "realized_vol_30": FeatureValidationSpec(
        feature_name="realized_vol_30",
        # Vol level predicts |return| / forward vol, not signed return.
        # Until the runner supports feature-aware targets, the spec
        # reports two_sided so the UI flags the test as "wrong question".
        expected_direction="two_sided",
        expected_shape="none",
        stationarity_required=False,
        monotonicity_required=False,
        intent=(
            "30-bar realized volatility level. Predicts the SIZE of the "
            "next move, not the SIGN. Validating against signed forward "
            "returns is asking the wrong question."
        ),
        notes=(
            "Validation target should be |forward return| or forward "
            "realized vol, not signed forward return.",
            "A near-zero IC against signed forward return is the "
            "expected null result here, not evidence of failure.",
        ),
    ),
    "volume_zscore": FeatureValidationSpec(
        feature_name="volume_zscore",
        expected_direction="two_sided",
        expected_shape="u_shaped",
        stationarity_required=True,
        monotonicity_required=False,
        intent=(
            "Volume z-score. Volume spikes (large |z|) tend to coincide "
            "with information events; the SIGN of the predicted return "
            "is regime-dependent, but the size of the move tends to "
            "increase with |z|."
        ),
        notes=(
            "Expect u-shaped (or tail-only) quantile chart, not "
            "monotonic. A monotonic chart would actually be the "
            "surprising result.",
        ),
    ),
}


def get_spec(feature_name: str) -> FeatureValidationSpec:
    """Look up a feature's validation spec, falling back to a generic.

    Generic fallback uses ``unknown`` direction, ``none`` shape, and
    treats stationarity / monotonicity as diagnostics. The UI marks
    spec-less features as "no validation contract" so the reader
    knows the screens are running with default thresholds.
    """
    spec = _BUILTIN_SPECS.get(feature_name)
    if spec is not None:
        return spec
    return FeatureValidationSpec(
        feature_name=feature_name,
        intent="",
        notes=("No validation contract registered for this feature.",),
    )


def list_specs() -> list[FeatureValidationSpec]:
    """All registered specs, in stable insertion order."""
    return list(_BUILTIN_SPECS.values())
