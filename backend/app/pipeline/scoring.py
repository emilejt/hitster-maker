"""Occurrence weight — how strong one player's copy of a song is.

The single scoring primitive the rest of the pipeline builds on: given a track's provenance
(`source` + top-track `rank`), how strongly does that player "own" it. Computed once in
`matching.py` and stored on each `OwnerContribution`; the deck builder (`pipeline/deck.py`)
consumes those weights and never recomputes them.

The fair-selection and fallback-planning steps that used to live here were replaced by the
greedy tiered builder in `pipeline/deck.py`.

The constants below are the tunables the plans keep flagging. Adjust freely.
"""

from __future__ import annotations

from app.pipeline.models import Source

# ── Occurrence weight (TUNABLE) ───────────────────────────────────────────────
# Top-track weight is a rank curve, one per range: f(rank) = 1 / (1 + (rank/r0)^ALPHA).
# rank 0 (strongest) → 1.0; at rank == r0 → exactly 0.5; then it keeps decaying. Each range
# gets a wider r0 the longer its window, because a long-term library runs much deeper — being
# someone's 350th all-time track still says something, whereas a 60th short-term track does.
_TOP_R0: dict[Source, float] = {
    Source.TOP_SHORT: 60.0,
    Source.TOP_MEDIUM: 150.0,
    Source.TOP_LONG: 350.0,
}
_TOP_ALPHA = 1.3  # steepness of the falloff around each range's r0

# Playlist tracks are a flat, weak "they'll recognize it" signal — no rank, no curve, no floor.
_PLAYLIST_WEIGHT = 0.3
# Liked songs are no longer fetched (removed from the Profile Maker for now), but the plumbing
# stays; keep a sensible weight so occurrence_weight is still defined if a LIKED track appears.
_LIKED_WEIGHT = 0.8

# A generated (embedding) song is a weaker signal than a real match, so it counts for less.
# Used by the deck builder's synthetic tier as the base of its estimated contributions.
FALLBACK_WEIGHT = 0.5

# NOTE: `added_at` (when a liked/playlist track was saved) is deliberately NOT used in the
# weight. When a song was added is a poor signal of how well someone knows it — plenty of
# songs saved years ago are still in heavy rotation, and plenty saved yesterday are noise.
# We keep `added_at` on the models as provenance; it just doesn't influence the weight.


def occurrence_weight(source: Source, rank: int | None) -> float:
    """How strong one occurrence of a song is for one player, from its provenance."""
    r0 = _TOP_R0.get(source)
    if r0 is not None:  # a top-track range: rank curve, 0 → 1.0, r0 → 0.5
        r = rank or 0
        return 1.0 / (1.0 + (r / r0) ** _TOP_ALPHA)
    if source == Source.PLAYLIST:
        return _PLAYLIST_WEIGHT
    if source == Source.LIKED:
        return _LIKED_WEIGHT
    return 0.5
