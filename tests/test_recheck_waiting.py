"""The hourly re-check of items parked without content data.

Two things go wrong when a worker assumes the row it read is still the row it
is writing to. Between claiming an item and deciding it, the worker spends
real time on TMDB and IMDb, and in that window the operator can reject the item from
the web UI or the bot. The old code re-read the status once, before the
lookups, and then wrote the decision over whatever was there afterwards -- so
a rejection could be turned into an automatic approval.
"""

import asyncio
import logging
import os
import unittest
from datetime import datetime, timedelta, timezone
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

from sqlalchemy.sql import Select, Update

from vettarr import pipeline, review, search_jobs
from vettarr.checker.imdb_client import ParentsGuide
from vettarr.checker.tmdb_client import TMDBResult
from vettarr.models import (
    OP_RECHECK,
    AutoDecision,
    ContentCheck,
    ItemStatus,
    MediaItem,
    MediaType,
    Recommendation,
    REJECTABLE_STATUSES,
)

NOW = datetime(2026, 9, 21, 10, 0, tzinfo=timezone.utc)
ITEM_ID = 11


def where_of(statement) -> dict:
    """Column name -> value, for the equality conditions of a statement."""
    pairs = {}
    for criterion in statement._where_criteria:
        left = getattr(criterion, "left", None)
        right = getattr(criterion, "right", None)
        if left is not None and hasattr(right, "value"):
            pairs[left.name] = right.value
    return pairs


def set_of(statement: Update) -> dict:
    return {
        column.name: getattr(value, "value", value)
        for column, value in statement._values.items()
    }


def make_item(**kwargs) -> MediaItem:
    item = MediaItem(
        id=ITEM_ID,
        telegram_message_id=4242,
        telegram_chat_id=-1,
        raw_text="Alguna Pelicula (2024) 1080p WEB-DL-GRUPO",
        title="Alguna Pelicula",
        year=2024,
        media_type=MediaType.MOVIE,
        status=ItemStatus.CHECKING,
        auto_recommendation=Recommendation.NEUTRAL,
    )
    item.created_at = NOW - timedelta(hours=1)
    for key, value in kwargs.items():
        setattr(item, key, value)
    return item


class ScriptedResult:
    def __init__(self, value, rows=()):
        self._value = value
        self._rows = rows

    def scalar_one_or_none(self):
        return self._value

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)

    def first(self):
        return self._value

    @property
    def rowcount(self):
        return len(self._rows)


class ScriptedSession:
    """Records every statement and answers UPDATE ... RETURNING from a script.

    `claims` and `writes` are the answers the database gives to the two
    conditional updates: an id when the update matched a row, None when it did
    not because somebody else got there first.
    """

    def __init__(self, item, *, ids=(ITEM_ID,), claim=ITEM_ID, write=ITEM_ID):
        self.item = item
        self.ids = ids
        self.claim = claim
        self.write = write
        self.statements = []
        self.commits = 0
        self.rollbacks = 0

    async def get(self, _model, pk, **_kwargs):
        return self.item if self.item is not None and self.item.id == pk else None

    async def execute(self, statement):
        self.statements.append(statement)
        if isinstance(statement, Select):
            return ScriptedResult(None, rows=self.ids)
        values = set_of(statement)
        if values.get("status") is ItemStatus.CHECKING:
            return ScriptedResult(self.claim)
        return ScriptedResult(self.write)

    def add(self, _obj):
        pass

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    # -- helpers for the assertions ----------------------------------------
    def updates(self):
        return [s for s in self.statements if isinstance(s, Update)]

    def selects(self):
        return [s for s in self.statements if isinstance(s, Select)]


def run_recheck(session, *, content=None, duplicate=None, raises=None, **kwargs):
    check = mock.AsyncMock(return_value=content or (TMDBResult(), ParentsGuide()))
    if raises is not None:
        check.side_effect = raises
    with (
        mock.patch.object(pipeline, "async_session", lambda: session),
        mock.patch.object(pipeline, "_now", lambda: NOW),
        mock.patch.object(pipeline, "_run_content_check", check),
        mock.patch.object(
            pipeline, "_check_duplicate", mock.AsyncMock(return_value=duplicate)
        ),
        mock.patch.object(pipeline, "_lookup_book", mock.AsyncMock(return_value=None)),
    ):
        return asyncio.run(pipeline.recheck_waiting_items(**kwargs))


