"""End-to-end test against a real Home Assistant core with TMDB faked."""

import asyncio
from datetime import timedelta

import pytest
from homeassistant.exceptions import ServiceValidationError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.tvtracker.const import DOMAIN
from test_logic import ADB_TEXT, RAW_TV

RAW_GHOSTS = {
    "name": "Ghosts", "first_air_date": "2021-10-07", "status": "Returning Series", "episode_run_time": [30],
    "seasons": [{"season_number": 1, "episode_count": 20}],
    "last_episode_to_air": {"season_number": 1, "episode_number": 20, "runtime": 30},
    "watch/providers": {"results": {"GB": {"flatrate": [{"provider_name": "BBC iPlayer"}]}}},
}

RAW_WREXHAM = {
    "name": "Welcome to Wrexham", "first_air_date": "2022-08-24", "status": "Returning Series",
    "seasons": [{"season_number": 1, "episode_count": 20}],
    "last_episode_to_air": {"season_number": 1, "episode_number": 20},
    "watch/providers": {"results": {"GB": {"flatrate": [{"provider_name": "Disney Plus"}]}}},
}

pytest.importorskip("pytest_homeassistant_custom_component")


@pytest.fixture(autouse=True)
def _enable(enable_custom_integrations):
    yield


@pytest.fixture
def fake_tmdb(monkeypatch):
    async def fake_get(self, path, **params):
        if path == "/configuration":
            return {}
        if path == "/search/tv":
            q = params["query"].lower()
            hits = [{"id": 95396, "name": "Severance", "first_air_date": "2022-02-18", "popularity": 9},
                    {"id": 1234, "name": "Welcome to Wrexham", "first_air_date": "2022-08-24", "popularity": 8},
                    {"id": 4242, "name": "Ghosts", "first_air_date": "2021-10-07", "popularity": 7}]
            return {"results": [h for h in hits if q in h["name"].lower()]}
        if path == "/search/movie":
            return {"results": [{"id": 438631, "title": "Dune", "release_date": "2021-10-01", "popularity": 5}]
                    if "dune" in params["query"].lower() else []}
        if path == "/tv/95396":
            return RAW_TV
        if path == "/tv/1234":
            return {**RAW_WREXHAM}
        if path == "/tv/4242":
            return {**RAW_GHOSTS}
        if path == "/movie/438631":
            return {"title": "Dune", "release_date": "2021-10-01", "runtime": 155,
                    "watch/providers": {"results": {"GB": {"flatrate": [{"provider_name": "Netflix"}]}}}}
        raise AssertionError(f"unexpected TMDB call {path}")

    monkeypatch.setattr("custom_components.tvtracker.tmdb.TMDB._get", fake_get)


@pytest.fixture
async def setup(hass, fake_tmdb):
    entry = MockConfigEntry(domain=DOMAIN, data={"tmdb_api_key": "k", "region": "GB"})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def call(hass, svc_name, **data):
    return await hass.services.async_call(DOMAIN, svc_name, data, blocking=True, return_response=True)


def attrs(hass, entity_id):
    return hass.states.get(entity_id).attributes


async def test_full_flow(hass, setup, freezer):
    # Sensors exist, one per configured room.
    assert hass.states.get("sensor.tv_tracker_now_watching_bedroom").state == "Idle"
    assert hass.states.get("sensor.tv_tracker_services").state == "8"

    # Search, create a list, add by title.
    res = await call(hass, "search", query="Severance")
    assert res["results"][0]["tmdb_id"] == 95396
    res = await call(hass, "add_to_list", list="Shows", title="Severance")
    assert res["added"]["availability"] == "Watch on Apple TV"
    assert hass.states.get("sensor.tv_tracker_watchlists").state == "1"
    assert "Shows" in attrs(hass, "sensor.tv_tracker_watchlists")["lists"]

    # Ambiguous / unknown titles give a helpful error.
    with pytest.raises(ServiceValidationError):
        await call(hass, "add_to_list", list="Shows", title="Nonexistent Show")

    # Bedroom TV starts playing Severance on Netflix... then turns off 45 min later.
    hass.states.async_set("media_player.master_room_tv", "playing",
        {"app_name": "Netflix", "app_id": "com.netflix.ninja", "media_title": "Hello, Ms. Cobel",
         "media_series_title": "Severance", "media_season": 2, "media_episode": 4})
    await hass.async_block_till_done()
    now = hass.states.get("sensor.tv_tracker_now_watching_bedroom")
    assert now.state == "Severance" and now.attributes["service"] == "Netflix"
    freezer.tick(timedelta(minutes=45))
    hass.states.async_set("media_player.master_room_tv", "off", {})
    await hass.async_block_till_done()

    assert hass.states.get("sensor.tv_tracker_now_watching_bedroom").state == "Idle"
    hist = attrs(hass, "sensor.tv_tracker_history")["tv_movies"]
    assert hist[0]["title"] == "Severance" and hist[0]["room"] == "Bedroom"
    cw = attrs(hass, "sensor.tv_tracker_continue_watching")["items"]
    assert cw[0]["title"] == "Severance" and cw[0]["progress"] == "S2E4" and cw[0]["next"] == "S2E5"

    # SHIELD: YouTube via Cast title + ADB app; tracked as youtube, not on the watchlist.
    hass.states.async_set("media_player.shield_2", "on",
        {"app_id": "com.google.android.youtube.tv", "app_name": "com.google.android.youtube.tv"})
    hass.states.async_set("media_player.shield", "playing",
        {"app_id": "2C6A6E3D", "app_name": "YouTube", "media_title": "A video", "media_artist": "BBC News"})
    await hass.async_block_till_done()
    assert hass.states.get("sensor.tv_tracker_now_watching_living_room").state == "A video"
    freezer.tick(timedelta(minutes=20))
    hass.states.async_set("media_player.shield_2", "off", {})
    hass.states.async_set("media_player.shield", "idle", {})
    await hass.async_block_till_done()
    yt = attrs(hass, "sensor.tv_tracker_history")["youtube"]
    assert yt[0]["title"] == "A video" and yt[0]["channel"] == "BBC News"
    assert "YouTube" not in attrs(hass, "sensor.tv_tracker_services")["services"]

    # Music is ignored.
    hass.states.async_set("media_player.family_room_tv", "playing",
        {"app_id": "AndroidNativeApp", "app_name": "Spotify", "media_title": "song"})
    await hass.async_block_till_done()
    freezer.tick(timedelta(minutes=30))
    hass.states.async_set("media_player.family_room_tv", "off", {})
    await hass.async_block_till_done()
    assert hass.states.get("sensor.tv_tracker_history").state == "2"

    # A new streaming app is added to the services list automatically.
    hass.states.async_set("media_player.family_room_tv", "playing",
        {"app_id": "com.cbs.ott", "app_name": "Paramount+", "media_title": "Some show"})
    await hass.async_block_till_done()
    freezer.tick(timedelta(minutes=30))
    hass.states.async_set("media_player.family_room_tv", "off", {})
    await hass.async_block_till_done()
    assert "Paramount+" in attrs(hass, "sensor.tv_tracker_services")["services"]


