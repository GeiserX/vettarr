"""The request the three delivery clients share, and the one error they raise.

The API key travels in a header, never in a URL, so no address this module
builds, logs or puts in an error can carry it. An error says which service,
what it answered and the service's own short message; never a header.
"""

from __future__ import annotations

from typing import Any

import httpx

from vettarr import dryrun

TIMEOUT_SECONDS = 15.0
_MESSAGE_LIMIT = 200


class DeliveryError(Exception):
    """A delivery target said no, or could not be asked.

    ``status`` is the HTTP status, or None when there was no answer at all (a
    timeout, a refused connection) or nothing was sent. ``str()`` is one line
    a person reads.
    """

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def _upstream_message(resp: httpx.Response) -> str:
    """What the service itself said about the refusal, cut short."""
    try:
        data = resp.json()
    except Exception:
        return ""
    if isinstance(data, list) and data and isinstance(data[0], dict):
        # The arrs answer a validation failure with a list of errors.
        data = data[0]
    if not isinstance(data, dict):
        return ""
    text = data.get("errorMessage") or data.get("message") or data.get("error") or ""
    return str(text)[:_MESSAGE_LIMIT]


class Api:
    """One service's JSON API: a base URL, its auth headers, one error type."""

    def __init__(
        self, name: str, url: str, headers: dict[str, str], http: httpx.AsyncClient | None = None,
    ) -> None:
        self.name = name
        self._base = url.rstrip("/")
        self._headers = headers
        self._http = http or httpx.AsyncClient(timeout=TIMEOUT_SECONDS)

    async def close(self) -> None:
        await self._http.aclose()

    def cookie(self, name: str) -> str | None:
        """A cookie the service set on an earlier answer, or None."""
        return self._http.cookies.get(name)

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        headers: dict[str, str] | None = None,
        missing_ok: bool = False,
    ) -> Any:
        """The decoded JSON answer. ``missing_ok`` turns a 404 into None.

        Under ``dry_run`` anything but a GET is not sent: it is a
        ``would_have`` event, answered with the body it would have sent and
        a made-up id, which is what an add or a request answers.
        """
        if method.upper() != "GET" and dryrun.active():
            dryrun.would_have(self.name, f"{method.upper()} {path}", method=method.upper(), path=path)
            return {**(json if isinstance(json, dict) else {}), "id": dryrun.mark()}
        try:
            resp = await self._http.request(
                method, self._base + path, params=params, json=json,
                headers={**self._headers, **(headers or {})},
            )
        except httpx.TimeoutException:
            raise DeliveryError(f"{self.name} did not answer in time") from None
        except httpx.HTTPError as exc:
            raise DeliveryError(f"{self.name} could not be reached ({type(exc).__name__})") from None
        if resp.status_code == 404 and missing_ok:
            return None
        if resp.status_code == 401:
            raise DeliveryError(f"{self.name} refused the API key (HTTP 401)", 401)
        if not resp.is_success:
            said = _upstream_message(resp)
            raise DeliveryError(
                f"{self.name} answered HTTP {resp.status_code}" + (f": {said}" if said else ""),
                resp.status_code,
            )
        if not resp.content:
            return None
        try:
            return resp.json()
        except Exception:
            raise DeliveryError(
                f"{self.name} answered with something that is not JSON", resp.status_code,
            ) from None


async def arr_profile_id(api: Api, wanted: str | int | None) -> int:
    """The quality profile id Radarr and Sonarr want, from an id or a name.

    A number is the id as it is. A name is looked up in
    ``/api/v3/qualityprofile``, without case; digits that name no profile are
    read as the id.
    """
    if wanted is None or (isinstance(wanted, str) and not wanted.strip()):
        raise DeliveryError(f"integrations.{api.name.lower()}.quality_profile is not set")
    if isinstance(wanted, int) and not isinstance(wanted, bool):
        return wanted
    name = str(wanted).strip()
    profiles = [p for p in await api.request("GET", "/api/v3/qualityprofile") or [] if isinstance(p, dict)]
    for profile in profiles:
        if str(profile.get("name", "")).strip().lower() == name.lower():
            return int(profile["id"])
    if name.isdigit():
        return int(name)
    known = ", ".join(sorted(str(p.get("name")) for p in profiles)) or "none"
    raise DeliveryError(f"{api.name} has no quality profile named {name!r}; it has: {known[:_MESSAGE_LIMIT]}")


async def arr_version(api: Api) -> str:
    """Test connection: ``GET /api/v3/system/status`` answers only to a valid key."""
    status = await api.request("GET", "/api/v3/system/status")
    return str((status or {}).get("version") or "unknown")
