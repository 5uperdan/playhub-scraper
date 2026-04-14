import csv
import io
import re
from datetime import date

import openpyxl
import requests
from lxml import html as lhtml

SHEET_ID = "1ozBDV9SERmCBBvzmQyZfilclgJwWmA48"
XLSX_URL = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=xlsx"
OUTPUT_FILE = "results.csv"
PLAYER_XPATH = "/html/body/div[3]/main/div[2]/div/div/div/div[1]/div/div[1]/div[2]/div/div[3]/div[2]"

HEADERS = {"User-Agent": "Mozilla/5.0"}


def fetch_sheet_rows():
    """Download the spreadsheet as XLSX and extract event rows from all sheets."""
    print(f"Downloading spreadsheet: {XLSX_URL}")
    response = requests.get(XLSX_URL, headers=HEADERS, timeout=30)
    response.raise_for_status()

    wb = openpyxl.load_workbook(io.BytesIO(response.content))
    print(f"Sheets found: {wb.sheetnames}")

    rows = []
    seen = set()

    today = date.today()

    for sheet_name in wb.sheetnames:
        if not sheet_name.lower().startswith("week"):
            print(f"  Skipping sheet: {sheet_name}")
            continue
        ws = wb[sheet_name]
        for row in ws.iter_rows(min_row=1):
            # Column A must be a date object
            date_cell = row[0]
            if not hasattr(date_cell.value, "strftime"):
                continue

            # Columns B and C must have text
            location = str(row[1].value).strip() if row[1].value else ""
            store = str(row[2].value).strip() if row[2].value else ""
            if not location or not store:
                continue

            event_date = date_cell.value.date() if hasattr(date_cell.value, "date") else date_cell.value
            # Skip events in the future (keep today)
            if event_date > today:
                continue

            date_str = event_date.strftime("%d/%m/%Y")

            # Column F (index 5): extract hyperlink URL if present
            playhub_link = None
            if len(row) > 5:
                f_cell = row[5]
                if f_cell.hyperlink:
                    playhub_link = f_cell.hyperlink.target.strip()

            # Deduplicate across sheets
            key = (date_str, location, store)
            if key in seen:
                continue
            seen.add(key)

            rows.append(
                {
                    "date": date_str,
                    "location": location,
                    "store_name": store,
                    "playhub_link": playhub_link,
                }
            )

    print(f"Found {len(rows)} event rows.")
    return rows


def _parse_player_text(text):
    """Parse a player count string like '6 players' or '6/64 players', returning the current count."""
    # Matches "6 players" or "6/64 players" — always take the first number
    match = re.match(r"(\d+)(?:/\d+)?", text.strip())
    if match:
        return int(match.group(1))
    return None


def get_player_count(url):
    """Visit a Play Hub event page and return the attended player count."""
    try:
        response = requests.get(url, headers=HEADERS, timeout=15)
        response.raise_for_status()

        # --- Primary: XPATH ---
        tree = lhtml.fromstring(response.content)
        nodes = tree.xpath(PLAYER_XPATH)
        if nodes:
            text = nodes[0].text_content().strip()
            count = _parse_player_text(text)
            if count is not None:
                return count

        # --- Fallback: search page text for "N players" or "N/M players" ---
        match = re.search(r"(\d+)(?:/\d+)?\s+players", response.text, re.IGNORECASE)
        if match:
            return int(match.group(1))

    except Exception as exc:
        print(f"  Warning: could not fetch {url} — {exc}")

    return None


def main():
    rows = fetch_sheet_rows()

    results = []
    for row in rows:
        playhub_link = row["playhub_link"]
        attended = None

        if playhub_link:
            print(f"  Scraping: {playhub_link}")
            attended = get_player_count(playhub_link)
            if attended is not None:
                print(f"    -> {attended} players")
            else:
                print(f"    -> player count not found")

        results.append(
            {
                "date": row["date"],
                "location": row["location"],
                "store_name": row["store_name"],
                "playhub_link": playhub_link or "",
                "attended_players": attended if attended is not None else "",
            }
        )

    results.sort(key=lambda r: (r["date"].split("/")[::-1], r["location"].lower()))

    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "date",
                "location",
                "store_name",
                "playhub_link",
                "attended_players",
            ],
        )
        writer.writeheader()
        writer.writerows(results)

    print(f"\nDone. Results saved to '{OUTPUT_FILE}' ({len(results)} rows).")


if __name__ == "__main__":
    main()
