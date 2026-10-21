"""Stats and Activity: the sums made from the events table, the two pages and the nav's count.

The sums are functions of plain rows, so they are fed rows here. The pages
are driven through the test client with a session that answers their queries
in the order they are asked.
"""

import asyncio
import json
import os
import re
import time
import unittest
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

from sqlalchemy.dialects import postgresql

from test_web_foundation import WEB_DIR, WebCase

from vettarr import events
from vettarr.checker.policy import APPROVE, AUTO_APPROVE_OFF, Decision, Reason, act
from vettarr.models import AutoDecision, ItemStatus
from vettarr.web import shell, ui
from vettarr.web.routes import stats

NOON = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)
TODAY = stats._local(NOON).date()


def event(kind, at=NOON, item_id=None, **payload):
    return SimpleNamespace(id=1, at=at, kind=kind, item_id=item_id, payload=payload)


def decision(done, days_ago=0.0, item_id=None, media_type="movie", reasons=(), **payload):
    return event(
        events.DECISION, NOON - timedelta(days=days_ago), item_id,
        done=done, outcome=payload.pop("outcome", done), media_type=media_type,
        reasons=list(reasons), title="x", **payload,
    )


def sql(statement) -> str:
    """The statement as Postgres gets it, values written in."""
    text = str(statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
    return text.replace("%%", "%")  # the driver's own escaping of a percent sign


# ---------------------------------------------------------------------------
# Decisions
# ---------------------------------------------------------------------------


class DecisionRowTests(unittest.TestCase):
    def test_a_row_reads_what_was_done_and_falls_back_to_the_outcome(self) -> None:
        old = event(events.DECISION, item_id=1, outcome="approve", media_type="movie", reasons=["a"])
        new = decision("review", item_id=2, outcome="approve", reasons=["shadow mode: would have approved"])
        self.assertEqual([d.done for d in stats.decided_from([old, new])], ["approve", "review"])

    def test_an_outcome_nobody_knows_and_a_row_without_a_payload_are_left_out(self) -> None:
        rows = [decision("teleport"), SimpleNamespace(at=NOON, item_id=1, payload=None), decision("reject")]
        self.assertEqual([d.done for d in stats.decided_from(rows)], ["reject"])

    def test_rows_come_out_oldest_first(self) -> None:
        rows = [decision("approve", 0), decision("reject", 2), decision("wait", 1)]
        self.assertEqual([d.done for d in stats.decided_from(rows)], ["reject", "wait", "approve"])

    def test_an_hourly_wait_counts_once_and_the_approval_after_it_counts_too(self) -> None:
        rows = [decision("wait", 1 - n / 24, item_id=5) for n in range(6)] + [decision("approve", 0, item_id=5)]
        self.assertEqual([d.done for d in stats.changes(stats.decided_from(rows))], ["wait", "approve"])

    def test_two_items_decided_alike_are_two_decisions(self) -> None:
        rows = [decision("wait", 1, item_id=5), decision("wait", 1, item_id=6), decision("wait", 0.5)]
        self.assertEqual(len(stats.changes(stats.decided_from(rows))), 3)


class ChartTests(unittest.TestCase):
    def chart(self, *rows):
        return stats.chart_of(stats.decided_from(rows), TODAY)

    def test_thirty_days_end_today_and_an_older_decision_is_not_drawn(self) -> None:
        chart = self.chart(decision("approve", 0, 1), decision("approve", 29, 2), decision("approve", 31, 3))
        self.assertEqual(len(chart.days), 30)
        self.assertEqual(chart.days[-1].date, TODAY)
        self.assertEqual(chart.days[0].date, TODAY - timedelta(days=29))
        self.assertEqual(chart.total, 2)
        self.assertEqual((chart.days[0].total, chart.days[-1].total), (1, 1))

    def test_a_bar_stacks_the_outcomes_in_one_order_from_the_baseline(self) -> None:
        chart = self.chart(
            decision("reject", 0, 1), decision("approve", 0, 2), decision("approve", 0, 3),
            decision("review", 0, 4), decision("wait", 0, 5),
        )
        bar = chart.days[-1]
        self.assertEqual([s.outcome for s in bar.segments], ["approve", "review", "wait", "reject"])
        self.assertEqual([s.count for s in bar.segments], [2, 1, 1, 1])
        # Each segment sits on the one before it, and the stack is the day's total high.
        unit = chart.HEIGHT / chart.top
        feet = [s.y + s.count * unit for s in bar.segments]
        self.assertAlmostEqual(feet[0], chart.HEIGHT, places=1)
        for lower, upper_foot in zip(bar.segments, feet[1:]):
            self.assertAlmostEqual(lower.y, upper_foot, places=1)
        self.assertAlmostEqual(chart.HEIGHT - bar.segments[-1].y, bar.total * unit, places=1)
        # A segment is never taller than its count, so the gap between two is the card showing through.
        self.assertTrue(all(0 < s.height <= s.count * unit + 0.01 for s in bar.segments))
        self.assertLess(bar.segments[1].height, bar.segments[1].count * unit)

    def test_every_segment_says_its_day_count_and_outcome_in_words(self) -> None:
        bar = self.chart(decision("review", 0, 1), decision("review", 0, 2)).days[-1]
        self.assertEqual(bar.segments[0].title, f"{TODAY:%a} {TODAY.day} {TODAY:%b}: 2 held for you")

    def test_the_axis_top_is_a_round_even_number_at_or_above_the_tallest_bar(self) -> None:
        for tallest, top in ((0, 2), (1, 2), (3, 4), (5, 6), (9, 10), (10, 10), (11, 20), (37, 50), (120, 200)):
            with self.subTest(tallest=tallest):
                self.assertEqual(stats.nice_top(tallest), top)
                self.assertEqual(top % 2, 0)
        chart = self.chart(*(decision("approve", 0, n) for n in range(9)))
        self.assertEqual((chart.top, chart.ticks), (10, (10, 5, 0)))

    def test_the_summary_says_the_chart_in_a_sentence(self) -> None:
        chart = self.chart(decision("approve", 0, 1), decision("review", 0, 2), decision("review", 1, 3))
        self.assertEqual(
            chart.summary,
            "Decisions per day, last 30 days: 1 approved, 2 held for you. "
            f"The busiest day was {TODAY.day} {TODAY:%b} with 2.",
        )
        self.assertEqual(self.chart().summary, "Decisions per day, last 30 days: none.")

    def test_a_day_is_the_server_s_day_not_utc_s(self) -> None:
        at = datetime(2026, 10, 12, 23, 30, tzinfo=timezone.utc)
        # A zone east of UTC, set here: on a runner in UTC the two days are the same and this could not fail.
        with mock.patch.dict(os.environ, {"TZ": "Europe/Madrid"}):
            time.tzset()
            try:
                local = at.astimezone().date()
                self.assertEqual(local, date(2026, 10, 13))
                chart = stats.chart_of(stats.decided_from([event(events.DECISION, at, 1, done="approve")]), local)
            finally:
                os.environ.pop("TZ", None)
        time.tzset()
        self.assertEqual(chart.days[-1].total, 1)

    def test_the_hourly_re_check_is_one_decision_in_the_bars_and_the_tiles(self) -> None:
        """Six hourly waits of one item and its approval: two decisions, not seven."""
        chart = self.chart(*(decision("wait", 1 - n / 24, 5) for n in range(6)), decision("approve", 0, 5))
        self.assertEqual(chart.total, 2)
        self.assertEqual(chart.totals, {"approve": 1, "review": 0, "wait": 1, "reject": 0})


class ReasonTests(unittest.TestCase):
    def test_the_reasons_of_what_was_not_approved_most_common_first(self) -> None:
        rows = [
            decision("review", 3, 1, reasons=["imdb.violence: severe"]),
            decision("review", 2, 2, reasons=["imdb.violence: severe", "imdb.nudity: moderate"]),
            decision("reject", 1, 3, reasons=["keyword: list-a-word"]),
            decision("approve", 1, 4, reasons=["no rule hit (5 checked)"]),
        ]
        found = stats.top_reasons(stats.decided_from(rows))
        self.assertEqual([(r.label, r.count) for r in found], [
            ("Violence (IMDb): severe", 2), ("Nudity (IMDb): moderate", 1), ("Keyword: list-a-word", 1),
        ])
        self.assertEqual([r.percent for r in found], [100.0, 50.0, 50.0])

    def test_an_item_counts_by_its_latest_decision(self) -> None:
        rows = [
            decision("wait", 2, 1, reasons=["imdb.violence: parents guide not fetched"]),
            decision("approve", 1, 1, reasons=["no rule hit (5 checked)"]),
        ]
        self.assertEqual(stats.top_reasons(stats.decided_from(rows)), [])

    def test_a_review_the_settings_made_counts_by_its_lead_only(self) -> None:
        """What the policy found is listed after the lead, and is why it would have approved."""
        found = Decision(APPROVE, (Reason("identity", None, "same title and year"), Reason("rule", None, "no rule hit")))
        for mode, lead in (("shadow", "shadow mode: would have approved"), ("off", AUTO_APPROVE_OFF)):
            with self.subTest(mode=mode):
                applied = act(found, mode)
                self.assertEqual(applied.reasons[0].text, lead)
                row = decision("review", 0, 1, reasons=[r.text for r in applied.reasons])
                self.assertEqual(
                    [(r.label, r.count) for r in stats.top_reasons(stats.decided_from([row]))], [(lead, 1)],
                )

    def test_the_scope_a_decision_was_made_under_is_not_why_it_was_held(self) -> None:
        rows = [
            decision("review", 2, 1, reasons=["imdb.violence: severe", "decided under the scope kids"]),
            decision("wait", 1, 2, reasons=["imdb.nudity: no data", "decided under the scope kids"]),
        ]
        found = stats.top_reasons(stats.decided_from(rows))
        self.assertEqual([r.label for r in found], ["Violence (IMDb): severe", "Nudity (IMDb): no data"])

    def test_only_the_top_eight_are_listed(self) -> None:
        rows = [decision("review", 0, n, reasons=[f"reason {n}"]) for n in range(12)]
        self.assertEqual(len(stats.top_reasons(stats.decided_from(rows))), stats.TOP_REASONS)


class PerTypeTests(unittest.TestCase):
    def test_each_type_is_split_by_its_items_latest_decisions(self) -> None:
        rows = [
            decision("wait", 3, 1), decision("approve", 2, 1),
            decision("review", 2, 2), decision("reject", 1, 3),
            decision("review", 1, 4, media_type="ebook"),
        ]
        movie, ebook = stats.per_type(stats.decided_from(rows))
        self.assertEqual((movie.media_type, movie.total), ("movie", 3))
        self.assertEqual([(p.label, p.count) for p in movie.parts], [("approve", 1), ("review", 1), ("reject", 1)])
        self.assertEqual(movie.words, "1 approved · 1 held for you · 1 rejected")
        self.assertAlmostEqual(sum(p.percent for p in movie.parts), 100.0, delta=0.2)
        self.assertEqual((ebook.media_type, ebook.words), ("ebook", "1 held for you"))


# ---------------------------------------------------------------------------
# Shadow agreement
# ---------------------------------------------------------------------------


def shadow_row(item_id, would, status, *, reviewed=True, auto=AutoDecision.NEEDS_REVIEW):
    return SimpleNamespace(
        item_id=item_id, payload={"outcome": would, "done": "review"}, status=status,
        reviewed_at=NOON if reviewed else None, auto_decision=auto,
    )


class ShadowAgreementTests(unittest.TestCase):
    def test_would_have_approved_fourteen_you_rejected_two(self) -> None:
        rows = [shadow_row(n, "approve", ItemStatus.COMPLETED) for n in range(12)]
        rows += [shadow_row(n, "approve", ItemStatus.REJECTED) for n in (12, 13)]
        agreement = stats.agreement_of(rows)
        self.assertEqual([r.sentence for r in agreement.rows], ["Would have approved 14: you approved 12, rejected 2."])
        self.assertEqual((agreement.agreed, agreement.decided, agreement.percent), (12, 14, 86))

    def test_a_rejection_you_agreed_with_and_what_still_waits(self) -> None:
        rows = [
            shadow_row(1, "reject", ItemStatus.REJECTED),
            shadow_row(2, "reject", ItemStatus.DOWNLOADING),
            shadow_row(3, "approve", ItemStatus.PENDING_REVIEW, reviewed=False),
            shadow_row(4, "wait", ItemStatus.APPROVED),
        ]
        agreement = stats.agreement_of(rows)
        self.assertEqual([r.sentence for r in agreement.rows], [
            "Would have approved 1: you approved 0, rejected 0, 1 still waits for you.",
            "Would have rejected 2: you approved 1, rejected 1.",
            "Would have waited for data on 1: you approved 1, rejected 0.",
        ])
        # Waiting for data is nothing a person can agree with.
        self.assertEqual((agreement.agreed, agreement.decided, agreement.percent), (1, 2, 50))

    def test_an_item_the_policy_approved_later_is_nobody_s_decision(self) -> None:
        rows = [shadow_row(1, "approve", ItemStatus.COMPLETED, auto=AutoDecision.AUTO_APPROVE)]
        agreement = stats.agreement_of(rows)
        self.assertEqual((agreement.rows[0].approved, agreement.rows[0].undecided), (0, 1))
        self.assertIsNone(agreement.percent)

    def test_a_rejection_no_person_made_is_nobody_s_decision(self) -> None:
        """REJECTED with no review: the policy rejected it later, so no person agreed or disagreed."""
        agreement = stats.agreement_of([shadow_row(1, "reject", ItemStatus.REJECTED, reviewed=False)])
        self.assertEqual((agreement.rows[0].rejected, agreement.rows[0].undecided), (0, 1))
        self.assertIsNone(agreement.percent)

    def test_an_item_counts_once_by_its_latest_shadow_decision(self) -> None:
        rows = [shadow_row(1, "wait", ItemStatus.COMPLETED), shadow_row(1, "approve", ItemStatus.COMPLETED)]
        agreement = stats.agreement_of(rows)
        self.assertEqual([(r.would, r.total) for r in agreement.rows], [("approve", 1)])

    def test_no_shadow_decisions_is_no_agreement(self) -> None:
        agreement = stats.agreement_of([])
        self.assertEqual((agreement.total, agreement.percent), (0, None))


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------


class GuardHistoryTests(unittest.TestCase):
    def test_pauses_are_counted_and_an_open_one_says_how_long_is_left(self) -> None:
        rows = [  # newest first, as the page asks for them
            event(events.COOKIE_EXPIRED, NOON - timedelta(minutes=10), provider="northwind", reason="refused"),
            event(events.GUARD, NOON - timedelta(minutes=10), provider="northwind", state="cooldown",
                  reason="the session cookie was refused", seconds=3600),
            event(events.GUARD, NOON - timedelta(days=2, hours=1), provider="harbor", state="ok"),
            event(events.GUARD, NOON - timedelta(days=2, hours=2), provider="harbor", state="cooldown",
                  reason="HTTP 429", seconds=900),
            event(events.GUARD, NOON - timedelta(days=9), provider="northwind", state="cooldown",
                  reason="HTTP 503", seconds=60),
        ]
        harbor, northwind = stats.guard_history(rows, NOON)
        self.assertEqual((harbor.name, harbor.pauses, harbor.expired, harbor.state), ("harbor", 1, 0, "Working"))
        self.assertEqual(harbor.last_reason, "HTTP 429")
        self.assertEqual((northwind.pauses, northwind.expired), (2, 1))
        self.assertEqual(northwind.state, "Paused, 50 min left")
        self.assertEqual(northwind.last_reason, "the session cookie was refused")

    def test_a_configured_provider_with_no_trouble_is_listed(self) -> None:
        rows = stats.guard_history([], NOON, ["quiet"])
        self.assertEqual([(r.name, r.pauses, r.state, r.last_at) for r in rows], [("quiet", 0, "Working", None)])

    def test_a_row_without_a_provider_is_skipped(self) -> None:
        self.assertEqual(stats.guard_history([event(events.GUARD, state="cooldown")], NOON), [])


class DownloadTotalsTests(unittest.TestCase):
    def test_totals_per_provider_with_what_came_from_no_search_last(self) -> None:
        row = lambda provider, status, n, size: SimpleNamespace(  # noqa: E731
            provider=provider, status=status, downloads=n, size=size,
        )
        found = stats.downloads_of([
            row("harbor", "COMPLETED", 2, 2000), row("harbor", "ERROR", 1, 700),
            row("northwind", "COMPLETED", 4, 4000), row("northwind", "DOWNLOADING", 1, 500),
            row("northwind", "CANCELLED", 1, 900), row(None, "COMPLETED", 9, 0),
        ])
        self.assertEqual([r.provider for r in found], ["northwind", "harbor", stats.DIRECT])
        northwind, harbor, direct = found
        self.assertEqual(
            (northwind.total, northwind.completed, northwind.running, northwind.failed, northwind.size),
            (6, 4, 1, 1, 4500),
        )
        # A failed add brought nothing, so its size is not counted.
        self.assertEqual((harbor.total, harbor.failed, harbor.size), (3, 1, 2000))
        self.assertEqual((direct.total, direct.completed), (9, 9))

    def test_the_query_names_the_release_the_download_was_picked_from(self) -> None:
        text = str(stats._DOWNLOADS_SQL)
        for part in ("r.value->>'provider'", "r.value->>'indexer'", "value->>'download_url' = d.download_url",
                     "value->>'magnet_url' = d.magnet_url", "jsonb_typeof(s.prowlarr_results) = 'array'"):
            with self.subTest(part=part):
                self.assertIn(part, text)


# ---------------------------------------------------------------------------
# The log's rows and its query
# ---------------------------------------------------------------------------


class LogRowTests(unittest.TestCase):
    def test_every_kind_reads_as_a_sentence(self) -> None:
        cases = [
            (event(events.DECISION, done="approve", reasons=["no rule hit"]), "The policy approved it: no rule hit."),
            (event(events.DECISION, done="review", reasons=["a", "b"]), "The policy held it for you: a; b."),
            (event(events.DECISION, outcome="wait"), "The policy is waiting for data."),
            (event(events.DECISION, done="reject", reasons=["keyword: list-a-word"]), "The policy rejected it: Keyword: list-a-word."),
            (event(events.WOULD_HAVE, outcome="approve"),
             "Shadow mode: the policy would have approved it, and left it to you."),
            (event(events.NEEDS_YOU, reasons=["imdb.violence: severe"]), "Held for you: Violence (IMDb): severe."),
            (event(events.GUARD, provider="harbor", state="cooldown", reason="HTTP 429", seconds=900),
             "harbor paused for 15 min: HTTP 429."),
            (event(events.GUARD, provider="harbor", state="ok"), "harbor is working again."),
            (event(events.COOKIE_EXPIRED, provider="harbor", reason="refused"),
             "harbor: the session ended: refused. It needs a new cookie."),
            (event(events.BOT_ADDED, source="inbox"), "Added through the bot (inbox)."),
            (event(events.DELIVERED, target="radarr", remote_id=12), "Handed to Radarr."),
            (event(events.DELIVERED, target="qbittorrent"), "Handed to qBittorrent."),
            (event(events.COMPLETED, target="sonarr", note="already in the library (Sonarr)"),
             "Sonarr has it: already in the library (Sonarr)."),
            (event(events.COMPLETED, target="radarr"), "Radarr has it."),
            (event(events.COMPLETED, target="none", note="ebooks are delivered nowhere."), "Done: ebooks are delivered nowhere."),
            (event(events.DELIVERY_FAILED, target="radarr", reason="HTTP 400"), "Radarr did not take it: HTTP 400."),
            (event(events.DELIVERY_FAILED, target="seerr", reason="timed out", retrying=True),
             "Seerr could not be asked: timed out. It is tried again."),
            (event(events.DELIVERY_FAILED, target="none", reason="no target for comics"),
             "It could not be delivered: no target for comics."),
            (event(events.DECLINED, target="seerr", reason="Its Seerr request was declined."), "Its Seerr request was declined."),
            (event(events.DECLINED), "Its Seerr request was declined."),
            (event(events.WEBHOOK_TEST, source="requests"), "Seerr's test notification reached the source requests."),
            (event(events.BOT_CHAT, chat_id=-100, title="Film club", state="joined", is_admin=True),
             "The bot was added to Film club as an admin."),
            (event(events.BOT_CHAT, chat_id=-100, state="left"), "The bot left a chat."),
        ]
        for row, text in cases:
            with self.subTest(text=text):
                self.assertEqual(stats.log_row(row).text, text)

    def test_every_kind_has_a_label_in_words(self) -> None:
        self.assertEqual(set(stats.KIND_LABELS), set(events.KINDS))
        for kind in events.KINDS:
            with self.subTest(kind=kind):
                self.assertTrue(stats.log_row(event(kind)).label[0].isupper())
                # A row always says something, even when its payload is empty.
                self.assertTrue(stats.log_row(event(kind)).text)

    def test_a_kind_this_page_never_heard_of_and_an_empty_payload_still_make_a_row(self) -> None:
        row = stats.log_row(SimpleNamespace(at=NOON, kind="moved", item_id=None, payload=None))
        self.assertEqual((row.label, row.text, row.title), ("moved", "", None))

    def test_a_chat_the_bot_joined_is_not_an_item_that_is_gone(self) -> None:
        row = stats.log_row(event(events.BOT_CHAT, chat_id=-100, title="Film club", state="joined"))
        self.assertIsNone(row.title)
        self.assertEqual(row.text, "The bot was added to Film club.")

    def test_the_item_s_own_title_wins_over_the_one_the_event_kept(self) -> None:
        row = event(events.NEEDS_YOU, item_id=7, title="As posted")
        self.assertEqual(stats.log_row(row, "As it is now").title, "As it is now")
        self.assertEqual(stats.log_row(row).title, "As posted")

    def test_the_time_is_the_server_s_and_machine_readable(self) -> None:
        row = stats.log_row(event(events.GUARD, provider="x", state="ok"))
        local = NOON.astimezone()
        self.assertEqual(row.when, f"{local.day} {local:%b %H:%M}")
        self.assertEqual(datetime.fromisoformat(row.iso), NOON)


class LogQueryTests(unittest.TestCase):
    def test_no_filter_is_every_event_with_its_item_s_title(self) -> None:
        text = sql(stats.log_query())
        self.assertIn("LEFT OUTER JOIN media_items ON media_items.id = events.item_id", text)
        for part in ("events.kind =", "ILIKE", "events.at >=", "IN (SELECT media_items.id"):
            self.assertNotIn(part, text)

    def test_a_decision_that_repeats_the_one_before_it_is_left_out(self) -> None:
        """The hourly re-check writes the same decision again; the log keeps the first, as the chart does."""
        text = sql(stats.log_query()).replace("\n", " ")
        self.assertIn("WHERE (events.id NOT IN (SELECT anon_1.id", text)
        inner = sql(stats.repeats()).replace("\n", " ")
        for part in (
            "lag(events.payload ->> 'done') OVER (PARTITION BY events.item_id, events.kind ORDER BY events.at, events.id)",
            "lag(events.payload ->> 'outcome') OVER (PARTITION BY events.item_id, events.kind ORDER BY events.at, events.id)",
            "events.kind IN ('decision', 'would_have')",
            "events.item_id IS NOT NULL",
            "anon_1.n > 1",
            "anon_1.done IS NOT DISTINCT FROM anon_1.done_before",
            "anon_1.outcome IS NOT DISTINCT FROM anon_1.outcome_before",
        ):
            with self.subTest(part=part):
                self.assertIn(part, inner)
        # Every filter keeps it, and so does the count, which is made from the same query.
        self.assertIn("NOT IN", sql(stats.log_query(kind="decision", item="42", since="week", waiting=True, now=NOON)))

    def test_the_kind_filter_takes_only_a_kind_that_exists(self) -> None:
        self.assertIn("AND events.kind = 'guard'", sql(stats.log_query(kind="guard")))
        self.assertNotIn("events.kind =", sql(stats.log_query(kind="'; drop table events")))

    def test_a_number_is_an_item_and_words_are_part_of_a_title_or_a_provider(self) -> None:
        for typed in ("42", "#42", " 42 "):
            self.assertIn("AND (events.item_id = 42 OR", sql(stats.log_query(item=typed)))
        text = sql(stats.log_query(item="Lantern"))
        self.assertIn("media_items.title ILIKE '%' || 'Lantern' || '%'", text)
        self.assertIn("(events.payload ->> 'title')) ILIKE", text)
        self.assertIn("(events.payload ->> 'provider')) ILIKE", text)

    def test_a_number_no_item_could_have_is_searched_as_words(self) -> None:
        for typed in ("99999999999999999999", "\u00b2", "\u0663"):
            with self.subTest(typed=typed):
                text = sql(stats.log_query(item=typed))
                self.assertNotIn("events.item_id =", text)
                self.assertIn("media_items.title ILIKE", text)

    def test_a_title_of_digits_is_found_by_its_title_too(self) -> None:
        text = sql(stats.log_query(item="1917"))
        self.assertIn("events.item_id = 1917 OR", text)
        self.assertIn("media_items.title ILIKE '%' || '1917' || '%'", text)

    def test_a_nul_never_reaches_the_database(self) -> None:
        """Postgres refuses a NUL in text, and the page would be a 500."""
        text = sql(stats.log_query(item="a\x00b"))
        self.assertNotIn("\x00", text)
        self.assertIn("'ab'", text)

    def test_a_percent_sign_is_searched_for_not_obeyed(self) -> None:
        self.assertIn("'100/%' || '%' ESCAPE '/'", sql(stats.log_query(item="100%")))

    def test_the_time_filter_counts_back_from_now(self) -> None:
        text = sql(stats.log_query(since="week", now=NOON))
        self.assertIn("events.at >= '2026-10-05 12:00:00+00:00'", text)
        self.assertNotIn("events.at >=", sql(stats.log_query(since="fortnight", now=NOON)))

    def test_still_waiting_is_the_query_the_nav_counts(self) -> None:
        text = sql(stats.log_query(waiting=True))
        self.assertIn(f"events.item_id IN ({sql(shell.waiting_items())})".replace("\n", " "), text.replace("\n", " "))


# ---------------------------------------------------------------------------
# The pages
# ---------------------------------------------------------------------------


class _Answer:
    def __init__(self, value):
        self.value = value

    def all(self):
        return list(self.value)

    def scalar(self):
        return self.value


class Answers:
    """A session that answers the page's queries in the order they are asked."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.statements = []

    def __call__(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, statement):
        self.statements.append(statement)
        return _Answer(self.answers.pop(0))


class PageCase(WebCase):
    AUTH = None

    def serve(self, *answers) -> Answers:
        session = Answers(*answers)
        patcher = mock.patch.object(stats, "async_session", session)
        patcher.start()
        self.addCleanup(patcher.stop)
        return session


class StatsPageTests(PageCase):
    def page(self, decisions=(), shadow=(), guards=(), downloads=()) -> str:
        self.serve(list(decisions), list(shadow), list(guards), list(downloads))
        with mock.patch.object(stats, "_providers", return_value=["harbor", "northwind"]):
            resp = self.get("/stats")
        self.assertEqual(resp.status_code, 200)
        return resp.text

    def full(self) -> str:
        return self.page(
            decisions=[
                decision("approve", 0, 1, reasons=["no rule hit (5 checked)"]),
                decision("review", 0, 2, reasons=["imdb.violence: severe"]),
                decision("reject", 1, 3, media_type="series", reasons=["keyword: list-a-word"]),
                decision("wait", 2, 4, reasons=["imdb.nudity: parents guide not fetched"]),
            ],
            shadow=[shadow_row(n, "approve", ItemStatus.COMPLETED) for n in range(12)]
            + [shadow_row(n, "approve", ItemStatus.REJECTED) for n in (12, 13)],
            guards=[event(events.GUARD, datetime.now(timezone.utc) - timedelta(minutes=5), provider="northwind",
                          state="cooldown", reason="HTTP 429", seconds=1800)],
            downloads=[SimpleNamespace(provider="harbor", status="COMPLETED", downloads=3, size=5 * 1024**3)],
        )

    def test_the_chart_is_an_svg_with_its_sentence_a_legend_in_words_and_its_numbers(self) -> None:
        page = self.full()
        svg = re.search(r"<svg class=\"chart-plot\".*?</svg>", page, re.S).group(0)
        self.assertIn('role="img"', svg)
        self.assertIn('aria-label="Decisions per day, last 30 days: 1 approved, 1 held for you, '
                      '1 waiting for data, 1 rejected.', svg)
        self.assertEqual(len(re.findall(r"<rect ", svg)), 4)
        for word in ("approved", "held for you", "waiting for data", "rejected"):
            self.assertRegex(svg, rf"<title>[^<]*: 1 {word}</title>")
        self.assertNotIn("<script", svg)
        legend = re.search(r'<ul class="legend".*?</ul>', page, re.S).group(0)
        for label in ("Approved", "Held for you", "Waiting for data", "Rejected"):
            self.assertIn(label, legend)
        numbers = re.search(r"<details.*?</details>", page, re.S).group(0)
        self.assertIn("Show the numbers per day", numbers)
        days = re.findall(r"<li>(.*?)</li>", numbers, re.S)
        self.assertEqual(len(days), 3)
        self.assertRegex(days[0], r"2 decisions</span>\s*</div>\s*<p class=\"text-sm text-muted\">1 approved, 1 held for you\.</p>")

    def test_the_numbers_per_day_fit_a_phone(self) -> None:
        """A six-column table was 454 px wide in a 332 px card at 390: the last columns were cut off.
        Each day is a row of words now, as the downloads per provider are."""
        page = self.full()
        self.assertNotIn("<table", page)
        self.assertNotIn("table-scroll", page)

    def test_no_chart_library_and_nothing_from_another_host(self) -> None:
        page = self.full()
        self.assertEqual(re.findall(r'<script[^>]+src="(?!/static/)[^"]*"', page), [])
        self.assertEqual(len(re.findall(r"<script", page)), 3)  # the theme line, htmx, app.js

    def test_the_tiles_say_the_totals(self) -> None:
        page = self.full()
        tiles = re.findall(r'<span class="stat-label">(?:<span[^>]*></span>)?([^<]+)</span>\s*<span class="stat-value">(\d+)</span>', page)
        self.assertEqual(tiles, [
            ("Decisions", "4"), ("Approved", "1"), ("Held for you", "1"), ("Waiting for data", "1"), ("Rejected", "1"),
        ])

    def test_reasons_types_shadow_guards_and_downloads_are_on_the_page(self) -> None:
        page = self.full()
        for text in (
            "Violence (IMDb): severe", "Keyword: list-a-word",
            "1 approved · 1 held for you · 1 waiting for data", "1 rejected",
            "Would have approved 14: you approved 12, rejected 2.",
            "86%", "on 12 of the 14 you decided",
            "Paused, 25 min left", "Paused 1 time", "HTTP 429",
            "No pause in the last 30 days.",
            '<span class="font-semibold">harbor</span>', "5.0 GB", "3 downloads: 3 done, 0 running, 0 failed.",
        ):
            with self.subTest(text=text):
                self.assertIn(text, page)
        # An approval's reason is not why anything was held.
        self.assertNotIn("no rule hit", page.split('id="top-reasons"')[1].split("</section>")[0])

    def test_every_section_has_a_heading_and_names_itself(self) -> None:
        page = self.full()
        self.assertEqual(len(re.findall(r"<h1", page)), 1)
        named = re.findall(r'<section[^>]*aria-labelledby="([^"]+)"', page)
        self.assertEqual(named, ["per-day", "top-reasons", "per-type", "shadow", "guards", "downloads"])
        for name in named:
            self.assertRegex(page, rf'<h2 class="section-title" id="{name}">')

    def test_an_empty_database_says_so_in_every_section(self) -> None:
        self.serve([], [], [], [])
        with mock.patch.object(stats, "_providers", return_value=[]):
            page = self.get("/stats").text
        for text in (
            "The policy decided nothing in the last 30 days.",
            "Nothing was held, left waiting or rejected.",
            "No decisions yet.",
            "No shadow decisions in the last 30 days.",
            "No provider is configured and none was paused.",
            "Nothing has been downloaded yet.",
        ):
            with self.subTest(text=text):
                self.assertIn(text, page)
        self.assertNotIn("<svg class=\"chart-plot\"", page)

    def test_the_page_reads_thirty_days_of_events(self) -> None:
        session = self.serve([], [], [], [])
        self.get("/stats")
        decisions, shadow, guards, _downloads = (sql(s) for s in session.statements)
        self.assertIn("events.kind = 'decision'", decisions)
        self.assertIn("events.kind = 'would_have'", shadow)
        self.assertIn("JOIN media_items ON media_items.id = events.item_id", shadow)
        self.assertIn("events.kind IN ('guard', 'cookie_expired')", guards)
        floor = datetime.now(timezone.utc) - timedelta(days=30)
        for text in (decisions, shadow, guards):
            self.assertIn(f"events.at >= '{floor:%Y-%m-%d}", text)

    def test_stats_and_activity_link_to_each_other_and_say_which_is_open(self) -> None:
        page = self.full()
        self.assertIn('<a class="tab" href="/stats" aria-current="page">Stats</a>', page)
        self.assertIn('<a class="tab" href="/activity" >Activity</a>', page)


def log_rows(n, first_id=1):
    return [
        (event(events.NEEDS_YOU, NOON - timedelta(minutes=i), first_id + i, title=f"Kept title {first_id + i}",
               reasons=["imdb.violence: severe"]), f"Title {first_id + i}")
        for i in range(n)
    ]


class ActivityPageTests(PageCase):
    def page(self, url="/activity", total=3, rows=None):
        session = self.serve(total, log_rows(3) if rows is None else rows)
        resp = self.get(url)
        self.assertEqual(resp.status_code, 200)
        return resp.text, session

    def test_every_row_links_to_its_item_with_the_time_the_kind_and_the_words(self) -> None:
        page, _ = self.page()
        rows = re.findall(r'<li class="log-row">(.*?)</li>', page, re.S)
        self.assertEqual(len(rows), 3)
        # The item page's back link then says Activity, not Inbox.
        self.assertIn('<a class="link font-semibold" href="/item/1?from=activity">Title 1</a>', rows[0])
        self.assertRegex(rows[0], r'<time class="log-time" datetime="2026-10-12T[^"]+">\d+ Oct \d\d:\d\d</time>')
        self.assertIn('<span class="badge badge-warn">Needs you</span>', rows[0])
        self.assertIn("Held for you: Violence (IMDb): severe.", rows[0])

    def test_an_event_with_no_item_has_no_link_and_one_whose_item_is_gone_says_so(self) -> None:
        page, _ = self.page(rows=[
            (event(events.GUARD, provider="harbor", state="ok"), None),
            (event(events.DECISION, done="reject", title="Gone Film"), None),
        ])
        guard, gone = re.findall(r'<li class="log-row">(.*?)</li>', page, re.S)
        self.assertNotIn('href="/item', guard)
        self.assertIn("harbor is working again.", guard)
        # A provider's row links to where its fix is: its card in Setup.
        self.assertIn('<a class="link" href="/setup#provider-harbor">Open its card in Setup</a>', guard)
        self.assertNotIn("<a ", gone)
        self.assertIn("Gone Film", gone)
        self.assertIn("(the item is gone)", gone)

    def test_the_log_is_newest_first_and_fifty_to_a_page(self) -> None:
        _, session = self.page("/activity?page=3", total=140)
        count, rows = (sql(s) for s in session.statements)
        self.assertIn("count(*)", count)
        self.assertIn("ORDER BY events.at DESC, events.id DESC", rows)
        self.assertRegex(rows.replace("\n", " "), r"LIMIT 50\s+OFFSET 100")

    def test_the_pager_keeps_the_filters_and_stops_at_both_ends(self) -> None:
        page, _ = self.page("/activity?kind=needs_you&item=Lantern+Keeper&since=week&waiting=1&page=2", total=140)
        self.assertIn("Page 2 of 3", page)
        self.assertIn('href="/activity?kind=needs_you&amp;item=Lantern+Keeper&amp;since=week&amp;waiting=1" rel="prev"', page)
        self.assertIn('href="/activity?kind=needs_you&amp;item=Lantern+Keeper&amp;since=week&amp;waiting=1&amp;page=3" rel="next"', page)
        first, _ = self.page("/activity", total=140)
        self.assertNotIn('rel="prev"', first)
        self.assertIn('href="/activity?page=2" rel="next"', first)
        last, _ = self.page("/activity?page=3", total=140)
        self.assertNotIn('rel="next"', last)
        one, _ = self.page("/activity", total=3)
        self.assertNotIn('class="pager"', one)

    def test_a_page_past_the_end_is_the_last_page(self) -> None:
        page, session = self.page("/activity?page=99", total=60)
        self.assertIn("Page 2 of 2", page)
        self.assertRegex(sql(session.statements[1]).replace("\n", " "), r"LIMIT 50\s+OFFSET 50")
        # Not a page number at all, or one far past the end: still a page, never FastAPI's JSON.
        for asked, shown in (("0", "Page 1 of 2"), ("abc", "Page 1 of 2"), ("999999", "Page 2 of 2"),
                             ("99999999999999999999", "Page 2 of 2"), ("-3", "Page 1 of 2")):
            with self.subTest(page=asked):
                self.assertIn(shown, self.page(f"/activity?page={asked}", total=60)[0])

    def test_a_nul_in_the_item_field_is_a_page_not_an_error(self) -> None:
        page, session = self.page("/activity?item=a%00b")
        self.assertNotIn("\x00", sql(session.statements[1]))
        self.assertIn('value="ab"', page)

    def test_the_filters_reach_the_query(self) -> None:
        _, session = self.page("/activity?kind=guard&item=harbor&since=day&waiting=1")
        rows = sql(session.statements[1])
        for part in ("events.kind = 'guard'", "ILIKE '%' || 'harbor' || '%'", "events.at >= ", "events.item_id IN (SELECT media_items.id"):
            with self.subTest(part=part):
                self.assertIn(part, rows)

    def test_the_form_shows_what_is_filtered_and_every_field_has_a_label(self) -> None:
        page, _ = self.page("/activity?kind=guard&item=harbor&since=day&waiting=1")
        form = re.search(r"<form class=\"card log-filters.*?</form>", page, re.S).group(0)
        self.assertIn('<option value="guard" selected>Provider</option>', form)
        self.assertIn('<option value="day" selected>Last 24 hours</option>', form)
        self.assertIn('value="harbor"', form)
        self.assertRegex(form, r'name="waiting" value="1" checked')
        for field in re.findall(r'<(?:select|input)[^>]*\bid="([^"]+)"', form):
            with self.subTest(field=field):
                self.assertIn(f'<label class="label" for="{field}">', form)
        # The checkbox sits inside its label.
        self.assertRegex(form, r'<label class="check">\s*<input type="checkbox"')
        # Works without the script, and with it only the log is replaced.
        self.assertIn('method="get" action="/activity"', form)
        self.assertIn('hx-select="#activity-log"', form)
        self.assertIn("<button", form)

    def test_a_kind_or_a_time_nobody_offers_is_ignored_not_an_error(self) -> None:
        page, session = self.page("/activity?kind=nonsense&since=never")
        self.assertNotIn("events.kind =", sql(session.statements[1]))
        self.assertNotIn("events.at >=", sql(session.statements[1]))
        self.assertNotIn("match the filters", page)

    def test_the_count_is_a_status_line_outside_what_a_filter_replaces(self) -> None:
        page, _ = self.page("/activity?kind=guard", total=1, rows=log_rows(1))
        self.assertRegex(page, r'<p[^>]*id="activity-count" role="status">1 event matches the filters\.\s*</p>')
        self.assertLess(page.index('id="activity-count"'), page.index('<div id="activity-log">'))
        self.assertIn('hx-select-oob="#activity-count:innerHTML"', page)
        plain, _ = self.page(total=140)
        self.assertIn("140 events, page 1 of 3.", plain)

    def test_nothing_found_says_so(self) -> None:
        page, _ = self.page("/activity?kind=guard", total=0, rows=[])
        self.assertIn("No event matches the filters.", page)
        empty, _ = self.page(total=0, rows=[])
        self.assertIn("Nothing has happened yet.", empty)

    def test_a_title_is_escaped(self) -> None:
        page, _ = self.page(rows=[(event(events.NEEDS_YOU, item_id=1, reasons=["<b>x</b>"]), "<script>alert(1)</script>")])
        self.assertNotIn("<script>alert", page)
        self.assertNotIn("<b>x</b>", page)

    def test_the_page_says_how_many_items_need_you_and_links_to_their_events(self) -> None:
        self.needs_you.return_value = 4
        page, _ = self.page()
        self.assertIn('<a class="link" href="/activity?waiting=1">4 items need you</a>', page)
        self.needs_you.return_value = 1
        self.assertIn("1 item needs you", self.page()[0])
        self.needs_you.return_value = 0
        self.assertIn("Nothing is waiting for you.", self.page()[0])

    def test_back_loads_the_address_from_the_server_and_keeps_no_copy(self) -> None:
        """htmx's own history kept the log of the last filter under a form reset to nothing, and the
        pages stayed in the tab's storage after Sign out. Back now asks the server, which draws the
        form from the query string."""
        page, _ = self.page("/activity?kind=guard")
        meta = re.search(r'<meta name="htmx-config" content=\'([^\']+)\'>', page)
        self.assertIsNotNone(meta)
        self.assertEqual(json.loads(meta.group(1)), {"historyCacheSize": 0, "refreshOnHistoryMiss": True})
        # Before htmx is loaded, or it reads its defaults.
        self.assertLess(page.index('name="htmx-config"'), page.index("htmx-2.0.11.min.js"))
        self.assertIn('hx-push-url="true"', page)

    def test_a_stranger_is_sent_to_the_login_page(self) -> None:
        with mock.patch.object(self.gate, "mode", "password"):
            resp = self.get("/activity?kind=guard")
        self.assertEqual((resp.status_code, resp.headers["location"]), (303, "/login?next=/activity%3Fkind%3Dguard"))


# ---------------------------------------------------------------------------
# The nav's count
# ---------------------------------------------------------------------------


class NeedsYouTests(PageCase):
    def inbox_link(self, page: str) -> str:
        return re.search(r'<a class="nav-link" href="/".*?</a>', page, re.S).group(0)

    def test_the_inbox_entry_carries_the_count_in_figures_and_in_words(self) -> None:
        self.needs_you.return_value = 5
        link = self.inbox_link(self.get("/review").text)
        self.assertIn('<span class="nav-count"><span aria-hidden="true">5</span>'
                      '<span class="sr-only">, 5 need you</span></span>', link)
        self.needs_you.return_value = 1
        self.assertIn(", 1 needs you", self.inbox_link(self.get("/review").text))
        self.needs_you.return_value = 250
        link = self.inbox_link(self.get("/review").text)
        self.assertIn('<span aria-hidden="true">99+</span>', link)
        self.assertIn(", 250 need you", link)

    def test_nothing_waiting_is_no_badge_and_only_inbox_ever_has_one(self) -> None:
        self.assertNotIn('class="nav-count"', self.get("/review").text)
        self.needs_you.return_value = 5
        self.assertEqual(self.get("/review").text.count('class="nav-count"'), 1)

    def test_the_count_is_read_for_a_full_page_only(self) -> None:
        self.get("/review")
        self.needs_you.assert_awaited_once()
        self.needs_you.reset_mock()
        self.get("/review", headers={"HX-Request": "true"})
        self.approve()
        self.needs_you.assert_not_awaited()

    def test_an_action_on_the_page_reads_the_count_again(self) -> None:
        """Every action answers with a toast; the count listens for it and asks the server again."""
        self.needs_you.return_value = 14
        slot = re.search(r'<span id="nav-count"[^>]*>', self.inbox_link(self.get("/review").text)).group(0)
        self.assertIn('hx-get="/inbox/count"', slot)
        self.assertIn('hx-trigger="toast from:body"', slot)
        self.assertIn('hx-swap="outerHTML"', slot)
        self.needs_you.return_value = 13
        resp = self.get("/inbox/count", headers={"HX-Request": "true"})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.text.startswith('<span id="nav-count"'), resp.text[:80])
        self.assertIn('<span aria-hidden="true">13</span><span class="sr-only">, 13 need you</span>', resp.text)
        self.needs_you.return_value = 0
        empty = self.get("/inbox/count", headers={"HX-Request": "true"}).text
        self.assertIn('id="nav-count"', empty)  # the slot stays, so the next action can fill it
        self.assertNotIn('class="nav-count"', empty)
        # What every action answers with carries the toast the count listens for.
        from vettarr.web import cards
        self.assertIn('"toast"', cards.announce("Approved.")["HX-Trigger"])

    def test_activity_belongs_to_stats_in_the_nav(self) -> None:
        lit = [label for label, _, on in ui._shell(SimpleNamespace(
            state=SimpleNamespace(), scope={"path": "/activity"},
        ))["nav"] if on]
        self.assertEqual(lit, ["Stats"])

    def test_the_count_is_the_items_waiting_for_a_decision(self) -> None:
        """The Inbox's "Needs you" tab and Review count three statuses; the badge counted one."""
        text = sql(shell.waiting_items())
        self.assertIn("SELECT media_items.id", text)
        self.assertIn("media_items.status IN ('ERROR', 'PARSE_FAILED', 'PENDING_REVIEW')", text)


class NeedsYouQueryTests(unittest.TestCase):
    def test_it_counts_the_waiting_items(self) -> None:
        session = Answers(7)
        with mock.patch.object(shell, "async_session", session):
            self.assertEqual(asyncio.run(shell.needs_you()), 7)
        text = sql(session.statements[0])
        self.assertIn("count(*)", text)
        self.assertIn("media_items.status IN ('ERROR', 'PARSE_FAILED', 'PENDING_REVIEW')", text)

    def test_a_database_that_is_down_is_no_badge_and_raises_nothing(self) -> None:
        def down():
            raise ConnectionRefusedError("down")

        with mock.patch.object(shell, "async_session", down):
            self.assertEqual(asyncio.run(shell.needs_you()), 0)


class StylesheetTests(unittest.TestCase):
    def test_every_chart_colour_is_set_for_dark_and_for_light(self) -> None:
        css = (WEB_DIR / "static" / "src" / "app.css").read_text()
        for outcome in stats.OUTCOMES:
            with self.subTest(outcome=outcome):
                # Once in the dark tokens (the default), once in the light block.
                self.assertEqual(len(re.findall(rf"--chart-{outcome}: #[0-9a-f]{{6}};", css)), 2)
                self.assertRegex(css, rf"\.chart-{outcome} \{{ fill: var\(--chart-{outcome}\)")


if __name__ == "__main__":
    unittest.main()
