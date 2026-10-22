from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass
from functools import cached_property
from urllib.parse import parse_qs, urlparse

import httpx

from vettarr import dryrun
from vettarr.config import settings
from vettarr.models import MediaType
from vettarr.searcher.providers import shown

log = logging.getLogger(__name__)
_ADD_LOCK = asyncio.Lock()


@dataclass(frozen=True)
class AddOutcome:
    """What handing a torrent to qBittorrent actually did.

    `created` is the fact rejecting an item depends on. qBittorrent answers an
    add with the hash of a torrent it was already holding, so "we have a hash"
    never meant "we put it there". Deleting files for a torrent that was in the
    library before this item existed destroys somebody's copy of something they
    never asked to have removed.
    """

    #: The info hash, when qBittorrent gave one. None means "added, identity
    #: unknown", which is all an older qBittorrent reports for a URL add.
    hash: str | None = None
    #: True only when this add is provably what put the torrent in the client.
    created: bool = False


def _skip_bencoded_value(data: bytes, pos: int) -> int:
    if pos >= len(data):
        raise ValueError("truncated bencoded value")
    token = data[pos:pos + 1]
    if token == b"i":
        end = data.index(b"e", pos + 1)
        int(data[pos + 1:end])
        return end + 1
    if token in (b"l", b"d"):
        pos += 1
        while pos < len(data) and data[pos:pos + 1] != b"e":
            pos = _skip_bencoded_value(data, pos)
            if token == b"d":
                pos = _skip_bencoded_value(data, pos)
        if pos >= len(data):
            raise ValueError("unterminated bencoded container")
        return pos + 1
    if token.isdigit():
        colon = data.index(b":", pos)
        length = int(data[pos:colon])
        end = colon + 1 + length
        if end > len(data):
            raise ValueError("truncated bencoded string")
        return end
    raise ValueError("invalid bencoded value")


def torrent_info_hash(data: bytes) -> str | None:
    """The info hash of a bencoded .torrent, or None if `data` is not one."""
    try:
        if not data.startswith(b"d"):
            return None
        pos = 1
        while data[pos:pos + 1] != b"e":
            colon = data.index(b":", pos)
            key_length = int(data[pos:colon])
            key_start = colon + 1
            key_end = key_start + key_length
            key = data[key_start:key_end]
            value_start = key_end
            value_end = _skip_bencoded_value(data, value_start)
            if key == b"info":
                return hashlib.sha1(data[value_start:value_end]).hexdigest()
            pos = value_end
    except (IndexError, ValueError, RecursionError):
        # RecursionError: containers nested deeper than any torrent's are.
        return None
    return None


def _magnet_info_hash(magnet_url: str) -> str | None:
    for xt in parse_qs(urlparse(magnet_url).query).get("xt", []):
        prefix = "urn:btih:"
        if not xt.lower().startswith(prefix):
            continue
        value = xt[len(prefix):]
        if len(value) == 40:
            try:
                int(value, 16)
            except ValueError:
                return None
            return value.lower()
        if len(value) == 32:
            try:
                return base64.b32decode(value.upper()).hex()
            except (ValueError, base64.binascii.Error):
                return None
    return None


def _add_was_accepted(resp: httpx.Response) -> bool:
    if resp.status_code != 200:
        return False
    text = resp.text.strip()
    if text in ("", "Ok."):
        return True
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return False
    return data.get("success_count", 0) > 0


def _added_torrent_hash(resp: httpx.Response) -> str | None:
    try:
        torrent_ids = resp.json().get("added_torrent_ids") or []
    except (json.JSONDecodeError, AttributeError):
        return None
    return str(torrent_ids[0]).lower() if torrent_ids else None


def _normalize_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()


def _per_type(field: str, media_type: MediaType) -> str:
    block = settings.config.integrations.qbittorrent
    values = getattr(block, field) if block is not None else {}
    return values.get(getattr(media_type, "value", media_type), "")


def category_for(media_type: MediaType) -> str:
    """The qBittorrent category an item of `media_type` is added under.

    `integrations.qbittorrent.categories`; a type not listed gets none.
    """
    return _per_type("categories", media_type)


