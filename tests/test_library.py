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


def test_a_service_you_watch_it_on_counts_even_if_tmdb_does_not_list_it():
    lib = make_lib()
    assert lib.view("movie:1", TODAY)["on_my_services"] == ["Netflix"]
    # told by hand
    lib.set_watch_service("movie:1", "now tv")
    assert lib.view("movie:1", TODAY)["on_my_services"] == ["Netflix", "Now TV"]
    lib.set_watch_service("movie:1", "Now TV")  # idempotent
    assert lib.data["items"]["movie:1"]["watch_on"] == ["Now TV"]
    lib.set_watch_service("movie:1", "Now TV", present=False)
    assert lib.view("movie:1", TODAY)["on_my_services"] == ["Netflix"]
    # learned from watching (even a short viewing that doesn't count)
    s = session("Dune", minutes=1)
    s["service"] = "Now TV"
    lib.apply_session(s, 600)
    assert lib.view("movie:1", TODAY)["on_my_services"] == ["Netflix", "Now TV"]
    # a service you haven't got isn't "yours"
    lib.set_watch_service("movie:1", "Paramount+")
    assert "Paramount+" not in lib.view("movie:1", TODAY)["on_my_services"]


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


def test_reported_episode_from_iplayer_style_session_updates_progress():
    lib = Library()
    lib.upsert_item("tv", 4242, {"title": "Ghosts", "year": "2021", "seasons": {1: 20}, "first_air_date": "2021-10-07",
                                 "last_aired": {"season": 1, "episode": 20}, "status": "Returning Series"}, {}, NOW)
    sess = {"room": "Living Room", "category": "tv_movies", "service": "BBC iPlayer", "title": "Ghosts US",
            "series_title": None, "season": 1, "episode": 18, "channel": None, "subtitle": "Series 1: 18. Farnsby & B",
            "start": NOW, "end": NOW + timedelta(minutes=25)}
    assert lib.apply_session(sess, 600) == "tv:4242"
    item = lib.data["items"]["tv:4242"]
    assert item["progress"] == {"season": 1, "episode": 18} and item["progress_source"] == "reported"


def test_now_tv_style_title_with_trailing_number_matches_a_show_but_not_a_movie():
    lib = Library()
    lib.upsert_item("tv", 1, {"title": "Last Week Tonight with John Oliver", "year": "2014",
                              "seasons": {12: 30}, "first_air_date": "2014-04-27",
                              "last_aired": {"season": 12, "episode": 24}}, {}, NOW)
    lib.upsert_item("movie", 2, {"title": "Toy Story", "year": "1995", "release_date": "1995-11-22"}, {}, NOW)
    base = {"room": "Bedroom", "category": "tv_movies", "service": "Now TV", "series_title": None,
            "season": None, "episode": None, "channel": None,
            "start": NOW, "end": NOW + timedelta(minutes=35)}
    assert lib.match_session({**base, "title": "Last Week Tonight With John Oliver 24"}) == "tv:1"
    assert lib.match_session({**base, "title": "Toy Story 4"}) is None


def test_now_tv_episode_number_sets_progress_with_inferred_season_and_trakt_can_correct_it():
    lib = Library()
    lib.upsert_item("tv", 1, {"title": "Last Week Tonight with John Oliver", "year": "2014",
                              "seasons": {12: 30, 13: 30}, "first_air_date": "2014-04-27",
                              "last_aired": {"season": 13, "episode": 26}}, {}, NOW)
    sess = {"room": "Bedroom", "category": "tv_movies", "service": "Now TV",
            "title": "Last Week Tonight With John Oliver 24", "series_title": "Last Week Tonight With John Oliver",
            "season": None, "episode": 24, "channel": None, "start": NOW, "end": NOW + timedelta(minutes=35)}
    assert lib.apply_session(sess, 600) == "tv:1"
    item = lib.data["items"]["tv:1"]
    assert item["progress"] == {"season": 13, "episode": 24} and item["progress_source"] == "inferred"
    # Trakt knows better and may correct an inferred value, even backwards
    lib.apply_trakt_event("tv:1", {"media_type": "tv", "season": 13, "episode": 22,
                                   "watched_at": "2026-09-29T20:00:00.000Z"})
    assert lib.data["items"]["tv:1"]["progress"] == {"season": 13, "episode": 22}
    assert lib.data["items"]["tv:1"]["progress_source"] == "trakt"


