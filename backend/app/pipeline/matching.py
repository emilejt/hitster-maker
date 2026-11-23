"""Cross-player match detection (the deck's tier 1).

Pure ISRC set-intersection across players' libraries. No embedding, no bridge, no MBID —
the ISRC comes straight from Spotify's payload during extraction. Produces, per song, the
set of players who have it and each owner's strongest occurrence.
"""

from __future__ import annotations

from app.core.logging import get_logger
from app.pipeline.models import MatchedSong, OwnerContribution, PlayerLibrary, SourcedTrack
from app.pipeline.scoring import occurrence_weight

log = get_logger("pipeline.matching")


def detect_matches(libraries: list[PlayerLibrary]) -> dict[str, MatchedSong]:
    """Map ISRC → MatchedSong. Includes single-owner songs too; callers filter to
    shared (>=2 owners) for the match pool."""
    matches: dict[str, MatchedSong] = {}

    for lib in libraries:
        # A player may have the same ISRC from several sources — keep their strongest.
        strongest: dict[str, tuple[float, SourcedTrack]] = {}
        for t in lib.tracks:
            if not t.isrc:
                continue
            w = occurrence_weight(t.source, t.rank)
            prev = strongest.get(t.isrc)
            if prev is None or w > prev[0]:
                strongest[t.isrc] = (w, t)

        for isrc, (weight, track) in strongest.items():
            m = matches.get(isrc)
            if m is None:
                m = MatchedSong(isrc=isrc, track=track)
                matches[isrc] = m
            m.owners[lib.player_id] = OwnerContribution(
                source=track.source, weight=weight, added_at=track.added_at, rank=track.rank
            )
            # Prefer a representative track that actually has a release year (game needs it).
            if m.track.year is None and track.year is not None:
                m.track = track

    shared = sum(1 for m in matches.values() if len(m.owners) >= 2)
    log.info("matching: %d distinct songs across %d players, %d shared by >=2",
             len(matches), len(libraries), shared)
    return matches
