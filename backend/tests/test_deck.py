"""End-to-end behaviour of the greedy, tiered deck builder on small synthetic groups."""

import asyncio

import numpy as np
import pytest

from app.pipeline import deck
from app.pipeline.models import ClusterInfo, PlayerClusters
from app.services import bridge_service, embedding_service
from tests.conftest import library

SHARED = [f"S{i:02d}" for i in range(40)]


def build(libraries, clusters=None, **kwargs):
    return asyncio.run(deck.build_deck(libraries, clusters or {}, resolve_token=None, **kwargs))


def isrcs(cards):
    return {c.track.isrc for c in cards}


def test_enough_shared_songs_gives_a_full_fair_tier1_deck():
    cards, diag = build([library("a", SHARED), library("b", SHARED)])

    assert len(cards) == deck.SOFT_SIZE_PER_PLAYER * 2
    assert all(c.tier == 1 for c in cards)
    assert len(isrcs(cards)) == len(cards)  # no duplicates
    assert diag["exit_reason"] == "fair past soft size"
    assert diag["final_gap"] <= deck.TOLERANCE


def test_played_songs_are_never_dealt_again():
    played = set(SHARED[:10])
    cards, _ = build(
        [library("a", SHARED), library("b", SHARED)],
        exclusion_groups=[deck.ExclusionGroup("s1", started_at=1.0, isrcs=played)],
    )
    assert not isrcs(cards) & played


def test_exclusions_that_starve_the_deck_are_relaxed_oldest_first():
    old = deck.ExclusionGroup("old", started_at=1.0, isrcs=set(SHARED[:20]))
    new = deck.ExclusionGroup("new", started_at=2.0, isrcs=set(SHARED[20:25]))
    # Honouring both leaves 15 songs, below the 80% fill threshold of a 30-card deck.
    cards, diag = build([library("a", SHARED), library("b", SHARED)], exclusion_groups=[new, old])

    assert diag["relaxed"]
    assert diag["excluded_sessions"] == ["new"]  # the older night came back, the recent one didn't
    assert not isrcs(cards) & new.isrcs
    assert len(cards) == deck.SOFT_SIZE_PER_PLAYER * 2


def test_play_again_carries_over_unplayed_songs():
    # The weakest shared songs (highest ranks) would normally lose to stronger ones.
    leftovers = {f"sp-{isrc}" for isrc in SHARED[-5:]}
    cards, _ = build([library("a", SHARED), library("b", SHARED)], seed_spotify_ids=leftovers)
    assert leftovers <= {c.track.spotify_id for c in cards}


@pytest.fixture
def fake_embedding(monkeypatch):
    """Every song is in the embedding and sits at cosine 0.8 from every cluster centroid."""
    monkeypatch.setattr(bridge_service, "get_mbids_for_isrcs", lambda xs: {x: f"mbid-{x}" for x in xs})
    monkeypatch.setattr(embedding_service, "is_in_model", lambda mbid: True)
    monkeypatch.setattr(
        embedding_service, "cosine_mbids_against_vector", lambda mbids, v: {m: 0.8 for m in mbids}
    )


def test_bridge_tier_fills_the_deck_when_nobody_shares_anything(fake_embedding):
    a = library("a", [f"A{i:02d}" for i in range(30)])
    b = library("b", [f"B{i:02d}" for i in range(30)])
    centroid = np.zeros(128)
    clusters = {p: PlayerClusters(p, [ClusterInfo(0, centroid)]) for p in ("a", "b")}

    cards, diag = build([a, b], clusters)

    assert cards and all(c.tier == 2 for c in cards)
    # Each bridge card credits its real owner plus an estimate for the other player.
    assert all(len(c.owners) == 2 for c in cards)
    assert {c.track.isrc[0] for c in cards} == {"A", "B"}  # both players' songs get used
    assert diag["final_gap"] <= deck.TOLERANCE


def test_synthetic_tier_stays_off():
    # No shared songs and no bridge candidates: the deck comes out empty rather than inventing
    # songs nobody owns, while tier 3 is disabled.
    assert not deck.TIER3_ENABLED
    cards, diag = build([library("a", ["A1"]), library("b", ["B1"])])
    assert cards == []
    assert diag["tier_counts"][3] == 0
