from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from vettarr.config import settings
from vettarr.models import Base

log = logging.getLogger(__name__)

engine = create_async_engine(settings.database_url, pool_size=10, max_overflow=5)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

# The image ships alembic.ini and alembic/ next to the package source.
_REPO_ROOT = Path(__file__).resolve().parents[2]


def _alembic_config():
    from alembic.config import Config

    ini = _REPO_ROOT / "alembic.ini"
    scripts = _REPO_ROOT / "alembic"
    if not ini.exists() or not scripts.exists():
        return None
    config = Config(str(ini))
    config.set_main_option("script_location", str(scripts))
    # Logging is ours already; env.py must not reconfigure (and disable) it.
    config.attributes["configure_logging"] = False
    return config


_UNKNOWN_REVISION = re.compile(r"Can't locate revision identified by '([^']+)'")


def _revision_unknown_to_this_build(config, exc: Exception) -> str | None:
    """The revision the error names, when this build's migrations lack it.

    Upgrading to head can only fail to locate one revision this build does not
    ship: the one the database is already at, written by a newer release.
    """
    from alembic.script import ScriptDirectory

    match = _UNKNOWN_REVISION.search(str(exc))
    if match is None:
        return None
    revision = match.group(1)
    try:
        ScriptDirectory.from_config(config).get_revision(revision)
    except Exception:
        return revision
    return None


def _upgrade_to_head() -> bool:
    """Upgrade to head. False when the database is ahead of this build.

    A rollback to an older image meets a database a newer release already
    migrated. Its revision is not in this image's alembic/ directory, so the
    upgrade cannot even start; the older code still runs on the newer schema,
    because migrations only ever add nullable columns and new tables.
    """
    from alembic import command
    from alembic.util import CommandError

    config = _alembic_config()
    if config is None:
        log.warning("No alembic directory found next to the package; skipping migrations")
        return True
    try:
        command.upgrade(config, "head")
    except CommandError as exc:
        revision = _revision_unknown_to_this_build(config, exc)
        if revision is None:
            raise
        log.warning(
            "database is ahead of this build (revision %s is not among this "
            "build's migrations); starting without migrating",
            revision,
        )
        return False
    return True


async def run_migrations() -> bool:
    """Bring the schema up to head before anything touches it.

    create_all only ever adds missing tables, so a column added to a table that
    already exists would never appear on a live database. The migrations are
    written to be safe to re-run, and alembic runs in a worker thread because
    its env.py drives its own event loop. Returns False when the database is
    ahead of this build, and the schema must then be left exactly as it is.
    """
    try:
        at_head = await asyncio.to_thread(_upgrade_to_head)
    except Exception:
        log.exception("Database migration failed")
        raise
    if at_head:
        log.info("Database migrations are at head")
    return at_head


def _has_items_table(sync_conn) -> bool:
    from sqlalchemy import inspect

    return inspect(sync_conn).has_table("media_items")


def _is_new(sync_conn) -> bool:
    """True for a database with none of our tables and no migration revision.

    A revision with none of our tables is not new: `init_db` then upgrades
    from it like any other, and refuses a revision this build does not know
    as another application's database.
    """
    from sqlalchemy import inspect, text

    existing = set(inspect(sync_conn).get_table_names())
    if existing.intersection(Base.metadata.tables):
        return False
    if "alembic_version" in existing:
        return sync_conn.execute(text("SELECT count(*) FROM alembic_version")).scalar() == 0
    return True


async def _is_new_database() -> bool:
    async with engine.connect() as conn:
        return await conn.run_sync(_is_new)


async def _build_new_schema() -> None:
    """Build today's schema from the models, then run the migrations over it.

    The first migration alters tables an older release built, so it cannot run
    on an empty database: the models build the schema first. The migrations
    then run from the start, which records the head and, being safe to run
    again, changes nothing on a schema just built. They are not skipped with a
    stamp, because no table does not mean nothing is left: a database whose
    tables were dropped keeps the enum types an older release created,
    `create_all` reuses them as they are, and only the migrations add the
    labels they lack.
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await run_migrations()
    log.info("New database: schema created at the migration head")


async def init_db() -> None:
    if await _is_new_database():
        await _build_new_schema()
        return
    if not await run_migrations():
        # A revision this build lacks is either a newer release's, or some
        # other application's (a wrong DATABASE_URL, a restore from another
        # project). Only the first one holds our tables.
        async with engine.connect() as conn:
            if not await conn.run_sync(_has_items_table):
                raise RuntimeError(
                    "The database's migration revision is not one of this "
                    "build's and it has no media_items table: it is not this "
                    "service's database. Check DATABASE_URL."
                )
        return
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_session() -> AsyncSession:
    async with async_session() as session:
        yield session
