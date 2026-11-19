"""The data model: one class per table.

Every row that belongs to someone carries workspace_id (or hangs off a row
that does, like a check off its monitor). There is the owner's workspace (the
real one) and one sandbox workspace per Try Demo visitor, so a demo can never
see or touch the owner's monitors, incidents or alerts.

Times are naive UTC (dates.utcnow()). Ids of workspaces and users are never
reused (sqlite_autoincrement), so an old demo cookie can't open a newer demo.
Every foreign key has an index that STARTS with its column: deleting a demo
cascades through thousands of checks, and without the index each one would
scan the whole table.
"""
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint, false
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .dates import DEFAULT_ZONE, utcnow
from .db import Base


class Workspace(Base):
    """One account's monitors: the owner's, or a demo's sandbox."""
    __tablename__ = "workspaces"
    __table_args__ = {"sqlite_autoincrement": True}

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    demo_expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    time_zone: Mapped[str] = mapped_column(String(64), default=DEFAULT_ZONE, server_default=DEFAULT_ZONE)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    # True while a new demo's past is being replayed (seed.py): the live crew leaves it alone.
    replaying: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())

    users: Mapped[list["User"]] = relationship(back_populates="workspace", cascade="all, delete-orphan",
                                               passive_deletes=True)


class User(Base):
    """Someone who can log in. The owner (made with flask create-owner) or a demo visitor."""
    __tablename__ = "users"
    __table_args__ = {"sqlite_autoincrement": True}

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(80))
    email: Mapped[str] = mapped_column(String(254), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    # Part of the login cookie. Changing it (a new password) logs out every other device.
    session_version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    failed_logins: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    locked_until: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    workspace: Mapped[Workspace] = relationship(back_populates="users")

    @property
    def first_name(self):
        return self.name.split()[0] if self.name else ""

    @property
    def initial(self):
        return (self.name or "?")[:1].upper()


class RateLimit(Base):
    """One fixed window of tries (limits.py): "login-ip:1.2.3.4" has had 3 tries since 9:00."""
    __tablename__ = "rate_limits"

    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    count: Mapped[int] = mapped_column(Integer)
    window_start: Mapped[datetime] = mapped_column(DateTime)


class Monitor(Base):
    """One thing to check: an address, how to ask it, and what a good answer is."""
    __tablename__ = "monitors"

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(80))
    # "http" (an address on the web); later stages add "practice", "tcp" and "heartbeat".
    kind: Mapped[str] = mapped_column(String(12), default="http", server_default="http")
    url: Mapped[str] = mapped_column(String(2000), default="", server_default="")
    method: Mapped[str] = mapped_column(String(8), default="GET", server_default="GET")
    # Which answers count as up, like "200-299" or "200,204,301" (probe.parse_statuses).
    expected_statuses: Mapped[str] = mapped_column(String(60), default="200-299", server_default="200-299")
    timeout_s: Mapped[int] = mapped_column(Integer, default=10, server_default="10")
    interval_s: Mapped[int] = mapped_column(Integer, default=60, server_default="60")
    paused: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    # When the next check is due (scheduler.py). None while paused.
    next_check_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    # Failure handling (incidents.py): the state, and the runs of failed and good checks.
    state: Mapped[str] = mapped_column(String(12), default="new", server_default="new")
    state_since: Mapped[datetime | None] = mapped_column(DateTime)
    fail_streak: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    ok_streak: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # Failures in a row before it is a Flatline, and good checks in a row before it is up again.
    confirm_tries: Mapped[int] = mapped_column(Integer, default=3, server_default="3")
    recover_tries: Mapped[int] = mapped_column(Integer, default=2, server_default="2")
    # The newest check already judged: an older or repeated one is never counted again.
    judged_until: Mapped[datetime | None] = mapped_column(DateTime)
    # Optimistic locking: a save from a stale copy (another worker changed it meanwhile) fails.
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")

    workspace: Mapped[Workspace] = relationship()

    __mapper_args__ = {"version_id_col": version}


