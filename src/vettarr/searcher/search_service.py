"""Search and download, as the worker runs it after an approval.

The pieces a person's own search and grab use as well -- the one search
(`search_for`), the stored release list, adding a release and the download
row -- live here once, and `vettarr.actions` builds the web and bot
actions from them.

Everything tracker-specific comes from config.yaml's `providers`: which hosts a
session may go to, how a post's link becomes a download URL, how to search a
tracker directly, and whether to thank an uploader or keep a session alive.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import Any

from sqlalchemy import select, update

from vettarr import dryrun
from vettarr.config import settings
from vettarr.config.schema import Config, Provider
from vettarr.database import async_session
from vettarr.models import (
    OP_APPROVAL,
    Download,
    DownloadStatus,
    ItemStatus,
    MediaItem,
    MediaType,
    TorrentSearch,
    record_torrent_origin,
)
from vettarr.searcher import providers
from vettarr.searcher.prowlarr_client import (
    ProwlarrClient,
    SearchResult,
    TorrentResult,
    clean_title,
)
from vettarr.searcher.qbittorrent_client import QBittorrentClient, category_for
from vettarr.searcher.tracker_guard import GuardedUnit3dClient, TrackerGuard
from vettarr.searcher.unit3d_client import Unit3dClient

log = logging.getLogger(__name__)

prowlarr = ProwlarrClient()
qbittorrent = QBittorrentClient()

# One client per provider, named by its key: the session, the direct search,
# the .torrent fetch, the thank and the keep-alive all go through it.
_trackers: list[Unit3dClient] = []
_trackers_initialized = False

# Book titles on the tracker read "Title - Author - Year - Series", and its
# search matches the keywords as one piece of the title in order, so a year
# appended to "Title - Author" finds nothing. Films carry "Title Year" and
# keep theirs.
_NO_YEAR_IN_QUERY = frozenset({MediaType.EBOOK, MediaType.AUDIOBOOK, MediaType.COMIC})

#: `SearchResult.error` when Prowlarr and every direct search failed.
ALL_UNAVAILABLE = "All search providers unavailable"
#: `SearchResult.error` when there is nothing to search with: no Prowlarr and
#: no provider with a direct search.
NO_SEARCH = "No search is configured"
PROWLARR_UNAVAILABLE = "Prowlarr search unavailable"


def has_search(config: Config | None = None) -> bool:
    """Whether anything is set up to search with: Prowlarr, or a provider's own search.

    `search_for`'s own condition, asked before a search is started, so a
    person's Search is not offered, or is refused, when it could only fail.
    """
    config = config or settings.config
    return config.integrations.enabled("prowlarr") or any(p.search != "none" for p in config.providers)


def search_failure(result: SearchResult) -> str | None:
    """What an item shows when no search completed, or None when one did.

    One wording for the worker and for a person's own search.
    """
    if result.error is None:
        return None
    return result.error if result.error in (ALL_UNAVAILABLE, NO_SEARCH) else PROWLARR_UNAVAILABLE


@dataclass
class ItemRef:
    id: int
    title: str | None
    year: int | None
    quality: str | None
    season: str | None
    media_type: MediaType
    telegram_message_id: int
    tmdb_id: int | None = None
    imdb_id: str | None = None
    tmdb_title: str | None = None
    external_links: list[str] = field(default_factory=list)
    #: ``MediaItem.origin_key``: says when the item is a Seerr request, which
    #: is approved in Seerr and never searched for.
    origin_key: str | None = None


@dataclass
class SearchItem:
    """What `search_for` reads of an item. `ItemRef` carries the same fields."""

    title: str
    year: int | None
    season: str | None
    media_type: MediaType
    tmdb_title: str | None = None
    tmdb_id: int | None = None
    imdb_id: str | None = None


# --- providers -------------------------------------------------------------


def init_trackers() -> None:
    """Build one guarded client per configured provider, once."""
    global _trackers_initialized
    if _trackers_initialized:
        return
    _trackers_initialized = True
    for provider in settings.config.providers:
        auth = provider.auth
        cookie_path = auth.cookie_file or ""
        if auth.from_ == "cookie_file" and not Path(cookie_path).exists():
            log.info("Provider %s skipped (no cookie file at %s)", provider.key, cookie_path)
            continue
        guard = TrackerGuard(
            provider.key, cookie_path,
            error_limit=provider.guard.error_limit,
            base_backoff=provider.guard.backoff_minutes * 60.0,
            max_backoff=provider.guard.max_backoff_minutes * 60.0,
        )
        _trackers.append(GuardedUnit3dClient(
            provider.base_url, cookie_path, name=provider.key,
            username=auth.username or "",
            password=auth.password.get_secret_value() if auth.password else "",
            logged_out_marker=provider.logged_out_marker or "",
            software=provider.search,
            allowed=partial(providers.carries_session, provider),
            cookie_hosts=[host for host, _ in providers.hosts(provider)],
            guard=guard,
        ))
        log.info("Provider %s initialized (session from %s, search: %s)", provider.key, auth.from_, provider.search)


def get_trackers() -> list[Unit3dClient]:
    return _trackers


def _client(key: str) -> Unit3dClient | None:
    return next((t for t in _trackers if t.name == key), None)


def _provider(key: str, config: Config | None = None) -> Provider | None:
    return next((p for p in (config or settings.config).providers if p.key == key), None)


async def _with_session(provider: Provider) -> Unit3dClient | None:
    """The provider's client, holding Prowlarr's cookie when Prowlarr has the session."""
    init_trackers()
    client = _client(provider.key)
    if client is None:
        return None
    if provider.auth.from_ == "prowlarr":
        cookie = await prowlarr.cookie_for(provider)
        if cookie is None:
            log.info("No Prowlarr indexer is logged in to %s", provider.key)
            return None
        client.use_cookie_header(cookie)
    return client


async def thank_after_download(provider: Provider, torrent_id: str | None) -> None:
    """Thank the uploader once a download started, when the provider says so."""
    if not provider.thank.enabled or not torrent_id:
        return
    client = await _with_session(provider)
    if client is None:
        return
    thank = provider.thank
    try:
        ok = await asyncio.to_thread(client.thank, torrent_id, thank.path, thank.method, thank.body)
    except Exception:
        log.exception("Thank error for torrent %s on %s", torrent_id, provider.key)
        return
    if ok:
        log.info("Thanked torrent %s on %s", torrent_id, provider.key)
    else:
        log.warning("Thank failed for torrent %s on %s", torrent_id, provider.key)


async def keep_alive(key: str) -> bool:
    """Visit the provider's `keep_alive.path` with its session. The scheduler calls this."""
    provider = _provider(key)
    if provider is None:
        return False
    client = await _with_session(provider)
    if client is None:
        return False
    try:
        return await asyncio.to_thread(client.keep_alive, provider.keep_alive.path)
    except Exception:
        log.exception("Session keep-alive failed for %s", key)
        return False


