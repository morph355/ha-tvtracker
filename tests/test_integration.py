"""End-to-end test against a real Home Assistant core with TMDB faked."""

from datetime import timedelta

import pytest
from homeassistant.exceptions import ServiceValidationError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.tvtracker.const import DOMAIN
from test_logic import RAW_TV

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
            hits = [{"id": 95396, "name": "Severance", "first_air_date": "2022-02-18", "popularity": 9}]
            return {"results": [h for h in hits if q in h["name"].lower()]}
        if path == "/search/movie":
            return {"results": [{"id": 438631, "title": "Dune", "release_date": "2021-10-01", "popularity": 5}]
                    if "dune" in params["query"].lower() else []}
        if path == "/tv/95396":
            return RAW_TV
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
            out[card["title"]] = Template(card["content"], hass).async_render(parse_result=False)
    assert "**Bedroom** — Severance (Netflix)" in out["Now watching"]
    assert "**Family Room** — off" in out["Now watching"]
    assert "**Severance** — up next **S1E4**" in out["Continue watching"]
    assert "### Shows" in out["Watchlists"] and "### Movies" in out["Watchlists"]
    assert "Dune" in out["Watchlists"] and "Watch on Netflix" in out["Watchlists"]
    assert "Severance" in out["Recently watched — TV & movies"] and "S1E3" in out["Recently watched — TV & movies"]
    assert "Cool video" in out["Recently watched — YouTube"] and "Chan" in out["Recently watched — YouTube"]
    assert "- Netflix" in out["My streaming services"]
    for k, v in out.items():
        print(f"=== {k} ===\n{v}")
