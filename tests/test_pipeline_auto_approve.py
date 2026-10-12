"""Pipeline wiring: what a decision actually does to an item, and what the
service is allowed to delete.

The database is faked. These tests are about the decisions the pipeline makes,
not about SQL.
"""

import asyncio
import contextlib
import logging
import os
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

from sqlalchemy.sql import Update

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

from pathlib import Path

import policy_fixture
from policy_fixture import FLAGGED_GENRE_ID

from vettarr import pipeline, review
from vettarr.config import Settings
from vettarr.config.loader import load
from vettarr.parser.formats import compile_formats
from vettarr.checker.book_client import BookVerdict
from vettarr.checker.content_rules import CONCURRENT_DUPLICATE
from vettarr.checker.policy import APPROVE, REVIEW, WAIT, Applied, Decision, Reason
from vettarr.checker.imdb_client import ParentsGuide
from vettarr.checker.tmdb_client import MATCH_WITH_YEAR, TMDBResult
from vettarr.models import (
    AutoDecision,
    ItemStatus,
    MediaItem,
    MediaType,
    Recommendation,
    SeverityRating,
)

NOW = datetime(2026, 9, 21, 10, 0, tzinfo=timezone.utc)

# Posts are read with the example formats, whatever config the process has.
EXAMPLE = Path(__file__).resolve().parents[1] / "config.example.yaml"
EXAMPLE_FORMATS = compile_formats(load(EXAMPLE, env={}).formats)


def example_formats():
    return mock.patch("vettarr.parser.regex_parser.formats_for", lambda *_: EXAMPLE_FORMATS)

MOVIE_POST = "Some Film (2024) 1080p WEB-DL DD+5.1 H.264-GROUP\n8.4 GiB"
MOVIE_TITLE = "Some Film"
MOVIE_YEAR = 2024


def matched_tmdb(**kwargs) -> TMDBResult:
    """A TMDB record the identity check accepts as the film in MOVIE_POST."""
    defaults = {
        "tmdb_id": 1,
        "imdb_id": "tt1",
        "title": MOVIE_TITLE,
        "original_title": MOVIE_TITLE,
        "year": MOVIE_YEAR,
        "match_mode": MATCH_WITH_YEAR,
        "keywords": ["based on a novel"],
    }
    defaults.update(kwargs)
    return TMDBResult(**defaults)


def full_guide(**kwargs) -> ParentsGuide:
    defaults = {
        "sex_rating": SeverityRating.NONE,
        "violence_rating": SeverityRating.MILD,
        "frightening_rating": SeverityRating.NONE,
        "fetched": True,
        "votes": {"NUDITY": 100, "VIOLENCE": 100, "FRIGHTENING": 100},
    }
    defaults.update(kwargs)
    return ParentsGuide(**defaults)
MAGAZINE_POST = "\U0001f4f0 NEW MAGAZINE\nTitle: Example Geographic\nLink: https://example.invalid/x"
EBOOK_POST = "\U0001f4da NEW EBOOK\nTitle: The Name of the Wind\nLink: https://example.invalid/y"
INSTAGRAM_POST = (
    "look at this a friend sent me this afternoon after work\n"
    "https://www.instagram.com/reel/DAbC12xyz/?utm_source=ig_web_copy_link&igsh=MXYz"
)


def settings_stub(**kwargs):
    """The fixture's operator-shaped policy, varied through the 0.11 knobs."""
    base = {
        "auto_approve_enabled": True,
        "auto_approve_types": frozenset(
            {"movie", "series", "magazine", "ebook", "audiobook", "comic"}
        ),
        "min_guide_votes": 5,
        "auto_approve_max_wait_hours": 24,
    }
    base.update(kwargs)
    return policy_fixture.settings_stub(**base)


def verdict(outcome: str, *texts: str) -> "pipeline.Verdict":
    reasons = tuple(Reason("rule", None, t) for t in texts)
    return pipeline.Verdict(Decision(outcome, reasons, "v1"), Applied(outcome, reasons), {})


def make_item(raw_text: str, **kwargs) -> MediaItem:
    item = MediaItem(
        id=1,
        telegram_message_id=4242,
        telegram_chat_id=-1,
        raw_text=raw_text,
        status=ItemStatus.PENDING_PARSE,
        media_type=MediaType.UNKNOWN,
        auto_recommendation=Recommendation.NEUTRAL,
    )
    item.created_at = NOW - timedelta(minutes=1)
    for key, value in kwargs.items():
        setattr(item, key, value)
    return item


class FakeResult:
    def __init__(self, value=None):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def scalars(self):
        return self

    def first(self):
        return self._value

    def all(self):
        return [] if self._value is None else [self._value]

    def __iter__(self):
        return iter([] if self._value is None else [self._value])


def where_of(statement) -> dict:
    """Column name -> value, for the equality conditions of a statement."""
    pairs = {}
    for criterion in statement._where_criteria:
        left = getattr(criterion, "left", None)
        right = getattr(criterion, "right", None)
        if left is not None and hasattr(right, "value"):
            pairs[left.name] = right.value
    return pairs


