"""What the review of the policy engine found, one test class per finding.

Each test fails against the code as it merged: a lookup that fails takes
only its own signals with it, a type no rule applies to is held, an
environment boot says what its policy is, the re-run only takes what could
end elsewhere, events survive a stop and a database outage, and the rest.
"""

import asyncio
import http.server
import logging
import os
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
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

import httpx
from pydantic import ValidationError

import policy_fixture
from vettarr import __main__ as cli
from vettarr import actions, events, ingest, notify, pipeline, review
from vettarr.checker import gather, tmdb_client
from vettarr.checker import policy as engine
from vettarr.checker.policy import APPROVE, REJECT, REVIEW, WAIT, Decision, Reason
from vettarr.checker.tmdb_client import MATCH_WITH_YEAR, TMDBResult
from vettarr.config import runtime
from vettarr.config import schema as config_schema
from vettarr.config.schema import Policy, Rule, Source
from vettarr.config.signals import MEDIA_TYPES
from vettarr.models import ItemStatus, MediaItem, MediaType
from vettarr.searcher.tracker_guard import TrackerGuard

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)
DOC = Path(__file__).resolve().parents[1] / "docs" / "CONFIG.md"


def rule(**fields) -> Rule:
    return Rule.model_validate({"id": "r", **fields})


def hit(rule_fields: dict, value, lists=None):
    return engine.check_rule(rule(**rule_fields), value, lists or {}, 0)[0]


def policy(*rules, lists=None, types=None, **fields) -> Policy:
    return Policy.model_validate({
        "auto_approve": "on", "rules": list(rules), "lists": lists or {}, "types": types or {}, **fields,
    })


def movie(**fields):
    return policy_fixture.item(created_at=NOW, **fields)


def with_policy(block: Policy, **changes):
    cfg = policy_fixture.config()
    return cfg.model_copy(update={"policy": block, **changes})


def without(cfg, integration: str):
    return cfg.model_copy(update={"integrations": cfg.integrations.model_copy(update={integration: None})})


def stored_item(**fields) -> MediaItem:
    item = MediaItem(id=7, telegram_message_id=1, telegram_chat_id=-1, raw_text="",
                     media_type=MediaType.MOVIE, title="Some Film", year=2024)
    item.created_at = NOW
    for key, value in fields.items():
        setattr(item, key, value)
    return item


class _Rows:
    def __init__(self, rows=(), scalar=None, first=None):
        self._rows, self._scalar, self._first = list(rows), scalar, first

    def __iter__(self):
        return iter(self._rows)

    def scalar_one_or_none(self):
        return self._scalar

    def scalars(self):
        return SimpleNamespace(first=lambda: self._first)


class _Session:
    """Answers every query the same way: a count, and no stored search."""

    def __init__(self, scalar=None):
        self.scalar = scalar

    async def execute(self, _statement):
        return _Rows(scalar=self.scalar)


def costly(needed, *, cfg=None, item=None, tmdb=None, scalar=None, size_known=True):
    return asyncio.run(gather.costly_signals(
        _Session(scalar), item or stored_item(), cfg or policy_fixture.config(), set(needed),
        tmdb=tmdb, now=NOW, size_known=size_known,
    ))


def drain() -> list:
    out = []
    while events.pending():
        out.append(events._pending.get_nowait())
    return out


def forget_kept() -> None:
    getattr(gather, "forget_kept", lambda: None)()


class GatherCase(unittest.TestCase):
    def setUp(self) -> None:
        forget_kept()
        self.addCleanup(forget_kept)


# --- a lookup that fails -----------------------------------------------------


class OneFailedLookupTests(GatherCase):
    POLICY = policy(
        {"id": "no-exe", "check": "file_extensions", "any_of": ["exe", "scr", "lnk"], "on_hit": "reject"},
        {"id": "busy", "check": "max_active_downloads", "at_least": 3, "on_hit": "review"},
        types={"movie": {"when_data_missing": "approve"}},
    )

    def _snapshot(self):
        cfg = with_policy(self.POLICY)
        item = stored_item(external_links=["https://x/details.php?id=1"])
        with mock.patch.object(gather, "_torrent_signals", mock.AsyncMock(side_effect=TypeError("broken"))):
            return item, asyncio.run(gather.gather_signals(
                _Session(scalar=9), item, cfg, tmdb=None, guide=None, book=None, book_looked_up=False,
                duplicate_of=None, twin_of=None, now=NOW,
            ))

    def test_the_other_lookups_keep_their_answers(self) -> None:
        _item, snapshot = self._snapshot()
        self.assertEqual(snapshot["max_active_downloads"], 9)
        for name in ("file_count", "file_extensions"):
            self.assertEqual(snapshot[name], {"missing": "the lookup failed"})

    def test_so_the_rule_that_could_be_read_still_holds_the_item(self) -> None:
        """when_data_missing: approve must not approve past a rule whose data was there."""
        item, snapshot = self._snapshot()
        decision = engine.evaluate(item, snapshot, self.POLICY, now=NOW)
        self.assertEqual(decision.outcome, REVIEW)
        self.assertEqual([r.rule for r in decision.hits], ["busy"])


# --- an environment boot and its policy --------------------------------------


ENV = {
    "TELEGRAM_API_ID": "1", "TELEGRAM_API_HASH": "h", "TELEGRAM_PHONE": "+10000000000",
    "TELEGRAM_GROUP_ID": "-1", "TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_OWNER_ID": "1",
    "TMDB_API_KEY": "k", "PROWLARR_URL": "http://prowlarr", "PROWLARR_API_KEY": "k",
    "QBITTORRENT_URL": "http://qbittorrent",
    "AUTO_APPROVE_ENABLED": "true",
    "AUTO_APPROVE_TYPES": "movie,series,magazine,ebook,audiobook,comic",
}


