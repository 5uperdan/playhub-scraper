"""
playhub-scraper CLI

Usage:
  uv run main.py add-set-championship-type --url <event-url>
  uv run main.py import-set-championship
  uv run main.py player-info <player-name>
  uv run main.py leaderboard [--name <filter>] [--top <n>]
"""

import csv
import os
import re
import sys
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import date as _date
from datetime import datetime, timezone

import click
import matplotlib
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
from sqlalchemy import func, nullslast

import db as _db
import scrape as _scrape

matplotlib.use("Agg")  # non-interactive backend — no display needed

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_or_create_player(session, ph_user_id, name):
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


def _get_or_create_venue(session, ph_uuid, name, address=None):
    venue = session.query(_db.Venue).filter_by(ph_uuid=ph_uuid).first()
    if venue is None:
        venue = _db.Venue(ph_uuid=ph_uuid, name=name, address=address)
        session.add(venue)
        session.flush()
    else:
        # Update name to the latest value seen, in case the store has been renamed
        venue.name = name
        if address:
            venue.address = address
    return venue


def _get_or_create_competition(session, ph_event_id, name, venue_uuid, start_date, player_count, set_type_uuid=None):
    comp = session.query(_db.Competition).filter_by(ph_event_id=ph_event_id).first()
    if comp is None:
        comp = _db.Competition(
            ph_event_id=ph_event_id,
            name=name,
            venue_uuid=venue_uuid,
            start_date=start_date,
            attended_player_count=player_count,
            set_championship_type_uuid=set_type_uuid,
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


@cli.result_callback()
def _after_command(*args, **kwargs):
    count = _scrape.get_api_call_count()
    if count > 0:
        s = "s" if count != 1 else ""
        click.echo(f"\n({count} API call{s} made)")


# ---------------------------------------------------------------------------
# Shared event-processing helper
# ---------------------------------------------------------------------------


def _process_event(session, event_id, set_type_uuid=None, replace=False):
    """
    Fetch and store one Play Hub event by ID.

    Idempotent: safe to call on already-imported events (updates mutable fields).
    Pass replace=True to wipe and re-scrape match/standings data.

    Returns True if the competition was new or replaced (ratings stale), False otherwise.
    """
    try:
        event = _scrape.fetch_event(event_id)
    except Exception as e:
        click.echo(f"    Warning: could not fetch event {event_id}: {e}")
        return False

    # --- Venue ---
    venue_info = _scrape.get_venue_from_event(event)
    if not venue_info.ph_uuid:
        click.echo(f"    Warning: no venue UUID for event {event_id}, skipping")
        return False
    venue = _get_or_create_venue(session, venue_info.ph_uuid, venue_info.name, address=event.full_address)

    # --- Competition ---
    comp_name = event.name or f"Event {event_id}"
    # start_datetime is an ISO 8601 string e.g. "2026-04-18T10:49:49+00:00"; split on T to get just the date
    start_date = (event.start_datetime or "").split("T")[0]
    player_count = event.starting_player_count

    existing_comp = session.query(_db.Competition).filter_by(ph_event_id=event_id).first()
    new_data = existing_comp is None or replace
    if existing_comp and replace:
        click.echo(f"    Replacing existing data for event {event_id}")
        session.query(_db.Match).filter_by(competition_uuid=existing_comp.uuid).delete()
        session.query(_db.CompetitionResult).filter_by(competition_uuid=existing_comp.uuid).delete()
        session.delete(existing_comp)
        session.flush()
        existing_comp = None

    comp = _get_or_create_competition(
        session, event_id, comp_name, venue.ph_uuid, start_date, player_count, set_type_uuid
    )
    comp.name = comp_name
    comp.attended_player_count = player_count
    comp.is_complete = event.display_status == "complete"
    if set_type_uuid and comp.set_championship_type_uuid is None:
        comp.set_championship_type_uuid = set_type_uuid

    # --- Registrations → players (positions assigned after rounds are known) ---
    try:
        registrations = _scrape.fetch_all_registrations(event_id)
    except Exception as e:
        click.echo(f"    Warning: could not fetch registrations: {e}")
        registrations = []

    reg_by_uid = {}
    for reg in registrations:
        player = _get_or_create_player(session, reg.ph_user_id, reg.name)
        reg_by_uid[reg.ph_user_id] = (player, reg.final_place)

    for ph_uid, (player, _) in reg_by_uid.items():
        existing = (
            session.query(_db.CompetitionResult).filter_by(competition_uuid=comp.uuid, player_uuid=player.uuid).first()
        )
        if existing is None:
            session.add(
                _db.CompetitionResult(
                    competition_uuid=comp.uuid,
                    player_uuid=player.uuid,
                    position=None,
                )
            )

    # --- Rounds and matches ---
    event_rounds = _scrape.get_rounds_from_event(event)
    click.echo(f"    {len(event_rounds)} rounds to process")

    need_start_time = bool(event_rounds) and comp.start_time is None
    elim_match_data = {}  # round_name -> list[MatchInfo], collected for standings computation

    for i, round_info in enumerate(event_rounds):
        round_name = round_info.round_name
        round_id = round_info.round_id
        db_round = _get_or_create_round(session, round_name)

        try:
            matches = _scrape.fetch_matches_for_round(round_id)
        except Exception as e:
            click.echo(f"    Warning: could not fetch {round_name}: {e}")
            continue

        # Extract start_time from the earliest created_at in Round 1's matches
        if i == 0 and need_start_time:
            timestamps = [m.created_at for m in matches if m.created_at]
            if timestamps:
                comp.start_time = min(timestamps)

        if _is_elimination_round(round_name):
            elim_match_data[round_name] = matches

        for match_data in matches:
            pa_info = match_data.player_a
            pb_info = match_data.player_b

            pa = _get_or_create_player(session, pa_info.ph_user_id, pa_info.name)
            pb = _get_or_create_player(session, pb_info.ph_user_id, pb_info.name)

            winner_uid = match_data.winner_ph_user_id
            if winner_uid == pa_info.ph_user_id:
                winner_uuid = pa.uuid
            elif winner_uid == pb_info.ph_user_id:
                winner_uuid = pb.uuid
            else:
                winner_uuid = None

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
                        player_a_score=match_data.player_a_score,
                        player_b_score=match_data.player_b_score,
                        winning_player_uuid=winner_uuid,
                        competition_uuid=comp.uuid,
                        round_uuid=db_round.uuid,
                    )
                )
            else:
                existing_match.player_a_score = match_data.player_a_score
                existing_match.player_b_score = match_data.player_b_score
                existing_match.winning_player_uuid = winner_uuid

    # --- Final standings ---
    if not elim_match_data:
        # Swiss-only tournament: use the positions returned by the API registrations endpoint.
        # Detect shared places so they can be prefixed with "=".
        from collections import Counter

        place_counts = Counter(final_place for _, (_, final_place) in reg_by_uid.items() if final_place is not None)
        for ph_uid, (player, final_place) in reg_by_uid.items():
            if final_place is None:
                pos_str = None
            else:
                label = _ordinal(final_place)
                pos_str = f"={label}" if place_counts[final_place] > 1 else label
            existing = (
                session.query(_db.CompetitionResult)
                .filter_by(competition_uuid=comp.uuid, player_uuid=player.uuid)
                .first()
            )
            if existing is not None:
                existing.position = pos_str
            if pos_str == "1st":
                comp.winning_player_uuid = player.uuid
    else:
        # Tournament had knockout rounds.  The API registration standings are unreliable
        # once elimination matches exist, so compute positions from match results instead.
        # Winner of Top 2 → "1st", loser → "2nd".
        # Losers of Top N (N > 2) → "=<N/2+1><suffix>", e.g. Top 4 losers → "=3rd".
        # Players who did not reach the knockout stage → null.
        positions = {}  # ph_user_id -> position string

        ko_round_names = sorted(
            elim_match_data.keys(),
            key=lambda r: int(re.match(r"Top (\d+)", r).group(1)),
            reverse=True,
        )

        for rname in ko_round_names:
            n = int(re.match(r"Top (\d+)", rname).group(1))
            for match in elim_match_data[rname]:
                winner_uid = match.winner_ph_user_id
                pa_uid = match.player_a.ph_user_id
                pb_uid = match.player_b.ph_user_id
                loser_uid = pb_uid if winner_uid == pa_uid else pa_uid

                if n == 2:
                    if winner_uid is not None:
                        positions[winner_uid] = "1st"
                        # Look up the player record to set winning_player_uuid on comp
                        winner_player = reg_by_uid.get(winner_uid, (None,))[0]
                        if winner_player is None:
                            winner_player = session.query(_db.Player).filter_by(ph_user_id=winner_uid).first()
                        if winner_player is not None:
                            comp.winning_player_uuid = winner_player.uuid
                    if loser_uid is not None:
                        positions[loser_uid] = "2nd"
                else:
                    if loser_uid is not None:
                        positions[loser_uid] = f"={_ordinal(n // 2 + 1)}"

        # Apply computed positions to all registered players.
        # Players not reached the knockout stage get null.
        for ph_uid, (player, _) in reg_by_uid.items():
            pos_label = positions.get(ph_uid)
            existing = (
                session.query(_db.CompetitionResult)
                .filter_by(competition_uuid=comp.uuid, player_uuid=player.uuid)
                .first()
            )
            if existing is not None:
                existing.position = pos_label

        # Edge case: knockout players not in registrations.
        for ph_uid, pos_label in positions.items():
            if ph_uid in reg_by_uid:
                continue
            player = session.query(_db.Player).filter_by(ph_user_id=ph_uid).first()
            if player is None:
                continue
            existing = (
                session.query(_db.CompetitionResult)
                .filter_by(competition_uuid=comp.uuid, player_uuid=player.uuid)
                .first()
            )
            if existing is not None:
                existing.position = pos_label
            else:
                session.add(
                    _db.CompetitionResult(
                        competition_uuid=comp.uuid,
                        player_uuid=player.uuid,
                        position=pos_label,
                    )
                )

    return new_data