def set_of(statement) -> dict:
    return {
        column.name: getattr(value, "value", value)
        for column, value in statement._values.items()
    }


class FakeRows:
    """A result you can iterate, the way a SELECT of one column comes back."""

    def __init__(self, rows):
        self._rows = list(rows)

    def __iter__(self):
        return iter(self._rows)


class FakeSession:
    """Just enough of an AsyncSession for the pipeline's own calls.

    The conditional UPDATE is honoured for real: the WHERE on status is
    checked against the item as it stands *now*, and only a matching update
    writes its values onto it. That is the whole point of these tests -- a
    rejection landing mid-lookup has to be able to turn the update into a
    no-op, exactly as PostgreSQL would.
    """

    def __init__(self, item: MediaItem | None):
        self.item = item
        self.added: list = []
        self.commits = 0
        self.rollbacks = 0
        self.updates: list = []
        #: Called just before a conditional UPDATE is evaluated, which is the
        #: instant a human decision can land.
        self.before_update = None

    async def get(self, _model, pk, **_kwargs):
        if self.item is not None and self.item.id == pk:
            return self.item
        return None

    async def execute(self, statement):
        if not isinstance(statement, Update):
            return FakeResult(None)
        self.updates.append(statement)
        if self.before_update is not None:
            hook, self.before_update = self.before_update, None
            hook()
        conditions = where_of(statement)
        if self.item is None or conditions.get("id") != self.item.id:
            return FakeResult(None)
        if "status" in conditions and conditions["status"] is not self.item.status:
            return FakeResult(None)
        for column, value in set_of(statement).items():
            setattr(self.item, column, value)
        return FakeResult(self.item.id)

    def add(self, obj) -> None:
        self.added.append(obj)

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def session_factory(session: FakeSession):
    return lambda: session


def as_content(content):
    """What the content check returns: the TMDB record and the parents guide."""
    if content is None:
        return content
    return tuple(content[:2])


def run_process(
    item: MediaItem, *, settings=None, content=None, duplicate=None, book=None,
    twin=None, session=None,
):
    session = session or FakeSession(item)
    patches = [
        mock.patch.object(pipeline, "async_session", session_factory(session)),
        mock.patch.object(pipeline, "settings", settings or settings_stub()),
        mock.patch.object(pipeline, "_now", lambda: NOW),
        example_formats(),
        mock.patch.object(pipeline, "_check_duplicate", mock.AsyncMock(return_value=duplicate)),
        mock.patch.object(
            pipeline, "_check_in_flight_twin", mock.AsyncMock(return_value=twin)
        ),
    ]
    if content is not None:
        async def fake_check_content(_session, checked_item):
            # The real one parks the item in CHECKING before it looks anything
            # up, and that status is what the closing UPDATE tests for.
            checked_item.status = ItemStatus.CHECKING
            return as_content(content)

        patches.append(
            mock.patch.object(pipeline, "_check_content", fake_check_content)
        )
    if book is not None:
        patches.append(
            mock.patch.object(pipeline, "check_book", mock.AsyncMock(return_value=book))
        )
    with contextlib.ExitStack() as stack:
        for patch in patches:
            stack.enter_context(patch)
        asyncio.run(pipeline.process_item(item.id))
    return session


class ApplyDecisionTests(unittest.TestCase):
    """AUTO_APPROVE has to leave exactly what a human approval leaves, so the
    existing search worker picks the item up with no further wiring."""

    def test_auto_approve_lands_in_approved(self) -> None:
        item = make_item(MOVIE_POST, status=ItemStatus.PENDING_REVIEW, error_message="old")
        pipeline.apply_decision(item, verdict(APPROVE, "clean"), NOW)
        self.assertEqual(item.status, ItemStatus.APPROVED)
        self.assertEqual(item.reviewed_at, NOW)
        self.assertIsNone(item.error_message)
        self.assertEqual(item.auto_decision, AutoDecision.AUTO_APPROVE)
        self.assertEqual(item.auto_reasons, ["clean"])
        self.assertEqual(item.auto_decided_at, NOW)

    def test_wait_parks_the_item_without_reviewing_it(self) -> None:
        item = make_item(MOVIE_POST)
        pipeline.apply_decision(item, verdict(WAIT, "no data"), NOW)
        self.assertEqual(item.status, ItemStatus.AWAITING_DATA)
        self.assertIsNone(item.reviewed_at)
        self.assertEqual(item.auto_reasons, ["no data"])

    def test_needs_review_lands_in_pending_review(self) -> None:
        item = make_item(MOVIE_POST)
        pipeline.apply_decision(item, verdict(REVIEW, "flagged"), NOW)
        self.assertEqual(item.status, ItemStatus.PENDING_REVIEW)
        self.assertIsNone(item.reviewed_at)

    def test_needs_review_keeps_the_flag_message(self) -> None:
        item = make_item(MOVIE_POST, error_message="Duplicate: DB: Some Film")
        pipeline.apply_decision(item, verdict(REVIEW, "duplicate"), NOW)
        self.assertEqual(item.error_message, "Duplicate: DB: Some Film")