class EnvironmentBootPolicyTests(unittest.TestCase):
    def test_a_book_type_with_no_rule_is_held_not_approved(self) -> None:
        cfg = runtime.from_environment(ENV)
        for media_type in ("ebook", "audiobook", "comic"):
            with self.subTest(media_type=media_type):
                self.assertEqual(cfg.policy.type_policy(media_type).mode, "rules")
                decision = engine.evaluate(
                    movie(media_type=media_type), {"subject": ["anything", "at", "all"]}, cfg.policy, now=NOW,
                )
                self.assertEqual(decision.outcome, REVIEW)
                self.assertEqual(decision.reasons[0].kind, "type")
                self.assertEqual(engine.act(decision, cfg.policy.auto_approve).outcome, REVIEW)

    def test_the_boot_warning_says_what_the_policy_is(self) -> None:
        for phrase in (
            "The policy is the public default",
            "no keyword, genre or subject rule and no certification rule for your country",
            "is approved now",
            "is held for a person",
        ):
            self.assertIn(phrase, runtime.ENVIRONMENT_ONLY)

    def test_the_doc_says_what_the_policy_of_an_environment_boot_is(self) -> None:
        boot = DOC.read_text().split("## The rules of the file")[0]
        for phrase in (
            "The policy is the public default, not the one the deployment decided with",
            "no keyword, genre or subject rule and no certification rule",
            "is approved",
            "has no rule left and is held for a person",
        ):
            self.assertIn(phrase, boot)

    def test_config_check_warns_about_the_types_no_rule_applies_to(self) -> None:
        lines = [text for _loc, text in config_schema.warnings(runtime.from_environment(ENV))]
        self.assertTrue(any("no rule applies to audiobook, ebook, comic" in line for line in lines), lines)

    def test_tmdb_is_asked_in_the_language_0_11_asked_in(self) -> None:
        self.assertEqual(runtime.from_environment(ENV).integrations.tmdb.language, "es-ES")


# --- a type no rule applies to ------------------------------------------------


class UncheckedTypeTests(unittest.TestCase):
    VIDEO_ONLY = policy({"id": "violence", "check": "imdb.violence", "at_least": "severe"})

    def test_a_type_with_no_rule_goes_to_a_person(self) -> None:
        for media_type in ("music", "ebook", "audiobook", "comic", "magazine"):
            with self.subTest(media_type=media_type):
                decision = engine.evaluate(movie(media_type=media_type), {}, self.VIDEO_ONLY, now=NOW)
                self.assertEqual(decision.outcome, REVIEW)
                self.assertEqual(decision.reasons[0].kind, "type")
                self.assertIn(f"no rule applies to {media_type}", decision.reasons[0].text)
                self.assertEqual(engine.act(decision, "on").outcome, REVIEW)

    def test_always_still_approves_without_a_rule(self) -> None:
        block = policy(types={"magazine": {"mode": "always"}})
        self.assertEqual(engine.evaluate(movie(media_type="magazine"), {}, block, now=NOW).outcome, APPROVE)

    def test_a_policy_with_no_rules_approves_nothing(self) -> None:
        for media_type in MEDIA_TYPES:
            with self.subTest(media_type=media_type):
                self.assertEqual(engine.evaluate(movie(media_type=media_type), {}, policy(), now=NOW).outcome, REVIEW)

    def test_config_check_says_so(self) -> None:
        lines = [text for _loc, text in config_schema.warnings(with_policy(self.VIDEO_ONLY))]
        named = [line for line in lines if "no rule applies to" in line]
        self.assertEqual(len(named), 1, lines)
        for media_type in ("audiobook", "ebook", "magazine", "comic", "music"):
            self.assertIn(media_type, named[0])
        # Not while nothing is approved anyway, and not for a type the file settles.
        off = self.VIDEO_ONLY.model_copy(update={"auto_approve": "off"})
        self.assertFalse(any("no rule applies" in t for _l, t in config_schema.warnings(with_policy(off))))
        self.assertFalse(any(
            "no rule applies" in t for _l, t in config_schema.warnings(policy_fixture.config())
        ))


# --- auto-approval off and data nobody fetched --------------------------------


class OffAndMissingDataTests(unittest.TestCase):
    def test_a_review_for_data_nobody_fetched_is_a_settings_review(self) -> None:
        block = policy(
            {"id": "lib", "check": "in_library", "is": True, "on_hit": "reject"},
            {"id": "subj", "check": "subject", "matches_any": ["x"], "types": ["ebook"]},
            types={"movie": {"when_data_missing": "review"}, "ebook": {"when_data_missing": "review"}},
        )
        for media_type in ("movie", "ebook"):
            with self.subTest(media_type=media_type):
                decision = engine.evaluate(movie(media_type=media_type), {}, block, now=NOW)
                self.assertEqual((decision.outcome, decision.reasons[0].kind), (REVIEW, "missing"))
                applied = engine.act(decision, "off")
                self.assertEqual(applied.reasons[0].kind, "setting")
                self.assertTrue(engine.is_settings_review([r.to_json() for r in applied.reasons]))
                # In shadow and on the lookups ran: the review is the data's.
                for mode in ("shadow", "on"):
                    self.assertEqual(engine.act(decision, mode).reasons, decision.reasons)

    def test_a_finding_under_off_stays_a_finding(self) -> None:
        block = policy({"id": "big", "check": "size_gb", "at_least": 50},
                       {"id": "lib", "check": "in_library", "is": True},
                       types={"movie": {"when_data_missing": "review"}})
        decision = engine.evaluate(movie(), {"size_gb": 80}, block, now=NOW)
        applied = engine.act(decision, "off")
        self.assertEqual(applied.reasons[0].kind, "rule")
        self.assertFalse(engine.is_settings_review([r.to_json() for r in applied.reasons]))


# --- preview impact and the wait ---------------------------------------------


class PreviewAtDecisionTimeTests(unittest.TestCase):
    BLOCK = policy({"id": "v", "check": "imdb.violence", "at_least": "severe"})
    SNAPSHOT = {"imdb.violence": {"missing": "parents guide not fetched"}}

    def _row(self, **fields):
        base = dict(
            id=1, title="Some Film", media_type=MediaType.MOVIE, created_at=NOW,
            auto_decided_at=NOW + timedelta(hours=1), signals=self.SNAPSHOT, policy_outcome=WAIT,
        )
        base.update(fields)
        return SimpleNamespace(**base)

    def test_an_old_wait_is_not_a_change_under_the_same_policy(self) -> None:
        later = NOW + timedelta(days=3)
        impact = engine.compare([self._row()], self.BLOCK, now=later)
        self.assertEqual((impact.compared, impact.changed), (1, []))
        self.assertEqual(impact.counts[WAIT], 1)

    def test_a_wait_that_had_expired_when_decided_still_reads_expired(self) -> None:
        row = self._row(auto_decided_at=NOW + timedelta(hours=30), policy_outcome=REVIEW)
        self.assertEqual(engine.compare([row], self.BLOCK, now=NOW + timedelta(days=3)).changed, [])

    def test_a_changed_policy_still_shows(self) -> None:
        approve = self.BLOCK.model_copy(update={"types": {"movie": config_schema.TypePolicy(when_data_missing="approve")}})
        changed = engine.compare([self._row()], approve, now=NOW + timedelta(days=3)).changed
        self.assertEqual([(c["before"], c["after"]) for c in changed], [(WAIT, APPROVE)])


