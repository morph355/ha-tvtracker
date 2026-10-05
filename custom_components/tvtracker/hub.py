"""The hub: owns the library, TMDB client and per-room session tracking."""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    ADB_POLL_SECONDS,
    MAX_CANDIDATES,
    MAX_SEARCH_HITS,
    MIN_COUNT_SECONDS,
    MIN_SESSION_SECONDS,
    REFRESH_INTERVAL_HOURS,
    SIGNAL_UPDATE,
    STORAGE_KEY,
    TRAKT_HIDE_SECTIONS,
    TRAKT_PUSH_SERVICES,
    STORAGE_VERSION,
    TRAKT_INITIAL_DAYS,
    TRAKT_SYNC_HOURS,
    TITLE_LOOKUP_VERSION,
    TRAKT_SYNC_OVERLAP_DAYS,
    MAX_PICKER_EPISODES,
    WATCHED_FRACTION,
)
from .library import Library, item_key
from .logic import (
    COUNTRY_SUFFIX,
    MEDIA_SESSION_CMD,
    RoomTracker,
    analyse_trakt_progress,
    build_history_payload,
    canonical_service,
    describe_candidate,
    rank_episode_candidates,
    MIN_EPISODE_SEARCH_CHARS,
    find_episode_by_title,
    norm,
    norm_title,
    observe,
    parse_media_sessions,
    parse_trakt_episode_search,
    parse_trakt_history,
    parse_trakt_watched_movies,
    response_timestamp,
)
from .tmdb import TMDB, TMDBAuthError, TMDBError
from .trakt import TraktAuthError, TraktClient, TraktError

_LOGGER = logging.getLogger(__name__)


