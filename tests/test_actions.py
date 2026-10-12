"""The five actions a person takes on an item, and the surfaces that call them.

The web UI and the bot used to carry their own copy of each action, and the
copies drifted. These tests drive `vettarr.actions` against an in-memory
store that evaluates the real SQLAlchemy statements (their WHERE clauses,
values and RETURNING columns), so a condition dropped from a query changes
what the store does and fails a test, instead of a fake that decides the rule
for itself.
"""

import asyncio
import logging
import os
import pathlib
import tempfile
import unittest
from collections import namedtuple
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

from sqlalchemy.sql import operators
from sqlalchemy.sql.dml import Update
from sqlalchemy.sql.elements import (
    BinaryExpression,
    BindParameter,
    BooleanClauseList,
    Grouping,
    Null,
    UnaryExpression,
)
from sqlalchemy.sql.schema import Column

from vettarr import actions, review
from vettarr.config import runtime
from vettarr.config.schema import Config
from vettarr.models import (
    OP_MANUAL_DOWNLOAD,
    OP_RESCAN,
    ContentCheck,
    Download,
    DownloadStatus,
    Event,
    ItemStatus,
    MediaItem,
    MediaType,
    Recommendation,
    TorrentSearch,
)
from vettarr.searcher import search_service
from vettarr.searcher.prowlarr_client import (
    ProwlarrClient,
    SearchResult,
    TorrentResult,
)
from vettarr.searcher.qbittorrent_client import AddOutcome
from vettarr.web import cards

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)

# ---------------------------------------------------------------------------
# An in-memory store that runs the statements it is handed
# ---------------------------------------------------------------------------

_MODELS = {cls.__table__.name: cls for cls in (MediaItem, TorrentSearch, Download, ContentCheck, Event)}


def _value(expr, row):
    if isinstance(expr, Grouping):
        return _value(expr.element, row)
    if isinstance(expr, BindParameter):
        return expr.effective_value
    if isinstance(expr, Null):
        return None
    if isinstance(expr, Column):
        return getattr(row, expr.key)
    raise NotImplementedError(f"cannot evaluate {type(expr).__name__}: {expr}")


def _matches(criterion, row) -> bool:
    if isinstance(criterion, Grouping):
        return _matches(criterion.element, row)
    if isinstance(criterion, BooleanClauseList):
        results = [_matches(c, row) for c in criterion.clauses]
        return all(results) if criterion.operator is operators.and_ else any(results)
    if isinstance(criterion, BinaryExpression):
        left = _value(criterion.left, row)
        right = _value(criterion.right, row)
        op = criterion.operator
        if op is operators.in_op:
            return left in right
        if op is operators.not_in_op:
            return left not in right
        if op is operators.is_:
            return left is right
        if op is operators.is_not:
            return left is not right
        if left is None or right is None:
            return False
        return bool(op(left, right))
    raise NotImplementedError(f"cannot evaluate {type(criterion).__name__}")


class _Scalars:
    def __init__(self, values):
        self._values = list(values)

    def all(self):
        return list(self._values)

    def first(self):
        return self._values[0] if self._values else None

    def __iter__(self):
        return iter(self._values)


class _Result:
    def __init__(self, rows):
        self._rows = list(rows)

    def all(self):
        return list(self._rows)

    def one_or_none(self):
        assert len(self._rows) <= 1, self._rows
        return self._rows[0] if self._rows else None

    def scalar_one_or_none(self):
        row = self.one_or_none()
        if row is None:
            return None
        return row[0] if isinstance(row, tuple) else row

    def scalars(self):
        return _Scalars(r[0] if isinstance(r, tuple) else r for r in self._rows)


class Store:
    def __init__(self):
        self.rows = {cls: {} for cls in _MODELS.values()}
        self._next_id = 1000
        self.statements = []

    def put(self, obj):
        if obj.id is None:
            obj.id = self._next_id
            self._next_id += 1
        if isinstance(obj, TorrentSearch) and obj.searched_at is None:
            obj.searched_at = T0 + timedelta(seconds=obj.id)
        self.rows[type(obj)][obj.id] = obj
        return obj

    def session(self):
        return FakeSession(self)

    def all(self, cls):
        return list(self.rows[cls].values())


class FakeSession:
    def __init__(self, store: Store):
        self.store = store
        self.pending = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, cls, ident, **_kwargs):
        return self.store.rows[cls].get(ident)

    def add(self, obj):
        self.pending.append(obj)

    async def flush(self):
        for obj in self.pending:
            self.store.put(obj)
        self.pending.clear()

    async def commit(self):
        await self.flush()

    async def execute(self, statement):
        self.store.statements.append(statement)
        if isinstance(statement, Update):
            return self._update(statement)
        return self._select(statement)

    def _update(self, statement):
        cls = _MODELS[statement.table.name]
        hit = [
            row for row in self.store.rows[cls].values()
            if statement.whereclause is None or _matches(statement.whereclause, row)
        ]
        for row in hit:
            for column, value in statement._values.items():
                key = column if isinstance(column, str) else column.key
                setattr(row, key, value.effective_value if isinstance(value, BindParameter) else value)
        if not statement._returning:
            return _Result([])
        keys = [c.key for c in statement._returning]
        Row = namedtuple("Row", keys)
        return _Result([Row(*(getattr(r, k) for k in keys)) for r in hit])

    def _select(self, statement):
        described = statement.column_descriptions
        entity = described[0]["entity"]
        rows = [
            row for row in self.store.rows[entity].values()
            if statement.whereclause is None or _matches(statement.whereclause, row)
        ]
        for clause in reversed(statement._order_by_clauses):
            descending = False
            if isinstance(clause, UnaryExpression):
                descending = clause.modifier is operators.desc_op
                clause = clause.element
            rows.sort(key=lambda r, c=clause: getattr(r, c.key), reverse=descending)
        if statement._limit is not None:
            rows = rows[: statement._limit]
        if all(d["expr"] is d["entity"] for d in described):
            return _Result(rows)
        keys = [d["name"] for d in described]
        return _Result([tuple(getattr(r, k) for k in keys) for r in rows])


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def make_item(store: Store, item_id: int, **kwargs) -> MediaItem:
    item = MediaItem(
        id=item_id,
        telegram_message_id=5000 + item_id,
        telegram_chat_id=-1,
        raw_text="Una Serie Inventada S02 1080p",
        title="Una Serie Inventada",
        year=2024,
        season="S02",
        quality="1080p",
        media_type=MediaType.SERIES,
        status=ItemStatus.PENDING_REVIEW,
        created_at=T0 + timedelta(minutes=item_id),
    )
    for key, value in kwargs.items():
        setattr(item, key, value)
    return store.put(item)


