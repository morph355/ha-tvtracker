"""TV Tracker: what are we watching, where, and what's next."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_REGION,
    CONF_ROOMS,
    CONF_TMDB_KEY,
    CONF_TRAKT_ID,
    CONF_TRAKT_SECRET,
    DEFAULT_REGION,
    DEFAULT_ROOMS,
    DOMAIN,
)
from .hub import TVTrackerHub
from .services import async_register_services
from .tmdb import TMDB
from .trakt import TraktClient

PLATFORMS = [Platform.BUTTON, Platform.SELECT, Platform.SENSOR, Platform.TEXT]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    tmdb = TMDB(
        async_get_clientsession(hass),
        entry.data[CONF_TMDB_KEY],
        entry.data.get(CONF_REGION, DEFAULT_REGION),
    )
    rooms = _normalise_rooms(entry.options.get(CONF_ROOMS) or DEFAULT_ROOMS)
    trakt = None
    if entry.options.get(CONF_TRAKT_ID) and entry.options.get(CONF_TRAKT_SECRET):
        trakt = TraktClient(
            async_get_clientsession(hass),
            entry.options[CONF_TRAKT_ID],
            entry.options[CONF_TRAKT_SECRET],
        )
    hub = TVTrackerHub(hass, tmdb, rooms, trakt)
    await hub.async_load()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = hub
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await hub.async_start()

    async def _on_stop(_event) -> None:
        await hub.async_stop()

    entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _on_stop))
    entry.async_on_unload(entry.add_update_listener(_reload))
    async_register_services(hass)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hub: TVTrackerHub = hass.data[DOMAIN][entry.entry_id]
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        await hub.async_stop()
        hass.data[DOMAIN].pop(entry.entry_id)
    return ok


async def _reload(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


def _normalise_rooms(rooms: list) -> list[dict]:
    """Accept entities as a list or a single string; drop malformed rooms."""
    out = []
    for room in rooms:
        if not isinstance(room, dict) or not room.get("name"):
            continue
        entities = room.get("entities") or []
        if isinstance(entities, str):
            entities = [entities]
        if entities:
            out.append({"name": str(room["name"]), "entities": list(entities)})
    return out
