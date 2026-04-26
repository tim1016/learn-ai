"""K-means and Gaussian HMM regime clustering.

Hand-rolled implementations to avoid adding hmmlearn / scikit-learn dependencies.
Per CLAUDE.md "Sovereign over the math" — the only third-party math here is
scipy.stats.multivariate_normal (already in the existing dependency set).

K-means: Lloyd (1982) with k-means++ initialization (Arthur & Vassilvitskii 2007).
Gaussian HMM: Baum-Welch EM with forward-backward in log-space for numerical
              stability. See Rabiner (1989), "A Tutorial on Hidden Markov Models".
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.special import logsumexp
from scipy.stats import multivariate_normal


@dataclass(frozen=True)
class KMeansResult:
    labels: np.ndarray         # (T,) int
    centroids: np.ndarray      # (K, D)
    inertia: float             # within-cluster SSE


@dataclass(frozen=True)
class HMMResult:
    labels: np.ndarray              # (T,) Viterbi path
    posterior: np.ndarray           # (T, K)
    means: np.ndarray               # (K, D)
    covariances: np.ndarray         # (K, D, D)
    transition_matrix: np.ndarray   # (K, K)
    initial_probs: np.ndarray       # (K,)
    log_likelihood: float


def kmeans(
    X: np.ndarray, n_clusters: int, *, n_iter: int = 100,
    tol: float = 1e-6, seed: int = 42,
) -> KMeansResult:
    """Lloyd's algorithm with k-means++ init."""
    if X.ndim != 2:
        raise ValueError("X must be 2-D (T x D)")
    rng = np.random.default_rng(seed)
    centroids = _kmeans_pp_init(X, n_clusters, rng)
    labels = np.zeros(len(X), dtype=int)

    for _ in range(n_iter):
        d = np.linalg.norm(X[:, None, :] - centroids[None, :, :], axis=2)
        new_labels = np.argmin(d, axis=1)

        new_centroids = np.empty_like(centroids)
        for k in range(n_clusters):
            mask = new_labels == k
            if not mask.any():
                idx = rng.integers(0, len(X))
                new_centroids[k] = X[idx]
            else:
                new_centroids[k] = X[mask].mean(axis=0)

        shift = np.linalg.norm(new_centroids - centroids)
        centroids = new_centroids
        labels = new_labels
        if shift < tol:
            break

    inertia = float(((X - centroids[labels]) ** 2).sum())
    return KMeansResult(labels=labels, centroids=centroids, inertia=inertia)


def _kmeans_pp_init(X: np.ndarray, k: int, rng: np.random.Generator) -> np.ndarray:
    n = len(X)
    centroids = np.empty((k, X.shape[1]), dtype=X.dtype)
    centroids[0] = X[rng.integers(0, n)]
    for j in range(1, k):
        d2 = np.min(np.linalg.norm(X[:, None, :] - centroids[None, :j, :], axis=2) ** 2, axis=1)
        if d2.sum() == 0:
            centroids[j] = X[rng.integers(0, n)]
        else:
            probs = d2 / d2.sum()
            centroids[j] = X[rng.choice(n, p=probs)]
    return centroids


