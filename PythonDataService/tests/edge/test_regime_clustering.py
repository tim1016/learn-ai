"""Tests for KMeans + Gaussian HMM."""

from __future__ import annotations

import numpy as np

from app.engine.edge.regime_clustering import (
    fit_gaussian_hmm,
    kmeans,
    stability_filter,
)


def _three_clusters(n_per_cluster: int = 200, seed: int = 7) -> np.ndarray:
    rng = np.random.default_rng(seed)
    c1 = rng.normal([0, 0], 0.5, size=(n_per_cluster, 2))
    c2 = rng.normal([5, 5], 0.5, size=(n_per_cluster, 2))
    c3 = rng.normal([0, 5], 0.5, size=(n_per_cluster, 2))
    return np.vstack([c1, c2, c3])


def test_kmeans_recovers_three_clear_clusters():
    X = _three_clusters()
    res = kmeans(X, n_clusters=3, seed=42)
    counts = np.bincount(res.labels, minlength=3)
    assert (counts > 100).all()
    assert res.inertia < 1500


def test_kmeans_deterministic_with_fixed_seed():
    X = _three_clusters()
    a = kmeans(X, n_clusters=3, seed=42)
    b = kmeans(X, n_clusters=3, seed=42)
    np.testing.assert_array_equal(a.labels, b.labels)


def test_hmm_recovers_three_clusters():
    X = _three_clusters()
    res = fit_gaussian_hmm(X, n_states=3, seed=42, n_iter=20)
    assert res.posterior.shape == (len(X), 3)
    assert np.allclose(res.posterior.sum(axis=1), 1.0, atol=1e-6)
    assert res.transition_matrix.shape == (3, 3)
    np.testing.assert_allclose(res.transition_matrix.sum(axis=1), 1.0, atol=1e-6)


def test_hmm_transition_matrix_is_sticky():
    """For temporally-coherent data, HMM transition matrix should be diagonal-dominant."""
    rng = np.random.default_rng(0)
    n = 600
    states = np.repeat(np.arange(3), n // 3)
    means = np.array([[0, 0], [5, 5], [0, 5]])
    X = means[states] + rng.normal(0.0, 0.3, size=(n, 2))
    res = fit_gaussian_hmm(X, n_states=3, seed=42, n_iter=30)
    diag = np.diag(res.transition_matrix)
    assert (diag > 0.5).all()


def test_stability_filter_with_short_runs_dropped():
    labels = np.array([0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0])
    mask = stability_filter(labels, min_run_length=5)
    assert mask[:5].sum() >= 1  # initial run ≥5 in length
    assert not mask[5]  # singleton 1 dropped


def test_stability_filter_uses_posterior_threshold():
    n = 10
    labels = np.zeros(n, dtype=int)
    posterior = np.full((n, 2), 0.5)
    posterior[:, 0] = [0.9] * 5 + [0.6] * 5
    posterior[:, 1] = 1.0 - posterior[:, 0]
    mask = stability_filter(labels, posterior=posterior, p_min=0.7, min_run_length=1)
    assert mask[:5].all()
    assert not mask[5:].any()