class ProcessItemTests(unittest.TestCase):
    def test_a_clean_film_is_approved_end_to_end(self) -> None:
        item = make_item(MOVIE_POST)
        run_process(item, content=(matched_tmdb(), full_guide()))
        self.assertEqual(item.status, ItemStatus.APPROVED)
        self.assertEqual(item.auto_decision, AutoDecision.AUTO_APPROVE)
        self.assertEqual(item.reviewed_at, NOW)

    def test_a_film_with_no_guide_waits(self) -> None:
        item = make_item(MOVIE_POST)
        run_process(item, content=(matched_tmdb(), ParentsGuide()))
        self.assertEqual(item.status, ItemStatus.AWAITING_DATA)
        self.assertEqual(item.auto_decision, AutoDecision.WAIT)

    def test_a_film_with_a_flagged_genre_needs_review(self) -> None:
        item = make_item(MOVIE_POST)
        tmdb = matched_tmdb(genre_ids=[FLAGGED_GENRE_ID])
        run_process(item, content=(tmdb, ParentsGuide(fetched=True)))
        self.assertEqual(item.status, ItemStatus.PENDING_REVIEW)
        self.assertEqual(item.auto_decision, AutoDecision.NEEDS_REVIEW)

    def test_a_magazine_is_approved_without_any_lookup(self) -> None:
        item = make_item(MAGAZINE_POST)
        run_process(item)
        self.assertEqual(item.media_type, MediaType.MAGAZINE)
        self.assertEqual(item.status, ItemStatus.APPROVED)
        self.assertEqual(item.auto_decision, AutoDecision.AUTO_APPROVE)

    def test_a_clean_book_is_approved(self) -> None:
        item = make_item(EBOOK_POST)
        verdict = BookVerdict(
            matched=True, source="openlibrary", matched_title="El nombre del viento",
            subjects=["Fiction", "Adventure", "Spanish literature"],
        )
        run_process(
            item,
            settings=settings_stub(
                auto_approve_types=frozenset({"movie", "series", "magazine", "ebook"})
            ),
            book=verdict,
        )
        self.assertEqual(item.media_type, MediaType.EBOOK)
        self.assertEqual(item.status, ItemStatus.APPROVED)

    def test_an_unclassifiable_book_with_a_clean_title_is_approved(self) -> None:
        item = make_item(EBOOK_POST)
        run_process(item, book=BookVerdict())
        self.assertEqual(item.status, ItemStatus.APPROVED)
        self.assertEqual(item.auto_decision, AutoDecision.AUTO_APPROVE)

    def test_an_unclassifiable_book_is_approved_whatever_its_title_says(self) -> None:
        """The title-word rule is gone: only a catalogue subject flags a book."""
        item = make_item(EBOOK_POST.replace("El nombre del viento", "Manual de list-a-word"))
        run_process(item, book=BookVerdict(error="http 429"))
        self.assertEqual(item.status, ItemStatus.APPROVED)
        self.assertEqual(item.auto_decision, AutoDecision.AUTO_APPROVE)
        self.assertIn(
            "subject: no catalogue classification (lookup failed: http 429)", item.auto_reasons
        )

    def test_a_duplicate_is_never_approved(self) -> None:
        item = make_item(MAGAZINE_POST)
        run_process(item, duplicate="DB: Trail and Tide - Octubre 2022")
        self.assertEqual(item.status, ItemStatus.PENDING_REVIEW)
        self.assertEqual(item.auto_recommendation, Recommendation.REJECT)
        self.assertIn("Duplicate", item.error_message)

    def test_the_switch_being_off_approves_nothing(self) -> None:
        item = make_item(MAGAZINE_POST)
        run_process(item, settings=settings_stub(auto_approve_enabled=False))
        self.assertEqual(item.status, ItemStatus.PENDING_REVIEW)
        self.assertEqual(item.auto_decision, AutoDecision.NEEDS_REVIEW)

    def test_a_type_off_the_allow_list_approves_nothing(self) -> None:
        item = make_item(MAGAZINE_POST)
        run_process(item, settings=settings_stub(auto_approve_types=frozenset({"movie"})))
        self.assertEqual(item.status, ItemStatus.PENDING_REVIEW)

    def test_a_book_is_not_looked_up_while_the_switch_is_off(self) -> None:
        item = make_item(EBOOK_POST)
        lookup = mock.AsyncMock(return_value=BookVerdict())
        session = FakeSession(item)
        with (
            mock.patch.object(pipeline, "async_session", session_factory(session)),
            mock.patch.object(pipeline, "settings", settings_stub(auto_approve_enabled=False)),
            mock.patch.object(pipeline, "_now", lambda: NOW),
            example_formats(),
            mock.patch.object(pipeline, "_check_duplicate", mock.AsyncMock(return_value=None)),
            mock.patch.object(pipeline, "check_book", lookup),
        ):
            asyncio.run(pipeline.process_item(item.id))
        lookup.assert_not_awaited()

    def test_a_book_is_looked_up_with_the_configured_format_tags(self) -> None:
        """A group's own audiobook tag is a word of `release_words.format_tags`,
        never of the code, so the lookup is handed the config's words."""
        item = make_item(EBOOK_POST)
        lookup = mock.AsyncMock(return_value=BookVerdict())
        stub = settings_stub()
        words = stub.config.release_words.model_copy(update={"format_tags": ["mp3", "narrated"]})
        stub = Settings(stub.config.model_copy(update={"release_words": words}))
        session = FakeSession(item)
        with (
            mock.patch.object(pipeline, "async_session", session_factory(session)),
            mock.patch.object(pipeline, "settings", stub),
            mock.patch.object(pipeline, "_now", lambda: NOW),
            example_formats(),
            mock.patch.object(pipeline, "_check_duplicate", mock.AsyncMock(return_value=None)),
            mock.patch.object(pipeline, "_check_in_flight_twin", mock.AsyncMock(return_value=None)),
            mock.patch.object(pipeline, "check_book", lookup),
        ):
            asyncio.run(pipeline.process_item(item.id))
        lookup.assert_awaited_once()
        self.assertEqual(list(lookup.await_args.args[2]), ["mp3", "narrated"])


