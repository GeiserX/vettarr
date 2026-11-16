"""A source of kind `abreq`: AudioBookRequest's requests, decided by the policy.

AudioBookRequest (ABR) users ask for audiobooks; a user it does not trust
cannot start a download. An abreq source makes each book that waits in ABR
an item, runs the policy on it and carries the decision out in ABR: an
approved book is downloaded by ABR (`auto-download`), a rejected request is
deleted. These tests drive the whole path (the webhook route, the poll, the
pipeline, the search worker, the download check and a reviewer's own approve
and reject) against the fake ABR of `delivery_fakes` and the in-memory store
of `test_actions`, which runs the real SQL statements.
"""

import asyncio
import logging
import os
import pathlib
import tempfile
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

import httpx
from fastapi.testclient import TestClient

import policy_fixture
from delivery_fakes import API_KEY, FakeAbreq
from test_seerr_source import _Store, drain
from test_web_foundation import HASH

from vettarr import (
    abreq_source,
    actions,
    delivery,
    events,
    ingest,
    pipeline,
    review,
    scheduler,
    search_jobs,
)
from vettarr.checker.book_client import BookVerdict
from vettarr.config import settings
from vettarr.config.components import components
from vettarr.config.loader import ConfigError, load_text
from vettarr.config.schema import AudioBookRequest, Config, Source, warnings
from vettarr.delivery import abreq as abreq_api
from vettarr.delivery.abreq import AbreqClient
from vettarr.delivery.http import DeliveryError
from vettarr.models import Download, DownloadStatus, ItemStatus, MediaItem, MediaType
from vettarr.web import app as web_app
from vettarr.web import cards, connections, shell

SECRET = "a-long-shared-secret-0123456789"
KEY = "audiobooks"
HOOK = f"/hooks/abreq/{KEY}"
BOOK, OTHER = "B0ABCDEF12", "B0ZZZZZZ99"
T0 = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=1)
#: What the catalogue says of a book no rule minds.
PLAIN = BookVerdict(matched=True, source="openlibrary", subjects=["History", "Travel", "Biography"])


def run(coro):
    return asyncio.run(coro)


def config(mode: str = "on", *, rejecting: bool = False, dry_run: bool = False, **source) -> Config:
    """The fixture's operator-shaped policy, with one abreq source and ABR on.

    ``rejecting`` turns the subject-c rule into a rejection, so the policy
    rejects some audiobooks.
    """
    cfg = policy_fixture.config(enabled=mode)
    if rejecting:
        rules = [r.model_copy(update={"on_hit": "reject"}) if r.id == "subject-c" else r for r in cfg.policy.rules]
        cfg = cfg.model_copy(update={"policy": cfg.policy.model_copy(update={"rules": rules})})
    abreq = Source.model_validate({"key": KEY, "kind": "abreq", "webhook_secret": SECRET, **source})
    integrations = cfg.integrations.model_copy(update={
        "abreq": AudioBookRequest.model_validate({"url": "http://abr:8000", "api_key": API_KEY}),
    })
    return cfg.model_copy(update={
        "sources": [*cfg.sources, abreq],
        "integrations": integrations,
        # Audiobooks have a target of their own, so an item that follows
        # `deliver` instead of its request is seen at once.
        "deliver": {"audiobook": "qbittorrent"},
        "dry_run": dry_run,
    })


