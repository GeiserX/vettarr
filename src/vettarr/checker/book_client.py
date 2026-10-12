"""Catalogue lookup for books, audiobooks and comics.

There is no parental guide for books, so the signals a policy reads about a
book come from the record a catalogue already carries: its subjects, language,
publisher and page count. Open Library first, Google Books as a second
opinion. Open Library is keyless. Google Books takes an optional key, because
keyless requests share one daily quota that stays exhausted.

No confident match, or a catalogue that will not answer, ends in an
unmatched verdict, and the policy treats the book signals as missing. Which
subjects matter is the operator's policy (`book.subject` rules), never this
module's.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import re
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from vettarr.config.schema import ReleaseWords

log = logging.getLogger(__name__)

_OPENLIBRARY_URL = "https://openlibrary.org/search.json"
_GOOGLE_BOOKS_URL = "https://www.googleapis.com/books/v1/volumes"

_USER_AGENT = "vettarr/1.0 (+https://github.com/GeiserX/vettarr)"
_HEADERS = {"Accept": "application/json", "User-Agent": _USER_AGENT}

_TIMEOUT = 15.0
_RESULT_LIMIT = 5

# A title only counts as the same book above this similarity. An exact match
# scores 1.0 and a missing or extra word costs a few points ("el nombre del
# viento" against "nombre del viento" is 0.95), so 0.85 survives accents,
# punctuation and an article, while a different book by the same author lands
# far below it. Anything under the line is handed to a human, not guessed at.
MATCH_THRESHOLD = 0.85

# One request every two seconds to any single host, across every worker thread.
_MIN_INTERVAL = 2.0
_pace_lock = threading.Lock()
_last_request_at: dict[str, float] = {}

# Indirection so the pacing test can drive a fake clock.
_clock = time.monotonic
_sleep = time.sleep

_executor = ThreadPoolExecutor(max_workers=2)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

_SUBTITLE_CUT = re.compile(r"[:(\[].*$", re.DOTALL)
_NON_ALNUM = re.compile(r"[^a-z0-9\s]")
_MULTI_SPACE = re.compile(r"\s{2,}")
_AUTHOR_SEP = re.compile(r"\s+[-–—]\s+")

# A parenthetical or bracket that holds nothing but file formats, like
# "(MP3)" or "[EPUB, PDF]". Left in the title, it gives Open Library zero
# records even for a book it holds. A parenthetical with any other word in it
# ("(1871)", "(Revised edition)") is part of the title. The words are
# `release_words.format_tags`, so a group's own tag ("(MP3-Narrated)") is a
# line of its config, not of this file.
DEFAULT_FORMAT_TAGS: tuple[str, ...] = tuple(ReleaseWords().format_tags)
_FORMAT_JOIN = r"(?:\s*[-,/+&]\s*|\s+(?:y|and)\s+|\s+)"


def _word_source(word: str) -> str:
    """One word as regex source: ``/regex/`` as written, anything else literal."""
    if len(word) > 2 and word.startswith("/") and word.endswith("/"):
        return word[1:-1]
    return r"\s+".join(re.escape(part) for part in word.split())


@functools.lru_cache(maxsize=8)
def _format_tag(words: tuple[str, ...]) -> re.Pattern[str] | None:
    sources = [_word_source(word) for word in words if word.strip()]
    if not sources:
        return None
    one = "(?:" + "|".join(sources) + ")"
    return re.compile(rf"[(\[]\s*{one}(?:{_FORMAT_JOIN}{one})*\s*[)\]]", re.IGNORECASE)


def fold(text: str) -> str:
    """Lowercase, accent-free, punctuation-free. Keeps any subtitle."""
    t = unicodedata.normalize("NFD", text or "")
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    t = _NON_ALNUM.sub(" ", t.lower())
    return _MULTI_SPACE.sub(" ", t).strip()


def normalise(text: str) -> str:
    """Lowercase, accent-free, subtitle-free, punctuation-free."""
    return fold(_SUBTITLE_CUT.sub(" ", text or ""))


def similarity(a: str, b: str) -> float:
    na, nb = normalise(a), normalise(b)
    if not na or not nb:
        return 0.0
    return SequenceMatcher(None, na, nb).ratio()


@dataclass(frozen=True)
class BookQuery:
    """What we think the post is about.

    The parser has no author field, so the author is whatever sits on the left
    of an "Author - Title" separator, which is how the group writes books.
    """

    raw: str
    title: str
    author: str | None = None


def build_query(raw_title: str, format_tags: Iterable[str] | None = None) -> BookQuery:
    """Split "Title - Author", which is the order the group writes.

    Backwards for a long time, which put the author in the title field and
    searched catalogues for a person's name. ``format_tags`` are the words a
    bracket of file formats is made of; ``None`` is the English default.
    """
    raw = (raw_title or "").strip()
    tag = _format_tag(DEFAULT_FORMAT_TAGS if format_tags is None else tuple(format_tags))
    text = _MULTI_SPACE.sub(" ", tag.sub(" ", raw) if tag else raw).strip()
    # The *last* separator, because a title may carry one of its own
    # ("Sobrevivi. El gran incendio, 1871 - Nombre Ejemplo") while the author
    # is always what comes after the final one.
    separators = list(_AUTHOR_SEP.finditer(text))
    if separators:
        cut = separators[-1]
        title, author = text[: cut.start()].strip(), text[cut.end():].strip()
        if title and author:
            return BookQuery(raw=raw, title=title, author=author)
    return BookQuery(raw=raw, title=text, author=None)


@dataclass
class BookRecord:
    title: str
    authors: list[str] = field(default_factory=list)
    subjects: list[str] = field(default_factory=list)
    source: str = ""
    languages: list[str] = field(default_factory=list)
    publishers: list[str] = field(default_factory=list)
    pages: int | None = None


@dataclass
class BookVerdict:
    #: True only when a catalogue record matched the parsed title confidently.
    matched: bool = False
    source: str | None = None
    matched_title: str | None = None
    subjects: list[str] = field(default_factory=list)
    languages: list[str] = field(default_factory=list)
    publishers: list[str] = field(default_factory=list)
    pages: int | None = None
    #: Short reason when the lookup itself failed ("http 429", "timeout", ...).
    error: str | None = None


def _authors_agree(query: BookQuery, record: BookRecord) -> bool:
    """Both sides must name the same person, and both sides must name one.

    This used to abstain when either side had no author, which let a post
    carrying only a title match any book of that name by anybody. A book has
    no parental guide behind it, so the author is the only second opinion
    there is: without one, the record is not trusted and the title decides.
    """
    if not query.author or not record.authors:
        return False
    wanted = {w for w in normalise(query.author).split() if len(w) >= 4}
    if not wanted:
        # Nothing long enough to compare. Unconfirmed, which is a no.
        return False
    for author in record.authors:
        got = {w for w in normalise(author).split() if len(w) >= 4}
        if not got:
            continue
        # The shorter name has to sit entirely inside the longer one, so
        # "Rothfuss" agrees with "Patrick Rothfuss" while "Frank Herbert" and
        # "Brian Herbert" do not: a shared surname is not the same author.
        if wanted <= got or got <= wanted:
            return True
    return False


def best_match(records: list[BookRecord], query: BookQuery) -> BookRecord | None:
    """The closest record above the threshold, or None."""
    best: BookRecord | None = None
    best_score = 0.0
    for record in records:
        if not _authors_agree(query, record):
            continue
        score = similarity(record.title, query.title)
        if score >= MATCH_THRESHOLD and score > best_score:
            best, best_score = record, score
    return best


# ---------------------------------------------------------------------------
# Catalogue lookups
# ---------------------------------------------------------------------------


def _pace(host: str) -> None:
    """Block until at least _MIN_INTERVAL has passed since this host's last hit."""
    with _pace_lock:
        last = _last_request_at.get(host, float("-inf"))
        wait = _MIN_INTERVAL - (_clock() - last)
        if wait > 0:
            _sleep(wait)
        _last_request_at[host] = _clock()


