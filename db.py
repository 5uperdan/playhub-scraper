"""SQLAlchemy models for the playhub-scraper database."""

import uuid as _uuid
from datetime import datetime

from sqlalchemy import (
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    create_engine,
)
from sqlalchemy.orm import DeclarativeBase, relationship, sessionmaker

DB_PATH = "playhub.db"
DATABASE_URL = f"sqlite:///{DB_PATH}"


def make_engine(url: str = DATABASE_URL):
    return create_engine(url, echo=False)


def make_session_factory(engine):
    return sessionmaker(bind=engine)


def new_uuid() -> str:
    return str(_uuid.uuid4())


class Base(DeclarativeBase):
    pass


class Source(Base):
    """A downloaded Google Sheet XLSX file used as a data source."""

    __tablename__ = "sources"

    uuid = Column(String, primary_key=True, default=new_uuid)
    file_name = Column(String, nullable=False)
    processed_on = Column(DateTime, nullable=True)

    venues = relationship("Venue", back_populates="first_source")
    players = relationship("Player", back_populates="first_source")


class Venue(Base):
    """A store / tournament venue identified by its Play Hub store UUID."""

    __tablename__ = "venues"

    ph_uuid = Column(String, primary_key=True)
    name = Column(String, nullable=False)
    first_source_uuid = Column(String, ForeignKey("sources.uuid"), nullable=False)

    first_source = relationship("Source", back_populates="venues")
    competitions = relationship("Competition", back_populates="venue")


class Player(Base):
    """A player, identified by our own UUID.

    Note: player names are not guaranteed to be unique and can change over time.
    The ph_user_id (Play Hub internal user ID) is stored alongside and is used
    for deduplication when re-processing the same or overlapping sources.
    Re-running a source after a player has changed their name will update the
    stored name to the latest value seen for that ph_user_id.
    """

    __tablename__ = "players"

    uuid = Column(String, primary_key=True, default=new_uuid)
    ph_user_id = Column(Integer, nullable=True, unique=True)
    name = Column(String, nullable=False)
    first_source_uuid = Column(String, ForeignKey("sources.uuid"), nullable=False)

    first_source = relationship("Source", back_populates="players")
    matches_as_a = relationship("Match", foreign_keys="Match.player_a_uuid", back_populates="player_a")
    matches_as_b = relationship("Match", foreign_keys="Match.player_b_uuid", back_populates="player_b")
    match_wins = relationship("Match", foreign_keys="Match.winning_player_uuid", back_populates="winning_player")
    results = relationship("CompetitionResult", back_populates="player")
    rating = relationship("PlayerRating", back_populates="player", uselist=False)


class Round(Base):
    """A named tournament round (e.g. 'Round 1', 'Top 8')."""

    __tablename__ = "rounds"

    uuid = Column(String, primary_key=True, default=new_uuid)
    name = Column(String, nullable=False, unique=True)

    matches = relationship("Match", back_populates="round")


class Competition(Base):
    """A single tournament event."""

    __tablename__ = "competitions"

    uuid = Column(String, primary_key=True, default=new_uuid)
    ph_event_id = Column(Integer, nullable=True, unique=True)
    name = Column(String, nullable=False)
    venue_uuid = Column(String, ForeignKey("venues.ph_uuid"), nullable=False)
    start_date = Column(String, nullable=False)
    attended_player_count = Column(Integer, nullable=True)

    venue = relationship("Venue", back_populates="competitions")
    matches = relationship("Match", back_populates="competition")
    results = relationship("CompetitionResult", back_populates="competition")


class Match(Base):
    """A single match between player_a and player_b within a competition round."""

    __tablename__ = "matches"

    uuid = Column(String, primary_key=True, default=new_uuid)
    player_a_uuid = Column(String, ForeignKey("players.uuid"), nullable=False)
    player_b_uuid = Column(String, ForeignKey("players.uuid"), nullable=False)
    player_a_score = Column(Integer, nullable=False)
    player_b_score = Column(Integer, nullable=False)
    # null means a draw/bye
    winning_player_uuid = Column(String, ForeignKey("players.uuid"), nullable=True)
    competition_uuid = Column(String, ForeignKey("competitions.uuid"), nullable=False)
    round_uuid = Column(String, ForeignKey("rounds.uuid"), nullable=False)

    player_a = relationship("Player", foreign_keys=[player_a_uuid], back_populates="matches_as_a")
    player_b = relationship("Player", foreign_keys=[player_b_uuid], back_populates="matches_as_b")
    winning_player = relationship("Player", foreign_keys=[winning_player_uuid], back_populates="match_wins")
    competition = relationship("Competition", back_populates="matches")
    round = relationship("Round", back_populates="matches")


class CompetitionResult(Base):
    """Final standings for a player in a competition."""

    __tablename__ = "competition_results"

    competition_uuid = Column(String, ForeignKey("competitions.uuid"), primary_key=True)
    player_uuid = Column(String, ForeignKey("players.uuid"), primary_key=True)
    position = Column(Integer, nullable=True)

    competition = relationship("Competition", back_populates="results")
    player = relationship("Player", back_populates="results")

    __table_args__ = (UniqueConstraint("competition_uuid", "player_uuid"),)


class PlayerRating(Base):
    """Cached Elo rating for a player, recomputed by the update-ratings command."""

    __tablename__ = "player_ratings"

    player_uuid = Column(String, ForeignKey("players.uuid"), primary_key=True)
    rating = Column(Float, nullable=False, default=1000.0)
    match_count = Column(Integer, nullable=False, default=0)
    last_recalculated_at = Column(DateTime, nullable=True)

    player = relationship("Player", back_populates="rating")


class BacktestSummary(Base):
    """Headline stats from the most recent backtest run (always a single row)."""

    __tablename__ = "backtest_summary"

    id = Column(Integer, primary_key=True)  # always 1
    total_matches = Column(Integer, nullable=False)
    brier_score = Column(Float, nullable=False)
    run_at = Column(DateTime, nullable=True)


class BacktestBucket(Base):
    """Calibration data for one 5%-wide probability bucket."""

    __tablename__ = "backtest_buckets"

    # Lower bound of bucket, e.g. 0.50 covers predicted probabilities [0.50, 0.55)
    bucket_min = Column(Float, primary_key=True)
    match_count = Column(Integer, nullable=False)
    actual_wins = Column(Integer, nullable=False)


class BacktestExperience(Base):
    """Calibration data grouped by experience (min Swiss matches of either player)."""

    __tablename__ = "backtest_experience"

    tier_label = Column(String, primary_key=True)  # e.g. "0", "1-4", "5-9"
    tier_min = Column(Integer, nullable=False)  # numeric lower bound for ordering
    match_count = Column(Integer, nullable=False)
    actual_wins = Column(Integer, nullable=False)
    sum_predicted = Column(Float, nullable=False)  # sum of predicted probs for avg


def init_db(engine=None):
    """Create all tables if they don't exist."""
    if engine is None:
        engine = make_engine()
    Base.metadata.create_all(engine)
    return engine
