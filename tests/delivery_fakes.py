"""Fake Radarr, Sonarr and Seerr servers for the delivery and the Seerr source tests.

Each one answers the routes the app calls in the shape the real service
answers them (field names, status codes and error bodies as recorded from
Radarr 5, Sonarr 4 and Seerr 3), keeps the state a real one would keep
(movies, series, requests), and records every call with its headers. A test
drives the real client against it through ``httpx.MockTransport``, so what
is checked is the request the app really builds.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx

API_KEY = "fake-key-0123456789abcdef"


@dataclass
class Call:
    method: str
    path: str
    params: dict[str, str]
    body: Any
    headers: dict[str, str]
    url: str


def _json(status: int, body: Any) -> httpx.Response:
    return httpx.Response(status, json=body)


class FakeServer:
    """Records every call, checks the key, and can be told to fail."""

    def __init__(self) -> None:
        self.calls: list[Call] = []
        #: Raised instead of answering: a timeout, a refused connection.
        self.down: type[Exception] | None = None
        #: The key the server accepts. Change it to see a 401.
        self.accepts = API_KEY

    def http(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self._handle))

    def _handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.calls.append(Call(
            request.method, request.url.path, dict(request.url.params), body,
            {k.lower(): v for k, v in request.headers.items()}, str(request.url),
        ))
        if self.down is not None:
            raise self.down("fake failure", request=request)
        if request.headers.get("X-Api-Key") != self.accepts:
            return _json(401, {"error": "Unauthorized"})
        return self.route(request.method, request.url.path, dict(request.url.params), body, request)

    def route(self, method, path, params, body, request) -> httpx.Response:
        raise NotImplementedError

    def sent(self, method: str, path: str | None = None) -> list[Call]:
        return [c for c in self.calls if c.method == method and (path is None or c.path == path)]

    def writes(self) -> list[Call]:
        return [c for c in self.calls if c.method != "GET"]


# ---------------------------------------------------------------------------
# Radarr
# ---------------------------------------------------------------------------


def radarr_lookup(tmdb_id: int = 603, title: str = "Some Film", year: int = 1999) -> dict[str, Any]:
    """One entry of ``GET /api/v3/movie/lookup`` for a movie Radarr does not hold."""
    return {
        "title": title, "originalTitle": title, "sortTitle": title.lower(), "status": "released",
        "overview": "", "images": [], "year": year, "tmdbId": tmdb_id, "imdbId": "tt0000001",
        "titleSlug": str(tmdb_id), "monitored": False, "hasFile": False, "isAvailable": True,
        "minimumAvailability": "tba", "qualityProfileId": 0, "runtime": 100, "genres": [],
        "tags": [], "folder": f"{title} ({year})",
    }


class FakeRadarr(FakeServer):
    def __init__(self) -> None:
        super().__init__()
        self.catalog: dict[int, dict[str, Any]] = {603: radarr_lookup()}
        self.movies: dict[int, dict[str, Any]] = {}
        self.profiles = [{"id": 1, "name": "Any"}, {"id": 4, "name": "HD-1080p"}]
        self._next_id = 11
        #: Hide held movies from the first ``GET /movie?tmdbId=``: somebody
        #: added it between the app's two calls.
        self.hide_first_find = False

    def hold(self, tmdb_id: int = 603, *, has_file: bool, monitored: bool = True) -> dict[str, Any]:
        """A movie already in Radarr before the app delivered anything."""
        movie = {
            **self.catalog[tmdb_id], "id": self._next_id, "hasFile": has_file, "monitored": monitored,
            "qualityProfileId": 1, "rootFolderPath": "/movies", "path": "/movies/Some Film (1999)",
        }
        self.movies[movie["id"]] = movie
        self._next_id += 1
        return movie

    def route(self, method, path, params, body, request) -> httpx.Response:
        if (method, path) == ("GET", "/api/v3/system/status"):
            return _json(200, {"appName": "Radarr", "instanceName": "Radarr", "version": "5.14.0.9383"})
        if (method, path) == ("GET", "/api/v3/qualityprofile"):
            return _json(200, self.profiles)
        if (method, path) == ("GET", "/api/v3/movie"):
            if self.hide_first_find:
                self.hide_first_find = False
                return _json(200, [])
            wanted = int(params["tmdbId"])
            return _json(200, [m for m in self.movies.values() if m["tmdbId"] == wanted])
        if (method, path) == ("GET", "/api/v3/movie/lookup"):
            wanted = int(params["term"].removeprefix("tmdb:"))
            return _json(200, [self.catalog[wanted]] if wanted in self.catalog else [])
        if (method, path) == ("POST", "/api/v3/movie"):
            if any(m["tmdbId"] == body["tmdbId"] for m in self.movies.values()):
                return _json(400, [{
                    "propertyName": "TmdbId", "errorMessage": "This movie has already been added",
                    "attemptedValue": body["tmdbId"], "severity": "error",
                    "errorCode": "MovieExistsValidator",
                }])
            movie = {**body, "id": self._next_id, "hasFile": False}
            movie.pop("addOptions", None)
            self.movies[movie["id"]] = movie
            self._next_id += 1
            return _json(201, movie)
        if (method, path) == ("POST", "/api/v3/command"):
            return _json(201, {"id": 77, "name": body["name"], "status": "queued"})
        if path.startswith("/api/v3/movie/"):
            movie = self.movies.get(int(path.rsplit("/", 1)[1]))
            if movie is None:
                return _json(404, {"message": "NotFound"})
            if method == "GET":
                return _json(200, movie)
            if method == "PUT":
                movie.update(body)
                return _json(202, movie)
        return _json(404, {"message": "NotFound"})


# ---------------------------------------------------------------------------
# Sonarr
# ---------------------------------------------------------------------------

TVDB_ID = 81189
EPISODES = 8


def sonarr_lookup(tvdb_id: int = TVDB_ID, seasons: int = 3) -> dict[str, Any]:
    """One entry of ``GET /api/v3/series/lookup`` for a series Sonarr does not hold."""
    return {
        "title": "Some Show", "sortTitle": "some show", "status": "ended", "overview": "",
        "images": [], "year": 2008, "tvdbId": tvdb_id, "tmdbId": 1396, "imdbId": "tt0000002",
        "titleSlug": "some-show", "seriesType": "standard", "monitored": True,
        "seasonFolder": True, "qualityProfileId": 0, "genres": [], "tags": [],
        "seasons": [{"seasonNumber": n, "monitored": n > 0} for n in range(seasons + 1)],
    }


class FakeSonarr(FakeServer):
    def __init__(self) -> None:
        super().__init__()
        self.catalog: dict[int, dict[str, Any]] = {TVDB_ID: sonarr_lookup()}
        self.series: dict[int, dict[str, Any]] = {}
        self.profiles = [{"id": 1, "name": "Any"}, {"id": 4, "name": "HD-1080p"}]
        self._next_id = 21
        #: Hide held series from the first ``GET /series?tvdbId=``: somebody
        #: added it between the app's two calls.
        self.hide_first_find = False

    @staticmethod
    def _statistics(season: dict[str, Any], files: int) -> dict[str, Any]:
        # What Sonarr counts: a monitored season's aired episodes, plus any
        # episode with a file whether or not its season is monitored.
        aired = EPISODES if season["seasonNumber"] > 0 else 0
        counted = aired if season.get("monitored") else files
        return {
            "episodeFileCount": files, "episodeCount": counted, "totalEpisodeCount": aired,
            "sizeOnDisk": files * 1_000_000_000,
            "percentOfEpisodes": (100.0 * files / counted) if counted else 0.0,
        }

    def _refresh(self, series: dict[str, Any]) -> None:
        for season in series["seasons"]:
            files = (season.get("statistics") or {}).get("episodeFileCount", 0)
            season["statistics"] = self._statistics(season, files)

    def hold(self, *, monitored: bool = True, monitored_seasons=(1, 2, 3), files: dict[int, int] | None = None):
        """A series already in Sonarr before the app delivered anything."""
        series = {
            **self.catalog[TVDB_ID], "id": self._next_id, "monitored": monitored,
            "qualityProfileId": 1, "rootFolderPath": "/tv", "path": "/tv/Some Show",
        }
        series["seasons"] = [
            {"seasonNumber": s["seasonNumber"], "monitored": s["seasonNumber"] in monitored_seasons}
            for s in series["seasons"]
        ]
        for season in series["seasons"]:
            season["statistics"] = {"episodeFileCount": (files or {}).get(season["seasonNumber"], 0)}
        self._refresh(series)
        self.series[series["id"]] = series
        self._next_id += 1
        return series

    def set_files(self, series_id: int, season_number: int, files: int) -> None:
        for season in self.series[series_id]["seasons"]:
            if season["seasonNumber"] == season_number:
                season["statistics"] = {"episodeFileCount": files}
        self._refresh(self.series[series_id])

    def route(self, method, path, params, body, request) -> httpx.Response:
        if (method, path) == ("GET", "/api/v3/system/status"):
            return _json(200, {"appName": "Sonarr", "instanceName": "Sonarr", "version": "4.0.11.2680"})
        if (method, path) == ("GET", "/api/v3/qualityprofile"):
            return _json(200, self.profiles)
        if (method, path) == ("GET", "/api/v3/series"):
            if self.hide_first_find:
                self.hide_first_find = False
                return _json(200, [])
            wanted = int(params["tvdbId"])
            return _json(200, [s for s in self.series.values() if s["tvdbId"] == wanted])
        if (method, path) == ("GET", "/api/v3/series/lookup"):
            wanted = int(params["term"].removeprefix("tvdb:"))
            return _json(200, [self.catalog[wanted]] if wanted in self.catalog else [])
        if (method, path) == ("POST", "/api/v3/series"):
            if any(s["tvdbId"] == body["tvdbId"] for s in self.series.values()):
                return _json(400, [{
                    "propertyName": "TvdbId", "errorMessage": "This series has already been added",
                    "attemptedValue": body["tvdbId"], "severity": "error",
                    "errorCode": "SeriesExistsValidator",
                }])
            series = {**body, "id": self._next_id}
            series.pop("addOptions", None)
            series["seasons"] = [dict(s) for s in series["seasons"]]
            self._refresh(series)
            self.series[series["id"]] = series
            self._next_id += 1
            return _json(201, series)
        if (method, path) == ("POST", "/api/v3/command"):
            return _json(201, {"id": 78, "name": body["name"], "status": "queued"})
        if path.startswith("/api/v3/series/"):
            series = self.series.get(int(path.rsplit("/", 1)[1]))
            if series is None:
                return _json(404, {"message": "NotFound"})
            if method == "GET":
                return _json(200, series)
            if method == "PUT":
                series.update(body)
                series["seasons"] = [dict(s) for s in series["seasons"]]
                self._refresh(series)
                return _json(202, series)
        return _json(404, {"message": "NotFound"})


# ---------------------------------------------------------------------------
# Seerr
# ---------------------------------------------------------------------------

# Seerr's own numbers.
MEDIA_UNKNOWN, MEDIA_PENDING, MEDIA_PROCESSING, MEDIA_PARTIAL, MEDIA_AVAILABLE = 1, 2, 3, 4, 5
REQUEST_PENDING, REQUEST_APPROVED, REQUEST_DECLINED, REQUEST_FAILED, REQUEST_COMPLETED = 1, 2, 3, 4, 5
ADMIN_ID = 1
#: The seasons TMDB lists for a series, when a request asks for "all".
SHOW_SEASONS = (1, 2, 3)
# A request in one of these does not stand in the way of a new one.
_CLOSED = (REQUEST_DECLINED, REQUEST_COMPLETED)


CSRF_COOKIE = "XSRF-TOKEN"
CSRF_TOKEN = "fake-csrf-token"
#: The secret the token is checked against, in its own HttpOnly cookie.
CSRF_SECRET_COOKIE = "_csrf"
CSRF_SECRET = "fake-csrf-secret"


class FakeSeerr(FakeServer):
    def __init__(self) -> None:
        super().__init__()
        #: Seerr answers 409 to a decision on a request that is no longer
        #: pending since 3.5.0. False is every version before: the status is
        #: set to whatever was asked, whatever it was.
        self.guards = True
        #: (kind, tmdb id) -> mediaInfo
        self.media: dict[tuple[str, int], dict[str, Any]] = {}
        self.requests: dict[int, dict[str, Any]] = {}
        self._next_id = 31
        #: Seerr's "CSRF Protection" (Settings, Network). On, every write
        #: without the token is 403 "invalid csrf token", API key or not; the
        #: token is the XSRF-TOKEN cookie every answer sets, checked against
        #: the secret in the _csrf cookie set beside it.
        self.csrf = False
        #: Seerr 3.5.1 marks both cookies Secure, even on a plain http
        #: answer, so a cookie jar never sends them back over http. False is
        #: 3.5.0 and before.
        self.csrf_secure = True

    def _seasons_left(self, info: dict[str, Any], asked) -> list[int]:
        """The seasons of a request Seerr would still take.

        As Seerr's own MediaRequest.request does it: a season is dropped when
        an open request already names it, and also when the media lists it
        with any status (available, partly available), request or not.
        """
        taken = {
            s["seasonNumber"]
            for r in info["requests"] if r["status"] not in _CLOSED
            for s in r["seasons"]
        }
        taken |= {s["seasonNumber"] for s in info["seasons"] if s.get("status", MEDIA_UNKNOWN) != MEDIA_UNKNOWN}
        return [n for n in (SHOW_SEASONS if asked == "all" else asked) if n not in taken]

    def info(self, kind: str, tmdb_id: int) -> dict[str, Any]:
        return self.media.setdefault((kind, tmdb_id), {
            "id": len(self.media) + 1, "mediaType": kind, "tmdbId": tmdb_id, "status": 1,
            "requests": [], "seasons": [],
        })

    def add_request(
        self, kind: str, tmdb_id: int, *, user: int, status: int = REQUEST_PENDING, seasons=(),
        is4k: bool = False, name: str | None = None, created_at: str | None = None,
    ):
        info = self.info(kind, tmdb_id)
        request = {
            "id": self._next_id, "status": status, "type": kind, "is4k": is4k,
            "requestedBy": {"id": user, "displayName": name or f"user-{user}"},
            "seasons": [{"seasonNumber": n, "status": status} for n in seasons],
            "media": {"id": info["id"], "tmdbId": tmdb_id, "mediaType": kind},
            "createdAt": created_at or f"2026-10-11T08:{self._next_id % 60:02d}:00.000Z",
        }
        self._next_id += 1
        self.requests[request["id"]] = request
        info["requests"].append(request)
        info["status"] = max(info["status"], MEDIA_PENDING)
        return request

    def set_request(self, request_id: int, status: int) -> None:
        self.requests[request_id]["status"] = status

    def remove_request(self, request_id: int) -> None:
        request = self.requests.pop(request_id)
        for info in self.media.values():
            info["requests"] = [r for r in info["requests"] if r["id"] != request["id"]]

    def _list(self, params) -> httpx.Response:
        """``GET /api/v1/request``: one page, with Seerr's ``pageInfo``. Newest first unless asked."""
        found = [
            r for r in self.requests.values()
            if params.get("filter") != "pending" or r["status"] == REQUEST_PENDING
        ]
        found.sort(key=lambda r: r["id"], reverse=params.get("sortDirection") != "asc")
        take, skip = int(params.get("take") or 10), int(params.get("skip") or 0)
        return _json(200, {
            "pageInfo": {
                "pages": -(-len(found) // take), "pageSize": take, "results": len(found),
                "page": -(-skip // take) + 1,
            },
            "results": found[skip:skip + take],
        })

    def _decide(self, request_id: int, verb: str, request) -> httpx.Response:
        """``POST /api/v1/request/<id>/approve|decline``, as Seerr's route does it."""
        # Deciding takes manage-requests, which only the key's own user (the admin) has here.
        if int(request.headers.get("X-API-User") or ADMIN_ID) != ADMIN_ID:
            return _json(403, {"message": "You do not have permission to access this endpoint."})
        found = self.requests.get(request_id)
        if found is None:
            return _json(404, {"message": "Request not found."})
        if found["status"] != REQUEST_PENDING and self.guards:
            return _json(409, {"message": "Only pending requests can be approved or declined."})
        found["status"] = REQUEST_APPROVED if verb == "approve" else REQUEST_DECLINED
        if verb == "approve":
            info = self.media[(found["type"], found["media"]["tmdbId"])]
            key = "status4k" if found["is4k"] else "status"
            info[key] = max(info.get(key, MEDIA_UNKNOWN), MEDIA_PROCESSING)
        return _json(200, found)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        answer = super()._handle(request)
        if not self.csrf:
            return answer
        flags = "; Secure; SameSite=Strict" if self.csrf_secure else ""
        cookies = [
            ("set-cookie", f"{CSRF_SECRET_COOKIE}={CSRF_SECRET}; Path=/; HttpOnly{flags}"),
            ("set-cookie", f"{CSRF_COOKIE}={CSRF_TOKEN}; Path=/{flags}"),
        ]
        return httpx.Response(
            answer.status_code, headers=[*answer.headers.multi_items(), *cookies], content=answer.content,
        )

    def route(self, method, path, params, body, request) -> httpx.Response:
        if self.csrf and method != "GET":
            sent = request.headers.get("X-XSRF-TOKEN")
            jar = dict(
                part.strip().split("=", 1) for part in request.headers.get("cookie", "").split(";") if "=" in part
            )
            if sent != CSRF_TOKEN or jar.get(CSRF_SECRET_COOKIE) != CSRF_SECRET:
                return _json(403, {"message": "invalid csrf token"})
        if (method, path) == ("GET", "/api/v1/settings/about"):
            return _json(200, {"version": "3.5.0", "totalRequests": len(self.requests), "totalMediaItems": 0})
        if (method, path) == ("GET", "/api/v1/request"):
            return self._list(params)
        parts = path.strip("/").split("/")
        if method == "POST" and len(parts) == 5 and parts[2] == "request" and parts[4] in ("approve", "decline"):
            return self._decide(int(parts[3]), parts[4], request)
        if method == "GET" and len(parts) == 4 and parts[2] in ("movie", "tv"):
            details: dict[str, Any] = {"id": int(parts[3]), "overview": ""}
            info = self.media.get((parts[2], int(parts[3])))
            if info is not None:
                details["mediaInfo"] = info
            return _json(200, details)
        if (method, path) == ("POST", "/api/v1/request"):
            kind, tmdb_id = body["mediaType"], body["mediaId"]
            info = self.info(kind, tmdb_id)
            seasons: Any = ()
            if kind == "movie" and any(r["status"] not in _CLOSED for r in info["requests"]):
                return _json(409, {"message": "Request for this media already exists."})
            if kind == "tv":
                seasons = self._seasons_left(info, body.get("seasons") or "all")
                if not seasons:
                    return _json(202, {"message": "No seasons available to request"})
            # The trap: with the key alone Seerr acts as its admin, and an
            # admin's request is approved at once.
            user = int(request.headers.get("X-API-User") or ADMIN_ID)
            made = self.add_request(
                kind, tmdb_id, user=user, seasons=seasons,
                status=REQUEST_APPROVED if user == ADMIN_ID else REQUEST_PENDING,
            )
            return _json(201, made)
        if len(parts) == 4 and parts[2] == "request":
            found = self.requests.get(int(parts[3]))
            if found is None:
                return _json(404, {"message": "Request not found."})
            if method == "GET":
                return _json(200, found)
            if method == "DELETE":
                self.remove_request(found["id"])
                return httpx.Response(204)
        return _json(404, {"message": "Not Found"})
