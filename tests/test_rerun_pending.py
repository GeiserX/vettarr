"""The backlog re-run of reviews the settings made, not a finding.

Turning auto-approval on, leaving shadow mode or opening a type leaves a
review queue full of items decided under the old settings. The re-run puts
back only the reviews whose first stored reason is a setting, and never one a
human has touched, one a rule flagged, or a row decided before reasons were
structured.
"""

import asyncio
import contextlib
import io
import logging
import os
import unittest
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

from sqlalchemy.sql import Select, Update

import policy_fixture
from vettarr import __main__ as cli
from vettarr import events, ingest, pipeline
from vettarr.bot import handlers
from vettarr.checker.content_rules import CONCURRENT_DUPLICATE, IDENTITY_UNCONFIRMED
from vettarr.checker.policy import (
    APPROVE,
    REJECT,
    REVIEW,
    WAIT,
    Decision,
    Reason,
    act,
    is_settings_review,
)
from vettarr.models import AutoDecision, ItemStatus, MediaType


def setUpModule() -> None:
    # The re-run reads the policy's mode; under `off` it does nothing at all.
    patcher = mock.patch.object(pipeline, "settings", policy_fixture.settings(enabled="on"))
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)


def conditions(statement) -> dict:
    """Column name -> (operator, value) for every WHERE condition."""
    found = {}
    for criterion in statement._where_criteria:
        left = getattr(criterion, "left", None)
        right = getattr(criterion, "right", None)
        if left is None:
            continue
        op = getattr(criterion.operator, "__name__", str(criterion.operator))
        found[left.name] = (op, getattr(right, "value", None))
    return found


def set_of(statement: Update) -> dict:
    return {
        column.name: getattr(value, "value", value)
        for column, value in statement._values.items()
    }


class _Rows:
    def __init__(self, rows):
        self._rows = list(rows)

    def all(self):
        return list(self._rows)

    def scalars(self):
        return self


class FakeSession:
    """Answers the candidate SELECT with `rows` and the re-queue UPDATE with
    the ids the database would return: all candidates by default, fewer when a
    human got to some of them first."""

    def __init__(self, rows, *, requeued=None):
        self.rows = rows
        self.requeued = requeued
        self.statements = []
        self.commits = 0

    async def execute(self, statement):
        self.statements.append(statement)
        if isinstance(statement, Select):
            return _Rows(self.rows)
        wanted = conditions(statement)["id"][1]
        returned = wanted if self.requeued is None else self.requeued
        return _Rows(returned)

    async def commit(self):
        self.commits += 1

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def updates(self):
        return [s for s in self.statements if isinstance(s, Update)]

    def selects(self):
        return [s for s in self.statements if isinstance(s, Select)]


def reasons(*pairs) -> list[dict]:
    return [Reason(kind, rule, text).to_json() for kind, rule, text in pairs]


POLICY = {
    1: reasons(("setting", None, "auto-approval is switched off")),
    # Held by its type when it was decided; its type is in mode `rules` now.
    2: reasons(("type", None, "movie is always reviewed by hand")),
    3: reasons(("shadow", None, "shadow mode: would have approved"), ("clear", None, "no rule hit")),
    6: reasons(("setting", None, "auto-approval is switched off"), ("rule", "kw", "keyword: x")),
}
FLAGGED = {
    4: reasons(("rule", "subject-a", "subject: Beekeeping")),
    5: reasons(("duplicate", None, "duplicate of DB: Un titulo")),
    7: reasons(("rule", "adult", "certification: ES:18")),
}
# Decided before reasons were structured: the column is NULL, and the row's
# own words (auto_reasons) say a rule flagged it. Never re-run.
LEGACY = {8: None, 9: None}
LEGACY_WORDS = ["flagged keyword: x"]


def row(item_id, stored, auto_reasons=None, version=None, media_type=MediaType.MOVIE):
    """A candidate row as the re-run selects it: id, reasons, auto_reasons, policy_version, media_type."""
    if stored is None and auto_reasons is None:
        auto_reasons = LEGACY_WORDS
    return (item_id, stored, auto_reasons, version, media_type)


