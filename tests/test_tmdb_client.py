"""Reading a TMDB record: is it the right title, and what does it say?

A wrong match hands the parental-guide lookup another film's IMDb id. The
record's own fields (genre ids, language, credits, collection, the series'
status and episode counts) feed the policy's title and series signals.
"""

import asyncio
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

from vettarr.checker.tmdb_client import (
    MATCH_NO_YEAR,
    MATCH_NONE,
    MATCH_WITH_YEAR,
    MATCH_YEAR_DROPPED,
    TMDBClient,
    TMDBResult,
    normalise,
)
from vettarr.models import MediaType


class NormaliseTests(unittest.TestCase):
    def test_accents_case_and_punctuation_fall_away(self) -> None:
        self.assertEqual(normalise("Misión: Imposible"), "mision imposible")
        self.assertEqual(normalise("¿Quién?"), "quien")

    def test_nothing_normalises_to_nothing(self) -> None:
        self.assertEqual(normalise(None), "")
        self.assertEqual(normalise("   "), "")


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeHttp:
    """Answers TMDB searches from a queue, recording the params it was sent."""

    def __init__(self, payloads):
        self._payloads = list(payloads)
        self.calls = []

    async def get(self, url, params=None):
        self.calls.append((url, dict(params or {})))
        return _FakeResponse(self._payloads.pop(0))


class SearchModeTests(unittest.TestCase):
    """How a record was found decides whether it can be trusted on its own."""

    def _search(self, payloads, title, year, media_type):
        client = TMDBClient()
        client._client = _FakeHttp(payloads)
        search = client._search_tv if media_type == MediaType.SERIES else client._search_movie
        result = asyncio.run(search(title, year))
        return result, client._client

    def test_a_hit_with_the_year_is_recorded_as_such(self) -> None:
        (tmdb_id, mode, _hits), http = self._search(
            [{"results": [{"id": 7}]}], "Example", 2024, MediaType.MOVIE
        )
        self.assertEqual((tmdb_id, mode), (7, MATCH_WITH_YEAR))
        self.assertEqual(http.calls[0][1]["year"], 2024)
        self.assertEqual(len(http.calls), 1)

    def test_a_post_without_a_year_searches_once(self) -> None:
        (tmdb_id, mode, _hits), http = self._search(
            [{"results": [{"id": 7}]}], "Example", None, MediaType.MOVIE
        )
        self.assertEqual((tmdb_id, mode), (7, MATCH_NO_YEAR))
        self.assertNotIn("year", http.calls[0][1])

    def test_dropping_the_year_is_recorded_as_a_weaker_match(self) -> None:
        (tmdb_id, mode, _hits), http = self._search(
            [{"results": []}, {"results": [{"id": 9}]}], "Example", 2024, MediaType.MOVIE
        )
        self.assertEqual((tmdb_id, mode), (9, MATCH_YEAR_DROPPED))
        self.assertEqual(len(http.calls), 2)
        self.assertNotIn("year", http.calls[1][1])

    def test_nothing_anywhere_is_no_match(self) -> None:
        (tmdb_id, mode, _hits), _ = self._search(
            [{"results": []}, {"results": []}], "Example", 2024, MediaType.MOVIE
        )
        self.assertEqual((tmdb_id, mode), (None, MATCH_NONE))

    def test_a_series_hit_with_the_first_air_year(self) -> None:
        (tmdb_id, mode, _hits), http = self._search(
            [{"results": [{"id": 3}]}], "Example", 2019, MediaType.SERIES
        )
        self.assertEqual((tmdb_id, mode), (3, MATCH_WITH_YEAR))
        self.assertEqual(http.calls[0][1]["first_air_date_year"], 2019)

    def test_a_series_found_only_without_the_year(self) -> None:
        (tmdb_id, mode, _hits), http = self._search(
            [{"results": []}, {"results": [{"id": 4}]}], "Example", 2019, MediaType.SERIES
        )
        self.assertEqual((tmdb_id, mode), (4, MATCH_YEAR_DROPPED))
        self.assertNotIn("first_air_date_year", http.calls[1][1])


