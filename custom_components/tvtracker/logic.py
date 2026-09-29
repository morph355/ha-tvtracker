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


_EPISODE_LABEL_RE = re.compile(
    r"\b(?:series|season|s)\s*(\d{1,2})\s*[:,.\-\u2013]?\s*(?:episode|ep|e)?\s*[:.]?\s*(\d{1,3})\b",
    re.I,
)


def parse_episode_label(text: str | None) -> tuple[int, int] | None:
    """Read (season, episode) from a subtitle such as 'Series 1: 18. Farnsby & B',
    'S13 EP21: Sneaky Sasquatch', 'S2:E4' or 'Season 2, Episode 4'."""
    m = _EPISODE_LABEL_RE.search(text or "")
    if not m:
        return None
    season, episode = int(m.group(1)), int(m.group(2))
    return (season, episode) if season and episode else None


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
    # Now TV's Cast name is simply "NOW" (too short for a substring rule).
    if norm(app_name) in ("now", "nowtv", "nowentertainment"):
        return "tv_movies", "Now TV"
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
    position_ms: int | None = None
    subtitle: str | None = None
    duration_ms: int | None = None
    playing: bool | None = None
    position_at: datetime | None = None  # when position_ms was true (for extrapolating)


def _ms(seconds: Any, allow_zero: bool = False) -> int | None:
    """Seconds (float) from a media_player attribute -> whole milliseconds."""
    try:
        value = float(seconds)
    except (TypeError, ValueError):
        return None
    if value < 0 or (value == 0 and not allow_zero):
        return None
    return int(value * 1000)


def _as_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None

# Android media sessions (from `dumpsys media_session`, via the ADB integration)
# The trailing timestamp makes every answer different. The ADB entity keeps the
# previous `adb_response` when a command prints nothing (e.g. the session has
# ended), so without it a stale title would look current.
MEDIA_SESSION_CMD = (
    "dumpsys media_session | grep -E 'package=|metadata:|state=PlaybackState'; "
    "echo tvt_ts=$(date +%s%N)"
)
_TS_RE = re.compile(r"tvt_ts=(\d+)")


def response_timestamp(text: str | None) -> str | None:
    """The freshness marker MEDIA_SESSION_CMD appends, or None if absent."""
    m = _TS_RE.search(text or "")
    return m.group(1) if m else None
SESSION_PAUSED, SESSION_PLAYING, SESSION_BUFFERING = 2, 3, 6
_SESSION_RE = re.compile(r"package=(?P<pkg>\S+)(?P<body>.*?)(?=\n\s*package=|\Z)", re.S)


def parse_media_sessions(text: str | None) -> list[dict[str, Any]]:
    """Parse the output of MEDIA_SESSION_CMD into one dict per app session.

    Each: {package, state (Android PlaybackState int or None), position_ms,
    title, subtitle}. Apps publish "title, subtitle, description" joined by
    ", " so a title that itself contains ", " is rebuilt from the leading parts.
    """
    out: list[dict[str, Any]] = []
    for m in _SESSION_RE.finditer(text or ""):
        body = m.group("body")
        st = re.search(r"PlaybackState \{state=(\d+), position=(-?\d+)", body)
        md = re.search(r"metadata: size=\d+, description=(.*)", body)
        title = subtitle = None
        if md:
            parts = md.group(1).strip().split(", ")
            title = ", ".join(parts[:-2]) if len(parts) >= 3 else parts[0]
            if len(parts) >= 3 and parts[-2] != "null":
                subtitle = parts[-2].strip() or None
            title = title.strip() or None
            if title == "null":
                title = None
        out.append(
            {
                "package": m.group("pkg"),
                "state": int(st.group(1)) if st else None,
                "position_ms": int(st.group(2)) if st else None,
                "title": title,
                "subtitle": subtitle,
            }
        )
    return out


