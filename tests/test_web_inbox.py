"""The Inbox, Review and the item page: what each shows of an item, and what each route answers.

The items are the demo seed's, decided by the real policy engine, so a row
here carries the reasons, signals and releases a real one does. No database
runs: what a page reads (``vettarr.web.cards``'s ``load_*``) is replaced
by the rows a test hands it. The readers themselves are run as SQL by
``test_web_readers.py``, and the pages are checked against a real Postgres and
a real browser by ``scripts/checks.js`` (docs/DEV.md).
"""

import json
import os
import pathlib
import re
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

from test_web_foundation import WebCase  # sets the environment the config needs, first

from markupsafe import escape
from sqlalchemy.dialects import postgresql

from vettarr import actions, demo, events
from vettarr.models import (
    REJECTABLE_STATUSES,
    REVIEWABLE_STATUSES,
    Download,
    DownloadStatus,
    ItemStatus,
    MediaItem,
    MediaType,
    TorrentSearch,
)
from vettarr.web import cards, shell
from vettarr.web import app as web_app
from vettarr.web.routes import review as review_routes

WEB_DIR = pathlib.Path(web_app.__file__).parent
NOW = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)
HTMX = {"HX-Request": "true"}
FROM_INBOX = {**HTMX, "HX-Current-URL": "http://testserver/?tab=waiting&type=movie&source=demo-group"}
_S = ItemStatus


class Rows:
    """The demo seed's rows with ids, as a database would hand them back."""

    def __init__(self) -> None:
        seed = demo.build(NOW)
        self.items: list[MediaItem] = seed.items
        self.events: dict[int, list] = {}
        for n, item in enumerate(self.items, start=1):
            item.id = n
            for k, search in enumerate(item.torrent_searches):
                search.id = n * 10 + k
                search.media_item_id = n
        for index, event in seed.events:
            if index is not None:
                self.events.setdefault(index + 1, []).append(event)

    def one(self, status: ItemStatus, where=lambda item: True) -> MediaItem:
        return next(item for item in self.items if item.status is status and where(item))

    def card(self, item: MediaItem, **more) -> cards.Card:
        searches = sorted(item.torrent_searches, key=lambda s: s.id, reverse=True)
        return cards.Card(
            item, item.content_check, cards.search_summary(searches[0]) if searches else None, item.download, **more,
        )

    def detail(self, item: MediaItem, **more) -> cards.Detail:
        return cards.detail_of(
            item, check=item.content_check, searches=sorted(item.torrent_searches, key=lambda s: s.id, reverse=True),
            download=item.download, rows=self.events.get(item.id, []), **more,
        )

    # The items the tests keep coming back to.

    @property
    def violent(self) -> MediaItem:
        """Held by the violence rule, never searched."""
        return self.one(_S.PENDING_REVIEW, lambda i: not i.torrent_searches and any(
            r["rule"] == "violence" for r in i.reasons or []))

    @property
    def searched(self) -> MediaItem:
        return self.one(_S.PENDING_REVIEW, lambda i: i.torrent_searches and i.media_type is MediaType.MOVIE)

    @property
    def downloading(self) -> MediaItem:
        return self.one(_S.DOWNLOADING, lambda i: i.media_type is MediaType.MOVIE)

    @property
    def rejected_by_policy(self) -> MediaItem:
        return self.one(_S.REJECTED, lambda i: i.reviewed_at is None and i.policy_outcome == "reject")


def counts(**given: int) -> dict[str, int]:
    return {tab.key: given.get(tab.key.replace("-", "_"), 0) for tab in cards.TABS}


