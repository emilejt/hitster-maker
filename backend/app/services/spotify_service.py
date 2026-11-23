import asyncio
import base64
from urllib.parse import urlencode

import httpx

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger("services.spotify")

_TIMEOUT = httpx.Timeout(10.0)
_MAX_RETRIES = 1
# Never block a request on a 429 for longer than this. When Spotify's Retry-After is larger
# (it hands back long cooldowns once you've been rate-limited hard, e.g. during bulk neighbour
# resolution) we fail fast instead — the caller falls back to the cache / MusicBrainz name
# rather than the whole request hanging for minutes. Bounds one request to ~2·(timeout+sleep).
_MAX_RETRY_SLEEP = 5


class SpotifyRateLimited(httpx.HTTPStatusError):
    """A 429 we chose not to wait out — either Spotify's Retry-After exceeds `_MAX_RETRY_SLEEP`
    or we already spent our one retry. Subclasses `HTTPStatusError` so existing `except
    httpx.HTTPStatusError` handlers still treat it as a plain 429, but batch callers can catch
    it specifically to **stop calling Spotify for the rest of the run** instead of firing a
    fresh (doomed) request per remaining item and dragging the cooldown out."""

    def __init__(self, response: httpx.Response):
        self.retry_after = int(response.headers.get("Retry-After", "0"))
        super().__init__(
            f"spotify rate limited (Retry-After {self.retry_after}s)",
            request=response.request,
            response=response,
        )


async def _request(method: str, url: str, **kwargs) -> httpx.Response:
    """
    Thin wrapper around httpx that adds:
      - 10s timeout on every request
      - one retry on transient 5xx errors (short fixed delay)
      - one retry on 429, but only if Spotify's Retry-After is short (else fail fast — see
        `_MAX_RETRY_SLEEP`; we don't sleep for minutes). A 429 we give up on raises
        `SpotifyRateLimited` so batch callers can stop hammering the endpoint.
    """
    kwargs.setdefault("timeout", _TIMEOUT)
    for attempt in range(_MAX_RETRIES + 1):
        async with httpx.AsyncClient() as client:
            response = await getattr(client, method)(url, **kwargs)

        if response.status_code == 429:
            retry_after = int(response.headers.get("Retry-After", "2"))
            if attempt < _MAX_RETRIES and retry_after <= _MAX_RETRY_SLEEP:
                await asyncio.sleep(retry_after)
                continue
            # Long cooldown or retry already spent — don't hang, and signal the caller to stop
            # making further Spotify calls rather than 429-looping through the rest of a batch.
            log.warning("spotify 429: Retry-After %ss, not retrying (failing fast)", retry_after)
            raise SpotifyRateLimited(response)

        if response.status_code >= 500 and attempt < _MAX_RETRIES:
            await asyncio.sleep(1)
            continue

        response.raise_for_status()
        return response

    raise RuntimeError("unreachable")


SPOTIFY_AUTH_URL = "https://accounts.spotify.com/authorize"
SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"
SPOTIFY_API_BASE = "https://api.spotify.com/v1"

SCOPES = " ".join(
    [
        "user-top-read",
        "user-read-private",
        "user-library-read",
        "playlist-read-private",
        "playlist-read-collaborative",
        # In-game playback via the Web Playback SDK: `streaming` creates the browser device,
        # and the modify/read scopes let us start/pause a track on it. Requires Premium.
        "streaming",
        "user-modify-playback-state",
        "user-read-playback-state",
    ]
)


def get_auth_url(state: str) -> str:
    params = urlencode(
        {
            "response_type": "code",
            "client_id": settings.spotify_client_id,
            "scope": SCOPES,
            "redirect_uri": settings.spotify_redirect_uri,
            "state": state,
            # Always show the approval screen instead of silently auto-redirecting an
            # already-logged-in account — lets players log out and connect a different
            # Spotify account on the same browser (needed for pass-the-laptop play).
            "show_dialog": "true",
        }
    )
    return f"{SPOTIFY_AUTH_URL}?{params}"


def _auth_header() -> str:
    credentials = base64.b64encode(
        f"{settings.spotify_client_id}:{settings.spotify_client_secret}".encode()
    ).decode()
    return f"Basic {credentials}"


async def exchange_code(code: str) -> dict:
    response = await _request(
        "post",
        SPOTIFY_TOKEN_URL,
        headers={
            "Authorization": _auth_header(),
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": settings.spotify_redirect_uri,
        },
    )
    return response.json()


