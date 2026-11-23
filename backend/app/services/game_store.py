"""Durable game data — the first place this project stores anything about *people*.

Deliberately a separate SQLite file from `models/isrc_bridge.db`: that one is a large,
regenerable artifact built offline by `scripts/`, and user data must never live somewhere we
might reasonably delete and rebuild. This DB lives at `data/hitster.db` (mounted as `/data` in
Docker) and is the seam where a real database would slot in later.

Two concerns live here:

1. **Extraction cache** (`top_track_*`, `playlist_*`) — a player's Spotify library, keyed by
   their **Spotify user id** so it survives lobbies. Top tracks are cached per range with a
   fetch timestamp; the *freshness policy* (how old is too old) lives in `pipeline/library.py`,
   not here — this module just records when each range was fetched and hands the rows back.
   Playlists are keyed by Spotify's `snapshot_id`, which changes whenever a playlist is edited,
   so their cache is exact rather than time-based.

2. **Play history** (`game_sessions`, `session_players`, `played_songs`) — which songs a group
   actually heard, so later games can skip them. A song played in a session was heard by every
   player in that session, so plays hang off the **session** and per-player history is a join
   — which is why there's no "group" identity anywhere: the 3-players-then-4 question answers
   itself.

Threading: one long-lived connection in WAL mode, guarded by a lock. Callers are async FastAPI
handlers, so the bulk read/write functions should be invoked via `asyncio.to_thread` (see
`pipeline/library.py`) — SQLite work is blocking and shouldn't sit on the event loop.
"""

from __future__ import annotations

import secrets
import sqlite3
import threading
import time
from app.core.config import DATA_DIR
from app.core.logging import get_logger

log = get_logger("services.game_store")

_DB_PATH = DATA_DIR / "hitster.db"

# Stay well under SQLite's host-parameter limit in `IN (...)` queries.
_MAX_PARAMS = 900

_conn: sqlite3.Connection | None = None
_lock = threading.Lock()

_SCHEMA = """
-- ── Extraction cache ────────────────────────────────────────────────────────
-- One row per cached top track. `source` is the pipeline Source value (top_short/…).
CREATE TABLE IF NOT EXISTS top_track_cache (
    spotify_user_id TEXT    NOT NULL,
    source          TEXT    NOT NULL,
    spotify_id      TEXT    NOT NULL,
    rank            INTEGER NOT NULL,
    isrc            TEXT,
    spotify_uri     TEXT,
    name            TEXT,
    artist          TEXT,
    album_art       TEXT,
    year            INTEGER,
    PRIMARY KEY (spotify_user_id, source, spotify_id)
);

-- When each (user, range) was last fetched, and how deep. `requested_count` matters because
-- raising the extraction depth must invalidate a shallower cache; `exhausted` records that
-- Spotify ran out of tracks before we hit the target, so a small account isn't seen as
-- permanently stale and refetched every single time.
CREATE TABLE IF NOT EXISTS top_track_fetch (
    spotify_user_id TEXT    NOT NULL,
    source          TEXT    NOT NULL,
    fetched_at      REAL    NOT NULL,
    requested_count INTEGER NOT NULL,
    fetched_count   INTEGER NOT NULL,
    exhausted       INTEGER NOT NULL,
    PRIMARY KEY (spotify_user_id, source)
);

-- Playlist contents, keyed by the playlist's snapshot. A new snapshot_id means the playlist
-- changed, so old rows simply stop matching — no TTL needed.
CREATE TABLE IF NOT EXISTS playlist_cache (
    playlist_id TEXT    NOT NULL,
    snapshot_id TEXT    NOT NULL,
    position    INTEGER NOT NULL,
    spotify_id  TEXT    NOT NULL,
    isrc        TEXT,
    spotify_uri TEXT,
    name        TEXT,
    artist      TEXT,
    album_art   TEXT,
    year        INTEGER,
    added_at    TEXT,
    PRIMARY KEY (playlist_id, snapshot_id, spotify_id)
);

CREATE TABLE IF NOT EXISTS playlist_fetch (
    playlist_id TEXT PRIMARY KEY,
    snapshot_id TEXT    NOT NULL,
    fetched_at  REAL    NOT NULL,
    track_count INTEGER NOT NULL
);

-- ── Play history ───────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS game_sessions (
    session_id TEXT PRIMARY KEY,
    started_at REAL NOT NULL,
    ended_at   REAL
);

CREATE TABLE IF NOT EXISTS session_players (
    session_id      TEXT NOT NULL,
    spotify_user_id TEXT NOT NULL,
    username        TEXT,
    PRIMARY KEY (session_id, spotify_user_id)
);

-- A song heard during a session. `isrc` is the better identity (one recording has different
-- Spotify ids across markets and re-releases) but can be absent, so both are stored.
CREATE TABLE IF NOT EXISTS played_songs (
    session_id TEXT NOT NULL,
    spotify_id TEXT NOT NULL,
    isrc       TEXT,
    name       TEXT,
    artist     TEXT,
    year       INTEGER,
    played_at  REAL NOT NULL,
    PRIMARY KEY (session_id, spotify_id)
);

CREATE INDEX IF NOT EXISTS idx_session_players_user ON session_players (spotify_user_id);
CREATE INDEX IF NOT EXISTS idx_played_songs_isrc    ON played_songs (isrc);
"""

