# Competition Player Numbers

Scrapes Disney Lorcana Set Championship event data from a Google Sheet and collects attended player counts from each Play Hub event page.

## Vibes

This project was fully vibe coded by claude with not a single line of code written by myself. Even most of the readme was created by claude. What a guy, eh?

## What it does

1. Downloads the Google Sheet as XLSX (preserving hyperlinks)
2. Extracts events from sheets whose names begin with "Week"
3. Skips events scheduled in the future
4. Visits each Play Hub link and extracts the attended player count
5. Saves results to `results.csv`, sorted by date then location

## Requirements

- [uv](https://docs.astral.sh/uv/) (Python package manager)

## Running

```bash
uv run scrape.py
```

Output is written to `results.csv` in the same directory.

## CSV format

| Column | Description |
|---|---|
| `date` | Event date (DD/MM/YYYY) |
| `location` | City / region |
| `store_name` | Store name |
| `playhub_link` | Play Hub event URL (if available) |
| `attended_players` | Number of players who attended (if retrievable) |
