"""The plumbing the auto-approval rests on.

Every rule in the policy is tested as a pure function, which says nothing
about whether the service hands it the right arguments. Each test here fails
if one wire is pulled: a setting that stops being read, a clock that stops
being passed, a scheduled job that stops being registered, a migration that
stops running. All of them are silent failures in production -- the service
starts, the tests pass, and items quietly stop being decided correctly.
"""

import asyncio
import importlib.util
import pathlib
import inspect
import logging
import os
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

logging.disable(logging.CRITICAL)

for name, value in {
    "TELEGRAM_API_ID": "1",
    "TELEGRAM_API_HASH": "test",
    "TELEGRAM_PHONE": "+10000000000",
    "TELEGRAM_GROUP_ID": "-1",
    "TELEGRAM_BOT_TOKEN": "test",
    "TELEGRAM_OWNER_ID": "1",
    "TMDB_API_KEY": "test",
    "PROWLARR_URL": "http://prowlarr",
    "PROWLARR_API_KEY": "test",
    "QBITTORRENT_URL": "http://qbittorrent",
}.items():
    os.environ.setdefault(name, value)

import policy_fixture

from vettarr import config, database, pipeline, scheduler
from vettarr.config.runtime import from_environment
from vettarr.models import ItemStatus, MediaItem, MediaType, Recommendation

NOW = datetime(2026, 9, 21, 10, 0, tzinfo=timezone.utc)


def settings_stub(**kwargs):
    return policy_fixture.settings_stub(**kwargs)


def make_item(**kwargs) -> MediaItem:
    item = MediaItem(
        id=1,
        telegram_message_id=4242,
        telegram_chat_id=-1,
        raw_text="Alguna Pelicula (2024) 1080p WEB-DL-GRUPO",
        title="Alguna Pelicula",
        year=2024,
        media_type=MediaType.MOVIE,
        status=ItemStatus.PARSED,
        auto_recommendation=Recommendation.NEUTRAL,
    )
    item.created_at = NOW - timedelta(days=3)
    for key, value in kwargs.items():
        setattr(item, key, value)
    return item


def decide_with(settings, item=None):
    """Run the pipeline's own call into the policy and report what reached it."""
    seen = {}
    real = pipeline.evaluate

    def _spy(item_, signals, policy, **kwargs):
        seen.update(item=item_, signals=signals, policy=policy, **kwargs)
        return real(item_, signals, policy, **kwargs)

    with (
        mock.patch.object(pipeline, "settings", settings),
        mock.patch.object(pipeline, "evaluate", _spy),
    ):
        seen["verdict"] = asyncio.run(
            pipeline._decide(None, item or make_item(), None, None, None, None, NOW)
        )
    return seen


class PolicyArgumentTests(unittest.TestCase):
    def test_the_item_s_age_reaches_the_policy(self) -> None:
        """Without created_at and a clock the expiry can never fire, and an
        item that never gets its data waits for ever instead of coming back
        to the review queue."""
        item = make_item()
        seen = decide_with(settings_stub(), item)
        self.assertEqual(seen["item"].created_at, item.created_at)
        self.assertIsNotNone(seen["item"].created_at)
        self.assertEqual(seen["now"], NOW)

    def test_an_item_older_than_the_limit_is_handed_back(self) -> None:
        """The end-to-end shape of the same wire."""
        seen = decide_with(settings_stub())
        self.assertEqual(seen["verdict"].applied.outcome, "review")
        self.assertEqual(seen["verdict"].applied.reasons[0].kind, "expired")

    def test_the_vote_threshold_reaches_the_policy(self) -> None:
        """Wired as 0 and every severity is trusted on a single vote."""
        seen = decide_with(settings_stub(min_guide_votes=9))
        self.assertEqual(seen["policy"].min_votes, 9)
        self.assertNotEqual(seen["policy"].min_votes, 0)

    def test_the_imdb_guide_default_reaches_the_policy(self) -> None:
        """With no policy.min_votes, integrations.imdb_guide.min_votes decides."""
        seen = decide_with(settings_stub(min_guide_votes=None))
        self.assertIsNone(seen["policy"].min_votes)
        self.assertEqual(seen["default_min_votes"], 20)

    def test_the_wait_limit_reaches_the_policy(self) -> None:
        seen = decide_with(settings_stub(auto_approve_max_wait_hours=6))
        self.assertEqual(seen["policy"].type_policy("movie").max_wait_hours, 6)

    def test_the_switch_reaches_the_policy(self) -> None:
        fresh = make_item()
        fresh.created_at = NOW
        off = decide_with(settings_stub(auto_approve_enabled=False), fresh)["verdict"]
        on = decide_with(settings_stub(auto_approve_enabled=True), fresh)["verdict"]
        self.assertEqual(off.applied.reasons[0].kind, "setting")
        self.assertNotEqual(on.applied.reasons[0].kind, "setting")

    def test_the_type_list_reaches_the_policy(self) -> None:
        seen = decide_with(settings_stub(auto_approve_types=frozenset({"magazine"})))
        self.assertEqual(seen["policy"].type_policy("movie").mode, "never")

    def test_the_parsed_title_and_year_reach_the_policy(self) -> None:
        item = make_item(title="Alguna Pelicula", year=2024)
        seen = decide_with(settings_stub(), item)
        self.assertEqual(seen["item"].title, "Alguna Pelicula")
        self.assertEqual(seen["signals"]["year"], 2024)


