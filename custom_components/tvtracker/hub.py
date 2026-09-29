"""The hub: owns the library, TMDB client and per-room session tracking."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    MIN_COUNT_SECONDS,
    MIN_SESSION_SECONDS,
    REFRESH_INTERVAL_HOURS,
    SIGNAL_UPDATE,
    STORAGE_KEY,
    STORAGE_VERSION,
)
from .library import Library
from .logic import RoomTracker, observe
from .tmdb import TMDB, TMDBError

_LOGGER = logging.getLogger(__name__)


class TVTrackerHub:
    def __init__(
        self,
        hass: HomeAssistant,
        tmdb: TMDB,
        rooms: list[dict[str, Any]],
    ) -> None:
        self.hass = hass
        self.tmdb = tmdb
        self.rooms = rooms
        self.store: Store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self.library = Library()
        self.trackers = {r["name"]: RoomTracker(r["name"]) for r in rooms}
        self._unsubs: list[Any] = []

    # ---- lifecycle -------------------------------------------------------
    async def async_load(self) -> None:
        self.library = Library(await self.store.async_load())

    async def async_start(self) -> None:
        for room in self.rooms:
            entities = list(room["entities"])
            self._unsubs.append(
                async_track_state_change_event(
                    self.hass, entities, self._make_handler(room["name"])
                )
            )
            # Pick up a TV that is already on at startup.
            self._evaluate(room["name"])
        self._unsubs.append(
            async_track_time_interval(
                self.hass, self._scheduled_refresh, timedelta(hours=REFRESH_INTERVAL_HOURS)
            )
        )
        self.hass.async_create_task(self.async_refresh_all())

    async def async_stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        now = dt_util.utcnow()
        for tracker in self.trackers.values():
            session = tracker.close(now)
            if session:
                self._record(session)
        await self.store.async_save(self.library.data)

    # ---- persistence / notifications ------------------------------------
    @callback
    def changed(self) -> None:
        """Persist (debounced) and tell the sensors to refresh."""
        self.store.async_delay_save(lambda: self.library.data, 5)
        async_dispatcher_send(self.hass, SIGNAL_UPDATE)

    # ---- room tracking ---------------------------------------------------
    def _make_handler(self, room: str):
        @callback
        def _handle(_event: Any) -> None:
            self._evaluate(room)

        return _handle

    def _evaluate(self, room: str) -> None:
        entities = next(r["entities"] for r in self.rooms if r["name"] == room)
        states = []
        for entity_id in entities:
            st = self.hass.states.get(entity_id)
            if st is not None:
                states.append({"state": st.state, "attributes": dict(st.attributes)})
        closed = self.trackers[room].update(observe(states), dt_util.utcnow())
        for session in closed:
            self._record(session)
        self.changed()

    def now_watching(self, room: str) -> dict[str, Any] | None:
        return self.trackers[room].current

    def _record(self, session: dict[str, Any]) -> None:
        """Log a finished session and update the watchlist from it."""
        seconds = (session["end"] - session["start"]).total_seconds()
        if seconds < MIN_SESSION_SECONDS:
            return
        lib = self.library
        key = lib.apply_session(session, MIN_COUNT_SECONDS)
        if session["category"] != "youtube" and lib.add_service(session["service"]):
            _LOGGER.info("Added new streaming service: %s", session["service"])
        item = lib.data["items"].get(key) if key else None
        lib.add_history(
            {
                "start": session["start"],
                "end": session["end"],
                "room": session["room"],
                "category": session["category"],
                "service": session["service"],
                "title": (item["title"] if item else None)
                or session.get("series_title")
                or session.get("title"),
                "episode_title": session.get("title") if item else None,
                "season": session.get("season"),
                "episode": session.get("episode"),
                "channel": session.get("channel"),
                "item_key": key,
                "source": "auto",
            }
        )
        self.changed()

    # ---- TMDB ------------------------------------------------------------
    async def fetch_item(self, media_type: str, tmdb_id: int) -> dict[str, Any]:
        details, providers = await self.tmdb.details(media_type, tmdb_id)
        return self.library.upsert_item(
            media_type, tmdb_id, details, providers, dt_util.utcnow()
        )

    async def async_refresh_all(self) -> None:
        for item in list(self.library.data["items"].values()):
            try:
                await self.fetch_item(item["media_type"], item["tmdb_id"])
            except TMDBError as err:
                _LOGGER.warning("Could not refresh %s: %s", item["title"], err)
                return
        self.changed()

    async def _scheduled_refresh(self, _now: datetime) -> None:
        await self.async_refresh_all()
