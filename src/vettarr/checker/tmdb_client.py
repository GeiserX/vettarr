from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from functools import cached_property

import httpx

from vettarr.config import settings
from vettarr.models import MediaType

log = logging.getLogger(__name__)

_BASE = "https://api.themoviedb.org/3"
_IMG_BASE = "https://image.tmdb.org/t/p"

# How the TMDB record was found. A record found only after the year was
# dropped is never trusted enough to approve by itself.
MATCH_NONE = "none"
MATCH_WITH_YEAR = "with_year"
MATCH_NO_YEAR = "no_year"
MATCH_YEAR_DROPPED = "year_dropped"

_NON_ALNUM = re.compile(r"[^a-z0-9\s]")
_MULTI_SPACE = re.compile(r"\s{2,}")

# What a search result calls its titles. TMDB names them differently for films
# and for television.
_MOVIE_TITLE_FIELDS = ("title", "original_title")
_TV_TITLE_FIELDS = ("name", "original_name")


def normalise(text: str | None) -> str:
    """Lowercase, accent-free, punctuation-free, single-spaced."""
    t = unicodedata.normalize("NFD", text or "")
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    t = _NON_ALNUM.sub(" ", t.lower())
    return _MULTI_SPACE.sub(" ", t).strip()


@dataclass
class TMDBResult:
    tmdb_id: int | None = None
    imdb_id: str | None = None
    title: str | None = None
    #: The title in its own language, which is what a scene release usually
    #: carries while `title` comes back localised.
    original_title: str | None = None
    #: Release year (films) or first-air year (series) of the matched record.
    year: int | None = None
    #: One of MATCH_NONE / MATCH_WITH_YEAR / MATCH_NO_YEAR / MATCH_YEAR_DROPPED.
    match_mode: str = MATCH_NONE
    poster_url: str | None = None
    backdrop_url: str | None = None
    genres: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    plot: str | None = None
    certifications: dict[str, str] = field(default_factory=dict)
    #: TMDB genre ids, beside the localised names in `genres`. A rule names a
    #: genre by id so it reads the same whatever the language.
    genre_ids: list[int] = field(default_factory=list)
    original_language: str | None = None
    #: Production countries (films) or origin countries (series), ISO 3166-1.
    countries: list[str] = field(default_factory=list)
    runtime: int | None = None
    vote_average: float | None = None
    vote_count: int | None = None
    adult: bool | None = None
    #: Part of a collection (a saga or a sequel). Films only.
    collection: bool | None = None
    #: The first day anyone could watch it, ``YYYY-MM-DD``: the earliest
    #: theatrical, digital, physical or TV release in any country (films; a
    #: festival premiere does not count), or the first air date (series).
    released_on: str | None = None
    cast: list[str] = field(default_factory=list)
    crew: list[str] = field(default_factory=list)
    studios: list[str] = field(default_factory=list)
    #: Series only: TMDB's status ("Ended", "Returning Series", ...), the
    #: episode count of the whole show and of each season.
    status: str | None = None
    number_of_episodes: int | None = None
    season_episodes: dict[int, int] = field(default_factory=dict)
    #: How many records the search returned whose title, normalised, is exactly
    #: the title that was searched for. A post with no year can only be
    #: confirmed when that number is 1: "Suspiria" returns two films called
    #: Suspiria, and nothing in the post says which one it is.
    exact_title_hits: int = 0


def _exact_title_hits(results: list, title: str, fields: tuple[str, str]) -> int:
    """How many search results carry exactly the title that was searched for.

    Normalised on both sides, so accents and punctuation do not count as a
    difference. Both the localised and the original title are looked at,
    because a match on either one is what `identity_problem` accepts.
    """
    wanted = normalise(title)
    if not wanted:
        return 0
    hits = 0
    for record in results:
        if any(normalise(record.get(field)) == wanted for field in fields):
            hits += 1
    return hits


