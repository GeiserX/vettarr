"""Radarr: movies, by TMDB id.

``integrations.radarr`` (url, api_key, root_folder, quality_profile). The API
key is sent as ``X-Api-Key``.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import httpx

from vettarr.delivery.http import Api, DeliveryError, arr_profile_id, arr_version


class RadarrClient:
    def __init__(self, url: str, api_key: str, http: httpx.AsyncClient | None = None) -> None:
        self._api = Api("Radarr", url, {"X-Api-Key": api_key}, http)

    async def close(self) -> None:
        await self._api.close()

    async def test(self) -> str:
        """Test connection: Radarr's version, or a ``DeliveryError`` saying why not."""
        return await arr_version(self._api)

    async def profile_id(self, wanted: str | int | None) -> int:
        return await arr_profile_id(self._api, wanted)

    async def find(self, tmdb_id: int) -> dict[str, Any] | None:
        """The movie Radarr already holds for this TMDB id, or None."""
        movies = await self._api.request("GET", "/api/v3/movie", params={"tmdbId": tmdb_id}) or []
        return next(
            (m for m in movies if isinstance(m, dict) and m.get("tmdbId") == tmdb_id and m.get("id")), None,
        )

    async def lookup(self, tmdb_id: int) -> dict[str, Any] | None:
        """What Radarr knows about a TMDB id it does not hold yet, or None."""
        found = await self._api.request("GET", "/api/v3/movie/lookup", params={"term": f"tmdb:{tmdb_id}"}) or []
        return next((m for m in found if isinstance(m, dict) and m.get("tmdbId") == tmdb_id), None)

    async def add(self, movie: dict[str, Any], root_folder: str, profile_id: int) -> dict[str, Any]:
        """Add a looked-up movie, monitored, and start its search."""
        body = {
            **movie,
            "rootFolderPath": root_folder,
            "qualityProfileId": profile_id,
            "monitored": True,
            # Radarr's own default in its add dialog: wait for a real release.
            "minimumAvailability": "released",
            "addOptions": {"searchForMovie": True},
        }
        added = await self._api.request("POST", "/api/v3/movie", json=body)
        if not isinstance(added, dict) or not added.get("id"):
            raise DeliveryError("Radarr took the movie and answered without its id")
        return added

    async def get(self, movie_id: int | str) -> dict[str, Any] | None:
        """The movie by Radarr's id; None once it is gone from Radarr."""
        return await self._api.request("GET", f"/api/v3/movie/{movie_id}", missing_ok=True)

    async def set_monitored(self, movie: dict[str, Any], monitored: bool) -> None:
        """PUT the movie back with ``monitored`` changed. Nothing is deleted."""
        await self._api.request("PUT", f"/api/v3/movie/{movie['id']}", json={**movie, "monitored": monitored})

    async def search(self, movie_id: int | str) -> None:
        await self._api.request(
            "POST", "/api/v3/command", json={"name": "MoviesSearch", "movieIds": [int(movie_id)]},
        )

    async def has_tmdb_id(
        self, tmdb_id: int, media_type: str = "movie", titles: Iterable[str | None] = (),
    ) -> bool | None:
        """True when Radarr holds this movie with a file, False when not.

        None when Radarr could not be asked: the `in_library` signal is then
        missing, never "not in the library". The shape of
        ``JellyfinClient.has_tmdb_id``, so the signal reads both the same way.
        """
        if media_type != "movie":
            return None
        try:
            movie = await self.find(tmdb_id)
        except DeliveryError:
            return None
        return bool(movie and movie.get("hasFile"))
