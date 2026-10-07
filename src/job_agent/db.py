from datetime import datetime
from pathlib import Path
from typing import Optional
from sqlmodel import SQLModel, Field, create_engine, Session
import hashlib


def job_id(company: str, title: str, location: str) -> str:
    key = f"{company.lower().strip()}|{title.lower().strip()}|{location.lower().strip()}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]


class User(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    slug: str = Field(unique=True, index=True)
    profile_path: str


class Job(SQLModel, table=True):
    id: str = Field(primary_key=True)
    source: str
    company: str
    title: str
    location: str
    url: str
    description_raw: str = Field(default="")
    first_seen: datetime = Field(default_factory=datetime.utcnow)
    last_seen: datetime = Field(default_factory=datetime.utcnow)
    status: str = Field(default="active")
    # Last time prune-closed fetched this posting and got a definitive
    # open/closed answer. None = never checked (or only ever failed to fetch).
    # Used to avoid re-fetching every active LinkedIn posting on every run.
    last_checked: Optional[datetime] = Field(default=None)


class Score(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    job_id: str = Field(foreign_key="job.id", index=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    tier: str
    skill_fit: int
    preference_alignment: int
    recommended_resume_version: str
    gap_assessment: str
    positioning_notes: str
    rationale: str
    raw_response: str
    scored_at: datetime = Field(default_factory=datetime.utcnow)


class Application(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    job_id: str = Field(foreign_key="job.id", index=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    materials_path: str
    outcome: Optional[str] = None
    outcome_at: Optional[datetime] = None
    # Note: the SQLite DB still has an `applied_at` column from earlier schema;
    # sqlmodel ignores extra DB columns. UserJobMeta.app_status / app_status_at
    # is the source of truth for application state.


class UserJobMeta(SQLModel, table=True):
    """Per-user metadata for a job: tracking status and prescreen results."""
    id: Optional[int] = Field(default=None, primary_key=True)
    job_id: str = Field(foreign_key="job.id", index=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    # app_status: applied | interviewing | offer | rejected | None
    app_status: Optional[str] = Field(default=None)
    app_status_at: Optional[datetime] = Field(default=None)
    # prescreen_status: approved | rejected | manual_override | None
    prescreen_status: Optional[str] = Field(default=None)
    prescreen_reason: Optional[str] = Field(default=None)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


_engine = None


def get_engine(db_path: str = "data/jobs.sqlite") -> object:
    global _engine
    if _engine is None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        _engine = create_engine(f"sqlite:///{db_path}", echo=False)
    return _engine


def _add_missing_columns(engine) -> None:
    """Lightweight additive migration for existing SQLite DBs.

    `SQLModel.metadata.create_all` creates missing tables but never ALTERs an
    existing one, so columns added to a model after a DB already exists must be
    backfilled here. Only handles nullable column adds (idempotent, no data loss).
    """
    from sqlalchemy import text

    expected = {"last_checked": "DATETIME"}
    with engine.begin() as conn:
        existing = {row[1] for row in conn.execute(text("PRAGMA table_info(job)"))}
        for col, decl in expected.items():
            if col not in existing:
                conn.execute(text(f"ALTER TABLE job ADD COLUMN {col} {decl}"))


def init_db(db_path: str = "data/jobs.sqlite") -> object:
    engine = get_engine(db_path)
    SQLModel.metadata.create_all(engine)
    _add_missing_columns(engine)
    return engine


def get_session(db_path: str = "data/jobs.sqlite") -> Session:
    engine = get_engine(db_path)
    return Session(engine, expire_on_commit=False)


def get_or_create_meta(session: Session, job_id: str, user_id: int) -> "UserJobMeta":
    """Fetch or create a UserJobMeta row for the given job+user pair."""
    from sqlmodel import select
    meta = session.exec(
        select(UserJobMeta).where(
            UserJobMeta.job_id == job_id,
            UserJobMeta.user_id == user_id,
        )
    ).first()
    if not meta:
        meta = UserJobMeta(job_id=job_id, user_id=user_id)
        session.add(meta)
    return meta


def upsert_job(session: Session, job: Job) -> bool:
    """Insert or update a job. Returns True if new, False if updated."""
    existing = session.get(Job, job.id)
    if existing:
        existing.last_seen = datetime.utcnow()
        # "out_of_scope" is a deliberate user-side hide (e.g. the 2026-10
        # geography rescope); re-seeing the posting must not revive it.
        if existing.status != "out_of_scope":
            existing.status = "active"
        # Backfill description if we now have one and previously didn't
        if job.description_raw and not existing.description_raw:
            existing.description_raw = job.description_raw
        session.add(existing)
        return False
    session.add(job)
    return True