class Check(Base):
    """One check of one monitor, with how long each part took.

    scheduled_at is the moment the check was MEANT for. It is unique per
    monitor, so a check job that runs twice (the queue promises at least once,
    not exactly once) is still recorded once.
    """
    __tablename__ = "checks"
    __table_args__ = (UniqueConstraint("monitor_id", "scheduled_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    monitor_id: Mapped[int] = mapped_column(ForeignKey("monitors.id", ondelete="CASCADE"))
    scheduled_at: Mapped[datetime] = mapped_column(DateTime)
    started_at: Mapped[datetime] = mapped_column(DateTime)
    ok: Mapped[bool] = mapped_column(Boolean)
    # Up, but slower than the monitor's limit (a Slow Beat; stage 6).
    slow: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    # Failed while Pulse Check's own connection was failing too: not counted (stage 3).
    our_side: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    # Made by the Check now button, not the schedule.
    manual: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    error_kind: Mapped[str | None] = mapped_column(String(20))
    message: Mapped[str] = mapped_column(String(300), default="", server_default="")
    status_code: Mapped[int | None] = mapped_column(Integer)
    remote_ip: Mapped[str | None] = mapped_column(String(45))
    # Milliseconds for each part (None when the check never got that far).
    dns_ms: Mapped[float | None] = mapped_column(Float)
    connect_ms: Mapped[float | None] = mapped_column(Float)
    tls_ms: Mapped[float | None] = mapped_column(Float)
    ttfb_ms: Mapped[float | None] = mapped_column(Float)  # sent the request -> the first byte came back
    total_ms: Mapped[float | None] = mapped_column(Float)

    monitor: Mapped[Monitor] = relationship()


class Gap(Base):
    """Checks that never happened: the site was asleep (Render's free plan) or the crew was
    stopped. A gap is NOT downtime: nobody looked, so uptime leaves these minutes out."""
    __tablename__ = "gaps"

    id: Mapped[int] = mapped_column(primary_key=True)
    monitor_id: Mapped[int] = mapped_column(ForeignKey("monitors.id", ondelete="CASCADE"), index=True)
    start: Mapped[datetime] = mapped_column(DateTime)  # the first check that was skipped
    end: Mapped[datetime] = mapped_column(DateTime)  # the check that happened after it
    slots: Mapped[int] = mapped_column(Integer)  # how many checks were skipped


class Job(Base):
    """One piece of background work in the queue (queue.py).

    status: queued -> running -> done, or back to queued for a retry, or dead
    (a dead letter) after its last try. A running job holds a LEASE: if its
    worker dies, the lease runs out and the reaper puts the job back.
    dedupe_key is UNIQUE, so the same work ("check monitor 7 at 9:00") can
    never be queued twice, even by two workers at the same moment.
    """
    __tablename__ = "jobs"
    __table_args__ = (Index("ix_jobs_status_run_at", "status", "run_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int | None] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(40))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(12), default="queued", server_default="queued")
    run_at: Mapped[datetime] = mapped_column(DateTime)
    priority: Mapped[int] = mapped_column(Integer, default=5, server_default="5")  # lower goes first
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    max_attempts: Mapped[int] = mapped_column(Integer, default=6, server_default="6")
    lease_until: Mapped[datetime | None] = mapped_column(DateTime)
    locked_by: Mapped[str | None] = mapped_column(String(80))
    last_error: Mapped[str] = mapped_column(Text, default="", server_default="")
    dedupe_key: Mapped[str | None] = mapped_column(String(120), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)


class Worker(Base):
    """A member of the crew: a worker thread in the web app, or `flask worker`.
    It writes last_seen every round (a heartbeat), so the Crew page can tell who
    is on duty and who has gone quiet."""
    __tablename__ = "workers"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    kind: Mapped[str] = mapped_column(String(20))  # "in the web app" / "own process" / "timer"
    started_at: Mapped[datetime] = mapped_column(DateTime)
    last_seen: Mapped[datetime] = mapped_column(DateTime)
    jobs_done: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime)


class Incident(Base):
    """A confirmed failure of one monitor, from its first failed check to its recovery."""
    __tablename__ = "incidents"
    __table_args__ = (Index("ix_incidents_workspace_id_opened_at", "workspace_id", "opened_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    monitor_id: Mapped[int] = mapped_column(ForeignKey("monitors.id", ondelete="CASCADE"), index=True)
    # When it started: the first failed check of the run, not the moment it was confirmed.
    opened_at: Mapped[datetime] = mapped_column(DateTime)
    confirmed_at: Mapped[datetime] = mapped_column(DateTime)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime)
    cause: Mapped[str | None] = mapped_column(String(20))  # the error_kind of the confirming check
    message: Mapped[str] = mapped_column(String(300), default="", server_default="")
    failed_checks: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    monitor: Mapped[Monitor] = relationship()

    @property
    def is_open(self):
        return self.resolved_at is None


class CanaryCheck(Base):
    """Pulse Check checking ITSELF (canary.py): its own address (which also keeps Render
    awake) and an address outside that is always up. If the outside one fails, the
    problem is probably our own connection, and failures meanwhile are not counted."""
    __tablename__ = "canary_checks"
    __table_args__ = (Index("ix_canary_checks_target_at", "target", "at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    target: Mapped[str] = mapped_column(String(10))  # "self" or "outside"
    at: Mapped[datetime] = mapped_column(DateTime)
    ok: Mapped[bool] = mapped_column(Boolean)
    total_ms: Mapped[float | None] = mapped_column(Float)
    message: Mapped[str] = mapped_column(String(300), default="", server_default="")


class Timer(Base):
    """When the crew next does something on its own clock (the canary, the rollups).
    Claimed with a compare-and-swap, so of four worker threads only one does it."""
    __tablename__ = "timers"

    name: Mapped[str] = mapped_column(String(40), primary_key=True)
    next_at: Mapped[datetime] = mapped_column(DateTime)
