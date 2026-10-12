"""Book subject lookup: matching, classification, pacing and failures."""

import asyncio
import json
import logging
import os
import unittest
import urllib.error
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

from vettarr.checker import book_client
from vettarr.checker.book_client import (
    DEFAULT_FORMAT_TAGS,
    BookQuery,
    BookRecord,
    best_match,
    build_query,
    check_book_sync,
    normalise,
    similarity,
)
from vettarr.config.schema import ReleaseWords


class _FakeResponse:
    def __init__(self, body: str):
        self._body = body.encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _openlibrary(docs):
    return json.dumps({"docs": docs})


def _google(items):
    return json.dumps({"items": items})


class QueryTests(unittest.TestCase):
    def test_title_and_author_are_split_on_the_dash(self) -> None:
        """"Title - Author" is the order the group writes, every time."""
        q = build_query("El camino de los reyes - Nombre Ejemplo")
        self.assertEqual(q.title, "El camino de los reyes")
        self.assertEqual(q.author, "Nombre Ejemplo")
        self.assertEqual(q.raw, "El camino de los reyes - Nombre Ejemplo")

    def test_a_bare_title_has_no_author(self) -> None:
        q = build_query("El nombre del viento")
        self.assertIsNone(q.author)
        self.assertEqual(q.title, "El nombre del viento")

    def test_a_hyphenated_word_is_not_a_separator(self) -> None:
        q = build_query("Spider-Man")
        self.assertIsNone(q.author)

    def test_the_last_separator_is_the_one_that_splits(self) -> None:
        """A title can carry a dash of its own; the author never does."""
        q = build_query("Un titulo - con su propio guion - Nombre Ejemplo")
        self.assertEqual(q.title, "Un titulo - con su propio guion")
        self.assertEqual(q.author, "Nombre Ejemplo")


class FormatTagTests(unittest.TestCase):
    """A group may tag audiobooks "Title (MP3-Narrated) - Author". Left in
    the title, the tag gives Open Library zero records. Which words make such
    a tag is `release_words.format_tags`; the group's own word is config."""

    TAGS = (*DEFAULT_FORMAT_TAGS, "narrated", "full cast")

    def test_the_audiobook_shape_is_cleaned(self) -> None:
        raw = "Los habitos atomicos (MP3-Narrated) - Nombre Ejemplo"
        q = build_query(raw, self.TAGS)
        self.assertEqual(q.title, "Los habitos atomicos")
        self.assertEqual(q.author, "Nombre Ejemplo")
        self.assertEqual(q.raw, raw)

    def test_every_format_tag_is_stripped(self) -> None:
        for tag in (
            "(MP3-Narrated)", "(MP3)", "(Full Cast)", "(M4B)", "(EPUB)",
            "(PDF)", "(MOBI)", "(AZW3)", "(CBR)", "(CBZ)", "(epub)",
            "(EPUB, PDF)", "(MP3 - Full Cast)", "[EPUB]", "[PDF/EPUB]",
            "( mp3 )", "(Epub y Pdf)", "(full  cast)",
        ):
            with self.subTest(tag=tag):
                q = build_query(f"Un titulo cualquiera {tag} - Nombre Ejemplo", self.TAGS)
                self.assertEqual(q.title, "Un titulo cualquiera")
                self.assertEqual(q.author, "Nombre Ejemplo")

    def test_a_word_the_config_does_not_list_is_part_of_the_title(self) -> None:
        q = build_query("Un titulo cualquiera (MP3-Narrated) - Nombre Ejemplo")
        self.assertEqual(q.title, "Un titulo cualquiera (MP3-Narrated)")
        self.assertEqual(build_query("Un titulo (MP3) - Nombre Ejemplo", []).title, "Un titulo (MP3)")

    def test_the_default_words_are_the_schema_default(self) -> None:
        self.assertEqual(list(DEFAULT_FORMAT_TAGS), ReleaseWords().format_tags)

    def test_a_tag_anywhere_is_stripped(self) -> None:
        for raw in (
            "(EPUB) Un titulo cualquiera - Nombre Ejemplo",
            "Un titulo cualquiera - Nombre Ejemplo (PDF)",
            "Un titulo (MP3) cualquiera - Nombre Ejemplo",
        ):
            with self.subTest(raw=raw):
                q = build_query(raw)
                self.assertEqual(q.title, "Un titulo cualquiera")
                self.assertEqual(q.author, "Nombre Ejemplo")

    def test_a_bare_title_with_a_tag_is_cleaned(self) -> None:
        q = build_query("El nombre del viento (EPUB)")
        self.assertEqual(q.title, "El nombre del viento")
        self.assertIsNone(q.author)

    def test_a_parenthetical_that_is_part_of_the_title_stays(self) -> None:
        for title in (
            "Sobrevivi (1871)",
            "El principito (Edicion ilustrada)",
            "Manual de PDF (guia practica)",
            "Cuentos (MP3 de regalo)",
            "Historia [Tomo II]",
        ):
            with self.subTest(title=title):
                q = build_query(f"{title} - Nombre Ejemplo")
                self.assertEqual(q.title, title)
                self.assertEqual(q.author, "Nombre Ejemplo")

    def test_the_cleaned_title_is_what_gets_searched(self) -> None:
        body = _openlibrary([])
        with (
            mock.patch.object(book_client, "_sleep", lambda _s: None),
            mock.patch.object(
                book_client.urllib.request, "urlopen",
                side_effect=[_FakeResponse(body), _FakeResponse(_google([]))],
            ) as opener,
        ):
            check_book_sync("Los habitos atomicos (MP3-Narrated) - Nombre Ejemplo", None, ("mp3", "narrated"))
        url = opener.call_args_list[0].args[0].full_url
        self.assertIn("title=Los+habitos+atomicos&", url)
        self.assertNotIn("MP3", url)


