"""The button that applies the "watched up to" picker."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .hub import TVTrackerHub


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities([WatchedUpToButton(hass.data[DOMAIN][entry.entry_id])])


class WatchedUpToButton(ButtonEntity):
    _attr_should_poll = False
    _attr_icon = "mdi:check-all"

    def __init__(self, hub: TVTrackerHub) -> None:
        self._hub = hub
        self._attr_name = "TV Tracker Mark Watched Up To"
        self._attr_unique_id = f"{DOMAIN}_mark_watched_up_to"

    async def async_press(self) -> None:
        picker = self._hub.picker
        pick = next((e for e in picker["episodes"] if e["label"] == picker.get("choice")), None)
        if not picker.get("key") or pick is None:
            raise HomeAssistantError("Choose a show and the last episode you've watched first")
        await self._hub.async_watched_up_to(picker["key"], pick["season"], pick["episode"])