# ---------------------------------------------------------------------------
# Command: add-set-championship-type
# ---------------------------------------------------------------------------


@cli.command("add-set-championship-type")
@click.option("--url", required=True, help="URL of any Play Hub event from this set championship season.")
@click.option("--name", "display_name", required=True, help="Display name for this set, e.g. 'Whispers in the Well'.")
def add_set_championship_type(url, display_name):
    """Register a new set championship type using a sample event URL.

    Fetches the given event to extract its event_configuration_template UUID,
    which uniquely identifies all events for that set championship season.
    This UUID is stored in the database and used by import-set-championship
    to discover and import all UK events for this season.

    Run this once per set championship season, using any event from that season:

    \b
      uv run main.py add-set-championship-type \\
        --url "https://tcg.ravensburgerplay.com/events/275408" \\
        --name "Whispers in the Well"
    """
    _scrape.reset_api_call_count()

    event_id = _scrape.get_event_id_from_url(url)
    if event_id is None:
        click.echo(f"Could not extract an event ID from: {url}", err=True)
        sys.exit(1)

    click.echo(f"Fetching event {event_id} to extract template UUID…")
    try:
        event = _scrape.fetch_event(event_id)
    except Exception as e:
        click.echo(f"Error fetching event: {e}", err=True)
        sys.exit(1)

    tmpl = event.event_configuration_template
    if not tmpl:
        click.echo("Could not find event_configuration_template in the event data.", err=True)
        sys.exit(1)

    engine = _db.init_db()
    Session = _db.make_session_factory(engine)

    with Session() as session:
        existing = session.query(_db.SetChampionshipType).filter_by(event_configuration_template=tmpl).first()
        if existing is not None:
            click.echo(f"This template UUID is already registered as '{existing.display_name}'.\n" f"  UUID: {tmpl}")
            return

        sct = _db.SetChampionshipType(display_name=display_name, event_configuration_template=tmpl)
        session.add(sct)
        session.commit()
        click.echo("Registered set championship type:")
        click.echo(f"  Name:     {display_name}")
        click.echo(f"  Template: {tmpl}")
        click.echo(f"  UUID:     {sct.uuid}")
        click.echo("\nRun 'uv run main.py import-set-championship' to import all UK events for this season.")


# ---------------------------------------------------------------------------
# Command: import-set-championship
# ---------------------------------------------------------------------------


@cli.command("import-set-championship")
@click.option("--name", "filter_name", default=None, help="Only import events for this set (partial name match).")
@click.option(
    "--replace",
    is_flag=True,
    default=False,
    help="Delete and re-scrape match/standings data for competitions that already exist.",
)
def import_set_championship(filter_name, replace):
    """Discover and import all UK set championship events from Play Hub.

    Looks up registered set championship types in the database, fetches all
    UK events for each type from the Play Hub API, and imports any that are
    not yet in the database. Already-imported events are skipped unless
    --replace is passed.

    Use --name to limit to a specific set (partial, case-insensitive):

    \b
      uv run main.py import-set-championship --name "Whispers"
      uv run main.py import-set-championship --name "Winterspell" --replace

    Register new set championship types first with add-set-championship-type.
    """
    _scrape.reset_api_call_count()

    engine = _db.init_db()
    Session = _db.make_session_factory(engine)

    with Session() as session:
        query = session.query(_db.SetChampionshipType)
        if filter_name:
            query = query.filter(_db.SetChampionshipType.display_name.ilike(f"%{filter_name}%"))
        set_types = query.all()

        if not set_types:
            if filter_name:
                click.echo(f"No set championship types matching '{filter_name}' found in the database.")
            else:
                click.echo(
                    "No set championship types registered. " "Run 'uv run main.py add-set-championship-type' first."
                )
            return

        click.echo(f"Discovering UK events for {len(set_types)} set championship type(s)…\n")

        def _progress(label, page, total):
            click.echo(f"  {label}: page {page}/{total}…", nl=False)
            click.echo("\r", nl=False)

        set_type_infos = [
            _scrape.SetTypeInfo(
                display_name=st.display_name,
                event_configuration_template=st.event_configuration_template,
            )
            for st in set_types
        ]
        try:
            discovered = _scrape.fetch_uk_set_championships(set_type_infos, progress_callback=_progress)
        except Exception as e:
            click.echo(f"Error fetching events: {e}", err=True)
            sys.exit(1)

        click.echo(f"Found {len(discovered)} UK events. Processing…\n")

        # Build lookup: template → set_championship_type_uuid
        tmpl_to_uuid = {st.event_configuration_template: st.uuid for st in set_types}

        new_data_inserted = False
        for ev in discovered:
            event_id = ev.event_id
            tmpl = ev.set_championship_type_template
            set_type_uuid = tmpl_to_uuid.get(tmpl) if tmpl else None

            existing = session.query(_db.Competition).filter_by(ph_event_id=event_id).first()
            if existing is not None and not replace:
                if existing.is_complete:
                    continue

            click.echo(f"  Processing event {event_id}: {ev.name} …")
            was_new = _process_event(session, event_id, set_type_uuid=set_type_uuid, replace=replace)
            if was_new:
                new_data_inserted = True

        session.commit()

    click.echo("\nDone.")
    if new_data_inserted:
        click.echo("Ratings may be stale — run 'uv run main.py update-ratings' to recalculate.")


# ---------------------------------------------------------------------------
# Ratings helpers
# ---------------------------------------------------------------------------


@dataclass
class RatingSnapshot:
    competition_uuid: str
    rating: float
    match_count: int
    date: str


@dataclass
class PlayerRatingResult:
    rating: float
    swiss_match_count: int
    history: list = field(default_factory=list)  # list[RatingSnapshot]


def _ordinal(n: int) -> str:
    """Return ordinal string for a number, e.g. 1 -> '1st'."""
    if 11 <= (n % 100) <= 13:
        return f"{n}th"
    suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _is_elimination_round(round_name: str) -> bool:
    """Return True for knockout rounds (Top N), False for Swiss (Round N)."""
    return bool(re.match(r"^Top \d+$", round_name))


