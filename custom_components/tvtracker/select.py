"""The "watched up to" picker: choose a show, then the last episode you've watched."""

from __future__ import annotations

from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import DOMAIN, SIGNAL_UPDATE
from .hub import TVTrackerHub

NONE = "—"


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    hub = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([PickerShowSelect(hub), PickerEpisodeSelect(hub), GenreSelect(hub)])


class _PickerSelect(SelectEntity):
    _attr_should_poll = False
    # the option lists can be long; keep them out of the database
    _unrecorded_attributes = frozenset({"options", "episodes"})

    def __init__(self, hub: TVTrackerHub) -> None:
        self._hub = hub

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_UPDATE, self._refresh)
        )

    @callback
    def _refresh(self) -> None:
        self.async_write_ha_state()


class PickerShowSelect(_PickerSelect):
    _attr_icon = "mdi:television-classic"

    def __init__(self, hub: TVTrackerHub) -> None:
        super().__init__(hub)
        self._attr_name = "TV Tracker Pick Show"
        self._attr_unique_id = f"{DOMAIN}_pick_show"

    @property
    def options(self) -> list[str]:
        return [NONE, *self._hub.picker_shows()]

    @property
    def current_option(self) -> str:
        key = self._hub.picker.get("key")
        return next((label for label, k in self._hub.picker_shows().items() if k == key), NONE)

    async def async_select_option(self, option: str) -> None:
        key = self._hub.picker_shows().get(option)
        if key is None:
            self._hub.picker = {"key": None, "episodes": [], "choice": None, "loading": False}
            self._hub.changed()
            return
        await self._hub.async_pick_show(key)


class PickerEpisodeSelect(_PickerSelect):
    """Unwatched episodes of the chosen show; pick the last one you've watched."""

    _attr_icon = "mdi:playlist-check"

    def __init__(self, hub: TVTrackerHub) -> None:
        super().__init__(hub)
        self._attr_name = "TV Tracker Watched Up To"
        self._attr_unique_id = f"{DOMAIN}_watched_up_to"

    @property
    def options(self) -> list[str]:
        return [NONE, *(e["label"] for e in self._hub.picker["episodes"])]

    @property
    def current_option(self) -> str:
        choice = self._hub.picker.get("choice")
        return choice if choice in self.options else NONE

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "show": next(
                (label for label, k in self._hub.picker_shows().items() if k == self._hub.picker.get("key")),
                None,
            ),
            "loading": self._hub.picker.get("loading", False),
            "count": len(self._hub.picker["episodes"]),
            "episodes": self._hub.picker["episodes"],
        }

    async def async_select_option(self, option: str) -> None:
        self._hub.picker["choice"] = None if option == NONE else option
        self._hub.changed()


ALL_GENRES = "All genres"


class GenreSelect(_PickerSelect):
    """Show only one genre on the watchlists."""

    _attr_icon = "mdi:filter-variant"

    def __init__(self, hub: TVTrackerHub) -> None:
        super().__init__(hub)
        self._attr_name = "TV Tracker Genre"
        self._attr_unique_id = f"{DOMAIN}_genre"

    def _genres(self) -> list[str]:
        return self._hub.library.watchlist_genres(dt_util.now().date())

    @property
    def options(self) -> list[str]:
        chosen = self._hub.library.data.get("genre_filter")
        genres = self._genres()
        # keep a chosen genre listed even if nothing on the lists has it any more
        return [ALL_GENRES, *genres, *([chosen] if chosen and chosen not in genres else [])]

    @property
    def current_option(self) -> str:
        return self._hub.library.data.get("genre_filter") or ALL_GENRES

    async def async_select_option(self, option: str) -> None:
        self._hub.library.data["genre_filter"] = None if option == ALL_GENRES else option
        self._hub.changed()
