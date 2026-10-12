"""Telling a season pack from a film.

A video quality plus a source marker means the post is a release file rather
than a book or a magazine, but it does not say which kind of release. The
season number is only one of the things that says "series": a header line, a
type line and a series hint word say it too, and deciding on the quality
marker alone files a season pack under films, in the films folder, with a
film's Prowlarr search behind it.

The fixtures use the example "card" format from config.example.yaml, plus a
header-line card built here, since the example ships none.
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
from vettarr.models import MediaType
from vettarr.parser.formats import compile_format, compile_formats
from vettarr.parser.regex_parser import parse_message as _parse_message

EXAMPLE = Path(__file__).resolve().parents[1] / "config.example.yaml"
HEADER_CARD = FormatProfile(
    key="header-card",
    kind="card",
    header_markers=["Category:"],
    category_words={"movie": ["Movies"], "series": ["Series"]},
)
FORMATS = compile_formats(load(EXAMPLE, env={}).formats)
HEADER_ONLY = (compile_format(HEADER_CARD),)


def parse_message(text: str):
    return _parse_message(text, FORMATS)


def parse_with_header_card(text: str):
    return _parse_message(text, HEADER_ONLY)


class SeriesSignalsTests(unittest.TestCase):
    def test_an_explicit_series_header_line(self) -> None:
        text = (
            "An Invented Show 1080p WEB-DL AAC H.264-GROUP\n"
            "Category: Series → WEB-DL\n12.0 GiB"
        )
        item = parse_with_header_card(text)
        self.assertEqual(item.media_type, MediaType.SERIES)
        self.assertEqual(item.title, "An Invented Show")

    def test_a_series_hint_word(self) -> None:
        text = "An Invented Show 1080p WEB-DL AAC-GROUP\nNEW SERIES\n12.0 GiB"
        self.assertEqual(parse_message(text).media_type, MediaType.SERIES)

    def test_a_series_hint_word_in_the_middle_of_the_release_line(self) -> None:
        """A hint word counts wherever it is written, not only on its own line."""
        text = "An Invented Show NEW SERIES 1080p WEB-DL AAC-GROUP\n12.0 GiB"
        self.assertEqual(parse_message(text).media_type, MediaType.SERIES)

    def test_a_type_line_naming_series(self) -> None:
        text = (
            "[TAG] An Invented Show 1080p WEB-DL\n"
            "Type: Series - 1080p - WEB-DL - 12.0 GiB"
        )
        self.assertEqual(parse_message(text).media_type, MediaType.SERIES)

    def test_a_season_number(self) -> None:
        text = "An Invented Show S03 1080p WEB-DL AAC H.264-GROUP [PACK]\n18.0 GiB"
        self.assertEqual(parse_message(text).media_type, MediaType.SERIES)

    def test_a_pack_marker(self) -> None:
        text = "An Invented Show 1080p WEB-DL AAC-GROUP [PACK]\n18.0 GiB"
        self.assertEqual(parse_message(text).media_type, MediaType.SERIES)


class FilmSignalsTests(unittest.TestCase):
    """The other direction matters just as much: none of the above must start
    reading an ordinary film card as a series."""

    def test_a_plain_release_line(self) -> None:
        text = "An Invented Film (2024) 1080p WEB-DL DD+5.1 H.264-GROUP\n8.4 GiB"
        self.assertEqual(parse_message(text).media_type, MediaType.MOVIE)

    def test_a_movies_header_line(self) -> None:
        text = (
            "An Invented Film 2024 1080p WEB-DL-GROUP\n"
            "Category: Movies → WEB-DL\n8.4 GiB"
        )
        item = parse_with_header_card(text)
        self.assertEqual(item.media_type, MediaType.MOVIE)
        self.assertEqual(item.source, "WEB-DL")

    def test_a_type_line_naming_movies(self) -> None:
        text = (
            "[TAG] An Invented Film 2024 1080p WEB-DL\n"
            "Type: Movies - 1080p - WEB-DL - 8.4 GiB"
        )
        self.assertEqual(parse_message(text).media_type, MediaType.MOVIE)


if __name__ == "__main__":
    unittest.main()
