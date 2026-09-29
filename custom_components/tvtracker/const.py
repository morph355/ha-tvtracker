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
# A viewing at least this long counts as "watched" for the watchlist.
MIN_COUNT_SECONDS = 600

REFRESH_INTERVAL_HOURS = 12
# How often to look at a TV's media session (title, position) while it is on.
ADB_POLL_SECONDS = 30
HISTORY_LIMIT = 1000

SIGNAL_UPDATE = f"{DOMAIN}_update"
STORAGE_KEY = f"{DOMAIN}.library"
STORAGE_VERSION = 1
