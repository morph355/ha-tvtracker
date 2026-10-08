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
        TraktSensor(hub),
        NeedsConfirmingSensor(hub),
    ]
    async_add_entities(entities)


class _Base(SensorEntity):
    _attr_should_poll = False
    _attr_icon = "mdi:television-play"
    # Attribute blobs are big; keep them out of the recorder database.
    _unrecorded_attributes = frozenset({"items", "lists", "tv_movies", "youtube", "services", "matches"})

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
        genre = self._hub.library.data.get("genre_filter")
        return {
            "lists": self._hub.library.watchlists(self._today(), genre),
            "genre": genre,
        }


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


class TraktSensor(_Base):
    _attr_icon = "mdi:television-classic"

    def __init__(self, hub: TVTrackerHub) -> None:
        super().__init__(hub, "TV Tracker Trakt", "trakt")

    @property
    def native_value(self) -> str:
        return self._hub.trakt_status

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self._hub.trakt_info


class NeedsConfirmingSensor(_Base):
    """Viewings labelled 'probably' that are waiting for you to confirm or correct."""

    _attr_icon = "mdi:help-circle-outline"

    def __init__(self, hub: TVTrackerHub) -> None:
        super().__init__(hub, "TV Tracker Needs Confirming", "needs_confirming")

    @property
    def native_value(self) -> int:
        return len(self._hub.pending_matches())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        pending = self._hub.pending_matches()
        return {
            # how many options the viewing at the front of the queue has (drives which buttons show)
            "choice_count": len((pending[0].get("candidates") or [])) if pending else 0,
            "matches": [
                {
                    **{k: h.get(k) for k in ("id", "title", "season", "episode", "episode_title", "service", "room", "start", "skips")},
                    "candidates": [
                        {k: c.get(k) for k in ("show", "year", "season", "episode", "name", "hint")}
                        for c in (h.get("candidates") or [])
                    ],
                }
                for h in pending
            ],
        }