async def test_manual_history_and_movies(hass, setup):
    # Movie logged manually: marks it watched on the list, records history.
    res = await call(hass, "log_watch", title="Dune", service="Netflix", room="Living Room",
                     list="Movie Night", duration_minutes=155, watched_at="2026-09-20 20:00:00")
    assert res["item"]["status"] == "finished" and res["item"]["lists"] == ["Movie Night"]

    # TV episode logged manually moves progress forward only.
    await call(hass, "log_watch", title="Severance", season=1, episode=6, service="Apple TV")
    await call(hass, "log_watch", title="Severance", season=1, episode=2, service="Apple TV")
    res = await call(hass, "get_library")
    sev = next(i for i in res["continue_watching"] if i["title"] == "Severance")
    assert sev["progress"] == "S1E6" and sev["status"] == "watching"
    assert len(res["recent_history"]) == 3

    # YouTube manual entry, and progress / watched services.
    await call(hass, "log_watch", category="youtube", title="Cool video", channel="Chan")
    res = await call(hass, "set_progress", title="Severance", season=2, episode=10)
    assert res["item"]["status"] == "caught_up"
    res = await call(hass, "mark_watched", title="Dune", watched=False)
    assert res["item"]["status"] == "want_to_watch"

    # Services and history management.
    res = await call(hass, "add_service", name="Paramount Plus")
    assert "Paramount Plus" in res["services"]
    hid = (await call(hass, "get_library"))["recent_history"][0]["id"]
    assert (await call(hass, "delete_history", id=hid))["deleted"] is True
    res = await call(hass, "delete_list", list="Movie Night")
    assert res["deleted"] == "Movie Night"


async def test_persistence(hass, setup, hass_storage):
    await call(hass, "add_to_list", list="Shows", title="Severance")
    entry = setup
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    saved = hass_storage["tvtracker.library"]["data"]
    assert "shows" in saved["lists"] and "tv:95396" in saved["items"]


async def test_config_flow(hass, fake_tmdb, monkeypatch):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert result["type"] == "form"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"tmdb_api_key": " abc ", "region": "gb"}
    )
    assert result["type"] == "create_entry"
    assert result["data"] == {"tmdb_api_key": "abc", "region": "GB"}

    # A bad key is reported, not crashed on.
    from custom_components.tvtracker.tmdb import TMDBAuthError

    async def bad(self, path, **params):
        raise TMDBAuthError("no")

    monkeypatch.setattr("custom_components.tvtracker.tmdb.TMDB._get", bad)
    hass.config_entries.async_entries(DOMAIN)  # single instance already exists
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert result["type"] == "abort"


async def test_options_rooms(hass, setup):
    result = await hass.config_entries.options.async_init(setup.entry_id)
    assert result["type"] == "form"
    rooms = [{"name": "Kitchen", "entities": ["media_player.kitchen_tv"]}]
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"rooms": rooms})
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()
    assert hass.states.get("sensor.tv_tracker_now_watching_kitchen") is not None


async def test_dashboard_templates_render(hass, setup, freezer):
    """Render every markdown card in the dashboard against real data."""
    import pathlib
    import yaml
    from homeassistant.helpers.template import Template

    await call(hass, "add_to_list", list="Shows", title="Severance")
    await call(hass, "log_watch", title="Severance", season=1, episode=3, service="Apple TV", room="Bedroom")
    await call(hass, "log_watch", category="youtube", title="Cool video", channel="Chan", room="Living Room")
    await call(hass, "add_to_list", list="Movies", title="Dune")
    hass.states.async_set("media_player.master_room_tv", "playing",
        {"app_name": "Netflix", "media_title": "Ep", "media_series_title": "Severance"})
    await hass.async_block_till_done()

    dash = yaml.safe_load(pathlib.Path("dashboard/tvtracker.yaml").read_text())
    out = {}
    for view in dash["views"]:
        for card in view["cards"]:
            out[f"{view['title']} / {card['title']}"] = Template(card["content"], hass).async_render(parse_result=False)
    assert "**Bedroom** — Severance (Netflix)" in out["Watching / Now watching"]
    assert "**Family Room** — off" in out["Watching / Now watching"]
    assert "**Severance** — up next **S1E4**" in out["Watching / Continue watching"]
    assert "### Shows" in out["Watchlists / Watchlists"] and "### Movies" in out["Watchlists / Watchlists"]
    assert "Dune" in out["Watchlists / Watchlists"] and "Watch on Netflix" in out["Watchlists / Watchlists"]
    assert "Severance" in out["TV & Movies / Recently watched"] and "S1E3" in out["TV & Movies / Recently watched"]
    assert "Cool video" in out["YouTube / Recently watched"] and "Chan" in out["YouTube / Recently watched"]
    assert "- Netflix" in out["Services / My streaming services"]
    for k, v in out.items():
        print(f"=== {k} ===\n{v}")


