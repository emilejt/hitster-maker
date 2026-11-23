"""Shared MBID → playable Spotify track resolution, with full-hydration caching.

Both the fallback (building game cards) and the taste tool (labelling neighbours) turn model
MBIDs into real Spotify tracks. The game needs a release **year** and a playable **URI** for
every card, so this resolver treats the Spotify bridge cache as the source of truth and only
calls Spotify when the cache is missing or incomplete — the slow work happens once (e.g. while
tuning taste) and every later game is served from the cache.

Resolution ladder per MBID:

  1. **Complete cache hit** (has spotify id + year) → use it, no Spotify call.
  2. **Incomplete cache hit** (cached but no year) → *rehydrate*: fetch the known track by id
     (we already have the id, so this fills the year and can't miss).
  3. **Cache miss** → look up MusicBrainz meta (isrc, name, artist), then
       a. if it has an ISRC: Spotify ISRC search; on a miss, fall through to (b),
       b. Spotify text search `track:<title> artist:<artist>` (title lightly de-qualified so
          "… - Remastered 2011" style names still match a real track).

Every fresh resolution is written back to the cache **with year**, so it's complete next time.
Entries here are dicts: `{spotify_id, isrc, name, artist, album_art, year, spotify_uri}` (+
`mbid` on the cache-write entries). URI is always `spotify:track:<id>`.
"""

from __future__ import annotations

import re

import httpx

from app.core.logging import get_logger
from app.pipeline import library
from app.pipeline.models import SourcedTrack, Source
from app.services import bridge_service, spotify_service

log = get_logger("pipeline.track_resolution")

# Trailing qualifiers MusicBrainz recording titles carry that break an exact Spotify text
# search (kept in what we *store*/display — only stripped from the search query).
_QUALIFIERS = (
    r"remaster(ed)?|live|demo|mono|stereo|re-?mix|mix|version|edit|acoustic|anniversary|"
    r"deluxe|bonus|instrumental|radio|take \d+|session|single version|album version"
)
_PAREN_QUALIFIER = re.compile(rf"\s*[\(\[][^)\]]*(?:{_QUALIFIERS})[^)\]]*[\)\]]", re.I)
_DASH_QUALIFIER = re.compile(rf"\s+-\s+[^-]*(?:{_QUALIFIERS}).*$", re.I)


def _clean_title(name: str) -> str:
    """Drop remaster/live/demo-style qualifiers from a title for the Spotify text search."""
    cleaned = _PAREN_QUALIFIER.sub("", name)
    cleaned = _DASH_QUALIFIER.sub("", cleaned)
    return cleaned.strip() or name


def _track_isrc(track: dict) -> str | None:
    return (track.get("external_ids") or {}).get("isrc")


def _entry_from_track(track: dict, mbid: str, isrc_hint: str | None) -> dict:
    """Normalize a raw Spotify track into a cache/resolution entry (with year)."""
    images = (track.get("album") or {}).get("images") or [{}]
    return {
        "mbid": mbid,
        "spotify_id": track["id"],
        "isrc": _track_isrc(track) or isrc_hint,
        "name": track.get("name"),
        "artist": track["artists"][0]["name"] if track.get("artists") else None,
        "album_art": images[0].get("url") if images else None,
        "year": library._parse_year((track.get("album") or {}).get("release_date")),
    }


def _with_uri(entry: dict) -> dict:
    """Return the entry with a derived `spotify_uri` (URI is deterministic from the id)."""
    return {**entry, "spotify_uri": f"spotify:track:{entry['spotify_id']}"}


def _is_complete(entry: dict | None) -> bool:
    """A cache entry the game can use as-is: has a Spotify id and a release year."""
    return bool(entry and entry.get("spotify_id") and entry.get("year") is not None)


async def _call(coro):
    """Await a Spotify call, turning *any* HTTP error — status, timeout, connect — into None so
    one flaky track is skipped instead of crashing the whole batch. A `SpotifyRateLimited`
    (429 we won't wait out) is deliberately **not** swallowed: it propagates so `resolve_batch`
    can stop making Spotify calls entirely rather than 429-looping through the rest of the run."""
    try:
        return await coro
    except spotify_service.SpotifyRateLimited:
        raise
    except httpx.HTTPStatusError as e:
        log.warning("track_resolution: spotify %s", e.response.status_code)
        return None
    except httpx.HTTPError as e:
        log.warning("track_resolution: spotify call errored: %r", e)
        return None