class NonMediaTests(unittest.TestCase):
    def test_an_instagram_note_is_ignored_not_queued(self) -> None:
        item = make_item(INSTAGRAM_POST)
        run_process(item)
        self.assertEqual(item.status, ItemStatus.IGNORED)
        self.assertIn("instagram.com", item.error_message)

    def test_a_plain_note_is_ignored(self) -> None:
        item = make_item("remember to call the plumber tomorrow in the morning please")
        run_process(item)
        self.assertEqual(item.status, ItemStatus.IGNORED)

    def test_a_real_release_post_is_never_ignored(self) -> None:
        item = make_item(MOVIE_POST)
        run_process(item, content=(TMDBResult(), ParentsGuide()))
        self.assertNotEqual(item.status, ItemStatus.IGNORED)
        self.assertEqual(item.media_type, MediaType.MOVIE)

    def test_an_unreadable_media_post_still_fails_parsing(self) -> None:
        """A post that looks like a release but will not parse stays visible."""
        item = make_item("Type: Movies - 1080p - WEB-DL - 4.2 GiB")
        run_process(item, content=(TMDBResult(), ParentsGuide()))
        self.assertIn(item.status, (ItemStatus.PARSE_FAILED, ItemStatus.PENDING_REVIEW,
                                    ItemStatus.AWAITING_DATA, ItemStatus.APPROVED))
        self.assertNotEqual(item.status, ItemStatus.IGNORED)

    def test_an_ignored_item_is_never_re_parsed(self) -> None:
        """reprocess_failed only ever picks up PARSE_FAILED rows."""
        import inspect
        source = inspect.getsource(pipeline.reprocess_failed)
        self.assertIn("ItemStatus.PARSE_FAILED", source)
        self.assertNotIn("ItemStatus.IGNORED", source)


class SourceFormatsTests(unittest.TestCase):
    """An item is read with the formats of the source it came from.

    Two sources share the example formats: `cards` lists the card format,
    `inbox` the scene format alone. Nothing here patches the format lookup;
    the parser asks the config for the item's own source.
    """

    CARD_POST = "NEW MAGAZINE\nTitle: whatever was typed in the scene-only source"
    NOTE = "remember to call the plumber tomorrow in the morning, please."

    def process(self, item: MediaItem) -> MediaItem:
        from vettarr import ingest
        from vettarr.config import runtime
        from vettarr.config.schema import Source
        from vettarr.parser import formats as formats_mod

        cfg = load(EXAMPLE, env={}).model_copy(update={"sources": [
            Source(key="cards", kind="userbot", chat=-1, formats=["card"]),
            Source(key="inbox", kind="userbot", chat=-2, formats=["scene"]),
        ]})
        self.addCleanup(setattr, formats_mod, "_cache", None)
        with (
            mock.patch.object(pipeline, "async_session", session_factory(FakeSession(item))),
            mock.patch.object(pipeline, "settings", settings_stub()),
            mock.patch.object(pipeline, "_now", lambda: NOW),
            mock.patch.object(runtime, "get_config", lambda: cfg),
            mock.patch.object(ingest.settings, "_config", cfg),
            mock.patch.object(pipeline, "_check_duplicate", mock.AsyncMock(return_value=None)),
            mock.patch.object(pipeline, "_check_in_flight_twin", mock.AsyncMock(return_value=None)),
        ):
            asyncio.run(pipeline.process_item(item.id))
        return item

    def test_a_card_in_the_source_that_reads_cards(self) -> None:
        item = self.process(make_item(self.CARD_POST, source_key="cards"))
        self.assertEqual(item.media_type, MediaType.MAGAZINE)
        self.assertEqual(item.title, "whatever was typed in the scene-only source")

    def test_the_same_card_in_the_scene_only_source(self) -> None:
        item = self.process(make_item(self.CARD_POST, source_key="inbox"))
        self.assertEqual(item.media_type, MediaType.UNKNOWN)
        self.assertEqual(item.status, ItemStatus.PARSE_FAILED)

    def test_a_row_with_no_source_is_read_as_the_source_on_its_chat(self) -> None:
        item = self.process(make_item(self.CARD_POST, telegram_chat_id=-2))
        self.assertEqual(item.media_type, MediaType.UNKNOWN)
        item = self.process(make_item(self.CARD_POST, telegram_chat_id=-1))
        self.assertEqual(item.media_type, MediaType.MAGAZINE)

    def test_a_note_is_classified_with_its_own_sources_formats(self) -> None:
        """The card format lists media words and the note holds none, so in
        `cards` it is finished with. `inbox` has no such list and keeps it."""
        self.assertEqual(self.process(make_item(self.NOTE, source_key="cards")).status, ItemStatus.IGNORED)
        self.assertEqual(self.process(make_item(self.NOTE, source_key="inbox")).status, ItemStatus.PARSE_FAILED)