#: TMDB release types a viewer could watch: limited theatrical, theatrical,
#: digital, physical, TV. Type 1, a premiere, is a festival screening.
_VIEWABLE_RELEASE_TYPES = frozenset({2, 3, 4, 5, 6})
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}")


def _day_of(date_text: object) -> str | None:
    """``2007-05-04`` from TMDB's ``2007-05-04T00:00:00.000Z`` or ``2007-05-04``."""
    if isinstance(date_text, str) and _DAY.match(date_text):
        return date_text[:10]
    return None


def _year_of(date_text: object) -> int | None:
    if isinstance(date_text, str) and len(date_text) >= 4 and date_text[:4].isdigit():
        return int(date_text[:4])
    return None


class TMDBClient:
    def __init__(self) -> None:
        self._client = httpx.AsyncClient(timeout=15)

    # Read on first use, not at construction: a module-level client is built
    # at import, before the config is resolved.
    @cached_property
    def _api_key(self) -> str:
        return settings.tmdb_api_key

    @property
    def _language(self) -> str:
        """``integrations.tmdb.language``: titles and genre names come back in it."""
        block = settings.config.integrations.tmdb
        return block.language if block is not None else "en-US"

    async def close(self) -> None:
        await self._client.aclose()

    async def search(self, title: str, year: int | None, media_type: MediaType) -> TMDBResult:
        result = TMDBResult()
        try:
            if media_type == MediaType.SERIES:
                tmdb_id, mode, exact_hits = await self._search_tv(title, year)
                if tmdb_id:
                    result = await self._get_tv_details(tmdb_id)
            else:
                tmdb_id, mode, exact_hits = await self._search_movie(title, year)
                if tmdb_id:
                    result = await self._get_movie_details(tmdb_id)
            if result.tmdb_id:
                result.match_mode = mode
                result.exact_title_hits = exact_hits
        except httpx.HTTPError as exc:
            # Never the error's own text or a traceback: both carry the
            # request URL, and the URL carries the API key.
            status = getattr(getattr(exc, "response", None), "status_code", None)
            log.warning(
                "TMDB search failed for '%s' (%s): %s", title, year,
                f"HTTP {status}" if status else type(exc).__name__,
            )
        except Exception:
            log.exception("TMDB search failed for '%s' (%s)", title, year)
        return result

    async def by_id(self, tmdb_id: int, media_type: MediaType) -> TMDBResult:
        """The record of a title whose TMDB id is known: a Seerr request names its title by it.

        Nothing was searched for, so the record is the title asked for and
        not a match to doubt. Empty when TMDB cannot be asked, like ``search``.
        """
        result = TMDBResult()
        try:
            if media_type == MediaType.SERIES:
                result = await self._get_tv_details(tmdb_id)
            else:
                result = await self._get_movie_details(tmdb_id)
            result.match_mode = MATCH_WITH_YEAR
            result.exact_title_hits = 1
        except httpx.HTTPError as exc:
            # As in `search`: the error's own text carries the API key.
            status = getattr(getattr(exc, "response", None), "status_code", None)
            log.warning(
                "TMDB lookup of id %s failed: %s", tmdb_id, f"HTTP {status}" if status else type(exc).__name__,
            )
        except Exception:
            log.exception("TMDB lookup of id %s failed", tmdb_id)
        return result

    async def _search_movie(
        self, title: str, year: int | None
    ) -> tuple[int | None, str, int]:
        params: dict = {"api_key": self._api_key, "query": title, "language": self._language}
        if year:
            params["year"] = year
        resp = await self._client.get(f"{_BASE}/search/movie", params=params)
        resp.raise_for_status()
        results = resp.json().get("results", [])
        if results:
            return (
                results[0]["id"],
                MATCH_WITH_YEAR if year else MATCH_NO_YEAR,
                _exact_title_hits(results, title, _MOVIE_TITLE_FIELDS),
            )
        if not year:
            return None, MATCH_NONE, 0
        del params["year"]
        resp = await self._client.get(f"{_BASE}/search/movie", params=params)
        resp.raise_for_status()
        results = resp.json().get("results", [])
        if not results:
            return None, MATCH_NONE, 0
        return (
            results[0]["id"],
            MATCH_YEAR_DROPPED,
            _exact_title_hits(results, title, _MOVIE_TITLE_FIELDS),
        )

    async def _search_tv(
        self, title: str, year: int | None
    ) -> tuple[int | None, str, int]:
        params: dict = {"api_key": self._api_key, "query": title, "language": self._language}
        if year:
            params["first_air_date_year"] = year
        resp = await self._client.get(f"{_BASE}/search/tv", params=params)
        resp.raise_for_status()
        results = resp.json().get("results", [])
        if results:
            return (
                results[0]["id"],
                MATCH_WITH_YEAR if year else MATCH_NO_YEAR,
                _exact_title_hits(results, title, _TV_TITLE_FIELDS),
            )
        if not year:
            return None, MATCH_NONE, 0
        del params["first_air_date_year"]
        resp = await self._client.get(f"{_BASE}/search/tv", params=params)
        resp.raise_for_status()
        results = resp.json().get("results", [])
        if not results:
            return None, MATCH_NONE, 0
        return (
            results[0]["id"],
            MATCH_YEAR_DROPPED,
            _exact_title_hits(results, title, _TV_TITLE_FIELDS),
        )

    async def _get_movie_details(self, tmdb_id: int) -> TMDBResult:
        params = {
            "api_key": self._api_key,
            "language": self._language,
            "append_to_response": "keywords,release_dates,external_ids,credits",
        }
        resp = await self._client.get(f"{_BASE}/movie/{tmdb_id}", params=params)
        resp.raise_for_status()
        data = resp.json()

        genres = [g["name"] for g in data.get("genres", [])]
        keywords = [k["name"] for k in data.get("keywords", {}).get("keywords", [])]

        certs: dict[str, str] = {}
        released: list[str] = []
        for rd in data.get("release_dates", {}).get("results", []):
            country = rd.get("iso_3166_1", "")
            for rel in rd.get("release_dates", []):
                cert = rel.get("certification", "")
                if cert:
                    certs[country] = cert
                day = _day_of(rel.get("release_date"))
                if day and rel.get("type") in _VIEWABLE_RELEASE_TYPES:
                    released.append(day)

        result = TMDBResult(
            tmdb_id=tmdb_id,
            imdb_id=data.get("external_ids", {}).get("imdb_id"),
            title=data.get("title"),
            original_title=data.get("original_title"),
            year=_year_of(data.get("release_date")),
            poster_url=f"{_IMG_BASE}/w342{data['poster_path']}" if data.get("poster_path") else None,
            backdrop_url=f"{_IMG_BASE}/w780{data['backdrop_path']}" if data.get("backdrop_path") else None,
            genres=genres,
            keywords=keywords,
            plot=data.get("overview"),
            certifications=certs,
            countries=[
                c.get("iso_3166_1") for c in data.get("production_countries", [])
                if isinstance(c, dict) and c.get("iso_3166_1")
            ],
            runtime=_int(data.get("runtime")),
            adult=data.get("adult") if isinstance(data.get("adult"), bool) else None,
            collection=data.get("belongs_to_collection") is not None,
            released_on=min(released) if released else None,
            **_common(data),
        )
        return result

    async def tvdb_id(self, tmdb_id: int) -> int | None:
        """The TVDB id TMDB lists for a series, or None when it lists none.

        Sonarr and Seerr know a series by it. Raises when TMDB cannot be
        asked; the caller must not log the error's text, which carries the
        address and with it the API key.
        """
        resp = await self._client.get(
            f"{_BASE}/tv/{tmdb_id}/external_ids", params={"api_key": self._api_key},
        )
        resp.raise_for_status()
        return _int(resp.json().get("tvdb_id"))

    async def _get_tv_details(self, tmdb_id: int) -> TMDBResult:
        params = {
            "api_key": self._api_key,
            "language": self._language,
            "append_to_response": "keywords,content_ratings,external_ids,credits",
        }
        resp = await self._client.get(f"{_BASE}/tv/{tmdb_id}", params=params)
        resp.raise_for_status()
        data = resp.json()

        genres = [g["name"] for g in data.get("genres", [])]
        keywords = [k["name"] for k in data.get("keywords", {}).get("results", [])]

        certs: dict[str, str] = {}
        for cr in data.get("content_ratings", {}).get("results", []):
            country = cr.get("iso_3166_1", "")
            rating = cr.get("rating", "")
            if rating:
                certs[country] = rating

        result = TMDBResult(
            tmdb_id=tmdb_id,
            imdb_id=data.get("external_ids", {}).get("imdb_id"),
            title=data.get("name"),
            original_title=data.get("original_name"),
            year=_year_of(data.get("first_air_date")),
            poster_url=f"{_IMG_BASE}/w342{data['poster_path']}" if data.get("poster_path") else None,
            backdrop_url=f"{_IMG_BASE}/w780{data['backdrop_path']}" if data.get("backdrop_path") else None,
            genres=genres,
            keywords=keywords,
            plot=data.get("overview"),
            certifications=certs,
            countries=[c for c in data.get("origin_country", []) if isinstance(c, str)],
            runtime=_int(next(iter(data.get("episode_run_time") or []), None)),
            adult=data.get("adult") if isinstance(data.get("adult"), bool) else None,
            # A series is never part of a TMDB collection.
            collection=False,
            released_on=_day_of(data.get("first_air_date")),
            status=data.get("status") if isinstance(data.get("status"), str) else None,
            number_of_episodes=_int(data.get("number_of_episodes")),
            season_episodes={
                s["season_number"]: s["episode_count"]
                for s in data.get("seasons", [])
                if isinstance(s, dict)
                and isinstance(s.get("season_number"), int)
                and isinstance(s.get("episode_count"), int)
            },
            **_common(data),
        )
        return result


