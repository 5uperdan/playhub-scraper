# playhub-scraper

A tool for collecting and exploring Disney Lorcana Set Championship results from [Play Hub](https://tcg.ravensburgerplay.com). It scrapes match-level data (players, rounds, scores, standings) and stores it in a local database, which you can then explore through a web interface or query via a command-line tool.

**[Open the web interface →](https://5uperdan.github.io/playhub-scraper/)**

## Vibes

This project was fully vibe coded by Claude with not a single line of code written by myself. Even most of the readme was created by Claude. What a guy, eh?

---

## Using the web interface

The [web interface](https://5uperdan.github.io/playhub-scraper/) lets you explore competition results and player histories in your browser. It has two tabs:

- **Competitions** — browse and filter all competitions by name or venue, see player counts and winners
- **Players** — search by name, then click a player to expand their full competition and match history

All queries run entirely in your browser against your database — no external communication is required and once the page is loaded, queries should work even when you're offline.

To use it, you first need a `playhub.db` database file. There are two ways to get one:

### Option A — No Python required but untested (via GitHub Actions)

Forking creates your own copy of this repository under your GitHub account, including all the source data files and an automated workflow that builds the database for you. You can do this entirely from the GitHub website or mobile app — no terminal needed.

1. Click **Fork** at the top-right of this repository on GitHub (you'll need a free GitHub account)
2. In your fork, go to **Settings → Actions → General** and ensure Actions are enabled (forks sometimes have them disabled by default)
3. Go to the **Actions** tab in your fork
4. Select **Generate Database** in the left-hand list and click **Run workflow → Run workflow**
5. Wait for the run to complete (usually a few minutes) — a green tick means success
6. Click into the completed run, scroll to **Artifacts**, and download `playhub-db` — unzip it to get `playhub.db`
7. Visit the [web interface](https://5uperdan.github.io/playhub-scraper/), upload the file, and explore

### Option B — Generate the database locally (requires Python)

If you have Python set up on your machine, follow the CLI instructions below to generate `playhub.db`, then upload it to the [web interface](https://5uperdan.github.io/playhub-scraper/), use the CLI to query your data, or explore it on your own locally run web interface.

### Running the web interface locally

If you'd prefer not to use the hosted GitHub Pages site, you can serve it from your own machine:

```bash
python3 -m http.server 8000 --directory docs/
```

Then open [http://localhost:8000](http://localhost:8000) in your browser.

---

## CLI and advanced usage

The CLI is for adding new data sources (Google Sheets containing Play Hub event links), building and updating the database, and querying it from the terminal.

### Requirements

- [uv](https://docs.astral.sh/uv/) (Python package manager)

### `add-source <google-sheet-url>`

Downloads a Google Sheet (in XLSX format) and saves it to the `sources/` folder, registering it in the database.

```bash
uv run main.py add-source "https://docs.google.com/spreadsheets/d/YOUR_SHEET_ID"
```

The URL can be any Google Sheets share URL or direct export URL.

---

### `update-from-source <source-file-name> [--replace]`

Reads a saved source file from `sources/`, visits every Play Hub event link found in it, and populates the database with:

- Venue details (name, Play Hub store UUID)
- Competition details (name, date, player count)
- Players and their display names
- Every match in every round, with scores
- Final standings per player per competition

**Update mode (default):** Additive and idempotent. New competitions are inserted; existing ones are updated (scores, standings, player names) without removing any data. Safe to run on a new source file covering the same events — new competitions are added and existing data is refreshed.

```bash
uv run main.py update-from-source source_20260414T120000.xlsx
```

**Replace mode (`--replace`):** For each competition found in the source, all existing match and standings records are deleted and re-scraped from scratch. Player, venue, and round records are never deleted, so a player's full history is preserved through their UUID. Use this when you want a clean re-scrape of data you suspect is stale or partial.

```bash
uv run main.py update-from-source source_20260414T120000.xlsx --replace
```

**Updating player names:** Both modes always update the stored name to the latest value seen for each player's internal ID. Because matches reference players by internal UUID rather than name, all historical match data remains correct even after a name change.

---

### `player-info <player-name>`

Queries the database for a player (case-insensitive, partial match) and prints all their competition history, round-by-round match results, and final positions.

```bash
uv run main.py player-info "Mickey"
```

Example output:
```
Mickey
  The Disney Store: 2026-04-05
    Round 1: Buzz 1 - 2 Mickey
    Round 2: Jim 0 - 2 Mickey
    Round 3: Mickey 1 - 2 Minnie
    Top 4: Pete 1 - 2 Mickey
    Top 2: Mickey 0 - 2 Pluto
    Final position: 2
```

---

### `tournament-report --url <playhub-url>`

Fetches the list of registered players for a tournament directly from the Play Hub API, then looks each player up in the local database and prints their full competition history — one player at a time, separated by blank lines.

This works for upcoming tournaments as well as past ones: only the registration list is fetched from the API, not match data.

```bash
uv run main.py tournament-report --url "https://tcg.ravensburgerplay.com/events/12345"
```

Players who have no history in the local database are listed by name with a `(not found in database)` note.

---

### `list-competitions [--name <filter>]`

Lists all processed competitions sorted by date, showing the venue, competition name, winner, and player count. Optionally filter by competition or venue name (case-insensitive, partial match).

```bash
uv run main.py list-competitions
uv run main.py list-competitions --name "Element Games"
```

Example output:
```
2026-04-05  The Disney Store  —  Winterspell Championship
  Winner: Pluto  (34 players)
2026-04-05  UKGE  —  A Set Championship
  Winner: Woody  (15 players)
```

---

### Database schema

| Table | Key columns |
|---|---|
| `sources` | `uuid` (PK), `file_name`, `processed_on` |
| `venues` | `ph_uuid` (PK), `name`, `first_source_uuid` (FK) |
| `players` | `uuid` (PK), `ph_user_id`, `name`, `first_source_uuid` (FK) |
| `rounds` | `uuid` (PK), `name` (unique, e.g. "Round 1", "Top 8") |
| `competitions` | `uuid` (PK), `ph_event_id`, `name`, `venue_uuid` (FK), `start_date`, `attended_player_count` |
| `matches` | `uuid` (PK), `player_a_uuid` (FK), `player_b_uuid` (FK), `player_a_score`, `player_b_score`, `winning_player_uuid` (FK, NULL = draw), `competition_uuid` (FK), `round_uuid` (FK) |
| `competition_results` | `competition_uuid` + `player_uuid` (composite PK), `position` |

### A note on player and venue names

Player display names on Play Hub can be changed by the user at any time. The database uses the Play Hub internal user ID (`ph_user_id`) as the stable key for deduplication — so the same player is always one record regardless of name changes, and all their historical matches continue to reference the same UUID.

Similarly, store/venue names can change. Venues are keyed by their Play Hub store UUID, not their name, so a renamed venue remains a single record with all its historical competitions intact.

Every run of `update-from-source` (in either mode) updates the stored name to the latest value seen for each player and venue. This means current display names are always shown, even for older records. There is currently no mechanism to preserve a full name-change history (nor any interest in doing so).