async def check_session(key: str) -> bool | None:
    """Ask the provider now whether its session is live. None when the app has no client for it.

    One request to the tracker, through its guard, which sends nothing while
    a cooldown is open. For the Setup page's "Check now", never for a page load.
    """
    provider = _provider(key)
    if provider is None:
        return None
    client = await _with_session(provider)
    if client is None:
        return None
    try:
        return await asyncio.to_thread(client.check_session)
    except Exception:
        log.exception("Session check failed for %s", key)
        return False


# --- the linked torrent ----------------------------------------------------


@dataclass
class LinkedFetch:
    data: bytes
    url: str
    provider: Provider
    torrent_id: str


async def fetch_linked_torrent(links: list[str] | None) -> LinkedFetch | None:
    """The .torrent a post links to, through the provider whose link pattern reads it.

    None when no provider's pattern reads a link, when there is no session
    for that provider, or when the tracker answered with anything but a
    torrent. The caller searches then, as it always did.
    """
    linked = providers.linked_torrent(settings.config.providers, links)
    if linked is None:
        return None
    if dryrun.active():
        # The tracker counts the fetch as a download (`linked_not_fetched`).
        not_fetched(linked)
        return None
    client = await _with_session(linked.provider)
    if client is None:
        log.info("No session for %s; searching instead", linked.provider.key)
        return None
    try:
        data = await asyncio.to_thread(client.fetch_torrent, linked.download_url)
    except Exception as exc:
        log.warning(
            "Linked torrent %s could not be fetched: %s",
            providers.shown(linked.download_url), type(exc).__name__,
        )
        return None
    if not data:
        log.warning(
            "Linked torrent %s answered without a torrent; searching instead",
            providers.shown(linked.download_url),
        )
        return None
    return LinkedFetch(data, linked.download_url, linked.provider, linked.torrent_id)


