"""Tests for Hungarian-aligned regime drift module."""
from __future__ import annotations

import numpy as np

from app.engine.edge.regime_drift import (
    align_labels,
    centroid_displacement,
    relabel,
    stability_score,
    transition_matrix_kl,
)


def test_align_labels_recovers_permutation():
    ref = np.array([[0, 0], [10, 10], [0, 10]], dtype=np.float64)
    new = ref[[2, 0, 1]]  # reorder
    result = align_labels(reference_centroids=ref, new_centroids=new)
    new_labels = np.array([0, 1, 2, 0])
    aligned = relabel(new_labels, result.permutation)
    np.testing.assert_array_equal(aligned, [2, 0, 1, 2])


def test_align_labels_handles_identity():
    ref = np.array([[0, 0], [5, 5], [0, 5]], dtype=np.float64)
    result = align_labels(reference_centroids=ref, new_centroids=ref.copy())
    np.testing.assert_array_equal(result.permutation, np.arange(3))


def test_align_labels_permutes_transition_matrix():
    ref = np.array([[0, 0], [10, 10]], dtype=np.float64)
    new = ref[[1, 0]]
    new_A = np.array([[0.9, 0.1], [0.2, 0.8]])
    result = align_labels(
        reference_centroids=ref, new_centroids=new, new_transition_matrix=new_A,
    )
    assert result.aligned_transition_matrix is not None
    np.testing.assert_allclose(
        result.aligned_transition_matrix, np.array([[0.8, 0.2], [0.1, 0.9]]), atol=1e-12
    )


def test_kl_zero_for_identical_matrices():
    A = np.array([[0.9, 0.1], [0.1, 0.9]])
    assert abs(transition_matrix_kl(A, A)) < 1e-9


def test_kl_positive_for_different_matrices():
    A = np.array([[0.95, 0.05], [0.05, 0.95]])
    B = np.array([[0.50, 0.50], [0.50, 0.50]])
    kl = transition_matrix_kl(A, B)
    assert kl > 0.1


def test_stability_score_lower_for_more_similar_models():
    A1 = np.array([[0.9, 0.1], [0.1, 0.9]])
    A2 = np.array([[0.91, 0.09], [0.09, 0.91]])
    A3 = np.array([[0.5, 0.5], [0.5, 0.5]])
    c = np.array([[0.0, 0.0], [10.0, 10.0]])
    s_close = stability_score(
        prev_transition=A1, new_transition_aligned=A2,
        prev_centroids=c, new_centroids_aligned=c,
    )
    s_far = stability_score(
        prev_transition=A1, new_transition_aligned=A3,
        prev_centroids=c, new_centroids_aligned=c,
    )
    assert s_close < s_far


def test_centroid_displacement_zero_when_identical():
    c = np.array([[0.0, 0.0], [5.0, 5.0]])
    assert centroid_displacement(c, c) == 0.0
