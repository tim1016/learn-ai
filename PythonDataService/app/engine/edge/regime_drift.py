"""Regime drift control: rolling refit + Hungarian label alignment + stability score.

Hungarian (linear-sum-assignment) label alignment solves the canonical
HMM/k-means label-switching problem: state 0 in window 1 may correspond to
state 2 in window 2 even when the underlying regime is unchanged. We align
by minimum centroid distance.

Stability score combines:
- Symmetric KL divergence between consecutive transition matrices
- Mean centroid displacement (Euclidean)
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.optimize import linear_sum_assignment


@dataclass(frozen=True)
class AlignmentResult:
    permutation: np.ndarray   # (K,) int — old label j maps to permutation[j]
    aligned_centroids: np.ndarray
    aligned_transition_matrix: np.ndarray | None


def align_labels(
    *,
    reference_centroids: np.ndarray,
    new_centroids: np.ndarray,
    new_transition_matrix: np.ndarray | None = None,
) -> AlignmentResult:
    """Solve the assignment that minimizes total centroid distance.

    Returns a permutation P such that aligned_centroid[P[k]] ≈ reference_centroid[k].
    If a transition matrix is supplied, both rows and columns are permuted.
    """
    if reference_centroids.shape != new_centroids.shape:
        raise ValueError("reference and new centroids must have the same shape")
    cost = np.linalg.norm(reference_centroids[:, None, :] - new_centroids[None, :, :], axis=2)
    row_ind, col_ind = linear_sum_assignment(cost)
    permutation = np.empty(len(reference_centroids), dtype=int)
    for ref_idx, new_idx in zip(row_ind, col_ind, strict=True):
        permutation[new_idx] = ref_idx

    aligned_centroids = new_centroids[col_ind]
    aligned_transition = None
    if new_transition_matrix is not None:
        aligned_transition = new_transition_matrix[np.ix_(col_ind, col_ind)]
    return AlignmentResult(
        permutation=permutation,
        aligned_centroids=aligned_centroids,
        aligned_transition_matrix=aligned_transition,
    )


def relabel(labels: np.ndarray, permutation: np.ndarray) -> np.ndarray:
    """Apply the permutation to a label series."""
    return permutation[labels]


def transition_matrix_kl(p: np.ndarray, q: np.ndarray, eps: float = 1e-9) -> float:
    """Symmetric KL divergence between two transition matrices."""
    p_safe = p + eps
    q_safe = q + eps
    p_safe = p_safe / p_safe.sum(axis=1, keepdims=True)
    q_safe = q_safe / q_safe.sum(axis=1, keepdims=True)
    kl_pq = (p_safe * np.log(p_safe / q_safe)).sum()
    kl_qp = (q_safe * np.log(q_safe / p_safe)).sum()
    return float(0.5 * (kl_pq + kl_qp))


def centroid_displacement(c1: np.ndarray, c2: np.ndarray) -> float:
    """Mean Euclidean distance between aligned centroids."""
    return float(np.linalg.norm(c1 - c2, axis=1).mean())


def stability_score(
    *,
    prev_transition: np.ndarray,
    new_transition_aligned: np.ndarray,
    prev_centroids: np.ndarray,
    new_centroids_aligned: np.ndarray,
    kl_weight: float = 0.5,
) -> float:
    """Composite stability score; lower is more stable.

    Returns kl_weight * KL(prev || new) + (1 - kl_weight) * mean centroid shift.
    """
    kl = transition_matrix_kl(prev_transition, new_transition_aligned)
    shift = centroid_displacement(prev_centroids, new_centroids_aligned)
    return kl_weight * kl + (1.0 - kl_weight) * shift
