"""Who may use the web UI.

Three modes, from ``web.auth`` in the config:

- ``password``: ``auth.password_hash``. A login page, then a signed session
  cookie.
- ``proxy``: ``auth.trusted_header``. A reverse proxy signs people in and
  sets the header; a request without it is refused. The app trusts whoever
  reaches the port, so the port must be reachable only through that proxy.
- ``none``: no ``auth`` block. Everything is open, and the nav says so.

``Gate`` holds the mode and decides; ``GateMiddleware`` puts it in front of
every route and both static mounts. In order, it:

1. closes every websocket: no page has one, and the one that adds the first
   adds its check here;
2. refuses a POST, PUT, PATCH or DELETE that another site sent
   (``cross_origin``), in every mode;
3. lets ``/static`` through, so the login page has its stylesheet, and
   ``/hooks/``, where another program calls with a secret of its own
   (``web/routes/hooks.py``);
4. asks for a session (or the proxy's header) on everything else,
   ``/photos`` included, and tells the browser not to keep what it serves
   there, so Back after signing out shows nothing.

Which path a request is for is read from ``scope["path"]``, the one the
router uses. ``request.url`` is built from the ``Host`` header, which is the
client's to write.

The session cookie is ``<version>.<expires>.<remember>.<signature>``: an
HMAC-SHA256 over the first three parts. The key is made from a random secret
the server keeps in a file (``session_secret``) and the password hash, so the
hash line alone signs nothing, a new password signs every device out, and no
session is stored. The cookie is ``HttpOnly`` and ``SameSite=Lax``: Lax, not
Strict, so a link tapped in another app (the bot's item links) arrives signed
in. Lax still sends the cookie on a cross-site top-level GET, which is why no
GET changes anything and every other method goes through ``cross_origin``.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import logging
import math
import os
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlsplit

from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.responses import PlainTextResponse, RedirectResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from starlette.websockets import WebSocketClose

from vettarr.config.schema import Web
from vettarr.web import passwords

log = logging.getLogger(__name__)

COOKIE = "vettarr_session"
SESSION_SECONDS = 12 * 3600
REMEMBER_SECONDS = 30 * 24 * 3600
MAX_FAILURES = 5
LOCK_SECONDS = 15 * 60
# One scrypt check takes about 33 MB; this bounds what a burst of logins costs.
_CONCURRENT_CHECKS = 2
_TRACKED_ADDRESSES = 10_000
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_VERSION = "v1"

PASSWORD, PROXY, NONE = "password", "proxy", "none"
#: Where other programs call in (``web/routes/hooks.py``). Outside the login.
HOOKS = "/hooks/"

CROSS_ORIGIN = "Refused: this request came from another site."
SIGN_IN_FIRST = "Sign in first."


@dataclass(frozen=True)
class Identity:
    """Who a request is from. ``user`` is only known behind a proxy."""

    mode: str
    user: str | None = None


# --- the session cookie ----------------------------------------------------


_SECRET_BYTES = 32


def session_secret(path: Path) -> bytes:
    """The server's own half of the signing key: random, made once, kept in ``path``.

    The file is readable by the app's user only. Deleting it signs every
    device out. When it cannot be written the secret lives in memory, and
    sessions end when the app restarts.
    """
    try:
        kept = path.read_bytes().strip()
    except OSError:
        kept = b""
    if len(kept) >= _SECRET_BYTES:
        return kept
    secret = secrets.token_hex(_SECRET_BYTES).encode("ascii")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # A new file under another name, then renamed: a start that dies here leaves no short secret.
        fresh = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        with open(os.open(fresh, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb") as out:
            out.write(secret + b"\n")
        os.replace(fresh, path)
    except OSError as exc:
        log.warning(
            "%s cannot be written (%s): web sessions end when the app restarts",
            path, exc.strerror or type(exc).__name__,
        )
    return secret


def session_key(password_hash: str, secret: bytes) -> bytes:
    return hmac.new(secret, b"vettarr web session v1\0" + password_hash.encode("utf-8"), hashlib.sha256).digest()


def sign_session(key: bytes, expires: int, remember: bool) -> str:
    body = f"{_VERSION}.{expires}.{int(remember)}"
    return f"{body}.{hmac.new(key, body.encode('ascii'), hashlib.sha256).hexdigest()}"


def session_valid(key: bytes, value: str | None, now: float) -> bool:
    if not value:
        return False
    body, _, signature = value.rpartition(".")
    expected = hmac.new(key, body.encode("utf-8", "replace"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature.encode("utf-8", "replace"), expected.encode("ascii")):
        return False
    parts = body.split(".")
    if len(parts) != 3 or parts[0] != _VERSION or not parts[1].isdigit():
        return False
    return int(parts[1]) > now


# --- the lockout -----------------------------------------------------------


def address_key(host: str | None) -> str:
    """What a lock is held on: the IPv4 address, or the /64 an IPv6 address is in.

    One machine holds a whole IPv6 /64, so a lock on the single address would
    be walked around by changing it.
    """
    try:
        address = ipaddress.ip_address(host or "")
    except ValueError:
        return host or "unknown"
    if address.version == 6:
        if address.ipv4_mapped is not None:
            return str(address.ipv4_mapped)
        return str(ipaddress.ip_network((address, 64), strict=False).network_address) + "/64"
    return str(address)


class Lockout:
    """Five wrong passwords from an address lock it for fifteen minutes.

    A try is counted when it arrives (``attempt``), before the password is
    checked, so logins sent together cannot all be checked: the fifth one
    locks the address and the rest are refused unchecked. A correct password
    clears the address, the lock its own try may have set included.

    In memory: a restart forgets the locks, which costs an attacker a restart
    they cannot cause. Tries older than the window are forgotten.
    """

    def __init__(
        self, limit: int = MAX_FAILURES, seconds: float = LOCK_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.limit = limit
        self.seconds = seconds
        self._clock = clock
        self._failures: dict[str, list[float]] = {}
        self._locked_until: dict[str, float] = {}

    def locked_for(self, address: str) -> int:
        """Seconds the address stays locked; 0 when it may try."""
        until = self._locked_until.get(address)
        if until is None:
            return 0
        left = until - self._clock()
        if left <= 0:
            del self._locked_until[address]
            return 0
        return math.ceil(left)

    def attempt(self, address: str) -> int:
        """Count a try, before its password is checked. Returns the tries left; 0 means now locked."""
        now = self._clock()
        recent = [t for t in self._failures.get(address, ()) if now - t < self.seconds]
        recent.append(now)
        if len(recent) >= self.limit:
            self._failures.pop(address, None)
            self._locked_until[address] = now + self.seconds
            self._prune(now)
            return 0
        self._failures[address] = recent
        self._prune(now)
        return self.limit - len(recent)

    def clear(self, address: str) -> None:
        self._failures.pop(address, None)
        self._locked_until.pop(address, None)

    def _prune(self, now: float) -> None:
        if len(self._failures) + len(self._locked_until) <= _TRACKED_ADDRESSES:
            return
        self._locked_until = {a: t for a, t in self._locked_until.items() if t > now}
        self._failures = {
            a: ts for a, ts in self._failures.items() if ts and now - ts[-1] < self.seconds
        }
        while len(self._failures) > _TRACKED_ADDRESSES // 2:
            self._failures.pop(next(iter(self._failures)))


# --- other sites -----------------------------------------------------------


def _netloc(url: str) -> str:
    return urlsplit(url).netloc.lower()


def cross_origin(request: Request, base_url: str | None = None) -> bool:
    """True when a request that changes something was sent by another site.

    ``Sec-Fetch-Site`` decides when the browser sends it (every current one
    does, on https and on localhost): only ``same-origin`` and ``none`` (typed
    or bookmarked, never a page's doing) pass. Without it, ``Origin`` must
    name this host: the request's own ``Host``, or ``web.base_url`` for a
    proxy that rewrites ``Host``. A request with neither header is not a
    browser's, and a forged request needs a browser to carry the cookie.
    """
    if request.method in _SAFE_METHODS:
        return False
    site = request.headers.get("sec-fetch-site")
    if site:
        return site.lower() not in ("same-origin", "none")
    origin = request.headers.get("origin")
    if not origin:
        return False
    ours = {request.headers.get("host", "").lower()}
    if base_url:
        ours.add(_netloc(base_url))
    return _netloc(origin) not in ours


def safe_next(target: str | None) -> str:
    """Where to go after signing in: a path on this site, or ``/``."""
    if not target or not target.startswith("/") or target.startswith("//"):
        return "/"
    if "\\" in target or any(ord(c) < 32 for c in target):
        return "/"
    return target


# --- the gate --------------------------------------------------------------


class Gate:
    """The mode a config asks for, and what the middleware and the login routes do with it.

    Raises ``ValueError`` for a ``password_hash`` that is not a hash, so a
    bad line stops the start instead of refusing every login.
    """

    def __init__(
        self, web: Web,
        clock: Callable[[], float] = time.time,
        lockout: Lockout | None = None,
        secret_file: Path | None = None,
    ) -> None:
        self.base_url = web.base_url
        self.secure = bool(web.base_url and web.base_url.lower().startswith("https://"))
        self.clock = clock
        self.lockout = lockout or Lockout()
        self.header: str | None = None
        self._hash = ""
        self._key = b""
        self._checks = asyncio.Semaphore(_CONCURRENT_CHECKS)
        if web.auth is None:
            self.mode = NONE
        elif web.auth.trusted_header:
            self.mode = PROXY
            self.header = web.auth.trusted_header
        else:
            self.mode = PASSWORD
            self._hash = web.auth.password_hash.get_secret_value()
            passwords.parse(self._hash)
            secret = session_secret(secret_file) if secret_file else secrets.token_bytes(_SECRET_BYTES)
            self._key = session_key(self._hash, secret)

    # The login routes' side.

    async def check_password(self, password: str) -> bool:
        async with self._checks:
            return await asyncio.to_thread(passwords.verify, password, self._hash)

    def sign_in(self, response: Response, remember: bool) -> None:
        seconds = REMEMBER_SECONDS if remember else SESSION_SECONDS
        value = sign_session(self._key, int(self.clock()) + seconds, remember)
        response.set_cookie(
            COOKIE, value, max_age=REMEMBER_SECONDS if remember else None,
            path="/", secure=self.secure, httponly=True, samesite="lax",
        )

    def sign_out(self, response: Response) -> None:
        response.delete_cookie(COOKIE, path="/", secure=self.secure, httponly=True, samesite="lax")

    def signed_in(self, request: Request) -> bool:
        return session_valid(self._key, request.cookies.get(COOKIE), self.clock())

    # The middleware's side.

    def refusal(self, request: Request) -> Response | None:
        """The answer for a request that goes no further, or None to let it through."""
        if cross_origin(request, self.base_url):
            log.warning("Refused a cross-site %s %s", request.method, request.scope["path"])
            return PlainTextResponse(CROSS_ORIGIN, status_code=403)
        request.state.auth = Identity(self.mode)
        path = request.scope["path"]
        if not self.gated(path):
            return None
        if self.mode == PROXY:
            user = request.headers.get(self.header or "", "").strip()
            if not user:
                return PlainTextResponse(
                    f"This instance takes sign-ins from its reverse proxy, and this request "
                    f"did not carry the {self.header} header.",
                    status_code=401,
                )
            request.state.auth = Identity(PROXY, user)
            return None
        if path in ("/login", "/logout") or self.signed_in(request):
            return None
        return self._to_login(request)

    def gated(self, path: str) -> bool:
        """Whether a path needs a session (or the proxy's header) in this mode.

        ``/hooks/`` does not: another program calls it, with no browser and
        no session, and each route there checks a secret of its own.
        """
        return self.mode != NONE and not path.startswith(("/static/", HOOKS))

    @staticmethod
    def _to_login(request: Request) -> Response:
        if request.headers.get("HX-Request"):
            # A fragment request cannot show a login page; send the whole
            # page there, and back to where it was afterwards.
            current = urlsplit(request.headers.get("HX-Current-URL", ""))
            back = current.path + (f"?{current.query}" if current.query else "")
            return PlainTextResponse(
                SIGN_IN_FIRST, status_code=401,
                headers={"HX-Redirect": login_url(back)},
            )
        if request.method in ("GET", "HEAD"):
            query = request.scope.get("query_string", b"").decode("latin-1")
            back = request.scope["path"] + (f"?{query}" if query else "")
            return RedirectResponse(login_url(back), status_code=303)
        return PlainTextResponse(SIGN_IN_FIRST, status_code=401)


class GateMiddleware:
    def __init__(self, app: ASGIApp, gate: Gate) -> None:
        self.app = app
        self.gate = gate

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "websocket":
            await WebSocketClose(code=1008)(scope, receive, send)
            return
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        refusal = self.gate.refusal(Request(scope))
        if refusal is not None:
            await refusal(scope, receive, send)
            return
        path = scope["path"]
        if not self.gate.gated(path):
            await self.app(scope, receive, send)
            return
        # A photo may be kept and asked about again (it has an ETag); a page may not be kept at all.
        keep = "private, no-cache" if path.startswith("/photos/") else "no-store"

        async def send_private(message: Message) -> None:
            if message["type"] == "http.response.start":
                message.setdefault("headers", [])
                MutableHeaders(scope=message).setdefault("Cache-Control", keep)
            await send(message)

        await self.app(scope, receive, send_private)


def login_url(back: str | None) -> str:
    back = safe_next(back)
    return "/login" if back == "/" else f"/login?next={quote(back, safe='/')}"