def observe(
    states: list[dict[str, Any]], sessions: list[dict[str, Any]] | None = None
) -> Observation | None:
    """Combine a room's media_player states into one Observation (or None).

    Each state is {"state": str, "attributes": dict}. The app comes from any
    active entity; the title only from entities that are actually playing or
    paused *and* agree with the chosen app (Cast titles go stale when idle).
    `sessions` (see parse_media_sessions) fills in a title the entities lack,
    e.g. Disney+, which only publishes it to Android's media session.
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
        obs.duration_ms = _ms(attrs.get("media_duration"))
        obs.position_ms = _ms(attrs.get("media_position"), allow_zero=True)
        obs.playing = st.get("state") == "playing"
        obs.position_at = _as_datetime(attrs.get("media_position_updated_at"))
        break

    if obs.service == "Now TV" and obs.title and obs.season is None:
        # Now TV sends "Show Name 24": the trailing number is the episode.
        m = re.match(r"^(.*\S)\s+(\d{1,3})$", obs.title)
        if m:
            obs.series_title, obs.episode = m.group(1), int(m.group(2))

    if sessions:
        packages = {
            (st.get("attributes") or {}).get("app_id")
            for st in states
            if "." in ((st.get("attributes") or {}).get("app_id") or "")
            and classify((st.get("attributes") or {}).get("app_id"), None)[1] == chosen[1]
        }
        for sess in sessions:
            if (
                sess["package"] in packages
                and sess["state"] in (SESSION_PLAYING, SESSION_PAUSED, SESSION_BUFFERING)
                and sess["title"]
            ):
                if obs.title is None:
                    obs.title = sess["title"]
                # The media session's position is fresher than Cast's.
                obs.position_ms = sess["position_ms"]
                obs.position_at = None
                obs.playing = sess["state"] == SESSION_PLAYING
                # The subtitle often carries "Series 1: 18. Episode name" (iPlayer).
                # Only trust it for the same programme the entities named.
                if norm_title(sess["title"]) == norm_title(obs.title) and obs.season is None:
                    label = parse_episode_label(sess.get("subtitle"))
                    if label:
                        obs.season, obs.episode = label
                        obs.subtitle = sess.get("subtitle")
                break
    return obs


# Back-to-back episodes (autoplay) keep the same title. A new episode is
# assumed when the position jumps back to the start after most of one was played.
NEW_EPISODE_PLAYED_MS = 600_000
NEW_EPISODE_START_MS = 120_000


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
            "subtitle": obs.subtitle,
            "start": now,
            "end": None,
            "duration_ms": obs.duration_ms,
            "_last_pos": self._position_now(obs, now),
            "_pos_time": now,
            "_playing": bool(obs.playing),
        }

    @staticmethod
    def _position_now(obs: Observation, now: datetime) -> int | None:
        """The playback position at `now`. Cast only reports the position when it
        changes, so while playing it is wound forward from when it was true."""
        pos = obs.position_ms
        if pos is None:
            return None
        if obs.playing and obs.position_at is not None:
            try:
                pos += int(max(0.0, (now - obs.position_at).total_seconds()) * 1000)
            except TypeError:  # naive vs aware datetimes: leave it as reported
                pass
        return pos

    @staticmethod
    def _finalise(cur: dict[str, Any], now: datetime) -> None:
        """Work out how far through the programme you had got when it ended."""
        pos = cur.get("_last_pos")
        if pos is not None and cur.get("_playing") and cur.get("_pos_time") is not None:
            pos += int(max(0.0, (now - cur["_pos_time"]).total_seconds()) * 1000)
        dur = cur.get("duration_ms")
        if pos is not None and dur:
            pos = min(pos, dur)
        cur["final_pos_ms"] = pos

    def _next_episode_started(self, cur: dict[str, Any], obs: Observation, now: datetime) -> bool:
        last, pos = cur.get("_last_pos"), self._position_now(obs, now)
        return (
            cur["category"] == "tv_movies"
            and last is not None
            and pos is not None
            and last > NEW_EPISODE_PLAYED_MS
            and pos < NEW_EPISODE_START_MS
        )

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
            if same and obs is not None and self._next_episode_started(cur, obs, now):
                same = False
            if same and obs is not None:
                if obs.position_ms is not None:
                    cur["_last_pos"] = self._position_now(obs, now)
                    cur["_pos_time"] = now
                    cur["_playing"] = bool(obs.playing)
                if obs.duration_ms:
                    cur["duration_ms"] = obs.duration_ms
                if cur.get("season") is None and obs.season is not None:
                    cur.update(season=obs.season, episode=obs.episode, subtitle=obs.subtitle)
                if cur["title"] is None and obs.title:
                    cur.update(
                        title=obs.title,
                        series_title=obs.series_title,
                        season=obs.season,
                        episode=obs.episode,
                        channel=obs.channel,
                        subtitle=obs.subtitle,
                    )
                return closed
            self._finalise(cur, now)
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
        runtimes = raw.get("episode_run_time") or []
        details.update(
            episode_runtime=last.get("runtime") or (runtimes[0] if runtimes else None),
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
# Trakt
# --------------------------------------------------------------------------
def parse_trakt_history(items: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Turn Trakt /sync/history items into events, oldest first.

    Only entries carrying a TMDB id (which is what our library is keyed on)
    are kept. Episode events include season/episode; movies don't.
    """
    events: list[dict[str, Any]] = []
    for it in items or []:
        kind = it.get("type")
        if kind == "episode" and it.get("show") and it.get("episode"):
            tmdb = (it["show"].get("ids") or {}).get("tmdb")
            ep = it["episode"]
            if tmdb and ep.get("season") and ep.get("number"):
                events.append(
                    {
                        "trakt_id": it.get("id"),
                        "media_type": "tv",
                        "tmdb_id": int(tmdb),
                        "title": it["show"].get("title"),
                        "season": int(ep["season"]),
                        "episode": int(ep["number"]),
                        "episode_title": ep.get("title"),
                        "watched_at": it.get("watched_at"),
                    }
                )
        elif kind == "movie" and it.get("movie"):
            tmdb = (it["movie"].get("ids") or {}).get("tmdb")
            if tmdb:
                events.append(
                    {
                        "trakt_id": it.get("id"),
                        "media_type": "movie",
                        "tmdb_id": int(tmdb),
                        "title": it["movie"].get("title"),
                        "watched_at": it.get("watched_at"),
                    }
                )
    events.sort(key=lambda e: e["watched_at"] or "")
    return events


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


