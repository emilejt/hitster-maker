"""The song embedding (item2vec, 802K × 128): loading, nearest-neighbour search, clustering.

Vectors are L2-normalised once at load time, so every cosine similarity below is a plain dot
product. Search is exact brute force (one 802K × 128 mat-vec, a few ms), which is why the Annoy
index exported by the training notebook is not used. See docs/song2vec.md.
"""

import json

import numpy as np

from app.core.config import MODELS_DIR
from app.core.logging import get_logger

log = get_logger("services.embedding")

# Module-level singletons: loaded once on first call, never reloaded.
_vectors_norm: np.ndarray | None = None
_mbid_to_idx: dict[str, int] | None = None
_idx_to_mbid: dict[str, str] | None = None


def _load() -> None:
    global _vectors_norm, _mbid_to_idx, _idx_to_mbid
    if _vectors_norm is not None:
        return

    log.info("loading embedding model...")
    vectors = np.load(MODELS_DIR / "item2vec.wordvectors.vectors.npy")
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    _vectors_norm = vectors / np.where(norms == 0, 1.0, norms)

    with open(MODELS_DIR / "mbid_to_idx.json") as f:
        _mbid_to_idx = json.load(f)

    with open(MODELS_DIR / "idx_to_mbid.json") as f:
        _idx_to_mbid = json.load(f)
    assert _mbid_to_idx is not None
    log.info("model ready: %s songs, %dd vectors", f"{len(_mbid_to_idx):,}", vectors.shape[1])


def is_in_model(mbid: str) -> bool:
    _load()
    assert _mbid_to_idx is not None
    return mbid in _mbid_to_idx


def cosine_mbids_against_vector(
    mbids: list[str], vector: np.ndarray
) -> dict[str, float]:
    """Cosine similarity of each given MBID's (unit-norm) vector against `vector`.

    Unlike `get_neighbors_for_vector` (which searches the whole vocab), this scores only a
    caller-supplied set — used to rank one player's own cluster songs by closeness to another
    player's centroid (the nearest-seeds view). `vector` is assumed unit-norm (a centroid), so
    the dot product is a cosine. MBIDs absent from the model vocab are skipped.
    """
    _load()
    assert _mbid_to_idx is not None and _vectors_norm is not None

    out: dict[str, float] = {}
    for mbid in mbids:
        idx = _mbid_to_idx.get(mbid)
        if idx is not None:
            out[mbid] = float(_vectors_norm[idx] @ vector)
    return out


def get_neighbors_for_vector(
    vector: np.ndarray,
    n: int,
    exclude: set[str] | None = None,
) -> list[str]:
    """The `n` songs closest (cosine) to an arbitrary unit-norm query vector, best first.

    The query need not be a song: it is usually a cluster centroid or a blend of two.
    `exclude` lists MBIDs to skip (typically the players' own songs).
    """
    _load()
    assert _idx_to_mbid is not None and _vectors_norm is not None

    exclude = exclude or set()
    sims = _vectors_norm @ vector

    # Over-fetch so that filtering out `exclude` still leaves n results.
    fetch = min(n + len(exclude) + 1, len(sims))
    top_idxs = np.argpartition(sims, -fetch)[-fetch:]
    top_idxs = top_idxs[np.argsort(sims[top_idxs])[::-1]]

    result = []
    for i in top_idxs:
        mbid = _idx_to_mbid[str(int(i))]
        if mbid in exclude:
            continue
        result.append(mbid)
        if len(result) == n:
            break

    return result


# ── Taste-cluster analysis ────────────────────────────────────────────────────