def sql(query) -> str:
    return str(query.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


def esc(text: str) -> str:
    """Text as a template writes it."""
    return str(escape(text))


def toast(resp) -> dict:
    return json.loads(resp.headers["HX-Trigger"])["toast"]


def ok(message: str = "Done.", item_id: int = 7) -> actions.ActionResult:
    return actions.ActionResult(item_id, actions.Status.OK, message)


BUSY = actions.ActionResult(7, actions.Status.BUSY, actions.BUSY_MESSAGE)
GONE = actions.ActionResult(7, actions.Status.NOT_FOUND, actions.NOT_FOUND_MESSAGE)
NO_TITLE = actions.SearchOutcome(7, actions.Status.FAILED, "This item has no title to search for.")


def acting(name: str, result):
    return mock.patch.object(actions, name, mock.AsyncMock(return_value=result))


# ---------------------------------------------------------------------------
# What the three pages agree on
# ---------------------------------------------------------------------------


class TabTests(unittest.TestCase):
    def test_the_five_tabs_in_order(self) -> None:
        self.assertEqual(
            [(tab.key, tab.label) for tab in cards.TABS],
            [("needs", "Needs you"), ("waiting", "Waiting for data"), ("no-release", "No release"),
             ("done", "Decided"), ("all", "All")],
        )

    def test_every_status_a_person_may_act_on_is_in_one_queue(self) -> None:
        queues = [tab.statuses for tab in cards.TABS[:3]]
        for status in REVIEWABLE_STATUSES:
            with self.subTest(status=status):
                self.assertEqual(sum(status in statuses for statuses in queues), 1)
        self.assertFalse(cards.TABS[3].statuses & REVIEWABLE_STATUSES)
        self.assertIsNone(cards.tab_of("all").statuses)

    def test_an_unknown_tab_is_the_first(self) -> None:
        for key in (None, "", "bogus", "NEEDS"):
            self.assertIs(cards.tab_of(key), cards.TABS[0])

    def test_a_queue_is_read_oldest_first_like_the_bot(self) -> None:
        for key in ("needs", "waiting", "no-release"):
            with self.subTest(tab=key):
                query = sql(cards.items_query(cards.tab_of(key), cards.Filters()))
                self.assertTrue(query.endswith("ORDER BY media_items.created_at ASC, media_items.id ASC"), query)

    def test_what_is_finished_is_read_newest_first(self) -> None:
        for key in ("done", "all"):
            with self.subTest(tab=key):
                query = sql(cards.items_query(cards.tab_of(key), cards.Filters()))
                self.assertTrue(query.endswith("ORDER BY media_items.created_at DESC, media_items.id DESC"), query)

    def test_a_tab_reads_its_statuses_and_all_reads_every_one(self) -> None:
        needs = sql(cards.items_query(cards.tab_of("needs"), cards.Filters()))
        self.assertIn("media_items.status IN ('ERROR', 'PARSE_FAILED', 'PENDING_REVIEW')", needs)
        self.assertNotIn("WHERE", sql(cards.items_query(cards.tab_of("all"), cards.Filters())))

    def test_the_filters_narrow_the_list_and_the_counts_alike(self) -> None:
        filters = cards.Filters.read({"type": "movie", "source": "demo-group", "outcome": "review"})
        for query in (sql(cards.items_query(cards.tab_of("needs"), filters)), sql(cards.counts_query(filters))):
            self.assertIn("media_items.media_type = 'MOVIE'", query)
            self.assertIn("media_items.source_key = 'demo-group'", query)
            self.assertIn("media_items.policy_outcome = 'review'", query)
        self.assertIn("GROUP BY media_items.status", sql(cards.counts_query(filters)))

    def test_a_filter_value_nothing_has_is_dropped_not_an_error(self) -> None:
        filters = cards.Filters.read({"type": "zzz", "outcome": "maybe", "source": " demo-bot "})
        self.assertEqual(filters, cards.Filters(source="demo-bot"))
        self.assertFalse(cards.Filters.read({}).any)
        self.assertEqual(cards.Filters.read({"type": ["series"]}).type, "series")

    def test_a_link_keeps_the_filters(self) -> None:
        filters = cards.Filters(type="movie", outcome="review")
        self.assertEqual(filters.query(tab="done", page=2), "type=movie&outcome=review&tab=done&page=2")
        self.assertEqual(cards.Filters().query(tab="needs"), "tab=needs")

    def test_a_tab_s_count_is_the_sum_of_its_statuses(self) -> None:
        by_status = {_S.PENDING_REVIEW: 14, _S.ERROR: 2, _S.PARSE_FAILED: 2, _S.AWAITING_DATA: 6, _S.NO_MATCH: 4,
                     _S.COMPLETED: 10, _S.REJECTED: 8, _S.IGNORED: 2}
        self.assertEqual(cards.tab_counts(by_status), {"needs": 18, "waiting": 6, "no-release": 4, "done": 18, "all": 48})

    def test_the_page_an_htmx_request_came_from_names_the_tab_and_filters(self) -> None:
        request = SimpleNamespace(headers=FROM_INBOX)
        tab, filters = cards.where(request)
        self.assertEqual((tab.key, filters), ("waiting", cards.Filters(type="movie", source="demo-group")))
        self.assertEqual(cards.where(SimpleNamespace(headers={}))[0].key, "needs")


class CardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.rows = Rows()

    def test_a_status_is_said_in_words(self) -> None:
        said = {item.status: self.rows.card(item).status[0] for item in self.rows.items}
        self.assertEqual(said[_S.PENDING_REVIEW], "Needs you")
        self.assertEqual(said[_S.AWAITING_DATA], "Waiting for data")
        self.assertEqual(said[_S.NO_MATCH], "No release")
        self.assertEqual(said[_S.PARSE_FAILED], "Could not be read")
        self.assertEqual(said[_S.COMPLETED], "Downloaded")
        for status in _S:
            self.assertTrue(cards.Card(MediaItem(id=1, status=status)).status[0], status)

    def test_a_rejection_says_who_made_it(self) -> None:
        self.assertEqual(self.rows.card(self.rows.rejected_by_policy).status[0], "Rejected by the policy")
        person = self.rows.one(_S.REJECTED, lambda i: i.reviewed_at is not None)
        self.assertEqual(self.rows.card(person).status[0], "Rejected by a person")

    def test_a_download_says_how_far_it_is(self) -> None:
        item = self.rows.downloading
        card = self.rows.card(item)
        self.assertTrue(card.downloading)
        self.assertEqual(card.status[0], f"Downloading {round(item.download.progress * 100)}%")
        done = self.rows.card(self.rows.one(_S.COMPLETED))
        self.assertFalse(done.downloading)
        self.assertEqual(done.progress, 100)

    def test_the_verdict_is_what_the_policy_decided(self) -> None:
        self.assertEqual(self.rows.card(self.rows.violent).verdict, ("Policy: review", "warn"))
        self.assertEqual(self.rows.card(self.rows.rejected_by_policy).verdict, ("Policy: reject", "flag"))
        self.assertEqual(self.rows.card(self.rows.one(_S.AWAITING_DATA)).verdict, ("Policy: wait", "muted"))
        # Shadow mode held what the policy would have approved.
        shadow = next(i for i in self.rows.items if (i.reasons or [{}])[0].get("kind") == "shadow")
        self.assertEqual(self.rows.card(shadow).verdict, ("Policy: approve", "ok"))
        self.assertIsNone(self.rows.card(self.rows.one(_S.PARSE_FAILED)).verdict)

    def test_a_row_decided_before_the_engine_is_read_by_its_old_column(self) -> None:
        from vettarr.models import AutoDecision

        old = MediaItem(id=1, status=_S.PENDING_REVIEW, auto_decision=AutoDecision.NEEDS_REVIEW,
                        auto_reasons=["sex rated moderate"])
        card = cards.Card(old)
        self.assertEqual(card.verdict, ("Policy: review", "warn"))
        self.assertEqual([(r.tag, r.text) for r in card.reasons], [("legacy", "sex rated moderate")])

    def test_every_reason_is_tagged_by_its_rule(self) -> None:
        card = self.rows.card(self.rows.violent)
        self.assertEqual([(r.tag, r.note, r.finding) for r in card.reasons], [("violence", "", True)])
        waiting = self.rows.card(self.rows.one(_S.AWAITING_DATA))
        # "Waiting for IMDb's parents guide" says it is missing: no "missing:" note beside it.
        self.assertEqual({(r.tag, r.note) for r in waiting.reasons},
                         {("violence", ""), ("nudity", ""), ("frightening", "")})
        # Three rules waiting for the same guide are one line in a row.
        self.assertEqual(len(waiting.row_reasons), 1)
        self.assertTrue(waiting.row_reasons[0].text.startswith("Waiting for IMDb's parents guide"))
        clear = self.rows.card(self.rows.one(_S.COMPLETED, lambda i: i.media_type is MediaType.MOVIE))
        self.assertEqual({r.tag for r in clear.reasons}, {"clear"})

    def test_a_row_leads_with_what_was_found(self) -> None:
        item = MediaItem(id=1, status=_S.PENDING_REVIEW, reasons=[
            {"kind": "shadow", "rule": None, "text": "shadow mode: would have rejected"},
            {"kind": "rule", "rule": "unwanted-keywords", "text": "keyword: list-a-word"},
        ])
        self.assertEqual(cards.Card(item).lead.tag, "unwanted-keywords")
        self.assertIsNone(cards.Card(MediaItem(id=2, status=_S.CHECKING)).lead)

    def test_the_pills_carry_the_rating_s_word(self) -> None:
        pills = dict(self.rows.card(self.rows.violent).pills)
        self.assertEqual(pills["Violence"], "severe")
        self.assertEqual(set(pills), {"Nudity", "Violence", "Profanity", "Frightening", "Alcohol"})
        self.assertEqual(self.rows.card(self.rows.one(_S.PARSE_FAILED)).pills, [])
        unrated = self.rows.card(self.rows.one(_S.AWAITING_DATA))
        self.assertEqual(unrated.pills, [])
        self.assertTrue(unrated.guide_expected)

    def test_the_best_release_is_the_latest_search_s_first(self) -> None:
        item = self.rows.searched
        best = self.rows.card(item).best
        latest = item.torrent_searches[-1]
        self.assertEqual((best["title"], best["search_id"]), (latest.prowlarr_results[0]["title"], latest.id))
        self.assertIsNone(self.rows.card(self.rows.violent).best)
        nothing = self.rows.card(self.rows.one(_S.NO_MATCH))
        self.assertIsNone(nothing.best)
        self.assertEqual(nothing.search["found"], 0)

    def test_the_buttons_an_item_gets_follow_what_the_actions_allow(self) -> None:
        for status in _S:
            card = cards.Card(MediaItem(id=1, status=status, title="A title"))
            with self.subTest(status=status):
                self.assertEqual(card.can_approve, status in REVIEWABLE_STATUSES)
                self.assertEqual(card.can_reject, status in REJECTABLE_STATUSES)
                self.assertEqual(card.can_search, status in REVIEWABLE_STATUSES)
                self.assertEqual(card.can_grab, status in REVIEWABLE_STATUSES)
                self.assertEqual(card.can_rerun, status in (_S.PENDING_REVIEW, _S.AWAITING_DATA))
                self.assertEqual(card.selectable, card.can_approve or card.can_reject)

    def test_review_s_grab_needs_a_stored_release(self) -> None:
        """Grab sat in the phone's action bar on items never searched, where it could only answer an error."""
        self.assertTrue(self.rows.card(self.rows.searched).can_grab_best)
        never_searched = self.rows.card(self.rows.violent)
        self.assertTrue(never_searched.can_grab)
        self.assertFalse(never_searched.can_grab_best)
        found_nothing = cards.Card(MediaItem(id=1, status=_S.PENDING_REVIEW, title="A"), search={"search_id": 3, "found": 0, "title": None})
        self.assertFalse(found_nothing.can_grab_best)

    def test_a_type_delivered_elsewhere_has_no_release_to_grab(self) -> None:
        card = self.rows.card(self.rows.searched, to_client=False)
        self.assertFalse(card.can_grab or card.can_grab_best)
        self.assertTrue(card.can_approve and card.can_reject)

    def test_a_policy_rejection_can_be_overruled_and_run_again(self) -> None:
        card = self.rows.card(self.rows.rejected_by_policy)
        self.assertTrue(card.can_approve and card.can_rerun)
        self.assertEqual(card.approve_label, "Approve anyway")
        self.assertFalse(card.can_reject)
        person = self.rows.card(self.rows.one(_S.REJECTED, lambda i: i.reviewed_at is not None))
        self.assertFalse(person.can_approve or person.can_rerun or person.selectable)

    def test_an_item_without_a_title_is_not_searched(self) -> None:
        self.assertFalse(self.rows.card(self.rows.one(_S.PARSE_FAILED)).can_search)

    def test_the_reject_dialog_says_what_happens_to_the_post_in_the_bot_s_words(self) -> None:
        """It said "deleted or reacted to as its source's on_reject says" for every item."""
        from vettarr.bot import handlers

        for effect in ("delete", "react", "none"):
            card = self.rows.card(self.rows.violent, on_reject=effect)
            self.assertEqual(f"Reject this item? {card.reject_confirm}", handlers._REJECT_PROMPTS[effect])
        self.assertEqual(self.rows.card(self.rows.violent, on_reject="delete").reject_confirm, "Its post will be deleted.")

    def test_rejecting_a_post_nobody_could_read_keeps_it(self) -> None:
        unread = self.rows.card(self.rows.one(_S.PARSE_FAILED), on_reject="delete")
        self.assertEqual((unread.post_effect, unread.reject_confirm), ("none", "Its post stays where it is."))

    def test_the_reject_dialog_names_a_download_only_when_there_is_one(self) -> None:
        item = self.rows.downloading
        self.assertNotIn("download", self.rows.card(self.rows.violent, on_reject="delete").reject_confirm)
        ours = cards.Card(item, download=Download(media_item_id=item.id, created_torrent=True), on_reject="delete")
        self.assertEqual(ours.reject_confirm, "Its post will be deleted. The download this app started is removed with its files.")
        theirs = cards.Card(item, download=Download(media_item_id=item.id, created_torrent=False), on_reject="react")
        self.assertEqual(
            theirs.reject_confirm,
            "Its post will get a reaction. Its torrent was in the download client before this app added it, and stays there.",
        )
        sent = cards.Card(item, download=Download(media_item_id=item.id, delivery_target="sonarr", created_torrent=True))
        self.assertEqual(
            sent.reject_confirm, "Its post stays where it is. What this app added to Sonarr for it is taken back.",
        )

    def test_the_bulk_dialog_says_what_one_sentence_can_say_for_every_ticked_row(self) -> None:
        held, other, done = self.rows.violent, self.rows.searched, self.rows.one(_S.COMPLETED)

        def said(*loaded: cards.Card) -> str:
            return cards.Inbox(list(loaded), counts(), len(loaded), 1, 1, []).bulk_reject_confirm

        delete = lambda item: self.rows.card(item, on_reject="delete")  # noqa: E731
        self.assertEqual(said(delete(held), delete(other)), "Every post will be deleted.")
        self.assertEqual(said(self.rows.card(held, on_reject="react")), "Every post will get a reaction.")
        self.assertEqual(said(self.rows.card(held)), "Every post stays where it is.")
        self.assertEqual(said(delete(held), self.rows.card(other)), cards.REJECT_BULK_MIXED)
        # A row that cannot be rejected is not spoken for.
        self.assertEqual(said(delete(held), self.rows.card(done)), "Every post will be deleted.")
        self.assertEqual(
            said(delete(held), delete(self.rows.downloading)),
            "Every post will be deleted. A download this app started is removed with its files.",
        )

    def test_what_went_wrong_is_said_for_an_error_only(self) -> None:
        self.assertEqual(self.rows.card(self.rows.one(_S.ERROR)).failed, "The download client did not answer")
        self.assertTrue(self.rows.card(self.rows.one(_S.PARSE_FAILED)).failed)
        self.assertIsNone(self.rows.card(self.rows.rejected_by_policy).failed)

    def test_ages_are_short(self) -> None:
        self.assertEqual(cards.ago(NOW - timedelta(days=3, hours=2), NOW), "3 d ago")
        self.assertEqual(cards.ago(NOW - timedelta(hours=5), NOW), "5 h ago")
        self.assertEqual(cards.ago((NOW - timedelta(minutes=7)).replace(tzinfo=None), NOW), "7 min ago")
        self.assertEqual(cards.ago(NOW, NOW), "just now")
        self.assertEqual(cards.ago(None), "")
        self.assertEqual(cards.stamp(datetime(2026, 10, 2, 9, 5, tzinfo=timezone.utc)), "2 Oct 2026, 09:05 UTC")


class DetailTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.rows = Rows()

    def test_a_bar_per_imdb_category_with_every_label_s_votes(self) -> None:
        item = self.rows.violent
        bars = {bar.label: bar for bar in self.rows.detail(item).guide}
        self.assertEqual(list(bars), ["Nudity", "Violence", "Profanity", "Frightening", "Alcohol"])
        stored = item.signals["imdb.violence"]
        violence = bars["Violence"]
        self.assertEqual((violence.severity, violence.votes), ("severe", stored["votes"]))
        self.assertEqual([(name, votes) for name, votes, _ in violence.segments], list(stored["breakdown"].items()))
        self.assertAlmostEqual(sum(percent for _, _, percent in violence.segments), 100, delta=0.3)
        self.assertEqual(max(violence.segments, key=lambda s: s[1])[0], "severe")
        self.assertTrue(violence.spoken.startswith("Violence votes: none "))

    def test_missing_data_says_why_in_the_policy_s_words(self) -> None:
        bars = self.rows.detail(self.rows.one(_S.AWAITING_DATA)).guide
        self.assertEqual(len(bars), 5)
        for bar in bars:
            self.assertEqual((bar.severity, bar.segments), (None, ()))
            self.assertIn("parents guide not fetched", bar.note)

    def test_a_row_without_a_snapshot_keeps_its_rating_and_total(self) -> None:
        item = self.rows.violent
        old = MediaItem(id=1, status=_S.PENDING_REVIEW, media_type=MediaType.MOVIE, signals=None)
        bars = {bar.label: bar for bar in cards.guide_bars(old, item.content_check)}
        self.assertEqual(bars["Violence"].severity, "severe")
        self.assertEqual(bars["Violence"].votes, item.content_check.guide_votes["VIOLENCE"])
        self.assertEqual(bars["Violence"].segments, ())

    def test_a_book_has_no_bars(self) -> None:
        self.assertEqual(self.rows.detail(self.rows.one(_S.PENDING_REVIEW, lambda i: i.media_type is MediaType.COMIC)).guide, [])

    def test_certifications_are_listed_by_country(self) -> None:
        held = self.rows.one(_S.PENDING_REVIEW, lambda i: any(r["rule"] == "adult-certification" for r in i.reasons or []))
        self.assertEqual(self.rows.detail(held).certifications, [("GB", "18"), ("US", "NC-17")])

    def test_every_stored_search_is_kept_newest_first(self) -> None:
        item = self.rows.searched
        older = TorrentSearch(id=1, media_item_id=item.id, prowlarr_results=[], result_count=0)
        detail = cards.detail_of(item, searches=[item.torrent_searches[0], older])
        self.assertEqual([s.id for s in detail.searches], [item.torrent_searches[0].id, 1])
        self.assertEqual(detail.card.search["search_id"], item.torrent_searches[0].id)

    def test_the_history_is_the_item_s_events_between_its_own_dates(self) -> None:
        item = self.rows.downloading
        texts = [moment.text for moment in self.rows.detail(item).history]
        self.assertEqual(texts[0], f"Posted in {item.source_key}")
        self.assertEqual(texts[1], "Read")
        self.assertTrue(any(text.startswith("The policy decided to approve it: ") for text in texts))
        self.assertEqual(texts[-1], "Download started")
        times = [moment.at for moment in self.rows.detail(item).history]
        self.assertEqual(times, sorted(times))

    def test_a_person_s_decision_is_told_from_the_policy_s(self) -> None:
        person = self.rows.one(_S.REJECTED, lambda i: i.reviewed_at is not None)
        self.assertIn("Rejected by a person", [m.text for m in self.rows.detail(person).history])
        auto = self.rows.one(_S.APPROVED, lambda i: i.reviewed_at == i.auto_decided_at)
        self.assertNotIn("Approved by a person", [m.text for m in self.rows.detail(auto).history])

    def test_an_event_is_one_line(self) -> None:
        self.assertEqual(
            cards.describe(events.DECISION, {"outcome": "review", "reasons": ["imdb.violence: severe"]}),
            "The policy decided to hold it for a person: Violence (IMDb): severe",
        )
        self.assertEqual(
            cards.describe(events.WOULD_HAVE, {"outcome": "approve"}),
            "Shadow mode: the policy would have approved it, and left it to a person",
        )
        # Under shadow mode the policy decided one thing and another was done: the
        # line says what was done, and the would-have line beside it says the rest.
        self.assertEqual(
            cards.describe(events.DECISION, {
                "outcome": "approve", "done": "review", "reasons": ["shadow mode: would have approved"],
            }),
            "The policy decided to hold it for a person: shadow mode: would have approved",
        )
        self.assertEqual(
            cards.describe(events.DECISION, {"outcome": "approve", "done": "approve"}), "The policy decided to approve it",
        )
        self.assertEqual(cards.describe(events.NEEDS_YOU, None), "Sent to the review queue")
        self.assertEqual(cards.describe(events.BOT_ADDED, {}), "Sent to the bot")
        self.assertEqual(cards.describe("something_new", {}), "Something new")

    def test_a_post_is_linked_where_its_chat_has_an_address(self) -> None:
        item = MediaItem(id=1, telegram_chat_id=-1001234567890, telegram_message_id=42)
        self.assertEqual(cards.post_link(item, None), "https://t.me/c/1234567890/42")
        self.assertEqual(cards.post_link(item, SimpleNamespace(chat="@films")), "https://t.me/films/42")
        private = MediaItem(id=2, telegram_chat_id=999000002, telegram_message_id=7)
        self.assertIsNone(cards.post_link(private, SimpleNamespace(chat="private")))

    def test_only_web_addresses_in_a_post_become_links(self) -> None:
        item = MediaItem(id=1, status=_S.PENDING_REVIEW, external_links=[
            "https://example.org/a", "javascript:alert(1)", "magnet:?xt=urn:btih:abc", "http://example.org/b",
        ])
        self.assertEqual(cards.detail_of(item).links, ["https://example.org/a", "http://example.org/b"])


class ReviewOrderTests(unittest.TestCase):
    T = datetime(2026, 10, 1, tzinfo=timezone.utc)

    def setUp(self) -> None:
        # ids 5, 3 and 9, queued in that order; 3 and 9 at the same moment.
        self.queue = [(5, self.T), (3, self.T + timedelta(hours=1)), (9, self.T + timedelta(hours=1))]

    def test_with_no_anchor_the_oldest(self) -> None:
        self.assertEqual(cards.step(self.queue, None, True), 5)
        self.assertIsNone(cards.step([], None, True))
        self.assertIsNone(cards.step([], (5, self.T), True))

    def test_next_and_previous_follow_the_queue_and_wrap(self) -> None:
        at = dict(self.queue)
        self.assertEqual([cards.step(self.queue, (i, at[i]), True) for i in (5, 3, 9)], [3, 9, 5])
        self.assertEqual([cards.step(self.queue, (i, at[i]), False) for i in (5, 3, 9)], [9, 5, 3])

    def test_an_item_that_left_the_queue_still_has_a_next(self) -> None:
        # 4 was decided a moment ago: it sat between 5 and 3.
        gone = (4, self.T + timedelta(minutes=30))
        self.assertEqual(cards.step(self.queue, gone, True), 3)
        self.assertEqual(cards.step(self.queue, gone, False), 5)

    def test_the_only_item_waiting_is_its_own_neighbour(self) -> None:
        self.assertEqual(cards.step([(5, self.T)], (5, self.T), True), 5)
        self.assertEqual(cards.step([(5, self.T)], (5, self.T), False), 5)


# ---------------------------------------------------------------------------
# The Inbox
# ---------------------------------------------------------------------------


class PageCase(WebCase):
    """Signed out of the way: these tests are about the pages, not the gate."""

    AUTH = None

    @classmethod
    def setUpClass(cls) -> None:
        cls.rows = Rows()

    def inbox(self, items, **given):
        loaded = [self.rows.card(item) for item in items]
        values = dict(counts=counts(needs=18, waiting=6, no_release=4, done=29, all=60),
                      total=len(loaded), page=1, pages=1, sources=["demo-bot", "demo-group"])
        values.update(given)
        self.load_inbox.return_value = cards.Inbox(loaded, **values)

    def row_of(self, page: str, item_id: int) -> str:
        return re.search(rf'<article id="row-{item_id}".*?</article>', page, re.S).group(0)


class InboxPageTests(PageCase):
    def test_the_tabs_carry_their_counts_and_the_current_one_is_marked(self) -> None:
        self.inbox([self.rows.violent])
        page = self.get("/?tab=waiting").text
        tabs = re.findall(r'<a class="tab" href="([^"]+)" (aria-current="page")?>([^<]+)<span class="tab-count">(\d+)</span>', page)
        self.assertEqual([(href, bool(current), label.strip(), int(n)) for href, current, label, n in tabs], [
            ("/?tab=needs", False, "Needs you", 18), ("/?tab=waiting", True, "Waiting for data", 6),
            ("/?tab=no-release", False, "No release", 4), ("/?tab=done", False, "Decided", 29), ("/?tab=all", False, "All", 60),
        ])
        tab, filters, page_number, _per = self.load_inbox.await_args.args
        self.assertEqual((tab.key, filters, page_number), ("waiting", cards.Filters(), 1))

    def test_a_row_shows_what_the_plan_lists(self) -> None:
        # The shelf: a poster with the verdict stamped on its corner, then the
        # title, a byline and the reason. Size, releases and the guide are the
        # item's, one tap away.
        item = self.rows.searched
        self.inbox([item])
        row = self.row_of(self.get("/").text, item.id)
        self.assertIn(f'<img class="poster" src="{item.content_check.poster_url}" alt=""', row)
        self.assertRegex(row, rf'<a class="tap" href="/item/{item.id}"[^>]*>{re.escape(esc(item.title))}</a>')
        self.assertIn(f"<span>{item.year}</span>", row)
        self.assertIn(f'<span class="badge badge-{item.media_type.value.lower()}">', row)
        # The open tab already says "Needs you": every row repeating it was noise.
        self.assertNotIn(">Needs you</span>", row)
        # The verdict is a mark and a word; a screen reader hears "Policy: review".
        self.assertIn('<span class="stamp stamp-warn"><span class="sr-only">Policy: </span>review</span>', row)

    def test_a_poster_missing_is_the_type_s_tile_not_a_broken_image(self) -> None:
        item = self.rows.one(_S.PARSE_FAILED)
        self.inbox([item])
        row = self.row_of(self.get("/?tab=all").text, item.id)
        self.assertNotIn("<img", row)
        self.assertRegex(row, r'<span class="poster poster-none" aria-hidden="true"><svg [^>]*aria-hidden="true">')

    def test_a_row_says_its_reason_in_words_once(self) -> None:
        item = self.rows.violent
        self.inbox([item])
        row = self.row_of(self.get("/").text, item.id)
        # The reason in words, once: no rule tag beside the signal that says the same.
        self.assertIn("Violence (IMDb): severe", row)
        self.assertNotIn('<span class="tag">violence</span>', row)
        # The guide's ratings are the item's: a poster with five pills under it was the noise.
        self.assertNotIn('class="pill', row)

    def test_a_row_whose_guide_is_all_none_says_so_in_one_pill(self) -> None:
        card = self.rows.card(self.rows.violent)
        card.check = SimpleNamespace(**{column: "none" for _, column, _, _ in cards.GUIDE}, poster_url=None)
        self.assertEqual(card.row_pills, [("Parents guide", "none")])
        self.assertEqual(len(card.pills), 5)

    def test_a_row_has_inline_approve_reject_and_search_answered_into_the_row(self) -> None:
        item = self.rows.violent
        self.inbox([item])
        row = self.row_of(self.get("/").text, item.id)
        for action in ("approve", "reject", "search"):
            button = re.search(rf'<button[^>]*hx-post="/inbox/{item.id}/{action}"[^>]*>', row, re.S).group(0)
            self.assertIn(f'hx-target="#row-{item.id}"', button)
            self.assertIn('hx-swap="outerHTML"', button)
        reject = re.search(rf'<button[^>]*hx-post="/inbox/{item.id}/reject"[^>]*>', row, re.S).group(0)
        self.assertIn(f'data-confirm-title="Reject {esc(item.title)}?"', reject)

    def test_a_row_offers_only_what_its_status_allows(self) -> None:
        downloading, done, unread = self.rows.downloading, self.rows.one(_S.COMPLETED), self.rows.one(_S.PARSE_FAILED)
        self.inbox([downloading, done, unread, self.rows.rejected_by_policy])
        page = self.get("/?tab=all").text
        offered = lambda item: set(re.findall(rf'hx-post="/inbox/{item.id}/(\w+)"', self.row_of(page, item.id)))  # noqa: E731
        self.assertEqual(offered(downloading), {"reject"})
        self.assertEqual(offered(done), set())
        # A post nothing could read has no title to approve: it is dismissed, or a format is taught.
        self.assertEqual(offered(unread), {"reject"})
        self.assertIn(f'href="/setup?try={unread.id}#try-a-post">Teach a format</a>', self.row_of(page, unread.id))
        self.assertIn('data-confirm-action="Dismiss">Dismiss</button>', self.row_of(page, unread.id))
        self.assertEqual(offered(self.rows.rejected_by_policy), {"approve"})
        self.assertIn(">Approve anyway</button>", self.row_of(page, self.rows.rejected_by_policy.id))
        self.assertNotIn('name="ids"', self.row_of(page, done.id))
        self.assertIn("Select all 3 on this page", page)
        self.assertIn('data-confirm="Its post stays where it is."', self.row_of(page, unread.id))
        self.assertIn(f'data-confirm="{self.rows.card(downloading).reject_confirm}"', self.row_of(page, downloading.id))
        self.assertIn("The download this app started is removed with its files.", self.row_of(page, downloading.id))
        self.assertIn(f"Downloading {round(downloading.download.progress * 100)}%", self.row_of(page, downloading.id))

    def test_the_filters_apply_on_change_and_each_has_a_label(self) -> None:
        self.inbox([self.rows.violent])
        page = self.get("/?tab=needs&type=movie&source=demo-group&outcome=review&page=3").text
        form = re.search(r'<form class="filters".*?</form>', page, re.S).group(0)
        for attribute in ('hx-get="/"', 'hx-trigger="change"', 'hx-target="#inbox-list"', 'hx-select="#inbox-list"',
                          'hx-select-oob="#inbox-tabs"', 'hx-push-url="true"',
                          # No copy of the list in sessionStorage, and Back reads it from the server.
                          'hx-history="false"'):
            self.assertIn(attribute, form)
        self.assertEqual(re.findall(r'<select class="select" name="(\w+)"', form), ["type", "source", "outcome"])
        self.assertEqual(len(re.findall(r"<label>\w[\w ]*\s+<select ", form)), 3)
        for chosen in ('<option value="movie" selected>', '<option value="demo-group" selected>', '<option value="review" selected>'):
            self.assertIn(chosen, form)
        self.assertIn('<input type="hidden" name="tab" value="needs">', form)
        tab, filters, page_number, _per = self.load_inbox.await_args.args
        self.assertEqual(filters, cards.Filters(type="movie", source="demo-group", outcome="review"))
        self.assertEqual(page_number, 3)
        # The tabs keep the filters.
        self.assertIn('href="/?type=movie&amp;source=demo-group&amp;outcome=review&amp;tab=done"', page)

    def test_the_list_says_its_order_and_pages_keep_the_filters(self) -> None:
        self.inbox([self.rows.violent], total=60, page=2, pages=3)
        page = self.get("/?tab=needs&type=movie&page=2").text
        self.assertIn("60 items, filtered · oldest first", page)
        self.assertIn('href="/?type=movie&amp;tab=needs&amp;page=1"', page)
        self.assertIn('href="/?type=movie&amp;tab=needs&amp;page=3"', page)
        self.assertIn("Page 2 of 3", page)
        self.inbox([self.rows.one(_S.COMPLETED)])
        self.assertIn("1 item · newest first", self.get("/?tab=done").text)

    def test_rows_can_be_ticked_and_acted_on_together(self) -> None:
        item = self.rows.violent
        self.inbox([item])
        page = self.get("/").text
        self.assertIn(f'<input type="checkbox" name="ids" value="{item.id}"><span class="sr-only">Select {esc(item.title)}</span>', page)
        self.assertIn("<input type=\"checkbox\" data-select-all> Select all 1 on this page", page)
        bar = re.search(r'<div id="bulk-bar" class="bulk-bar" hidden>.*?</div>', page, re.S).group(0)
        self.assertIn('<span data-count role="status">', bar)
        self.assertEqual(re.findall(r'hx-post="/bulk/(\w+)"', bar), ["approve", "reject"])
        # Without the script the title is this; with it, the title counts the ticked rows.
        self.assertIn('data-confirm-title="Reject the selected items?"', bar)
        self.assertIn('data-confirm="Every post stays where it is."', bar)
        script = (WEB_DIR / "static" / "app.js").read_text()
        self.assertIn("dataset.confirmTitle = 'Reject ' + ticked + (ticked === 1 ? ' item?' : ' items?')", script)
        css = (WEB_DIR / "static" / "src" / "app.css").read_text()
        self.assertRegex(css, r"\.bulk-bar \{\s*position: sticky;")

    def test_the_bulk_dialog_names_what_the_page_s_rows_lose(self) -> None:
        self.inbox([self.rows.violent, self.rows.downloading])
        self.load_inbox.return_value.cards = [
            self.rows.card(self.rows.violent, on_reject="delete"), self.rows.card(self.rows.downloading, on_reject="delete"),
        ]
        bar = re.search(r'<div id="bulk-bar" class="bulk-bar" hidden>.*?</div>', self.get("/?tab=all").text, re.S).group(0)
        self.assertIn(
            'data-confirm="Every post will be deleted. A download this app started is removed with its files."', bar,
        )

    def test_a_page_number_that_is_not_one_is_the_first_page(self) -> None:
        """/?page=0 and /?page=abc answered FastAPI's JSON error as a whole page."""
        self.inbox([self.rows.violent])
        for asked, page_number in (("0", 1), ("abc", 1), ("-2", 1), ("99999999999999", 1), ("²", 1), ("", 1), ("7", 7)):
            with self.subTest(page=asked):
                resp = self.get("/", params={"page": asked})
                self.assertEqual(resp.status_code, 200)
                self.assertEqual(self.load_inbox.await_args.args[2], page_number)

    def test_the_page_size_is_what_the_browser_measured(self) -> None:
        """A page was 25 rows whatever the screen: a cut line of posters, or a scroll."""
        self.inbox([self.rows.violent])
        for cookie, per in (("21", 21), ("28", 28), ("1", 1), ("120", 120), ("0", 25), ("121", 25), ("abc", 25),
                            ("", 25), ("1e2", 25), ("1000", 25)):
            with self.subTest(per=cookie):
                self.client.cookies.set("per", cookie)
                resp = self.get("/")
                self.assertEqual(resp.status_code, 200)
                self.assertEqual(self.load_inbox.await_args.args[3], per)
        self.client.cookies.delete("per")
        resp = self.get("/")
        self.assertEqual(self.load_inbox.await_args.args[3], 25)

    def test_the_list_says_the_size_it_was_built_with(self) -> None:
        self.inbox([self.rows.violent], per_page=21)
        self.assertIn('<div class="rows" data-per="21">', self.get("/").text)

    def test_the_shelf_holds_the_whole_lines_that_fit_the_screen(self) -> None:
        """The browser measures the shelf, keeps the number in a cookie and reads the list
        again when it got one sized for another screen. What it does is measured in
        a browser by scripts/checks.js; this is the text of the script."""
        script = (WEB_DIR / "static" / "app.js").read_text()
        self.assertIn("const columns = style.gridTemplateColumns.split(' ').length;", script)
        self.assertIn("const lines = Math.max(columns <= 2 ? 5 : 1, Math.floor((room + gap) / (line + gap)));", script)
        self.assertIn("const per = Math.min(120, columns * lines);", script)
        self.assertIn("document.cookie = 'per=' + per + '; path=/; max-age=31536000; SameSite=Lax';", script)
        # Sized for this screen already, or everything is on the page: nothing to read again.
        self.assertIn("if (per === built || (!document.querySelector('.pager') && rows.childElementCount <= per)) return;", script)
        # Never under a ticked or a leaving row.
        self.assertIn("if (document.querySelector('input[name=\"ids\"]:checked, .row-left')) return;", script)
        # The first item in sight stays the first.
        self.assertIn("const page = Math.floor(((Number(url.searchParams.get('page')) || 1) - 1) * built / per) + 1;", script)
        self.assertIn("window.addEventListener('resize'", script)
        self.assertIn("document.addEventListener('DOMContentLoaded', fit);", script)

    def test_on_a_phone_every_tab_is_in_sight(self) -> None:
        # The text of the stylesheet only. That the open tab is inside a 390 px
        # screen on every tab is measured in a browser by scripts/checks.js.
        css = (WEB_DIR / "static" / "src" / "app.css").read_text()
        phone = css[css.index("/* All five tabs in sight"):]
        self.assertRegex(phone, r"\.tabs \{\s*flex-wrap: wrap;\s*overflow-x: visible;\s*\}")

    def test_an_empty_tab_says_so(self) -> None:
        self.inbox([], counts=counts())
        self.assertIn("Nothing needs you.", self.get("/").text)
        page = self.get("/?tab=waiting&type=comic").text
        self.assertIn("Nothing is waiting for data.", page)
        self.assertIn('<a class="link" href="/?tab=waiting">Clear the filters</a>', page)
        self.assertNotIn("data-select-all", page)

    def test_a_failing_integration_is_a_banner_over_the_inbox(self) -> None:
        self.failing.return_value = [shell.Failing("northwind", "the session cookie was refused", 48)]
        self.inbox([self.rows.violent])
        self.assertIn("<strong>Not working:</strong> northwind.", self.get("/").text)

    def test_the_title_opens_the_item_in_the_slide_over(self) -> None:
        item = self.rows.violent
        self.inbox([item])
        page = self.get("/").text
        self.assertIn(f'hx-get="/item/{item.id}" hx-target="#drawer-body" hx-swap="innerHTML"', self.row_of(page, item.id))
        self.assertEqual(page.count('<dialog id="drawer"'), 1)
        self.assertRegex(page, r'<dialog id="drawer"[^>]*>\s*<form method="dialog"[^>]*><button[^>]*>Close</button></form>\s*<div id="drawer-body"></div>')

    def test_a_row_reads_itself_again_when_its_item_changes(self) -> None:
        item = self.rows.violent
        self.inbox([item])
        row = self.row_of(self.get("/").text, item.id)
        self.assertIn(f'hx-get="/inbox/row/{item.id}" hx-trigger="item-{item.id} from:body" hx-swap="outerHTML"', row)


class InboxActionTests(PageCase):
    def setUp(self) -> None:
        super().setUp()
        self.item = self.rows.violent
        self.load_rows.return_value = ([self.rows.card(self.item)], counts(needs=17, waiting=6, done=30, all=60))

    def post_row(self, action: str, result, name: str | None = None):
        with acting(name or action, result) as ran:
            resp = self.post(f"/inbox/{self.item.id}/{action}", headers=FROM_INBOX)
        return resp, ran

    def test_an_action_answers_with_the_row_and_the_new_counts(self) -> None:
        resp, ran = self.post_row("approve", ok("Queued The Lantern Keeper for download."))
        self.assertEqual(resp.status_code, 200)
        ran.assert_awaited_once_with(self.item.id)
        self.assertTrue(resp.text.lstrip().startswith(f'<article id="row-{self.item.id}" class="row'))
        self.assertNotIn("<html", resp.text)
        self.assertNotRegex(resp.text, r"<article[^>]*hx-swap-oob")
        self.assertRegex(resp.text, r'<nav id="inbox-tabs"[^>]*hx-swap-oob="true"')
        self.assertIn('Needs you <span class="tab-count">17</span>', resp.text)
        self.assertEqual(toast(resp), {"text": "Queued The Lantern Keeper for download.", "kind": "ok"})

    def test_the_counts_are_those_of_the_page_the_row_is_on(self) -> None:
        self.post_row("approve", ok())
        ids, filters = self.load_rows.await_args.args
        self.assertEqual((list(ids), filters), ([self.item.id], cards.Filters(type="movie", source="demo-group")))
        resp, _ = self.post_row("approve", ok())
        self.assertRegex(resp.text, r'href="/\?type=movie&amp;source=demo-group&amp;tab=waiting" aria-current="page"')

    def test_reject_reaches_the_post_actions_and_search_runs_the_search(self) -> None:
        self.app.state.posts = posts = object()
        _, ran = self.post_row("reject", ok("Rejected, and its post was deleted."))
        ran.assert_awaited_once_with(self.item.id, posts)
        found = actions.SearchOutcome(self.item.id, actions.Status.OK, "Found 3 releases.", search_id=5)
        resp, ran = self.post_row("search", found)
        ran.assert_awaited_once_with(self.item.id)
        self.assertEqual(toast(resp), {"text": "Found 3 releases.", "kind": "ok"})

    def test_a_busy_item_is_a_409_whose_body_is_the_toast(self) -> None:
        for action in ("approve", "reject", "search"):
            with self.subTest(action=action):
                resp, _ = self.post_row(action, BUSY)
                self.assertEqual((resp.status_code, resp.text), (409, actions.BUSY_MESSAGE))
                self.assertNotIn("HX-Trigger", resp.headers)
        self.load_rows.assert_not_awaited()
        # htmx swaps nothing on a 4xx; the layout's script shows the body.
        script = (WEB_DIR / "static" / "app.js").read_text()
        self.assertRegex(script, r"addEventListener\('htmx:responseError', \(e\) => \{\s*const text = e\.detail\.xhr\.responseText")

    def test_a_search_that_cannot_run_is_a_422_and_a_gone_item_a_404(self) -> None:
        resp, _ = self.post_row("search", NO_TITLE)
        self.assertEqual((resp.status_code, resp.text), (422, "This item has no title to search for."))
        resp, _ = self.post_row("approve", GONE)
        self.assertEqual((resp.status_code, resp.text), (404, actions.NOT_FOUND_MESSAGE))

    def test_a_search_that_ran_and_failed_shows_the_row_and_an_error_toast(self) -> None:
        down = actions.SearchOutcome(self.item.id, actions.Status.FAILED, "Prowlarr search unavailable.", ran=True)
        resp, _ = self.post_row("search", down)
        self.assertEqual(resp.status_code, 200)
        self.assertIn(f'<article id="row-{self.item.id}"', resp.text)
        self.assertEqual(toast(resp), {"text": "Prowlarr search unavailable.", "kind": "error"})

    def test_any_other_word_is_not_an_action(self) -> None:
        with acting("grab", ok()) as grab, acting("skip", ok()) as skip:
            for word in ("grab", "skip", "rerun", "delete"):
                self.assertEqual(self.post(f"/inbox/{self.item.id}/{word}", headers=FROM_INBOX).status_code, 404)
        grab.assert_not_awaited()
        skip.assert_not_awaited()

    def test_an_id_no_row_can_have_is_a_missing_item_not_a_server_error(self) -> None:
        """An id past int4 reached the database and came back as a 500."""
        with acting("approve", ok()) as ran:
            for url in ("/inbox/2147483648/approve", "/inbox/99999999999/approve", "/inbox/abc/approve", "/inbox/0/approve"):
                with self.subTest(url=url):
                    resp = self.post(url, headers=FROM_INBOX)
                    self.assertEqual((resp.status_code, resp.text), (404, actions.NOT_FOUND_MESSAGE))
        ran.assert_not_awaited()
        self.load_rows.reset_mock()
        self.assertEqual(self.get("/inbox/row/2147483648", headers=FROM_INBOX).status_code, 404)
        self.load_rows.assert_not_awaited()
        # The largest id there is still goes through.
        with acting("approve", ok()) as ran:
            self.assertEqual(self.post("/inbox/2147483647/approve", headers=FROM_INBOX).status_code, 200)
        ran.assert_awaited_once_with(2147483647)

    def test_the_row_fragment_alone(self) -> None:
        resp = self.get(f"/inbox/row/{self.item.id}", headers=FROM_INBOX)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.text.lstrip().startswith(f'<article id="row-{self.item.id}"'))
        self.assertNotIn("HX-Trigger", resp.headers)
        # An item that is gone: no row comes back, so the row leaves the list.
        self.load_rows.return_value = ([], counts())
        gone = self.get("/inbox/row/999", headers=FROM_INBOX)
        self.assertEqual(gone.status_code, 200)
        self.assertNotIn("<article", gone.text)


