"""Per-player taste clustering, computed once at Build-Profile time.

Wraps the agglomerative (single global cosine-distance cut) clustering so a player's taste
zones are built **when they finish their profile**, not recomputed on every Start-Game pool
build. The result is persisted in the lobby store and later fed to the deck builder
(`pipeline/deck.py`) for the bridge (tier 2) and synthetic (tier 3) candidate tiers.

Takes a `PlayerLibrary`, returns a `PlayerClusters` (plain data — centroids are numpy arrays
kept in memory, matching the ephemeral lobby store). No FastAPI, no lobby store. As a side
effect it seeds the mbid→Spotify cache with each in-vocab owned song (see
`_hydrate_bridge_cache`), so later resolution never has to search Spotify for a song a player
already owns.
"""

from __future__ import annotations

from app.core.logging import get_logger
from app.pipeline import taste
from app.pipeline.models import ClusterInfo, PlayerClusters, PlayerLibrary, SourcedTrack
from app.services import bridge_service, embedding_service

log = get_logger("pipeline.clustering")

# The fixed clustering knobs for the game deck (agglomerative). A global cosine-distance cut
# plus a min-size floor that dismisses weak singletons/pairs — those songs are kept but land in
# no cluster. Tuned via the /analysis tool; change here to change the whole game's clustering.
CLUSTER_CUT = 0.7
CLUSTER_MIN_SIZE = 30


def _hydrate_bridge_cache(seeds: list[tuple[str, SourcedTrack]]) -> None:
    """Seed the mbid→Spotify cache from a player's own library.

    Each in-vocab owned song already carries the full Spotify metadata we'd otherwise pay a
    search for — id, uri, name, artist, album art, and the release **year** — so writing it into
    `mbid_spotify_bridge` (keyed by MBID) makes any later neighbour/synthetic resolve that lands
    on one of these songs a free cache hit. ~700 rows/player, zero Spotify calls.
    """
    entries = [
        {
            "mbid": mbid,
            "spotify_id": t.spotify_id,
            "isrc": t.isrc,
            "name": t.name,
            "artist": t.artist,
            "album_art": t.album_art,
            "year": t.year,
        }
        for mbid, t in seeds
        if t.spotify_id
    ]
    bridge_service.upsert_spotify_mappings(entries)
    log.info("hydrated spotify cache with %d owned songs", len(entries))


def build_player_clusters(library: PlayerLibrary) -> PlayerClusters:
    """Cluster one player's in-vocab library songs into persisted taste zones.

    Songs dismissed by the size cut (and any in-vocab song in no surviving cluster) are kept in
    `unclustered_mbids`. A player with no in-vocab seeds gets empty clusters + no unclustered.
    """
    seeds = taste.seed_tracks(library)  # [(mbid, SourcedTrack)], in-vocab, deduped, ordered
    mbids = [mbid for mbid, _ in seeds]

    _hydrate_bridge_cache(seeds)

    analysis = embedding_service.agglomerate_taste_clusters(
        mbids, CLUSTER_CUT, CLUSTER_MIN_SIZE
    )
    if not analysis or not analysis["clusters"]:
        return PlayerClusters(
            player_id=library.player_id, clusters=[], unclustered_mbids=mbids
        )

    clusters: list[ClusterInfo] = []
    clustered: set[str] = set()
    for index, cluster in enumerate(analysis["clusters"]):
        member_mbids = list(cluster["mbids"])
        clusters.append(
            ClusterInfo(index=index, centroid=cluster["centroid"], mbids=member_mbids)
        )
        clustered.update(member_mbids)

    unclustered = [mbid for mbid in mbids if mbid not in clustered]
    log.info(
        "clusters for %s: %d clusters over %d seeds, %d unclustered",
        library.player_id, len(clusters), len(mbids), len(unclustered),
    )
    return PlayerClusters(
        player_id=library.player_id, clusters=clusters, unclustered_mbids=unclustered
    )
