# playhub-scraper

Scrapes Disney Lorcana Set Championship tournament data from Play Hub and stores it in a queryable SQLite database.

## Vibes

This project was fully vibe coded by Claude with not a single line of code written by myself. Even most of the readme was created by Claude. What a guy, eh?

## What it does

- Downloads Google Sheets containing Play Hub event links
- Fetches match-level data (players, rounds, scores, results) directly from the Play Hub REST API — no headless browser needed
- Stores everything in a local SQLite database (`playhub.db`) via SQLAlchemy
- Exposes a CLI for adding sources, processing data, and querying player history
- Includes a browser-based web UI for exploring competitions and player histories without the CLI

## Web Interface

The [GitHub Pages site](https://5uperdan.github.io/playhub-scraper/) provides a browser-based UI for exploring your data:

- **Competitions tab** — browse and filter all competitions by name or venue, see player counts and winners
- **Players tab** — search by name, then click a player to expand their full competition and match history

Since the database is generated from your own sources, you need to produce a `playhub.db` file first, then upload it to the site. There are two ways to do this:

**Option A — No Python required (GitHub Actions):**

Forking creates your own copy of this repository under your GitHub account, including all the source files and the automated workflow. You can do this entirely from the GitHub website or mobile app — no terminal needed.

1. Click **Fork** at the top-right of this repository on GitHub (you'll need a free GitHub account)
2. In your fork, go to **Settings → Actions → General** and ensure Actions are enabled (forks sometimes have them disabled by default)
3. Go to the **Actions** tab in your fork
4. Select **Generate Database** in the left-hand list and click **Run workflow → Run workflow**
5. Wait for the run to complete (usually a few minutes) — a green tick means success
6. Click into the completed run, scroll to **Artifacts**, and download `playhub-db` — unzip it to get `playhub.db`
7. Visit the [site](https://5uperdan.github.io/playhub-scraper/), upload the file, and explore

**Option B — Local CLI (requires Python / uv):**

1. Run the CLI commands below to generate `playhub.db`
2. Visit the [site](https://5uperdan.github.io/playhub-scraper/) and upload the file

All queries run entirely in your browser using SQLite compiled to WebAssembly — nothing is uploaded or sent anywhere.

### Running the site locally

You can serve the site from your machine without deploying to GitHub Pages:

```bash
python3 -m http.server 8000 --directory docs/
```

Then open [http://localhost:8000](http://localhost:8000) in your browser.

## Requirements

- [uv](https://docs.astral.sh/uv/) (Python package manager)

## CLI Commands

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

**Replace mode (`--replace`):** For each competition found in the source, all existing match and standings records are deleted and re-scraped from scratch. Player, venue, and round records are never deleted, so a player's full history is preserved through their UUID.  Use this when you want a clean re-scrape of data you suspect is stale or partial.

```bash
uv run main.py update-from-source source_20260414T120000.xlsx --replace
```

**Updating player names:** Both modes always update the stored name to the latest value seen for each player's internal ID. Because matches reference players by internal UUID rather than name, all historical match data remains correct even after a name change.

---

### `player-info <player-name>`

Queries the database for a player (case-insensitive, partial match) and prints all their competition history, round-by-round match results, and final positions.

```bash
uv run main.py player-info "Danny"
```

Example output:
```
MK_DannyB
  Black Dragon Games Ltd: 2026-04-05
    Round 1: ToInfinity_AndBeyond 1 - 2 MK_DannyB
    Round 2: JoePope27 0 - 2 MK_DannyB
    Round 3: MK_DannyB 1 - 2 MK_ifoughtthelore
    Top 4: Bradley 1 - 2 MK_DannyB
    Top 2: MK_DannyB 0 - 2 OL_Okan
    Final position: 2
```

---

### `list-competitions [--name <filter>]`

Lists all processed competitions sorted by date, showing the venue, competition name, winner, and player count. Optionally filter by competition or venue name (case-insensitive, partial match).

```bash
uv run main.py list-competitions
uv run main.py list-competitions --name "Element Games"
```

Example output:
```
2026-04-05  Black Dragon Games Ltd  —  Winterspell Championship
  Winner: OL_Okan  (34 players)
2026-04-05  Element Games  —  Winterspell Set Championship - Element Games
  Winner: Kravex  (15 players)
```

---

| Table | Key columns |
|---|---|
| `sources` | `uuid` (PK), `file_name`, `processed_on` |
| `venues` | `ph_uuid` (PK), `name`, `first_source_uuid` (FK) |
| `players` | `uuid` (PK), `ph_user_id`, `name`, `first_source_uuid` (FK) |
| `rounds` | `uuid` (PK), `name` (unique, e.g. "Round 1", "Top 8") |
| `competitions` | `uuid` (PK), `ph_event_id`, `name`, `venue_uuid` (FK), `start_date`, `attended_player_count` |
| `matches` | `uuid` (PK), `player_a_uuid` (FK), `player_b_uuid` (FK), `player_a_score`, `player_b_score`, `winning_player_uuid` (FK, NULL = draw), `competition_uuid` (FK), `round_uuid` (FK) |
| `competition_results` | `competition_uuid` + `player_uuid` (composite PK), `position` |

## A note on player names

Player display names on Play Hub can be changed by the user at any time. The database uses the Play Hub internal user ID (`ph_user_id`) as the stable key for deduplication — so the same player is always one record regardless of name changes, and all their historical matches continue to reference the same UUID.

Every run of `update-from-source` (in either mode) updates the stored name to the latest value seen for each player. This means a player's current display name is always shown, even for their older matches. There is currently no mechanism to preserve a full name-change history (nor any interest in doing so).

