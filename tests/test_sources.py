"""Sources: every way a post comes in, and what happens to it afterwards.

One ``ingest()`` serves the userbot and the bot. These tests drive it with
each kind of message, check that each source's history is read as its
``backfill`` says, that ``on_download`` and ``on_reject`` act on the item's
own chat through the right client, and that the bot turns away whoever the
source does not let in. The database and Telegram are faked.
"""

import asyncio
import contextlib
import datetime
import importlib.util
import os
import pathlib
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

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

from telegram import (
    Chat,
    Message,
    MessageEntity,
    MessageOriginChannel,
    MessageOriginUser,
    PhotoSize,
    Update,
    User,
)
from telegram.constants import MessageEntityType

from vettarr import ingest, pipeline
from vettarr.checker.imdb_client import ParentsGuide
from vettarr.bot import handlers
from vettarr.config.schema import Config
from vettarr.ingester import telegram_reader
from vettarr.models import ItemStatus, MediaType
from vettarr.searcher import search_service
from vettarr.searcher.search_service import ItemRef
from policy_fixture import FLAGGED_GENRE_ID
from test_pipeline_auto_approve import (
    FakeResult,
    FakeSession,
    full_guide,
    make_item,
    matched_tmdb,
    run_process,
    session_factory,
)

REVIEWER = 1001
OTHER_REVIEWER = 1002
STRANGER = 2002
GROUP = -1001234567890
INBOX_USER_CHAT = 1001
HASH = "a" * 40


def config(**overrides) -> Config:
    doc = {
        "telegram": {
            "api_id": 1, "api_hash": "h", "bot_token": "t",
            "reviewers": [REVIEWER, OTHER_REVIEWER],
        },
        "sources": [
            {"key": "films", "kind": "userbot", "chat": GROUP,
             "on_download": "delete", "on_reject": "react"},
            {"key": "inbox", "kind": "bot", "chat": "private",
             "on_download": "react", "on_reject": "delete"},
            {"key": "club", "kind": "bot", "chat": -1009999,
             "allowed_senders": "anyone", "on_download": "delete", "notify_on_review": True},
        ],
        # A torrent a post carries is added as it is only by the download client.
        "deliver": {"movie": "qbittorrent", "series": "qbittorrent"},
    }
    doc.update(overrides)
    return Config.model_validate(doc)


def bencode(value) -> bytes:
    if isinstance(value, int):
        return b"i%de" % value
    if isinstance(value, str):
        value = value.encode()
    if isinstance(value, bytes):
        return b"%d:%s" % (len(value), value)
    if isinstance(value, list):
        return b"l" + b"".join(bencode(v) for v in value) + b"e"
    return b"d" + b"".join(bencode(k) + bencode(v) for k, v in sorted(value.items())) + b"e"


def torrent(name: str, files: list[str] | None = None) -> bytes:
    info: dict = {"name": name, "piece length": 16384, "pieces": b"\0" * 20}
    if files:
        info["files"] = [{"length": 1, "path": [f]} for f in files]
    else:
        info["length"] = 1
    return bencode({"announce": "http://tracker.invalid/announce", "info": info})


class Harness:
    """Patches the config and the two database calls ``ingest`` makes.

    ``existing`` is the id of the item the message already is, if any.
    """

    def __init__(self, cfg: Config | None = None, existing: int | None = None, new_id: int = 77):
        self.cfg = cfg or config()
        self.inserted: list[dict] = []
        self.existing = existing
        self.new_id = new_id
        self.queue: asyncio.Queue[int] = asyncio.Queue()
        #: Every message the "is it an item already" lookup was asked about.
        self.checked: list[ingest.Incoming] = []

    async def _insert(self, values):
        self.inserted.append(values)
        return self.new_id

    async def _existing(self, message):
        self.checked.append(message)
        return self.existing

    def __enter__(self):
        self.stack = contextlib.ExitStack()
        self.stack.enter_context(mock.patch.object(ingest.settings, "_config", self.cfg))
        self.stack.enter_context(mock.patch.object(ingest, "_insert", self._insert))
        self.stack.enter_context(mock.patch.object(ingest, "_existing", self._existing))
        ingest.set_queue(self.queue)
        self.stack.callback(ingest.set_queue, None)
        return self

    def __exit__(self, *exc):
        self.stack.close()
        return False

    def source(self, key: str):
        return next(s for s in self.cfg.sources if s.key == key)

    def run(self, key: str, message: ingest.Incoming) -> ingest.IngestResult:
        return asyncio.run(ingest.ingest(self.source(key), message))


def tmdb_answers(answers: dict):
    """Patch TMDB: path -> JSON body. Records each call."""
    calls: list = []

    async def fake(path, **params):
        calls.append((path, params))
        return answers.get(path)

    return mock.patch.object(ingest, "_tmdb_get", fake), calls


# ---------------------------------------------------------------------------
# Every input kind
# ---------------------------------------------------------------------------


