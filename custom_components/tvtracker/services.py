"""Services: the interface for dashboards, automations and Claude."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .hub import TVTrackerHub
from .library import item_key
from .logic import canonical_service, norm_title
from .tmdb import TMDBError
from .trakt import TraktError

MEDIA_TYPE = vol.In(["tv", "movie"])

# How a service names the show/movie it is about.
TARGET = {
    vol.Optional("title"): cv.string,
    vol.Optional("tmdb_id"): vol.Coerce(int),
    vol.Optional("media_type"): MEDIA_TYPE,
    vol.Optional("year"): vol.Coerce(int),
}


def _hub(hass: HomeAssistant) -> TVTrackerHub:
    hubs = hass.data.get(DOMAIN) or {}
    if not hubs:
        raise ServiceValidationError("TV Tracker is not set up")
    return next(iter(hubs.values()))


def _today(hass: HomeAssistant):
    return dt_util.now().date()


async def _resolve(hub: TVTrackerHub, data: dict[str, Any]) -> dict[str, Any]:
    """Turn title / tmdb_id into a library item, fetching from TMDB if needed."""
    lib = hub.library
    media_type, tmdb_id, title = data.get("media_type"), data.get("tmdb_id"), data.get("title")
    try:
        if tmdb_id:
            if not media_type:
                raise ServiceValidationError("media_type is required with tmdb_id")
            key = item_key(media_type, tmdb_id)
            return lib.data["items"].get(key) or await hub.fetch_item(media_type, tmdb_id)

        if not title:
            raise ServiceValidationError("Give a title or a tmdb_id")
        keys = lib.find_by_title(title, media_type)
        if len(keys) == 1:
            return lib.data["items"][keys[0]]

        results = await hub.tmdb.search(title, media_type)
        exact = [r for r in results if norm_title(r["title"]) == norm_title(title)]
        if data.get("year"):
            by_year = [r for r in (exact or results) if r["year"] == str(data["year"])]
            exact = by_year or exact
        pool = exact or results
        if len(pool) == 1 or (len(exact) == 1):
            pick = (exact or pool)[0]
            return await hub.fetch_item(pick["media_type"], pick["tmdb_id"])
        if not pool:
            raise ServiceValidationError(f"TMDB found nothing for '{title}'")
        options = "; ".join(
            f"{r['media_type']} {r['tmdb_id']}: {r['title']} ({r['year']})" for r in pool[:6]
        )
        raise ServiceValidationError(
            f"Several matches for '{title}': {options}. "
            "Call again with tmdb_id and media_type."
        )
    except TMDBError as err:
        raise HomeAssistantError(str(err)) from err


def _wrap(func):
    """Turn library ValueErrors into friendly service errors."""

    async def inner(call: ServiceCall):
        try:
            return await func(call)
        except ValueError as err:
            raise ServiceValidationError(str(err)) from err

    return inner


def async_register_services(hass: HomeAssistant) -> None:
    if hass.services.has_service(DOMAIN, "search"):
        return

    async def search(call: ServiceCall):
        hub = _hub(hass)
        try:
            return {"results": await hub.tmdb.search(call.data["query"], call.data.get("media_type"))}
        except TMDBError as err:
            raise HomeAssistantError(str(err)) from err

    async def create_list(call: ServiceCall):
        hub = _hub(hass)
        list_id = hub.library.create_list(call.data["name"])
        hub.changed()
        return {"list_id": list_id}

    async def delete_list(call: ServiceCall):
        hub = _hub(hass)
        name = hub.library.delete_list(call.data["list"])
        hub.changed()
        return {"deleted": name}

    async def add_to_list(call: ServiceCall):
        hub = _hub(hass)
        item = await _resolve(hub, call.data)
        list_id = hub.library.ensure_list(call.data["list"])
        hub.library.add_to_list(list_id, item["key"])
        hub.changed()
        return {"added": hub.library.view(item["key"], _today(hass))}

    async def remove_from_list(call: ServiceCall):
        hub = _hub(hass)
        item = await _resolve(hub, call.data)
        hub.library.remove_from_list(call.data["list"], item["key"])
        hub.changed()
        return {"removed": item["title"]}

    async def set_progress(call: ServiceCall):
        hub = _hub(hass)
        item = await _resolve(hub, call.data)
        hub.library.set_progress(item["key"], call.data["season"], call.data["episode"], dt_util.utcnow())
        hub.changed()
        return {"item": hub.library.view(item["key"], _today(hass))}

    async def mark_watched(call: ServiceCall):
        hub = _hub(hass)
        item = await _resolve(hub, call.data)
        hub.library.mark_watched(item["key"], call.data["watched"], dt_util.utcnow())
        hub.changed()
        return {"item": hub.library.view(item["key"], _today(hass))}

    async def log_watch(call: ServiceCall):
        """Record a viewing that the TVs did not capture."""
        hub, lib, data = _hub(hass), _hub(hass).library, call.data
        minutes = data["duration_minutes"]
        start = dt_util.as_utc(data["watched_at"]) if "watched_at" in data else (
            dt_util.utcnow() - timedelta(minutes=minutes)
        )
        end = start + timedelta(minutes=minutes)
        service = canonical_service(data["service"]) if data.get("service") else None
        category = data["category"]
        entry: dict[str, Any] = {
            "start": start, "end": end, "room": data.get("room"), "category": category,
            "service": service or ("YouTube" if category == "youtube" else None),
            "channel": data.get("channel"), "season": data.get("season"),
            "episode": data.get("episode"), "item_key": None, "source": "manual",
        }
        result: dict[str, Any] = {}
        if category == "youtube":
            if not data.get("title"):
                raise ServiceValidationError("A YouTube viewing needs a title")
            entry["title"] = data["title"]
        else:
            item = await _resolve(hub, data)
            entry.update(title=item["title"], item_key=item["key"])
            if data.get("list"):
                lib.add_to_list(lib.ensure_list(data["list"]), item["key"])
            if item["media_type"] == "movie":
                lib.mark_watched(item["key"], True, end)
            elif data.get("season") and data.get("episode"):
                lib.set_progress(item["key"], data["season"], data["episode"], end, only_forward=True)
            elif data.get("season") or data.get("episode"):
                raise ServiceValidationError("Give both season and episode, or neither")
            if service:
                lib.add_service(service)
            result["item"] = lib.view(item["key"], _today(hass))
        result["history_id"] = lib.add_history(entry)["id"]
        hub.changed()
        return result

    async def delete_history(call: ServiceCall):
        hub = _hub(hass)
        ok = hub.library.delete_history(call.data["id"])
        hub.changed()
        return {"deleted": ok}

    async def add_service(call: ServiceCall):
        hub = _hub(hass)
        added = hub.library.add_service(call.data["name"])
        hub.changed()
        return {"added": added, "services": hub.library.data["services"]}

    async def remove_service(call: ServiceCall):
        hub = _hub(hass)
        removed = hub.library.remove_service(call.data["name"])
        hub.changed()
        return {"removed": removed, "services": hub.library.data["services"]}

    async def refresh(call: ServiceCall):
        hub = _hub(hass)
        await hub.async_refresh_all()
        return {"items": len(hub.library.data["items"])}

    async def trakt_connect(call: ServiceCall):
        hub = _hub(hass)
        try:
            return await hub.async_trakt_connect()
        except TraktError as err:
            raise HomeAssistantError(str(err)) from err

    async def trakt_sync(call: ServiceCall):
        hub = _hub(hass)
        return await hub.async_trakt_sync()

    async def trakt_disconnect(call: ServiceCall):
        hub = _hub(hass)
        await hub.async_trakt_disconnect()
        return {"status": hub.trakt_status}

    async def get_library(call: ServiceCall):
        hub = _hub(hass)
        lib, today = hub.library, _today(hass)
        return {
            "lists": lib.watchlists(today),
            "continue_watching": lib.continue_watching(today),
            "services": lib.data["services"],
            "recent_history": lib.recent_history(None, 25),
        }

    def register(name: str, func, schema: dict) -> None:
        hass.services.async_register(
            DOMAIN, name, _wrap(func), schema=vol.Schema(schema), supports_response=SupportsResponse.OPTIONAL
        )

    register("search", search, {vol.Required("query"): cv.string, vol.Optional("media_type"): MEDIA_TYPE})
    register("create_list", create_list, {vol.Required("name"): cv.string})
    register("delete_list", delete_list, {vol.Required("list"): cv.string})
    register("add_to_list", add_to_list, {vol.Required("list"): cv.string, **TARGET})
    register("remove_from_list", remove_from_list, {vol.Required("list"): cv.string, **TARGET})
    register(
        "set_progress",
        set_progress,
        {vol.Required("season"): vol.Coerce(int), vol.Required("episode"): vol.Coerce(int), **TARGET},
    )
    register("mark_watched", mark_watched, {vol.Optional("watched", default=True): cv.boolean, **TARGET})
    register(
        "log_watch",
        log_watch,
        {
            vol.Optional("category", default="tv_movies"): vol.In(["tv_movies", "youtube"]),
            vol.Optional("service"): cv.string,
            vol.Optional("room"): cv.string,
            vol.Optional("list"): cv.string,
            vol.Optional("season"): vol.Coerce(int),
            vol.Optional("episode"): vol.Coerce(int),
            vol.Optional("channel"): cv.string,
            vol.Optional("watched_at"): cv.datetime,
            vol.Optional("duration_minutes", default=0): vol.All(vol.Coerce(int), vol.Range(min=0, max=1440)),
            **TARGET,
        },
    )
    register("delete_history", delete_history, {vol.Required("id"): cv.string})
    register("add_service", add_service, {vol.Required("name"): cv.string})
    register("remove_service", remove_service, {vol.Required("name"): cv.string})
    register("refresh", refresh, {})
    register("trakt_connect", trakt_connect, {})
    register("trakt_sync", trakt_sync, {})
    register("trakt_disconnect", trakt_disconnect, {})
    register("get_library", get_library, {})
