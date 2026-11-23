"""Taste-analysis reports: the math behind the /analysis tuning tool.

Exposes the primitives the deck builder's embedding tiers run on (per-player clustering,
centroid neighbours, cross-player centroid proximity, centroid blends) one step at a time,
so constants such as the clustering cut could be chosen by inspecting real accounts.

Pure: takes `PlayerLibrary` objects + parameters, returns plain dicts. Clustering is
deterministic (seeded k-means, or a fixed agglomerative cut), so a cluster can be referred to
by index across requests.
"""

from __future__ import annotations

import numpy as np

from app.core.logging import get_logger
from app.pipeline import track_resolution
from app.pipeline.models import PlayerLibrary, SourcedTrack
from app.services import bridge_service, embedding_service, popularity_service

log = get_logger("pipeline.taste")


class ClusterIndexError(ValueError):
    """A requested cluster index is out of range for that player's effective k."""


def _candidate_seeds(library: PlayerLibrary) -> list[tuple[str, SourcedTrack, bool]]:
    """Library tracks resolved to model MBIDs — deduped, order preserved.

    Returns `(mbid, track, in_model)` per unique bridge-resolved MBID, keeping the
    `SourcedTrack` (so names/years come for free) and reporting model membership so the caller
    can show bridge vs. vocabulary coverage.
    """
    by_isrc = bridge_service.get_mbids_for_isrcs(
        [t.isrc for t in library.tracks if t.isrc]
    )
    out: list[tuple[str, SourcedTrack, bool]] = []
    seen: set[str] = set()
    for t in library.tracks:
        if not t.isrc:
            continue
        mbid = by_isrc.get(t.isrc)
        if not mbid or mbid in seen:
            continue
        seen.add(mbid)
        out.append((mbid, t, embedding_service.is_in_model(mbid)))
    return out


def seed_tracks(library: PlayerLibrary) -> list[tuple[str, SourcedTrack]]:
    """The in-vocab subset of `_candidate_seeds` — the MBIDs that actually get clustered
    and generate neighbours. Order preserved; deduped. Reused by later reports."""
    return [
        (mbid, track) for mbid, track, in_model in _candidate_seeds(library) if in_model
    ]


# Clustering method the reports understand. "kmeans" uses `k`; "agglomerative" uses a single
# global cosine-distance `distance` cut (k becomes an *output*, the resulting cluster count).
KMEANS = "kmeans"
AGGLOMERATIVE = "agglomerative"


def _cluster_analysis(
    mbids: list[str],
    method: str,
    k: int,
    distance: float,
    min_cluster_size: int = 1,
) -> dict | None:
    """One clustering *analysis* dict — `{k, silhouette, clusters:[{centroid, mbids, sims}]}`
    (+ `distance`/`min_cluster_size`/`dismissed` for agglomerative) — for the chosen method, or
    None if no seeds are in-vocab.

    Both methods share the same shape, so every report below is method-agnostic: k-means takes
    the last of the `analyse_taste_clusters` ladder (the requested k, clamped to the seed
    count); agglomerative takes the single global-cut result, minus clusters below
    `min_cluster_size` (k-means ignores that threshold — its k clusters are all kept)."""
    if method == AGGLOMERATIVE:
        return embedding_service.agglomerate_taste_clusters(
            mbids, distance, min_cluster_size
        )
    analyses = embedding_service.analyse_taste_clusters(mbids, k_max=k)
    return analyses[-1] if analyses else None


