"""The reading half of ``web/cards.py``, run as real SQL.

The page tests replace the five readers (``WebCase`` hands each page the rows
a test made), so nothing there runs a query: page 1 could show page 2's rows,
a row its oldest search, Review the wrong place in the queue, and every one
of those tests would pass. Here the readers run against SQLite in memory,
with the tables the models declare, and are asked what a page asks them.

SQLite is not the Postgres the app runs on. What these tests hold is what the
two agree on: which rows a query picks, in what order, how many, and which
related row goes with which item.
"""

import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import test_web_foundation  # noqa: F401  sets the environment the config needs, first

from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from vettarr import demo, events
from vettarr.config import settings
from vettarr.config.schema import Config
from vettarr.models import (
    AutoDecision,
    Base,
    ContentCheck,
    Download,
    DownloadStatus,
    Event,
    ItemStatus,
    MediaItem,
    MediaType,
    TorrentSearch,
)
from vettarr.web import cards, shell

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
_S = ItemStatus


@compiles(JSONB, "sqlite")
def _jsonb_on_sqlite(_type, _compiler, **_kw) -> str:
    return "JSON"


class _AsyncSession:
    """The four things the readers ask of an AsyncSession, over a plain one."""

    def __init__(self, engine) -> None:
        self._session = Session(engine)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> bool:
        self._session.close()
        return False

    async def execute(self, statement):
        return self._session.execute(statement)

    async def get(self, model, key):
        return self._session.get(model, key)


def run(coro):
    return asyncio.run(coro)


