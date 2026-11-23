"""Shared fixtures. Tests never touch the real model artifacts, the bridge DB or Spotify."""

import pytest

from app.pipeline.models import PlayerLibrary, Source, SourcedTrack


def track(isrc: str, rank: int = 0, source: Source = Source.TOP_MEDIUM, year: int | None = 2000):
    """A library track whose Spotify id is derived from its ISRC (unique per song)."""
    return SourcedTrack(
        isrc=isrc,
        spotify_id=f"sp-{isrc}",
        spotify_uri=f"spotify:track:sp-{isrc}",
        name=f"Song {isrc}",
        artist="Artist",
        album_art=None,
        year=year,
        source=source,
        rank=rank,
    )


def library(player_id: str, isrcs: list[str], rank_offset: int = 0) -> PlayerLibrary:
    tracks = [track(isrc, rank=rank_offset + i) for i, isrc in enumerate(isrcs)]
    return PlayerLibrary(player_id=player_id, username=player_id, tracks=tracks)


@pytest.fixture(autouse=True)
def no_bridge(monkeypatch):
    """By default no song resolves to the embedding, so the bridge tier is empty. Tests that
    exercise the embedding tiers patch these again with something useful."""
    from app.services import bridge_service

    monkeypatch.setattr(bridge_service, "get_mbids_for_isrcs", lambda isrcs: {})
