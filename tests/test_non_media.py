"""Telling the operator's own messages apart from the release posts.

Both directions matter equally. A note that gets treated as a media post ends
up in the review queue and can be deleted from the group; a release post that
gets treated as a note is silently dropped.

The fixtures use the vocabulary of the example "card" format in
config.example.yaml.
"""

import logging
import os
import unittest

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

from vettarr.config.loader import load
from vettarr.config.schema import FormatProfile
from vettarr.parser import regex_parser
from vettarr.parser.formats import BARE, compile_format, compile_formats

EXAMPLE = Path(__file__).resolve().parents[1] / "config.example.yaml"
FORMATS = compile_formats(load(EXAMPLE, env={}).formats)
# The example's scene profile on its own: what `config import-env` writes and
# what a boot from the environment reads with. It lists no media words.
SCENE_ONLY = tuple(f for f in FORMATS if f.kind == "scene")
# A card whose header line opens with a marker, which the example does not ship.
MARKER = "\U0001f39e"
HEADER_CARD = (compile_format(FormatProfile(
    key="header-card", header_markers=[MARKER],
    category_words={"movie": ["Movies"], "series": ["Series"]},
)),)


def classify_non_media(text: str):
    return regex_parser.classify_non_media(text, FORMATS)


def has_release_structure(text: str) -> bool:
    return regex_parser.has_release_structure(text, FORMATS)


def has_title_like_content(text: str) -> bool:
    return regex_parser.has_title_like_content(text, FORMATS)


def parse_message(text: str):
    return regex_parser.parse_message(text, FORMATS)


class NotMediaTests(unittest.TestCase):
    def test_an_instagram_reel_with_tracking_parameters(self) -> None:
        text = (
            "https://www.instagram.com/reel/DAbC12xyz/"
            "?utm_source=ig_web_copy_link&igsh=MXYzNDVhbmQ4ZGoy"
        )
        self.assertEqual(classify_non_media(text), "instagram.com link, not a media post")

    def test_an_instagram_link_with_a_caption(self) -> None:
        text = "look at this clip my sister just sent over to me\nhttps://instagram.com/p/XYZ/"
        self.assertEqual(classify_non_media(text), "instagram.com link, not a media post")

    def test_a_tiktok_link(self) -> None:
        self.assertEqual(
            classify_non_media("https://www.tiktok.com/@someone/video/7300000000000000000"),
            "tiktok.com link, not a media post",
        )

    def test_a_youtube_link(self) -> None:
        self.assertEqual(
            classify_non_media("https://youtu.be/dQw4w9WgXcQ"),
            "youtu.be link, not a media post",
        )

    def test_an_x_link(self) -> None:
        self.assertEqual(
            classify_non_media("https://x.com/someone/status/1800000000000000000"),
            "x.com link, not a media post",
        )

    def test_a_facebook_link(self) -> None:
        self.assertEqual(
            classify_non_media("https://www.facebook.com/share/p/abc123/"),
            "facebook.com link, not a media post",
        )

    def test_a_plain_sentence(self) -> None:
        self.assertEqual(
            classify_non_media("remember to take the bins out before nine tonight"),
            "no release information in the message",
        )

    def test_a_short_note(self) -> None:
        self.assertEqual(classify_non_media("ok"), "no release information in the message")

    def test_an_empty_message(self) -> None:
        self.assertEqual(classify_non_media("   "), "the message is empty")

    def test_an_unknown_link_is_still_not_a_release(self) -> None:
        self.assertEqual(
            classify_non_media("https://example.invalid/some/page"),
            "no release information in the message",
        )