class ConfigDefaultTests(unittest.TestCase):
    """Deploying the image must change nothing until the switch is flipped."""

    @staticmethod
    def _fresh_settings(**env):
        cleared = {
            "AUTO_APPROVE_ENABLED": None,
            "AUTO_APPROVE_TYPES": None,
            "MIN_GUIDE_VOTES": None,
            "AUTO_APPROVE_MAX_WAIT_HOURS": None,
        }
        cleared.update(env)
        with mock.patch.dict(os.environ, {k: v for k, v in cleared.items() if v}, clear=False):
            for key, value in cleared.items():
                if value is None:
                    os.environ.pop(key, None)
            return config.Settings(from_environment(dict(os.environ)))

    def test_auto_approval_is_off_by_default(self) -> None:
        self.assertFalse(self._fresh_settings().auto_approve_enabled)

    def test_the_switch_can_be_turned_on(self) -> None:
        """A default of False is only meaningful if True is reachable."""
        self.assertTrue(self._fresh_settings(AUTO_APPROVE_ENABLED="true").auto_approve_enabled)

    def test_books_are_not_in_the_default_type_list(self) -> None:
        types = self._fresh_settings().auto_approve_types
        self.assertEqual(types, frozenset({"movie", "series", "magazine"}))
        for book_type in ("ebook", "audiobook", "comic"):
            self.assertNotIn(book_type, types)

    def test_the_vote_threshold_defaults_to_five(self) -> None:
        self.assertEqual(self._fresh_settings().min_guide_votes, 5)

    def test_the_wait_limit_defaults_to_a_day(self) -> None:
        self.assertEqual(self._fresh_settings().auto_approve_max_wait_hours, 24)


class ScheduledJobTests(unittest.TestCase):
    def test_the_hourly_re_check_is_registered(self) -> None:
        """Without this job an item that waits for data waits for ever."""
        sched = scheduler.Scheduler()
        added = []
        sched._scheduler = mock.MagicMock()
        sched._scheduler.add_job.side_effect = lambda *a, **k: added.append(k.get("id"))
        sched.start()
        self.assertIn("recheck_waiting", added)

    def test_the_download_poll_is_still_registered(self) -> None:
        sched = scheduler.Scheduler()
        added = []
        sched._scheduler = mock.MagicMock()
        sched._scheduler.add_job.side_effect = lambda *a, **k: added.append(k.get("id"))
        sched.start()
        self.assertIn("check_downloads", added)

    def test_the_re_check_job_runs_the_re_check(self) -> None:
        """Registering a job that calls nothing would pass the test above."""
        called = mock.AsyncMock(return_value=0)
        with mock.patch.object(pipeline, "recheck_waiting_items", called):
            asyncio.run(scheduler._recheck_waiting())
        called.assert_awaited_once()


def _USED_DATABASE():
    """A database that already holds the app's tables: the path every start but the first takes."""
    return mock.patch.object(database, "_is_new_database", mock.AsyncMock(return_value=False))


