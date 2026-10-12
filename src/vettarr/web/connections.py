"""Test connection: one request to an integration, made when the operator asks.

Each probe sends the smallest request that proves the address answers and
the credential is accepted. Radarr, Sonarr and Seerr are asked by the
delivery code itself (``delivery.test_connection``), with the client an
approved item is handed over with. Nothing here runs on a page load. The answer is
one sentence for the page: it never holds the address, the key, or an
exception's own text, which can carry either.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx

from vettarr.config.schema import Config

TIMEOUT_SECONDS = 8.0
NOT_ENABLED = "It is not enabled in the config file, so there is nothing to test. Save it enabled first."


@dataclass(frozen=True)
class Result:
    ok: bool
    text: str


def _secret(value: Any) -> str:
    return value.get_secret_value() if value is not None else ""


def _base(block: Any) -> str:
    return str(block.url or "").rstrip("/")


def _verdict(resp: httpx.Response, name: str, credential: str = "the API key") -> Result:
    if resp.is_success:
        version = ""
        try:
            body = resp.json()
            if isinstance(body, dict):
                version = str(body.get("version") or body.get("Version") or "")
        except ValueError:
            pass
        return Result(True, f"Connected: {name}{' ' + version if version else ''} accepted {credential}.")
    if resp.status_code in (401, 403):
        return Result(False, f"It answered, and refused {credential} (HTTP {resp.status_code}).")
    return Result(False, f"Something answered at that address with HTTP {resp.status_code}; it may not be {name}.")


async def _prowlarr(http: httpx.AsyncClient, block: Any) -> Result:
    resp = await http.get(f"{_base(block)}/api/v1/system/status", headers={"X-Api-Key": _secret(block.api_key)})
    return _verdict(resp, "Prowlarr")


DELIVERY_TARGETS = {"radarr": "Radarr", "sonarr": "Sonarr", "seerr": "Seerr"}


async def _delivery(name: str, config: Config) -> Result:
    """A delivery target, through the client delivery uses. Its error is one line with no address or key."""
    from vettarr import delivery
    from vettarr.delivery.http import DeliveryError

    try:
        version = await delivery.test_connection(name, config)
    except DeliveryError as exc:
        return Result(False, f"{str(exc).rstrip('.')}.")
    known = f" {version}" if version and version != "unknown" else ""
    return Result(True, f"Connected: {DELIVERY_TARGETS[name]}{known} accepted the API key.")


async def _qbittorrent(http: httpx.AsyncClient, block: Any) -> Result:
    base = _base(block)
    resp = await http.post(
        f"{base}/api/v2/auth/login",
        data={"username": block.username, "password": _secret(block.password)},
        headers={"Referer": base},
    )
    # qBittorrent answers 200 with "Fails." to a wrong password, and 403 once it has banned the address.
    if resp.is_success and resp.text.strip() == "Fails.":
        return Result(False, "It answered, and refused the username or the password.")
    return _verdict(resp, "qBittorrent", "the login")


async def _tmdb(http: httpx.AsyncClient, block: Any) -> Result:
    resp = await http.get("https://api.themoviedb.org/3/configuration", params={"api_key": _secret(block.api_key)})
    return _verdict(resp, "TMDB")


async def _google_books(http: httpx.AsyncClient, block: Any) -> Result:
    params = {"q": "intitle:test", "maxResults": "1"}
    if block.api_key is not None:
        params["key"] = _secret(block.api_key)
    resp = await http.get("https://www.googleapis.com/books/v1/volumes", params=params)
    if resp.status_code == 400 and block.api_key is not None:
        return Result(False, "It answered, and refused the API key (HTTP 400).")
    return _verdict(resp, "Google Books", "the API key" if block.api_key is not None else "a request without a key")


async def _jellyfin(http: httpx.AsyncClient, block: Any) -> Result:
    resp = await http.get(
        f"{_base(block)}/System/Info",
        headers={"Authorization": f'MediaBrowser Token="{_secret(block.api_key)}"'},
    )
    return _verdict(resp, "Jellyfin")


PROBES: dict[str, Callable[[httpx.AsyncClient, Any], Awaitable[Result]]] = {
    "prowlarr": _prowlarr,
    "qbittorrent": _qbittorrent,
    "tmdb": _tmdb,
    "google_books": _google_books,
    "jellyfin": _jellyfin,
}


async def test(name: str, config: Config, transport: httpx.AsyncBaseTransport | None = None) -> Result:
    """Ask the integration ``name`` of ``config`` once. Never raises. ``transport`` is for tests."""
    probe = PROBES.get(name)
    block = getattr(config.integrations, name, None)
    if probe is None and name not in DELIVERY_TARGETS:
        return Result(False, "There is nothing to test for it.")
    if block is None or not block.enabled:
        return Result(False, NOT_ENABLED)
    try:
        if probe is None:
            return await _delivery(name, config)
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS, transport=transport) as http:
            return await probe(http, block)
    except httpx.TimeoutException:
        return Result(False, f"No answer within {TIMEOUT_SECONDS:g} seconds.")
    except httpx.HTTPError as exc:
        # The type only: the exception's text names the address.
        return Result(False, f"Could not connect ({type(exc).__name__}). Is the address right, and reachable from this container?")
    except Exception as exc:
        return Result(False, f"The test itself failed ({type(exc).__name__}).")
