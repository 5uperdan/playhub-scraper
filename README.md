# playhub-scraper

A tool for collecting and exploring Disney Lorcana Set Championship results from [Play Hub](https://tcg.ravensburgerplay.com). It fetches match-level data (players, rounds, scores, standings) directly from the Play Hub API and stores it in a local database, which you can then explore through a web interface or query via a command-line tool.

**[Open the web interface →](https://5uperdan.github.io/playhub-scraper/)**

## Vibes

This project was fully vibe coded by Claude with not a single line of code written by myself. Even most of the readme was created by Claude. What a guy, eh?

---

## Using the web interface

The [web interface](https://5uperdan.github.io/playhub-scraper/) lets you explore competition results and player histories in your browser. It has four tabs:

- **Competitions** — browse and filter all competitions by name or venue, see player counts and winners
- **Players** — search by name, then click a player to expand their full competition and match history
- **Leaderboard** — browse the Elo rating leaderboard for all players, filterable by name
- **Prediction Accuracy** — calibration chart and experience breakdown evaluating how well the Elo win predictions match real outcomes

All queries run entirely in your browser against your database — no external communication is required and once the page is loaded, queries should work even when you're offline.

To use it, you first need a `playhub.db` database file. There are two ways to get one:

### Option A — No Python required but untested (via GitHub Actions)

Forking creates your own copy of this repository under your GitHub account, including an automated workflow that builds the database for you. You can do this entirely from the GitHub website or mobile app — no terminal needed.

1. Click **Fork** at the top-right of this repository on GitHub (you'll need a free GitHub account)
2. In your fork, go to **Settings → Actions → General** and ensure Actions are enabled (forks sometimes have them disabled by default)
3. Go to the **Actions** tab in your fork
4. Select **Generate Database** in the left-hand list and click **Run workflow → Run workflow**
5. Wait for the run to complete (usually a few minutes) — a green tick means success
6. Click into the completed run, scroll to **Artifacts**, and download `playhub-db` — unzip it to get `playhub.db`
7. Visit the [web interface](https://5uperdan.github.io/playhub-scraper/), upload the file, and explore

**Adding a new set championship season:** When a new season is released, edit `.github/workflows/generate-db.yml` in your fork (you can do this directly on GitHub) and add an `add-set-championship-type` call for the new season. Use any event URL from that season — the tool fetches it to extract the internal template UUID. Then re-run the workflow.

### Option B — Generate the database locally (requires Python)

If you have Python set up on your machine, follow the CLI instructions below to generate `playhub.db`, then upload it to the [web interface](https://5uperdan.github.io/playhub-scraper/), use the CLI to query your data, or explore it on your own locally run web interface.

### Running the web interface locally

If you'd prefer not to use the hosted GitHub Pages site, you can serve it from your own machine:

```bash
python3 -m http.server 8000 --directory docs/
```

Then open [http://localhost:8000](http://localhost:8000) in your browser.

---

## CLI quick start

The CLI fetches all data directly from the Play Hub API. No Google Sheets or file downloads needed.

### Requirements

- [uv](https://docs.astral.sh/uv/) (Python package manager)

### Fresh setup

```bash
# Register each set championship season (once per season, using any event from that season)
uv run main.py add-set-championship-type \
  --url "https://tcg.ravensburgerplay.com/events/199050" \
  --name "Fabled"

uv run main.py add-set-championship-type \
  --url "https://tcg.ravensburgerplay.com/events/275408" \
  --name "Whispers in the Well"

uv run main.py add-set-championship-type \
  --url "https://tcg.ravensburgerplay.com/events/440043" \
  --name "Winterspell"

# Import all UK events for all registered seasons
uv run main.py import-set-championship

# Compute Elo ratings
uv run main.py update-ratings

# (Optional) Backtest the win-prediction model
uv run main.py run-backtest
```

### Staying up to date

```bash
# Check for any new events not yet in the database
uv run main.py discover-set-championships

# Import any missing events
uv run main.py import-set-championship

# Refresh ratings
uv run main.py update-ratings
```

> **Note:** If you have an existing `playhub.db` from before this version of the tool, delete it and start fresh — the database schema has changed and the old file is not compatible.

---

## CLI reference

### `add-set-championship-type --url <event-url> --name <display-name>`

Registers a new set championship season in the database. Pass any Play Hub event URL from that season — the tool fetches it to extract the internal `event_configuration_template` UUID that identifies all events for that season. This UUID is stored and used by `import-set-championship` and `discover-set-championships` to find UK events.

Run this once per season before importing.

```bash
uv run main.py add-set-championship-type \
  --url "https://tcg.ravensburgerplay.com/events/275408" \
  --name "Whispers in the Well"
```

---

### `import-set-championship [--name <filter>] [--replace]`

Discovers and imports all UK set championship events for each registered season. For each season, the Play Hub API is queried and any events not already in the database are fetched and stored.

```bash
# Import everything not yet in the database
uv run main.py import-set-championship

# Limit to one season (partial, case-insensitive name match)
uv run main.py import-set-championship --name "Whispers"

# Re-scrape match and standings data for already-imported events
uv run main.py import-set-championship --replace
```

**Update mode (default):** Additive and idempotent. Already-imported competitions are skipped. Safe to run repeatedly.

**Replace mode (`--replace`):** For each competition discovered, all existing match and standings records are deleted and re-scraped from scratch. Player, venue, and round records are never deleted, so a player's full history is preserved through their UUID. Use this when you suspect data is stale or partial.

After importing new data, run `update-ratings` to refresh Elo ratings.

---

### `discover-set-championships [--name <filter>]`

Shows which UK set championship events are already in the database and which are not. Useful for checking whether any events have been missed before running `import-set-championship`.

```bash
uv run main.py discover-set-championships

# Check only one season
uv run main.py discover-set-championships --name "Winterspell"
```

---

### `player-info <player-name>`

Queries the database for a player (case-insensitive, partial match) and prints all their competition history, round-by-round match results, and final positions. If Elo ratings have been calculated, the player's rating and leaderboard rank are shown in the header.

```bash
uv run main.py player-info "Mickey"
```

Example output:
```
Mickey [Elo: 1104.37 | 3rd of 312 | 14 Swiss, 3 KO]
  The Disney Store: 2026-04-05 (34 players)
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

Players who have no history in the local database are listed by name with a `(not found in database)` note. If Elo ratings have been calculated, each player's rating and rank will appear in their header line.

---

### `update-ratings`

Computes Elo ratings for all players from scratch and stores them in the database. Run this after importing new data with `import-set-championship`.

```bash
uv run main.py update-ratings
```

Ratings are **always recalculated from scratch** across the full match history. There is no incremental update — this ensures results are always deterministic and consistent regardless of the order events were imported. After running, the top 10 leaderboard is printed automatically.

---

### `leaderboard [--top <n>] [--name <filter>]`

Prints the Elo rating leaderboard, sorted from highest to lowest. Defaults to 25 players; use `--top` to change this. Use `--name` to filter by player name — when filtering, all matching players are shown (the `--top` limit is ignored) and their global rank is displayed.

```bash
uv run main.py leaderboard
uv run main.py leaderboard --top 50
uv run main.py leaderboard --name "MK_"
```

Example output:
```
Elo Leaderboard — top 25 of 312 rated players

    1. Pluto                           1223.41  (18 Swiss, 5 KO)
    2. Minnie                          1187.05  (22 Swiss, 3 KO)
    3. Mickey                          1104.37  (14 Swiss, 3 KO)
```

The **Swiss match count** is a reliability indicator — the more Swiss matches a player has played, the more their rating reflects real performance. Treat ratings for players with only a handful of matches with caution.

---

### `predict-match --player1 <name> --player2 <name>`

Estimates win probability for a hypothetical head-to-head match based on stored Elo ratings.

```bash
uv run main.py predict-match --player1 "Mickey" --player2 "Pluto"
```

Example output:
```
  Mickey vs Pluto

  Mickey                         Elo: 1104.37  Win probability: 36.2%
  Pluto                          Elo: 1223.41  Win probability: 63.8%
```

A warning is shown if either player has fewer than 5 Swiss matches, as their rating may not be reliable yet. Players not found in the rating table are assumed to be at the starting rating of 1000.

---

### `run-backtest`

Backtests Elo win-probability predictions against historical match outcomes and stores calibration data in the database. Run this after `update-ratings`.

```bash
uv run main.py run-backtest
```

Matches are replayed chronologically. The predicted win probability is recorded *before* the corresponding Elo update is applied — this means every prediction is genuinely out-of-sample; the model hasn't yet seen the match it's predicting. Both Swiss and knockout decisive matches are included; draws are skipped.

Output includes:
- **Brier score** — a single-number accuracy summary (random-guess baseline = 0.2500, perfect = 0.0000; lower is better)
- **Calibration table** — predicted probability buckets vs actual win rates
- **Experience breakdown** — whether predictions are better when both players have more match history

Results are stored in the database so the web interface **Prediction Accuracy** tab can display them.

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
| `set_championship_types` | `uuid` (PK), `display_name`, `event_configuration_template` (unique) |
| `venues` | `ph_uuid` (PK), `name` |
| `players` | `uuid` (PK), `ph_user_id`, `name` |
| `rounds` | `uuid` (PK), `name` (unique, e.g. "Round 1", "Top 8") |
| `competitions` | `uuid` (PK), `ph_event_id`, `name`, `venue_uuid` (FK), `start_date`, `attended_player_count`, `set_championship_type_uuid` (FK, nullable) |
| `matches` | `uuid` (PK), `player_a_uuid` (FK), `player_b_uuid` (FK), `player_a_score`, `player_b_score`, `winning_player_uuid` (FK, NULL = draw), `competition_uuid` (FK), `round_uuid` (FK) |
| `competition_results` | `competition_uuid` + `player_uuid` (composite PK), `position` |
| `player_ratings` | `player_uuid` (PK, FK), `rating`, `match_count`, `last_recalculated_at` |
| `backtest_summary` | `id` (PK), `total_matches`, `brier_score`, `run_at` |
| `backtest_buckets` | `bucket_min` (PK), `match_count`, `actual_wins` |
| `backtest_experience` | `tier_label` (PK), `tier_min`, `match_count`, `actual_wins`, `sum_predicted` |

---

## Elo rating system

Player ratings are computed using a customised [Elo rating system](https://en.wikipedia.org/wiki/Elo_rating_system). This estimates the relative skill level of each player based on their match history and is used to power the leaderboard and `predict-match` command.

### How Elo works

Every player starts at a rating of **1000**. After each match, points are transferred from the loser to the winner. The amount transferred depends on how surprising the result was:

- Beating a much higher-rated player earns a lot of points — the upset was unlikely
- Beating a much lower-rated player earns very few — the outcome was expected
- Two evenly-matched players exchange around 16 points per match

The formula is:

```
Expected win probability:  E = 1 / (1 + 10^((opponent_rating - your_rating) / 400))
Rating change:             Δ = 32 × (result - E)
```

Where `result` is 1 for a win, 0 for a loss, and 0 for a draw. **K=32** means the maximum any single match can move your rating is 32 points (achieved when a 100% underdog pulls off an upset).

### Swiss and knockout rounds

**Swiss rounds** (Round 1, Round 2, etc.) use standard Elo: a win gains points, a loss loses points. Draws have no effect on ratings.

**Knockout rounds** also affect ratings, but through the zero-sum distribution mechanism described below rather than direct win/loss Elo transfer. The net effect depends on how far you go: players who win multiple knockout rounds can easily gain more from those wins than they lose through pool deductions, while players eliminated early in the top cut or not at all will typically lose a small amount. Players eliminated in Swiss absorb pool deductions across every knockout round without any offsetting wins, so they tend to lose the most from this phase.

### Knockout rounds — zero-sum distribution

Knockout matches are **zero-sum**: winners gain Elo, but instead of the loser absorbing that loss, it is shared equally across a growing pool of eliminated players.

Here's how the pool works for a 32-player tournament with a Top 8 cut:

1. **Before knockouts start:** The pool contains all 24 players who didn't make the top cut.
2. **Quarterfinals (Top 8 round):** The 4 winners each gain Elo. The 4 losers join the pool — now 28 players. All 28 take an equal deduction totalling the Elo the winners gained.
3. **Semifinals (Top 4 round):** The 2 winners gain Elo. The 2 losers join the pool — now 30 players. All 30 take an equal deduction totalling the Elo the winners gained.
4. **Final (Top 2 round):** The winner gains Elo. The finalist joins the pool — now 31 players. All 31 take an equal deduction totalling the Elo the winner gained.

The winner of the event gains a small amount of Elo; every other player at the event takes a small, equal deduction. The total Elo across the whole field is unchanged — no inflation.

This design means:
- Making the top cut reduces your Elo losses from the knockout phase — Swiss-only players absorb pool deductions across every knockout round, while top cut players only absorb deductions from the rounds they've already been eliminated in
- A player who finishes Swiss 4-0 and loses in the quarters will always end the event with more Elo than a player who went 0-4 at the same event
- Every tournament is, in aggregate, a zero-sum redistribution of Elo within the field

### Match count and reliability

The **match count** shown alongside each rating tracks how many Swiss matches a player has played. A player with 20+ matches has a well-established rating; a player with 3 matches may have a rating that doesn't yet reflect their true skill. Use match count as a reliability signal — especially when using `predict-match` for players with limited history.

### Recalculation

Ratings are always computed **from scratch** across all historical data in chronological order. There is no incremental update mechanism. This means:

- Adding new competitions and re-running `update-ratings` will produce a fully re-derived leaderboard
- The results are deterministic — running `update-ratings` twice produces the same output
- The stored `player_ratings` table is a cache of the most recent calculation — it must be updated manually after importing new data

### Model accuracy

Running `run-backtest` against the dataset gives a feel for how well the predictions hold up:

- **Brier score: 0.2446** — just below the random-guess baseline of 0.2500, confirming the model adds genuine signal but not a large amount
- **Systematic under-confidence** — in every probability bucket, players win slightly *more* often than predicted. When the model says 60%, players actually win around 67%. This suggests the Elo rating differences are real but underestimated
- **Data-constrained, not wrong** — 77% of all evaluated matches fall in the 50–55% bucket, because most players have short histories and their ratings cluster near 1000. The model correctly says "roughly 50/50" when it doesn't know enough; the slight positive error reflects genuine skill differences the limited data can't yet quantify
- **Experience matters** — predictions involving at least one player with fewer than 5 Swiss matches show a larger error than predictions between more experienced players, as expected

As more competition data is collected, ratings will stabilise further from 1000 and predictions will become more differentiated.

### A note on player and venue names

Player display names on Play Hub can be changed by the user at any time. The database uses the Play Hub internal user ID (`ph_user_id`) as the stable key for deduplication — so the same player is always one record regardless of name changes, and all their historical matches continue to reference the same UUID.

Similarly, store/venue names can change. Venues are keyed by their Play Hub store UUID, not their name, so a renamed venue remains a single record with all its historical competitions intact.

Every run of `import-set-championship` (in either mode) updates the stored name to the latest value seen for each player and venue. This means current display names are always shown, even for older records. There is currently no mechanism to preserve a full name-change history (nor any interest in doing so).

### Byes

Totally skipped at the moment, not even stored as results. Haven't decided the best way to handle them.