class BulkTests(PageCase):
    def setUp(self) -> None:
        super().setUp()
        self.items = [self.rows.violent, self.rows.searched]
        self.ids = [item.id for item in self.items]
        self.load_rows.return_value = ([self.rows.card(item) for item in self.items], counts(needs=16, done=31, all=60))

    def send(self, action: str, ids=None):
        return self.post(f"/bulk/{action}", data={"ids": [str(i) for i in (self.ids if ids is None else ids)]}, headers=FROM_INBOX)

    def test_approving_several_answers_with_every_row_out_of_band(self) -> None:
        with mock.patch.object(actions, "approve_many", mock.AsyncMock(return_value=self.ids)) as ran:
            resp = self.send("approve")
        self.assertEqual(resp.status_code, 200)
        ran.assert_awaited_once_with(self.ids)
        for item_id in self.ids:
            self.assertRegex(resp.text, rf'<article id="row-{item_id}" class="row[^"]*" data-id="{item_id}" hx-swap-oob="true"')
        self.assertRegex(resp.text, r'<nav id="inbox-tabs"[^>]*hx-swap-oob="true"')
        self.assertEqual(toast(resp), {"text": "Queued 2 for download.", "kind": "ok"})

    def test_a_dry_run_says_it_would_have_queued_them(self) -> None:
        with mock.patch.object(actions, "approve_many", mock.AsyncMock(return_value=self.ids)), \
                mock.patch.dict(os.environ, {"DRY_RUN": "1"}):
            self.assertEqual(toast(self.send("approve"))["text"], "Would have queued 2 for download.")

    def test_what_was_left_out_is_said(self) -> None:
        with mock.patch.object(actions, "approve_many", mock.AsyncMock(return_value=self.ids[:1])):
            self.assertEqual(
                toast(self.send("approve"))["text"],
                "Queued 1 for download. 1 was left as it is: already decided or being processed.",
            )
        with mock.patch.object(actions, "approve_many", mock.AsyncMock(return_value=[])):
            self.assertEqual(toast(self.send("approve"))["kind"], "error")

    def test_rejecting_several_rejects_each_and_one_failure_does_not_stop_the_rest(self) -> None:
        self.app.state.posts = posts = object()
        outcomes = [RuntimeError("telegram is down"), ok("Rejected.")]
        with mock.patch.object(actions, "reject", mock.AsyncMock(side_effect=outcomes)) as ran:
            resp = self.send("reject")
        self.assertEqual([call.args for call in ran.await_args_list], [(i, posts) for i in self.ids])
        self.assertEqual(toast(resp)["text"], "Rejected 1. 1 was left as it is: already decided or being processed.")

    def test_a_rejection_its_delivery_target_did_not_hear_of_is_an_error_that_says_which(self) -> None:
        note = "Radarr could not be told and still has the title."
        outcomes = [
            actions.RejectResult(self.ids[0], actions.Status.OK, f"Rejected. {note}", remote_note=note, remote_failed=True),
            actions.RejectResult(self.ids[1], actions.Status.OK, "Rejected. Radarr no longer monitors it.",
                                 remote_note="Radarr no longer monitors it.", remote_failed=False),
        ]
        with mock.patch.object(actions, "reject", mock.AsyncMock(side_effect=outcomes)):
            resp = self.send("reject")
        self.assertEqual(toast(resp), {"text": f"Rejected 2. {note}", "kind": "error"})

    def test_ids_are_numbers_once_each_and_capped(self) -> None:
        with mock.patch.object(actions, "approve_many", mock.AsyncMock(return_value=[])) as ran:
            # The last three are numbers no row can have; one of them used to fail the whole batch.
            resp = self.send("approve", ids=["7", "7", "x", "-3", "8", "1 or 1=1", "²", "", "0", "2147483648", "9" * 40])
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(ran.await_args.args[0], [7, 8])
            self.send("approve", ids=["2147483647"])
            self.assertEqual(ran.await_args.args[0], [2147483647])
            self.send("approve", ids=range(1, 500))
            self.assertEqual(len(ran.await_args.args[0]), 100)

    def test_nothing_ticked_is_a_422_and_nothing_runs(self) -> None:
        with mock.patch.object(actions, "approve_many", mock.AsyncMock()) as approve, \
                mock.patch.object(actions, "reject", mock.AsyncMock()) as reject:
            for action in ("approve", "reject"):
                resp = self.send(action, ids=[])
                self.assertEqual((resp.status_code, resp.text), (422, "Select at least one item."))
        approve.assert_not_awaited()
        reject.assert_not_awaited()


