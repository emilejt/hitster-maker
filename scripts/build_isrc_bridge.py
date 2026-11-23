"""
One-time script: builds models/isrc_bridge.db from a MusicBrainz dump.

The DB has one table, isrc_to_mbid, indexed in both directions:
  - isrc         → recording_mbid   (input side: Spotify ISRC → MBID for user vectors)
  - recording_mbid → isrc           (output side: nearest-neighbor MBID → ISRC → Spotify)

Usage:
    python scripts/build_isrc_bridge.py --mbdump path/to/mbdump
"""

import argparse
import os
import sqlite3

BATCH_SIZE = 100_000
DB_PATH = os.path.join(os.path.dirname(__file__), "..", "models", "isrc_bridge.db")


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mbdump",
        required=True,
        help="Path to the extracted mbdump directory (contains 'recording' and 'isrc' files)",
    )
    return parser.parse_args()


def build_bridge(mbdump_path: str, db_path: str) -> None:
    recording_path = os.path.join(mbdump_path, "recording")
    isrc_path = os.path.join(mbdump_path, "isrc")

    for path in (recording_path, isrc_path):
        if not os.path.exists(path):
            raise FileNotFoundError(f"Expected dump file not found: {path}")

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS isrc_to_mbid (
            isrc            TEXT PRIMARY KEY,
            recording_mbid  TEXT NOT NULL
        )
    """)
    conn.commit()

    # ── Step 1: recording internal id → MBID ────────────────────────────────
    # recording columns: id | gid (MBID) | name | artist_credit | ...
    print("Loading recording table...")
    recording_id_to_mbid: dict[str, str] = {}

    with open(recording_path, encoding="utf-8") as f:
        for line in f:
            parts = line.rstrip("\n").split("\t")
            recording_id_to_mbid[parts[0]] = parts[1]  # internal id → gid (MBID)

    print(f"  Loaded {len(recording_id_to_mbid):,} recordings")

    # ── Step 2: isrc table → populate bridge ────────────────────────────────
    # isrc columns: id | recording (internal id) | isrc | source | edits_pending
    print("Loading isrc table and writing to SQLite...")
    batch: list[tuple[str, str]] = []
    inserted = 0
    skipped = 0

    with open(isrc_path, encoding="utf-8") as f:
        for line in f:
            parts = line.rstrip("\n").split("\t")
            internal_id = parts[1]
            isrc = parts[2]

            mbid = recording_id_to_mbid.get(internal_id)
            if not mbid:
                skipped += 1
                continue

            batch.append((isrc, mbid))

            if len(batch) >= BATCH_SIZE:
                cur.executemany(
                    "INSERT OR IGNORE INTO isrc_to_mbid (isrc, recording_mbid) VALUES (?, ?)",
                    batch,
                )
                conn.commit()
                inserted += len(batch)
                batch.clear()
                print(f"  {inserted:,} rows inserted...")

    if batch:
        cur.executemany(
            "INSERT OR IGNORE INTO isrc_to_mbid (isrc, recording_mbid) VALUES (?, ?)",
            batch,
        )
        conn.commit()
        inserted += len(batch)

    print(f"  Done. {inserted:,} inserted, {skipped:,} skipped (no MBID match)")

    # ── Step 3: indexes ──────────────────────────────────────────────────────
    # isrc is already PRIMARY KEY (implicit index for forward lookups)
    # recording_mbid index enables reverse lookups without a scan
    print("Creating index on recording_mbid...")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_mbid ON isrc_to_mbid (recording_mbid)")
    conn.commit()
    conn.close()

    size_mb = os.path.getsize(db_path) / 1_048_576
    print(f"\nDatabase saved to {db_path} ({size_mb:.1f} MB)")
    print(f"Total rows: {inserted:,}")


if __name__ == "__main__":
    args = parse_args()
    build_bridge(args.mbdump, os.path.abspath(DB_PATH))