# --- events: a failed write, a stop, the rerun command ------------------------


class _EventSession:
    def __init__(self, written, fail):
        self.written, self.fail = written, fail

    def add_all(self, rows):
        self.rows = list(rows)

    async def commit(self):
        if self.fail:
            raise ConnectionError("database is down")
        self.written.extend(self.rows)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class EventsSurviveTests(unittest.TestCase):
    def setUp(self) -> None:
        drain()
        self.addCleanup(drain)

    def _flush(self, written, *, fail, call="flush"):
        async def sent(_event):
            return True

        with (
            mock.patch("vettarr.database.async_session", lambda: _EventSession(written, fail)),
            mock.patch.object(notify, "send", sent),
        ):
            return asyncio.run(getattr(events, call)())

    def test_a_batch_the_database_refused_is_written_by_the_next_flush(self) -> None:
        for n in range(3):
            events.emit(events.DECISION, n, outcome="approve")
        written: list = []
        self.assertEqual(self._flush(written, fail=True), 0)
        self.assertEqual(events.pending(), 3)
        self.assertEqual(self._flush(written, fail=False), 3)
        self.assertEqual(sorted(row.item_id for row in written), [0, 1, 2])
        self.assertEqual(events.pending(), 0)

    def test_flush_all_empties_the_queue_past_one_batch(self) -> None:
        for n in range(7):
            events.emit(events.DECISION, n)
        written: list = []

        async def run():
            return await events.flush_all(limit=3)

        with (
            mock.patch("vettarr.database.async_session", lambda: _EventSession(written, False)),
            mock.patch.object(notify, "send", mock.AsyncMock(return_value=True)),
        ):
            self.assertEqual(asyncio.run(run()), 7)
        self.assertEqual(events.pending(), 0)

    def test_flush_all_stops_on_a_database_that_is_down(self) -> None:
        events.emit(events.DECISION, 1)
        self.assertEqual(self._flush([], fail=True, call="flush_all"), 0)
        self.assertEqual(events.pending(), 1)

    def test_a_stop_writes_what_is_still_queued(self) -> None:
        """The flush job ran every five seconds and died with the scheduler."""
        flushed = mock.AsyncMock(return_value=0)

        async def boom(*_args, **_kwargs):
            raise RuntimeError("stop")

        async def idle(_queue):
            await asyncio.Event().wait()

        self.addCleanup(ingest.set_queue, None)
        with (
            mock.patch.object(cli, "init_db", mock.AsyncMock()),
            mock.patch("vettarr.search_jobs.run_search_jobs", boom),
            mock.patch("vettarr.pipeline.process_queue", idle),
            mock.patch("vettarr.scheduler.Scheduler", mock.Mock()),
            mock.patch("vettarr.searcher.search_service.init_trackers", mock.Mock()),
            mock.patch.object(events, "flush_all", flushed, create=True),
        ):
            asyncio.run(cli._run(policy_fixture.config(), [("scheduler", "always")]))
        flushed.assert_awaited_once()

    def test_the_rerun_command_writes_its_events_before_it_exits(self) -> None:
        order: list[str] = []

        async def rerun():
            order.append("rerun")
            return pipeline.RerunSummary()

        async def flush_all(*_args):
            order.append("flush")
            return 0

        async def dispose():
            order.append("dispose")

        with (
            mock.patch.object(pipeline, "rerun_pending_reviews", rerun),
            mock.patch.object(events, "flush_all", flush_all, create=True),
            mock.patch("vettarr.database.engine", SimpleNamespace(dispose=dispose)),
            mock.patch("builtins.print"),
        ):
            asyncio.run(cli._rerun())
        self.assertEqual(order, ["rerun", "flush", "dispose"])


# --- a notify URL that hangs ---------------------------------------------------


class _Hanging(http.server.BaseHTTPRequestHandler):
    requests = 0
    release = threading.Event()

    def do_POST(self):  # noqa: N802
        type(self).requests += 1
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        type(self).release.wait(10)
        try:
            self.send_response(200)
            self.end_headers()
        except OSError:
            pass

    def log_message(self, *_args):
        pass


class HangingReceiverTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Hanging)
        cls.server.daemon_threads = True
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.server.server_address[1]}/hook"

    @classmethod
    def tearDownClass(cls) -> None:
        _Hanging.release.set()
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self) -> None:
        _Hanging.requests = 0
        self._reset()
        self.addCleanup(self._reset)

    @staticmethod
    def _reset() -> None:
        notify._client = None
        notify._last_failure_log = float("-inf")
        notify._failures_since_log = 0
        if hasattr(notify, "_paused_until"):
            notify._paused_until = float("-inf")

    def test_a_receiver_that_hangs_costs_one_timeout_not_one_per_event(self) -> None:
        clock = [1000.0]

        async def four():
            first = await notify.send({"kind": "decision", "id": 0})
            # Timed apart from the first: the one timeout it costs, and the
            # setup around it, stretch on a loaded runner; three sends that
            # wait for nothing take microseconds there too.
            started = time.monotonic()
            rest = [await notify.send({"kind": "decision", "id": n}) for n in range(1, 4)]
            return [first, *rest], time.monotonic() - started

        with (
            mock.patch.object(notify, "TIMEOUT_SECONDS", 0.5),
            mock.patch.object(notify, "_url", lambda: self.url),
            mock.patch.object(notify, "_clock", lambda: clock[0]),
        ):
            results, took = asyncio.run(four())
            self.assertEqual(results, [False] * 4)
            self.assertEqual(_Hanging.requests, 1, "the three after the timeout were not sent")
            self.assertLess(took, notify.TIMEOUT_SECONDS, "a send after the timeout waited for one more")

            # Once the pause is over the receiver is tried again, one event at a time.
            notify._client = None
            clock[0] += notify.PAUSE_SECONDS + 1
            self.assertFalse(asyncio.run(notify.send({"kind": "decision"})))
            self.assertEqual(_Hanging.requests, 2)

    def test_an_error_answer_does_not_pause(self) -> None:
        """A receiver that answers, even with a 500, costs no time: nothing is skipped."""
        received = []

        def handler(request):
            received.append(request)
            return httpx.Response(500)

        async def three():
            notify._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            return [await notify.send({"kind": "decision"}) for _ in range(3)]

        with mock.patch.object(notify, "_url", lambda: "http://receiver/hook"):
            self.assertEqual(asyncio.run(three()), [False] * 3)
        self.assertEqual(len(received), 3)