class BulkWithoutASessionTests(WebCase):
    def test_a_lost_session_sends_the_whole_page_to_login_and_nothing_runs(self) -> None:
        with mock.patch.object(actions, "approve_many", mock.AsyncMock()) as ran:
            resp = self.post("/bulk/approve", data={"ids": ["7"]}, headers={
                **HTMX, "HX-Current-URL": "http://testserver/?tab=waiting",
            })
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.headers["HX-Redirect"], "/login?next=/%3Ftab%3Dwaiting")
        ran.assert_not_awaited()

    def test_another_site_s_bulk_post_is_refused(self) -> None:
        self.sign_in()
        with mock.patch.object(actions, "reject", mock.AsyncMock()) as ran:
            resp = self.post("/bulk/reject", data={"ids": ["7"]}, headers={**HTMX, "Sec-Fetch-Site": "cross-site"})
        self.assertEqual(resp.status_code, 403)
        ran.assert_not_awaited()


# ---------------------------------------------------------------------------
# Review
# ---------------------------------------------------------------------------


class ReviewTests(PageCase):
    def setUp(self) -> None:
        super().setUp()
        self.item = self.rows.searched
        self.next = self.rows.violent
        self.show(self.item, 3, 14)

    def show(self, item, position=None, total=0) -> None:
        self.load_review.return_value = cards.Review(self.rows.card(item) if item else None, position, total)

    def act(self, action: str, result, name: str | None = None, **patched):
        with acting(name or action, result) as ran:
            resp = self.post(f"/review/{self.item.id}/{action}", headers=HTMX)
        return resp, ran

    def test_one_item_with_its_place_in_the_queue(self) -> None:
        page = self.get("/review").text
        self.assertEqual(page.count('id="review-card"'), 1)
        self.assertIn('3 of 14<span class="review-order"> · decisions first, oldest first</span>', page)
        self.assertIn(esc(self.item.title), page)
        self.assertIn('<span class="tag">', page)
        self.assertIn(self.item.torrent_searches[0].prowlarr_results[0]["title"], page)
        self.assertIn(esc(self.item.content_check.plot), page)
        self.load_review.assert_awaited_once_with(item=None, after=None, before=None)

    def test_the_keyboard_map(self) -> None:
        page = self.get("/review").text
        i = self.item.id
        on = {key: tag for tag, key in re.findall(r'(<(?:a|button|summary)[^>]*data-key="([^"]+)"[^>]*>)', page, re.S)}
        self.assertEqual(sorted(on), sorted(key for key, _ in review_routes.KEYS))
        self.assertEqual(sorted(on), ["?", "a", "g", "j", "k", "o", "r", "s"])
        for key, action in (("a", "approve"), ("r", "reject"), ("s", "search"), ("g", "grab")):
            self.assertIn(f'hx-post="/review/{i}/{action}"', on[key])
            self.assertIn('hx-target="#review-card"', on[key])
        self.assertIn("data-confirm=", on["r"])
        self.assertIn(f'hx-get="/review?after={i}"', on["j"])
        self.assertIn(f'hx-get="/review?before={i}"', on["k"])
        self.assertIn(f'hx-get="/item/{i}" hx-target="#drawer-body"', on["o"])
        self.assertTrue(on["?"].startswith("<summary"))

    def test_the_legend_is_on_the_page_and_names_every_key(self) -> None:
        page = self.get("/review").text
        legend = re.search(r'<details class="keys" open>.*?</details>', page, re.S).group(0)
        listed = re.findall(r'<dt><kbd class="kbd">(.)</kbd></dt><dd>([^<]+)</dd>', legend)
        self.assertEqual(listed, [(key.upper(), what) for key, what in review_routes.KEYS])
        # Each button says its key too.
        for key in "ARSGJKO":
            self.assertIn(f'<kbd class="kbd">{key}</kbd>', page.replace(legend, ""))

    def test_the_key_handler_s_text(self) -> None:
        """The text of static/app.js, and nothing more: no browser runs in this suite.

        It keeps the guards from being deleted by accident. What a key does
        (one click per press, none for a held key's repeats, none in a field,
        under a dialog or with Ctrl) is measured in a browser by
        scripts/checks.js, which no workflow runs yet: docs/DEV.md says to run
        it on every pull request that touches the script, a template or the
        stylesheet. A held A key approved 13 items in a second while this
        test, then named after the behaviour, passed.
        """
        script = (WEB_DIR / "static" / "app.js").read_text()
        self.assertIn("""document.querySelector('[data-key="' + CSS.escape(key) + '"]:not(:disabled)')""", script)
        handler = re.search(r"addEventListener\('keydown', \(e\) => \{(.*?)\n  \}\);", script, re.S).group(1)
        # The early return: a repeat, a modifier, a key that is not a character, an open dialog.
        self.assertIn("if (e.repeat || e.ctrlKey || e.metaKey || e.altKey || e.key.length !== 1 || modal()) return;", handler)
        self.assertIn("if (e.repeat && e.key === 'Enter' && e.target.closest('button, a, summary')) e.preventDefault();", handler)
        self.assertIn("e.target.closest('input, textarea, select, [contenteditable]')", handler)
        self.assertIn("press(e.key.toLowerCase())", handler)
        # The return comes before the click.
        self.assertLess(handler.index("e.repeat ||"), handler.index("press("))

    def test_an_item_with_no_stored_release_offers_no_grab(self) -> None:
        self.show(self.next, 4, 14)
        page = self.get("/review").text
        card = re.search(r'<section id="review-card".*?</section>', page, re.S).group(0)
        self.assertNotIn('data-key="g"', card)
        self.assertNotIn("/grab", card)
        self.assertEqual(re.findall(r'hx-post="/review/\d+/(\w+)"', card), ["approve", "reject", "search"])
        # The route still answers a Grab sent from a card drawn earlier.
        with acting("latest_search", None):
            self.assertEqual(self.post(f"/review/{self.next.id}/grab", headers=HTMX).status_code, 422)

    def test_an_address_no_item_can_have_is_a_missing_item(self) -> None:
        self.load_review.reset_mock()
        for url in ("/review?item=2147483648", "/review?after=99999999999", "/review?before=abc", "/review?item=0"):
            with self.subTest(url=url):
                whole = self.get(url)
                self.assertEqual(whole.status_code, 404)
                self.assertIn("This item no longer exists.", whole.text)
                self.assertIn("<html", whole.text)
                part = self.get(url, headers=HTMX)
                self.assertEqual((part.status_code, part.text), (404, actions.NOT_FOUND_MESSAGE))
        with acting("approve", ok()) as ran:
            resp = self.post("/review/2147483648/approve", headers=HTMX)
        self.assertEqual((resp.status_code, resp.text), (404, actions.NOT_FOUND_MESSAGE))
        ran.assert_not_awaited()
        self.load_review.assert_not_awaited()

    def test_a_phone_gets_a_bottom_action_bar_and_swipes(self) -> None:
        page = self.get("/review").text
        self.assertRegex(page, r'<section id="review-card" class="review" data-swipe')
        self.assertIn("Swipe left for the next item, right for the previous one.", page)
        css = (WEB_DIR / "static" / "src" / "app.css").read_text()
        phone = css[css.index("/* Room for the action bar"):]
        self.assertRegex(phone, r"\.review-actions \{\s*position: fixed;\s*inset: auto 0 calc\(3\.5625rem \+ env\(safe-area-inset-bottom\)\) 0;")
        script = (WEB_DIR / "static" / "app.js").read_text()
        self.assertIn("window.innerWidth < 640 && e.target.closest('[data-swipe]')", script)
        self.assertIn("press(dx < 0 ? 'j' : 'k')", script)
        # Next and Previous are buttons as well, so nothing needs the swipe.
        self.assertIn("Previous <kbd", page)
        self.assertIn("Next <kbd", page)

    def test_next_and_previous_answer_htmx_with_the_card_alone(self) -> None:
        self.show(self.next, 4, 14)
        resp = self.get(f"/review?after={self.item.id}", headers=HTMX)
        self.assertTrue(resp.text.lstrip().startswith('<section id="review-card"'))
        self.assertNotIn("<html", resp.text)
        self.assertIn(esc(self.next.title), resp.text)
        self.assertNotIn("HX-Trigger", resp.headers)
        self.load_review.assert_awaited_with(item=None, after=self.item.id, before=None)
        self.get(f"/review?before={self.item.id}", headers=HTMX)
        self.load_review.assert_awaited_with(item=None, after=None, before=self.item.id)

    def test_next_on_the_only_item_says_so(self) -> None:
        self.show(self.item, 1, 1)
        resp = self.get(f"/review?after={self.item.id}", headers=HTMX)
        self.assertEqual(toast(resp), {"text": review_routes.ALONE, "kind": "ok"})

    def test_a_decision_swaps_the_next_item_in_with_a_toast(self) -> None:
        self.show(self.next, 3, 13)
        for action, said in (("approve", "Queued it for download."), ("reject", "Rejected, and its post was deleted.")):
            with self.subTest(action=action):
                resp, ran = self.act(action, ok(said))
                self.assertEqual(resp.status_code, 200)
                self.assertTrue(resp.text.lstrip().startswith('<section id="review-card"'))
                self.assertIn(esc(self.next.title), resp.text)
                self.assertEqual(toast(resp), {"text": said, "kind": "ok"})
                self.load_review.assert_awaited_with(after=self.item.id)
        ran.assert_awaited_once_with(self.item.id, self.app.state.posts)

    def test_a_search_keeps_the_item_and_shows_what_it_found(self) -> None:
        found = actions.SearchOutcome(self.item.id, actions.Status.OK, "Found 3 releases.", search_id=5)
        resp, _ = self.act("search", found)
        self.load_review.assert_awaited_with(item=self.item.id)
        self.assertIn(esc(self.item.title), resp.text)
        self.assertEqual(toast(resp), {"text": "Found 3 releases.", "kind": "ok"})

    def test_grab_takes_the_best_release_of_the_latest_search_and_moves_on(self) -> None:
        stored = SimpleNamespace(id=55, prowlarr_results=[{"title": "best"}, {"title": "second"}])
        with acting("latest_search", stored), acting("grab", ok("Downloading best.")) as grab:
            resp = self.post(f"/review/{self.item.id}/grab", headers=HTMX)
        grab.assert_awaited_once_with(self.item.id, 55, 0)
        self.load_review.assert_awaited_with(after=self.item.id)
        self.assertEqual(toast(resp), {"text": "Downloading best.", "kind": "ok"})

    def test_grab_with_nothing_stored_is_a_422_that_says_to_search(self) -> None:
        for stored in (None, SimpleNamespace(id=55, prowlarr_results=[])):
            with acting("latest_search", stored), acting("grab", ok()) as grab:
                resp = self.post(f"/review/{self.item.id}/grab", headers=HTMX)
            self.assertEqual((resp.status_code, resp.text), (422, review_routes.NOTHING_STORED))
            grab.assert_not_awaited()

    def test_a_grab_the_client_refused_keeps_the_item_and_says_so(self) -> None:
        refused = actions.ActionResult(self.item.id, actions.Status.FAILED, "qBittorrent did not take the torrent.")
        stored = SimpleNamespace(id=55, prowlarr_results=[{"title": "best"}])
        with acting("latest_search", stored), acting("grab", refused):
            resp = self.post(f"/review/{self.item.id}/grab", headers=HTMX)
        self.load_review.assert_awaited_with(item=self.item.id)
        self.assertEqual(toast(resp), {"text": "qBittorrent did not take the torrent.", "kind": "error"})

    def test_a_busy_item_is_a_409_toast_and_the_card_stays(self) -> None:
        self.load_review.reset_mock()
        for action in ("approve", "reject", "search"):
            resp, _ = self.act(action, BUSY)
            self.assertEqual((resp.status_code, resp.text), (409, actions.BUSY_MESSAGE))
        self.assertEqual(self.act("search", NO_TITLE)[0].status_code, 422)
        self.assertEqual(self.post(f"/review/{self.item.id}/skip", headers=HTMX).status_code, 404)
        self.load_review.assert_not_awaited()

    def test_nothing_to_review(self) -> None:
        self.show(None)
        page = self.get("/review").text
        self.assertIn("Nothing to review.", page)
        self.assertIn('<a class="btn btn-secondary mt-4" href="/">Open the Inbox</a>', page)
        self.assertNotIn("hx-post", page)
        # The last decision of a session lands here too, with its toast.
        resp, _ = self.act("approve", ok("Queued it for download."))
        self.assertIn("Nothing to review.", resp.text)
        self.assertEqual(toast(resp)["text"], "Queued it for download.")

    def test_an_item_that_is_no_longer_waiting_shows_what_became_of_it(self) -> None:
        done = self.rows.one(_S.COMPLETED)
        self.show(done, None, 14)
        resp = self.get(f"/review?item={done.id}", headers=HTMX)
        self.assertIn("Not waiting for you · 14 waiting", resp.text)
        self.assertIn("Downloaded", resp.text)
        self.assertNotIn("hx-post", resp.text)
        self.assertIn('data-key="j"', resp.text)

    def test_the_card_reads_itself_again_when_its_item_changes(self) -> None:
        page = self.get("/review").text
        i = self.item.id
        self.assertIn(f'hx-get="/review?item={i}" hx-trigger="item-{i} from:body" hx-swap="outerHTML"', page)
        self.assertEqual(page.count('<dialog id="drawer"'), 1)