def _connect() -> sqlite3.Connection:
    """The shared connection, created (and migrated) on first use."""
    global _conn
    if _conn is not None:
        return _conn
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(_SCHEMA)
    conn.commit()
    _conn = conn
    log.info("game store ready at %s", _DB_PATH)
    return conn


# ── Top-track cache ───────────────────────────────────────────────────────────


def get_top_fetch(spotify_user_id: str, source: str) -> dict | None:
    """When this (user, range) was last fetched, and how deep — or None if never.

    Returns `{fetched_at, requested_count, fetched_count, exhausted}`. Freshness is the
    caller's decision (`pipeline/library.py` owns the TTLs)."""
    with _lock:
        row = _connect().execute(
            "SELECT fetched_at, requested_count, fetched_count, exhausted "
            "FROM top_track_fetch WHERE spotify_user_id = ? AND source = ?",
            (spotify_user_id, source),
        ).fetchone()
    if row is None:
        return None
    return {
        "fetched_at": row["fetched_at"],
        "requested_count": row["requested_count"],
        "fetched_count": row["fetched_count"],
        "exhausted": bool(row["exhausted"]),
    }


def get_top_tracks(spotify_user_id: str, source: str) -> list[dict]:
    """Cached tracks for one (user, range), in rank order. Each dict mirrors `SourcedTrack`
    fields plus `rank`; `source` is added by the caller."""
    with _lock:
        rows = _connect().execute(
            "SELECT spotify_id, rank, isrc, spotify_uri, name, artist, album_art, year "
            "FROM top_track_cache WHERE spotify_user_id = ? AND source = ? ORDER BY rank",
            (spotify_user_id, source),
        ).fetchall()
    return [dict(row) for row in rows]


