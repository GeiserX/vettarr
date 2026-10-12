"""What the download row records about where a torrent came from.

That row is the only lasting account of whether this item is the reason a
torrent is in qBittorrent, and a rejection reads it before deleting files off
the disk. Every rule protecting the library rests on this one assignment.
"""

import asyncio
import logging
import os
import unittest
from dataclasses import dataclass, field
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

from vettarr.config import runtime
from vettarr.config.schema import Config
from vettarr.models import Download, DownloadStatus, MediaType
from vettarr.searcher import search_service
from vettarr.searcher.qbittorrent_client import AddOutcome


@dataclass
class _Release:
    guid: str = "guid-1"
    title: str = "Una Pelicula Inventada 2024 1080p WEB-DL-GRUPO"
    size: int = 8_400_000_000
    seeders: int = 20
    indexer: str = "someindexer"
    download_url: str = "http://indexer.invalid/download/1"
    magnet_url: str | None = None
    info_url: str = "http://indexer.invalid/1"


@dataclass
class _SearchResult:
    results: list = field(default_factory=list)
    error: str | None = None


class _Result:
    def scalar_one_or_none(self):
        return None

    def scalars(self):
        return self

    def first(self):
        return None


class _Session:
    def __init__(self):
        self.added = []

    async def execute(self, _statement):
        return _Result()

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        return None

    async def commit(self):
        return None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def download(self) -> Download:
        rows = [o for o in self.added if isinstance(o, Download)]
        assert len(rows) == 1, rows
        return rows[0]


def run_search(outcome):
    """One Prowlarr hit, handed to a qBittorrent that answers `outcome`."""
    session = _Session()
    item = search_service.ItemRef(
        id=7, title="Una Pelicula Inventada", year=2024, quality="1080p",
        season=None, media_type=MediaType.MOVIE, telegram_message_id=1,
    )
    prowlarr = SimpleNamespace(
        build_query=lambda *a, **k: "query",
        search=mock.AsyncMock(return_value=_SearchResult(results=[_Release()])),
    )
    with (
        mock.patch.object(search_service, "async_session", lambda: session),
        mock.patch.object(search_service, "prowlarr", prowlarr),
        mock.patch.object(search_service, "init_trackers", lambda: None),
        mock.patch.object(
            search_service, "qbittorrent",
            SimpleNamespace(add_torrent=mock.AsyncMock(return_value=outcome)),
        ),
    ):
        session.outcome = asyncio.run(search_service.search_and_download(item))
    return session


class DownloadRowTests(unittest.TestCase):
    def test_a_torrent_this_search_added_is_recorded_as_ours(self) -> None:
        session = run_search(AddOutcome(hash="abc123", created=True))
        download = session.download()
        self.assertTrue(download.created_torrent)
        self.assertEqual(download.qbittorrent_hash, "abc123")
        self.assertEqual(download.status, DownloadStatus.DOWNLOADING)
        self.assertEqual(session.outcome, "downloading")

    def test_a_torrent_qbittorrent_already_held_is_recorded_as_not_ours(self) -> None:
        """Rejecting this item must not delete a copy that was there first."""
        session = run_search(AddOutcome(hash="abc123", created=False))
        download = session.download()
        self.assertFalse(download.created_torrent)
        self.assertEqual(download.qbittorrent_hash, "abc123")
        self.assertEqual(download.status, DownloadStatus.DOWNLOADING)

    def test_a_failed_add_leaves_an_error_row_owning_nothing(self) -> None:
        session = run_search(None)
        download = session.download()
        self.assertEqual(download.status, DownloadStatus.ERROR)
        self.assertIsNone(download.qbittorrent_hash)
        self.assertFalse(download.created_torrent)
        self.assertIsNone(download.started_at)
        self.assertEqual(session.outcome, "error")

    def test_an_accepted_add_with_no_hash_records_no_hash(self) -> None:
        session = run_search(AddOutcome(hash=None, created=False))
        self.assertIsNone(session.download().qbittorrent_hash)
        self.assertEqual(session.download().status, DownloadStatus.DOWNLOADING)


# A bencoded torrent with an info dictionary, which is all the hash parser
# asks of a body before it counts as a torrent.
TORRENT = b"d8:announce10:http://x/a4:infod6:lengthi1e4:name1:x12:piece lengthi1e6:pieces0:ee"
LINK = "https://tracker.invalid/index.php?page=torrent-details-cultura&id=" + "ab" * 20 + ")"
NUMERIC_LINK = "https://www.tracker.invalid/details.php?id=52117"


