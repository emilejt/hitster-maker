"""Game sessions — recording what a group actually played.

Deliberately **not** lobby-scoped past creation. A session needs a lobby to know who's playing
(the server reads their Spotify ids out of the lobby rather than trusting the client), but once
it exists, recording plays must keep working even if the lobby has expired — lobbies live an
hour and a long game night outlasts them.

Plays are recorded card by card as the game deals past them, not batched at the end: games get
abandoned by someone closing a laptop at least as often as by pressing "End game", and a
history that only survives a clean exit isn't much of a history.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.core.logging import get_logger
from app.services import game_store, lobby_store
from app.services.lobby_store import UnknownLobbyError

router = APIRouter()
log = get_logger("routers.session")


class CreateSessionRequest(BaseModel):
    lobby_id: str


class PlayedSongRequest(BaseModel):
    spotify_id: str
    isrc: str | None = None
    name: str | None = None
    artist: str | None = None
    year: int | None = None


@router.post("")
async def create_session(body: CreateSessionRequest):
    """Open a session for everyone in the lobby who has a Spotify identity.

    Called at Start Game and again on Play again — two games in one evening are two sessions,
    which is what makes the lobby's per-session checklist readable."""
    try:
        lobby = lobby_store.get_lobby_players(body.lobby_id)
    except UnknownLobbyError:
        raise HTTPException(status_code=404, detail="Unknown lobby")

    players = [
        (p.spotify_user_id, p.username) for p in lobby if p.spotify_user_id
    ]
    if not players:
        raise HTTPException(
            status_code=400, detail="No connected players with a Spotify identity"
        )
    return {"session_id": game_store.create_session(players)}


@router.post("/{session_id}/played")
async def record_played(session_id: str, body: PlayedSongRequest):
    """Record one song as played. Idempotent, so the client can fire and forget."""
    game_store.record_play(session_id, body.model_dump())
    return {"ok": True}


@router.post("/{session_id}/end")
async def end_session(session_id: str):
    """Mark a session finished. Informational only — an abandoned session keeps its plays."""
    game_store.end_session(session_id)
    return {"ok": True}
