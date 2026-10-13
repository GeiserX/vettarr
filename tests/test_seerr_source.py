"""A source of kind `seerr`: Seerr's pending requests, decided by the policy.

Seerr users ask and Seerr admins approve; nothing there approves by rule. A
seerr source makes each pending request an item, runs the policy on it and
carries the decision out in Seerr. These tests drive the whole path (the
webhook route, the poll, the pipeline, the search worker, the download check
and a reviewer's own approve and reject) against the fake Seerr server of
`delivery_fakes`, which answers the routes in Seerr's shapes and records
every call, and the in-memory store of `test_actions`, which runs the real
SQL statements.
"""

import asyncio
import json
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

import delivery_fakes as fakes
import policy_fixture
from delivery_fakes import API_KEY, FakeSeerr
from test_actions import FakeSession, Store
from test_pipeline_auto_approve import full_guide, matched_tmdb
from test_web_foundation import HASH

from vettarr import (
    actions,
    delivery,
    events,
    ingest,
    pipeline,
    review,
    scheduler,
    search_jobs,
    seerr_source,
)
from vettarr.checker import gather
from vettarr.checker.imdb_client import ParentsGuide
from vettarr.checker.tmdb_client import MATCH_WITH_YEAR, TMDBClient
from vettarr.config import settings
from vettarr.config.components import components
from vettarr.config.loader import ConfigError, load_text
from vettarr.config.schema import Config, Seerr, Source, warnings
from vettarr.delivery import seerr as seerr_api
from vettarr.delivery.seerr import SeerrClient
from vettarr.models import (
    OP_RECHECK,
    ContentCheck,
    Download,
    DownloadStatus,
    ItemStatus,
    MediaItem,
    MediaType,
    SeverityRating,
    TorrentSearch,
)
from vettarr.web import app as web_app
from vettarr.web import cards, shell
from vettarr.web.routes import hooks

_ROOT = pathlib.Path(__file__).resolve().parents[1]
SECRET = "a-long-shared-secret-0123456789"
KEY = "requests"
HOOK = f"/hooks/seerr/{KEY}"
FILM, SHOW = 603, 1396
ALICE, BOB = 7, 8
TITLES = {FILM: ("Some Film", 1999), SHOW: ("Some Show", 2008)}
# When the store's items were created: an hour ago, so an item waiting for data
# is inside its 24-hour wait whenever the suite runs. A fixed date here expired
# on 2026-10-12 at 09:00 UTC and turned every wait into a review.
T0 = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=1)


def run(coro):
    return asyncio.run(coro)


def drain() -> list[tuple[str, int | None, dict]]:
    out = []
    while events.pending():
        _at, kind, item_id, payload = events._pending.get_nowait()
        out.append((kind, item_id, payload))
    return out


def config(mode: str = "on", *, user_id: int | None = None, deliver: dict | None = None, **source) -> Config:
    """The fixture's operator-shaped policy, with one seerr source and Seerr on."""
    cfg = policy_fixture.config(enabled=mode)
    seerr_source_ = Source.model_validate({"key": KEY, "kind": "seerr", "webhook_secret": SECRET, **source})
    integrations = cfg.integrations.model_copy(update={
        "seerr": Seerr.model_validate({"url": "http://seerr:5055", "api_key": API_KEY, "user_id": user_id}),
    })
    return cfg.model_copy(update={
        "sources": [*cfg.sources, seerr_source_],
        "integrations": integrations,
        # Every type has a target of its own, so an item that follows
        # `deliver` instead of its request is seen at once.
        "deliver": deliver or {"movie": "qbittorrent", "series": "qbittorrent"},
    })


def default_payload(event: str = "MEDIA_PENDING", request_id: int | None = 31, seasons: str | None = None) -> dict:
    """Seerr's webhook body with its default JSON template, as the agent fills it."""
    if event == "TEST_NOTIFICATION":
        return {
            "notification_type": event, "event": "", "subject": "Test Notification",
            "message": "Check check, 1, 2, 3. Are we coming in clear?", "image": "",
            "media": None, "request": None, "issue": None, "comment": None, "extra": [],
        }
    return {
        "notification_type": event, "event": "New Movie Request", "subject": "Some Film (1999)",
        "message": "A film.", "image": "https://image.tmdb.org/t/p/w600_and_h900_bestv2/x.jpg",
        "media": {
            "media_type": "movie", "tmdbId": str(FILM), "tvdbId": "", "status": "PENDING",
            "status4k": "UNKNOWN",
        },
        "request": {
            "request_id": str(request_id), "requestedBy_email": "", "requestedBy_username": "user-7",
            "requestedBy_avatar": "", "requestedBy_settings_discordId": "",
            "requestedBy_settings_telegramChatId": "",
        },
        "issue": None, "comment": None,
        "extra": [{"name": "Requested Seasons", "value": seasons}] if seasons else [],
    }


def flat_payload(event: str = "MEDIA_PENDING", request_id: int = 31) -> dict:
    """A template an operator flattened: Seerr's variable names at the top."""
    return {
        "notification_type": event, "media_type": "movie", "media_tmdbid": str(FILM),
        "media_tvdbid": "", "request_id": str(request_id), "requestedBy_username": "user-7", "extra": [],
    }


class _Session(FakeSession):
    async def rollback(self) -> None:
        pass


class _Store(Store):
    def session(self):
        return _Session(self)