class RejectionTests(unittest.TestCase):
    """The group post is the only copy of a request. Nothing the service could
    not read gets destroyed automatically."""

    def _reject(self, item):
        session = FakeSession(item)
        with mock.patch.object(review, "async_session", session_factory(session)):
            return asyncio.run(review.reject_item(item.id))

    def test_a_pending_item_may_have_its_post_deleted(self) -> None:
        item = make_item(MOVIE_POST, status=ItemStatus.PENDING_REVIEW)
        outcome = self._reject(item)
        self.assertTrue(outcome.rejected)
        self.assertEqual(outcome.telegram_message_id, 4242)
        self.assertEqual(item.status, ItemStatus.REJECTED)

    def test_a_parse_failed_item_keeps_its_post(self) -> None:
        item = make_item(MOVIE_POST, status=ItemStatus.PARSE_FAILED)
        outcome = self._reject(item)
        self.assertTrue(outcome.rejected)
        self.assertIsNone(outcome.telegram_message_id)

    def test_an_ignored_item_cannot_be_rejected_at_all(self) -> None:
        item = make_item(INSTAGRAM_POST, status=ItemStatus.IGNORED)
        outcome = self._reject(item)
        self.assertFalse(outcome.rejected)
        self.assertIsNone(outcome.telegram_message_id)
        self.assertEqual(item.status, ItemStatus.IGNORED)

    def test_a_completed_item_cannot_be_rejected(self) -> None:
        item = make_item(MOVIE_POST, status=ItemStatus.COMPLETED)
        self.assertFalse(self._reject(item).rejected)

    def test_a_searching_item_is_left_to_the_worker(self) -> None:
        item = make_item(MOVIE_POST, status=ItemStatus.SEARCHING)
        self.assertFalse(self._reject(item).rejected)

    def test_an_auto_approved_item_can_be_taken_back(self) -> None:
        item = make_item(MOVIE_POST, status=ItemStatus.APPROVED)
        outcome = self._reject(item)
        self.assertTrue(outcome.rejected)
        self.assertEqual(outcome.telegram_message_id, 4242)

    def test_a_downloading_item_can_be_taken_back(self) -> None:
        item = make_item(MOVIE_POST, status=ItemStatus.DOWNLOADING)
        self.assertTrue(self._reject(item).rejected)

    def test_a_waiting_item_can_be_rejected(self) -> None:
        item = make_item(MOVIE_POST, status=ItemStatus.AWAITING_DATA)
        self.assertTrue(self._reject(item).rejected)

    def test_nothing_is_deleted_when_the_post_must_be_kept(self) -> None:
        ingester = mock.AsyncMock()
        qbt = mock.AsyncMock()
        outcome = review.RejectOutcome(item_id=1, rejected=True, telegram_message_id=None)
        asyncio.run(review.finish_rejection(outcome, ingester, qbt))
        ingester.after.assert_not_awaited()
        qbt.remove_torrent.assert_not_awaited()

    def test_nothing_happens_when_the_rejection_did_not_take(self) -> None:
        ingester = mock.AsyncMock()
        outcome = review.RejectOutcome(item_id=1, rejected=False, telegram_message_id=4242)
        asyncio.run(review.finish_rejection(outcome, ingester, mock.AsyncMock()))
        ingester.after.assert_not_awaited()

    def test_a_running_torrent_is_dropped_with_the_rejection(self) -> None:
        ingester = mock.AsyncMock()
        qbt = mock.AsyncMock()
        outcome = review.RejectOutcome(
            item_id=1, rejected=True, telegram_message_id=4242, torrent_hash="abc123"
        )
        asyncio.run(review.finish_rejection(outcome, ingester, qbt))
        qbt.remove_torrent.assert_awaited_once_with("abc123")
        ingester.after.assert_awaited_once_with("reject", 1)