def _compute_ratings(session) -> dict:
    """
    Replay all matches chronologically and compute Delo ratings.

    Rules:
    - Swiss rounds only: standard Delo (K=32). Draws have no effect.
    - Elimination rounds: winners gain Delo normally. The total Delo gained by
      all winners in a knockout round is distributed as an equal loss across
      the cumulative eliminated pool (Swiss non-qualifiers + all prior knockout
      losers + this round's losers). The pool grows each round.
    - Always computed from scratch from all stored matches.

    Returns:
        dict[player_uuid, PlayerRatingResult]
    """
    K = 32
    ratings = {}  # player_uuid -> float
    swiss_match_counts = {}  # player_uuid -> int
    total_match_counts = {}  # player_uuid -> int (Swiss + KO)
    history: list[tuple[str, RatingSnapshot]] = []

    def get_rating(uuid):
        return ratings.get(uuid, 1000.0)

    def expected(ra, rb):
        return 1.0 / (1.0 + 10 ** ((rb - ra) / 400.0))

    comps = (
        session.query(_db.Competition)
        .order_by(
            _db.Competition.start_date,
            nullslast(_db.Competition.start_time),
            _db.Competition.ph_event_id,
        )
        .all()
    )

    for comp in comps:
        all_matches = session.query(_db.Match).filter_by(competition_uuid=comp.uuid).all()

        swiss_matches = [m for m in all_matches if not _is_elimination_round(m.round.name if m.round else "")]
        elim_matches = [m for m in all_matches if _is_elimination_round(m.round.name if m.round else "")]

        swiss_matches.sort(key=_round_sort_key)

        # Track all players appearing in Swiss and in elimination
        swiss_participants = set()
        for m in swiss_matches:
            swiss_participants.add(m.player_a_uuid)
            swiss_participants.add(m.player_b_uuid)

        knockout_participants = set()
        for m in elim_matches:
            knockout_participants.add(m.player_a_uuid)
            knockout_participants.add(m.player_b_uuid)

        all_participants = swiss_participants | knockout_participants

        # --- Swiss phase ---
        for m in swiss_matches:
            if m.winning_player_uuid is None:  # draw — skip
                continue
            winner_uuid = m.winning_player_uuid
            loser_uuid = m.player_b_uuid if m.player_a_uuid == winner_uuid else m.player_a_uuid
            ra, rb = get_rating(winner_uuid), get_rating(loser_uuid)
            ea = expected(ra, rb)
            ratings[winner_uuid] = ra + K * (1 - ea)
            ratings[loser_uuid] = rb + K * (0 - (1 - ea))
            swiss_match_counts[winner_uuid] = swiss_match_counts.get(winner_uuid, 0) + 1
            swiss_match_counts[loser_uuid] = swiss_match_counts.get(loser_uuid, 0) + 1
            total_match_counts[winner_uuid] = total_match_counts.get(winner_uuid, 0) + 1
            total_match_counts[loser_uuid] = total_match_counts.get(loser_uuid, 0) + 1

        # --- Elimination phase ---
        # Players who didn't make the top cut start in the eliminated pool
        eliminated_pool = swiss_participants - knockout_participants

        # Group elimination matches by round name, sort descending by N
        # (Top 16 before Top 8 before Top 4 before Top 2)
        elim_by_round = {}
        for m in elim_matches:
            rname = m.round.name if m.round else ""
            elim_by_round.setdefault(rname, []).append(m)

        def _elim_sort_key(rname):
            match = re.match(r"^Top (\d+)$", rname)
            return -int(match.group(1)) if match else 0

        for rname in sorted(elim_by_round.keys(), key=_elim_sort_key):
            round_elo_gained = 0.0
            round_losers = set()

            for m in elim_by_round[rname]:
                if m.winning_player_uuid is None:  # draw — no effect
                    continue
                winner_uuid = m.winning_player_uuid
                loser_uuid = m.player_b_uuid if m.player_a_uuid == winner_uuid else m.player_a_uuid
                ra, rb = get_rating(winner_uuid), get_rating(loser_uuid)
                ea = expected(ra, rb)
                gain = K * (1 - ea)
                ratings[winner_uuid] = ra + gain
                round_elo_gained += gain
                round_losers.add(loser_uuid)
                total_match_counts[winner_uuid] = total_match_counts.get(winner_uuid, 0) + 1
                total_match_counts[loser_uuid] = total_match_counts.get(loser_uuid, 0) + 1

            # Add this round's losers to the cumulative pool
            eliminated_pool.update(round_losers)

            # Distribute the total gain as an equal loss across the whole pool
            if round_elo_gained > 0 and eliminated_pool:
                loss_per_player = round_elo_gained / len(eliminated_pool)
                for uuid in eliminated_pool:
                    ratings[uuid] = get_rating(uuid) - loss_per_player

        # Snapshot every participant's rating after this competition is fully processed
        if all_participants and all_matches:
            for uuid in all_participants:
                history.append(
                    (
                        uuid,
                        RatingSnapshot(
                            competition_uuid=comp.uuid,
                            rating=get_rating(uuid),
                            match_count=total_match_counts.get(uuid, 0),
                            date=comp.start_date,
                        ),
                    )
                )

    all_uuids = set(ratings.keys()) | set(swiss_match_counts.keys()) | {uuid for uuid, _ in history}
    results = {
        uuid: PlayerRatingResult(
            rating=ratings.get(uuid, 1000.0),
            swiss_match_count=swiss_match_counts.get(uuid, 0),
        )
        for uuid in all_uuids
    }
    for player_uuid, snap in history:
        results[player_uuid].history.append(snap)
    return results


# ---------------------------------------------------------------------------
# Display helper
# ---------------------------------------------------------------------------


def _round_sort_key(match):
    name = match.round.name if match.round else ""
    swiss = re.match(r"Round (\d+)", name)
    top = re.match(r"Top (\d+)", name)
    if swiss:
        return (0, int(swiss.group(1)))
    if top:
        # Top 8 comes before Top 4, so invert
        return (1, -int(top.group(1)))
    return (2, 0)


def _get_knockout_count(session, player_uuid: str) -> int:
    """Return the number of knockout (elimination) matches a player has played."""
    all_player_matches = (
        session.query(_db.Match)
        .filter((_db.Match.player_a_uuid == player_uuid) | (_db.Match.player_b_uuid == player_uuid))
        .join(_db.Round, _db.Match.round_uuid == _db.Round.uuid)
        .all()
    )
    return sum(1 for m in all_player_matches if _is_elimination_round(m.round.name if m.round else ""))


def _print_player_info(session, player):
    """Print a player's competition history to stdout."""
    pr = session.query(_db.PlayerRating).filter_by(player_uuid=player.uuid).first()
    if pr is not None:
        total_rated = session.query(_db.PlayerRating).count()
        rank = session.query(_db.PlayerRating).filter(_db.PlayerRating.rating > pr.rating).count() + 1
        knockout_count = _get_knockout_count(session, player.uuid)
        elo_str = (
            f" [Delo: {pr.rating:.2f} | {_ordinal(rank)} of {total_rated}"
            f" | {pr.match_count} Swiss, {knockout_count} KO]"
        )
    else:
        elo_str = ""
    click.echo(f"{player.name}{elo_str}")

    results = session.query(_db.CompetitionResult).filter_by(player_uuid=player.uuid).all()

    if not results:
        click.echo("  (no competition results recorded)")
        click.echo("")
        return

    comps = sorted(
        [r.competition for r in results],
        key=lambda c: c.start_date,
    )

    for comp in comps:
        venue_name = comp.venue.name if comp.venue else "Unknown Venue"
        player_count = f" ({comp.attended_player_count} players)" if comp.attended_player_count else ""
        click.echo(f"  {venue_name}: {comp.start_date}{player_count}")

        matches = (
            session.query(_db.Match)
            .filter(
                _db.Match.competition_uuid == comp.uuid,
                ((_db.Match.player_a_uuid == player.uuid) | (_db.Match.player_b_uuid == player.uuid)),
            )
            .all()
        )

        matches = sorted(matches, key=_round_sort_key)

        for match in matches:
            round_name = match.round.name if match.round else "Unknown Round"
            pa_name = match.player_a.name
            pb_name = match.player_b.name
            pa_score = match.player_a_score
            pb_score = match.player_b_score

            if match.winning_player_uuid is None:
                line = f"{pa_name} TIE {pb_name}"
            else:
                line = f"{pa_name} {pa_score} - {pb_score} {pb_name}"

            click.echo(f"    {round_name}: {line}")

        comp_result = next((r for r in results if r.competition_uuid == comp.uuid), None)
        if comp_result and comp_result.position is not None:
            click.echo(f"    Final position: {comp_result.position}")
        click.echo("")

    click.echo("")


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
            _print_player_info(session, player)


# ---------------------------------------------------------------------------
# Command: tournament-report
# ---------------------------------------------------------------------------


@cli.command("tournament-report")
@click.option(
    "--url",
    required=True,
    help="Play Hub tournament URL, e.g. https://tcg.ravensburgerplay.com/events/12345",
)
def tournament_report(url):
    """Print a history report for every player registered in a tournament.

    Fetches the attendance list for the given tournament from the Play Hub API,
    then looks up each player in the local database and prints their full
    competition history. Players not yet in the database are noted.

    The tournament may be upcoming — only the registration list is fetched,
    not match data.

    \b
    Example:
      uv run main.py tournament-report --url "https://tcg.ravensburgerplay.com/events/12345"
    """
    _scrape.reset_api_call_count()
    event_id = _scrape.get_event_id_from_url(url)
    if event_id is None:
        click.echo(f"Could not extract an event ID from: {url}", err=True)
        sys.exit(1)

    click.echo(f"Fetching registrations for event {event_id}…")
    try:
        registrations = _scrape.fetch_all_registrations(event_id)
    except Exception as e:
        click.echo(f"Error fetching registrations: {e}", err=True)
        sys.exit(1)

    if not registrations:
        click.echo("No registrations found for this event.")
        return

    # Build ordered list of (ph_user_id, display_name) from registrations
    players_to_lookup = []
    for reg in registrations:
        user = reg.get("user") or {}
        ph_uid = user.get("id")
        name = reg.get("best_identifier") or user.get("best_identifier") or "Unknown"
        players_to_lookup.append((ph_uid, name))

    click.echo(f"Found {len(players_to_lookup)} registered players.\n")

    engine = _db.init_db()
    Session = _db.make_session_factory(engine)

    with Session() as session:
        for ph_uid, reg_name in players_to_lookup:
            # Prefer lookup by Play Hub user ID for accuracy; fall back to name
            player = None
            if ph_uid is not None:
                player = session.query(_db.Player).filter_by(ph_user_id=ph_uid).first()
            if player is None:
                player = session.query(_db.Player).filter(_db.Player.name.ilike(reg_name)).first()

            if player is None:
                click.echo(reg_name)
                click.echo("  (not found in database)\n")
                continue

            _print_player_info(session, player)