def store_top_tracks(
    spotify_user_id: str,
    source: str,
    tracks: list[dict],
    *,
    requested_count: int,
    exhausted: bool,
) -> None:
    """Replace the cached tracks for one (user, range) and stamp the fetch.

    A full replace (not an upsert) is deliberate: it's what makes the cache self-healing when
    someone's top tracks reorder or a track disappears from Spotify."""
    now = time.time()
    with _lock:
        conn = _connect()
        with conn:  # one transaction — never leave a range half-replaced
            conn.execute(
                "DELETE FROM top_track_cache WHERE spotify_user_id = ? AND source = ?",
                (spotify_user_id, source),
            )
            conn.executemany(
                "INSERT INTO top_track_cache (spotify_user_id, source, spotify_id, rank, "
                "isrc, spotify_uri, name, artist, album_art, year) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        spotify_user_id,
                        source,
                        t["spotify_id"],
                        t["rank"],
                        t.get("isrc"),
                        t.get("spotify_uri"),
                        t.get("name"),
                        t.get("artist"),
                        t.get("album_art"),
                        t.get("year"),
                    )
                    for t in tracks
                ],
            )
            conn.execute(
                "INSERT OR REPLACE INTO top_track_fetch (spotify_user_id, source, fetched_at, "
                "requested_count, fetched_count, exhausted) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    spotify_user_id,
                    source,
                    now,
                    requested_count,
                    len(tracks),
                    int(exhausted),
                ),
            )
    log.info(
        "cached %d %s tracks for user %s", len(tracks), source, spotify_user_id
    )


# ── Playlist cache ────────────────────────────────────────────────────────────


def get_playlist_tracks(playlist_id: str, snapshot_id: str) -> list[dict] | None:
    """Cached contents of a playlist **at this exact snapshot**, in playlist order.

    None means we've never cached this snapshot (the playlist is new to us or has been edited
    since) — an empty list means a genuinely empty playlist, which is why this isn't `[]`."""
    with _lock:
        conn = _connect()
        fetch = conn.execute(
            "SELECT snapshot_id FROM playlist_fetch WHERE playlist_id = ?", (playlist_id,)
        ).fetchone()
        if fetch is None or fetch["snapshot_id"] != snapshot_id:
            return None
        rows = conn.execute(
            "SELECT spotify_id, isrc, spotify_uri, name, artist, album_art, year, added_at "
            "FROM playlist_cache WHERE playlist_id = ? AND snapshot_id = ? ORDER BY position",
            (playlist_id, snapshot_id),
        ).fetchall()
    return [dict(row) for row in rows]


def store_playlist_tracks(
    playlist_id: str, snapshot_id: str, tracks: list[dict]
) -> None:
    """Replace a playlist's cached contents. Older snapshots of the same playlist are dropped —
    only the current one is ever read, so keeping them would just grow the file."""
    now = time.time()
    with _lock:
        conn = _connect()
        with conn:
            conn.execute(
                "DELETE FROM playlist_cache WHERE playlist_id = ?", (playlist_id,)
            )
            conn.executemany(
                "INSERT INTO playlist_cache (playlist_id, snapshot_id, position, spotify_id, "
                "isrc, spotify_uri, name, artist, album_art, year, added_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        playlist_id,
                        snapshot_id,
                        position,
                        t["spotify_id"],
                        t.get("isrc"),
                        t.get("spotify_uri"),
                        t.get("name"),
                        t.get("artist"),
                        t.get("album_art"),
                        t.get("year"),
                        t.get("added_at"),
                    )
                    for position, t in enumerate(tracks)
                ],
            )
            conn.execute(
                "INSERT OR REPLACE INTO playlist_fetch (playlist_id, snapshot_id, fetched_at, "
                "track_count) VALUES (?, ?, ?, ?)",
                (playlist_id, snapshot_id, now, len(tracks)),
            )
    log.info("cached %d tracks for playlist %s (%s)", len(tracks), playlist_id, snapshot_id)


# ── Play history ──────────────────────────────────────────────────────────────


def create_session(players: list[tuple[str, str]]) -> str:
    """Start a game session for `[(spotify_user_id, username)]`. Returns its id.

    Membership is recorded up front because that's what makes a play attributable: a song
    played during this session was heard by *everyone* listed here, which is how per-player
    history is derived later without ever needing a "group" identity."""
    session_id = secrets.token_urlsafe(9)
    now = time.time()
    with _lock:
        conn = _connect()
        with conn:
            conn.execute(
                "INSERT INTO game_sessions (session_id, started_at) VALUES (?, ?)",
                (session_id, now),
            )
            conn.executemany(
                "INSERT OR REPLACE INTO session_players (session_id, spotify_user_id, username) "
                "VALUES (?, ?, ?)",
                [(session_id, uid, name) for uid, name in players if uid],
            )
    log.info("session %s started with %d players", session_id, len(players))
    return session_id


