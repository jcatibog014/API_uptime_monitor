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

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint, false, true
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
    # "http" (an address on the web), "tcp" (a port: tcp://host:port), "heartbeat" (it calls us)
    # or "practice" (the demo's pretend services, practice.py).
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
    # Smarter checks (stage 6, checks_extra.py): what a good answer must say, and when it is too slow.
    # assertions: {"contains": "...", "not_contains": "...", "json_path": "data.status", "json_value": "ok",
    # "header": "x-version"}; empty keys are left out.
    assertions: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")
    slow_ms: Mapped[int | None] = mapped_column(Integer)  # slower than this is a Weak Pulse
    # A site on a free plan that sleeps and takes a minute to wake: wait up to 90 s for it.
    sleepy: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    # Heartbeat monitors: their own address (/hb/<token>) that a cron job calls, and how late it may be.
    heartbeat_token: Mapped[str | None] = mapped_column(String(40), unique=True)
    grace_s: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_ping_at: Mapped[datetime | None] = mapped_column(DateTime)
    # The certificate (https), from the last check: when it runs out, who issued it, and the
    # last warning sent (days left: 14, 7, 3 or 1), so each warning goes out once.
    cert_expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    cert_issuer: Mapped[str] = mapped_column(String(120), default="", server_default="")
    cert_warned_days: Mapped[int | None] = mapped_column(Integer)

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
    # Someone said "I'm on it" (the Acknowledge link in an alert, or the button): reminders stop.
    acked_at: Mapped[datetime | None] = mapped_column(DateTime)
    acked_by: Mapped[str] = mapped_column(String(80), default="", server_default="")

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


# Response-time buckets for the histograms in rollups (metrics.py): the upper edge of each, in ms.
# Wider as they go up (a log scale): 10 ms matters at 50 ms, not at 5 seconds. One more bucket
# holds everything slower than the last edge.
BUCKET_EDGES = (10, 20, 30, 50, 75, 100, 150, 200, 300, 500, 750, 1000, 1500, 2000, 3000, 5000, 7500, 10000,
                15000, 30000)
BUCKETS = len(BUCKET_EDGES) + 1


class Rollup(Base):
    """One monitor's numbers for one hour (UTC) or one day (the workspace's own day), worked out
    from its checks by rollups.py. Long periods (30, 90 days) add these up instead of reading
    every check, and raw checks can then be deleted after 14 days.

    A percentile can't be added up (the average of two p95s is not the p95 of both), but a
    histogram can: b0..b20 count the good checks in each response-time bucket, and adding
    two histograms gives the histogram of both. metrics.py reads percentiles off it."""
    __tablename__ = "rollups"
    __table_args__ = (UniqueConstraint("monitor_id", "period", "start"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    monitor_id: Mapped[int] = mapped_column(ForeignKey("monitors.id", ondelete="CASCADE"))
    period: Mapped[str] = mapped_column(String(4))  # "hour" or "day"
    start: Mapped[datetime] = mapped_column(DateTime)
    checks: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    ok: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    slow: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    our_side: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    missed: Mapped[int] = mapped_column(Integer, default=0, server_default="0")  # skipped slots (gaps)
    sum_ms: Mapped[float] = mapped_column(Float, default=0, server_default="0")  # of good checks
    min_ms: Mapped[float | None] = mapped_column(Float)
    max_ms: Mapped[float | None] = mapped_column(Float)
    # b0 .. b20: good checks per response-time bucket (BUCKET_EDGES). Written in a loop: 21
    # columns that are all the same. vars() here is the class being built.
    for _n in range(BUCKETS):
        vars()[f"b{_n}"] = mapped_column(Integer, default=0, server_default="0")
    del _n

    def histogram(self):
        return [getattr(self, f"b{n}") or 0 for n in range(BUCKETS)]


class Channel(Base):
    """Somewhere alerts go (alerts.py): an email address, a Discord or Slack webhook, a Telegram
    chat, the phones that turned on alerts, or the Practice Inbox (the demo's, and for trying).
    Webhook addresses and bot tokens are secrets: they're kept encrypted (crypto.py)."""
    __tablename__ = "channels"

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(10))  # email / discord / slack / telegram / push / practice
    name: Mapped[str] = mapped_column(String(80))
    # Not secret: the email address, the Telegram chat's name, how a practice channel looks...
    settings: Mapped[dict] = mapped_column(JSON, default=dict)
    secret: Mapped[str] = mapped_column(Text, default="", server_default="")  # encrypted
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    # The breaker: failures in a row, and no sending until paused_until (alerts.py).
    fail_streak: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    paused_until: Mapped[datetime | None] = mapped_column(DateTime)
    last_sent_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_error: Mapped[str] = mapped_column(String(300), default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Alert(Base):
    """One alert to one channel: "Payments API is down" to the team's Discord.
    UNIQUE per incident, channel, event and seq, so the same alert is never made twice
    (seq numbers the reminders of one incident; stage 7)."""
    __tablename__ = "alerts"
    __table_args__ = (UniqueConstraint("incident_id", "channel_id", "event", "seq"),
                      Index("ix_alerts_workspace_id_created_at", "workspace_id", "created_at"))

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id", ondelete="CASCADE"), index=True)
    incident_id: Mapped[int | None] = mapped_column(ForeignKey("incidents.id", ondelete="CASCADE"), index=True)
    event: Mapped[str] = mapped_column(String(12))  # down / up / test (later: reminder, escalation...)
    seq: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    status: Mapped[str] = mapped_column(String(10), default="waiting", server_default="waiting")
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text, default="", server_default="")
    link: Mapped[str] = mapped_column(String(500), default="", server_default="")
    tries: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_error: Mapped[str] = mapped_column(String(300), default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime)

    channel: Mapped[Channel] = relationship()


class PracticeMessage(Base):
    """What a practice channel "received": the Practice Inbox shows each alert the way the real
    service would (an email, a Discord message, a Telegram message, a phone notification)."""
    __tablename__ = "practice_messages"
    __table_args__ = (Index("ix_practice_messages_workspace_id_at", "workspace_id", "at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    alert_id: Mapped[int] = mapped_column(ForeignKey("alerts.id", ondelete="CASCADE"), unique=True)
    looks_like: Mapped[str] = mapped_column(String(10))  # email / discord / telegram / push
    channel_name: Mapped[str] = mapped_column(String(80))
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text, default="", server_default="")
    link: Mapped[str] = mapped_column(String(500), default="", server_default="")
    at: Mapped[datetime] = mapped_column(DateTime)


class PushSubscription(Base):
    """A browser or phone that turned on alerts (web push). One push channel sends to all of them."""
    __tablename__ = "push_subscriptions"

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    endpoint: Mapped[str] = mapped_column(String(1000), unique=True)
    p256dh: Mapped[str] = mapped_column(String(200))
    auth: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