class SourceCase(unittest.TestCase):
    MODE = "on"
    REJECTING = False
    SOURCE: dict = {}

    def setUp(self) -> None:
        self.store = _Store()
        self.abr = FakeAbreq()
        self.cfg = config(self.MODE, rejecting=self.REJECTING, **self.SOURCE)
        self.source = next(s for s in self.cfg.sources if s.key == KEY)
        self.client = AbreqClient("http://abr:8000", API_KEY, http=self.abr.http())
        self.queue: asyncio.Queue[int] = asyncio.Queue()
        self.posts = mock.AsyncMock()
        #: What the catalogue answers; a test swaps it to steer the policy.
        self.book = PLAIN
        self.now = [1000.0]

        async def insert(values):
            for row in self.store.all(MediaItem):
                same_message = (row.telegram_chat_id, row.telegram_message_id) == (
                    values["telegram_chat_id"], values["telegram_message_id"],
                )
                if same_message or row.origin_key == values["origin_key"]:
                    return None
            item = MediaItem(**values)
            item.created_at = T0 + timedelta(seconds=len(self.store.all(MediaItem)))
            return self.store.put(item).id

        async def check_book(title, *_args):
            return self.book

        self.check_book = mock.AsyncMock(side_effect=check_book)
        self.add_direct = mock.AsyncMock(return_value=None)
        self.search_and_download = mock.AsyncMock(return_value="downloading")
        failing = SimpleNamespace(
            search=mock.AsyncMock(side_effect=AssertionError("an audiobook was looked up on TMDB")),
            by_id=mock.AsyncMock(side_effect=AssertionError("an audiobook was looked up on TMDB")),
        )
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data = pathlib.Path(tmp.name)
        for patcher in (
            *(mock.patch.object(module, "async_session", self.store.session)
              for module in (delivery, search_jobs, scheduler, review, actions, pipeline, abreq_source, ingest)),
            mock.patch.object(settings, "_config", self.cfg),
            mock.patch.object(
                delivery, "client_for", lambda target, config=None: self.client if target == "abreq" else None,
            ),
            mock.patch.object(delivery, "_clock", lambda: self.now[0]),
            mock.patch.dict(delivery._retries, clear=True),
            mock.patch.object(ingest, "_insert", insert),
            mock.patch.object(ingest, "TORRENTS_DIR", self.data / "torrents"),
            mock.patch.object(pipeline, "check_book", self.check_book),
            mock.patch.object(pipeline, "tmdb_client", failing),
            mock.patch.object(pipeline, "_check_duplicate", mock.AsyncMock(return_value=None)),
            mock.patch.object(pipeline, "_check_in_flight_twin", mock.AsyncMock(return_value=None)),
            mock.patch.object(search_jobs, "add_direct", self.add_direct),
            mock.patch.object(search_jobs, "search_and_download", self.search_and_download),
            mock.patch.object(scheduler, "qbt", SimpleNamespace(get_torrent_progress=mock.AsyncMock(return_value=None))),
            mock.patch.dict(scheduler._unanswered, clear=True),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        ingest.set_queue(self.queue)
        self.addCleanup(ingest.set_queue, None)
        drain()
        self.addCleanup(drain)

    # --- what the app does, step by step ---

    def ask(self, asin: str = BOOK, **fields) -> str:
        self.abr.ask(asin, **fields)
        return asin

    def poll(self) -> abreq_source.Polled:
        return run(abreq_source.poll(self.source))

    def decide(self) -> None:
        while not self.queue.empty():
            run(pipeline.process_item(self.queue.get_nowait()))

    def work(self) -> list:
        refs = run(search_jobs._claim_approved_items(3))
        for ref in refs:
            run(search_jobs._process_item(ref, self.posts))
        return refs

    def check_downloads(self) -> None:
        run(scheduler._check_downloads(self.posts))

    def taken(self, asin: str = BOOK, **ask) -> MediaItem:
        """A book asked for, taken by the poll and decided by the policy. Its item."""
        self.ask(asin, **ask)
        self.poll()
        self.decide()
        return self.item(asin)

    # --- what is looked at ---

    def item(self, asin: str = BOOK) -> MediaItem | None:
        key = abreq_api.request_key(KEY, asin)
        found = [i for i in self.store.all(MediaItem) if i.origin_key == key]
        self.assertLessEqual(len(found), 1)
        return found[0] if found else None

    def download(self, item: MediaItem) -> Download | None:
        return next((d for d in self.store.all(Download) if d.media_item_id == item.id), None)

    def reasons(self, item: MediaItem) -> list[str]:
        return [r["text"] for r in item.reasons or [] if r["kind"] == delivery.REASON_KIND]

    def grabs(self) -> list:
        return self.abr.sent("POST")

    def deletes(self) -> list:
        return self.abr.sent("DELETE")

    def held(self) -> None:
        """The catalogue files the book under a subject the policy holds for a person."""
        self.book = BookVerdict(matched=True, source="openlibrary", subjects=["Beekeeping", "Nature", "Hobbies"])

    def refused(self) -> None:
        """... or under one the rejecting policy refuses."""
        self.book = BookVerdict(matched=True, source="openlibrary", subjects=["Origami", "Paper", "Crafts"])

    def assert_download_client_untouched(self) -> None:
        self.add_direct.assert_not_awaited()
        self.search_and_download.assert_not_awaited()


# ---------------------------------------------------------------------------
# The client and the keys
# ---------------------------------------------------------------------------


class ClientTests(SourceCase):
    def test_the_key_goes_as_a_bearer_token_and_one_call_reads_the_list(self) -> None:
        self.ask(subtitle="A Tale")
        listed = run(self.client.wishlist())
        self.assertEqual(list(listed), [BOOK])
        [call] = self.abr.calls
        self.assertEqual((call.path, call.params), ("/api/requests", {"filter": "all"}))
        self.assertEqual(call.headers["authorization"], f"Bearer {API_KEY}")
        self.assertNotIn(API_KEY, call.url)

    def test_the_flag_is_read_under_either_name(self) -> None:
        for v2 in (False, True):
            with self.subTest(v2=v2):
                self.abr.v2 = v2
                self.ask()
                self.abr.books[BOOK]["downloaded"] = True
                entry = run(self.client.entry(BOOK))
                self.assertIn("fulfilled" if v2 else "downloaded", entry["book"])
                self.assertTrue(abreq_api.fulfilled(entry))
                self.abr.books[BOOK]["downloaded"] = False
                self.assertFalse(abreq_api.fulfilled(run(self.client.entry(BOOK))))

    def test_auto_download_waits_longer_than_other_calls(self) -> None:
        self.ask()
        seen = {}
        real = self.client._api._http.request

        async def spy(*args, **kwargs):
            seen[args[1].rsplit("/", 1)[-1]] = kwargs.get("timeout")
            return await real(*args, **kwargs)

        with mock.patch.object(self.client._api._http, "request", spy):
            run(self.client.auto_download(BOOK))
            run(self.client.wishlist())
        self.assertEqual(seen["auto-download"], abreq_api.AUTO_DOWNLOAD_SECONDS)
        self.assertIsNone(seen["requests"])

    def test_a_redirect_to_the_prowlarr_settings_is_an_error_and_is_not_followed(self) -> None:
        self.ask()
        self.abr.prowlarr = False
        with self.assertRaises(DeliveryError) as caught:
            run(self.client.auto_download(BOOK))
        self.assertIn("no working Prowlarr", str(caught.exception))
        self.assertEqual(len(self.abr.calls), 1)

    def test_the_test_says_whether_the_key_is_an_admin_s(self) -> None:
        self.assertEqual(run(self.client.test()), "unknown")
        self.abr.group = "trusted"
        with self.assertRaises(DeliveryError) as caught:
            run(self.client.test())
        self.assertIn("needs an admin", str(caught.exception))
        self.abr.accepts = "another-key-0123456789"
        with self.assertRaises(DeliveryError) as caught:
            run(self.client.test())
        self.assertEqual(caught.exception.status, 401)

    def test_connections_test_it_through_the_delivery_client(self) -> None:
        result = run(connections.test("abreq", self.cfg))
        self.assertEqual(result, connections.Result(True, "Connected: AudioBookRequest accepted the API key."))

    def test_the_keys_of_an_item(self) -> None:
        key = abreq_api.request_key(KEY, BOOK)
        self.assertEqual(key, f"abreq:{KEY}:{BOOK}")
        self.assertEqual(abreq_api.request_of(key), (KEY, BOOK))
        self.assertEqual(abreq_api.request_of(abreq_api.retired_key(key, 9)), (KEY, BOOK))
        self.assertTrue(abreq_api.retired(abreq_api.retired_key(key, 9)))
        self.assertFalse(abreq_api.retired(key))
        for other in (None, "", "seerr:requests:31", f"abreq:{KEY}:short", f"abreq:{KEY}:{BOOK}:x", "-1:5"):
            with self.subTest(other=other):
                self.assertIsNone(abreq_api.request_of(other))
        self.assertEqual(delivery.request_kind(key), delivery.ABREQ)
        self.assertEqual(delivery.request_kind("seerr:requests:31"), delivery.SEERR)
        self.assertIsNone(delivery.request_kind("-100:5"))


# ---------------------------------------------------------------------------
# The webhook route
# ---------------------------------------------------------------------------


class HookCase(SourceCase):
    def setUp(self) -> None:
        super().setUp()
        banner = mock.patch.object(shell, "failing", mock.AsyncMock(return_value=[]))
        banner.start()
        self.addCleanup(banner.stop)
        real_path = pathlib.Path

        def _path(*args):
            if args and str(args[0]).startswith("/data/"):
                return self.data / str(args[0])[len("/data/"):]
            return real_path(*args)

        with mock.patch.object(web_app, "Path", _path):
            self.app = web_app.create_web_app(
                self.posts, Config.model_validate({"web": {"port": 4850, "auth": {"password_hash": HASH}}}),
            )
        self.http = TestClient(self.app)

    def send(self, payload, *, secret: str | None = SECRET, url: str = HOOK):
        headers = {"Authorization": secret} if secret is not None else {}
        return self.http.post(url, json=payload, headers=headers, follow_redirects=False)


class WebhookTests(HookCase):
    def test_a_new_request_answers_at_once_and_makes_the_item_after(self) -> None:
        self.ask(user="reader-with-2160p-in-the-name", subtitle="A Tale of Hives")
        resp = self.send({"event": "onNewRequest", "asin": BOOK})
        self.assertEqual((resp.status_code, resp.json()), (200, {"result": "queued"}))
        item = self.item()
        self.assertEqual(
            (item.title, item.year, item.media_type, item.source_key, item.sender_id, item.status),
            ("Some Book - Ann Author", 2019, MediaType.AUDIOBOOK, KEY, None, ItemStatus.PENDING_PARSE),
        )
        self.assertEqual(item.telegram_chat_id, abreq_source.ABREQ_CHAT)
        self.assertEqual(item.posted_at, datetime(2026, 11, 10, 9, 0, tzinfo=timezone.utc))
        self.assertIn(f"https://www.audible.com/pd/{BOOK}", item.raw_text)
        self.assertIn("By Ann Author", item.raw_text)
        self.assertIn("A Tale of Hives", item.raw_text)
        # A username is the user's to write, and is no text the policy reads.
        self.assertNotIn("reader", item.raw_text)
        self.assertEqual(self.queue.get_nowait(), item.id)
        self.assertEqual(self.abr.writes(), [])

    def test_the_webhook_and_the_poll_make_one_item(self) -> None:
        self.ask()
        self.send({"event": "onNewRequest", "asin": BOOK})
        self.send({"event": "onNewRequest", "asin": BOOK})
        self.assertEqual(self.poll().added, 0)
        self.assertEqual(len(self.store.all(MediaItem)), 1)

    def test_a_second_user_asking_for_the_same_book_is_the_same_item(self) -> None:
        self.ask()
        self.send({"event": "onNewRequest", "asin": BOOK})
        self.ask(user="someone-else", at="2026-11-10T10:00:00")
        self.send({"event": "onNewRequest", "asin": BOOK})
        self.assertEqual(len(self.store.all(MediaItem)), 1)

    def test_abr_s_test_button_answers_test_and_writes_one_event(self) -> None:
        resp = self.send({"event": "{eventType}", "asin": "{bookASIN}"})
        self.assertEqual((resp.status_code, resp.json()), (200, {"result": "test"}))
        self.assertEqual(drain(), [(events.WEBHOOK_TEST, None, {"source": KEY, "app": "AudioBookRequest"})])
        self.assertEqual(self.abr.calls, [])

    def test_other_events_and_bad_asins_are_ignored(self) -> None:
        self.ask()
        for payload in (
            {"event": "onSuccessfulDownload", "asin": BOOK}, {"event": "onNewRequest", "asin": "not-an-asin"},
            {"event": "onNewRequest"}, [1, 2], {},
        ):
            with self.subTest(payload=payload):
                self.assertEqual(self.send(payload).json(), {"result": "ignored"})
        self.assertEqual(self.abr.calls, [])
        self.assertEqual(self.store.all(MediaItem), [])

    def test_a_wrong_or_missing_secret_is_refused(self) -> None:
        self.ask()
        for secret in ("not-the-secret-0123456789abc", "", None, "Bearer wrong"):
            with self.subTest(secret=secret):
                self.assertEqual(self.send({"event": "onNewRequest", "asin": BOOK}, secret=secret).status_code, 401)
        self.assertEqual(self.send({"event": "{eventType}", "asin": "{bookASIN}"}, secret=f"Bearer {SECRET}").json(),
                         {"result": "test"})
        self.assertEqual(self.abr.calls, [])

    def test_a_key_that_is_no_abreq_source_answers_404(self) -> None:
        for url in ("/hooks/abreq/nobody", "/hooks/seerr/audiobooks"):
            with self.subTest(url=url):
                self.assertEqual(self.send({"event": "onNewRequest", "asin": BOOK}, url=url).status_code, 404)

    def test_an_abr_that_is_down_still_gets_200_and_the_poll_takes_the_book(self) -> None:
        self.ask()
        self.abr.down = httpx.ConnectError
        self.assertEqual(self.send({"event": "onNewRequest", "asin": BOOK}).json(), {"result": "queued"})
        self.assertIsNone(self.item())
        self.abr.down = None
        self.assertEqual(self.poll().added, 1)


class NoSecretTests(HookCase):
    SOURCE = {"webhook_secret": None}

    def test_a_source_with_no_secret_has_no_route(self) -> None:
        self.assertEqual(self.send({"event": "onNewRequest", "asin": BOOK}).status_code, 404)


# ---------------------------------------------------------------------------
# The poll and the policy
# ---------------------------------------------------------------------------


class PolicyTests(SourceCase):
    def test_an_approved_book_is_downloaded_by_abr_and_completes(self) -> None:
        self.abr.releases.add(BOOK)
        item = self.taken()
        self.assertEqual(item.status, ItemStatus.APPROVED)
        # The book lookup was given the author, without which it matches nothing.
        self.check_book.assert_awaited_once()
        self.assertEqual(self.check_book.await_args.args[0], "Some Book - Ann Author")
        self.work()
        self.assertEqual([c.path for c in self.grabs()], [f"/api/requests/{BOOK}/auto-download"])
        self.assertTrue(self.abr.downloaded(BOOK))
        self.assertEqual(item.status, ItemStatus.DOWNLOADING)
        dl = self.download(item)
        self.assertEqual((dl.delivery_target, dl.remote_id, dl.status), ("abreq", BOOK, DownloadStatus.DOWNLOADING))
        self.assertIn("AudioBookRequest started the download", self.reasons(item))
        self.assert_download_client_untouched()
        self.check_downloads()
        self.assertEqual((item.status, dl.status), (ItemStatus.COMPLETED, DownloadStatus.COMPLETED))
        self.assertEqual(self.deletes(), [])

    def test_no_release_comes_back_to_a_person_and_a_second_approval_asks_again(self) -> None:
        item = self.taken()
        self.work()
        self.assertEqual(item.status, ItemStatus.ERROR)
        self.assertIn("found no release", item.error_message)
        self.assertEqual(len(self.grabs()), 1)
        self.abr.releases.add(BOOK)
        self.assertTrue(run(actions.approve(item.id)).ok)
        self.work()
        self.assertEqual((item.status, len(self.grabs())), (ItemStatus.DOWNLOADING, 2))

    def test_prowlarr_s_refusal_and_a_missing_prowlarr_come_back_to_a_person(self) -> None:
        for setup, said in (
            (lambda: setattr(self.abr, "refuse_grab", "Indexer said no"), "Indexer said no"),
            (lambda: setattr(self.abr, "prowlarr", False), "no working Prowlarr"),
        ):
            with self.subTest(said=said):
                asin = OTHER if said == "Indexer said no" else "B0CCCCCC33"
                setup()
                item = self.taken(asin)
                self.work()
                self.assertEqual(item.status, ItemStatus.ERROR)
                self.assertIn(said, item.error_message)
                self.abr.refuse_grab, self.abr.prowlarr = None, True

    def test_a_grab_whose_own_notification_failed_is_still_a_download(self) -> None:
        self.abr.releases.add(BOOK)
        self.abr.fail_after_grab = True
        item = self.taken()
        self.work()
        self.assertEqual(item.status, ItemStatus.DOWNLOADING)

    def test_an_outage_keeps_the_item_approved_and_tries_again(self) -> None:
        self.abr.releases.add(BOOK)
        item = self.taken()
        self.abr.down = httpx.ConnectError
        self.work()
        self.assertEqual(item.status, ItemStatus.APPROVED)
        self.assertIn("AudioBookRequest could not be told yet", item.error_message)
        self.assertEqual(delivery.held_back(), [item.id])
        self.abr.down = None
        self.now[0] += 31
        self.work()
        self.assertEqual(item.status, ItemStatus.DOWNLOADING)

    def test_a_held_book_waits_and_is_pushed_once_when_the_source_asks(self) -> None:
        pushed = mock.AsyncMock()
        source = self.source.model_copy(update={"notify_on_review": True})
        cfg = self.cfg.model_copy(update={"sources": [*(s for s in self.cfg.sources if s.key != KEY), source]})
        ingest.set_review_pusher(pushed)
        self.addCleanup(ingest.set_review_pusher, None)
        with mock.patch.object(settings, "_config", cfg), mock.patch.object(ingest, "reviewers", lambda config=None: [42]):
            self.held()
            item = self.taken()
            self.poll()
        self.assertEqual(item.status, ItemStatus.PENDING_REVIEW)
        pushed.assert_awaited_once()
        self.assertEqual(pushed.await_args.args[0], 42)
        self.assertEqual(self.abr.writes(), [])
        card = cards.Card(item=item)
        self.assertEqual(card.reject_confirm, cards.REJECT_ABREQ_REQUEST)
        self.assertTrue(card.can_rerun)
        self.assertIn("AudioBookRequest request", delivery.where(item))

    def test_a_reviewer_s_approve_downloads_and_reject_deletes(self) -> None:
        self.held()
        self.abr.releases.add(BOOK)
        item = self.taken()
        self.assertEqual(run(actions.approve(item.id)).message,
                         "Approved Some Book - Ann Author. AudioBookRequest is asked to download it next.")
        self.work()
        self.assertTrue(self.abr.downloaded(BOOK))
        # Rejecting it now takes nothing back: ABR cannot.
        result = run(actions.reject(item.id, self.posts))
        self.assertIn("cannot take a download back", result.message)
        self.assertEqual(self.deletes(), [])

        self.ask(OTHER)
        self.poll()
        self.decide()
        other = self.item(OTHER)
        self.assertEqual(cards.Card(item=other).reject_confirm, cards.REJECT_ABREQ_REQUEST)
        result = run(actions.reject(other.id, self.posts))
        self.assertEqual(result.message, f"Rejected. {delivery.ABREQ_DELETED}")
        self.assertEqual([c.path for c in self.deletes()], [f"/api/requests/{OTHER}"])
        self.assertNotIn(OTHER, self.abr.requests)

    def test_a_grab_is_refused_for_an_abr_request(self) -> None:
        self.held()
        item = self.taken()
        refusal = delivery.grab_refusal(item.media_type, origin_key=item.origin_key)
        self.assertIn("AudioBookRequest picks its own release", refusal)
        self.assertFalse(cards._handed(item)[2])

    def test_a_book_downloaded_or_deleted_in_abr_is_written_on_its_item(self) -> None:
        self.held()
        item = self.taken()
        self.ask(OTHER)
        self.poll()
        self.decide()
        other = self.item(OTHER)
        self.abr.books[BOOK]["downloaded"] = True
        self.abr.requests.pop(OTHER)
        self.assertEqual(self.poll().reflected, 2)
        self.assertEqual(item.status, ItemStatus.APPROVED)
        self.assertEqual(other.status, ItemStatus.REJECTED)
        self.assertIn("removed there", other.error_message)
        # The approval is recorded without a second download.
        self.work()
        self.assertEqual((item.status, self.grabs()), (ItemStatus.DOWNLOADING, []))
        self.assertIn("decided in AudioBookRequest: the book was downloaded there", self.reasons(item))

    def test_an_unfinished_item_is_queued_again(self) -> None:
        self.ask()
        self.poll()
        item = self.item()
        ingest._added.discard(item.id)
        while not self.queue.empty():
            self.queue.get_nowait()
        self.assertEqual(self.poll().requeued, 1)

    def test_a_downloaded_book_is_not_taken(self) -> None:
        self.ask()
        self.abr.books[BOOK]["downloaded"] = True
        self.assertEqual(self.poll().added, 0)
        self.assertEqual(self.store.all(MediaItem), [])

    def test_a_poll_is_one_call(self) -> None:
        self.held()
        self.taken()
        self.ask(OTHER)
        self.abr.calls.clear()
        self.poll()
        self.assertEqual([c.path for c in self.abr.calls], ["/api/requests"])


class RejectionTests(SourceCase):
    REJECTING = True

    def test_the_policy_s_rejection_deletes_the_request(self) -> None:
        self.refused()
        item = self.taken()
        self.assertEqual(item.status, ItemStatus.REJECTED)
        self.assertEqual([c.path for c in self.deletes()], [f"/api/requests/{BOOK}"])
        self.assertIn(f"rejected: {delivery.ABREQ_DELETED}", self.reasons(item))
        self.assertFalse(cards.Card(item=item).can_rerun)
        result = run(actions.rerun(item.id))
        self.assertEqual(result.message, actions.RERUN_ABREQ_DELETED)

    def test_a_user_who_asks_again_makes_a_new_item(self) -> None:
        self.refused()
        old = self.taken()
        self.ask(at="2026-11-12T09:00:00")
        self.book = PLAIN
        self.assertEqual(self.poll().added, 1)
        self.decide()
        new = self.item()
        self.assertNotEqual(new.id, old.id)
        self.assertEqual(new.status, ItemStatus.APPROVED)
        self.assertTrue(abreq_api.retired(old.origin_key))
        self.assertEqual(old.telegram_message_id, -old.id)
        # Only the first rejection deleted anything.
        self.assertEqual(len(self.deletes()), 1)
        # The old item is not told anything later, and decides nothing in ABR.
        self.assertEqual(self.poll().added, 0)
        self.assertIsNone(run(delivery.decline_for(old.id, old.origin_key)))
        # A person who approves the old one finds its request gone.
        self.assertTrue(run(actions.approve(old.id)).ok)
        self.abr.releases.add(BOOK)
        self.work()
        self.assertEqual(old.status, ItemStatus.REJECTED)
        self.assertIn("deleted when this item was rejected", old.error_message)

    def test_a_delete_that_did_not_arrive_is_sent_by_the_next_poll_and_no_new_item_is_made(self) -> None:
        self.refused()
        self.ask()
        self.poll()
        self.abr.down = httpx.ConnectError
        self.decide()
        item = self.item()
        self.assertEqual(item.status, ItemStatus.REJECTED)
        self.assertIn(BOOK, self.abr.requests)
        self.abr.down = None
        done = self.poll()
        self.assertEqual((done.added, done.declined), (0, 1))
        self.assertNotIn(BOOK, self.abr.requests)
        self.assertEqual(len(self.store.all(MediaItem)), 1)


class DryRunTests(SourceCase):
    REJECTING = True

    def setUp(self) -> None:
        super().setUp()
        dry = mock.patch.object(settings, "_config", self.cfg.model_copy(update={"dry_run": True}))
        dry.start()
        self.addCleanup(dry.stop)

    def test_nothing_is_sent_and_the_poll_does_not_delete_again(self) -> None:
        self.refused()
        item = self.taken()
        self.assertEqual(item.status, ItemStatus.REJECTED)
        self.assertEqual(self.abr.writes(), [])
        self.assertIn(BOOK, self.abr.requests)
        done = self.poll()
        self.assertEqual((done.added, done.declined), (0, 0))
        self.assertEqual(self.abr.writes(), [])

    def test_an_approval_is_recorded_and_completes_with_nothing_sent(self) -> None:
        self.ask(OTHER)
        self.poll()
        self.decide()
        item = self.item(OTHER)
        self.work()
        self.assertEqual(item.status, ItemStatus.DOWNLOADING)
        self.assertEqual(self.abr.writes(), [])
        self.check_downloads()
        self.assertEqual(item.status, ItemStatus.COMPLETED)


# ---------------------------------------------------------------------------
# The config
# ---------------------------------------------------------------------------

_BASE = """
integrations:
  abreq: {url: http://abr:8000, api_key: k}
web: {port: 4850}
sources:
"""


class ConfigTests(unittest.TestCase):
    def test_an_abreq_source_takes_the_request_defaults(self) -> None:
        cfg = load_text(_BASE + "  - {key: audiobooks, kind: abreq, webhook_secret: 0123456789abcdef}\n", {})
        source = cfg.sources[0]
        self.assertEqual(
            (source.poll_minutes, source.allowed_senders, source.direct_adds, source.backfill, source.chat),
            (5, "anyone", "off", "off", None),
        )
        self.assertEqual([path for path, _text in warnings(cfg) if path[0] == "sources"], [])
        self.assertIn(("abreq-source", "audiobooks: webhook plus a poll every 5 min"), components(cfg))
        self.assertIn(("abreq", "integration"), components(cfg))

    def test_what_an_abreq_source_cannot_have(self) -> None:
        for line, fragment in (
            ("  - {key: a, kind: abreq, chat: -100}\n", "has no chat"),
            ("  - {key: a, kind: abreq, on_reject: delete}\n", "no post to delete"),
            ("  - {key: a, kind: abreq, allowed_senders: reviewers}\n", "allowed_senders must be anyone"),
            ("  - {key: a, kind: abreq, allowed_senders: [7]}\n", "allowed_senders must be anyone"),
            ("  - {key: a, kind: abreq, direct_adds: anyone}\n", "direct_adds must be off"),
            ("  - {key: a, kind: abreq, webhook_secret: short}\n", "at least 16"),
        ):
            with self.subTest(line=line):
                with self.assertRaises(ConfigError) as caught:
                    load_text(_BASE + line, {})
                self.assertIn(fragment, "\n".join(caught.exception.errors))

    def test_it_needs_the_integration_and_warns_about_two_sources(self) -> None:
        with self.assertRaises(ConfigError) as caught:
            load_text("sources:\n  - {key: a, kind: abreq}\n", {})
        self.assertIn("needs integrations.abreq", "\n".join(caught.exception.errors))
        cfg = load_text(_BASE + "  - {key: a, kind: abreq}\n  - {key: b, kind: abreq}\n", {})
        self.assertTrue(any("more than one abreq source" in text for _path, text in warnings(cfg)))

    def test_abreq_is_no_deliver_target(self) -> None:
        with self.assertRaises(ConfigError):
            load_text(_BASE + "  - {key: a, kind: abreq}\ndeliver: {audiobook: abreq}\n", {})

    def test_the_example_file_carries_a_commented_abreq_source_that_validates(self) -> None:
        import textwrap

        example = (pathlib.Path(__file__).resolve().parents[1] / "config.example.yaml").read_text()
        start = example.index("#   - key: audiobooks")
        lines = []
        for line in example[start:].splitlines():
            if not line.startswith("#   ") or (lines and line.startswith("#   - key:")):
                break
            lines.append(line[2:])
        block = "\n".join(lines)
        self.assertIn("kind: abreq", block)
        cfg = load_text(
            "integrations:\n  abreq: {url: 'http://abr:8000', api_key: k}\n"
            "web: {auth: {trusted_header: R}}\nsources:\n" + textwrap.indent(textwrap.dedent(block), "  "),
            env={"ABREQ_WEBHOOK_SECRET": SECRET},
        )
        self.assertEqual((cfg.sources[0].kind, cfg.sources[0].poll_minutes), ("abreq", 5))
        self.assertEqual(warnings(cfg), [])

    def test_the_poll_job_is_scheduled(self) -> None:
        cfg = load_text(_BASE + "  - {key: audiobooks, kind: abreq, poll_minutes: 7}\n", {})
        sched = scheduler.Scheduler()
        with mock.patch.object(sched._scheduler, "add_job") as add_job:
            self.assertEqual(sched.add_seerr_poll_jobs(cfg), 1)
        self.assertEqual(add_job.call_args.args[0], abreq_source.poll_job)
        self.assertEqual(add_job.call_args.kwargs["id"], "abreq_poll_audiobooks")
        self.assertEqual(add_job.call_args.kwargs["minutes"], 7)


if __name__ == "__main__":
    unittest.main()