def cluster_report(
    library: PlayerLibrary,
    k: int,
    method: str = KMEANS,
    distance: float = 0.5,
    min_cluster_size: int = 1,
) -> dict:
    """Cluster one player's in-vocab seed songs into taste zones.

    With `method="kmeans"` splits into `k` zones; with `method="agglomerative"` merges under a
    single global cosine-distance `distance` cut, dropping clusters below `min_cluster_size`
    (the returned `k` is then the resulting count, `dismissed` the number cut). Returns::

        {k, method, distance, min_cluster_size, dismissed, silhouette, seeds_total,
         seeds_in_model, clusters: [{index, size, songs: [{mbid, name, artist, year, similarity}]}]}

    `seeds_total` counts unique bridge-resolved library tracks; `seeds_in_model` the subset
    actually clustered (dismissed songs are excluded from the clusters but still counted here).
    Each cluster's songs are sorted by descending closeness to the centroid. All metadata comes
    from the player's own library — no Spotify calls.
    """
    candidates = _candidate_seeds(library)
    seeds_total = len(candidates)
    in_model = [(mbid, track) for mbid, track, is_in in candidates if is_in]
    track_by_mbid = {mbid: track for mbid, track in in_model}

    analysis = _cluster_analysis(
        list(track_by_mbid.keys()), method, k, distance, min_cluster_size
    )
    if not analysis:
        return {
            "k": 0,
            "method": method,
            "distance": None,
            "min_cluster_size": None,
            "dismissed": None,
            "silhouette": None,
            "seeds_total": seeds_total,
            "seeds_in_model": 0,
            "clusters": [],
        }

    clusters = []
    for index, cluster in enumerate(analysis["clusters"]):
        songs = [
            {
                "mbid": mbid,
                "name": track_by_mbid[mbid].name,
                "artist": track_by_mbid[mbid].artist,
                "year": track_by_mbid[mbid].year,
                "similarity": sim,
            }
            for mbid, sim in zip(cluster["mbids"], cluster["sims"])
        ]
        songs.sort(key=lambda s: s["similarity"], reverse=True)
        clusters.append({"index": index, "size": len(songs), "songs": songs})

    return {
        "k": analysis["k"],
        "method": method,
        "distance": analysis.get("distance"),
        "min_cluster_size": analysis.get("min_cluster_size"),
        "dismissed": analysis.get("dismissed"),
        "silhouette": analysis["silhouette"],
        "seeds_total": seeds_total,
        "seeds_in_model": len(in_model),
        "clusters": clusters,
    }


async def resolve_metadata(mbids: list[str], token: str | None) -> dict[str, dict]:
    """Human-readable metadata for raw model MBIDs.

    Delegates to the shared `track_resolution` ladder (cache → rehydrate → ISRC → name/artist
    search), so viewing neighbours also **fully hydrates the Spotify cache** (year + uri) that
    the game later reads — "wait a little now, play fast later". For anything Spotify still
    can't find, we fall back to the MusicBrainz title/artist purely for display.

    Returns `{mbid: {name, artist, year, ...}}`; MBIDs with no Spotify hit *and* no MB entry
    are absent. No token ⇒ complete cache hits + MB names only (no Spotify calls).
    """
    unique = list(dict.fromkeys(mbids))
    if not unique:
        return {}

    resolved = await track_resolution.resolve_batch(unique, token)
    result: dict[str, dict] = dict(resolved)

    # Display-only names for whatever Spotify couldn't resolve (no year/uri, but readable).
    unresolved = [m for m in unique if m not in result]
    if unresolved:
        for mbid, m in bridge_service.get_meta_for_mbids(unresolved).items():
            result[mbid] = {"name": m["name"], "artist": m["artist"], "year": None}

    log.info(
        "taste: %d neighbours → %d resolved on Spotify, %d MB-name-only",
        len(unique), len(resolved), len(unique) - len(resolved),
    )
    return result


def _player_centroids(
    library: PlayerLibrary,
    method: str = KMEANS,
    k: int = 3,
    distance: float = 0.5,
    min_cluster_size: int = 1,
) -> tuple[list[np.ndarray], set[str], int]:
    """A player's cluster centroids (indexed by cluster), their seed MBID set, and the
    resulting cluster count. Same clustering as `cluster_report` (via `_cluster_analysis`), so
    cluster indices line up across all the analysis endpoints. Empty centroids ⇒ no in-vocab
    seeds. In agglomerative mode `k` is ignored, the count comes from the cut, and clusters
    below `min_cluster_size` are dropped (so their songs stay in the seed set but out of a
    centroid — the neighbours/proximity views then ignore that noise)."""
    seed_mbids = [mbid for mbid, _ in seed_tracks(library)]
    analysis = _cluster_analysis(seed_mbids, method, k, distance, min_cluster_size)
    if not analysis:
        return [], set(seed_mbids), 0
    centroids = [cluster["centroid"] for cluster in analysis["clusters"]]
    return centroids, set(seed_mbids), analysis["k"]


