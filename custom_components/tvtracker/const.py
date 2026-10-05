"""Constants for TV Tracker."""

DOMAIN = "tvtracker"

CONF_TMDB_KEY = "tmdb_api_key"
CONF_REGION = "region"
CONF_ROOMS = "rooms"

DEFAULT_REGION = "GB"

# Rooms: each room groups the media_player entities that belong to one TV.
# The SHIELD has two entities (Google Cast gives titles, ADB gives the app).
DEFAULT_ROOMS = [
    {"name": "Family Room", "entities": ["media_player.family_room_tv"]},
    {"name": "Bedroom", "entities": ["media_player.master_room_tv"]},
    {
        "name": "Living Room",
        # Cast (titles), Android TV Remote (app), Android Debug Bridge (titles
        # for apps like Disney+ that only publish to the media session).
        "entities": [
            "media_player.shield",
            "media_player.shield_2",
            "media_player.android_tv_192_168_3_131",
        ],
    },
]

DEFAULT_SERVICES = [
    "Netflix",
    "Disney+",
    "Apple TV",
    "Prime Video",
    "BBC iPlayer",
    "ITVX",
    "Channel 4",
    "Now TV",
]

# A viewing shorter than this is channel surfing and is not logged.
MIN_SESSION_SECONDS = 120
# A viewing at least this long counts as "watched" for the watchlist, when we
# can't tell how much of the programme it was (see WATCHED_FRACTION).
MIN_COUNT_SECONDS = 600
# With the programme's length known, it counts once this much of it was watched.
WATCHED_FRACTION = 0.8

REFRESH_INTERVAL_HOURS = 12
# How often to look at a TV's media session (title, position) while it is on.
ADB_POLL_SECONDS = 30
HISTORY_LIMIT = 1000
# A show you've finished stays under Up Next > Finished for this long.
FINISHED_SHOWN_DAYS = 60
# The most episodes offered in the "watched up to" picker.
MAX_PICKER_EPISODES = 400
# Bump when the rules for matching a title to a TMDB show improve, so titles that
# couldn't be matched before are looked up again (2: country suffixes, "Ghosts US").
TITLE_LOOKUP_VERSION = 2

CONF_TRAKT_ID = "trakt_client_id"
CONF_TRAKT_SECRET = "trakt_client_secret"
# Trakt's streaming sync (Netflix etc.) can add watches days late, dated when you
# actually watched, so each sync re-reads this many days before the last one and
# skips what it has already applied.
TRAKT_SYNC_OVERLAP_DAYS = 30
# On first connect only import this much history (not years of it).
TRAKT_INITIAL_DAYS = 60
TRAKT_SYNC_HOURS = 1
# When Trakt finds one episode title in several shows, this many are offered
# (best first); more than MAX_SEARCH_HITS means the title is too generic to bother.
MAX_CANDIDATES = 4
MAX_SEARCH_HITS = 8
# Services whose viewings TV Tracker sends to Trakt. Trakt's own streaming sync
# already covers Netflix, Disney+, Prime Video and Apple TV, so sending those
# too would create duplicate watches. Nothing is ever deleted from Trakt.
TRAKT_PUSH_SERVICES = ("BBC iPlayer", "ITVX", "Channel 4", "Now TV")
# Where a hidden show is hidden on Trakt.
TRAKT_HIDE_SECTIONS = ("progress_watched", "calendar")

SIGNAL_UPDATE = f"{DOMAIN}_update"
STORAGE_KEY = f"{DOMAIN}.library"
STORAGE_VERSION = 1