class GoogleBooksKeyTests(unittest.TestCase):
    """Keyless Google Books shares one exhausted daily quota. A key fixes that,
    and must never reach a log line."""

    KEY = "test-key-9f3a1c"

    def setUp(self) -> None:
        patches = [
            mock.patch.object(book_client, "_sleep", lambda _s: None),
            mock.patch.dict(book_client._last_request_at, {}, clear=True),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def _google_url(self, api_key):
        with mock.patch.object(
            book_client.urllib.request, "urlopen",
            side_effect=[_FakeResponse(_openlibrary([])), _FakeResponse(_google([]))],
        ) as opener:
            check_book_sync("Cocina de mercado - Nombre Ejemplo", api_key)
        return opener.call_args_list[1].args[0].full_url

    def test_the_key_is_sent_when_configured(self) -> None:
        url = self._google_url(self.KEY)
        self.assertIn("googleapis.com", url)
        self.assertIn(f"key={self.KEY}", url)

    def test_no_key_is_sent_when_not_configured(self) -> None:
        for api_key in (None, ""):
            with self.subTest(api_key=api_key):
                self.assertNotIn("key=", self._google_url(api_key))

    def test_open_library_never_gets_the_key(self) -> None:
        with mock.patch.object(
            book_client.urllib.request, "urlopen",
            side_effect=[_FakeResponse(_openlibrary([])), _FakeResponse(_google([]))],
        ) as opener:
            check_book_sync("Cocina de mercado - Nombre Ejemplo", self.KEY)
        self.assertNotIn("key=", opener.call_args_list[0].args[0].full_url)

    def test_a_failed_request_never_logs_the_key(self) -> None:
        query = BookQuery(raw="X", title="X")

        def http_error(req, **_kwargs):
            raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests", {}, None)

        def odd_error(req, **_kwargs):
            raise RuntimeError(f"unexpected failure fetching {req.full_url}")

        logging.disable(logging.NOTSET)
        self.addCleanup(logging.disable, logging.CRITICAL)
        for failure, expected in ((http_error, "http 429"), (odd_error, "network error")):
            with self.subTest(failure=failure.__name__):
                with (
                    mock.patch.object(
                        book_client.urllib.request, "urlopen", side_effect=failure
                    ),
                    self.assertLogs(book_client.log, level="DEBUG") as logs,
                ):
                    records, error = book_client.search_google_books(query, self.KEY)
                self.assertEqual((records, error), ([], expected))
                self.assertTrue(logs.records)
                # Formatted in full, traceback included: output alone omits it.
                formatter = logging.Formatter()
                for record in logs.records:
                    line = formatter.format(record)
                    self.assertNotIn(self.KEY, line)
                    self.assertNotIn("key=", line)


class NormaliseTests(unittest.TestCase):
    def test_accents_and_case_are_removed(self) -> None:
        self.assertEqual(normalise("El Nombre del Viento"), "el nombre del viento")
        self.assertEqual(normalise("Crónica"), "cronica")

    def test_a_subtitle_is_cut(self) -> None:
        self.assertEqual(
            normalise("El camino de los reyes: Archivo de las tormentas 1"),
            "el camino de los reyes",
        )

    def test_a_parenthetical_is_cut(self) -> None:
        self.assertEqual(normalise("Dune (Dune Chronicles #1)"), "dune")


class SimilarityTests(unittest.TestCase):
    def test_identical_titles_score_one(self) -> None:
        self.assertEqual(similarity("Dune", "dune"), 1.0)

    def test_a_missing_article_still_matches(self) -> None:
        self.assertGreaterEqual(
            similarity("El nombre del viento", "Nombre del viento"),
            book_client.MATCH_THRESHOLD,
        )

    def test_a_different_book_is_far_below_the_line(self) -> None:
        self.assertLess(
            similarity("El nombre del viento", "El temor de un hombre sabio"),
            book_client.MATCH_THRESHOLD,
        )

    def test_an_empty_side_scores_zero(self) -> None:
        self.assertEqual(similarity("", "Dune"), 0.0)


class NoWordListTests(unittest.TestCase):
    def test_no_subject_table_lives_in_the_client(self) -> None:
        """Which subjects matter is the operator's policy (book.subject
        rules), never the catalogue client's."""
        word_sets = [
            name for name, value in vars(book_client).items()
            if not name.startswith("__")
            and isinstance(value, (frozenset, dict)) and len(value) > 10
            and all(isinstance(v, str) for v in value)
        ]
        self.assertEqual(word_sets, [])
        self.assertFalse(hasattr(book_client, "classify_subjects"))


class MatchTests(unittest.TestCase):
    def test_the_closest_record_above_the_line_wins(self) -> None:
        records = [
            BookRecord(title="El temor de un hombre sabio", authors=["Nombre Ejemplo"]),
            BookRecord(title="El nombre del viento", authors=["Nombre Ejemplo"]),
        ]
        match = best_match(records, build_query("El nombre del viento - Nombre Ejemplo"))
        self.assertEqual(match.title, "El nombre del viento")

    def test_the_title_half_is_what_is_compared(self) -> None:
        """The post is "Title - Author"; the catalogue record is just the title."""
        records = [BookRecord(title="El camino de los reyes", authors=["Nombre Ejemplo"])]
        match = best_match(records, build_query("El camino de los reyes - Nombre Ejemplo"))
        self.assertIsNotNone(match)

    def test_nothing_above_the_line_means_no_match(self) -> None:
        records = [BookRecord(title="Una historia completamente distinta",
                              authors=["Nombre Ejemplo"])]
        self.assertIsNone(
            best_match(records, build_query("El nombre del viento - Nombre Ejemplo"))
        )

    def test_a_different_author_disqualifies_a_matching_title(self) -> None:
        records = [BookRecord(title="Dune", authors=["Otro Ejemplo"])]
        self.assertIsNone(
            best_match(records, BookQuery(raw="x", title="Dune", author="Nombre Ejemplo"))
        )

    def test_the_same_author_keeps_the_record(self) -> None:
        records = [BookRecord(title="Dune", authors=["Frank Herbert"])]
        self.assertIsNotNone(
            best_match(records, BookQuery(raw="x", title="Dune", author="Frank Herbert"))
        )

    def test_a_surname_agrees_with_the_full_name(self) -> None:
        records = [BookRecord(title="Dune", authors=["Frank Herbert"])]
        self.assertIsNotNone(
            best_match(records, BookQuery(raw="x", title="Dune", author="Herbert"))
        )

    def test_a_record_without_an_author_cannot_be_confirmed(self) -> None:
        """A book has no parental guide behind it, so the author is the only
        second opinion there is. No author, no automatic decision."""
        records = [BookRecord(title="Dune", authors=[])]
        self.assertIsNone(
            best_match(records, BookQuery(raw="x", title="Dune", author="Nombre Ejemplo"))
        )

    def test_a_post_without_an_author_cannot_be_confirmed(self) -> None:
        records = [BookRecord(title="Dune", authors=["Nombre Ejemplo"])]
        self.assertIsNone(best_match(records, BookQuery(raw="Dune", title="Dune")))


class LookupTests(unittest.TestCase):
    def setUp(self) -> None:
        """Nobody waits two real seconds in a unit test."""
        patches = [
            mock.patch.object(book_client, "_sleep", lambda _s: None),
            mock.patch.dict(book_client._last_request_at, {}, clear=True),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def test_open_library_subjects_are_used(self) -> None:
        body = _openlibrary([
            {"title": "El nombre del viento", "author_name": ["Patrick Rothfuss"],
             "subject": ["Science fiction", "Adventure"], "language": ["spa", "eng"],
             "publisher": ["Editorial Ejemplo"], "number_of_pages_median": 880},
        ])
        with mock.patch.object(book_client.urllib.request, "urlopen",
                               return_value=_FakeResponse(body)) as urlopen:
            verdict = check_book_sync("El nombre del viento - Patrick Rothfuss")
        self.assertTrue(verdict.matched)
        self.assertEqual(verdict.source, "openlibrary")
        self.assertEqual(verdict.subjects, ["Science fiction", "Adventure"])
        self.assertEqual(verdict.languages, ["spa", "eng"])
        self.assertEqual(verdict.publishers, ["Editorial Ejemplo"])
        self.assertEqual(verdict.pages, 880)
        asked = urlopen.call_args[0][0].full_url
        self.assertIn("number_of_pages_median", asked)

    def test_google_books_is_the_second_opinion(self) -> None:
        bodies = [_openlibrary([]), _google([
            {"volumeInfo": {"title": "Cocina de mercado", "authors": ["Nombre Ejemplo"],
                            "categories": ["Cooking"]}},
        ])]
        with mock.patch.object(book_client.urllib.request, "urlopen",
                               side_effect=[_FakeResponse(b) for b in bodies]):
            verdict = check_book_sync("Cocina de mercado - Nombre Ejemplo")
        self.assertTrue(verdict.matched)
        self.assertEqual(verdict.source, "googlebooks")
        self.assertEqual(verdict.subjects, ["Cooking"])

    def test_no_match_anywhere_is_reported_as_unmatched(self) -> None:
        bodies = [_openlibrary([{"title": "Algo distinto"}]), _google([])]
        with mock.patch.object(book_client.urllib.request, "urlopen",
                               side_effect=[_FakeResponse(b) for b in bodies]):
            verdict = check_book_sync("El nombre del viento")
        self.assertFalse(verdict.matched)

    def test_a_match_with_no_subjects_is_reported_empty(self) -> None:
        bodies = [
            _openlibrary([{"title": "Dune", "author_name": ["Nombre Ejemplo"]}]),
            _google([]),
        ]
        with mock.patch.object(book_client.urllib.request, "urlopen",
                               side_effect=[_FakeResponse(b) for b in bodies]):
            verdict = check_book_sync("Dune - Nombre Ejemplo")
        self.assertTrue(verdict.matched)
        self.assertEqual(verdict.subjects, [])

    def test_the_title_and_author_fields_are_searched_not_free_text(self) -> None:
        """Open Library runs Solr, and a leading hyphen in q means NOT, so the
        raw "Author - Title" string the group posts returns nothing at all."""
        body = _openlibrary([])
        with mock.patch.object(book_client.urllib.request, "urlopen",
                               side_effect=[_FakeResponse(body),
                                            _FakeResponse(_google([]))]) as opener:
            check_book_sync("El nombre del viento - Patrick Rothfuss")
        url = opener.call_args_list[0].args[0].full_url
        self.assertIn("title=El+nombre+del+viento", url)
        self.assertIn("author=Patrick+Rothfuss", url)
        self.assertNotIn("q=", url)
        self.assertNotIn("-", url.split("?", 1)[1])

    def test_each_catalogue_is_asked_once(self) -> None:
        with mock.patch.object(book_client.urllib.request, "urlopen",
                               side_effect=[_FakeResponse(_openlibrary([])),
                                            _FakeResponse(_google([]))]) as opener:
            check_book_sync("Patrick Rothfuss - El nombre del viento")
        self.assertEqual(opener.call_count, 2, "one Open Library call, one Google Books call")

    def test_an_open_library_match_survives_a_google_books_rate_limit(self) -> None:
        body = _openlibrary([{"title": "Dune", "author_name": ["Nombre Ejemplo"]}])
        error = urllib.error.HTTPError("u", 429, "Too Many Requests", {}, None)
        with mock.patch.object(book_client.urllib.request, "urlopen",
                               side_effect=[_FakeResponse(body), error]):
            verdict = check_book_sync("Dune - Nombre Ejemplo")
        self.assertTrue(verdict.matched)
        self.assertIsNone(verdict.error)

    def test_a_403_stops_the_lookup_there(self) -> None:
        error = urllib.error.HTTPError("u", 403, "Forbidden", {}, None)
        with mock.patch.object(book_client.urllib.request, "urlopen",
                               side_effect=error) as opener:
            verdict = check_book_sync("Dune")
        self.assertEqual(verdict.error, "http 403")
        self.assertFalse(verdict.matched)
        self.assertEqual(opener.call_count, 1, "no second source after a 403")

    def test_a_429_stops_the_lookup_there(self) -> None:
        error = urllib.error.HTTPError("u", 429, "Too Many Requests", {}, None)
        with mock.patch.object(book_client.urllib.request, "urlopen",
                               side_effect=error) as opener:
            verdict = check_book_sync("Dune")
        self.assertEqual(verdict.error, "http 429")
        self.assertEqual(opener.call_count, 1)

    def test_a_404_still_tries_google_books(self) -> None:
        error = urllib.error.HTTPError("u", 404, "Not Found", {}, None)
        body = _google([{"volumeInfo": {"title": "Dune", "authors": ["Nombre Ejemplo"],
                                        "categories": ["Fiction"]}}])
        with mock.patch.object(book_client.urllib.request, "urlopen",
                               side_effect=[error, _FakeResponse(body)]) as opener:
            verdict = check_book_sync("Dune - Nombre Ejemplo")
        self.assertEqual(opener.call_count, 2)
        self.assertTrue(verdict.matched)

    def test_a_timeout_is_not_a_match(self) -> None:
        with mock.patch.object(book_client.urllib.request, "urlopen",
                               side_effect=TimeoutError()):
            verdict = check_book_sync("Dune")
        self.assertFalse(verdict.matched)
        self.assertEqual(verdict.error, "timeout")

    def test_a_non_json_answer_is_not_a_match(self) -> None:
        with mock.patch.object(book_client.urllib.request, "urlopen",
                               return_value=_FakeResponse("<html>nope</html>")):
            verdict = check_book_sync("Dune")
        self.assertFalse(verdict.matched)

    def test_an_empty_title_asks_for_nothing(self) -> None:
        with mock.patch.object(book_client.urllib.request, "urlopen") as opener:
            verdict = check_book_sync("   ")
        self.assertFalse(verdict.matched)
        opener.assert_not_called()

    def test_check_book_never_raises(self) -> None:
        with mock.patch.object(book_client, "check_book_sync", side_effect=RuntimeError("boom")):
            verdict = asyncio.run(book_client.check_book("Dune"))
        self.assertEqual(verdict.error, "network error")


class PacingTests(unittest.TestCase):
    def test_two_hits_on_one_host_are_two_seconds_apart(self) -> None:
        slept: list[float] = []
        now = [1000.0]

        def fake_sleep(seconds: float) -> None:
            slept.append(seconds)
            now[0] += seconds

        with (
            mock.patch.object(book_client, "_clock", lambda: now[0]),
            mock.patch.object(book_client, "_sleep", fake_sleep),
            mock.patch.dict(book_client._last_request_at, {}, clear=True),
        ):
            book_client._pace("openlibrary.org")
            book_client._pace("openlibrary.org")

        self.assertEqual(len(slept), 1)
        self.assertAlmostEqual(slept[0], book_client._MIN_INTERVAL)

    def test_a_different_host_is_not_made_to_wait(self) -> None:
        slept: list[float] = []
        now = [1000.0]

        with (
            mock.patch.object(book_client, "_clock", lambda: now[0]),
            mock.patch.object(book_client, "_sleep", slept.append),
            mock.patch.dict(book_client._last_request_at, {}, clear=True),
        ):
            book_client._pace("openlibrary.org")
            book_client._pace("www.googleapis.com")

        self.assertEqual(slept, [])


if __name__ == "__main__":
    unittest.main()