class RejectWhileCheckingTests(unittest.TestCase):
    """A rejection taken while the lookups are running is the one that stands.

    CHECKING is a status the operator can reject from, and the lookups behind it take
    as long as TMDB and IMDb take. The first pass used to write its decision
    through the loaded row with no condition, so a reject that landed in that
    window was simply overwritten: with the switch on the item downloaded after
    its group post had already been deleted, and with the switch off it came
    back as PENDING_REVIEW. The closing write is now conditional on the item
    still being CHECKING.
    """

    def _run(self, *, enabled: bool, content, reject: bool):
        item = make_item(MOVIE_POST)
        session = FakeSession(item)
        if reject:
            session.before_update = lambda: setattr(
                item, "status", ItemStatus.REJECTED
            )
        run_process(
            item, session=session, content=content,
            settings=settings_stub(auto_approve_enabled=enabled),
        )
        return item, session

    def test_a_rejection_mid_lookup_survives_every_outcome(self) -> None:
        clean = (matched_tmdb(), full_guide())
        flagged = (matched_tmdb(genre_ids=[FLAGGED_GENRE_ID]), full_guide())
        for enabled, label, content in (
            (True, "would have auto-approved", clean),
            (True, "would have needed review", flagged),
            (False, "switch off, clean film", clean),
            (False, "switch off, flagged film", flagged),
        ):
            with self.subTest(enabled=enabled, outcome=label):
                item, _session = self._run(
                    enabled=enabled, content=content, reject=True,
                )
                self.assertEqual(item.status, ItemStatus.REJECTED)

    def test_the_dropped_run_writes_nothing_at_all(self) -> None:
        """Not the decision, not the reasons, not the recommendation."""
        item, session = self._run(
            enabled=True, content=(matched_tmdb(), full_guide()), reject=True,
        )
        self.assertIsNone(item.auto_decision)
        self.assertIsNone(item.auto_reasons)
        self.assertIsNone(item.auto_decided_at)
        self.assertIsNone(item.reviewed_at)
        self.assertEqual(session.rollbacks, 1)

    def test_a_rejected_item_never_reaches_the_search_worker(self) -> None:
        """The search worker polls for APPROVED, so not writing APPROVED is
        what keeps the download from happening."""
        item, _session = self._run(
            enabled=True, content=(matched_tmdb(), full_guide()), reject=True,
        )
        self.assertNotEqual(item.status, ItemStatus.APPROVED)

    def test_the_closing_write_asks_for_the_item_to_still_be_checking(self) -> None:
        _item, session = self._run(
            enabled=True, content=(matched_tmdb(), full_guide()), reject=False,
        )
        conditions = where_of(session.updates[-1])
        self.assertEqual(conditions.get("status"), ItemStatus.CHECKING)
        self.assertEqual(conditions.get("id"), 1)

    def test_an_untouched_item_is_decided_as_it_always_was(self) -> None:
        """The control: without a rejection the same run lands normally."""
        item, _session = self._run(
            enabled=True, content=(matched_tmdb(), full_guide()), reject=False,
        )
        self.assertEqual(item.status, ItemStatus.APPROVED)
        self.assertEqual(item.auto_decision, AutoDecision.AUTO_APPROVE)

    def test_a_post_that_never_enters_checking_still_lands(self) -> None:
        """A magazine is decided without a lookup, so there is no window and
        no conditional write to pass."""
        item = make_item(MAGAZINE_POST)
        run_process(item)
        self.assertEqual(item.status, ItemStatus.APPROVED)


class ConcurrentTwinTests(unittest.TestCase):
    """Two posts of the same title, both in flight, both auto-approving.

    The duplicate check only ever looked at items that were already settled --
    approved, downloading or done. Two posts arriving together are in none of
    those, so both used to approve themselves and the same thing downloaded
    twice.
    """

    def test_the_later_of_two_twins_goes_to_review(self) -> None:
        item = make_item(MOVIE_POST)
        run_process(item, content=(matched_tmdb(), full_guide()), twin="Some Film")
        self.assertEqual(item.status, ItemStatus.PENDING_REVIEW)
        self.assertEqual(item.auto_decision, AutoDecision.NEEDS_REVIEW)
        self.assertEqual(item.auto_reasons, [CONCURRENT_DUPLICATE])

    def test_the_earlier_twin_is_unaffected(self) -> None:
        """Exactly one of the two still downloads, or the rule would just move
        the double download into the review queue."""
        item = make_item(MOVIE_POST)
        run_process(item, content=(matched_tmdb(), full_guide()), twin=None)
        self.assertEqual(item.status, ItemStatus.APPROVED)

    def test_a_twin_is_not_reported_as_a_duplicate_to_the_reviewer(self) -> None:
        """The review queue shows what it always showed: a "Duplicate:" message
        means a settled item, not one still being worked out."""
        item = make_item(MOVIE_POST)
        run_process(item, content=(matched_tmdb(), full_guide()), twin="Some Film")
        self.assertIsNone(item.error_message)
        self.assertNotEqual(item.auto_recommendation, Recommendation.REJECT)

    def test_a_settled_duplicate_still_reads_as_a_duplicate(self) -> None:
        item = make_item(MOVIE_POST)
        run_process(
            item, content=(matched_tmdb(), full_guide()), duplicate="DB: Some Film",
        )
        self.assertEqual(item.error_message, "Duplicate: DB: Some Film")
        self.assertEqual(item.auto_recommendation, Recommendation.REJECT)