# ---------------------------------------------------------------------------
# The item
# ---------------------------------------------------------------------------


class ItemPageTests(PageCase):
    def show(self, item, **more) -> cards.Detail:
        detail = self.rows.detail(item, **more)
        self.load_detail.return_value = detail
        return detail

    def test_the_verdict_lists_every_reason_under_its_rule(self) -> None:
        item = self.rows.one(_S.AWAITING_DATA)
        self.show(item)
        page = self.get(f"/item/{item.id}").text
        verdict = re.search(r'<section[^>]*aria-labelledby="verdict-title">.*?</section>', page, re.S).group(0)
        self.assertIn('<span class="badge badge-muted">Policy: wait</span>', verdict)
        self.assertIn(f"policy {item.policy_version}", verdict)
        # Each under its rule, in words: "Waiting for IMDb's parents guide", not "imdb.violence: ... not fetched".
        listed = re.findall(r'<li><span class="tag">([^<]+)</span> ([^<]+)</li>', verdict)
        self.assertEqual([tag for tag, _ in listed], ["violence", "nudity", "frightening"])
        for _, text in listed:
            self.assertTrue(text.startswith("Waiting for IMDb&#39;s parents guide"), text)
        self.assertEqual(len(listed), len(item.reasons))

    def test_the_vote_bars_carry_their_counts_as_text(self) -> None:
        item = self.rows.violent
        self.show(item)
        page = self.get(f"/item/{item.id}").text
        guide = re.search(r'<section[^>]*aria-labelledby="guide-title">.*?</section>', page, re.S).group(0)
        self.assertEqual(guide.count('<div class="bar" role="img"'), 5)
        stored = item.signals["imdb.violence"]
        spoken = "Violence votes: " + ", ".join(f"{name} {votes}" for name, votes in stored["breakdown"].items())
        self.assertIn(f'aria-label="{spoken}"', guide)
        self.assertIn('<span class="pill pill-severe">Violence: severe</span>', guide)
        self.assertIn(f'{stored["votes"]} votes', guide)
        for name, votes in stored["breakdown"].items():
            self.assertIn(f'<li><span class="swatch sev-{name}" aria-hidden="true"></span>{name} {votes}</li>', guide)
        widths = [float(w) for w in re.findall(r'style="width: ([\d.]+)%"', guide)]
        self.assertGreaterEqual(len(widths), 15)

    def test_certification_by_country(self) -> None:
        item = self.rows.one(_S.PENDING_REVIEW, lambda i: any(r["rule"] == "adult-certification" for r in i.reasons or []))
        self.show(item)
        page = self.get(f"/item/{item.id}").text
        self.assertRegex(page, r'<dt>Certification</dt>\s*<dd><span class="tag">GB 18</span> <span class="tag">US NC-17</span>')

    def test_every_stored_release_has_a_grab_button(self) -> None:
        item = self.rows.searched
        search = item.torrent_searches[0]
        # Ten stored: the old page stopped at five.
        search.prowlarr_results = [dict(search.prowlarr_results[0], title=f"Release.{n}") for n in range(10)]
        self.addCleanup(setattr, search, "prowlarr_results", search.prowlarr_results[:1])
        self.show(item)
        page = self.get(f"/item/{item.id}").text
        grabs = re.findall(rf'hx-post="/item/{item.id}/download/{search.id}/(\d+)" hx-target="#item-panel"', page)
        self.assertEqual(grabs, [str(n) for n in range(10)])
        self.assertIn('aria-label="Grab Release.9"', page)
        self.assertIn("10 stored", page)

    def test_a_type_delivered_elsewhere_lists_its_releases_without_a_grab_and_says_where_it_goes(self) -> None:
        item = self.rows.searched
        line = "Approving sends the title to Radarr, which picks its own release."
        self.show(item, to_client=False, delivery=line)
        page = self.get(f"/item/{item.id}").text
        self.assertIn(item.torrent_searches[0].prowlarr_results[0]["title"], page)
        self.assertNotIn("/download/", page)
        self.assertIn(f"<strong>Delivery:</strong> {line}", page)
        self.show(item)
        self.assertNotIn("Delivery:", self.get(f"/item/{item.id}").text)

    def test_what_the_policy_read_is_listed_folded_with_missing_data_in_words(self) -> None:
        item = self.rows.violent
        self.show(item)
        page = self.get(f"/item/{item.id}").text
        block = re.search(r'<details class="search mt-3">.*?</details>', page, re.S).group(0)
        self.assertNotIn("<details class=\"search mt-3\" open", block)
        # Each signal by its name in words; the engine's key is its title.
        listed = {key: (label, said) for key, label, said in re.findall(
            r'<div><dt title="([^"]+)">([^<]+)</dt><dd>([^<]*)</dd></div>', block)}
        self.assertEqual(set(listed), {str(name) for name in item.signals})
        self.assertIn(f"What the policy read ({len(item.signals)} signals)", block)
        self.assertEqual(listed["imdb.violence"][0], "Violence (IMDb)")
        self.assertRegex(listed["imdb.violence"][1], r"^severe \(\d+ votes\)$")
        # No snapshot, no list.
        bare = MediaItem(id=7, status=_S.PENDING_REVIEW, title="Bare", raw_text="x", created_at=NOW)
        self.load_detail.return_value = cards.detail_of(bare)
        self.assertNotIn("What the policy read", self.get("/item/7").text)

    def test_a_signal_says_its_value_or_why_it_has_none(self) -> None:
        read = dict(cards.signals_read({
            "rating": 7.4, "adult": False, "country": ["US", "GB"], "cast": [],
            "imdb.nudity": {"severity": "mild", "votes": 12, "breakdown": {"mild": 12}},
            "imdb.alcohol": {"missing": "no parents guide for this title"},
            "imdb.profanity": {"unavailable": "imdb_guide"},
            "size_gb": None,
        }))
        self.assertEqual(read, {
            "rating": "7.4", "adult": "no", "country": "US, GB", "cast": "none",
            "imdb.nudity": "mild (12 votes)",
            "imdb.alcohol": "missing: no parents guide for this title",
            "imdb.profanity": "not read: imdb_guide is switched off",
            "size_gb": "none",
        })
        self.assertEqual(cards.signals_read(None), [])
        self.assertEqual([name for name, _ in cards.signals_read({"b": 1, "a": 2})], ["a", "b"])

    def test_releases_are_listed_but_not_grabbable_once_the_item_is_decided(self) -> None:
        item = self.rows.downloading
        self.show(item)
        page = self.get(f"/item/{item.id}").text
        self.assertIn(item.torrent_searches[0].prowlarr_results[0]["title"], page)
        self.assertNotIn("/download/", page)

    def test_the_download_is_polled_only_while_it_is_downloading(self) -> None:
        item = self.rows.downloading
        self.show(item)
        page = self.get(f"/item/{item.id}").text
        block = re.search(r'<div id="download"[^>]*>', page, re.S).group(0)
        self.assertIn(f'hx-get="/item/{item.id}/progress" hx-trigger="every 5s" hx-swap="outerHTML"', block)
        self.assertRegex(page, r'<progress class="progress" value="\d+" max="100" aria-label="Download progress">')
        for other in (self.rows.one(_S.COMPLETED), self.rows.violent):
            self.show(other)
            self.assertNotIn("every 5s", self.get(f"/item/{other.id}").text)

    def test_the_poll_s_answer_stops_the_polling_when_the_download_is_over(self) -> None:
        item = self.rows.downloading
        self.load_download.return_value = cards.Card(item, download=item.download)
        going = self.get(f"/item/{item.id}/progress", headers=HTMX)
        self.assertTrue(going.text.lstrip().startswith('<div id="download"'))
        self.assertIn('hx-trigger="every 5s"', going.text)
        self.load_download.assert_awaited_once_with(item.id)

        done = Download(media_item_id=item.id, status=DownloadStatus.COMPLETED, progress=1.0)
        finished = MediaItem(id=item.id, status=_S.COMPLETED)
        cancelled = Download(media_item_id=item.id, status=DownloadStatus.CANCELLED, progress=0.4)
        for card in (cards.Card(finished, download=done), cards.Card(MediaItem(id=item.id, status=_S.REJECTED), download=cancelled), None):
            self.load_download.return_value = card
            over = self.get(f"/item/{item.id}/progress", headers=HTMX)
            # A 200 with no trigger: an error would leave the old block asking forever.
            self.assertEqual(over.status_code, 200)
            self.assertTrue(over.text.lstrip().startswith('<div id="download"'))
            self.assertNotIn("hx-trigger", over.text)
            self.assertNotIn("hx-get", over.text)
            with self.subTest(card=card):
                if card is None:
                    self.assertNotIn("HX-Trigger", over.headers)
                else:
                    # The panel around the block and the row behind it read the item again:
                    # the badge said "Downloading 60%" and Reject was offered long after.
                    self.assertEqual(json.loads(over.headers["HX-Trigger"]), {f"over-{item.id}": True, f"item-{item.id}": True})
        self.assertNotIn("HX-Trigger", going.headers)
        self.show(item)
        panel = re.search(r'<article id="item-panel"[^>]*>', self.get(f"/item/{item.id}").text, re.S).group(0)
        self.assertIn(f'hx-get="/item/{item.id}" hx-trigger="over-{item.id} from:body" hx-swap="outerHTML"', panel)
        script = (WEB_DIR / "static" / "app.js").read_text()
        self.assertIn("if (e.target.id === 'drawer') document.getElementById('drawer-body').replaceChildren();", script)

    def test_the_source_post_its_link_and_its_links(self) -> None:
        item = self.rows.violent
        item.external_links = ["https://example.org/thread/9"]
        self.addCleanup(setattr, item, "external_links", [])
        detail = self.show(item)
        self.assertEqual(detail.post_link, f"https://t.me/c/{str(item.telegram_chat_id)[4:]}/{item.telegram_message_id}")
        page = self.get(f"/item/{item.id}").text
        post = re.search(r'<section[^>]*aria-labelledby="post-title">.*?</section>', page, re.S).group(0)
        self.assertIn(f"From {item.source_key}", post)
        self.assertIn(f'<a class="link" href="{detail.post_link}" target="_blank" rel="noopener noreferrer">Open the post in Telegram<span class="sr-only"> (opens in a new tab)</span></a>', post)
        self.assertIn(esc(f"Title: {item.title} ({item.year})"), post)
        self.assertIn('href="https://example.org/thread/9"', post)

    def test_the_history_comes_from_the_events(self) -> None:
        item = self.rows.violent
        self.show(item)
        page = self.get(f"/item/{item.id}").text
        history = re.search(r'<ol class="history">.*?</ol>', page, re.S).group(0)
        lines = re.findall(r'<li><time datetime="([^"]+)">[^<]+</time><span>([^<]+)</span></li>', history)
        self.assertEqual([text for _, text in lines], [
            f"Posted in {item.source_key}", "Read",
            "The policy decided to hold it for a person: Violence (IMDb): severe", "Sent to the review queue",
        ])
        self.assertEqual([at for at, _ in lines], sorted(at for at, _ in lines))

    def test_the_actions_and_the_rerun_button(self) -> None:
        item = self.rows.violent
        self.show(item)
        page = self.get(f"/item/{item.id}").text
        offered = re.findall(rf'<button[^>]*hx-post="/item/{item.id}/(\w+)" hx-target="#item-panel" hx-swap="outerHTML"', page)
        self.assertEqual(offered, ["approve", "reject", "search", "rerun"])
        self.assertRegex(page, r">Rerun policy</button>")
        # What Rerun does is on the page, where a phone shows it: a title tooltip never does.
        rerun = re.search(rf'<button[^>]*hx-post="/item/{item.id}/rerun"[^>]*>', page, re.S).group(0)
        self.assertIn('aria-describedby="rerun-says"', rerun)
        self.assertNotIn("title=", rerun)
        self.assertRegex(page, r'<p id="rerun-says"[^>]*>Rerun policy asks TMDB and the parents guide again')
        self.assertIn("If the lookup finds nothing, the item is left as it is.", page)
        done = self.rows.one(_S.COMPLETED)
        self.show(done)
        self.assertNotIn("hx-post", self.get(f"/item/{done.id}").text)
        self.assertNotIn("rerun-says", self.get(f"/item/{done.id}").text)

    def test_text_from_a_post_is_escaped(self) -> None:
        item = MediaItem(id=7, status=_S.PENDING_REVIEW, title="<b>Bold</b>", raw_text="<script>alert(1)</script>",
                         telegram_chat_id=1, telegram_message_id=1, reasons=[{"kind": "rule", "rule": "r", "text": "<i>x</i>"}])
        self.load_detail.return_value = cards.detail_of(item)
        page = self.get("/item/7").text
        self.assertNotIn("<script>alert", page)
        self.assertNotIn("<b>Bold</b>", page)
        self.assertNotIn("<i>x</i>", page)
        self.assertIn("&lt;b&gt;Bold&lt;/b&gt;", page)

    def test_the_same_panel_is_a_page_of_its_own_and_the_slide_over_s_content(self) -> None:
        item = self.rows.violent
        self.show(item)
        whole = self.get(f"/item/{item.id}")
        self.assertIn("<html", whole.text)
        self.assertIn('<a class="link text-sm" href="/"><span aria-hidden="true">&larr;</span> Inbox</a>', whole.text)
        part = self.get(f"/item/{item.id}", headers={**HTMX, "HX-Target": "drawer-body"})
        self.assertTrue(part.text.lstrip().startswith('<article id="item-panel"'))
        self.assertNotIn("<html", part.text)
        panel = re.search(r'<article id="item-panel".*</article>', whole.text, re.S).group(0)
        self.assertEqual(panel.split(), part.text.split())

    def test_an_item_that_is_gone(self) -> None:
        whole = self.get("/item/999")
        self.assertEqual(whole.status_code, 404)
        self.assertIn("This item no longer exists.", whole.text)
        self.assertIn("<html", whole.text)
        part = self.get("/item/999", headers=HTMX)
        self.assertEqual((part.status_code, part.text), (404, "This item no longer exists."))

    def test_an_address_no_item_can_have_is_a_missing_item_not_a_server_error(self) -> None:
        """/item/2147483648 reached the database and came back as a 500; /item/abc as a JSON error."""
        self.load_detail.reset_mock()
        for url in ("/item/2147483648", "/item/99999999999", "/item/abc", "/item/0", "/item/2147483648/progress"):
            with self.subTest(url=url):
                whole = self.get(url)
                self.assertEqual(whole.status_code, 404)
                self.assertIn("This item no longer exists.", whole.text)
                self.assertIn("<html", whole.text)
                part = self.get(url, headers=HTMX)
                self.assertEqual((part.status_code, part.text), (404, "This item no longer exists."))
        self.load_detail.assert_not_awaited()
        self.load_download.assert_not_awaited()
        for name, url in (("approve", "/item/99999999999/approve"), ("reject", "/item/2147483648/reject"),
                          ("search", "/item/abc/search"), ("rerun", "/item/2147483648/rerun"),
                          ("grab", "/item/7/download/2147483648/0")):
            with self.subTest(url=url), acting(name, ok()) as ran:
                resp = self.post(url, headers=HTMX)
                self.assertEqual((resp.status_code, resp.text), (404, "This item no longer exists."))
                ran.assert_not_awaited()


