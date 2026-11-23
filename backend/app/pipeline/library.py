"""Per-player library extraction from Spotify.

Fetches a player's Spotify library and normalizes each raw track into a `SourcedTrack`,
tagged with where it came from. Split into two independently-callable parts so the
frontend can pre-fetch top tracks the moment a player connects, then fetch liked +
playlists later when they hit "Build profile" — less waiting at that moment.

**Extraction is cached** (`services/game_store.py`), keyed by the player's Spotify user id so
it outlives the 1h lobby: someone who plays twice in a week doesn't re-download the same
hundreds of tracks. This module owns the *freshness policy*; the store just records when each
range was fetched. Two different strategies, because the two sources change differently:

- **Top tracks** — time-based, one TTL per range (below). They drift continuously and there's
  no change token to compare against.
- **Playlists** — snapshot-based. Spotify's `snapshot_id` changes whenever a playlist is
  edited, so one cheap call tells us whether a cached copy is still exact. No TTL, no drift.

Uses `spotify_service` for the HTTP calls; no FastAPI here (pure pipeline).
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime

from app.core.logging import get_logger
from app.pipeline.models import Source, SourcedTrack, SourceSelection
from app.pipeline.scoring import occurrence_weight
from app.services import game_store, spotify_service

log = get_logger("pipeline.library")

# Top tracks come in three ranges; each maps to a distinct source tag and a target depth.
# Long-term reaches furthest back, so we pull the most of it; short-term is a small, recent
# snapshot. Spotify caps a single request at 50, so each range is paginated by offset.
_TOP_RANGES: list[tuple[str, Source, int]] = [
    ("short_term", Source.TOP_SHORT, 100),
    ("medium_term", Source.TOP_MEDIUM, 500),
    ("long_term", Source.TOP_LONG, 700),
]
_TOP_PAGE = 50  # Spotify max per request

_DAY = 86400.0
# How long a cached range stays usable (TUNABLE). Matched to how fast each range actually
# moves: Spotify's short_term is a ~4-week rolling window, medium_term ~6 months, and
# long_term is computed from several years of history and barely shifts week to week. A cache
# older than every TTL means all three refetch — which is the "it's been a year, rerun
# everything" case, with no special handling needed.
_TOP_TTL: dict[Source, float] = {
    Source.TOP_SHORT: 7 * _DAY,
    Source.TOP_MEDIUM: 30 * _DAY,
    Source.TOP_LONG: 180 * _DAY,
}


def _parse_year(release_date: str | None) -> int | None:
    # release_date is "YYYY", "YYYY-MM", or "YYYY-MM-DD".
    if not release_date:
        return None
    try:
        return int(release_date[:4])
    except ValueError:
        return None


def _parse_added_at(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _normalize(
    track: dict | None,
    source: Source,
    *,
    rank: int | None = None,
    added_at: datetime | None = None,
) -> SourcedTrack | None:
    """Turn one raw Spotify track object into a SourcedTrack, or None if unusable
    (missing, local file, or a podcast episode)."""
    if not track or track.get("is_local") or not track.get("id"):
        return None
    if track.get("type") not in (None, "track"):
        return None  # skip episodes in playlists

    album = track.get("album") or {}
    images = album.get("images") or []
    artists = track.get("artists") or []
    return SourcedTrack(
        isrc=(track.get("external_ids") or {}).get("isrc"),
        spotify_id=track["id"],
        spotify_uri=track.get("uri") or f"spotify:track:{track['id']}",
        name=track.get("name"),
        artist=artists[0]["name"] if artists else None,
        album_art=images[0]["url"] if images else None,
        year=_parse_year(album.get("release_date")),
        source=source,
        added_at=added_at,
        rank=rank,
    )


# ── Cache (de)serialization ───────────────────────────────────────────────────


def _to_row(track: SourcedTrack) -> dict:
    """A `SourcedTrack` as a plain cache row. `source` isn't stored per row (it's part of the
    cache key for top tracks, and implied for playlists)."""
    return {
        "spotify_id": track.spotify_id,
        "isrc": track.isrc,
        "spotify_uri": track.spotify_uri,
        "name": track.name,
        "artist": track.artist,
        "album_art": track.album_art,
        "year": track.year,
        "rank": track.rank,
        "added_at": track.added_at.isoformat() if track.added_at else None,
    }


def _from_row(row: dict, source: Source) -> SourcedTrack:
    return SourcedTrack(
        isrc=row.get("isrc"),
        spotify_id=row["spotify_id"],
        spotify_uri=row.get("spotify_uri") or f"spotify:track:{row['spotify_id']}",
        name=row.get("name"),
        artist=row.get("artist"),
        album_art=row.get("album_art"),
        year=row.get("year"),
        source=source,
        added_at=_parse_added_at(row.get("added_at")),
        rank=row.get("rank"),
    )


def dedupe_tracks(tracks: list[SourcedTrack]) -> list[SourcedTrack]:
    """Collapse duplicates to one entry per Spotify id, keeping the strongest occurrence.

    The same song routinely appears across the three top-track ranges and again in
    liked/playlists; without this a library stores it several times over. We key on
    `spotify_id` (every track has one — ISRC can be missing) and keep the occurrence with
    the highest `occurrence_weight`, the same strength used for matching/scoring, so the
    survivor carries the best provenance (e.g. top-short beats a plain liked entry)."""
    best: dict[str, SourcedTrack] = {}
    for t in tracks:
        prev = best.get(t.spotify_id)
        if prev is None or occurrence_weight(t.source, t.rank) > occurrence_weight(
            prev.source, prev.rank
        ):
            best[t.spotify_id] = t
    return list(best.values())


def _sample(tracks: list[SourcedTrack], n: int = 5) -> str:
    """A few 'Name — Artist (year)' examples, for logging what actually got fetched."""
    return (
        " | ".join(f"{t.name} — {t.artist} ({t.year})" for t in tracks[:n]) or "(none)"
    )


# ── Top tracks ────────────────────────────────────────────────────────────────


async def _fetch_top_range(
    access_token: str, time_range: str, count: int
) -> tuple[list[dict], bool]:
    """Paginate `/me/top/tracks` for one range up to `count` items, 50 per request.

    Returns `(items, exhausted)`. `exhausted` means Spotify ran out before we hit `count` —
    the account simply has fewer top tracks in this range. That's recorded in the cache so a
    small account isn't mistaken for a shallow cache and refetched on every connect."""
    items: list[dict] = []
    offset = 0
    exhausted = False
    while len(items) < count:
        limit = min(_TOP_PAGE, count - len(items))
        resp = await spotify_service.get_top_tracks(
            access_token, time_range=time_range, limit=limit, offset=offset
        )
        batch = resp.get("items", [])
        if not batch:
            exhausted = True
            break
        items.extend(batch)
        offset += len(batch)
        if len(batch) < limit:
            exhausted = True
            break  # reached the end of this range's top tracks
    return items[:count], exhausted


