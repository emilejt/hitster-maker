"""The Spotify ↔ embedding ID bridge, in `models/isrc_bridge.db` (SQLite).

Three tables:

- `isrc_to_mbid` (5.8M rows, built by scripts/build_isrc_bridge.py): a player's Spotify track
  ISRC → MusicBrainz recording MBID, i.e. its row in the embedding.
- `mbid_meta` (~800K rows, built by scripts/build_mbid_meta.py): one row per embedding MBID
  with a representative ISRC, title and artist, to label and resolve a neighbour.
- `mbid_spotify_bridge` (written at runtime): cache of MBIDs already resolved to a playable
  Spotify track, so later games make no Spotify calls for them.

All lookups are batched: the file is ~850 MB, so opening a connection per song is ruinous.
"""

import sqlite3
from collections.abc import Iterator

from app.core.config import MODELS_DIR

_DB_PATH = MODELS_DIR / "isrc_bridge.db"

# Stay well under SQLite's host-parameter limit in `IN (...)` queries.
_MAX_PARAMS = 900

_cache_table_ready = False


def _connect() -> sqlite3.Connection:
    if not _DB_PATH.exists():
        raise RuntimeError(
            f"ISRC bridge database not found at {_DB_PATH}. "
            "Run scripts/build_isrc_bridge.py first."
        )
    return sqlite3.connect(str(_DB_PATH))


def _chunks(items: list[str]) -> Iterator[tuple[list[str], str]]:
    """Yield `(chunk, placeholders)` pairs for a chunked `IN (...)` query."""
    for start in range(0, len(items), _MAX_PARAMS):
        chunk = items[start : start + _MAX_PARAMS]
        yield chunk, ",".join("?" * len(chunk))


def _ensure_spotify_bridge_table(conn: sqlite3.Connection) -> None:
    """Create the runtime cache table (and add columns older DBs lack), once per process."""
    global _cache_table_ready
    if _cache_table_ready:
        return
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS mbid_spotify_bridge (
            mbid       TEXT PRIMARY KEY,
            spotify_id TEXT NOT NULL,
            isrc       TEXT,
            name       TEXT,
            artist     TEXT,
            album_art  TEXT,
            year       INTEGER
        )
        """
    )
    existing = {row[1] for row in conn.execute("PRAGMA table_info(mbid_spotify_bridge)")}
    for col, typ in [("name", "TEXT"), ("artist", "TEXT"), ("album_art", "TEXT"), ("year", "INTEGER")]:
        if col not in existing:
            conn.execute(f"ALTER TABLE mbid_spotify_bridge ADD COLUMN {col} {typ}")
    _cache_table_ready = True


def get_mbids_for_isrcs(isrcs: list[str]) -> dict[str, str]:
    """`{isrc: recording_mbid}` for the ISRCs that resolve."""
    if not isrcs:
        return {}
    out: dict[str, str] = {}
    with _connect() as conn:
        for chunk, ph in _chunks(list(dict.fromkeys(isrcs))):
            rows = conn.execute(
                f"SELECT isrc, recording_mbid FROM isrc_to_mbid WHERE isrc IN ({ph})", chunk
            )
            for isrc, mbid in rows:
                out.setdefault(isrc, mbid)
    return out


def get_meta_for_mbids(mbids: list[str]) -> dict[str, dict]:
    """MusicBrainz `{mbid: {isrc, name, artist}}` for embedding MBIDs.

    Returns {} if the `mbid_meta` table hasn't been built, so the app still runs against an
    older DB (name resolution then degrades to Spotify-only)."""
    if not mbids:
        return {}
    out: dict[str, dict] = {}
    with _connect() as conn:
        try:
            for chunk, ph in _chunks(list(dict.fromkeys(mbids))):
                rows = conn.execute(
                    f"SELECT recording_mbid, isrc, name, artist FROM mbid_meta "
                    f"WHERE recording_mbid IN ({ph})",
                    chunk,
                )
                for mbid, isrc, name, artist in rows:
                    out[mbid] = {"isrc": isrc, "name": name, "artist": artist}
        except sqlite3.OperationalError:
            return {}
    return out


def upsert_spotify_mappings(entries: list[dict]) -> None:
    """Write resolved tracks to the cache.

    Each entry: `{mbid, spotify_id, isrc, name, artist, album_art, year}`; all but the first
    two may be None. Storing full metadata is what makes a later hit need no API call.
    """
    if not entries:
        return
    with _connect() as conn:
        _ensure_spotify_bridge_table(conn)
        conn.executemany(
            """
            INSERT OR REPLACE INTO mbid_spotify_bridge
                (mbid, spotify_id, isrc, name, artist, album_art, year)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    e["mbid"],
                    e["spotify_id"],
                    e.get("isrc"),
                    e.get("name"),
                    e.get("artist"),
                    e.get("album_art"),
                    e.get("year"),
                )
                for e in entries
            ],
        )


def get_cached_tracks_for_mbids(mbids: list[str]) -> dict[str, dict]:
    """Cached tracks with metadata (`name IS NOT NULL`):
    `{mbid: {spotify_id, isrc, name, artist, album_art, year}}`."""
    if not mbids:
        return {}
    out: dict[str, dict] = {}
    with _connect() as conn:
        _ensure_spotify_bridge_table(conn)
        for chunk, ph in _chunks(list(dict.fromkeys(mbids))):
            rows = conn.execute(
                f"""
                SELECT mbid, spotify_id, isrc, name, artist, album_art, year
                FROM mbid_spotify_bridge
                WHERE mbid IN ({ph}) AND name IS NOT NULL
                """,
                chunk,
            )
            for mbid, spotify_id, isrc, name, artist, album_art, year in rows:
                out[mbid] = {
                    "spotify_id": spotify_id,
                    "isrc": isrc,
                    "name": name,
                    "artist": artist,
                    "album_art": album_art,
                    "year": year,
                }
    return out