async def get_user_profile(access_token: str) -> dict:
    response = await _request(
        "get",
        f"{SPOTIFY_API_BASE}/me",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    return response.json()


async def get_top_tracks(
    access_token: str,
    time_range: str = "medium_term",
    limit: int = 20,
    offset: int = 0,
) -> dict:
    response = await _request(
        "get",
        f"{SPOTIFY_API_BASE}/me/top/tracks",
        headers={"Authorization": f"Bearer {access_token}"},
        params={"time_range": time_range, "limit": limit, "offset": offset},
    )
    return response.json()


async def get_liked_tracks(access_token: str, limit: int = 20, offset: int = 0) -> dict:
    response = await _request(
        "get",
        f"{SPOTIFY_API_BASE}/me/tracks",
        headers={"Authorization": f"Bearer {access_token}"},
        params={"limit": limit, "offset": offset},
    )
    return response.json()


async def get_user_playlists(access_token: str, limit: int = 50, offset: int = 0) -> dict:
    response = await _request(
        "get",
        f"{SPOTIFY_API_BASE}/me/playlists",
        headers={"Authorization": f"Bearer {access_token}"},
        params={"limit": limit, "offset": offset},
    )
    return response.json()


async def get_owned_playlists(access_token: str, limit: int = 50, offset: int = 0) -> dict:
    """Only playlists the current user owns. Spotify 403s when fetching the tracks of
    playlists the user doesn't own (editorial, algorithmic, other users', artist
    compilations), so we filter `/me/playlists` down to those where `owner.id` matches the
    current user — no point offering ones we can't read later."""
    profile, playlists = await asyncio.gather(
        get_user_profile(access_token),
        get_user_playlists(access_token, limit=limit, offset=offset),
    )
    user_id = profile.get("id")
    items = [
        p
        for p in playlists.get("items", [])
        if p and (p.get("owner") or {}).get("id") == user_id
    ]
    return {**playlists, "items": items}


async def get_playlist_tracks(
    access_token: str, playlist_id: str, limit: int = 50, offset: int = 0
) -> dict:
    response = await _request(
        "get",
        f"{SPOTIFY_API_BASE}/playlists/{playlist_id}/items",
        headers={"Authorization": f"Bearer {access_token}"},
        params={"limit": limit, "offset": offset},
    )
    return response.json()


async def get_playlist_snapshot_id(access_token: str, playlist_id: str) -> str | None:
    """The playlist's current `snapshot_id` — Spotify's own change token, which changes
    whenever the playlist is edited. One cheap call (`fields=snapshot_id`) that tells us
    whether a cached copy of the playlist's tracks is still exact, instead of re-paginating
    every page to find out."""
    response = await _request(
        "get",
        f"{SPOTIFY_API_BASE}/playlists/{playlist_id}",
        headers={"Authorization": f"Bearer {access_token}"},
        params={"fields": "snapshot_id"},
    )
    return response.json().get("snapshot_id")


async def get_all_liked_tracks(access_token: str, count: int) -> list[dict]:
    """Paginate /me/tracks up to `count` items. Returns raw saved-track items
    (each has `added_at` + `track`). Stops early if the library is smaller than `count`."""
    items: list[dict] = []
    offset = 0
    while len(items) < count:
        limit = min(50, count - len(items))
        resp = await get_liked_tracks(access_token, limit=limit, offset=offset)
        batch = resp.get("items", [])
        if not batch:
            break
        items.extend(batch)
        offset += len(batch)
        if len(batch) < limit:
            break  # reached the end of the saved library
    return items[:count]


async def get_all_playlist_tracks(access_token: str, playlist_id: str) -> list[dict]:
    """Paginate all items of a playlist. Returns raw items (each has `added_at` + `track`)."""
    items: list[dict] = []
    offset = 0
    while True:
        resp = await get_playlist_tracks(access_token, playlist_id, limit=50, offset=offset)
        batch = resp.get("items", [])
        if not batch:
            break
        items.extend(batch)
        offset += len(batch)
        if len(batch) < 50:
            break
    return items


async def search_by_isrc(access_token: str, isrc: str) -> dict | None:
    """Returns the first Spotify track matching the ISRC, or None if not found."""
    response = await _request(
        "get",
        f"{SPOTIFY_API_BASE}/search",
        headers={"Authorization": f"Bearer {access_token}"},
        params={"q": f"isrc:{isrc}", "type": "track", "limit": 1},
    )
    items = response.json().get("tracks", {}).get("items", [])
    return items[0] if items else None


async def search_by_track_artist(
    access_token: str, track: str, artist: str | None
) -> dict | None:
    """Returns the first Spotify track matching a `track:… artist:…` text search, or None.

    Used as the fallback when a recording has no ISRC (or its ISRC isn't on Spotify) but we
    do know its title/artist from MusicBrainz."""
    query = f"track:{track}"
    if artist:
        query += f" artist:{artist}"
    response = await _request(
        "get",
        f"{SPOTIFY_API_BASE}/search",
        headers={"Authorization": f"Bearer {access_token}"},
        params={"q": query, "type": "track", "limit": 1},
    )
    items = response.json().get("tracks", {}).get("items", [])
    return items[0] if items else None


async def get_track(access_token: str, track_id: str) -> dict:
    """Fetch one track by its Spotify id — used to rehydrate a cached row that's missing its
    release year (we already have the id, so this can't miss)."""
    response = await _request(
        "get",
        f"{SPOTIFY_API_BASE}/tracks/{track_id}",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    return response.json()