def _cache_is_fresh(meta: dict | None, source: Source, requested: int) -> bool:
    """Can we serve this range from cache instead of calling Spotify?"""
    if meta is None:
        return False
    if time.time() - meta["fetched_at"] > _TOP_TTL[source]:
        return False
    # Extraction depth was raised since this was cached — the cache is too shallow to satisfy
    # the new target, unless Spotify had already given us everything it had.
    return meta["requested_count"] >= requested or meta["exhausted"]


def _load_cached_top(spotify_user_id: str) -> dict[Source, list[SourcedTrack]]:
    """Every range that's still fresh, as tracks. Blocking (SQLite) — call in a thread."""
    out: dict[Source, list[SourcedTrack]] = {}
    for _time_range, source, count in _TOP_RANGES:
        meta = game_store.get_top_fetch(spotify_user_id, str(source))
        if not _cache_is_fresh(meta, source, count):
            continue
        rows = game_store.get_top_tracks(spotify_user_id, str(source))
        out[source] = [_from_row(row, source) for row in rows]
    return out


async def fetch_top_tracks(
    access_token: str, spotify_user_id: str | None = None
) -> list[SourcedTrack]:
    """Call 1: all three top-track ranges (short/medium/long). `rank` = position within a
    range (0 = strongest).

    Ranges still inside their TTL are served from the cache; only the stale ones are fetched
    (concurrently) and written back. Without a `spotify_user_id` there's no cache key, so
    everything is fetched — the pre-cache behaviour."""
    cached: dict[Source, list[SourcedTrack]] = {}
    if spotify_user_id:
        cached = await asyncio.to_thread(_load_cached_top, spotify_user_id)

    stale = [r for r in _TOP_RANGES if r[1] not in cached]
    responses = await asyncio.gather(
        *(
            _fetch_top_range(access_token, time_range, count)
            for time_range, _source, count in stale
        )
    )

    fetched: dict[Source, list[SourcedTrack]] = {}
    for (_time_range, source, count), (items, exhausted) in zip(stale, responses):
        tracks = [
            st
            for rank, track in enumerate(items)
            if (st := _normalize(track, source, rank=rank)) is not None
        ]
        fetched[source] = tracks
        if spotify_user_id:
            await asyncio.to_thread(
                game_store.store_top_tracks,
                spotify_user_id,
                str(source),
                [_to_row(t) for t in tracks],
                requested_count=count,
                exhausted=exhausted,
            )

    out: list[SourcedTrack] = []
    for _time_range, source, _count in _TOP_RANGES:
        out.extend(cached.get(source, fetched.get(source, [])))

    deduped = dedupe_tracks(out)
    log.info(
        "top tracks: %d after dedupe (from %d raw); %d ranges from cache, %d fetched. e.g. %s",
        len(deduped),
        len(out),
        len(cached),
        len(fetched),
        _sample(deduped),
    )
    return deduped