class InputKindTests(unittest.TestCase):
    CARD = "NEW MOVIE\nTitle: Some Film - 2024\nType: Movie - 1080p - WEB-DL - 4.2 GB"

    def test_a_post_is_stored_as_written_and_queued(self) -> None:
        with Harness() as h:
            result = h.run("films", ingest.Incoming(GROUP, 10, "Some.Film.2024.1080p.WEB-DL.x264-GRP\n4.2 GB"))
        self.assertEqual(result.outcome, "added")
        self.assertEqual(result.kind, "post")
        values = h.inserted[0]
        self.assertEqual(values["raw_text"], "Some.Film.2024.1080p.WEB-DL.x264-GRP\n4.2 GB")
        self.assertEqual(values["source_key"], "films")
        self.assertEqual(values["telegram_chat_id"], GROUP)
        self.assertEqual(values["origin_key"], f"{GROUP}:10")
        self.assertNotIn("title", values, "the parser reads a post; intake presets nothing")
        self.assertEqual(h.queue.get_nowait(), 77)

    def test_a_caption_with_a_text_link_reaches_ingest(self) -> None:
        entity = SimpleNamespace(url="https://tracker-a.example/details.php?id=42")
        message = SimpleNamespace(
            text=None, caption="Some Film 2024", entities=None, caption_entities=[entity],
            forward_origin=None, document=None, chat_id=INBOX_USER_CHAT, message_id=5,
        )
        incoming = handlers.incoming_from_bot(message, REVIEWER)
        self.assertEqual(incoming.text, "Some Film 2024\nhttps://tracker-a.example/details.php?id=42")
        self.assertEqual(incoming.sender_id, REVIEWER)
        self.assertIsNone(incoming.fetch_document)

    def test_a_forwarded_channel_post_keys_on_its_origin(self) -> None:
        origin = MessageOriginChannel(
            date=None, chat=Chat(GROUP, "channel"), message_id=321,
        )
        message = SimpleNamespace(
            text="Some Film 2024", caption=None, entities=None, caption_entities=None,
            forward_origin=origin, document=None, chat_id=INBOX_USER_CHAT, message_id=6,
        )
        incoming = handlers.incoming_from_bot(message, REVIEWER)
        self.assertEqual(incoming.forward_from, (GROUP, 321))
        self.assertEqual(incoming.origin_key, f"{GROUP}:321")
        with Harness() as h:
            h.run("inbox", incoming)
        self.assertEqual(h.inserted[0]["origin_key"], f"{GROUP}:321")
        self.assertEqual(h.inserted[0]["telegram_chat_id"], INBOX_USER_CHAT)

    def test_a_forward_from_a_person_has_no_origin_but_its_own(self) -> None:
        origin = MessageOriginUser(date=None, sender_user=User(5, "x", False))
        message = SimpleNamespace(
            text="t", caption=None, entities=None, caption_entities=None,
            forward_origin=origin, document=None, chat_id=INBOX_USER_CHAT, message_id=6,
        )
        incoming = handlers.incoming_from_bot(message, REVIEWER)
        self.assertIsNone(incoming.forward_from)
        self.assertEqual(incoming.origin_key, f"{INBOX_USER_CHAT}:6")

    def test_a_telethon_forward_keys_on_the_channel_post(self) -> None:
        from telethon.tl.types import PeerChannel

        message = SimpleNamespace(
            chat_id=INBOX_USER_CHAT, id=8, text="Some Film 2024", entities=None,
            sender_id=REVIEWER, document=None,
            fwd_from=SimpleNamespace(from_id=PeerChannel(1234567890), channel_post=321),
        )
        incoming = telegram_reader.incoming(message, client=None)
        self.assertEqual(incoming.forward_from, (GROUP, 321))

    def test_a_torrent_attachment_is_a_direct_add_named_by_the_torrent(self) -> None:
        data = torrent("Meridian.2024.1080p.WEB-DL.x264-GRP.mkv")

        async def fetch():
            return data

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(ingest, "TORRENTS_DIR", pathlib.Path(tmp)):
            with Harness() as h:
                result = h.run("inbox", ingest.Incoming(
                    INBOX_USER_CHAT, 7, "", REVIEWER,
                    document_name="meridian.torrent", fetch_document=fetch,
                ))
            saved = (pathlib.Path(tmp) / "77.torrent").read_bytes()
        self.assertEqual(result.kind, "torrent")
        self.assertEqual(result.title, "Meridian")
        values = h.inserted[0]
        self.assertEqual((values["title"], values["year"], values["media_type"]), ("Meridian", 2024, MediaType.MOVIE))
        self.assertTrue(values["raw_text"].startswith("Meridian.2024.1080p"))
        self.assertEqual(saved, data)

    def test_a_file_that_is_not_a_torrent_is_not_one(self) -> None:
        async def fetch():
            return b"<html>nope</html>"

        with Harness() as h:
            result = h.run("inbox", ingest.Incoming(
                INBOX_USER_CHAT, 7, "", REVIEWER, document_name="x.torrent", fetch_document=fetch,
            ))
        self.assertEqual(result.outcome, "skipped")
        self.assertEqual(h.inserted, [])

    def test_a_magnet_is_a_direct_add_named_by_its_display_name(self) -> None:
        magnet = f"magnet:?xt=urn:btih:{HASH}&dn=Some.Show.S02E01.720p.HDTV.x264-GRP"
        with Harness() as h:
            result = h.run("inbox", ingest.Incoming(INBOX_USER_CHAT, 8, magnet, REVIEWER))
        self.assertEqual(result.kind, "magnet")
        values = h.inserted[0]
        self.assertEqual(values["title"], "Some Show")
        self.assertEqual(values["media_type"], MediaType.SERIES)
        self.assertEqual(values["season"], "S02")
        self.assertIn(magnet, values["raw_text"])

    def test_an_imdb_link_is_named_through_tmdb_find(self) -> None:
        patch, calls = tmdb_answers({"/find/tt0111161": {
            "movie_results": [{"title": "The Shawshank Redemption", "release_date": "1994-09-23"}],
            "tv_results": [],
        }})
        with Harness() as h, patch:
            result = h.run("inbox", ingest.Incoming(
                INBOX_USER_CHAT, 9, "https://www.imdb.com/title/tt0111161/", REVIEWER,
            ))
        self.assertEqual(result.kind, "id_link")
        self.assertEqual(calls, [("/find/tt0111161", {"external_source": "imdb_id"})])
        values = h.inserted[0]
        self.assertEqual((values["title"], values["year"], values["media_type"]),
                         ("The Shawshank Redemption", 1994, MediaType.MOVIE))

    def test_a_tmdb_link_is_read_by_its_own_id(self) -> None:
        patch, calls = tmdb_answers({"/tv/1396": {"name": "Breaking Bad", "first_air_date": "2008-01-20"}})
        with Harness() as h, patch:
            h.run("inbox", ingest.Incoming(
                INBOX_USER_CHAT, 9, "https://www.themoviedb.org/tv/1396-breaking-bad", REVIEWER,
            ))
        self.assertEqual(calls[0][0], "/tv/1396")
        self.assertEqual(h.inserted[0]["media_type"], MediaType.SERIES)
        self.assertEqual(h.inserted[0]["title"], "Breaking Bad")

    def test_a_tvdb_link_is_named_through_tmdb_find(self) -> None:
        patch, calls = tmdb_answers({"/find/81189": {
            "movie_results": [], "tv_results": [{"name": "Breaking Bad", "first_air_date": "2008-01-20"}],
        }})
        with Harness() as h, patch:
            h.run("inbox", ingest.Incoming(
                INBOX_USER_CHAT, 9, "https://thetvdb.com/?tab=series&id=81189", REVIEWER,
            ))
        self.assertEqual(calls, [("/find/81189", {"external_source": "tvdb_id"})])
        self.assertEqual(h.inserted[0]["media_type"], MediaType.SERIES)

    def test_an_id_link_without_tmdb_is_left_to_the_parser(self) -> None:
        patch, _calls = tmdb_answers({})  # the integration is off: every call is None
        with Harness() as h, patch:
            result = h.run("inbox", ingest.Incoming(
                INBOX_USER_CHAT, 9, "https://www.imdb.com/title/tt0111161/", REVIEWER,
            ))
        self.assertEqual(result.kind, "post")
        self.assertNotIn("title", h.inserted[0])

    def test_a_tracker_details_link_goes_through_the_linked_torrent_path(self) -> None:
        """Through the real ``fetch_linked_torrent``: only the provider's
        session is faked, so what it returns is whatever the searcher really
        hands back, not a shape this test made up."""
        link = "https://tracker-a.example/details.php?id=42"
        cfg = config(providers=[{
            "key": "tracker-a", "base_url": "https://tracker-a.example",
            "auth": {"from": "cookie_file", "cookie_file": "/nonexistent/cookies.txt"},
            "links": [{
                "pattern": r"details\.php\?id=(?P<id>\d+)",
                "download_url": "{base}/download.php?id={id}",
                "examples": [{"url": link, "id": "42"}],
            }],
        }])
        client = mock.Mock()
        client.fetch_torrent.return_value = torrent("Some.Film.2024.1080p.BluRay.x264-GRP")
        with Harness(cfg) as h, mock.patch.object(
            search_service, "_with_session", mock.AsyncMock(return_value=client),
        ):
            result = h.run("inbox", ingest.Incoming(INBOX_USER_CHAT, 11, link, REVIEWER))
        client.fetch_torrent.assert_called_once_with("https://tracker-a.example/download.php?id=42")
        self.assertEqual(result.kind, "tracker_link")
        values = h.inserted[0]
        self.assertEqual(values["raw_text"], f"Some.Film.2024.1080p.BluRay.x264-GRP\n{link}")
        self.assertEqual((values["title"], values["year"]), ("Some Film", 2024))

    def test_a_plain_title_is_named_by_a_tmdb_search(self) -> None:
        patch, calls = tmdb_answers({"/search/multi": {"results": [
            {"media_type": "person", "name": "Meridian"},
            {"media_type": "movie", "title": "Merīdian", "original_title": "Meridian", "release_date": "2024-06-04"},
        ]}})
        with Harness() as h, patch:
            result = h.run("inbox", ingest.Incoming(INBOX_USER_CHAT, 12, "Meridian 2024", REVIEWER))
        self.assertEqual(result.kind, "title")
        self.assertEqual(calls, [("/search/multi", {"query": "Meridian"})])
        values = h.inserted[0]
        self.assertEqual((values["year"], values["media_type"]), (2024, MediaType.MOVIE))

    def test_a_title_tmdb_does_not_know_by_that_name_is_left_to_the_parser(self) -> None:
        patch, _calls = tmdb_answers({"/search/multi": {"results": [
            {"media_type": "movie", "title": "Meridian Something Else", "release_date": "2024-01-01"},
        ]}})
        with Harness() as h, patch:
            result = h.run("inbox", ingest.Incoming(INBOX_USER_CHAT, 12, "Meridian 2024", REVIEWER))
        self.assertEqual(result.kind, "post")

    def test_a_line_in_a_group_without_a_year_is_not_looked_up(self) -> None:
        patch, calls = tmdb_answers({})
        with Harness() as h, patch:
            h.run("films", ingest.Incoming(GROUP, 13, "Thanks to everyone"))
        self.assertEqual(calls, [])

    def test_an_instagram_only_post_is_skipped(self) -> None:
        with Harness() as h:
            result = h.run("films", ingest.Incoming(GROUP, 14, "https://www.instagram.com/reel/x"))
        self.assertEqual(result.outcome, "skipped")
        self.assertEqual(h.inserted, [])

    def test_release_names_and_typed_titles_split(self) -> None:
        cases = {
            "Meridian.2024.1080p.WEB-DL-GRP": ("Meridian", 2024, None),
            "Meridian 2024": ("Meridian", 2024, None),
            "Show.Name.S02E01.720p.HDTV": ("Show Name", None, "S02"),
            "1917 (2019) 2160p": ("1917", 2019, None),
            "Some Book - Some Author.epub": ("Some Book - Some Author", None, None),
        }
        for name, expected in cases.items():
            with self.subTest(name=name):
                self.assertEqual(ingest.split_release_name(name), expected)


class SourceFormatsAtIntakeTests(unittest.TestCase):
    """Intake recognises a post by the formats of the source it arrived in.

    A post with release structure is stored as it is; one without has its
    link followed first. The same card is the first in a source that reads
    cards and the second in a source that reads scene names only.
    """

    CARD = "NEW EBOOK\nTitle: An Invented Book\nLink: https://example.invalid/details/42"

    def arrive_in(self, key: str):
        from vettarr.config import runtime
        from vettarr.config.loader import load
        from vettarr.parser import formats as formats_mod

        example = load(pathlib.Path(__file__).resolve().parents[1] / "config.example.yaml", env={})
        cfg = config(
            formats=[f.model_dump(mode="json") for f in example.formats],
            sources=[
                {"key": "cards", "kind": "userbot", "chat": GROUP, "formats": ["card"]},
                {"key": "scenes", "kind": "userbot", "chat": -1009999, "formats": ["scene"]},
            ],
        )
        self.addCleanup(setattr, formats_mod, "_cache", None)
        followed = mock.AsyncMock(return_value=None)
        with Harness(cfg) as h, \
                mock.patch.object(runtime, "get_config", lambda: cfg), \
                mock.patch.object(ingest, "_linked_torrent", followed):
            result = h.run(key, ingest.Incoming(h.source(key).chat, 10, self.CARD))
        return result, followed, h

    def test_a_card_in_a_source_that_reads_cards_is_a_post(self) -> None:
        result, followed, h = self.arrive_in("cards")
        self.assertEqual((result.outcome, result.kind), ("added", "post"))
        followed.assert_not_awaited()
        self.assertEqual(h.inserted[0]["source_key"], "cards")

    def test_the_same_card_in_a_scene_only_source_has_no_structure(self) -> None:
        result, followed, h = self.arrive_in("scenes")
        self.assertEqual((result.outcome, result.kind), ("added", "post"))
        followed.assert_awaited_once()
        self.assertEqual(h.inserted[0]["source_key"], "scenes")


