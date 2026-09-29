"""Pure logic for TV Tracker (no Home Assistant imports, fully unit-testable)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

POSTER_BASE = "https://image.tmdb.org/t/p/w185"


# --------------------------------------------------------------------------
# Names
# --------------------------------------------------------------------------
def norm(text: str | None) -> str:
    """Normalise text for comparison: 'Disney+' -> 'disneyplus'."""
    t = (text or "").lower().replace("&", "and").replace("+", "plus")
    return re.sub(r"[^a-z0-9]+", "", t)


def norm_title(text: str | None) -> str:
    """Normalise a show/movie title, ignoring a leading 'the'."""
    t = re.sub(r"^\s*the\s+", "", (text or "").lower())
    return norm(t)


SERVICE_ALIASES = {
    "netflix": "Netflix",
    "netflixbasicwithads": "Netflix",
    "disneyplus": "Disney+",
    "disney": "Disney+",
    "appletv": "Apple TV",
    "appletvplus": "Apple TV",
    "amazonprime": "Prime Video",
    "amazonprimevideo": "Prime Video",
    "primevideo": "Prime Video",
    "bbciplayer": "BBC iPlayer",
    "iplayer": "BBC iPlayer",
    "itvx": "ITVX",
    "itvplayer": "ITVX",
    "itv": "ITVX",
    "channel4": "Channel 4",
    "all4": "Channel 4",
    "nowtv": "Now TV",
    "now": "Now TV",
    "nowcinema": "Now TV",
    "nowtvcinema": "Now TV",
    "nowentertainment": "Now TV",
    "skygo": "Sky Go",
}


def canonical_service(name: str) -> str:
    """Map any spelling of a streaming service to our canonical name."""
    return SERVICE_ALIASES.get(norm(name), (name or "").strip())


# --------------------------------------------------------------------------
# Apps -> category / service
# --------------------------------------------------------------------------
# Order matters: first match wins. (keyword found in normalised app id + name,
# category, service). category None => ignore (music).
APP_RULES: list[tuple[str, str | None, str | None]] = [
    ("youtubemusic", None, None),
    ("spotify", None, None),
    ("amazonmusic", None, None),
    ("applemusic", None, None),
    ("tidal", None, None),
    ("deezer", None, None),
    ("pandora", None, None),
    ("bbcsounds", None, None),
    ("youtube", "youtube", "YouTube"),
    ("netflix", "tv_movies", "Netflix"),
    ("disney", "tv_movies", "Disney+"),
    ("appletv", "tv_movies", "Apple TV"),
    ("primevideo", "tv_movies", "Prime Video"),
    ("amazonvideo", "tv_movies", "Prime Video"),
    ("avod", "tv_movies", "Prime Video"),
    ("iplayer", "tv_movies", "BBC iPlayer"),
    ("bbc", "tv_movies", "BBC iPlayer"),
    ("itv", "tv_movies", "ITVX"),
    ("channel4", "tv_movies", "Channel 4"),
    ("all4", "tv_movies", "Channel 4"),
    ("nowtv", "tv_movies", "Now TV"),
    ("skygo", "tv_movies", "Sky Go"),
]

# Apps that are not content (home screen, settings...).
IGNORED_APP_WORDS = (
    "launcher",
    "settings",
    "screensaver",
    "backdrop",
    "dreams",
    "setupwraith",
    "androidnativeapp",
    "inputselect",
    "hdmi",
)


def classify(app_id: str | None, app_name: str | None) -> tuple[str | None, str | None]:
    """Return (category, service) for an app, or (None, None) to ignore it.

    category is 'youtube' or 'tv_movies'.
    """
    blob = norm(f"{app_id or ''} {app_name or ''}")
    if not blob:
        return None, None
    for keyword, category, service in APP_RULES:
        if keyword in blob:
            return category, service
    if any(word in blob for word in IGNORED_APP_WORDS):
        return None, None
    # Unknown streaming app: use its name, or tidy up the package name.
    name = (app_name or "").strip()
    if not name or "." in name:
        source = name or (app_id or "")
        name = source.split(".")[-1].replace("_", " ").title()
    return "tv_movies", canonical_service(name)


# --------------------------------------------------------------------------
# Observing a room
# --------------------------------------------------------------------------
ACTIVE_STATES = {"playing", "paused", "on", "buffering"}
PLAYBACK_STATES = {"playing", "paused", "buffering"}


@dataclass
class Observation:
    category: str
    service: str
    title: str | None = None
    series_title: str | None = None
    season: int | None = None
    episode: int | None = None
    channel: str | None = None


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def observe(states: list[dict[str, Any]]) -> Observation | None:
    """Combine a room's media_player states into one Observation (or None).

    Each state is {"state": str, "attributes": dict}. The app comes from any
    active entity; the title only from entities that are actually playing or
    paused *and* agree with the chosen app (Cast titles go stale when idle).
    """
    # The foreground app is authoritative from an entity that is simply "on"
    # (Android TV Remote / ADB report only the foreground app). A paused or
    # playing Cast entity can be a stale session, so it is only the fallback
    # for TVs that have a single entity.
    chosen: tuple[str, str] | None = None
    ordered = [s for s in states if s.get("state") == "on"] + [
        s for s in states if s.get("state") != "on"
    ]
    for st in ordered:
        if st.get("state") not in ACTIVE_STATES:
            continue
        attrs = st.get("attributes") or {}
        if not (attrs.get("app_id") or attrs.get("app_name")):
            continue
        category, service = classify(attrs.get("app_id"), attrs.get("app_name"))
        if category and service:
            chosen = (category, service)
        # An "on" entity that names a non-content app (launcher, music) means
        # nothing is being watched; don't fall through to a stale entity.
        break
    if chosen is None:
        return None

    obs = Observation(category=chosen[0], service=chosen[1])
    for st in states:
        if st.get("state") not in PLAYBACK_STATES:
            continue
        attrs = st.get("attributes") or {}
        if not attrs.get("media_title"):
            continue
        # An entity that names an app must be the *chosen* app. This also
        # rejects stale titles from ignored apps (e.g. a paused Spotify
        # podcast left on a Cast entity while Disney+ is in the foreground).
        if attrs.get("app_id") or attrs.get("app_name"):
            _, service = classify(attrs.get("app_id"), attrs.get("app_name"))
            if service != chosen[1]:
                continue
        obs.title = attrs.get("media_title")
        obs.series_title = attrs.get("media_series_title")
        obs.season = _as_int(attrs.get("media_season"))
        obs.episode = _as_int(attrs.get("media_episode"))
        if chosen[0] == "youtube":
            obs.channel = attrs.get("media_artist")
        break
    return obs


class RoomTracker:
    """Turns a stream of observations for one room into closed sessions."""

    def __init__(self, room: str) -> None:
        self.room = room
        self.current: dict[str, Any] | None = None

    def _new(self, obs: Observation, now: datetime) -> dict[str, Any]:
        return {
            "room": self.room,
            "category": obs.category,
            "service": obs.service,
            "title": obs.title,
            "series_title": obs.series_title,
            "season": obs.season,
            "episode": obs.episode,
            "channel": obs.channel,
            "start": now,
            "end": None,
        }

    def update(self, obs: Observation | None, now: datetime) -> list[dict[str, Any]]:
        """Feed the latest observation; returns sessions that just closed."""
        closed: list[dict[str, Any]] = []
        cur = self.current
        if cur is not None:
            same = (
                obs is not None
                and obs.category == cur["category"]
                and obs.service == cur["service"]
                and (obs.title == cur["title"] or obs.title is None or cur["title"] is None)
            )
            if same and obs is not None:
                if cur["title"] is None and obs.title:
                    cur.update(
                        title=obs.title,
                        series_title=obs.series_title,
                        season=obs.season,
                        episode=obs.episode,
                        channel=obs.channel,
                    )
                return closed
            cur["end"] = now
            closed.append(cur)
            self.current = None
        if obs is not None:
            self.current = self._new(obs, now)
        return closed

    def close(self, now: datetime) -> dict[str, Any] | None:
        """Force-close the open session (e.g. on shutdown)."""
        closed = self.update(None, now)
        return closed[0] if closed else None


# --------------------------------------------------------------------------
# TMDB parsing
# --------------------------------------------------------------------------
PROVIDER_KINDS = ("flatrate", "ads", "free", "rent", "buy")


def _year(text: str | None) -> str | None:
    return text[:4] if text else None


def parse_providers(raw: dict[str, Any], region: str) -> dict[str, list[str]]:
    """Extract one region's providers from a TMDB details response."""
    results = (raw.get("watch/providers") or {}).get("results") or {}
    region_data = results.get(region) or {}
    out: dict[str, list[str]] = {}
    for kind in PROVIDER_KINDS:
        names: list[str] = []
        for p in region_data.get(kind) or []:
            name = canonical_service(p.get("provider_name", ""))
            if name and name not in names:
                names.append(name)
        out[kind] = names
    return out


