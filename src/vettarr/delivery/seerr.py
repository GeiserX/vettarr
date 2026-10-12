"""Seerr (the former Jellyseerr and Overseerr): a request, by TMDB id.

``integrations.seerr`` (url, api_key, user_id). The API key is sent as
``X-Api-Key``. A request is made as ``user_id``, sent as ``X-API-User``: with
the key alone Seerr acts as its admin and approves the request at once, so no
request leaves here without it.

A ``seerr`` source goes the other way: the requests are Seerr's users' own,
and the app approves or declines them. Those two calls, and the list of
pending requests, are made with the key alone: deciding a request takes the
manage-requests permission, which the key's own user (the admin) has.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

import httpx

from vettarr.delivery.http import Api, DeliveryError

Kind = Literal["movie", "tv"]

# Seerr's media status: 5 is available.
AVAILABLE = 5
# Seerr's request status. 5 is completed: the request is over, whatever the
# media's status says now.
REQUEST_PENDING = 1
REQUEST_APPROVED = 2
REQUEST_DECLINED = 3
REQUEST_FAILED = 4

NO_USER = (
    "integrations.seerr.user_id is not set: a request made with the API key alone "
    "would be approved as the Seerr admin, so none is sent"
)

#: How many pending requests one page of the list asks for.
PAGE_SIZE = 50
#: The most pages one read of the list follows: a server that never says it
#: ran out is not followed for ever.
MAX_PAGES = 200


def request_key(source_key: str, request_id: int | str) -> str:
    """``MediaItem.origin_key`` of the item a Seerr request is: one item a request, whichever way it arrived."""
    return f"seerr:{source_key}:{request_id}"


def retired_key(origin_key: str, item_id: int) -> str:
    """The key of an item whose request id Seerr has since given to another request.

    Still the key of a Seerr request, so the item is never sent anywhere
    else, and no longer the key any request in Seerr has.
    """
    return f"{origin_key}:{item_id}"


def request_of(origin_key: str | None) -> tuple[str, int] | None:
    """(source key, request id) of an item that is a Seerr request; None for any other item.

    ``seerr:<source key>:<request id>``, with the item's own id as a fourth
    part once it is retired (``retired_key``).
    """
    parts = (origin_key or "").split(":")
    if len(parts) not in (3, 4) or parts[0] != "seerr":
        return None
    if not all(part.isascii() and part.isdigit() for part in parts[2:]):
        return None
    return parts[1], int(parts[2])


def created_at(request: dict[str, Any]) -> datetime | None:
    """When a request was made, as Seerr dates it (``createdAt``)."""
    try:
        made = datetime.fromisoformat(str(request.get("createdAt") or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return made if made.tzinfo is not None else made.replace(tzinfo=timezone.utc)


class SeerrClient:
    def __init__(
        self, url: str, api_key: str, user_id: int | None, http: httpx.AsyncClient | None = None,
    ) -> None:
        self._api = Api("Seerr", url, {"X-Api-Key": api_key}, http)
        self._user_id = user_id

    async def close(self) -> None:
        await self._api.close()

    def _as_user(self) -> dict[str, str]:
        if self._user_id is None:
            raise DeliveryError(NO_USER)
        return {"X-API-User": str(self._user_id)}

    async def test(self) -> str:
        """Test connection: Seerr's version. ``/settings/about`` answers only to a valid key."""
        about = await self._api.request("GET", "/api/v1/settings/about")
        return str((about or {}).get("version") or "unknown")

    async def media(self, kind: Kind, tmdb_id: int) -> dict[str, Any]:
        """``mediaInfo`` of a title: its status, seasons and requests. Empty when Seerr has none."""
        details = await self._api.request("GET", f"/api/v1/{kind}/{tmdb_id}")
        info = (details or {}).get("mediaInfo")
        return info if isinstance(info, dict) else {}

    async def request(
        self, kind: Kind, tmdb_id: int, *, tvdb_id: int | None = None, seasons: list[int] | None = None,
    ) -> dict[str, Any] | None:
        """Ask for a title as ``user_id``. The request, or None when Seerr made none.

        None says only that: Seerr already had a request for the movie (409),
        or every season asked for is already requested or already has a
        status there, available and partly available included (202). The
        caller asks ``media`` which of the two it was.

        Refused before anything is sent when ``user_id`` is missing. For a
        series ``seasons`` None asks for all of them.
        """
        headers = self._as_user()
        body: dict[str, Any] = {"mediaType": kind, "mediaId": tmdb_id}
        if kind == "tv":
            if tvdb_id is not None:
                body["tvdbId"] = tvdb_id
            body["seasons"] = seasons if seasons else "all"
        try:
            made = await self._api.request("POST", "/api/v1/request", json=body, headers=headers)
        except DeliveryError as exc:
            if exc.status == 409:
                # "Request for this media already exists."
                return None
            raise
        # 202 "No seasons available to request" carries no request.
        return made if isinstance(made, dict) and made.get("id") else None

    async def get_request(self, request_id: int | str) -> dict[str, Any] | None:
        """One request by its id; None once it is gone from Seerr.

        A 404 alone does not say that. A reverse proxy answers 404 for every
        path while the Seerr behind it restarts, and a request read as gone
        rejects its item. So a 404 is believed only when Seerr itself then
        answers (its version, which takes the key) and still has no such
        request. Otherwise this raises: Seerr could not be asked.
        """
        path = f"/api/v1/request/{request_id}"
        found = await self._api.request("GET", path, missing_ok=True)
        if found is not None:
            return found
        try:
            about = await self._api.request("GET", "/api/v1/settings/about")
        except DeliveryError as exc:
            about, said = None, str(exc)
        else:
            said = "no version in its answer"
        if not isinstance(about, dict) or not about.get("version"):
            raise DeliveryError(
                f"Seerr answered 404 for request {request_id} and then did not answer as Seerr ({said}); "
                "the request is not taken as removed"
            )
        return await self._api.request("GET", path, missing_ok=True)

    async def pending_requests(self) -> list[dict[str, Any]]:
        """Every request waiting for a decision, oldest first.

        ``GET /api/v1/request?filter=pending``, page after page until Seerr
        says there are no more. A request that arrives while the pages are
        read can shift them by one, so each request is kept once, by its id.
        """
        found: dict[Any, dict[str, Any]] = {}
        for page in range(MAX_PAGES):
            answer = await self._api.request("GET", "/api/v1/request", params={
                "filter": "pending", "take": PAGE_SIZE, "skip": page * PAGE_SIZE,
                "sort": "added", "sortDirection": "asc",
            })
            results = (answer or {}).get("results") if isinstance(answer, dict) else None
            results = [r for r in results or [] if isinstance(r, dict) and r.get("id") is not None]
            for request in results:
                found.setdefault(request["id"], request)
            total = ((answer or {}).get("pageInfo") or {}).get("results") if isinstance(answer, dict) else None
            if len(results) < PAGE_SIZE or (isinstance(total, int) and (page + 1) * PAGE_SIZE >= total):
                break
        return list(found.values())

    async def approve(self, request_id: int | str) -> None:
        """Approve a pending request, as the admin. Seerr answers 409 for one that is no longer pending."""
        await self._api.request("POST", f"/api/v1/request/{request_id}/approve")

    async def decline(self, request_id: int | str) -> None:
        """Decline a pending request, as the admin. 409 for one that is no longer pending, 404 for one that is gone."""
        await self._api.request("POST", f"/api/v1/request/{request_id}/decline")

    async def withdraw(self, request_id: int | str) -> None:
        """Delete a request, as the user who made it. Seerr allows that only while it is pending."""
        await self._api.request(
            "DELETE", f"/api/v1/request/{request_id}", headers=self._as_user(), missing_ok=True,
        )
