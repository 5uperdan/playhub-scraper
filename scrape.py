"""
Play Hub API client and scraping helpers.

Data is fetched directly from the Play Hub REST API:
  https://api.cloudflare.ravensburgerplay.com/hydraproxy/api/v2/
No headless browser is required.
"""

import re
from typing import Optional

import requests
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

API_BASE = "https://api.cloudflare.ravensburgerplay.com/hydraproxy/api/v2"
HEADERS = {
    # Prevents some CDN/WAF layers from rejecting headless requests outright.
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json",
    # The API serves multiple games; this header selects Disney Lorcana data.
    # Requests without it return nothing or data for the wrong game.
    "x-game-slug": "disney-lorcana",
    # The API enforces these server-side (not just as a CORS check) to ensure
    # requests appear to originate from the official Play Hub frontend.
    # Omitting them results in 403 responses.
    "Origin": "https://tcg.ravensburgerplay.com",
    "Referer": "https://tcg.ravensburgerplay.com/",
}


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

_api_call_count = 0


def get_api_call_count() -> int:
    return _api_call_count


def reset_api_call_count() -> None:
    global _api_call_count
    _api_call_count = 0


def _is_transient_error(exc: BaseException) -> bool:
    """Return True for HTTP errors that are worth retrying (5xx, connection issues)."""
    if isinstance(exc, requests.exceptions.ConnectionError):
        return True
    if isinstance(exc, requests.exceptions.Timeout):
        return True
    if isinstance(exc, requests.exceptions.HTTPError):
        response = getattr(exc, "response", None)
        if response is not None and response.status_code >= 500:
            return True
    return False


@retry(
    retry=retry_if_exception(_is_transient_error),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    stop=stop_after_attempt(5),
    reraise=True,
)
def _get(path: str, params=None) -> dict:
    global _api_call_count
    _api_call_count += 1
    url = API_BASE + path
    r = requests.get(url, headers=HEADERS, params=params, timeout=20)
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------------------
# Event data
# ---------------------------------------------------------------------------


def fetch_event(event_id: int) -> dict:
    return _get(f"/events/{event_id}/")


def get_event_id_from_url(url: str) -> Optional[int]:
    m = re.search(r"/events/(\d+)", url)
    return int(m.group(1)) if m else None


_UK_COUNTRY_CODES = {"GB", "UK"}