async def test_adb_media_session_gives_disney_title_and_tracks_episodes(hass, setup, freezer):
    """Disney+ only publishes its title to Android's media session (via ADB)."""
    from homeassistant.core import ServiceCall

    ADB = "media_player.android_tv_192_168_3_131"
    box = {"pos": 5_000, "state": 3, "n": 0, "silent": False}

    async def fake_adb(call: ServiceCall):
        if box["silent"]:  # the real entity keeps its previous adb_response
            return
        box["n"] += 1
        text = ADB_TEXT.replace("state=3, position=234528", f"state={box['state']}, position={box['pos']}")
        text += f"\ntvt_ts={box['n']}"
        cur = hass.states.get(ADB)
        hass.states.async_set(ADB, cur.state, {**cur.attributes, "adb_response": text})

    hass.services.async_register("androidtv", "adb_command", fake_adb)

    await call(hass, "add_to_list", list="Shows", title="Welcome to Wrexham")

    async def show(adb_state):
        hass.states.async_set("media_player.shield_2", "on",
            {"app_id": "com.disney.disneyplus", "app_name": "com.disney.disneyplus"})
        # a stale Cast entity holding the Spotify podcast, exactly as on the real SHIELD
        hass.states.async_set("media_player.shield", "paused",
            {"app_id": "AndroidNativeApp", "app_name": "Spotify", "media_title": "S13 EP21: Sneaky Sasquatch "})
        hass.states.async_set(ADB, adb_state,
            {"app_id": "com.disney.disneyplus", "app_name": "Disney+", "adb_response": None})
        await hass.async_block_till_done()

    await show("playing")
    now = hass.states.get("sensor.tv_tracker_now_watching_living_room")
    assert now.state == "Welcome to Wrexham", now  # not the Spotify podcast
    assert now.attributes["service"] == "Disney+"

    # 25 minutes in, then autoplay starts the next episode (position resets)
    freezer.tick(timedelta(minutes=25))
    box["pos"] = 1_500_000
    hass.states.async_set(ADB, "paused", {"app_id": "com.disney.disneyplus", "app_name": "Disney+", "adb_response": None})
    await hass.async_block_till_done()
    hass.states.async_set(ADB, "playing", {"app_id": "com.disney.disneyplus", "app_name": "Disney+", "adb_response": None})
    await hass.async_block_till_done()
    freezer.tick(timedelta(minutes=1))
    box["pos"] = 3_000
    hass.states.async_set(ADB, "paused", {"app_id": "com.disney.disneyplus", "app_name": "Disney+", "adb_response": None})
    await hass.async_block_till_done()
    hass.states.async_set(ADB, "playing", {"app_id": "com.disney.disneyplus", "app_name": "Disney+", "adb_response": None})
    await hass.async_block_till_done()

    # episode 1 was logged and counted
    lib = (await call(hass, "get_library"))
    wrex = next(i for i in lib["continue_watching"] if i["title"] == "Welcome to Wrexham")
    assert wrex["progress"] == "S1E1" and wrex["next"] == "S1E2"

    # 25 more minutes of episode 2, then the TV goes off
    freezer.tick(timedelta(minutes=25))
    for e in ("media_player.shield_2", ADB):
        hass.states.async_set(e, "off", {"adb_response": None} if e == ADB else {})
    await hass.async_block_till_done()
    lib = (await call(hass, "get_library"))
    wrex = next(i for i in lib["continue_watching"] if i["title"] == "Welcome to Wrexham")
    assert wrex["progress"] == "S1E2"
    titles = [h["title"] for h in lib["recent_history"]]
    assert titles.count("Welcome to Wrexham") == 2 and not any("Sasquatch" in (t or "") for t in titles)
    assert wrex["on_my_services"] == ["Disney+"]


async def test_stale_adb_response_is_not_trusted(hass, setup, freezer):
    """If a poll produces no fresh answer, an old title must not linger."""
    from homeassistant.core import ServiceCall

    ADB = "media_player.android_tv_192_168_3_131"
    stale = ADB_TEXT + "\ntvt_ts=1"

    async def silent_adb(call: ServiceCall):
        return  # leaves the previous adb_response in place, like the real entity

    hass.services.async_register("androidtv", "adb_command", silent_adb)
    hass.states.async_set("media_player.shield_2", "on",
        {"app_id": "com.disney.disneyplus", "app_name": "com.disney.disneyplus"})
    hass.states.async_set(ADB, "playing",
        {"app_id": "com.disney.disneyplus", "app_name": "Disney+", "adb_response": stale})
    await hass.async_block_till_done()
    hub = hass.data[DOMAIN][setup.entry_id]
    # The first answer (unseen timestamp) is trusted and names the show...
    assert hass.states.get("sensor.tv_tracker_now_watching_living_room").state == "Welcome to Wrexham"
    # ...but the same (old) timestamp again -> not fresh -> its sessions are discarded
    hass.states.async_set(ADB, "paused",
        {"app_id": "com.disney.disneyplus", "app_name": "Disney+", "adb_response": stale})
    await hass.async_block_till_done()
    assert hub._sessions["Living Room"] == []
    # ...and a response with no timestamp at all is never trusted either
    hass.states.async_set(ADB, "playing",
        {"app_id": "com.disney.disneyplus", "app_name": "Disney+", "adb_response": ADB_TEXT})
    await hass.async_block_till_done()
    assert hub._sessions["Living Room"] == []