def save_path_for(media_type: MediaType) -> str:
    """Where qBittorrent saves an item of `media_type`.

    `integrations.qbittorrent.save_paths`; a type not listed gets the
    category's path, or qBittorrent's default.
    """
    return _per_type("save_paths", media_type)


def _simulated_hashes(params: object) -> list[str]:
    """The hashes a dry run made up that a torrent list read asks for."""
    if not isinstance(params, dict):
        return []
    return [h for h in str(params.get("hashes") or "").split("|") if dryrun.simulated(h)]


def _dry_run_answer(method: str, base: str, path: str, kwargs: dict) -> httpx.Response | None:
    """What the download client answers under ``dry_run``, without being asked; None to ask it.

    A write is not sent: an add answers as an accepted add of a torrent
    whose hash is marked, anything else as done. A read of a marked torrent
    answers that it is complete, so the download poll completes its item.
    Every other read goes to the client.
    """
    asked = httpx.Request(method, f"{base}{path}")
    if method.upper() == "GET":
        made = _simulated_hashes(kwargs.get("params"))
        if not made:
            return None
        return httpx.Response(200, request=asked, json=[
            {"hash": h, "state": "uploading", "progress": 1.0, "name": h} for h in made
        ])
    data = kwargs.get("data") or {}
    detail: dict = {"category": data.get("category") or None}
    if path == "/api/v2/torrents/add":
        files = kwargs.get("files") or {}
        if files:
            torrent = files.get("torrents", (None, b""))[1]
            info_hash = torrent_info_hash(torrent) if isinstance(torrent, bytes) else None
            action = "add .torrent"
        elif str(data.get("urls", "")).startswith("magnet:"):
            info_hash, action = _magnet_info_hash(data["urls"]), "add magnet"
        else:
            info_hash, action = None, "add URL"
            detail["url"] = shown(data.get("urls"))
        detail["hash"] = info_hash
        dryrun.would_have("download client", action, method="POST", path=path, **detail)
        return httpx.Response(
            200, request=asked, json={"success_count": 1, "added_torrent_ids": [dryrun.mark(info_hash)]},
        )
    hashes = data.get("hashes")
    dryrun.would_have(
        "download client", path.rsplit("/", 1)[-1], method=method.upper(), path=path,
        hash=hashes, delete_files=data.get("deleteFiles"), **detail,
    )
    return httpx.Response(200, request=asked, text="Ok.")


# qBittorrent reports these when the torrent is registered but unusable, and
# neither recovers on its own. Every other state -- downloading, queued,
# stalled, checking, or seeding an existing copy -- means qBittorrent has it.
_BROKEN_STATES = {"error", "missingfiles"}
# A bulk reject removes its torrents one after another. The list of every
# torrent's path is read once and reused for this long, so twenty rejects cost
# one read of the 18 MB list, not twenty.
_PATHS_CACHE_SECONDS = 15.0


def _norm_path(path: object) -> str | None:
    if not isinstance(path, str):
        return None
    return path.rstrip("/") or None


def _paths_overlap(own: str, own_root: str | None, other: str, other_root: str | None) -> bool:
    """True when deleting the files under `own` takes files from `other`.

    `content_path` is the torrent's folder or single file. A multi-file torrent
    with no root folder reports its save path instead (the category root), and
    that path "contains" every torrent in the category; such a flat torrent
    only counts when the paths are equal, never through containment.
    """
    if own == other:
        return True
    if other.startswith(own + "/") and own != own_root:
        return True
    return own.startswith(other + "/") and other != other_root


_CONFIRM_ATTEMPTS = 6
_CONFIRM_DELAY = 1.0
# How many of the most recently added torrents to look at while waiting for one
# that was just handed over. Asking for the whole list instead costs 18 MB an
# answer on a library of ten thousand torrents, thirty times per wait.
_PENDING_WINDOW = 50