class DirectAddTests(unittest.TestCase):
    """Once approved, a torrent the post carried goes straight to the client."""

    def _ref(self) -> ItemRef:
        return ItemRef(id=77, title="Meridian", year=2024, quality=None, season=None,
                       media_type=MediaType.MOVIE, telegram_message_id=7)

    def _patches(self, qbt, raw_text="", sender=REVIEWER, cfg=None):
        """The item is one the inbox source took from ``sender``."""
        record = mock.AsyncMock()
        item = make_item(
            raw_text, id=77, source_key="inbox", telegram_chat_id=INBOX_USER_CHAT, sender_id=sender,
        )

        return record, [
            mock.patch.object(ingest.settings, "_config", cfg or config()),
            mock.patch.object(search_service, "qbittorrent", qbt),
            mock.patch.object(search_service, "record_download", record),
            mock.patch.object(ingest, "async_session", session_factory(FakeSession(item))),
        ]

    def test_a_kept_torrent_file_is_added_without_a_search(self) -> None:
        qbt = mock.AsyncMock()
        qbt.add_torrent_file.return_value = SimpleNamespace(hash=HASH, created=True)
        record, patches = self._patches(qbt)
        with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
            (pathlib.Path(tmp) / "77.torrent").write_bytes(b"d4:infod4:name1:xee")
            stack.enter_context(mock.patch.object(ingest, "TORRENTS_DIR", pathlib.Path(tmp)))
            for p in patches:
                stack.enter_context(p)
            result = asyncio.run(ingest.add_direct(self._ref()))
            kept = (pathlib.Path(tmp) / "77.torrent").exists()
        self.assertEqual(result, "downloading")
        qbt.add_torrent_file.assert_awaited_once_with(b"d4:infod4:name1:xee", MediaType.MOVIE)
        record.assert_awaited_once()
        self.assertFalse(kept, "the kept .torrent is removed once the download client has it")

    def test_a_magnet_in_the_post_is_added_without_a_search(self) -> None:
        magnet = f"magnet:?xt=urn:btih:{HASH}&dn=Meridian"
        qbt = mock.AsyncMock()
        qbt.add_torrent.return_value = SimpleNamespace(hash=HASH, created=True)
        record, patches = self._patches(qbt, raw_text=f"look\n{magnet}")
        with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(ingest, "TORRENTS_DIR", pathlib.Path(tmp)))
            for p in patches:
                stack.enter_context(p)
            result = asyncio.run(ingest.add_direct(self._ref()))
        self.assertEqual(result, "downloading")
        self.assertEqual(qbt.add_torrent.await_args.kwargs["magnet_url"], magnet)
        self.assertEqual(record.await_args.kwargs["magnet_url"], magnet)

    def test_an_item_with_no_torrent_of_its_own_is_searched(self) -> None:
        qbt = mock.AsyncMock()
        record, patches = self._patches(qbt, raw_text="Some.Film.2024.1080p")
        with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(ingest, "TORRENTS_DIR", pathlib.Path(tmp)))
            for p in patches:
                stack.enter_context(p)
            self.assertIsNone(asyncio.run(ingest.add_direct(self._ref())))
        qbt.add_torrent.assert_not_awaited()
        record.assert_not_awaited()


class IntakePresetTests(unittest.TestCase):
    def test_what_intake_resolved_survives_the_parser(self) -> None:
        """A magnet post reads as nothing to the parser; the title from its
        name is what the policy and the search see."""
        item = make_item(
            f"magnet:?xt=urn:btih:{HASH}&dn=Some.Film",
            title="Some Film", year=2024, media_type=MediaType.MOVIE,
        )
        run_process(item, content=(matched_tmdb(), full_guide()))
        self.assertEqual((item.title, item.year, item.media_type), ("Some Film", 2024, MediaType.MOVIE))
        self.assertEqual(item.status, ItemStatus.APPROVED)

    def test_a_post_with_no_preset_is_parsed_as_before(self) -> None:
        """The same post with nothing resolved at intake: the parser cannot say
        what kind of media it is, as it never could."""
        item = make_item(f"magnet:?xt=urn:btih:{HASH}")
        run_process(item)
        self.assertEqual(item.media_type, MediaType.UNKNOWN)
        self.assertIn(item.status, (ItemStatus.PARSE_FAILED, ItemStatus.IGNORED))


# ---------------------------------------------------------------------------
# Duplicate forwards
# ---------------------------------------------------------------------------


class DuplicateForwardTests(unittest.TestCase):
    def test_a_forward_of_a_post_already_in_the_inbox_is_not_a_second_item(self) -> None:
        with Harness(existing=55) as h:
            result = h.run("inbox", ingest.Incoming(
                INBOX_USER_CHAT, 20, "Some Film 2024", REVIEWER, forward_from=(GROUP, 321),
            ))
        self.assertEqual(result.outcome, "duplicate")
        self.assertEqual(result.item_id, 55)
        self.assertEqual(result.reply(), "Already in your inbox.")
        self.assertEqual(h.inserted, [])
        self.assertEqual(h.checked[0].origin_key, f"{GROUP}:321")

    def test_the_origin_lookup_also_finds_the_original_row(self) -> None:
        """Rows from before origin_key existed are found by their own chat and
        id, and the forward itself by its own."""
        captured = []

        class Session:
            async def execute(self, statement):
                captured.append(str(statement.compile(compile_kwargs={"literal_binds": True})))
                return FakeResult(None)

        @contextlib.asynccontextmanager
        async def factory():
            yield Session()

        with mock.patch.object(ingest, "async_session", factory):
            asyncio.run(ingest._existing(
                ingest.Incoming(INBOX_USER_CHAT, 20, "x", REVIEWER, forward_from=(GROUP, 321)),
            ))
        sql = captured[0]
        self.assertIn(f"media_items.origin_key = '{GROUP}:321'", sql)
        self.assertIn(f"media_items.telegram_chat_id = {GROUP}", sql)
        self.assertIn("media_items.telegram_message_id = 321", sql)
        self.assertIn(f"media_items.telegram_chat_id = {INBOX_USER_CHAT}", sql)
        self.assertIn("media_items.telegram_message_id = 20", sql)

    def test_a_conflict_on_insert_is_a_duplicate(self) -> None:
        with Harness(new_id=None) as h:
            result = h.run("films", ingest.Incoming(GROUP, 10, "Some Film (2024) 1080p WEB-DL"))
        self.assertEqual(result.outcome, "duplicate")
        self.assertTrue(h.queue.empty())


# ---------------------------------------------------------------------------
# allowed_senders, and updates with no user
# ---------------------------------------------------------------------------


class AllowedSendersTests(unittest.TestCase):
    def test_who_gets_in(self) -> None:
        cfg = config()
        reviewers = cfg.sources[1]  # inbox: allowed_senders defaults to reviewers
        anyone = cfg.sources[2]
        listed = Config.model_validate({
            "telegram": {"bot_token": "t"},
            "sources": [{"key": "s", "kind": "bot", "chat": "private", "allowed_senders": [STRANGER]}],
        }).sources[0]
        cases = [
            (reviewers, REVIEWER, True), (reviewers, OTHER_REVIEWER, True),
            (reviewers, STRANGER, False), (reviewers, None, False),
            (anyone, STRANGER, True), (anyone, None, True),
            (listed, STRANGER, True), (listed, REVIEWER, False), (listed, None, False),
        ]
        for source, sender, expected in cases:
            with self.subTest(source=source.key, sender=sender):
                self.assertIs(ingest.sender_allowed(source, sender, cfg), expected)

    def test_a_stranger_s_message_never_becomes_an_item(self) -> None:
        with Harness() as h:
            result = h.run("inbox", ingest.Incoming(INBOX_USER_CHAT, 1, "Meridian 2024", STRANGER))
        self.assertEqual(result.outcome, "denied")
        self.assertIsNone(result.reply())
        self.assertEqual(h.inserted, [])


class NoUserUpdateTests(unittest.TestCase):
    def _run(self, user):
        called = mock.AsyncMock()
        wrapped = handlers._authorized(called)
        update = SimpleNamespace(effective_user=user)
        with mock.patch.object(ingest.settings, "_config", config()):
            asyncio.run(wrapped(update, None))
        return called

    def test_an_update_with_no_user_is_turned_away(self) -> None:
        self._run(None).assert_not_awaited()

    def test_a_stranger_is_turned_away(self) -> None:
        self._run(SimpleNamespace(id=STRANGER)).assert_not_awaited()

    def test_every_reviewer_is_answered(self) -> None:
        for reviewer in (REVIEWER, OTHER_REVIEWER):
            with self.subTest(reviewer=reviewer):
                self._run(SimpleNamespace(id=reviewer)).assert_awaited_once()


