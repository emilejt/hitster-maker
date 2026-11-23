"""
One-time script: builds an `mbid_meta` table in models/isrc_bridge.db from a MusicBrainz dump.

  mbid_meta:  recording_mbid → (isrc, name, artist)

This is the *reverse/output* side of the bridge, keyed by MBID: given an embedding neighbour
(a bare MusicBrainz recording MBID) it returns everything needed to label and resolve it in a
single lookup — a representative ISRC (for an optional Spotify enrichment) plus the title and
artist MusicBrainz already knows.

Why this is separate from `isrc_to_mbid`: that table is keyed by ISRC (5.8M rows) for the
*seed* direction (a player's track ISRC → MBID), and one MBID has many ISRCs — so name/artist
can't live there without duplicating across every release. `mbid_meta` keeps one row per
recording, restricted to the embedding model's vocab (models/mbid_to_idx.json, ~800K).

Reads three dump files:
  recording           id | gid(MBID) | name | artist_credit | ...
  isrc                id | recording(internal id) | isrc | ...
  artist_credit_name  artist_credit | position | artist | name | join_phrase

A recording's displayed artist is its artist_credit_name rows concatenated in position order
(name + join_phrase), reproducing credits like "A feat. B".

Usage:
    python scripts/build_mbid_meta.py --mbdump path/to/mbdump
"""

import argparse
import json
import os
import sqlite3

BATCH_SIZE = 100_000
_HERE = os.path.dirname(__file__)
DB_PATH = os.path.join(_HERE, "..", "models", "isrc_bridge.db")
VOCAB_PATH = os.path.join(_HERE, "..", "models", "mbid_to_idx.json")


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mbdump",
        required=True,
        help="Path to the extracted mbdump directory (contains 'recording', 'isrc' and "
        "'artist_credit_name' files)",
    )
    return parser.parse_args()


def _unescape(field: str) -> str | None:
    """Undo MusicBrainz's Postgres COPY escaping. `\\N` is SQL NULL."""
    if field == r"\N":
        return None
    return (
        field.replace(r"\t", "\t")
        .replace(r"\r", "\r")
        .replace(r"\n", "\n")
        .replace("\\\\", "\\")
    )


def build_mbid_meta(mbdump_path: str, db_path: str, vocab_path: str) -> None:
    recording_path = os.path.join(mbdump_path, "recording")
    isrc_path = os.path.join(mbdump_path, "isrc")
    acn_path = os.path.join(mbdump_path, "artist_credit_name")
    for path in (recording_path, isrc_path, acn_path):
        if not os.path.exists(path):
            raise FileNotFoundError(f"Expected dump file not found: {path}")

    with open(vocab_path, encoding="utf-8") as f:
        vocab = set(json.load(f).keys())
    print(f"Model vocab: {len(vocab):,} MBIDs")

    # ── Pass 1: recording → name + artist_credit, for vocab MBIDs only ──────────
    # Also remember each recording's internal id so the isrc pass can attach an ISRC.
    print("Loading recording table...")
    mbid_name: dict[str, str | None] = {}
    mbid_ac: dict[str, str] = {}
    recid_to_mbid: dict[str, str] = {}
    needed_ac: set[str] = set()

    with open(recording_path, encoding="utf-8") as f:
        for line in f:
            parts = line.rstrip("\n").split("\t")
            gid = parts[1]
            if gid not in vocab:
                continue
            mbid_name[gid] = _unescape(parts[2])
            mbid_ac[gid] = parts[3]
            recid_to_mbid[parts[0]] = gid
            needed_ac.add(parts[3])

    print(f"  Matched {len(mbid_name):,} vocab recordings")

    # ── Pass 2: isrc → one representative ISRC per vocab MBID ────────────────────
    print("Loading isrc table...")
    mbid_isrc: dict[str, str] = {}
    with open(isrc_path, encoding="utf-8") as f:
        for line in f:
            parts = line.rstrip("\n").split("\t")
            gid = recid_to_mbid.get(parts[1])
            if gid is not None:
                mbid_isrc.setdefault(gid, parts[2])  # first ISRC seen wins
    print(f"  Attached ISRCs to {len(mbid_isrc):,} MBIDs")

    # ── Pass 3: artist_credit_name → assemble each needed credit's artist string ─
    print("Loading artist_credit_name table...")
    ac_parts: dict[str, list[tuple[int, str, str]]] = {}
    with open(acn_path, encoding="utf-8") as f:
        for line in f:
            parts = line.rstrip("\n").split("\t")
            ac = parts[0]
            if ac not in needed_ac:
                continue
            position = int(parts[1])
            name = _unescape(parts[3]) or ""
            join_phrase = _unescape(parts[4]) if len(parts) > 4 else ""
            ac_parts.setdefault(ac, []).append((position, name, join_phrase or ""))

    ac_artist: dict[str, str] = {}
    for ac, rows in ac_parts.items():
        rows.sort()
        ac_artist[ac] = "".join(name + join for _, name, join in rows).strip()
    print(f"  Assembled {len(ac_artist):,} artist strings")

    # ── Write the table ─────────────────────────────────────────────────────────
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS mbid_meta (
            recording_mbid  TEXT PRIMARY KEY,
            isrc            TEXT,
            name            TEXT,
            artist          TEXT
        )
        """
    )
    conn.commit()

    print("Writing mbid_meta...")
    batch: list[tuple[str, str | None, str | None, str | None]] = []
    written = 0
    for mbid, name in mbid_name.items():
        artist = ac_artist.get(mbid_ac.get(mbid, ""))
        batch.append((mbid, mbid_isrc.get(mbid), name, artist))
        if len(batch) >= BATCH_SIZE:
            cur.executemany(
                "INSERT OR REPLACE INTO mbid_meta (recording_mbid, isrc, name, artist) "
                "VALUES (?, ?, ?, ?)",
                batch,
            )
            conn.commit()
            written += len(batch)
            batch.clear()
            print(f"  {written:,} rows written...")

    if batch:
        cur.executemany(
            "INSERT OR REPLACE INTO mbid_meta (recording_mbid, isrc, name, artist) "
            "VALUES (?, ?, ?, ?)",
            batch,
        )
        conn.commit()
        written += len(batch)

    print(f"  Done. {written:,} rows written.")
    print("Sample rows:")
    for row in cur.execute(
        "SELECT recording_mbid, isrc, name, artist FROM mbid_meta LIMIT 5"
    ):
        print("   ", row)
    conn.close()


if __name__ == "__main__":
    args = parse_args()
    build_mbid_meta(args.mbdump, os.path.abspath(DB_PATH), os.path.abspath(VOCAB_PATH))
