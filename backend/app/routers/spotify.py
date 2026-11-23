"""Thin Spotify pass-throughs the frontend calls directly.

Only the two endpoints the new flow needs live here: the player's profile and their
playlist list (for the Profile Maker modal). All library fetching (top tracks, liked,
playlists' tracks) now happens inside `pipeline/library.py`, not as public endpoints.
"""

from typing import Annotated

import httpx
from fastapi import APIRouter, Header, HTTPException

from app.core.auth import extract_bearer_token
from app.services import spotify_service

router = APIRouter()


@router.get("/me")
async def get_profile(authorization: Annotated[str | None, Header()] = None):
    try:
        return await spotify_service.get_user_profile(extract_bearer_token(authorization))
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=e.response.status_code, detail=e.response.text)


@router.get("/playlists")
async def list_user_playlists(
    limit: int = 50,
    offset: int = 0,
    authorization: Annotated[str | None, Header()] = None,
):
    try:
        return await spotify_service.get_owned_playlists(
            extract_bearer_token(authorization), limit=limit, offset=offset
        )
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=e.response.status_code, detail=e.response.text)
