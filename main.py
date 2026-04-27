"""
playhub-scraper CLI

Usage:
  uv run main.py add-set-championship-type --url <event-url>
  uv run main.py import-set-championship
  uv run main.py player-info <player-name>
  uv run main.py leaderboard [--name <filter>] [--top <n>]
"""

import re
import sys
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


def _get_or_create_venue(session, ph_uuid, name):
    venue = session.query(_db.Venue).filter_by(ph_uuid=ph_uuid).first()
    if venue is None:
        venue = _db.Venue(ph_uuid=ph_uuid, name=name)
        session.add(venue)
        session.flush()
    else:
        # Update name to the latest value seen, in case the store has been renamed
        venue.name = name
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
    venue = _get_or_create_venue(session, venue_info.ph_uuid, venue_info.name)

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
    many Swiss matches a player has played — a higher count means a more
    reliable rating. The KO count shows knockout matches played.

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


if __name__ == "__main__":
    cli()