# ---------------------------------------------------------------- Trakt ----
TRAKT_TOKENS = {"access_token": "acc1", "refresh_token": "ref1", "expires_in": 7 * 86400}


def _trakt_ep(id_, tmdb, title, season, number, when):
    return {"id": id_, "watched_at": when, "action": "watch", "type": "episode",
            "episode": {"season": season, "number": number, "title": f"{title} ep", "ids": {"tmdb": 5}},
            "show": {"title": title, "year": 2022, "ids": {"tmdb": tmdb}}}


@pytest.fixture
def fake_trakt(monkeypatch):
    """Replace the network calls of TraktClient; record what the hub asked for."""
    state = {"polls": ["pending", "ok"], "history": [], "history_calls": [], "refreshed": 0,
             "history_error": None, "trakt_show": 777, "show_progress": {}, "watched_movies": [],
             "added": [], "hidden": [], "add_error": None}

    async def device_code(self):
        return {"device_code": "dc", "user_code": "ABCD1234", "verification_url": "https://trakt.tv/activate",
                "expires_in": 600, "interval": 1}

    async def poll_token(self, code):
        assert code == "dc"
        which = state["polls"].pop(0)
        return which, (dict(TRAKT_TOKENS) if which == "ok" else None)

    async def refresh(self, refresh_token):
        state["refreshed"] += 1
        return {"access_token": "acc2", "refresh_token": "ref2", "expires_in": 7 * 86400}

    async def history(self, token, start_at):
        state["history_calls"].append((token, start_at))
        if state["history_error"]:
            raise state["history_error"]
        return list(state["history"])

    async def find_show(self, token, tmdb_id):
        return state["trakt_show"]

    async def show_progress(self, token, trakt_id):
        assert trakt_id == state["trakt_show"]
        return state["show_progress"]

    async def watched_movies(self, token):
        return list(state["watched_movies"])

    async def add_history(self, token, payload):
        if state["add_error"]:
            raise state["add_error"]
        state["added"].append(payload)
        return {"added": {"episodes": sum(len(s["episodes"]) for sh in payload.get("shows", []) for s in sh["seasons"]),
                          "movies": len(payload.get("movies", []))}}

    async def hide(self, token, section, payload):
        state["hidden"].append(("hide", section, payload))
        return {}

    async def unhide(self, token, section, payload):
        state["hidden"].append(("unhide", section, payload))
        return {}

    for name, fn in (("device_code", device_code), ("poll_token", poll_token),
                     ("refresh", refresh), ("history", history), ("find_show", find_show), ("show_progress", show_progress),
                     ("watched_movies", watched_movies), ("add_history", add_history),
                     ("hide", hide), ("unhide", unhide)):
        monkeypatch.setattr(f"custom_components.tvtracker.trakt.TraktClient.{name}", fn)
    return state


@pytest.fixture
async def setup_trakt(hass, fake_tmdb, fake_trakt):
    entry = MockConfigEntry(domain=DOMAIN, data={"tmdb_api_key": "k", "region": "GB"},
                            options={"trakt_client_id": "cid", "trakt_client_secret": "sec"})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    hub = hass.data[DOMAIN][entry.entry_id]

    gate = asyncio.Event()
    gate.set()

    async def gated_sleep(_):
        await gate.wait()   # tests clear the gate to hold the login "waiting for approval"

    hub._sleep = gated_sleep
    hub._gate = gate
    return hub


async def test_trakt_not_configured_without_client_credentials(hass, setup):
    assert hass.states.get("sensor.tv_tracker_trakt").state == "not_configured"
    with pytest.raises(ServiceValidationError):
        await call(hass, "trakt_connect")


async def test_trakt_connect_sync_and_dedupe(hass, setup_trakt, fake_trakt, freezer):
    hub = setup_trakt
    assert hass.states.get("sensor.tv_tracker_trakt").state == "not_connected"

    # A Disney+ viewing the TV could not name (Disney+ gave no title this time).
    hass.states.async_set("media_player.shield_2", "on",
        {"app_id": "com.disney.disneyplus", "app_name": "com.disney.disneyplus"})
    await hass.async_block_till_done()
    freezer.tick(timedelta(minutes=30))
    hass.states.async_set("media_player.shield_2", "off", {})
    await hass.async_block_till_done()
    assert [h["title"] for h in (await call(hass, "get_library"))["recent_history"]] == [None]

    today = dt_util_now_iso()
    fake_trakt["history"] = [
        _trakt_ep(11, 1234, "Welcome to Wrexham", 1, 7, today),
        _trakt_ep(12, 95396, "Severance", 2, 4, today),
        {"id": 13, "watched_at": today, "action": "watch", "type": "movie",
         "movie": {"title": "Dune", "year": 2021, "ids": {"tmdb": 438631}}},
    ]

    # connect: shows the code, then the background poll completes the login and syncs
    hub._gate.clear()   # hold the login until "approved"
    res = await call(hass, "trakt_connect")
    assert res["user_code"] == "ABCD1234" and res["verification_url"] == "https://trakt.tv/activate"
    waiting = hass.states.get("sensor.tv_tracker_trakt")
    assert waiting.state == "waiting_for_approval" and waiting.attributes["user_code"] == "ABCD1234"
    hub._gate.set()     # ...you approved it at trakt.tv/activate
    await hass.async_block_till_done()
    sensor = hass.states.get("sensor.tv_tracker_trakt")
    assert sensor.state == "connected" and sensor.attributes["last_sync"]

    # first sync only asks for ~60 days of history, with the API token
    token, start_at = fake_trakt["history_calls"][0]
    assert token == "acc1" and start_at.endswith("Z")

    lib = await call(hass, "get_library")
    by_title = {i["title"]: i for i in lib["continue_watching"]}
    assert by_title["Welcome to Wrexham"]["progress"] == "S1E7"      # new to the library, created from Trakt
    assert by_title["Severance"]["progress"] == "S2E4"
    # the untitled Disney+ viewing was named from Trakt's Wrexham watch
    hist = lib["recent_history"]
    assert hist[0]["title"] == "Welcome to Wrexham" and hist[0]["season"] == 1 and hist[0]["episode"] == 7
    # the movie was recorded as watched (it isn't "continue watching", so check the item directly)
    assert hub.library.view("movie:438631", dt_util_date())["status"] == "finished"

    # the sync reports what Trakt returned, so "nothing imported" can be explained
    last = hass.states.get("sensor.tv_tracker_trakt").attributes["last_result"]
    assert last["fetched"] == 3 and last["without_tmdb_id"] == 0 and last["applied"] == 3
    # syncing again applies nothing new, even though Trakt returns the same rows...
    res = await call(hass, "trakt_sync")
    assert res["applied"] == 0 and res["status"] == "connected"
    assert (res["fetched"], res["already_applied"]) == (3, 3)
    # ...and the next sync starts a few days before the last one (late, date-only syncs)
    # (start_at is last_sync minus the overlap, so it is later than the first sync's 60-day window
    # but earlier than "now")
    assert fake_trakt["history_calls"][-1][1] > fake_trakt["history_calls"][0][1]
    # a genuinely new watch is applied
    fake_trakt["history"].append(_trakt_ep(14, 95396, "Severance", 2, 5, today))
    res = await call(hass, "trakt_sync")
    assert res["applied"] == 1
    sev = next(i for i in (await call(hass, "get_library"))["continue_watching"] if i["title"] == "Severance")
    assert sev["progress"] == "S2E5"