def infer_season(
    details: dict[str, Any], progress: dict[str, int] | None, episode: int
) -> int | None:
    """Work out which season a bare episode number belongs to.

    For apps that only give "Show Name 24". Uses where you are in the show: the
    season you're on, or the latest aired one for a show you haven't started. A
    lower number right at the end of a season means the next season began.
    Returns None when it can't be one of the show's seasons.
    """
    seasons = {s: c for s, c in _seasons(details).items() if c > 0}
    if not seasons or episode < 1:
        return None
    if progress:
        cur = progress["season"]
    elif details.get("last_aired"):
        cur = details["last_aired"]["season"]
    else:
        cur = min(seasons)
    if cur not in seasons:
        return None
    if progress and episode < progress["episode"]:
        near_end = progress["episode"] >= seasons[cur] - 1
        if near_end and cur + 1 in seasons and episode <= seasons[cur + 1]:
            return cur + 1
        return cur if episode <= seasons[cur] else None   # an older episode again
    return cur if episode <= seasons[cur] else None


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


_COUNTRY_SUFFIX = re.compile(r"[\s(]+(?:us|uk|au|ca)\)?\s*$", re.I)


_TRAILING_NUMBER = re.compile(r"\s+\d{1,3}\s*$")


def match_score(
    item_title: str, *candidates: str | None, allow_trailing_number: bool = False
) -> bool:
    """Does a reported playback title refer to this watchlist title?

    allow_trailing_number: TV apps like Now TV send "Show Name 24" (an episode
    number on the end). Off for movies, where "Toy Story 4" is a different title.
    """
    target = norm_title(item_title)
    if not target:
        return False
    for cand in candidates:
        if not cand:
            continue
        if allow_trailing_number:
            trimmed = _TRAILING_NUMBER.sub("", cand)
            if trimmed != cand and match_score(item_title, trimmed):
                return True
        if norm_title(cand) == target:
            return True
        # "Ghosts US" / "Ghosts (UK)" vs "Ghosts" (and the other way round)
        stripped = _COUNTRY_SUFFIX.sub("", cand)
        if stripped != cand and norm_title(stripped) == target:
            return True
        if norm_title(_COUNTRY_SUFFIX.sub("", item_title)) == norm_title(cand):
            return True
        # "Severance - S2E4", "Severance: The Chair"
        low = cand.lower().lstrip()
        if re.match(rf"^(the\s+)?{re.escape(item_title.lower().removeprefix('the '))}\s*[-:|(]", low):
            return True
    return False