LINKED_CONFIG = Config.model_validate({
    "integrations": {"prowlarr": {"url": "http://prowlarr", "api_key": "k"}},
    "providers": [{
        "key": "tracker-x",
        "base_url": "https://www.tracker.invalid",
        "aliases": ["tracker.invalid"],
        "links": [
            {
                "pattern": r"index\.php\?page=torrent-details[^&\s]*&id=(?P<id>[0-9a-fA-F]{40})",
                "download_url": "{base}/download.php?id={id}",
                "examples": [{
                    "url": "https://tracker.invalid/index.php?page=torrent-details&id=" + "ab" * 20,
                    "id": "ab" * 20,
                }],
            },
            {
                "pattern": r"details\.php\?id=(?P<id>\d+)",
                "download_url": "{base}/download.php?id={id}",
                "examples": [{"url": "https://tracker.invalid/details.php?id=42", "id": "42"}],
            },
        ],
    }],
})


def run_linked(outcome, *, cookie="uid=1", body=TORRENT, links=(LINK,)):
    """A post linking its torrent, handed to a qBittorrent that answers `outcome`.

    The provider's client answers the fetch with `body`, or with nothing (a
    login page, an error) when `body` is None.
    """
    session = _Session()
    item = search_service.ItemRef(
        id=7, title="Una Revista - Octubre", year=2026, quality=None,
        season=None, media_type=MediaType.EBOOK, telegram_message_id=1,
        external_links=list(links),
    )
    prowlarr = SimpleNamespace(
        build_query=lambda *a, **k: "query",
        search=mock.AsyncMock(return_value=_SearchResult(results=[_Release()])),
        cookie_for=mock.AsyncMock(return_value=cookie),
    )
    client = SimpleNamespace(
        name="tracker-x",
        use_cookie_header=mock.Mock(),
        fetch_torrent=mock.Mock(return_value=body),
    )
    qbittorrent = SimpleNamespace(
        add_torrent=mock.AsyncMock(return_value=outcome),
        add_torrent_file=mock.AsyncMock(return_value=outcome),
    )
    with (
        mock.patch.object(runtime, "_config", LINKED_CONFIG),
        mock.patch.object(search_service, "async_session", lambda: session),
        mock.patch.object(search_service, "prowlarr", prowlarr),
        mock.patch.object(search_service, "_trackers", [client]),
        mock.patch.object(search_service, "init_trackers", lambda: None),
        mock.patch.object(search_service, "qbittorrent", qbittorrent),
    ):
        session.outcome = asyncio.run(search_service.search_and_download(item))
    session.prowlarr, session.client, session.qbittorrent = prowlarr, client, qbittorrent
    return session