def parse_details(media_type: str, raw: dict[str, Any]) -> dict[str, Any]:
    """Boil a TMDB details response down to what we store."""
    poster = raw.get("poster_path")
    details: dict[str, Any] = {
        "title": raw.get("name") or raw.get("title") or "Unknown",
        "year": _year(raw.get("first_air_date") or raw.get("release_date")),
        "overview": raw.get("overview") or "",
        "poster": f"{POSTER_BASE}{poster}" if poster else None,
        "status": raw.get("status"),
        "first_air_date": raw.get("first_air_date") or None,
        "release_date": raw.get("release_date") or None,
        "runtime": raw.get("runtime"),
    }
    if media_type == "tv":
        seasons = {
            int(s["season_number"]): int(s.get("episode_count") or 0)
            for s in raw.get("seasons") or []
            if (s.get("season_number") or 0) >= 1
        }
        last = raw.get("last_episode_to_air") or {}
        nxt = raw.get("next_episode_to_air") or {}
        details.update(
            seasons=seasons,
            last_aired=(
                {"season": last["season_number"], "episode": last["episode_number"]}
                if last.get("season_number") and last.get("episode_number")
                else None
            ),
            next_air_date=nxt.get("air_date") or None,
        )
    return details


# --------------------------------------------------------------------------
# Episodes and status
# --------------------------------------------------------------------------
def _seasons(details: dict[str, Any]) -> dict[int, int]:
    return {int(k): int(v) for k, v in (details.get("seasons") or {}).items()}


