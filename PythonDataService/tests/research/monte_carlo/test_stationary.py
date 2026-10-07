"""Stationary bootstrap draws: determinism, block structure and the variance Politis & Romano derive."""

from __future__ import annotations

import numpy as np
import pytest

from app.research.monte_carlo.stationary import CHUNK_REPLICATIONS, stationary_index_chunks, stationary_indices


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


def test_stationary_indices_same_seed_same_indices() -> None:
    first = stationary_indices(50, size=50, mean_block=5, replications=20, rng=np.random.default_rng(9))
    again = stationary_indices(50, size=50, mean_block=5, replications=20, rng=np.random.default_rng(9))
    assert first.shape == (20, 50)
    assert first.dtype == np.int64
    assert np.array_equal(first, again)
    assert first.min() >= 0 and first.max() < 50


def test_stationary_indices_mean_block_one_is_the_ordinary_bootstrap() -> None:
    drawn = stationary_indices(30, size=30, mean_block=1, replications=8, rng=np.random.default_rng(3))
    assert np.array_equal(drawn, np.random.default_rng(3).integers(0, 30, size=(8, 30)))


def test_stationary_indices_one_block_wraps_from_last_to_first() -> None:
    drawn = stationary_indices(7, size=20, mean_block=1e12, replications=5, rng=np.random.default_rng(4))
    assert np.array_equal(drawn, (drawn[:, :1] + np.arange(20)) % 7)


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
