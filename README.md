# TV Tracker for Home Assistant

Track what the household watches on the Android TVs, where, and what to watch next.

- **What's on where** – a live "now watching" per room (Family Room, Bedroom, Living Room).
- **Viewing history** – every session is logged (room, app, title, time). **YouTube is tracked as its own category**, separate from TV & movies. **Music is ignored.**
- **Multiple watchlists** – TV shows and movies. When a TV plays something on a list, it is marked watched / progress is advanced automatically.
- **Where to watch** – for every title, TMDB (JustWatch data) says which of *your* services carry it, whether it's on another service, rent/buy only, or **not available yet** (with the due date when known).
- **Up next** – shows you've started, in three groups: *available to watch* (the next episode is out, with the service you watch it on), *coming soon* (you're caught up; the next episode's date) and *finished* (ended, or nothing new announced).
- **Catch up** – shows you've watched where only the title was seen, so the episode isn't known (BBC iPlayer, for example), then shows with a new episode out (in case you watched it somewhere we couldn't see, like a live channel). Choose one and see its unwatched episodes by name; choose the last one you've watched and everything up to it is marked watched, here and on Trakt (only what Trakt hasn't got).
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

If it says `connected` but imports nothing, `sensor.tv_tracker_trakt` has a `last_result` attribute (and `tvtracker.trakt_sync` returns the same numbers): `fetched` is how many watches Trakt sent, `without_tmdb_id` how many we couldn't use, `already_applied` how many an earlier sync handled. `fetched: 0` means Trakt has no history in that window yet.

### Adding to Trakt (never deleting)

TV Tracker can also **add** to your Trakt history. It never deletes or edits anything on Trakt.

- **Viewings** on services Trakt does *not* sync itself (**BBC iPlayer, ITVX, Channel 4, Now TV**) are added automatically once you've watched 80% of it, but only when the exact show and episode are known (iPlayer with ADB reports them). Netflix, Disney+, Prime Video and Apple TV are left to Trakt's own sync, so nothing is duplicated, and an episode we only *counted on* is never sent. Anything that can't be sent (Trakt down) waits in a queue (`waiting_to_send` on the Trakt sensor) and is retried every hour.
- **"I'm up to date on Last Week Tonight"**: `tvtracker.mark_watched` sets HA to the latest aired episode (the show shows as *caught up*, and goes back to *watching* when the next episode airs) and adds the episodes Trakt is missing, dated to when each aired. Trakt is asked, for that show, which episodes it already has (in *Trakt's* season/episode numbering, which can differ from TMDB's), so no episode is watched twice and none is filed under the wrong number. Add `dry_run: true` first to see exactly what would be added, changing nothing. `trakt: false` keeps it to HA.
- **Hiding**: `tvtracker.hide` removes a show from the dashboard lists and hides it in Trakt's progress and calendar (`hidden: false` reverses it). Its history is kept in both places.

Remove the connection any time with `tvtracker.trakt_disconnect`.

## Using it

Everything is a Home Assistant service (Developer tools → Actions), so it works from dashboards, automations, scripts and Claude via the HA MCP server.

| Service | What it does |
|---|---|
| `tvtracker.search` | Find a show/movie on TMDB (returns `tmdb_id`s). |
| `tvtracker.create_list` / `delete_list` | Manage watchlists. |
| `tvtracker.add_to_list` / `remove_from_list` | By `title` (or `tmdb_id` + `media_type`). The list is created if missing. |
| `tvtracker.find_episode` | Which season/episode of a show has a given name (e.g. Now TV only sends the episode's title); leave the show out to search all of Trakt. |
| `tvtracker.confirm_match` | Make a *(probably)* viewing certain, and send it to Trakt (iPlayer/Now TV/ITVX/Channel 4). `id` defaults to `latest`. |
| `tvtracker.pick_match` | An episode title that fits **several shows** is offered as numbered options (best first, with hints such as *you track it · aired 27 Sep · 45 min · on Now TV*). `choice` picks one (`id` defaults to `latest`); only then is anything added, progress set (if you watched enough) and the episode sent to Trakt. |
| `tvtracker.dismiss_match` | "None of these / not right": stop asking; the viewing stays in the history untitled and a show that was only added for a wrong guess is removed. |
| `tvtracker.skip_match` | "Ask me later": move a waiting viewing behind the others. |
| `tvtracker.assign_match` | The guess was the wrong show: name the right one (or type it into `text.tv_tracker_show_search`). The episode is found by the same title within it, the wrong guess is undone, and the viewing is confirmed. |
| `tvtracker.log_watch` | Add a viewing the TVs missed. Updates progress / marks movies watched. On iPlayer/Now TV/ITVX/Channel 4 (which Trakt doesn't sync) a viewing you log is also sent to Trakt. |
| `tvtracker.set_progress` | "We're up to S2E4." |
| `tvtracker.mark_watched` | Movie watched, or show up to date (also adds the missing episodes to Trakt; try `dry_run: true` first). |
| `tvtracker.hide` | Hide a show/film from the lists here and (shows) on Trakt. History is kept. |
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

- A session shorter than 2 minutes is ignored (channel surfing). Stopping part-way still logs the viewing; it just doesn't move your progress until you've finished it.
- A viewing counts as **watched once you've seen 80% of it**. "How much" is worked out from the playback position and the length the TV reports (iPlayer, Now TV and Apple TV report a length); that also copes with resuming part-way through, so finishing an episode you started yesterday counts. If the TV doesn't report a length, TMDB's episode runtime is used; if neither is known, 10 minutes is the fallback. Movies use the same rule; for shows on your lists, the reported season/episode is used, or progress advances by one episode. Each history entry records `watched_pct`, so you can see why something didn't count.
- While a TV with an ADB entity is in use, TV Tracker reads its media session every 30 s (and whenever it plays/pauses) to get the title.
- Progress only moves forward automatically (a re-watch won't reset it). `set_progress` can move it anywhere.
- Status is one of *want to watch*, *upcoming* (not released), *watching*, *caught up* (up to date on a running show) or *finished*.

## Limitations

- Titles depend on what each app publishes. YouTube (Cast) gives video + channel; Disney+ gives the title through ADB; other apps (Netflix, iPlayer…) are untested and may publish nothing, in which case the session is logged without a title. Fill those in with `log_watch`.
- **Now TV** puts the episode number on the end of the title ("Show Name 24") but no season; the season is inferred from where you are in the show (marked `inferred`, and Trakt corrects it). **BBC iPlayer** publishes "Series 1: 18. Episode name" through ADB, which gives both. Without ADB on a TV, iPlayer gives only the show name via Cast.
- When a title matches no show, and Trakt is set up, Trakt is searched for an episode with **exactly** that title (TMDB can't search episode names). A single, distinctive (8+ characters) match is used: the show is added to your library, the viewing is labelled *(probably)*, and it is **not sent to Trakt** until you confirm it. On the dashboard's *Watching* tab an **"Is this right?"** card appears whenever something is waiting. For a single guess: a **Yes, that's right** button; if the title fits **several shows** (more than one, up to 8; more than that is treated as too generic to guess), the options are numbered, best first, with **Choose 1–4** buttons and nothing is added until you choose. Either way there is **None of these / not right**, **Ask me about this later** (when more than one is waiting), and a box to **type the right show** (`sensor.tv_tracker_needs_confirming` drives it). The buttons act on the newest waiting viewing; the same actions are `tvtracker.confirm_match` and `tvtracker.assign_match`. `tvtracker.find_episode` with no show does the same search on demand: the title is tried as sent, with a straight apostrophe and without punctuation, and if nothing matches the error says what was tried and what Trakt returned.
- When a title matches no show, it is looked up among the *episode names* of the shows you track (lists or in progress), so Now TV's "The Jordan Boys' Legacy" is recognised as an episode of Lanterns. `log_watch` also accepts `episode_title` instead of season/episode.
- Apps publish a title but not season/episode numbers, so watching a show on the watchlist advances it by one episode per viewing. Back-to-back episodes (autoplay) are detected when the playback position jumps back to the start after most of an episode was played. Correct it any time with `set_progress`.
- Availability comes from TMDB/JustWatch for the configured country and can lag reality.
- If HA restarts mid-viewing, the open session is closed and logged at shutdown.

## Development

```
pip install -r requirements-dev.txt   # Python 3.13, Home Assistant test harness
pytest
```

`logic.py` and `library.py` have no Home Assistant imports and their tests run on any Python; `tests/test_integration.py` runs the whole integration against real Home Assistant core with TMDB faked.

### Watchlists and Trakt

- Your Trakt watchlist is copied into a list called **Watchlist**, and each of your own Trakt lists into a list of the same name. Only additions: each Trakt entry is copied once, so something you take off a list here isn't put back, and nothing is changed on Trakt.
- Watchlists show only what you haven't started yet. Once you start a show it moves to Up next; a film you've watched is done. Trakt's full watched record is checked on every sync, so things you watched long ago count too.
- The **Genre** dropdown on the Watchlists tab shows one genre at a time.