def release(i: int = 1, **kwargs) -> TorrentResult:
    values = dict(
        guid=f"guid-{i}", title=f"Una Serie Inventada S02 1080p WEB-DL-{i}",
        size=1_000_000_000 * i, seeders=10 + i, indexer="tracker-a",
        download_url=f"http://indexer.invalid/dl/{i}", magnet_url=None,
        info_url=f"http://indexer.invalid/{i}",
    )
    values.update(kwargs)
    return TorrentResult(**values)


class ActionTestCase(unittest.TestCase):
    """Patches the store and the two outside clients into every module."""

    def setUp(self) -> None:
        self.store = Store()
        self.prowlarr = SimpleNamespace(
            build_query=mock.Mock(side_effect=ProwlarrClient.build_query),
            search=mock.AsyncMock(return_value=SearchResult(results=[release(1), release(2)])),
        )
        self.qbittorrent = SimpleNamespace(
            add_torrent=mock.AsyncMock(return_value=AddOutcome(hash="abc", created=True)),
            remove_torrent=mock.AsyncMock(return_value=True),
        )
        for patcher in (
            mock.patch.object(actions, "async_session", self.store.session),
            mock.patch.object(review, "async_session", self.store.session),
            mock.patch.object(search_service, "prowlarr", self.prowlarr),
            mock.patch.object(search_service, "qbittorrent", self.qbittorrent),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_(self, coro):
        return asyncio.run(coro)


# ---------------------------------------------------------------------------
# approve
# ---------------------------------------------------------------------------


class ApproveTests(ActionTestCase):
    def test_a_reviewable_item_is_queued_for_the_worker(self) -> None:
        item = make_item(self.store, 1)
        result = self.run_(actions.approve(1))
        self.assertTrue(result.ok)
        self.assertEqual(item.status, ItemStatus.APPROVED)
        self.assertIsNotNone(item.reviewed_at)
        self.assertIn("Una Serie Inventada", result.message)

    def test_an_item_the_worker_holds_is_a_conflict(self) -> None:
        """The web answers this with 409 and a toast, not a silent success."""
        item = make_item(self.store, 1, status=ItemStatus.SEARCHING)
        result = self.run_(actions.approve(1))
        self.assertTrue(result.conflict)
        self.assertEqual(result.status, actions.Status.BUSY)
        self.assertEqual(result.message, actions.BUSY_MESSAGE)
        self.assertEqual(item.status, ItemStatus.SEARCHING)

    def test_a_missing_item_is_not_found(self) -> None:
        result = self.run_(actions.approve(99))
        self.assertEqual(result.status, actions.Status.NOT_FOUND)
        self.assertFalse(result.conflict)

    def test_an_item_without_a_title_is_refused_with_what_to_do(self) -> None:
        """A post nothing could read was saved as APPROVED with no title, and
        the answer said it was queued for download. It is refused instead, with
        the two things that help, and never as "already being processed"."""
        item = make_item(self.store, 1, title=None, status=ItemStatus.PARSE_FAILED)
        result = self.run_(actions.approve(1))
        self.assertFalse(result.ok)
        self.assertEqual(result.message, actions.NO_TITLE_MESSAGE)
        self.assertNotEqual(result.message, actions.BUSY_MESSAGE)
        self.assertEqual(item.status, ItemStatus.PARSE_FAILED)

    def test_an_untitled_post_with_its_own_magnet_is_approved(self) -> None:
        """A magnet in the post is something to fetch, title or not."""
        item = make_item(self.store, 1, title=None, status=ItemStatus.PARSE_FAILED)
        with mock.patch.object(actions.ingest, "direct_add_of", return_value="magnet"):
            result = self.run_(actions.approve(1))
        self.assertTrue(result.ok)
        self.assertEqual(item.status, ItemStatus.APPROVED)

    def test_approve_many_leaves_an_untitled_post(self) -> None:
        make_item(self.store, 1)
        untitled = make_item(self.store, 2, title=None, status=ItemStatus.PARSE_FAILED)
        self.assertEqual(self.run_(actions.approve_many([1, 2])), [1])
        self.assertEqual(untitled.status, ItemStatus.PARSE_FAILED)

    def test_approve_many_takes_only_the_reviewable_ones(self) -> None:
        make_item(self.store, 1)
        make_item(self.store, 2, status=ItemStatus.DOWNLOADING)
        make_item(self.store, 3, status=ItemStatus.NO_MATCH)
        self.assertEqual(sorted(self.run_(actions.approve_many(["1", 2, 3]))), [1, 3])


# ---------------------------------------------------------------------------
# reject
# ---------------------------------------------------------------------------


class RejectTests(ActionTestCase):
    def test_a_rejection_deletes_the_post_and_the_torrent_it_added(self) -> None:
        item = make_item(self.store, 1, status=ItemStatus.DOWNLOADING)
        self.store.put(Download(
            id=7, media_item_id=1, qbittorrent_hash="abc", created_torrent=True,
            status=DownloadStatus.DOWNLOADING,
        ))
        ingester = SimpleNamespace(after=mock.AsyncMock(return_value="deleted"))
        result = self.run_(actions.reject(1, ingester))
        self.assertTrue(result.ok)
        self.assertTrue(result.deletes_post)
        self.assertEqual(result.message, "Rejected, and its post was deleted.")
        self.assertEqual(item.status, ItemStatus.REJECTED)
        self.qbittorrent.remove_torrent.assert_awaited_once_with("abc")
        ingester.after.assert_awaited_once_with("reject", 1)

    def test_the_answer_says_what_happened_to_the_post(self) -> None:
        """The source's on_reject decides, and its default is to leave the
        post alone: the answer used to say "deleted" whatever happened."""
        unchanged = "Rejected. Its post could not be changed and is still there."
        cases = [
            ("deleted", "Rejected, and its post was deleted.", True),
            ("reacted", "Rejected, and its post got a reaction.", False),
            ("none", "Rejected. Its post was left in place.", False),
            ("refused", unchanged, False),
            ("unavailable", unchanged, False),
            ("failed", unchanged, False),
        ]
        for item_id, (done, message, deleted) in enumerate(cases, start=1):
            with self.subTest(done=done):
                make_item(self.store, item_id)
                ingester = SimpleNamespace(after=mock.AsyncMock(return_value=done))
                result = self.run_(actions.reject(item_id, ingester))
                self.assertEqual((result.message, result.deletes_post), (message, deleted))
                # A green toast said the post could not be changed: it is an error's colour now.
                self.assertEqual(result.post_unchanged, message == unchanged)
                self.assertEqual(cards.toast_kind(result), "error" if message == unchanged else "ok")

    def test_a_rejection_with_nothing_connected_to_the_post_leaves_it(self) -> None:
        make_item(self.store, 1)
        result = self.run_(actions.reject(1, None))
        self.assertEqual((result.message, result.deletes_post), ("Rejected. Its post was left in place.", False))

    def test_a_parse_failure_is_rejected_but_its_post_is_kept(self) -> None:
        make_item(self.store, 1, status=ItemStatus.PARSE_FAILED)
        ingester = SimpleNamespace(after=mock.AsyncMock())
        result = self.run_(actions.reject(1, ingester))
        self.assertTrue(result.ok)
        self.assertFalse(result.deletes_post)
        ingester.after.assert_not_awaited()

    def test_an_item_the_worker_holds_is_a_conflict(self) -> None:
        make_item(self.store, 1, status=ItemStatus.SEARCHING)
        result = self.run_(actions.reject(1, None))
        self.assertTrue(result.conflict)
        self.assertEqual(result.message, actions.BUSY_MESSAGE)

    def test_a_missing_item_is_not_found_not_busy(self) -> None:
        result = self.run_(actions.reject(99, None))
        self.assertEqual(result.status, actions.Status.NOT_FOUND)
        self.assertEqual(result.message, actions.NOT_FOUND_MESSAGE)

    def test_a_finished_item_says_what_it_is_not_being_processed(self) -> None:
        """A stale bot card can still confirm a rejection on a done item."""
        for status in (ItemStatus.COMPLETED, ItemStatus.REJECTED):
            with self.subTest(status=status):
                item = make_item(self.store, 1, status=status)
                result = self.run_(actions.reject(1, None))
                self.assertFalse(result.ok)
                self.assertNotEqual(result.message, actions.BUSY_MESSAGE)
                self.assertEqual(result.message, f"This item is already {status.value}.")
                self.assertEqual(item.status, status)


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


class SearchTests(ActionTestCase):
    def test_the_query_carries_the_season_never_the_quality(self) -> None:
        """/rescan passed the quality where build_query expects the season."""
        make_item(self.store, 1, status=ItemStatus.NO_MATCH)
        self.run_(actions.search(1, from_statuses={ItemStatus.NO_MATCH}, op=OP_RESCAN))
        self.prowlarr.build_query.assert_called_once_with("Una Serie Inventada", 2024, "S02")
        query = self.prowlarr.search.await_args.args[0]
        self.assertEqual(query, "Una Serie Inventada 2024 S02")
        self.assertNotIn("1080p", query)

    def test_a_book_is_searched_without_its_year_like_the_worker(self) -> None:
        make_item(self.store, 1, media_type=MediaType.EBOOK, season=None,
                  title="Un Titulo - Nombre Ejemplo")
        self.run_(actions.search(1))
        self.prowlarr.build_query.assert_called_once_with("Un Titulo - Nombre Ejemplo", None, None)

    def test_found_releases_are_stored_and_the_item_waits_for_a_pick(self) -> None:
        item = make_item(self.store, 1, status=ItemStatus.NO_MATCH)
        result = self.run_(actions.search(1))
        self.assertTrue(result.ok)
        self.assertEqual(item.status, ItemStatus.PENDING_REVIEW)
        self.assertIsNone(item.error_message)
        [ts] = self.store.all(TorrentSearch)
        self.assertEqual(result.search_id, ts.id)
        self.assertEqual([r["guid"] for r in result.releases], ["guid-1", "guid-2"])
        self.assertEqual(ts.prowlarr_results[0]["info_url"], "http://indexer.invalid/1")
        self.assertEqual(ts.best_match_guid, "guid-1")
        self.assertEqual(result.message, "Found 2 releases.")

    def test_nothing_found_is_no_match(self) -> None:
        self.prowlarr.search.return_value = SearchResult(results=[])
        item = make_item(self.store, 1)
        result = self.run_(actions.search(1))
        self.assertTrue(result.ok)
        self.assertEqual(item.status, ItemStatus.NO_MATCH)
        self.assertEqual(result.message, "No releases found.")

    def test_tmdb_s_title_is_tried_when_the_post_s_finds_nothing(self) -> None:
        """The worker always did this; the web and the bot never did."""
        self.prowlarr.search.side_effect = [
            SearchResult(results=[]), SearchResult(results=[release(3)]),
        ]
        make_item(self.store, 1)
        self.store.put(ContentCheck(id=1, media_item_id=1, tmdb_title="An Invented Series"))
        result = self.run_(actions.search(1))
        self.assertEqual([r["guid"] for r in result.releases], ["guid-3"])
        self.assertEqual(self.prowlarr.search.await_count, 2)

    def test_prowlarr_down_is_an_error_the_item_shows(self) -> None:
        self.prowlarr.search.return_value = SearchResult(error="ConnectError")
        item = make_item(self.store, 1)
        result = self.run_(actions.search(1))
        self.assertEqual(result.status, actions.Status.FAILED)
        self.assertEqual(item.status, ItemStatus.ERROR)
        self.assertEqual(item.error_message, "Prowlarr search unavailable")

    def test_a_search_that_did_not_complete_stores_nothing(self) -> None:
        """An empty row stored for an outage became the item's latest search:
        it said "the last search found nothing" and folded the releases of the
        search before it, with their Grab buttons, under "Earlier search"."""
        self.prowlarr.search.return_value = SearchResult(error="ConnectError")
        make_item(self.store, 1)
        result = self.run_(actions.search(1))
        self.assertTrue(result.ran)
        self.assertIsNone(result.search_id)
        self.assertEqual(self.store.all(TorrentSearch), [])

    def test_with_nothing_to_search_with_nothing_is_claimed_or_stored(self) -> None:
        """No Prowlarr and no provider with its own search: the item stayed in
        Needs you no longer, it moved to Error with an empty search stored."""
        item = make_item(self.store, 1)
        bare = Config.model_validate({})
        with mock.patch.object(runtime, "_config", bare):
            result = self.run_(actions.search(1))
        self.assertEqual(result.status, actions.Status.FAILED)
        self.assertFalse(result.ran)
        self.assertEqual(result.message, actions.NO_SEARCH_MESSAGE)
        self.assertEqual(item.status, ItemStatus.PENDING_REVIEW)
        self.assertEqual(self.store.all(TorrentSearch), [])
        self.prowlarr.search.assert_not_awaited()

    DIRECT = Config.model_validate({
        "integrations": {"prowlarr": {"url": "http://prowlarr", "api_key": "k"}},
        "providers": [{
            "key": "direct", "base_url": "https://direct.example", "search": "unit3d",
            "auth": {"from": "cookie_file", "cookie_file": "/nonexistent"},
        }],
    })

    def search_with_a_direct_provider(self, answer):
        """A person's search of item 1 when Prowlarr has nothing and a provider searches directly."""
        client = SimpleNamespace(name="direct", search_with_status=mock.Mock(return_value=answer))
        with (
            mock.patch.object(runtime, "_config", self.DIRECT),
            mock.patch.object(search_service, "_trackers", [client]),
            mock.patch.object(search_service, "init_trackers", lambda: None),
        ):
            return client, self.run_(actions.search(1))

    def test_a_direct_search_gets_the_ids_the_worker_s_gets(self) -> None:
        """The worker's ItemRef carries the ids; a person's search asked by name alone,
        so the two could list different films of the same name."""
        self.prowlarr.search.return_value = SearchResult(results=[])
        make_item(self.store, 1)
        self.store.put(ContentCheck(id=1, media_item_id=1, tmdb_id=603, imdb_id="tt0133093"))
        client, result = self.search_with_a_direct_provider(([], None))
        client.search_with_status.assert_called_once_with(
            "Una Serie Inventada", imdb_id="tt0133093", tmdb_id=603,
        )
        self.assertEqual(result.message, "No releases found.")

    def test_an_item_never_matched_is_searched_by_name(self) -> None:
        self.prowlarr.search.return_value = SearchResult(results=[])
        make_item(self.store, 1)
        client, _ = self.search_with_a_direct_provider(([], None))
        client.search_with_status.assert_called_once_with("Una Serie Inventada", imdb_id=None, tmdb_id=None)

    def test_every_search_down_reads_as_it_does_for_the_worker(self) -> None:
        self.prowlarr.search.return_value = SearchResult(error="ConnectError")
        item = make_item(self.store, 1)
        _, result = self.search_with_a_direct_provider(([], "session expired"))
        self.assertEqual(result.status, actions.Status.FAILED)
        self.assertEqual(item.status, ItemStatus.ERROR)
        self.assertEqual(item.error_message, search_service.ALL_UNAVAILABLE)
        self.assertEqual(result.message, "All search providers unavailable.")

    def test_an_item_the_worker_holds_is_a_conflict_and_nothing_is_searched(self) -> None:
        make_item(self.store, 1, status=ItemStatus.SEARCHING)
        result = self.run_(actions.search(1))
        self.assertTrue(result.conflict)
        self.prowlarr.search.assert_not_awaited()

    def test_rescan_only_claims_no_match_items(self) -> None:
        item = make_item(self.store, 1, status=ItemStatus.PENDING_REVIEW)
        result = self.run_(actions.search(1, from_statuses={ItemStatus.NO_MATCH}, op=OP_RESCAN))
        self.assertTrue(result.conflict)
        self.assertEqual(item.status, ItemStatus.PENDING_REVIEW)

    def test_an_item_without_a_title_is_refused_without_a_claim(self) -> None:
        """It used to be claimed, then crash in build_query, and sit in SEARCHING."""
        item = make_item(self.store, 1, title=None, status=ItemStatus.PARSE_FAILED)
        result = self.run_(actions.search(1))
        self.assertEqual(result.status, actions.Status.FAILED)
        self.assertEqual(item.status, ItemStatus.PARSE_FAILED)
        self.prowlarr.search.assert_not_awaited()


# ---------------------------------------------------------------------------
# grab
# ---------------------------------------------------------------------------


class GrabTests(ActionTestCase):
    def _searched(self, **item_kwargs):
        item = make_item(self.store, 1, **item_kwargs)
        ts = self.store.put(TorrentSearch(
            id=50, media_item_id=1,
            prowlarr_results=[
                {"title": "Release A", "download_url": "http://indexer.invalid/a", "indexer": "tracker-a"},
                {"title": "Release B", "magnet_url": "magnet:?xt=urn:btih:b", "indexer": "tracker-a"},
            ],
        ))
        return item, ts

    def test_the_picked_release_is_added_and_recorded(self) -> None:
        item, _ts = self._searched()
        result = self.run_(actions.grab(1, 50, 1))
        self.assertTrue(result.ok)
        self.assertEqual(result.title, "Release B")
        self.qbittorrent.add_torrent.assert_awaited_once_with(
            MediaType.SERIES, magnet_url="magnet:?xt=urn:btih:b",
            download_url=None, expected_title="Release B",
        )
        [dl] = self.store.all(Download)
        self.assertEqual(dl.torrent_search_id, 50)
        self.assertEqual(dl.status, DownloadStatus.DOWNLOADING)
        self.assertTrue(dl.created_torrent)
        self.assertEqual(item.status, ItemStatus.DOWNLOADING)
        self.assertIsNotNone(item.reviewed_at)

    def test_every_grab_writes_the_download_category(self) -> None:
        """Only the bot used to write it, and it wrote the indexer name.

        The category is the one integrations.qbittorrent.categories names.
        """
        cfg = runtime.from_environment(dict(os.environ))
        cfg.integrations.qbittorrent.categories = {"series": "tv-shows"}
        self._searched()
        with mock.patch.object(runtime, "_config", cfg):
            self.run_(actions.grab(1, 50, 0))
        [dl] = self.store.all(Download)
        self.assertEqual(dl.category, "tv-shows")

    def test_qbittorrent_refusing_is_a_failure_the_item_shows(self) -> None:
        self.qbittorrent.add_torrent.return_value = None
        item, _ts = self._searched()
        result = self.run_(actions.grab(1, 50, 0))
        self.assertEqual(result.status, actions.Status.FAILED)
        self.assertEqual(item.status, ItemStatus.ERROR)
        [dl] = self.store.all(Download)
        self.assertEqual(dl.status, DownloadStatus.ERROR)

    def test_a_failed_grab_says_why_on_the_item_not_the_claim_marker(self) -> None:
        """The item showed "operation:manual-download" as its error, and the
        toast blamed qBittorrent when the tracker had refused the .torrent."""
        self.qbittorrent.add_torrent.return_value = None
        item, _ts = self._searched()
        result = self.run_(actions.grab(1, 50, 0))
        self.assertEqual(item.error_message, search_service.CLIENT_REFUSED)
        self.assertEqual(result.message, search_service.CLIENT_REFUSED)
        refused = search_service.AddRefused("The tracker refused the .torrent: northwind has no session.")
        item.status, item.error_message = ItemStatus.PENDING_REVIEW, None
        with mock.patch.object(search_service, "add_release", mock.AsyncMock(return_value=refused)):
            result = self.run_(actions.grab(1, 50, 0))
        self.assertEqual(item.status, ItemStatus.ERROR)
        self.assertEqual(item.error_message, refused.reason)
        self.assertEqual(result.message, refused.reason)
        self.assertFalse(item.error_message.startswith("operation:"))

    def test_an_index_off_the_list_is_not_found(self) -> None:
        """A negative index used to pick from the end of the list."""
        self._searched()
        for index in (-1, 2, 99):
            with self.subTest(index=index):
                result = self.run_(actions.grab(1, 50, index))
                self.assertEqual(result.status, actions.Status.NOT_FOUND)
        self.qbittorrent.add_torrent.assert_not_awaited()

    def test_another_item_s_search_is_not_found(self) -> None:
        self._searched()
        make_item(self.store, 2)
        result = self.run_(actions.grab(2, 50, 0))
        self.assertEqual(result.status, actions.Status.NOT_FOUND)

    def test_an_item_the_worker_holds_is_a_conflict(self) -> None:
        item, _ts = self._searched(status=ItemStatus.SEARCHING)
        result = self.run_(actions.grab(1, 50, 0))
        self.assertTrue(result.conflict)
        self.qbittorrent.add_torrent.assert_not_awaited()
        self.assertEqual(item.status, ItemStatus.SEARCHING)

    def test_the_claim_is_stamped_as_a_manual_download(self) -> None:
        """record_download only moves an item still carrying this stamp."""
        self._searched()
        seen = []

        async def _add(*_a, **_k):
            seen.append(self.store.rows[MediaItem][1].error_message)
            return AddOutcome(hash="h", created=True)

        self.qbittorrent.add_torrent.side_effect = _add
        self.run_(actions.grab(1, 50, 0))
        self.assertEqual(seen, [OP_MANUAL_DOWNLOAD])


# ---------------------------------------------------------------------------
# skip
# ---------------------------------------------------------------------------


class SkipTests(ActionTestCase):
    def test_skip_moves_on_to_the_next_item(self) -> None:
        """It answered with the oldest pending item: the one just skipped."""
        make_item(self.store, 1)
        make_item(self.store, 2)
        result = self.run_(actions.skip(1))
        self.assertEqual(result.next_item.id, 2)

    def test_skip_never_answers_with_the_skipped_item(self) -> None:
        make_item(self.store, 1)
        make_item(self.store, 2)
        make_item(self.store, 3)
        for skipped in (1, 2, 3):
            with self.subTest(skipped=skipped):
                self.assertNotEqual(self.run_(actions.skip(skipped)).next_item.id, skipped)

    def test_skip_wraps_round_to_the_oldest(self) -> None:
        make_item(self.store, 1)
        make_item(self.store, 2)
        self.assertEqual(self.run_(actions.skip(2)).next_item.id, 1)

    def test_skipping_the_only_item_leaves_nothing(self) -> None:
        make_item(self.store, 1)
        result = self.run_(actions.skip(1))
        self.assertIsNone(result.next_item)
        self.assertIn("Nothing else", result.message)

    def test_skip_changes_nothing_about_the_item(self) -> None:
        item = make_item(self.store, 1)
        make_item(self.store, 2)
        self.run_(actions.skip(1))
        self.assertEqual(item.status, ItemStatus.PENDING_REVIEW)
        self.assertIsNone(item.reviewed_at)

    def test_items_queued_at_the_same_moment_still_advance(self) -> None:
        make_item(self.store, 1, created_at=T0)
        make_item(self.store, 2, created_at=T0)
        self.assertEqual(self.run_(actions.skip(1)).next_item.id, 2)
        self.assertEqual(self.run_(actions.skip(2)).next_item.id, 1)

    def test_only_items_waiting_for_a_decision_come_next(self) -> None:
        make_item(self.store, 1)
        make_item(self.store, 2, status=ItemStatus.DOWNLOADING)
        make_item(self.store, 3)
        self.assertEqual(self.run_(actions.skip(1)).next_item.id, 3)


# ---------------------------------------------------------------------------
# rerun
# ---------------------------------------------------------------------------


class RerunTests(ActionTestCase):
    """`rerun` puts an undecided item back to waiting and has the hourly re-check decide it."""

    def rerun(self, item_id: int, decided: str | None = "review", *, lookup_missed: bool = False):
        seen = {}

        async def recheck(limit, *, only_ids, decisions, push_held, keep_match, missed):
            item = self.store.rows[MediaItem].get(only_ids[0])
            seen.update(
                limit=limit, only_ids=only_ids, push_held=push_held, keep_match=keep_match,
                status=item.status if item else None,
            )
            if lookup_missed:
                # What the re-check does then: nothing written, the claim released.
                missed.append(only_ids[0])
                return 0
            if decided:
                decisions.append(decided)
            return 1 if decided else 0

        with mock.patch("vettarr.pipeline.recheck_waiting_items", recheck):
            return self.run_(actions.rerun(item_id)), seen

    def test_a_held_item_goes_back_to_waiting_and_is_decided_again(self) -> None:
        item = make_item(self.store, 1, error_message="imdb.violence: severe")
        result, seen = self.rerun(1, "approve")
        self.assertTrue(result.ok)
        self.assertEqual(result.message, "The policy approved it. It is queued for download.")
        self.assertEqual(seen, {
            "limit": 1, "only_ids": [1], "push_held": False, "keep_match": True,
            "status": ItemStatus.AWAITING_DATA,
        })
        self.assertIsNone(item.error_message)

    def test_each_outcome_has_its_own_words(self) -> None:
        said = {}
        for n, outcome in enumerate(("approve", "review", "wait", "reject"), start=1):
            make_item(self.store, n)
            said[outcome] = self.rerun(n, outcome)[0].message
        self.assertEqual(len(set(said.values())), 4)
        self.assertIn("leaves it to you", said["review"])
        self.assertIn("waiting for data", said["wait"])
        self.assertIn("rejected", said["reject"])

    def test_a_waiting_item_and_one_the_policy_rejected_can_be_run_again(self) -> None:
        make_item(self.store, 1, status=ItemStatus.AWAITING_DATA)
        make_item(self.store, 2, status=ItemStatus.REJECTED, policy_outcome="reject", reviewed_at=None)
        self.assertTrue(self.rerun(1)[0].ok)
        self.assertTrue(self.rerun(2)[0].ok)

    def test_an_item_a_person_decided_or_the_worker_holds_is_a_conflict(self) -> None:
        make_item(self.store, 1, status=ItemStatus.REJECTED, policy_outcome="reject", reviewed_at=T0)
        make_item(self.store, 2, status=ItemStatus.REJECTED, policy_outcome="review", reviewed_at=None)
        for n, status in enumerate((ItemStatus.APPROVED, ItemStatus.SEARCHING, ItemStatus.DOWNLOADING,
                                    ItemStatus.COMPLETED, ItemStatus.NO_MATCH, ItemStatus.CHECKING), start=3):
            make_item(self.store, n, status=status)
        for item_id in range(1, 9):
            with self.subTest(item=item_id):
                before = self.store.rows[MediaItem][item_id].status
                result, seen = self.rerun(item_id)
                self.assertTrue(result.conflict)
                self.assertEqual(seen, {})
                self.assertEqual(self.store.rows[MediaItem][item_id].status, before)

    def test_a_missing_item_is_not_found(self) -> None:
        result, seen = self.rerun(404)
        self.assertEqual(result.status, actions.Status.NOT_FOUND)
        self.assertEqual(seen, {})

    def test_a_lookup_that_found_nothing_changes_nothing(self) -> None:
        """One click wiped the poster, the ratings and the finding whenever TMDB did not answer."""
        held = make_item(self.store, 1, error_message="imdb.frightening: severe")
        refused = make_item(self.store, 2, status=ItemStatus.REJECTED, policy_outcome="reject", reviewed_at=None)
        for item, status, error in ((held, ItemStatus.PENDING_REVIEW, "imdb.frightening: severe"),
                                    (refused, ItemStatus.REJECTED, None)):
            with self.subTest(status=status):
                result, seen = self.rerun(item.id, lookup_missed=True)
                self.assertEqual(seen["status"], ItemStatus.AWAITING_DATA)
                self.assertEqual(result.status, actions.Status.FAILED)
                self.assertEqual(result.message, actions.RERUN_LOOKUP_MISSED)
                self.assertIn("nothing was changed", result.message)
                # Back where it was, not left waiting for the hourly re-check to decide.
                self.assertEqual((item.status, item.error_message), (status, error))

    def test_a_run_that_decided_nothing_puts_a_held_item_back(self) -> None:
        item = make_item(self.store, 1, error_message="imdb.violence: severe")
        result, _ = self.rerun(1, decided=None)
        self.assertEqual(result.status, actions.Status.FAILED)
        self.assertEqual(result.message, actions.RERUN_NOT_RUN)
        self.assertEqual((item.status, item.error_message), (ItemStatus.PENDING_REVIEW, "imdb.violence: severe"))

    def test_a_waiting_item_that_was_not_decided_keeps_waiting(self) -> None:
        item = make_item(self.store, 1, status=ItemStatus.AWAITING_DATA)
        result, _ = self.rerun(1, decided=None)
        self.assertEqual(result.status, actions.Status.FAILED)
        self.assertIn("tried again within the hour", result.message)
        self.assertEqual(item.status, ItemStatus.AWAITING_DATA)

    def test_a_decision_a_person_took_meanwhile_is_not_put_back(self) -> None:
        item = make_item(self.store, 1)

        async def recheck(limit, **_kwargs):
            item.status = ItemStatus.REJECTED  # a person, while the lookups ran
            return 0

        with mock.patch("vettarr.pipeline.recheck_waiting_items", recheck):
            result = self.run_(actions.rerun(1))
        self.assertEqual(result.status, actions.Status.FAILED)
        self.assertEqual(item.status, ItemStatus.REJECTED)

    def test_a_decision_made_without_a_match_says_so(self) -> None:
        make_item(self.store, 1)
        self.store.put(ContentCheck(id=1, media_item_id=1, tmdb_id=None))
        make_item(self.store, 2)
        self.store.put(ContentCheck(id=2, media_item_id=2, tmdb_id=603))
        make_item(self.store, 3, media_type=MediaType.EBOOK)  # never looked up on TMDB
        self.assertEqual(
            self.rerun(1)[0].message, "The policy still leaves it to you." + actions.RERUN_NO_MATCH,
        )
        self.assertIn("decided without that data", actions.RERUN_NO_MATCH)
        self.assertEqual(self.rerun(2)[0].message, "The policy still leaves it to you.")
        self.assertEqual(self.rerun(3)[0].message, "The policy still leaves it to you.")


# ---------------------------------------------------------------------------
# The surfaces: the web and the bot render what the actions return
# ---------------------------------------------------------------------------


class WebSurfaceTests(unittest.TestCase):
    def setUp(self) -> None:
        from fastapi.testclient import TestClient

        from vettarr.web import app as web_app

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        real_path = pathlib.Path

        def _path(*args):
            if args == ("/data/photos",):
                return real_path(tmp.name)
            return real_path(*args)

        # The layout's banner reads the events table on a full page; no
        # database runs under these tests.
        from vettarr.web import shell

        from vettarr.web import cards

        banner = mock.patch.object(shell, "failing", mock.AsyncMock(return_value=[]))
        banner.start()
        self.addCleanup(banner.stop)
        # What the item page reads; a test sets the item it should find.
        self.item = make_item(Store(), 1, auto_recommendation=Recommendation.NEUTRAL)
        self.detail_of = cards.detail_of
        loaded = mock.patch.object(cards, "load_detail", mock.AsyncMock(return_value=cards.detail_of(self.item)))
        self.load_detail = loaded.start()
        self.addCleanup(loaded.stop)
        with mock.patch.object(web_app, "Path", _path):
            self.client = TestClient(web_app.create_web_app(ingester=None))

    def _post(self, url, result, action):
        with mock.patch.object(actions, action, mock.AsyncMock(return_value=result)):
            return self.client.post(url, headers={"HX-Request": "true"}, follow_redirects=False)

    def test_the_item_page_says_when_approving_adds_the_post_s_own_torrent(self) -> None:
        """Approving a direct add downloads what the post carried, not a
        search result, and the page has to say so before the click."""
        for carried in ("magnet", None):
            with self.subTest(carried=carried):
                self.load_detail.return_value = self.detail_of(self.item, direct_add=carried)
                resp = self.client.get("/item/1")
                self.assertEqual(resp.status_code, 200)
                said = "Approving downloads the magnet this post carries, as it is. Nothing is searched" in resp.text
                self.assertEqual(said, carried is not None)

    def test_a_busy_item_answers_409_with_the_message_as_text(self) -> None:
        busy = actions.ActionResult(1, actions.Status.BUSY, actions.BUSY_MESSAGE)
        for url, action in (
            ("/item/1/approve", "approve"),
            ("/item/1/reject", "reject"),
            ("/item/1/search", "search"),
            ("/item/1/download/5/0", "grab"),
        ):
            with self.subTest(url=url):
                resp = self._post(url, busy, action)
                self.assertEqual(resp.status_code, 409)
                self.assertEqual(resp.text, actions.BUSY_MESSAGE)
                self.assertTrue(resp.headers["content-type"].startswith("text/html"))

    def test_a_vanished_release_answers_404_not_a_json_200(self) -> None:
        gone = actions.ActionResult(1, actions.Status.NOT_FOUND, "That release is no longer listed.")
        resp = self._post("/item/1/download/5/9", gone, "grab")
        self.assertEqual(resp.status_code, 404)
        self.assertIn("no longer listed", resp.text)

    def _toast(self, resp) -> dict:
        import json

        return json.loads(resp.headers["HX-Trigger"])["toast"]

    def test_an_approval_answers_with_a_fragment(self) -> None:
        ok = actions.ActionResult(1, actions.Status.OK, "Queued x.")
        resp = self._post("/item/1/approve", ok, "approve")
        self.assertEqual(resp.status_code, 200)
        self.assertIn('<article id="item-panel"', resp.text)
        self.assertNotIn("<html", resp.text)
        self.assertEqual(self._toast(resp), {"text": "Queued x.", "kind": "ok"})

    def test_a_search_answers_htmx_with_the_item_as_it_is_now(self) -> None:
        ok = actions.SearchOutcome(1, actions.Status.OK, "Found 1 release.", search_id=3)
        resp = self._post("/item/1/search", ok, "search")
        self.assertEqual(resp.status_code, 200)
        self.assertIn('<article id="item-panel"', resp.text)
        self.assertEqual(self._toast(resp), {"text": "Found 1 release.", "kind": "ok"})

    def test_a_search_that_never_ran_answers_422_with_the_reason(self) -> None:
        """Without this the click reloads the same page and says nothing."""
        never_ran = actions.SearchOutcome(
            1, actions.Status.FAILED, "This item has no title to search for.",
        )
        resp = self._post("/item/1/search", never_ran, "search")
        self.assertEqual(resp.status_code, 422)
        self.assertEqual(resp.text, "This item has no title to search for.")
        self.assertNotIn("HX-Trigger", resp.headers)

    def test_a_search_that_ran_and_failed_still_shows_the_item_and_says_why(self) -> None:
        """Prowlarr down: the item moved to Error, so it is drawn again and the toast is an error."""
        failed = actions.SearchOutcome(
            1, actions.Status.FAILED, "Prowlarr search unavailable.", ran=True,
        )
        resp = self._post("/item/1/search", failed, "search")
        self.assertEqual(resp.status_code, 200)
        self.assertIn('<article id="item-panel"', resp.text)
        self.assertEqual(self._toast(resp), {"text": "Prowlarr search unavailable.", "kind": "error"})

    def test_without_htmx_a_search_and_a_grab_redirect_to_the_item(self) -> None:
        for url, action, result in (
            ("/item/1/search", "search",
             actions.SearchOutcome(1, actions.Status.OK, "Found 1 release.", search_id=3)),
            ("/item/1/download/3/0", "grab",
             actions.ActionResult(1, actions.Status.OK, "Downloading x.")),
        ):
            with self.subTest(url=url):
                with mock.patch.object(actions, action, mock.AsyncMock(return_value=result)):
                    resp = self.client.post(url, follow_redirects=False)
                self.assertEqual(resp.status_code, 303)
                self.assertEqual(resp.headers["location"], "/item/1")

    def test_the_message_is_escaped(self) -> None:
        busy = actions.ActionResult(1, actions.Status.BUSY, "<script>x</script>")
        resp = self._post("/item/1/approve", busy, "approve")
        self.assertNotIn("<script>", resp.text)

    def test_the_page_turns_an_error_answer_into_a_toast(self) -> None:
        """htmx swaps nothing on a 4xx; without this listener a 409 is silent."""
        import vettarr.web as web

        web_dir = pathlib.Path(web.__file__).parent
        self.assertIn("htmx:responseError", (web_dir / "static" / "app.js").read_text())
        base = (web_dir / "templates" / "base.html").read_text()
        self.assertIn('src="/static/app.js"', base)
        self.assertIn('id="toast"', base)


class BotSurfaceTests(unittest.TestCase):
    def setUp(self) -> None:
        from vettarr.bot import handlers

        self.handlers = handlers
        self.query = SimpleNamespace(
            message=SimpleNamespace(chat_id=77, photo=None),
            answer=mock.AsyncMock(),
        )

    def test_skip_shows_the_next_item_not_the_same_one(self) -> None:
        nxt = MediaItem(id=2, title="Otra")
        skip = mock.AsyncMock(return_value=actions.SkipResult(
            1, actions.Status.OK, "Skipped.", next_item=nxt,
        ))
        with (
            mock.patch.object(actions, "skip", skip),
            mock.patch.object(self.handlers, "_respond", mock.AsyncMock()),
            mock.patch.object(self.handlers, "_send_review", mock.AsyncMock()) as send,
        ):
            asyncio.run(self.handlers._handle_skip(self.query, 1))
        skip.assert_awaited_once_with(1)
        send.assert_awaited_once_with(77, nxt)

    def test_the_review_keyboard_has_a_releases_button(self) -> None:
        from vettarr.bot.keyboards import review_keyboard

        for flagged in (False, True):
            with self.subTest(flagged=flagged):
                data = [
                    b.callback_data
                    for row in review_keyboard(9, flagged).inline_keyboard
                    for b in row
                ]
                self.assertIn("releases:9", data)

    def _callback(self, data: str):
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=self.handlers.settings.telegram_owner_id),
            callback_query=SimpleNamespace(
                data=data, answer=mock.AsyncMock(), message=self.query.message,
            ),
        )
        asyncio.run(self.handlers.on_callback(update, None))

    def test_the_releases_button_is_routed(self) -> None:
        with mock.patch.object(self.handlers, "_handle_releases", mock.AsyncMock()) as handle:
            self._callback("releases:9")
        handle.assert_awaited_once()
        self.assertEqual(handle.await_args.args[1], 9)

    def test_a_release_button_grabs_from_the_search_it_was_drawn_from(self) -> None:
        from vettarr.bot.keyboards import torrent_keyboard

        button = torrent_keyboard(9, [{"seeders": 1, "size": 1, "indexer": "x"}], 41)
        data = button.inline_keyboard[0][0].callback_data
        grab = mock.AsyncMock(return_value=actions.ActionResult(9, actions.Status.OK, "ok", "R"))
        with (
            mock.patch.object(actions, "grab", grab),
            mock.patch.object(self.handlers, "_respond", mock.AsyncMock()),
            mock.patch.object(self.handlers, "_send_next", mock.AsyncMock()),
        ):
            self._callback(data)
        grab.assert_awaited_once_with(9, 41, 0)

    def test_releases_shows_the_stored_list_without_searching_again(self) -> None:
        store = Store()
        make_item(store, 9)
        ts = store.put(TorrentSearch(id=41, media_item_id=9, prowlarr_results=[
            {"title": "R", "seeders": 3, "size": 10, "indexer": "x"},
        ]))
        search = mock.AsyncMock()
        with (
            mock.patch.object(actions, "latest_search", mock.AsyncMock(return_value=ts)),
            mock.patch.object(actions, "search", search),
            mock.patch.object(self.handlers, "async_session", store.session),
            mock.patch.object(self.handlers, "_respond", mock.AsyncMock()) as respond,
        ):
            asyncio.run(self.handlers._handle_releases(self.query, 9))
        search.assert_not_awaited()
        markup = respond.await_args.kwargs["reply_markup"]
        self.assertEqual(markup.inline_keyboard[0][0].callback_data, "torrent:9:41:0")

    def test_releases_searches_when_nothing_is_stored(self) -> None:
        with (
            mock.patch.object(actions, "latest_search", mock.AsyncMock(return_value=None)),
            mock.patch.object(self.handlers, "_handle_research", mock.AsyncMock()) as research,
        ):
            asyncio.run(self.handlers._handle_releases(self.query, 9))
        research.assert_awaited_once_with(self.query, 9)

    def test_rescan_searches_no_match_items_through_the_shared_action(self) -> None:
        store = Store()
        make_item(store, 1, status=ItemStatus.NO_MATCH)
        make_item(store, 2, status=ItemStatus.PENDING_REVIEW)
        search = mock.AsyncMock(return_value=actions.SearchOutcome(
            1, actions.Status.OK, "Found 1 release.", search_id=5, releases=[{"title": "R"}],
        ))
        message = SimpleNamespace(reply_text=mock.AsyncMock())
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=self.handlers.settings.telegram_owner_id),
            message=message,
        )
        with (
            mock.patch.object(self.handlers, "async_session", store.session),
            mock.patch.object(actions, "search", search),
        ):
            asyncio.run(self.handlers.cmd_rescan(update, None))
        search.assert_awaited_once_with(1, from_statuses={ItemStatus.NO_MATCH}, op=OP_RESCAN)
        self.assertIn("1/1", message.reply_text.await_args.args[0])


if __name__ == "__main__":
    unittest.main()