def _kmeans_cosine(
    vectors: np.ndarray, k: int, n_iter: int = 50, seed: int = 42
) -> tuple[np.ndarray, np.ndarray]:
    """K-means on unit-norm vectors using cosine similarity. Returns (centroids [k,d], labels [n])."""
    rng = np.random.default_rng(seed)
    n = len(vectors)

    # K-means++ init — pick centroids with probability ∝ cosine distance to nearest chosen
    first = int(rng.integers(n))
    centroids = [vectors[first].copy()]
    for _ in range(k - 1):
        sims = vectors @ np.array(centroids).T          # [n, chosen]
        dists = np.clip(1.0 - sims.max(axis=1), 0, None)
        total = dists.sum()
        probs = dists / total if total > 0 else np.full(n, 1.0 / n)
        centroids.append(vectors[int(rng.choice(n, p=probs))].copy())
    centroids_arr = np.array(centroids)                 # [k, d]

    labels = np.zeros(n, dtype=int)
    for _ in range(n_iter):
        new_labels = (vectors @ centroids_arr.T).argmax(axis=1)
        if np.array_equal(new_labels, labels):
            break
        labels = new_labels
        for i in range(k):
            mask = labels == i
            if mask.any():
                c = vectors[mask].mean(axis=0)
                norm = np.linalg.norm(c)
                centroids_arr[i] = c / norm if norm > 0 else c

    return centroids_arr, labels


def _silhouette_score(vectors: np.ndarray, labels: np.ndarray, k: int) -> float:
    """Mean silhouette coefficient using cosine distance (1 − cosine_sim)."""
    n = len(vectors)
    if k <= 1 or n <= k:
        return 0.0

    dists = np.clip(1.0 - (vectors @ vectors.T), 0, None)  # [n, n]
    scores = []
    for i in range(n):
        own = int(labels[i])
        own_mask = labels == own
        own_mask[i] = False
        a = float(dists[i, own_mask].mean()) if own_mask.any() else 0.0
        own_mask[i] = True  # restore

        b_vals = [
            float(dists[i, labels == c].mean())
            for c in range(k)
            if c != own and (labels == c).any()
        ]
        if not b_vals:
            continue
        b = min(b_vals)
        denom = max(a, b)
        scores.append((b - a) / denom if denom > 0 else 0.0)

    return round(float(np.mean(scores)), 4) if scores else 0.0


def _agglomerative_labels(vectors: np.ndarray, distance_threshold: float) -> np.ndarray:
    """Average-linkage (UPGMA) agglomerative clustering on **cosine distance**, cut once at a
    single global `distance_threshold`. Returns a labels array [n] with contiguous cluster ids.

    Vectors are unit-norm, so cosine distance = 1 − (vᵢ · vⱼ) ∈ [0, 2]. Clusters merge from
    the closest pair up; merging stops the moment the closest remaining pair sits *above* the
    cut, so a lower cut ⇒ more, tighter clusters. Uses the Lance-Williams recurrence for the
    average-linkage update over a live distance matrix (dead clusters held at ∞), so each merge
    is an O(n²) numpy scan — fine for a player's few-hundred seeds."""
    n = len(vectors)
    if n <= 1:
        return np.zeros(n, dtype=int)

    sims = np.clip(vectors @ vectors.T, -1.0, 1.0)
    dist = (1.0 - sims).astype(float)
    np.fill_diagonal(dist, np.inf)

    members: dict[int, list[int]] = {i: [i] for i in range(n)}
    sizes = np.ones(n, dtype=float)
    active = np.ones(n, dtype=bool)

    for _ in range(n - 1):
        flat = int(np.argmin(dist))
        i, j = divmod(flat, n)
        if not np.isfinite(dist[i, j]) or dist[i, j] > distance_threshold:
            break
        # Merge j into i. Lance-Williams average linkage: dead columns are already ∞, so the
        # weighted mean leaves them ∞; positions i and j resolve to ∞ too (their own diagonals).
        ni, nj = sizes[i], sizes[j]
        new_row = (ni * dist[i] + nj * dist[j]) / (ni + nj)
        dist[i, :] = new_row
        dist[:, i] = new_row
        dist[j, :] = np.inf
        dist[:, j] = np.inf
        active[j] = False
        sizes[i] = ni + nj
        members[i].extend(members[j])

    labels = np.empty(n, dtype=int)
    for cid, rep in enumerate(i for i in range(n) if active[i]):
        for m in members[rep]:
            labels[m] = cid
    return labels