class ItemActionTests(PageCase):
    def setUp(self) -> None:
        super().setUp()
        self.item = self.rows.violent
        self.load_detail.return_value = self.rows.detail(self.item)

    def test_every_action_answers_with_the_panel_a_toast_and_the_item_s_event(self) -> None:
        i = self.item.id
        for url, name, args in (
            (f"/item/{i}/approve", "approve", (i,)),
            (f"/item/{i}/reject", "reject", (i, None)),
            (f"/item/{i}/search", "search", (i,)),
            (f"/item/{i}/download/55/2", "grab", (i, 55, 2)),
            (f"/item/{i}/rerun", "rerun", (i,)),
        ):
            with self.subTest(url=url), acting(name, actions.SearchOutcome(i, actions.Status.OK, f"{name} ran.", search_id=1)) as ran:
                resp = self.post(url, headers=HTMX)
                ran.assert_awaited_once_with(*args)
                self.assertEqual(resp.status_code, 200)
                self.assertTrue(resp.text.lstrip().startswith('<article id="item-panel"'))
                self.assertEqual(json.loads(resp.headers["HX-Trigger"]), {
                    "toast": {"text": f"{name} ran.", "kind": "ok"}, f"item-{i}": True,
                })

    def test_a_busy_item_is_a_409_toast_on_every_action(self) -> None:
        i = self.item.id
        self.load_detail.reset_mock()
        for url, name in ((f"/item/{i}/approve", "approve"), (f"/item/{i}/reject", "reject"), (f"/item/{i}/search", "search"),
                          (f"/item/{i}/download/55/2", "grab"), (f"/item/{i}/rerun", "rerun")):
            with self.subTest(url=url), acting(name, BUSY):
                resp = self.post(url, headers=HTMX)
                self.assertEqual((resp.status_code, resp.text), (409, actions.BUSY_MESSAGE))
                self.assertNotIn("HX-Trigger", resp.headers)
        self.load_detail.assert_not_awaited()

    def test_a_rerun_that_decided_nothing_is_an_error_toast_over_the_item(self) -> None:
        failed = actions.ActionResult(self.item.id, actions.Status.FAILED, "The policy could not be run again just now.")
        with acting("rerun", failed):
            resp = self.post(f"/item/{self.item.id}/rerun", headers=HTMX)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(toast(resp)["kind"], "error")

    def test_a_rejection_its_delivery_target_did_not_hear_of_is_an_error_toast(self) -> None:
        note = "Radarr could not be told and still has the title."
        told = actions.RejectResult(self.item.id, actions.Status.OK, f"Rejected. {note}", remote_note=note, remote_failed=True)
        with acting("reject", told):
            resp = self.post(f"/item/{self.item.id}/reject", headers=HTMX)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(toast(resp), {"text": f"Rejected. {note}", "kind": "error"})
        heard = actions.RejectResult(self.item.id, actions.Status.OK, "Rejected. Radarr no longer monitors it.",
                                     remote_note="Radarr no longer monitors it.")
        for url in (f"/item/{self.item.id}/reject", f"/inbox/{self.item.id}/reject", f"/review/{self.item.id}/reject"):
            with self.subTest(url=url), acting("reject", heard):
                self.assertEqual(toast(self.post(url, headers=HTMX))["kind"], "ok")
            with self.subTest(url=url, failed=True), acting("reject", told):
                self.assertEqual(toast(self.post(url, headers=HTMX))["kind"], "error")

    def test_a_title_outside_latin_1_still_makes_a_header(self) -> None:
        with acting("approve", ok("Queued Señales – 信号 for download.")):
            resp = self.post(f"/item/{self.item.id}/approve", headers=HTMX)
        self.assertEqual(toast(resp)["text"], "Queued Señales – 信号 for download.")

    def test_without_htmx_an_action_goes_back_to_the_item(self) -> None:
        with acting("approve", ok()):
            resp = self.post(f"/item/{self.item.id}/approve")
        self.assertEqual((resp.status_code, resp.headers["location"]), (303, f"/item/{self.item.id}"))

    def test_an_item_that_vanished_after_the_action_answers_nothing_to_swap(self) -> None:
        self.load_detail.return_value = None
        with acting("reject", ok("Rejected.")):
            resp = self.post(f"/item/{self.item.id}/reject", headers=HTMX)
        self.assertEqual((resp.status_code, resp.text), (200, ""))
        self.assertEqual(toast(resp)["text"], "Rejected.")