class MigrationWiringTests(unittest.TestCase):
    def test_start_up_runs_the_migrations(self) -> None:
        """create_all only ever adds missing tables, so a column added to a
        table that already exists would never appear on the live database."""
        upgrade = mock.AsyncMock()
        engine = mock.MagicMock()
        with (
            mock.patch.object(database, "run_migrations", upgrade),
            mock.patch.object(database, "engine", engine),
            _USED_DATABASE(),
        ):
            asyncio.run(database.init_db())
        upgrade.assert_awaited_once()

    def test_the_migrations_run_before_anything_touches_the_schema(self) -> None:
        source = inspect.getsource(database.init_db)
        self.assertLess(
            source.index("run_migrations"),
            source.index("create_all"),
            "a migration that runs after create_all is a migration that ran too late",
        )


def _alembic_error(current_revision: str):
    """The exact error alembic raises upgrading a database at `current_revision`."""
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(database._alembic_config())
    try:
        script._upgrade_revs("head", current_revision)
    except Exception as exc:  # alembic.util.CommandError
        return exc
    raise AssertionError(f"alembic accepted {current_revision!r}")


class RollbackToleranceTests(unittest.TestCase):
    """A rollback to an older image meets a database a newer release migrated.

    Its revision is not in the older image's alembic/ directory, the upgrade
    cannot start, and the service used to refuse to boot -- so rolling back
    was not a tag change. Now that case alone is tolerated; every other
    migration error still stops the start.
    """

    def test_a_database_ahead_of_this_build_starts_with_one_warning(self) -> None:
        logging.disable(logging.NOTSET)
        self.addCleanup(logging.disable, logging.CRITICAL)
        error = _alembic_error("0099_from_a_newer_build")
        with (
            mock.patch("alembic.command.upgrade", side_effect=error),
            self.assertLogs(database.log, level="WARNING") as logs,
        ):
            self.assertFalse(database._upgrade_to_head())
        self.assertEqual(len(logs.records), 1)
        self.assertIn("database is ahead of this build", logs.output[0])
        self.assertIn("0099_from_a_newer_build", logs.output[0])

    @staticmethod
    def _engine_whose_items_table(exists: bool):
        conn = mock.MagicMock()
        conn.run_sync = mock.AsyncMock(return_value=exists)
        engine = mock.MagicMock()
        engine.connect.return_value.__aenter__ = mock.AsyncMock(return_value=conn)
        engine.connect.return_value.__aexit__ = mock.AsyncMock(return_value=False)
        return engine, conn

    def test_a_database_ahead_skips_create_all(self) -> None:
        """create_all against a schema this build does not know is a guess."""
        engine, conn = self._engine_whose_items_table(True)
        with (
            mock.patch.object(database, "_upgrade_to_head", return_value=False),
            mock.patch.object(database, "engine", engine),
            _USED_DATABASE(),
        ):
            asyncio.run(database.init_db())
        engine.begin.assert_not_called()
        conn.run_sync.assert_awaited_once_with(database._has_items_table)

    def test_another_application_s_database_still_refuses_to_start(self) -> None:
        """An unknown revision with none of our tables is not a rollback: it is
        the wrong database, and starting on it fails on the first query."""
        engine, _ = self._engine_whose_items_table(False)
        with (
            mock.patch.object(database, "_upgrade_to_head", return_value=False),
            mock.patch.object(database, "engine", engine),
            _USED_DATABASE(),
        ):
            with self.assertRaisesRegex(RuntimeError, "DATABASE_URL"):
                asyncio.run(database.init_db())
        engine.begin.assert_not_called()

    def test_the_items_table_probe_reads_the_real_schema(self) -> None:
        from sqlalchemy import create_engine, text

        sync_engine = create_engine("sqlite://")
        with sync_engine.connect() as conn:
            self.assertFalse(database._has_items_table(conn))
            conn.execute(text("CREATE TABLE media_items (id INTEGER PRIMARY KEY)"))
            self.assertTrue(database._has_items_table(conn))

    def test_a_database_at_head_still_runs_create_all(self) -> None:
        """The positive control for the test above."""
        conn = mock.MagicMock()
        conn.run_sync = mock.AsyncMock()
        engine = mock.MagicMock()
        engine.begin.return_value.__aenter__ = mock.AsyncMock(return_value=conn)
        engine.begin.return_value.__aexit__ = mock.AsyncMock(return_value=False)
        with (
            mock.patch.object(database, "_upgrade_to_head", return_value=True),
            mock.patch.object(database, "engine", engine),
            _USED_DATABASE(),
        ):
            asyncio.run(database.init_db())
        conn.run_sync.assert_awaited_once()

    def test_a_revision_this_build_knows_still_raises(self) -> None:
        """Only an unknown revision is the rollback case."""
        from alembic.util import CommandError

        error = CommandError("Can't locate revision identified by '0001_auto_approval'")
        with mock.patch("alembic.command.upgrade", side_effect=error):
            with self.assertRaises(CommandError):
                database._upgrade_to_head()

    def test_any_other_alembic_error_still_raises(self) -> None:
        from alembic.util import CommandError

        error = CommandError("Multiple head revisions are present for given argument 'head'")
        with mock.patch("alembic.command.upgrade", side_effect=error):
            with self.assertRaises(CommandError):
                asyncio.run(database.run_migrations())

    def test_a_database_error_still_raises(self) -> None:
        with mock.patch("alembic.command.upgrade", side_effect=OSError("connection refused")):
            with self.assertRaises(OSError):
                asyncio.run(database.run_migrations())