class LinkedTorrentTests(unittest.TestCase):
    def test_the_linked_torrent_is_added_without_a_search(self) -> None:
        session = run_linked(AddOutcome(hash="abc123", created=True))
        self.assertEqual(session.outcome, "downloading")
        session.prowlarr.search.assert_not_awaited()
        session.qbittorrent.add_torrent.assert_not_awaited()
        session.qbittorrent.add_torrent_file.assert_awaited_once_with(TORRENT, MediaType.EBOOK)
        download = session.download()
        self.assertEqual(
            download.download_url,
            "https://www.tracker.invalid/download.php?id=" + "ab" * 20,
        )
        self.assertIsNone(download.torrent_search_id)
        self.assertTrue(download.created_torrent)
        self.assertEqual(download.status, DownloadStatus.DOWNLOADING)

    def test_a_numeric_details_link_names_the_torrent_too(self) -> None:
        """The tracker's new software links details.php?id=N, download.php?id=N."""
        session = run_linked(AddOutcome(hash="abc123", created=True), links=(NUMERIC_LINK,))
        self.assertEqual(session.outcome, "downloading")
        session.prowlarr.search.assert_not_awaited()
        self.assertEqual(
            session.download().download_url, "https://www.tracker.invalid/download.php?id=52117",
        )

    def test_the_fetch_carries_the_indexers_cookie(self) -> None:
        session = run_linked(AddOutcome(hash="abc123", created=True))
        [provider] = LINKED_CONFIG.providers
        session.prowlarr.cookie_for.assert_awaited_once_with(provider)
        session.client.use_cookie_header.assert_called_once_with("uid=1")
        session.client.fetch_torrent.assert_called_once_with(
            "https://www.tracker.invalid/download.php?id=" + "ab" * 20,
        )

    def test_a_torrent_qbittorrent_already_held_is_recorded_as_not_ours(self) -> None:
        session = run_linked(AddOutcome(hash="abc123", created=False))
        self.assertFalse(session.download().created_torrent)
        self.assertEqual(session.outcome, "downloading")

    def test_a_linked_torrent_this_add_created_is_thanked_for_once(self) -> None:
        [provider] = LINKED_CONFIG.providers
        for outcome, thanked in (
            (AddOutcome(hash="abc123", created=True), True),
            (AddOutcome(hash="abc123", created=False), False),
            (None, False),
        ):
            with self.subTest(outcome=outcome):
                thank = mock.AsyncMock()
                with mock.patch.object(search_service, "thank_after_download", thank):
                    run_linked(outcome)
                if thanked:
                    thank.assert_awaited_once_with(provider, "ab" * 20)
                else:
                    thank.assert_not_awaited()

    def test_a_refused_add_is_an_error_not_a_search(self) -> None:
        session = run_linked(None)
        self.assertEqual(session.outcome, "error")
        self.assertEqual(session.download().status, DownloadStatus.ERROR)
        session.prowlarr.search.assert_not_awaited()

    def test_no_session_for_that_tracker_falls_back_to_the_search(self) -> None:
        session = run_linked(AddOutcome(hash="abc123", created=True), cookie=None)
        session.client.fetch_torrent.assert_not_called()
        session.prowlarr.search.assert_awaited_once()
        session.qbittorrent.add_torrent.assert_awaited_once()
        self.assertEqual(session.outcome, "downloading")

    def test_no_torrent_from_the_tracker_falls_back_to_the_search(self) -> None:
        """A login page, an error or a redirect: the client answers None."""
        session = run_linked(AddOutcome(hash="abc123", created=True), body=None)
        session.prowlarr.search.assert_awaited_once()
        self.assertEqual(session.outcome, "downloading")

    def test_a_post_without_a_tracker_link_searches(self) -> None:
        session = run_linked(
            AddOutcome(hash="abc123", created=True),
            links=("https://www.instagram.com/reel/x/",),
        )
        session.prowlarr.cookie_for.assert_not_awaited()
        session.prowlarr.search.assert_awaited_once()

    def test_a_link_on_a_subdomain_is_not_the_provider_s(self) -> None:
        session = run_linked(
            AddOutcome(hash="abc123", created=True),
            links=("https://cdn.tracker.invalid/details.php?id=52117",),
        )
        session.prowlarr.cookie_for.assert_not_awaited()
        session.client.fetch_torrent.assert_not_called()
        session.prowlarr.search.assert_awaited_once()

    def test_a_link_no_pattern_reads_searches(self) -> None:
        session = run_linked(
            AddOutcome(hash="abc123", created=True),
            links=("https://www.tracker.invalid/browse.php?cat=7",),
        )
        session.client.fetch_torrent.assert_not_called()
        session.prowlarr.search.assert_awaited_once()


def run_query(media_type):
    """The query handed to Prowlarr for an item of `media_type` posted with a year."""
    session = _Session()
    item = search_service.ItemRef(
        id=7, title="Un Titulo - Nombre Ejemplo", year=2026, quality=None,
        season=None, media_type=media_type, telegram_message_id=1,
    )
    prowlarr = SimpleNamespace(
        build_query=mock.Mock(return_value="query"),
        search=mock.AsyncMock(return_value=_SearchResult(results=[_Release()])),
    )
    with (
        mock.patch.object(search_service, "async_session", lambda: session),
        mock.patch.object(search_service, "prowlarr", prowlarr),
        mock.patch.object(search_service, "init_trackers", lambda: None),
        mock.patch.object(
            search_service, "qbittorrent",
            SimpleNamespace(add_torrent=mock.AsyncMock(return_value=AddOutcome("h", True))),
        ),
    ):
        asyncio.run(search_service.search_and_download(item))
    return prowlarr.build_query.call_args.args


class QueryYearTests(unittest.TestCase):
    """The tracker matches the keywords as one piece of the title, and a book's
    year sits between dashes after the author, so it must stay out."""

    def test_a_book_is_searched_without_its_year(self) -> None:
        for media_type in (MediaType.EBOOK, MediaType.AUDIOBOOK, MediaType.COMIC):
            with self.subTest(media_type=media_type):
                self.assertEqual(run_query(media_type), ("Un Titulo - Nombre Ejemplo", None, None))

    def test_a_film_keeps_its_year(self) -> None:
        self.assertEqual(run_query(MediaType.MOVIE), ("Un Titulo - Nombre Ejemplo", 2026, None))

    def test_a_magazine_keeps_its_year(self) -> None:
        """Magazine titles read "Name - Month Year", so the year is part of the match."""
        self.assertEqual(run_query(MediaType.MAGAZINE), ("Un Titulo - Nombre Ejemplo", 2026, None))


if __name__ == "__main__":
    unittest.main()
