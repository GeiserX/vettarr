"""``vettarr demo-seed``: what it builds, and when it refuses to write.

The rows are built by the real policy engine, so these tests also fail when
the engine's record changes shape and the demo data would no longer look like
a real instance's.
"""

import asyncio
import contextlib
import io
import logging
import os
import re
import unittest
from collections import Counter
from datetime import datetime, timezone
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

from sqlalchemy.sql.dml import Delete

from vettarr import database, demo, events
from vettarr.checker.policy import Reason, policy_version
from vettarr.config.loader import load_text
from vettarr.config.schema import Policy
from vettarr.models import Event, ItemStatus, MediaItem, MediaType
from vettarr.web import passwords, shell

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)


class BuildTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.seed = demo.build(NOW)
        cls.items = cls.seed.items

    def test_sixty_items_across_statuses_and_every_media_type(self) -> None:
        self.assertEqual(len(self.items), 60)
        statuses = Counter(item.status for item in self.items)
        self.assertEqual(statuses[ItemStatus.PENDING_REVIEW], 14)
        self.assertGreaterEqual(len(statuses), 12)
        for status in (
            ItemStatus.AWAITING_DATA, ItemStatus.NO_MATCH, ItemStatus.PARSE_FAILED, ItemStatus.ERROR,
            ItemStatus.APPROVED, ItemStatus.SEARCHING, ItemStatus.DOWNLOADING, ItemStatus.COMPLETED,
            ItemStatus.REJECTED, ItemStatus.IGNORED, ItemStatus.CHECKING,
        ):
            self.assertGreater(statuses[status], 0, status)
        self.assertEqual({item.media_type for item in self.items}, set(MediaType))

    def test_two_sources_and_two_providers(self) -> None:
        self.assertEqual({item.source_key for item in self.items}, set(demo.CHATS))
        for item in self.items:
            self.assertEqual(item.telegram_chat_id, demo.CHATS[item.source_key])
        indexers = {r["indexer"] for item in self.items for s in item.torrent_searches for r in s.prowlarr_results}
        self.assertEqual(indexers, set(demo.PROVIDERS))

    def test_every_decision_is_the_engine_s_with_structured_reasons(self) -> None:
        version = policy_version(Policy.model_validate(demo.POLICY))
        undecided = {ItemStatus.PARSE_FAILED, ItemStatus.IGNORED, ItemStatus.CHECKING}
        kinds = Counter()
        for item in self.items:
            if item.status in undecided:
                self.assertIsNone(item.reasons)
                continue
            self.assertEqual(item.policy_version, version)
            self.assertIn(item.policy_outcome, ("approve", "review", "reject", "wait"))
            self.assertTrue(item.reasons)
            self.assertIsInstance(item.signals, dict)
            for reason in item.reasons:
                self.assertEqual(set(reason), {"kind", "rule", "text"})
                self.assertEqual(Reason.from_json(reason).to_json(), reason)
                kinds[reason["kind"]] += 1
        for kind in ("rule", "missing", "type", "clear", "identity", "duplicate", "shadow"):
            self.assertGreater(kinds[kind], 0, kind)
        rules = {r["rule"] for item in self.items for r in item.reasons or () if r["kind"] == "rule"}
        self.assertEqual(rules, {"violence", "nudity", "frightening", "adult-certification", "unwanted-keywords"})

    def test_a_status_agrees_with_its_decision(self) -> None:
        for item in self.items:
            if item.status is ItemStatus.PENDING_REVIEW:
                self.assertEqual(item.policy_outcome, "review")
                self.assertIsNone(item.reviewed_at)
            elif item.status is ItemStatus.AWAITING_DATA:
                self.assertEqual(item.policy_outcome, "wait")
            elif item.status is ItemStatus.REJECTED:
                by_policy = item.policy_outcome == "reject"
                self.assertEqual(item.reviewed_at is None, by_policy)
        rejected = [i.policy_outcome for i in self.items if i.status is ItemStatus.REJECTED]
        self.assertIn("reject", rejected)
        self.assertIn("review", rejected)

    def test_releases_and_downloads_sit_where_a_real_item_has_them(self) -> None:
        for item in self.items:
            if item.status in (ItemStatus.DOWNLOADING, ItemStatus.COMPLETED):
                self.assertIsNotNone(item.download)
                self.assertTrue(item.torrent_searches[0].prowlarr_results)
                self.assertEqual(item.download.progress == 1.0, item.status is ItemStatus.COMPLETED)
            elif item.status is ItemStatus.NO_MATCH:
                self.assertEqual(item.torrent_searches[0].result_count, 0)
            else:
                self.assertIsNone(item.download)
        watched = [i for i in self.items if i.media_type in (MediaType.MOVIE, MediaType.SERIES) and i.reasons]
        self.assertTrue(all(i.content_check and i.content_check.poster_url.startswith("/static/demo/") for i in watched))

    def test_events_are_marked_and_of_known_kinds(self) -> None:
        kinds = Counter(event.kind for _, event in self.seed.events)
        # Every kind but two: the demo delivers to the download client, so no
        # Seerr request is declined, and it has no seerr source whose webhook
        # could be tested.
        self.assertEqual(set(kinds), set(events.KINDS) - {events.DECLINED, events.WEBHOOK_TEST})
        for index, event in self.seed.events:
            self.assertIs(event.payload["demo"], True)
            self.assertTrue(index is None or 0 <= index < 60)
        self.assertEqual(kinds[events.DECISION], sum(1 for i in self.items if i.reasons))

    def test_one_provider_is_in_an_open_cooldown_for_the_banner(self) -> None:
        guards = sorted(
            (e for _, e in self.seed.events if e.kind == events.GUARD), key=lambda e: e.at, reverse=True,
        )
        self.assertEqual([f.name for f in shell.failing_from(guards, NOW)], [demo.PROVIDERS[0]])

    def test_the_same_moment_builds_the_same_rows(self) -> None:
        def shape(seed):
            return [(i.title, i.status, i.source_key, i.reasons, i.size_text, len(i.torrent_searches)) for i in seed.items]

        self.assertEqual(shape(demo.build(NOW)), shape(self.seed))

    def test_nothing_real_is_named(self) -> None:
        text = repr([(i.raw_text, i.title) for i in self.items]) + demo.config_text()
        for host in set(re.findall(r"https?://([^/\s'\"]+)", text)):
            self.assertTrue(host.endswith(".example"), host)


