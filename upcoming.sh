#!/bin/bash

# Main update script. Called by run.sh after it has pulled the latest code.
# Can also be run directly for a one-off update without pulling first.

set -e

cd "$(dirname "$(realpath "$0")")"

echo "Update set champs db"
uv run main.py import-set-championship --name "Wilds Unknown"

echo "Updating CSVs..."
uv run main.py upcoming-set-champs --name "Wilds Unknown" --postcodes-file postcodes.txt

echo "Pushing latest csvs"
git add docs/*.csv docs/postcodes.json

if git diff --cached --quiet; then
    echo "No changes detected."
    exit 0
fi

echo "Committing changes..."
git commit -m "Automated CSV update $(date -Iseconds)"

echo "Pushing..."
git push

echo "Done."