# --- certification at_least ----------------------------------------------------


class CertificationCountryTests(unittest.TestCase):
    RULE = {"check": "certification", "at_least": "ES:16"}

    def test_no_rating_in_the_rule_s_country_is_missing_data(self) -> None:
        self.assertIsNone(hit(self.RULE, {"US": "NC-17"}))
        self.assertIsNone(hit(self.RULE, {}))
        self.assertTrue(hit(self.RULE, {"ES": "18"}))
        self.assertFalse(hit(self.RULE, {"ES": "12", "US": "NC-17"}))

    def test_so_the_type_s_when_data_missing_decides(self) -> None:
        block = policy({"id": "cert", **self.RULE})
        decision = engine.evaluate(movie(), {"certification": {"US": "NC-17"}}, block, now=NOW)
        self.assertEqual(decision.outcome, WAIT)
        self.assertEqual(decision.reasons[0].text, "certification: none for ES")


# --- the searches and the linked torrent ---------------------------------------


class SameSearchAsTheDownloadTests(GatherCase):
    def test_a_linked_torrent_is_read_without_prowlarr(self) -> None:
        """It is fetched with the provider's own session, whatever Prowlarr is."""
        import test_policy_engine as base
        from vettarr.searcher.search_service import LinkedFetch

        fetched = LinkedFetch(base.TorrentFileTests().multi(), "u", provider=None, torrent_id="1")
        cfg = without(policy_fixture.config(), "prowlarr")
        with mock.patch("vettarr.searcher.search_service.fetch_linked_torrent",
                        mock.AsyncMock(return_value=fetched)):
            out = costly({"file_count", "file_extensions"}, cfg=cfg,
                         item=stored_item(external_links=["https://x/details.php?id=1"]))
        self.assertEqual(out, {"file_count": 3, "file_extensions": ["exe", "mkv"]})

    def test_seeders_come_from_the_search_the_download_uses(self) -> None:
        """Prowlarr, then each provider's own search: a setup with no Prowlarr has seeders too."""
        from vettarr.searcher import search_service

        found = SimpleNamespace(error=None, results=[SimpleNamespace(seeders=7, size=3 * 1024 ** 3)])
        search = mock.AsyncMock(return_value=found)
        cfg = without(policy_fixture.config(), "prowlarr")
        tmdb = TMDBResult(tmdb_id=99, imdb_id="tt99", title="Una Pelicula")
        with mock.patch.object(search_service, "search_for", search):
            out = costly({"seeders", "size_gb"}, cfg=cfg, tmdb=tmdb, size_known=False)
        self.assertEqual(out, {"seeders": 7, "size_gb": 3.0})
        asked = search.await_args.args[0]
        self.assertEqual((asked.title, asked.year, asked.tmdb_id, asked.imdb_id, asked.tmdb_title),
                         ("Some Film", 2024, 99, "tt99", "Una Pelicula"))

    def test_no_search_at_all_is_unavailable(self) -> None:
        cfg = without(policy_fixture.config(), "prowlarr")
        self.assertEqual(costly({"seeders"}, cfg=cfg), {"seeders": {"unavailable": "prowlarr"}})

    def test_a_post_that_links_its_torrent_gets_no_other_release_s_seeders(self) -> None:
        from vettarr.searcher import providers, search_service

        search = mock.AsyncMock()
        with (
            mock.patch.object(providers, "linked_torrent", return_value=object()),
            mock.patch.object(search_service, "search_for", search),
            mock.patch.object(search_service, "find_releases", search),
        ):
            out = costly({"seeders"}, item=stored_item(external_links=["https://x/details.php?id=1"]))
        self.assertIn("own torrent", out["seeders"]["missing"])
        search.assert_not_awaited()

    def test_a_direct_add_gets_no_other_release_s_seeders(self) -> None:
        from vettarr.searcher import search_service

        search = mock.AsyncMock()
        source = Source.model_validate({"key": "feed", "kind": "userbot", "chat": -1, "direct_adds": "anyone"})
        cfg = policy_fixture.config().model_copy(update={"sources": [source]})
        item = stored_item(raw_text="magnet:?xt=urn:btih:" + "a" * 40)
        with (
            mock.patch.object(search_service, "search_for", search),
            mock.patch.object(search_service, "find_releases", search),
        ):
            out = costly({"seeders"}, cfg=cfg, item=item)
        self.assertIn("own torrent", out["seeders"]["missing"])
        search.assert_not_awaited()


# --- lookups repeated every hour -----------------------------------------------


