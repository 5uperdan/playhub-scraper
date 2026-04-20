"""
Play Hub API client and scraping helpers.

Data is fetched directly from the Play Hub REST API:
  https://api.cloudflare.ravensburgerplay.com/hydraproxy/api/v2/
No headless browser is required.
"""

import re
from typing import Optional, Type, TypeVar

import requests
from pydantic import BaseModel, ConfigDict
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

# ---------------------------------------------------------------------------
# Pydantic models — raw API response shapes
# ---------------------------------------------------------------------------

T = TypeVar("T", bound=BaseModel)

_IGNORE_EXTRA = ConfigDict(extra="ignore")


class ApiStore(BaseModel):
    model_config = _IGNORE_EXTRA
    name: Optional[str] = None
    country: Optional[str] = None


class ApiRound(BaseModel):
    model_config = _IGNORE_EXTRA
    id: int
    round_number: int


class ApiPhase(BaseModel):
    model_config = _IGNORE_EXTRA
    round_type: str = ""
    rank_required_to_enter_phase: Optional[int] = None
    rounds: list[ApiRound] = []


class ApiEvent(BaseModel):
    model_config = _IGNORE_EXTRA
    name: Optional[str] = None
    start_datetime: Optional[str] = None
    starting_player_count: Optional[int] = None
    game_store_id: Optional[str] = None
    store: Optional[ApiStore] = None
    event_configuration_template: Optional[str] = None
    tournament_phases: list[ApiPhase] = []


class ApiEventListItem(BaseModel):
    model_config = _IGNORE_EXTRA
    id: int
    name: Optional[str] = None
    store: Optional[ApiStore] = None


class ApiEventListPage(BaseModel):
    model_config = _IGNORE_EXTRA
    count: int = 0
    page_size: Optional[int] = None
    next_page_number: Optional[int] = None
    results: list[ApiEventListItem] = []


class ApiUser(BaseModel):
    model_config = _IGNORE_EXTRA
    id: Optional[int] = None
    best_identifier: Optional[str] = None


class ApiRegistration(BaseModel):
    model_config = _IGNORE_EXTRA
    best_identifier: Optional[str] = None
    user: Optional[ApiUser] = None
    final_place_in_standings: Optional[int] = None


class ApiRegistrationPage(BaseModel):
    model_config = _IGNORE_EXTRA
    next_page_number: Optional[int] = None
    results: list[ApiRegistration] = []


class ApiMatchPlayer(BaseModel):
    model_config = _IGNORE_EXTRA
    id: Optional[int] = None
    best_identifier: Optional[str] = None


class ApiUserEventStatus(BaseModel):
    model_config = _IGNORE_EXTRA
    best_identifier: Optional[str] = None


class ApiPlayerMatchRelationship(BaseModel):
    model_config = _IGNORE_EXTRA
    player_order: Optional[int] = None
    player: Optional[ApiMatchPlayer] = None
    user_event_status: Optional[ApiUserEventStatus] = None


class ApiMatchRecord(BaseModel):
    model_config = _IGNORE_EXTRA
    player_match_relationships: list[ApiPlayerMatchRelationship] = []
    match_is_intentional_draw: bool = False
    match_is_unintentional_draw: bool = False
    match_is_bye: bool = False
    winning_player: Optional[int] = None
    games_won_by_winner: Optional[int] = None
    games_won_by_loser: Optional[int] = None
    created_at: Optional[str] = None


class ApiMatchResponse(BaseModel):
    model_config = _IGNORE_EXTRA
    matches: list[ApiMatchRecord] = []


class ApiMatchPaginatedResponse(BaseModel):
    model_config = _IGNORE_EXTRA
    results: list[ApiMatchRecord] = []


# ---------------------------------------------------------------------------
# Pydantic models — domain objects returned to callers
# ---------------------------------------------------------------------------


class VenueInfo(BaseModel):
    ph_uuid: str
    name: str


class RoundInfo(BaseModel):
    round_id: int
    round_name: str