def agglomerate_taste_clusters(
    mbids: list[str], distance_threshold: float, min_cluster_size: int = 1
) -> dict | None:
    """Cluster seed MBIDs by agglomerative (hierarchical) clustering with a single global
    cosine-distance cut. Returns one analysis dict shaped like an `analyse_taste_clusters`
    entry — `{k, distance, min_cluster_size, dismissed, silhouette, clusters:[{centroid, mbids,
    sims}]}` — so the taste reports treat it interchangeably with the k-means result. `k` is the
    *resulting* cluster count (an output of the cut, not an input). None if no MBIDs are in vocab.

    `min_cluster_size` dismisses clusters with fewer than that many songs — a distance cut tends
    to shed weak singletons/pairs that are poor taste signal. Dismissed clusters' songs drop out
    of the analysis (treated as noise); `dismissed` reports how many clusters were cut."""
    _load()
    assert _mbid_to_idx is not None and _vectors_norm is not None

    valid_pairs = [(m, _mbid_to_idx[m]) for m in mbids if m in _mbid_to_idx]
    if not valid_pairs:
        return None

    valid_mbids, idxs = zip(*valid_pairs)
    vectors = _vectors_norm[list(idxs)]  # [n, 128], already unit-norm
    n = len(vectors)

    labels = _agglomerative_labels(vectors, distance_threshold)
    raw_k = int(labels.max()) + 1

    # Keep only clusters clearing the minimum-size threshold; their order (ascending original
    # id) is deterministic, so the surviving clusters get stable contiguous indices 0..k-1 —
    # the same indices every analysis endpoint sees for a given (distance, min) pair.
    kept = [ci for ci in range(raw_k) if int((labels == ci).sum()) >= min_cluster_size]

    clusters = []
    for ci in kept:
        mask = labels == ci
        c = vectors[mask].mean(axis=0)
        norm = np.linalg.norm(c)
        centroid = c / norm if norm > 0 else c
        clusters.append(
            {
                "centroid": centroid,
                "mbids": [valid_mbids[i] for i in range(n) if labels[i] == ci],
                "sims": [
                    round(float(vectors[i] @ centroid), 4)
                    for i in range(n)
                    if labels[i] == ci
                ],
            }
        )
    k = len(clusters)

    # Silhouette over the surviving songs only, with kept clusters relabeled 0..k-1.
    if k > 1:
        remap = {ci: i for i, ci in enumerate(kept)}
        keep_mask = np.isin(labels, np.array(kept))
        sub_labels = np.array([remap[int(lbl)] for lbl in labels[keep_mask]])
        sil = _silhouette_score(vectors[keep_mask], sub_labels, k)
    else:
        sil = None

    return {
        "k": k,
        "distance": round(float(distance_threshold), 4),
        "min_cluster_size": int(min_cluster_size),
        "dismissed": raw_k - k,
        "silhouette": sil,
        "clusters": clusters,
    }


def analyse_taste_clusters(mbids: list[str], k_max: int = 4) -> list[dict]:
    """
    Runs k-means for k=1..k_max on the seed MBIDs' embedding vectors.
    Returns [{k, silhouette, clusters: [{centroid: np.ndarray, mbids: list[str]}]}].
    MBIDs absent from the model vocab are silently skipped.
    """
    _load()
    assert _mbid_to_idx is not None and _vectors_norm is not None

    valid_pairs = [(m, _mbid_to_idx[m]) for m in mbids if m in _mbid_to_idx]
    if not valid_pairs:
        return []

    valid_mbids, idxs = zip(*valid_pairs)
    vectors = _vectors_norm[list(idxs)]  # [n, 128], already unit-norm
    n = len(vectors)

    results = []
    for k in range(1, min(k_max, n) + 1):
        if k == 1:
            c = vectors.mean(axis=0)
            norm = np.linalg.norm(c)
            centroids = (c / norm if norm > 0 else c).reshape(1, -1)
            labels = np.zeros(n, dtype=int)
            sil = None
        else:
            centroids, labels = _kmeans_cosine(vectors, k)
            sil = _silhouette_score(vectors, labels, k)

        clusters = [
            {
                "centroid": centroids[ci],
                "mbids": [valid_mbids[i] for i in range(n) if labels[i] == ci],
                "sims": [round(float(vectors[i] @ centroids[ci]), 4) for i in range(n) if labels[i] == ci],
            }
            for ci in range(k)
        ]
        results.append({"k": k, "silhouette": sil, "clusters": clusters})

    return results