def rows_of(*groups):
    return [row(item_id, stored) for group in groups for item_id, stored in group.items()]


def run_rerun(session, *, decide=APPROVE, **kwargs):
    calls = []

    async def fake_recheck(limit, *, only_ids, decisions, push_held=True):
        calls.append(list(only_ids))
        decisions.extend([decide] * len(only_ids))
        return len(only_ids)

    with (
        mock.patch.object(pipeline, "async_session", lambda: session),
        mock.patch.object(pipeline, "recheck_waiting_items", fake_recheck),
    ):
        summary = asyncio.run(pipeline.rerun_pending_reviews(**kwargs))
    return summary, calls


class NoSecondPushTests(unittest.TestCase):
    def test_a_rerun_tells_the_re_check_not_to_push_again(self) -> None:
        """Every item it re-decides was pushed to the reviewers when it was
        first held; one that is still held must not reach them twice."""
        seen = []

        async def fake_recheck(limit, *, only_ids, decisions, push_held):
            seen.append(push_held)
            decisions.extend([REVIEW] * len(only_ids))
            return len(only_ids)

        session = FakeSession(rows_of(POLICY))
        with (
            mock.patch.object(pipeline, "async_session", lambda: session),
            mock.patch.object(pipeline, "recheck_waiting_items", fake_recheck),
        ):
            summary = asyncio.run(pipeline.rerun_pending_reviews())
        self.assertEqual(summary.still_review, summary.requeued)
        self.assertEqual(seen, [False])


class PolicyReasonTests(unittest.TestCase):
    def test_the_settings_reasons_are_policy(self) -> None:
        for stored in POLICY.values():
            with self.subTest(reasons=stored):
                self.assertTrue(is_settings_review(stored))

    def test_a_finding_is_never_policy(self) -> None:
        for stored in (
            *FLAGGED.values(),
            reasons(("duplicate", None, CONCURRENT_DUPLICATE)),
            reasons(("identity", None, IDENTITY_UNCONFIRMED)),
            reasons(("expired", None, "no data after 24 hours")),
            [],
            None,
        ):
            with self.subTest(reasons=stored):
                self.assertFalse(is_settings_review(stored))

    def test_a_legacy_row_with_a_finding_is_never_rerun(self) -> None:
        """Old rows carry only plain strings; apart from the two that name a
        setting, those words cannot say what sent the item to review."""
        self.assertFalse(is_settings_review(None))
        self.assertFalse(is_settings_review(None, LEGACY_WORDS))
        self.assertFalse(is_settings_review(None, ["could not classify"]))
        # A setting word after a finding is not the first reason.
        self.assertFalse(is_settings_review(None, [*LEGACY_WORDS, "auto-approval is switched off"]))

    def test_a_flag_after_a_setting_word_is_still_read_from_the_first_reason(self) -> None:
        stored = reasons(("rule", "x", "subject: X"), ("setting", None, "auto-approval is switched off"))
        self.assertFalse(is_settings_review(stored))

    def test_the_predicate_matches_what_act_actually_writes(self) -> None:
        """Pinned to act()'s own output, so a changed kind breaks this test
        instead of silently emptying the re-run."""
        clean = Decision(APPROVE, (Reason("clear", None, "no rule hit"),), "v")
        flagged = Decision(REVIEW, (Reason("rule", "r", "subject: X"),), "v")
        rejected = Decision(REJECT, (Reason("rule", "r", "imdb.violence: severe"),), "v")
        waiting = Decision(WAIT, (Reason("missing", "r", "keyword: no data"),), "v")
        for decision, mode, expected in (
            (clean, "off", True), (clean, "shadow", True), (waiting, "off", True),
            (rejected, "shadow", True),
            (flagged, "off", False), (flagged, "shadow", False),
        ):
            with self.subTest(outcome=decision.outcome, mode=mode):
                applied = act(decision, mode)
                self.assertEqual(applied.outcome, REVIEW)
                stored = [r.to_json() for r in applied.reasons]
                self.assertEqual(is_settings_review(stored), expected)