# ── Liked + playlists ─────────────────────────────────────────────────────────


async def fetch_liked(access_token: str, count: int) -> list[SourcedTrack]:
    if count <= 0:
        return []
    items = await spotify_service.get_all_liked_tracks(access_token, count)
    out: list[SourcedTrack] = []
    for item in items:
        st = _normalize(
            item.get("track"),
            Source.LIKED,
            added_at=_parse_added_at(item.get("added_at")),
        )
        if st:
            out.append(st)
    log.info("liked: fetched %d. e.g. %s", len(out), _sample(out))
    return out


async def _fetch_one_playlist(access_token: str, playlist_id: str) -> list[SourcedTrack]:
    """One playlist's tracks, from cache when its snapshot hasn't changed.

    `snapshot_id` is Spotify's own change token, so a matching snapshot means the cached copy
    is exact — one small call replaces paginating the whole playlist. If the snapshot lookup
    fails we just fetch normally rather than treating it as a failed playlist."""
    snapshot: str | None = None
    try:
        snapshot = await spotify_service.get_playlist_snapshot_id(
            access_token, playlist_id
        )
    except Exception as e:  # noqa: BLE001 — snapshot is an optimization, never a hard failure
        log.warning("playlist %s: snapshot lookup failed (%r); fetching", playlist_id, e)

    if snapshot:
        cached = await asyncio.to_thread(
            game_store.get_playlist_tracks, playlist_id, snapshot
        )
        if cached is not None:
            tracks = [_from_row(row, Source.PLAYLIST) for row in cached]
            log.info(
                "playlist %s: %d tracks from cache (snapshot %s). e.g. %s",
                playlist_id, len(tracks), snapshot, _sample(tracks),
            )
            return tracks

    items = await spotify_service.get_all_playlist_tracks(access_token, playlist_id)
    tracks: list[SourcedTrack] = []
    for item in items:
        st = _normalize(
            item.get("item"),
            Source.PLAYLIST,
            added_at=_parse_added_at(item.get("added_at")),
        )
        if st:
            tracks.append(st)
    log.info(
        "playlist %s: fetched %d. e.g. %s", playlist_id, len(tracks), _sample(tracks)
    )
    if snapshot:
        await asyncio.to_thread(
            game_store.store_playlist_tracks,
            playlist_id,
            snapshot,
            [_to_row(t) for t in tracks],
        )
    return tracks


async def fetch_playlists(
    access_token: str, playlist_ids: list[str]
) -> list[SourcedTrack]:
    if not playlist_ids:
        return []
    # return_exceptions so ONE unreadable playlist doesn't sink the whole build. Spotify
    # 403/404s on Spotify-owned algorithmic/editorial playlists (Discover Weekly, Daily
    # Mix, editorial lists) for apps outside extended quota — skip those, keep the rest.
    results = await asyncio.gather(
        *(_fetch_one_playlist(access_token, pid) for pid in playlist_ids),
        return_exceptions=True,
    )
    out: list[SourcedTrack] = []
    for pid, tracks in zip(playlist_ids, results):
        if isinstance(tracks, BaseException):
            status = getattr(getattr(tracks, "response", None), "status_code", "?")
            log.warning("playlist %s: skipped (fetch failed, status %s)", pid, status)
            continue
        out.extend(tracks)
    return out


async def fetch_source_selection(
    access_token: str, sources: SourceSelection
) -> list[SourcedTrack]:
    """Call 2: liked songs + selected playlists, fetched concurrently."""
    liked, playlists = await asyncio.gather(
        fetch_liked(access_token, sources.liked_count),
        fetch_playlists(access_token, sources.playlist_ids),
    )
    deduped = dedupe_tracks(liked + playlists)
    log.info(
        "fetched %d liked + %d playlist tracks (%d playlists) -> %d after dedupe",
        len(liked),
        len(playlists),
        len(sources.playlist_ids),
        len(deduped),
    )
    return deduped