class QBittorrentClient:
    def __init__(self) -> None:
        self._client = httpx.AsyncClient(timeout=30)
        # Separate from _client so the indexer never shares the qBittorrent
        # cookie jar: the two often differ only by port, and cookies ignore
        # ports, so one jar would hand the session around and let either side
        # overwrite the other's.
        self._indexer_client = httpx.AsyncClient(timeout=30)
        self._authenticated = False
        self._auth_lock = asyncio.Lock()
        self._paths_cache: tuple[float, list[tuple[str, str, str | None]]] | None = None

    # Read on first use, not at construction: a module-level client is built
    # at import, before the config is resolved.
    @cached_property
    def _base(self) -> str:
        return settings.qbittorrent_url.rstrip("/")

    @cached_property
    def _user(self) -> str:
        return settings.qbittorrent_user

    @cached_property
    def _pass(self) -> str:
        return settings.qbittorrent_pass

    async def close(self) -> None:
        await self._client.aclose()
        await self._indexer_client.aclose()

    async def _login(self) -> None:
        async with self._auth_lock:
            if self._authenticated:
                return
            resp = await self._client.post(
                f"{self._base}/api/v2/auth/login",
                data={"username": self._user, "password": self._pass},
                headers={"Referer": self._base},
            )
            if resp.status_code == 204 or (
                resp.is_success and resp.text.strip() != "Fails."
            ):
                self._authenticated = True
            else:
                raise RuntimeError(f"qBittorrent login failed: HTTP {resp.status_code}")

    async def _request(
        self,
        method: str,
        path: str,
        *,
        retry: bool = True,
        **kwargs,
    ) -> httpx.Response:
        if dryrun.active() or _simulated_hashes(kwargs.get("params")):
            simulated = _dry_run_answer(method, self._base, path, kwargs)
            if simulated is not None:
                return simulated
        await self._login()
        resp = await self._client.request(method, f"{self._base}{path}", **kwargs)
        if resp.status_code in (401, 403) and retry:
            self._authenticated = False
            await self._login()
            return await self._request(method, path, retry=False, **kwargs)
        return resp

    async def _fetch_torrent_file(self, url: str) -> bytes | None:
        """Download a .torrent from an indexer link.

        Under ``dry_run`` it is not fetched: the tracker behind the link
        counts the fetch as a download. The add goes on with the link.
        """
        if dryrun.active():
            dryrun.would_have(
                "indexer", "fetch .torrent", method="GET", path=shown(url), reason=dryrun.NOT_FETCHED,
            )
            return None
        try:
            resp = await self._indexer_client.get(url, follow_redirects=True)
        except (httpx.HTTPError, httpx.InvalidURL):
            # InvalidURL is not an HTTPError, and a link that redirects to
            # something httpx cannot parse must not take the whole add down.
            return None
        if not resp.is_success or not resp.content.startswith(b"d"):
            return None
        return resp.content

    async def _existing_torrent_hash(
        self,
        info_hash: str | None,
        download_url: str | None,
    ) -> str | None:
        """Return the hash if qBittorrent is already holding this torrent.

        A URL add carries no identity, so the .torrent is fetched here purely
        to compute the info hash the add itself never reveals.
        """
        if info_hash is None and download_url:
            torrent_data = await self._fetch_torrent_file(download_url)
            if torrent_data:
                info_hash = torrent_info_hash(torrent_data)
        if not info_hash:
            return None
        return info_hash if await self._torrent_exists(info_hash) else None

    async def _torrent_exists(self, info_hash: str) -> bool:
        resp = await self._request(
            "GET",
            "/api/v2/torrents/info",
            params={"hashes": info_hash},
        )
        if not resp.is_success:
            return False
        try:
            torrents = resp.json()
        except json.JSONDecodeError:
            # An older qBittorrent answers a plain "Ok." to some calls. Not a
            # list of torrents means nothing can be confirmed present.
            return False
        if not isinstance(torrents, list):
            return False
        return any(
            isinstance(item, dict) and item.get("hash", "").lower() == info_hash.lower()
            for item in torrents
        )

    async def _wait_for_pending_torrent(
        self,
        known_hashes: set[str],
        *,
        expected_hash: str | None = None,
        expected_title: str | None = None,
    ) -> str | None:
        expected_name = _normalize_name(expected_title or "")
        # Never the whole list. A torrent that has just arrived is the hash we
        # expect, or it is among the most recently added ones.
        params = (
            {"hashes": expected_hash}
            if expected_hash
            else {
                "sort": "added_on",
                "reverse": "true",
                "limit": str(_PENDING_WINDOW),
            }
        )
        for _ in range(30):
            await asyncio.sleep(0.5)
            resp = await self._request(
                "GET", "/api/v2/torrents/info", params=params
            )
            if not resp.is_success:
                continue
            torrents = resp.json()
            if expected_hash and any(
                item.get("hash", "").lower() == expected_hash
                for item in torrents
            ):
                return expected_hash
            candidates = [
                item for item in torrents
                if item.get("hash", "").lower() not in known_hashes
            ]
            if expected_name:
                candidates = [
                    item for item in candidates
                    if expected_name in _normalize_name(item.get("name", ""))
                    or _normalize_name(item.get("name", "")) in expected_name
                ]
            if len(candidates) == 1:
                return candidates[0].get("hash", "").lower() or None
        return None

    async def add_torrent(
        self,
        media_type: MediaType,
        magnet_url: str | None = None,
        download_url: str | None = None,
        expected_title: str | None = None,
    ) -> AddOutcome | None:
        await self._login()
        # A reject a few seconds from now must see this torrent.
        self._paths_cache = None

        category = category_for(media_type)
        save_path = save_path_for(media_type)

        data: dict = {}
        if category:
            data["category"] = category
        if save_path:
            data["savepath"] = save_path

        if magnet_url:
            data["urls"] = magnet_url
        elif download_url:
            data["urls"] = download_url
        else:
            log.error("No magnet or download URL provided")
            return None

        if not magnet_url:
            # A link names nothing, and qBittorrent 5.x answers a duplicate
            # link with the same 202 it gives one it is still resolving. So
            # fetch the .torrent here and hand over the file: its info hash is
            # exact, one question says whether the client already held it, and
            # nobody has to read the client's whole list to find out. Before
            # this, a link whose release title differed from the torrent's name
            # cost thirty-one full lists (18 MB each on this library) and a
            # minute of polling, and the torrent we had just added came back
            # reported as one the client already held.
            torrent_data = await self._fetch_torrent_file(download_url)
            if torrent_data:
                return await self.add_torrent_file(torrent_data, media_type)
            log.info("Could not fetch the .torrent; handing qBittorrent the link instead")

        async with _ADD_LOCK:
            info_hash = _magnet_info_hash(magnet_url) if magnet_url else None
            known_hashes: set[str] = set()
            # Whether the client already held this torrent before we asked. A
            # magnet names its own hash, so one question answers it. A link we
            # could not fetch names nothing, so the whole list is the baseline
            # instead: one full list, on the rare path only.
            held_before = bool(info_hash) and await self._torrent_exists(info_hash)
            if not info_hash:
                before_resp = await self._request("GET", "/api/v2/torrents/info")
                if not before_resp.is_success:
                    log.error("Cannot verify pending URL torrent: baseline request failed")
                    return None
                existing_torrents = before_resp.json()
                known_hashes = {
                    item.get("hash", "").lower()
                    for item in existing_torrents
                }
                expected_name = _normalize_name(expected_title or "")
                existing_matches = [
                    item for item in existing_torrents
                    if expected_name
                    and item.get("category") == category
                    and _normalize_name(item.get("name", "")) == expected_name
                ]
                if len(existing_matches) == 1:
                    existing_hash = existing_matches[0].get("hash", "").lower()
                    if existing_hash:
                        log.info(
                            "Torrent already present in qBittorrent (duplicate): %s",
                            existing_hash,
                        )
                        return AddOutcome(hash=existing_hash, created=False)

            resp = await self._request("POST", "/api/v2/torrents/add", data=data)
            if _add_was_accepted(resp):
                log.info("Torrent added to qBittorrent (category=%s)", category)
                added = _added_torrent_hash(resp) or info_hash
                return AddOutcome(
                    hash=added,
                    created=self._is_new(added, held_before, known_hashes),
                )
            if resp.is_success:
                # 5.x answers a URL add with 202 and an identical body whether
                # it is still resolving the link or has silently dropped a
                # duplicate, so only the torrent list can tell the two apart.
                pending_hash = await self._wait_for_pending_torrent(
                    known_hashes,
                    expected_hash=info_hash or _added_torrent_hash(resp),
                    expected_title=expected_title,
                )
                if pending_hash:
                    log.info("Pending torrent appeared in qBittorrent: %s", pending_hash)
                    return AddOutcome(
                        hash=pending_hash,
                        created=self._is_new(pending_hash, held_before, known_hashes),
                    )
                duplicate_hash = await self._existing_torrent_hash(
                    info_hash, download_url,
                )
                if duplicate_hash:
                    log.info(
                        "Torrent already present in qBittorrent (duplicate): %s",
                        duplicate_hash,
                    )
                    return AddOutcome(hash=duplicate_hash, created=False)
            if resp.status_code == 409 and info_hash:
                if await self._torrent_exists(info_hash):
                    log.info(
                        "Torrent already present in qBittorrent (duplicate): %s",
                        info_hash,
                    )
                    return AddOutcome(hash=info_hash, created=False)
            log.error("qBittorrent add failed: HTTP %s", resp.status_code)
            return None

    @staticmethod
    def _is_new(
        torrent_hash: str | None,
        held_before: bool,
        known_hashes: set[str],
    ) -> bool:
        """True only when this add is what put the torrent in the client.

        Everything unproven answers False, because the cost of being wrong the
        other way is deleting files nobody asked to lose.
        """
        if not torrent_hash or held_before:
            return False
        return torrent_hash.lower() not in known_hashes

    async def add_torrent_file(
        self,
        torrent_data: bytes,
        media_type: MediaType,
    ) -> AddOutcome | None:
        """Add a .torrent file (raw bytes) to qBittorrent."""
        await self._login()
        self._paths_cache = None

        category = category_for(media_type)
        save_path = save_path_for(media_type)

        files = {"torrents": ("file.torrent", torrent_data, "application/x-bittorrent")}
        data: dict = {}
        if category:
            data["category"] = category
        if save_path:
            data["savepath"] = save_path

        async with _ADD_LOCK:
            # The .torrent names its own hash, so whether the client already
            # held it is one question, asked before anything is added.
            info_hash = torrent_info_hash(torrent_data)
            held_before = bool(info_hash) and await self._torrent_exists(info_hash)

            resp = await self._request(
                "POST", "/api/v2/torrents/add", files=files, data=data
            )
            if _add_was_accepted(resp):
                log.info("Torrent file added to qBittorrent (category=%s)", category)
                added = _added_torrent_hash(resp) or info_hash
                return AddOutcome(
                    hash=added,
                    created=self._is_new(added, held_before, set()),
                )
            if resp.is_success or resp.status_code == 409:
                if info_hash and await self._torrent_exists(info_hash):
                    log.info(
                        "Torrent already present in qBittorrent (duplicate): %s",
                        info_hash,
                    )
                    return AddOutcome(hash=info_hash, created=False)
            log.error("qBittorrent add_torrent_file failed: HTTP %s", resp.status_code)
            return None

    async def confirm_torrent_healthy(
        self,
        torrent_hash: str,
        *,
        attempts: int = _CONFIRM_ATTEMPTS,
        delay: float = _CONFIRM_DELAY,
    ) -> bool:
        """Return True once qBittorrent holds the torrent in a working state.

        A freshly added torrent takes a moment to appear, so poll before
        giving up. A broken state is final and fails straight away.
        """
        await self._login()
        wanted = torrent_hash.lower()
        for attempt in range(attempts):
            if attempt:
                await asyncio.sleep(delay)
            resp = await self._request(
                "GET",
                "/api/v2/torrents/info",
                params={"hashes": torrent_hash},
            )
            if not resp.is_success:
                continue
            try:
                torrents = resp.json()
            except json.JSONDecodeError:
                continue
            state = next(
                (
                    str(item.get("state", "")).lower()
                    for item in torrents
                    if item.get("hash", "").lower() == wanted
                ),
                None,
            )
            if state is None or state == "unknown":
                continue
            if state in _BROKEN_STATES:
                log.error("Torrent %s is in qBittorrent state %s", torrent_hash, state)
                return False
            return True
        log.error("Torrent %s never registered healthy in qBittorrent", torrent_hash)
        return False

    async def get_torrent_progress(self, torrent_hash: str) -> float | None:
        await self._login()
        resp = await self._request(
            "GET",
            "/api/v2/torrents/info",
            params={"hashes": torrent_hash},
        )
        resp.raise_for_status()
        data = resp.json()
        if data:
            return data[0].get("progress", 0.0)
        return None

    async def list_all_names(self) -> list[tuple[str, str]]:
        """(name, category) of every torrent in qBittorrent; no category is ""."""
        await self._login()
        resp = await self._request("GET", "/api/v2/torrents/info")
        resp.raise_for_status()
        # Keep each name and category as its object is parsed and drop the
        # rest: the full objects of ten thousand torrents are 30 MB that nobody reads.
        rows = json.loads(
            resp.content,
            object_hook=lambda torrent: (torrent.get("name"), torrent.get("category") or ""),
        )
        return [(name, category) for name, category in rows if isinstance(name, str)]

    async def _all_content_paths(self) -> list[tuple[str, str, str | None]]:
        """(hash, content_path, save_path) of every torrent, read once per cache window."""
        now = time.monotonic()
        if self._paths_cache is not None and now - self._paths_cache[0] < _PATHS_CACHE_SECONDS:
            return self._paths_cache[1]
        resp = await self._request("GET", "/api/v2/torrents/info")
        resp.raise_for_status()
        # Reduce each torrent to the two fields as it is parsed; the full
        # objects of ten thousand torrents are 30 MB that nobody reads.
        rows = json.loads(
            resp.content,
            object_hook=lambda torrent: (
                torrent.get("hash"), torrent.get("content_path"), torrent.get("save_path")
            ),
        )
        paths = [
            (h.lower(), _norm_path(p), _norm_path(root))
            for h, p, root in rows
            if isinstance(h, str) and _norm_path(p) is not None
        ]
        self._paths_cache = (now, paths)
        return paths

    async def _files_shared(self, torrent_hash: str) -> bool | None:
        """Whether another torrent in qBittorrent lives on this torrent's files.

        Two torrents of the same release from different trackers land in the
        same folder, and a single-episode torrent can sit inside a season
        pack's folder. Deleting the files of one breaks the other, which then
        shows missingFiles. None means the answer could not be read.
        """
        resp = await self._request(
            "GET", "/api/v2/torrents/info", params={"hashes": torrent_hash}
        )
        resp.raise_for_status()
        own = resp.json()
        own_path = _norm_path(own[0].get("content_path")) if own else None
        if own_path is None:
            return None
        own_root = _norm_path(own[0].get("save_path"))
        for other_hash, other_path, other_root in await self._all_content_paths():
            if other_hash == torrent_hash:
                continue
            if _paths_overlap(own_path, own_root, other_path, other_root):
                return True
        return False

    async def remove_torrent(self, torrent_hash: str, delete_files: bool = True) -> bool:
        """Drop a torrent from qBittorrent, files and all.

        Used when a rejected item was already handed to qBittorrent: the point
        of rejecting it is that the content should not be on the disk. The
        files stay when another torrent still uses them, or when that could
        not be checked: a stale torrent is cheap, somebody's broken library
        copy is not.
        """
        if not torrent_hash:
            return False
        torrent_hash = torrent_hash.lower()
        if delete_files:
            try:
                shared = await self._files_shared(torrent_hash)
            except Exception:
                log.exception("Could not check whether %s shares its files", torrent_hash)
                shared = None
            if shared is not False:
                log.warning(
                    "Keeping the files of %s: %s",
                    torrent_hash,
                    "another torrent uses them" if shared else "the check could not be made",
                )
                delete_files = False
        try:
            resp = await self._request(
                "POST",
                "/api/v2/torrents/delete",
                data={
                    "hashes": torrent_hash,
                    "deleteFiles": "true" if delete_files else "false",
                },
            )
        except Exception:
            log.exception("qBittorrent delete failed for %s", torrent_hash)
            return False
        if not resp.is_success:
            log.warning(
                "qBittorrent refused to delete %s: HTTP %s", torrent_hash, resp.status_code
            )
            return False
        log.info("Removed torrent %s from qBittorrent", torrent_hash)
        return True

    async def list_active(self) -> list[dict]:
        await self._login()
        resp = await self._request(
            "GET",
            "/api/v2/torrents/info",
            params={"filter": "downloading"},
        )
        resp.raise_for_status()
        return resp.json()
