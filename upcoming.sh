#!/bin/bash

set -e

# cd /home/pi/YOUR_REPO

echo "Resetting local changes..."
git reset --hard HEAD

echo "Pulling latest..."
git pull --rebase

echo "Update set champs db"
uv run main.py import-set-championship --name "Wilds Unknown"

echo "Updating CSVs..."
uv run main.py upcoming-set-champs --name "Wilds Unknown" --postcodes-file postcodes.txt

echo "Pushing latest csvs"
git add docs/*.csv

if git diff --cached --quiet; then
    echo "No changes detected."
    exit 0
fi

echo "Committing changes..."
git commit -m "Automated CSV update $(date -Iseconds)"

echo "Pushing..."
git push

echo "Done."