class SelectionTests(unittest.TestCase):
    def test_only_untouched_policy_reviews_are_selected(self) -> None:
        session = FakeSession(rows_of(POLICY, FLAGGED))
        run_rerun(session)
        where = conditions(session.selects()[0])
        self.assertEqual(where["status"][1], ItemStatus.PENDING_REVIEW)
        self.assertEqual(where["auto_decision"][1], AutoDecision.NEEDS_REVIEW)
        self.assertEqual(where["reviewed_at"][0], "is_")

    def test_flagged_and_legacy_items_are_not_requeued(self) -> None:
        session = FakeSession(rows_of(POLICY, FLAGGED, LEGACY))
        summary, calls = run_rerun(session)
        update = session.updates()[0]
        self.assertEqual(sorted(conditions(update)["id"][1]), sorted(POLICY))
        self.assertEqual(sorted(i for batch in calls for i in batch), sorted(POLICY))
        self.assertEqual(summary.requeued, len(POLICY))

    def test_the_requeue_is_one_conditional_update(self) -> None:
        """Conditional on the same predicate, so an item a human decided
        between the SELECT and the UPDATE is not pulled back."""
        session = FakeSession(rows_of(POLICY))
        run_rerun(session)
        self.assertEqual(len(session.updates()), 1)
        update = session.updates()[0]
        where = conditions(update)
        self.assertEqual(where["status"][1], ItemStatus.PENDING_REVIEW)
        self.assertEqual(where["reviewed_at"][0], "is_")
        self.assertEqual(where["auto_decision"][1], AutoDecision.NEEDS_REVIEW)
        self.assertEqual(set_of(update), {"status": ItemStatus.AWAITING_DATA})
        self.assertEqual(session.commits, 1)

    def test_only_what_the_update_returned_is_rechecked(self) -> None:
        session = FakeSession(rows_of(POLICY), requeued=[1, 3])
        summary, calls = run_rerun(session)
        self.assertEqual(calls, [[1, 3]])
        self.assertEqual(summary.requeued, 2)

    def test_nothing_to_rerun_touches_nothing(self) -> None:
        session = FakeSession(rows_of(FLAGGED, LEGACY))
        summary, calls = run_rerun(session)
        self.assertEqual(session.updates(), [])
        self.assertEqual(calls, [])
        self.assertEqual(summary.requeued, 0)
        self.assertIn("nothing", summary.text())


class LoopTests(unittest.TestCase):
    def _many(self, count):
        return [row(i, POLICY[1]) for i in range(1, count + 1)]

    def test_every_requeued_item_is_rechecked_once(self) -> None:
        session = FakeSession(self._many(45))
        summary, calls = run_rerun(session)
        self.assertEqual([len(c) for c in calls], [20, 20, 5])
        seen = [i for batch in calls for i in batch]
        self.assertEqual(sorted(seen), list(range(1, 46)))
        self.assertEqual(summary.redecided, 45)

    def test_the_loop_is_bounded(self) -> None:
        session = FakeSession(self._many(45))
        summary, calls = run_rerun(session, max_loops=2)
        self.assertEqual(len(calls), 2)
        self.assertEqual(summary.requeued, 45)
        self.assertEqual(summary.redecided, 40)
        self.assertIn("Not re-decided here: 5", summary.text())

    def test_the_summary_counts_each_outcome(self) -> None:
        session = FakeSession(rows_of(POLICY))
        outcomes = iter([APPROVE, REVIEW, WAIT, APPROVE])

        async def fake_recheck(limit, *, only_ids, decisions, push_held=True):
            for _ in only_ids:
                decisions.append(next(outcomes))
            return len(only_ids)

        with (
            mock.patch.object(pipeline, "async_session", lambda: session),
            mock.patch.object(pipeline, "recheck_waiting_items", fake_recheck),
        ):
            summary = asyncio.run(pipeline.rerun_pending_reviews())
        self.assertEqual(
            summary,
            pipeline.RerunSummary(
                requeued=4, redecided=4, auto_approved=2, still_review=1, waiting=1
            ),
        )

    def test_an_item_decided_by_hand_mid_run_is_not_counted(self) -> None:
        """The re-check's claim fails for it, so it writes no decision."""
        session = FakeSession(rows_of(POLICY))

        async def fake_recheck(limit, *, only_ids, decisions, push_held=True):
            decisions.append(APPROVE)
            return 1

        with (
            mock.patch.object(pipeline, "async_session", lambda: session),
            mock.patch.object(pipeline, "recheck_waiting_items", fake_recheck),
        ):
            summary = asyncio.run(pipeline.rerun_pending_reviews())
        self.assertEqual(summary.redecided, 1)
        self.assertIn("Not re-decided here: 3", summary.text())