class ReaderCase(unittest.TestCase):
    """An empty database with the app's tables, and the demo's config."""

    DELIVER: dict | None = None

    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False},
        )
        self.addCleanup(self.engine.dispose)
        Base.metadata.create_all(self.engine)
        document = demo.document(web=False)
        if self.DELIVER is not None:
            document["deliver"] = self.DELIVER
            document["integrations"]["radarr"] = {
                "url": "http://radarr.example", "api_key": "k" * 32, "root_folder": "/movies", "quality_profile": 1,
            }
        for patcher in (
            mock.patch.object(cards, "async_session", lambda: _AsyncSession(self.engine)),
            mock.patch.object(settings, "_config", Config.model_validate(document)),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def put(self, *rows) -> None:
        with Session(self.engine) as session:
            session.add_all(rows)
            session.commit()

    def item(self, item_id: int, status: ItemStatus = _S.PENDING_REVIEW, **values) -> MediaItem:
        """An item created ``item_id`` minutes after T0, so a lower id is an older item."""
        row = MediaItem(
            id=item_id, telegram_message_id=5000 + item_id, telegram_chat_id=-1001, raw_text=f"post {item_id}",
            title=f"Title {item_id}", media_type=MediaType.MOVIE, status=status, source_key="demo-group",
            created_at=T0 + timedelta(minutes=item_id),
        )
        for name, value in values.items():
            setattr(row, name, value)
        self.put(row)
        return row

    def search(self, search_id: int, item_id: int, title: str | None, *, minutes: int, found: int = 1) -> None:
        self.put(TorrentSearch(
            id=search_id, media_item_id=item_id, result_count=found if title else 0,
            best_match_title=title, best_match_size=1000, best_match_seeders=5,
            prowlarr_results=[{"title": title, "size": 1000, "seeders": 5}] if title else [],
            searched_at=T0 + timedelta(minutes=minutes),
        ))


class InboxReaderTests(ReaderCase):
    def test_a_page_shows_its_own_rows_and_the_last_page_is_the_end(self) -> None:
        for n in range(1, 6):
            self.item(n)
        needs = cards.tab_of("needs")
        ids = lambda page: [card.id for card in run(cards.load_inbox(needs, cards.Filters(), page, per_page=2)).cards]  # noqa: E731
        self.assertEqual([ids(1), ids(2), ids(3)], [[1, 2], [3, 4], [5]])
        inbox = run(cards.load_inbox(needs, cards.Filters(), 99, per_page=2))
        self.assertEqual((inbox.page, inbox.pages, inbox.total, [c.id for c in inbox.cards]), (3, 3, 5, [5]))
        self.assertEqual(run(cards.load_inbox(needs, cards.Filters(), 0, per_page=2)).page, 1)

    def test_a_queue_is_oldest_first_and_what_is_finished_newest_first(self) -> None:
        for n in (1, 2, 3):
            self.item(n)
            self.item(10 + n, _S.COMPLETED)
        newest = run(cards.load_inbox(cards.tab_of("done"), cards.Filters()))
        self.assertEqual([c.id for c in newest.cards], [13, 12, 11])
        everything = run(cards.load_inbox(cards.tab_of("all"), cards.Filters()))
        self.assertEqual([c.id for c in everything.cards], [13, 12, 11, 3, 2, 1])

    def test_a_tab_holds_its_statuses_and_every_tab_is_counted(self) -> None:
        for n, status in enumerate(
            (_S.PENDING_REVIEW, _S.ERROR, _S.PARSE_FAILED, _S.AWAITING_DATA, _S.NO_MATCH, _S.COMPLETED, _S.REJECTED, _S.IGNORED),
            start=1,
        ):
            self.item(n, status)
        inbox = run(cards.load_inbox(cards.tab_of("needs"), cards.Filters()))
        self.assertEqual([c.id for c in inbox.cards], [1, 2, 3])
        self.assertEqual(inbox.counts, {"needs": 3, "waiting": 1, "no-release": 1, "done": 2, "all": 8})
        self.assertEqual(inbox.total, 3)

    def test_the_filters_narrow_the_rows_and_the_counts_alike(self) -> None:
        self.item(1)
        self.item(2, media_type=MediaType.SERIES)
        self.item(3, source_key="demo-bot")
        self.item(4, _S.COMPLETED)
        self.item(5, _S.COMPLETED, media_type=MediaType.SERIES)
        movies = run(cards.load_inbox(cards.tab_of("needs"), cards.Filters(type="movie")))
        self.assertEqual([c.id for c in movies.cards], [1, 3])
        self.assertEqual(movies.counts, {"needs": 2, "waiting": 0, "no-release": 0, "done": 1, "all": 3})
        from_bot = run(cards.load_inbox(cards.tab_of("needs"), cards.Filters(source="demo-bot")))
        self.assertEqual(([c.id for c in from_bot.cards], from_bot.counts["all"]), ([3], 1))
        # Every source there is, whatever the filter: the select must keep offering them.
        self.assertEqual(from_bot.sources, ["demo-bot", "demo-group"])

    def test_the_outcome_filter_finds_what_the_verdict_chip_shows(self) -> None:
        """A row from before the policy engine showed "Policy: review" and the filter for review missed it."""
        self.item(1, policy_outcome="review")
        self.item(2, policy_outcome=None, auto_decision=AutoDecision.NEEDS_REVIEW)        # decided by 0.11
        self.item(3, policy_outcome="approve", auto_decision=AutoDecision.NEEDS_REVIEW)   # shadow mode
        self.item(4, _S.AWAITING_DATA, policy_outcome=None, auto_decision=AutoDecision.WAIT)
        self.item(5, _S.APPROVED, policy_outcome=None, auto_decision=AutoDecision.AUTO_APPROVE)
        self.item(6, policy_outcome=None, auto_decision=None)                              # never decided
        needs = cards.tab_of("needs")
        for outcome, ids, counted in (
            ("review", [1, 2], {"needs": 2, "all": 2}),
            ("approve", [3], {"needs": 1, "done": 1, "all": 2}),
            ("wait", [], {"waiting": 1, "all": 1}),
            ("reject", [], {"all": 0}),
        ):
            with self.subTest(outcome=outcome):
                inbox = run(cards.load_inbox(needs, cards.Filters(outcome=outcome)))
                self.assertEqual([c.id for c in inbox.cards], ids)
                for tab, n in counted.items():
                    self.assertEqual(inbox.counts[tab], n, tab)
                # The filter and the chip are one reading of the row.
                for card in inbox.cards:
                    self.assertEqual(card.outcome, outcome)
        everything = run(cards.load_inbox(cards.tab_of("all"), cards.Filters()))
        self.assertEqual(
            {c.id: c.outcome for c in everything.cards},
            {1: "review", 2: "review", 3: "approve", 4: "wait", 5: "approve", 6: None},
        )

    def test_a_row_carries_its_own_check_its_newest_search_and_its_download(self) -> None:
        self.item(1)
        self.item(2)
        self.item(3, _S.DOWNLOADING)
        self.put(ContentCheck(id=1, media_item_id=2, tmdb_id=603, poster_url="https://image/two.jpg"))
        # Stored out of order: the newest is neither the first row nor the last.
        self.search(11, 1, "Old.Release", minutes=10)
        self.search(12, 1, "New.Release", minutes=30, found=4)
        self.search(13, 1, "Middle.Release", minutes=20)
        self.search(14, 2, None, minutes=40)
        self.put(Download(id=1, media_item_id=3, status=DownloadStatus.DOWNLOADING, progress=0.6))
        one, two, three = run(cards.load_inbox(cards.tab_of("all"), cards.Filters())).cards[::-1]
        self.assertEqual((one.best["title"], one.best["search_id"], one.best["found"]), ("New.Release", 12, 4))
        self.assertIsNone(one.check)
        self.assertEqual((two.poster, two.best, two.search["found"]), ("https://image/two.jpg", None, 0))
        self.assertEqual((three.progress, three.downloading, three.search), (60, True, None))
        self.assertIsNone(one.download)

    def test_a_row_knows_what_its_source_does_on_reject(self) -> None:
        self.item(1)                                 # demo-group deletes
        self.item(2, source_key="demo-bot")          # demo-bot does nothing
        self.item(3, _S.PARSE_FAILED)                # never touched, whatever the source says
        inbox = run(cards.load_inbox(cards.tab_of("needs"), cards.Filters()))
        self.assertEqual([c.post_effect for c in inbox.cards], ["delete", "none", "none"])
        self.assertEqual(inbox.cards[0].reject_confirm, "Its post will be deleted.")
        self.assertEqual(inbox.bulk_reject_confirm, cards.REJECT_BULK_MIXED)

    def test_load_rows_answers_in_the_order_asked_with_the_filtered_counts(self) -> None:
        for n in (1, 2, 3):
            self.item(n)
        self.item(4, media_type=MediaType.SERIES)
        loaded, counts = run(cards.load_rows([3, 99, 1], cards.Filters(type="movie")))
        self.assertEqual([c.id for c in loaded], [3, 1])
        self.assertEqual((counts["needs"], counts["all"]), (3, 3))
        self.assertEqual(run(cards.load_rows([], cards.Filters()))[0], [])


class ReviewReaderTests(ReaderCase):
    def setUp(self) -> None:
        super().setUp()
        for n in (1, 2, 3):
            self.item(n)
        self.item(4, _S.COMPLETED)
        self.item(5, _S.AWAITING_DATA)

    def place(self, **asked) -> tuple[int | None, int | None, int]:
        review = run(cards.load_review(**asked))
        return (review.card.id if review.card else None), review.position, review.total

    def test_the_oldest_first_and_each_item_its_place(self) -> None:
        self.assertEqual(self.place(), (1, 1, 3))
        self.assertEqual(self.place(item=2), (2, 2, 3))
        self.assertEqual(self.place(item=3), (3, 3, 3))

    def test_next_and_previous_follow_the_queue_and_wrap(self) -> None:
        self.assertEqual(self.place(after=1), (2, 2, 3))
        self.assertEqual(self.place(after=3), (1, 1, 3))
        self.assertEqual(self.place(before=1), (3, 3, 3))
        self.assertEqual(self.place(before=3), (2, 2, 3))

    def test_an_item_that_left_the_queue_has_no_place_and_still_a_next(self) -> None:
        self.assertEqual(self.place(item=4), (4, None, 3))
        # Created after 1, 2 and 3, so the one after it is the oldest again.
        self.assertEqual(self.place(after=4), (1, 1, 3))
        self.assertEqual(self.place(item=999), (None, None, 3))

    def test_the_card_carries_the_item_s_newest_search(self) -> None:
        self.search(21, 2, "Old.Release", minutes=10)
        self.search(22, 2, "New.Release", minutes=20)
        review = run(cards.load_review(item=2))
        self.assertEqual(review.card.best["title"], "New.Release")
        self.assertTrue(review.card.can_grab_best)
        self.assertFalse(run(cards.load_review(item=1)).card.can_grab_best)


class ReviewOrderTests(ReaderCase):
    def test_the_items_to_decide_come_before_errors_and_unread_posts(self) -> None:
        """Review opened on an item in Error ("1 of 18"): the fast loop started with a retry."""
        self.item(1, _S.ERROR)
        self.item(2, _S.PARSE_FAILED, title=None)
        self.item(3)
        self.item(4)
        review = run(cards.load_review())
        self.assertEqual((review.card.id, review.position, review.total), (3, 1, 4))
        self.assertEqual(run(cards.load_review(after=3)).card.id, 4)
        self.assertEqual(run(cards.load_review(after=4)).card.id, 1)
        self.assertEqual(run(cards.load_review(after=2)).card.id, 3)
        self.assertEqual(run(cards.load_review(before=3)).card.id, 2)


class NeedsYouAgreesTests(ReaderCase):
    def test_the_badge_the_tab_review_and_activity_say_one_number_on_the_demo_seed(self) -> None:
        """On a fresh seed the nav said 14, the tab 18, Review "1 of 18" and Activity 14."""
        built = demo.build(T0)
        self.put(*built.items)
        with mock.patch.object(shell, "async_session", lambda: _AsyncSession(self.engine)):
            badge = run(shell.needs_you())
        tab = run(cards.load_inbox(cards.tab_of("needs"), cards.Filters())).counts["needs"]
        review = run(cards.load_review()).total
        # Activity's "N items need you" is the badge's number, read by the same layout.
        with Session(self.engine) as session:
            from sqlalchemy import func, select
            waiting = session.execute(select(func.count()).select_from(shell.waiting_items().subquery())).scalar()
            pending = session.execute(
                select(func.count()).select_from(MediaItem).where(MediaItem.status == _S.PENDING_REVIEW)
            ).scalar()
        self.assertEqual({badge, tab, review, waiting}, {tab})
        # Not a number every reader could agree on by accident.
        self.assertGreater(tab, pending)


class DetailReaderTests(ReaderCase):
    def test_the_whole_item_with_what_belongs_to_it_and_nothing_of_another(self) -> None:
        self.item(1)
        self.item(2, _S.DOWNLOADING)
        self.put(ContentCheck(id=1, media_item_id=1, tmdb_id=603, plot="One."),
                 ContentCheck(id=2, media_item_id=2, tmdb_id=604, plot="Two."))
        self.search(31, 2, "Old.Release", minutes=10)
        self.search(32, 2, "New.Release", minutes=20)
        self.search(33, 1, "Another.Item", minutes=30)
        self.put(Download(id=1, media_item_id=2, status=DownloadStatus.DOWNLOADING, progress=0.25, created_torrent=True))
        self.put(
            Event(id=1, item_id=2, kind=events.NEEDS_YOU, at=T0 + timedelta(minutes=9), payload={}),
            Event(id=2, item_id=2, kind=events.DECISION, at=T0 + timedelta(minutes=8),
                  payload={"outcome": "review", "done": "review", "reasons": ["imdb.violence: severe"]}),
            Event(id=3, item_id=1, kind=events.BOT_ADDED, at=T0, payload={}),
        )
        detail = run(cards.load_detail(2))
        self.assertEqual(detail.card.check.plot, "Two.")
        self.assertEqual([s.best_match_title for s in detail.searches], ["New.Release", "Old.Release"])
        self.assertEqual(detail.card.best["title"], "New.Release")
        self.assertEqual(detail.card.progress, 25)
        told = [m.text for m in detail.history]
        self.assertLess(told.index("The policy decided to hold it for a person: Violence (IMDb): severe"),
                        told.index("Sent to the review queue"))
        self.assertNotIn("Sent to the bot", told)
        self.assertEqual(detail.card.reject_confirm, f"Its post will be deleted. {cards.REJECT_OWN_DOWNLOAD}")
        self.assertIsNone(detail.delivery)
        self.assertIsNone(run(cards.load_detail(999)))

    def test_the_download_poll_reads_the_item_with_its_download(self) -> None:
        """Without the download the block is empty and never says the download is over."""
        self.item(1, _S.DOWNLOADING)
        self.item(2)
        self.put(Download(id=1, media_item_id=1, status=DownloadStatus.DOWNLOADING, progress=0.6))
        card = run(cards.load_download(1))
        self.assertEqual((card.id, card.progress, card.downloading), (1, 60, True))
        self.assertIsNone(run(cards.load_download(2)).download)
        self.assertIsNone(run(cards.load_download(999)))


class DeliveredElsewhereTests(ReaderCase):
    """A type ``deliver`` sends to Radarr: the page says where, and offers no release of its own."""

    DELIVER = {"movie": "radarr", "series": "qbittorrent"}

    def test_the_item_page_says_where_approving_sends_it(self) -> None:
        self.item(1, raw_text="magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567")
        self.search(41, 1, "Some.Release", minutes=10)
        with mock.patch.object(cards.ingest, "direct_add_of", return_value="magnet"):
            detail = run(cards.load_detail(1))
            [row] = run(cards.load_inbox(cards.tab_of("needs"), cards.Filters())).cards
        self.assertEqual(
            detail.delivery,
            "Approving sends the title to Radarr, which picks its own release. "
            "The magnet this post carries is not what downloads.",
        )
        for card in (detail.card, row):
            # The post's magnet is not what Radarr fetches, and neither is a picked release.
            self.assertIsNone(card.direct_add)
            self.assertFalse(card.can_grab or card.can_grab_best)
            self.assertTrue(card.can_approve)

    def test_a_type_for_the_download_client_is_as_ever(self) -> None:
        self.item(1, media_type=MediaType.SERIES)
        self.search(41, 1, "Some.Release", minutes=10)
        with mock.patch.object(cards.ingest, "direct_add_of", return_value="magnet"):
            detail = run(cards.load_detail(1))
        self.assertEqual((detail.delivery, detail.card.direct_add, detail.card.can_grab_best), (None, "magnet", True))

    def test_what_was_sent_there_is_said_and_rejecting_names_it(self) -> None:
        self.item(1, _S.DOWNLOADING)
        self.put(Download(
            id=1, media_item_id=1, status=DownloadStatus.DOWNLOADING, delivery_target="radarr",
            remote_id="11", created_torrent=True,
        ))
        detail = run(cards.load_detail(1))
        self.assertIn("Radarr:", detail.delivery)
        self.assertIn("(id 11)", detail.delivery)
        self.assertEqual(
            detail.card.reject_confirm,
            "Its post will be deleted. What this app added to Radarr for it is taken back.",
        )


if __name__ == "__main__":
    unittest.main()