def fit_gaussian_hmm(
    X: np.ndarray, n_states: int, *, n_iter: int = 50,
    tol: float = 1e-4, seed: int = 42, reg_covar: float = 1e-4,
) -> HMMResult:
    """Fit a Gaussian HMM with full covariance via Baum-Welch.

    Initialization is k-means; emission covariances are regularized by
    `reg_covar * I` to ensure positive-definiteness.
    """
    if X.ndim != 2:
        raise ValueError("X must be 2-D (T x D)")
    T, D = X.shape

    km = kmeans(X, n_states, seed=seed)
    means = km.centroids.copy()
    covariances = np.stack([
        np.cov(X[km.labels == k].T) + reg_covar * np.eye(D)
        if (km.labels == k).sum() > 1 else np.eye(D) * reg_covar * 10
        for k in range(n_states)
    ])
    transition_matrix = np.full((n_states, n_states), 1.0 / n_states)
    transition_matrix = 0.9 * np.eye(n_states) + 0.1 * transition_matrix
    transition_matrix /= transition_matrix.sum(axis=1, keepdims=True)
    initial_probs = np.full(n_states, 1.0 / n_states)

    prev_ll = -np.inf
    for _ in range(n_iter):
        log_emit = _log_emission(X, means, covariances)
        log_alpha = _forward(log_emit, np.log(transition_matrix), np.log(initial_probs))
        log_beta = _backward(log_emit, np.log(transition_matrix))

        log_gamma = log_alpha + log_beta
        log_gamma -= logsumexp(log_gamma, axis=1, keepdims=True)
        gamma = np.exp(log_gamma)

        log_xi = (
            log_alpha[:-1, :, None]
            + np.log(transition_matrix)[None, :, :]
            + log_emit[1:, None, :]
            + log_beta[1:, None, :]
        )
        log_xi -= logsumexp(log_xi.reshape(T - 1, -1), axis=1)[:, None, None]
        xi = np.exp(log_xi)

        initial_probs = gamma[0]
        transition_matrix = xi.sum(axis=0) / gamma[:-1].sum(axis=0)[:, None]
        transition_matrix /= transition_matrix.sum(axis=1, keepdims=True)

        weights = gamma.sum(axis=0)
        for k in range(n_states):
            means[k] = (gamma[:, k:k + 1] * X).sum(axis=0) / max(weights[k], 1e-12)
            diff = X - means[k]
            covariances[k] = (gamma[:, k:k + 1] * diff).T @ diff / max(weights[k], 1e-12)
            covariances[k] += reg_covar * np.eye(D)

        ll = float(logsumexp(log_alpha[-1]))
        if abs(ll - prev_ll) < tol:
            break
        prev_ll = ll

    log_emit = _log_emission(X, means, covariances)
    log_alpha = _forward(log_emit, np.log(transition_matrix), np.log(initial_probs))
    log_beta = _backward(log_emit, np.log(transition_matrix))
    log_gamma = log_alpha + log_beta
    log_gamma -= logsumexp(log_gamma, axis=1, keepdims=True)
    posterior = np.exp(log_gamma)
    viterbi_path = _viterbi(log_emit, np.log(transition_matrix), np.log(initial_probs))

    return HMMResult(
        labels=viterbi_path,
        posterior=posterior,
        means=means,
        covariances=covariances,
        transition_matrix=transition_matrix,
        initial_probs=initial_probs,
        log_likelihood=float(logsumexp(log_alpha[-1])),
    )


def _log_emission(X: np.ndarray, means: np.ndarray, covs: np.ndarray) -> np.ndarray:
    T = len(X)
    K = len(means)
    out = np.empty((T, K))
    for k in range(K):
        out[:, k] = multivariate_normal.logpdf(X, mean=means[k], cov=covs[k], allow_singular=True)
    return out


def _forward(log_emit: np.ndarray, log_A: np.ndarray, log_pi: np.ndarray) -> np.ndarray:
    T = log_emit.shape[0]
    log_alpha = np.empty_like(log_emit)
    log_alpha[0] = log_pi + log_emit[0]
    for t in range(1, T):
        log_alpha[t] = log_emit[t] + logsumexp(log_alpha[t - 1][:, None] + log_A, axis=0)
    return log_alpha


def _backward(log_emit: np.ndarray, log_A: np.ndarray) -> np.ndarray:
    T = log_emit.shape[0]
    log_beta = np.zeros_like(log_emit)
    for t in range(T - 2, -1, -1):
        log_beta[t] = logsumexp(log_A + log_emit[t + 1][None, :] + log_beta[t + 1][None, :], axis=1)
    return log_beta


def _viterbi(log_emit: np.ndarray, log_A: np.ndarray, log_pi: np.ndarray) -> np.ndarray:
    T, K = log_emit.shape
    delta = np.empty_like(log_emit)
    psi = np.zeros_like(log_emit, dtype=int)
    delta[0] = log_pi + log_emit[0]
    for t in range(1, T):
        scores = delta[t - 1][:, None] + log_A
        psi[t] = np.argmax(scores, axis=0)
        delta[t] = scores[psi[t], np.arange(K)] + log_emit[t]
    path = np.empty(T, dtype=int)
    path[-1] = int(np.argmax(delta[-1]))
    for t in range(T - 2, -1, -1):
        path[t] = psi[t + 1, path[t + 1]]
    return path


def stability_filter(
    labels: np.ndarray,
    *,
    posterior: np.ndarray | None = None,
    p_min: float = 0.7,
    min_run_length: int = 5,
) -> np.ndarray:
    """Return a boolean mask: True iff regime label is confidently active.

    Confidence test: max posterior > p_min (skipped if posterior is None).
    Persistence test: current run-length >= min_run_length.
    """
    n = len(labels)
    if posterior is not None:
        confident = posterior.max(axis=1) > p_min
    else:
        confident = np.ones(n, dtype=bool)

    run_length = np.empty(n, dtype=int)
    run_length[0] = 1
    for i in range(1, n):
        run_length[i] = run_length[i - 1] + 1 if labels[i] == labels[i - 1] else 1

    return confident & (run_length >= min_run_length)
