"""The hand-written average-linkage clustering behind each player's taste zones."""

import numpy as np

from app.services.embedding_service import _agglomerative_labels


def _blob(center: np.ndarray, n: int, noise: float, rng) -> np.ndarray:
    pts = center + noise * rng.standard_normal((n, len(center)))
    return pts / np.linalg.norm(pts, axis=1, keepdims=True)


def _three_taste_zones(rng):
    d = 16
    centers = np.eye(d)[:3]  # mutually orthogonal: cosine distance 1 between zones
    vectors = np.vstack([_blob(c, 20, 0.05, rng) for c in centers])
    truth = np.repeat([0, 1, 2], 20)
    return vectors, truth


def test_recovers_well_separated_zones():
    vectors, truth = _three_taste_zones(np.random.default_rng(0))
    labels = _agglomerative_labels(vectors, distance_threshold=0.5)
    assert len(set(labels)) == 3
    # Same partition as the ground truth, up to renaming the cluster ids.
    for zone in range(3):
        assert len(set(labels[truth == zone])) == 1


def test_cut_controls_granularity():
    vectors, _ = _three_taste_zones(np.random.default_rng(1))
    assert len(set(_agglomerative_labels(vectors, distance_threshold=1.5))) == 1
    assert len(set(_agglomerative_labels(vectors, distance_threshold=1e-6))) == len(vectors)


def test_labels_are_contiguous_and_deterministic():
    vectors, _ = _three_taste_zones(np.random.default_rng(2))
    a = _agglomerative_labels(vectors, 0.5)
    b = _agglomerative_labels(vectors, 0.5)
    assert np.array_equal(a, b)
    assert set(a) == set(range(a.max() + 1))
