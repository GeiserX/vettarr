"""AudioBookRequest: audiobook requests, by Audible ASIN.

``integrations.abreq`` (url, api_key). The key is sent as
``Authorization: Bearer <key>``, and its user must be an admin: only an admin
sees every user's requests, and deletes every user's request for a book.

AudioBookRequest has no pending, approved or declined state. A request is a
row per (book, user) while the book waits, and the book has one flag,
``downloaded`` (``fulfilled`` from 2.0 on), that turns true once a download
was handed to the download client. So an ``abreq`` source approves a request
by asking AudioBookRequest to download the book (``auto_download``), and
declines it by deleting it (``delete``).

Every call with an API key makes AudioBookRequest check the key against every
key it stores and write when it was last used, so the list is read once a
poll, whole (``wishlist``), and a single book is found in it.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

import httpx

from vettarr.delivery.http import Api, DeliveryError

#: An Audible ASIN: ten capital letters and digits.
ASIN = re.compile(r"[A-Z0-9]{10}")
#: The longest an ``auto-download`` call may take. AudioBookRequest searches
#: Prowlarr inside the call, with a 30 s timeout of its own, then grabs.
AUTO_DOWNLOAD_SECONDS = 120.0
#: The list filter that reads every book with a request, downloaded or not.
#: 1.x knows ``all``, ``downloaded`` and ``not_downloaded``; 2.x keeps them.
ALL = "all"


def request_key(source_key: str, asin: str) -> str:
    """``MediaItem.origin_key`` of the item a book's requests are: one item a book, whichever way it arrived."""
    return f"abreq:{source_key}:{asin}"


def retired_key(origin_key: str, item_id: int) -> str:
    """The key of an item whose book was asked for again after its request was deleted.

    Still the key of an AudioBookRequest request, so the item is never sent
    anywhere else, and no longer the key the book's new item has.
    """
    return f"{origin_key}:{item_id}"


def request_of(origin_key: str | None) -> tuple[str, str] | None:
    """(source key, ASIN) of an item that is an AudioBookRequest request; None for any other item."""
    parts = (origin_key or "").split(":")
    if len(parts) not in (3, 4) or parts[0] != "abreq" or not ASIN.fullmatch(parts[2]):
        return None
    if len(parts) == 4 and not (parts[3].isascii() and parts[3].isdigit()):
        return None
    return parts[1], parts[2]


def retired(origin_key: str | None) -> bool:
    """Whether the item's book has a newer item: nothing it decides is sent to AudioBookRequest."""
    return request_of(origin_key) is not None and len((origin_key or "").split(":")) == 4


def asin_of(entry: Any) -> str | None:
    """The ASIN of one entry of the list, or None when it has none that is an ASIN."""
    book = entry.get("book") if isinstance(entry, dict) else None
    asin = str((book or {}).get("asin") or "").strip() if isinstance(book, dict) else ""
    return asin if ASIN.fullmatch(asin) else None


def fulfilled(entry: dict[str, Any]) -> bool:
    """Whether the book was handed to the download client: ``fulfilled`` from 2.0, ``downloaded`` before."""
    book = entry.get("book") if isinstance(entry.get("book"), dict) else {}
    flag = book.get("fulfilled", book.get("downloaded"))
    return flag is True


def when(value: Any) -> datetime | None:
    """A date AudioBookRequest sent. It writes them without a zone; they are read as UTC."""
    try:
        made = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return made if made.tzinfo is not None else made.replace(tzinfo=timezone.utc)


class AbreqClient:
    def __init__(self, url: str, api_key: str, http: httpx.AsyncClient | None = None) -> None:
        self._api = Api("AudioBookRequest", url, {"Authorization": f"Bearer {api_key}"}, http)

    async def close(self) -> None:
        await self._api.close()

    async def test(self) -> str:
        """Test connection: the key's own user, who must be an admin to decide requests."""
        me = await self._api.request("GET", "/api/users/me")
        group = str((me or {}).get("group") or "") if isinstance(me, dict) else ""
        if group != "admin":
            raise DeliveryError(
                f"AudioBookRequest accepted the API key, but its user is {group or 'not an admin'}; "
                "deciding every user's requests needs an admin's key"
            )
        return "unknown"

    async def wishlist(self) -> dict[str, dict[str, Any]]:
        """Every book somebody asked for, downloaded or not, by ASIN. One call."""
        found = await self._api.request("GET", "/api/requests", params={"filter": ALL})
        if not isinstance(found, list):
            raise DeliveryError("AudioBookRequest answered its list of requests with something that is not a list")
        return {asin: entry for entry in found if (asin := asin_of(entry)) is not None}

    async def entry(self, asin: str) -> dict[str, Any] | None:
        """One book's entry of the list; None once nobody asks for it any more."""
        return (await self.wishlist()).get(asin)

    async def auto_download(self, asin: str) -> None:
        """Ask AudioBookRequest to search for the book and download the best release, now.

        It answers 204 also when it found nothing, when the book is already
        downloaded and when a search for it is already running, so the caller
        reads the book's flag again. A 302 (to its Prowlarr settings) means
        Prowlarr is not set up there; redirects are never followed.
        """
        try:
            await self._api.request(
                "POST", f"/api/requests/{asin}/auto-download", timeout=AUTO_DOWNLOAD_SECONDS,
            )
        except DeliveryError as exc:
            if exc.status is not None and 300 <= exc.status < 400:
                raise DeliveryError(
                    "AudioBookRequest has no working Prowlarr set up, so it cannot search for the book",
                    exc.status,
                ) from None
            raise

    async def delete(self, asin: str) -> None:
        """Delete every user's request for the book. AudioBookRequest tells nobody."""
        await self._api.request("DELETE", f"/api/requests/{asin}")
