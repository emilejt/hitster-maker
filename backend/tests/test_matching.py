"""Occurrence weights and ISRC matching: the inputs every deck decision is built on."""

import pytest

from app.pipeline.matching import detect_matches
from app.pipeline.models import PlayerLibrary, Source
from app.pipeline.scoring import occurrence_weight
from tests.conftest import library, track


@pytest.mark.parametrize(
    "source, r0", [(Source.TOP_SHORT, 60), (Source.TOP_MEDIUM, 150), (Source.TOP_LONG, 350)]
)
def test_top_track_rank_curve(source, r0):
    # rank 0 is full strength, rank r0 is exactly half, and strength only ever decays.
    assert occurrence_weight(source, 0) == 1.0
    assert occurrence_weight(source, r0) == pytest.approx(0.5)
    weights = [occurrence_weight(source, r) for r in range(0, 1000, 25)]
    assert weights == sorted(weights, reverse=True)


def test_playlist_weight_is_flat():
    assert occurrence_weight(Source.PLAYLIST, None) == occurrence_weight(Source.PLAYLIST, 500)


def test_only_songs_in_two_libraries_are_shared():
    matches = detect_matches([library("a", ["X", "Y"]), library("b", ["Y", "Z"])])
    shared = {isrc for isrc, m in matches.items() if len(m.owners) >= 2}
    assert shared == {"Y"}
    assert set(matches["Y"].owners) == {"a", "b"}


def test_a_player_counts_once_with_their_strongest_copy():
    # Same recording as a weak playlist entry and a #1 short-term top track.
    lib = PlayerLibrary("a", "a", [track("X", source=Source.PLAYLIST), track("X", rank=0, source=Source.TOP_SHORT)])
    owner = detect_matches([lib])["X"].owners["a"]
    assert owner.source == Source.TOP_SHORT
    assert owner.weight == 1.0


def test_tracks_without_isrc_are_ignored_and_year_is_preferred():
    no_isrc = track("X")
    no_isrc.isrc = None
    a = PlayerLibrary("a", "a", [no_isrc, track("Y", year=None)])
    b = PlayerLibrary("b", "b", [track("Y", year=1999)])
    matches = detect_matches([a, b])
    assert set(matches) == {"Y"}
    # The game needs a release year on every card, so the representative copy must have one.
    assert matches["Y"].track.year == 1999
