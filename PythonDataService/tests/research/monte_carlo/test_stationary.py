"""Stationary bootstrap draws: determinism, block structure and the variance Politis & Romano derive."""

from __future__ import annotations

import numpy as np
import pytest

from app.research.monte_carlo.stationary import CHUNK_REPLICATIONS, stationary_index_chunks, stationary_indices
from tests.fixtures.golden_support.io import read_arrow
from tests.fixtures.golden_support.registry import default as registry


def _ar1(n: int, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    noise = rng.normal(0.0, 0.01, n)
    series = np.empty(n)
    series[0] = noise[0]
    for t in range(1, n):
        series[t] = 0.4 * series[t - 1] + noise[t]
    return series


def _variance_of_resampled_mean(series: np.ndarray, mean_block: float) -> float:
    """(1/n^2)[n C(0) + 2 sum_k (n - k)(1 - p)^k C(k)], C the circular autocovariance."""
    n, p = series.size, 1.0 / mean_block
    centered = series - series.mean()
    autocov = np.array([np.mean(centered * np.roll(centered, -k)) for k in range(n)])
    lags = np.arange(1, n)
    return float((n * autocov[0] + 2.0 * np.sum((n - lags) * (1.0 - p) ** lags * autocov[1:])) / n**2)


def test_stationary_indices_seeded_draw_properties() -> None:
    first = stationary_indices(50, size=50, mean_block=5, replications=20, rng=np.random.default_rng(9))
    again = stationary_indices(50, size=50, mean_block=5, replications=20, rng=np.random.default_rng(9))
    assert first.shape == (20, 50), "same seed draw has shape (replications, size)"
    assert first.dtype == np.int64, "same seed draw is int64"
    assert np.array_equal(first, again), "same seed same indices"
    assert first.min() >= 0 and first.max() < 50, "same seed draw stays inside [0, n)"

    drawn = stationary_indices(30, size=30, mean_block=1, replications=8, rng=np.random.default_rng(3))
    ordinary = np.random.default_rng(3).integers(0, 30, size=(8, 30))
    assert np.array_equal(drawn, ordinary), "mean_block one is the ordinary bootstrap"

    drawn = stationary_indices(7, size=20, mean_block=1e12, replications=5, rng=np.random.default_rng(4))
    assert np.array_equal(drawn, (drawn[:, :1] + np.arange(20)) % 7), "one block wraps from last to first"


def test_stationary_indices_seeded_draw_is_pinned() -> None:
    # The raw draws under this seed: starts 5 5 2 2 3 6 6 1 / 6 3 0 1 6 4 5 1 / 0 6 4 6 6 1 1 2, and a new block at
    # positions 4 and 7 / 1 and 2 / 2, 6 and 7 (each row also starts one at 0). Rows 0 and 2 wrap from 6 to 0.
    # A stored reading replays to the same numbers only while this holds, so a failure calls for a new
    # convention name, never a new literal.
    drawn = stationary_indices(7, size=8, mean_block=3, replications=3, rng=np.random.default_rng(20261006))
    assert drawn.tolist() == [[5, 6, 0, 1, 3, 4, 5, 1], [6, 3, 0, 1, 2, 3, 4, 5], [0, 1, 4, 5, 6, 0, 1, 2]]


def test_stationary_indices_blocks_average_the_mean_block() -> None:
    n, mean_block = 400, 5
    drawn = stationary_indices(n, size=400, mean_block=mean_block, replications=5_000, rng=np.random.default_rng(5))
    restarts = np.mean((drawn[:, 1:] - drawn[:, :-1]) % n != 1)
    # A restart that lands on the next row by chance (1 in n) reads as a continuation.
    # Two million steps estimate the share to 0.0003 (one standard error); 0.002 is seven of them.
    assert np.isclose(restarts, (1 / mean_block) * (1 - 1 / n), atol=0.002, rtol=0.0)


@pytest.mark.parametrize("mean_block", [1, 5, 10, 20])
def test_stationary_indices_resampled_mean_has_the_closed_form_variance(mean_block: int) -> None:
    series = _ar1(120, seed=7)
    chunks = stationary_index_chunks(120, size=120, mean_block=mean_block, replications=200_000, rng=np.random.default_rng(11))
    observed = float(np.concatenate([series[index].mean(axis=1) for index in chunks]).var())
    # 200,000 resamples estimate a variance to about 0.3% (one standard error); 2% is six of them.
    assert np.isclose(observed, _variance_of_resampled_mean(series, mean_block), rtol=0.02, atol=0.0)


def test_stationary_index_chunks_are_the_seeded_draws_in_order() -> None:
    chunks = list(stationary_index_chunks(40, size=10, mean_block=5, replications=2_500, rng=np.random.default_rng(6)))
    assert [chunk.shape[0] for chunk in chunks] == [CHUNK_REPLICATIONS, CHUNK_REPLICATIONS, 500]
    rng = np.random.default_rng(6)
    expected = [stationary_indices(40, size=10, mean_block=5, replications=count, rng=rng) for count in (1_000, 1_000, 500)]
    assert all(np.array_equal(a, b) for a, b in zip(chunks, expected, strict=True))


@pytest.mark.parametrize(
    "kwargs",
    [{"n": 0}, {"size": 0}, {"replications": 0}, {"mean_block": 0.5}, {"mean_block": float("nan")}, {"mean_block": float("inf")}],
)
def test_stationary_draws_refuse_bad_arguments(kwargs: dict[str, float]) -> None:
    arguments = {"n": 10, "size": 10, "mean_block": 5.0, "replications": 10, **kwargs}
    n = int(arguments.pop("n"))
    with pytest.raises(ValueError):
        stationary_indices(n, rng=np.random.default_rng(0), **arguments)  # type: ignore[arg-type]
    # The chunked form refuses when it is called, not when its first chunk is taken.
    with pytest.raises(ValueError):
        stationary_index_chunks(n, rng=np.random.default_rng(0), **arguments)  # type: ignore[arg-type]


def _read(file_key: str) -> dict[str, np.ndarray]:
    table = read_arrow(registry.resolve("SB-001", file_key))
    return {column: table[column].to_numpy() for column in table.schema.names}


def test_stationary_bootstrap_agrees_with_arch_fixture_sb_001() -> None:
    # The statistic the readings score a resampled path on, so the fixture holds the draw and it together.
    from app.research.golden_search.chance import path_sharpe

    series, reference = _read("input"), _read("output")
    a, b = series["a"], series["b"]
    means, sharpes, differences = [], [], []
    for index in stationary_index_chunks(a.size, size=a.size, mean_block=5, replications=200_000, rng=np.random.default_rng(1)):
        sharpe = path_sharpe(a[index])
        means.append(a[index].mean(axis=1))
        sharpes.append(sharpe)
        differences.append(sharpe - path_sharpe(b[index]))
    for name, ours in (("mean", means), ("sharpe", sharpes), ("sharpe_difference", differences)):
        drawn = np.concatenate(ours)
        shares = [float(np.mean(drawn <= threshold)) for threshold in reference[name]]
        # Monte Carlo agreement, not float equality: each side places a share to sqrt(p(1-p)/200,000) <= 0.00112,
        # so their difference has a standard error of at most 0.0016, and 0.008 is five of them.
        assert np.allclose(shares, reference["level"], atol=0.008, rtol=0.0), name
