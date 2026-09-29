"""Sensors: now watching (per room), continue watching, watchlists, history."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import DOMAIN, SIGNAL_UPDATE
from .hub import TVTrackerHub


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    hub: TVTrackerHub = hass.data[DOMAIN][entry.entry_id]
    entities: list[SensorEntity] = [
        NowWatchingSensor(hub, room) for room in hub.trackers
    ]
    entities += [
        ContinueWatchingSensor(hub),
        WatchlistsSensor(hub),
        HistorySensor(hub),
        ServicesSensor(hub),
    ]
    async_add_entities(entities)


class _Base(SensorEntity):
    _attr_should_poll = False
    _attr_icon = "mdi:television-play"
    # Attribute blobs are big; keep them out of the recorder database.
    _unrecorded_attributes = frozenset({"items", "lists", "tv_movies", "youtube", "services"})

    def __init__(self, hub: TVTrackerHub, name: str, unique: str) -> None:
        self._hub = hub
        self._attr_name = name
        self._attr_unique_id = f"{DOMAIN}_{unique}"

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_UPDATE, self.async_write_ha_state)
        )

    def _today(self):
        return dt_util.now().date()


class NowWatchingSensor(_Base):
    def __init__(self, hub: TVTrackerHub, room: str) -> None:
        super().__init__(hub, f"TV Tracker Now Watching {room}", f"now_{room.lower()}")
        self._room = room

    @property
    def native_value(self) -> str:
        s = self._hub.now_watching(self._room)
        if not s:
            return "Idle"
        return s.get("series_title") or s.get("title") or s["service"]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        s = self._hub.now_watching(self._room)
        if not s:
            return {"room": self._room}
        return {
            "room": self._room,
            "service": s["service"],
            "category": s["category"],
            "episode_title": s.get("title"),
            "channel": s.get("channel"),
            "since": s["start"].isoformat(),
        }


class ContinueWatchingSensor(_Base):
    def __init__(self, hub: TVTrackerHub) -> None:
        super().__init__(hub, "TV Tracker Continue Watching", "continue")

    def _rows(self):
        return self._hub.library.continue_watching(self._today())

    @property
    def native_value(self) -> int:
        return sum(1 for v in self._rows() if v["status"] == "watching")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"items": self._rows()}


class WatchlistsSensor(_Base):
    def __init__(self, hub: TVTrackerHub) -> None:
        super().__init__(hub, "TV Tracker Watchlists", "watchlists")

    @property
    def native_value(self) -> int:
        return len(self._hub.library.data["lists"])

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"lists": self._hub.library.watchlists(self._today())}


class HistorySensor(_Base):
    def __init__(self, hub: TVTrackerHub) -> None:
        super().__init__(hub, "TV Tracker History", "history")

    @property
    def native_value(self) -> int:
        return len(self._hub.library.data["history"])

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        lib = self._hub.library
        return {
            "tv_movies": lib.recent_history("tv_movies"),
            "youtube": lib.recent_history("youtube"),
        }


class ServicesSensor(_Base):
    def __init__(self, hub: TVTrackerHub) -> None:
        super().__init__(hub, "TV Tracker Services", "services")

    @property
    def native_value(self) -> int:
        return len(self._hub.library.data["services"])

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"services": self._hub.library.data["services"]}