def linked_not_fetched(links: list[str] | None) -> providers.LinkedTorrent | None:
    """Under ``dry_run``: the torrent a post links to that live would fetch, and a dry run does not.

    None when no provider's pattern reads a link, or when it is not a dry run.
    """
    if not dryrun.active():
        return None
    return providers.linked_torrent(settings.config.providers, links)


def not_fetched(linked: providers.LinkedTorrent, item_id: int | None = None) -> None:
    """Record the ``.torrent`` a dry run did not fetch from its tracker."""
    dryrun.would_have(
        linked.provider.key, "fetch .torrent", method="GET",
        path=providers.shown(linked.download_url), item_id=item_id, reason=dryrun.NOT_FETCHED,
    )


def not_fetched_refusal(provider_key: str) -> AddRefused:
    """Why a dry run adds nothing whose ``.torrent`` only the tracker has."""
    return AddRefused(f"{dryrun.NOT_FETCHED.capitalize()}: {provider_key} counts a fetch as a download.")


# --- the search ------------------------------------------------------------


# How many releases of a search are kept on the item for a person to pick from.
STORED_RELEASES = 10


def search_query(
    title: str,
    year: int | None,
    season: str | None,
    media_type: MediaType,
) -> str:
    """The one Prowlarr query for an item, whoever asks for the search."""
    if media_type in _NO_YEAR_IN_QUERY:
        year = None
    return prowlarr.build_query(title, year, season)


async def find_releases(
    title: str,
    year: int | None,
    season: str | None,
    media_type: MediaType,
    tmdb_title: str | None = None,
) -> SearchResult:
    """Search Prowlarr for the post's title, then for TMDB's when that differs.

    `error` is set only when no search completed: a fallback that fails after
    a first search that answered with nothing is still a search that found
    nothing, not an outage.
    """
    result = await prowlarr.search(search_query(title, year, season, media_type), media_type)
    if result.results or not tmdb_title:
        return result
    if clean_title(tmdb_title).lower() == clean_title(title).lower():
        return result
    log.info("Retrying Prowlarr with TMDB title '%s'", tmdb_title)
    fallback = await prowlarr.search(
        search_query(tmdb_title, year, season, media_type), media_type,
    )
    if fallback.error is not None and result.error is None:
        return result
    return fallback


async def search_for(item: Any, config: Config | None = None) -> SearchResult:
    """The one search for an item, for the worker, the web and the bot alike.

    Prowlarr first, when `integrations.prowlarr` is on, then TMDB's title when
    that differs. When that finds nothing, each provider with a direct search
    (`search` is not `none`), in file order, until one finds something. A
    direct search filters by the item's IMDb and TMDB ids when it has them, so
    `item` carries them whoever asks. `error` is set only when no search
    completed at all.
    """
    config = config or settings.config
    if config.integrations.enabled("prowlarr"):
        result = await find_releases(
            item.title, item.year, item.season, item.media_type, getattr(item, "tmdb_title", None),
        )
    else:
        # Nothing to ask: no request, and no failure to log either.
        result = SearchResult(
            query=search_query(item.title, item.year, item.season, item.media_type), error=NO_SEARCH,
        )
    if result.results:
        return result
    searchers = [p for p in config.providers if p.search != "none"]
    if not searchers:
        return result

    cleaned = clean_title(item.title)
    log.info("Nothing from Prowlarr for '%s', trying direct tracker search", cleaned)
    completed = result.error is None
    for provider in searchers:
        client = await _with_session(provider)
        if client is None:
            continue
        try:
            found, error = await asyncio.to_thread(
                client.search_with_status, cleaned,
                imdb_id=getattr(item, "imdb_id", None), tmdb_id=getattr(item, "tmdb_id", None),
            )
        except Exception:
            log.exception("Tracker %s search failed for '%s'", provider.key, item.title)
            continue
        if error:
            log.warning("Tracker %s unavailable for '%s': %s", provider.key, item.title, error)
            continue
        completed = True
        if found:
            log.info("Found on %s: %s", provider.key, found[0].name)
            return SearchResult(
                results=[
                    TorrentResult(
                        guid=r.detail_url or r.download_url, title=r.name, size=r.size_bytes,
                        seeders=r.seeders, leechers=r.leechers, indexer=provider.key,
                        download_url=r.download_url, info_url=r.detail_url,
                        provider=provider.key, provider_id=str(r.torrent_id),
                    )
                    for r in found
                ],
                query=cleaned,
            )
    return SearchResult(query=result.query, error=None if completed else ALL_UNAVAILABLE)