def fetch_events_for_template(template_id: str, progress_callback=None) -> list:
    """
    Paginate all events for a single event_configuration_template_id and
    return those belonging to UK stores.

    Each returned dict contains:
      {"event_id", "name", "store_name", "store_country", "start_date",
       "event_configuration_template"}

    progress_callback, if provided, is called with (page, total_pages).
    """
    results = []
    page = 1
    total_pages = None

    while True:
        data = _get(
            "/events/",
            {
                "event_configuration_template_id": template_id,
                "page": page,
                "page_size": 100,
            },
        )

        count = data.get("count", 0)
        if total_pages is None:
            page_size = data.get("page_size", 100) or 100
            total_pages = max(1, -(-count // page_size))  # ceiling division

        if progress_callback:
            progress_callback(page, total_pages)

        for event in data.get("results", []):
            store = event.get("store") or {}
            country = store.get("country") or ""
            if country not in _UK_COUNTRY_CODES:
                continue
            start = (event.get("start_datetime") or "")[:10]
            results.append(
                {
                    "event_id": event["id"],
                    "name": event.get("name") or f"Event {event['id']}",
                    "store_name": store.get("name") or "Unknown",
                    "store_country": country,
                    "start_date": start,
                    "event_configuration_template": template_id,
                }
            )

        if data.get("next_page_number") is None:
            break
        page = data["next_page_number"]

    return results


def fetch_uk_set_championships(set_types: list, progress_callback=None) -> list:
    """
    Return all UK events for the given set championship types.

    set_types is a list of dicts with keys {"display_name", "event_configuration_template"}.
    Iterates through each type, fetching events server-side filtered by
    event_configuration_template_id, then filters client-side for UK stores.

    Returns a flat list of event dicts, each including "set_championship_type_template"
    so the caller can match back to the original type.

    progress_callback, if provided, is called as (label, page, total_pages).
    """
    results = []
    for entry in set_types:
        tmpl = entry["event_configuration_template"]
        label = entry["display_name"]
        cb = (lambda p, t, lbl=label: progress_callback(lbl, p, t)) if progress_callback else None
        for ev in fetch_events_for_template(tmpl, cb):
            ev["set_championship_type_template"] = tmpl
            results.append(ev)
    return results


def get_venue_from_event(event: dict) -> dict:
    return {
        "ph_uuid": event.get("game_store_id") or "",
        "name": (event.get("store") or {}).get("name") or "Unknown",
    }


# ---------------------------------------------------------------------------
# Players / registrations
# ---------------------------------------------------------------------------


def fetch_all_registrations(event_id: int) -> list:
    results = []
    page = 1
    while True:
        data = _get(f"/events/{event_id}/registrations/", {"page": page, "page_size": 100})
        results.extend(data.get("results", []))
        if data.get("next_page_number") is None:
            break
        page = data["next_page_number"]
    return results


# ---------------------------------------------------------------------------
# Rounds and matches
# ---------------------------------------------------------------------------


def get_rounds_from_event(event: dict) -> list:
    """
    Return ordered round descriptors:
      {"round_id", "round_name", "phase_type", "round_number"}

    Swiss rounds are labelled "Round N".
    Elimination rounds are labelled "Top N" based on how many players
    entered the phase (rank_required_to_enter_phase).
    """
    rounds = []
    for phase in event.get("tournament_phases", []):
        phase_type = phase.get("round_type", "")
        elim_entry = phase.get("rank_required_to_enter_phase")
        phase_rounds = sorted(phase.get("rounds", []), key=lambda r: r.get("round_number", 0))

        if phase_type == "SWISS":
            for rnd in phase_rounds:
                rounds.append(
                    {
                        "round_id": rnd["id"],
                        "round_name": f"Round {rnd['round_number']}",
                        "phase_type": phase_type,
                        "round_number": rnd["round_number"],
                    }
                )
        else:
            players_remaining = elim_entry or (2 ** len(phase_rounds))
            for rnd in phase_rounds:
                label = f"Top {players_remaining}" if players_remaining and players_remaining > 1 else "Final"
                rounds.append(
                    {
                        "round_id": rnd["id"],
                        "round_name": label,
                        "phase_type": phase_type,
                        "round_number": rnd["round_number"],
                    }
                )
                if players_remaining:
                    players_remaining //= 2

    return rounds


def fetch_matches_for_round(round_id: int) -> list:
    """
    Return match records for a round. Each dict contains:
      player_a / player_b: {"ph_user_id", "name"}
      player_a_score / player_b_score: int
      winner_ph_user_id: int or None (None = draw)
      is_bye: bool
    """
    data = _get(f"/tournament-rounds/{round_id}/matches/")
    matches_raw = data.get("matches", [])

    if not matches_raw:
        try:
            paged = _get(f"/tournament-rounds/{round_id}/matches/paginated/")
            matches_raw = paged.get("results", [])
        except requests.HTTPError:
            pass

    results = []
    for m in matches_raw:
        rels = sorted(m.get("player_match_relationships", []), key=lambda r: r.get("player_order", 0))
        if len(rels) < 2:
            continue  # bye — skip as a match record

        pa, pb = rels[0], rels[1]

        def _uid(rel):
            return (rel.get("player") or {}).get("id")

        def _name(rel):
            return (rel.get("user_event_status") or {}).get("best_identifier") or (rel.get("player") or {}).get(
                "best_identifier", "Unknown"
            )

        pa_uid, pb_uid = _uid(pa), _uid(pb)
        is_draw = m.get("match_is_intentional_draw") or m.get("match_is_unintentional_draw") or False
        is_bye = m.get("match_is_bye") or False
        winner_raw = m.get("winning_player")
        wins = m.get("games_won_by_winner") or 0
        losses = m.get("games_won_by_loser") or 0

        if winner_raw and winner_raw == pa_uid:
            pa_score, pb_score = wins, losses
        elif winner_raw and winner_raw == pb_uid:
            pa_score, pb_score = losses, wins
        else:
            pa_score, pb_score = wins, losses

        results.append(
            {
                "player_a": {"ph_user_id": pa_uid, "name": _name(pa)},
                "player_b": {"ph_user_id": pb_uid, "name": _name(pb)},
                "player_a_score": pa_score,
                "player_b_score": pb_score,
                "winner_ph_user_id": None if (is_draw or is_bye or not winner_raw) else winner_raw,
                "is_bye": is_bye,
            }
        )

    return results


# ---------------------------------------------------------------------------
# Rounds and matches
