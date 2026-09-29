# TV Tracker for Home Assistant

Track what the household watches on the Android TVs, where, and what to watch next.

- **What's on where** – a live "now watching" per room (Family Room, Bedroom, Living Room).
- **Viewing history** – every session is logged (room, app, title, time). **YouTube is tracked as its own category**, separate from TV & movies. **Music is ignored.**
- **Multiple watchlists** – TV shows and movies. When a TV plays something on a list, it is marked watched / progress is advanced automatically.
- **Where to watch** – for every title, TMDB (JustWatch data) says which of *your* services carry it, whether it's on another service, rent/buy only, or **not available yet** (with the due date when known).
- **Continue watching** – shows you've started but not finished, with the next episode and where to stream it. Shows you're up to date on are shown as "caught up".
- **Your services** – Netflix, Disney+, Apple TV, Prime Video, BBC iPlayer, ITVX, Channel 4 and Now TV to start with. If a TV is used with a new streaming app, it is added to the list automatically.
- **Manual history** – anything the TVs missed can be logged by a service call, or by asking Claude (see below).

## Install

1. Copy `custom_components/tvtracker` into your HA `config/custom_components/` (or add this repo to HACS as a custom repository).
2. Restart Home Assistant.
3. Create a free account at [themoviedb.org](https://www.themoviedb.org), then *Settings → API* and copy the API key (the v3 key or the v4 read token both work).
4. *Settings → Devices & services → Add integration → TV Tracker*. Paste the key and your country code (default `GB`).
5. *Configure* on the integration to set up rooms. The defaults are:

   | Room | Entities |
   |---|---|
   | Family Room | `media_player.family_room_tv` |
   | Bedroom | `media_player.master_room_tv` |
   | Living Room | `media_player.shield`, `media_player.shield_2` |

   The SHIELD appears twice in HA: the Google Cast entity gives video titles, the Android TV (ADB) entity gives the app. Put both in one room and TV Tracker combines them.
6. Optional: paste `dashboard/tvtracker.yaml` into a new dashboard's raw configuration editor (room names in the "Now watching" card must match your rooms).

## Using it

Everything is a Home Assistant service (Developer tools → Actions), so it works from dashboards, automations, scripts and Claude via the HA MCP server.

| Service | What it does |
|---|---|
| `tvtracker.search` | Find a show/movie on TMDB (returns `tmdb_id`s). |
| `tvtracker.create_list` / `delete_list` | Manage watchlists. |
| `tvtracker.add_to_list` / `remove_from_list` | By `title` (or `tmdb_id` + `media_type`). The list is created if missing. |
| `tvtracker.log_watch` | Add a viewing the TVs missed. Updates progress / marks movies watched. |
| `tvtracker.set_progress` | "We're up to S2E4." |
| `tvtracker.mark_watched` | Movie watched, or whole show watched. |
| `tvtracker.add_service` / `remove_service` | Edit your streaming services. |
| `tvtracker.delete_history` | Remove a wrong history entry. |
| `tvtracker.refresh` | Re-check availability now (otherwise every 12 h). |
| `tvtracker.get_library` | Everything, as a response (handy for Claude). |

Sensors: `sensor.tv_tracker_now_watching_<room>`, `…_continue_watching`, `…_watchlists`, `…_history`, `…_services`.

### With Claude

With the Home Assistant MCP server connected, just say things like:

- "Add Severance and The Bear to a watchlist called Shows."
- "We watched Dune last Saturday night on Netflix in the Living Room."
- "We're up to Severance S1E6." (→ `set_progress`) or "We watched Severance S1E7 on Apple TV last night." (→ `log_watch`, one episode per entry)
- "What are we halfway through, and where can we stream it?"

If a title matches more than one thing (e.g. a TV show and a movie with the same name) the service says so and lists the candidates; Claude then calls again with the `tmdb_id`.

## How it decides what counts

- A session shorter than 2 minutes is ignored (channel surfing).
- A session of 10+ minutes on a title in your lists counts as watched: movies are marked watched; for shows the reported season/episode is used, or otherwise progress advances by one episode.
- Progress only moves forward automatically (a re-watch won't reset it). `set_progress` can move it anywhere.
- Status is one of *want to watch*, *upcoming* (not released), *watching*, *caught up* (up to date on a running show) or *finished*.

## Limitations

- Titles depend on what each app tells Android TV. YouTube (via Cast) usually gives the video and channel; other apps often give only the app name. Those sessions are logged without a title; fill them in with `log_watch`.
- Availability comes from TMDB/JustWatch for the configured country and can lag reality.
- If HA restarts mid-viewing, the open session is closed and logged at shutdown.

## Development

```
pip install -r requirements-dev.txt   # Python 3.13, Home Assistant test harness
pytest
```

`logic.py` and `library.py` have no Home Assistant imports and their tests run on any Python; `tests/test_integration.py` runs the whole integration against real Home Assistant core with TMDB faked.
