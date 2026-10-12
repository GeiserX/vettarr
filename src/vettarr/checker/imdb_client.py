from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from vettarr.models import SeverityRating

log = logging.getLogger(__name__)

# IMDb's own website talks to this endpoint. The /parentalguide HTML page is
# behind a JavaScript WAF challenge that answers 202 with no data, so scraping
# it silently produced empty guides for every title.
_GRAPHQL_URL = "https://caching.graphql.imdb.com/"

# severityBreakdown is how many people voted for each severity, not just the
# one that won. IMDb's own site shows it, and it is the difference between
# "labelled Mild" and "labelled Mild, with 45% of the votes at Moderate or
# worse".
_QUERY = (
    "query($id:ID!){ title(id:$id){ titleText{text} parentsGuide{ categories{"
    " category{id} severity{text votedFor} totalSeverityVotes"
    " severityBreakdown{ text votedFor } } } } }"
)

# Only the client-name header is needed: without it every host answers 403.
# No browser User-Agent, Origin or Referer is sent; the request says what it
# is. This is IMDb's undocumented website API, whose terms forbid automated
# use without consent (docs/CONFIG.md, integrations.imdb_guide).
_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/graphql+json, application/json",
    "x-imdb-client-name": "imdb-web-next-localized",
}

_SEVERITY_MAP = {
    "none": SeverityRating.NONE,
    "mild": SeverityRating.MILD,
    "moderate": SeverityRating.MODERATE,
    "severe": SeverityRating.SEVERE,
}

_CATEGORY_ATTR = {
    "NUDITY": "sex_rating",
    "VIOLENCE": "violence_rating",
    "PROFANITY": "profanity_rating",
    "FRIGHTENING": "frightening_rating",
    "ALCOHOL": "alcohol_rating",
}

_TIMEOUT = 15.0

# One request every two seconds to imdb.com, across every worker thread.
_MIN_INTERVAL = 2.0
_pace_lock = threading.Lock()
_last_request_at = float("-inf")

# Indirection so the pacing test can drive a fake clock.
_clock = time.monotonic
_sleep = time.sleep

_executor = ThreadPoolExecutor(max_workers=2)


@dataclass
class ParentsGuide:
    sex_rating: SeverityRating | None = None
    violence_rating: SeverityRating | None = None
    profanity_rating: SeverityRating | None = None
    frightening_rating: SeverityRating | None = None
    alcohol_rating: SeverityRating | None = None
    #: True only when IMDb answered with a parents guide for this title. A
    #: guide with fetched=False carries no information at all: every rating is
    #: None because nobody answered, not because the title is clean.
    fetched: bool = False
    #: Category id -> total severity votes, for whatever IMDb returned.
    votes: dict[str, int] = field(default_factory=dict)
    #: Category id -> severity label -> votes cast for that label. Empty when
    #: IMDb sent no per-severity breakdown.
    breakdown: dict[str, dict[str, int]] = field(default_factory=dict)
    #: Short reason when fetched is False.
    error: str | None = None


def _canonical_id(imdb_id: str) -> str:
    imdb_id = (imdb_id or "").strip()
    return imdb_id if imdb_id.startswith("tt") else f"tt{imdb_id}"


def _pace() -> None:
    """Block until at least _MIN_INTERVAL has passed since the last request."""
    global _last_request_at
    with _pace_lock:
        wait = _MIN_INTERVAL - (_clock() - _last_request_at)
        if wait > 0:
            _sleep(wait)
        _last_request_at = _clock()


def _post_graphql(imdb_id: str) -> tuple[object | None, str | None]:
    """POST the parents guide query. Returns (payload, error)."""
    body = json.dumps({"query": _QUERY, "variables": {"id": imdb_id}}).encode("utf-8")
    req = urllib.request.Request(_GRAPHQL_URL, data=body, headers=_HEADERS, method="POST")

    _pace()
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        # 403 and 429 mean IMDb wants us to back off: no retry inside the call.
        return None, f"http {exc.code}"
    except TimeoutError:
        return None, "timeout"
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            return None, "timeout"
        return None, "network error"
    except Exception:
        return None, "network error"

    try:
        return json.loads(raw), None
    except json.JSONDecodeError:
        return None, "malformed response"


def _severity_breakdown(node: object) -> dict[str, int]:
    """Severity label -> votes, from IMDb's per-severity breakdown list."""
    if not isinstance(node, list):
        return {}
    spread: dict[str, int] = {}
    for entry in node:
        if not isinstance(entry, dict):
            continue
        text = entry.get("text")
        count = entry.get("votedFor")
        if isinstance(text, str) and text.strip() and isinstance(count, int) and count >= 0:
            spread[text.strip().lower()] = count
    return spread


def _parse_payload(imdb_id: str, payload: object) -> ParentsGuide:
    if not isinstance(payload, dict):
        return ParentsGuide(error="malformed response")

    data = payload.get("data")
    if data is not None and not isinstance(data, dict):
        return ParentsGuide(error="malformed response")

    title = data.get("title") if isinstance(data, dict) else None
    if title is None:
        errors = payload.get("errors")
        if not errors and not isinstance(data, dict):
            return ParentsGuide(error="malformed response")
        return ParentsGuide(error="title not found")
    if not isinstance(title, dict):
        return ParentsGuide(error="malformed response")

    guide_node = title.get("parentsGuide")
    if not isinstance(guide_node, dict):
        return ParentsGuide(error="title not found")

    guide = ParentsGuide(fetched=True)

    categories = guide_node.get("categories")
    if not isinstance(categories, list):
        categories = []

    for cat in categories:
        if not isinstance(cat, dict):
            continue
        category = cat.get("category")
        cat_id = category.get("id") if isinstance(category, dict) else None
        if not isinstance(cat_id, str) or not cat_id:
            continue

        total_votes = cat.get("totalSeverityVotes")
        votes = total_votes if isinstance(total_votes, int) else 0
        guide.votes[cat_id] = votes

        spread = _severity_breakdown(cat.get("severityBreakdown"))
        if spread:
            guide.breakdown[cat_id] = spread

        attr = _CATEGORY_ATTR.get(cat_id)
        if not attr or votes <= 0:
            continue

        severity = cat.get("severity")
        text = severity.get("text") if isinstance(severity, dict) else None
        rating = _SEVERITY_MAP.get(text.lower().strip()) if isinstance(text, str) else None
        if rating is not None:
            setattr(guide, attr, rating)

    return guide


def _fetch_parents_guide_sync(imdb_id: str) -> ParentsGuide:
    canonical = _canonical_id(imdb_id)
    payload, error = _post_graphql(canonical)
    guide = ParentsGuide(error=error) if error is not None else _parse_payload(canonical, payload)
    # The one line this module logs, at debug: the title and how it went.
    log.debug("Parents guide %s: %s", canonical, "fetched" if guide.fetched else guide.error)
    return guide


async def fetch_parents_guide(imdb_id: str) -> ParentsGuide:
    """Fetch the IMDb parents guide. Never raises: failures come back as
    fetched=False with error set."""
    try:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(_executor, _fetch_parents_guide_sync, imdb_id)
    except Exception:
        return ParentsGuide(error="network error")