def prefetch(mbids: list[str]) -> tuple[dict[str, dict], dict[str, dict]]:
    """Batch the two cheap DB reads (Spotify cache + MusicBrainz meta) the ladder needs."""
    return (
        bridge_service.get_cached_tracks_for_mbids(mbids),
        bridge_service.get_meta_for_mbids(mbids),
    )


def commit(new_mappings: list[dict]) -> None:
    """Persist freshly resolved entries back to the Spotify bridge cache."""
    bridge_service.upsert_spotify_mappings(new_mappings)


async def resolve_one(
    mbid: str,
    token: str,
    cache_entry: dict | None,
    meta_entry: dict | None,
    new_mappings: list[dict],
) -> dict | None:
    """Resolve one MBID to a fully-hydrated entry (see module docstring for the ladder).

    `cache_entry`/`meta_entry` are this MBID's rows from `prefetch` (may be None). Anything
    freshly fetched from Spotify is appended to `new_mappings` for the caller to `commit`.
    Returns the entry (with `spotify_uri`) or None if Spotify couldn't find the track."""
    if _is_complete(cache_entry):
        return _with_uri(cache_entry)  # type: ignore[arg-type]

    # Incomplete cache hit (we have the id, just no year) → rehydrate by id.
    if cache_entry and cache_entry.get("spotify_id"):
        track = await _call(spotify_service.get_track(token, cache_entry["spotify_id"]))
        if track:
            entry = _entry_from_track(track, mbid, cache_entry.get("isrc"))
            new_mappings.append(entry)
            return _with_uri(entry)
        # rehydrate failed (deleted track?) — fall through to a fresh search

    meta = meta_entry or {}
    isrc, name, artist = meta.get("isrc"), meta.get("name"), meta.get("artist")

    track = None
    if isrc:
        track = await _call(spotify_service.search_by_isrc(token, isrc))
    if track is None and name:  # no isrc, or Spotify didn't know that isrc
        track = await _call(
            spotify_service.search_by_track_artist(token, _clean_title(name), artist)
        )
    if track is None:
        return None

    entry = _entry_from_track(track, mbid, isrc)
    new_mappings.append(entry)
    return _with_uri(entry)


async def resolve_batch(mbids: list[str], token: str | None) -> dict[str, dict]:
    """Resolve every MBID that can be, filling the cache. Returns `{mbid: entry}` for the ones
    that resolved to a Spotify track (with year + uri); unresolved MBIDs are absent. With no
    token, returns only complete cache hits (no Spotify calls)."""
    unique = list(dict.fromkeys(mbids))
    if not unique:
        return {}
    cache, meta = prefetch(unique)
    resolved: dict[str, dict] = {}
    new_mappings: list[dict] = []
    for i, mbid in enumerate(unique):
        entry = cache.get(mbid)
        if _is_complete(entry):
            resolved[mbid] = _with_uri(entry)  # type: ignore[arg-type]
            continue
        if not token:
            continue
        try:
            got = await resolve_one(mbid, token, entry, meta.get(mbid), new_mappings)
        except spotify_service.SpotifyRateLimited as e:
            # Hit the rate limit once — stop calling Spotify for the rest of this batch instead
            # of firing a fresh 429 per remaining mbid. Complete cache hits still resolve below.
            log.warning(
                "track_resolution: rate-limited (Retry-After %ss); serving cache only for the "
                "remaining %d mbids",
                e.retry_after,
                len(unique) - i - 1,
            )
            token = None
            continue
        if got is not None:
            resolved[mbid] = got
    commit(new_mappings)
    return resolved


def to_sourced_track(entry: dict, source: Source) -> SourcedTrack | None:
    """Build a domain `SourcedTrack` from a resolved entry (for the game pool)."""
    spotify_id = entry.get("spotify_id")
    if not spotify_id:
        return None
    return SourcedTrack(
        isrc=entry.get("isrc"),
        spotify_id=spotify_id,
        spotify_uri=entry.get("spotify_uri") or f"spotify:track:{spotify_id}",
        name=entry.get("name"),
        artist=entry.get("artist"),
        album_art=entry.get("album_art"),
        year=entry.get("year"),
        source=source,
    )
