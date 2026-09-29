from datetime import date, datetime, timedelta

from tvt import logic
from tvt.logic import (
    Observation,
    RoomTracker,
    availability,
    canonical_service,
    classify,
    derive_status,
    next_episode,
    observe,
    parse_details,
    parse_media_sessions,
    parse_providers,
)

TODAY = date(2026, 9, 29)


def test_canonical_service():
    assert canonical_service("Amazon Prime") == "Prime Video"
    assert canonical_service("Disney Plus") == "Disney+"
    assert canonical_service("ITV Player") == "ITVX"
    assert canonical_service("Apple TV+") == "Apple TV"
    assert canonical_service("Max") == "Max"


def test_classify():
    assert classify("com.google.android.youtube.tv", "com.google.android.youtube.tv") == ("youtube", "YouTube")
    assert classify("2C6A6E3D", "YouTube") == ("youtube", "YouTube")
    assert classify("com.google.android.apps.youtube.music", None) == (None, None)
    assert classify("AndroidNativeApp", "Spotify") == (None, None)
    assert classify("com.netflix.ninja", "Netflix") == ("tv_movies", "Netflix")
    assert classify("air.uk.co.bbc.android.mediacentre", "com.x") == ("tv_movies", "BBC iPlayer")
    assert classify("com.google.android.tvlauncher", None) == (None, None)
    assert classify(None, None) == (None, None)
    # unknown streaming app -> named after the app
    assert classify("com.foo.paramountplus", "Paramount+") == ("tv_movies", "Paramount+")
    assert classify("com.foo.mubi", "com.foo.mubi") == ("tv_movies", "Mubi")


def test_observe_shield_combines_cast_title_and_adb_app():
    cast = {"state": "playing", "attributes": {"app_id": "2C6A6E3D", "app_name": "YouTube",
            "media_title": "A video", "media_artist": "BBC News"}}
    adb = {"state": "on", "attributes": {"app_id": "com.google.android.youtube.tv",
           "app_name": "com.google.android.youtube.tv"}}
    obs = observe([cast, adb])
    assert obs == Observation("youtube", "YouTube", title="A video", channel="BBC News")


def test_observe_ignores_stale_cast_title_when_idle():
    cast = {"state": "idle", "attributes": {"app_name": "YouTube", "media_title": "Old"}}
    adb = {"state": "on", "attributes": {"app_id": "com.google.android.youtube.tv"}}
    obs = observe([cast, adb])
    assert obs.service == "YouTube" and obs.title is None


def test_observe_ignores_stale_title_from_a_different_app():
    """Real case: Disney+ in the foreground (ADB) while the Cast entity still
    holds a paused Spotify podcast. The podcast title must not leak in."""
    cast = {"state": "paused", "attributes": {
        "app_id": "AndroidNativeApp", "app_name": "Spotify",
        "media_title": "S13 EP21: Sneaky Sasquatch ",
        "media_artist": "Parenting Hell with Rob Beckett and Josh Widdicombe"}}
    adb = {"state": "on", "attributes": {
        "app_id": "com.disney.disneyplus", "app_name": "com.disney.disneyplus"}}
    obs = observe([cast, adb])
    assert obs == Observation("tv_movies", "Disney+")  # app known, no title
    # ...and the same for a stale title from another *streaming* app
    cast["attributes"] = {"app_name": "Netflix", "media_title": "Old show"}
    assert observe([cast, adb]).title is None


def test_observe_ignores_music_and_off():
    spotify = {"state": "paused", "attributes": {"app_id": "AndroidNativeApp", "app_name": "Spotify",
               "media_title": "song"}}
    assert observe([spotify]) is None
    assert observe([{"state": "off", "attributes": {}}]) is None
    assert observe([{"state": "unavailable", "attributes": {}}]) is None


def test_observe_tv_episode_attributes():
    st = {"state": "playing", "attributes": {"app_name": "Netflix", "media_title": "Chapter 2",
          "media_series_title": "Severance", "media_season": "2", "media_episode": 4}}
    obs = observe([st])
    assert (obs.series_title, obs.season, obs.episode) == ("Severance", 2, 4)


def t(minutes):
    return datetime(2026, 9, 29, 20, 0) + timedelta(minutes=minutes)


def test_room_tracker_session_lifecycle():
    rt = RoomTracker("Bedroom")
    netflix = Observation("tv_movies", "Netflix", title="Severance")
    assert rt.update(netflix, t(0)) == []
    assert rt.update(netflix, t(5)) == []  # unchanged
    closed = rt.update(None, t(45))
    assert len(closed) == 1
    assert closed[0]["service"] == "Netflix"
    assert closed[0]["end"] - closed[0]["start"] == timedelta(minutes=45)
    assert rt.update(None, t(50)) == []


