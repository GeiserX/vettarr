"""The bot does what the web does, through the same actions and in the same words.

A review card links to the item's page, its buttons each call one action of
``vettarr.actions`` (Rerun policy among them), and what the bot says
after an action is the action's own message: the line the web shows.
"""

import asyncio
import inspect
import unittest
from types import SimpleNamespace
from unittest import mock

from telegram.error import BadRequest, TimedOut
from test_actions import Store, make_item

import policy_fixture
from vettarr import actions, ingest
from vettarr.bot import handlers
from vettarr.bot.keyboards import review_keyboard
from vettarr.config.schema import Web
from vettarr.models import AutoDecision, ItemStatus, MediaItem, MediaType, Recommendation

BASE = "https://vettarr.example"


def config(base_url: str | None = BASE, **policy):
    cfg = policy_fixture.config(**policy)
    return cfg.model_copy(update={"web": Web(base_url=base_url) if base_url else None})


def buttons(markup) -> list:
    return [b for row in markup.inline_keyboard for b in row]


class BotCase(unittest.TestCase):
    BASE_URL: str | None = BASE

    def setUp(self) -> None:
        self.store = Store()
        self.bot = SimpleNamespace(send_message=mock.AsyncMock(), send_photo=mock.AsyncMock())
        for patcher in (
            mock.patch.object(handlers.settings, "_config", config(self.BASE_URL, enabled="on")),
            mock.patch.object(handlers, "async_session", self.store.session),
            mock.patch.object(handlers, "_bot_app", SimpleNamespace(bot=self.bot)),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.query = SimpleNamespace(
            message=SimpleNamespace(chat_id=77, photo=None), answer=mock.AsyncMock(),
        )

    def held(self, item_id: int = 7, **fields) -> MediaItem:
        return make_item(
            self.store, item_id, title="The Lantern Keeper", media_type=MediaType.MOVIE,
            auto_recommendation=Recommendation.NEUTRAL, auto_decision=AutoDecision.NEEDS_REVIEW,
            auto_reasons=["imdb.violence: severe"], external_links=[], **fields,
        )

    def card(self, item: MediaItem):
        """Send the item's review card; return its text and its buttons."""
        asyncio.run(handlers._send_review(77, item))
        sent = self.bot.send_message.await_args.kwargs
        return sent["text"], buttons(sent["reply_markup"])


# ---------------------------------------------------------------------------
# The deep link
# ---------------------------------------------------------------------------


class DeepLinkTests(BotCase):
    def test_a_card_links_to_the_item_s_page_in_its_text_and_as_a_button(self) -> None:
        text, keys = self.card(self.held(7))
        self.assertIn(f'<a href="{BASE}/item/7">Open in vettarr</a>', text)
        self.assertEqual([b.url for b in keys if b.url], [f"{BASE}/item/7"])
        self.assertEqual([b.text for b in keys if b.url], ["Open in vettarr"])

    def test_the_card_a_held_item_is_pushed_with_is_that_card(self) -> None:
        """``held_for_review`` pushes through whatever the bot registered: the card with the link."""
        item = self.held(9, source_key="club", telegram_chat_id=-1009999)
        ingest.set_review_pusher(handlers._send_review)
        self.addCleanup(ingest.set_review_pusher, None)
        source = SimpleNamespace(key="club", notify_on_review=True)
        with (
            mock.patch.object(ingest, "async_session", self.store.session),
            mock.patch.object(ingest, "source_for", return_value=source),
            mock.patch.object(ingest, "reviewers", return_value=[501, 502]),
        ):
            asyncio.run(ingest.held_for_review(item.id))
        sent = self.bot.send_message.await_args_list
        self.assertEqual([c.kwargs["chat_id"] for c in sent], [501, 502])
        for call in sent:
            self.assertIn(f'href="{BASE}/item/9"', call.kwargs["text"])
            self.assertIn(f"{BASE}/item/9", [b.url for b in buttons(call.kwargs["reply_markup"])])

    def test_the_bot_registers_the_card_as_the_push(self) -> None:
        source = inspect.getsource(handlers.create_bot_app)
        self.assertIn("ingest.set_review_pusher(_send_review)", source)

    def test_a_card_with_a_poster_carries_the_link_in_its_caption(self) -> None:
        item = self.held(7)
        check = SimpleNamespace(
            genres=[], sex_rating=None, violence_rating=None, profanity_rating=None, frightening_rating=None,
            title_rule_hit=False, certifications={}, tmdb_id=None, imdb_id=None, poster_url="https://img.example/p.jpg",
        )
        session = mock.MagicMock()
        session.__aenter__.return_value.execute = mock.AsyncMock(
            return_value=SimpleNamespace(scalar_one_or_none=lambda: check),
        )
        with mock.patch.object(handlers, "async_session", return_value=session):
            asyncio.run(handlers._send_review(77, item))
        sent = self.bot.send_photo.await_args.kwargs
        self.assertIn(f'href="{BASE}/item/7"', sent["caption"])
        self.assertIn(f"{BASE}/item/7", [b.url for b in buttons(sent["reply_markup"])])

    def test_a_title_rule_hit_is_named_for_what_it_is(self) -> None:
        """The flag says a title rule of the policy hit, not which list the
        operator keeps: the words are config, the card stays neutral."""
        item = self.held(7)
        check = SimpleNamespace(
            genres=[], sex_rating=None, violence_rating=None, profanity_rating=None, frightening_rating=None,
            title_rule_hit=True, certifications={}, tmdb_id=None, imdb_id=None, poster_url=None,
        )
        session = mock.MagicMock()
        session.__aenter__.return_value.execute = mock.AsyncMock(
            return_value=SimpleNamespace(scalar_one_or_none=lambda: check),
        )
        with mock.patch.object(handlers, "async_session", return_value=session):
            asyncio.run(handlers._send_review(77, item))
        text = self.bot.send_message.await_args.kwargs["text"]
        self.assertIn("⚡ Title rule hit", text)

    def test_a_card_telegram_refuses_goes_again_without_the_button(self) -> None:
        """A link button Telegram will not take must not cost the reviewer the card."""
        self.bot.send_message.side_effect = [BadRequest("Wrong HTTP URL"), None]
        asyncio.run(handlers._send_review(77, self.held(7)))
        first, second = (c.kwargs for c in self.bot.send_message.await_args_list)
        self.assertIn(f"{BASE}/item/7", [b.url for b in buttons(first["reply_markup"])])
        self.assertEqual([b.url for b in buttons(second["reply_markup"]) if b.url], [])
        self.assertIn("rerun:7", [b.callback_data for b in buttons(second["reply_markup"])])
        self.assertIn(f'href="{BASE}/item/7"', second["text"])

    def test_any_other_failure_is_not_sent_twice(self) -> None:
        """A flood limit or a timeout is for whoever pushes to wait out, not to send again at once."""
        self.bot.send_message.side_effect = TimedOut()
        with self.assertRaises(TimedOut):
            asyncio.run(handlers._send_review(77, self.held(7)))
        self.assertEqual(self.bot.send_message.await_count, 1)

    def test_a_trailing_slash_in_the_address_does_not_double(self) -> None:
        with mock.patch.object(handlers.settings, "_config", config(BASE + "/")):
            self.assertEqual(handlers.item_url(3), f"{BASE}/item/3")

    def test_the_details_view_keeps_the_link_button(self) -> None:
        self.held(7)
        respond = mock.AsyncMock()
        with mock.patch.object(handlers, "_respond", respond):
            asyncio.run(handlers._handle_details(self.query, 7))
        self.assertIn(f"{BASE}/item/7", [b.url for b in buttons(respond.await_args.kwargs["reply_markup"])])

    def test_the_address_is_escaped_in_the_text(self) -> None:
        with mock.patch.object(handlers.settings, "_config", config("https://vettarr.example/a?b=1&c=2")):
            text, _ = self.card(self.held(7))
        # The item's path goes before the query, so the link opens the item.
        self.assertIn('href="https://vettarr.example/a/item/7?b=1&amp;c=2"', text)

    def test_a_path_or_a_fragment_in_the_address_still_makes_a_link_that_opens(self) -> None:
        for base, url in (
            ("https://vettarr.example/ui/", "https://vettarr.example/ui/item/3"),
            ("https://vettarr.example#top", "https://vettarr.example/item/3"),
            ("http://10.0.0.5:4850", "http://10.0.0.5:4850/item/3"),
        ):
            with self.subTest(base=base), mock.patch.object(handlers.settings, "_config", config(base)):
                self.assertEqual(handlers.item_url(3), url)


class NoAddressTests(BotCase):
    BASE_URL = None

    def test_a_refusal_with_no_button_to_blame_is_raised(self) -> None:
        self.bot.send_message.side_effect = BadRequest("message is too long")
        with self.assertRaises(BadRequest):
            asyncio.run(handlers._send_review(77, self.held(7)))
        self.assertEqual(self.bot.send_message.await_count, 1)

    def test_without_web_base_url_a_card_has_no_link_and_is_still_sent(self) -> None:
        text, keys = self.card(self.held(7))
        self.assertNotIn("Open in vettarr", text)
        self.assertEqual([b.url for b in keys if b.url], [])
        self.assertIsNone(handlers.item_url(7))
        self.assertIn("rerun:7", [b.callback_data for b in keys])


class ButtonAddressTests(unittest.TestCase):
    def test_telegram_gets_a_button_only_for_an_address_it_takes(self) -> None:
        """A message with a button to localhost is refused whole, and the card is lost with it."""
        for url, ok in (
            ("https://vettarr.example/item/1", True),
            ("http://10.1.2.20:4850/item/1", True),
            ("http://[fd00::1]:4850/item/1", True),
            ("http://localhost:4850/item/1", False),
            ("http://vettarr:4850/item/1", False),
            (None, False),
        ):
            with self.subTest(url=url):
                self.assertEqual(handlers.button_url(url), url if ok else None)


class DotlessHostTests(BotCase):
    BASE_URL = "http://vettarr:4850"

    def test_an_address_telegram_refuses_as_a_button_stays_a_link_in_the_text(self) -> None:
        text, keys = self.card(self.held(7))
        self.assertIn('<a href="http://vettarr:4850/item/7">Open in vettarr</a>', text)
        self.assertEqual([b.url for b in keys if b.url], [])


# ---------------------------------------------------------------------------
# The buttons, and that each is one action
# ---------------------------------------------------------------------------


class KeyboardTests(unittest.TestCase):
    def test_a_card_has_every_action_the_web_has(self) -> None:
        for flagged in (False, True):
            with self.subTest(flagged=flagged):
                data = [b.callback_data for b in buttons(review_keyboard(9, flagged)) if b.callback_data]
                self.assertEqual(
                    data,
                    ["approve:9", "reject:9", "skip:9", "details:9", "releases:9", "rerun:9"],
                )

    def test_the_rerun_button_says_what_it_does(self) -> None:
        labels = {b.callback_data: b.text for b in buttons(review_keyboard(9)) if b.callback_data}
        self.assertEqual(labels["rerun:9"], "Rerun policy")

    def test_every_button_s_data_fits_telegram_s_64_bytes(self) -> None:
        for b in buttons(review_keyboard(2_147_483_647, True, f"{BASE}/item/2147483647")):
            if b.callback_data:
                self.assertLessEqual(len(b.callback_data.encode()), 64)


class RoutingTests(BotCase):
    def press(self, data: str) -> None:
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=1),
            callback_query=SimpleNamespace(data=data, answer=mock.AsyncMock(), message=self.query.message),
        )
        with mock.patch.object(ingest, "reviewers", return_value=[1]):
            asyncio.run(handlers.on_callback(update, None))

    def test_every_button_reaches_its_action(self) -> None:
        """Skip, Releases, search again, a release and Rerun policy: one action each, nothing of the bot's own."""
        nothing = SimpleNamespace(prowlarr_results=[], id=4)
        cases = (
            ("approve:7", "approve", actions.ActionResult(7, actions.Status.OK, "Queued x."), (7,)),
            ("confirm_reject:7", "reject", actions.RejectResult(7, actions.Status.OK, "Rejected."), (7, None)),
            ("skip:7", "skip", actions.SkipResult(7, actions.Status.OK, "Skipped."), (7,)),
            ("research:7", "search", actions.SearchOutcome(7, actions.Status.OK, "No releases found.", search_id=4), (7,)),
            ("torrent:7:4:0", "grab", actions.ActionResult(7, actions.Status.OK, "Downloading x."), (7, 4, 0)),
            ("rerun:7", "rerun", actions.ActionResult(7, actions.Status.OK, "The policy approved it. It is queued for download."), (7,)),
        )
        for data, name, result, args in cases:
            with self.subTest(button=data):
                action = mock.AsyncMock(return_value=result)
                with (
                    mock.patch.object(actions, name, action),
                    mock.patch.object(actions, "latest_search", mock.AsyncMock(return_value=nothing)),
                    mock.patch.object(actions, "next_for_review", mock.AsyncMock(return_value=None)),
                    mock.patch.object(handlers, "_respond", mock.AsyncMock()),
                    mock.patch.object(handlers, "_edit_or_send", mock.AsyncMock()),
                    mock.patch.object(handlers, "_ingester", None),
                ):
                    self.press(data)
                action.assert_awaited_once_with(*args)

    def test_releases_reads_the_stored_search_through_the_actions(self) -> None:
        latest = mock.AsyncMock(return_value=SimpleNamespace(prowlarr_results=[{"title": "R"}], id=4))
        self.held(7)
        with (
            mock.patch.object(actions, "latest_search", latest),
            mock.patch.object(handlers, "_respond", mock.AsyncMock()),
        ):
            self.press("releases:7")
        latest.assert_awaited_once_with(7)

    def test_the_handlers_change_nothing_themselves(self) -> None:
        """No UPDATE, no search, no download client in the bot: an action is the only way to change an item."""
        source = inspect.getsource(handlers)
        for forbidden in ("update(", "search_service", "qbittorrent", "reject_item", "prowlarr.", ".status = "):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)