# Crew jobs that name who made the title, for the `crew` signal.
_CREW_JOBS = frozenset({
    "Director", "Screenplay", "Writer", "Story", "Novel", "Producer",
    "Executive Producer", "Original Music Composer", "Creator",
})
_CAST_LIMIT = 20


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _names(rows: object, limit: int | None = None) -> list[str]:
    names: list[str] = []
    for row in rows if isinstance(rows, list) else []:
        name = row.get("name") if isinstance(row, dict) else None
        if isinstance(name, str) and name and name not in names:
            names.append(name)
        if limit is not None and len(names) >= limit:
            break
    return names


def _common(data: dict) -> dict:
    """The fields films and series share, read the same way for both."""
    credits = data.get("credits") if isinstance(data.get("credits"), dict) else {}
    crew = _names([
        row for row in credits.get("crew", [])
        if isinstance(row, dict) and row.get("job") in _CREW_JOBS
    ])
    crew += [name for name in _names(data.get("created_by")) if name not in crew]
    studios = _names(data.get("production_companies"))
    studios += [name for name in _names(data.get("networks")) if name not in studios]
    vote_average = data.get("vote_average")
    return {
        "genre_ids": [
            g["id"] for g in data.get("genres", [])
            if isinstance(g, dict) and isinstance(g.get("id"), int)
        ],
        "original_language": data.get("original_language") or None,
        "vote_average": float(vote_average) if isinstance(vote_average, (int, float)) else None,
        "vote_count": _int(data.get("vote_count")),
        "cast": _names(credits.get("cast"), _CAST_LIMIT),
        "crew": crew,
        "studios": studios,
    }
