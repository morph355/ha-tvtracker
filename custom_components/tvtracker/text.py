"""A box on the dashboard to type the right show when the guess was wrong."""

from __future__ import annotations

from homeassistant.components.text import TextEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, SIGNAL_UPDATE
from .hub import TVTrackerHub


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities([ShowSearchText(hass.data[DOMAIN][entry.entry_id])])


class ShowSearchText(TextEntity):
    _attr_should_poll = False
    _attr_icon = "mdi:text-search"
    _attr_native_max = 100

    def __init__(self, hub: TVTrackerHub) -> None:
        self._hub = hub
        self._attr_name = "TV Tracker Show Search"
        self._attr_unique_id = f"{DOMAIN}_show_search"

    @property
    def native_value(self) -> str:
        return self._hub.search_text

    async def async_set_value(self, value: str) -> None:
        self._hub.search_text = value
        self.async_write_ha_state()
        async_dispatcher_send(self.hass, SIGNAL_UPDATE)
