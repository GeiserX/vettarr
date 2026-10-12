"""Tracker client: session, search, download, thank and keep-alive.

Started as a vendored UNIT3D client from an older companion tool, and grown to the other two tracker
software families config.yaml names (`search: xbtit | nexusphp`). One class:
what differs per software is a few paths, the search parameters, the result
rows and the login form, all in `SOFTWARE` and the `search`, `_parse_listing`
and `login` branches. The session is a cookie file, a login, or a cookie header
taken from Prowlarr (`use_cookie_header`). Whatever its source, it only ever
goes to the provider's own hosts, and nothing that carries a body leaves them
(`_ProviderSession`).
All methods are synchronous — call via asyncio.to_thread() from async code.
"""

import json
import logging
import re
import threading
import uuid
from dataclasses import dataclass
from functools import wraps
from http.cookiejar import MozillaCookieJar
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urlsplit

import requests
from bs4 import BeautifulSoup

from vettarr import dryrun
from vettarr.searcher.providers import USER_AGENT, fill, session_allowed, shown
from vettarr.searcher.qbittorrent_client import torrent_info_hash

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Software:
    """Where a tracker software keeps the pages the client needs."""

    #: A page only a signed-in user sees: the session check and the default
    #: keep-alive visit.
    check_path: str
    #: The torrent's .torrent, with `{id}`.
    download_path: str


SOFTWARE = {
    "unit3d": Software(check_path="/torrents", download_path="/torrents/download/{id}"),
    "nexusphp": Software(check_path="/torrents.php", download_path="/download.php?id={id}"),
    "xbtit": Software(check_path="/index.php?page=torrents", download_path="/download.php?id={id}"),
    # No known software: the client can keep a session alive and thank, not
    # search or log in.
    "none": Software(check_path="/", download_path=""),
}


class OffProviderRequest(requests.RequestException):
    """A request with a body was about to leave the provider's hosts."""


class DryRunRefused(requests.RequestException):
    """Under ``dry_run``, a request with a body (a login, a thank) is not sent to a tracker."""


class _ProviderSession(requests.Session):
    """A session that keeps what it carries on the provider's own hosts.

    `requests` sends a cookie to every subdomain of the cookie's domain, and
    follows redirects wherever they point: a 307 or 308 repeats the method,
    the body and every header at the new address. Every request it sends, the
    first and each redirect, passes through `send`. One to anywhere `allowed`
    does not name:

    - is refused when it is anything but a GET or a HEAD, or has a body: a
      login form holds the password and a thank holds the CSRF token;
    - otherwise leaves without the headers that carry the session.
    """

    #: The session itself and the tokens that stand for it.
    SESSION_HEADERS = ("Cookie", "Authorization", "X-CSRF-TOKEN", "X-XSRF-TOKEN")

    def __init__(self, allowed: Callable[[str], bool], name: str | None = None) -> None:
        super().__init__()
        self._allowed = allowed
        #: The provider's key, which a ``would_have`` names it by.
        self._name = name

    def send(self, request, **kwargs):
        if (request.method not in ("GET", "HEAD") or request.body) and dryrun.active():
            dryrun.would_have(
                self._name or "a tracker", f"{request.method} {urlsplit(request.url).path}",
                method=request.method, path=shown(request.url),
            )
            raise DryRunRefused(f"dry run: a {request.method} is not sent to a tracker", request=request)
        if not self._allowed(request.url):
            if request.method not in ("GET", "HEAD") or request.body:
                raise OffProviderRequest(
                    f"refused to send a {request.method} to {_host(request.url) or 'an unreadable address'}: "
                    "it is not this provider, or not over the scheme and port its session uses",
                    request=request,
                )
            for header in self.SESSION_HEADERS:
                request.headers.pop(header, None)
        return super().send(request, **kwargs)


def _synchronized(method):
    @wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapper


_SIZE_UNITS = {
    "b": 1,
    "kib": 1024, "mib": 1024**2, "gib": 1024**3, "tib": 1024**4,
    "kb": 1000, "mb": 1000**2, "gb": 1000**3, "tb": 1000**4,
}


def parse_size(size_str: str) -> int:
    match = re.match(r"([\d.,]+)\s*(\w+)", size_str.strip())
    if not match:
        return 0
    value = float(match.group(1).replace(",", "."))
    unit = match.group(2).lower()
    return int(value * _SIZE_UNITS.get(unit, 1))


