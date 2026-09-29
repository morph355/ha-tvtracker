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
5. *Configure* on the integration to set up rooms. Each room lists the `media_player` entities that belong to one TV. The defaults are:

   | Room | Entities |
   |---|---|
   | Family Room | `media_player.family_room_tv` |
   | Bedroom | `media_player.master_room_tv` |
   | Living Room | `media_player.shield`, `media_player.shield_2`, `media_player.android_tv_192_168_3_131` |

   A TV can show up as up to three entities, and each knows something different:

   | Integration | Gives | Doesn't give |
   |---|---|---|
   | **Android TV Remote** | the app in the foreground | any title |
   | **Google Cast** | title + channel for apps that cast (YouTube) | titles for Disney+, Netflix… playing natively; can go stale |
   | **Android Debug Bridge (ADB)** | the app **and the current title/position** from Android's media session (Disney+ works) | season/episode numbers |

   Put all of a TV's entities in one room and TV Tracker combines them. **ADB is what makes Disney+ (and similar apps) show a title**, and it is optional per TV:
   on the TV enable *Developer options → Network debugging*, then add *Settings → Devices & services → Android Debug Bridge* with the TV's IP and add the new `media_player` to the room.
6. Optional: paste `dashboard/tvtracker.yaml` into a new dashboard's raw configuration editor (room names in the "Now watching" card must match your rooms).

## Trakt (optional, recommended for Netflix, Disney+, Prime, Apple TV)

Netflix publishes nothing about what is playing, and Disney+ only the show name, so the TVs alone can't tell us the show or episode. **Trakt VIP's streaming sync** reads your viewing history from those services, and TV Tracker reads it from Trakt.

1. In the Trakt phone app link your streaming services (Trakt VIP > streaming sync).
2. Create a Trakt API app at <https://trakt.tv/oauth/applications/new>. Name it anything; set **Redirect URI** to `urn:ietf:wg:oauth:2.0:oob`; you don't need to tick any permissions. Copy the **Client ID** and **Client Secret**.
3. *Settings → Devices & services → TV Tracker → Configure*: paste them in.
4. Run the **`tvtracker.trakt_connect`** action (Developer tools → Actions). A notification shows a short code; enter it at <https://trakt.tv/activate>. Your Trakt password is never seen by Home Assistant.

What it does once connected (`sensor.tv_tracker_trakt` shows `connected`):
- Every hour (or with `tvtracker.trakt_sync`) it reads new watches, **sets the real season/episode** on your shows (replacing any episode TV Tracker only *guessed*, but never moving progress you set by hand backwards), and marks movies watched. Shows you watch that aren't in the library yet are added.
- A viewing the TV logged without a title ("Netflix, Living Room, 40 min") is **named** when Trakt reports a watch of a show on that service within about a day.
- The first sync only imports the last 60 days.

**Not instant.** Trakt's streaming sync runs about once a day, and some services give only a date, so Trakt updates arrive later than the viewing. The TVs still record the service, room and time straight away. BBC iPlayer, ITVX, Channel 4 and Now TV are not among Trakt's supported services.

Remove it any time with `tvtracker.trakt_disconnect`.

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
| `tvtracker.trakt_connect` / `trakt_sync` / `trakt_disconnect` | Link your Trakt account, sync now, or forget the login. |
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
- While a TV with an ADB entity is in use, TV Tracker reads its media session every 30 s (and whenever it plays/pauses) to get the title.
- Progress only moves forward automatically (a re-watch won't reset it). `set_progress` can move it anywhere.
- Status is one of *want to watch*, *upcoming* (not released), *watching*, *caught up* (up to date on a running show) or *finished*.

## Limitations

- Titles depend on what each app publishes. YouTube (Cast) gives video + channel; Disney+ gives the title through ADB; other apps (Netflix, iPlayer…) are untested and may publish nothing, in which case the session is logged without a title. Fill those in with `log_watch`.
- Apps publish a title but not season/episode numbers, so watching a show on the watchlist advances it by one episode per viewing. Back-to-back episodes (autoplay) are detected when the playback position jumps back to the start after most of an episode was played. Correct it any time with `set_progress`.
- Availability comes from TMDB/JustWatch for the configured country and can lag reality.
- If HA restarts mid-viewing, the open session is closed and logged at shutdown.

## Development

```
pip install -r requirements-dev.txt   # Python 3.13, Home Assistant test harness
pytest
```

`logic.py` and `library.py` have no Home Assistant imports and their tests run on any Python; `tests/test_integration.py` runs the whole integration against real Home Assistant core with TMDB faked.