class KeptAnswersTests(GatherCase):
    def test_free_space_is_read_once_in_five_minutes(self) -> None:
        clock = [100.0]
        resp = mock.Mock(json=lambda: {"server_state": {"free_space_on_disk": 50 * 1024 ** 3}})
        request = mock.AsyncMock(return_value=resp)
        with (
            mock.patch.object(pipeline._qbt, "_request", request),
            mock.patch.object(gather, "_clock", lambda: clock[0]),
        ):
            for item_id in (1, 2, 3):
                self.assertEqual(costly({"disk_free_gb"}, item=stored_item(id=item_id)), {"disk_free_gb": 50.0})
            self.assertEqual(request.await_count, 1)
            clock[0] += gather.DISK_FREE_SECONDS + 1
            costly({"disk_free_gb"})
            self.assertEqual(request.await_count, 2)

    def test_an_unanswered_free_space_is_asked_again(self) -> None:
        request = mock.AsyncMock(side_effect=ConnectionError("down"))
        with mock.patch.object(pipeline._qbt, "_request", request):
            for _ in range(2):
                self.assertIn("missing", costly({"disk_free_gb"})["disk_free_gb"])
        self.assertEqual(request.await_count, 2)

    def test_the_search_of_a_waiting_item_is_not_repeated_every_hour(self) -> None:
        from vettarr.searcher import search_service

        clock = [100.0]
        found = SimpleNamespace(error=None, results=[SimpleNamespace(seeders=3, size=0)])
        search = mock.AsyncMock(return_value=found)
        with (
            mock.patch.object(search_service, "find_releases", search),
            mock.patch.object(gather, "_clock", lambda: clock[0]),
        ):
            for _hour in range(5):
                self.assertEqual(costly({"seeders"}), {"seeders": 3})
                clock[0] += 3600
            self.assertEqual(search.await_count, 1)
            # Another item is another search, and the answer does not live for ever.
            costly({"seeders"}, item=stored_item(id=8))
            self.assertEqual(search.await_count, 2)
            clock[0] += gather.SEARCH_SECONDS
            costly({"seeders"})
            self.assertEqual(search.await_count, 3)

    def test_a_search_that_found_nothing_is_kept_too(self) -> None:
        from vettarr.searcher import search_service

        search = mock.AsyncMock(return_value=SimpleNamespace(error=None, results=[]))
        with mock.patch.object(search_service, "find_releases", search):
            for _ in range(3):
                self.assertEqual(costly({"seeders"}), {"seeders": {"missing": "the search found nothing"}})
        self.assertEqual(search.await_count, 1)

    def test_a_search_that_failed_is_asked_again(self) -> None:
        from vettarr.searcher import search_service

        search = mock.AsyncMock(return_value=SimpleNamespace(error="Prowlarr HTTP 500", results=[]))
        with mock.patch.object(search_service, "find_releases", search):
            for _ in range(2):
                self.assertEqual(costly({"seeders"}), {"seeders": {"missing": "the search failed"}})
        self.assertEqual(search.await_count, 2)

    def test_a_file_list_read_once_is_not_fetched_from_the_tracker_again(self) -> None:
        fetch = mock.AsyncMock()
        item = stored_item(
            external_links=["https://x/details.php?id=1"],
            signals={"file_count": 3, "file_extensions": ["exe", "mkv"], "size_gb": 4.0},
        )
        with mock.patch("vettarr.searcher.search_service.fetch_linked_torrent", fetch):
            out = costly({"file_count", "file_extensions"}, item=item)
        self.assertEqual(out, {"file_count": 3, "file_extensions": ["exe", "mkv"]})
        fetch.assert_not_awaited()

    def test_a_file_list_that_was_missing_is_fetched(self) -> None:
        fetch = mock.AsyncMock(return_value=None)
        gap = {"missing": "the linked torrent could not be read"}
        item = stored_item(
            external_links=["https://x/details.php?id=1"],
            signals={"file_count": gap, "file_extensions": gap},
        )
        with mock.patch("vettarr.searcher.search_service.fetch_linked_torrent", fetch):
            costly({"file_count", "file_extensions"}, item=item)
        fetch.assert_awaited_once()


# --- TMDB: the language and the key --------------------------------------------


class TmdbLanguageTests(unittest.TestCase):
    def _asked(self, language: str | None):
        seen = []

        def handler(request):
            seen.append(dict(request.url.params))
            if request.url.path.endswith("/search/movie"):
                return httpx.Response(200, json={"results": [{"id": 5, "title": "Some Film"}]})
            return httpx.Response(200, json={"id": 5, "title": "Some Film", "genres": [{"id": 27, "name": "Horror"}]})

        cfg = policy_fixture.config()
        if language is not None:
            tmdb = cfg.integrations.tmdb.model_copy(update={"language": language})
            cfg = cfg.model_copy(update={"integrations": cfg.integrations.model_copy(update={"tmdb": tmdb})})

        async def run():
            client = tmdb_client.TMDBClient()
            client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            return await client.search("Some Film", 2024, MediaType.MOVIE)

        with mock.patch.object(tmdb_client, "settings", policy_fixture.Settings(cfg)):
            result = asyncio.run(run())
        return result, seen

    def test_the_configured_language_is_the_one_asked(self) -> None:
        result, seen = self._asked("fr-FR")
        self.assertEqual(result.genres, ["Horror"])
        self.assertEqual({params["language"] for params in seen}, {"fr-FR"})
        self.assertEqual(len(seen), 2)

    def test_the_default_is_the_documented_one(self) -> None:
        _result, seen = self._asked(None)
        self.assertEqual({params["language"] for params in seen}, {"en-US"})


class TmdbKeyTests(unittest.TestCase):
    KEY = "SECRETKEY123"

    def test_an_error_answer_never_logs_the_key(self) -> None:
        for status in (401, 429, 503):
            with self.subTest(status=status):
                self._one(status)

    def _one(self, status: int) -> None:
        async def run():
            client = tmdb_client.TMDBClient()
            client.__dict__["_api_key"] = self.KEY
            client._client = httpx.AsyncClient(
                transport=httpx.MockTransport(lambda _request: httpx.Response(status))
            )
            return await client.search("Some Film", 2024, MediaType.MOVIE)

        logging.disable(logging.NOTSET)
        self.addCleanup(logging.disable, logging.CRITICAL)
        with self.assertLogs(tmdb_client.log, level="DEBUG") as logs:
            result = asyncio.run(run())
        self.assertIsNone(result.tmdb_id)
        formatter = logging.Formatter()
        text = "\n".join(formatter.format(record) for record in logs.records)
        self.assertNotIn(self.KEY, text)
        self.assertIn(f"HTTP {status}", text)


# --- in_library is a film's and a series' ---------------------------------------


class InLibraryTypesTests(unittest.TestCase):
    RULE = {"id": "have-it", "check": "in_library", "is": True, "on_hit": "reject"}

    def test_a_rule_without_types_does_not_reach_books(self) -> None:
        block = policy(self.RULE, {"id": "s", "check": "size_gb", "at_least": 50})
        self.assertEqual([r.id for r in engine.applicable_rules(block, "movie")], ["have-it", "s"])
        for media_type in ("ebook", "audiobook", "comic", "magazine", "music"):
            with self.subTest(media_type=media_type):
                self.assertEqual([r.id for r in engine.applicable_rules(block, media_type)], ["s"])
                decision = engine.evaluate(movie(media_type=media_type), {"size_gb": 1}, block, now=NOW)
                self.assertEqual(decision.outcome, APPROVE)

    def test_naming_a_book_type_is_an_error_at_load(self) -> None:
        with self.assertRaises(ValidationError) as caught:
            rule(**{**self.RULE, "types": ["ebook"]})
        self.assertIn("in_library applies to movie, series", str(caught.exception))


# --- the re-run ------------------------------------------------------------------


def reasons(*pairs) -> list[dict]:
    return [Reason(kind, rule_id, text).to_json() for kind, rule_id, text in pairs]