async def test_trakt_corrects_a_guessed_episode(hass, setup_trakt, fake_trakt, freezer):
    hub = setup_trakt
    await call(hass, "add_to_list", list="Shows", title="Welcome to Wrexham")
    # Disney+ named the show but not the episode: we count on by one (a guess)
    hass.states.async_set("media_player.shield_2", "on",
        {"app_id": "com.disney.disneyplus", "app_name": "Disney+"})
    hass.states.async_set("media_player.android_tv_192_168_3_131", "playing",
        {"app_id": "com.disney.disneyplus", "app_name": "Disney+", "media_title": "Welcome to Wrexham"})
    await hass.async_block_till_done()
    freezer.tick(timedelta(minutes=30))
    hass.states.async_set("media_player.shield_2", "off", {})
    hass.states.async_set("media_player.android_tv_192_168_3_131", "off", {})
    await hass.async_block_till_done()
    item = hub.library.data["items"]["tv:1234"]
    assert item["progress"] == {"season": 1, "episode": 1} and item["progress_source"] == "guess"

    # connect and let Trakt say the real episode was S1E9... then a later, lower guess can't undo it
    fake_trakt["history"] = [_trakt_ep(21, 1234, "Welcome to Wrexham", 1, 9, dt_util_now_iso())]
    await call(hass, "trakt_connect")
    await hass.async_block_till_done()
    item = hub.library.data["items"]["tv:1234"]
    assert item["progress"] == {"season": 1, "episode": 9} and item["progress_source"] == "trakt"


async def test_trakt_refreshes_token_and_handles_rejected_login(hass, setup_trakt, fake_trakt):
    hub = setup_trakt
    await call(hass, "trakt_connect")
    await hass.async_block_till_done()
    assert hub.trakt_status == "connected"

    # near expiry -> refreshed before the next sync
    hub.library.data["trakt"]["expires_at"] = 0
    await call(hass, "trakt_sync")
    assert fake_trakt["refreshed"] == 1
    assert fake_trakt["history_calls"][-1][0] == "acc2"
    assert hub.library.data["trakt"]["refresh_token"] == "ref2"   # new tokens kept, state preserved

    # Trakt rejects us: we report "not_connected" instead of crashing or looping
    from custom_components.tvtracker.trakt import TraktAuthError, TraktError
    fake_trakt["history_error"] = TraktAuthError("rejected")
    res = await call(hass, "trakt_sync")
    assert res["status"] == "not_connected"
    assert hass.states.get("sensor.tv_tracker_trakt").state == "not_connected"
    assert "rejected" in hass.states.get("sensor.tv_tracker_trakt").attributes["last_error"]

    # a plain network error keeps the connection and reports the error
    fake_trakt["polls"] = ["ok"]
    fake_trakt["history_error"] = None
    await call(hass, "trakt_connect")
    await hass.async_block_till_done()
    fake_trakt["history_error"] = TraktError("boom")
    res = await call(hass, "trakt_sync")
    assert res["error"] == "boom" and hub.trakt_status == "connected"

    res = await call(hass, "trakt_disconnect")
    assert res["status"] == "not_connected" and "trakt" not in hub.library.data


async def test_trakt_login_denied_or_expired(hass, setup_trakt, fake_trakt):
    hub = setup_trakt
    fake_trakt["polls"] = ["pending", "denied"]
    await call(hass, "trakt_connect")
    await hass.async_block_till_done()
    assert hub.trakt_status == "not_connected" and hub._connecting is None


