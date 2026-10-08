# TV Tracker: notes for Claude

## "Add X to the watch list" / "add X to <list>"

When the user asks to add a show or film to "the watch list", "my watchlist", or one of
their other lists (e.g. "Del's List", "Dvd collection", "For Arden"), they mean a TV
Tracker list in their Home Assistant, not a file in this repo:

- Call the Home Assistant action `tvtracker.add_to_list` (through the HA MCP tools) with
  `list` and either `title` or `tmdb_id` + `media_type` (`tv` / `movie`). Use
  `return_response: true` and report what came back.
- "the watch list" / "my watchlist" → `list: "Watchlist"` (their Trakt watchlist).
  Other names → that list; the current names are the options of
  `select.tv_tracker_watchlist` (ignore "All lists").
- If a title is ambiguous, the action fails listing the matches: ask which one, or use
  `tvtracker.search` and retry with `tmdb_id` + `media_type`.
- Lists that came from Trakt get the item on Trakt too (the response's `trakt` field
  says "added on Trakt", "already on the Trakt list", or why not). Additions only:
  never remove anything from Trakt. Pass `trakt: false` only if asked to keep it off Trakt.
- Removing: `tvtracker.remove_from_list` (this never touches Trakt).

## Other standing rules from the user

- Trakt is only ever added to, never deleted from.
- The Trakt client secret is entered in HA's options, never put in the repo.
- The user merges PRs. After a merge, update via HACS (`update_information`, then
  `download` for `morph355/ha-tvtracker`); the user has asked for a restart after updating.
- The live dashboard (`tv-tracker`) is a separate copy from `dashboard/tvtracker.yaml`;
  change both.