def store_search(session, item_id: int, result: SearchResult) -> TorrentSearch:
    """Add the search's releases to the session, best first. Flush for its id."""
    ts = TorrentSearch(
        media_item_id=item_id,
        prowlarr_results=[_stored(r) for r in result.results[:STORED_RELEASES]],
        result_count=len(result.results),
    )
    if result.results:
        best = result.results[0]
        ts.best_match_guid = best.guid
        ts.best_match_title = best.title
        ts.best_match_size = best.size
        ts.best_match_seeders = best.seeders
        ts.best_match_url = best.download_url or best.magnet_url
    session.add(ts)
    return ts


def _stored(r: TorrentResult) -> dict:
    row = {
        "guid": r.guid, "title": r.title, "size": r.size,
        "seeders": r.seeders, "indexer": r.indexer,
        "download_url": r.download_url, "magnet_url": r.magnet_url,
        "info_url": r.info_url,
    }
    if _field(r, "provider"):
        row["provider"] = r.provider
        row["provider_id"] = r.provider_id
    return row


# --- adding a release ------------------------------------------------------


#: Why an add failed, when the download client said no.
CLIENT_REFUSED = "qBittorrent did not take the torrent. Check it under Integrations in Setup."


@dataclass(frozen=True)
class AddRefused:
    """An add that did not happen, with why in a sentence a person reads.

    False, like the None it stands for, so every ``if add_result`` still reads
    it as a failure; `record_download` writes the reason on the item.
    """

    reason: str

    def __bool__(self) -> bool:
        return False


def refusal_of(add_result: Any) -> str:
    """The sentence an item shows for a failed add."""
    return getattr(add_result, "reason", None) or CLIENT_REFUSED


def _session_fix(provider: Provider) -> str:
    if provider.auth.from_ == "prowlarr":
        return "Sign its indexer in to Prowlarr again."
    if provider.auth.from_ == "cookie_file":
        return "Paste new cookies in Setup."
    return "Check its username and password in the config file."


def _field(release: Any, name: str) -> Any:
    return release.get(name) if isinstance(release, dict) else getattr(release, name, None)


def _origin(release: Any) -> tuple[Provider | None, str | None]:
    """The provider a release came from and its torrent id there, if known."""
    key = _field(release, "provider")
    if key:
        return _provider(key), _field(release, "provider_id")
    for name in ("info_url", "guid", "download_url"):
        url = _field(release, name)
        provider = providers.provider_for_url(settings.config.providers, url or "")
        if provider is not None:
            for candidate in ("info_url", "guid", "download_url"):
                tid = providers.torrent_id(provider, _field(release, candidate))
                if tid:
                    return provider, tid
            return provider, None
    return None, None


async def add_release(release: Any, media_type: MediaType):
    """Hand one release (a search result or a stored one) to qBittorrent.

    A release a provider's direct search found is fetched with that
    provider's session and added as a file; any other goes as its magnet or
    link. A torrent this add put in the client is thanked for when the
    provider asks for it.
    """
    provider, torrent_id = _origin(release)
    if _field(release, "provider"):
        if provider is None:
            log.error("The provider of '%s' is no longer in the config", _field(release, "title"))
            return AddRefused(f"{_field(release, 'provider')} is no longer in the config, so its .torrent cannot be fetched.")
        client = await _with_session(provider)
        if client is None:
            log.error("No session on %s to fetch '%s'", provider.key, _field(release, "title"))
            return AddRefused(f"The tracker refused the .torrent: {provider.key} has no session. {_session_fix(provider)}")
        data = await asyncio.to_thread(client.fetch_torrent, _field(release, "download_url"))
        if not data and dryrun.active():
            # Its file comes only from the tracker, which counts the fetch as a download.
            return not_fetched_refusal(provider.key)
        if not data:
            log.error("Could not fetch the .torrent of '%s' from its tracker", _field(release, "title"))
            return AddRefused(
                f"The tracker refused the .torrent: {provider.key} did not hand it over with its session. "
                f"{_session_fix(provider)}"
            )
        add_result = await qbittorrent.add_torrent_file(data, media_type)
    else:
        add_result = await qbittorrent.add_torrent(
            media_type,
            magnet_url=_field(release, "magnet_url"),
            download_url=_field(release, "download_url"),
            expected_title=_field(release, "title"),
        )
    if add_result and add_result.created and provider is not None:
        await thank_after_download(provider, torrent_id)
    return add_result or AddRefused(CLIENT_REFUSED)


