"""Our own per-song popularity score, from ListenBrainz unique-listener counts.

Spotify's `popularity` field is deprecated, so popularity comes from the same listening data
the embedding was trained on (`models/mbid_popularity.parquet`).
"""

import math

import pyarrow.parquet as pq

from app.core.config import MODELS_DIR

_PARQUET_PATH = MODELS_DIR / "mbid_popularity.parquet"

_counts: dict[str, int] | None = None
_log_max: float = 1.0


def _load() -> None:
    global _counts, _log_max
    if _counts is not None:
        return
    table = pq.read_table(
        str(_PARQUET_PATH), columns=["recording_mbid", "unique_listeners"]
    )
    mbids = table.column("recording_mbid").to_pylist()
    counts = table.column("unique_listeners").to_pylist()
    _counts = dict(zip(mbids, counts))
    _log_max = math.log1p(max(counts)) if counts else 1.0


def get_popularity_scores(mbids: list[str]) -> dict[str, float]:
    """
    Returns {mbid: score} where score ∈ [0, 1].
    Score = log(1 + unique_listeners) / log(1 + global_max), so it follows a
    log relationship and MBIDs absent from the parquet score 0.
    """
    _load()
    assert _counts is not None
    return {
        mbid: math.log1p(_counts.get(mbid, 0)) / _log_max
        for mbid in mbids
    }
