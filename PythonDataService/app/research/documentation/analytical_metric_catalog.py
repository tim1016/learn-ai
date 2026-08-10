"""Versioned, transport-only catalog for Strategy Lab analytical metrics.

Python owns the definitions published in this module.  Consumers render the
generated contract; they do not reconstruct a formula from a metric label.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

CATALOG_VERSION = 1

MetricProducer = Literal[
    "platform",
    "lean_native",
    "lean_runtime",
    "validation_analytics",
    "verdict_policy",
]
MetricValueState = Literal[
    "zero",
    "undefined",
    "unavailable",
    "infinite",
    "sentinel",
    "not_applicable",
]


class VariableDefinition(BaseModel):
    """One displayed formula variable, kept in authored display order."""

    model_config = ConfigDict(frozen=True)

    symbol: str = Field(min_length=1)
    definition: str = Field(min_length=1)


class ValueState(BaseModel):
    """A producer-specific state that must not be conflated with zero."""

    model_config = ConfigDict(frozen=True)

    state: MetricValueState
    display: str = Field(min_length=1)
    scoring_behavior: str = Field(min_length=1)


class MetricVariant(BaseModel):
    """Complete documentation contract for one metric producer variant."""

    model_config = ConfigDict(frozen=True)

    metric_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    variant_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]*$")
    contract_id: str | None = None
    producer: MetricProducer
    label: str = Field(min_length=1)
    definition: str = Field(min_length=1)
    interpretation: str = Field(min_length=1)
    common_misreadings: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    search_terms: tuple[str, ...] = ()
    formula_latex: str | None = None
    variables: tuple[VariableDefinition, ...] = ()
    input_series: str = Field(min_length=1)
    units: str = Field(min_length=1)
    output_scale: str = Field(min_length=1)
    formatting: str = Field(min_length=1)
    sampling_cadence: str = Field(min_length=1)
    annualization: str | None = None
    benchmark: str | None = None
    risk_free_rate: str | None = None
    cost_treatment: str = Field(min_length=1)
    timezone_convention: str = Field(min_length=1)
    value_states: tuple[ValueState, ...] = ()
    canonical_symbol: str = Field(min_length=1)
    source_reference: str | None = None
    validating_tests: tuple[str, ...] = ()
    fixture_or_receipt: str | None = None
    numerical_tolerance: str | None = None
    results_surfaces: tuple[str, ...] = ()
    verdict_membership: str | None = None
    alternative_variant_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def require_self_describing_formula(self) -> MetricVariant:
        if self.formula_latex is None and self.variables:
            raise ValueError("variable definitions require formula_latex")
        return self


class AnalyticalMetricCatalog(BaseModel):
    """Stable v1 catalog, serialized deterministically for the Frontend."""

    model_config = ConfigDict(frozen=True)

    catalog_version: int = CATALOG_VERSION
    variants: tuple[MetricVariant, ...]

    @model_validator(mode="after")
    def validate_variant_links(self) -> AnalyticalMetricCatalog:
        variant_ids = {variant.variant_id for variant in self.variants}
        if len(variant_ids) != len(self.variants):
            raise ValueError("variant_id values must be unique")
        for variant in self.variants:
            missing = set(variant.alternative_variant_ids) - variant_ids
            if missing:
                raise ValueError(f"{variant.variant_id} references missing alternatives: {sorted(missing)}")
        return self


PLATFORM_SHARPE_VARIANT = MetricVariant(
    metric_id="sharpe",
    variant_id="sharpe.platform.v1",
    contract_id="platform-sharpe-v1",
    producer="platform",
    label="Sharpe ratio",
    definition="Annualized average return per unit of observed return variability.",
    interpretation="Use it to compare return against the variability of the same documented return series.",
    common_misreadings=(
        "It is not a probability of future profit.",
        "It is not interchangeable with LEAN-native Sharpe.",
    ),
    aliases=("Sharpe", "sharpe ratio", "risk-adjusted return"),
    search_terms=("volatility", "daily returns", "annualized", "sqrt 252"),
    formula_latex=r"\operatorname{Sharpe}=\frac{\bar r}{s_r}\sqrt{252}",
    variables=(
        VariableDefinition(symbol=r"\bar r", definition="mean of the selected return observations"),
        VariableDefinition(symbol=r"s_r", definition="sample standard deviation of those return observations"),
    ),
    input_series=(
        "Daily returns resampled from the recorded equity curve; without that curve, per-trade returns from "
        "the reconstructed all-in trade curve."
    ),
    units="ratio",
    output_scale="annualized ratio",
    formatting="two decimal places",
    sampling_cadence="daily when an equity curve is retained; otherwise completed trades",
    annualization="sqrt(252) for daily returns; trade fallback scales by the documented trading-day coverage",
    benchmark=None,
    risk_free_rate="0 under the platform contract",
    cost_treatment="Uses the persisted equity/trade inputs; fees are not subtracted again by this metric.",
    timezone_convention="UTC timestamps at storage and wire boundaries; daily resampling follows the producing run.",
    value_states=(
        ValueState(
            state="unavailable",
            display="Unavailable",
            scoring_behavior="Not a zero; downstream policy must treat missing evidence explicitly.",
        ),
        ValueState(
            state="undefined",
            display="Unavailable",
            scoring_behavior="Returned when fewer than two observations or zero sample deviation makes the ratio undefined.",
        ),
    ),
    canonical_symbol="PythonDataService/app/engine/results/statistics.py::_sharpe",
    source_reference='Sharpe (1994), "The Sharpe Ratio", Journal of Portfolio Management 21(1), §IV.',
    validating_tests=(
        "PythonDataService/tests/fixtures/test_strategy_metric_help_golden.py::test_strategy_metric_help_matches_canonical_golden_values",
        "PythonDataService/tests/test_statistics.py",
    ),
    fixture_or_receipt="contracts/fixtures/strategy-metric-help-golden-v1.json",
    numerical_tolerance="absolute=1e-12, relative=0",
    results_surfaces=("Strategy Lab Results summary",),
    verdict_membership="Platform verdict policy may consume its platform metric; this entry does not define a grade.",
    alternative_variant_ids=("sharpe.lean_native.v1",),
)

LEAN_NATIVE_SHARPE_VARIANT = MetricVariant(
    metric_id="sharpe",
    variant_id="sharpe.lean_native.v1",
    contract_id="lean-statistics-oracle-v1",
    producer="lean_native",
    label="Sharpe ratio",
    definition="LEAN-native annual performance above its selected risk-free rate, divided by annual standard deviation.",
    interpretation="Use it to audit the native LEAN result under its pinned source and primary-credit-rate inputs.",
    common_misreadings=(
        "It is not the platform Sharpe under a different input and risk-free-rate contract.",
        "A familiar label does not make two producer variants numerically interchangeable.",
    ),
    aliases=("LEAN Sharpe", "native Sharpe", "sharpe ratio"),
    search_terms=("lean", "risk-free", "annual performance", "annual standard deviation"),
    formula_latex=r"\operatorname{Sharpe}_{LEAN}=\frac{R_{annual}-r_f}{\sigma_{annual}}",
    variables=(
        VariableDefinition(symbol=r"R_{annual}", definition="LEAN annual performance from the preprocessed return vector"),
        VariableDefinition(symbol=r"r_f", definition="average dated LEAN risk-free rate selected for the equity sample"),
        VariableDefinition(symbol=r"\sigma_{annual}", definition="annual standard deviation from sample variance times trading days per year"),
    ),
    input_series=(
        "LEAN Strategy Return chart observations after the pinned preprocessing: discard day 0 and day 1, then "
        "convert percentages to fractional returns."
    ),
    units="ratio",
    output_scale="annualized ratio",
    formatting="native result precision; Results currently displays two decimal places",
    sampling_cadence="LEAN Strategy Return observations",
    annualization="annual performance and annual standard deviation use the configured LEAN trading-days-per-year value",
    benchmark="Not used in the Sharpe numerator; LEAN benchmark inputs support other native metrics.",
    risk_free_rate="Average dated LEAN primary-credit-rate observations retained in the oracle fixture.",
    cost_treatment="Native LEAN portfolio result; fee effects are represented by LEAN's portfolio evidence.",
    timezone_convention="Source chart timestamps are normalized to int64 ms UTC at system boundaries.",
    value_states=(
        ValueState(
            state="zero",
            display="0.00",
            scoring_behavior="A valid native zero remains zero and is not treated as missing.",
        ),
        ValueState(
            state="sentinel",
            display="Engine-defined finite value",
            scoring_behavior="Preserve the pinned LEAN behavior; do not reinterpret it as a platform result.",
        ),
        ValueState(
            state="unavailable",
            display="Unavailable",
            scoring_behavior="Missing native evidence is not a zero and must not be backfilled from the platform variant.",
        ),
    ),
    canonical_symbol="PythonDataService/app/engine/results/lean_statistics.py::reproduce_lean_total_performance",
    source_reference=(
        "QuantConnect LEAN commit 261366a7e26ae942df858ab20df4fef8fa07de67; "
        "Common/Statistics/PortfolioStatistics.cs and Statistics.cs."
    ),
    validating_tests=(
        "PythonDataService/tests/test_lean_statistics.py::test_oracle_reproduction_matches_native_statistics",
    ),
    fixture_or_receipt="PythonDataService/tests/fixtures/golden/lean-statistics-oracle-v1/",
    numerical_tolerance="Pinned by the LEAN native-statistics oracle receipt; see docs/references/lean-native-statistics-oracle-v1.md.",
    results_surfaces=("Strategy Lab Results summary for LEAN-native runs",),
    verdict_membership="Not a platform-verdict input; native parity evidence remains separately identifiable.",
    alternative_variant_ids=("sharpe.platform.v1",),
)


def catalog() -> AnalyticalMetricCatalog:
    """Return the minimal v1 tracer catalog in its authored display order."""

    return AnalyticalMetricCatalog(
        catalog_version=CATALOG_VERSION,
        variants=(PLATFORM_SHARPE_VARIANT, LEAN_NATIVE_SHARPE_VARIANT),
    )


def metric_documentation_context_for_source(source: str) -> tuple[dict[str, str], ...]:
    """Return persistable documentation context for a newly-produced run.

    Source is a producer boundary, not a calculation.  The Backend carries this
    recorded metadata verbatim and only falls back to inference for older rows.
    """

    variant = LEAN_NATIVE_SHARPE_VARIANT if source == "lean-sidecar" else PLATFORM_SHARPE_VARIANT
    return (
        {
            "metric_id": variant.metric_id,
            "variant_id": variant.variant_id,
            "producer": variant.producer,
            "contract_id": variant.contract_id or "",
        },
    )