class TorrentResult:
    __slots__ = (
        "torrent_id", "name", "size_str", "size_bytes",
        "seeders", "leechers", "download_url", "detail_url",
    )

    def __init__(
        self, torrent_id: int | str, name: str, size_str: str, size_bytes: int,
        seeders: int, leechers: int, download_url: str, detail_url: str,
    ):
        self.torrent_id = torrent_id
        self.name = name
        self.size_str = size_str
        self.size_bytes = size_bytes
        self.seeders = seeders
        self.leechers = leechers
        self.download_url = download_url
        self.detail_url = detail_url


# What only a logged-out page carries: UNIT3D answers a dead session with its
# login form, whatever the site's language, and that form has a password field.
DEFAULT_LOGGED_OUT_MARKER = 'name="password"'


def _host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").rstrip(".")
    except ValueError:
        return ""


class Unit3dClient:
    """Client for a private tracker running UNIT3D, NexusPHP or xbtit."""

    def __init__(
        self,
        base_url: str,
        cookie_path: str,
        name: str = "UNIT3D",
        username: str = "",
        password: str = "",
        logged_out_marker: str = DEFAULT_LOGGED_OUT_MARKER,
        software: str = "unit3d",
        allowed: Callable[[str], bool] | None = None,
        cookie_hosts: Iterable[str] = (),
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.name = name
        self.logged_out_marker = logged_out_marker or DEFAULT_LOGGED_OUT_MARKER
        self.software = software if software in SOFTWARE else "none"
        # Which URLs may carry the session: the provider's hosts, exactly, on
        # its port, and never from https down to http.
        self._allowed = allowed or (lambda url: session_allowed(self.base_url, (), url))
        # The hosts a cookie handed over by Prowlarr is stored for: base_url's
        # and the provider's aliases.
        self._cookie_hosts = tuple(dict.fromkeys(h for h in (_host(self.base_url), *cookie_hosts) if h))
        self.session = _ProviderSession(self._allowed, name)
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        })
        self._cookie_path = cookie_path
        self._cookies_loaded = False
        # A Cookie header handed over by Prowlarr, for `auth.from: prowlarr`.
        self._cookie_header: str | None = None
        self._username = username
        self._password = password
        self.last_search_error: str | None = None
        # The body of the last session check: the page a thank reads its CSRF
        # token from, so a thank costs no extra request.
        self._last_page = ""
        self._lock = threading.RLock()

    @property
    def _software(self) -> Software:
        return SOFTWARE[self.software]

    def _held(self, action: str, url: str, method: str = "GET", **detail: Any) -> bool:
        """Under ``dry_run``: record what would have been sent to the tracker, and say so.

        The tracker's own writes go through here first (a thank, a
        keep-alive visit, a ``.torrent`` fetch, which the tracker counts as
        a download) and anything with a body goes through the session's
        ``send``; neither is sent.
        """
        if not dryrun.active():
            return False
        dryrun.would_have(self.name, action, method=method, path=shown(url), **detail)
        return True

    @_synchronized
    def use_cookie_header(self, header: str | None) -> None:
        """Use `header` (``a=1; b=2``) as the session, set for the provider's hosts.

        Called before each use when Prowlarr holds the session. The same
        header again keeps the jar as it is.
        """
        if header == self._cookie_header:
            return
        self._cookie_header = header
        self._cookies_loaded = False

    def _ensure_cookies(self) -> bool:
        if self._cookies_loaded:
            return True
        if self._cookie_header is not None:
            jar = requests.cookies.RequestsCookieJar()
            for part in self._cookie_header.split(";"):
                name, sep, value = part.strip().partition("=")
                if sep and name:
                    for host in self._cookie_hosts:
                        jar.set(name, value, domain=host, path="/")
            self.session.cookies = jar
            self._cookies_loaded = True
            return True
        if not self._cookie_path or not Path(self._cookie_path).is_file():
            log.warning("[%s] Cookie file not found: %s", self.name, self._cookie_path or "(none)")
            return False
        jar = MozillaCookieJar(self._cookie_path)
        jar.load(ignore_discard=True, ignore_expires=True)
        self.session.cookies = jar
        self._cookies_loaded = True
        log.info("[%s] Loaded cookies from %s", self.name, self._cookie_path)
        return True

    def login(self) -> bool:
        """Authenticate via the software's login form and persist cookies."""
        if not self._username or not self._password:
            log.warning("[%s] No credentials configured — cannot auto-login", self.name)
            return False
        if self.software == "nexusphp":
            return self._login_form(
                "/takelogin.php", {"username": self._username, "password": self._password},
            )
        if self.software == "xbtit":
            return self._login_form(
                "/index.php?page=login", {"uid": self._username, "pwd": self._password},
            )
        if self.software != "unit3d":
            log.warning("[%s] No login form known without a tracker software (search: none)", self.name)
            return False

        try:
            r = self.session.get(f"{self.base_url}/login", timeout=30)
            r.raise_for_status()
        except requests.RequestException as e:
            log.error("[%s] Cannot load login page: %s", self.name, e)
            return False

        csrf = self._extract_csrf(r.text)
        if not csrf:
            log.error("[%s] Cannot extract CSRF from login page", self.name)
            return False

        payload = {
            "_token": csrf,
            "username": self._username,
            "password": self._password,
            "remember": "on",
        }
        try:
            resp = self.session.post(
                f"{self.base_url}/login",
                data=payload,
                timeout=30,
                allow_redirects=True,
            )
            resp.raise_for_status()
        except requests.RequestException as e:
            log.error("[%s] Login POST failed: %s", self.name, e)
            return False

        if not self._is_logged_in(resp.text):
            log.error("[%s] Login failed — still on login page", self.name)
            return False

        self._keep_login()
        log.info("[%s] Login succeeded, cookies saved to %s", self.name, self._cookie_path or "(memory only)")
        return True

    def _login_form(self, path: str, payload: dict[str, str]) -> bool:
        """Post a plain login form, as NexusPHP and xbtit take it."""
        try:
            resp = self.session.post(
                f"{self.base_url}{path}", data=payload, timeout=30, allow_redirects=True,
            )
            resp.raise_for_status()
        except requests.RequestException as e:
            log.error("[%s] Login POST failed: %s", self.name, e)
            return False
        if not self._is_logged_in(resp.text):
            log.error("[%s] Login failed — still on login page", self.name)
            return False
        self._keep_login()
        log.info("[%s] Login succeeded", self.name)
        return True

    def _keep_login(self) -> None:
        """Hold on to the session a login just gave.

        The jar in memory is that session. Without a cookie file it is the
        only copy, so it has to count as loaded: otherwise the next call finds
        "no cookies" and logs in with the password again, once per search,
        download and thank.
        """
        self._cookies_loaded = True
        self._save_cookies()

    def _save_cookies(self) -> None:
        if not self._cookie_path or self._cookie_header is not None:
            return
        if dryrun.active():
            # The cookie file may be the live instance's too: a dry run leaves it as it is.
            return
        jar = MozillaCookieJar(self._cookie_path)
        for c in self.session.cookies:
            jar.set_cookie(c)
        jar.save(ignore_discard=True, ignore_expires=True)

    def ensure_session(self) -> bool:
        """Load cookies, check session, auto-login if expired.

        Falls back to reloading cookies from disk if login fails (CAPTCHA).
        """
        self._ensure_cookies()
        if self.check_session():
            return True
        log.info("[%s] Session expired, attempting auto-login", self.name)
        self._cookies_loaded = False
        if self.login():
            return True
        # Login failed — try fresh cookies from disk
        log.info("[%s] Auto-login failed, reloading cookies from disk", self.name)
        self._cookies_loaded = False
        self._ensure_cookies()
        return self.check_session()

    def _extract_csrf(self, html: str) -> str | None:
        for pattern in (
            r'<meta\s+name="csrf-token"\s+content="([^"]+)"',
            r'<input[^>]*name="_token"[^>]*value="([^"]+)"',
            r"window\.livewire_token\s*=\s*['\"]([^'\"]+)['\"]",
        ):
            m = re.search(pattern, html)
            if m:
                return m.group(1)
        return None

    def _is_logged_in(self, html: str) -> bool:
        return self.logged_out_marker not in html

    @_synchronized
    def search(
        self,
        query: str,
        imdb_id: str | None = None,
        tmdb_id: int | None = None,
    ) -> list[TorrentResult]:
        self.last_search_error = None
        if not self.ensure_session():
            self.last_search_error = "authentication failed"
            return []

        if self.software == "nexusphp":
            url = f"{self.base_url}/torrents.php"
            params = {"search": query, "search_area": "0", "search_mode": "0", "incldead": "0"}
        elif self.software == "xbtit":
            url = f"{self.base_url}/index.php"
            params = {"page": "torrents", "search": query, "category": "0", "active": "0"}
        else:
            params = {
                "name": query,
                "sortField": "size",
                "sortDirection": "asc",
                "perPage": "100",
            }
            if imdb_id:
                params["imdbId"] = imdb_id.replace("tt", "")
            if tmdb_id:
                params["tmdbId"] = str(tmdb_id)
            url = f"{self.base_url}/torrents"
        log.info("[%s] Searching: %s", self.name, params)

        try:
            resp = self.session.get(url, params=params, timeout=30)
            resp.raise_for_status()
        except requests.RequestException as e:
            log.error("[%s] Search failed: %s", self.name, e)
            self.last_search_error = f"request failed: {type(e).__name__}"
            return []

        if not self._is_logged_in(resp.text):
            log.error("[%s] Session expired \u2014 cookies need refresh", self.name)
            self.last_search_error = "session expired"
            return []

        if self.software in ("nexusphp", "xbtit"):
            return self._parse_listing(resp.text)
        return self._parse_results(resp.text)

    def _parse_listing(self, html: str) -> list[TorrentResult]:
        """Result rows of a NexusPHP or xbtit torrent list.

        Both draw a table row per torrent with a `download.php?id=` link: a
        number on NexusPHP, the 40-hex info hash on xbtit. The size is the
        first cell that reads like one; seeders and leechers follow it.
        """
        if self.software == "xbtit":
            id_re = re.compile(r"download\.php\?id=([0-9a-fA-F]{40})")
            detail_sel = "a[href*='page=torrent-details']"
        else:
            id_re = re.compile(r"download\.php\?id=(\d+)")
            detail_sel = "a[href*='details.php?id=']"
        soup = BeautifulSoup(html, "html.parser")
        results: list[TorrentResult] = []
        seen: set[str] = set()
        for row in soup.select("tr"):
            # A torrent's row links one torrent and holds its size in a cell of
            # its own. A layout row around the whole list links many, and the
            # small table NexusPHP nests in the name cell holds no size.
            links = [a for a in row.select("a[href*='download.php?id=']") if id_re.search(a.get("href", ""))]
            ids = {id_re.search(a.get("href", "")).group(1) for a in links}
            title_el = row.select_one(detail_sel)
            if len(ids) != 1 or title_el is None:
                continue
            cells = row.find_all("td", recursive=False)
            size_index = next(
                (
                    i for i, cell in enumerate(cells)
                    if re.fullmatch(
                        r"[\d.,]+\s*[KMGT]i?B", cell.get_text(" ", strip=True), re.IGNORECASE,
                    )
                ),
                None,
            )
            tid = ids.pop()
            if size_index is None or tid in seen:
                continue
            seen.add(tid)
            dl_el = links[0]
            size_str = cells[size_index].get_text(" ", strip=True)

            def count(offset: int) -> int:
                if len(cells) <= size_index + offset:
                    return 0
                digits = re.sub(r"\D", "", cells[size_index + offset].get_text(strip=True))
                return int(digits) if digits else 0

            results.append(TorrentResult(
                torrent_id=tid,
                name=title_el.get("title") or title_el.get_text(strip=True),
                size_str=size_str,
                size_bytes=parse_size(size_str),
                seeders=count(1),
                leechers=count(2),
                download_url=self._abs(dl_el.get("href", "")),
                detail_url=self._abs(title_el.get("href", "")),
            ))
        log.info("[%s] Parsed %d results", self.name, len(results))
        return results

    def _parse_results(self, html: str) -> list[TorrentResult]:
        soup = BeautifulSoup(html, "html.parser")
        results: list[TorrentResult] = []

        rows = soup.select("table > tbody > tr")
        if not rows:
            rows = soup.select("tr:has(a[href*='/download/'])")

        for row in rows:
            try:
                title_el = (
                    row.select_one("a.view-torrent")
                    or row.select_one("a.torrent-listings-name")
                )
                if not title_el:
                    continue

                dl_el = row.select_one("a[href*='/torrents/download/']")
                if not dl_el:
                    continue

                dl_href = dl_el.get("href", "")
                detail_href = title_el.get("href", "")

                id_match = re.search(r"/torrents/download/(\d+)", dl_href)
                if not id_match:
                    id_match = re.search(r"/torrents/(\d+)", detail_href)
                if not id_match:
                    continue
                tid = int(id_match.group(1))

                cells = row.select("td")
                size_td = row.select_one("td.torrent-listings-size")
                size_index = None
                if size_td:
                    size_index = next(
                        (i for i, cell in enumerate(cells) if cell is size_td),
                        None,
                    )
                else:
                    for i, cell in enumerate(cells):
                        if re.fullmatch(
                            r"[\d.,]+\s*(?:[KMGT]i?B)",
                            cell.get_text(strip=True),
                            re.IGNORECASE,
                        ):
                            size_td = cell
                            size_index = i
                            break
                size_str = size_td.get_text(strip=True) if size_td else "0"

                seeders_td = row.select_one("td.torrent-listings-seeders")
                if not seeders_td and size_index is not None and len(cells) > size_index + 1:
                    seeders_td = cells[size_index + 1]
                seeders = int(re.sub(r"\D", "", seeders_td.get_text(strip=True))) if seeders_td else 0

                leechers_td = row.select_one("td.torrent-listings-leechers")
                if not leechers_td and size_index is not None and len(cells) > size_index + 2:
                    leechers_td = cells[size_index + 2]
                leechers = int(re.sub(r"\D", "", leechers_td.get_text(strip=True))) if leechers_td else 0

                results.append(TorrentResult(
                    torrent_id=tid,
                    name=title_el.get_text(strip=True),
                    size_str=size_str,
                    size_bytes=parse_size(size_str),
                    seeders=seeders,
                    leechers=leechers,
                    download_url=self._abs(dl_href),
                    detail_url=self._abs(detail_href),
                ))
            except (ValueError, AttributeError) as exc:
                log.debug("[%s] Skipping unparseable row: %s", self.name, exc)

        log.info("[%s] Parsed %d results", self.name, len(results))
        return results

    @_synchronized
    def search_with_status(
        self,
        query: str,
        imdb_id: str | None = None,
        tmdb_id: int | None = None,
    ) -> tuple[list[TorrentResult], str | None]:
        results = self.search(query, imdb_id=imdb_id, tmdb_id=tmdb_id)
        return results, self.last_search_error

    @_synchronized
    def download_torrent(self, torrent_id: int | str) -> bytes | None:
        template = self._software.download_path
        if not template:
            return None
        return self.fetch_torrent(self.base_url + fill(template, str(torrent_id)))

    @_synchronized
    def fetch_torrent(self, url: str) -> bytes | None:
        """The .torrent at `url`, fetched with this tracker's session."""
        if not self._allowed(url):
            log.error("[%s] %s is not on this tracker; its session is not sent there", self.name, shown(url))
            return None
        if self._held("fetch .torrent", url, reason=dryrun.NOT_FETCHED):
            return None
        if not self.ensure_session():
            return None
        log.info("[%s] Downloading %s", self.name, shown(url))
        try:
            resp = self.session.get(url, timeout=30)
            resp.raise_for_status()
        except requests.RequestException as e:
            log.error("[%s] Download failed for %s: %s", self.name, shown(url), type(e).__name__)
            return None

        if torrent_info_hash(resp.content) is None:
            # A login page, a maintenance redirect, an error page: anything
            # but a bencoded torrent with an info dictionary.
            log.error("[%s] %s answered without a torrent (session expired?)", self.name, shown(url))
            return None
        return resp.content

    @_synchronized
    def thank(
        self,
        torrent_id: int | str,
        path: str,
        method: str = "POST",
        body: str | dict[str, Any] | None = None,
    ) -> bool:
        """Thank the uploader the way the provider's `thank` block says.

        `{id}` in the path and the body is the torrent id. A POST or PUT
        carries the CSRF token of the page the session check just read, as
        `X-CSRF-TOKEN`, when that page has one. LIVEWIRE loads `path` and
        presses its Livewire `store` button.
        """
        url = self.base_url + fill(path, str(torrent_id))
        if self._held("thank", url, method, torrent_id=str(torrent_id)):
            return True
        if not self.ensure_session():
            return False
        log.info("[%s] Thanking torrent %s", self.name, torrent_id)
        if method == "LIVEWIRE":
            return self._thank_livewire_page(url, torrent_id)

        headers = {"Referer": self.base_url + "/", "X-Requested-With": "XMLHttpRequest"}
        csrf = self._extract_csrf(self._last_page) if method != "GET" else None
        if csrf:
            headers["X-CSRF-TOKEN"] = csrf
        data: Any = None
        if isinstance(body, dict):
            data = {k: fill(v, str(torrent_id)) if isinstance(v, str) else v for k, v in body.items()}
        elif isinstance(body, str):
            data = fill(body, str(torrent_id))
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        try:
            resp = self.session.request(method, url, data=data, headers=headers, timeout=30)
        except requests.RequestException as e:
            log.error("[%s] Thank request failed for %s: %s", self.name, torrent_id, e)
            return False
        ok = resp.ok and self._is_logged_in(resp.text)
        if not ok:
            log.warning("[%s] Thank for %s answered HTTP %d", self.name, torrent_id, resp.status_code)
        return ok

    def _thank_livewire_page(self, url: str, torrent_id: int | str) -> bool:
        try:
            page = self.session.get(url, timeout=30)
            page.raise_for_status()
        except requests.RequestException as e:
            log.error("[%s] Cannot load detail page for %s: %s", self.name, torrent_id, e)
            return False

        csrf = self._extract_csrf(page.text)
        if not csrf:
            log.error("[%s] CSRF extraction failed on detail page", self.name)
            return False
        param = int(torrent_id) if str(torrent_id).isdigit() else torrent_id
        if self._thank_livewire(page.text, param, csrf, url):
            return True
        log.error("[%s] The Livewire thank failed for torrent %s", self.name, torrent_id)
        return False

    def thank_torrent(self, torrent_id: int | str) -> bool:
        """UNIT3D's thank: the Livewire button on the torrent's page."""
        return self.thank(torrent_id, "/torrents/{id}", "LIVEWIRE")

    def _thank_livewire(self, html: str, torrent_id: int, csrf: str, referer: str) -> bool:
        soup = BeautifulSoup(html, "html.parser")
        btn = None
        for el in soup.select("[wire\\:click]"):
            if "store" in el.get("wire:click", ""):
                btn = el
                break
        if not btn:
            return False

        wire_id = self._find_wire_id(btn)
        snapshot = self._find_attr_upward(btn, "wire:snapshot")
        if snapshot:
            return self._lw3_call(snapshot, torrent_id, csrf, referer)
        initial = self._find_attr_upward(btn, "wire:initial-data")
        if initial:
            return self._lw2_call(initial, torrent_id, csrf, referer)
        if wire_id:
            return self._lw2_minimal(wire_id, torrent_id, csrf, referer)
        return False

    def _lw3_call(self, snapshot: str, torrent_id: int, csrf: str, referer: str) -> bool:
        payload = {
            "components": [{
                "snapshot": snapshot,
                "updates": {},
                "calls": [{"path": "", "method": "store", "params": [torrent_id]}],
            }],
            "_token": csrf,
        }
        try:
            r = self.session.post(
                f"{self.base_url}/livewire/update",
                json=payload, headers=self._lw_headers(csrf, referer), timeout=30,
            )
            if r.ok:
                log.info("[%s] Livewire v3 thank OK for %d", self.name, torrent_id)
                return True
        except requests.RequestException:
            pass
        return False

    def _lw2_call(self, initial_data_raw: str, torrent_id: int, csrf: str, referer: str) -> bool:
        try:
            data = json.loads(initial_data_raw)
        except json.JSONDecodeError:
            return False
        fp = data.get("fingerprint", {})
        memo = data.get("serverMemo", {})
        name = fp.get("name", "thank-button")
        payload = {
            "fingerprint": fp, "serverMemo": memo,
            "updates": [self._lw2_update(torrent_id)],
        }
        try:
            r = self.session.post(
                f"{self.base_url}/livewire/message/{name}",
                json=payload, headers=self._lw_headers(csrf, referer), timeout=30,
            )
            if r.ok:
                log.info("[%s] Livewire v2 thank OK for %d", self.name, torrent_id)
                return True
        except requests.RequestException:
            pass
        return False

    def _lw2_minimal(self, wire_id: str, torrent_id: int, csrf: str, referer: str) -> bool:
        payload = {
            "fingerprint": {
                "id": wire_id, "name": "thank-button", "locale": "en",
                "path": f"torrents/{torrent_id}", "method": "GET", "v": "acj",
            },
            "serverMemo": {
                "children": [], "errors": [], "htmlHash": "",
                "data": {"torrent": torrent_id}, "dataMeta": [], "checksum": "",
            },
            "updates": [self._lw2_update(torrent_id)],
        }
        try:
            r = self.session.post(
                f"{self.base_url}/livewire/message/thank-button",
                json=payload, headers=self._lw_headers(csrf, referer), timeout=30,
            )
            if r.ok:
                log.info("[%s] Livewire v2 minimal thank OK for %d", self.name, torrent_id)
                return True
        except requests.RequestException:
            pass
        return False

    @staticmethod
    def _lw2_update(torrent_id: int) -> dict:
        return {
            "type": "callMethod",
            "payload": {"id": uuid.uuid4().hex[:8], "method": "store", "params": [torrent_id]},
        }

    @staticmethod
    def _lw_headers(csrf: str, referer: str) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "X-CSRF-TOKEN": csrf, "X-Livewire": "true", "Referer": referer,
        }

    @staticmethod
    def _find_wire_id(el: Any) -> str:
        wid = el.get("wire:id", "")
        if wid:
            return wid
        p = el.parent
        while p:
            wid = p.get("wire:id", "") if hasattr(p, "get") else ""
            if wid:
                return wid
            p = getattr(p, "parent", None)
        return ""

    @staticmethod
    def _find_attr_upward(el: Any, attr: str) -> str:
        val = el.get(attr, "") if hasattr(el, "get") else ""
        if val:
            return val
        p = el.parent
        while p:
            val = p.get(attr, "") if hasattr(p, "get") else ""
            if val:
                return val
            p = getattr(p, "parent", None)
        return ""

    def _abs(self, href: str) -> str:
        if href.startswith("http"):
            return href
        # NexusPHP and xbtit write their links relative: `download.php?id=1`.
        return f"{self.base_url}{href if href.startswith('/') else '/' + href}"

    @_synchronized
    def keep_alive(self, path: str | None = None) -> bool:
        """Visit `path` (default: the torrent list) to keep the session alive.

        Re-login if expired. If auto-login fails (e.g. CAPTCHA), reload
        cookies from disk in case they were manually updated.
        """
        page = f"{self.base_url}{path or self._software.check_path}"
        if self._held("keep-alive", page):
            return True
        if not self.ensure_session():
            return False
        try:
            r = self.session.get(page, timeout=30)
            alive = self._is_logged_in(r.text)
            if alive:
                self._save_cookies()
                log.info("[%s] Session keep-alive OK (cookies persisted)", self.name)
                return True
            log.info("[%s] Session dead during keep-alive, attempting recovery", self.name)
            self._cookies_loaded = False
            if self.login():
                return True
            # Login failed (CAPTCHA?) — try reloading cookies from disk
            log.info("[%s] Auto-login failed, reloading cookies from disk", self.name)
            self._cookies_loaded = False
            if self._ensure_cookies():
                r2 = self.session.get(page, timeout=30)
                if self._is_logged_in(r2.text):
                    self._save_cookies()
                    log.info("[%s] Cookies reloaded from disk — session restored", self.name)
                    return True
            log.warning("[%s] Session expired and could not be restored", self.name)
            return False
        except requests.RequestException as e:
            log.error("[%s] Keep-alive request failed: %s", self.name, e)
            return False

    @_synchronized
    def check_session(self) -> bool:
        if not self._ensure_cookies():
            return False
        try:
            r = self.session.get(f"{self.base_url}{self._software.check_path}", timeout=30)
            self._last_page = r.text
            logged_in = self._is_logged_in(r.text)
            if logged_in:
                self._save_cookies()
            return logged_in
        except requests.RequestException:
            return False
