"""What decides a post's media type.

The type decides everything that follows: a magazine skips every content
check, downloads, and has its group post deleted afterwards. So the type has
to come from what the group writes, not from an emoji that can turn up in any
message.

The fixtures here are invented, written in the vocabulary of the example
"card" format in config.example.yaml: a written header line, then Title /
Date / Link fields, or a bare scene release name with a quality and a source
in it.
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
from vettarr.models import MediaType
from vettarr.parser.formats import compile_formats
from vettarr.parser.regex_parser import parse_message as _parse_message

EXAMPLE = Path(__file__).resolve().parents[1] / "config.example.yaml"
FORMATS = compile_formats(load(EXAMPLE, env={}).formats)


def parse_message(text: str):
    return _parse_message(text, FORMATS)

NEWSPAPER = "\U0001f4f0"
BOOKS = "\U0001f4da"
BOOK = "\U0001f4d6"
HEADPHONE = "\U0001f3a7"
NOTE = "\U0001f3b5"


def kind(text: str) -> MediaType:
    return parse_message(text).media_type


class WrittenHeaderTests(unittest.TestCase):
    """The header the group actually writes is what names the type."""

    def test_a_magazine_card(self) -> None:
        text = (
            "**NEW MAGAZINE**\n"
            "**Title:** __Example Magazine - May - No 101__\n"
            "**Date:** __2026__\n"
            "**Link:** [__Example Magazine__](https://example.invalid/a)"
        )
        self.assertEqual(kind(text), MediaType.MAGAZINE)

    def test_an_ebook_card(self) -> None:
        text = (
            "**NEW EBOOK**\n"
            "**Title:** __An Invented Book - Example Author__\n"
            "**Year:** __2025__"
        )
        self.assertEqual(kind(text), MediaType.EBOOK)

    def test_an_audiobook_card(self) -> None:
        text = (
            "**NEW AUDIOBOOK**\n"
            "**Title:** __Another Invented Book - Example Author__"
        )
        self.assertEqual(kind(text), MediaType.AUDIOBOOK)

    def test_a_comic_card(self) -> None:
        self.assertEqual(
            kind("NEW COMIC\nTitle: An Invented Comic"), MediaType.COMIC
        )

    def test_a_music_card(self) -> None:
        self.assertEqual(
            kind("NEW ALBUM\nTitle: An Invented Album"), MediaType.MUSIC
        )


class DecoratedCardTests(unittest.TestCase):
    """The card as a bot posts it: marks and markup wrapped around the header,
    an emoji in front of every label, markup around every value."""

    HEADER = "‼️**{}**‼️\n"

    def test_a_magazine_card(self) -> None:
        text = (
            self.HEADER.format("NEW MAGAZINE")
            + "\U0001f3ac **Title:** __Example Magazine - May - No 101__\n"
            "\U0001f4c6 **Date:** __2026__\n"
            "\U0001f517 **Link:** [__Example Magazine__](https://example.invalid/a)"
        )
        item = parse_message(text)
        self.assertEqual(item.media_type, MediaType.MAGAZINE)
        self.assertEqual(item.title, "Example Magazine - May - No 101")

    def test_an_ebook_card(self) -> None:
        text = (
            self.HEADER.format("NEW EBOOK")
            + "\U0001f3ac **Title:** __An Invented Book - Example Author__\n"
            "\U0001f4c6 **Year:** __2025__"
        )
        item = parse_message(text)
        self.assertEqual(
            (item.media_type, item.title, item.year),
            (MediaType.EBOOK, "An Invented Book - Example Author", 2025),
        )

    def test_a_release_file_beats_a_wrapped_magazine_header(self) -> None:
        text = (
            self.HEADER.format("NEW MAGAZINE")
            + "Invented Film (2024) 1080p WEB-DL DD+5.1 H.264-GROUP\n8.4 GiB"
        )
        self.assertEqual(kind(text), MediaType.MOVIE)


class EmojiAloneTests(unittest.TestCase):
    """An emoji on its own must never name a type.

    A note carrying one used to become a magazine: waved through every check,
    searched, downloaded, and its group post deleted afterwards.
    """

    def test_a_note_with_a_newspaper_emoji_is_not_a_magazine(self) -> None:
        self.assertNotEqual(kind(f"{NEWSPAPER} look what they say today"), MediaType.MAGAZINE)

    def test_a_note_with_a_books_emoji_is_not_an_ebook(self) -> None:
        self.assertNotEqual(kind(f"{BOOKS} how many have you read this month"), MediaType.EBOOK)

    def test_a_note_with_an_open_book_emoji_is_not_an_ebook(self) -> None:
        self.assertNotEqual(kind(f"{BOOK} finished it last night"), MediaType.EBOOK)

    def test_a_note_with_a_headphone_emoji_is_not_an_audiobook(self) -> None:
        self.assertNotEqual(kind(f"{HEADPHONE} listening while I walk"), MediaType.AUDIOBOOK)

    def test_a_note_with_a_musical_note_emoji_is_not_music(self) -> None:
        self.assertNotEqual(kind(f"{NOTE} what song is this"), MediaType.MUSIC)

    def test_an_emoji_only_message_names_no_type(self) -> None:
        self.assertEqual(kind(NEWSPAPER), MediaType.UNKNOWN)


class VideoMarkersWinTests(unittest.TestCase):
    """Quality plus source means a release file, whatever emoji rode along."""

    def test_a_film_card_carrying_a_newspaper_emoji_is_still_a_film(self) -> None:
        text = f"{NEWSPAPER} Invented Film (2024) 1080p WEB-DL DD+5.1 H.264-GROUP\n8.4 GiB"
        self.assertEqual(kind(text), MediaType.MOVIE)

    def test_a_film_card_carrying_a_books_emoji_is_still_a_film(self) -> None:
        text = f"Invented Film (2024) 1080p BluRay x264-GROUP {BOOKS}\n8.4 GiB"
        self.assertEqual(kind(text), MediaType.MOVIE)

    def test_a_series_pack_carrying_a_book_emoji_is_still_a_series(self) -> None:
        text = f"{BOOK} Invented Show S03 1080p WEB-DL AAC H.264-GROUP [PACK]\n18.0 GiB"
        self.assertEqual(kind(text), MediaType.SERIES)

    def test_a_film_card_carrying_a_headphone_emoji_is_still_a_film(self) -> None:
        text = f"{HEADPHONE} Invented Film 2019 2160p UHD BluRay REMUX HDR-TEAM\n42.1 GiB"
        self.assertEqual(kind(text), MediaType.MOVIE)

    def test_a_release_file_beats_a_magazine_header(self) -> None:
        """Both signals at once: the one that names a video file wins.

        A magazine is waved through every content check, so a film must never
        be able to ride in as one."""
        text = (
            "**NEW MAGAZINE**\n"
            "Invented Film (2024) 1080p WEB-DL DD+5.1 H.264-GROUP\n8.4 GiB"
        )
        self.assertEqual(kind(text), MediaType.MOVIE)

    def test_a_release_file_beats_an_ebook_header(self) -> None:
        text = (
            "**NEW EBOOK**\n"
            "Invented Film (2024) 1080p BluRay x264-GROUP\n8.4 GiB"
        )
        self.assertEqual(kind(text), MediaType.MOVIE)

    def test_a_magazine_header_still_wins_without_video_markers(self) -> None:
        """The written header keeps its meaning; only a release file outranks it."""
        text = "**NEW MAGAZINE**\nTitle: Example Magazine 2026"
        self.assertEqual(kind(text), MediaType.MAGAZINE)


class FieldsSurviveTests(unittest.TestCase):
    """Narrowing the type rules must not cost the fields that already parse."""

    def test_a_magazine_keeps_its_title(self) -> None:
        text = (
            "**NEW MAGAZINE**\n"
            "Title: Example Magazine - May - No 101\n"
            "Link: https://example.invalid/a"
        )
        item = parse_message(text)
        self.assertEqual(item.media_type, MediaType.MAGAZINE)
        self.assertTrue(item.is_valid)
        self.assertIn("Example Magazine", item.title)

    def test_a_plain_author_title_line_is_still_a_book(self) -> None:
        self.assertEqual(
            kind("An Invented Book - Example Author"), MediaType.EBOOK
        )


if __name__ == "__main__":
    unittest.main()