class DeepLinkTests(WebCase):
    """A link the bot sends, tapped in Telegram's browser: the item, alone on a page."""

    FROM_ANOTHER_APP = {
        "Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document", "Sec-Fetch-User": "?1",
    }

    def setUp(self) -> None:
        super().setUp()
        self.rows = Rows()
        self.item = self.rows.searched
        self.load_detail.return_value = self.rows.detail(self.item)

    def test_with_a_session_the_link_lands_on_the_whole_item(self) -> None:
        self.sign_in(remember="1")
        resp = self.get(f"/item/{self.item.id}", headers=self.FROM_ANOTHER_APP)
        self.assertEqual(resp.status_code, 200)
        self.load_detail.assert_awaited_once_with(self.item.id)
        for part in ("verdict-title", "guide-title", "releases-title", "post-title", "history-title"):
            self.assertIn(f'aria-labelledby="{part}"', resp.text)
        self.assertIn(f'hx-post="/item/{self.item.id}/approve"', resp.text)
        # The page stands alone: no slide-over to open it in, and a way back.
        self.assertNotIn('<dialog id="drawer"', resp.text)
        self.assertIn('href="/"', resp.text)
        self.assertIn('aria-current="page"', re.search(r'<a class="nav-link" href="/"[^>]*>', resp.text).group(0))

    def test_without_one_it_lands_there_after_signing_in(self) -> None:
        url = f"/item/{self.item.id}"
        resp = self.get(url, headers=self.FROM_ANOTHER_APP)
        self.assertEqual(resp.headers["location"], f"/login?next={url}")
        self.load_detail.assert_not_awaited()
        self.assertEqual(self.sign_in(next=url).headers["location"], url)
        self.assertIn(esc(self.item.title), self.get(url).text)



