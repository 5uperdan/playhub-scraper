"""
playhub-scraper CLI

Usage:
  uv run main.py add-source <google-sheet-url>
  uv run main.py update-from-source <source-file-name>
  uv run main.py player-info <player-name>
"""

import os
import sys
from datetime import datetime, timezone

import click

import db as _db
import scrape as _scrape

SOURCES_DIR = "sources"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_or_create_player(session, ph_user_id, name, source_uuid):
    """
    Look up a player by their Play Hub user ID. If found, update their name
    (in case it changed). If not found, create a new Player record.
    """
    player = None
    if ph_user_id is not None:
        player = session.query(_db.Player).filter_by(ph_user_id=ph_user_id).first()

    if player is None:
        player = _db.Player(
            ph_user_id=ph_user_id,
            name=name,
            first_source_uuid=source_uuid,
        )
        session.add(player)
        session.flush()
    else:
        # Update name to the latest value seen
        player.name = name

    return player


def _get_or_create_round(session, round_name):
    rnd = session.query(_db.Round).filter_by(name=round_name).first()
    if rnd is None:
        rnd = _db.Round(name=round_name)
        session.add(rnd)
        session.flush()
    return rnd


def _get_or_create_venue(session, ph_uuid, name, source_uuid):
    venue = session.query(_db.Venue).filter_by(ph_uuid=ph_uuid).first()
    if venue is None:
        venue = _db.Venue(ph_uuid=ph_uuid, name=name, first_source_uuid=source_uuid)
        session.add(venue)
        session.flush()
    return venue


def _get_or_create_competition(session, ph_event_id, name, venue_uuid, start_date, player_count):
    comp = session.query(_db.Competition).filter_by(ph_event_id=ph_event_id).first()
    if comp is None:
        comp = _db.Competition(
            ph_event_id=ph_event_id,
            name=name,
            venue_uuid=venue_uuid,
            start_date=start_date,
            attended_player_count=player_count,
        )
        session.add(comp)
        session.flush()
    return comp


# ---------------------------------------------------------------------------
# CLI group
# ---------------------------------------------------------------------------


@click.group()
def cli():
    """playhub-scraper: collect and query Play Hub tournament data."""


# ---------------------------------------------------------------------------
# Command: add-source
# ---------------------------------------------------------------------------


@cli.command("add-source")
@click.argument("sheet_url")
def add_source(sheet_url):
    """Download a Google Sheet and save it as a new source file.

    SHEET_URL can be any Google Sheets share URL or direct export URL.
    The downloaded file is saved under the sources/ directory and
    registered in the database.
    """
    os.makedirs(SOURCES_DIR, exist_ok=True)

    click.echo(f"Downloading sheet: {sheet_url}")
    try:
        xlsx_bytes = _scrape.download_google_sheet(sheet_url)
    except Exception as e:
        click.echo(f"Error downloading sheet: {e}", err=True)
        sys.exit(1)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    file_name = f"source_{timestamp}.xlsx"
    file_path = os.path.join(SOURCES_DIR, file_name)

    with open(file_path, "wb") as f:
        f.write(xlsx_bytes)
    click.echo(f"Saved: {file_path}")

    engine = _db.init_db()
    Session = _db.make_session_factory(engine)
    with Session() as session:
        source = _db.Source(file_name=file_name)
        session.add(source)
        session.commit()
        click.echo(f"Registered source uuid={source.uuid}")


# ---------------------------------------------------------------------------
# Command: update-from-source
# ---------------------------------------------------------------------------