async def test_trakt_client_http_shapes(hass, aioclient_mock):
    from homeassistant.helpers.aiohttp_client import async_get_clientsession
    from custom_components.tvtracker.trakt import BASE, TraktAuthError, TraktClient

    client = TraktClient(async_get_clientsession(hass), " cid ", " sec ")
    aioclient_mock.post(f"{BASE}/oauth/device/code", json={"user_code": "X", "device_code": "d"})
    assert (await client.device_code())["user_code"] == "X"
    method, url, data, headers = aioclient_mock.mock_calls[-1]
    assert data == {"client_id": "cid"} and headers["trakt-api-key"] == "cid" and headers["trakt-api-version"] == "2"

    for status, expect in ((400, "pending"), (429, "slow_down"), (410, "expired"), (418, "denied"), (404, "invalid")):
        aioclient_mock.clear_requests()
        aioclient_mock.post(f"{BASE}/oauth/device/token", status=status)
        assert await client.poll_token("d") == (expect, None)
    aioclient_mock.clear_requests()
    aioclient_mock.post(f"{BASE}/oauth/device/token", json={"access_token": "a", "refresh_token": "r"})
    state, tokens = await client.poll_token("d")
    assert state == "ok" and tokens["access_token"] == "a"
    assert aioclient_mock.mock_calls[-1][2] == {"code": "d", "client_id": "cid", "client_secret": "sec"}

    aioclient_mock.clear_requests()
    aioclient_mock.post(f"{BASE}/oauth/token", status=401)
    with pytest.raises(TraktAuthError):
        await client.refresh("r")

    # history: bearer token, start_at, and it keeps paging until a short page
    aioclient_mock.clear_requests()
    pages = {"1": [{"id": i} for i in range(100)], "2": [{"id": 999}]}

    from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMockResponse

    async def side_effect(method, url, data):
        return AiohttpClientMockResponse(method, url, json=pages[url.query["page"]])

    aioclient_mock.get(f"{BASE}/sync/history", side_effect=side_effect)
    items = await client.history("tok", "2026-09-01T00:00:00.000Z")
    assert len(items) == 101
    calls = [c for c in aioclient_mock.mock_calls if c[0] == "GET"]
    assert len(calls) == 2 and calls[0][3]["Authorization"] == "Bearer tok"
    assert calls[0][1].query["start_at"] == "2026-09-01T00:00:00.000Z"


def dt_util_now_iso():
    from homeassistant.util import dt as dt_util
    return dt_util.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000Z")


def dt_util_date():
    from homeassistant.util import dt as dt_util
    return dt_util.now().date()


async def test_trakt_sync_explains_an_empty_import(hass, setup_trakt, fake_trakt):
    """The failure the user hit: connected, nothing imported. Say why."""
    # Trakt returns items we can't use (no TMDB id) plus an unrelated type
    unusable = _trakt_ep(31, None, "No Id Show", 1, 1, dt_util_now_iso())
    fake_trakt["history"] = [unusable, {"id": 32, "type": "season", "watched_at": dt_util_now_iso()}]
    await call(hass, "trakt_connect")
    await hass.async_block_till_done()
    res = await call(hass, "trakt_sync")
    assert (res["fetched"], res["without_tmdb_id"], res["applied"]) == (2, 2, 0)
    # and when Trakt itself has nothing:
    fake_trakt["history"] = []
    res = await call(hass, "trakt_sync")
    assert (res["fetched"], res["without_tmdb_id"], res["applied"]) == (0, 0, 0)
    assert hass.states.get("sensor.tv_tracker_trakt").attributes["last_result"]["fetched"] == 0


async def test_partial_viewing_is_not_counted_but_finishing_after_resuming_is(hass, setup, freezer):
    """A Cast-only TV (like the Bedroom one): 80% of the programme has to be watched."""
    from homeassistant.util import dt as dt_util

    await call(hass, "add_to_list", list="Shows", title="Welcome to Wrexham")
    room = "media_player.master_room_tv"
    hub = hass.data[DOMAIN][setup.entry_id]

    def play(position_s):
        hass.states.async_set(room, "playing", {
            "app_id": "AndroidNativeApp", "app_name": "Disney+", "media_title": "Welcome to Wrexham",
            "media_duration": 2700.0, "media_position": position_s,
            "media_position_updated_at": dt_util.utcnow().isoformat()})

    # 15 of 45 minutes, then the TV goes off: logged (over 2 min) but not counted
    play(0.0)
    await hass.async_block_till_done()
    freezer.tick(timedelta(minutes=15))
    hass.states.async_set(room, "off", {})
    await hass.async_block_till_done()
    assert hub.library.data["items"]["tv:1234"]["progress"] is None
    assert hub.library.data["history"][-1]["watched_pct"] == 33

    # later: resume at 15 min and watch 25 more -> 40 of 45 minutes, 89%: counts, once
    play(900.0)
    await hass.async_block_till_done()
    freezer.tick(timedelta(minutes=25))
    hass.states.async_set(room, "off", {})
    await hass.async_block_till_done()
    assert hub.library.data["items"]["tv:1234"]["progress"] == {"season": 1, "episode": 1}
    assert hub.library.data["history"][-1]["watched_pct"] == 89
    assert [h["watched_pct"] for h in hub.library.data["history"]] == [33, 89]


def trakt_progress(seasons):
    """{season: (episodes_aired_on_trakt, {watched episode numbers})} -> Trakt's progress shape."""
    return {"seasons": [
        {"number": n, "episodes": [{"number": e, "completed": e in done} for e in range(1, aired + 1)]}
        for n, (aired, done) in seasons.items()], "aired": sum(a for a, _ in seasons.values())}


# Trakt: S1 all 9 watched, S2 has 12 episodes (TMDB says 10!) and only 1-3 watched
SEVERANCE_ON_TRAKT = trakt_progress({1: (9, set(range(1, 10))), 2: (12, {1, 2, 3})})


async def _connect(hass, hub):
    await call(hass, "trakt_connect")
    await hass.async_block_till_done()
    assert hub.trakt_status == "connected"


