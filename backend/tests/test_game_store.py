"""Play history: what makes "skip the songs we heard last time" work across game nights."""

import pytest

from app.services import game_store


@pytest.fixture(autouse=True)
def temp_db(monkeypatch, tmp_path):
    monkeypatch.setattr(game_store, "_DB_PATH", tmp_path / "hitster.db")
    monkeypatch.setattr(game_store, "_conn", None)
    yield
    if game_store._conn is not None:
        game_store._conn.close()


def test_plays_are_recorded_once_per_session():
    sid = game_store.create_session([("u1", "Ana"), ("u2", "Ben")])
    song = {"spotify_id": "sp1", "isrc": "ISRC1", "name": "Song", "artist": "X", "year": 1999}
    game_store.record_play(sid, song)
    game_store.record_play(sid, song)  # the client fires and forgets; replays must not duplicate

    played = game_store.get_played_by_session([sid])
    assert played[sid] == {"spotify_ids": {"sp1"}, "isrcs": {"ISRC1"}}
    [session] = game_store.get_sessions_for_users(["u1"])
    assert session["song_count"] == 1
    assert sorted(session["players"]) == ["Ana", "Ben"]


def test_history_follows_players_not_groups():
    trio = game_store.create_session([("u1", "Ana"), ("u2", "Ben"), ("u3", "Cy")])
    game_store.create_session([("u4", "Dee")])

    # A new group containing any past player sees that player's sessions...
    assert [s["session_id"] for s in game_store.get_sessions_for_users(["u3", "u9"])] == [trio]
    # ...and strangers see nothing.
    assert game_store.get_sessions_for_users(["u9"]) == []