class ExactTitleHitTests(unittest.TestCase):
    """How many records came back under exactly the title that was searched.

    A post with no year is only confirmed when that number is 1, so the count
    has to be right for the localised title, the original title, and accents.
    """

    def _search(self, payloads, title, year, media_type):
        client = TMDBClient()
        client._client = _FakeHttp(payloads)
        search = client._search_tv if media_type == MediaType.SERIES else client._search_movie
        return asyncio.run(search(title, year))

    def test_one_record_under_the_title_counts_once(self) -> None:
        _id, _mode, hits = self._search(
            [{"results": [{"id": 1, "title": "Suspiria", "original_title": "Suspiria"}]}],
            "Suspiria", None, MediaType.MOVIE,
        )
        self.assertEqual(hits, 1)

    def test_two_films_of_the_same_name_count_twice(self) -> None:
        _id, _mode, hits = self._search(
            [{"results": [
                {"id": 1, "title": "Suspiria", "original_title": "Suspiria"},
                {"id": 2, "title": "Suspiria", "original_title": "Suspiria"},
            ]}],
            "Suspiria", None, MediaType.MOVIE,
        )
        self.assertEqual(hits, 2)

    def test_a_sequel_in_the_results_is_not_a_hit(self) -> None:
        _id, _mode, hits = self._search(
            [{"results": [
                {"id": 1, "title": "Gladiator", "original_title": "Gladiator"},
                {"id": 2, "title": "Gladiator II", "original_title": "Gladiator II"},
            ]}],
            "Gladiator", None, MediaType.MOVIE,
        )
        self.assertEqual(hits, 1)

    def test_accents_and_punctuation_are_not_a_difference(self) -> None:
        _id, _mode, hits = self._search(
            [{"results": [{"id": 1, "title": "Misión: Imposible",
                           "original_title": "Mission: Impossible"}]}],
            "Mision Imposible", None, MediaType.MOVIE,
        )
        self.assertEqual(hits, 1)

    def test_a_hit_on_the_original_title_counts(self) -> None:
        _id, _mode, hits = self._search(
            [{"results": [{"id": 1, "title": "Otro nombre",
                           "original_title": "Example"}]}],
            "Example", None, MediaType.MOVIE,
        )
        self.assertEqual(hits, 1)

    def test_a_record_matching_on_both_titles_is_still_one_record(self) -> None:
        _id, _mode, hits = self._search(
            [{"results": [{"id": 1, "title": "Example", "original_title": "Example"}]}],
            "Example", None, MediaType.MOVIE,
        )
        self.assertEqual(hits, 1)

    def test_a_series_is_counted_on_its_own_title_fields(self) -> None:
        """TMDB calls them name and original_name for television."""
        _id, _mode, hits = self._search(
            [{"results": [
                {"id": 1, "name": "Shameless", "original_name": "Shameless"},
                {"id": 2, "name": "Shameless", "original_name": "Shameless"},
            ]}],
            "Shameless", None, MediaType.SERIES,
        )
        self.assertEqual(hits, 2)

    def test_the_count_reaches_the_result_the_policy_reads(self) -> None:
        """The number is no use unless it travels from the search to the record
        the identity check is handed."""
        client = TMDBClient()
        client._client = _FakeHttp([
            {"results": [
                {"id": 5, "title": "Suspiria", "original_title": "Suspiria"},
                {"id": 6, "title": "Suspiria", "original_title": "Suspiria"},
            ]},
            {"id": 5, "title": "Suspiria", "original_title": "Suspiria",
             "release_date": "2018-01-01", "genres": [], "keywords": {"keywords": []},
             "release_dates": {"results": []}, "external_ids": {"imdb_id": "tt5"}},
        ])
        result = asyncio.run(client.search("Suspiria", None, MediaType.MOVIE))
        self.assertEqual(result.exact_title_hits, 2)


class MatchYearTests(unittest.TestCase):
    def test_a_release_date_becomes_a_year(self) -> None:
        from vettarr.checker.tmdb_client import _year_of

        self.assertEqual(_year_of("2024-07-19"), 2024)
        self.assertIsNone(_year_of(""))
        self.assertIsNone(_year_of(None))
        self.assertIsNone(_year_of("soon"))


if __name__ == "__main__":
    unittest.main()