async def record_download(
    session,
    item_id: int,
    add_result,
    *,
    op: str,
    media_type: MediaType,
    download_url: str | None,
    magnet_url: str | None = None,
    torrent_search_id: int | None = None,
) -> None:
    """Write what qBittorrent did with the add onto the item's download row.

    Every add goes through here: the worker's two paths and a person's grab.
    `op` is the stamp the caller claimed the item with, so the item only moves
    on if nobody else took it in the meantime.
    """
    dl_result = await session.execute(
        select(Download).where(Download.media_item_id == item_id)
    )
    dl = dl_result.scalar_one_or_none()
    if dl is None:
        dl = Download(media_item_id=item_id)
        session.add(dl)
    dl.torrent_search_id = torrent_search_id
    dl.magnet_url = magnet_url
    dl.download_url = download_url
    dl.category = category_for(media_type) or None
    record_torrent_origin(dl, add_result)
    dl.status = DownloadStatus.DOWNLOADING if add_result else DownloadStatus.ERROR
    dl.started_at = datetime.now(timezone.utc) if add_result else None
    # A failed add says why on the item. The claim marker it was taken with
    # is not a sentence, and was what the item showed as its error.
    failed = {} if add_result else {"error_message": refusal_of(add_result)}
    await session.execute(
        update(MediaItem).where(
            MediaItem.id == item_id,
            MediaItem.status == ItemStatus.SEARCHING,
            MediaItem.error_message == op,
        )
        .values(
            status=ItemStatus.DOWNLOADING if add_result else ItemStatus.ERROR,
            reviewed_at=datetime.now(timezone.utc),
            **failed,
        )
    )
    await session.commit()


# --- the worker ------------------------------------------------------------


async def search_and_download(item: ItemRef) -> str:
    """The post's linked torrent, else `search_for` and its best release.

    Returns a status string: 'downloading', 'error', or 'no_match'.
    """
    if not item.title:
        return "no_match"

    init_trackers()

    # The post names its torrent. A search can pick a different release of
    # the same title, or nothing when the tracker's listing is down, so the
    # linked torrent comes first and the search is the fallback.
    linked = await fetch_linked_torrent(item.external_links)
    held = linked_not_fetched(item.external_links) if linked is None else None
    if linked or held is not None:
        if linked:
            add_result, url = await qbittorrent.add_torrent_file(linked.data, item.media_type), linked.url
        else:
            # Live adds the linked file. A search would pick another release,
            # so a dry run adds nothing, and the item says why.
            add_result, url = not_fetched_refusal(held.provider.key), held.download_url
        async with async_session() as session:
            await record_download(
                session, item.id, add_result, op=OP_APPROVAL,
                media_type=item.media_type, download_url=url,
            )
        log.info(
            "Linked torrent for '%s' (id=%d): %s",
            item.title, item.id, "ok" if add_result else "FAILED",
        )
        if add_result and add_result.created:
            await thank_after_download(linked.provider, linked.torrent_id)
        return "downloading" if add_result else "error"

    result = await search_for(item)

    async with async_session() as session:
        ts = store_search(session, item.id, result)
        await session.flush()

        if result.results:
            best = result.results[0]
            add_result = await add_release(best, item.media_type)
            await record_download(
                session, item.id, add_result, op=OP_APPROVAL,
                media_type=item.media_type,
                download_url=best.download_url, magnet_url=best.magnet_url,
                torrent_search_id=ts.id,
            )
            log.info("Auto-download for '%s' (id=%d): %s", item.title, item.id, "ok" if add_result else "FAILED")
            return "downloading" if add_result else "error"

        await session.commit()

    message = search_failure(result)
    status = ItemStatus.NO_MATCH if message is None else ItemStatus.ERROR
    async with async_session() as session:
        await session.execute(
            update(MediaItem).where(
                MediaItem.id == item.id,
                MediaItem.status == ItemStatus.SEARCHING,
                MediaItem.error_message == OP_APPROVAL,
            )
            .values(status=status, error_message=message)
        )
        await session.commit()
    if message:
        log.error("%s for '%s' (id=%d)", message, item.title, item.id)
        return "error"
    log.info("No results anywhere for '%s' (id=%d)", item.title, item.id)
    return "no_match"
