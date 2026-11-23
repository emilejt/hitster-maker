"""Taste-analysis endpoints: the HTTP surface of the /analysis tuning tool (dev-facing).

Thin layer over `pipeline/taste.py`: read the player's stored library out of the lobby
store, run the pure report, map domain errors to HTTP.

Endpoints: 1-player **clusters** + **neighbors**; 2-player **proximity** + **combine** +
**nearest-seeds**.
"""

from __future__ import annotations

from typing import Literal

import httpx
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.core.logging import get_logger
from app.pipeline import taste
from app.services import lobby_store
from app.services.lobby_store import UnknownLobbyError, UnknownPlayerError

router = APIRouter()
log = get_logger("routers.analysis")

# k range shown in the UI; analyse_taste_clusters further clamps to the seed count.
_MIN_K = 1
_MAX_K = 6
# Neighbours per centroid. Uncached neighbours cost one Spotify search each, so cap it.
_MAX_N = 200
# Clustering method: "kmeans" uses k; "agglomerative" uses a single global cosine-distance cut.
Method = Literal["kmeans", "agglomerative"]
# Cosine distance ∈ [0, 2] on unit vectors; the tuning slider lives well inside that.
_MIN_DIST = 0.0
_MAX_DIST = 2.0
# Agglomerative only: drop clusters with fewer than this many songs (weak taste signal).
_MIN_CLUSTER_SIZE = 1


def _require_player_with_library(lobby_id: str, player_id: str):
    """Fetch a connected player that has an extracted library, or raise the right HTTP error."""
    try:
        player = lobby_store.get_player(lobby_id, player_id)
    except UnknownLobbyError:
        raise HTTPException(status_code=404, detail="Unknown lobby")
    except UnknownPlayerError:
        raise HTTPException(status_code=404, detail="Unknown player")
    if player.library is None:
        raise HTTPException(status_code=409, detail="Player has no extracted library yet")
    return player


@router.get("/{lobby_id}/{player_id}/clusters")
async def clusters(
    lobby_id: str,
    player_id: str,
    k: int = Query(3, ge=_MIN_K, le=_MAX_K),
    method: Method = Query("kmeans"),
    distance: float = Query(0.5, ge=_MIN_DIST, le=_MAX_DIST),
    min_cluster_size: int = Query(_MIN_CLUSTER_SIZE, ge=1),
):
    """Cluster one connected player's in-vocab seed songs. `method=kmeans` splits into `k`
    zones; `method=agglomerative` merges under a single global cosine-`distance` cut, dropping
    clusters smaller than `min_cluster_size`."""
    player = _require_player_with_library(lobby_id, player_id)
    return taste.cluster_report(player.library, k, method, distance, min_cluster_size)


@router.get("/{lobby_id}/{player_id}/neighbors")
async def neighbors(
    lobby_id: str,
    player_id: str,
    k: int = Query(3, ge=_MIN_K, le=_MAX_K),
    n: int = Query(50, ge=1, le=_MAX_N),
    method: Method = Query("kmeans"),
    distance: float = Query(0.5, ge=_MIN_DIST, le=_MAX_DIST),
    min_cluster_size: int = Query(_MIN_CLUSTER_SIZE, ge=1),
):
    """The n popularity-ranked model neighbours each of the player's centroids generates,
    with names/years resolved (cache-first, then Spotify via the player's token)."""
    player = _require_player_with_library(lobby_id, player_id)
    try:
        return await taste.neighbors_report(
            player.library, k, n, player.access_token, method, distance, min_cluster_size
        )
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=e.response.status_code, detail=e.response.text)


@router.get("/{lobby_id}/proximity")
async def proximity(
    lobby_id: str,
    a: str,
    b: str,
    k_a: int = Query(3, ge=_MIN_K, le=_MAX_K),
    k_b: int = Query(3, ge=_MIN_K, le=_MAX_K),
    method: Method = Query("kmeans"),
    distance: float = Query(0.5, ge=_MIN_DIST, le=_MAX_DIST),
    min_cluster_size: int = Query(_MIN_CLUSTER_SIZE, ge=1),
):
    """Cross-cluster closeness between two connected players (the taste-overlap signal).
    In agglomerative mode both players share the one global `distance` cut + `min_cluster_size`."""
    player_a = _require_player_with_library(lobby_id, a)
    player_b = _require_player_with_library(lobby_id, b)
    return taste.proximity(
        player_a.library, k_a, player_b.library, k_b, method, distance, min_cluster_size
    )


class CombineRequest(BaseModel):
    a: str
    a_cluster: int = Field(ge=0)
    k_a: int = Field(3, ge=_MIN_K, le=_MAX_K)
    b: str
    b_cluster: int = Field(ge=0)
    k_b: int = Field(3, ge=_MIN_K, le=_MAX_K)
    n: int = Field(50, ge=1, le=_MAX_N)
    method: Method = "kmeans"
    distance: float = Field(0.5, ge=_MIN_DIST, le=_MAX_DIST)
    min_cluster_size: int = Field(_MIN_CLUSTER_SIZE, ge=1)


@router.post("/{lobby_id}/combine")
async def combine(lobby_id: str, body: CombineRequest):
    """Blend one cluster from each of two players and generate the shared-taste neighbours —
    the common-ground mechanic behind tier 3."""
    player_a = _require_player_with_library(lobby_id, body.a)
    player_b = _require_player_with_library(lobby_id, body.b)
    try:
        return await taste.combine_neighbors(
            player_a.library, body.a_cluster, body.k_a,
            player_b.library, body.b_cluster, body.k_b,
            body.n, player_a.access_token, body.method, body.distance,
            body.min_cluster_size,
        )
    except taste.ClusterIndexError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=e.response.status_code, detail=e.response.text)


class NearestSeedsRequest(BaseModel):
    base: str
    base_cluster: int = Field(ge=0)
    k_base: int = Field(3, ge=_MIN_K, le=_MAX_K)
    candidate: str
    candidate_cluster: int = Field(ge=0)
    k_candidate: int = Field(3, ge=_MIN_K, le=_MAX_K)
    n: int = Field(20, ge=1, le=_MAX_N)
    method: Method = "kmeans"
    distance: float = Field(0.5, ge=_MIN_DIST, le=_MAX_DIST)
    min_cluster_size: int = Field(_MIN_CLUSTER_SIZE, ge=1)


@router.post("/{lobby_id}/nearest-seeds")
def nearest_seeds(lobby_id: str, body: NearestSeedsRequest):
    """The candidate cluster's own library songs closest to the base cluster's centroid —
    concrete shared-taste songs. Metadata is free (library tracks), so no Spotify call."""
    base = _require_player_with_library(lobby_id, body.base)
    candidate = _require_player_with_library(lobby_id, body.candidate)
    try:
        return taste.nearest_seeds(
            base.library, body.base_cluster, body.k_base,
            candidate.library, body.candidate_cluster, body.k_candidate,
            body.n, body.method, body.distance, body.min_cluster_size,
        )
    except taste.ClusterIndexError as e:
        raise HTTPException(status_code=422, detail=str(e))