async def _ranked_neighbors(mbids: list[str], token: str | None) -> list[dict]:
    """Popularity-rank a neighbour MBID set and attach resolved metadata. One shape shared
    by the neighbours and combine views: `[{mbid, popularity, name, artist, year}]`, sorted
    by our popularity score descending."""
    popularity = popularity_service.get_popularity_scores(mbids)
    metadata = await resolve_metadata(mbids, token)
    ranked = sorted(mbids, key=lambda m: popularity.get(m, 0.0), reverse=True)
    return [
        {
            "mbid": mbid,
            "popularity": round(popularity.get(mbid, 0.0), 4),
            "name": metadata.get(mbid, {}).get("name"),
            "artist": metadata.get(mbid, {}).get("artist"),
            "year": metadata.get(mbid, {}).get("year"),
        }
        for mbid in ranked
    ]


async def neighbors_report(
    library: PlayerLibrary,
    k: int,
    n: int,
    token: str | None,
    method: str = KMEANS,
    distance: float = 0.5,
    min_cluster_size: int = 1,
) -> dict:
    """Per-cluster neighbours a player's centroids generate: the "what does this
    cluster pull" view.

    Clusters the player's in-vocab seeds (same as `cluster_report`, honouring `method`/`k`/
    `distance`/`min_cluster_size`), pulls the `n` nearest model neighbours of each centroid
    (excluding the player's own seeds), popularity-ranks them (our own `popularity_service`
    score — Spotify's is deprecated), and resolves every neighbour's name/artist/year. Returns::

        {k, n, clusters: [{index, neighbors: [{mbid, popularity, name, artist, year}]}]}
    """
    centroids, seeds_set, k_eff = _player_centroids(
        library, method, k, distance, min_cluster_size
    )
    if not centroids:
        return {"k": 0, "n": n, "clusters": []}

    per_cluster = [
        embedding_service.get_neighbors_for_vector(c, n=n, exclude=seeds_set)
        for c in centroids
    ]
    all_neighbors = [m for nbrs in per_cluster for m in nbrs]

    # Resolve + score once over the union, then rank each cluster independently.
    popularity = popularity_service.get_popularity_scores(all_neighbors)
    metadata = await resolve_metadata(all_neighbors, token)
    clusters = [
        {
            "index": index,
            "neighbors": [
                {
                    "mbid": mbid,
                    "popularity": round(popularity.get(mbid, 0.0), 4),
                    "name": metadata.get(mbid, {}).get("name"),
                    "artist": metadata.get(mbid, {}).get("artist"),
                    "year": metadata.get(mbid, {}).get("year"),
                }
                for mbid in sorted(
                    nbrs, key=lambda m: popularity.get(m, 0.0), reverse=True
                )
            ],
        }
        for index, nbrs in enumerate(per_cluster)
    ]
    return {"k": k_eff, "n": n, "clusters": clusters}


def proximity(
    lib_a: PlayerLibrary,
    k_a: int,
    lib_b: PlayerLibrary,
    k_b: int,
    method: str = KMEANS,
    distance: float = 0.5,
    min_cluster_size: int = 1,
) -> dict:
    """Cross-player cluster closeness: the "taste overlap" signal.

    Every A-cluster × B-cluster cosine similarity (both centroids are unit-norm), sorted
    strongest first, so you can see which of the two players' taste zones sit closest. In
    agglomerative mode both players share the one global `distance` cut and `min_cluster_size`.
    Returns `{k_a, k_b, pairs: [{a_cluster, b_cluster, similarity}]}` (resulting counts)."""
    centroids_a, _, k_a_eff = _player_centroids(
        lib_a, method, k_a, distance, min_cluster_size
    )
    centroids_b, _, k_b_eff = _player_centroids(
        lib_b, method, k_b, distance, min_cluster_size
    )

    pairs = [
        {"a_cluster": i, "b_cluster": j, "similarity": round(float(va @ vb), 4)}
        for i, va in enumerate(centroids_a)
        for j, vb in enumerate(centroids_b)
    ]
    pairs.sort(key=lambda p: p["similarity"], reverse=True)
    return {"k_a": k_a_eff, "k_b": k_b_eff, "pairs": pairs}