class BotIntakeTests(unittest.TestCase):
    def _update(self, chat_type, chat_id, user_id, text="Meridian 2024"):
        message = SimpleNamespace(
            text=text, caption=None, entities=None, caption_entities=None,
            forward_origin=None, document=None, chat_id=chat_id, message_id=3,
            reply_text=mock.AsyncMock(),
        )
        return SimpleNamespace(
            effective_message=message,
            effective_chat=SimpleNamespace(id=chat_id, type=chat_type, username=None),
            effective_user=SimpleNamespace(id=user_id) if user_id else None,
        )

    def test_a_private_message_is_ingested_and_answered(self) -> None:
        update = self._update("private", INBOX_USER_CHAT, REVIEWER)
        result = ingest.IngestResult("added", "title", item_id=4, title="Meridian")
        seen = mock.AsyncMock(return_value=result)
        with mock.patch.object(ingest.settings, "_config", config()), mock.patch.object(ingest, "ingest", seen):
            asyncio.run(handlers.on_message(update, None))
        source, incoming = seen.await_args.args
        self.assertEqual(source.key, "inbox")
        self.assertEqual(incoming.sender_id, REVIEWER)
        update.effective_message.reply_text.assert_awaited_once_with("Added: Meridian")

    def test_a_chat_no_source_reads_is_ignored(self) -> None:
        update = self._update("supergroup", -100555, REVIEWER)
        seen = mock.AsyncMock()
        with mock.patch.object(ingest.settings, "_config", config()), mock.patch.object(ingest, "ingest", seen):
            asyncio.run(handlers.on_message(update, None))
        seen.assert_not_awaited()

    def test_a_group_source_is_never_answered_in_the_group(self) -> None:
        update = self._update("supergroup", -1009999, None)
        result = ingest.IngestResult("added", "post", item_id=4)
        with (
            mock.patch.object(ingest.settings, "_config", config()),
            mock.patch.object(ingest, "ingest", mock.AsyncMock(return_value=result)),
        ):
            asyncio.run(handlers.on_message(update, None))
        update.effective_message.reply_text.assert_not_awaited()

    def test_the_bot_registers_intake_and_my_chat_member(self) -> None:
        from telegram.ext import ChatMemberHandler, MessageHandler

        app = handlers.create_bot_app(None)
        self.addCleanup(ingest.set_review_pusher, None)
        kinds = {type(h) for group in app.handlers.values() for h in group}
        self.assertIn(MessageHandler, kinds)
        self.assertIn(ChatMemberHandler, kinds)

    def test_being_added_to_a_chat_is_recorded(self) -> None:
        change = SimpleNamespace(
            old_chat_member=SimpleNamespace(status="left"),
            new_chat_member=SimpleNamespace(status="administrator"),
            chat=SimpleNamespace(id=-100321, title="Films", username=None),
            from_user=SimpleNamespace(id=REVIEWER),
        )
        update = SimpleNamespace(my_chat_member=change)
        context = SimpleNamespace(bot=SimpleNamespace(can_read_all_group_messages=False))
        self.addCleanup(ingest._bot_chats.clear)
        with mock.patch.object(ingest.settings, "_config", config()):
            asyncio.run(handlers.on_my_chat_member(update, context))
        chat = next(c for c in ingest.bot_chats() if c.chat_id == -100321)
        self.assertEqual(chat.title, "Films")
        self.assertTrue(chat.is_admin)
        self.assertFalse(chat.reads_all_messages)


# ---------------------------------------------------------------------------
# backfill
# ---------------------------------------------------------------------------


class BackfillTests(unittest.TestCase):
    def _ingester(self, backfill: str, last: int | None = 120):
        cfg = config(sources=[{"key": "films", "kind": "userbot", "chat": GROUP, "backfill": backfill}])
        calls = []

        async def iter_messages(chat, **kwargs):
            calls.append((chat, kwargs))
            for message_id in (121, 122):
                yield SimpleNamespace(id=message_id)

        with mock.patch.object(telegram_reader, "TelegramClient", mock.MagicMock()):
            ingester = telegram_reader.TelegramIngester(cfg.sources)
        ingester.client = SimpleNamespace(
            iter_messages=iter_messages, get_peer_id=mock.AsyncMock(return_value=GROUP),
        )
        ingester.get_last_ingested_id = mock.AsyncMock(return_value=last)
        ingester._handle = mock.AsyncMock()
        self.addCleanup(ingest.set_resolved_chats, None)
        asyncio.run(ingester.resolve_chats())
        asyncio.run(ingester.backfill())
        return ingester, calls

    def test_since_last_reads_after_the_last_ingested_message(self) -> None:
        ingester, calls = self._ingester("since_last")
        ingester.get_last_ingested_id.assert_awaited_once_with(GROUP)
        self.assertEqual(calls, [(GROUP, {"reverse": True, "min_id": 120})])
        self.assertEqual(ingester._handle.await_count, 2)

    def test_since_last_on_a_first_run_reads_everything(self) -> None:
        _ingester, calls = self._ingester("since_last", last=None)
        self.assertEqual(calls[0][1]["min_id"], 0)

    def test_full_reads_from_the_first_message(self) -> None:
        ingester, calls = self._ingester("full")
        ingester.get_last_ingested_id.assert_not_awaited()
        self.assertEqual(calls, [(GROUP, {"reverse": True, "min_id": 0})])

    def test_off_reads_no_history(self) -> None:
        _ingester, calls = self._ingester("off")
        self.assertEqual(calls, [])

    def test_the_environment_s_group_backfills_since_last(self) -> None:
        """The live deployment: every start used to re-read the group from message 1."""
        from vettarr.config.runtime import from_environment

        source = from_environment().sources[0]
        self.assertEqual((source.kind, source.backfill), ("userbot", "since_last"))


# ---------------------------------------------------------------------------
# on_download / on_reject, by the item's own chat
# ---------------------------------------------------------------------------


class PostActionTests(unittest.TestCase):
    def _after(self, event, post, *, userbot=None, bot=None, cfg=None):
        actions = ingest.PostActions(userbot=userbot, bot=bot, config=cfg or config())
        with mock.patch.object(ingest, "_load_post", mock.AsyncMock(return_value=post)):
            return actions, asyncio.run(actions.after(event, 9))

    @staticmethod
    def _userbot():
        client = mock.AsyncMock()
        client.get_input_entity.return_value = "peer"
        return SimpleNamespace(client=client)

    def test_a_userbot_delete_targets_the_item_s_own_chat(self) -> None:
        userbot = self._userbot()
        _a, done = self._after("download", (-100777, 5, "films"), userbot=userbot)
        self.assertEqual(done, "deleted")
        userbot.client.delete_messages.assert_awaited_once_with(-100777, [5])

    def test_a_row_from_before_sources_belongs_to_the_first_userbot_source(self) -> None:
        userbot = self._userbot()
        _a, done = self._after("download", (GROUP, 5, None), userbot=userbot)
        self.assertEqual(done, "deleted")
        userbot.client.delete_messages.assert_awaited_once_with(GROUP, [5])

    def test_a_userbot_reaction(self) -> None:
        userbot = self._userbot()
        _a, done = self._after("reject", (GROUP, 5, "films"), userbot=userbot)
        self.assertEqual(done, "reacted")
        request = userbot.client.await_args.args[0]
        self.assertEqual((request.peer, request.msg_id), ("peer", 5))
        self.assertEqual(request.reaction[0].emoticon, ingest.REACTIONS["reject"])
        userbot.client.delete_messages.assert_not_awaited()

    def test_a_bot_source_reacts_through_the_bot(self) -> None:
        bot = mock.AsyncMock()
        _a, done = self._after("download", (INBOX_USER_CHAT, 5, "inbox"), bot=bot)
        self.assertEqual(done, "reacted")
        bot.set_message_reaction.assert_awaited_once_with(
            INBOX_USER_CHAT, 5, reaction=ingest.REACTIONS["download"],
        )

    def test_a_bot_deletes_in_its_private_chat(self) -> None:
        bot = mock.AsyncMock()
        _a, done = self._after("reject", (INBOX_USER_CHAT, 5, "inbox"), bot=bot)
        self.assertEqual(done, "deleted")
        bot.delete_message.assert_awaited_once_with(INBOX_USER_CHAT, 5)

    def test_none_touches_nothing(self) -> None:
        bot = mock.AsyncMock()
        _a, done = self._after("reject", (-1009999, 5, "club"), bot=bot)
        self.assertEqual(done, "none")
        self.assertEqual(bot.mock_calls, [])

    def test_a_bot_that_is_not_an_admin_refuses_to_delete_and_says_so_once(self) -> None:
        bot = mock.AsyncMock()
        bot.id = 42
        bot.get_chat_member.return_value = SimpleNamespace(status="member")
        actions = ingest.PostActions(bot=bot, config=config())
        with (
            mock.patch.object(ingest, "_load_post", mock.AsyncMock(return_value=(-1009999, 5, "club"))),
            mock.patch.object(ingest, "log") as log,
        ):
            first = asyncio.run(actions.after("download", 9))
            second = asyncio.run(actions.after("download", 10))
        self.assertEqual((first, second), ("refused", "refused"))
        bot.delete_message.assert_not_awaited()
        bot.get_chat_member.assert_awaited_with(-1009999, 42)
        self.assertEqual(log.warning.call_count, 1)

    def test_an_admin_bot_with_the_delete_right_deletes(self) -> None:
        bot = mock.AsyncMock()
        bot.id = 42
        bot.get_chat_member.return_value = SimpleNamespace(status="administrator", can_delete_messages=True)
        _a, done = self._after("download", (-1009999, 5, "club"), bot=bot)
        self.assertEqual(done, "deleted")
        bot.delete_message.assert_awaited_once_with(-1009999, 5)

    def test_a_failed_action_is_reported_not_raised(self) -> None:
        userbot = self._userbot()
        userbot.client.delete_messages.side_effect = RuntimeError("telegram down")
        _a, done = self._after("download", (GROUP, 5, "films"), userbot=userbot)
        self.assertEqual(done, "failed")

    def test_without_the_client_a_source_needs_nothing_happens(self) -> None:
        _a, done = self._after("download", (GROUP, 5, "films"))
        self.assertEqual(done, "unavailable")


# ---------------------------------------------------------------------------
# notify_on_review
# ---------------------------------------------------------------------------


class NotifyOnReviewTests(unittest.TestCase):
    def _held(self, source_key):
        item = make_item("x", status=ItemStatus.PENDING_REVIEW, source_key=source_key,
                         telegram_chat_id=-1009999)
        pushed = mock.AsyncMock()
        ingest.set_review_pusher(pushed)
        self.addCleanup(ingest.set_review_pusher, None)
        with (
            mock.patch.object(ingest.settings, "_config", config()),
            mock.patch.object(ingest, "async_session", session_factory(FakeSession(item))),
        ):
            asyncio.run(ingest.held_for_review(item.id))
        return pushed, item

    def test_a_held_item_goes_to_every_reviewer_when_its_source_asks(self) -> None:
        pushed, item = self._held("club")
        self.assertEqual([c.args for c in pushed.await_args_list], [(REVIEWER, item), (OTHER_REVIEWER, item)])

    def test_a_source_that_does_not_ask_pushes_nothing(self) -> None:
        pushed, _item = self._held("inbox")
        pushed.assert_not_awaited()

    def test_the_pipeline_pushes_what_it_holds(self) -> None:
        item = make_item("Some Film (2024) 1080p WEB-DL DD+5.1 H.264-GROUP\n8.4 GiB")
        held = mock.AsyncMock()
        tmdb = matched_tmdb(genre_ids=[FLAGGED_GENRE_ID])
        with mock.patch.object(pipeline, "held_for_review", held):
            run_process(item, content=(tmdb, ParentsGuide(fetched=True)))
        self.assertEqual(item.status, ItemStatus.PENDING_REVIEW)
        held.assert_awaited_once_with(item.id)

    def test_the_pipeline_pushes_nothing_it_approves(self) -> None:
        item = make_item("Some Film (2024) 1080p WEB-DL DD+5.1 H.264-GROUP\n8.4 GiB")
        held = mock.AsyncMock()
        with mock.patch.object(pipeline, "held_for_review", held):
            run_process(item, content=(matched_tmdb(), full_guide()))
        held.assert_not_awaited()


