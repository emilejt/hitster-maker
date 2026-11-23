"""Lobby endpoints — create a lobby and extract each player's library into it.

Extraction is two calls per player, on purpose:
  1. POST /lobby/{id}/player            — right after connect: fetch top tracks (all ranges),
                                           register the player, seed their library.
  2. POST /lobby/{id}/player/{pid}/sources — on Build profile: fetch the chosen playlists,
                                           assemble the library, cluster the player's taste.
Splitting them means top tracks are already in hand by the time the modal is submitted.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.core.logging import get_logger
from app.pipeline import clustering, deck, library, pool
from app.pipeline.models import PlayerLibrary, Source, SourceSelection
from app.services import game_store, lobby_store, spotify_service
from app.services.lobby_store import (
    LobbyPlayer,
    UnknownLobbyError,
    UnknownPlayerError,
)

router = APIRouter()
log = get_logger("routers.lobby")

_TOP_SOURCES = {Source.TOP_SHORT, Source.TOP_MEDIUM, Source.TOP_LONG}

# Sessions newer than this are pre-ticked in the lobby's exclusion list. Purely a UI default:
# what actually gets excluded is whatever the frontend sends back.
_RECENT_WINDOW_DAYS = 30
_HISTORY_LIMIT = 20  # most recent sessions offered in the list


def _lobby_user_ids(lobby_id: str) -> list[str]:
    """Spotify account ids of everyone in the lobby — the key play history is stored under."""
    return [
        p.spotify_user_id
        for p in lobby_store.get_lobby_players(lobby_id)
        if p.spotify_user_id
    ]


class AddPlayerRequest(BaseModel):
    player_id: str
    username: str
    access_token: str


class SourcesRequest(BaseModel):
    liked_count: int = 0
    playlist_ids: list[str] = []
    # False = drop the eagerly-fetched top tracks and build from the chosen sources only.
    include_top_tracks: bool = True


class PoolRequest(BaseModel):
    """Optional body for Start Game / Play again.

    `exclude_spotify_ids` / `seed_spotify_ids` are the *in-game* Play-again memory: songs
    already played this sitting, and the prior deck's unplayed songs to carry over before
    topping the deck back up.

    `exclude_session_ids` is the *cross-session* memory — past games the players ticked in the
    lobby's history list. The frontend sends exactly which sessions to skip, so the default
    time window only decides what's pre-checked, never what's enforced."""

    exclude_spotify_ids: list[str] = []
    seed_spotify_ids: list[str] = []
    exclude_session_ids: list[str] = []


def _summary(lib: PlayerLibrary) -> dict:
    """Lightweight per-player extraction summary — the library itself stays server-side."""
    by_source: dict[str, int] = {}
    isrcs: set[str] = set()
    for t in lib.tracks:
        by_source[str(t.source)] = by_source.get(str(t.source), 0) + 1
        if t.isrc:
            isrcs.add(t.isrc)
    return {
        "player_id": lib.player_id,
        "username": lib.username,
        "track_count": len(lib.tracks),
        "unique_isrcs": len(isrcs),
        "by_source": by_source,
    }


@router.post("")
async def create_lobby():
    return {"lobby_id": lobby_store.create_lobby()}


@router.post("/{lobby_id}/player")
async def add_player(lobby_id: str, body: AddPlayerRequest):
    """Call 1: register the player and seed their library with top tracks.

    The Spotify profile is fetched first because their account id is the extraction cache's
    key — with it, ranges still inside their TTL cost no Spotify calls at all."""
    try:
        profile = await spotify_service.get_user_profile(body.access_token)
        spotify_user_id = profile.get("id")
        top_tracks = await library.fetch_top_tracks(body.access_token, spotify_user_id)
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=e.response.status_code, detail=e.response.text)

    player = LobbyPlayer(
        player_id=body.player_id,
        username=body.username,
        access_token=body.access_token,
        sources=SourceSelection(),
        spotify_user_id=spotify_user_id,
        library=PlayerLibrary(
            player_id=body.player_id, username=body.username, tracks=top_tracks
        ),
    )
    try:
        lobby_store.add_or_update_player(lobby_id, player)
    except UnknownLobbyError:
        raise HTTPException(status_code=404, detail="Unknown lobby")
    assert player.library is not None
    return _summary(player.library)


@router.post("/{lobby_id}/player/{player_id}/sources")
async def add_sources(lobby_id: str, player_id: str, body: SourcesRequest):
    """Call 2: fetch the chosen sources, assemble the final library, cluster it."""
    try:
        player = lobby_store.get_player(lobby_id, player_id)
    except UnknownLobbyError:
        raise HTTPException(status_code=404, detail="Unknown lobby")
    except UnknownPlayerError:
        raise HTTPException(status_code=404, detail="Unknown player")

    sources = SourceSelection(
        liked_count=body.liked_count,
        playlist_ids=body.playlist_ids,
        include_top_tracks=body.include_top_tracks,
    )
    try:
        tracks = await library.fetch_source_selection(player.access_token, sources)
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=e.response.status_code, detail=e.response.text)

    # Merge with the top tracks from call 1 and dedupe across both batches: a song that's
    # both a top track and a liked/playlist track collapses to its strongest occurrence.
    # If the player unticked top songs, they're dropped here rather than never fetched — the
    # eager fetch is what keeps Build profile fast, and it's cached for next time regardless.
    # Filtering by source (not just taking everything already stored) keeps this correct if
    # the profile is rebuilt on a library that already holds playlist tracks.
    existing = (
        [t for t in (player.library.tracks if player.library else []) if t.source in _TOP_SOURCES]
        if body.include_top_tracks
        else []
    )
    merged = library.dedupe_tracks(existing + tracks)
    if not merged:
        raise HTTPException(
            status_code=400,
            detail="A profile needs at least one source — pick some playlists or keep top songs.",
        )
    merged_library = PlayerLibrary(
        player_id=player_id, username=player.username, tracks=merged
    )
    lobby_store.set_sources(lobby_id, player_id, sources)
    lobby_store.store_library(lobby_id, player_id, merged_library)

    # Cluster this player's taste now (Build profile) so Start Game doesn't pay for it. The
    # clustering is CPU-bound numpy work, so run it off the event loop.
    clusters = await asyncio.to_thread(clustering.build_player_clusters, merged_library)
    lobby_store.store_player_clusters(lobby_id, player_id, clusters)

    return _summary(merged_library)


@router.post("/{lobby_id}/pool")
async def build_pool(lobby_id: str, body: PoolRequest | None = None):
    """Start Game / Play again: build the group pool from the lobby's stored libraries.

    Deck size is dynamic (SOFT_SIZE_PER_PLAYER × players). The optional body carries "Play again"
    memory — songs to exclude (already played) and unplayed songs to seed the new deck with."""
    try:
        libraries = lobby_store.get_libraries(lobby_id)
    except UnknownLobbyError:
        raise HTTPException(status_code=404, detail="Unknown lobby")

    if len(libraries) < 2:
        raise HTTPException(
            status_code=400, detail="Need at least 2 players with extracted libraries"
        )

    clusters_by_player = lobby_store.get_all_player_clusters(lobby_id)
    token = lobby_store.get_any_token(lobby_id)
    body = body or PoolRequest()

    # Past sessions the players chose to skip, as excludable units. Kept as groups (not one
    # flat set) so the builder can relax oldest-first if the deck comes out short.
    exclusion_groups: list[deck.ExclusionGroup] = []
    if body.exclude_session_ids:
        played = await asyncio.to_thread(
            game_store.get_played_by_session, body.exclude_session_ids
        )
        started = {
            s["session_id"]: s["started_at"]
            for s in await asyncio.to_thread(
                game_store.get_sessions_for_users, _lobby_user_ids(lobby_id), _HISTORY_LIMIT
            )
        }
        exclusion_groups = [
            deck.ExclusionGroup(
                session_id=sid,
                started_at=started.get(sid, 0.0),
                spotify_ids=entry["spotify_ids"],
                isrcs=entry["isrcs"],
            )
            for sid, entry in played.items()
        ]

    try:
        return await pool.build_group_pool(
            libraries,
            clusters_by_player,
            resolve_token=token,
            exclude_spotify_ids=set(body.exclude_spotify_ids),
            seed_spotify_ids=set(body.seed_spotify_ids),
            exclusion_groups=exclusion_groups,
        )
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=e.response.status_code, detail=e.response.text)


@router.get("/{lobby_id}/history")
async def get_history(lobby_id: str):
    """Past sessions any of this lobby's players took part in, newest first.

    Drives the lobby's exclusion checklist. `recent` marks the sessions inside
    `_RECENT_WINDOW_DAYS` — those are pre-ticked, because after a couple of months nobody
    remembers what they heard. The host is free to tick or untick anything; the backend
    enforces only what it's actually sent."""
    try:
        user_ids = _lobby_user_ids(lobby_id)
    except UnknownLobbyError:
        raise HTTPException(status_code=404, detail="Unknown lobby")

    sessions = await asyncio.to_thread(
        game_store.get_sessions_for_users, user_ids, _HISTORY_LIMIT
    )
    cutoff = time.time() - _RECENT_WINDOW_DAYS * 86400
    return {
        "window_days": _RECENT_WINDOW_DAYS,
        "sessions": [
            {
                "session_id": s["session_id"],
                "started_at": datetime.fromtimestamp(
                    s["started_at"], tz=timezone.utc
                ).isoformat(),
                "players": s["players"],
                "song_count": s["song_count"],
                "recent": s["started_at"] >= cutoff,
            }
            for s in sessions
            # A session nobody finished a card in is noise in the list.
            if s["song_count"] > 0
        ],
    }


@router.get("/{lobby_id}")
async def get_lobby(lobby_id: str):
    """Per-player extraction summaries for the lobby (handy for debugging the flow)."""
    try:
        libs = lobby_store.get_libraries(lobby_id)
    except UnknownLobbyError:
        raise HTTPException(status_code=404, detail="Unknown lobby")
    return {"players": [_summary(lib) for lib in libs]}