# ---------------------------------------------------------------------------
# What the bot says
# ---------------------------------------------------------------------------


class ReplyTests(BotCase):
    def said(self, handler, *args, **patches) -> str:
        respond = mock.AsyncMock(return_value=SimpleNamespace())
        edit = mock.AsyncMock()
        with (
            mock.patch.object(handlers, "_respond", respond),
            mock.patch.object(handlers, "_edit_or_send", edit),
            mock.patch.object(handlers, "_send_next", mock.AsyncMock()),
            mock.patch.multiple(actions, **{k: mock.AsyncMock(return_value=v) for k, v in patches.items()}),
        ):
            asyncio.run(handler(self.query, *args))
        if edit.await_count:
            return edit.await_args.args[2]
        return respond.await_args.kwargs["text"]

    def test_the_reply_is_the_action_s_own_line(self) -> None:
        """The web shows ``result.message`` as its toast; the bot says the same words."""
        cases = (
            (handlers._handle_approve, (7,), "approve",
             actions.ActionResult(7, actions.Status.OK, "Queued The Lantern Keeper for download.")),
            (handlers._handle_approve, (7,), "approve",
             actions.ActionResult(7, actions.Status.BUSY, actions.BUSY_MESSAGE)),
            (handlers._handle_confirm_reject, (7,), "reject",
             actions.RejectResult(7, actions.Status.OK, "Rejected, and its post was deleted.")),
            (handlers._handle_skip, (7,), "skip",
             actions.SkipResult(7, actions.Status.OK, "Skipped. Nothing else is waiting for you.")),
            (handlers._handle_torrent_select, (7, 0, 4), "grab",
             actions.ActionResult(7, actions.Status.OK, "Downloading The.Lantern.Keeper.2021.1080p-KITE.")),
            (handlers._handle_torrent_select, (7, 0, 4), "grab",
             actions.ActionResult(7, actions.Status.FAILED, "qBittorrent did not take the torrent.")),
            (handlers._handle_torrent_select, (7, 9, 4), "grab",
             actions.ActionResult(7, actions.Status.NOT_FOUND, "That release is no longer listed. Search again.")),
            (handlers._handle_rerun, (7,), "rerun",
             actions.ActionResult(7, actions.Status.OK, actions._RERUN["approve"])),
            (handlers._handle_rerun, (7,), "rerun",
             actions.ActionResult(7, actions.Status.FAILED, actions.RERUN_NOT_RUN)),
        )
        for handler, args, name, result in cases:
            with self.subTest(action=name, status=result.status.value):
                self.assertEqual(self.said(handler, *args, **{name: result}), result.message)

    def test_the_line_is_escaped_for_telegram(self) -> None:
        result = actions.ActionResult(7, actions.Status.OK, "Queued <b>Tom & Jerry</b> for download.")
        self.assertEqual(
            self.said(handlers._handle_approve, 7, approve=result),
            "Queued &lt;b&gt;Tom &amp; Jerry&lt;/b&gt; for download.",
        )

    def test_a_search_says_what_the_action_says_when_nothing_comes_back(self) -> None:
        self.held(7)
        result = actions.SearchOutcome(7, actions.Status.OK, "No releases found.", search_id=4)
        self.assertEqual(self.said(handlers._handle_research, 7, search=result), "No releases found.")
        failed = actions.SearchOutcome(7, actions.Status.FAILED, "This item has no title to search for.")
        self.assertEqual(self.said(handlers._handle_research, 7, search=failed), failed.message)