@cli.command("update-from-source")
@click.argument("source_file")
@click.option(
    "--replace",
    is_flag=True,
    default=False,
    help=(
        "Delete and re-scrape all competitions found in this source file. "
        "Player, venue, and round records are preserved. "
        "Use this when you want a clean re-scrape of existing data."
    ),
)
def update_from_source(source_file, replace):
    """Process a source file and populate the database with event data.

    SOURCE_FILE is the file name (not full path) of an XLSX file in the
    sources/ directory, e.g. source_20260414T120000.xlsx

    By default this is additive: new competitions are inserted and existing
    ones are updated (scores, standings, player names) without losing any data.

    Pass --replace to wipe and re-scrape every competition found in the source.
    Player, venue, and round records are never deleted — only per-competition
    match and standings data is removed before re-insertion.
    """
    file_path = os.path.join(SOURCES_DIR, source_file)
    if not os.path.exists(file_path):
        click.echo(f"File not found: {file_path}", err=True)
        sys.exit(1)

    engine = _db.init_db()
    Session = _db.make_session_factory(engine)

    with Session() as session:
        source = session.query(_db.Source).filter_by(file_name=source_file).first()
        if source is None:
            source = _db.Source(file_name=source_file)
            session.add(source)
            session.flush()

        with open(file_path, "rb") as f:
            xlsx_bytes = f.read()

        links = _scrape.extract_playhub_links_from_xlsx(xlsx_bytes)
        click.echo(f"Found {len(links)} Play Hub links in {source_file}")

        for url in links:
            event_id = _scrape.get_event_id_from_url(url)
            if event_id is None:
                click.echo(f"  Skipping (no event ID): {url}")
                continue

            click.echo(f"  Processing event {event_id} …")

            try:
                event = _scrape.fetch_event(event_id)
            except Exception as e:
                click.echo(f"    Warning: could not fetch event {event_id}: {e}")
                continue

            # --- Venue ---
            venue_info = _scrape.get_venue_from_event(event)
            if not venue_info["ph_uuid"]:
                click.echo(f"    Warning: no venue UUID for event {event_id}, skipping")
                continue
            venue = _get_or_create_venue(session, venue_info["ph_uuid"], venue_info["name"], source.uuid)

            # --- Competition ---
            comp_name = event.get("name") or f"Event {event_id}"
            start_date = (event.get("start_datetime") or "")[:10]
            player_count = event.get("starting_player_count")

            existing_comp = session.query(_db.Competition).filter_by(ph_event_id=event_id).first()
            if existing_comp and replace:
                click.echo(f"    Replacing existing data for event {event_id}")
                session.query(_db.Match).filter_by(competition_uuid=existing_comp.uuid).delete()
                session.query(_db.CompetitionResult).filter_by(competition_uuid=existing_comp.uuid).delete()
                session.delete(existing_comp)
                session.flush()
                existing_comp = None

            comp = _get_or_create_competition(session, event_id, comp_name, venue.ph_uuid, start_date, player_count)
            # Always refresh mutable fields in case they changed
            comp.name = comp_name
            comp.attended_player_count = player_count

            # --- Registrations → players + final standings ---
            try:
                registrations = _scrape.fetch_all_registrations(event_id)
            except Exception as e:
                click.echo(f"    Warning: could not fetch registrations: {e}")
                registrations = []

            reg_by_uid = {}
            for reg in registrations:
                user = reg.get("user") or {}
                ph_uid = user.get("id")
                name = reg.get("best_identifier") or user.get("best_identifier") or "Unknown"
                final_place = reg.get("final_place_in_standings")
                player = _get_or_create_player(session, ph_uid, name, source.uuid)
                reg_by_uid[ph_uid] = (player, final_place)

            # Upsert CompetitionResults
            for ph_uid, (player, final_place) in reg_by_uid.items():
                existing = (
                    session.query(_db.CompetitionResult)
                    .filter_by(competition_uuid=comp.uuid, player_uuid=player.uuid)
                    .first()
                )
                if existing is None:
                    session.add(
                        _db.CompetitionResult(
                            competition_uuid=comp.uuid,
                            player_uuid=player.uuid,
                            position=final_place,
                        )
                    )
                else:
                    existing.position = final_place

            # --- Rounds and matches ---
            event_rounds = _scrape.get_rounds_from_event(event)
            click.echo(f"    {len(event_rounds)} rounds to process")

            for round_info in event_rounds:
                round_name = round_info["round_name"]
                round_id = round_info["round_id"]
                db_round = _get_or_create_round(session, round_name)

                try:
                    matches = _scrape.fetch_matches_for_round(round_id)
                except Exception as e:
                    click.echo(f"    Warning: could not fetch {round_name}: {e}")
                    continue

                for match_data in matches:
                    pa_info = match_data["player_a"]
                    pb_info = match_data["player_b"]

                    pa = _get_or_create_player(session, pa_info["ph_user_id"], pa_info["name"], source.uuid)
                    pb = _get_or_create_player(session, pb_info["ph_user_id"], pb_info["name"], source.uuid)

                    winner_uid = match_data["winner_ph_user_id"]
                    if winner_uid == pa_info["ph_user_id"]:
                        winner_uuid = pa.uuid
                    elif winner_uid == pb_info["ph_user_id"]:
                        winner_uuid = pb.uuid
                    else:
                        winner_uuid = None

                    # Avoid duplicate match records (idempotent re-runs)
                    existing_match = (
                        session.query(_db.Match)
                        .filter_by(
                            player_a_uuid=pa.uuid,
                            player_b_uuid=pb.uuid,
                            competition_uuid=comp.uuid,
                            round_uuid=db_round.uuid,
                        )
                        .first()
                    )
                    if existing_match is None:
                        session.add(
                            _db.Match(
                                player_a_uuid=pa.uuid,
                                player_b_uuid=pb.uuid,
                                player_a_score=match_data["player_a_score"],
                                player_b_score=match_data["player_b_score"],
                                winning_player_uuid=winner_uuid,
                                competition_uuid=comp.uuid,
                                round_uuid=db_round.uuid,
                            )
                        )
                    else:
                        existing_match.player_a_score = match_data["player_a_score"]
                        existing_match.player_b_score = match_data["player_b_score"]
                        existing_match.winning_player_uuid = winner_uuid

        source.processed_on = datetime.now(timezone.utc)
        session.commit()

    click.echo("Done.")