class DetailFieldTests(unittest.TestCase):
    """The fields the title and series signals read, as TMDB sends them."""

    MOVIE = {
        "title": "Ejemplo", "original_title": "Example", "release_date": "2024-05-01",
        "genres": [{"id": 18, "name": "Drama"}, {"id": 10402, "name": "Música"}],
        "keywords": {"keywords": [{"name": "lighthouse"}]},
        "release_dates": {"results": [
            {"iso_3166_1": "ES", "release_dates": [{"certification": "12"}]},
            {"iso_3166_1": "US", "release_dates": [{"certification": ""}, {"certification": "R"}]},
        ]},
        "external_ids": {"imdb_id": "tt7"},
        "original_language": "en",
        "production_countries": [{"iso_3166_1": "GB"}, {"iso_3166_1": "US"}],
        "runtime": 117, "vote_average": 7.4, "vote_count": 2300, "adult": False,
        "belongs_to_collection": {"id": 1, "name": "Saga"},
        "production_companies": [{"name": "Studio A"}],
        "credits": {
            "cast": [{"name": f"Actor {n}"} for n in range(30)],
            "crew": [{"name": "Dir One", "job": "Director"}, {"name": "Grip", "job": "Key Grip"}],
        },
    }
    SERIES = {
        "name": "Serie", "original_name": "Series", "first_air_date": "2019-01-01",
        "genres": [{"id": 10759, "name": "Action & Adventure"}],
        "keywords": {"results": []},
        "content_ratings": {"results": [{"iso_3166_1": "US", "rating": "TV-MA"}]},
        "external_ids": {"imdb_id": "tt9"},
        "original_language": "es", "origin_country": ["ES"],
        "episode_run_time": [45], "vote_average": 8.0, "vote_count": 10,
        "status": "Returning Series", "number_of_episodes": 26,
        "seasons": [{"season_number": 0, "episode_count": 2}, {"season_number": 1, "episode_count": 8},
                    {"season_number": 2, "episode_count": 18}],
        "networks": [{"name": "Channel B"}],
        "created_by": [{"name": "Maker C"}],
        "credits": {"cast": [], "crew": []},
    }

    def _details(self, payload, series=False):
        client = TMDBClient()
        client._client = _FakeHttp([payload])
        fetch = client._get_tv_details if series else client._get_movie_details
        return asyncio.run(fetch(5)), client._client

    def test_a_film_carries_every_title_field(self) -> None:
        result, http = self._details(self.MOVIE)
        self.assertIn("credits", http.calls[0][1]["append_to_response"])
        self.assertEqual(result.genre_ids, [18, 10402])
        self.assertEqual(result.certifications, {"ES": "12", "US": "R"})
        self.assertEqual(result.original_language, "en")
        self.assertEqual(result.countries, ["GB", "US"])
        self.assertEqual(result.runtime, 117)
        self.assertEqual((result.vote_average, result.vote_count), (7.4, 2300))
        self.assertIs(result.adult, False)
        self.assertIs(result.collection, True)
        self.assertEqual(result.studios, ["Studio A"])
        self.assertEqual(result.crew, ["Dir One"])
        self.assertEqual(len(result.cast), 20)

    def test_a_film_outside_any_collection(self) -> None:
        result, _ = self._details({**self.MOVIE, "belongs_to_collection": None})
        self.assertIs(result.collection, False)

    def test_a_series_carries_its_status_and_episode_counts(self) -> None:
        result, _ = self._details(self.SERIES, series=True)
        self.assertEqual(result.status, "Returning Series")
        self.assertEqual(result.number_of_episodes, 26)
        self.assertEqual(result.season_episodes, {0: 2, 1: 8, 2: 18})
        self.assertEqual(result.countries, ["ES"])
        self.assertEqual(result.runtime, 45)
        self.assertEqual(result.studios, ["Channel B"])
        self.assertEqual(result.crew, ["Maker C"])
        self.assertEqual(result.certifications, {"US": "TV-MA"})
        self.assertIs(result.collection, False)

    def test_a_film_is_released_on_its_first_viewable_date(self) -> None:
        """A festival premiere is not a release: nobody outside the festival saw it."""
        payload = {**self.MOVIE, "release_dates": {"results": [
            {"iso_3166_1": "FR", "release_dates": [
                {"certification": "", "type": 1, "release_date": "2024-02-10T00:00:00.000Z"}]},
            {"iso_3166_1": "US", "release_dates": [
                {"certification": "R", "type": 3, "release_date": "2024-06-07T00:00:00.000Z"}]},
            {"iso_3166_1": "ES", "release_dates": [
                {"certification": "12", "type": 4, "release_date": "2024-05-20T00:00:00.000Z"}]},
        ]}}
        result, _ = self._details(payload)
        self.assertEqual(result.released_on, "2024-05-20")

    def test_a_film_with_only_a_premiere_has_no_release(self) -> None:
        payload = {**self.MOVIE, "release_dates": {"results": [
            {"iso_3166_1": "FR", "release_dates": [
                {"certification": "", "type": 1, "release_date": "2024-02-10T00:00:00.000Z"}]},
        ]}}
        result, _ = self._details(payload)
        self.assertIsNone(result.released_on)

    def test_a_series_is_released_on_its_first_air_date(self) -> None:
        result, _ = self._details(self.SERIES, series=True)
        self.assertEqual(result.released_on, "2019-01-01")
        result, _ = self._details({**self.SERIES, "first_air_date": ""}, series=True)
        self.assertIsNone(result.released_on)

    def test_no_word_list_lives_in_the_client(self) -> None:
        """Which keywords matter is the operator's policy, never the client's."""
        import vettarr.checker.tmdb_client as module

        word_sets = [
            name for name, value in vars(module).items()
            if isinstance(value, frozenset) and len(value) > 10
            and all(isinstance(v, str) for v in value)
        ]
        self.assertEqual(word_sets, [])