# ---------------------------------------------------------------------------
# Command: update-ratings
# ---------------------------------------------------------------------------


@cli.command("update-ratings")
def update_ratings():
    """Recompute Delo ratings for all players from scratch and store them.

    Ratings are always recalculated from the full match history — no incremental
    updates. Run this after importing new competition data with update-from-source.

    Raises an error if any player has more than one competition on the same date,
    as the processing order would be ambiguous and the ratings undefined.
    """
    engine = _db.init_db()
    Session = _db.make_session_factory(engine)

    with Session() as session:

        click.echo("Computing ratings from scratch…")
        results = _compute_ratings(session)

        now = datetime.now(timezone.utc)

        # Update final ratings
        for player_uuid, result in results.items():
            existing = session.query(_db.PlayerRating).filter_by(player_uuid=player_uuid).first()
            if existing is None:
                session.add(
                    _db.PlayerRating(
                        player_uuid=player_uuid,
                        rating=result.rating,
                        match_count=result.swiss_match_count,
                        last_recalculated_at=now,
                    )
                )
            else:
                existing.rating = result.rating
                existing.match_count = result.swiss_match_count
                existing.last_recalculated_at = now

        # Replace rating history
        session.query(_db.PlayerRatingHistory).delete()
        history_count = 0
        for player_uuid, result in results.items():
            for snap in result.history:
                session.add(
                    _db.PlayerRatingHistory(
                        player_uuid=player_uuid,
                        competition_uuid=snap.competition_uuid,
                        rating=snap.rating,
                        match_count=snap.match_count,
                        date=snap.date,
                    )
                )
                history_count += 1

        session.commit()
        click.echo(f"Updated ratings for {len(results)} players.")
        click.echo(f"Stored {history_count} rating history snapshots.")

        top = (
            session.query(_db.PlayerRating)
            .join(_db.Player, _db.PlayerRating.player_uuid == _db.Player.uuid)
            .order_by(_db.PlayerRating.rating.desc())
            .limit(10)
            .all()
        )
        click.echo("\nTop 10:")
        for i, pr in enumerate(top, 1):
            ko = _get_knockout_count(session, pr.player_uuid)
            click.echo(f"  {i:3}. {pr.player.name:<30} {pr.rating:7.2f}  ({pr.match_count} Swiss, {ko} KO)")


# ---------------------------------------------------------------------------
# Command: leaderboard
# ---------------------------------------------------------------------------


@cli.command("leaderboard")
@click.option("--top", "top_n", default=25, show_default=True, help="Number of players to show.")
@click.option("--name", "filter_name", default=None, help="Filter by player name (partial, case-insensitive).")
def leaderboard(top_n, filter_name):
    """Show the Delo rating leaderboard.

    Displays the top N players sorted by rating. The Swiss match count shows how
    many decisive (win or loss) Swiss matches a player has played — draws are not
    counted. A higher count means a more reliable rating. The KO count shows
    knockout matches played.

    Use --name to search for specific players by name:

    \b
      uv run main.py leaderboard --name "MK_"
      uv run main.py leaderboard --name "MK_" --top 50

    Run update-ratings first to generate or refresh the ratings.
    """
    engine = _db.init_db()
    Session = _db.make_session_factory(engine)

    with Session() as session:
        query = (
            session.query(_db.PlayerRating)
            .join(_db.Player, _db.PlayerRating.player_uuid == _db.Player.uuid)
            .order_by(_db.PlayerRating.rating.desc())
        )
        if filter_name:
            query = query.filter(_db.Player.name.ilike(f"%{filter_name}%"))
        else:
            query = query.limit(top_n)

        results = query.all()

        if not results:
            if filter_name:
                click.echo(f"No rated players found matching '{filter_name}'.")
            else:
                click.echo("No ratings found. Run 'uv run main.py update-ratings' first.")
            return

        total_rated = session.query(_db.PlayerRating).count()
        if filter_name:
            header = f"Delo Leaderboard — {len(results)} player(s) matching '{filter_name}' of {total_rated} rated"
        else:
            header = f"Delo Leaderboard — top {min(top_n, len(results))} of {total_rated} rated players"
        header += "\nDraws are not added to a player's swiss match count"
        click.echo(header + "\n")

        # Compute global rank for each result
        for pr in results:
            rank = session.query(_db.PlayerRating).filter(_db.PlayerRating.rating > pr.rating).count() + 1
            ko = _get_knockout_count(session, pr.player_uuid)
            click.echo(f"  {rank:3}. {pr.player.name:<30} {pr.rating:7.2f}  ({pr.match_count} Swiss, {ko} KO)")


# ---------------------------------------------------------------------------
# Command: predict-match
# ---------------------------------------------------------------------------


@cli.command("predict-match")
@click.option("--player1", required=True, help="Name of the first player (partial match).")
@click.option("--player2", required=True, help="Name of the second player (partial match).")
def predict_match(player1, player2):
    """Estimate win probability for a head-to-head match.

    Looks up stored Delo ratings for both players and calculates expected win
    probability from the rating difference. Players with fewer than 5 Swiss
    matches will trigger a low-confidence warning.

    Run update-ratings first to ensure ratings are current.
    """
    LOW_MATCH_THRESHOLD = 5

    engine = _db.init_db()
    Session = _db.make_session_factory(engine)

    with Session() as session:

        def lookup(name):
            players = session.query(_db.Player).filter(_db.Player.name.ilike(f"%{name}%")).all()
            if not players:
                return None, 1000.0, 0
            player = players[0]
            pr = session.query(_db.PlayerRating).filter_by(player_uuid=player.uuid).first()
            swiss = pr.match_count if pr else 0
            ko = _get_knockout_count(session, player.uuid)
            return player, (pr.rating if pr else 1000.0), swiss + ko

        p1, r1, mc1 = lookup(player1)
        p2, r2, mc2 = lookup(player2)

        if p1 is None:
            click.echo(f"No player found matching '{player1}'", err=True)
            sys.exit(1)
        if p2 is None:
            click.echo(f"No player found matching '{player2}'", err=True)
            sys.exit(1)

        e1 = 1.0 / (1.0 + 10 ** ((r2 - r1) / 400.0))
        e2 = 1.0 - e1

        click.echo(f"\n  {p1.name} vs {p2.name}\n")
        click.echo(f"  {p1.name:<30} Delo: {r1:7.2f}  Win probability: {e1 * 100:.1f}%")
        click.echo(f"  {p2.name:<30} Delo: {r2:7.2f}  Win probability: {e2 * 100:.1f}%")

        warnings = []
        if mc1 < LOW_MATCH_THRESHOLD:
            warnings.append(f"  Warning: {p1.name} has only {mc1} match(es) — low confidence.")
        if mc2 < LOW_MATCH_THRESHOLD:
            warnings.append(f"  Warning: {p2.name} has only {mc2} match(es) — low confidence.")
        if warnings:
            click.echo("")
            for w in warnings:
                click.echo(w)


# ---------------------------------------------------------------------------
# Backtest helper
# ---------------------------------------------------------------------------

_EXP_TIERS = [
    ("0", 0, lambda n: n == 0),
    ("1-4", 1, lambda n: 1 <= n <= 4),
    ("5-9", 5, lambda n: 5 <= n <= 9),
    ("10-19", 10, lambda n: 10 <= n <= 19),
    ("20+", 20, lambda n: n >= 20),
]
_BUCKET_MINS = [round(0.50 + i * 0.05, 2) for i in range(10)]  # 0.50 … 0.95