class StaysReviewableTests(unittest.TestCase):
    """A request typed in plain words is still a request.

    `ignored` is terminal: never reviewed, never re-parsed, never shown again.
    Applying it to a message that names something the operator wants loses the
    request, so anything that could be a name stays in the review queue where
    it lands today. These are the shapes the group's own history contains,
    rewritten with invented titles.
    """

    def test_a_bare_film_title_in_capitals(self) -> None:
        self.assertIsNone(classify_non_media("ESCAPE ROUTE"))

    def test_a_bare_film_title_in_ordinary_case(self) -> None:
        self.assertIsNone(classify_non_media("Some Film 64"))

    def test_a_sentence_asking_for_a_book(self) -> None:
        self.assertIsNone(classify_non_media("want to read this book!! hahaha"))

    def test_a_sentence_asking_for_a_film(self) -> None:
        self.assertIsNone(classify_non_media("put on the film we were talking about"))

    REQUESTS = (
        "could you look for that movie we were all talking about yesterday evening, thanks.",
        "i would like to get the second book of that saga you told me about, if you can.",
    )
    NOTE = "remember to take the bins out before nine tonight, please."

    def test_a_media_word_comes_from_the_format(self) -> None:
        """A format that lists media words keeps a sentence that holds one and
        is finished with a sentence that holds none."""
        words = (compile_format(FormatProfile(key="words", kind="scene", media_words=["movie", "book"])),)
        for text in self.REQUESTS:
            self.assertIsNone(regex_parser.classify_non_media(text, words))
        self.assertEqual(
            regex_parser.classify_non_media(self.NOTE, words), "no release information in the message"
        )

    def test_with_no_media_words_a_request_is_never_finished_with(self) -> None:
        """No format lists media words: no formats at all, or the scene profile
        alone. Nothing can then tell a request typed in plain words from a
        note, and `ignored` is terminal, so both stay reviewable."""
        for name, formats in (("bare", (BARE,)), ("scene only", SCENE_ONLY), ("none", ())):
            for text in (*self.REQUESTS, self.NOTE):
                with self.subTest(formats=name, text=text):
                    self.assertIsNone(regex_parser.classify_non_media(text, formats))

    def test_with_no_media_words_a_message_with_no_words_is_still_not_media(self) -> None:
        for name, formats in (("bare", (BARE,)), ("scene only", SCENE_ONLY)):
            with self.subTest(formats=name):
                self.assertEqual(
                    regex_parser.classify_non_media("https://example.invalid/some/page", formats),
                    "no release information in the message",
                )
        hosts = (compile_format(FormatProfile(key="hosts", kind="scene", ignore_hosts=["instagram.com"])),)
        self.assertEqual(
            regex_parser.classify_non_media("https://www.instagram.com/reel/DAbC12xyz/", hosts),
            "instagram.com link, not a media post",
        )

    def test_a_quoted_title_with_an_author(self) -> None:
        self.assertIsNone(
            classify_non_media('"an invented title" book by Someone Example')
        )

    def test_a_list_of_titles(self) -> None:
        text = (
            "the first two,\n"
            "*An Invented Title by Someone Example\n"
            "*Another Invented Title"
        )
        self.assertIsNone(classify_non_media(text))

    def test_a_request_written_as_a_card_by_another_bot(self) -> None:
        text = "**New Movie Request - Something Invented (1995)**\nany synopsis at all"
        self.assertIsNone(classify_non_media(text))

    def test_a_complaint_that_names_films(self) -> None:
        text = "you posted several films twice, these ones are duplicated"
        self.assertIsNone(classify_non_media(text))


class TitleLikeContentTests(unittest.TestCase):
    def test_a_quoted_phrase_counts(self) -> None:
        """Quotes around a phrase are how somebody writes a title they are not
        sure how to write."""
        self.assertTrue(
            has_title_like_content(
                'someone said "something completely invented" is very good and nobody knows where to find it'
            )
        )

    def test_a_media_noun_counts(self) -> None:
        self.assertTrue(has_title_like_content("want a new series to watch this week"))

    def test_a_year_counts(self) -> None:
        self.assertTrue(has_title_like_content("something from 1995 would be nice for the weekend"))

    def test_a_shouted_word_counts(self) -> None:
        self.assertTrue(has_title_like_content("see if you can find AUTOMATA somewhere around"))

    def test_a_capital_that_does_not_start_the_line_counts(self) -> None:
        self.assertTrue(has_title_like_content("that thing from Example you mentioned earlier this afternoon"))

    def test_the_second_word_of_a_line_counts_too(self) -> None:
        """A name can sit anywhere after the first word, including right
        after it."""
        self.assertTrue(
            has_title_like_content("play Example when you have a moment this afternoon")
        )

    def test_a_shouted_word_counts_even_when_it_opens_the_line(self) -> None:
        """A title typed in capitals at the start of a long sentence has no
        other signal to be found by."""
        self.assertTrue(
            has_title_like_content("AUTOMATA can you put it on this week please")
        )

    def test_a_short_sentence_that_ends_in_punctuation_does_not(self) -> None:
        """A title is not typed with a full stop after it. Without that rule
        every three-word reply in the group reads as a request."""
        self.assertFalse(has_title_like_content("not today."))
        self.assertFalse(has_title_like_content("fine, will look later."))

    def test_an_ordinary_lowercase_sentence_does_not(self) -> None:
        self.assertFalse(
            has_title_like_content("remember to take the bins out before nine tonight")
        )

    def test_a_link_on_its_own_does_not(self) -> None:
        self.assertFalse(has_title_like_content("https://www.instagram.com/reel/DAbC12xyz/"))

    def test_a_link_is_not_read_as_a_title(self) -> None:
        """The URL is stripped first, so its capitals and digits count for
        nothing."""
        self.assertFalse(
            has_title_like_content("https://www.tiktok.com/@Someone/video/7300000000000000000")
        )


