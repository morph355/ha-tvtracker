from datetime import date, datetime, timedelta, timezone

from tvt.library import Library
from tvt.logic import parse_details, parse_providers, parse_trakt_history
from test_logic import RAW_TV

NOW = datetime(2026, 9, 29, 21, 0, tzinfo=timezone.utc)


def ep_item(id_, tmdb, title, season, number, when, ep_title="Ep"):
    return {"id": id_, "watched_at": when, "action": "watch", "type": "episode",
            "episode": {"season": season, "number": number, "title": ep_title, "ids": {"trakt": 9, "tmdb": 111}},
            "show": {"title": title, "year": 2022, "ids": {"trakt": 1, "slug": "s", "tmdb": tmdb}}}


def movie_item(id_, tmdb, title, when):
    return {"id": id_, "watched_at": when, "action": "watch", "type": "movie",
            "movie": {"title": title, "year": 2021, "ids": {"trakt": 5, "tmdb": tmdb}}}


def test_parse_trakt_history():
    items = [
        ep_item(2, 95396, "Severance", 2, 4, "2026-09-28T20:00:00.000Z", "Woe's Hollow"),
        movie_item(1, 438631, "Dune", "2026-09-27T00:00:00.000Z"),
        ep_item(3, None, "No TMDB id", 1, 1, "2026-09-28T00:00:00.000Z"),      # dropped
        {"id": 4, "type": "season", "watched_at": "2026-09-28T00:00:00.000Z"},  # dropped
        {"id": 5, "type": "episode", "watched_at": "x", "show": {"ids": {"tmdb": 1}}},  # no episode: dropped
    ]
    ev = parse_trakt_history(items)
    assert [e["trakt_id"] for e in ev] == [1, 2]  # oldest first
    assert ev[1] == {"trakt_id": 2, "media_type": "tv", "tmdb_id": 95396, "title": "Severance",
                     "season": 2, "episode": 4, "episode_title": "Woe's Hollow",
                     "watched_at": "2026-09-28T20:00:00.000Z"}
    assert ev[0]["media_type"] == "movie" and ev[0]["tmdb_id"] == 438631
    assert parse_trakt_history(None) == []


def make_lib():
    lib = Library()
    lib.upsert_item("tv", 95396, parse_details("tv", RAW_TV), parse_providers(RAW_TV, "GB"), NOW)
    return lib


def test_trakt_replaces_a_guess_even_backwards_but_not_manual_progress():
    lib = make_lib()
    key = "tv:95396"
    ev = lambda s, e: {"media_type": "tv", "season": s, "episode": e, "watched_at": "2026-09-28T20:00:00.000Z"}
    # our detection guessed S1E5; Trakt says the real episode was S1E4
    lib.set_progress(key, 1, 5, NOW, source="guess")
    lib.apply_trakt_event(key, ev(1, 4))
    assert lib.data["items"][key]["progress"] == {"season": 1, "episode": 4}
    assert lib.data["items"][key]["progress_source"] == "trakt"
    # now Trakt-sourced: a rewatch of an older episode does not move it back
    lib.apply_trakt_event(key, ev(1, 2))
    assert lib.data["items"][key]["progress"] == {"season": 1, "episode": 4}
    # forward is fine
    lib.apply_trakt_event(key, ev(1, 6))
    assert lib.data["items"][key]["progress"] == {"season": 1, "episode": 6}
    # progress you set by hand is never pulled backwards by Trakt
    lib.set_progress(key, 2, 8, NOW, source="manual")
    lib.apply_trakt_event(key, ev(1, 9))
    assert lib.data["items"][key]["progress"] == {"season": 2, "episode": 8}


def test_trakt_movie_marks_watched():
    lib = Library()
    lib.upsert_item("movie", 438631, {"title": "Dune", "release_date": "2021-10-01"}, {}, NOW)
    lib.apply_trakt_event("movie:438631", {"media_type": "movie", "watched_at": "2026-09-27T00:00:00.000Z"})
    assert lib.view("movie:438631", date(2026, 9, 29))["status"] == "finished"


def hist(lib, service, start, title=None, source="auto"):
    return lib.add_history({"start": start, "end": start + timedelta(minutes=40), "room": "Living Room",
                            "category": "tv_movies", "service": service, "title": title,
                            "item_key": None, "source": source})


def test_attach_history_title_matches_service_and_day_once():
    lib = make_lib()  # Severance is on Apple TV in RAW_TV
    hist(lib, "Apple TV", NOW - timedelta(hours=3))
    hist(lib, "Apple TV", NOW - timedelta(hours=30))
    other = hist(lib, "Netflix", NOW - timedelta(hours=3))          # wrong service
    titled = hist(lib, "Apple TV", NOW - timedelta(hours=2), title="Known")  # already has a title
    manual = hist(lib, "Apple TV", NOW - timedelta(hours=1), source="manual")
    ev = {"season": 2, "episode": 4, "episode_title": "Woe's Hollow", "watched_at": "2026-09-29T00:00:00.000Z"}
    assert lib.attach_history_title("tv:95396", ev)
    rows = {h["id"]: h for h in lib.data["history"]}
    claimed = [h for h in rows.values() if h["item_key"]]
    assert len(claimed) == 1 and claimed[0]["title"] == "Severance" and claimed[0]["season"] == 2
    assert rows[other["id"]]["item_key"] is None and rows[titled["id"]]["title"] == "Known"
    assert rows[manual["id"]]["item_key"] is None
    # a second watch claims the next untitled viewing, not the same one
    assert lib.attach_history_title("tv:95396", {**ev, "episode": 5})
    assert len([h for h in lib.data["history"] if h["item_key"]]) == 2
    # nothing left within a day of a far-away watch
    assert not lib.attach_history_title("tv:95396", {**ev, "watched_at": "2026-08-01T00:00:00.000Z"})
    assert not lib.attach_history_title("tv:95396", {**ev, "watched_at": "garbage"})
