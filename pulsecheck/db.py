"""The database: one SQLAlchemy engine per app and one session per request.

Locally and in tests it is a SQLite file in instance/ (git ignores it).
On the live site DATABASE_URL points at PostgreSQL (Supabase). The models and
queries are the same for both: SQLAlchemy writes the right SQL for each.
The few places where they really differ (claiming a job) ask is_postgres().

Why Supabase and not Neon (the other apps' database): Pulse Check writes a
check every minute, all day. Neon's free plan counts the hours its database
is awake, and a write every minute keeps it awake all month. Supabase's free
database is always on (it only pauses after a week with NO activity, which a
monitor never has).

Use it like this in a view:

    s = db.session()
    monitor = s.get(Monitor, 7)
    monitor.name = "Payments API"
    s.commit()          # nothing is saved without commit()

The crew (worker.py) runs outside any request, so it makes its own
sessions with new_session() and closes them itself.
"""
from pathlib import Path

import click
from alembic import command
from alembic.config import Config
from flask import current_app, g
from flask.cli import with_appcontext
from sqlalchemy import MetaData, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session

MIGRATIONS = Path(__file__).parent / "migrations"
ENGINE = "pulsecheck_engine"

# Every constraint gets a predictable name (uq_users_email, ...).
# Alembic needs names to change or drop a constraint in a later migration.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """The parent class of every model in models.py."""
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def database_url(config):
    """The SQLAlchemy URL: Supabase's DATABASE_URL if set, else the SQLite file."""
    url = config.get("DATABASE_URL")
    if not url:
        return "sqlite:///" + config["DATABASE"]
    # Supabase gives "postgresql://..." (some hosts "postgres://..."). SQLAlchemy
    # also needs the driver's name, and ours is psycopg (version 3).
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


def make_engine(url):
    if url.startswith("sqlite"):
        # timeout: when another worker is writing, wait up to 30 s for it instead of
        # failing at once with "database is locked" (SQLite lets one writer in at a time).
        engine = create_engine(url, connect_args={"timeout": 30})

        @event.listens_for(engine, "connect")
        def sqlite_settings(dbapi_connection, _record):
            # SQLite ignores FOREIGN KEY rules unless asked, per connection.
            dbapi_connection.execute("PRAGMA foreign_keys = ON")
            # WAL: people can read pages while the crew writes checks (the default
            # journal blocks readers during every write).
            dbapi_connection.execute("PRAGMA journal_mode = WAL")
            dbapi_connection.execute("PRAGMA synchronous = NORMAL")
        return engine
    # pool_pre_ping: the pooler closes idle connections, so check one before using it.
    # pool_size: the crew's threads and the pages share these connections.
    engine = create_engine(url, pool_pre_ping=True, pool_size=8, max_overflow=4)

    @event.listens_for(engine, "connect")
    def plain_numbers(dbapi_connection, _record):
        # PostgreSQL's SUM() and AVG() give "numeric", which psycopg turns into Decimal.
        # Decimal * 1.5 is a TypeError, and the code (and its tests, on SQLite) expect int
        # and float. So read numeric the way SQLite gives it (Tally Ho!'s lesson).
        dbapi_connection.adapters.register_loader("numeric", PlainNumberLoader)
    return engine


try:
    from psycopg.adapt import Loader
except ImportError:  # psycopg is only needed for PostgreSQL
    Loader = object


class PlainNumberLoader(Loader):
    """A PostgreSQL numeric as an int when it is whole ("123"), else a float ("12.5")."""
    def load(self, data):
        text = bytes(data).decode()
        try:
            return int(text)
        except ValueError:
            return float(text)  # also "NaN" and "Infinity"


def engine():
    return current_app.extensions[ENGINE]


def is_postgres(s_or_engine=None):
    bind = s_or_engine.get_bind() if isinstance(s_or_engine, Session) else (s_or_engine or engine())
    return bind.dialect.name == "postgresql"


def new_session(app=None) -> Session:
    """A session of its own (the crew, the scheduler). The caller closes it."""
    return Session((app or current_app).extensions[ENGINE], expire_on_commit=False)


def session() -> Session:
    """This request's session (made on first use)."""
    if "db" not in g:
        # expire_on_commit=False: objects keep their values after commit(),
        # so a view can still read monitor.name after saving it.
        g.db = Session(engine(), expire_on_commit=False)
    return g.db


def close_session(_error=None):
    s = g.pop("db", None)
    if s is not None:
        s.close()


# ---------- Making and upgrading the tables ----------

def create_tables():
    """Make every table straight from the models (tests use this; it's fast)."""
    from . import models  # noqa: F401  (registers the tables on Base.metadata)
    Base.metadata.create_all(engine())


def drop_tables():
    from . import models  # noqa: F401
    Base.metadata.drop_all(engine())


def alembic_config():
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS))
    return config


def upgrade_db(revision="head"):
    """Run the Alembic migrations that this database hasn't had yet.

    It keeps all data, and does nothing when the tables are up to date.
    Render runs it before every start (render.yaml).
    """
    config = alembic_config()
    with engine().connect() as connection:
        sqlite = connection.dialect.name == "sqlite"
        if sqlite:
            # SQLite changes a table by copying it and DROPPING the old one. With
            # foreign keys on, that drop deletes every row pointing at the old table
            # (ON DELETE CASCADE): changing "monitors" would empty "checks".
            # The switch only works outside a transaction, so commit straight after.
            connection.exec_driver_sql("PRAGMA foreign_keys = OFF")
            connection.commit()
        try:
            with connection.begin():
                config.attributes["connection"] = connection  # migrations/env.py uses it
                command.upgrade(config, revision)
            if sqlite:
                broken = connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
                if broken:
                    raise RuntimeError(f"The upgrade left {len(broken)} broken links between tables: {broken[:5]}")
        finally:
            if sqlite:
                connection.rollback()
                connection.exec_driver_sql("PRAGMA foreign_keys = ON")
                connection.commit()


@click.command("upgrade-db")
@with_appcontext
def upgrade_db_command():
    """Make or update the tables."""
    upgrade_db()
    click.echo("The database is up to date.")


def init_app(app):
    Path(app.instance_path).mkdir(parents=True, exist_ok=True)
    app.extensions[ENGINE] = make_engine(database_url(app.config))
    app.teardown_appcontext(close_session)
    app.cli.add_command(upgrade_db_command)