class ConfigTests(unittest.TestCase):
    def test_the_printed_config_is_valid_and_is_the_data_s(self) -> None:
        env = {"WEB_PORT": "4861", "WEB_PASSWORD_HASH": passwords.hash_password("correct horse", n=2**10, p=1)}
        cfg = load_text(demo.config_text(), "demo.yaml", env)
        self.assertEqual((cfg.web.bind, cfg.web.port), ("127.0.0.1", 4861))
        self.assertIsNotNone(cfg.web.auth.password_hash)
        self.assertEqual([s.key for s in cfg.sources], list(demo.CHATS))
        self.assertEqual([p.key for p in cfg.providers], list(demo.PROVIDERS))
        self.assertEqual(cfg.policy, Policy.model_validate(demo.POLICY))

    def test_config_prints_it_and_touches_no_database(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.object(demo, "_run") as run:
            self.assertEqual(demo.main(["--config"]), 0)
        run.assert_not_called()
        self.assertEqual(out.getvalue(), demo.config_text())


class FakeSession:
    """Answers the two counts ``seed`` asks for, and records what it writes."""

    def __init__(self, real_items: int, real_events: int) -> None:
        self.counts = [real_items, real_events]
        self.deletes: list[str] = []
        self.added: list = []
        self.committed = False

    async def execute(self, statement):
        if isinstance(statement, Delete):
            self.deletes.append(statement.table.name)
            return None
        return mock.Mock(scalar=mock.Mock(return_value=self.counts.pop(0)))

    def add_all(self, rows) -> None:
        self.added.extend(rows)

    async def flush(self) -> None:
        for n, row in enumerate(self.added, start=101):
            if isinstance(row, MediaItem):
                row.id = n

    async def commit(self) -> None:
        self.committed = True

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class SeedTests(unittest.TestCase):
    def seed(self, session, **kwargs):
        with mock.patch.object(database, "async_session", lambda: session):
            return asyncio.run(demo.seed(now=NOW, **kwargs))

    def test_an_empty_database_gets_the_rows_and_demo_rows_are_replaced(self) -> None:
        session = FakeSession(0, 0)
        self.assertEqual(self.seed(session), (60, len(demo.build(NOW).events)))
        self.assertTrue(session.committed)
        # Replaced, not added to: seeding twice leaves one set.
        self.assertEqual(session.deletes, ["events", "media_items"])
        written_events = [row for row in session.added if isinstance(row, Event)]
        linked = [e for e in written_events if e.kind == events.DECISION]
        self.assertTrue(linked and all(e.item_id and e.item_id > 100 for e in linked))

    def test_real_rows_refuse_the_seed_and_nothing_is_written(self) -> None:
        for real_items, real_events in ((3, 0), (0, 2)):
            with self.subTest(items=real_items, events=real_events):
                session = FakeSession(real_items, real_events)
                with self.assertRaises(RuntimeError) as caught:
                    self.seed(session)
                self.assertIn("not demo data", str(caught.exception))
                self.assertEqual((session.deletes, session.added, session.committed), ([], [], False))

    def test_force_adds_the_demo_rows_beside_real_ones(self) -> None:
        session = FakeSession(3, 2)
        self.assertEqual(self.seed(session, force=True)[0], 60)
        self.assertTrue(session.committed)

    def test_the_command_exits_1_when_refused(self) -> None:
        err = io.StringIO()
        refused = RuntimeError(demo.REFUSED.format(items=3, events=0))
        with mock.patch.object(demo, "_run", mock.AsyncMock(side_effect=refused)), contextlib.redirect_stderr(err):
            self.assertEqual(demo.main([]), 1)
        self.assertIn("--force", err.getvalue())

    def test_the_command_says_the_full_app_would_act_on_the_rows(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(demo, "_run", mock.AsyncMock(return_value=(60, 109))), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(demo.main(["--force"]), 0)
        self.assertEqual(out.getvalue(), "demo-seed: 60 items and 109 events written\n")
        self.assertIn("web serve", err.getvalue())
        self.assertIn("search and download", err.getvalue())

    def test_the_entry_point_knows_the_command(self) -> None:
        from vettarr import __main__ as entry

        with mock.patch.object(demo, "main", return_value=0) as main, self.assertRaises(SystemExit) as done:
            entry.main(["demo-seed", "--force"])
        self.assertEqual(done.exception.code, 0)
        main.assert_called_once_with(["--force"])


if __name__ == "__main__":
    unittest.main()
