"""In-memory lobby sessions — the backend's first piece of server-side state.

A lobby groups the players of one game. As each player connects (one at a time), the
backend fetches their library and stores it here alongside their token and source
choices; on Start Game the pool is built from the stored libraries.

Deliberately ephemeral (a module-level dict): wiped on restart, single-process only. What
must outlive a lobby (extraction cache, play history) lives in `game_store`. `pipeline/`
never imports this: routers read from the store and pass plain data into the pipeline.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field

from app.core.logging import get_logger
from app.pipeline.models import PlayerClusters, PlayerLibrary, SourceSelection

log = get_logger("lobby_store")

# Idle lobbies are dropped after this long. Bounded anyway by ~1h Spotify token expiry.
LOBBY_TTL_SECONDS = 60 * 60


@dataclass
class LobbyPlayer:
    player_id: str
    username: str
    access_token: str
    sources: SourceSelection
    # The player's real Spotify account id. `player_id` is a throwaway lobby slot ("p0") and
    # `username` is typed by hand, so this is the only stable identity we have — it's the key
    # the extraction cache and (later) play history are stored under. See services/game_store.
    spotify_user_id: str | None = None
    library: PlayerLibrary | None = None
    clusters: PlayerClusters | None = None  # computed + stored at Build-Profile time


@dataclass
class Lobby:
    lobby_id: str
    players: dict[str, LobbyPlayer] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    last_active: float = field(default_factory=time.time)


_lobbies: dict[str, Lobby] = {}


class UnknownLobbyError(KeyError):
    """Raised when a lobby_id isn't in the store (expired, evicted, or never existed)."""


class UnknownPlayerError(KeyError):
    """Raised when a player_id isn't in the given lobby."""


def create_lobby() -> str:
    """Create a fresh lobby and return its id."""
    _evict_expired()
    lobby_id = secrets.token_urlsafe(9)
    _lobbies[lobby_id] = Lobby(lobby_id=lobby_id)
    log.info("created lobby %s", lobby_id)
    return lobby_id


def _get(lobby_id: str) -> Lobby:
    lobby = _lobbies.get(lobby_id)
    if lobby is None:
        raise UnknownLobbyError(lobby_id)
    lobby.last_active = time.time()
    return lobby


def add_or_update_player(lobby_id: str, player: LobbyPlayer) -> None:
    """Register (or replace) a player in the lobby, before their library is extracted."""
    lobby = _get(lobby_id)
    lobby.players[player.player_id] = player
    log.info("lobby %s: registered player %s (%s)", lobby_id, player.player_id, player.username)


def get_player(lobby_id: str, player_id: str) -> LobbyPlayer:
    """Fetch a registered player (for their token / current library)."""
    player = _get(lobby_id).players.get(player_id)
    if player is None:
        raise UnknownPlayerError(player_id)
    return player


def store_library(lobby_id: str, player_id: str, library: PlayerLibrary) -> None:
    """Attach (replacing) an extracted library to an already-registered player."""
    get_player(lobby_id, player_id).library = library
    log.info("lobby %s: stored library for %s (%d tracks)", lobby_id, player_id, len(library.tracks))


def set_sources(lobby_id: str, player_id: str, sources: SourceSelection) -> None:
    """Record a player's Profile Maker source choices."""
    get_player(lobby_id, player_id).sources = sources


def store_player_clusters(lobby_id: str, player_id: str, clusters: PlayerClusters) -> None:
    """Persist a player's taste clusters, computed once at Build-Profile time."""
    get_player(lobby_id, player_id).clusters = clusters
    log.info(
        "lobby %s: stored clusters for %s (%d clusters, %d unclustered)",
        lobby_id, player_id, len(clusters.clusters), len(clusters.unclustered_mbids),
    )


def get_all_player_clusters(lobby_id: str) -> dict[str, PlayerClusters]:
    """Every player's persisted clusters (players without any are skipped)."""
    lobby = _get(lobby_id)
    return {
        pid: p.clusters
        for pid, p in lobby.players.items()
        if p.clusters is not None
    }


def get_lobby_players(lobby_id: str) -> list[LobbyPlayer]:
    """Every registered player in the lobby — used to open a game session with their real
    Spotify identities (read server-side rather than trusted from the client)."""
    return list(_get(lobby_id).players.values())


def get_libraries(lobby_id: str) -> list[PlayerLibrary]:
    """All extracted libraries in the lobby (players without one yet are skipped)."""
    lobby = _get(lobby_id)
    return [p.library for p in lobby.players.values() if p.library is not None]


def get_any_token(lobby_id: str) -> str | None:
    """Any connected player's access token — used to resolve tier-3 songs on Spotify."""
    lobby = _get(lobby_id)
    for p in lobby.players.values():
        if p.access_token:
            return p.access_token
    return None


def _evict_expired() -> None:
    now = time.time()
    for lobby_id in [lid for lid, lb in _lobbies.items() if now - lb.last_active > LOBBY_TTL_SECONDS]:
        del _lobbies[lobby_id]
        log.info("evicted idle lobby %s", lobby_id)