def test_room_tracker_title_fills_in_and_app_switch_splits():
    rt = RoomTracker("Living Room")
    rt.update(Observation("youtube", "YouTube"), t(0))
    rt.update(Observation("youtube", "YouTube", title="Video", channel="Chan"), t(1))
    assert rt.current["title"] == "Video" and rt.current["channel"] == "Chan"
    # title vanishing (cast idle) keeps the session
    assert rt.update(Observation("youtube", "YouTube"), t(2)) == []
    # new video -> new session
    closed = rt.update(Observation("youtube", "YouTube", title="Video 2"), t(20))
    assert [c["title"] for c in closed] == ["Video"]
    # app switch -> new session
    closed = rt.update(Observation("tv_movies", "Netflix", title="X"), t(30))
    assert [c["title"] for c in closed] == ["Video 2"]
    assert rt.close(t(40))["service"] == "Netflix"


RAW_TV = {
    "name": "Severance",
    "first_air_date": "2022-02-18",
    "status": "Returning Series",
    "poster_path": "/p.jpg",
    "seasons": [
        {"season_number": 0, "episode_count": 5},
        {"season_number": 1, "episode_count": 9},
        {"season_number": 2, "episode_count": 10},
    ],
    "last_episode_to_air": {"season_number": 2, "episode_number": 10},
    "next_episode_to_air": None,
    "watch/providers": {"results": {"GB": {
        "flatrate": [{"provider_name": "Apple TV+"}],
        "rent": [{"provider_name": "Amazon Video"}]}}},
}


def tv_item(progress=None, **detail_overrides):
    details = parse_details("tv", RAW_TV)
    details.update(detail_overrides)
    return {"media_type": "tv", "title": "Severance", "progress": progress, "watched": False,
            "details": details, "providers": parse_providers(RAW_TV, "GB")}


def test_parse_details_and_providers():
    d = parse_details("tv", RAW_TV)
    assert d["seasons"] == {1: 9, 2: 10}  # specials dropped
    assert d["last_aired"] == {"season": 2, "episode": 10}
    assert d["poster"].endswith("/p.jpg")
    assert parse_providers(RAW_TV, "GB")["flatrate"] == ["Apple TV"]
    assert parse_providers(RAW_TV, "US")["flatrate"] == []


def test_next_episode():
    d = parse_details("tv", RAW_TV)
    assert next_episode(d, None) == (1, 1)
    assert next_episode(d, {"season": 1, "episode": 4}) == (1, 5)
    assert next_episode(d, {"season": 1, "episode": 9}) == (2, 1)
    assert next_episode(d, {"season": 2, "episode": 10}) is None


def test_derive_status_tv():
    assert derive_status(tv_item(), TODAY) == "want_to_watch"
    assert derive_status(tv_item({"season": 1, "episode": 3}), TODAY) == "watching"
    assert derive_status(tv_item({"season": 2, "episode": 10}), TODAY) == "caught_up"
    assert derive_status(tv_item({"season": 2, "episode": 10}, status="Ended"), TODAY) == "finished"
    assert derive_status(tv_item(first_air_date="2027-01-01"), TODAY) == "upcoming"
    assert derive_status(tv_item(first_air_date=None), TODAY) == "upcoming"


def test_derive_status_movie():
    movie = {"media_type": "movie", "title": "M", "watched": False, "details": {"release_date": "2026-01-01"}}
    assert derive_status(movie, TODAY) == "want_to_watch"
    assert derive_status({**movie, "watched": True}, TODAY) == "finished"
    assert derive_status({**movie, "details": {"release_date": "2027-03-01"}}, TODAY) == "upcoming"


def test_availability():
    mine = ["Netflix", "Apple TV+"]
    a = availability(tv_item(), mine, TODAY)
    assert a["text"] == "Watch on Apple TV" and a["on_my_services"] == ["Apple TV"]

    a = availability(tv_item(), ["Netflix"], TODAY)
    assert a["text"] == "Not on your services (on Apple TV)"

    item = tv_item()
    item["providers"] = {"rent": ["Amazon Video"], "buy": ["Amazon Video", "Apple TV"]}
    assert availability(item, mine, TODAY)["text"] == "Rent or buy only (Amazon Video, Apple TV)"


def test_availability_not_available_yet():
    item = tv_item(first_air_date="2026-12-01", last_aired=None, next_air_date="2026-12-01")
    item["providers"] = {}
    assert availability(item, [], TODAY)["text"] == "Not available yet (due 1 Dec 2026)"
    old = tv_item()
    old["providers"] = {}
    assert availability(old, [], TODAY)["text"] == "Not currently available to stream"


def test_match_score():
    assert logic.match_score("Severance", "Severance", "Chapter 1")
    assert logic.match_score("The Bear", None, "Bear")
    assert logic.match_score("Severance", None, "Severance - S2E4")
    assert logic.match_score("Severance", None, "Severance: The Chair")
    assert not logic.match_score("Up", None, "Upload")
    assert not logic.match_score("Up", None, "Up Here")