class EntryPointTests(unittest.TestCase):
    def test_no_arguments_still_starts_the_service(self) -> None:
        # _run answers with the components that failed: none is a clean stop.
        run, rerun = mock.AsyncMock(return_value=[]), mock.AsyncMock()
        with mock.patch.object(cli, "_run", run), mock.patch.object(cli, "_rerun", rerun):
            cli.main([])
        run.assert_awaited_once()
        rerun.assert_not_awaited()

    def test_rerun_runs_the_rerun_and_prints_it(self) -> None:
        summary = pipeline.RerunSummary(requeued=2, redecided=2, auto_approved=2)
        rerun = mock.AsyncMock(return_value=summary)
        run = mock.AsyncMock()
        out = io.StringIO()
        with (
            mock.patch.object(cli, "_run", run),
            mock.patch.object(pipeline, "rerun_pending_reviews", rerun),
            mock.patch("vettarr.database.engine", mock.MagicMock(dispose=mock.AsyncMock())),
            mock.patch.object(events, "flush_all", mock.AsyncMock(return_value=0), create=True) as flush,
            contextlib.redirect_stdout(out),
        ):
            cli.main(["rerun"])
        rerun.assert_awaited_once()
        run.assert_not_awaited()
        self.assertIn("Auto-approved: 2", out.getvalue())
        # No scheduler runs in this command: it writes its own events before it exits.
        flush.assert_awaited_once()


class BotCommandTests(unittest.TestCase):
    OWNER = handlers.settings.telegram_owner_id

    def _update(self, user_id):
        message = SimpleNamespace(reply_text=mock.AsyncMock())
        return SimpleNamespace(
            effective_user=SimpleNamespace(id=user_id), message=message
        )

    def test_the_owner_gets_the_summary(self) -> None:
        summary = pipeline.RerunSummary(requeued=1, redecided=1, still_review=1)
        rerun = mock.AsyncMock(return_value=summary)
        update = self._update(self.OWNER)
        with (
            mock.patch.object(pipeline, "rerun_pending_reviews", rerun),
        ):
            asyncio.run(handlers.cmd_rerun(update, None))
        rerun.assert_awaited_once()
        update.message.reply_text.assert_awaited_with(summary.text())

    def test_anyone_else_is_ignored(self) -> None:
        rerun = mock.AsyncMock()
        update = self._update(self.OWNER + 1)
        with (
            mock.patch.object(pipeline, "rerun_pending_reviews", rerun),
        ):
            asyncio.run(handlers.cmd_rerun(update, None))
        rerun.assert_not_awaited()
        update.message.reply_text.assert_not_awaited()

    def test_the_command_is_registered(self) -> None:
        app = handlers.create_bot_app(mock.MagicMock())
        self.addCleanup(ingest.set_review_pusher, None)
        commands = {
            command
            for group in app.handlers.values()
            for handler in group
            for command in getattr(handler, "commands", ())
        }
        self.assertIn("rerun", commands)


if __name__ == "__main__":
    unittest.main()