async def test_mark_up_to_date_previews_then_adds_only_the_missing_episodes(hass, setup_trakt, fake_trakt):
    hub = setup_trakt
    await _connect(hass, hub)
    fake_trakt["show_progress"] = SEVERANCE_ON_TRAKT
    await call(hass, "add_to_list", list="Shows", title="Severance")

    # 1. preview: says what Trakt holds and what would be added; changes nothing anywhere
    res = await call(hass, "mark_watched", title="Severance", dry_run=True)
    assert res["dry_run"] is True
    t = res["trakt"]
    assert (t["already_on_trakt"], t["trakt_aired"], t["last_watched_on_trakt"]) == (12, 21, "S2E3")
    # Trakt's numbering (12 episodes in S2), not TMDB's (10): 9 missing, S2E4..S2E12
    assert t["to_add"] == 9 and t["episodes"] == [f"S2E{n}" for n in range(4, 13)]
    assert fake_trakt["added"] == []
    assert hub.library.data["items"]["tv:95396"]["progress"] is None

    # 2. for real: HA is up to date (TMDB's view), Trakt gets exactly what *it* is missing, pinned by its own show id
    res = await call(hass, "mark_watched", title="Severance")
    assert res["item"]["status"] == "caught_up" and res["item"]["progress"] == "S2E10"
    assert res["trakt"]["added"] == 9 and res["trakt"]["still_queued"] == 0
    (payload,) = fake_trakt["added"]
    assert payload == {"shows": [{"ids": {"trakt": 777}, "seasons": [{"number": 2, "episodes": [
        {"number": n, "watched_at": "released"} for n in range(4, 13)]}]}]}

    # 3. un-marking changes HA only: nothing is ever removed from Trakt
    res = await call(hass, "mark_watched", title="Severance", watched=False)
    assert res["trakt"]["status"] == "unchanged" and len(fake_trakt["added"]) == 1


async def test_nothing_is_written_if_trakt_cannot_find_the_show_or_already_has_everything(hass, setup_trakt, fake_trakt):
    hub = setup_trakt
    await _connect(hass, hub)
    await call(hass, "add_to_list", list="Shows", title="Severance")

    fake_trakt["trakt_show"] = None                                     # Trakt doesn't know the show
    res = await call(hass, "mark_watched", title="Severance")
    assert res["item"]["progress"] == "S2E10"                             # HA still updated
    assert "couldn't find" in res["trakt"]["error"] and res["trakt"]["added"] == 0
    assert fake_trakt["added"] == [] and hub.trakt_outbox == []

    fake_trakt["trakt_show"] = 777                                      # Trakt has every episode
    fake_trakt["show_progress"] = trakt_progress({1: (9, set(range(1, 10))), 2: (10, set(range(1, 11)))})
    res = await call(hass, "mark_watched", title="Severance")
    assert res["trakt"]["to_add"] == 0 and res["trakt"]["added"] == 0 and fake_trakt["added"] == []


async def test_a_failed_send_stays_queued_and_is_retried_by_the_next_sync(hass, setup_trakt, fake_trakt):
    from custom_components.tvtracker.trakt import TraktError
    hub = setup_trakt
    await _connect(hass, hub)
    fake_trakt["show_progress"] = SEVERANCE_ON_TRAKT
    await call(hass, "add_to_list", list="Shows", title="Severance")

    fake_trakt["add_error"] = TraktError("Trakt is down")
    res = await call(hass, "mark_watched", title="Severance")
    assert res["item"]["progress"] == "S2E10"                       # HA is still updated
    assert res["trakt"]["added"] == 0 and res["trakt"]["still_queued"] == 9 and "down" in res["trakt"]["error"]
    assert hass.states.get("sensor.tv_tracker_trakt").attributes["waiting_to_send"] == 9

    fake_trakt["add_error"] = None
    await call(hass, "trakt_sync")                                  # the hourly sync flushes the queue
    assert len(fake_trakt["added"]) == 1 and hub.trakt_outbox == []
    assert hass.states.get("sensor.tv_tracker_trakt").attributes["waiting_to_send"] == 0


async def test_mark_up_to_date_for_a_film_and_when_trakt_is_not_connected(hass, setup, fake_tmdb):
    # not connected: HA is updated, Trakt is simply not involved
    res = await call(hass, "mark_watched", title="Severance")
    assert res["item"]["status"] == "caught_up" and "trakt" not in res
    res = await call(hass, "mark_watched", title="Severance", dry_run=True)
    assert res["trakt"]["status"] == "not_configured"


async def test_hide_and_unhide_in_ha_and_on_trakt(hass, setup_trakt, fake_trakt):
    hub = setup_trakt
    await _connect(hass, hub)
    await call(hass, "add_to_list", list="Shows", title="Severance")
    await call(hass, "set_progress", title="Severance", season=1, episode=3)
    assert len((await call(hass, "get_library"))["continue_watching"]) == 1

    res = await call(hass, "hide", title="Severance")
    assert res["hidden"] is True and res["trakt"] == "hidden"
    assert [(a, b) for a, b, _ in fake_trakt["hidden"]] == [("hide", "progress_watched"), ("hide", "calendar")]
    assert fake_trakt["hidden"][0][2] == {"shows": [{"ids": {"tmdb": 95396}}]}
    lib = await call(hass, "get_library")
    assert lib["continue_watching"] == [] and lib["lists"]["Shows"] == []
    assert hub.library.data["items"]["tv:95396"]["progress"] == {"season": 1, "episode": 3}   # history kept

    res = await call(hass, "hide", title="Severance", hidden=False)
    assert res["trakt"] == "shown" and len((await call(hass, "get_library"))["continue_watching"]) == 1
    assert [a for a, _, _ in fake_trakt["hidden"][2:]] == ["unhide", "unhide"]

    # HA only: Trakt untouched
    before = len(fake_trakt["hidden"])
    await call(hass, "hide", title="Severance", trakt=False)
    assert len(fake_trakt["hidden"]) == before