def _get_json(url: str) -> tuple[object | None, str | None]:
    host = urllib.parse.urlsplit(url).hostname or url
    req = urllib.request.Request(url, headers=_HEADERS, method="GET")

    _pace(host)
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        # 403 and 429 mean back off: no retry, and no second source either.
        log.warning("Book lookup on %s: HTTP %s", host, exc.code)
        return None, f"http {exc.code}"
    except TimeoutError:
        log.warning("Book lookup on %s timed out", host)
        return None, "timeout"
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            log.warning("Book lookup on %s timed out", host)
            return None, "timeout"
        log.warning("Book lookup on %s failed: %s", host, exc.reason)
        return None, "network error"
    except Exception as exc:  # noqa: BLE001 -- a lookup never raises
        # The type only: the message or a traceback can quote the URL, and the
        # Google Books URL can carry an API key.
        log.warning("Book lookup request failed on %s: %s", host, type(exc).__name__)
        return None, "network error"

    try:
        return json.loads(raw), None
    except json.JSONDecodeError:
        log.warning("Book lookup on %s: response was not JSON", host)
        return None, "malformed response"


def _backs_off(error: str | None) -> bool:
    return error in {"http 403", "http 429"}


def _strings(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [v.strip() for v in value if isinstance(v, str) and v.strip()]


def _pages(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _openlibrary_params(title: str, author: str | None) -> dict[str, str]:
    """Search the title and author fields, never the free-text q.

    Open Library runs Solr, which reads a leading hyphen in q as NOT: the raw
    "Author - Title" string that the group posts returns zero results.
    """
    params = {"title": title,
              "fields": "title,author_name,subject,language,publisher,number_of_pages_median",
              "limit": str(_RESULT_LIMIT)}
    if author:
        params["author"] = author
    return params


def search_openlibrary(query: BookQuery) -> tuple[list[BookRecord], str | None]:
    params = _openlibrary_params(query.title, query.author)
    payload, error = _get_json(f"{_OPENLIBRARY_URL}?{urllib.parse.urlencode(params)}")
    if error is not None:
        return [], error
    if not isinstance(payload, dict):
        return [], "malformed response"

    records: list[BookRecord] = []
    docs = payload.get("docs")
    for doc in docs if isinstance(docs, list) else []:
        if not isinstance(doc, dict):
            continue
        title = doc.get("title")
        if not isinstance(title, str) or not title.strip():
            continue
        records.append(BookRecord(
            title=title.strip(),
            authors=_strings(doc.get("author_name")),
            subjects=_strings(doc.get("subject")),
            source="openlibrary",
            languages=_strings(doc.get("language")),
            publishers=_strings(doc.get("publisher")),
            pages=_pages(doc.get("number_of_pages_median")),
        ))
    return records, None


def search_google_books(
    query: BookQuery, api_key: str | None = None
) -> tuple[list[BookRecord], str | None]:
    terms = f'intitle:"{query.title}"'
    if query.author:
        terms += f' inauthor:"{query.author}"'
    params = {"q": terms, "maxResults": str(_RESULT_LIMIT), "printType": "books"}
    if api_key:
        params["key"] = api_key
    payload, error = _get_json(f"{_GOOGLE_BOOKS_URL}?{urllib.parse.urlencode(params)}")
    if error is not None:
        return [], error
    if not isinstance(payload, dict):
        return [], "malformed response"

    records: list[BookRecord] = []
    items = payload.get("items")
    for item in items if isinstance(items, list) else []:
        info = item.get("volumeInfo") if isinstance(item, dict) else None
        if not isinstance(info, dict):
            continue
        title = info.get("title")
        if not isinstance(title, str) or not title.strip():
            continue
        records.append(BookRecord(
            title=title.strip(),
            authors=_strings(info.get("authors")),
            subjects=_strings(info.get("categories")),
            source="googlebooks",
            languages=_strings([info.get("language")]),
            publishers=_strings([info.get("publisher")]),
            pages=_pages(info.get("pageCount")),
        ))
    return records, None


def _verdict_from(record: BookRecord) -> BookVerdict:
    return BookVerdict(
        matched=True,
        source=record.source,
        matched_title=record.title,
        subjects=list(record.subjects),
        languages=list(record.languages),
        publishers=list(record.publishers),
        pages=record.pages,
    )


def check_book_sync(
    raw_title: str, google_api_key: str | None = None, format_tags: Iterable[str] | None = None,
) -> BookVerdict:
    query = build_query(raw_title, format_tags)
    if not query.raw:
        return BookVerdict()

    records, error = search_openlibrary(query)
    if _backs_off(error):
        return BookVerdict(error=error)

    match = best_match(records, query)
    if match is not None and match.subjects:
        return _verdict_from(match)

    # Open Library had nothing useful. Google Books is the second opinion.
    google_records, google_error = search_google_books(query, google_api_key)
    if _backs_off(google_error):
        # A match we already hold beats an empty back-off report.
        return _verdict_from(match) if match is not None else BookVerdict(error=google_error)

    google_match = best_match(google_records, query)
    if google_match is not None and google_match.subjects:
        return _verdict_from(google_match)

    if match is not None:
        return _verdict_from(match)
    if google_match is not None:
        return _verdict_from(google_match)
    return BookVerdict(error=error or google_error)


async def check_book(
    raw_title: str, google_api_key: str | None = None, format_tags: Iterable[str] | None = None,
) -> BookVerdict:
    """Classify a book by its catalogue subjects. Never raises."""
    try:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            _executor, check_book_sync, raw_title, google_api_key,
            None if format_tags is None else tuple(format_tags),
        )
    except Exception:
        log.exception("Book lookup failed for '%s'", raw_title)
        return BookVerdict(error="network error")