# ---------------------------------------------------------------------------
# Command: player-info
# ---------------------------------------------------------------------------


@cli.command("player-info")
@click.argument("player_name")
def player_info(player_name):
    """Show all competitions, matches and results for a player.

    PLAYER_NAME is matched case-insensitively against stored display names.
    If multiple players share a name, results for all of them are shown.

    Example output:
    \b
      Danny
      Element Games: 2026-04-01
        Round 1: Danny 2 - 1 Jim
        Round 2: Alex 2 - 1 Danny
        Final position: 5
    """
    engine = _db.init_db()
    Session = _db.make_session_factory(engine)

    with Session() as session:
        players = session.query(_db.Player).filter(_db.Player.name.ilike(f"%{player_name}%")).all()

        if not players:
            click.echo(f"No player found matching '{player_name}'")
            return

        for player in players:
            click.echo(player.name)

            # Get all competitions this player participated in, ordered by date
            results = session.query(_db.CompetitionResult).filter_by(player_uuid=player.uuid).all()

            if not results:
                click.echo("  (no competition results recorded)")
                continue

            comps = sorted(
                [r.competition for r in results],
                key=lambda c: c.start_date,
            )

            for comp in comps:
                venue_name = comp.venue.name if comp.venue else "Unknown Venue"
                click.echo(f"  {venue_name}: {comp.start_date}")

                # Get matches in this competition, ordered by round
                matches = (
                    session.query(_db.Match)
                    .filter(
                        _db.Match.competition_uuid == comp.uuid,
                        ((_db.Match.player_a_uuid == player.uuid) | (_db.Match.player_b_uuid == player.uuid)),
                    )
                    .all()
                )

                # Load rounds and sort by round_number via name heuristic
                def _round_sort_key(match):
                    name = match.round.name if match.round else ""
                    # "Round N" sorts before "Top N" (elimination)
                    import re as _re

                    swiss = _re.match(r"Round (\d+)", name)
                    top = _re.match(r"Top (\d+)", name)
                    if swiss:
                        return (0, int(swiss.group(1)))
                    if top:
                        # Top 8 comes before Top 4, so invert
                        return (1, -int(top.group(1)))
                    return (2, 0)

                matches = sorted(matches, key=_round_sort_key)

                for match in matches:
                    round_name = match.round.name if match.round else "Unknown Round"
                    pa_name = match.player_a.name
                    pb_name = match.player_b.name
                    pa_score = match.player_a_score
                    pb_score = match.player_b_score

                    if match.winning_player_uuid is None:
                        # Draw
                        if match.player_a_uuid == player.uuid:
                            line = f"{pa_name} TIE {pb_name}"
                        else:
                            line = f"{pa_name} TIE {pb_name}"
                    else:
                        line = f"{pa_name} {pa_score} - {pb_score} {pb_name}"

                    click.echo(f"    {round_name}: {line}")

                comp_result = next((r for r in results if r.competition_uuid == comp.uuid), None)
                if comp_result and comp_result.position is not None:
                    click.echo(f"    Final position: {comp_result.position}")

            click.echo("")


# ---------------------------------------------------------------------------
# Command: list-competitions
# ---------------------------------------------------------------------------


@cli.command("list-competitions")
@click.option("--name", default=None, help="Filter by competition or venue name (case-insensitive, partial match).")
def list_competitions(name):
    """List all processed competitions and their winners.

    Competitions are sorted by date. The winner is the player with
    position 1 in the final standings.

    Optionally filter by competition or venue name:

      uv run main.py list-competitions --name "Element Games"
    """
    engine = _db.init_db()
    Session = _db.make_session_factory(engine)

    with Session() as session:
        query = session.query(_db.Competition)
        if name:
            query = query.join(_db.Venue, _db.Competition.venue_uuid == _db.Venue.ph_uuid, isouter=True).filter(
                _db.Competition.name.ilike(f"%{name}%") | _db.Venue.name.ilike(f"%{name}%")
            )
        comps = query.order_by(_db.Competition.start_date, _db.Competition.name).all()

        if not comps:
            click.echo("No competitions in the database yet.")
            return

        for comp in comps:
            venue_name = comp.venue.name if comp.venue else "Unknown Venue"

            winner_result = (
                session.query(_db.CompetitionResult).filter_by(competition_uuid=comp.uuid, position=1).first()
            )
            winner_name = winner_result.player.name if winner_result and winner_result.player else "Unknown"

            click.echo(f"{comp.start_date}  {venue_name}  —  {comp.name}")
            click.echo(f"  Winner: {winner_name}  ({comp.attended_player_count or '?'} players)")


if __name__ == "__main__":
    cli()