class TwinLookupTests(unittest.TestCase):
    """Which rows the in-flight twin check actually asks the database for."""

    class _RowSession:
        """Answers one SELECT with the titles it was given."""

        def __init__(self, titles):
            self.titles = titles
            self.statements = []

        async def execute(self, statement):
            self.statements.append(statement)
            return FakeRows([(t,) for t in self.titles])

    def _lookup(self, titles, item):
        session = self._RowSession(titles)
        found = asyncio.run(pipeline._check_in_flight_twin(session, item))
        return found, session.statements[-1] if session.statements else None

    def test_a_post_of_the_same_title_still_in_flight_is_found(self) -> None:
        item = make_item(MOVIE_POST, title="Some Film", media_type=MediaType.MOVIE)
        found, _ = self._lookup(["Some Film"], item)
        self.assertEqual(found, "Some Film")

    def test_an_unrelated_title_is_not_a_twin(self) -> None:
        item = make_item(MOVIE_POST, title="Some Film", media_type=MediaType.MOVIE)
        found, _ = self._lookup(["Another Film Entirely"], item)
        self.assertIsNone(found)

    def test_it_looks_at_the_statuses_that_mean_still_being_worked_out(self) -> None:
        item = make_item(MOVIE_POST, title="Some Film", media_type=MediaType.MOVIE)
        _found, statement = self._lookup(["Some Film"], item)
        sql = str(statement.compile(compile_kwargs={"literal_binds": True}))
        for status in ("CHECKING", "SEARCHING", "AWAITING_DATA"):
            self.assertIn(status, sql)

    def test_only_an_earlier_post_counts(self) -> None:
        """Of two racing posts exactly one gives way, or the rule would just
        move the double download into the review queue."""
        item = make_item(MOVIE_POST, title="Some Film", media_type=MediaType.MOVIE)
        _found, statement = self._lookup(["Some Film"], item)
        sql = str(statement.compile(compile_kwargs={"literal_binds": True}))
        self.assertIn("media_items.id < 1", sql)

    def test_a_magazine_has_no_twins(self) -> None:
        """Every issue shares the base name, so they would all be twins."""
        item = make_item(MAGAZINE_POST, title="Trail and Tide",
                         media_type=MediaType.MAGAZINE)
        found, statement = self._lookup(["Trail and Tide"], item)
        self.assertIsNone(found)
        self.assertIsNone(statement)

    def test_the_settled_duplicate_check_is_a_different_question(self) -> None:
        """It keeps asking about approved, downloading and completed items,
        because that is what the review queue is told about."""
        item = make_item(MOVIE_POST, title="Some Film", media_type=MediaType.MOVIE)
        session = self._RowSession(["Some Film"])
        with mock.patch.object(
            pipeline, "_get_qbt_names", mock.AsyncMock(return_value=[])
        ):
            found = asyncio.run(pipeline._check_duplicate(session, item))
        self.assertEqual(found, "DB: Some Film")
        sql = str(
            session.statements[-1].compile(compile_kwargs={"literal_binds": True})
        )
        self.assertIn("APPROVED", sql)
        self.assertNotIn("CHECKING", sql)
        self.assertIn("media_items.id != 1", sql)


class HandBackClockTests(unittest.TestCase):
    """The 24-hour hand-back needs a real clock to fire."""

    def test_an_item_waiting_too_long_is_handed_back(self) -> None:
        item = make_item(MOVIE_POST)
        item.created_at = NOW - timedelta(hours=25)
        run_process(item, content=(matched_tmdb(), ParentsGuide()))
        self.assertEqual(item.status, ItemStatus.PENDING_REVIEW)
        self.assertEqual(item.auto_decision, AutoDecision.NEEDS_REVIEW)
        self.assertIn("after 24 hours", item.auto_reasons[0])

    def test_an_item_still_inside_the_window_keeps_waiting(self) -> None:
        item = make_item(MOVIE_POST)
        item.created_at = NOW - timedelta(hours=23)
        run_process(item, content=(matched_tmdb(), ParentsGuide()))
        self.assertEqual(item.status, ItemStatus.AWAITING_DATA)
        self.assertEqual(item.auto_decision, AutoDecision.WAIT)