def episode_index(seasons: dict[int, int], season: int, episode: int) -> int:
    """1-based position of S<season>E<episode> within the whole show."""
    return sum(c for s, c in seasons.items() if s < season) + episode


def next_episode(details: dict[str, Any], progress: dict[str, int] | None) -> tuple[int, int] | None:
    """The episode after `progress` (or the first episode), if one exists."""
    seasons = {s: c for s, c in _seasons(details).items() if c > 0}
    if not seasons:
        return None if progress else (1, 1)
    if not progress:
        first = min(seasons)
        return first, 1
    s, e = progress["season"], progress["episode"]
    if e < seasons.get(s, 0):
        return s, e + 1
    later = [n for n in seasons if n > s]
    return (min(later), 1) if later else None


def has_aired(details: dict[str, Any], season: int, episode: int) -> bool:
    """Whether S<season>E<episode> has aired, as far as TMDB tells us."""
    last = details.get("last_aired")
    if not last:
        return False
    seasons = _seasons(details)
    return episode_index(seasons, season, episode) <= episode_index(
        seasons, last["season"], last["episode"]
    )


def _parse_date(text: str | None) -> date | None:
    try:
        return date.fromisoformat(text) if text else None
    except ValueError:
        return None


def derive_status(item: dict[str, Any], today: date) -> str:
    """One of: want_to_watch, upcoming, watching, caught_up, finished."""
    details = item.get("details") or {}
    if item["media_type"] == "movie":
        if item.get("watched"):
            return "finished"
        release = _parse_date(details.get("release_date"))
        return "upcoming" if (release is None or release > today) else "want_to_watch"

    progress = item.get("progress")
    if item.get("watched"):
        return "finished"
    if not progress:
        first_air = _parse_date(details.get("first_air_date"))
        return "upcoming" if (first_air is None or first_air > today) else "want_to_watch"
    last = details.get("last_aired")
    if last is None:
        return "watching"
    seasons = _seasons(details)
    done = episode_index(seasons, progress["season"], progress["episode"])
    latest = episode_index(seasons, last["season"], last["episode"])
    if done < latest:
        return "watching"
    return "finished" if details.get("status") in ("Ended", "Canceled") else "caught_up"


def _pretty_date(text: str | None) -> str | None:
    d = _parse_date(text)
    return f"{d.day} {d:%b %Y}" if d else None


def availability(item: dict[str, Any], my_services: list[str], today: date) -> dict[str, Any]:
    """Where can this be watched? Returns text plus the raw groups."""
    providers = item.get("providers") or {}
    details = item.get("details") or {}
    mine = {canonical_service(s) for s in my_services}

    streaming = [
        n for kind in ("flatrate", "ads", "free") for n in providers.get(kind, [])
    ]
    streaming = list(dict.fromkeys(streaming))
    on_mine = [n for n in streaming if n in mine]
    elsewhere = [n for n in streaming if n not in mine]
    rent_buy = list(
        dict.fromkeys(providers.get("rent", []) + providers.get("buy", []))
    )

    if on_mine:
        text = "Watch on " + ", ".join(on_mine)
    elif elsewhere:
        text = "Not on your services (on " + ", ".join(elsewhere) + ")"
    elif rent_buy:
        text = "Rent or buy only (" + ", ".join(rent_buy) + ")"
    else:
        upcoming = derive_status({**item, "watched": False, "progress": None}, today) == "upcoming"
        next_air = _parse_date(details.get("next_air_date"))
        if upcoming or (next_air and next_air > today and not details.get("last_aired")):
            when = _pretty_date(
                details.get("next_air_date")
                or details.get("first_air_date")
                or details.get("release_date")
            )
            text = "Not available yet" + (f" (due {when})" if when else "")
        else:
            text = "Not currently available to stream"
    return {
        "text": text,
        "on_my_services": on_mine,
        "elsewhere": elsewhere,
        "rent_buy": rent_buy,
    }


def match_score(item_title: str, *candidates: str | None) -> bool:
    """Does a reported playback title refer to this watchlist title?"""
    target = norm_title(item_title)
    if not target:
        return False
    for cand in candidates:
        if not cand:
            continue
        if norm_title(cand) == target:
            return True
        # "Severance - S2E4", "Severance: The Chair"
        low = cand.lower().lstrip()
        if re.match(rf"^(the\s+)?{re.escape(item_title.lower().removeprefix('the '))}\s*[-:|(]", low):
            return True
    return False