OFF = reasons(("setting", None, "auto-approval is switched off"))
SHADOW = reasons(("shadow", None, "shadow mode: would have approved"), ("clear", None, "no rule hit"))
NEVER = reasons(("type", None, "ebook is always reviewed by hand"))
FLAG = reasons(("rule", "kw", "keyword: x"))


def rerun(mode, rows):
    """Run the re-run under `auto_approve: mode` over the candidate rows the database holds."""
    from test_rerun_pending import FakeSession, conditions

    session = FakeSession(rows)
    calls = []

    async def recheck(limit, *, only_ids, decisions, push_held=True):
        calls.append(sorted(only_ids))
        decisions.extend([REVIEW] * len(only_ids))
        return len(only_ids)

    with (
        mock.patch.object(pipeline, "settings", policy_fixture.settings(enabled=mode)),
        mock.patch.object(pipeline, "async_session", lambda: session),
        mock.patch.object(pipeline, "recheck_waiting_items", recheck),
    ):
        summary = asyncio.run(pipeline.rerun_pending_reviews())
    requeued = sorted(conditions(session.updates()[0])["id"][1]) if session.updates() else []
    return summary, session, requeued, calls


class RerunGuardTests(unittest.TestCase):
    """The fixture policy: films and books in mode rules, music never, magazines always."""

    def _policy(self, mode):
        return policy_fixture.config(enabled=mode).policy

    def test_nothing_is_worth_rerunning_while_the_switch_is_off(self) -> None:
        block = self._policy("off")
        for stored in (OFF, SHADOW, NEVER):
            self.assertFalse(engine.worth_rerunning(stored, None, "old", MediaType.MOVIE, block))

    def test_in_shadow_only_what_could_change_is_rerun(self) -> None:
        block = self._policy("shadow")
        same, other = engine.policy_version(block), "an-older-policy"
        film = MediaType.MOVIE
        self.assertTrue(engine.worth_rerunning(OFF, None, same, film, block))
        self.assertFalse(engine.worth_rerunning(SHADOW, None, same, film, block))
        self.assertTrue(engine.worth_rerunning(SHADOW, None, other, film, block))
        self.assertFalse(engine.worth_rerunning(FLAG, None, other, film, block))

    def test_with_the_switch_on_a_shadow_review_is_rerun(self) -> None:
        block = self._policy("on")
        same = engine.policy_version(block)
        self.assertTrue(engine.worth_rerunning(SHADOW, None, same, MediaType.MOVIE, block))
        self.assertTrue(engine.worth_rerunning(OFF, None, same, MediaType.MOVIE, block))

    def test_a_type_review_is_rerun_only_once_the_type_is_no_longer_held(self) -> None:
        block = self._policy("on")
        for version in (engine.policy_version(block), "an-older-policy", None):
            with self.subTest(version=version):
                # music is `never`: held again, whatever else changed in the file.
                self.assertFalse(engine.worth_rerunning(NEVER, None, version, MediaType.MUSIC, block))
                # A film was held by its type under an older file; films have rules now.
                self.assertTrue(engine.worth_rerunning(NEVER, None, version, MediaType.MOVIE, block))
                self.assertTrue(engine.worth_rerunning(NEVER, None, version, "magazine", block))
        no_rules = policy({"id": "v", "check": "imdb.violence", "at_least": "severe"})
        self.assertFalse(engine.worth_rerunning(NEVER, None, None, MediaType.EBOOK, no_rules))

    def _rerun(self, mode, rows):
        return rerun(mode, rows)

    def test_off_reads_and_requeues_nothing_and_says_why(self) -> None:
        rows = [(1, OFF, None, None, MediaType.MOVIE), (2, SHADOW, None, None, MediaType.MOVIE)]
        summary, session, requeued, calls = self._rerun("off", rows)
        self.assertEqual((session.statements, requeued, calls), ([], [], []))
        self.assertIn("auto-approval is off", summary.text())
        self.assertIn("shadow or on", summary.text())

    def test_shadow_does_not_requeue_its_own_reviews(self) -> None:
        version = engine.policy_version(self._policy("shadow"))
        film = MediaType.MOVIE
        rows = [
            (1, SHADOW, None, version, film), (2, SHADOW, None, version, film),
            (3, NEVER, None, version, MediaType.MUSIC),
            (4, OFF, None, version, film), (5, SHADOW, None, "an-older-policy", film),
        ]
        summary, _session, requeued, calls = self._rerun("shadow", rows)
        self.assertEqual(requeued, [4, 5])
        self.assertEqual(calls, [[4, 5]])
        self.assertEqual(summary.requeued, 2)

    def test_a_type_still_held_is_not_requeued_on_every_run(self) -> None:
        for version in (engine.policy_version(self._policy("on")), "an-older-policy"):
            with self.subTest(version=version):
                rows = [(1, NEVER, None, version, MediaType.MUSIC)]
                summary, _session, requeued, _calls = self._rerun("on", rows)
                self.assertEqual((requeued, summary.requeued), ([], 0))

    def test_an_item_left_in_the_queue_is_not_announced_again(self) -> None:
        import test_recheck_waiting as harness
        from vettarr.checker.policy import Applied

        drain()
        self.addCleanup(drain)
        held = (Reason("shadow", None, "shadow mode: would have approved"),)
        verdict = pipeline.Verdict(Decision(APPROVE, held[1:], "v1"), Applied(REVIEW, held, shadow=True), {})
        for push_held, expected in ((False, [events.DECISION, events.WOULD_HAVE]),
                                    (True, [events.DECISION, events.WOULD_HAVE, events.NEEDS_YOU])):
            with self.subTest(push_held=push_held):
                session = harness.ScriptedSession(harness.make_item())
                with (
                    mock.patch.object(pipeline, "_decide", mock.AsyncMock(return_value=verdict)),
                    mock.patch.object(pipeline, "held_for_review", mock.AsyncMock()),
                ):
                    harness.run_recheck(session, push_held=push_held)
                self.assertEqual([kind for _at, kind, _id, _payload in drain()], expected)


