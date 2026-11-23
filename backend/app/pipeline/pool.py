"""Orchestrator — build the multi-player game deck from extracted libraries + clusters.

Thin wrapper over `pipeline/deck.py` (the greedy, tiered builder). The router reads each
player's stored library + persisted clusters out of the lobby store and hands them in; this
formats the resulting deck for the Game view.

Pure: plain data in, plain dict out.
"""

from __future__ import annotations

from app.core.logging import get_logger
from app.pipeline import deck
from app.pipeline.deck import Candidate
from app.pipeline.models import PlayerClusters, PlayerLibrary

log = get_logger("pipeline.pool")


def _format_song(c: Candidate) -> dict:
    t = c.track
    return {
        "isrc": t.isrc,
        "spotify_id": t.spotify_id,
        "spotify_uri": t.spotify_uri,
        "name": t.name,
        "artist": t.artist,
        "year": t.year,
        "album_art": t.album_art,
        "owners": list(c.owners.keys()),
        "tier": c.tier,
        "from_fallback": c.tier == 3,
    }


async def build_group_pool(
    libraries: list[PlayerLibrary],
    clusters_by_player: dict[str, PlayerClusters],
    resolve_token: str | None = None,
    exclude_spotify_ids: set[str] | None = None,
    seed_spotify_ids: set[str] | None = None,
    exclusion_groups: list[deck.ExclusionGroup] | None = None,
) -> dict:
    cards, diagnostics = await deck.build_deck(
        libraries,
        clusters_by_player,
        resolve_token,
        exclude_spotify_ids=exclude_spotify_ids,
        seed_spotify_ids=seed_spotify_ids,
        exclusion_groups=exclusion_groups,
    )
    return {
        "pool": [_format_song(c) for c in cards],
        "diagnostics": diagnostics,
    }