# ---------------------------------------------------------------------------
# What the UX audit found
# ---------------------------------------------------------------------------


class AuditCardTests(unittest.TestCase):
    """What an item offers, said once in cards.py for the row, the card and the panel."""

    def item(self, status: ItemStatus, **values) -> MediaItem:
        item = MediaItem(id=7, status=status, title="A Film", raw_text="A.Film.2024.1080p", created_at=NOW)
        for name, value in values.items():
            setattr(item, name, value)
        return item

    def test_a_post_with_no_title_is_dismissed_or_taught_never_approved(self) -> None:
        """It was approved, queued "for download", and saved APPROVED with no title."""
        card = cards.Card(self.item(_S.PARSE_FAILED, title=None))
        self.assertFalse(card.can_approve)
        self.assertTrue(card.can_reject)
        self.assertEqual(card.reject_label, "Dismiss")
        self.assertEqual(card.teach_href, "/setup?try=7#try-a-post")
        self.assertEqual(card.reject_confirm, "Its post stays where it is.")
        # Its own magnet is something to fetch.
        self.assertTrue(cards.Card(self.item(_S.PARSE_FAILED, title=None), direct_add="magnet").can_approve)
        self.assertEqual(cards.Card(self.item(_S.PENDING_REVIEW)).reject_label, "Reject")

    def test_an_item_in_error_offers_a_retry(self) -> None:
        self.assertEqual(cards.Card(self.item(_S.ERROR)).approve_label, "Retry")
        self.assertEqual(cards.Card(self.item(_S.PENDING_REVIEW)).approve_label, "Approve")

    def test_a_claim_marker_is_never_shown_as_the_reason(self) -> None:
        """A failed Grab showed "operation:manual-download" as its red notice."""
        self.assertIsNone(cards.Card(self.item(_S.ERROR, error_message="operation:manual-download")).failed)
        said = "The tracker refused the .torrent: northwind has no session. Paste new cookies in Setup."
        self.assertEqual(cards.Card(self.item(_S.ERROR, error_message=said)).failed, said)

    def test_search_is_not_offered_with_nothing_to_search_with(self) -> None:
        self.assertTrue(cards.Card(self.item(_S.PENDING_REVIEW)).can_search)
        self.assertFalse(cards.Card(self.item(_S.PENDING_REVIEW), searchable=False).can_search)

    def test_a_final_item_says_so(self) -> None:
        rejected = self.item(_S.REJECTED, reviewed_at=NOW)
        self.assertEqual(cards.Card(rejected).final, "Rejected by a person. Nothing more to do.")
        self.assertEqual(cards.Card(self.item(_S.COMPLETED)).final, "Downloaded. Nothing more to do.")
        by_policy = self.item(_S.REJECTED, policy_outcome="reject")
        self.assertEqual(cards.Card(by_policy).final, "")
        self.assertEqual(cards.Card(self.item(_S.PENDING_REVIEW)).final, "")

    def test_the_shadow_line_goes_last(self) -> None:
        item = self.item(_S.PENDING_REVIEW, reasons=[
            {"kind": "shadow", "rule": None, "text": "shadow mode: would have approved"},
            {"kind": "clear", "rule": None, "text": "no rule hit (5 checked)"},
        ])
        self.assertEqual([line.tag for line in cards.Card(item).reasons], ["clear", "shadow"])


class AuditPageTests(PageCase):
    def test_a_row_that_left_its_tab_shrinks_to_one_line(self) -> None:
        """An approved row stayed in Needs you with only a solid red Reject left on it."""
        approved = self.rows.one(_S.APPROVED)
        self.load_rows.return_value = ([self.rows.card(approved)], counts(needs=17))
        with acting("approve", ok("Queued it.")):
            resp = self.post(f"/inbox/{approved.id}/approve", headers={**HTMX, "HX-Current-URL": "http://testserver/"})
        row = self.row_of(resp.text, approved.id)
        self.assertIn('class="row row-left"', row)
        self.assertIn('<span class="badge badge-ok">Approved, queued</span>', row)
        self.assertIn(f'href="/item/{approved.id}"', row)
        self.assertNotIn("<button", row)
        # In All it is a row as ever.
        with acting("approve", ok("Queued it.")):
            resp = self.post(f"/inbox/{approved.id}/approve", headers={**HTMX, "HX-Current-URL": "http://testserver/?tab=all"})
        self.assertNotIn("row-left", self.row_of(resp.text, approved.id))

    def test_a_row_that_left_its_tab_goes_after_five_seconds(self) -> None:
        """Approved rows stayed on the shelf, faded, in the way of the next ones."""
        script = (WEB_DIR / "static" / "app.js").read_text()
        self.assertIn("document.querySelectorAll('.row-left:not([data-going])')", script)
        self.assertIn("if (row.matches(':hover, :focus-within')) return setTimeout(go, 1000);", script)
        self.assertIn("row.classList.add('row-going');", script)
        self.assertIn("}, 5000);", script)
        # The last row gone: the list is read again, from its first page.
        self.assertIn("url.searchParams.delete('page');", script)
        self.assertIn("htmx.ajax('GET', url.pathname + url.search, { target: '#inbox-list', select: '#inbox-list', swap: 'outerHTML' });", script)
        css = (WEB_DIR / "static" / "src" / "app.css").read_text()
        self.assertRegex(css, r"\.row\.row-going \{\s*opacity: 0;\s*transition: opacity 0\.4s;")

    def test_reject_in_a_row_is_outlined(self) -> None:
        self.inbox([self.rows.violent])
        row = self.row_of(self.get("/").text, self.rows.violent.id)
        self.assertIn('class="btn btn-danger-outline" hx-post=', row)
        self.assertNotIn('"btn btn-danger"', row)

    def test_the_needs_you_tab_leads_into_review(self) -> None:
        self.inbox([self.rows.violent])
        self.assertIn('<a class="btn btn-primary review-cta" href="/review">Review 18 one at a time</a>', self.get("/").text)
        # Review has no filters, so a filtered list does not promise a number.
        self.assertIn(">Review one at a time</a>", self.get("/?type=movie").text)
        self.assertNotIn("review-cta", self.get("/?tab=all").text)

    def test_the_back_link_says_where_the_item_was_opened_from(self) -> None:
        item = self.rows.violent
        self.load_detail.return_value = self.rows.detail(item)
        for asked, href, label in (("", "/", "Inbox"), ("review", "/review", "Review"),
                                   ("activity", "/activity", "Activity"), ("policy", "/policy", "Policy"),
                                   ("elsewhere", "/", "Inbox")):
            with self.subTest(asked=asked):
                page = self.get(f"/item/{item.id}?from={asked}").text
                self.assertIn(f'<a class="link text-sm" href="{href}"><span aria-hidden="true">&larr;</span> {label}</a>', page)

    def test_a_failed_download_says_why_and_shows_no_empty_bar(self) -> None:
        item = self.rows.violent
        failed = MediaItem(id=item.id, status=_S.ERROR, title=item.title, raw_text="x", created_at=NOW,
                           error_message="qBittorrent did not take the torrent.")
        self.load_download.return_value = cards.Card(failed, download=Download(
            media_item_id=item.id, status=DownloadStatus.ERROR, progress=0.0))
        block = self.get(f"/item/{item.id}/progress").text
        self.assertIn("Not downloading.", block)
        self.assertIn("qBittorrent did not take the torrent.", block)
        self.assertIn('href="/setup#integrations"', block)
        self.assertNotIn("<progress", block)
        self.assertNotIn("Error · 0%", block)


class ReasonWordsTests(unittest.TestCase):
    def test_a_combined_rule_s_text_is_worded_part_by_part(self) -> None:
        from vettarr.web import words

        self.assertEqual(words.reason("year: 1990; imdb.violence: severe"), "Year: 1990; Violence (IMDb): severe")
        self.assertEqual(words.reason("size_gb: 12; imdb.nudity: parents guide not fetched (not listed yet)"),
                         "Size (GB): 12; Waiting for IMDb's parents guide (not listed yet)")
        self.assertEqual(words.reason("no rule hit (2 checked)"), "no rule hit (2 checked)")


if __name__ == "__main__":
    unittest.main()