class PlayerInfo(BaseModel):
    ph_user_id: Optional[int]
    name: str


class MatchInfo(BaseModel):
    player_a: PlayerInfo
    player_b: PlayerInfo
    player_a_score: int
    player_b_score: int
    winner_ph_user_id: Optional[int]
    created_at: Optional[str]


class RegistrationInfo(BaseModel):
    ph_user_id: Optional[int]
    name: str
    final_place: Optional[int]


class UkEventInfo(BaseModel):
    event_id: int
    name: str
    set_championship_type_template: Optional[str] = None


class SetTypeInfo(BaseModel):
    display_name: str
    event_configuration_template: str


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
def _get_raw(path: str, params: Optional[dict] = None) -> dict:
    global _api_call_count
    _api_call_count += 1
    url = API_BASE + path
    r = requests.get(url, headers=HEADERS, params=params, timeout=20)
    r.raise_for_status()
    return r.json()


def _get(path: str, params: Optional[dict] = None, response_model: Optional[Type[T]] = None):
    """Fetch a JSON resource and optionally validate it into a Pydantic model.

    When response_model is provided the return type is an instance of that model.
    Without it the raw dict is returned (used internally where no model exists yet).
    """
    data = _get_raw(path, params)
    if response_model is not None:
        return response_model.model_validate(data)
    return data


# ---------------------------------------------------------------------------
# Event data
# ---------------------------------------------------------------------------


def fetch_event(event_id: int) -> ApiEvent:
    return _get(f"/events/{event_id}/", response_model=ApiEvent)


def get_event_id_from_url(url: str) -> Optional[int]:
    m = re.search(r"/events/(\d+)", url)
    return int(m.group(1)) if m else None


_UK_COUNTRY_CODES = {"GB", "UK"}