class SelectionTests(unittest.TestCase):
    def test_only_items_that_are_waiting_are_picked_up(self) -> None:
        session = ScriptedSession(make_item())
        run_recheck(session)
        picked = where_of(session.selects()[0])
        self.assertEqual(picked.get("status"), ItemStatus.AWAITING_DATA)

    def test_items_in_the_review_queue_are_not_picked_up(self) -> None:
        session = ScriptedSession(make_item())
        run_recheck(session)
        picked = where_of(session.selects()[0])
        self.assertNotEqual(picked.get("status"), ItemStatus.PENDING_REVIEW)


class BacklogRerunHookTests(unittest.TestCase):
    """What the backlog re-run needs from the re-check, and nothing more."""

    def test_only_ids_narrows_the_pick_to_those_items(self) -> None:
        session = ScriptedSession(make_item())
        run_recheck(session, only_ids=[ITEM_ID, 12])
        picked = where_of(session.selects()[0])
        self.assertEqual(picked.get("status"), ItemStatus.AWAITING_DATA)
        self.assertEqual(picked.get("id"), [ITEM_ID, 12])

    def test_without_only_ids_the_whole_queue_is_eligible(self) -> None:
        session = ScriptedSession(make_item())
        run_recheck(session)
        self.assertNotIn("id", where_of(session.selects()[0]))

    def test_decisions_collects_what_was_written(self) -> None:
        session = ScriptedSession(make_item())
        decisions = []
        run_recheck(session, decisions=decisions)
        self.assertEqual(len(decisions), 1)

    def test_a_write_that_lost_to_a_human_is_not_collected(self) -> None:
        session = ScriptedSession(make_item(), write=None)
        decisions = []
        run_recheck(session, decisions=decisions)
        self.assertEqual(decisions, [])


class ClaimTests(unittest.TestCase):
    def test_the_claim_only_takes_a_waiting_item(self) -> None:
        session = ScriptedSession(make_item())
        run_recheck(session)
        claim = session.updates()[0]
        self.assertEqual(where_of(claim).get("status"), ItemStatus.AWAITING_DATA)
        self.assertEqual(where_of(claim).get("id"), ITEM_ID)

    def test_the_claim_marks_the_item_as_this_worker_s(self) -> None:
        session = ScriptedSession(make_item())
        run_recheck(session)
        values = set_of(session.updates()[0])
        self.assertEqual(values.get("status"), ItemStatus.CHECKING)
        self.assertEqual(values.get("error_message"), OP_RECHECK)

    def test_an_item_someone_else_claimed_is_left_alone(self) -> None:
        """The claim matched nothing, so nothing else may be written."""
        session = ScriptedSession(make_item(), claim=None)
        self.assertEqual(run_recheck(session), 0)
        self.assertEqual(len(session.updates()), 1, "only the failed claim")


class ConditionalWriteTests(unittest.TestCase):
    def test_the_decision_is_written_only_while_the_claim_still_holds(self) -> None:
        session = ScriptedSession(make_item())
        run_recheck(session)
        write = session.updates()[-1]
        conditions = where_of(write)
        self.assertEqual(conditions.get("status"), ItemStatus.CHECKING)
        self.assertEqual(conditions.get("error_message"), OP_RECHECK)
        self.assertEqual(conditions.get("id"), ITEM_ID)

    def test_a_decision_taken_mid_lookup_is_not_overwritten(self) -> None:
        """The operator rejected the item while TMDB was being asked. The write
        matches no row, and the re-check reports nothing done."""
        session = ScriptedSession(make_item(), write=None)
        self.assertEqual(run_recheck(session), 0)
        self.assertEqual(session.rollbacks, 1)

    def test_a_clean_item_is_approved(self) -> None:
        session = ScriptedSession(make_item())
        clean = (TMDBResult(tmdb_id=1, imdb_id="tt1"), ParentsGuide(fetched=True))
        from vettarr.checker.policy import APPROVE, Applied, Decision, Reason

        reasons = (Reason("clear", None, "clean"),)
        approved = pipeline.Verdict(Decision(APPROVE, reasons, "v1"), Applied(APPROVE, reasons), {})
        with mock.patch.object(pipeline, "_decide", mock.AsyncMock(return_value=approved)):
            count = run_recheck(session, content=clean)
        self.assertEqual(count, 1)
        values = set_of(session.updates()[-1])
        self.assertEqual(values.get("status"), ItemStatus.APPROVED)
        self.assertIsNone(values.get("error_message"))


