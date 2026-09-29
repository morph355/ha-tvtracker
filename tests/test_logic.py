from datetime import date, datetime, timedelta, timezone

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
    describe_candidate,
    find_episode_by_title,
    infer_season,
    parse_episode_label,
    parse_media_sessions,
    rank_episode_candidates,
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
    assert obs == Observation("youtube", "YouTube", title="A video", channel="BBC News", playing=True)


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


def test_parse_episode_label():
    assert parse_episode_label("Series 1: 18. Farnsby & B") == (1, 18)      # BBC iPlayer, real
    assert parse_episode_label("S13 EP21: Sneaky Sasquatch ") == (13, 21)   # the Spotify podcast style
    assert parse_episode_label("S2:E4 Woe's Hollow") == (2, 4)
    assert parse_episode_label("S2 E4") == (2, 4)
    assert parse_episode_label("Season 2, Episode 4") == (2, 4)
    assert parse_episode_label("Series 12: 3. The One") == (12, 3)
    for none in (None, "", "Farnsby & B", "Sunday 8pm", "Series 1", "Episode 4"):
        assert parse_episode_label(none) is None


# Real output from the SHIELD while BBC iPlayer played Ghosts US.
IPLAYER_TEXT = """package=bbc.iplayer.android
      state=PlaybackState {state=3, position=5921, buffered position=57600, speed=1.0, updated=5387213341, actions=1049423, custom actions=[], active item id=-1, error=null}
      metadata: size=4, description=Ghosts US, Series 1: 18. Farnsby & B, null
      package=com.spotify.tv.android
      state=PlaybackState {state=2, position=2647093, buffered position=0, speed=0.0, updated=5366468773, actions=7319548, custom actions=[], active item id=0, error=null}
      metadata: size=21, description=S13 EP21: Sneaky Sasquatch , Parenting Hell with Rob Beckett and Josh Widdicombe, null"""


def test_observe_reads_season_and_episode_from_iplayer_subtitle():
    remote = {"state": "on", "attributes": {"app_id": "bbc.iplayer.android", "app_name": "bbc.iplayer.android"}}
    cast = {"state": "playing", "attributes": {"app_id": "AndroidNativeApp", "app_name": "BBC iPlayer",
            "media_title": "Ghosts US"}}
    adb = {"state": "playing", "attributes": {"app_id": "bbc.iplayer.android", "app_name": "bbc.iplayer.android"}}
    obs = observe([remote, cast, adb], parse_media_sessions(IPLAYER_TEXT))
    assert (obs.service, obs.title, obs.season, obs.episode) == ("BBC iPlayer", "Ghosts US", 1, 18)
    assert obs.subtitle == "Series 1: 18. Farnsby & B"
    # the Spotify podcast's "S13 EP21" must never be read as iPlayer's episode
    assert obs.season != 13
    # a subtitle for a *different* programme than the entities named is not trusted
    cast["attributes"]["media_title"] = "Something Else"
    assert observe([remote, cast, adb], parse_media_sessions(IPLAYER_TEXT)).season is None


def test_match_score_country_suffix():
    assert logic.match_score("Ghosts", None, "Ghosts US")
    assert logic.match_score("Ghosts", "Ghosts (UK)", None)
    assert logic.match_score("Ghosts US", None, "Ghosts")
    assert not logic.match_score("Ghosts", None, "Ghost")
    assert logic.match_score("Us", None, "Us")  # an exact title is unaffected by the suffix rule


def test_now_tv_app_is_recognised():
    """Real case: the Bedroom TV's Cast entity reports the app as just "NOW"."""
    assert classify("AndroidNativeApp", "NOW") == ("tv_movies", "Now TV")
    st = {"state": "playing", "attributes": {"app_id": "AndroidNativeApp", "app_name": "NOW",
          "media_title": "Last Week Tonight With John Oliver 24", "media_duration": 2379}}
    obs = observe([st])
    assert (obs.service, obs.title) == ("Now TV", "Last Week Tonight With John Oliver 24")
    # other, unrelated things still aren't mistaken for Now TV
    assert classify("AndroidNativeApp", "LiveTV") == (None, None)
    assert classify("com.snowplow.app", "Snow") != ("tv_movies", "Now TV")