def nearest_seeds(
    base_lib: PlayerLibrary,
    base_cluster: int,
    k_base: int,
    cand_lib: PlayerLibrary,
    cand_cluster: int,
    k_candidate: int,
    n: int,
    method: str = KMEANS,
    distance: float = 0.5,
    min_cluster_size: int = 1,
) -> dict:
    """Which of the *candidate* player's real library songs sit closest to the *base* player's
    taste zone — concrete shared-taste songs (vs `combine_neighbors`' generated neighbours).

    Two directional roles: the **base** cluster provides the target centroid; the **candidate**
    cluster provides the pool of seed songs searched. Cosine each candidate seed against the
    base centroid, return the top `n` with their proximity score. Metadata is **free** — the
    candidate songs are that player's own library `SourcedTrack`s, so no Spotify resolution.

    Returns `{n, k_base, k_candidate, songs:[{mbid, name, artist, year, similarity}]}` (effective
    k's). Raises `ClusterIndexError` if either chosen index is out of range for its effective k.
    """
    base_centroids, _, k_base_eff = _player_centroids(
        base_lib, method, k_base, distance, min_cluster_size
    )
    if not (0 <= base_cluster < len(base_centroids)):
        raise ClusterIndexError(
            f"base_cluster {base_cluster} out of range (0..{len(base_centroids) - 1})"
        )
    centroid = base_centroids[base_cluster]

    cand_pairs = seed_tracks(cand_lib)  # [(mbid, SourcedTrack)], in-vocab, ordered
    track_by_mbid = {mbid: track for mbid, track in cand_pairs}
    cand_analysis = _cluster_analysis(
        [mbid for mbid, _ in cand_pairs], method, k_candidate, distance, min_cluster_size
    )
    clusters = cand_analysis["clusters"] if cand_analysis else []
    if not (0 <= cand_cluster < len(clusters)):
        raise ClusterIndexError(
            f"candidate_cluster {cand_cluster} out of range (0..{len(clusters) - 1})"
        )
    k_cand_eff = cand_analysis["k"] if cand_analysis else 0
    cand_mbids = clusters[cand_cluster]["mbids"]

    sims = embedding_service.cosine_mbids_against_vector(cand_mbids, centroid)
    ranked = sorted(sims.items(), key=lambda kv: kv[1], reverse=True)[:n]
    songs = [
        {
            "mbid": mbid,
            "name": track_by_mbid[mbid].name if mbid in track_by_mbid else None,
            "artist": track_by_mbid[mbid].artist if mbid in track_by_mbid else None,
            "year": track_by_mbid[mbid].year if mbid in track_by_mbid else None,
            "similarity": round(sim, 4),
        }
        for mbid, sim in ranked
    ]
    return {"n": n, "k_base": k_base_eff, "k_candidate": k_cand_eff, "songs": songs}


async def combine_neighbors(
    lib_a: PlayerLibrary,
    a_cluster: int,
    k_a: int,
    lib_b: PlayerLibrary,
    b_cluster: int,
    k_b: int,
    n: int,
    token: str | None,
    method: str = KMEANS,
    distance: float = 0.5,
    min_cluster_size: int = 1,
) -> dict:
    """Blend one A-cluster with one B-cluster and generate their shared-taste neighbours —
    the "common ground" mechanic behind tier 3.

    Blends the two chosen centroids (mean, renormalized — same as
    `deck._common_ground_vectors`), pulls the `n` nearest model neighbours of the blend
    excluding *both* players' seeds, popularity-ranks and resolves them. Returns
    `{n, neighbors: [{mbid, popularity, name, artist, year}]}`. Raises `ClusterIndexError` if
    a chosen cluster index is out of range for that player's cluster count."""
    centroids_a, seeds_a, _ = _player_centroids(
        lib_a, method, k_a, distance, min_cluster_size
    )
    centroids_b, seeds_b, _ = _player_centroids(
        lib_b, method, k_b, distance, min_cluster_size
    )
    if not (0 <= a_cluster < len(centroids_a)):
        raise ClusterIndexError(
            f"a_cluster {a_cluster} out of range (0..{len(centroids_a) - 1})"
        )
    if not (0 <= b_cluster < len(centroids_b)):
        raise ClusterIndexError(
            f"b_cluster {b_cluster} out of range (0..{len(centroids_b) - 1})"
        )

    blended = centroids_a[a_cluster] + centroids_b[b_cluster]
    norm = float(np.linalg.norm(blended))
    if norm == 0:  # antipodal centroids — no meaningful blend
        return {"n": n, "neighbors": []}
    query = blended / norm

    neighbors = embedding_service.get_neighbors_for_vector(
        query, n=n, exclude=seeds_a | seeds_b
    )
    return {"n": n, "neighbors": await _ranked_neighbors(neighbors, token)}