class HeldForReviewTests(unittest.TestCase):
    """A re-check that holds the item for a person pushes it to the reviewers."""

    def _recheck(self, outcome, **kwargs):
        from vettarr.checker.policy import Applied, Decision, Reason

        session = ScriptedSession(make_item())
        reasons = (Reason("clear", None, "why"),)
        verdict = pipeline.Verdict(Decision(outcome, reasons, "v1"), Applied(outcome, reasons), {})
        held = mock.AsyncMock()
        with (
            mock.patch.object(pipeline, "_decide", mock.AsyncMock(return_value=verdict)),
            mock.patch.object(pipeline, "held_for_review", held),
        ):
            run_recheck(session, **kwargs)
        return held

    def test_the_backlog_re_run_does_not_push_a_held_item_again(self) -> None:
        from vettarr.checker.policy import REVIEW

        self._recheck(REVIEW, push_held=False).assert_not_awaited()

    def test_a_held_item_is_pushed(self) -> None:
        from vettarr.checker.policy import REVIEW

        self._recheck(REVIEW).assert_awaited_once_with(ITEM_ID)

    def test_an_approved_item_is_not_pushed(self) -> None:
        from vettarr.checker.policy import APPROVE

        self._recheck(APPROVE).assert_not_awaited()


class DuplicateTests(unittest.TestCase):
    def test_a_title_approved_in_the_meantime_is_not_downloaded_twice(self) -> None:
        """The duplicate check runs again at re-check time. An item that was
        unique an hour ago may not be now."""
        session = ScriptedSession(make_item())
        count = run_recheck(session, duplicate="DB: Alguna Pelicula")
        self.assertEqual(count, 1)
        values = set_of(session.updates()[-1])
        self.assertEqual(values.get("status"), ItemStatus.PENDING_REVIEW)
        self.assertEqual(values.get("error_message"), "DB: Alguna Pelicula")
        self.assertEqual(values.get("auto_recommendation"), Recommendation.REJECT)


class FailureTests(unittest.TestCase):
    def test_a_failed_lookup_puts_the_item_back_in_the_queue(self) -> None:
        session = ScriptedSession(make_item())
        self.assertEqual(run_recheck(session, raises=RuntimeError("tmdb down")), 0)
        release = session.updates()[-1]
        self.assertEqual(set_of(release).get("status"), ItemStatus.AWAITING_DATA)
        self.assertEqual(where_of(release).get("status"), ItemStatus.CHECKING)
        self.assertEqual(where_of(release).get("error_message"), OP_RECHECK)


class KeepMatchTests(unittest.TestCase):
    """A person's re-run: an empty lookup must not replace a stored match or decide the item."""

    def _check(self, existing, found, **kwargs):
        session = mock.Mock(
            execute=mock.AsyncMock(return_value=ScriptedResult(existing)),
            commit=mock.AsyncMock(), add=mock.Mock(),
        )
        with (
            mock.patch.object(pipeline.tmdb_client, "search", mock.AsyncMock(return_value=found)),
            mock.patch.object(pipeline, "fetch_parents_guide", mock.AsyncMock(return_value=ParentsGuide())),
        ):
            result = asyncio.run(pipeline._run_content_check(session, make_item(), **kwargs))
        return result, session

    def _stored(self) -> ContentCheck:
        return ContentCheck(
            media_item_id=ITEM_ID, tmdb_id=603, imdb_id="tt0133093", poster_url="https://image/p.jpg",
            plot="A plot.", guide_votes={"VIOLENCE": 40},
        )

    def test_an_empty_lookup_leaves_the_stored_check_as_it_is(self) -> None:
        stored = self._stored()
        with self.assertRaises(pipeline.LookupMissed):
            self._check(stored, TMDBResult(), keep_match=True)
        self.assertEqual(
            (stored.tmdb_id, stored.imdb_id, stored.poster_url, stored.plot, stored.guide_votes),
            (603, "tt0133093", "https://image/p.jpg", "A plot.", {"VIOLENCE": 40}),
        )

    def test_a_lookup_that_answers_is_stored_as_ever(self) -> None:
        stored = self._stored()
        self._check(stored, TMDBResult(tmdb_id=604, title="Another"), keep_match=True)
        self.assertEqual((stored.tmdb_id, stored.tmdb_title), (604, "Another"))

    def test_an_item_that_never_had_a_match_is_checked_as_ever(self) -> None:
        empty = ContentCheck(media_item_id=ITEM_ID)
        (found, _guide), session = self._check(empty, TMDBResult(), keep_match=True)
        self.assertIsNone(found.tmdb_id)
        session.commit.assert_awaited()

    def test_the_hourly_re_check_does_not_ask_for_it(self) -> None:
        stored = self._stored()
        self._check(stored, TMDBResult())
        self.assertIsNone(stored.tmdb_id)

    def test_the_re_check_writes_nothing_and_names_the_item(self) -> None:
        session = ScriptedSession(make_item())
        decisions, missed = [], []
        count = run_recheck(
            session, raises=pipeline.LookupMissed(ITEM_ID), decisions=decisions,
            keep_match=True, missed=missed,
        )
        self.assertEqual((count, decisions, missed), (0, [], [ITEM_ID]))
        # The claim and its release, and no decision between them.
        claim, release = session.updates()
        self.assertEqual(set_of(claim).get("status"), ItemStatus.CHECKING)
        self.assertEqual(set_of(release), {"status": ItemStatus.AWAITING_DATA, "error_message": None})

    def test_the_re_check_passes_the_wish_on(self) -> None:
        for wished in (True, False):
            check = mock.AsyncMock(return_value=(TMDBResult(), ParentsGuide()))
            session = ScriptedSession(make_item())
            with (
                mock.patch.object(pipeline, "async_session", lambda: session),
                mock.patch.object(pipeline, "_now", lambda: NOW),
                mock.patch.object(pipeline, "_run_content_check", check),
                mock.patch.object(pipeline, "_check_duplicate", mock.AsyncMock(return_value=None)),
                mock.patch.object(pipeline, "_lookup_book", mock.AsyncMock(return_value=None)),
            ):
                asyncio.run(pipeline.recheck_waiting_items(keep_match=wished))
            self.assertEqual(check.await_args.kwargs, {"keep_match": wished})