def _compute_backtest(session):
    """
    Replay all matches chronologically, capturing pre-match win predictions,
    then evaluate calibration against actual outcomes.

    Predictions are always recorded from the favourite's perspective (prob ≥ 0.5)
    to avoid symmetric duplication. Both Swiss and knockout decisive matches are
    included. Draws are skipped.

    Returns (total_matches, brier_score, bucket_data, exp_data) where:
      bucket_data: {bucket_min: {"count": int, "wins": int}}
      exp_data:    {tier_label: {"tier_min": int, "count": int, "wins": int,
                                 "sum_pred": float}}
    """
    K = 32
    ratings = {}
    swiss_counts = {}

    def get_rating(uuid):
        return ratings.get(uuid, 1000.0)

    def elo_expected(ra, rb):
        return 1.0 / (1.0 + 10 ** ((rb - ra) / 400.0))

    bucket_data = {b: {"count": 0, "wins": 0} for b in _BUCKET_MINS}
    exp_data = {label: {"tier_min": tmin, "count": 0, "wins": 0, "sum_pred": 0.0} for label, tmin, _ in _EXP_TIERS}
    total_matches = 0
    brier_sum = 0.0

    def _record(pa_uuid, pb_uuid, winner_uuid):
        """Record one prediction and update accumulators (nonlocal)."""
        nonlocal total_matches, brier_sum
        ra, rb = get_rating(pa_uuid), get_rating(pb_uuid)
        ea = elo_expected(ra, rb)

        # Always evaluate from the favourite's perspective
        if ea >= 0.5:
            fav_prob = ea
            fav_won = winner_uuid == pa_uuid
        else:
            fav_prob = 1.0 - ea
            fav_won = winner_uuid == pb_uuid

        actual = 1 if fav_won else 0

        # Calibration bucket
        raw = fav_prob - (fav_prob % 0.05)
        bucket_key = round(min(0.95, max(0.50, raw)), 2)
        bucket_data[bucket_key]["count"] += 1
        bucket_data[bucket_key]["wins"] += actual

        # Experience tier — keyed by the less experienced player
        min_count = min(swiss_counts.get(pa_uuid, 0), swiss_counts.get(pb_uuid, 0))
        for label, _, pred in _EXP_TIERS:
            if pred(min_count):
                exp_data[label]["count"] += 1
                exp_data[label]["wins"] += actual
                exp_data[label]["sum_pred"] += fav_prob
                break

        brier_sum += (fav_prob - actual) ** 2
        total_matches += 1

    comps = (
        session.query(_db.Competition)
        .order_by(
            _db.Competition.start_date,
            nullslast(_db.Competition.start_time),
            _db.Competition.ph_event_id,
        )
        .all()
    )

    for comp in comps:
        all_matches = session.query(_db.Match).filter_by(competition_uuid=comp.uuid).all()

        swiss_matches = [m for m in all_matches if not _is_elimination_round(m.round.name if m.round else "")]
        elim_matches = [m for m in all_matches if _is_elimination_round(m.round.name if m.round else "")]
        swiss_matches.sort(key=_round_sort_key)

        swiss_participants = set()
        for m in swiss_matches:
            swiss_participants.add(m.player_a_uuid)
            swiss_participants.add(m.player_b_uuid)

        knockout_participants = set()
        for m in elim_matches:
            knockout_participants.add(m.player_a_uuid)
            knockout_participants.add(m.player_b_uuid)

        # --- Swiss phase ---
        for m in swiss_matches:
            if m.winning_player_uuid is None:
                continue

            _record(m.player_a_uuid, m.player_b_uuid, m.winning_player_uuid)

            # Apply Delo update after recording
            winner_uuid = m.winning_player_uuid
            loser_uuid = m.player_b_uuid if m.player_a_uuid == winner_uuid else m.player_a_uuid
            ea = elo_expected(get_rating(winner_uuid), get_rating(loser_uuid))
            ratings[winner_uuid] = get_rating(winner_uuid) + K * (1 - ea)
            ratings[loser_uuid] = get_rating(loser_uuid) + K * (0 - (1 - ea))
            swiss_counts[winner_uuid] = swiss_counts.get(winner_uuid, 0) + 1
            swiss_counts[loser_uuid] = swiss_counts.get(loser_uuid, 0) + 1

        # --- Elimination phase ---
        eliminated_pool = swiss_participants - knockout_participants

        elim_by_round = {}
        for m in elim_matches:
            rname = m.round.name if m.round else ""
            elim_by_round.setdefault(rname, []).append(m)

        def _elim_sort_key(rname):
            match = re.match(r"^Top (\d+)$", rname)
            return -int(match.group(1)) if match else 0

        for rname in sorted(elim_by_round.keys(), key=_elim_sort_key):
            round_elo_gained = 0.0
            round_losers = set()

            for m in elim_by_round[rname]:
                if m.winning_player_uuid is None:
                    continue

                _record(m.player_a_uuid, m.player_b_uuid, m.winning_player_uuid)

                winner_uuid = m.winning_player_uuid
                loser_uuid = m.player_b_uuid if m.player_a_uuid == winner_uuid else m.player_a_uuid
                gain = K * (1 - elo_expected(get_rating(winner_uuid), get_rating(loser_uuid)))
                ratings[winner_uuid] = get_rating(winner_uuid) + gain
                round_elo_gained += gain
                round_losers.add(loser_uuid)

            eliminated_pool.update(round_losers)
            if round_elo_gained > 0 and eliminated_pool:
                loss_per = round_elo_gained / len(eliminated_pool)
                for uuid in eliminated_pool:
                    ratings[uuid] = get_rating(uuid) - loss_per

    brier_score = brier_sum / total_matches if total_matches else 0.0
    return total_matches, brier_score, bucket_data, exp_data


# ---------------------------------------------------------------------------
# Command: run-backtest
# ---------------------------------------------------------------------------


@cli.command("run-backtest")
def run_backtest():
    """Evaluate Delo win-probability predictions against historical match outcomes.

    Replays all matches chronologically, recording the predicted win probability
    before each Delo update is applied (genuine out-of-sample evaluation). Both
    Swiss and knockout decisive matches are included; draws are skipped.

    Results are stored in the database so the web interface can display them,
    and a calibration summary is printed to the terminal.

    Run update-ratings first, then run this command after importing new data.
    """
    engine = _db.init_db()
    Session = _db.make_session_factory(engine)

    with Session() as session:
        click.echo("Running backtest…")
        total, brier, bucket_data, exp_data = _compute_backtest(session)

        # --- Persist results ---
        # Clear old data
        session.query(_db.BacktestBucket).delete()
        session.query(_db.BacktestExperience).delete()
        session.query(_db.BacktestSummary).delete()

        now = datetime.now(timezone.utc)
        session.add(_db.BacktestSummary(id=1, total_matches=total, brier_score=brier, run_at=now))
        for bmin, d in bucket_data.items():
            if d["count"] > 0:
                session.add(_db.BacktestBucket(bucket_min=bmin, match_count=d["count"], actual_wins=d["wins"]))
        for label, d in exp_data.items():
            session.add(
                _db.BacktestExperience(
                    tier_label=label,
                    tier_min=d["tier_min"],
                    match_count=d["count"],
                    actual_wins=d["wins"],
                    sum_predicted=d["sum_pred"],
                )
            )
        session.commit()

        # --- Print summary ---
        click.echo(f"\nBacktested {total} decisive matches.")
        click.echo(f"Brier score: {brier:.4f}  (random-guess baseline = 0.2500, perfect = 0.0000)\n")

        click.echo("Calibration by predicted probability (favourite's perspective):")
        click.echo(f"  {'Bucket':<10} {'Predicted':>10} {'Actual':>10} {'Matches':>9}")
        click.echo(f"  {'-'*44}")
        for bmin in _BUCKET_MINS:
            d = bucket_data[bmin]
            if d["count"] == 0:
                continue
            bmax = round(bmin + 0.05, 2)
            predicted_mid = round(bmin + 0.025, 4)
            actual_rate = d["wins"] / d["count"]
            label = f"{int(bmin*100)}–{int(bmax*100)}%"
            click.echo(f"  {label:<10} {predicted_mid*100:>9.1f}% {actual_rate*100:>9.1f}%  {d['count']:>7}")

        click.echo("\nCalibration by experience (min Swiss matches of either player):")
        click.echo(f"  {'Tier':<10} {'Matches':>8} {'Avg Pred':>10} {'Actual':>8} {'Error':>8}")
        click.echo(f"  {'-'*48}")
        for label, tmin, _ in _EXP_TIERS:
            d = exp_data[label]
            if d["count"] == 0:
                continue
            avg_pred = d["sum_pred"] / d["count"]
            actual_rate = d["wins"] / d["count"]
            error = actual_rate - avg_pred
            sign = "+" if error >= 0 else ""
            click.echo(
                f"  {label:<10} {d['count']:>8} {avg_pred*100:>9.1f}% {actual_rate*100:>7.1f}%"
                f" {sign}{error*100:>6.1f}%"
            )


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


# ---------------------------------------------------------------------------
# Command: discover-set-championships
# ---------------------------------------------------------------------------