_REVISION = (
    pathlib.Path(__file__).resolve().parents[1]
    / "alembic" / "versions" / "0001_auto_approval.py"
)


def _load_revision():
    """Import the revision by path: alembic/versions is not a package."""
    spec = importlib.util.spec_from_file_location("_revision_0001", _REVISION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _emitted_statements() -> list[str]:
    module = _load_revision()
    statements: list = []
    with mock.patch.object(module.op, "execute", statements.append):
        module.upgrade()
    return [str(s) for s in statements]


def _emitted_sql() -> str:
    return "\n".join(_emitted_statements())


def _throwaway_postgres() -> str | None:
    """A database this test may create and drop things in, or None.

    Set MIGRATION_TEST_DSN to point at one. Nothing here ever runs against a
    real deployment: the test creates its own database and drops it again.
    """
    dsn = os.environ.get("MIGRATION_TEST_DSN")
    if not dsn:
        return None
    try:
        import psycopg
    except ImportError:
        return None
    try:
        with psycopg.connect(dsn, connect_timeout=3):
            return dsn
    except Exception:
        return None


class MigrationSqlTests(unittest.TestCase):
    """What the migration actually emits.

    Checked against the generated SQL, which is what runs everywhere: on a
    laptop and on the self-hosted CI runner there is no PostgreSQL to point
    at. `MigrationAgainstPostgresTests` below runs the real thing when
    MIGRATION_TEST_DSN names one, and skips loudly when it does not.
    """

    @staticmethod
    def _emitted() -> str:
        return _emitted_sql()

    def test_the_new_item_statuses_are_added(self) -> None:
        sql = self._emitted()
        self.assertIn("ALTER TYPE itemstatus ADD VALUE IF NOT EXISTS 'AWAITING_DATA'", sql)
        self.assertIn("ALTER TYPE itemstatus ADD VALUE IF NOT EXISTS 'IGNORED'", sql)

    def test_the_cancelled_download_status_is_added(self) -> None:
        self.assertIn(
            "ALTER TYPE downloadstatus ADD VALUE IF NOT EXISTS 'CANCELLED'",
            self._emitted(),
        )

    def test_the_torrent_ownership_column_is_added(self) -> None:
        sql = self._emitted()
        self.assertIn("created_torrent BOOLEAN NOT NULL DEFAULT FALSE", sql)
        self.assertIn("ADD COLUMN IF NOT EXISTS created_torrent", sql)

    def test_the_audit_columns_are_added(self) -> None:
        sql = self._emitted()
        for column in ("auto_decision", "auto_reasons", "auto_decided_at",
                       "alcohol_rating", "guide_votes"):
            self.assertIn(f"ADD COLUMN IF NOT EXISTS {column}", sql)

    def test_every_statement_is_safe_to_run_twice(self) -> None:
        """The live schema was built by create_all and never stamped, so this
        revision runs against a database that already has everything."""
        for statement in _emitted_statements():
            self.assertIn("IF NOT EXISTS", statement, statement[:90])

    def test_no_statement_reads_rewrites_or_deletes_a_row(self) -> None:
        sql = self._emitted().upper()
        for forbidden in ("DELETE ", "TRUNCATE", "UPDATE ", "DROP TABLE"):
            self.assertNotIn(forbidden, sql)

    def test_the_rollback_sql_is_written_down(self) -> None:
        """Rolling the image back needs the new labels moved off the rows
        first, because the old code cannot load a label it has never heard
        of. That instruction lives with the migration, not in someone's head."""
        source = _REVISION.read_text()
        self.assertIn("UPDATE media_items SET status = 'PENDING_REVIEW'", source)
        self.assertIn("UPDATE downloads SET status = 'ERROR'", source)


@unittest.skipUnless(
    _throwaway_postgres(),
    "no throwaway PostgreSQL offered (set MIGRATION_TEST_DSN); "
    "the generated SQL is checked instead",
)
class MigrationAgainstPostgresTests(unittest.TestCase):
    """Run the revision against a real, throwaway database.

    Builds the pre-migration schema, puts a row in it, migrates, and checks
    the row survived and the new columns arrived. Skipped when no database is
    offered, which is why MigrationSqlTests exists as well.
    """

    def test_the_migration_adds_the_columns_and_keeps_the_rows(self) -> None:
        import psycopg

        dsn = _throwaway_postgres()
        with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
            cur.execute("DROP SCHEMA IF EXISTS migration_probe CASCADE")
            cur.execute("CREATE SCHEMA migration_probe")
            cur.execute("SET search_path TO migration_probe")
            cur.execute(
                "CREATE TYPE itemstatus AS ENUM ('PENDING_REVIEW', 'APPROVED')"
            )
            cur.execute(
                "CREATE TYPE downloadstatus AS ENUM "
                "('PENDING', 'DOWNLOADING', 'COMPLETED', 'ERROR')"
            )
            cur.execute("CREATE TYPE severityrating AS ENUM ('none', 'mild')")
            cur.execute(
                "CREATE TABLE media_items (id serial PRIMARY KEY, "
                "status itemstatus NOT NULL DEFAULT 'PENDING_REVIEW')"
            )
            cur.execute("CREATE TABLE content_checks (id serial PRIMARY KEY)")
            cur.execute(
                "CREATE TABLE downloads (id serial PRIMARY KEY, "
                "status downloadstatus NOT NULL DEFAULT 'PENDING')"
            )
            cur.execute("INSERT INTO media_items DEFAULT VALUES")
            cur.execute("INSERT INTO downloads DEFAULT VALUES")

            for statement in _emitted_statements():
                cur.execute(statement)

            cur.execute("SELECT count(*) FROM media_items")
            self.assertEqual(cur.fetchone()[0], 1, "the existing row survived")
            cur.execute("SELECT created_torrent FROM downloads")
            self.assertIs(cur.fetchone()[0], False)
            cur.execute("SELECT auto_decision, auto_reasons FROM media_items")
            self.assertEqual(cur.fetchone(), (None, None))
            cur.execute("DROP SCHEMA migration_probe CASCADE")


class NewDatabaseTests(unittest.TestCase):
    """A brand-new database gets today's schema from the models, then the migrations.

    The first migration alters types an older release created, so `alembic
    upgrade head` on an empty database fails at its first statement, and a
    new install never started.
    """

    def test_an_empty_database_is_new(self) -> None:
        from sqlalchemy import create_engine

        with create_engine("sqlite://").connect() as conn:
            self.assertTrue(database._is_new(conn))

    def test_a_database_with_one_of_our_tables_is_not_new(self) -> None:
        from sqlalchemy import create_engine, text

        with create_engine("sqlite://").connect() as conn:
            conn.execute(text("CREATE TABLE downloads (id INTEGER PRIMARY KEY)"))
            self.assertFalse(database._is_new(conn))

    def test_a_revision_with_none_of_our_tables_is_not_new(self) -> None:
        """Another application's database: it must still refuse to start, not
        get our tables next to its own."""
        from sqlalchemy import create_engine, text

        with create_engine("sqlite://").connect() as conn:
            conn.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32))"))
            self.assertTrue(database._is_new(conn), "an empty version table says nothing")
            conn.execute(text("INSERT INTO alembic_version VALUES ('someone_elses')"))
            self.assertFalse(database._is_new(conn))

    @staticmethod
    def _engine():
        conn = mock.MagicMock()
        conn.run_sync = mock.AsyncMock()
        engine = mock.MagicMock()
        engine.begin.return_value.__aenter__ = mock.AsyncMock(return_value=conn)
        engine.begin.return_value.__aexit__ = mock.AsyncMock(return_value=False)
        return engine, conn

    def test_a_new_database_is_built_from_the_models_and_then_migrated(self) -> None:
        """The models first, since the first migration cannot run on nothing;
        then the migrations, which record the head and add what a leftover
        enum type of an older release lacks."""
        engine, conn = self._engine()
        order: list[str] = []
        conn.run_sync.side_effect = lambda fn: order.append("create_all")
        with (
            mock.patch.object(database, "_is_new_database", mock.AsyncMock(return_value=True)),
            mock.patch.object(
                database, "_upgrade_to_head", side_effect=lambda: order.append("upgrade") or True
            ),
            mock.patch.object(database, "engine", engine),
        ):
            asyncio.run(database.init_db())
        conn.run_sync.assert_awaited_once_with(database.Base.metadata.create_all)
        self.assertEqual(order, ["create_all", "upgrade"])

    def test_a_used_database_is_migrated_before_the_models_add_tables(self) -> None:
        """The positive control for the order: only a new database is built first."""
        engine, conn = self._engine()
        order: list[str] = []
        conn.run_sync.side_effect = lambda fn: order.append("create_all")
        with (
            _USED_DATABASE(),
            mock.patch.object(
                database, "_upgrade_to_head", side_effect=lambda: order.append("upgrade") or True
            ),
            mock.patch.object(database, "engine", engine),
        ):
            asyncio.run(database.init_db())
        self.assertEqual(order, ["upgrade", "create_all"])


_FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"


@unittest.skipUnless(
    _throwaway_postgres(),
    "no throwaway PostgreSQL offered (set MIGRATION_TEST_DSN); "
    "the start-up path is checked against mocks instead",
)
class InitDbAgainstPostgresTests(unittest.TestCase):
    """`init_db()` itself, against the three databases a start can meet.

    Each test creates its own database on the server MIGRATION_TEST_DSN names
    (the role must be allowed to), points the app at it and drops it again.
    The two old schemas are dumps of what releases 0.8.5 and 0.11.2 built.
    """

    DATABASE = "init_db_probe"

    def setUp(self) -> None:
        import psycopg
        from sqlalchemy.ext.asyncio import create_async_engine
        from sqlalchemy.pool import NullPool

        self.admin = psycopg.connect(_throwaway_postgres(), autocommit=True)
        self.addCleanup(self.admin.close)
        self._drop()
        self.admin.execute(f"CREATE DATABASE {self.DATABASE}")
        self.addCleanup(self._drop)
        info = self.admin.info
        environment = mock.patch.dict(os.environ, {
            "POSTGRES_HOST": info.host,
            "POSTGRES_PORT": str(info.port),
            "POSTGRES_USER": info.user,
            "POSTGRES_PASSWORD": info.password or "",
            "POSTGRES_DB": self.DATABASE,
        })
        environment.start()
        self.addCleanup(environment.stop)
        # One connection per use: every asyncio.run here is its own event loop.
        engine = create_async_engine(config.settings.database_url, poolclass=NullPool)
        patched = mock.patch.object(database, "engine", engine)
        patched.start()
        self.addCleanup(patched.stop)

    def _drop(self) -> None:
        self.admin.execute(f"DROP DATABASE IF EXISTS {self.DATABASE} WITH (FORCE)")

    def _connect(self):
        import psycopg

        info = self.admin.info
        conn = psycopg.connect(
            host=info.host, port=info.port, user=info.user, password=info.password,
            dbname=self.DATABASE, autocommit=True,
        )
        self.addCleanup(conn.close)
        return conn

    def _load(self, fixture: str) -> None:
        with self._connect() as conn:
            conn.execute((_FIXTURES / fixture).read_text())

    def _insert_an_old_row(self) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO media_items (telegram_message_id, telegram_chat_id, raw_text, "
                "media_type, status, auto_recommendation) "
                "VALUES (7, -1, 'Some Film (2024)', 'MOVIE', 'PENDING_REVIEW', 'NEUTRAL')"
            )

    def _assert_at_head_with_every_column(self) -> None:
        from alembic.script import ScriptDirectory

        head = ScriptDirectory.from_config(database._alembic_config()).get_current_head()
        with self._connect() as conn:
            self.assertEqual(conn.execute("SELECT version_num FROM alembic_version").fetchall(), [(head,)])
            found = conn.execute(
                "SELECT table_name, column_name FROM information_schema.columns "
                "WHERE table_schema = 'public'"
            ).fetchall()
        wanted = {
            (table.name, column.name)
            for table in database.Base.metadata.tables.values()
            for column in table.columns
        }
        self.assertGreater(len(wanted), 40, "the models were not read")
        self.assertEqual(sorted(wanted - set(found)), [], "columns the models have and the database lacks")

    def _assert_the_old_row_survived(self) -> None:
        with self._connect() as conn:
            rows = conn.execute("SELECT telegram_message_id, raw_text, sender_id FROM media_items").fetchall()
        self.assertEqual(rows, [(7, "Some Film (2024)", None)])

    def test_an_empty_database_boots_and_boots_again(self) -> None:
        asyncio.run(database.init_db())
        self._assert_at_head_with_every_column()
        # The restart: a stamped schema with nothing to migrate.
        asyncio.run(database.init_db())
        self._assert_at_head_with_every_column()

    def test_an_unstamped_old_schema_is_upgraded(self) -> None:
        self._load("schema-0.8.5-unstamped.sql")
        self._insert_an_old_row()
        asyncio.run(database.init_db())
        self._assert_at_head_with_every_column()
        self._assert_the_old_row_survived()

    def _enum_labels(self) -> dict[str, set[str]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT t.typname, e.enumlabel FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid"
            ).fetchall()
        labels: dict[str, set[str]] = {}
        for name, label in rows:
            labels.setdefault(name, set()).add(label)
        return labels

    def test_old_enum_types_left_without_their_tables_get_the_labels_they_lack(self) -> None:
        """Dropping the tables leaves the enum types an older release created.

        The database then has none of our tables and counts as new, and
        `create_all` reuses the types as they are. Stamping it at head would
        skip the migrations that add the newer labels for good, and the first
        item parked for data would be refused by the database.
        """
        from sqlalchemy import Enum

        self._load("schema-0.8.5-unstamped.sql")
        with self._connect() as conn:
            conn.execute("DROP TABLE downloads, torrent_searches, content_checks, media_items CASCADE")
        self.assertNotIn("AWAITING_DATA", self._enum_labels()["itemstatus"], "the fixture is not an old schema")

        asyncio.run(database.init_db())

        self._assert_at_head_with_every_column()
        found = self._enum_labels()
        wanted = {
            column.type.name: set(column.type.enums)
            for table in database.Base.metadata.tables.values()
            for column in table.columns
            if isinstance(column.type, Enum)
        }
        self.assertIn("AWAITING_DATA", wanted["itemstatus"], "the models were not read")
        for name, labels in wanted.items():
            with self.subTest(enum=name):
                self.assertEqual(sorted(labels - found.get(name, set())), [])

    def test_a_stamped_0_11_2_schema_is_upgraded(self) -> None:
        self._load("schema-0.11.2-stamped.sql")
        self._insert_an_old_row()
        with self._connect() as conn:
            self.assertEqual(
                conn.execute("SELECT version_num FROM alembic_version").fetchall(),
                [("0001_auto_approval",)],
            )
        asyncio.run(database.init_db())
        self._assert_at_head_with_every_column()
        self._assert_the_old_row_survived()


if __name__ == "__main__":
    unittest.main()


class MigrationLoggingTests(unittest.TestCase):
    """Running the migration at start-up must not silence the service's loggers.

    env.py configures logging from alembic.ini for the command-line tool.
    `fileConfig` disables every logger that already exists unless told
    otherwise, and by the time the service runs the upgrade its own loggers
    exist -- so 0.9.0 went quiet after "Initializing database...". The service
    hands alembic a config that opts out of that logging setup.
    """

    def test_the_service_upgrade_leaves_its_loggers_alive(self) -> None:
        import contextlib
        import io

        from alembic import command

        from vettarr.database import _alembic_config

        probe = logging.getLogger("vettarr.probe_for_logging_test")
        probe.disabled = False
        config = _alembic_config()
        self.assertIsNotNone(config)
        with contextlib.redirect_stdout(io.StringIO()):
            command.upgrade(config, "head", sql=True)
        self.assertFalse(probe.disabled)

    def test_the_command_line_tool_still_configures_logging(self) -> None:
        from alembic.config import Config

        cli_config = Config(str(pathlib.Path(__file__).resolve().parents[1] / "alembic.ini"))
        self.assertTrue(cli_config.attributes.get("configure_logging", True))