def fetch_events_for_template(template_id: str, progress_callback=None) -> list[UkEventInfo]:
    """
    Paginate all events for a single event_configuration_template_id and
    return those belonging to UK stores.
    """
    results: list[UkEventInfo] = []
    page = 1
    total_pages = None

    while True:
        page_data = _get(
            "/events/",
            {
                "event_configuration_template_id": template_id,
                "page": page,
                "page_size": 100,
            },
            response_model=ApiEventListPage,
        )

        if total_pages is None:
            page_size = page_data.page_size or 100
            total_pages = max(1, -(-page_data.count // page_size))  # ceiling division

        if progress_callback:
            progress_callback(page, total_pages)

        for event in page_data.results:
            country = (event.store.country or "") if event.store else ""
            if country not in _UK_COUNTRY_CODES:
                continue
            results.append(
                UkEventInfo(
                    event_id=event.id,
                    name=event.name or f"Event {event.id}",
                )
            )

        if page_data.next_page_number is None:
            break
        page = page_data.next_page_number

    return results


def fetch_uk_set_championships(set_types: list[SetTypeInfo], progress_callback=None) -> list[UkEventInfo]:
    """
    Return all UK events for the given set championship types.

    progress_callback, if provided, is called as (label, page, total_pages).
    """
    results: list[UkEventInfo] = []
    for entry in set_types:
        tmpl = entry.event_configuration_template
        label = entry.display_name
        cb = (lambda p, t, lbl=label: progress_callback(lbl, p, t)) if progress_callback else None
        for ev in fetch_events_for_template(tmpl, cb):
            results.append(ev.model_copy(update={"set_championship_type_template": tmpl}))
    return results


def get_venue_from_event(event: ApiEvent) -> VenueInfo:
    return VenueInfo(
        ph_uuid=event.game_store_id or "",
        name=(event.store.name or "Unknown") if event.store else "Unknown",
    )


# ---------------------------------------------------------------------------
# Players / registrations
# ---------------------------------------------------------------------------


def fetch_all_registrations(event_id: int) -> list[RegistrationInfo]:
    results: list[RegistrationInfo] = []
    page = 1
    while True:
        page_data = _get(
            f"/events/{event_id}/registrations/",
            {"page": page, "page_size": 100},
            response_model=ApiRegistrationPage,
        )
        for reg in page_data.results:
            ph_uid = reg.user.id if reg.user else None
            name = reg.best_identifier or (reg.user.best_identifier if reg.user else None) or "Unknown"
            results.append(RegistrationInfo(ph_user_id=ph_uid, name=name, final_place=reg.final_place_in_standings))
        if page_data.next_page_number is None:
            break
        page = page_data.next_page_number
    return results


# ---------------------------------------------------------------------------
# Rounds and matches
# ---------------------------------------------------------------------------


def get_rounds_from_event(event: ApiEvent) -> list[RoundInfo]:
    """
    Return ordered round descriptors.

    Swiss rounds are labelled "Round N".
    Elimination rounds are labelled "Top N" based on how many players
    entered the phase (rank_required_to_enter_phase).
    """
    rounds: list[RoundInfo] = []
    for phase in event.tournament_phases:
        phase_type = phase.round_type
        elim_entry = phase.rank_required_to_enter_phase
        phase_rounds = sorted(phase.rounds, key=lambda r: r.round_number)

        if phase_type == "SWISS":
            for rnd in phase_rounds:
                rounds.append(
                    RoundInfo(
                        round_id=rnd.id,
                        round_name=f"Round {rnd.round_number}",
                    )
                )
        else:
            players_remaining = elim_entry or (2 ** len(phase_rounds))
            for rnd in phase_rounds:
                label = f"Top {players_remaining}" if players_remaining and players_remaining > 1 else "Final"
                rounds.append(
                    RoundInfo(
                        round_id=rnd.id,
                        round_name=label,
                    )
                )
                if players_remaining:
                    players_remaining //= 2

    return rounds


def fetch_matches_for_round(round_id: int) -> list[MatchInfo]:
    """Return parsed match records for a round, skipping byes."""
    response = _get(f"/tournament-rounds/{round_id}/matches/", response_model=ApiMatchResponse)
    matches_raw = response.matches

    if not matches_raw:
        try:
            paged = _get(
                f"/tournament-rounds/{round_id}/matches/paginated/",
                response_model=ApiMatchPaginatedResponse,
            )
            matches_raw = paged.results
        except requests.HTTPError as exc:
            if exc.response is None or exc.response.status_code != 404:
                raise

    results: list[MatchInfo] = []
    for m in matches_raw:
        rels = sorted(m.player_match_relationships, key=lambda r: r.player_order or 0)
        if len(rels) < 2:
            continue  # bye — skip as a match record

        pa_rel, pb_rel = rels[0], rels[1]

        def _uid(rel: ApiPlayerMatchRelationship) -> Optional[int]:
            return rel.player.id if rel.player else None

        def _name(rel: ApiPlayerMatchRelationship) -> str:
            if rel.user_event_status and rel.user_event_status.best_identifier:
                return rel.user_event_status.best_identifier
            if rel.player and rel.player.best_identifier:
                return rel.player.best_identifier
            return "Unknown"

        pa_uid, pb_uid = _uid(pa_rel), _uid(pb_rel)
        is_draw = m.match_is_intentional_draw or m.match_is_unintentional_draw
        is_bye = m.match_is_bye
        winner_raw = m.winning_player
        wins = m.games_won_by_winner or 0
        losses = m.games_won_by_loser or 0

        if winner_raw and winner_raw == pa_uid:
            pa_score, pb_score = wins, losses
        elif winner_raw and winner_raw == pb_uid:
            pa_score, pb_score = losses, wins
        else:
            pa_score, pb_score = wins, losses

        results.append(
            MatchInfo(
                player_a=PlayerInfo(ph_user_id=pa_uid, name=_name(pa_rel)),
                player_b=PlayerInfo(ph_user_id=pb_uid, name=_name(pb_rel)),
                player_a_score=pa_score,
                player_b_score=pb_score,
                winner_ph_user_id=None if (is_draw or is_bye or not winner_raw) else winner_raw,
                created_at=m.created_at,
            )
        )

    return results
