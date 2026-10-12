from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from functools import cached_property

import httpx

from vettarr.config import settings
from vettarr.config.schema import Provider
from vettarr.models import MediaType
from vettarr.searcher.providers import on_provider

log = logging.getLogger(__name__)

# The standard Newznab category of each media type, sent when
# `integrations.prowlarr.categories` does not list the type. A type listed
# with `[]` searches every category.
STANDARD_CATEGORIES = {
    MediaType.MOVIE: [2000],
    MediaType.SERIES: [5000],
    MediaType.AUDIOBOOK: [3030],
    MediaType.EBOOK: [7020],
    MediaType.MAGAZINE: [7010],
    MediaType.COMIC: [7030],
    MediaType.MUSIC: [3000],
}


def categories_for(media_type: MediaType) -> list[int]:
    """The Prowlarr categories a search for `media_type` sends."""
    block = settings.config.integrations.prowlarr
    configured = block.categories if block is not None else {}
    name = getattr(media_type, "value", media_type)
    if name in configured:
        return list(configured[name])
    return STANDARD_CATEGORIES.get(media_type, [])


@dataclass
class TorrentResult:
    guid: str = ""
    title: str = ""
    size: int = 0
    seeders: int = 0
    leechers: int = 0
    indexer: str = ""
    download_url: str | None = None
    magnet_url: str | None = None
    info_url: str | None = None
    #: Set on a release a provider's direct search found: its `.torrent`
    #: needs that provider's session, so it is fetched through the provider.
    provider: str | None = None
    #: The torrent's id on that provider.
    provider_id: str | None = None


@dataclass
class SearchResult:
    results: list[TorrentResult] = field(default_factory=list)
    query: str = ""
    error: str | None = None


_UNWANTED_QUALITY_RE = re.compile(
    r"\b(2160[pP]|4[kK]|UHD|Full\s*Blu[\-\s]?[Rr]ay)\b",
    re.IGNORECASE,
)

_REMUX_RE = re.compile(r"\b(REMUX|BDRemux)\b", re.IGNORECASE)

_MARKDOWN_RE = re.compile(r"[*_]{1,3}")
_PUNCTUATION_RE = re.compile(r"[¡¿!?\.\,\;\:\"\'…\(\)\[\]\{\}]")


def clean_title(title: str) -> str:
    """Strip markdown formatting, punctuation, and normalize whitespace."""
    title = _MARKDOWN_RE.sub("", title)
    title = _PUNCTUATION_RE.sub(" ", title)
    return " ".join(title.split()).strip()


# How long the indexer list is kept before Prowlarr is asked again. A tracker
# cookie changes when somebody logs in again, not between two posts.
_INDEXER_CACHE_SECONDS = 300


class ProwlarrClient:
    def __init__(self) -> None:
        self._client = httpx.AsyncClient(timeout=30)
        self._indexers: list[dict] = []
        self._indexers_at: float = 0.0

    # Read on first use, not at construction: a module-level client is built
    # at import, before the config is resolved.
    @cached_property
    def _base(self) -> str:
        return settings.prowlarr_url.rstrip("/")

    @cached_property
    def _key(self) -> str:
        return settings.prowlarr_api_key

    async def close(self) -> None:
        await self._client.aclose()

    async def _list_indexers(self) -> list[dict]:
        now = time.monotonic()
        if self._indexers and now - self._indexers_at < _INDEXER_CACHE_SECONDS:
            return self._indexers
        try:
            resp = await self._client.get(
                f"{self._base}/api/v1/indexer", headers={"X-Api-Key": self._key}
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            log.exception("Prowlarr indexer list failed")
            return self._indexers
        if isinstance(data, list):
            self._indexers = data
            self._indexers_at = now
        return self._indexers

    async def cookie_for(self, provider: Provider) -> str | None:
        """The cookie of the Prowlarr indexer logged in to `provider`, or None.

        For `auth.from: prowlarr`: Prowlarr is the one place that tracker's
        session lives, so a cookie refreshed in Prowlarr is refreshed here too.
        The indexer is the one whose base URL is on one of the provider's
        hosts (`base_url` and `aliases`), compared exactly. An indexer on any
        other host, one that merely carries the tracker's name or sits on a
        subdomain, gives nothing.
        """
        for indexer in await self._list_indexers():
            fields = {f.get("name"): f.get("value") for f in indexer.get("fields", [])}
            cookie = fields.get("cookie")
            base = fields.get("baseUrl") or next(iter(indexer.get("indexerUrls") or []), None)
            if not cookie or not base or not indexer.get("enable", True):
                continue
            if on_provider(provider, base):
                return cookie
        return None

    async def search(
        self,
        query: str,
        media_type: MediaType = MediaType.UNKNOWN,
        limit: int = 25,
    ) -> SearchResult:
        # One list of pairs, so each category goes out as its own
        # `categories=` parameter. httpx replaces a query written into the URL
        # with `params`, which is how the categories used to be dropped.
        params: list[tuple[str, str | int]] = [("query", query), ("limit", limit)]
        params += [("categories", c) for c in categories_for(media_type)]

        headers = {"X-Api-Key": self._key}

        try:
            resp = await self._client.get(
                f"{self._base}/api/v1/search", params=params, headers=headers
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            log.exception("Prowlarr search failed for '%s'", query)
            return SearchResult(query=query, error=type(exc).__name__)

        results = []
        for item in data:
            title = item.get("title", "")
            if _UNWANTED_QUALITY_RE.search(title):
                continue
            results.append(
                TorrentResult(
                    guid=item.get("guid", ""),
                    title=title,
                    size=item.get("size", 0),
                    seeders=item.get("seeders", 0),
                    leechers=item.get("leechers", 0),
                    indexer=item.get("indexer", ""),
                    download_url=item.get("downloadUrl"),
                    magnet_url=item.get("magnetUrl"),
                    info_url=item.get("infoUrl"),
                )
            )

        results = [r for r in results if r.seeders > 0]
        # Sort: non-remux first (by size), then remux (by size)
        results.sort(key=lambda r: (
            1 if _REMUX_RE.search(r.title) else 0,
            r.size if r.size > 0 else float("inf"),
        ))
        return SearchResult(results=results[:limit], query=query)

    @staticmethod
    def build_query(
        title: str,
        year: int | None = None,
        season: str | None = None,
    ) -> str:
        parts = [clean_title(title)]
        if year:
            parts.append(str(year))
        if season:
            parts.append(season)
        return " ".join(parts)
