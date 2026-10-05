"""The watchlist library: lists, items, progress, history (pure, no HA imports)."""

from __future__ import annotations

import re
import uuid
from datetime import date, datetime, timedelta
from typing import Any

from .const import DEFAULT_SERVICES, FINISHED_SHOWN_DAYS, HISTORY_LIMIT, WATCHED_FRACTION
from .logic import (
    availability,
    canonical_service,
    derive_status,
    infer_season,
    match_score,
    next_episode,
    norm_title,
    up_next_group,
)


def item_key(media_type: str, tmdb_id: int) -> str:
    return f"{media_type}:{int(tmdb_id)}"


def _iso(value: datetime | str | None) -> str | None:
    if value is None:
        return None
    return value.isoformat() if isinstance(value, datetime) else value


def fmt_episode(progress: dict[str, int] | tuple[int, int] | None) -> str | None:
    if not progress:
        return None
    if isinstance(progress, tuple):
        return f"S{progress[0]}E{progress[1]}"
    return f"S{progress['season']}E{progress['episode']}"


class Library:
    """All persisted state. `data` is JSON-serialisable."""

    def __init__(self, data: dict[str, Any] | None = None) -> None:
        self.data: dict[str, Any] = data or {}
        self.data.setdefault("lists", {})
        self.data.setdefault("items", {})
        self.data.setdefault("history", [])
        self.data.setdefault("services", list(DEFAULT_SERVICES))

    # ---- lists -----------------------------------------------------------
    def resolve_list(self, ref: str) -> str:
        """Find a list by id or (case-insensitive) name."""
        lists = self.data["lists"]
        if ref in lists:
            return ref
        for list_id, lst in lists.items():
            if lst["name"].strip().lower() == ref.strip().lower():
                return list_id
        raise ValueError(f"No watchlist called '{ref}'")

    def create_list(self, name: str) -> str:
        name = name.strip()
        if not name:
            raise ValueError("A watchlist needs a name")
        try:
            self.resolve_list(name)
        except ValueError:
            pass
        else:
            raise ValueError(f"A watchlist called '{name}' already exists")
        base = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "list"
        list_id, n = base, 2
        while list_id in self.data["lists"]:
            list_id, n = f"{base}_{n}", n + 1
        self.data["lists"][list_id] = {"name": name}
        return list_id

    def ensure_list(self, name: str) -> str:
        try:
            return self.resolve_list(name)
        except ValueError:
            return self.create_list(name)

    def delete_list(self, ref: str) -> str:
        list_id = self.resolve_list(ref)
        name = self.data["lists"].pop(list_id)["name"]
        for item in self.data["items"].values():
            if list_id in item["lists"]:
                item["lists"].remove(list_id)
        return name

    # ---- items -----------------------------------------------------------
    def upsert_item(
        self,
        media_type: str,
        tmdb_id: int,
        details: dict[str, Any],
        providers: dict[str, list[str]] | None = None,
        now: datetime | str | None = None,
    ) -> dict[str, Any]:
        key = item_key(media_type, tmdb_id)
        item = self.data["items"].get(key)
        if item is None:
            item = {
                "key": key,
                "tmdb_id": int(tmdb_id),
                "media_type": media_type,
                "title": details.get("title"),
                "year": details.get("year"),
                "lists": [],
                "progress": None,
                "watched": False,
                "last_watched": None,
                "added": _iso(now),
                "details": {},
                "providers": {},
            }
            self.data["items"][key] = item
        item["title"] = details.get("title") or item["title"]
        item["year"] = details.get("year") or item["year"]
        item["details"] = details
        if providers is not None:
            item["providers"] = providers
        item["refreshed"] = _iso(now)
        return item

    def get_item(self, key: str) -> dict[str, Any]:
        try:
            return self.data["items"][key]
        except KeyError:
            raise ValueError(f"Unknown item {key}") from None

    def find_by_title(self, title: str, media_type: str | None = None) -> list[str]:
        target = norm_title(title)
        return [
            k
            for k, it in self.data["items"].items()
            if norm_title(it["title"]) == target
            and (media_type is None or it["media_type"] == media_type)
        ]

    def unwatched_episodes(self, key: str, episodes: list[dict[str, Any]], today: date) -> list[dict[str, Any]]:
        """Aired episodes after where you are (all of them if you haven't started)."""
        progress = self.get_item(key).get("progress") or {"season": 0, "episode": 0}
        done = (progress["season"], progress["episode"])
        out = [
            e for e in episodes
            if e["season"] >= 1 and (e["season"], e["episode"]) > done
            and e.get("air_date") and e["air_date"] <= today.isoformat()
        ]
        return sorted(out, key=lambda e: (e["season"], e["episode"]))

    def episode_unknown(self, key: str) -> bool:
        """Watched, but we don't know which episode you're up to: only the show's
        title was seen (a guessed position, or none at all)."""
        item = self.get_item(key)
        if item.get("progress"):
            return item.get("progress_source") == "guess"
        return bool(item.get("episode_unknown"))

    def set_hidden(self, key: str, hidden: bool = True) -> None:
        """Hide a show or film from the dashboard lists (its history is kept)."""
        self.get_item(key)["hidden"] = bool(hidden)

    def set_watch_service(self, key: str, service: str, present: bool = True) -> None:
        """Record (or forget) a service this title is watched on, whatever TMDB says."""
        item = self.get_item(key)
        name = canonical_service(service)
        current = [canonical_service(s) for s in item.get("watch_on") or []]
        if present and name not in current:
            current.append(name)
        elif not present:
            current = [s for s in current if s != name]
        item["watch_on"] = current

    def add_to_list(self, list_ref: str, key: str) -> str:
        list_id = self.resolve_list(list_ref)
        item = self.get_item(key)
        if list_id not in item["lists"]:
            item["lists"].append(list_id)
        return list_id

    def remove_from_list(self, list_ref: str, key: str) -> None:
        list_id = self.resolve_list(list_ref)
        item = self.get_item(key)
        if list_id in item["lists"]:
            item["lists"].remove(list_id)

    def set_progress(
        self,
        key: str,
        season: int,
        episode: int,
        when: datetime | str | None = None,
        only_forward: bool = False,
        source: str = "manual",
    ) -> bool:
        """Record 'watched up to S<season>E<episode>'. Returns True if changed.

        `source` says where the value came from: manual (you said so), trakt,
        reported (the app gave season/episode) or guess (we counted one on).
        """
        from .logic import episode_index  # local: keeps import list short

        item = self.get_item(key)
        if item["media_type"] != "tv":
            raise ValueError(f"{item['title']} is a movie; use mark_watched")
        current = item.get("progress")
        if only_forward and current:
            seasons = {int(k): int(v) for k, v in (item["details"].get("seasons") or {}).items()}
            if episode_index(seasons, season, episode) <= episode_index(
                seasons, current["season"], current["episode"]
            ):
                item["last_watched"] = _iso(when) or item.get("last_watched")
                return False
        item["progress"] = {"season": int(season), "episode": int(episode)}
        item["progress_source"] = source
        item["watched"] = False
        item["last_watched"] = _iso(when) or item.get("last_watched")
        return True

    def mark_watched(
        self, key: str, watched: bool = True, when: datetime | str | None = None
    ) -> None:
        """Movie: watched. TV: up to date, i.e. progress is the latest episode aired.

        A show is never given a permanent "watched" flag: it stays *caught up*
        while it is airing and becomes *watching* again when a new episode airs.
        """
        item = self.get_item(key)
        if item["media_type"] == "movie":
            item["watched"] = watched
            item["last_watched"] = (_iso(when) or item.get("last_watched")) if watched else None
            return
        if watched:
            item["watched"] = False
            item["last_watched"] = _iso(when) or item.get("last_watched")
            last = (item["details"] or {}).get("last_aired")
            if last:
                item["progress"] = {"season": last["season"], "episode": last["episode"]}
                item["progress_source"] = "manual"

    # ---- services --------------------------------------------------------
    def add_service(self, name: str) -> bool:
        name = canonical_service(name)
        if name and name not in self.data["services"]:
            self.data["services"].append(name)
            return True
        return False

    def remove_service(self, name: str) -> bool:
        name = canonical_service(name)
        if name in self.data["services"]:
            self.data["services"].remove(name)
            return True
        return False

    # ---- history ---------------------------------------------------------
    def add_history(self, entry: dict[str, Any]) -> dict[str, Any]:
        entry = {**entry, "id": uuid.uuid4().hex[:8]}
        for field in ("start", "end"):
            entry[field] = _iso(entry.get(field))
        history = self.data["history"]
        history.append(entry)
        history.sort(key=lambda h: h.get("start") or "")
        del history[:-HISTORY_LIMIT]
        return entry

    def delete_history(self, entry_id: str) -> bool:
        before = len(self.data["history"])
        self.data["history"] = [h for h in self.data["history"] if h["id"] != entry_id]
        return len(self.data["history"]) != before

    # ---- live sessions ---------------------------------------------------
    def match_session(self, session: dict[str, Any]) -> str | None:
        """Which watchlist item does a playback session belong to?"""
        if session.get("category") != "tv_movies":
            return None
        title, series = session.get("title"), session.get("series_title")
        matches = [
            k
            for k, it in self.data["items"].items()
            if match_score(
                it["title"], series, title,
                allow_trailing_number=it["media_type"] == "tv",
            )
        ]
        if not matches:
            return None
        # Prefer items that are on a list; if the player reported a series
        # title it's a TV show.
        matches.sort(
            key=lambda k: (
                not self.data["items"][k]["lists"],
                series is not None and self.data["items"][k]["media_type"] != "tv",
            )
        )
        return matches[0]

    def apply_session(
        self, session: dict[str, Any], min_count_seconds: int
    ) -> str | None:
        """Update watchlist progress from a finished session; returns item key."""
        key = self.match_session(session)
        if key is None:
            return None
        if session.get("service"):
            self.set_watch_service(key, session["service"])
        seconds = (session["end"] - session["start"]).total_seconds()
        item = self.data["items"][key]
        fraction = self.watched_fraction(session, item, seconds)
        session["_fraction"] = fraction
        counts = fraction >= WATCHED_FRACTION if fraction is not None else seconds >= min_count_seconds
        if not counts:
            return key
        session["_counted"] = True
        when = session["end"]
        if item["media_type"] == "movie":
            self.mark_watched(key, True, when)
        elif session.get("episode") and not session.get("season"):
            # An episode number without a season (Now TV): infer the season.
            season = infer_season(item["details"], item.get("progress"), session["episode"])
            if season:
                self.set_progress(
                    key, season, session["episode"], when,
                    only_forward=True, source="inferred",
                )
        elif session.get("season") and session.get("episode"):
            self.set_progress(
                key, session["season"], session["episode"], when,
                only_forward=True,
                # a match found by searching Trakt for the episode title is only "probably" right
                source="found" if session.get("_found") else "reported",
            )
        else:
            # Only the show's title: guess the next episode. For a show that is airing
            # now and that we know nothing about yet, the latest one (people mostly
            # watch what's just aired) rather than the first.
            details = item["details"] or {}
            last = details.get("last_aired")
            if item.get("progress") is None and last and details.get("next_air_date"):
                nxt = (last["season"], last["episode"])
            else:
                nxt = next_episode(item["details"], item.get("progress"))
            if not nxt:
                item["episode_unknown"] = True
            if nxt:
                session["_guess"] = {
                    "season": nxt[0], "episode": nxt[1],
                    "previous": item.get("progress"), "previous_source": item.get("progress_source"),
                }
                self.set_progress(key, nxt[0], nxt[1], when, source="guess")
        return key

    @staticmethod
    def watched_fraction(session: dict[str, Any], item: dict[str, Any], seconds: float) -> float | None:
        """How much of the programme was watched (0..1), or None if unknowable.

        Best: the playback position over the length the TV reported (that also
        copes with resuming part-way through). Next: time watched over TMDB's
        runtime. Otherwise None, and the caller uses a minimum-minutes rule.
        """
        duration, position = session.get("duration_ms"), session.get("final_pos_ms")
        if duration and duration >= 60_000 and position is not None:
            return min(position / duration, 1.0)
        details = item.get("details") or {}
        runtime = details.get("episode_runtime") if item["media_type"] == "tv" else details.get("runtime")
        if runtime:
            return min(seconds / 60 / runtime, 1.0)
        return None

    # ---- Trakt -------------------------------------------------------------
    def apply_trakt_event(self, key: str, event: dict[str, Any]) -> None:
        """Apply one Trakt watch (see logic.parse_trakt_history) to an item.

        Trakt knows the real season/episode, so it replaces a *guessed*
        position (even backwards) but otherwise only ever moves progress
        forward, so a re-watch or something you set by hand isn't undone.
        """
        item = self.get_item(key)
        when = event.get("watched_at")
        if item["media_type"] == "movie":
            self.mark_watched(key, True, when)
            return
        guessed = item.get("progress_source") in ("guess", "inferred", "found")
        self.set_progress(
            key, event["season"], event["episode"], when,
            only_forward=not guessed, source="trakt",
        )

    def attach_history_title(self, key: str, event: dict[str, Any]) -> bool:
        """Give a TV-detected viewing with no title the show Trakt says it was.

        Trakt records no room and (for streaming syncs) often only a date, so
        match on: same service carries the title, and within a day of the watch.
        Each untitled viewing is claimed at most once.
        """
        item = self.get_item(key)
        try:
            when = datetime.fromisoformat((event.get("watched_at") or "").replace("Z", "+00:00"))
        except ValueError:
            return False
        providers = item.get("providers") or {}
        carried = {
            canonical_service(n)
            for kind in ("flatrate", "ads", "free")
            for n in providers.get(kind, [])
        }
        for h in reversed(self.data["history"]):
            if h.get("item_key") or h.get("title") or h.get("category") != "tv_movies":
                continue
            if h.get("source") != "auto" or canonical_service(h.get("service") or "") not in carried:
                continue
            try:
                start = datetime.fromisoformat(h["start"])
            except (KeyError, ValueError):
                continue
            if start.tzinfo is None:
                start = start.replace(tzinfo=when.tzinfo)
            if abs((start - when).total_seconds()) <= 36 * 3600:
                h.update(
                    title=item["title"],
                    item_key=key,
                    season=event.get("season"),
                    episode=event.get("episode"),
                    episode_title=event.get("episode_title"),
                )
                return True
        return False

    # ---- views -----------------------------------------------------------
    def view(self, key: str, today: date) -> dict[str, Any]:
        item = self.data["items"][key]
        status = derive_status(item, today)
        avail = availability(item, self.data["services"], today)
        nxt = None
        if item["media_type"] == "tv" and status in ("watching", "want_to_watch"):
            nxt = next_episode(item["details"], item.get("progress"))
        details = item.get("details") or {}
        return {
            "key": key,
            "title": item["title"],
            "type": item["media_type"],
            "year": item.get("year"),
            "status": status,
            "progress": fmt_episode(item.get("progress")),
            "next": fmt_episode(nxt),
            "next_air_date": details.get("next_air_date"),
            "availability": avail["text"],
            "on_my_services": avail["on_my_services"],
            "poster": details.get("poster"),
            "lists": [self.data["lists"][i]["name"] for i in item["lists"] if i in self.data["lists"]],
            "last_watched": item.get("last_watched"),
            "hidden": bool(item.get("hidden")),
            # the services you've actually watched it on (learned, or set by you)
            "watched_on": list(item.get("watch_on") or []),
            "group": up_next_group(status, details.get("next_air_date"), details.get("announced_season")),
            "announced_season": details.get("announced_season"),
            # when the next episode (or the announced season) is expected, if known
            "expected": details.get("next_air_date")
            or (details.get("announced_season") or {}).get("air_date"),
        }

    def watchlists(self, today: date) -> dict[str, list[dict[str, Any]]]:
        out: dict[str, list[dict[str, Any]]] = {
            lst["name"]: [] for lst in self.data["lists"].values()
        }
        for key, item in self.data["items"].items():
            if item.get("hidden"):
                continue
            for list_id in item["lists"]:
                if list_id in self.data["lists"]:
                    out[self.data["lists"][list_id]["name"]].append(self.view(key, today))
        order = {"watching": 0, "caught_up": 1, "want_to_watch": 2, "upcoming": 3, "finished": 4}
        for views in out.values():
            views.sort(key=lambda v: (order.get(v["status"], 9), v["title"].lower()))
        return out

    def continue_watching(self, today: date) -> list[dict[str, Any]]:
        views = [self.view(k, today) for k in self.data["items"]]
        recent = (today - timedelta(days=FINISHED_SHOWN_DAYS)).isoformat()
        started = [
            v for v in views
            # shows only, for now: films don't have an "up next"
            if v["type"] == "tv" and not v["hidden"] and (
                v["status"] in ("watching", "caught_up")
                # a finished show stays a while, under "Finished"
                or (v["status"] == "finished" and (v["last_watched"] or "") >= recent)
            )
        ]
        # Most recently watched first, then "watching" ahead of "caught up".
        started.sort(key=lambda v: v["last_watched"] or "", reverse=True)
        started.sort(key=lambda v: v["status"] != "watching")
        return started

    def recent_history(self, category: str | None, limit: int = 15) -> list[dict[str, Any]]:
        rows = [
            h
            for h in reversed(self.data["history"])
            if category is None
            or (category == "youtube") == (h.get("category") == "youtube")
        ]
        return rows[:limit]