@cli.command("discover-set-championships")
@click.option("--name", "filter_name", default=None, help="Filter to a specific set (partial name match).")
def discover_set_championships(filter_name):
    """Show which UK set championship events are already imported and which are not.

    Reads registered set championship types from the database, fetches all UK
    events for each type from the Play Hub API, and compares against the local
    database. Useful for checking whether any events have been missed before
    running import-set-championship.

    Use --name to narrow results to a specific season (partial, case-insensitive):

    \b
      uv run main.py discover-set-championships --name "Whispers"
      uv run main.py discover-set-championships --name "Winterspell"

    Register new set championship types first with add-set-championship-type.
    """
    _scrape.reset_api_call_count()

    engine = _db.init_db()
    Session = _db.make_session_factory(engine)

    with Session() as session:
        query = session.query(_db.SetChampionshipType)
        if filter_name:
            query = query.filter(_db.SetChampionshipType.display_name.ilike(f"%{filter_name}%"))
        set_types = query.all()

        if not set_types:
            if filter_name:
                click.echo(f"No set championship types matching '{filter_name}' found in the database.")
            else:
                click.echo(
                    "No set championship types registered. " "Run 'uv run main.py add-set-championship-type' first."
                )
            return

        set_display = filter_name or "all registered sets"
        click.echo(f"Fetching UK set championships ({set_display}) from Play Hub…")

        def _progress(label, page, total):
            click.echo(f"  {label}: page {page}/{total}…", nl=False)
            click.echo("\r", nl=False)

        set_type_infos = [
            _scrape.SetTypeInfo(
                display_name=st.display_name,
                event_configuration_template=st.event_configuration_template,
            )
            for st in set_types
        ]
        try:
            events = _scrape.fetch_uk_set_championships(set_type_infos, progress_callback=_progress)
        except Exception as e:
            click.echo(f"Error fetching events: {e}", err=True)
            sys.exit(1)

        click.echo(f"Found {len(events)} UK set championship events on Play Hub.\n")

        known_ids = {row[0] for row in session.query(_db.Competition.ph_event_id).all() if row[0] is not None}

    already_imported = sorted([e for e in events if e.event_id in known_ids], key=lambda e: e.start_date)
    not_imported = sorted([e for e in events if e.event_id not in known_ids], key=lambda e: e.start_date)

    if already_imported:
        click.echo(f"Already in database ({len(already_imported)}):")
        for e in already_imported:
            click.echo(f"  {e.start_date}  {e.store_name:<35}  {e.name}")
        click.echo("")

    if not_imported:
        click.echo(f"Not yet imported ({len(not_imported)}):")
        for e in not_imported:
            click.echo(f"  {e.start_date}  {e.store_name:<35}  {e.name}  [ID: {e.event_id}]")
        click.echo("\nRun 'uv run main.py import-set-championship' to import these.")
    else:
        click.echo("All discovered events are already in the database.")


# ---------------------------------------------------------------------------
# Command: compare-ratings
# ---------------------------------------------------------------------------