# ---------------------------------------------------------------------------
# The migration
# ---------------------------------------------------------------------------

_REVISION = pathlib.Path(__file__).resolve().parents[1] / "alembic" / "versions" / "0002_sources.py"


def _statements() -> list[str]:
    spec = importlib.util.spec_from_file_location("_revision_0002", _REVISION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    out: list = []
    with mock.patch.object(module.op, "execute", out.append):
        module.upgrade()
    return [" ".join(str(s).split()) for s in out], module


class SourcesMigrationTests(unittest.TestCase):
    def test_it_follows_the_first_migration(self) -> None:
        _sql, module = _statements()
        self.assertEqual(module.down_revision, "0001_auto_approval")

    def test_the_columns_and_the_partial_unique_index(self) -> None:
        sql = "\n".join(_statements()[0])
        self.assertIn("ADD COLUMN IF NOT EXISTS source_key TEXT", sql)
        self.assertIn("ADD COLUMN IF NOT EXISTS origin_key TEXT", sql)
        self.assertIn(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_media_items_origin_key ON media_items (origin_key) "
            "WHERE origin_key IS NOT NULL", sql,
        )

    def test_it_only_adds(self) -> None:
        for statement in _statements()[0]:
            self.assertIn("IF NOT EXISTS", statement)
            for forbidden in ("UPDATE ", "DELETE ", "DROP ", "NOT NULL DEFAULT", "RENAME"):
                self.assertNotIn(forbidden, statement.upper().replace("IS NOT NULL", ""))

    def test_the_model_matches_the_migration(self) -> None:
        from vettarr.models import MediaItem

        index = next(i for i in MediaItem.__table__.indexes if i.name == "uq_media_items_origin_key")
        self.assertTrue(index.unique)
        self.assertEqual(str(index.dialect_options["postgresql"]["where"]), "origin_key IS NOT NULL")
        self.assertTrue(MediaItem.__table__.c.source_key.nullable)


# ---------------------------------------------------------------------------
# A file a stranger chose
# ---------------------------------------------------------------------------

DEEP = b"d4:info" + b"l" * 200_000 + b"e" * 200_000 + b"e"
# Valid bencode that is not a torrent, each in its own way.
NOT_TORRENTS = {
    "nested deeper than any torrent": DEEP,
    "files is a number": b"d4:infod5:filesi5e4:name1:xee",
    "a file path is a dictionary": b"d4:infod5:filesld4:pathd1:a1:beee4:name1:xee",
    "a dictionary keyed by a list": b"d4:infodl1:ae1:b4:name1:xee",
    "a name that is a number": b"d4:infod4:namei5eee",
    "info is a number": b"d4:infoi5ee",
}
CLUB = -1009999
WHEN = datetime.datetime(2026, 1, 2, 3, 4, tzinfo=datetime.timezone.utc)


class HostileTorrentTests(unittest.TestCase):
    """Anyone a source lets in can post a file named ``x.torrent``."""

    def test_a_file_that_is_bencode_but_no_torrent_is_skipped_not_raised(self) -> None:
        for what, data in NOT_TORRENTS.items():
            with self.subTest(what=what):
                async def fetch(data=data):
                    return data

                patch, _calls = tmdb_answers({})
                with tempfile.TemporaryDirectory() as tmp, \
                        mock.patch.object(ingest, "TORRENTS_DIR", pathlib.Path(tmp)), \
                        Harness() as h, patch:
                    result = h.run("club", ingest.Incoming(
                        CLUB, 7, "", STRANGER, document_name="x.torrent", fetch_document=fetch,
                    ))
                    kept = list(pathlib.Path(tmp).iterdir())
                self.assertEqual(result.outcome, "skipped")
                self.assertEqual(h.inserted, [])
                self.assertEqual(kept, [])

    def test_the_name_reader_returns_nothing_for_each(self) -> None:
        for what, data in NOT_TORRENTS.items():
            with self.subTest(what=what):
                self.assertEqual(ingest.torrent_name(data), (None, []))

    def test_a_real_torrent_still_reads(self) -> None:
        self.assertEqual(
            ingest.torrent_name(torrent("Pack", ["a.mkv", "b.mkv"])), ("Pack", ["a.mkv", "b.mkv"]),
        )
        self.assertEqual(ingest.torrent_name(torrent("Book.epub")), ("Book.epub", ["Book.epub"]))

    def test_a_file_too_large_to_be_a_torrent_is_never_downloaded(self) -> None:
        fetch = mock.AsyncMock(return_value=torrent("Some.Film.2024.1080p.WEB-DL.x264-GRP"))
        with Harness() as h:
            result = h.run("club", ingest.Incoming(
                CLUB, 7, "", STRANGER, document_name="x.torrent", fetch_document=fetch,
                document_size=2 * 1024 ** 3,
            ))
        fetch.assert_not_awaited()
        self.assertEqual(result.outcome, "skipped")
        self.assertEqual(h.inserted, [])

    def test_a_file_at_the_limit_is_read(self) -> None:
        fetch = mock.AsyncMock(return_value=torrent("Some.Film.2024.1080p.WEB-DL.x264-GRP"))
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(ingest, "TORRENTS_DIR", pathlib.Path(tmp)), Harness() as h:
            result = h.run("club", ingest.Incoming(
                CLUB, 7, "", STRANGER, document_name="x.torrent", fetch_document=fetch,
                document_size=ingest.MAX_TORRENT_BYTES,
            ))
        fetch.assert_awaited_once()
        self.assertEqual((result.outcome, result.kind), ("added", "torrent"))

    def test_the_userbot_passes_on_the_size_telegram_states(self) -> None:
        message = SimpleNamespace(
            chat_id=GROUP, id=9, text="", entities=None, sender_id=STRANGER, fwd_from=None,
            document=object(), file=SimpleNamespace(name="x.torrent", size=2 * 1024 ** 3), date=WHEN,
        )
        incoming = telegram_reader.incoming(message, client=None)
        self.assertEqual(incoming.document_size, 2 * 1024 ** 3)
        self.assertEqual(incoming.document_name, "x.torrent")

    def test_the_bot_passes_on_the_size_too(self) -> None:
        message = SimpleNamespace(
            text=None, caption=None, entities=None, caption_entities=None, forward_origin=None,
            document=SimpleNamespace(file_name="x.torrent", file_size=123),
            chat_id=INBOX_USER_CHAT, message_id=5, date=WHEN,
        )
        incoming = handlers.incoming_from_bot(message, REVIEWER)
        self.assertEqual((incoming.document_size, incoming.posted_at), (123, WHEN))


# ---------------------------------------------------------------------------
# A message that is already an item
# ---------------------------------------------------------------------------


class KnownMessageTests(unittest.TestCase):
    def test_it_costs_no_download_and_no_lookup(self) -> None:
        """Re-reading a history (backfill: full, a restart) asks the database
        and nobody else."""
        fetch = mock.AsyncMock(return_value=torrent("Some.Film.2024.1080p.WEB-DL.x264-GRP"))
        linked = mock.AsyncMock()
        patch, calls = tmdb_answers({})
        with Harness(existing=55) as h, patch, \
                mock.patch.object(search_service, "fetch_linked_torrent", linked):
            results = [
                h.run("films", ingest.Incoming(GROUP, 10, "", document_name="a.torrent", fetch_document=fetch)),
                h.run("films", ingest.Incoming(GROUP, 11, "Some Film 2024")),
                h.run("films", ingest.Incoming(GROUP, 12, "https://tracker-a.example/details.php?id=42")),
            ]
        self.assertEqual([(r.outcome, r.item_id) for r in results], [("duplicate", 55)] * 3)
        fetch.assert_not_awaited()
        linked.assert_not_awaited()
        self.assertEqual(calls, [])
        self.assertEqual(h.inserted, [])
        self.assertEqual([m.message_id for m in h.checked], [10, 11, 12])

    def test_a_message_with_nothing_to_read_is_skipped_without_asking(self) -> None:
        with Harness() as h:
            result = h.run("films", ingest.Incoming(GROUP, 13, ""))
        self.assertEqual(result.outcome, "skipped")
        self.assertEqual(h.checked, [])

    def test_the_lookup_of_a_plain_message_is_by_its_own_chat_and_id(self) -> None:
        captured = []

        class Session:
            async def execute(self, statement):
                captured.append(str(statement.compile(compile_kwargs={"literal_binds": True})))
                return FakeResult(None)

        @contextlib.asynccontextmanager
        async def factory():
            yield Session()

        with mock.patch.object(ingest, "async_session", factory):
            asyncio.run(ingest._existing(ingest.Incoming(GROUP, 10, "x")))
        self.assertIn(f"media_items.telegram_chat_id = {GROUP}", captured[0])
        self.assertIn("media_items.telegram_message_id = 10", captured[0])
        self.assertIn(f"media_items.origin_key = '{GROUP}:10'", captured[0])


# ---------------------------------------------------------------------------
# Who sent it, and when
# ---------------------------------------------------------------------------


class SenderAndPostTimeTests(unittest.TestCase):
    def test_the_sender_and_the_post_time_are_stored(self) -> None:
        with Harness() as h:
            h.run("club", ingest.Incoming(
                CLUB, 5, "Some.Film.2024.1080p.WEB-DL.x264-GRP\n4.2 GB", STRANGER, posted_at=WHEN,
            ))
        self.assertEqual((h.inserted[0]["sender_id"], h.inserted[0]["posted_at"]), (STRANGER, WHEN))

    def _telethon(self, sender_id):
        return telegram_reader.incoming(SimpleNamespace(
            chat_id=GROUP, id=9, text="Some Film 2024", entities=None, sender_id=sender_id,
            fwd_from=None, document=None, date=WHEN,
        ), client=None)

    def test_the_userbot_reads_the_sender_and_the_date(self) -> None:
        incoming = self._telethon(STRANGER)
        self.assertEqual((incoming.sender_id, incoming.posted_at), (STRANGER, WHEN))

    def test_a_channel_post_and_an_anonymous_admin_have_no_sender(self) -> None:
        """Telethon gives those the channel's or the group's id: not a person."""
        for sender_id in (GROUP, None):
            with self.subTest(sender_id=sender_id):
                self.assertIsNone(self._telethon(sender_id).sender_id)

    def test_the_model_has_the_two_columns(self) -> None:
        from vettarr.models import MediaItem

        columns = MediaItem.__table__.c
        self.assertTrue(columns.sender_id.nullable)
        self.assertTrue(columns.posted_at.nullable)


# ---------------------------------------------------------------------------
# direct_adds: whose own torrent is added as it is
# ---------------------------------------------------------------------------


def with_direct_adds(value: str) -> Config:
    return config(sources=[
        {"key": "films", "kind": "userbot", "chat": GROUP, "direct_adds": value},
        {"key": "inbox", "kind": "bot", "chat": "private", "allowed_senders": "anyone", "direct_adds": value},
    ])


class DirectAddsSettingTests(unittest.TestCase):
    MAGNET = f"magnet:?xt=urn:btih:{HASH}&dn=Some.Film.2024.1080p.BluRay.x264-GRP"

    def test_the_default_is_reviewers_only(self) -> None:
        cfg = config()
        self.assertEqual([s.direct_adds for s in cfg.sources], ["reviewers"] * 3)

    def test_a_seerr_source_carries_no_torrent(self) -> None:
        from pydantic import ValidationError

        from vettarr.config.schema import Source

        self.assertEqual(Source.model_validate({"key": "r", "kind": "seerr"}).direct_adds, "off")
        with self.assertRaises(ValidationError):
            Source.model_validate({"key": "r", "kind": "seerr", "direct_adds": "anyone"})

    def test_who_may_hand_over_a_torrent_of_their_own(self) -> None:
        cases = [
            ("reviewers", REVIEWER, True), ("reviewers", STRANGER, False), ("reviewers", None, False),
            ("anyone", STRANGER, True), ("anyone", None, True),
            ("off", REVIEWER, False), ("off", STRANGER, False),
        ]
        for value, sender, expected in cases:
            with self.subTest(direct_adds=value, sender=sender):
                cfg = with_direct_adds(value)
                self.assertIs(ingest.direct_adds_allowed(cfg.sources[0], sender, cfg), expected)

    def _torrent_from(self, sender, cfg=None):
        data = torrent("Some.Film.2024.1080p.WEB-DL.x264-GRP.mkv")

        async def fetch():
            return data

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(ingest, "TORRENTS_DIR", pathlib.Path(tmp)):
            with Harness(cfg or config()) as h:
                result = h.run("club", ingest.Incoming(
                    CLUB, 7, "", sender, document_name="x.torrent", fetch_document=fetch,
                ))
            kept = (pathlib.Path(tmp) / "77.torrent").exists()
        return result, h, kept

    def test_a_stranger_s_torrent_names_the_item_and_is_not_kept(self) -> None:
        result, h, kept = self._torrent_from(STRANGER)
        self.assertEqual((result.outcome, result.kind, result.title), ("added", "torrent", "Some Film"))
        self.assertEqual(h.inserted[0]["sender_id"], STRANGER)
        self.assertFalse(kept, "only a sender direct_adds allows has their .torrent kept for the add")

    def test_a_reviewer_s_torrent_is_kept(self) -> None:
        _result, _h, kept = self._torrent_from(REVIEWER)
        self.assertTrue(kept)

    def _item(self, sender, raw_text=None):
        return make_item(
            raw_text or self.MAGNET, id=77, source_key="inbox", telegram_chat_id=INBOX_USER_CHAT,
            sender_id=sender, title="Some Film", media_type=MediaType.MOVIE,
            status=ItemStatus.PENDING_REVIEW,
        )

    def test_a_stranger_s_magnet_is_not_a_direct_add(self) -> None:
        cfg = with_direct_adds("reviewers")
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(ingest, "TORRENTS_DIR", pathlib.Path(tmp)):
            self.assertIsNone(ingest.direct_add_of(self._item(STRANGER), cfg))
            self.assertEqual(ingest.direct_add_of(self._item(REVIEWER), cfg), "magnet")
            self.assertEqual(ingest.direct_add_of(self._item(STRANGER), with_direct_adds("anyone")), "magnet")
            self.assertIsNone(ingest.direct_add_of(self._item(REVIEWER), with_direct_adds("off")))
            self.assertIsNone(ingest.direct_add_of(self._item(REVIEWER, "Some.Film.2024.1080p"), cfg))

    def _add(self, sender, cfg):
        qbt = mock.AsyncMock()
        qbt.add_torrent.return_value = SimpleNamespace(hash=HASH, created=True)
        record = mock.AsyncMock()
        ref = ItemRef(id=77, title="Some Film", year=2024, quality=None, season=None,
                      media_type=MediaType.MOVIE, telegram_message_id=7)
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(ingest, "TORRENTS_DIR", pathlib.Path(tmp)), \
                mock.patch.object(ingest.settings, "_config", cfg), \
                mock.patch.object(search_service, "qbittorrent", qbt), \
                mock.patch.object(search_service, "record_download", record), \
                mock.patch.object(ingest, "async_session", session_factory(FakeSession(self._item(sender)))):
            return asyncio.run(ingest.add_direct(ref)), qbt

    def test_an_approved_stranger_s_magnet_is_searched_for_not_added(self) -> None:
        """The policy approved a name the sender chose; what the magnet points
        to never reaches the download client."""
        result, qbt = self._add(STRANGER, with_direct_adds("reviewers"))
        self.assertIsNone(result)
        qbt.add_torrent.assert_not_awaited()

    def test_anyone_lets_a_stranger_s_magnet_through(self) -> None:
        result, qbt = self._add(STRANGER, with_direct_adds("anyone"))
        self.assertEqual(result, "downloading")
        self.assertEqual(qbt.add_torrent.await_args.kwargs["magnet_url"], self.MAGNET)

    def _card(self, sender):
        item = self._item(sender)
        bot = mock.AsyncMock()
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(ingest, "TORRENTS_DIR", pathlib.Path(tmp)), \
                mock.patch.object(ingest.settings, "_config", with_direct_adds("reviewers")), \
                mock.patch.object(handlers, "_bot_app", SimpleNamespace(bot=bot)), \
                mock.patch.object(handlers, "async_session", session_factory(FakeSession(item))):
            asyncio.run(handlers._send_review(REVIEWER, item))
        return bot.send_message.await_args.kwargs["text"]

    def test_the_review_card_says_when_approving_adds_the_post_s_own_torrent(self) -> None:
        card = self._card(REVIEWER)
        self.assertIn("Direct add", card)
        self.assertIn("magnet", card)
        self.assertIn("Nothing is searched", card)

    def test_the_card_of_an_item_that_will_be_searched_says_nothing_of_it(self) -> None:
        self.assertNotIn("Direct add", self._card(STRANGER))


# ---------------------------------------------------------------------------
# New messages while histories are read, and messages that fail
# ---------------------------------------------------------------------------


def live(chat: int, message_id: int) -> SimpleNamespace:
    return SimpleNamespace(chat_id=chat, message=SimpleNamespace(id=message_id, chat_id=chat))


def arrives(chat: int, message_id: int):
    """A step of a history: a new message lands while it is being read."""

    async def step(ingester) -> None:
        await ingester._on_new(live(chat, message_id))
        await asyncio.sleep(0)  # the loop runs whatever that started

    return step


class LiveAndHistoryTests(unittest.TestCase):
    A, B = -1001, -1002

    def _ingester(self, history: dict, handle):
        cfg = config(sources=[
            {"key": "a", "kind": "userbot", "chat": self.A},
            {"key": "b", "kind": "userbot", "chat": self.B},
        ])
        with mock.patch.object(telegram_reader, "TelegramClient", mock.MagicMock()):
            ingester = telegram_reader.TelegramIngester(cfg.sources)

        async def iter_messages(chat, **_kwargs):
            for step in history.get(chat, []):
                if callable(step):
                    await step(ingester)
                else:
                    yield SimpleNamespace(id=step, chat_id=chat)

        async def peer_id(chat):
            return chat

        ingester.client = SimpleNamespace(
            iter_messages=iter_messages, get_peer_id=peer_id, add_event_handler=mock.Mock(),
        )
        ingester.get_last_ingested_id = mock.AsyncMock(return_value=0)
        ingester._handle = handle
        self.addCleanup(ingest.set_resolved_chats, None)
        return ingester

    def _run(self, ingester, then=()):
        async def go():
            await ingester.resolve_chats()
            ingester.subscribe()
            await ingester.backfill()
            for event in then:
                await ingester._on_new(event)
            while ingester._tasks:  # let what was started finish
                await asyncio.sleep(0)

        with mock.patch.object(telegram_reader, "_RETRY_SECONDS", 0):
            asyncio.run(go())

    def test_new_messages_are_taken_before_any_history_is_read(self) -> None:
        order = []
        ingester = self._ingester({}, mock.AsyncMock())
        ingester.start = mock.AsyncMock(side_effect=lambda: order.append("start"))
        ingester.subscribe = mock.Mock(side_effect=lambda: order.append("subscribe"))
        ingester.backfill = mock.AsyncMock(side_effect=lambda: order.append("backfill"))
        ingester.listen = mock.AsyncMock(side_effect=lambda _stop: order.append("listen"))
        asyncio.run(ingester.run(None))
        self.assertEqual(order, ["start", "subscribe", "backfill", "listen"])

    def test_the_app_starts_the_userbot_through_run(self) -> None:
        import inspect

        from vettarr import __main__ as cli

        source = inspect.getsource(cli._run)
        self.assertIn("await ingester.run(stop)", source)
        self.assertNotIn("ingester.backfill()", source)

    def test_subscribing_registers_the_handler_for_every_source_chat(self) -> None:
        ingester = self._ingester({}, mock.AsyncMock())
        asyncio.run(ingester.resolve_chats())
        ingester.subscribe()
        handler, event = ingester.client.add_event_handler.call_args.args
        self.assertEqual(handler, ingester._on_new)
        self.assertEqual(set(event.chats), {self.A, self.B})

    def test_a_post_that_arrives_while_histories_are_read_waits_for_its_own(self) -> None:
        """A's history is read, then B's. A post landing in A meanwhile goes
        in at once; one landing in B waits until B's history is done, or the
        next start would begin after it and never read the rest."""
        seen = []

        async def handle(source, message):
            seen.append((source.key, message.id))

        ingester = self._ingester({
            self.A: [1, 2],
            self.B: [arrives(self.A, 3), 1, arrives(self.B, 3), 2],
        }, handle)
        self._run(ingester)
        self.assertEqual(seen, [("a", 1), ("a", 2), ("a", 3), ("b", 1), ("b", 2), ("b", 3)])

    def test_a_source_with_backfill_off_takes_new_messages_at_once(self) -> None:
        seen = []

        async def handle(source, message):
            seen.append((source.key, message.id))

        cfg = config(sources=[
            {"key": "a", "kind": "userbot", "chat": self.A, "backfill": "off"},
            {"key": "b", "kind": "userbot", "chat": self.B},
        ])
        ingester = self._ingester({self.B: [arrives(self.A, 9), 1]}, handle)
        ingester._sources = list(cfg.sources)
        self._run(ingester)
        self.assertEqual(seen, [("a", 9), ("b", 1)])

    def test_a_message_that_cannot_be_read_does_not_end_the_history_read(self) -> None:
        tried, seen = [], []

        async def handle(_source, message):
            tried.append(message.id)
            if message.id == 121:
                raise TypeError("'LinkedFetch' object is not subscriptable")
            seen.append(message.id)

        ingester = self._ingester({self.A: [121, 122, 123]}, handle)
        self._run(ingester)
        self.assertEqual(seen, [122, 123])
        self.assertEqual(tried.count(121), telegram_reader._ATTEMPTS)

    def test_a_message_that_fails_once_is_tried_again(self) -> None:
        failed, seen = [], []

        async def handle(_source, message):
            if message.id == 121 and not failed:
                failed.append(message.id)
                raise RuntimeError("database down")
            seen.append(message.id)

        ingester = self._ingester({self.A: [121, 122]}, handle)
        self._run(ingester)
        self.assertEqual(seen, [121, 122])

    def test_a_live_message_that_fails_is_tried_again_before_the_next_one(self) -> None:
        """Were 6 ingested while 5 is lost, the next start would begin after
        6 and 5 would never be read."""
        failed, seen = [], []

        async def handle(_source, message):
            if message.id == 5 and not failed:
                failed.append(message.id)
                raise RuntimeError("database down")
            seen.append(message.id)

        ingester = self._ingester({}, handle)
        self._run(ingester, then=[live(self.A, 5), live(self.A, 6)])
        self.assertEqual(seen, [5, 6])


# ---------------------------------------------------------------------------
# The photo of a post
# ---------------------------------------------------------------------------


class PhotoTests(unittest.TestCase):
    def _keep(self, tmp, item_id, chat, message_id, content: bytes):
        item = make_item("x", id=item_id)

        async def download(dest):
            pathlib.Path(dest).write_bytes(content)

        with mock.patch.object(ingest, "PHOTOS_DIR", pathlib.Path(tmp)), \
                mock.patch.object(ingest, "async_session", session_factory(FakeSession(item))):
            name = asyncio.run(ingest.keep_photo(item_id, chat, message_id, download))
        self.assertEqual(item.telegram_photo_path, name)
        return name

    def test_two_chats_with_the_same_message_id_keep_two_photos(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            first = self._keep(tmp, 1, -1001, 812, b"cover of A")
            second = self._keep(tmp, 2, -1002, 812, b"cover of B")
            self.assertNotEqual(first, second)
            self.assertEqual((pathlib.Path(tmp) / first).read_bytes(), b"cover of A")
            self.assertEqual((pathlib.Path(tmp) / second).read_bytes(), b"cover of B")

    def test_the_userbot_keeps_a_photo_by_chat_and_message(self) -> None:
        with mock.patch.object(telegram_reader, "TelegramClient", mock.MagicMock()):
            ingester = telegram_reader.TelegramIngester([])
        keep = mock.AsyncMock()
        with mock.patch.object(ingest, "keep_photo", keep):
            asyncio.run(ingester._save_photo(SimpleNamespace(photo=object(), chat_id=-1002, id=812), 4))
        self.assertEqual(keep.await_args.args[:3], (4, -1002, 812))

    def test_a_bot_source_keeps_the_photo_of_a_post_too(self) -> None:
        message = SimpleNamespace(
            text=None, caption="Some Film 2024", entities=None, caption_entities=None,
            forward_origin=None, document=None, chat_id=CLUB, message_id=3,
            photo=[object(), object()], reply_text=mock.AsyncMock(),
        )
        update = SimpleNamespace(
            effective_message=message,
            effective_chat=SimpleNamespace(id=CLUB, type="supergroup", username=None),
            effective_user=SimpleNamespace(id=STRANGER),
        )
        keep = mock.AsyncMock()
        added = ingest.IngestResult("added", "title", item_id=4, title="Some Film")
        with mock.patch.object(ingest.settings, "_config", config()), \
                mock.patch.object(ingest, "ingest", mock.AsyncMock(return_value=added)), \
                mock.patch.object(ingest, "keep_photo", keep):
            asyncio.run(handlers.on_message(update, None))
        self.assertEqual(keep.await_args.args[:3], (4, CLUB, 3))


# ---------------------------------------------------------------------------
# Pushing a held item, one reviewer at a time
# ---------------------------------------------------------------------------


class PushEachReviewerTests(unittest.TestCase):
    def _held(self, pushed):
        item = make_item("x", status=ItemStatus.PENDING_REVIEW, source_key="club", telegram_chat_id=CLUB)
        ingest.set_review_pusher(pushed)
        self.addCleanup(ingest.set_review_pusher, None)
        with mock.patch.object(ingest.settings, "_config", config()), \
                mock.patch.object(ingest, "async_session", session_factory(FakeSession(item))):
            asyncio.run(ingest.held_for_review(item.id))
        return [call.args[0] for call in pushed.await_args_list]

    def test_a_reviewer_who_cannot_be_messaged_does_not_keep_the_card_from_the_others(self) -> None:
        pushed = mock.AsyncMock(side_effect=[RuntimeError("Forbidden: bot can't initiate conversation"), None])
        self.assertEqual(self._held(pushed), [REVIEWER, OTHER_REVIEWER])

    def test_a_flood_limit_is_waited_out_once(self) -> None:
        class Flood(Exception):
            retry_after = datetime.timedelta(seconds=0)

        pushed = mock.AsyncMock(side_effect=[Flood(), None, None])
        self.assertEqual(self._held(pushed), [REVIEWER, REVIEWER, OTHER_REVIEWER])

    def test_the_wait_telegram_asks_for_is_capped(self) -> None:
        class Flood(Exception):
            retry_after = 86_400

        self.assertEqual(ingest._retry_after(Flood()), ingest._PUSH_MAX_WAIT)
        self.assertIsNone(ingest._retry_after(RuntimeError("no")))


# ---------------------------------------------------------------------------
# What a rejection says it did
# ---------------------------------------------------------------------------


class RejectWordingTests(unittest.TestCase):
    def test_the_prompt_says_what_will_happen_to_the_post(self) -> None:
        for effect, phrase in (
            ("delete", "will be deleted"), ("react", "will get a reaction"), ("none", "stays where it is"),
        ):
            with self.subTest(effect=effect):
                respond = mock.AsyncMock()
                with mock.patch.object(ingest, "reject_effect", mock.AsyncMock(return_value=effect)), \
                        mock.patch.object(handlers, "_respond", respond):
                    asyncio.run(handlers._handle_reject(object(), 1))
                self.assertIn(phrase, respond.await_args.kwargs["text"])
                self.assertNotIn("Delete this message", respond.await_args.kwargs["text"])

    def _effect(self, source_key, status=ItemStatus.PENDING_REVIEW):
        item = make_item("x", status=status, source_key=source_key, telegram_chat_id=GROUP)
        with mock.patch.object(ingest, "async_session", session_factory(FakeSession(item))):
            return asyncio.run(ingest.reject_effect(item.id, config()))

    def test_the_effect_is_the_source_s_on_reject(self) -> None:
        self.assertEqual(self._effect("films"), "react")
        self.assertEqual(self._effect("inbox"), "delete")
        self.assertEqual(self._effect("club"), "none")

    def test_a_post_the_app_could_not_read_is_never_touched(self) -> None:
        self.assertEqual(self._effect("inbox", ItemStatus.PARSE_FAILED), "none")

    def test_an_item_whose_source_is_gone_keeps_its_post(self) -> None:
        self.assertEqual(self._effect("removed"), "none")

    def test_the_confirm_button_does_not_promise_a_delete(self) -> None:
        from vettarr.bot.keyboards import confirm_reject_keyboard

        labels = [b.text for row in confirm_reject_keyboard(1).inline_keyboard for b in row]
        self.assertFalse([label for label in labels if "delete" in label.lower()])


# ---------------------------------------------------------------------------
# Real updates through the bot's handlers
# ---------------------------------------------------------------------------


def real_update(text=None, *, caption=None, photo=False, chat=None, user=REVIEWER) -> Update:
    """A real python-telegram-bot Update, as the dispatcher sees it."""
    command = text or caption or ""
    entities = (
        [MessageEntity(MessageEntityType.BOT_COMMAND, 0, len(command.split()[0]))]
        if command.startswith("/") else []
    )
    message = Message(
        message_id=1, date=WHEN, chat=chat or Chat(INBOX_USER_CHAT, "private"),
        from_user=User(user, "someone", False),
        text=text, caption=caption,
        entities=entities if text else None,
        caption_entities=entities if caption else None,
        photo=(PhotoSize("file", "unique", 10, 10),) if photo else None,
    )
    message.set_bot(SimpleNamespace(username="some_bot"))
    return Update(update_id=1, message=message)


class HandlerOrderTests(unittest.TestCase):
    """Which registered handler takes an update: a command never reaches intake."""

    def _taken_by(self, update):
        app = handlers.create_bot_app(None)
        self.addCleanup(ingest.set_review_pusher, None)
        for group in sorted(app.handlers):
            for handler in app.handlers[group]:
                if handler.check_update(update):
                    return handler.callback
        return None

    def test_a_command_goes_to_its_own_handler(self) -> None:
        self.assertIs(self._taken_by(real_update("/next")), handlers.cmd_next)

    def test_an_unknown_command_is_taken_by_nothing(self) -> None:
        self.assertIsNone(self._taken_by(real_update("/nope")))

    def test_a_typed_title_goes_to_intake(self) -> None:
        self.assertIs(self._taken_by(real_update("Some Film 2024")), handlers.on_message)

    def test_a_photo_with_a_caption_goes_to_intake(self) -> None:
        update = real_update(caption="Some Film 2024", photo=True)
        self.assertIs(self._taken_by(update), handlers.on_message)

    def test_a_command_written_as_a_caption_never_becomes_an_item(self) -> None:
        update = real_update(caption="/next", photo=True)
        self.assertIsNot(self._taken_by(update), handlers.on_message)
        self.assertFalse(handlers.INTAKE_FILTER.check_update(update))


# ---------------------------------------------------------------------------
# The bot being added to a chat, and leaving it
# ---------------------------------------------------------------------------


class BotChatTests(unittest.TestCase):
    def _change(self, old, new, by=REVIEWER, chat_id=-100321):
        change = SimpleNamespace(
            old_chat_member=SimpleNamespace(status=old),
            new_chat_member=SimpleNamespace(status=new),
            chat=SimpleNamespace(id=chat_id, title="A title its creator chose", username=None),
            from_user=SimpleNamespace(id=by) if by else None,
        )
        context = SimpleNamespace(bot=SimpleNamespace(can_read_all_group_messages=False))
        self.addCleanup(ingest._bot_chats.clear)
        with mock.patch.object(ingest.settings, "_config", config()):
            asyncio.run(handlers.on_my_chat_member(SimpleNamespace(my_chat_member=change), context))
        return [c.chat_id for c in ingest.bot_chats()]

    def test_a_chat_a_stranger_adds_the_bot_to_is_not_noted(self) -> None:
        self.assertEqual(self._change("left", "member", by=STRANGER), [])
        self.assertEqual(self._change("left", "member", by=None), [])

    def test_a_chat_the_bot_leaves_is_forgotten(self) -> None:
        self.assertEqual(self._change("left", "member"), [-100321])
        self.assertEqual(self._change("member", "kicked", by=STRANGER), [])

    def test_only_so_many_chats_are_remembered(self) -> None:
        self.addCleanup(ingest._bot_chats.clear)
        with mock.patch.object(ingest, "_BOT_CHATS_MAX", 3):
            for chat_id in range(1, 6):
                ingest.bot_added(chat_id, "t", is_admin=False, reads_all_messages=False)
        self.assertEqual(sorted(c.chat_id for c in ingest.bot_chats()), [3, 4, 5])


class ResolvedChatTests(unittest.TestCase):
    def test_a_row_with_no_source_finds_the_source_written_as_a_username(self) -> None:
        """The old group, written as @name and not the first userbot source."""
        cfg = config(sources=[
            {"key": "news", "kind": "userbot", "chat": -1005, "on_reject": "delete"},
            {"key": "old", "kind": "userbot", "chat": "@oldgroup", "on_reject": "none"},
        ])
        self.addCleanup(ingest.set_resolved_chats, None)
        self.assertEqual(ingest.source_for(None, GROUP, cfg).key, "news")
        ingest.set_resolved_chats({-1005: "news", GROUP: "old"})
        self.assertEqual(ingest.source_for(None, GROUP, cfg).key, "old")

    def test_the_userbot_reports_what_it_resolved(self) -> None:
        cfg = config(sources=[
            {"key": "news", "kind": "userbot", "chat": -1005},
            {"key": "old", "kind": "userbot", "chat": "@oldgroup"},
        ])
        with mock.patch.object(telegram_reader, "TelegramClient", mock.MagicMock()):
            ingester = telegram_reader.TelegramIngester(cfg.sources)

        async def peer_id(chat):
            return GROUP if chat == "@oldgroup" else chat

        ingester.client = SimpleNamespace(get_peer_id=peer_id)
        self.addCleanup(ingest.set_resolved_chats, None)
        asyncio.run(ingester.resolve_chats())
        self.assertEqual(ingest.source_for(None, GROUP, cfg).key, "old")


# ---------------------------------------------------------------------------
# What the docs and the image promise
# ---------------------------------------------------------------------------

_ROOT = pathlib.Path(__file__).resolve().parents[1]


class DocsTests(unittest.TestCase):
    doc = (_ROOT / "docs" / "CONFIG.md").read_text()

    def test_every_source_field_is_in_the_sources_table(self) -> None:
        from vettarr.config.schema import Source

        table = self.doc.split("### `sources`")[1].split("#### What a source reads")[0]
        for field in Source.model_fields:
            with self.subTest(field=field):
                self.assertIn(f"| `{field}` |", table)

    def test_the_doc_says_what_a_source_reads(self) -> None:
        reads = self.doc.split("#### What a source reads")[1].split("### `formats`")[0]
        for phrase in (
            "`.torrent` attachment", "`magnet:` link", "IMDb, TMDB or TVDB",
            "In the bot's private chat every short line is looked up",
            "**Forwards.**", "#### Direct adds", "`reviewers` (the default)",
            "/data/torrents/<item id>.torrent", "/data/photos",
            "asks its database whether the message is already an item",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, reads)

    def test_the_doc_no_longer_says_the_sender_is_not_stored(self) -> None:
        self.assertNotIn("Not evaluated yet", self.doc)
        self.assertNotIn("as soon as the sources milestone stores it", self.doc)
        self.assertIn("| `sender`, `per_sender_per_week` |", self.doc)
        self.assertIn("| `post_age_hours` |", self.doc)

    def test_the_kept_torrents_are_a_volume(self) -> None:
        dockerfile = (_ROOT / "Dockerfile").read_text()
        volumes = next(line for line in dockerfile.splitlines() if line.startswith("VOLUME"))
        self.assertIn('"/data/torrents"', volumes)
        self.assertIn(":/data/torrents", (_ROOT / "docker-compose.yml").read_text())


# ---------------------------------------------------------------------------
# Migration 0004: the sender and the post time
# ---------------------------------------------------------------------------

_VERSIONS = _ROOT / "alembic" / "versions"


def _revision_0004():
    spec = importlib.util.spec_from_file_location(
        "_revision_0004", _VERSIONS / "0004_sender_and_post_time.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    out: list = []
    with mock.patch.object(module.op, "execute", out.append):
        module.upgrade()
    return [" ".join(str(s).split()) for s in out], module


class SenderMigrationTests(unittest.TestCase):
    def test_it_follows_the_policy_engine_migration(self) -> None:
        _sql, module = _revision_0004()
        self.assertEqual(module.down_revision, "0003_policy_engine")

    def test_it_adds_two_nullable_columns_and_nothing_else(self) -> None:
        sql, _module = _revision_0004()
        self.assertEqual(len(sql), 1)
        self.assertIn("ADD COLUMN IF NOT EXISTS sender_id BIGINT", sql[0])
        self.assertIn("ADD COLUMN IF NOT EXISTS posted_at TIMESTAMPTZ", sql[0])
        for forbidden in ("UPDATE ", "DELETE ", "DROP ", "NOT NULL", "RENAME", "DEFAULT"):
            self.assertNotIn(forbidden, sql[0].upper())

    def test_the_migrations_are_one_line_with_one_head(self) -> None:
        from alembic.config import Config as AlembicConfig
        from alembic.script import ScriptDirectory

        cfg = AlembicConfig()
        cfg.set_main_option("script_location", str(_ROOT / "alembic"))
        self.assertEqual(len(ScriptDirectory.from_config(cfg).get_heads()), 1)


if __name__ == "__main__":
    unittest.main()