def _movie_or_show_lib(runtime=None):
    lib = Library()
    details = {"title": "Ghosts", "year": "2021", "seasons": {1: 20}, "first_air_date": "2021-10-07",
               "last_aired": {"season": 1, "episode": 20}}
    if runtime:
        details["episode_runtime"] = runtime
    lib.upsert_item("tv", 7, details, {}, NOW)
    return lib


def _sess(minutes, duration_ms=None, final_pos_ms=None, season=1, episode=3):
    return {"room": "Bedroom", "category": "tv_movies", "service": "BBC iPlayer", "title": "Ghosts",
            "series_title": None, "season": season, "episode": episode, "channel": None,
            "duration_ms": duration_ms, "final_pos_ms": final_pos_ms,
            "start": NOW, "end": NOW + timedelta(minutes=minutes)}


def test_an_episode_counts_only_once_80_percent_watched():
    lib = _movie_or_show_lib()
    length = 30 * 60_000
    # 40% through: matched, but progress is left alone
    assert lib.apply_session(_sess(12, length, int(length * 0.4)), 600) == "tv:7"
    assert lib.data["items"]["tv:7"]["progress"] is None
    # 79% -> no, 80% -> yes
    lib.apply_session(_sess(24, length, int(length * 0.79)), 600)
    assert lib.data["items"]["tv:7"]["progress"] is None
    sess = _sess(24, length, int(length * 0.80))
    lib.apply_session(sess, 600)
    assert lib.data["items"]["tv:7"]["progress"] == {"season": 1, "episode": 3}
    assert sess["_fraction"] == 0.8


def test_resuming_partway_still_counts_because_position_is_absolute():
    lib = _movie_or_show_lib()
    length = 30 * 60_000
    # only 8 minutes of *this* viewing (it used to fail the 10-minute rule), but you finished the episode
    lib.apply_session(_sess(8, length, length), 600)
    assert lib.data["items"]["tv:7"]["progress"] == {"season": 1, "episode": 3}


def test_falls_back_to_tmdb_runtime_then_to_ten_minutes():
    # no length from the TV, but TMDB says 45-minute episodes: 30 min watched is 67% -> not yet
    lib = _movie_or_show_lib(runtime=45)
    lib.apply_session(_sess(30), 600)
    assert lib.data["items"]["tv:7"]["progress"] is None
    lib.apply_session(_sess(37), 600)                       # 82%
    assert lib.data["items"]["tv:7"]["progress"] == {"season": 1, "episode": 3}
    # nothing known about length at all: the old 10-minute rule
    lib = _movie_or_show_lib()
    lib.apply_session(_sess(9), 600)
    assert lib.data["items"]["tv:7"]["progress"] is None
    lib.apply_session(_sess(11), 600)
    assert lib.data["items"]["tv:7"]["progress"] == {"season": 1, "episode": 3}


def test_movies_use_the_same_rule():
    lib = Library()
    lib.upsert_item("movie", 9, {"title": "Dune", "release_date": "2021-10-01", "runtime": 155}, {}, NOW)
    base = {"room": "Living Room", "category": "tv_movies", "service": "Netflix", "title": "Dune",
            "series_title": None, "season": None, "episode": None, "channel": None,
            "start": NOW, "end": NOW + timedelta(minutes=60)}
    lib.apply_session({**base, "duration_ms": 155 * 60_000, "final_pos_ms": 60 * 60_000}, 600)   # 39%
    assert not lib.data["items"]["movie:9"]["watched"]
    lib.apply_session({**base, "duration_ms": 155 * 60_000, "final_pos_ms": 140 * 60_000}, 600)  # 90%
    assert lib.data["items"]["movie:9"]["watched"]