async def test_only_certain_viewings_on_services_trakt_does_not_sync_are_sent(hass, setup_trakt, fake_trakt, freezer):
    """iPlayer (with the episode reported) goes to Trakt; Disney+ (Trakt syncs it itself) does not."""
    from homeassistant.core import ServiceCall
    from homeassistant.util import dt as dt_util
    from test_logic import IPLAYER_TEXT

    hub = setup_trakt
    await _connect(hass, hub)
    await call(hass, "add_to_list", list="Shows", title="Ghosts")
    await call(hass, "add_to_list", list="Shows", title="Welcome to Wrexham")
    ADB = "media_player.android_tv_192_168_3_131"
    box = {"n": 0, "text": IPLAYER_TEXT}

    async def fake_adb(call_: ServiceCall):
        box["n"] += 1
        cur = hass.states.get(ADB)
        hass.states.async_set(ADB, cur.state, {**cur.attributes, "adb_response": f"{box['text']}\ntvt_ts={box['n']}"})

    hass.services.async_register("androidtv", "adb_command", fake_adb)

    # BBC iPlayer, Series 1 episode 18 reported by the media session, watched to the end
    hass.states.async_set("media_player.shield_2", "on", {"app_id": "bbc.iplayer.android", "app_name": "bbc.iplayer.android"})
    hass.states.async_set("media_player.shield", "playing", {
        "app_id": "AndroidNativeApp", "app_name": "BBC iPlayer", "media_title": "Ghosts US",
        "media_duration": 1800.0, "media_position": 5.0, "media_position_updated_at": dt_util.utcnow().isoformat()})
    hass.states.async_set(ADB, "playing", {"app_id": "bbc.iplayer.android", "app_name": "BBC iPlayer", "adb_response": None})
    await hass.async_block_till_done()
    freezer.tick(timedelta(minutes=29))
    for e in ("media_player.shield_2", "media_player.shield", ADB):
        hass.states.async_set(e, "off", {"adb_response": None} if e == ADB else {})
    await hass.async_block_till_done()

    assert hub.library.data["items"]["tv:4242"]["progress"] == {"season": 1, "episode": 18}
    (payload,) = fake_trakt["added"]
    show = payload["shows"][0]
    assert show["ids"] == {"tmdb": 4242} and show["seasons"][0]["number"] == 1
    assert show["seasons"][0]["episodes"][0]["number"] == 18
    assert show["seasons"][0]["episodes"][0]["watched_at"].endswith(".000Z")
    assert hub.trakt_outbox == []

    # Disney+ for 40 of 45 minutes: counted in HA, but NOT sent (Trakt's own sync covers Disney+)
    hass.states.async_set("media_player.master_room_tv", "playing", {
        "app_id": "AndroidNativeApp", "app_name": "Disney+", "media_title": "Welcome to Wrexham",
        "media_duration": 2700.0, "media_position": 0.0, "media_position_updated_at": dt_util.utcnow().isoformat()})
    await hass.async_block_till_done()
    freezer.tick(timedelta(minutes=40))
    hass.states.async_set("media_player.master_room_tv", "off", {})
    await hass.async_block_till_done()
    assert hub.library.data["items"]["tv:1234"]["progress"] == {"season": 1, "episode": 1}
    assert len(fake_trakt["added"]) == 1 and hub.trakt_outbox == []


async def _watch_on_bedroom(hass, freezer, attrs, minutes=40):
    from homeassistant.util import dt as dt_util
    room = "media_player.master_room_tv"
    hass.states.async_set(room, "playing", {
        "app_id": "AndroidNativeApp", "media_duration": 2700.0, "media_position": 0.0,
        "media_position_updated_at": dt_util.utcnow().isoformat(), **attrs})
    await hass.async_block_till_done()
    freezer.tick(timedelta(minutes=minutes))
    hass.states.async_set(room, "off", {})
    await hass.async_block_till_done()


async def test_a_certain_episode_on_a_service_trakt_already_syncs_is_not_sent(hass, setup_trakt, fake_trakt, freezer):
    """Disney+ reports season and episode here (so it is certain), but Trakt's own
    streaming sync covers Disney+, and sending it too would create a duplicate."""
    hub = setup_trakt
    await _connect(hass, hub)
    await call(hass, "add_to_list", list="Shows", title="Welcome to Wrexham")
    await _watch_on_bedroom(hass, freezer, {
        "app_name": "Disney+", "media_title": "Episode Three", "media_series_title": "Welcome to Wrexham",
        "media_season": 1, "media_episode": 3})
    item = hub.library.data["items"]["tv:1234"]
    assert item["progress"] == {"season": 1, "episode": 3} and item["progress_source"] == "reported"
    assert fake_trakt["added"] == [] and hub.trakt_outbox == []


async def test_a_guessed_episode_on_a_service_trakt_does_not_sync_is_not_sent(hass, setup_trakt, fake_trakt, freezer):
    """iPlayer is on the send list, but with only the show name we merely counted one
    episode on: not sure enough to write to Trakt."""
    hub = setup_trakt
    await _connect(hass, hub)
    await call(hass, "add_to_list", list="Shows", title="Ghosts")
    await _watch_on_bedroom(hass, freezer, {"app_name": "BBC iPlayer", "media_title": "Ghosts US"})
    item = hub.library.data["items"]["tv:4242"]
    assert item["progress"] == {"season": 1, "episode": 1} and item["progress_source"] == "guess"
    assert fake_trakt["added"] == [] and hub.trakt_outbox == []


async def test_nothing_is_sent_when_trakt_is_not_connected(hass, setup, fake_tmdb, freezer):
    await call(hass, "add_to_list", list="Shows", title="Ghosts")
    await _watch_on_bedroom(hass, freezer, {
        "app_name": "BBC iPlayer", "media_title": "Ghosts US", "media_series_title": "Ghosts",
        "media_season": 1, "media_episode": 18})
    hub = hass.data[DOMAIN][setup.entry_id]
    assert hub.library.data["items"]["tv:4242"]["progress"] == {"season": 1, "episode": 18}
    assert hub.library.data.get("trakt_outbox", []) == []
