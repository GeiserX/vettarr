"""Sonarr: series, by TVDB id, a season at a time.

``integrations.sonarr`` (url, api_key, root_folder, quality_profile). The API
key is sent as ``X-Api-Key``. Written against Sonarr v4, whose add needs no
language profile.
"""

from __future__ import annotations

from typing import Any

import httpx

from vettarr.delivery.http import Api, DeliveryError, arr_profile_id, arr_version


def season_numbers(series: dict[str, Any]) -> list[int]:
    """The real seasons Sonarr lists for a series: every one but the specials."""
    return [
        s["seasonNumber"] for s in series.get("seasons") or []
        if isinstance(s, dict) and isinstance(s.get("seasonNumber"), int) and s["seasonNumber"] > 0
    ]


def seasons_complete(series: dict[str, Any], wanted: list[int] | None) -> bool:
    """Whether every wanted season has a file for each episode it should have.

    Read from each season's ``statistics``. For a monitored season
    ``episodeCount`` is the episodes that have aired, and ``episodeFileCount``
    how many of them have a file. An unmonitored season counts only the
    episodes it has files for, so it is measured against ``totalEpisodeCount``
    instead. A season with nothing to have yet (not aired) is left out, and
    with no season left there is nothing to call complete.

    ``wanted`` None means the whole series: every season but the specials.
    """
    numbers = set(season_numbers(series) if wanted is None else wanted)
    measured = 0
    for season in series.get("seasons") or []:
        if not isinstance(season, dict) or season.get("seasonNumber") not in numbers:
            continue
        stats = season.get("statistics") or {}
        files = stats.get("episodeFileCount") or 0
        due = (stats.get("episodeCount") if season.get("monitored") else stats.get("totalEpisodeCount")) or 0
        if not due:
            continue
        if files < due:
            return False
        measured += 1
    return measured > 0


def _with_seasons(series: dict[str, Any], wanted: list[int] | None, monitored: bool) -> list[dict[str, Any]]:
    """The series' season list with ``monitored`` set on the wanted ones."""
    numbers = set(season_numbers(series) if wanted is None else wanted)
    return [
        {**s, "monitored": monitored} if s.get("seasonNumber") in numbers else s
        for s in series.get("seasons") or [] if isinstance(s, dict)
    ]


class SonarrClient:
    def __init__(self, url: str, api_key: str, http: httpx.AsyncClient | None = None) -> None:
        self._api = Api("Sonarr", url, {"X-Api-Key": api_key}, http)

    async def close(self) -> None:
        await self._api.close()

    async def test(self) -> str:
        """Test connection: Sonarr's version, or a ``DeliveryError`` saying why not."""
        return await arr_version(self._api)

    async def profile_id(self, wanted: str | int | None) -> int:
        return await arr_profile_id(self._api, wanted)

    async def find(self, tvdb_id: int) -> dict[str, Any] | None:
        """The series Sonarr already holds for this TVDB id, or None."""
        found = await self._api.request("GET", "/api/v3/series", params={"tvdbId": tvdb_id}) or []
        return next(
            (s for s in found if isinstance(s, dict) and s.get("tvdbId") == tvdb_id and s.get("id")), None,
        )

    async def lookup(self, tvdb_id: int) -> dict[str, Any] | None:
        """What Sonarr knows about a TVDB id it does not hold yet, or None."""
        found = await self._api.request("GET", "/api/v3/series/lookup", params={"term": f"tvdb:{tvdb_id}"}) or []
        return next((s for s in found if isinstance(s, dict) and s.get("tvdbId") == tvdb_id), None)

    async def add(
        self, series: dict[str, Any], root_folder: str, profile_id: int, seasons: list[int] | None,
    ) -> dict[str, Any]:
        """Add a looked-up series with the wanted seasons monitored, and search them.

        ``seasons`` None monitors every season but the specials. Each season
        carries its own ``monitored`` and the add names no monitoring preset,
        so Sonarr monitors exactly the episodes of those seasons.
        """
        body = {
            **series,
            "rootFolderPath": root_folder,
            "qualityProfileId": profile_id,
            "monitored": True,
            "seasonFolder": True,
            "seasons": [
                {**s, "monitored": False} for s in series.get("seasons") or [] if isinstance(s, dict)
            ],
            "addOptions": {"searchForMissingEpisodes": True},
        }
        body["seasons"] = _with_seasons(body, seasons, True)
        added = await self._api.request("POST", "/api/v3/series", json=body)
        if not isinstance(added, dict) or not added.get("id"):
            raise DeliveryError("Sonarr took the series and answered without its id")
        return added

    async def get(self, series_id: int | str) -> dict[str, Any] | None:
        """The series by Sonarr's id, with season statistics; None once it is gone."""
        return await self._api.request("GET", f"/api/v3/series/{series_id}", missing_ok=True)

    async def monitor(self, series: dict[str, Any], seasons: list[int] | None) -> None:
        """Turn monitoring on for the series and the wanted seasons, and search them."""
        await self._api.request(
            "PUT", f"/api/v3/series/{series['id']}",
            json={**series, "monitored": True, "seasons": _with_seasons(series, seasons, True)},
        )
        await self._api.request(
            "POST", "/api/v3/command", json={"name": "SeriesSearch", "seriesId": series["id"]},
        )

    async def unmonitor(
        self, series: dict[str, Any], seasons: list[int], *, series_too: bool = False,
    ) -> None:
        """Turn monitoring off for the named seasons, and for the series itself when asked.

        Every other flag goes back as Sonarr gave it. A PUT of the series
        with ``monitored`` changed: no file and no entry is deleted.
        """
        body = {**series, "seasons": _with_seasons(series, list(seasons), False)}
        if series_too:
            body["monitored"] = False
        await self._api.request("PUT", f"/api/v3/series/{series['id']}", json=body)

    async def has_tvdb_id(self, tvdb_id: int, seasons: list[int] | None = None) -> bool | None:
        """True when Sonarr holds the wanted seasons with their files, False when not.

        None when Sonarr could not be asked, as ``JellyfinClient.has_tmdb_id``
        answers: the `in_library` signal is then missing.
        """
        try:
            series = await self.find(tvdb_id)
        except DeliveryError:
            return None
        return bool(series and seasons_complete(series, seasons))