def test_match_score_trailing_episode_number_for_tv_only():
    show = "Last Week Tonight with John Oliver"
    assert logic.match_score(show, None, "Last Week Tonight With John Oliver 24", allow_trailing_number=True)
    assert not logic.match_score(show, None, "Last Week Tonight With John Oliver 24")  # off by default
    # a movie sequel must not match the original
    assert not logic.match_score("Toy Story", None, "Toy Story 4", allow_trailing_number=False)
    assert logic.match_score("Toy Story", None, "Toy Story 4", allow_trailing_number=True)  # only ever enabled for TV items


def test_now_tv_trailing_number_is_the_episode():
    st = {"state": "playing", "attributes": {"app_id": "AndroidNativeApp", "app_name": "NOW",
          "media_title": "Last Week Tonight With John Oliver 24"}}
    obs = observe([st])
    assert (obs.series_title, obs.episode, obs.season) == ("Last Week Tonight With John Oliver", 24, None)
    # only for Now TV: the same shape on another service is left alone
    st["attributes"]["app_name"] = "BBC iPlayer"
    obs = observe([st])
    assert obs.series_title is None and obs.episode is None


def details(seasons, last):
    return {"seasons": seasons, "last_aired": {"season": last[0], "episode": last[1]}}


def test_infer_season():
    lwt = details({12: 30, 13: 30}, (13, 26))
    assert infer_season(lwt, None, 24) == 13                                # not started: latest aired season
    assert infer_season(lwt, {"season": 13, "episode": 23}, 24) == 13       # the next episode in this season
    # a low number right after the end of a season -> the next season started
    assert infer_season(lwt, {"season": 12, "episode": 29}, 1) == 13
    # a low number mid-season is a re-watch of that season, not a new one
    assert infer_season(lwt, {"season": 13, "episode": 20}, 3) == 13
    assert infer_season(lwt, None, 99) is None                              # can't exist
    assert infer_season({}, None, 3) is None


def test_observe_reads_position_and_length_from_cast():
    """Real values from the Bedroom TV playing BBC iPlayer (QI)."""
    st = {"state": "playing", "attributes": {
        "app_id": "AndroidNativeApp", "app_name": "BBC iPlayer", "media_title": "QI",
        "media_duration": 1761.04, "media_position": 930.612,
        "media_position_updated_at": "2026-09-29T19:17:10.497022+00:00"}}
    obs = observe([st])
    assert (obs.duration_ms, obs.position_ms, obs.playing) == (1_761_040, 930_612, True)
    assert obs.position_at == datetime(2026, 9, 29, 19, 17, 10, 497022, tzinfo=timezone.utc)
    # Prime reports a nonsense duration (-0.001): treated as unknown
    st["attributes"].update(app_name="Prime Video", media_duration=-0.001)
    assert observe([st]).duration_ms is None
    # HA can hand the timestamp over as a datetime already
    st["attributes"].update(app_name="BBC iPlayer", media_position_updated_at=datetime(2026, 9, 29, 19, 0, tzinfo=timezone.utc))
    assert observe([st]).position_at == datetime(2026, 9, 29, 19, 0, tzinfo=timezone.utc)