class LegacyRowTests(unittest.TestCase):
    """A row 0.11 decided has no structured reasons, only its own words."""

    OFF_WORDS = ["auto-approval is switched off"]
    TYPE_WORDS = ["ebook is not in the auto-approve list"]

    def test_the_two_settings_0_11_wrote_are_read_as_settings(self) -> None:
        self.assertEqual([r.kind for r in engine.legacy_reasons(None, self.OFF_WORDS)], ["setting"])
        self.assertEqual([r.kind for r in engine.legacy_reasons(None, self.TYPE_WORDS)], ["type"])
        self.assertEqual(
            [r.kind for r in engine.legacy_reasons(None, ["flagged keyword: x", "could not classify"])],
            ["legacy", "legacy"],
        )
        self.assertTrue(engine.is_settings_review(None, self.OFF_WORDS))
        self.assertTrue(engine.is_settings_review(None, self.TYPE_WORDS))

    def test_structured_reasons_win_over_the_old_words(self) -> None:
        self.assertFalse(engine.is_settings_review(FLAG, self.OFF_WORDS))

    def test_the_rerun_takes_them_once_the_switch_is_on(self) -> None:
        book, film = MediaType.EBOOK, MediaType.MOVIE
        rows = [
            (1, None, self.OFF_WORDS, None, film), (2, None, self.TYPE_WORDS, None, book),
            (3, None, ["flagged keyword: x"], None, film), (4, None, None, None, film), (5, None, [], None, film),
            # Its type is still held: 0.11 never took music, and the file says `never`.
            (6, None, ["music is not in the auto-approve list"], None, MediaType.MUSIC),
        ]
        _summary, _session, requeued, _calls = rerun("on", rows)
        self.assertEqual(requeued, [1, 2])


# --- a title word is not a release tag --------------------------------------------


class TitleWordsAreNotTagsTests(unittest.TestCase):
    def _signals(self, title, raw, **fields):
        return gather.release_signals(movie(title=title, raw_text=raw, **fields))

    def test_the_film_cam_is_not_a_cam_copy(self) -> None:
        out = self._signals("Cam", "Cam (2018) 1080p WEB-DL x264")
        self.assertEqual(out["release_source"], ["WEB-DL"])
        self.assertEqual(out["codec"], ["h264"])

    def test_so_the_catalogue_s_own_example_rule_does_not_reject_it(self) -> None:
        block = policy({"id": "no-cam", "check": "release_source", "any_of": ["CAM", "TS", "TC", "SCR"],
                        "on_hit": "reject"})
        item = movie(title="Cam", raw_text="Cam (2018) 1080p WEB-DL x264")
        self.assertEqual(engine.evaluate(item, gather.release_signals(item), block, now=NOW).outcome, APPROVE)

    def test_a_cam_copy_of_the_film_cam_is_still_one(self) -> None:
        self.assertEqual(self._signals("Cam", "Cam.2018.CAM.x264-GRP")["release_source"], ["CAM"])
        self.assertEqual(self._signals("Cam", "Title: Cam\nYear: 2018", source="CAM")["release_source"], ["CAM"])

    def test_a_language_in_the_title_is_not_the_audio(self) -> None:
        out = self._signals("The Italian Job", "The Italian Job (2003) 1080p BluRay x264 Spanish")
        self.assertEqual(out["audio_language"], ["es"])
        out = self._signals("The English Patient", "The.English.Patient.1996.1080p.BluRay.x264-GRP")
        self.assertIn("missing", out["audio_language"])
        self.assertEqual(out["release_source"], ["BluRay"])

    def test_tags_are_still_read_when_the_title_is_not_in_the_post(self) -> None:
        out = self._signals("Example", "Some.Film.2024.TS.XviD.ITALIAN-GRP")
        self.assertEqual((out["release_source"], out["codec"], out["audio_language"]), (["TS"], ["xvid"], ["it"]))


# --- a policy rejection can be overruled --------------------------------------------


class OverruleAPolicyRejectionTests(unittest.TestCase):
    def setUp(self) -> None:
        import test_actions as harness

        self.harness = harness
        self.store = harness.Store()
        for module in (actions, review):
            patcher = mock.patch.object(module, "async_session", self.store.session)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _rejected(self, item_id, **fields):
        return self.harness.make_item(
            self.store, item_id, status=ItemStatus.REJECTED,
            error_message="Rejected by policy: imdb.violence: severe", **fields,
        )

    def test_a_person_can_approve_what_the_policy_rejected(self) -> None:
        item = self._rejected(1, policy_outcome="reject", reviewed_at=None)
        result = asyncio.run(actions.approve(1))
        self.assertTrue(result.ok, result.message)
        self.assertEqual(item.status, ItemStatus.APPROVED)
        self.assertIsNotNone(item.reviewed_at)
        self.assertIsNone(item.error_message)

    def test_a_person_s_own_rejection_stays_final(self) -> None:
        by_hand = self._rejected(1, policy_outcome="review", reviewed_at=NOW)
        overruled_then_rejected = self._rejected(2, policy_outcome="reject", reviewed_at=NOW)
        old_row = self._rejected(3, policy_outcome=None, reviewed_at=None)
        for item in (by_hand, overruled_then_rejected, old_row):
            with self.subTest(item=item.id):
                self.assertTrue(asyncio.run(actions.approve(item.id)).conflict)
                self.assertEqual(item.status, ItemStatus.REJECTED)

    def test_the_item_page_offers_it(self) -> None:
        import pathlib
        import tempfile

        from fastapi.testclient import TestClient

        from vettarr.models import Recommendation
        from vettarr.web import app as web_app
        from vettarr.web import cards

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        real_path = pathlib.Path

        def _path(*args):
            return real_path(tmp.name) if args == ("/data/photos",) else real_path(*args)

        with mock.patch.object(web_app, "Path", _path):
            client = TestClient(web_app.create_web_app(ingester=None))

        def page(**fields):
            item = self._rejected(9, auto_recommendation=Recommendation.REJECT, **fields)
            found = mock.AsyncMock(return_value=cards.detail_of(item))
            with mock.patch.object(cards, "load_detail", found):
                resp = client.get("/item/9")
            self.assertEqual(resp.status_code, 200)
            return resp.text

        self.assertIn("Approve anyway", page(policy_outcome="reject", reviewed_at=None))
        self.assertNotIn("Approve anyway", page(policy_outcome="reject", reviewed_at=NOW))
        self.assertNotIn("Approve anyway", page(policy_outcome="review", reviewed_at=None))

    def test_the_doc_says_which_rejection_is_final(self) -> None:
        self.assertIn("A policy rejection can be overruled, a person's cannot", DOC.read_text())


# --- the smaller ones ------------------------------------------------------------


