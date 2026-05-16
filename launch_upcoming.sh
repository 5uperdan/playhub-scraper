#!/bin/bash

# Bootstrap launcher — always runs the latest version of upcoming.sh.
#
# The systemd service (or cron) should point here, NOT at upcoming.sh directly.
# This script pulls the latest code and then exec's upcoming.sh, which replaces
# this process entirely so the freshly-pulled version of upcoming.sh is what runs.

set -e

cd "$(dirname "$(realpath "$0")")"

echo "Resetting local changes..."
git reset --hard HEAD

echo "Pulling latest..."
git pull --rebase

echo "Handing off to upcoming.sh..."
exec ./upcoming.sh