def test_room_tracker_works_out_how_far_through_you_got():
    T0 = datetime(2026, 9, 29, 20, 0, tzinfo=timezone.utc)
    ep = lambda pos, playing=True, at=T0: Observation(
        "tv_movies", "BBC iPlayer", title="QI", position_ms=pos, duration_ms=1_800_000,
        playing=playing, position_at=at)

    # playing: the position is wound forward from when Cast said it was true, until the TV goes off
    rt = RoomTracker("Bedroom")
    rt.update(ep(60_000), T0)
    closed = rt.update(None, T0 + timedelta(minutes=20))
    assert closed[0]["final_pos_ms"] == 60_000 + 20 * 60_000 and closed[0]["duration_ms"] == 1_800_000

    # paused: it stays where it was, however long the TV is left on
    rt = RoomTracker("Bedroom")
    rt.update(ep(60_000), T0)
    rt.update(ep(600_000, playing=False, at=T0 + timedelta(minutes=9)), T0 + timedelta(minutes=9))
    closed = rt.update(None, T0 + timedelta(hours=3))
    assert closed[0]["final_pos_ms"] == 600_000

    # never past the end
    rt = RoomTracker("Bedroom")
    rt.update(ep(1_700_000), T0)
    assert rt.update(None, T0 + timedelta(hours=1))[0]["final_pos_ms"] == 1_800_000

    # no position known at all
    rt = RoomTracker("Bedroom")
    rt.update(Observation("tv_movies", "Netflix"), T0)
    assert rt.update(None, T0 + timedelta(minutes=30))[0]["final_pos_ms"] is None


def test_find_episode_by_title():
    eps = [{"season": 1, "episode": 1, "name": "Pilot"}, {"season": 1, "episode": 2, "name": "The Jordan Boys\u2019 Legacy"},
           {"season": 2, "episode": 1, "name": "Pilot"}, {"season": 2, "episode": 2, "name": "A Much Longer Episode Name"}]
    assert [(e["season"], e["episode"]) for e in find_episode_by_title(eps, "the jordan boys' legacy")] == [(1, 2)]
    assert len(find_episode_by_title(eps, "Pilot")) == 2                       # ambiguous stays ambiguous
    assert find_episode_by_title(eps, "Pil") == []                             # short: exact only
    # a longer title that merely contains the episode name is accepted; a short name never matches loosely
    assert [(e["season"], e["episode"]) for e in find_episode_by_title(eps, "A Much Longer Episode Name (Part 1)")] == [(2, 2)]
    assert find_episode_by_title(eps, "Pilot: The Beginning") == []
    assert find_episode_by_title(eps, "") == [] and find_episode_by_title([], "x") == []


def test_candidates_are_ranked_tracked_then_on_the_service_then_recent_then_runtime():
    start = datetime(2026, 9, 29, 20, 0, tzinfo=timezone.utc)
    mk = lambda name, tracked=False, on=False, aired=None, runtime=None: {
        "show": name, "tracked": tracked, "on_service": on, "air_date": aired, "runtime": runtime}
    cands = [
        mk("Old Other", aired="2015-01-01", runtime=44),
        mk("On The Service", on=True, aired="2015-01-01"),
        mk("Tracked", tracked=True),
        mk("Aired Last Week", aired="2026-09-22", runtime=90),
        mk("Aired Last Week, Right Length", aired="2026-09-22", runtime=45),
        mk("Aired Last Year", aired="2025-09-22", runtime=45),
        mk("Aired Tomorrow (not yet)", aired="2026-09-30", runtime=45),
    ]
    order = [c["show"] for c in rank_episode_candidates(cands, start, 45.4)]
    assert order[:3] == ["Tracked", "On The Service", "Aired Last Week, Right Length"]
    assert order.index("Aired Last Week") < order.index("Aired Last Year")        # recent beats old
    assert order.index("Aired Last Week") < order.index("Aired Tomorrow (not yet)")  # not yet aired isn't "recent"
    assert rank_episode_candidates([], start, None) == []
    # no start time or runtime known: still returns everything, stably
    assert len(rank_episode_candidates(cands, None, None)) == 7


def test_describe_candidate():
    assert describe_candidate({"tracked": True, "air_date": "2026-09-27", "runtime": 45, "on_service": True}, "Now TV") \
        == "you track it · aired 27 Sep · 45 min · on Now TV"
    assert describe_candidate({"air_date": None, "runtime": None, "on_service": True}, None) == ""
    assert describe_candidate({"air_date": "not a date"}, "Now TV") == ""
