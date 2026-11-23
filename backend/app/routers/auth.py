"""Spotify OAuth (Authorization Code flow), one lobby player at a time."""

import secrets
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import RedirectResponse

from app.core.config import settings
from app.services import spotify_service

router = APIRouter()

# CSRF `state` token → the lobby player_id it was issued for, so the callback can tell the
# frontend which player just connected. In memory: single process, local use.
_pending_states: dict[str, str | None] = {}


@router.get("/login")
async def spotify_login(player_id: str | None = None):
    """Start OAuth for one lobby player; `player_id` is echoed back on the callback."""
    state = secrets.token_urlsafe(16)
    _pending_states[state] = player_id
    return RedirectResponse(spotify_service.get_auth_url(state))


@router.get("/callback")
async def spotify_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
):
    """Exchange the code for a token and hand it to the Vue app's /spotify landing route."""
    frontend_spotify = f"{settings.frontend_url}/spotify"

    if error:
        if state is not None:
            _pending_states.pop(state, None)
        return RedirectResponse(f"{frontend_spotify}?" + urlencode({"error": error}))

    if not state or state not in _pending_states:
        raise HTTPException(status_code=400, detail="Invalid or missing state parameter")

    player_id = _pending_states.pop(state)

    if not code:
        raise HTTPException(status_code=400, detail="Missing authorization code")

    try:
        tokens = await spotify_service.exchange_code(code)
    except httpx.HTTPError:
        return RedirectResponse(
            f"{frontend_spotify}?" + urlencode({"error": "token_exchange_failed"})
        )

    params = {"access_token": tokens["access_token"]}
    if player_id:
        params["player_id"] = player_id
    return RedirectResponse(f"{frontend_spotify}?" + urlencode(params))