class RestartTests(unittest.TestCase):
    def test_a_restart_puts_stranded_claims_back(self) -> None:
        session = ScriptedSession(None, ids=(ITEM_ID,))
        with mock.patch.object(pipeline, "async_session", lambda: session):
            asyncio.run(pipeline.recover_stranded_rechecks())
        recovery = session.updates()[0]
        self.assertEqual(where_of(recovery).get("status"), ItemStatus.CHECKING)
        self.assertEqual(where_of(recovery).get("error_message"), OP_RECHECK)
        self.assertEqual(set_of(recovery).get("status"), ItemStatus.AWAITING_DATA)

    def test_the_search_worker_recovers_them_at_start_up(self) -> None:
        """The recovery has to happen somewhere that actually runs on boot."""
        session = ScriptedSession(None, ids=())
        with mock.patch.object(search_jobs, "async_session", lambda: session):
            asyncio.run(search_jobs._recover_interrupted_jobs())
        recheck_recoveries = [
            s for s in session.updates()
            if where_of(s).get("error_message") == OP_RECHECK
            and where_of(s).get("status") is ItemStatus.CHECKING
            and set_of(s).get("status") is ItemStatus.AWAITING_DATA
        ]
        self.assertEqual(len(recheck_recoveries), 1)

    def test_recovery_does_not_sweep_every_checking_row(self) -> None:
        """An item the ingestion pipeline is checking is not this worker's."""
        session = ScriptedSession(None, ids=())
        with mock.patch.object(pipeline, "async_session", lambda: session):
            asyncio.run(pipeline.recover_stranded_rechecks())
        self.assertIn("error_message", where_of(session.updates()[0]))


class RejectWhileCheckingTests(unittest.TestCase):
    def test_an_item_being_checked_can_still_be_rejected(self) -> None:
        """Otherwise the answer to "stop this one" is "wait for two API calls
        you cannot see"."""
        self.assertIn(ItemStatus.CHECKING, REJECTABLE_STATUSES)

    def test_rejecting_a_checking_item_takes(self) -> None:
        item = make_item(status=ItemStatus.CHECKING)

        class _Session(ScriptedSession):
            async def execute(self, statement):
                self.statements.append(statement)
                return ScriptedResult(None)

        session = _Session(item)
        with mock.patch.object(review, "async_session", lambda: session):
            outcome = asyncio.run(review.reject_item(item.id))
        self.assertTrue(outcome.rejected)
        self.assertEqual(item.status, ItemStatus.REJECTED)


if __name__ == "__main__":
    unittest.main()