class StillMediaTests(unittest.TestCase):
    """Anything that could be a release post must survive untouched."""

    def test_a_scene_release_line(self) -> None:
        text = "Some Film (2024) 1080p WEB-DL DD+5.1 H.264-GROUP\n8.4 GiB"
        self.assertIsNone(classify_non_media(text))
        self.assertTrue(parse_message(text).is_valid)

    def test_a_tracker_card(self) -> None:
        text = (
            "Another Film 2023 2160p UHD BluRay REMUX HDR -TEAM\n"
            "Movies → BluRay\n"
            "42.1 GiB\n100% Free"
        )
        self.assertIsNone(classify_non_media(text))

    def test_a_series_pack(self) -> None:
        text = "Some Series S03 1080p WEB-DL AAC H.264-GROUP [PACK]\n18.0 GiB"
        self.assertIsNone(classify_non_media(text))

    def test_an_ebook_card(self) -> None:
        text = "\U0001f4da NEW EBOOK\nTitle: The Name of the Wind\nLink: https://example.invalid/y"
        self.assertIsNone(classify_non_media(text))

    def test_a_magazine_card(self) -> None:
        text = "\U0001f4f0 NEW MAGAZINE\nTitle: Example Geographic\nLink: https://example.invalid/x"
        self.assertIsNone(classify_non_media(text))

    def test_an_audiobook_card(self) -> None:
        text = "\U0001f3a7 NEW AUDIOBOOK\nTitle: Dune\nLink: https://example.invalid/z"
        self.assertIsNone(classify_non_media(text))

    def test_a_plain_author_title_line(self) -> None:
        self.assertIsNone(classify_non_media("Brandon Sanderson - The Way of Kings"))

    def test_a_release_post_the_parser_cannot_read(self) -> None:
        """Structure without a readable title stays a media post, so it lands in
        parse_failed where the operator can see it, not in ignored."""
        text = "Movies → WEB-DL\n4.2 GiB\n50% Free"
        self.assertIsNone(classify_non_media(text))

    def test_a_tracker_card_with_a_header_marker(self) -> None:
        text = (
            "Another Film 2023 2160p UHD BluRay REMUX HDR -TEAM\n"
            f"{MARKER} Movies → BluRay\n"
            "42.1 GiB\n100% Free"
        )
        self.assertIsNone(regex_parser.classify_non_media(text, HEADER_CARD))
        item = regex_parser.parse_message(text, HEADER_CARD)
        self.assertTrue(item.is_valid)
        self.assertEqual(item.title, "Another Film 2023")

    def test_a_header_line_the_parser_cannot_read_a_title_from(self) -> None:
        """The marked header line and nothing to call the post by: it is a
        media post, so it is not ignored, and it is not valid, so it lands in
        parse_failed."""
        text = f"{MARKER} Movies → WEB-DL\n4.2 GiB\n50% Free"
        self.assertTrue(regex_parser.has_release_structure(text, HEADER_CARD))
        self.assertIsNone(regex_parser.classify_non_media(text, HEADER_CARD))
        self.assertFalse(regex_parser.parse_message(text, HEADER_CARD).is_valid)


class ReleaseStructureTests(unittest.TestCase):
    def test_quality_plus_size_counts_as_structure(self) -> None:
        self.assertTrue(has_release_structure("something 1080p something 4.2 GiB"))

    def test_quality_alone_is_already_a_recognised_type(self) -> None:
        self.assertTrue(has_release_structure("Something 1080p WEB-DL"))

    def test_a_sentence_has_no_structure(self) -> None:
        self.assertFalse(has_release_structure("had lunch with my mother today"))


if __name__ == "__main__":
    unittest.main()