class RerunButtonTests(BotCase):
    def rerun(self, result, item=None):
        respond = mock.AsyncMock(return_value=SimpleNamespace())
        with (
            mock.patch.object(actions, "rerun", mock.AsyncMock(return_value=result)) as action,
            mock.patch.object(handlers, "_respond", respond),
            mock.patch.object(handlers, "_edit_or_send", mock.AsyncMock()) as edit,
            mock.patch.object(handlers, "_send_review", mock.AsyncMock()) as card,
            mock.patch.object(handlers, "_send_next", mock.AsyncMock()) as nxt,
        ):
            asyncio.run(handlers._handle_rerun(self.query, 7))
        action.assert_awaited_once_with(7)
        return respond, edit, card, nxt

    def test_it_says_it_is_deciding_then_what_the_policy_said(self) -> None:
        result = actions.ActionResult(7, actions.Status.OK, actions._RERUN["approve"])
        respond, edit, _card, _nxt = self.rerun(result)
        self.assertIn("Deciding again", respond.await_args.kwargs["text"])
        self.assertEqual(edit.await_args.args[2], actions._RERUN["approve"])

    def test_an_item_the_policy_took_moves_on_to_the_next(self) -> None:
        for status in (ItemStatus.APPROVED, ItemStatus.REJECTED, ItemStatus.AWAITING_DATA):
            with self.subTest(status=status):
                self.held(7, status=status)
                _r, _e, card, nxt = self.rerun(actions.ActionResult(7, actions.Status.OK, "x"))
                card.assert_not_awaited()
                nxt.assert_awaited_once_with(77)

    def test_an_item_still_held_comes_back_as_its_card(self) -> None:
        item = self.held(7)
        _r, _e, card, nxt = self.rerun(actions.ActionResult(7, actions.Status.OK, actions._RERUN["review"]))
        card.assert_awaited_once_with(77, item)
        nxt.assert_not_awaited()

    def test_a_rerun_that_did_not_run_leaves_the_card_in_front_of_the_reviewer(self) -> None:
        item = self.held(7)
        _r, _e, card, nxt = self.rerun(actions.ActionResult(7, actions.Status.FAILED, actions.RERUN_NOT_RUN))
        card.assert_awaited_once_with(77, item)
        nxt.assert_not_awaited()

    def test_an_item_somebody_else_took_gets_no_card(self) -> None:
        self.held(7, status=ItemStatus.DOWNLOADING)
        _r, _e, card, nxt = self.rerun(actions.ActionResult(7, actions.Status.BUSY, actions.BUSY_MESSAGE))
        card.assert_not_awaited()
        nxt.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