def record_play(session_id: str, song: dict) -> None:
    """Record one song as played in this session. Idempotent — replaying the same card in the
    same session doesn't duplicate the row, so the caller can fire and forget."""
    with _lock:
        conn = _connect()
        with conn:
            conn.execute(
                "INSERT OR IGNORE INTO played_songs (session_id, spotify_id, isrc, name, "
                "artist, year, played_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    session_id,
                    song["spotify_id"],
                    song.get("isrc"),
                    song.get("name"),
                    song.get("artist"),
                    song.get("year"),
                    time.time(),
                ),
            )


def end_session(session_id: str) -> None:
    """Stamp a session as finished. Purely informational — an abandoned session simply keeps a
    null `ended_at` and its plays still count."""
    with _lock:
        conn = _connect()
        with conn:
            conn.execute(
                "UPDATE game_sessions SET ended_at = ? WHERE session_id = ?",
                (time.time(), session_id),
            )


def get_sessions_for_users(
    spotify_user_ids: list[str], limit: int = 20
) -> list[dict]:
    """Past sessions **any** of these players took part in, newest first.

    "Any", not "all", on purpose: if three people played and a fourth joins tonight, those
    songs are still burned for the three who heard them — the newcomer arriving doesn't un-hear
    them. Each entry carries its full player list and song count so the lobby can show a
    per-session checklist."""
    ids = [uid for uid in spotify_user_ids if uid]
    if not ids:
        return []
    out: list[dict] = []
    with _lock:
        conn = _connect()
        placeholders = ",".join("?" * len(ids))
        rows = conn.execute(
            f"""
            SELECT s.session_id, s.started_at, s.ended_at,
                   (SELECT COUNT(*) FROM played_songs p WHERE p.session_id = s.session_id)
                       AS song_count
            FROM game_sessions s
            WHERE s.session_id IN (
                SELECT session_id FROM session_players WHERE spotify_user_id IN ({placeholders})
            )
            ORDER BY s.started_at DESC
            LIMIT ?
            """,
            (*ids, limit),
        ).fetchall()
        for row in rows:
            members = conn.execute(
                "SELECT username, spotify_user_id FROM session_players WHERE session_id = ?",
                (row["session_id"],),
            ).fetchall()
            out.append(
                {
                    "session_id": row["session_id"],
                    "started_at": row["started_at"],
                    "ended_at": row["ended_at"],
                    "song_count": row["song_count"],
                    "players": [m["username"] or m["spotify_user_id"] for m in members],
                }
            )
    return out


def get_played_by_session(session_ids: list[str]) -> dict[str, dict]:
    """`{session_id: {"spotify_ids": set, "isrcs": set}}` for the given sessions.

    Both identities come back because they're used together: the deck builder keys candidates
    by ISRC, but a song without one can still be excluded by its Spotify id."""
    if not session_ids:
        return {}
    unique = list(dict.fromkeys(session_ids))
    out: dict[str, dict] = {
        sid: {"spotify_ids": set(), "isrcs": set()} for sid in unique
    }
    with _lock:
        conn = _connect()
        for start in range(0, len(unique), _MAX_PARAMS):
            chunk = unique[start : start + _MAX_PARAMS]
            placeholders = ",".join("?" * len(chunk))
            rows = conn.execute(
                f"SELECT session_id, spotify_id, isrc FROM played_songs "
                f"WHERE session_id IN ({placeholders})",
                chunk,
            ).fetchall()
            for row in rows:
                entry = out[row["session_id"]]
                entry["spotify_ids"].add(row["spotify_id"])
                if row["isrc"]:
                    entry["isrcs"].add(row["isrc"])
    return out