class EmptyListTests(unittest.TestCase):
    LISTS = {"empty": []}

    def test_a_rule_on_an_empty_list_never_hits(self) -> None:
        for value in (["u.s. marshal"], ["a.i."], ["friendship"]):
            self.assertIs(hit({"check": "keyword", "matches_any": "empty"}, value, self.LISTS), False)
        self.assertIs(hit({"check": "genre", "none_of": "empty"}, [18, "Drama"], self.LISTS), False)
        self.assertIs(hit({"check": "genre", "any_of": "empty"}, [18, "Drama"], self.LISTS), False)
        self.assertIs(hit({"check": "certification", "none_of": "empty"}, {"US": "R"}, self.LISTS), False)
        self.assertIs(hit({"check": "resolution", "none_of": "empty"}, "1080p", self.LISTS), False)

    def test_config_check_names_the_rule(self) -> None:
        block = policy({"id": "kw", "check": "keyword", "matches_any": "empty"}, lists=self.LISTS)
        lines = [text for _loc, text in config_schema.warnings(with_policy(block))]
        self.assertTrue(any("rule kw points at the empty list 'empty'" in line for line in lines), lines)


class LooseWordsTests(unittest.TestCase):
    def test_a_phrase_matches_its_hyphenated_spelling(self) -> None:
        match = {"check": "keyword", "matches_any": ["sea monster", "time-travel"]}
        self.assertTrue(hit(match, ["sea-monster"]))
        self.assertTrue(hit(match, ["time travel"]))
        self.assertTrue(hit(match, ["based on novel: sea monster"]))
        self.assertFalse(hit(match, ["seamonster"]))
        self.assertFalse(hit(match, ["monster"]))

    def test_any_of_still_compares_whole_values_as_written(self) -> None:
        self.assertFalse(hit({"check": "source", "any_of": ["feed-a"]}, ["feed_a"]))
        self.assertTrue(hit({"check": "release_source", "any_of": ["web-dl"]}, ["WEB-DL"]))


class ApproveRuleOrderTests(unittest.TestCase):
    def test_config_check_warns_when_an_approve_rule_comes_first(self) -> None:
        first = {"id": "trusted", "check": "source", "any_of": ["feed"], "on_hit": "approve"}
        after = {"id": "violence", "check": "imdb.violence", "at_least": "severe", "on_hit": "reject"}
        lines = [t for _l, t in config_schema.warnings(with_policy(policy(first, after)))]
        self.assertTrue(any("violence comes after the approve rule trusted" in line for line in lines), lines)
        lines = [t for _l, t in config_schema.warnings(with_policy(policy(after, first)))]
        self.assertFalse(any("comes after the approve rule" in line for line in lines), lines)

    def test_rules_for_different_types_do_not_warn(self) -> None:
        first = {"id": "mags", "check": "source", "any_of": ["feed"], "types": ["magazine"], "on_hit": "approve"}
        after = {"id": "violence", "check": "imdb.violence", "at_least": "severe"}
        lines = [t for _l, t in config_schema.warnings(with_policy(policy(first, after)))]
        self.assertFalse(any("comes after the approve rule" in line for line in lines), lines)


class WrongMatchTests(unittest.TestCase):
    WRONG = {"_identity": {"ok": False, "text": "the match is a sequel"}}

    def test_a_rule_read_from_the_tmdb_record_trusts_no_wrong_match(self) -> None:
        for check, rule_fields, value in (
            ("in_library", {"is": True}, True),
            ("status", {"any_of": ["ended"]}, ["ended"]),
            ("episode_count", {"at_least": 5}, 10),
        ):
            with self.subTest(check=check):
                block = policy({"id": "x", "check": check, "on_hit": "reject", **rule_fields})
                decision = engine.evaluate(movie(media_type="series"), {**self.WRONG, check: value}, block, now=NOW)
                self.assertEqual(decision.outcome, REVIEW)
                self.assertEqual(decision.reasons[0].kind, "identity")

    def test_a_rule_read_from_the_post_alone_does_not_ask(self) -> None:
        block = policy({"id": "big", "check": "size_gb", "at_least": 50})
        decision = engine.evaluate(movie(), {**self.WRONG, "size_gb": 4}, block, now=NOW)
        self.assertEqual(decision.outcome, APPROVE)


class GuardEventTests(unittest.TestCase):
    def setUp(self) -> None:
        drain()
        self.addCleanup(drain)

    def _kinds(self, status):
        guard = TrackerGuard("tracker-a", clock=lambda: 0.0)
        for _ in range(guard.error_limit):
            guard.note_request_error(status)
        return [kind for _at, kind, _id, _payload in drain()], guard

    def test_a_run_of_server_errors_is_not_an_expired_cookie(self) -> None:
        kinds, guard = self._kinds(503)
        self.assertEqual(kinds, [events.GUARD])
        self.assertIsNotNone(guard.blocked_reason())

    def test_a_run_of_refusals_still_is(self) -> None:
        kinds, _guard = self._kinds(403)
        self.assertEqual(kinds, [events.GUARD, events.COOKIE_EXPIRED])


class BotAddedEventTests(unittest.TestCase):
    def setUp(self) -> None:
        drain()
        self.addCleanup(drain)

    def test_an_item_from_a_bot_source_raises_the_event(self) -> None:
        import test_sources as harness

        post = "Some.Film.2024.1080p.WEB-DL.x264-GRP\n4.2 GB"
        with harness.Harness() as h:
            added = h.run("inbox", ingest.Incoming(harness.INBOX_USER_CHAT, 10, post, harness.REVIEWER))
            from_bot = drain()
            h.run("films", ingest.Incoming(harness.GROUP, 11, "Other.Film.2023.1080p.WEB-DL.x264-GRP"))
            from_userbot = drain()
        self.assertEqual(added.outcome, "added")
        self.assertEqual([(kind, item_id) for _at, kind, item_id, _payload in from_bot], [(events.BOT_ADDED, 77)])
        payload = from_bot[0][3]
        self.assertEqual((payload["source"], payload["read_as"]), ("inbox", "post"))
        self.assertNotIn("Some.Film", str(payload))
        self.assertEqual(from_userbot, [])


class ShadowEventDocTests(unittest.TestCase):
    def test_the_doc_says_every_shadow_decision_raises_would_have(self) -> None:
        doc = DOC.read_text()
        self.assertIn("Every decision made in `shadow` raises a `would_have` event", doc)
        self.assertNotIn('"shadow mode: would have approved", and a `would_have` event', doc)


if __name__ == "__main__":
    unittest.main()
