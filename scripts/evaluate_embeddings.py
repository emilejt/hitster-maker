"""
Intrinsic sanity checks for the song embedding (models/item2vec.wordvectors.vectors.npy).

Word2vec-style embeddings have no ground-truth accuracy, so this measures two things the
space *should* (and should not) encode, plus a qualitative neighbour dump:

  1. Artist coherence  — fraction of a song's 10 nearest neighbours by the same artist,
                         against a random-neighbour baseline. High = co-listening captured.
  2. Popularity tracking — correlation between a song's log-popularity and the mean
                         log-popularity of its neighbours. High = the space is partly
                         organised by frequency (a known word2vec bias).
  3. Anchor songs      — nearest *other-artist* neighbours of a few well-known tracks.

Needs only numpy, pyarrow and the prebuilt artifacts in models/ (no gensim, no backend).

Usage:
    python scripts/evaluate_embeddings.py [--queries 1000] [--seed 0]
"""

import argparse
import json
import os
import sqlite3
import sys

import numpy as np
import pyarrow.parquet as pq

MODELS = os.path.join(os.path.dirname(__file__), "..", "models")
K = 10
POPULAR_MIN_LISTENERS = 200  # ≈ the deck builder's popularity gate (score ≥ 0.7)
ANCHORS = [
    ("Bohemian Rhapsody", "Queen"),
    ("Smells Like Teen Spirit", "Nirvana"),
    ("Dancing Queen", "ABBA"),
    ("Blinding Lights", "The Weeknd"),
    ("HUMBLE.", "Kendrick Lamar"),
]


def load():
    vectors = np.load(os.path.join(MODELS, "item2vec.wordvectors.vectors.npy"))
    vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
    with open(os.path.join(MODELS, "idx_to_mbid.json"), encoding="utf-8") as f:
        idx_to_mbid = json.load(f)
    mbids = [idx_to_mbid[str(i)] for i in range(len(idx_to_mbid))]

    db = sqlite3.connect(f"file:{os.path.join(MODELS, 'isrc_bridge.db')}?mode=ro", uri=True)
    meta = {m: (n, a) for m, n, a in db.execute("SELECT recording_mbid, name, artist FROM mbid_meta")}

    table = pq.read_table(
        os.path.join(MODELS, "mbid_popularity.parquet"),
        columns=["recording_mbid", "unique_listeners"],
    )
    listeners = dict(zip(table.column(0).to_pylist(), table.column(1).to_pylist()))

    artists = np.array([meta.get(m, (None, None))[1] for m in mbids], dtype=object)
    popularity = np.array([listeners.get(m, 0) for m in mbids], dtype=float)
    return vectors, mbids, meta, artists, popularity


def nearest(vectors: np.ndarray, i: int, k: int) -> np.ndarray:
    sims = vectors @ vectors[i]
    sims[i] = -np.inf
    top = np.argpartition(sims, -k)[-k:]
    return top[np.argsort(-sims[top])]


def coherence(vectors, artists, popularity, mask, n_queries, rng) -> dict:
    pool = np.flatnonzero(mask & (artists != None))  # noqa: E711 — elementwise on object array
    queries = rng.choice(pool, size=min(n_queries, len(pool)), replace=False)
    same, same_random, pop_pairs = [], [], []
    for i in queries:
        nbrs = nearest(vectors, i, K)
        same.append(np.mean(artists[nbrs] == artists[i]))
        same_random.append(np.mean(artists[rng.choice(len(artists), K)] == artists[i]))
        pop_pairs.append((np.log1p(popularity[i]), np.log1p(popularity[nbrs]).mean()))
    pop_pairs = np.array(pop_pairs)
    return {
        "queries": len(queries),
        "artist_at_10": float(np.mean(same)),
        "random_baseline": float(np.mean(same_random)),
        "popularity_corr": float(np.corrcoef(pop_pairs[:, 0], pop_pairs[:, 1])[0, 1]),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--queries", type=int, default=1000)
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    rng = np.random.default_rng(args.seed)

    vectors, mbids, meta, artists, popularity = load()
    print(f"{len(mbids):,} songs × {vectors.shape[1]} dims\n")

    print(f"{'slice':<28}{'artist@10':>10}{'random':>10}{'pop corr':>10}")
    for label, mask in [
        ("all songs", np.ones(len(mbids), dtype=bool)),
        (f"popular (≥{POPULAR_MIN_LISTENERS} listeners)", popularity >= POPULAR_MIN_LISTENERS),
    ]:
        r = coherence(vectors, artists, popularity, mask, args.queries, rng)
        print(f"{label:<28}{r['artist_at_10']:>10.3f}{r['random_baseline']:>10.4f}"
              f"{r['popularity_corr']:>10.2f}")

    index = {m: i for i, m in enumerate(mbids)}
    for title, artist in ANCHORS:
        candidates = [m for m, (n, a) in meta.items() if n == title and (a or "").startswith(artist) and m in index]
        if not candidates:
            continue
        q = index[max(candidates, key=lambda m: popularity[index[m]])]
        sims = vectors @ vectors[q]
        sims[q] = -np.inf
        order = np.argsort(-sims)[:500]
        same10 = int(sum(artists[j] == artists[q] for j in order[:K]))
        others = [j for j in order if artists[j] not in (artists[q], None)][:5]
        print(f"\n{title} — {artist}  ({same10}/{K} nearest are the same artist). Nearest other artists:")
        for j in others:
            print(f"   {sims[j]:.3f}  {meta[mbids[j]][0]} — {artists[j]}")


if __name__ == "__main__":
    main()
