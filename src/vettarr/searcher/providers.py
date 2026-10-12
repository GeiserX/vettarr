"""What config.yaml's `providers` say about a tracker, as plain functions.

Two questions, answered apart:

- Whose is this link? (`on_provider`.) The host of `base_url` and each of
  `aliases`, compared exactly. A subdomain is another host, and so is a host
  that merely contains the name. The scheme is not part of the comparison, so
  `http://` and `https://` links to the same host are the same provider.
- May the session go to this URL? (`carries_session`.) Only to those hosts, on
  the port `base_url` or the alias names, and never from https down to http.

A post's link becomes a download URL through the provider's `links`: the link
must be on one of the provider's hosts, and a pattern must read its `id`. An id
is letters, digits, `-` and `_`; a link whose id holds anything else is not read,
because the id goes into a URL and a request body as it is.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from importlib import metadata
from urllib.parse import parse_qsl, urlsplit

from vettarr.config.schema import TORRENT_ID, Provider


def _version() -> str:
    try:
        return metadata.version("vettarr")
    except metadata.PackageNotFoundError:
        return "0"


#: Sent on every request to a tracker. The app says what it is; it never
#: passes itself off as a browser.
USER_AGENT = f"vettarr/{_version()} (+https://github.com/GeiserX/vettarr)"


_DEFAULT_PORT = {"http": 80, "https": 443}


def _split(value: str) -> tuple[str, str, int | None]:
    """Scheme, host and written port of a URL or a bare `host[:port]`.

    The host is lowercased, without a trailing dot. Anything unreadable is
    `("", "", None)`: no host, so it matches nothing.
    """
    # `requests` reads a backslash in the authority as the end of the host and
    # `urlsplit` does not, so the two would name different hosts for one URL.
    if "\\" in value:
        return "", "", None
    if "//" not in value:
        value = "//" + value
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        return "", "", None
    return parts.scheme.lower(), (parts.hostname or "").rstrip("."), port


def _target(url: str) -> tuple[str, str, int | None]:
    """Scheme, host and the port a request to `url` connects to."""
    scheme, host, port = _split(url)
    return scheme, host, port if port is not None else _DEFAULT_PORT.get(scheme)


def _hosts(base_url: str, aliases: Iterable[str]) -> tuple[tuple[str, int | None], ...]:
    scheme, host, port = _split(base_url)
    # `https://host:443` names no port of its own: it is the scheme's.
    out = [(host, None if port == _DEFAULT_PORT.get(scheme) else port)]
    out += [_split(alias)[1:] for alias in aliases]
    return tuple((host, port) for host, port in out if host)


def hosts(provider: Provider) -> tuple[tuple[str, int | None], ...]:
    """The provider's hosts: base_url's, then each alias. A port only when one is written."""
    return _hosts(provider.base_url, provider.aliases)


def on_provider(provider: Provider, url: str) -> bool:
    """True when `url` is on one of the provider's hosts, exactly.

    This says whose a link is, whatever its scheme. Whether the session may
    travel to it is `carries_session`.
    """
    _, host, port = _target(url or "")
    if not host:
        return False
    return any(host == h and (p is None or port == p) for h, p in hosts(provider))


def session_allowed(base_url: str, aliases: Iterable[str], url: str) -> bool:
    """True when a session for `base_url` (and `aliases`) may be sent to `url`.

    The host must be one of them, exactly. The port must be the one written
    for that host, or the scheme's own when none is. And the scheme must be
    https unless `base_url` itself is plain http: a cookie that travels over
    https is never repeated in the clear.
    """
    scheme, host, port = _target(url or "")
    if not host or scheme not in _DEFAULT_PORT:
        return False
    if _split(base_url)[0] == "https" and scheme != "https":
        return False
    return any(
        host == h and port == (p if p is not None else _DEFAULT_PORT[scheme])
        for h, p in _hosts(base_url, aliases)
    )


def carries_session(provider: Provider, url: str) -> bool:
    """True when the provider's session (cookie, login, token) may be sent to `url`."""
    return session_allowed(provider.base_url, provider.aliases, url)


def shown(url: str | None) -> str:
    """`url` as a log line may carry it: no user or password, and of the query only `id`.

    A base URL written with a password, or a download link that embeds a
    passkey, would otherwise put that secret in the log.
    """
    try:
        parts = urlsplit(url or "")
        port = parts.port
    except ValueError:
        return "(unreadable url)"
    host = (parts.hostname or "") + (f":{port}" if port else "")
    query = "&".join(
        f"{key}={value}" if key == "id" else f"{key}=..."
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
    )
    return f"{parts.scheme}://{host}{parts.path}" + (f"?{query}" if query else "")


def provider_for_url(providers: Iterable[Provider], url: str) -> Provider | None:
    return next((p for p in providers if on_provider(p, url)), None)


def base(provider: Provider) -> str:
    return provider.base_url.rstrip("/")


def fill(template: str, torrent_id: str) -> str:
    """`template` with `{id}` replaced. Nothing else in it is read as a placeholder."""
    return template.replace("{id}", str(torrent_id))


def _read_id(pattern: str, url: str) -> str | None:
    """The id `pattern` reads in `url`, when it is one that can go into a URL.

    None when the pattern does not match, when it matched through a branch
    that does not capture `id`, and when what it captured is not an id
    (`TORRENT_ID`): a slash, a dot, a `?` or an `&` in it would steer the
    download and the thank request somewhere else on the tracker.
    """
    match = re.search(pattern, url)
    if not match:
        return None
    tid = match.group("id")
    return tid if tid and TORRENT_ID.fullmatch(tid) else None


def torrent_id(provider: Provider, url: str | None) -> str | None:
    """The torrent id one of the provider's link patterns reads in `url`, if it is on the provider."""
    if not url or not on_provider(provider, url):
        return None
    for link in provider.links:
        tid = _read_id(link.pattern, url)
        if tid:
            return tid
    return None


@dataclass(frozen=True)
class LinkedTorrent:
    provider: Provider
    torrent_id: str
    download_url: str


def linked_torrent(providers: Iterable[Provider], links: Iterable[str] | None) -> LinkedTorrent | None:
    """The first post link a provider's pattern turns into a download URL."""
    providers = list(providers)
    for link in links or ():
        provider = provider_for_url(providers, link)
        if provider is None:
            continue
        for shape in provider.links:
            tid = _read_id(shape.pattern, link)
            if tid is None:
                continue
            url = shape.download_url.replace("{base}", base(provider)).replace("{id}", tid)
            return LinkedTorrent(provider, tid, url)
    return None