@cli.command("compare-ratings")
@click.option(
    "--player",
    "player_names",
    multiple=True,
    required=True,
    help="Player name to include (repeatable, case-insensitive partial match).",
)
@click.option(
    "--output",
    "output_path",
    default="elo_comparison.png",
    show_default=True,
    help="Path for the output PNG file.",
)
def compare_ratings(player_names, output_path):
    """Generate a PNG chart of Delo rating history for multiple players.

    Each --player argument is matched case-insensitively (partial match).
    If a name matches multiple players, the one with most history is used.
    Up to 6 players can be compared.

    \b
      uv run main.py compare-ratings --player "Alice" --player "Bob"
      uv run main.py compare-ratings --player "Alice" --player "Bob" --output chart.png
    """
    engine = _db.init_db()
    Session = _db.make_session_factory(engine)

    with Session() as session:
        # Check that any history exists at all
        if not session.query(_db.PlayerRatingHistory).first():
            click.echo("No rating history found. Run 'uv run main.py update-ratings' first.")
            sys.exit(1)

        series = []
        for raw_name in player_names:
            # Find the player matching the name who has the most history entries
            row = (
                session.query(_db.Player, func.count(_db.PlayerRatingHistory.competition_uuid).label("cnt"))
                .join(_db.PlayerRatingHistory, _db.PlayerRatingHistory.player_uuid == _db.Player.uuid)
                .filter(_db.Player.name.ilike(f"%{raw_name}%"))
                .group_by(_db.Player.uuid)
                .order_by(func.count(_db.PlayerRatingHistory.competition_uuid).desc())
                .first()
            )
            if not row:
                player = session.query(_db.Player).filter(_db.Player.name.ilike(f"%{raw_name}%")).first()
                if not player:
                    click.echo(f"Player not found: {raw_name}", err=True)
                else:
                    click.echo(
                        f"No rating history for {player.name}. Run 'uv run main.py update-ratings' first.",
                        err=True,
                    )
                continue

            player_obj, _ = row
            pts = (
                session.query(_db.PlayerRatingHistory)
                .filter_by(player_uuid=player_obj.uuid)
                .order_by(_db.PlayerRatingHistory.date)
                .all()
            )
            dates = [_date.fromisoformat(pt.date) for pt in pts]
            ratings = [pt.rating for pt in pts]
            series.append({"name": player_obj.name, "dates": dates, "ratings": ratings})

        if not series:
            click.echo("No data to plot.")
            sys.exit(1)

    fig, ax = plt.subplots(figsize=(12, 6))
    fig.patch.set_facecolor("#f8fafc")
    ax.set_facecolor("#f8fafc")

    # Baseline at 1000
    all_dates = [d for s in series for d in s["dates"]]
    ax.axhline(1000, color="#cbd5e1", linewidth=1, linestyle="--", zorder=1)

    colors = ["#3b82f6", "#ef4444", "#22c55e", "#f59e0b", "#8b5cf6", "#ec4899"]
    for i, s in enumerate(series):
        color = colors[i % len(colors)]
        ax.plot(s["dates"], s["ratings"], marker="o", markersize=5, linewidth=2, color=color, label=s["name"], zorder=3)

    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m-%d"))
    ax.xaxis.set_major_locator(mdates.AutoDateLocator())
    fig.autofmt_xdate(rotation=35, ha="right")

    ax.set_ylabel("Delo Rating", fontsize=11)
    ax.set_title("Delo Rating History", fontsize=13, fontweight="bold", pad=12)
    ax.legend(framealpha=0.85, fontsize=10)
    ax.grid(axis="y", color="#e2e8f0", linewidth=0.8, zorder=0)
    ax.spines[["top", "right"]].set_visible(False)
    ax.spines[["left", "bottom"]].set_color("#cbd5e1")
    ax.tick_params(colors="#475569")

    plt.tight_layout()
    plt.savefig(output_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    click.echo(f"Chart saved to {output_path}")


# ---------------------------------------------------------------------------
# Command: participation-stats
# ---------------------------------------------------------------------------


@cli.command("participation-stats")
def participation_stats():
    """Show participation stats for each set championship type.

    For each type (e.g. Whispers, Winterspell) shows:
      - Number of events with match data
      - Total player entries across all events
      - Unique players who participated in at least one event

    \b
      uv run main.py participation-stats
    """
    engine = _db.init_db()
    Session = _db.make_session_factory(engine)

    with Session() as session:
        set_types = (
            session.query(_db.SetChampionshipType)
            .outerjoin(_db.Competition, _db.Competition.set_championship_type_uuid == _db.SetChampionshipType.uuid)
            .group_by(_db.SetChampionshipType.uuid)
            .order_by(func.min(_db.Competition.start_date))
            .all()
        )

        if not set_types:
            click.echo("No set championship types registered.")
            return

        for st in set_types:
            # Only competitions for this type that have at least one match recorded
            comp_uuids = [
                row[0]
                for row in session.query(_db.Competition.uuid)
                .filter(_db.Competition.set_championship_type_uuid == st.uuid)
                .filter(
                    session.query(_db.Match.uuid).filter(_db.Match.competition_uuid == _db.Competition.uuid).exists()
                )
                .all()
            ]

            if not comp_uuids:
                click.echo(f"{st.display_name}: no events imported")
                continue

            total_entries = (
                session.query(func.count(_db.CompetitionResult.player_uuid))
                .filter(_db.CompetitionResult.competition_uuid.in_(comp_uuids))
                .scalar()
            )
            unique_players = (
                session.query(func.count(_db.CompetitionResult.player_uuid.distinct()))
                .filter(_db.CompetitionResult.competition_uuid.in_(comp_uuids))
                .scalar()
            )
            num_events = len(comp_uuids)

            click.echo(f"{st.display_name}")
            click.echo(f"  Events:          {num_events}")
            click.echo(f"  Total entries:   {total_entries}")
            click.echo(f"  Unique players:  {unique_players}")
            click.echo("")


@cli.command("export-anonymized")
@click.option(
    "--pattern",
    "-p",
    multiple=True,
    help="Substring to match against player names (case-insensitive). Repeatable.",
)
@click.option(
    "--exclude",
    "-e",
    multiple=True,
    help="Exact player name to exclude from anonymization. Repeatable.",
)
@click.option(
    "--output",
    "-o",
    default="playhub_anonymized.db",
    show_default=True,
    help="Output path for the anonymized database copy.",
)
@click.option("--dry-run", is_flag=True, help="Preview which players would be anonymized without writing output.")
def export_anonymized(pattern, exclude, output, dry_run):
    """Export an anonymized copy of the database.

    Players whose names contain any --pattern substring (case-insensitive) are
    renamed to 'anon' in the output copy. Players whose names exactly match any
    --exclude value are left unchanged.

    The source database is never modified.

    \b
      uv run main.py export-anonymized -p App -e Apple -o anon.db
      uv run main.py export-anonymized -p App -p Obb -e "Apple Orchard" -e "Applebee" --dry-run
    """
    import shutil

    if not pattern:
        raise click.UsageError("Specify at least one --pattern / -p to match against player names.")

    engine = _db.init_db()
    Session = _db.make_session_factory(engine)
    exclude_set = set(exclude)

    with Session() as session:
        all_players = session.query(_db.Player).order_by(_db.Player.uuid).all()
        to_anonymize = [
            p
            for p in all_players
            if p.name not in exclude_set and any(pat.lower() in p.name.lower() for pat in pattern)
        ]

    if not to_anonymize:
        click.echo("No players matched the given patterns.")
        return

    click.echo(f"Players to anonymize ({len(to_anonymize)}):")
    for player in to_anonymize:
        click.echo(f"  {player.name!r}  →  'anon'")

    if dry_run:
        click.echo("\n(Dry run — no output written.)")
        return

    shutil.copy2(_db.DB_PATH, output)
    click.echo(f"\nCopied database to {output!r}")

    anon_engine = _db.make_engine(f"sqlite:///{output}")
    AnonSession = _db.make_session_factory(anon_engine)

    with AnonSession() as anon_session:
        anon_players = anon_session.query(_db.Player).order_by(_db.Player.uuid).all()
        matched = [
            p
            for p in anon_players
            if p.name not in exclude_set and any(pat.lower() in p.name.lower() for pat in pattern)
        ]
        matched_uuids = [p.uuid for p in matched]

        for player in matched:
            player.name = "anon"

        # Purge rows that would expose the anonymized players' activity
        (
            anon_session.query(_db.CompetitionResult)
            .filter(_db.CompetitionResult.player_uuid.in_(matched_uuids))
            .delete(synchronize_session=False)
        )
        (
            anon_session.query(_db.PlayerRatingHistory)
            .filter(_db.PlayerRatingHistory.player_uuid.in_(matched_uuids))
            .delete(synchronize_session=False)
        )
        (
            anon_session.query(_db.PlayerRating)
            .filter(_db.PlayerRating.player_uuid.in_(matched_uuids))
            .delete(synchronize_session=False)
        )

        anon_session.commit()

    click.echo(f"Anonymized {len(matched)} player(s) in {output!r}.")


# ---------------------------------------------------------------------------
# Command: upcoming-set-champs
# ---------------------------------------------------------------------------

_NOMINATIM_API = "https://nominatim.openstreetmap.org/search"
_OSRM_API = "http://router.project-osrm.org/route/v1/driving"
_NOMINATIM_HEADERS = {
    "User-Agent": "playhub-scraper/1.0 (github.com/5uperdan/playhub-scraper)",
}
_UK_POSTCODE_RE = re.compile(r"\b([A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2})\b", re.IGNORECASE)
_UK_POSTCODE_ONLY_RE = re.compile(r"^[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}$", re.IGNORECASE)
_UPCOMING_CSV_HEADERS = [
    "Date",
    "Venue",
    "Location",
    "Postcode",
    "Players signed up",
    "Players at last set champ",
    "Travel time",
    "Route link",
    "Event link",
    "Last set champ link",
]


@dataclass
class _UpcomingRow:
    date: str
    venue: str
    location: str
    postcode: str
    players_signed_up: str
    players_at_last: str
    travel_time: str
    route_link: str
    event_link: str
    last_event_link: str
    travel_seconds: int = field(default=None, repr=False, compare=False)


def _extract_uk_postcode(address: str) -> str:
    m = _UK_POSTCODE_RE.search(address or "")
    return m.group(1).upper() if m else ""


def _geocode_query(query: str, cache: dict):
    """Geocode a query string via Nominatim. Returns (lat, lon) or None."""
    if query in cache:
        return cache[query]
    import requests as _req

    time.sleep(1.1)  # Nominatim rate limit: max 1 req/sec
    try:
        # Use the postalcode parameter for postcode-like queries (more reliable for UK)
        if _UK_POSTCODE_ONLY_RE.match(query.strip()):
            params = {"postalcode": query.strip(), "country": "gb", "format": "json", "limit": 1}
        else:
            params = {"q": query, "format": "json", "limit": 1, "countrycodes": "gb"}
        r = _req.get(_NOMINATIM_API, params=params, headers=_NOMINATIM_HEADERS, timeout=10)
        results = r.json()
        if results:
            coord = (float(results[0]["lat"]), float(results[0]["lon"]))
            cache[query] = coord
            return coord
    except Exception:
        pass
    cache[query] = None
    return None


def _get_osrm_duration(origin, dest):
    """Return driving duration in seconds via OSRM, or None on failure."""
    import requests as _req

    lat1, lon1 = origin
    lat2, lon2 = dest
    try:
        r = _req.get(
            f"{_OSRM_API}/{lon1},{lat1};{lon2},{lat2}",
            params={"overview": "false"},
            timeout=10,
        )
        data = r.json()
        if data.get("code") == "Ok" and data.get("routes"):
            return int(data["routes"][0]["duration"])
    except Exception:
        pass
    return None


def _format_travel_time(seconds: int) -> str:
    h = seconds // 3600
    m = (seconds % 3600) // 60
    return f"{h}h {m}m" if h else f"{m}m"


def _parse_travel_seconds(s: str):
    """Parse '1h 23m' or '45m' back to seconds for sorting. Returns None if unparseable."""
    if not s or s == "N/A":
        return None
    hours = re.search(r"(\d+)h", s)
    mins = re.search(r"(\d+)m", s)
    total = (int(hours.group(1)) * 3600 if hours else 0) + (int(mins.group(1)) * 60 if mins else 0)
    return total or None


def _make_route_link(origin_postcode: str, dest: str) -> str:
    o = urllib.parse.quote(origin_postcode)
    d = urllib.parse.quote(dest or "")
    return f"https://www.google.com/maps/dir/?api=1&origin={o}&destination={d}"


def _load_travel_cache(csv_path: str) -> dict:
    """Read an existing upcoming CSV and return {venue: (travel_time, route_link)}."""
    cache: dict = {}
    try:
        with open(csv_path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row.get("Venue"):
                    cache[row["Venue"]] = (
                        row.get("Travel time", ""),
                        row.get("Route link", ""),
                    )
    except FileNotFoundError:
        pass
    return cache


def _write_upcoming_csv(rows: list, csv_path: str) -> None:
    """Write rows sorted by date then travel time, with blank rows between dates."""
    rows.sort(
        key=lambda r: (r.date, r.travel_seconds if r.travel_seconds is not None else 999_999)
    )
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(_UPCOMING_CSV_HEADERS)
        prev_date = None
        for row in rows:
            if prev_date is not None and row.date != prev_date:
                writer.writerow([])
            writer.writerow(
                [
                    row.date,
                    row.venue,
                    row.location,
                    row.postcode,
                    row.players_signed_up,
                    row.players_at_last,
                    row.travel_time,
                    row.route_link,
                    row.event_link,
                    row.last_event_link,
                ]
            )
            prev_date = row.date


def _build_upcoming_rows(season_name: str, origin_postcode: str, csv_path: str, session) -> list:
    today = _date.today().isoformat()

    set_types_db = (
        session.query(_db.SetChampionshipType)
        .filter(_db.SetChampionshipType.display_name.ilike(f"%{season_name}%"))
        .all()
    )
    if not set_types_db:
        raise click.ClickException(f"No set championship type found matching {season_name!r}.")

    type_uuids = [st.uuid for st in set_types_db]
    labels = ", ".join(st.display_name for st in set_types_db)

    upcoming_comps = (
        session.query(_db.Competition)
        .filter(
            _db.Competition.set_championship_type_uuid.in_(type_uuids),
            _db.Competition.start_date >= today,
        )
        .order_by(_db.Competition.start_date)
        .all()
    )

    click.echo(f"  Loaded {len(upcoming_comps)} upcoming events from local DB ({labels}).")
    if not upcoming_comps:
        click.echo("  (Run import-set-championship to refresh.)")

    travel_cache = _load_travel_cache(csv_path)
    if travel_cache:
        click.echo(f"  Loaded {len(travel_cache)} cached travel times from existing CSV.")

    geo_cache: dict = {}
    origin_coord = None
    if origin_postcode:
        click.echo(f"  Geocoding origin {origin_postcode!r}…")
        origin_coord = _geocode_query(origin_postcode, geo_cache)
        if origin_coord is None:
            click.echo(f"  Warning: could not geocode {origin_postcode!r}.", err=True)

    rows: list = []
    for comp in upcoming_comps:
        venue = comp.venue
        venue_name = venue.name if venue else comp.name
        address = (venue.address if venue else None) or ""
        venue_postcode = _extract_uk_postcode(address)

        # Players at last set champ at this venue (from DB)
        last_attendance = "N/A"
        last_event_ph_id = None
        if venue:
            last_comp = (
                session.query(_db.Competition.attended_player_count, _db.Competition.ph_event_id)
                .filter(
                    _db.Competition.venue_uuid == venue.ph_uuid,
                    _db.Competition.start_date < today,
                    _db.Competition.is_complete,
                )
                .order_by(_db.Competition.start_date.desc())
                .first()
            )
            if last_comp and last_comp[0]:
                last_attendance = str(last_comp[0])
                last_event_ph_id = last_comp[1]

        # Travel time: reuse from existing CSV if available, else call geocoder + OSRM
        travel_time = "N/A"
        route_link = ""
        travel_seconds = None

        dest_str = address if address else f"{venue_name}, United Kingdom"
        # Prefer postcode for geocoding — Nominatim handles UK postcodes reliably
        # but fails on PlayHub's full address format (e.g. "5 High St, Cafe, Town, England, AB12CD, GB")
        dest_for_geocode = venue_postcode if venue_postcode else dest_str

        cached = travel_cache.get(venue_name)
        if cached and cached[0] and cached[0] != "N/A":
            # Reuse cached travel time but regenerate route link from current address
            travel_time = cached[0]
            travel_seconds = _parse_travel_seconds(travel_time)
            if origin_postcode:
                route_link = _make_route_link(origin_postcode, dest_str)
        elif origin_coord:
            dest_coord = _geocode_query(dest_for_geocode, geo_cache)
            if dest_coord:
                secs = _get_osrm_duration(origin_coord, dest_coord)
                if secs is not None:
                    travel_seconds = secs
                    travel_time = _format_travel_time(secs)
            if origin_postcode:
                route_link = _make_route_link(origin_postcode, dest_str)

        signed_up = str(comp.attended_player_count) if comp.attended_player_count is not None else "N/A"
        ph_base = "https://tcg.ravensburgerplay.com/events"
        event_link = f"{ph_base}/{comp.ph_event_id}" if comp.ph_event_id else ""
        last_event_link = f"{ph_base}/{last_event_ph_id}" if last_event_ph_id else ""

        rows.append(
            _UpcomingRow(
                date=comp.start_date,
                venue=venue_name,
                location=address,
                postcode=venue_postcode,
                players_signed_up=signed_up,
                players_at_last=last_attendance,
                travel_time=travel_time,
                route_link=route_link,
                event_link=event_link,
                last_event_link=last_event_link,
                travel_seconds=travel_seconds,
            )
        )

    return rows


@cli.command("upcoming-set-champs")
@click.option("--name", "season_name", required=True, help="Season name filter (partial, case-insensitive).")
@click.option("--postcode", default=None, help="Origin postcode for travel time calculation.")
@click.option(
    "--postcodes-file",
    "postcodes_file",
    default=None,
    type=click.Path(exists=True),
    help="Text file with one origin postcode per line. Generates one CSV per postcode.",
)
def upcoming_set_champs(season_name, postcode, postcodes_file):
    """Generate a CSV of upcoming set championship events with travel times.

    Writes docs/upcoming_<POSTCODE>.csv for each postcode. If the file already
    exists, cached travel times are reused and only other data is refreshed.
    """
    if not postcode and not postcodes_file:
        raise click.UsageError("Provide --postcode or --postcodes-file.")

    postcodes: list = []
    if postcodes_file:
        with open(postcodes_file, encoding="utf-8") as fh:
            postcodes.extend(line.strip() for line in fh if line.strip())
    if postcode:
        postcodes.append(postcode.strip())

    if not postcodes:
        raise click.ClickException("No postcodes found.")

    docs_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs")

    engine = _db.init_db()
    Session = _db.make_session_factory(engine)

    with Session() as session:
        for pc in postcodes:
            filename = f"upcoming_{pc.replace(' ', '_').upper()}.csv"
            csv_path = os.path.join(docs_dir, filename)
            click.echo(f"\n[{pc}] → {filename}")
            rows = _build_upcoming_rows(
                season_name=season_name,
                origin_postcode=pc,
                csv_path=csv_path,
                session=session,
            )
            _write_upcoming_csv(rows, csv_path)
            click.echo(f"  Written {len(rows)} events.")


# ---------------------------------------------------------------------------
# Command: peek-decklists
# ---------------------------------------------------------------------------

@cli.command("peek-decklists")
@click.argument("event_url")
def peek_decklists(event_url):
    """Check whether decklists are available for an event and print the first one.

    EVENT_URL is a PlayHub event URL, e.g.
    https://tcg.ravensburgerplay.com/events/349881
    """
    event_id = _scrape.get_event_id_from_url(event_url)
    if event_id is None:
        raise click.BadParameter(f"Could not parse an event ID from: {event_url!r}")

    import requests as _req

    _DECK_BASE = "https://api.ravensburgerplay.com/api/v2"

    def _deck_get(path, params=None):
        r = _req.get(_DECK_BASE + path, headers=_scrape.HEADERS, params=params, timeout=15)
        r.raise_for_status()
        return r.json()

    # Fetch event metadata
    try:
        ev = _scrape._get_raw(f"/events/{event_id}/")
    except Exception as exc:
        raise click.ClickException(f"Could not fetch event {event_id}: {exc}")

    ev_name = ev.get("name", f"Event {event_id}")
    settings = ev.get("settings") or {}
    dl_status = settings.get("decklist_status", "UNKNOWN")
    on_spicerack = settings.get("decklists_on_spicerack", False)
    n = ev.get("starting_player_count", "?")

    click.echo(f"Event:    {ev_name} (id={event_id})")
    click.echo(f"Players:  {n}")
    click.echo(f"Decklist status:    {dl_status}")
    click.echo(f"Submitted via PlayHub: {on_spicerack}")
    click.echo("")

    if not on_spicerack:
        click.echo("No decklists were submitted through PlayHub for this event.")
        return

    # Single pass: find first deck_id and count how many registrations have one
    first_reg = None
    deck_count = 0
    total_regs = None
    page = 1
    while True:
        data = _scrape._get_raw(f"/events/{event_id}/registrations/", {"page_size": 100, "page": page})
        if total_regs is None:
            total_regs = data.get("count", "?")
        for reg in data.get("results", []):
            if reg.get("deck_id"):
                deck_count += 1
                if first_reg is None:
                    first_reg = reg
        if not data.get("next_page_number"):
            break
        page = data["next_page_number"]

    click.echo(f"Total registrations: {total_regs}")
    click.echo(f"Registrations with decklist: {deck_count}")
    click.echo("")

    if first_reg is None:
        click.echo("No deck_id present in any registration.")
        return

    # Fetch and print the first available decklist
    player_name = first_reg.get("best_identifier") or first_reg.get("special_user_identifier") or "Unknown"
    place = first_reg.get("final_place_in_standings")
    user_id = (first_reg.get("user") or {}).get("id")
    deck_id = first_reg["deck_id"]

    click.echo(f"Sample decklist — {player_name} (user_id={user_id}, place={place}):")
    click.echo(f"  Deck UUID: {deck_id}")

    try:
        deck = _deck_get(f"/deckbuilder/decks/{deck_id}/")
    except Exception as exc:
        click.echo(f"  Could not fetch deck: {exc}")
        return

    deck_name = deck.get("name", "Unnamed")
    card_count = deck.get("card_count", "?")
    click.echo(f"  Name:  {deck_name}")
    click.echo(f"  Cards: {card_count}")
    click.echo("")

    for section in deck.get("sections", []):
        click.echo(f"  {section.get('name', 'Section')}:")
        for card in section.get("cards", []):
            qty = card.get("quantity", 1)
            card_data = card.get("card") or {}
            card_name = card_data.get("name") or card.get("card_id", "?")
            click.echo(f"    {qty}x {card_name}")


if __name__ == "__main__":
    cli()