class SourceCase(unittest.TestCase):
    """The store, the fake Seerr, the pipeline's lookups and the worker's download client, patched in."""

    MODE = "on"
    SOURCE: dict = {}
    USER_ID: int | None = None
    DELIVER: dict | None = None

    def setUp(self) -> None:
        self.store = _Store()
        self.seerr = FakeSeerr()
        self.cfg = config(self.MODE, user_id=self.USER_ID, deliver=self.DELIVER, **self.SOURCE)
        self.source = next(s for s in self.cfg.sources if s.key == KEY)
        self.client = SeerrClient("http://seerr:5055", API_KEY, self.USER_ID, http=self.seerr.http())
        self.queue: asyncio.Queue[int] = asyncio.Queue()
        self.posts = mock.AsyncMock()
        #: What the parents guide answers; a test swaps it to steer the policy.
        self.guide = full_guide()
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

        async def resolve_id(kind, value):
            title, year = TITLES.get(int(value), (f"Film {value}", 2000))
            return ingest.Resolved(title, year, MediaType.SERIES if kind == "tv" else MediaType.MOVIE)

        async def by_id(tmdb_id, media_type):
            title, year = TITLES.get(tmdb_id, (f"Film {tmdb_id}", 2000))
            return matched_tmdb(
                tmdb_id=tmdb_id, imdb_id=f"tt{tmdb_id:07d}", title=title, original_title=title, year=year,
                match_mode=MATCH_WITH_YEAR, exact_title_hits=1,
            )

        async def guide(_imdb_id):
            return self.guide

        self.resolve_id = mock.AsyncMock(side_effect=resolve_id)
        self.tmdb = SimpleNamespace(
            by_id=mock.AsyncMock(side_effect=by_id),
            # A request names its title by id; a search by name could match another film.
            search=mock.AsyncMock(side_effect=AssertionError("a Seerr request was searched for by name")),
        )
        self.add_direct = mock.AsyncMock(return_value=None)
        self.search_and_download = mock.AsyncMock(return_value="downloading")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data = pathlib.Path(tmp.name)
        for patcher in (
            *(mock.patch.object(module, "async_session", self.store.session)
              for module in (delivery, search_jobs, scheduler, review, actions, pipeline, seerr_source, ingest)),
            mock.patch.object(settings, "_config", self.cfg),
            mock.patch.object(
                delivery, "client_for", lambda target, config=None: self.client if target == "seerr" else None,
            ),
            mock.patch.object(delivery, "_clock", lambda: self.now[0]),
            mock.patch.dict(delivery._retries, clear=True),
            mock.patch.object(ingest, "_insert", insert),
            mock.patch.object(ingest, "resolve_id", self.resolve_id),
            mock.patch.object(ingest, "TORRENTS_DIR", self.data / "torrents"),
            mock.patch.object(pipeline, "tmdb_client", self.tmdb),
            mock.patch.object(pipeline, "fetch_parents_guide", guide),
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

    def ask(self, kind: str = "movie", tmdb_id: int = FILM, *, user: int = ALICE, **fields) -> int:
        """A Seerr user asks for a title: a pending request in Seerr. Its id."""
        return self.seerr.add_request(kind, tmdb_id, user=user, **fields)["id"]

    def poll(self) -> seerr_source.Polled:
        return run(seerr_source.poll(self.source))

    def decide(self) -> None:
        """The pipeline, for every item intake queued."""
        while not self.queue.empty():
            run(pipeline.process_item(self.queue.get_nowait()))

    def work(self) -> list:
        """The search worker: claim what is approved and deliver it."""
        refs = run(search_jobs._claim_approved_items(3))
        for ref in refs:
            run(search_jobs._process_item(ref, self.posts))
        return refs

    def check_downloads(self) -> None:
        run(scheduler._check_downloads(self.posts))

    def taken(self, request_id: int | None = None, **ask) -> MediaItem:
        """A request, taken by the poll and decided by the policy. Its item."""
        request_id = request_id or self.ask(**ask)
        self.poll()
        self.decide()
        return self.item(request_id)

    # --- what is looked at ---

    def item(self, request_id: int) -> MediaItem | None:
        key = seerr_api.request_key(KEY, request_id)
        found = [i for i in self.store.all(MediaItem) if i.origin_key == key]
        self.assertLessEqual(len(found), 1)
        return found[0] if found else None

    def download(self, item: MediaItem) -> Download | None:
        return next((d for d in self.store.all(Download) if d.media_item_id == item.id), None)

    def status(self, request_id: int) -> int | None:
        request = self.seerr.requests.get(request_id)
        return request["status"] if request else None

    def decisions(self) -> list[fakes.Call]:
        """The approve and decline calls Seerr received."""
        return [c for c in self.seerr.writes() if c.path.endswith(("/approve", "/decline"))]

    def reasons(self, item: MediaItem) -> list[str]:
        return [r["text"] for r in item.reasons or [] if r["kind"] == delivery.REASON_KIND]

    def assert_download_client_untouched(self) -> None:
        self.add_direct.assert_not_awaited()
        self.search_and_download.assert_not_awaited()

    def severe_violence(self) -> None:
        """The fixture policy rejects a film whose violence is severe."""
        self.guide = full_guide(violence_rating=SeverityRating.SEVERE)

    def moderate_nudity(self) -> None:
        """...and holds one with moderate nudity for a person."""
        self.guide = full_guide(sex_rating=SeverityRating.MODERATE)


# ---------------------------------------------------------------------------
# The webhook route
# ---------------------------------------------------------------------------


class HookCase(SourceCase):
    AUTH: dict | None = {"password_hash": HASH}

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

        web: dict = {"port": 4850}
        if self.AUTH:
            web["auth"] = self.AUTH
        with mock.patch.object(web_app, "Path", _path):
            self.app = web_app.create_web_app(self.posts, Config.model_validate({"web": web}))
        self.http = TestClient(self.app)

    def send(self, payload, *, secret: str | None = SECRET, url: str = HOOK, **headers):
        if secret is not None:
            headers["Authorization"] = secret
        return self.http.post(url, json=payload, headers=headers, follow_redirects=False)


class WebhookTests(HookCase):
    def test_the_default_template_makes_the_item_of_a_pending_request(self) -> None:
        request_id = self.ask(name="alice")
        resp = self.send(default_payload(request_id=request_id))
        self.assertEqual((resp.status_code, resp.json()), (200, {"result": "added"}))
        item = self.item(request_id)
        self.assertEqual(
            (item.title, item.year, item.media_type, item.source_key, item.sender_id),
            ("Some Film", 1999, MediaType.MOVIE, KEY, ALICE),
        )
        self.assertEqual(item.origin_key, f"seerr:{KEY}:{request_id}")
        self.assertEqual(item.status, ItemStatus.PENDING_PARSE)
        self.assertIn(f"https://www.themoviedb.org/movie/{FILM}", item.raw_text)
        # Who asked is the Seerr user's id. Their name is theirs to write, and is no text the policy reads.
        self.assertIn(f"Requested by Seerr user {ALICE}", item.raw_text)
        self.assertNotIn("alice", item.raw_text)
        self.assertEqual(item.posted_at, datetime(2026, 10, 11, 8, request_id, tzinfo=timezone.utc))
        self.assertEqual(self.queue.get_nowait(), item.id)
        # The request was read back from Seerr, as its admin, and nothing was written there.
        [read] = self.seerr.sent("GET", f"/api/v1/request/{request_id}")
        self.assertNotIn("x-api-user", read.headers)
        self.assertEqual(self.seerr.writes(), [])

    def test_a_flat_template_makes_the_same_item(self) -> None:
        request_id = self.ask()
        resp = self.send(flat_payload(request_id=request_id))
        self.assertEqual((resp.status_code, resp.json()), (200, {"result": "added"}))
        item = self.item(request_id)
        self.assertEqual((item.title, item.sender_id), ("Some Film", ALICE))

    def test_no_browser_session_is_asked_for_but_the_rest_of_the_app_still_asks(self) -> None:
        request_id = self.ask()
        self.assertEqual(self.send(default_payload(request_id=request_id)).status_code, 200)
        # The same client, still not signed in, gets nowhere else.
        self.assertEqual(self.http.post("/item/1/approve", follow_redirects=False).status_code, 401)
        self.assertEqual(self.http.get("/", follow_redirects=False).status_code, 303)

    def test_a_wrong_or_missing_secret_is_refused_before_anything_is_read(self) -> None:
        request_id = self.ask()
        for secret in ("not-the-secret-0123456789abc", SECRET[:-1], SECRET + "x", "", None, "Bearer wrong"):
            with self.subTest(secret=secret):
                resp = self.send(default_payload(request_id=request_id), secret=secret)
                self.assertEqual(resp.status_code, 401)
        self.assertEqual(self.seerr.calls, [])
        self.assertIsNone(self.item(request_id))
        self.assertEqual(drain(), [])

    def test_the_secret_is_compared_in_constant_time(self) -> None:
        with mock.patch.object(hooks.hmac, "compare_digest", wraps=hooks.hmac.compare_digest) as compare:
            self.send(default_payload("TEST_NOTIFICATION"), secret="not-the-secret-0123456789abc")
        compare.assert_called_once_with(b"not-the-secret-0123456789abc", SECRET.encode())

    def test_the_secret_is_also_taken_as_a_bearer_token(self) -> None:
        resp = self.send(default_payload("TEST_NOTIFICATION"), secret=f"Bearer {SECRET}")
        self.assertEqual((resp.status_code, resp.json()), (200, {"result": "test"}))

    def test_a_key_that_is_no_seerr_source_answers_404(self) -> None:
        for url in ("/hooks/seerr/nobody", "/hooks/seerr/feed"):
            with self.subTest(url=url):
                self.assertEqual(self.send(default_payload(), url=url).status_code, 404)
        self.assertEqual(self.seerr.calls, [])

    def test_the_test_notification_answers_200_and_writes_one_event(self) -> None:
        resp = self.send(default_payload("TEST_NOTIFICATION"))
        self.assertEqual((resp.status_code, resp.json()), (200, {"result": "test"}))
        self.assertEqual(drain(), [(events.WEBHOOK_TEST, None, {"source": KEY})])
        self.assertEqual(self.store.all(MediaItem), [])
        self.assertEqual(self.seerr.calls, [])

    def test_media_pending_twice_is_one_item(self) -> None:
        request_id = self.ask()
        first = self.send(default_payload(request_id=request_id))
        second = self.send(default_payload(request_id=request_id))
        self.assertEqual((first.json(), second.json()), ({"result": "added"}, {"result": "known"}))
        self.assertEqual(len(self.store.all(MediaItem)), 1)
        self.assertEqual(self.queue.qsize(), 1)
        # The second call asked TMDB nothing: the request was an item already.
        self.assertEqual(self.resolve_id.await_count, 1)

    def test_every_other_notification_answers_200_and_is_ignored(self) -> None:
        request_id = self.ask()
        for event in (
            "MEDIA_APPROVED", "MEDIA_AUTO_APPROVED", "MEDIA_AUTO_REQUESTED", "ISSUE_CREATED",
            "ISSUE_COMMENT", "ISSUE_RESOLVED", "ISSUE_REOPENED", "SOMETHING_NEW", "",
        ):
            with self.subTest(event=event):
                resp = self.send(default_payload(event, request_id=request_id))
                self.assertEqual((resp.status_code, resp.json()), (200, {"result": "ignored"}))
        self.assertEqual(self.seerr.calls, [])
        self.assertEqual(self.store.all(MediaItem), [])
        self.assertEqual(drain(), [])

    def test_news_about_a_request_this_app_does_not_hold_is_ignored(self) -> None:
        request_id = self.ask(status=fakes.REQUEST_DECLINED)
        for event in ("MEDIA_AVAILABLE", "MEDIA_DECLINED", "MEDIA_FAILED"):
            with self.subTest(event=event):
                resp = self.send(default_payload(event, request_id=request_id))
                self.assertEqual((resp.status_code, resp.json()), (200, {"result": "ignored"}))
        self.assertEqual(self.seerr.calls, [])

    def test_a_request_that_is_not_pending_in_seerr_makes_no_item(self) -> None:
        """The body is not believed: whoever holds the secret cannot invent a request."""
        approved = self.ask(status=fakes.REQUEST_APPROVED)
        for request_id in (approved, 999):
            with self.subTest(request_id=request_id):
                resp = self.send(default_payload(request_id=request_id))
                self.assertEqual((resp.status_code, resp.json()), (200, {"result": "ignored"}))
        self.assertEqual(self.store.all(MediaItem), [])

    def test_a_body_that_is_not_json_or_carries_no_request_changes_nothing(self) -> None:
        resp = self.http.post(HOOK, content=b"not json", headers={"Authorization": SECRET})
        self.assertEqual(resp.status_code, 400)
        for payload in ([], "text", {"notification_type": "MEDIA_PENDING"}, default_payload(request_id=None)):
            with self.subTest(payload=payload):
                resp = self.send(payload)
                self.assertEqual((resp.status_code, resp.json()), (200, {"result": "ignored"}))
        self.assertEqual(self.seerr.calls, [])

    def test_a_page_of_another_site_cannot_post_the_hook(self) -> None:
        request_id = self.ask()
        resp = self.send(default_payload(request_id=request_id), **{"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(resp.status_code, 403)
        self.assertIsNone(self.item(request_id))

    def test_when_seerr_cannot_be_asked_the_poll_picks_the_request_up(self) -> None:
        request_id = self.ask()
        self.seerr.down = httpx.ConnectError
        resp = self.send(default_payload(request_id=request_id))
        self.assertEqual((resp.status_code, resp.json()), (200, {"result": "deferred"}))
        self.assertIsNone(self.item(request_id))
        self.seerr.down = None
        self.assertEqual(self.poll().added, 1)
        self.assertIsNotNone(self.item(request_id))


class NoSecretTests(HookCase):
    SOURCE = {"webhook_secret": None}

    def test_a_source_with_no_secret_has_no_hook(self) -> None:
        """An unauthenticated hook is never open: with no secret the route is not there."""
        request_id = self.ask()
        for secret in (None, "", SECRET):
            with self.subTest(secret=secret):
                self.assertEqual(self.send(default_payload(request_id=request_id), secret=secret).status_code, 404)
        self.assertEqual(self.seerr.calls, [])
        self.assertIsNone(self.item(request_id))
        # The poll alone still reads the source.
        self.assertEqual(self.poll().added, 1)


class ProxyAuthHookTests(HookCase):
    AUTH = {"trusted_header": "Remote-User"}

    def test_the_hook_needs_no_proxy_header_and_the_pages_still_do(self) -> None:
        resp = self.send(default_payload("TEST_NOTIFICATION"))
        self.assertEqual((resp.status_code, resp.json()), (200, {"result": "test"}))
        self.assertEqual(self.send(default_payload("TEST_NOTIFICATION"), secret="wrong").status_code, 401)
        self.assertEqual(self.http.get("/", follow_redirects=False).status_code, 401)


class WebhookUpdateTests(HookCase):
    """MEDIA_AVAILABLE, MEDIA_DECLINED and MEDIA_FAILED update the item of a request this app holds."""

    def approved(self) -> tuple[int, MediaItem]:
        request_id = self.ask()
        item = self.taken(request_id)
        self.work()
        self.assertEqual(item.status, ItemStatus.DOWNLOADING)
        return request_id, item

    def test_media_available_completes_the_item_at_once(self) -> None:
        request_id, item = self.approved()
        self.seerr.info("movie", FILM)["status"] = fakes.MEDIA_AVAILABLE
        resp = self.send(default_payload("MEDIA_AVAILABLE", request_id=request_id))
        self.assertEqual((resp.status_code, resp.json()), (200, {"result": "updated"}))
        self.assertEqual(item.status, ItemStatus.COMPLETED)
        self.assertEqual(self.download(item).status, DownloadStatus.COMPLETED)

    def test_media_available_is_checked_with_seerr_and_not_believed(self) -> None:
        request_id, item = self.approved()
        self.send(default_payload("MEDIA_AVAILABLE", request_id=request_id))
        self.assertEqual(item.status, ItemStatus.DOWNLOADING)

    def test_media_failed_hands_the_item_to_a_person(self) -> None:
        request_id, item = self.approved()
        self.seerr.set_request(request_id, fakes.REQUEST_FAILED)
        drain()
        self.send(default_payload("MEDIA_FAILED", request_id=request_id))
        self.assertEqual(item.status, ItemStatus.ERROR)
        self.assertIn("could not hand the request on", item.error_message)
        self.assertEqual([kind for kind, _id, _p in drain()], [events.DELIVERY_FAILED, events.NEEDS_YOU])

    def test_media_declined_for_a_held_request_rejects_its_item(self) -> None:
        self.moderate_nudity()
        request_id = self.ask()
        item = self.taken(request_id)
        self.assertEqual(item.status, ItemStatus.PENDING_REVIEW)
        self.seerr.set_request(request_id, fakes.REQUEST_DECLINED)
        resp = self.send(default_payload("MEDIA_DECLINED", request_id=request_id))
        self.assertEqual(resp.json(), {"result": "updated"})
        self.assertEqual(item.status, ItemStatus.REJECTED)
        self.assertTrue(self.reasons(item)[-1].startswith("decided in Seerr"))
        self.assertEqual(self.decisions(), [])

    def test_this_app_s_own_decline_coming_back_changes_nothing(self) -> None:
        self.severe_violence()
        request_id = self.ask()
        item = self.taken(request_id)
        self.assertEqual((item.status, self.status(request_id)), (ItemStatus.REJECTED, fakes.REQUEST_DECLINED))
        before = list(item.reasons)
        self.send(default_payload("MEDIA_DECLINED", request_id=request_id))
        self.assertEqual((item.status, item.reasons), (ItemStatus.REJECTED, before))
        self.assertEqual(len(self.decisions()), 1)


# ---------------------------------------------------------------------------
# The poll
# ---------------------------------------------------------------------------


class PollTests(SourceCase):
    def test_the_poll_finds_what_the_webhook_missed(self) -> None:
        seen = self.ask()
        run(seerr_source.hook(self.source, default_payload(request_id=seen)))
        missed = self.ask(tmdb_id=604, user=BOB)
        done = self.poll()
        self.assertEqual((done.pending, done.added), (2, 1))
        self.assertEqual(self.item(missed).sender_id, BOB)
        self.assertEqual(len(self.store.all(MediaItem)), 2)
        [listed] = self.seerr.sent("GET", "/api/v1/request")
        self.assertEqual(listed.params["filter"], "pending")
        self.assertNotIn("x-api-user", listed.headers)

    def test_the_webhook_and_the_poll_make_one_item_of_one_request(self) -> None:
        request_id = self.ask()
        self.assertEqual(self.poll().added, 1)
        self.assertEqual(run(seerr_source.hook(self.source, flat_payload(request_id=request_id))), "known")
        self.assertEqual(self.poll().added, 0)
        self.assertEqual(len(self.store.all(MediaItem)), 1)
        self.assertEqual(self.queue.qsize(), 1)
        self.assertEqual(self.resolve_id.await_count, 1)

    def test_every_page_of_the_pending_list_is_read(self) -> None:
        ids = [self.ask(tmdb_id=1000 + n, user=ALICE) for n in range(120)]
        done = self.poll()
        self.assertEqual((done.pending, done.added), (120, 120))
        pages = self.seerr.sent("GET", "/api/v1/request")
        self.assertEqual(
            [(c.params["take"], c.params["skip"]) for c in pages], [("50", "0"), ("50", "50"), ("50", "100")],
        )
        self.assertEqual({i.telegram_message_id for i in self.store.all(MediaItem)}, set(ids))
        # Oldest first, so a request that arrives mid-read lands on the last page.
        self.assertEqual({c.params["sortDirection"] for c in pages}, {"asc"})

    def test_a_full_last_page_ends_the_read_by_seerr_s_own_count(self) -> None:
        for n in range(100):
            self.ask(tmdb_id=1000 + n)
        self.assertEqual(self.poll().pending, 100)
        self.assertEqual(len(self.seerr.sent("GET", "/api/v1/request")), 2)

    def test_a_request_decided_before_it_was_ever_seen_is_not_taken(self) -> None:
        self.ask(status=fakes.REQUEST_APPROVED)
        self.ask(tmdb_id=604, status=fakes.REQUEST_DECLINED)
        self.assertEqual(self.poll().added, 0)
        self.assertEqual(self.store.all(MediaItem), [])

    def test_a_request_tmdb_cannot_name_is_tried_again_at_the_next_poll(self) -> None:
        request_id = self.ask()
        self.resolve_id.side_effect = None
        self.resolve_id.return_value = None
        self.assertEqual(self.poll().added, 0)
        self.assertIsNone(self.item(request_id))
        self.resolve_id.return_value = ingest.Resolved("Some Film", 1999, MediaType.MOVIE)
        self.assertEqual(self.poll().added, 1)

    def test_one_request_that_cannot_be_handled_does_not_stop_the_others(self) -> None:
        bad, good = self.ask(), self.ask(tmdb_id=604)

        async def resolve(kind, value):
            if int(value) == FILM:
                raise RuntimeError("boom")
            return ingest.Resolved("Other Film", 2001, MediaType.MOVIE)

        self.resolve_id.side_effect = resolve
        self.assertEqual(self.poll().added, 1)
        self.assertIsNone(self.item(bad))
        self.assertIsNotNone(self.item(good))

    def test_the_scheduled_poll_survives_a_seerr_that_is_down(self) -> None:
        request_id = self.ask()
        self.seerr.down = httpx.ConnectError
        run(seerr_source.poll_job(KEY))
        self.assertIsNone(self.item(request_id))
        self.seerr.down = None
        run(seerr_source.poll_job(KEY))
        self.assertIsNotNone(self.item(request_id))
        run(seerr_source.poll_job("nobody"))


class AllowedSendersTests(SourceCase):
    SOURCE = {"allowed_senders": [ALICE]}

    def test_only_the_listed_seerr_users_requests_are_taken(self) -> None:
        hers, his = self.ask(user=ALICE), self.ask(tmdb_id=604, user=BOB)
        self.assertEqual(self.poll().added, 1)
        self.assertIsNotNone(self.item(hers))
        self.assertIsNone(self.item(his))
        # His stays for a person in Seerr; nothing is asked of TMDB about it.
        self.assertEqual(self.resolve_id.await_count, 1)
        self.assertEqual(self.status(his), fakes.REQUEST_PENDING)


class OwnDeliveryTests(SourceCase):
    """`deliver: seerr` asks as `user_id`. Those requests are this app's own."""

    USER_ID = 2
    DELIVER = {"movie": "seerr", "series": "seerr"}

    def test_a_request_this_app_made_for_a_delivery_is_not_taken_as_a_new_item(self) -> None:
        own, theirs = self.ask(user=2), self.ask(tmdb_id=604, user=ALICE)
        self.assertEqual(self.poll().added, 1)
        self.assertIsNone(self.item(own))
        self.assertIsNotNone(self.item(theirs))
        self.assertEqual(run(seerr_source.hook(self.source, default_payload(request_id=own))), "ignored")


# ---------------------------------------------------------------------------
# The decision, carried out in Seerr
# ---------------------------------------------------------------------------


class DecisionTests(SourceCase):
    def test_an_approval_approves_the_request_and_waits_for_seerr_s_files(self) -> None:
        request_id = self.ask()
        item = self.taken(request_id)
        self.assertEqual(item.status, ItemStatus.APPROVED)
        self.assertEqual(self.decisions(), [], "the policy's approval is carried out by the worker")
        self.work()
        [approve] = self.decisions()
        self.assertEqual((approve.method, approve.path), ("POST", f"/api/v1/request/{request_id}/approve"))
        # As the admin: deciding a request takes manage-requests.
        self.assertNotIn("x-api-user", approve.headers)
        self.assertEqual(self.status(request_id), fakes.REQUEST_APPROVED)
        self.assertEqual(item.status, ItemStatus.DOWNLOADING)
        dl = self.download(item)
        self.assertEqual(
            (dl.delivery_target, dl.remote_id, dl.created_torrent, dl.status),
            ("seerr", str(request_id), False, DownloadStatus.DOWNLOADING),
        )
        self.assertEqual(self.reasons(item), ["its Seerr request was approved"])
        self.assertIn((events.DELIVERED, "seerr"), [(k, p.get("target")) for k, _id, p in drain()])
        # Not there yet: the item waits, exactly as a delivery to Seerr does.
        self.check_downloads()
        self.assertEqual(item.status, ItemStatus.DOWNLOADING)
        self.seerr.info("movie", FILM)["status"] = fakes.MEDIA_AVAILABLE
        self.check_downloads()
        self.assertEqual((item.status, dl.status), (ItemStatus.COMPLETED, DownloadStatus.COMPLETED))
        self.assertIn((events.COMPLETED, "seerr"), [(k, p.get("target")) for k, _id, p in drain()])

    def test_deliver_is_not_read_for_a_seerr_request(self) -> None:
        """`deliver` says the download client for films. The request's delivery is Seerr itself."""
        item = self.taken()
        self.work()
        self.assert_download_client_untouched()
        self.assertEqual(self.download(item).delivery_target, "seerr")

    def test_the_title_is_read_by_its_tmdb_id_and_never_searched_for(self) -> None:
        item = self.taken()
        self.tmdb.by_id.assert_awaited_once_with(FILM, MediaType.MOVIE)
        self.tmdb.search.assert_not_awaited()
        [check] = self.store.all(ContentCheck)
        self.assertEqual((check.media_item_id, check.tmdb_id), (item.id, FILM))

    def test_a_rejection_declines_the_request(self) -> None:
        self.severe_violence()
        request_id = self.ask()
        item = self.taken(request_id)
        self.assertEqual((item.status, item.policy_outcome), (ItemStatus.REJECTED, "reject"))
        [decline] = self.decisions()
        self.assertEqual((decline.method, decline.path), ("POST", f"/api/v1/request/{request_id}/decline"))
        self.assertNotIn("x-api-user", decline.headers)
        self.assertEqual(self.status(request_id), fakes.REQUEST_DECLINED)
        self.assertEqual(self.reasons(item), ["rejected: Its Seerr request was declined."])
        self.assertEqual(self.work(), [])
        self.assertIsNone(self.download(item))

    def test_a_review_leaves_the_request_pending(self) -> None:
        self.moderate_nudity()
        request_id = self.ask()
        item = self.taken(request_id)
        self.assertEqual(item.status, ItemStatus.PENDING_REVIEW)
        self.work()
        self.poll()
        self.assertEqual(self.seerr.writes(), [])
        self.assertEqual(self.status(request_id), fakes.REQUEST_PENDING)
        self.assertEqual(item.status, ItemStatus.PENDING_REVIEW)
        self.assertIn(events.NEEDS_YOU, [kind for kind, _id, _p in drain()])
        self.assertIn(f"Seerr request {request_id}", delivery.where(item))

    def test_a_reviewer_s_approve_approves_the_request(self) -> None:
        self.moderate_nudity()
        request_id = self.ask()
        item = self.taken(request_id)
        result = run(actions.approve(item.id))
        self.assertTrue(result.ok)
        self.assertIn("Seerr request", result.message)
        self.work()
        self.assertEqual([c.path for c in self.decisions()], [f"/api/v1/request/{request_id}/approve"])
        self.assertEqual((item.status, self.status(request_id)), (ItemStatus.DOWNLOADING, fakes.REQUEST_APPROVED))
        self.assert_download_client_untouched()

    def test_a_reviewer_s_reject_declines_the_request(self) -> None:
        self.moderate_nudity()
        request_id = self.ask()
        item = self.taken(request_id)
        result = run(actions.reject(item.id, self.posts))
        self.assertTrue(result.ok)
        self.assertEqual(result.message, "Rejected. Its Seerr request was declined.")
        self.assertFalse(result.remote_failed)
        self.assertEqual([c.path for c in self.decisions()], [f"/api/v1/request/{request_id}/decline"])
        self.assertEqual((item.status, self.status(request_id)), (ItemStatus.REJECTED, fakes.REQUEST_DECLINED))
        self.assertEqual(self.reasons(item), ["rejected: Its Seerr request was declined."])

    def test_a_wait_keeps_the_request_pending_and_the_recheck_decides_it(self) -> None:
        self.guide = ParentsGuide()
        request_id = self.ask()
        item = self.taken(request_id)
        self.assertEqual(item.status, ItemStatus.AWAITING_DATA)
        self.work()
        self.poll()
        self.assertEqual(self.seerr.writes(), [])
        self.assertEqual(self.status(request_id), fakes.REQUEST_PENDING)
        # The guide has votes now, and the hourly re-check reads the title by its id again.
        self.guide = full_guide()
        self.assertEqual(run(pipeline.recheck_waiting_items()), 1)
        self.assertEqual(item.status, ItemStatus.APPROVED)
        self.assertEqual(self.tmdb.by_id.await_count, 2)
        self.tmdb.search.assert_not_awaited()
        self.work()
        self.assertEqual((item.status, self.status(request_id)), (ItemStatus.DOWNLOADING, fakes.REQUEST_APPROVED))

    def test_a_recheck_that_rejects_declines_the_request(self) -> None:
        self.guide = ParentsGuide()
        request_id = self.ask()
        item = self.taken(request_id)
        self.severe_violence()
        run(pipeline.recheck_waiting_items())
        self.assertEqual((item.status, self.status(request_id)), (ItemStatus.REJECTED, fakes.REQUEST_DECLINED))

    def test_a_series_request_carries_its_seasons(self) -> None:
        request_id = self.ask("tv", SHOW, seasons=[1, 2])
        resp = run(seerr_source.hook(self.source, default_payload(request_id=request_id, seasons="1, 2")))
        self.assertEqual(resp, "added")
        self.decide()
        item = self.item(request_id)
        self.assertEqual((item.media_type, item.title, item.season), (MediaType.SERIES, "Some Show", "S01-S02"))
        self.assertIn(f"https://www.themoviedb.org/tv/{SHOW}", item.raw_text)
        self.assertIn("Seasons: 1, 2", item.raw_text)
        self.tmdb.by_id.assert_awaited_once_with(SHOW, MediaType.SERIES)
        self.work()
        self.assertEqual((item.status, self.status(request_id)), (ItemStatus.DOWNLOADING, fakes.REQUEST_APPROVED))
        # Done when the seasons asked for are there, not the whole show.
        info = self.seerr.info("tv", SHOW)
        info["status"] = fakes.MEDIA_PARTIAL
        info["seasons"] = [
            {"seasonNumber": 1, "status": fakes.MEDIA_AVAILABLE},
            {"seasonNumber": 2, "status": fakes.MEDIA_PROCESSING},
        ]
        self.check_downloads()
        self.assertEqual(item.status, ItemStatus.DOWNLOADING)
        info["seasons"][1]["status"] = fakes.MEDIA_AVAILABLE
        self.check_downloads()
        self.assertEqual(item.status, ItemStatus.COMPLETED)

    def test_a_4k_request_is_done_when_the_4k_copy_is_there(self) -> None:
        request_id = self.ask(is4k=True)
        item = self.taken(request_id)
        self.work()
        self.assertIn("4K", item.raw_text)
        info = self.seerr.info("movie", FILM)
        info["status"] = fakes.MEDIA_AVAILABLE
        self.check_downloads()
        self.assertEqual(item.status, ItemStatus.DOWNLOADING, "the ordinary copy is not what was asked for")
        info["status4k"] = fakes.MEDIA_AVAILABLE
        self.check_downloads()
        self.assertEqual(item.status, ItemStatus.COMPLETED)

    def test_a_person_cannot_grab_a_release_for_a_seerr_request(self) -> None:
        self.moderate_nudity()
        item = self.taken()
        self.assertIn("Seerr request", delivery.grab_refusal(item.media_type, origin_key=item.origin_key))
        self.assertIsNone(delivery.grab_refusal(item.media_type))

    def test_rejecting_after_the_approval_says_the_request_stays(self) -> None:
        request_id = self.ask()
        item = self.taken(request_id)
        self.work()
        result = run(actions.reject(item.id, self.posts))
        self.assertTrue(result.ok)
        self.assertIn("no longer pending", result.message)
        self.assertFalse(result.remote_failed)
        self.assertEqual((item.status, self.status(request_id)), (ItemStatus.REJECTED, fakes.REQUEST_APPROVED))
        self.assertEqual(self.download(item).status, DownloadStatus.CANCELLED)


class ShadowTests(SourceCase):
    MODE = "shadow"

    def test_shadow_decides_records_and_calls_nothing_on_seerr(self) -> None:
        clean = self.ask()
        self.poll()
        self.decide()
        self.severe_violence()
        violent = self.ask(tmdb_id=604)
        self.poll()
        self.decide()
        self.work()
        self.poll()
        for request_id, verb in ((clean, "approved"), (violent, "rejected")):
            with self.subTest(verb=verb):
                item = self.item(request_id)
                self.assertEqual(item.status, ItemStatus.PENDING_REVIEW)
                self.assertEqual(item.reasons[0]["text"], f"shadow mode: would have {verb}")
                self.assertEqual(self.status(request_id), fakes.REQUEST_PENDING)
        self.assertEqual(self.seerr.writes(), [])
        would_have = [(item_id, p["outcome"]) for kind, item_id, p in drain() if kind == events.WOULD_HAVE]
        self.assertEqual(
            would_have, [(self.item(clean).id, "approve"), (self.item(violent).id, "reject")],
        )


class OffTests(SourceCase):
    MODE = "off"

    def test_with_auto_approve_off_every_request_waits_for_a_person(self) -> None:
        request_id = self.ask()
        item = self.taken(request_id)
        self.work()
        self.assertEqual(item.status, ItemStatus.PENDING_REVIEW)
        self.assertEqual(self.seerr.writes(), [])


# ---------------------------------------------------------------------------
# Decided in Seerr by somebody else
# ---------------------------------------------------------------------------


class DecidedElsewhereTests(SourceCase):
    def held(self) -> tuple[int, MediaItem]:
        self.moderate_nudity()
        request_id = self.ask()
        item = self.taken(request_id)
        self.assertEqual(item.status, ItemStatus.PENDING_REVIEW)
        drain()
        return request_id, item

    def test_a_request_declined_in_seerr_rejects_the_item(self) -> None:
        request_id, item = self.held()
        self.seerr.set_request(request_id, fakes.REQUEST_DECLINED)
        self.assertEqual(self.poll().reflected, 1)
        self.assertEqual(item.status, ItemStatus.REJECTED)
        self.assertIsNotNone(item.reviewed_at)
        [reason] = self.reasons(item)
        self.assertTrue(reason.startswith("decided in Seerr: its request is declined there"), reason)
        self.assertTrue(item.error_message.startswith("Decided in Seerr"))
        self.assertEqual(self.seerr.writes(), [], "never fought over")
        self.assertEqual([kind for kind, _id, _p in drain()], [events.DECLINED])
        self.assertEqual(self.poll().reflected, 0)

    def test_a_request_removed_in_seerr_rejects_the_item(self) -> None:
        request_id, item = self.held()
        self.seerr.remove_request(request_id)
        self.poll()
        self.assertEqual(item.status, ItemStatus.REJECTED)
        self.assertEqual(self.reasons(item), ["decided in Seerr: its request was removed there"])
        self.assertEqual(self.seerr.writes(), [])

    def test_a_request_approved_in_seerr_is_waited_on_and_never_approved_again(self) -> None:
        request_id, item = self.held()
        self.seerr.set_request(request_id, fakes.REQUEST_APPROVED)
        self.assertEqual(self.poll().reflected, 1)
        self.assertEqual(item.status, ItemStatus.APPROVED)
        self.work()
        self.assertEqual(self.seerr.writes(), [])
        self.assertEqual(item.status, ItemStatus.DOWNLOADING)
        self.assertEqual(self.reasons(item), ["decided in Seerr: its request was approved there"])
        self.assertEqual(self.download(item).remote_id, str(request_id))
        self.seerr.info("movie", FILM)["status"] = fakes.MEDIA_AVAILABLE
        self.check_downloads()
        self.assertEqual(item.status, ItemStatus.COMPLETED)

    def test_a_request_declined_while_the_item_waited_for_data_rejects_it(self) -> None:
        self.guide = ParentsGuide()
        request_id = self.ask()
        item = self.taken(request_id)
        self.assertEqual(item.status, ItemStatus.AWAITING_DATA)
        self.seerr.set_request(request_id, fakes.REQUEST_DECLINED)
        self.poll()
        self.assertEqual(item.status, ItemStatus.REJECTED)
        self.assertEqual(run(pipeline.recheck_waiting_items()), 0)

    def test_an_approval_that_arrives_after_a_decline_in_seerr_sends_nothing(self) -> None:
        """The policy approved, and before the worker got there somebody declined in Seerr."""
        request_id = self.ask()
        item = self.taken(request_id)
        self.assertEqual(item.status, ItemStatus.APPROVED)
        self.seerr.set_request(request_id, fakes.REQUEST_DECLINED)
        self.work()
        self.assertEqual(self.decisions(), [])
        self.assertEqual(item.status, ItemStatus.REJECTED)
        self.assertTrue(self.reasons(item)[-1].startswith("decided in Seerr"))
        self.assertIsNone(self.download(item))
        self.assert_download_client_untouched()

    def test_a_decline_in_seerr_between_the_read_and_the_approve_is_not_an_error(self) -> None:
        request_id = self.ask()
        item = self.taken(request_id)
        real = self.client.approve

        async def somebody_was_faster(asked):
            self.seerr.set_request(asked, fakes.REQUEST_DECLINED)
            await real(asked)

        with mock.patch.object(self.client, "approve", somebody_was_faster):
            self.work()
        self.assertEqual(item.status, ItemStatus.REJECTED)
        self.assertEqual(self.status(request_id), fakes.REQUEST_DECLINED)

    def test_a_request_declined_in_seerr_after_this_app_approved_it_rejects_the_item(self) -> None:
        request_id = self.ask()
        item = self.taken(request_id)
        self.work()
        drain()
        self.seerr.set_request(request_id, fakes.REQUEST_DECLINED)
        self.check_downloads()
        self.assertEqual(item.status, ItemStatus.REJECTED, "a decision taken in Seerr, not an error to sort out")
        self.assertEqual(self.reasons(item)[-1], "decided in Seerr: the request was declined in Seerr")
        self.assertEqual(self.download(item).status, DownloadStatus.CANCELLED)
        self.assertEqual([kind for kind, _id, _p in drain()], [events.DECLINED])
        self.assertEqual(len(self.decisions()), 1, "only this app's own approval")

    def test_a_reject_of_a_request_somebody_already_decided_says_so(self) -> None:
        request_id, item = self.held()
        self.seerr.set_request(request_id, fakes.REQUEST_APPROVED)
        result = run(actions.reject(item.id, self.posts))
        self.assertIn("no longer pending", result.message)
        self.assertFalse(result.remote_failed)
        self.assertEqual(self.status(request_id), fakes.REQUEST_APPROVED)


# ---------------------------------------------------------------------------
# Seerr down at decision time
# ---------------------------------------------------------------------------


class SeerrDownTests(SourceCase):
    def test_an_approval_seerr_does_not_answer_is_kept_and_tried_again_with_a_growing_wait(self) -> None:
        request_id = self.ask()
        item = self.taken(request_id)
        drain()
        self.seerr.down = httpx.ConnectError
        self.assertEqual(len(self.work()), 1)
        # Kept, and approved nowhere but here: nothing says it is on its way.
        self.assertEqual(item.status, ItemStatus.APPROVED)
        self.assertIn("still pending there", item.error_message)
        self.assertIsNone(self.download(item))
        self.assertEqual(self.status(request_id), fakes.REQUEST_PENDING)
        self.assertEqual(
            [(kind, p.get("retrying")) for kind, _id, p in drain()], [(events.DELIVERY_FAILED, True)],
        )
        # Not asked again every two seconds: the worker leaves it for 30 s...
        self.assertEqual(delivery.held_back(), [item.id])
        self.assertEqual(self.work(), [])
        self.now[0] += delivery.RETRY_FIRST_SECONDS + 1
        self.assertEqual(len(self.work()), 1)
        # ...then for 60, and says so once an outage, not once a try.
        self.assertEqual(drain(), [])
        self.now[0] += delivery.RETRY_FIRST_SECONDS + 1
        self.assertEqual(self.work(), [])
        self.now[0] += delivery.RETRY_FIRST_SECONDS
        self.seerr.down = None
        self.assertEqual(len(self.work()), 1)
        self.assertEqual((item.status, self.status(request_id)), (ItemStatus.DOWNLOADING, fakes.REQUEST_APPROVED))
        self.assertIsNone(item.error_message)
        self.assertEqual(delivery.held_back(), [])
        self.assert_download_client_untouched()

    def test_the_wait_between_tries_stops_growing_at_half_an_hour(self) -> None:
        item = self.taken()
        self.seerr.down = httpx.ReadTimeout
        waits = []
        for _ in range(9):
            self.assertEqual(len(self.work()), 1)
            waits.append(delivery._retries[item.id][1] - self.now[0])
            self.now[0] = delivery._retries[item.id][1] + 1
        self.assertEqual(waits[:4], [30.0, 60.0, 120.0, 240.0])
        self.assertEqual(waits[-1], delivery.RETRY_MAX_SECONDS)
        self.assertEqual(item.status, ItemStatus.APPROVED)

    def test_a_server_error_is_an_outage_too(self) -> None:
        item = self.taken()
        with mock.patch.object(self.seerr, "route", lambda *a: httpx.Response(502, json={"message": "Bad Gateway"})):
            self.work()
        self.assertEqual(item.status, ItemStatus.APPROVED)
        self.assertEqual(delivery.held_back(), [item.id])

    def test_a_refused_key_needs_a_person_and_the_request_stays_pending(self) -> None:
        request_id = self.ask()
        item = self.taken(request_id)
        self.seerr.accepts = "another-key"
        self.work()
        self.assertEqual(item.status, ItemStatus.ERROR)
        self.assertIn("refused the API key", item.error_message)
        self.assertEqual(delivery.held_back(), [])
        self.assertEqual(self.status(request_id), fakes.REQUEST_PENDING)
        # Once the key is right, the reviewer's approve goes through.
        self.seerr.accepts = API_KEY
        run(actions.approve(item.id))
        self.work()
        self.assertEqual((item.status, self.status(request_id)), (ItemStatus.DOWNLOADING, fakes.REQUEST_APPROVED))

    def test_a_decline_seerr_does_not_answer_is_made_by_the_next_poll_that_reaches_it(self) -> None:
        self.severe_violence()
        request_id = self.ask()
        self.poll()
        self.seerr.down = httpx.ConnectError
        self.decide()
        item = self.item(request_id)
        # The decision stands here, and Seerr still waits to hear it.
        self.assertEqual((item.status, self.status(request_id)), (ItemStatus.REJECTED, fakes.REQUEST_PENDING))
        self.assertIn("still waits there", self.reasons(item)[0])
        failed = [p for kind, _id, p in drain() if kind == events.DELIVERY_FAILED]
        self.assertEqual([(p["target"], p["rejected"]) for p in failed], [("seerr", True)])
        # A poll that cannot reach Seerr changes nothing and does not raise.
        run(seerr_source.poll_job(KEY))
        self.assertEqual(self.status(request_id), fakes.REQUEST_PENDING)
        self.seerr.down = None
        done = self.poll()
        self.assertEqual((done.added, done.declined), (0, 1))
        self.assertEqual(self.status(request_id), fakes.REQUEST_DECLINED)
        self.assertIn("rejected: Its Seerr request was declined.", self.reasons(item))
        self.assertEqual(self.poll().declined, 0)

    def test_a_reviewer_s_reject_seerr_does_not_answer_says_so_and_is_made_later(self) -> None:
        self.moderate_nudity()
        request_id = self.ask()
        item = self.taken(request_id)
        self.seerr.down = httpx.ConnectError
        result = run(actions.reject(item.id, self.posts))
        self.assertTrue(result.ok)
        self.assertTrue(result.remote_failed)
        self.assertIn("could not be told", result.message)
        self.seerr.down = None
        self.assertEqual(self.poll().declined, 1)
        self.assertEqual(self.status(request_id), fakes.REQUEST_DECLINED)


# ---------------------------------------------------------------------------
# The Inbox, Review and the item page, for an item that is a Seerr request
# ---------------------------------------------------------------------------


class RerunTests(SourceCase):
    """`actions.rerun` decides an item again. For a Seerr request the decision is carried out in Seerr."""

    def test_a_request_the_policy_rejected_is_not_run_again(self) -> None:
        """Its request is declined, and Seerr approves no declined request."""
        self.severe_violence()
        request_id = self.ask()
        item = self.taken(request_id)
        self.assertEqual((item.status, self.status(request_id)), (ItemStatus.REJECTED, fakes.REQUEST_DECLINED))
        before = (list(item.reasons), item.error_message, item.reviewed_at)
        drain()
        # The policy would approve it now.
        self.guide = full_guide()
        result = run(actions.rerun(item.id))
        self.assertFalse(result.ok, result.message)
        self.assertEqual(item.status, ItemStatus.REJECTED)
        self.assertEqual((result.status, result.message), (actions.Status.FAILED, actions.RERUN_SEERR_DECLINED))
        self.assertEqual((list(item.reasons), item.error_message, item.reviewed_at), before)
        # Nothing follows from it: no approval for the worker, nothing for the poll to read back.
        self.assertEqual(self.work(), [])
        done = self.poll()
        self.assertEqual((done.added, done.declined, done.reflected), (0, 0, 0))
        self.assertEqual(len(self.decisions()), 1, "the policy's own decline, and nothing since")
        self.assertEqual(drain(), [])

    def test_approving_a_request_the_policy_declined_says_whose_decline_it_was(self) -> None:
        self.severe_violence()
        request_id = self.ask()
        item = self.taken(request_id)
        self.assertEqual(self.status(request_id), fakes.REQUEST_DECLINED)
        self.assertTrue(run(actions.approve(item.id)).ok)
        self.work()
        self.assertEqual(item.status, ItemStatus.REJECTED)
        self.assertTrue(self.reasons(item)[-1].startswith("its Seerr request was declined when this item was rejected"))
        # Nobody in Seerr decided anything.
        self.assertFalse([r for r in self.reasons(item) if r.startswith(delivery.DECIDED_IN_SEERR)])
        self.assertEqual(len(self.decisions()), 1)
        self.assertEqual(self.status(request_id), fakes.REQUEST_DECLINED)

    def test_a_held_request_that_passes_now_is_approved_in_seerr(self) -> None:
        self.moderate_nudity()
        request_id = self.ask()
        item = self.taken(request_id)
        self.assertEqual(item.status, ItemStatus.PENDING_REVIEW)
        self.guide = full_guide()
        result = run(actions.rerun(item.id))
        self.assertTrue(result.ok, result.message)
        self.assertEqual(item.status, ItemStatus.APPROVED)
        # A poll that lands before the worker changes nothing: the request still waits in Seerr.
        self.assertEqual(self.poll().reflected, 0)
        self.work()
        self.assertEqual([c.path for c in self.decisions()], [f"/api/v1/request/{request_id}/approve"])
        self.assertEqual((item.status, self.status(request_id)), (ItemStatus.DOWNLOADING, fakes.REQUEST_APPROVED))
        self.tmdb.search.assert_not_awaited()
        self.assert_download_client_untouched()

    def test_a_held_request_the_policy_rejects_now_is_declined_in_seerr(self) -> None:
        self.moderate_nudity()
        request_id = self.ask()
        item = self.taken(request_id)
        self.severe_violence()
        result = run(actions.rerun(item.id))
        self.assertTrue(result.ok, result.message)
        self.assertEqual((item.status, self.status(request_id)), (ItemStatus.REJECTED, fakes.REQUEST_DECLINED))
        self.assertEqual([c.path for c in self.decisions()], [f"/api/v1/request/{request_id}/decline"])


class CardTests(SourceCase):
    """What the pages offer for a Seerr request: `deliver` says the download client for films here."""

    def card(self, item: MediaItem) -> cards.Card:
        return cards.Card(item, None, None, self.download(item), *cards._handed(item))

    def test_no_release_is_grabbed_for_a_seerr_request(self) -> None:
        self.moderate_nudity()
        card = self.card(self.taken())
        self.assertTrue(card.can_approve)
        self.assertFalse(card.to_client)
        self.assertFalse(card.can_grab)
        self.assertIsNone(card.direct_add)

    def test_the_reject_dialog_says_what_happens_to_the_request(self) -> None:
        self.moderate_nudity()
        held = self.taken()
        self.assertEqual(self.card(held).reject_confirm, cards.REJECT_SEERR_REQUEST)
        self.guide = full_guide()
        approved = self.taken(tmdb_id=604)
        self.work()
        self.assertEqual(approved.status, ItemStatus.DOWNLOADING)
        confirm = self.card(approved).reject_confirm
        self.assertEqual(confirm, cards.REJECT_SEERR_APPROVED)
        self.assertNotIn("taken back", confirm)

    def test_rerun_is_offered_for_a_held_request_and_not_for_one_the_policy_declined(self) -> None:
        self.moderate_nudity()
        self.assertTrue(self.card(self.taken()).can_rerun)
        self.severe_violence()
        rejected = self.card(self.taken(tmdb_id=604))
        self.assertTrue(rejected.rejected_by_policy)
        self.assertFalse(rejected.can_rerun)


class ReviewGrabTests(HookCase):
    AUTH = None

    def test_review_s_grab_is_refused_for_a_seerr_request(self) -> None:
        self.moderate_nudity()
        request_id = self.ask()
        item = self.taken(request_id)
        # A search a person ran stored a release.
        self.store.put(TorrentSearch(
            media_item_id=item.id, result_count=1,
            prowlarr_results=[{"title": "Some.Film.1999.1080p", "magnet_url": "magnet:?xt=urn:btih:abc"}],
        ))
        add_release = mock.AsyncMock()
        with mock.patch.object(actions.search_service, "add_release", add_release), \
                mock.patch.object(cards, "load_review", mock.AsyncMock(return_value=cards.Review(None, None, 0))):
            resp = self.http.post(f"/review/{item.id}/grab", headers={"HX-Request": "true"})
        self.assertEqual(resp.status_code, 200)
        toast = json.loads(resp.headers["HX-Trigger"])["toast"]
        self.assertEqual(toast["kind"], "error")
        self.assertIn("Seerr picks its own release", toast["text"])
        add_release.assert_not_awaited()
        self.assert_download_client_untouched()
        self.assertEqual((item.status, self.status(request_id)), (ItemStatus.PENDING_REVIEW, fakes.REQUEST_PENDING))
        self.assertEqual(self.seerr.writes(), [])
        self.assertIsNone(self.download(item))


# ---------------------------------------------------------------------------
# A 404 that is not Seerr's
# ---------------------------------------------------------------------------


def behind_a_proxy(seerr: FakeSeerr):
    """Seerr restarts, and the reverse proxy in front of it answers 404 for every path."""
    return mock.patch.object(seerr, "route", lambda *a: httpx.Response(404, text="404 page not found"))


class ProxyNotFoundTests(HookCase):
    def test_an_approval_during_the_404s_is_tried_again_and_the_request_is_never_declined(self) -> None:
        request_id = self.ask()
        item = self.taken(request_id)
        self.assertEqual(item.status, ItemStatus.APPROVED)
        with behind_a_proxy(self.seerr):
            self.work()
        # Not "removed in Seerr": the request is still there, and still pending.
        self.assertEqual(item.status, ItemStatus.APPROVED)
        self.assertEqual(delivery.held_back(), [item.id])
        self.assertEqual(self.reasons(item), [])
        self.assertEqual(self.status(request_id), fakes.REQUEST_PENDING)
        # Seerr is back: the poll declines nothing, and the retry approves.
        self.assertEqual(self.poll().declined, 0)
        self.now[0] += delivery.RETRY_FIRST_SECONDS + 1
        self.work()
        self.assertEqual([c.path for c in self.decisions()], [f"/api/v1/request/{request_id}/approve"])
        self.assertEqual((item.status, self.status(request_id)), (ItemStatus.DOWNLOADING, fakes.REQUEST_APPROVED))

    def test_a_webhook_during_the_404s_leaves_a_held_item_alone(self) -> None:
        self.moderate_nudity()
        request_id = self.ask()
        item = self.taken(request_id)
        for event in ("MEDIA_DECLINED", "MEDIA_FAILED", "MEDIA_AVAILABLE"):
            with self.subTest(event=event):
                with behind_a_proxy(self.seerr):
                    resp = self.send(default_payload(event, request_id=request_id))
                self.assertEqual(resp.json(), {"result": "deferred"})
                self.assertEqual(item.status, ItemStatus.PENDING_REVIEW)
        self.assertEqual(self.poll().declined, 0)
        self.assertEqual(self.status(request_id), fakes.REQUEST_PENDING)

    def test_a_reject_during_the_404s_is_made_by_the_next_poll(self) -> None:
        self.moderate_nudity()
        request_id = self.ask()
        item = self.taken(request_id)
        with behind_a_proxy(self.seerr):
            result = run(actions.reject(item.id, self.posts))
        self.assertTrue(result.remote_failed)
        self.assertIn("still waits there", result.message)
        self.assertEqual(self.poll().declined, 1)
        self.assertEqual(self.status(request_id), fakes.REQUEST_DECLINED)


# ---------------------------------------------------------------------------
# A Seerr older than 3.5.0, which declines whatever it is told to
# ---------------------------------------------------------------------------


class OlderSeerrTests(SourceCase):
    def setUp(self) -> None:
        super().setUp()
        self.seerr.guards = False

    def test_the_fake_declines_an_approved_request_as_seerr_did_before_3_5_0(self) -> None:
        """The positive control: without the read first, the two tests below fail."""
        request_id = self.ask(status=fakes.REQUEST_APPROVED)
        run(self.client.decline(request_id))
        self.assertEqual(self.status(request_id), fakes.REQUEST_DECLINED)

    def test_a_reject_does_not_decline_a_request_somebody_approved_in_seerr(self) -> None:
        self.moderate_nudity()
        request_id = self.ask()
        item = self.taken(request_id)
        # An admin approves it in Seerr, and before the next poll a reviewer rejects the item.
        self.seerr.set_request(request_id, fakes.REQUEST_APPROVED)
        result = run(actions.reject(item.id, self.posts))
        self.assertIn("no longer pending", result.message)
        self.assertFalse(result.remote_failed)
        self.assertEqual(self.decisions(), [], "decided in Seerr is never fought over")
        self.assertEqual(self.status(request_id), fakes.REQUEST_APPROVED)

    def test_a_reject_after_this_app_s_approval_leaves_the_approved_request(self) -> None:
        request_id = self.ask()
        item = self.taken(request_id)
        self.work()
        self.assertEqual(self.status(request_id), fakes.REQUEST_APPROVED)
        result = run(actions.reject(item.id, self.posts))
        self.assertIn("no longer pending", result.message)
        self.assertEqual([c.path for c in self.decisions()], [f"/api/v1/request/{request_id}/approve"])
        self.assertEqual(self.status(request_id), fakes.REQUEST_APPROVED)

    def test_a_pending_request_is_still_declined(self) -> None:
        self.severe_violence()
        request_id = self.ask()
        self.taken(request_id)
        self.assertEqual(self.status(request_id), fakes.REQUEST_DECLINED)


# ---------------------------------------------------------------------------
# An item inserted and never decided
# ---------------------------------------------------------------------------


class RestartTests(SourceCase):
    """The pipeline queue is in memory. The poll is what comes back for a row the queue lost."""

    def restart(self) -> None:
        """The process stops and starts: what was queued is gone."""
        self.queue = asyncio.Queue()
        ingest.set_queue(self.queue)

    def test_a_request_inserted_and_never_decided_is_queued_again(self) -> None:
        request_id = self.ask()
        self.assertEqual(self.poll().added, 1)
        item = self.item(request_id)
        self.restart()
        self.poll()
        self.assertEqual(self.queue.qsize(), 1, "the poll is the guarantee: the item goes back to the pipeline")
        # Once: the next polls find it queued by this process.
        self.assertEqual((self.poll().requeued, self.poll().requeued), (0, 0))
        self.assertEqual(self.queue.qsize(), 1)
        self.decide()
        self.assertEqual(item.status, ItemStatus.APPROVED)
        self.work()
        self.assertEqual(self.status(request_id), fakes.REQUEST_APPROVED)
        self.assertEqual(len(self.store.all(MediaItem)), 1)

    def test_an_item_the_process_stopped_on_mid_way_starts_again(self) -> None:
        for stopped_in in (ItemStatus.PARSED, ItemStatus.CHECKING):
            with self.subTest(stopped_in=stopped_in):
                request_id = self.ask(tmdb_id=600 + len(self.store.all(MediaItem)))
                self.poll()
                item = self.item(request_id)
                item.status = stopped_in
                self.restart()
                self.poll()
                self.assertEqual(item.status, ItemStatus.PENDING_PARSE)
                self.assertEqual(self.queue.qsize(), 1)
                self.decide()
                self.assertEqual(item.status, ItemStatus.APPROVED)

    def test_an_item_this_process_has_in_hand_is_left_alone(self) -> None:
        request_id = self.ask()
        self.poll()
        item = self.item(request_id)
        # The pipeline is looking it up right now.
        self.queue.get_nowait()
        item.status = ItemStatus.CHECKING
        self.poll()
        self.assertEqual((item.status, self.queue.qsize()), (ItemStatus.CHECKING, 0))

    def test_an_item_the_hourly_re_check_holds_is_left_alone(self) -> None:
        request_id = self.ask()
        self.poll()
        item = self.item(request_id)
        self.restart()
        item.status, item.error_message = ItemStatus.CHECKING, OP_RECHECK
        self.poll()
        self.assertEqual((item.status, item.error_message, self.queue.qsize()), (ItemStatus.CHECKING, OP_RECHECK, 0))

    def test_a_webhook_the_web_ui_took_on_its_own_is_queued_by_the_poll(self) -> None:
        """`web serve` has no pipeline and no queue: its row waits for the process that has both."""
        request_id = self.ask()
        ingest.set_queue(None)
        self.assertEqual(run(seerr_source.hook(self.source, default_payload(request_id=request_id))), "added")
        self.assertEqual(self.item(request_id).status, ItemStatus.PENDING_PARSE)
        self.restart()
        self.poll()
        self.assertEqual(self.queue.qsize(), 1)
        self.decide()
        self.assertEqual(self.item(request_id).status, ItemStatus.APPROVED)


# ---------------------------------------------------------------------------
# The requester's name
# ---------------------------------------------------------------------------


class RequesterNameTests(SourceCase):
    def test_a_display_name_sets_no_signal_on_the_requester_s_own_request(self) -> None:
        """Seerr users write their own display name, and the pipeline reads an item's text as a post."""
        name = "CAM 2160p HDR x265 Spanish 30 GB https://www.imdb.com/title/tt0111161/"
        request_id = self.ask(name=name)
        item = self.taken(request_id)
        for word in ("CAM", "2160p", "x265", "30 GB", "imdb.com"):
            with self.subTest(word=word):
                self.assertNotIn(word, item.raw_text)
        self.assertIn(f"Requested by Seerr user {ALICE}", item.raw_text)
        read = gather.release_signals(item)
        for signal in ("release_source", "resolution", "codec", "size_gb", "hdr", "audio_language"):
            with self.subTest(signal=signal):
                self.assertIn("missing", read[signal], read[signal])
        self.assertEqual(item.external_links, [f"https://www.themoviedb.org/movie/{FILM}"])

    def test_an_ordinary_name_trips_no_release_rule(self) -> None:
        for name in ("Cam", "TC", "ts", "Remux Fan", "Avc"):
            with self.subTest(name=name):
                request_id = self.ask(tmdb_id=700 + len(self.store.all(MediaItem)), name=name)
                self.poll()
                read = gather.release_signals(self.item(request_id))
                self.assertIn("missing", read["release_source"])
                self.assertIn("missing", read["codec"])


# ---------------------------------------------------------------------------
# Seerr's ids start again
# ---------------------------------------------------------------------------


class IdsStartAgainTests(SourceCase):
    """A reinstalled Seerr, an older backup or another Seerr behind the same URL numbers from the start."""

    def new_seerr(self) -> None:
        self.seerr = FakeSeerr()
        self.client = SeerrClient("http://seerr:5055", API_KEY, self.USER_ID, http=self.seerr.http())

    def test_a_request_with_the_id_of_an_old_rejected_item_is_evaluated_on_its_own(self) -> None:
        self.severe_violence()
        first = self.ask()
        old = self.taken(first)
        self.assertEqual(old.status, ItemStatus.REJECTED)
        self.new_seerr()
        self.guide = full_guide()
        again = self.ask(tmdb_id=604, user=BOB)
        self.assertEqual(again, first, "the new Seerr gave the old id to another request")
        done = self.poll()
        self.assertEqual((done.added, done.declined), (1, 0))
        self.assertEqual(self.decisions(), [], "the old item's rejection is not carried out on another request")
        new = self.item(again)
        self.assertIsNot(new, old)
        self.assertEqual((new.title, new.sender_id, new.status), ("Film 604", BOB, ItemStatus.PENDING_PARSE))
        # The old item is still a Seerr request, under a key no request has.
        self.assertNotEqual(old.origin_key, new.origin_key)
        self.assertEqual(seerr_api.request_of(old.origin_key), (KEY, first))
        self.assertEqual((old.status, old.telegram_message_id), (ItemStatus.REJECTED, -old.id))
        self.decide()
        self.work()
        self.assertEqual((new.status, self.status(again)), (ItemStatus.DOWNLOADING, fakes.REQUEST_APPROVED))
        self.assertEqual(self.poll().added, 0)

    def test_an_old_held_item_cannot_approve_the_request_that_took_its_id(self) -> None:
        self.moderate_nudity()
        first = self.ask()
        old = self.taken(first)
        self.assertEqual(old.status, ItemStatus.PENDING_REVIEW)
        self.new_seerr()
        again = self.ask(tmdb_id=604, user=BOB)
        # A reviewer approves the old item before any poll has seen the new Seerr.
        self.assertTrue(run(actions.approve(old.id)).ok)
        self.work()
        self.assertEqual(self.decisions(), [])
        self.assertEqual(self.status(again), fakes.REQUEST_PENDING)
        self.assertEqual(old.status, ItemStatus.REJECTED)
        self.assertEqual(self.reasons(old)[-1], "decided in Seerr: its request was removed there")
        self.assertIsNone(self.download(old))

    def test_an_old_held_item_cannot_decline_the_request_that_took_its_id(self) -> None:
        self.moderate_nudity()
        first = self.ask()
        old = self.taken(first)
        self.new_seerr()
        again = self.ask(tmdb_id=604, user=BOB)
        result = run(actions.reject(old.id, self.posts))
        self.assertIn("already gone", result.message)
        self.assertEqual(self.decisions(), [])
        self.assertEqual(self.status(again), fakes.REQUEST_PENDING)

    def test_the_poll_writes_the_old_held_item_as_removed_and_takes_the_new_request(self) -> None:
        self.moderate_nudity()
        first = self.ask()
        old = self.taken(first)
        drain()
        self.new_seerr()
        again = self.ask(tmdb_id=604, user=BOB)
        done = self.poll()
        self.assertEqual((done.added, done.reflected), (1, 1))
        self.assertEqual(old.status, ItemStatus.REJECTED)
        self.assertEqual(self.reasons(old), ["decided in Seerr: its request was removed there"])
        self.assertEqual(self.item(again).title, "Film 604")
        self.assertEqual(self.seerr.writes(), [])
        # And it stays that way.
        done = self.poll()
        self.assertEqual((done.added, done.reflected), (0, 0))
        self.assertEqual(len(self.store.all(MediaItem)), 2)

    def test_the_same_title_asked_again_under_the_same_id_is_another_request(self) -> None:
        self.severe_violence()
        first = self.ask()
        old = self.taken(first)
        self.new_seerr()
        self.guide = full_guide()
        again = self.ask(created_at="2026-11-02T10:00:00.000Z")
        self.assertEqual(again, first)
        self.assertEqual(self.poll().added, 1)
        self.assertEqual(self.decisions(), [])
        self.assertIsNot(self.item(again), old)

    def test_the_webhook_takes_the_new_request_too(self) -> None:
        self.severe_violence()
        first = self.ask()
        old = self.taken(first)
        self.new_seerr()
        again = self.ask(tmdb_id=604, user=BOB)
        self.assertEqual(run(seerr_source.hook(self.source, default_payload(request_id=again))), "added")
        self.assertIsNot(self.item(again), old)

    def test_a_seerr_put_back_as_it_was_keeps_its_items(self) -> None:
        """The control: the same id, title and date is the same request, and stays one item."""
        request_id = self.ask()
        item = self.taken(request_id)
        self.assertEqual(self.poll().added, 0)
        self.assertIs(self.item(request_id), item)
        self.assertTrue(delivery.same_request(item, self.seerr.requests[request_id]))
        self.assertEqual(len(self.store.all(MediaItem)), 1)


# ---------------------------------------------------------------------------
# A webhook or a late decline that keeps failing
# ---------------------------------------------------------------------------


class RepeatedFailureTests(HookCase):
    def test_webhook_calls_while_seerr_is_down_do_not_use_up_the_hour_an_item_is_given(self) -> None:
        request_id = self.ask()
        item = self.taken(request_id)
        self.work()
        self.assertEqual(item.status, ItemStatus.DOWNLOADING)
        self.seerr.down = httpx.ConnectError
        for _ in range(scheduler.UNANSWERED_LIMIT + 1):
            resp = self.send(default_payload("MEDIA_AVAILABLE", request_id=request_id))
            self.assertEqual(resp.json(), {"result": "deferred"})
        self.assertEqual(item.status, ItemStatus.DOWNLOADING)
        self.assertEqual(dict(scheduler._unanswered), {})
        # The five-minute check still counts, from the start.
        self.check_downloads()
        self.assertEqual(list(scheduler._unanswered.values()), [1])

    def test_a_late_decline_seerr_keeps_refusing_is_reported_once(self) -> None:
        self.severe_violence()
        request_id = self.ask()
        self.poll()
        refused = mock.patch.object(
            self.seerr, "_decide", lambda *a: httpx.Response(500, json={"message": "Internal Server Error"}),
        )
        with refused:
            self.decide()
            item = self.item(request_id)
            self.assertEqual((item.status, self.status(request_id)), (ItemStatus.REJECTED, fakes.REQUEST_PENDING))
            for _ in range(3):
                self.assertEqual(self.poll().declined, 0)
            # Asked each time, and said once.
            self.assertEqual(len(self.decisions()), 4)
            failed = [p for kind, _id, p in drain() if kind == events.DELIVERY_FAILED]
            self.assertEqual(len(failed), 1, failed)
        self.assertEqual(self.poll().declined, 1)
        self.assertEqual(self.status(request_id), fakes.REQUEST_DECLINED)


class HookBodyLimitTests(HookCase):
    def test_a_body_over_the_limit_is_refused_without_being_read_whole(self) -> None:
        big = b'{"notification_type": "' + b"x" * (5 * 1024 * 1024) + b'"}'
        read = []
        real = hooks.Request.stream

        async def counting(request):
            async for chunk in real(request):
                read.append(len(chunk))
                yield chunk

        with mock.patch.object(hooks.Request, "stream", counting):
            resp = self.http.post(HOOK, content=big, headers={"Authorization": SECRET})
            self.assertEqual(resp.status_code, 413)
            self.assertEqual(read, [], "its Content-Length says enough")
            # With no Content-Length the answer is the same.
            resp = self.http.post(HOOK, content=iter([big]), headers={"Authorization": SECRET})
            self.assertEqual(resp.status_code, 413)

    def test_a_body_with_no_length_is_read_no_further_than_the_limit(self) -> None:
        class Pieces:
            headers: dict = {}
            pulled = 0

            async def stream(self):
                for _ in range(80):
                    self.pulled += 1
                    yield b"x" * 4096

        request = Pieces()
        self.assertIsNone(run(hooks._body(request)))
        # 16 pieces fill the 64 KiB, and the 17th is the one too many.
        self.assertEqual(request.pulled, hooks.MAX_BODY_BYTES // 4096 + 1)
        # The same pieces under a limit they fit in come back whole.
        with mock.patch.object(hooks, "MAX_BODY_BYTES", 80 * 4096):
            self.assertEqual(len(run(hooks._body(Pieces()))), 80 * 4096)

    def test_a_body_nested_thousands_deep_changes_nothing(self) -> None:
        deep = b"[" * 20000 + b"]" * 20000
        resp = self.http.post(HOOK, content=deep, headers={"Authorization": SECRET})
        # Python 3.12 gives up on it with a RecursionError, which is "not JSON";
        # 3.14 parses it and the list is a call that says nothing. Either way
        # the app neither crashes nor asks Seerr anything.
        self.assertIn(resp.status_code, (200, 400))
        self.assertEqual(self.seerr.calls, [])

    def test_a_request_id_that_is_no_number_is_ignored(self) -> None:
        resp = self.send({"notification_type": "MEDIA_PENDING", "request_id": "²"})
        self.assertEqual((resp.status_code, resp.json()), (200, {"result": "ignored"}))
        self.assertEqual(self.seerr.calls, [])


# ---------------------------------------------------------------------------
# The pieces
# ---------------------------------------------------------------------------


class ClientTests(unittest.TestCase):
    def test_deciding_is_done_as_the_admin_even_when_requests_are_made_as_a_user(self) -> None:
        """Creating a request keeps X-API-User; approving one must not carry it."""
        seerr = FakeSeerr()
        client = SeerrClient("http://seerr:5055", API_KEY, 2, http=seerr.http())
        made = run(client.request("movie", FILM))
        self.assertEqual(seerr.sent("POST", "/api/v1/request")[0].headers["x-api-user"], "2")
        self.assertEqual(made["status"], fakes.REQUEST_PENDING)
        run(client.approve(made["id"]))
        other = seerr.add_request("movie", 604, user=ALICE)
        run(client.decline(other["id"]))
        for call in seerr.calls[1:]:
            self.assertNotIn("x-api-user", call.headers)
        self.assertEqual(
            (seerr.requests[made["id"]]["status"], seerr.requests[other["id"]]["status"]),
            (fakes.REQUEST_APPROVED, fakes.REQUEST_DECLINED),
        )

    def test_the_fake_refuses_a_decision_made_as_an_ordinary_user(self) -> None:
        """The positive control of the test above: with the header, Seerr says 403."""
        seerr = FakeSeerr()
        request = seerr.add_request("movie", FILM, user=ALICE)
        http = seerr.http()
        resp = run(http.post(
            f"http://seerr:5055/api/v1/request/{request['id']}/approve",
            headers={"X-Api-Key": API_KEY, "X-API-User": "2"},
        ))
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(request["status"], fakes.REQUEST_PENDING)

    def test_a_request_seen_on_two_pages_is_listed_once(self) -> None:
        seerr = FakeSeerr()
        client = SeerrClient("http://seerr:5055", API_KEY, None, http=seerr.http())
        for n in range(60):
            seerr.add_request("movie", 1000 + n, user=ALICE)
        real = seerr._list

        def shifting(params):
            # A request leaves the list between the two pages, so the second starts one early.
            if params.get("skip") == "50":
                params = {**params, "skip": "49"}
            return real(params)

        with mock.patch.object(seerr, "_list", shifting):
            found = run(client.pending_requests())
        self.assertEqual(len(found), 60)
        self.assertEqual(len({r["id"] for r in found}), 60)

    def test_request_keys(self) -> None:
        self.assertEqual(seerr_api.request_key("requests", 31), "seerr:requests:31")
        self.assertEqual(seerr_api.request_of("seerr:requests:31"), ("requests", 31))
        for other in (
            None, "", "-1001234567890:10", "seerr:requests", "seerr:requests:x", "bot:requests:31",
            "seerr:requests:\u00b2", "seerr:requests:31:x", "seerr:requests:31:1000:1",
        ):
            with self.subTest(other=other):
                self.assertIsNone(seerr_api.request_of(other))
        # An item whose id Seerr gave to another request is still a Seerr request, under a key of its own.
        retired = seerr_api.retired_key("seerr:requests:31", 1000)
        self.assertNotEqual(retired, seerr_api.request_key("requests", 31))
        self.assertEqual(seerr_api.request_of(retired), ("requests", 31))

    def test_a_404_is_believed_only_once_seerr_itself_answers(self) -> None:
        seerr = FakeSeerr()
        client = SeerrClient("http://seerr:5055", API_KEY, None, http=seerr.http())
        # Gone for real: Seerr answers its version, and still has no such request.
        self.assertIsNone(run(client.get_request(999)))
        self.assertEqual(
            [c.path for c in seerr.calls],
            ["/api/v1/request/999", "/api/v1/settings/about", "/api/v1/request/999"],
        )
        # A proxy that answers 404, or nothing, for every path says nothing about the request.
        for answer in (httpx.Response(404, text="404 page not found"), httpx.Response(200)):
            with self.subTest(status=answer.status_code):
                with mock.patch.object(seerr, "route", lambda *a, answer=answer: answer):
                    with self.assertRaises(delivery.DeliveryError) as caught:
                        run(client.get_request(999))
                self.assertIsNone(caught.exception.status)
                self.assertIn("not taken as removed", str(caught.exception))

    def test_tmdb_is_asked_for_the_record_of_the_id_and_not_searched(self) -> None:
        asked: list[str] = []

        def handle(request: httpx.Request) -> httpx.Response:
            asked.append(request.url.path)
            return httpx.Response(200, json={
                "title": "Some Film", "original_title": "Some Film", "release_date": "1999-03-31",
                "external_ids": {"imdb_id": "tt0000603"}, "genres": [], "keywords": {"keywords": []},
            })

        client = TMDBClient()
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        with mock.patch.object(TMDBClient, "_api_key", "k"), mock.patch.object(TMDBClient, "_language", "en-US"):
            result = run(client.by_id(FILM, MediaType.MOVIE))
        self.assertEqual(asked, [f"/3/movie/{FILM}"])
        self.assertEqual((result.tmdb_id, result.title, result.year), (FILM, "Some Film", 1999))
        self.assertEqual((result.match_mode, result.exact_title_hits), (MATCH_WITH_YEAR, 1))


class HookBodyTests(unittest.TestCase):
    def test_both_shapes_are_read(self) -> None:
        for payload in (default_payload(request_id=31), flat_payload(request_id=31)):
            with self.subTest(shape="nested" if "request" in payload else "flat"):
                self.assertEqual(seerr_source.read_hook(payload), seerr_source.Hook("MEDIA_PENDING", 31))

    def test_a_number_or_its_text_is_a_request_id_and_nothing_else_is(self) -> None:
        for raw, expected in (
            (31, 31), ("31", 31), (" 31 ", 31), ("", None), ("x", None), (None, None), (True, None), ("-1", None),
            # A digit to str.isdigit, and no number to int().
            ("\u00b2", None), ("\u2460", None),
        ):
            with self.subTest(raw=raw):
                self.assertEqual(seerr_source.read_hook({"notification_type": "x", "request_id": raw}).request_id, expected)

    def test_seasons_become_the_item_s_season(self) -> None:
        for numbers, text in (
            ([2], "S02"), ([1, 2, 3], "S01-S03"), ([3, 1], "S01 S03"), ([1, 2, 4, 5, 7, 8], "S1-S2 S4-S5 S7-S8"),
            ([0], "S00"), (list(range(1, 25, 2)), None),
        ):
            with self.subTest(numbers=numbers):
                found = seerr_source.season_text(numbers)
                self.assertEqual(found, text)
                if text:
                    self.assertLessEqual(len(text), 20)
                    self.assertEqual(delivery.seasons_of(found), sorted(numbers))


class SchedulerTests(unittest.TestCase):
    def test_each_seerr_source_gets_a_poll_at_its_own_interval(self) -> None:
        cfg = config(poll_minutes=7)
        made = scheduler.Scheduler()
        self.assertEqual(made.add_seerr_poll_jobs(cfg), 1)
        [job] = made._scheduler.get_jobs()
        self.assertEqual((job.id, job.args, job.func), (f"seerr_poll_{KEY}", (KEY,), seerr_source.poll_job))
        self.assertEqual(job.trigger.interval, timedelta(minutes=7))
        # The first read comes soon after the start, not a whole interval later.
        self.assertLess(job.next_run_time - datetime.now(timezone.utc), timedelta(minutes=1))

    def test_no_seerr_source_no_poll(self) -> None:
        made = scheduler.Scheduler()
        self.assertEqual(made.add_seerr_poll_jobs(policy_fixture.config()), 0)
        self.assertEqual(made._scheduler.get_jobs(), [])


class ConfigTests(unittest.TestCase):
    BASE = (
        "integrations:\n  seerr: {url: 'http://seerr:5055', api_key: k}\n  tmdb: {api_key: k}\n"
        "web: {auth: {trusted_header: Remote-User}}\n"
    )

    def load(self, sources: str, base: str | None = None) -> Config:
        text = (self.BASE if base is None else base) + "sources:\n" + sources
        return load_text(text, env={"SEERR_HOOK": SECRET})

    def test_the_secret_is_a_secret_read_from_the_environment(self) -> None:
        cfg = self.load("  - {key: requests, kind: seerr, webhook_secret: '${SEERR_HOOK}'}\n")
        source = cfg.sources[0]
        self.assertEqual(source.webhook_secret.get_secret_value(), SECRET)
        self.assertNotIn(SECRET, repr(cfg))
        self.assertEqual((source.poll_minutes, source.allowed_senders), (5, "anyone"))
        self.assertEqual(warnings(cfg), [])

    def test_it_is_optional(self) -> None:
        cfg = self.load("  - {key: requests, kind: seerr}\n")
        self.assertIsNone(cfg.sources[0].webhook_secret)
        self.assertEqual(warnings(cfg), [])

    def test_a_short_secret_and_one_on_another_kind_are_errors(self) -> None:
        for sources, fragment in (
            ("  - {key: requests, kind: seerr, webhook_secret: short}\n", "at least 16 characters"),
            ("  - {key: inbox, kind: bot, chat: private, webhook_secret: 0123456789abcdef}\n", "seerr sources only"),
            # Three a header can never match: the route would answer 401 for ever.
            ("  - {key: requests, kind: seerr, webhook_secret: 'Bearer 0123456789abcdef'}\n", "must not start with"),
            ("  - {key: requests, kind: seerr, webhook_secret: 'contrase\u00f1a-0123456789abcdef'}\n", "plain ASCII"),
            ("  - {key: requests, kind: seerr, webhook_secret: ' 0123456789abcdef '}\n", "no space at either end"),
        ):
            with self.subTest(fragment=fragment):
                with self.assertRaises(ConfigError) as caught:
                    self.load(sources, self.BASE + "telegram: {bot_token: t}\n")
                self.assertIn(fragment, "\n".join(caught.exception.errors))
                self.assertNotIn("0123456789abcdef", "\n".join(caught.exception.errors))

    def test_what_makes_a_seerr_source_take_nothing_is_a_warning(self) -> None:
        cases = (
            ("  - {key: requests, kind: seerr, allowed_senders: reviewers}\n", self.BASE, "Seerr user id"),
            (
                "  - {key: requests, kind: seerr}\n",
                "integrations:\n  seerr: {url: 'http://seerr:5055', api_key: k}\nweb: {auth: {trusted_header: R}}\n",
                "integrations.tmdb is not enabled",
            ),
            (
                "  - {key: requests, kind: seerr, webhook_secret: '${SEERR_HOOK}'}\n",
                "integrations:\n  seerr: {url: 'http://seerr:5055', api_key: k}\n  tmdb: {api_key: k}\n",
                "there is no web block",
            ),
            ("  - {key: a, kind: seerr}\n  - {key: b, kind: seerr}\n", self.BASE, "more than one seerr source"),
        )
        for sources, base, fragment in cases:
            with self.subTest(fragment=fragment):
                texts = [text for _loc, text in warnings(self.load(sources, base))]
                self.assertTrue(any(fragment in text for text in texts), texts)

    def test_a_seerr_source_without_the_integration_is_still_an_error(self) -> None:
        with self.assertRaises(ConfigError) as caught:
            self.load("  - {key: requests, kind: seerr}\n", "")
        self.assertIn("a seerr source needs integrations.seerr", "\n".join(caught.exception.errors))

    def test_what_starts_says_whether_the_source_has_a_webhook(self) -> None:
        cfg = self.load("  - {key: requests, kind: seerr, poll_minutes: 3, webhook_secret: '${SEERR_HOOK}'}\n")
        self.assertIn(("seerr-source", "requests: webhook plus a poll every 3 min"), components(cfg))
        cfg = self.load("  - {key: requests, kind: seerr, poll_minutes: 3}\n")
        self.assertIn(("seerr-source", "requests: a poll every 3 min, no webhook"), components(cfg))


class DocsTests(unittest.TestCase):
    doc = (_ROOT / "docs" / "CONFIG.md").read_text()

    def test_seerr_sources_are_no_longer_listed_as_not_enforced(self) -> None:
        pending = self.doc.split("## Not enforced yet", 1)[1]
        self.assertNotIn("seerr", pending.lower())

    def test_the_seerr_section_says_what_to_paste_and_what_to_watch_for(self) -> None:
        section = self.doc.split("#### Seerr requests", 1)[1].split("\n### ", 1)[0]
        for phrase in (
            "/hooks/seerr/<source key>", "Authorization Header", "`webhook_secret`", "JSON Payload",
            '"notification_type": "{{notification_type}}"', '"request_id": "{{request_id}}"',
            "Request Pending Approval", "Manage Requests", "Auto-Approve", "`poll_minutes`",
            "`X-API-User`", "never sends it again", "decided in Seerr", "`auto_approve: shadow`", "`deliver`",
            "`allowed_senders`", "`sender`", "404", "4K",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, section)

    def test_the_json_template_in_the_docs_is_one_the_hook_reads(self) -> None:
        import json
        import re

        section = self.doc.split("#### Seerr requests", 1)[1].split("\n### ", 1)[0]
        blocks = re.findall(r"```json\n(.*?)```", section, re.DOTALL)
        self.assertEqual(len(blocks), 1)
        template = json.loads(blocks[0])
        # What Seerr's agent does with the template: fill the variables in.
        filled = json.loads(
            json.dumps(template).replace("{{notification_type}}", "MEDIA_PENDING").replace("{{request_id}}", "31")
        )
        self.assertEqual(seerr_source.read_hook(filled), seerr_source.Hook("MEDIA_PENDING", 31))

    def test_the_new_field_and_the_event_are_documented(self) -> None:
        table = self.doc.split("### `sources`")[1].split("#### What a source reads")[0]
        self.assertIn("| `webhook_secret` | secret |", table)
        self.assertIn("`webhook_test`", self.doc.split("### `notify`")[1].split("### `web`")[0])

    def test_the_example_file_carries_a_commented_seerr_source_that_validates(self) -> None:
        import textwrap

        example = (_ROOT / "config.example.yaml").read_text()
        start = example.index("#   - key: requests")
        lines = []
        for line in example[start:].splitlines():
            if not line.startswith("#   "):
                break
            lines.append(line[2:])
        block = "\n".join(lines)
        self.assertIn("kind: seerr", block)
        self.assertIn("webhook_secret: ${SEERR_WEBHOOK_SECRET}", block)
        cfg = load_text(
            "integrations:\n  seerr: {url: 'http://seerr:5055', api_key: k}\n  tmdb: {api_key: k}\n"
            "web: {auth: {trusted_header: R}}\nsources:\n" + textwrap.indent(textwrap.dedent(block), "  "),
            env={"SEERR_WEBHOOK_SECRET": SECRET},
        )
        self.assertEqual((cfg.sources[0].kind, cfg.sources[0].poll_minutes), ("seerr", 5))
        self.assertEqual(warnings(cfg), [])


if __name__ == "__main__":
    unittest.main()