# Real output from the SHIELD (`dumpsys media_session`) while Disney+ played
# Welcome to Wrexham, with a stale paused Spotify podcast and an idle Netflix.
ADB_TEXT = """package=com.disney.disneyplus
      state=PlaybackState {state=3, position=234528, buffered position=0, speed=1.0, updated=5381215743, actions=879, custom actions=[], active item id=-1, error=null}
      metadata: size=3, description=Welcome to Wrexham, null, null
      package=com.spotify.tv.android
      state=PlaybackState {state=2, position=2647093, buffered position=0, speed=0.0, updated=5366468773, actions=7319548, custom actions=[], active item id=0, error=null}
      metadata: size=21, description=S13 EP21: Sneaky Sasquatch , Parenting Hell with Rob Beckett and Josh Widdicombe, null
      package=com.netflix.ninja
      metadata: null"""


def test_parse_media_sessions_real_output():
    disney, spotify, netflix = parse_media_sessions(ADB_TEXT)
    assert disney == {"package": "com.disney.disneyplus", "state": 3, "position_ms": 234528,
                      "title": "Welcome to Wrexham", "subtitle": None}
    assert spotify["state"] == 2 and spotify["title"] == "S13 EP21: Sneaky Sasquatch"
    assert spotify["subtitle"].startswith("Parenting Hell")
    assert netflix == {"package": "com.netflix.ninja", "state": None, "position_ms": None,
                       "title": None, "subtitle": None}
    assert parse_media_sessions(None) == [] and parse_media_sessions("") == []


def test_parse_media_sessions_title_with_comma():
    text = "package=a.b\n  state=PlaybackState {state=3, position=5, x}\n  metadata: size=3, description=Hello, World, null, null"
    assert parse_media_sessions(text)[0]["title"] == "Hello, World"


def test_observe_fills_title_from_adb_media_session():
    """Disney+ is the foreground app but only the media session has the title."""
    remote = {"state": "on", "attributes": {"app_id": "com.disney.disneyplus",
              "app_name": "com.disney.disneyplus"}}
    stale_cast = {"state": "paused", "attributes": {"app_name": "Spotify", "app_id": "AndroidNativeApp",
                  "media_title": "S13 EP21: Sneaky Sasquatch "}}
    obs = observe([remote, stale_cast], parse_media_sessions(ADB_TEXT))
    assert (obs.service, obs.title, obs.position_ms) == ("Disney+", "Welcome to Wrexham", 234528)
    # the stale Spotify session must never be picked up for Disney+
    assert observe([remote], [s for s in parse_media_sessions(ADB_TEXT) if s["package"] != "com.disney.disneyplus"]).title is None
    # paused Disney+ session still counts; a stopped/none one does not
    paused = [{**parse_media_sessions(ADB_TEXT)[0], "state": 2}]
    assert observe([remote], paused).title == "Welcome to Wrexham"
    stopped = [{**parse_media_sessions(ADB_TEXT)[0], "state": 1}]
    assert observe([remote], stopped).title is None


def test_cast_title_wins_over_adb_and_youtube_still_works():
    cast = {"state": "playing", "attributes": {"app_id": "2C6A6E3D", "app_name": "YouTube",
            "media_title": "A video", "media_artist": "BBC News"}}
    adb = {"state": "playing", "attributes": {"app_id": "com.google.android.youtube.tv", "app_name": "YouTube"}}
    sessions = [{"package": "com.google.android.youtube.tv", "state": 3, "position_ms": 10,
                 "title": "Something else", "subtitle": None}]
    obs = observe([cast, adb], sessions)
    assert obs.title == "A video" and obs.channel == "BBC News"


def test_room_tracker_detects_back_to_back_episodes():
    rt = RoomTracker("Living Room")
    ep = lambda pos: Observation("tv_movies", "Disney+", title="Welcome to Wrexham", position_ms=pos)
    rt.update(ep(5_000), t(0))
    rt.update(ep(1_500_000), t(25))            # well into episode 1
    assert rt.update(ep(1_600_000), t(26)) == []
    closed = rt.update(ep(3_000), t(45))       # position reset: episode 2 started
    assert len(closed) == 1 and closed[0]["title"] == "Welcome to Wrexham"
    assert rt.current["start"] == t(45)
    # pausing and resuming (position keeps increasing) is one session
    rt.update(ep(200_000), t(48))
    assert rt.update(ep(210_000), t(60)) == []
    # restarting right at the start of an episode you'd barely watched is NOT a new episode
    rt2 = RoomTracker("x")
    rt2.update(ep(90_000), t(0))
    assert rt2.update(ep(2_000), t(3)) == []
