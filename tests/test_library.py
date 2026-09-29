from datetime import date, datetime, timedelta

import pytest

from tvt.library import Library
from tvt.logic import parse_details, parse_providers
from test_logic import RAW_TV

TODAY = date(2026, 9, 29)
NOW = datetime(2026, 9, 29, 21, 0)


def make_lib():
    lib = Library()
    lib.upsert_item("tv", 95396, parse_details("tv", RAW_TV), parse_providers(RAW_TV, "GB"), NOW)
    lib.upsert_item("movie", 1, {"title": "Dune", "year": "2021", "release_date": "2021-10-01"}, {"flatrate": ["Netflix"]}, NOW)
    return lib


def session(title=None, series=None, season=None, episode=None, minutes=45, category="tv_movies"):
    return {"room": "Bedroom", "category": category, "service": "Apple TV", "title": title,
            "series_title": series, "season": season, "episode": episode, "channel": None,
            "start": NOW, "end": NOW + timedelta(minutes=minutes)}


def test_lists():
    lib = make_lib()
    a = lib.create_list("Our Shows")
    assert lib.resolve_list("our shows") == a
    with pytest.raises(ValueError):
        lib.create_list("OUR SHOWS")
    with pytest.raises(ValueError):
        lib.resolve_list("nope")
    lib.add_to_list("Our Shows", "tv:95396")
    lib.add_to_list(a, "tv:95396")  # idempotent
    b = lib.create_list("Movie Night")
    lib.add_to_list(b, "tv:95396")
    lib.add_to_list(b, "movie:1")
    wl = lib.watchlists(TODAY)
    assert sorted(v["title"] for v in wl["Movie Night"]) == ["Dune", "Severance"]
    assert len(wl["Our Shows"]) == 1
    lib.remove_from_list(b, "movie:1")
    assert len(lib.watchlists(TODAY)["Movie Night"]) == 1
    lib.delete_list("Movie Night")
    assert lib.data["items"]["tv:95396"]["lists"] == [a]


def test_progress_only_forward():
    lib = make_lib()
    assert lib.set_progress("tv:95396", 1, 5)
    assert not lib.set_progress("tv:95396", 1, 3, only_forward=True)
    assert lib.data["items"]["tv:95396"]["progress"] == {"season": 1, "episode": 5}
    assert lib.set_progress("tv:95396", 2, 1, only_forward=True)
    assert lib.set_progress("tv:95396", 1, 1)  # explicit set can go back
    with pytest.raises(ValueError):
        lib.set_progress("movie:1", 1, 1)


def test_mark_watched():
    lib = make_lib()
    lib.mark_watched("movie:1", True, NOW)
    assert lib.view("movie:1", TODAY)["status"] == "finished"
    lib.mark_watched("tv:95396", True, NOW)
    assert lib.data["items"]["tv:95396"]["progress"] == {"season": 2, "episode": 10}
    lib.mark_watched("movie:1", False)
    assert lib.view("movie:1", TODAY)["status"] == "want_to_watch"


def test_apply_session_marks_watchlist_items():
    lib = make_lib()
    lib.add_to_list(lib.create_list("L"), "tv:95396")
    # reported season/episode
    assert lib.apply_session(session("Chapter 4", "Severance", 1, 4), 600) == "tv:95396"
    assert lib.data["items"]["tv:95396"]["progress"] == {"season": 1, "episode": 4}
    # only the show title: advance to the next episode
    lib.apply_session(session("Severance"), 600)
    assert lib.data["items"]["tv:95396"]["progress"] == {"season": 1, "episode": 5}
    # too short: matched but not counted
    lib.apply_session(session("Severance", minutes=5), 600)
    assert lib.data["items"]["tv:95396"]["progress"] == {"season": 1, "episode": 5}
    # movie
    assert lib.apply_session(session("Dune"), 600) == "movie:1"
    assert lib.view("movie:1", TODAY)["status"] == "finished"
    # unknown / youtube
    assert lib.apply_session(session("Something else"), 600) is None
    assert lib.apply_session(session("Severance", category="youtube"), 600) is None


def test_views_and_continue_watching():
    lib = make_lib()
    lib.set_progress("tv:95396", 1, 3, NOW)
    cw = lib.continue_watching(TODAY)
    assert [v["title"] for v in cw] == ["Severance"]
    v = cw[0]
    assert v["status"] == "watching" and v["progress"] == "S1E3" and v["next"] == "S1E4"
    assert v["availability"] == "Watch on Apple TV"


def test_services_and_history():
    lib = make_lib()
    assert lib.add_service("Paramount+")
    assert not lib.add_service("Paramount+")  # duplicate
    assert lib.add_service("Amazon Prime") is False  # already present as Prime Video
    assert lib.remove_service("ITV Player")
    assert "ITVX" not in lib.data["services"]

    e1 = lib.add_history({"start": NOW, "end": NOW, "category": "youtube", "title": "v"})
    lib.add_history({"start": NOW - timedelta(days=1), "end": NOW, "category": "tv_movies", "title": "old"})
    assert [h["title"] for h in lib.recent_history("youtube")] == ["v"]
    assert [h["title"] for h in lib.recent_history("tv_movies")] == ["old"]
    assert [h["title"] for h in lib.recent_history(None)] == ["v", "old"]
    assert lib.delete_history(e1["id"]) and not lib.delete_history(e1["id"])