def _throwaway_postgres() -> str | None:
    """A database this test may create and drop things in, or None.

    Same opt-in as the migration tests: set MIGRATION_TEST_DSN. Nothing here
    ever runs against a real deployment -- it builds its own schema and drops
    it again.
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


@unittest.skipUnless(
    _throwaway_postgres(),
    "no throwaway PostgreSQL offered (set MIGRATION_TEST_DSN); "
    "the race is checked against the fake session instead",
)
class ConditionalWriteAgainstPostgresTests(unittest.TestCase):
    """The same race, decided by PostgreSQL rather than by a fake session.

    This is how the defect was found in the first place: a reject accepted
    mid-lookup set the row to rejected, and the run already in flight then set
    it back to approved. The unit tests above show the statement carries the
    right WHERE; this shows the database agrees about what that WHERE does.
    """

    SCHEMA = "checking_race_probe"

    def _connect(self):
        import psycopg

        conn = psycopg.connect(_throwaway_postgres(), autocommit=True)
        with conn.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {self.SCHEMA} CASCADE")
            cur.execute(f"CREATE SCHEMA {self.SCHEMA}")
            cur.execute(f"SET search_path TO {self.SCHEMA}")
            cur.execute(
                "CREATE TABLE media_items (id serial PRIMARY KEY, "
                "status text NOT NULL, auto_decision text)"
            )
        return conn

    def _closing_update(self, cur, item_id: int, status: str) -> int:
        """What `_write_checked_result` emits, in the words PostgreSQL takes."""
        cur.execute(
            "UPDATE media_items SET status = %s, auto_decision = %s "
            "WHERE id = %s AND status = 'CHECKING' RETURNING id",
            (status, "AUTO_APPROVE", item_id),
        )
        return len(cur.fetchall())

    def test_a_reject_landing_mid_lookup_is_not_overwritten(self) -> None:
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(f"SET search_path TO {self.SCHEMA}")
                cur.execute(
                    "INSERT INTO media_items (status) VALUES ('CHECKING') "
                    "RETURNING id"
                )
                item_id = cur.fetchone()[0]

                # The human rejects while TMDB and IMDb are being asked.
                cur.execute(
                    "UPDATE media_items SET status = 'REJECTED' WHERE id = %s",
                    (item_id,),
                )

                # The run already in flight comes back and tries to land.
                matched = self._closing_update(cur, item_id, "APPROVED")
                self.assertEqual(matched, 0, "the update must match no row")

                cur.execute(
                    "SELECT status, auto_decision FROM media_items WHERE id = %s",
                    (item_id,),
                )
                self.assertEqual(cur.fetchone(), ("REJECTED", None))
        finally:
            with conn.cursor() as cur:
                cur.execute(f"DROP SCHEMA {self.SCHEMA} CASCADE")
            conn.close()

    def test_an_untouched_item_still_lands(self) -> None:
        """The positive control: the same statement does write when the row
        really is still CHECKING."""
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(f"SET search_path TO {self.SCHEMA}")
                cur.execute(
                    "INSERT INTO media_items (status) VALUES ('CHECKING') "
                    "RETURNING id"
                )
                item_id = cur.fetchone()[0]
                matched = self._closing_update(cur, item_id, "APPROVED")
                self.assertEqual(matched, 1)
                cur.execute(
                    "SELECT status, auto_decision FROM media_items WHERE id = %s",
                    (item_id,),
                )
                self.assertEqual(cur.fetchone(), ("APPROVED", "AUTO_APPROVE"))
        finally:
            with conn.cursor() as cur:
                cur.execute(f"DROP SCHEMA {self.SCHEMA} CASCADE")
            conn.close()


class QbtNameCacheTests(unittest.TestCase):
    """A batch of posts checks several items at once. Each used to fetch the
    whole qBittorrent list when the cache had aged, three 18 MB answers within
    the same second."""

    def test_concurrent_checks_share_one_refresh(self) -> None:
        calls = 0

        async def _list_all_names() -> list[str]:
            nonlocal calls
            calls += 1
            await asyncio.sleep(0)  # let the other callers reach the cache
            return ["Some Torrent"]

        async def _three_at_once() -> list[list[str]]:
            return await asyncio.gather(*(pipeline._get_qbt_names() for _ in range(3)))

        with mock.patch.object(pipeline, "_qbt_name_cache", []), \
                mock.patch.object(pipeline, "_qbt_cache_ts", 0), \
                mock.patch.object(pipeline, "_qbt_cache_lock", asyncio.Lock()), \
                mock.patch.object(pipeline._qbt, "list_all_names", _list_all_names):
            results = asyncio.run(_three_at_once())

        self.assertEqual(calls, 1)
        self.assertEqual(results, [["Some Torrent"]] * 3)


if __name__ == "__main__":
    unittest.main()