class TVTrackerHub:
    def __init__(
        self,
        hass: HomeAssistant,
        tmdb: TMDB,
        rooms: list[dict[str, Any]],
        trakt: TraktClient | None = None,
    ) -> None:
        self.hass = hass
        self.tmdb = tmdb
        self.trakt = trakt
        self._connecting: dict[str, Any] | None = None
        self._connect_task: asyncio.Task | None = None
        self._trakt_lock = asyncio.Lock()
        self._outbox_lock = asyncio.Lock()
        self._sleep = asyncio.sleep
        self.search_text = ""        # what you typed into the "show search" box
        # the "watched up to" picker: the chosen show, its unwatched episodes, the chosen one
        self.picker: dict[str, Any] = {"key": None, "episodes": [], "choice": None, "loading": False}
        self.last_episode_search: dict[str, Any] = {}
        self.rooms = rooms
        self.store: Store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self.library = Library()
        self.trackers = {r["name"]: RoomTracker(r["name"]) for r in rooms}
        self._sessions: dict[str, list[dict[str, Any]]] = {}
        self._polling: set[str] = set()
        self._last_ts: dict[str, str] = {}
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
        self._unsubs.append(
            async_track_time_interval(
                self.hass, self._poll_all_adb, timedelta(seconds=ADB_POLL_SECONDS)
            )
        )
        if self.trakt:
            self._unsubs.append(
                async_track_time_interval(
                    self.hass, self._scheduled_trakt, timedelta(hours=TRAKT_SYNC_HOURS)
                )
            )
            self.hass.async_create_task(self.async_trakt_sync())
        self.hass.async_create_task(self.async_refresh_all())
        self.hass.async_create_task(self.async_track_past_titles())

    async def async_stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        if self._connect_task and not self._connect_task.done():
            self._connect_task.cancel()
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
        def _handle(event: Any) -> None:
            self._evaluate(room)
            # When the ADB entity changes state (play/pause/app switch), look
            # at the media session straight away instead of waiting for the timer.
            old, new = event.data.get("old_state"), event.data.get("new_state")
            if (
                new is not None
                and "adb_response" in new.attributes
                and (old is None or old.state != new.state)
            ):
                self.hass.async_create_task(self._poll_adb(room))

        return _handle

    def _evaluate(self, room: str) -> None:
        entities = next(r["entities"] for r in self.rooms if r["name"] == room)
        states = []
        for entity_id in entities:
            st = self.hass.states.get(entity_id)
            if st is not None:
                states.append({"state": st.state, "attributes": dict(st.attributes)})
        closed = self.trackers[room].update(
            observe(states, self._sessions.get(room)), dt_util.utcnow()
        )
        for session in closed:
            self._record(session)
        self._maybe_resolve_episode(room)
        self.changed()

    def _maybe_resolve_episode(self, room: str) -> None:
        """A TV programme whose title matches no show may be an *episode name*
        (Now TV sends "The Jordan Boys' Legacy"): look it up among tracked shows."""
        cur = self.trackers[room].current
        if (
            cur is None
            or cur["category"] != "tv_movies"
            or not cur.get("title")
            or cur.get("_episode_lookup")
            or cur.get("season")
            or self.library.match_session(cur) is not None
        ):
            return
        cur["_episode_lookup"] = cur["title"]
        self.hass.async_create_task(self._resolve_live_episode(cur))

    async def _resolve_live_episode(self, session: dict[str, Any]) -> None:
        title = session["title"]
        try:
            for key, item in list(self.library.data["items"].items()):
                if item["media_type"] != "tv" or not (item["lists"] or item.get("progress")):
                    continue
                details = item.get("details") or {}
                last = (details.get("last_aired") or {}).get("season")
                wanted = {s for s in (last, (last or 0) - 1, (item.get("progress") or {}).get("season")) if s}
                episodes = await self.episode_names(item, wanted)
                hits = find_episode_by_title(episodes, title)
                if len(hits) == 1:
                    hit = hits[0]
                    session.update(
                        series_title=item["title"], season=hit["season"], episode=hit["episode"],
                        subtitle=hit["name"],
                    )
                    _LOGGER.info("Recognised %s as %s S%sE%s", title, item["title"], hit["season"], hit["episode"])
                    self.changed()
                    return
            await self._find_episode_on_trakt(session)
        except (TMDBError, TraktError) as err:
            _LOGGER.debug("Episode lookup for %s failed: %s", title, err)

    async def search_trakt_episodes(self, title: str) -> list[dict[str, Any]]:
        """Episodes anywhere on Trakt with exactly this title (needs Trakt set up).

        Text search can be fussy about punctuation, so the title is tried as sent, with
        a straight apostrophe, and with punctuation removed. `last_episode_search`
        keeps what came back, so a miss can be explained.
        """
        self.last_episode_search = {"title": title, "queries": [], "returned": 0, "sample": []}
        if self.trakt is None or len(norm(title)) < MIN_EPISODE_SEARCH_CHARS:
            return []
        try:
            token = await self._trakt_access_token() if self.trakt_status == "connected" else None
        except TraktError:
            token = None
        variants = [title, title.replace("\u2019", "'").replace("\u2018", "'"),
                    re.sub(r"[^\w\s]", " ", title)]
        seen: list[str] = []
        raw: list[dict[str, Any]] = []
        for query in variants:
            query = " ".join(query.split())
            if not query or query in seen:
                continue
            seen.append(query)
            raw.extend(await self.trakt.search_episodes(query, token))
            if parse_trakt_episode_search(raw, title):
                break
        self.last_episode_search.update(
            queries=seen, returned=len(raw),
            sample=[f"{(h.get('show') or {}).get('title')}: {(h.get('episode') or {}).get('title')}" for h in raw[:5]],
        )
        return parse_trakt_episode_search(raw, title)

    async def _find_episode_on_trakt(self, session: dict[str, Any]) -> None:
        """Nothing you track has this episode: ask Trakt which show it belongs to.

        One exact match is used (show added, viewing labelled 'probably'). Several
        become numbered options for you to choose from; nothing is added anywhere
        until you do. Neither is sent to Trakt until confirmed.
        """
        hits = await self.search_trakt_episodes(session["title"])
        if not hits or len(hits) > MAX_SEARCH_HITS:
            return
        if len(hits) > 1:
            session["_candidates"] = await self._rank_candidates(hits, session)
            _LOGGER.info("%s could be any of %d shows; waiting for you to choose", session["title"], len(hits))
            self.changed()
            return
        hit = hits[0]
        key = item_key("tv", hit["tmdb_id"])
        if key not in self.library.data["items"]:
            await self.fetch_item("tv", hit["tmdb_id"])
        session.update(
            series_title=self.library.data["items"][key]["title"], season=hit["season"],
            episode=hit["episode"], subtitle=hit["name"], _found=True,
        )
        _LOGGER.info("Probably %s S%sE%s (found via Trakt search)", hit["show"], hit["season"], hit["episode"])
        self.changed()

    async def _rank_candidates(self, hits: list[dict[str, Any]], session: dict[str, Any]) -> list[dict[str, Any]]:
        """Add what helps you choose (do you track it, is it on this service, when it
        aired, how long it is), then put the likeliest first. Nothing is stored in the library."""
        service = session["service"]
        minutes = (session.get("duration_ms") or 0) / 60000 or None
        out: list[dict[str, Any]] = []
        for hit in hits:
            existing = self.library.data["items"].get(item_key("tv", hit["tmdb_id"]))
            cand = {**hit, "tracked": bool(existing and (existing["lists"] or existing.get("progress"))),
                    "on_service": False, "air_date": None, "runtime": None}
            try:
                _, providers = await self.tmdb.details("tv", hit["tmdb_id"])
                names = {canonical_service(n) for k in ("flatrate", "ads", "free") for n in providers.get(k, [])}
                cand["on_service"] = service in names
                for ep in await self.tmdb.season_episodes(hit["tmdb_id"], hit["season"]):
                    if ep["episode"] == hit["episode"]:
                        cand["air_date"], cand["runtime"] = ep["air_date"], ep.get("runtime")
            except TMDBError:
                pass  # keep the option, just without the extra hints
            out.append(cand)
        ranked = rank_episode_candidates(out, session.get("start"), minutes)[:MAX_CANDIDATES]
        for cand in ranked:
            cand["hint"] = describe_candidate(cand, service)
        return ranked

    async def episode_names(self, item: dict[str, Any], seasons: Any = None) -> list[dict[str, Any]]:
        """Episode names for a show, fetched from TMDB and cached on the item.

        `seasons`: which seasons (default all). The latest aired season is always
        refreshed, since new episodes appear there.
        """
        details = item.get("details") or {}
        available = sorted(int(s) for s in (details.get("seasons") or {}))
        wanted = sorted(set(available) & {int(s) for s in (seasons if seasons is not None else available)})
        latest = (details.get("last_aired") or {}).get("season")
        cache = item.setdefault("episodes", {})
        out: list[dict[str, Any]] = []
        for season in wanted:
            if str(season) not in cache or season >= (latest or 0):
                cache[str(season)] = await self.tmdb.season_episodes(item["tmdb_id"], season)
            out.extend({**e, "season": season} for e in cache[str(season)])
        return out

    # ---- ADB media-session polling --------------------------------------
    def _adb_entity(self, room: str) -> str | None:
        """The room's Android Debug Bridge entity, if it has one and is in use."""
        for entity_id in next(r["entities"] for r in self.rooms if r["name"] == room):
            st = self.hass.states.get(entity_id)
            if (
                st is not None
                and "adb_response" in st.attributes
                and st.state in ("playing", "paused", "on", "buffering")
            ):
                return entity_id
        return None

    async def _poll_adb(self, room: str) -> None:
        entity_id = self._adb_entity(room)
        if entity_id is None:
            if self._sessions.pop(room, None) is not None:
                self._evaluate(room)
            return
        if room in self._polling:
            return
        self._polling.add(room)
        try:
            await self.hass.services.async_call(
                "androidtv",
                "adb_command",
                {"entity_id": entity_id, "command": MEDIA_SESSION_CMD},
                blocking=True,
            )
            st = self.hass.states.get(entity_id)
            text = st.attributes.get("adb_response") if st else None
            stamp = response_timestamp(text)
            if stamp is None or stamp == self._last_ts.get(room):
                # No fresh answer: don't trust an old one.
                self._sessions[room] = []
            else:
                self._last_ts[room] = stamp
                self._sessions[room] = parse_media_sessions(text)
            self._evaluate(room)
        except Exception as err:  # noqa: BLE001 - never let a poll break tracking
            _LOGGER.debug("ADB poll for %s failed: %s", room, err)
        finally:
            self._polling.discard(room)

    async def _poll_all_adb(self, _now: datetime) -> None:
        for room in self.trackers:
            if self.trackers[room].current is not None or self._adb_entity(room):
                await self._poll_adb(room)

    def now_watching(self, room: str) -> dict[str, Any] | None:
        return self.trackers[room].current

    def _record(self, session: dict[str, Any]) -> None:
        """Log a finished session and update the watchlist from it."""
        seconds = (session["end"] - session["start"]).total_seconds()
        if seconds < MIN_SESSION_SECONDS:
            return
        lib = self.library
        if self._needs_lookup(session):
            self.hass.async_create_task(self._lookup_then_record(session))
            return
        key = lib.apply_session(session, MIN_COUNT_SECONDS)
        if key is None and session.get("_candidates"):
            # Several shows might be the one: nothing is applied yet, but work out now
            # whether enough was watched, so choosing later can set progress.
            fraction = lib.watched_fraction(session, {"media_type": "tv", "details": {}}, seconds)
            session["_fraction"] = fraction
            session["_counted"] = (
                fraction >= WATCHED_FRACTION if fraction is not None else seconds >= MIN_COUNT_SECONDS
            )
        if session["category"] != "youtube" and lib.add_service(session["service"]):
            _LOGGER.info("Added new streaming service: %s", session["service"])
        item = lib.data["items"].get(key) if key else None
        guess = session.get("_guess") if session.get("_counted") else None
        if guess and session["service"] in TRAKT_PUSH_SERVICES:
            # The TV gave only a show title, so the episode is a guess; Trakt can't be
            # told until you've confirmed it.
            session.update(season=guess["season"], episode=guess["episode"])
        if key and session.get("_counted") and self.trakt:
            self._queue_session_for_trakt(key, session)
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
                "episode_title": (
                    session.get("subtitle") or (session.get("title") if item else None)
                ),
                "season": session.get("season"),
                "episode": session.get("episode"),
                "channel": session.get("channel"),
                "item_key": key,
                "probable": (bool(session.get("_found")) and key is not None)
                or bool(session.get("_candidates"))
                or bool(guess and session["service"] in TRAKT_PUSH_SERVICES),
                "previous_progress": guess and {"progress": guess["previous"], "source": guess["previous_source"]},
                "candidates": session.get("_candidates"),
                "counted": bool(session.get("_counted")),
                "watched_pct": (
                    round(session["_fraction"] * 100)
                    if session.get("_fraction") is not None
                    else None
                ),
                "source": "auto",
            }
        )
        self.changed()

    def _needs_lookup(self, session: dict[str, Any]) -> bool:
        """A programme titled like a show we don't track yet (BBC iPlayer sends just
        "Colin from Accounts"): look it up on TMDB so it is tracked from now on."""
        return bool(
            self.tmdb is not None
            and session["category"] == "tv_movies"
            and session.get("title")
            and not session.get("_looked_up")
            and not session.get("_candidates")
            and self.library.match_session(session) is None
        )

    async def _find_on_tmdb(self, title: str) -> dict[str, Any] | None:
        """The one TMDB show/film with exactly this title (also tried without a
        country suffix like "US"); None when there's none, or several."""
        suffix = COUNTRY_SUFFIX.search(title)
        country = suffix and {"US": "US", "UK": "GB", "AU": "AU", "CA": "CA"}.get(
            suffix.group(0).strip(" ()").upper()
        )
        for query in dict.fromkeys([title, COUNTRY_SUFFIX.sub("", title).strip()]):
            exact = [
                r for r in await self.tmdb.search(query, None)
                if norm_title(r["title"]) == norm_title(query)
            ]
            if len(exact) > 1 and country:
                # "Ghosts US": of the shows called Ghosts, the American one
                exact = [r for r in exact if country in (r.get("origin_country") or [])]
            if len(exact) == 1:  # more than one (two shows, one title) is not a safe guess
                return exact[0]
        return None

    async def _lookup_then_record(self, session: dict[str, Any]) -> None:
        session["_looked_up"] = True
        title = session["title"]
        try:
            hit = await self._find_on_tmdb(title)
            if hit:
                await self.fetch_item(hit["media_type"], hit["tmdb_id"])
                _LOGGER.info("Now tracking %s (seen on %s)", hit["title"], session["service"])
        except TMDBError as err:
            _LOGGER.debug("TMDB lookup for %s failed: %s", title, err)
        self._record(session)

    async def async_track_past_titles(self) -> dict[str, Any]:
        """Viewings logged with a show's title but no show (from before new shows
        were tracked, e.g. BBC iPlayer's "The Celebrity Traitors"): track the show,
        link the viewings to it, and mark the episode as unknown so it is offered
        under "Where are you up to?". Nothing is sent to Trakt. Each title is
        looked up once."""
        lib = self.library
        # Titles already looked up, remembered per version of the matching rules, so a
        # title that couldn't be matched before (e.g. "Ghosts US") is retried once
        # the rules improve.
        tried: list[str] = lib.data.setdefault(f"looked_up_titles_v{TITLE_LOOKUP_VERSION}", [])
        for old in [k for k in lib.data if k.startswith("looked_up_titles") and k != f"looked_up_titles_v{TITLE_LOOKUP_VERSION}"]:
            del lib.data[old]
        result: dict[str, Any] = {"tracked": [], "not_found": []}
        rows = [
            h for h in lib.data["history"]
            if h.get("category") == "tv_movies" and h.get("title") and not h.get("item_key")
            and h.get("source") == "auto" and not h.get("candidates") and not h.get("probable")
        ]
        for title in dict.fromkeys(h["title"] for h in rows):
            if norm_title(title) in tried:
                continue
            try:
                hit = await self._find_on_tmdb(title)
                item = await self.fetch_item("tv", hit["tmdb_id"]) if hit and hit["media_type"] == "tv" else None
            except TMDBError as err:
                _LOGGER.debug("TMDB lookup for %s failed: %s", title, err)
                continue  # try again next time
            tried.append(norm_title(title))
            if item is None:  # films are left alone for now
                result["not_found"].append(title)
                continue
            for h in rows:
                if h["title"] == title:
                    h.update(item_key=item["key"], title=item["title"])
                    if h.get("service"):
                        lib.set_watch_service(item["key"], h["service"])
                    if (h.get("end") or "") > (item.get("last_watched") or ""):
                        item["last_watched"] = h["end"]
            if not item.get("progress"):
                item["episode_unknown"] = True
            result["tracked"].append(item["title"])
        if result["tracked"]:
            _LOGGER.info("Now tracking from past viewings: %s", ", ".join(result["tracked"]))
        self.changed()
        return result

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
            except TMDBAuthError as err:
                _LOGGER.warning("Could not refresh from TMDB: %s", err)
                break
            except TMDBError as err:  # one bad item mustn't stop the rest being refreshed
                _LOGGER.warning("Could not refresh %s: %s", item["title"], err)
        self.changed()

    async def _scheduled_refresh(self, _now: datetime) -> None:
        await self.async_refresh_all()

    # ---- Trakt -------------------------------------------------------------
    @property
    def trakt_status(self) -> str:
        if self.trakt is None:
            return "not_configured"
        if self._connecting:
            return "waiting_for_approval"
        tokens = self.library.data.get("trakt") or {}
        return "connected" if tokens.get("refresh_token") else "not_connected"

    @property
    def trakt_info(self) -> dict[str, Any]:
        tokens = self.library.data.get("trakt") or {}
        info: dict[str, Any] = {"last_sync": tokens.get("last_sync")}
        if tokens.get("last_result"):
            info["last_result"] = tokens["last_result"]
        info["waiting_to_send"] = len(self.library.data.get("trakt_outbox") or [])
        if tokens.get("last_error"):
            info["last_error"] = tokens["last_error"]
        if self._connecting:
            info["user_code"] = self._connecting["user_code"]
            info["verification_url"] = self._connecting["verification_url"]
        return info

    def _notify(self, notification_id: str, title: str, message: str) -> None:
        persistent_notification.async_create(
            self.hass, message, title=title, notification_id=f"tvtracker_{notification_id}"
        )

    async def async_trakt_connect(self) -> dict[str, Any]:
        """Start the device-code login. The user approves at trakt.tv/activate."""
        if self.trakt is None:
            raise ValueError(
                "Add your Trakt client ID and secret first "
                "(Settings > Devices & services > TV Tracker > Configure)"
            )
        code = await self.trakt.device_code()
        reply = {
            "user_code": code["user_code"],
            "verification_url": code.get("verification_url", "https://trakt.tv/activate"),
        }
        self._connecting = dict(reply)
        self._notify(
            "trakt",
            "Connect Trakt",
            f"Go to {reply['verification_url']} and enter the code "
            f"**{reply['user_code']}** to connect TV Tracker to your Trakt account.",
        )
        if self._connect_task and not self._connect_task.done():
            self._connect_task.cancel()
        self._connect_task = self.hass.async_create_task(self._poll_device(code))
        self.changed()
        return reply

    async def _poll_device(self, code: dict[str, Any]) -> None:
        interval = max(int(code.get("interval") or 5), 1)
        deadline = dt_util.utcnow() + timedelta(seconds=int(code.get("expires_in") or 600))
        outcome = "expired"
        try:
            while dt_util.utcnow() < deadline:
                await self._sleep(interval)
                state, tokens = await self.trakt.poll_token(code["device_code"])
                if state == "ok" and tokens:
                    self._store_trakt_tokens(tokens, connecting=True)
                    outcome = "ok"
                    break
                if state == "slow_down":
                    interval += 5
                elif state in ("expired", "denied", "invalid"):
                    outcome = state
                    break
        except TraktError as err:
            _LOGGER.warning("Trakt login failed: %s", err)
            outcome = "error"
        except asyncio.CancelledError:
            raise
        finally:
            self._connecting = None
        if outcome == "ok":
            self._notify("trakt", "Trakt connected", "TV Tracker is now connected to Trakt.")
            self.changed()
            await self.async_trakt_sync()
        else:
            self._notify(
                "trakt",
                "Trakt not connected",
                f"The Trakt login did not complete ({outcome}). "
                "Run the tvtracker.trakt_connect action to try again.",
            )
            self.changed()

    def _store_trakt_tokens(self, tokens: dict[str, Any], connecting: bool = False) -> None:
        old = self.library.data.get("trakt") or {}
        created = float(tokens.get("created_at") or dt_util.utcnow().timestamp())
        self.library.data["trakt"] = {
            "access_token": tokens["access_token"],
            "refresh_token": tokens["refresh_token"],
            "expires_at": created + float(tokens.get("expires_in") or 7 * 86400),
            "connected_at": (dt_util.utcnow().isoformat() if connecting else old.get("connected_at")),
            "last_sync": None if connecting else old.get("last_sync"),
            "seen": [] if connecting else old.get("seen", []),
        }
        self.changed()

    async def async_trakt_disconnect(self) -> None:
        self.library.data.pop("trakt", None)
        self.changed()

    async def _trakt_access_token(self) -> str:
        tokens = self.library.data.get("trakt") or {}
        if not tokens.get("refresh_token"):
            raise TraktAuthError("Trakt is not connected")
        if tokens["expires_at"] - dt_util.utcnow().timestamp() < 86400:
            self._store_trakt_tokens(await self.trakt.refresh(tokens["refresh_token"]))
            tokens = self.library.data["trakt"]
        return tokens["access_token"]

    async def async_trakt_sync(self) -> dict[str, Any]:
        """Pull new watches from Trakt and apply them to the library."""
        result: dict[str, Any] = {
            "status": self.trakt_status,
            "fetched": 0,            # watches Trakt returned
            "without_tmdb_id": 0,    # ...that we can't use (no TMDB id / not an episode or movie)
            "already_applied": 0,    # ...seen on an earlier sync
            "applied": 0,
            "new_items": 0,
            "titled_history": 0,
        }
        if self.trakt is None or self.trakt_status != "connected":
            return result
        await self._flush_outbox()
        async with self._trakt_lock:
            tokens = self.library.data["trakt"]
            now = dt_util.utcnow()
            try:
                access = await self._trakt_access_token()
                if tokens.get("last_sync"):
                    since = datetime.fromisoformat(tokens["last_sync"]) - timedelta(
                        days=TRAKT_SYNC_OVERLAP_DAYS
                    )
                else:
                    since = now - timedelta(days=TRAKT_INITIAL_DAYS)
                since_text = since.strftime("%Y-%m-%dT%H:%M:%S.000Z")
                raw = await self.trakt.history(access, since_text)
                events = parse_trakt_history(raw)
                result["fetched"] = len(raw)
                result["without_tmdb_id"] = len(raw) - len(events)
                result["since"] = since_text
            except TraktAuthError as err:
                self.library.data["trakt"] = {**tokens, "refresh_token": None, "last_error": str(err)}
                self._notify("trakt", "Trakt disconnected", f"{err}. Run tvtracker.trakt_connect.")
                self.changed()
                result["status"] = self.trakt_status
                return result
            except TraktError as err:
                _LOGGER.warning("Trakt sync failed: %s", err)
                tokens["last_error"] = str(err)
                self.changed()
                result["error"] = str(err)
                return result

            tokens = self.library.data["trakt"]
            seen: list = list(tokens.get("seen", []))
            for ev in events:
                if ev["trakt_id"] in seen:
                    result["already_applied"] += 1
                    continue
                key = item_key(ev["media_type"], ev["tmdb_id"])
                if key not in self.library.data["items"]:
                    try:
                        await self.fetch_item(ev["media_type"], ev["tmdb_id"])
                    except TMDBError as err:
                        _LOGGER.warning("Skipping Trakt item %s: %s", ev["title"], err)
                        continue
                    result["new_items"] += 1
                self.library.apply_trakt_event(key, ev)
                if self.library.attach_history_title(key, ev):
                    result["titled_history"] += 1
                seen.append(ev["trakt_id"])
                result["applied"] += 1
            tokens.update(
                seen=seen[-3000:],
                last_sync=now.isoformat(),
                last_error=None,
                last_result={k: v for k, v in result.items() if k != "status"},
            )
            self.changed()
        return result

    async def _scheduled_trakt(self, _now: datetime) -> None:
        await self.async_trakt_sync()

    # ---- writing to Trakt: additions and hiding only, never deletions ------------
    @property
    def trakt_outbox(self) -> list[dict[str, Any]]:
        return self.library.data.setdefault("trakt_outbox", [])

    def _queue_session_for_trakt(self, key: str, session: dict[str, Any]) -> None:
        """A finished viewing on a service Trakt doesn't sync itself, where we know
        exactly what it was, is added to Trakt (Netflix/Disney+/Prime/Apple TV are
        left to Trakt's own sync so nothing is duplicated)."""
        if session["service"] not in TRAKT_PUSH_SERVICES:
            return
        item = self.library.data["items"][key]
        when = session["end"].strftime("%Y-%m-%dT%H:%M:%S.000Z")
        if item["media_type"] == "movie":
            entry = {"media_type": "movie", "tmdb_id": item["tmdb_id"], "watched_at": when}
        else:
            progress = item.get("progress")
            if not progress or item.get("progress_source") not in ("reported", "inferred"):
                return  # we only counted it on; not sure enough to write to Trakt
            entry = {
                "media_type": "tv", "tmdb_id": item["tmdb_id"], "watched_at": when,
                "season": progress["season"], "episode": progress["episode"],
            }
        if entry not in self.trakt_outbox:
            self.trakt_outbox.append(entry)
            self.hass.async_create_task(self._flush_outbox())

    def queue_manual_watch(self, item: dict[str, Any], data: dict[str, Any], service: str | None, end: datetime) -> bool:
        """A viewing you logged yourself is certain: queue it for Trakt, but only on
        services Trakt doesn't sync itself (otherwise it would be a duplicate)."""
        if not (self.trakt and service in TRAKT_PUSH_SERVICES):
            return False
        when = end.astimezone(dt_util.UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        if item["media_type"] == "movie":
            entry = {"media_type": "movie", "tmdb_id": item["tmdb_id"], "watched_at": when}
        elif data.get("season") and data.get("episode"):
            entry = {"media_type": "tv", "tmdb_id": item["tmdb_id"], "season": data["season"],
                     "episode": data["episode"], "watched_at": when}
        else:
            return False
        if entry not in self.trakt_outbox:
            self.trakt_outbox.append(entry)
        return True

    async def flush_outbox(self) -> dict[str, Any]:
        return await self._flush_outbox()

    async def _flush_outbox(self) -> dict[str, Any]:
        """Send everything queued for Trakt. Failures stay queued and are retried."""
        result: dict[str, Any] = {"sent": 0, "queued": len(self.trakt_outbox)}
        self.changed()  # the "waiting to send" count on the sensor
        if self.trakt is None or self.trakt_status != "connected" or not self.trakt_outbox:
            return result
        async with self._outbox_lock:
            batch = list(self.trakt_outbox)
            try:
                access = await self._trakt_access_token()
                reply = await self.trakt.add_history(access, build_history_payload(batch))
            except TraktAuthError as err:
                self._trakt_lost_login(err)
                result["error"] = str(err)
                return result
            except TraktError as err:
                _LOGGER.warning("Trakt: could not send watches, will retry: %s", err)
                result["error"] = str(err)
                self.changed()
                return result
            self.library.data["trakt_outbox"] = [e for e in self.trakt_outbox if e not in batch]
            result.update(sent=len(batch), queued=len(self.trakt_outbox), trakt_reply=reply.get("added"))
            self.changed()
        return result

    def _trakt_lost_login(self, err: Exception) -> None:
        tokens = self.library.data.get("trakt") or {}
        self.library.data["trakt"] = {**tokens, "refresh_token": None, "last_error": str(err)}
        self._notify("trakt", "Trakt disconnected", f"{err}. Run tvtracker.trakt_connect.")
        self.changed()

    # ---- "watched up to" picker -------------------------------------------
    def picker_shows(self) -> dict[str, str]:
        """Shows to choose from (label -> item key): first the ones you've watched where
        we don't know which episode you're on (only the show's title was seen), then
        those with a new episode available to watch (you may have watched it somewhere
        we couldn't see, like Now TV's live channel); most recently watched first in
        each. The one already chosen stays until you've used it."""
        lib, today = self.library, dt_util.now().date()
        unknown: list[dict[str, Any]] = []
        available: list[dict[str, Any]] = []
        for k, it in lib.data["items"].items():
            if it["media_type"] != "tv" or it.get("hidden"):
                continue
            v = lib.view(k, today)
            if lib.episode_unknown(k):
                unknown.append(v)
            elif v["group"] == "available" or k == self.picker.get("key"):
                available.append(v)
        out: dict[str, str] = {}
        for group in (unknown, available):
            for v in sorted(group, key=lambda v: v["last_watched"] or "", reverse=True):
                label = v["title"] if v["title"] not in out else f"{v['title']} ({v['year']})"
                out[label] = v["key"]
        return out

    async def async_pick_show(self, key: str) -> None:
        """Load the chosen show's unwatched episodes (names from TMDB)."""
        self.picker = {"key": key, "episodes": [], "choice": None, "loading": True}
        self.changed()
        try:
            await self._load_picker_episodes()
        finally:
            self.picker["loading"] = False
            self.changed()

    async def _load_picker_episodes(self) -> None:
        key = self.picker["key"]
        item = self.library.data["items"].get(key or "")
        if item is None:
            return
        start = (item.get("progress") or {}).get("season") or 1
        seasons = [int(x) for x in (item.get("details") or {}).get("seasons") or {} if int(x) >= start]
        try:
            names = await self.episode_names(item, seasons)
        except TMDBError as err:
            _LOGGER.warning("Could not load the episodes of %s: %s", item["title"], err)
            names = []
        # too many to list: keep the newest (the one you've reached is usually recent)
        eps = self.library.unwatched_episodes(key, names, dt_util.now().date())[-MAX_PICKER_EPISODES:]
        self.picker["episodes"] = [
            {"label": f"S{e['season']}E{e['episode']} · {e['name'] or 'Episode ' + str(e['episode'])}",
             "season": e["season"], "episode": e["episode"], "air_date": e.get("air_date")}
            for e in eps
        ]
        self.picker["choice"] = None

    async def async_watched_up_to(self, key: str, season: int, episode: int, trakt: bool = True) -> dict[str, Any]:
        """You've watched everything up to and including S<season>E<episode>: set that
        as where you are, and add any of those episodes Trakt hasn't got (never deletes)."""
        item = self.library.get_item(key)
        self.library.set_progress(key, season, episode, dt_util.utcnow(), source="manual")
        result: dict[str, Any] = {"title": item["title"], "progress": f"S{season}E{episode}"}
        if trakt and self.trakt is not None and self.trakt_status == "connected":
            result["trakt"] = await self.async_trakt_mark_watched(key, upto=(season, episode))
        if self.picker.get("key") == key:
            await self._load_picker_episodes()
        self.changed()
        return result

    async def async_trakt_mark_watched(
        self, key: str, dry_run: bool = False, upto: tuple[int, int] | None = None
    ) -> dict[str, Any]:
        """Add to Trakt every aired episode (or the film) it doesn't have a watch for,
        or with `upto` only those up to and including that episode.

        Trakt is asked what you've already watched first, so nothing is watched
        twice; new watches are dated to when each episode aired. Never deletes.
        """
        if self.trakt is None or self.trakt_status != "connected":
            return {"status": self.trakt_status, "added": 0}
        item = self.library.get_item(key)
        extra: dict[str, Any] = {}
        try:
            access = await self._trakt_access_token()
            if item["media_type"] == "movie":
                have = parse_trakt_watched_movies(await self.trakt.watched_movies(access))
                todo = [] if item["tmdb_id"] in have else [{
                    "media_type": "movie", "tmdb_id": item["tmdb_id"],
                    "watched_at": dt_util.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000Z")}]
                already = 1 if item["tmdb_id"] in have else 0
            else:
                trakt_id = await self.trakt.find_show(access, item["tmdb_id"])
                if trakt_id is None:
                    return {"status": "connected", "added": 0,
                            "error": f"Trakt couldn't find this show (TMDB id {item['tmdb_id']}); nothing was sent"}
                info = analyse_trakt_progress(await self.trakt.show_progress(access, trakt_id))
                todo = [
                    {"media_type": "tv", "tmdb_id": item["tmdb_id"], "ids": {"trakt": trakt_id},
                     "season": s, "episode": e, "watched_at": "released"}
                    for s, e in info["missing"]
                    if upto is None or (s, e) <= tuple(upto)
                ]
                already = info["completed"]
                extra = {
                    "trakt_aired": info["aired"],
                    "last_watched_on_trakt": (
                        f"S{info['last_completed'][0]}E{info['last_completed'][1]}"
                        if info["last_completed"] else None
                    ),
                }
        except TraktAuthError as err:
            self._trakt_lost_login(err)
            return {"status": self.trakt_status, "added": 0, "error": str(err)}
        except TraktError as err:
            return {"status": self.trakt_status, "added": 0, "error": str(err)}

        result: dict[str, Any] = {
            "status": "connected",
            "already_on_trakt": already,
            "to_add": len(todo),
            "episodes": (
                [f"S{t['season']}E{t['episode']}" for t in todo[:40]]
                + ([f"... and {len(todo) - 40} more"] if len(todo) > 40 else [])
                if item["media_type"] == "tv" else []
            ),
            **extra,
        }
        if dry_run or not todo:
            result["added"] = 0
            result["dry_run"] = dry_run
            return result
        for entry in todo:
            if entry not in self.trakt_outbox:
                self.trakt_outbox.append(entry)
        sent = await self._flush_outbox()
        result["added"] = sent["sent"]
        result["still_queued"] = sent["queued"]
        if sent.get("error"):
            result["error"] = sent["error"]
        return result

    def pending_matches(self) -> list[dict[str, Any]]:
        """Viewings waiting for you (a 'probably', or several options), newest first.
        One you skipped goes behind those you haven't."""
        rows = [h for h in reversed(self.library.data["history"]) if h.get("probable")]
        rows.sort(key=lambda h: h.get("skips", 0))
        return rows

    def _pending_entry(self, entry_id: str) -> dict[str, Any]:
        """A history entry by id, or "latest" for the one at the front of the queue."""
        if entry_id == "latest":
            pending = self.pending_matches()
            if not pending:
                raise ValueError("Nothing is waiting for confirmation")
            return pending[0]
        entry = next((h for h in self.library.data["history"] if h["id"] == entry_id), None)
        if entry is None:
            raise ValueError("No such history entry")
        if not entry.get("probable"):
            raise ValueError("That viewing isn't waiting for confirmation")
        return entry

    def _send_confirmed(self, entry: dict[str, Any], item: dict[str, Any]) -> bool:
        """Queue a now-certain viewing for Trakt (only for services Trakt doesn't sync)."""
        if not (self.trakt and entry.get("counted") and entry.get("service") in TRAKT_PUSH_SERVICES):
            return False
        end = datetime.fromisoformat(entry["end"]).astimezone(dt_util.UTC)
        new = {"media_type": "tv", "tmdb_id": item["tmdb_id"], "season": entry["season"],
               "episode": entry["episode"], "watched_at": end.strftime("%Y-%m-%dT%H:%M:%S.000Z")}
        if new not in self.trakt_outbox:
            self.trakt_outbox.append(new)
        return True

    async def async_confirm_match(self, entry_id: str) -> dict[str, Any]:
        """You've confirmed a 'probably' viewing: make it certain, and send it to Trakt
        if it is on a service Trakt doesn't sync itself and was watched enough."""
        lib = self.library
        entry = self._pending_entry(entry_id)
        if entry.get("candidates"):
            n = len(entry["candidates"])
            raise ValueError(f"That title fits {n} shows. Choose one (1 to {n}), or type the right show")
        entry["probable"] = False
        item = lib.data["items"].get(entry.get("item_key") or "")
        result: dict[str, Any] = {"title": entry.get("title"), "season": entry.get("season"),
                                  "episode": entry.get("episode"), "sent_to_trakt": False}
        if item and item.get("progress") == {"season": entry.get("season"), "episode": entry.get("episode")}:
            item["progress_source"] = "reported"
        if item and self._send_confirmed(entry, item):
            await self._flush_outbox()
            result["sent_to_trakt"] = self.trakt_status == "connected"
        self.changed()
        return result

    async def async_assign_match(self, entry_id: str, show: str) -> dict[str, Any]:
        """The guess was the wrong show: use the show you name instead.

        The episode is found by the same title within that show (TMDB's episode
        names); the wrongly guessed show's progress is undone, and if it was only
        added because of the guess it is removed again.
        """
        entry = self._pending_entry(entry_id)
        show = (show or "").strip()
        if not show:
            raise ValueError("Type the show's name first")
        name = entry.get("episode_title") or entry.get("title")
        try:
            results = await self.tmdb.search(show, "tv")
            exact = [r for r in results if norm_title(r["title"]) == norm_title(show)]
            picks = exact or results
            if len(picks) != 1 and len(exact) != 1:
                options = "; ".join(f"{r['title']} ({r['year']})" for r in picks[:5]) or "nothing"
                raise ValueError(f"Which show did you mean? TMDB found: {options}")
            pick = (exact or picks)[0]
            item = await self.fetch_item("tv", pick["tmdb_id"])
            hits = find_episode_by_title(await self.episode_names(item), name)
        except TMDBError as err:
            raise ValueError(f"Could not look that up: {err}") from err
        if len(hits) != 1:
            raise ValueError(f"No single episode of {item['title']} is called '{name}'")
        hit = hits[0]

        return await self._make_certain(entry, item, hit["season"], hit["episode"], hit["name"])

    def _undo_guess(self, entry: dict[str, Any], keep: dict[str, Any] | None = None) -> None:
        """Take back the progress a wrong guess set; a show that was only added because
        of the guess (on no list) is removed again."""
        lib = self.library
        old = lib.data["items"].get(entry.get("item_key") or "")
        if (
            old is not None and old is not keep and old.get("progress_source") in ("found", "guess")
            and old.get("progress") == {"season": entry.get("season"), "episode": entry.get("episode")}
        ):
            before = entry.get("previous_progress") or {}
            old["progress"], old["progress_source"] = before.get("progress"), before.get("source")
            if not old["lists"] and not old["progress"]:
                del lib.data["items"][old["key"]]

    async def _make_certain(self, entry: dict[str, Any], item: dict[str, Any], season: int, episode: int, name: str) -> dict[str, Any]:
        """You decided which show/episode it was: record it as certain, set progress if it
        was watched enough, and send it to Trakt where that applies."""
        self._undo_guess(entry, keep=item)
        entry.update(title=item["title"], item_key=item["key"], season=season, episode=episode,
                     episode_title=name, probable=False, candidates=None)
        if entry.get("counted"):
            self.library.set_progress(item["key"], season, episode, entry["end"], only_forward=True, source="reported")
        sent = self._send_confirmed(entry, item)
        if sent:
            await self._flush_outbox()
        self.changed()
        return {"title": item["title"], "season": season, "episode": episode, "episode_title": name,
                "sent_to_trakt": sent and self.trakt_status == "connected"}

    async def async_pick_match(self, entry_id: str, choice: int) -> dict[str, Any]:
        """Choose one of the offered shows for a viewing that fitted several."""
        entry = self._pending_entry(entry_id)
        options = entry.get("candidates") or []
        if not options:
            raise ValueError("This viewing has no options to choose from; use confirm, or type the show")
        if not 1 <= choice <= len(options):
            raise ValueError(f"Choose a number from 1 to {len(options)}")
        pick = options[choice - 1]
        try:
            item = await self.fetch_item("tv", pick["tmdb_id"])
        except TMDBError as err:
            raise ValueError(f"Could not look that show up: {err}") from err
        return await self._make_certain(entry, item, pick["season"], pick["episode"], pick["name"])

    def async_dismiss_match(self, entry_id: str) -> dict[str, Any]:
        """Not any of these / the guess is wrong: stop asking. The viewing stays in the
        history untitled, and a guessed show that was only added for it is removed."""
        entry = self._pending_entry(entry_id)
        self._undo_guess(entry)
        entry.update(probable=False, candidates=None, item_key=None, season=None, episode=None, episode_title=None)
        self.changed()
        return {"dismissed": entry.get("title")}

    def async_skip_match(self, entry_id: str) -> dict[str, Any]:
        """Deal with it later: it goes behind the other viewings that are waiting."""
        entry = self._pending_entry(entry_id)
        entry["skips"] = entry.get("skips", 0) + 1
        self.changed()
        nxt = self.pending_matches()[0]
        return {"skipped": entry.get("title"), "now_first": nxt.get("title")}

    async def async_trakt_hide(self, key: str, hidden: bool = True) -> dict[str, Any]:
        """Hide (or un-hide) a show in Trakt's progress and calendar. History is untouched."""
        item = self.library.get_item(key)
        if self.trakt is None or self.trakt_status != "connected" or item["media_type"] != "tv":
            return {"trakt": "skipped"}
        body = {"shows": [{"ids": {"tmdb": item["tmdb_id"]}}]}
        try:
            access = await self._trakt_access_token()
            for section in TRAKT_HIDE_SECTIONS:
                call = self.trakt.hide if hidden else self.trakt.unhide
                await call(access, section, body)
        except TraktAuthError as err:
            self._trakt_lost_login(err)
            return {"trakt": "error", "error": str(err)}
        except TraktError as err:
            return {"trakt": "error", "error": str(err)}
        return {"trakt": "hidden" if hidden else "shown"}
