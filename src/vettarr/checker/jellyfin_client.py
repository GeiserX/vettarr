"""Is a title already in the Jellyfin library? The `in_library` signal.

Jellyfin is asked for items whose name matches the title, with their
provider ids, and an item counts only when its TMDB provider id is the TMDB
id of the matched record. A name alone never counts: two films share a name
far more often than a TMDB id. Uses ``integrations.jellyfin`` (url, api_key).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

import httpx

log = logging.getLogger(__name__)

_TIMEOUT = 10.0
_LIMIT = 50
_ITEM_TYPES = {"movie": "Movie", "series": "Series"}


def _tmdb_id_of(item: object) -> str | None:
    ids = item.get("ProviderIds") if isinstance(item, dict) else None
    if not isinstance(ids, dict):
        return None
    for key, value in ids.items():
        if str(key).lower() == "tmdb" and value not in (None, ""):
            return str(value).strip()
    return None


class JellyfinClient:
    def __init__(self, url: str, api_key: str, http: httpx.AsyncClient | None = None) -> None:
        self._base = url.rstrip("/")
        self._key = api_key
        self._http = http or httpx.AsyncClient(timeout=_TIMEOUT)

    async def close(self) -> None:
        await self._http.aclose()

    async def has_tmdb_id(
        self, tmdb_id: int, media_type: str, titles: Iterable[str | None]
    ) -> bool | None:
        """True when the library holds this TMDB id, False when it does not.

        None when Jellyfin could not be asked: the signal is then missing,
        never "not in the library".
        """
        wanted = str(tmdb_id)
        item_type = _ITEM_TYPES.get(media_type)
        if item_type is None:
            return None
        seen: set[str] = set()
        for title in titles:
            term = (title or "").strip()
            if not term or term.lower() in seen:
                continue
            seen.add(term.lower())
            params = {
                "searchTerm": term,
                "IncludeItemTypes": item_type,
                "Recursive": "true",
                "Fields": "ProviderIds",
                "Limit": str(_LIMIT),
            }
            try:
                resp = await self._http.get(
                    f"{self._base}/Items",
                    params=params,
                    headers={"Authorization": f'MediaBrowser Token="{self._key}"'},
                )
            except Exception as exc:
                log.warning("Jellyfin lookup failed: %s", type(exc).__name__)
                return None
            if not resp.is_success:
                log.warning("Jellyfin lookup failed: HTTP %s", resp.status_code)
                return None
            try:
                items = resp.json().get("Items")
            except Exception:
                log.warning("Jellyfin lookup: response was not JSON")
                return None
            if any(_tmdb_id_of(item) == wanted for item in items or []):
                return True
        return False if seen else None


_clients: dict[tuple[str, str], JellyfinClient] = {}


def client_for(cfg) -> JellyfinClient | None:
    """The client for ``integrations.jellyfin``, or None while it is off. One per URL and key."""
    if not cfg.integrations.enabled("jellyfin"):
        return None
    block = cfg.integrations.jellyfin
    key = (block.url, block.api_key.get_secret_value())
    if key not in _clients:
        _clients[key] = JellyfinClient(*key)
    return _clients[key]
