"""Which titles the duplicate check takes for the same thing.

A magazine filed as an ebook ("NEW EBOOK" over "Workshop Monthly -
October") used to match the 2024 issue already on disk, because the check
dropped years and issue numbers before comparing. Those are what tell one
issue from the next, so they decide first.
"""

import logging
import os
import unittest
from types import SimpleNamespace

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

from vettarr.models import MediaType
from vettarr.pipeline import _dup_title, _edition, titles_match


class EditionTests(unittest.TestCase):
    def test_an_issue_number_is_read_in_every_spelling(self) -> None:
        for title in (
            "Workshop Monthly - Octubre 2024 - Nº 256.pdf",
            "Trail and Tide - N° 167",
            "Clio núm. 298",
            "Coches No. 202",
            "Gadget & PC #214",
        ):
            with self.subTest(title=title):
                self.assertIsNotNone(_edition(title))
                self.assertIsNotNone(_edition(title).issue)

    def test_a_month_names_an_edition(self) -> None:
        edition = _edition("Workshop Monthly - Octubre 2026")
        self.assertEqual((edition.issue, edition.month, edition.year), (None, 10, 2026))

    def test_an_english_month_names_an_edition(self) -> None:
        edition = _edition("Workshop Monthly - October 2026")
        self.assertEqual((edition.issue, edition.month, edition.year), (None, 10, 2026))
        self.assertEqual(_edition("Workshop Monthly - Septiembre 2026"), _edition("Workshop Monthly - September 2026"))
        self.assertEqual(_edition("Workshop Monthly - Setiembre 2026"), _edition("Workshop Monthly - September 2026"))

    def test_a_year_alone_is_not_an_edition(self) -> None:
        self.assertIsNone(_edition("Un mar de oro verde - Emilio Lara - 2026"))
        self.assertIsNone(_edition("La hija - Carmen Laforet"))

    def test_a_word_ending_in_n_is_not_an_issue_marker(self) -> None:
        self.assertIsNone(_edition("Volumen 3 - Autor Ejemplo"))


class PeriodicalMatchTests(unittest.TestCase):
    def test_another_years_english_issue_is_not_a_duplicate(self) -> None:
        """The example in the module docstring, in English."""
        self.assertFalse(titles_match(
            "Workshop Monthly - October 2026", "Workshop Monthly - October 2024", MediaType.EBOOK,
        ))
        self.assertTrue(titles_match(
            "Workshop Monthly - October 2026", "Workshop Monthly - October 2026.pdf", MediaType.EBOOK,
        ))

    def test_another_years_issue_is_not_a_duplicate(self) -> None:
        self.assertFalse(titles_match(
            "Workshop Monthly - Octubre 2026",
            "Workshop Monthly - Octubre 2024 - Nº 256.pdf",
            MediaType.EBOOK,
        ))
        self.assertFalse(titles_match(
            "Trail and Tide - Octubre 2026", "Trail and Tide - Octubre 2022 - Nº 167",
            MediaType.EBOOK,
        ))

    def test_the_same_issue_number_is_a_duplicate(self) -> None:
        self.assertTrue(titles_match(
            "Workshop Monthly - Nº 256", "Workshop Monthly - Octubre 2024 - Nº 256.pdf",
            MediaType.EBOOK,
        ))

    def test_the_same_month_and_year_is_a_duplicate(self) -> None:
        self.assertTrue(titles_match(
            "Trail and Tide - Octubre 2022", "Trail and Tide - Octubre 2022.pdf",
            MediaType.EBOOK,
        ))

    def test_a_different_issue_number_is_not_a_duplicate(self) -> None:
        self.assertFalse(titles_match("Clio Nº 298", "Clio Nº 299", MediaType.EBOOK))

    def test_a_different_periodical_with_the_same_issue_is_not_a_duplicate(self) -> None:
        self.assertFalse(titles_match(
            "Coches Nº 202", "Motos Nº 202", MediaType.EBOOK,
        ))

    def test_a_film_named_after_a_month_is_not_a_periodical(self) -> None:
        """A month in a film or series title is part of the name: the year a
        release adds must not turn the copy already there into another issue."""
        for title, name in (("Octubre Rojo", "Octubre Rojo 1990 1080p"), ("October Sky", "October Sky (1999)")):
            with self.subTest(title=title):
                self.assertTrue(titles_match(title, name, MediaType.MOVIE))

    def test_a_periodical_never_matches_a_book(self) -> None:
        self.assertFalse(titles_match(
            "Octubre - Autor Ejemplo", "Octubre Nº 12", MediaType.EBOOK,
        ))


class BookMatchTests(unittest.TestCase):
    """The rules for everything else are the ones that were there before."""

    def test_case_and_accents_do_not_matter(self) -> None:
        self.assertTrue(titles_match(
            "La Hija - Carmen Laforet", "La hija - Carmen Laforet", MediaType.EBOOK,
        ))

    def test_a_different_book_is_not_a_duplicate(self) -> None:
        self.assertFalse(titles_match(
            "El grito - Yrsa Sigurdardóttir", "El castigo - Yrsa Sigurdardóttir",
            MediaType.EBOOK,
        ))


class DupTitleTests(unittest.TestCase):
    def test_the_post_year_joins_a_title_that_has_none(self) -> None:
        item = SimpleNamespace(title="Workshop Monthly - Octubre", year=2026)
        self.assertEqual(_dup_title(item), "Workshop Monthly - Octubre 2026")

    def test_a_title_with_its_own_year_is_left_alone(self) -> None:
        item = SimpleNamespace(title="Trail and Tide - Octubre 2022", year=2026)
        self.assertEqual(_dup_title(item), "Trail and Tide - Octubre 2022")

    def test_no_year_means_no_change(self) -> None:
        item = SimpleNamespace(title="La hija - Carmen Laforet", year=None)
        self.assertEqual(_dup_title(item), "La hija - Carmen Laforet")


if __name__ == "__main__":
    unittest.main()
