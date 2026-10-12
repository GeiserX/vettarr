"""Cooldown guard around the vendored UNIT3D client.

The guard subclasses `unit3d_client.Unit3dClient` and installs a `requests`
response hook instead of editing that file, so the vendored client stays close
to the copy it came from. Everything the guard adds to the client is named with
a `_guard_` prefix, so an upstream attribute can never silently shadow a guard
method (see the re-vendor note at the bottom of this file, and
`GuardMemberCollisionTests`).

Why it exists. The vendored `ensure_session()` re-runs the whole
check-session / login / reload-cookies dance on every search, every .torrent
download and every thank. When a tracker's saved session dies and its login
form cannot be completed unattended, a queue of N items becomes N logins, and a
tracker answering `429 Too Many Requests` gets hammered for as long as the queue
lasts. Both are a good way to lose an account.

The guard gives four guarantees, per tracker:

* once a session attempt fails, every later call is refused locally, with no
  HTTP request at all, until the cooldown expires — so one login attempt per
  cooldown window, not one per item;
* a `429` opens the cooldown and honours `Retry-After`, capped at
  `MAX_RETRY_AFTER_SECONDS` so a huge value cannot hold a tracker until a
  restart;
* any response of 400 or above reads as "not logged in", and
  `ERROR_STATUS_LIMIT` consecutive error responses open the cooldown exactly
  like a failed login — a challenge page or a 5xx has no login form in it, so
  without this the vendored text check reads it as a live session;
* repeated failures back off exponentially (15 min doubling to a 6 h ceiling),
  so a tracker that stays broken is contacted less and less often.

Recovery is not delayed by the backoff: a cookie file whose *contents* change
clears a session cooldown and drops the jar loaded from the old file, so the
next request is a session check carrying the new cookies and nothing else.
That is how cookies pasted into the settings page or dropped onto the box by
hand take effect immediately, without spending a password login that is already
known to fail. A session Prowlarr holds recovers the same way: a cookie header
that differs from the last one clears a session cooldown
(`note_new_cookie()`). A rate-limit hold is never shortened that way — the tracker asked
for a pause, and a new cookie is no answer to that. Writes made by this client
are excluded, and so is a rewrite that lands identical bytes, which is what
another tool sharing the same cookie file does on a healthy cycle.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Callable

import requests

from vettarr import events
from vettarr.searcher.unit3d_client import TorrentResult, Unit3dClient

log = logging.getLogger(__name__)

BASE_BACKOFF_SECONDS = 900.0
MAX_BACKOFF_SECONDS = 6 * 3600.0
# A `Retry-After` longer than this is treated as this. Without a ceiling a
# tracker (or a proxy in front of it) could park itself until the next restart.
MAX_RETRY_AFTER_SECONDS = MAX_BACKOFF_SECONDS

# Consecutive error responses (400 and above, other than 429) that open the
# cooldown. Three, because a single failing page must not park a healthy
# tracker — one search hitting a 404 or a 500 is noise — while a session
# recovery attempt already spends three requests (session check, login page,
# re-check), so a tracker answering 403 or 503 to everything is parked inside
# the very first attempt rather than after a queue of items.
ERROR_STATUS_LIMIT = 3

# Failure kinds. Only a SESSION failure can be cleared by a new cookie file.
SESSION_FAILURE = "session"
RATE_LIMIT_FAILURE = "rate-limit"


def parse_retry_after(value: str | None) -> float | None:
    """Return `Retry-After` as seconds from now, for both allowed forms."""
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())


class TrackerGuard:
    """Per-tracker cooldown state. Thread-safe; one instance per client."""

    def __init__(
        self,
        name: str,
        cookie_path: str = "",
        clock: Callable[[], float] = time.monotonic,
        on_cookie_refresh: Callable[[], None] | None = None,
        error_limit: int = ERROR_STATUS_LIMIT,
        base_backoff: float = BASE_BACKOFF_SECONDS,
        max_backoff: float = MAX_BACKOFF_SECONDS,
    ) -> None:
        self.name = name
        # A provider's `guard` block sets these; the module constants are the
        # defaults. `max_backoff` also caps a Retry-After.
        self.error_limit = error_limit
        self.base_backoff = base_backoff
        self.max_backoff = max_backoff
        self._cookie_path = cookie_path
        self._clock = clock
        # Called when a changed cookie file clears a session cooldown, so the
        # client can drop the jar it loaded from the old file. It runs while
        # this lock is held, so it must not call back into the guard.
        self.on_cookie_refresh = on_cookie_refresh
        # Reentrant: note_request_error() opens the cooldown through
        # note_failure() while still holding the lock.
        self._lock = threading.RLock()
        self._blocked_until = 0.0
        self._failures = 0
        self._reason = ""
        self._kind = SESSION_FAILURE
        self._error_run = 0
        self._cookie_stamp = self._cookie_digest()

    def _cookie_digest(self) -> str | None:
        """Hash of the cookie file, or None when there is no readable file.

        Contents, not mtime: another tool sharing this cookie file rewrites it
        on every healthy cycle, and a rewrite that lands the same bytes is not
        a new session.
        """
        if not self._cookie_path:
            return None
        try:
            return hashlib.sha256(Path(self._cookie_path).read_bytes()).hexdigest()
        except OSError:
            return None

    def blocked_reason(self) -> str | None:
        """Why the tracker is off-limits right now, or None if it is usable."""
        with self._lock:
            now = self._clock()
            if self._blocked_until <= now:
                return None
            if self._kind == SESSION_FAILURE and self._cookie_digest() != self._cookie_stamp:
                log.info("[%s] Cookie file changed, clearing tracker cooldown", self.name)
                self._reset_locked()
                if self.on_cookie_refresh is not None:
                    # Before anything else: the very next request has to carry
                    # the new cookies, not the jar read from the old file.
                    self.on_cookie_refresh()
                return None
            return f"{self._reason}; no requests for another {int(self._blocked_until - now)}s"

    def blocked_seconds(self) -> float:
        with self._lock:
            return max(0.0, self._blocked_until - self._clock())

    @property
    def failures(self) -> int:
        with self._lock:
            return self._failures

    @property
    def error_run(self) -> int:
        """Consecutive error responses seen since the last good one."""
        with self._lock:
            return self._error_run

    def note_success(self) -> None:
        with self._lock:
            self._reset_locked()

    def note_request_ok(self) -> None:
        """A response below 400 arrived: the error run is over."""
        with self._lock:
            self._error_run = 0

    def note_request_error(self, status: int) -> bool:
        """Count an error response. True once it opens the cooldown.

        One bad response is not a broken tracker. `ERROR_STATUS_LIMIT` of them
        in a row, with no good response in between, is — and it is parked with
        the same backoff as a failed login.
        """
        with self._lock:
            self._error_run += 1
            if self._error_run < self.error_limit:
                return False
            # A run of 5xx answers is the tracker's own trouble: the cooldown
            # is the same, but nobody is told to replace a cookie that is fine.
            self.note_failure(
                f"{self._error_run} consecutive request errors (last: HTTP {status})",
                session_lost=status < 500,
            )
            return True

    def note_failure(
        self,
        reason: str,
        retry_after: float | None = None,
        kind: str = SESSION_FAILURE,
        session_lost: bool = True,
    ) -> None:
        """Open the cooldown. A no-op while one is already open.

        ``session_lost=False`` opens the same cooldown without the
        ``cookie_expired`` event, for a failure that is not the session's.

        Staying a no-op is what keeps a single failure from being counted twice
        when it is seen both by the response hook and by the calling method. A
        `Retry-After` arriving during an open cooldown still extends it, and a
        rate limit arriving during a session cooldown upgrades the hold so a new
        cookie file cannot cut it short.
        """
        if retry_after is not None:
            retry_after = min(retry_after, self.max_backoff)
        with self._lock:
            now = self._clock()
            if self._blocked_until > now:
                if kind == RATE_LIMIT_FAILURE and self._kind != RATE_LIMIT_FAILURE:
                    self._kind = RATE_LIMIT_FAILURE
                    self._reason = reason
                if retry_after is not None:
                    self._blocked_until = max(self._blocked_until, now + retry_after)
                return
            self._failures += 1
            backoff = min(
                self.base_backoff * (2 ** (self._failures - 1)),
                self.max_backoff,
            )
            if retry_after is not None:
                backoff = max(backoff, retry_after)
            self._blocked_until = now + backoff
            self._reason = reason
            self._kind = kind
            self._cookie_stamp = self._cookie_digest()
            log.warning(
                "[%s] Tracker unavailable (%s), consecutive failure %d, "
                "sending nothing for %ds",
                self.name, reason, self._failures, int(backoff),
            )
            events.emit(events.GUARD, provider=self.name, state="cooldown", reason=reason,
                        cause=kind, seconds=int(backoff))
            if kind == SESSION_FAILURE and session_lost:
                events.emit(events.COOKIE_EXPIRED, provider=self.name, reason=reason)

    def note_new_cookie(self) -> None:
        """A different cookie arrived from outside the cookie file: lift a session cooldown.

        For a session Prowlarr holds. Logging in again there is the operator's
        recovery, exactly as dropping a new cookie file is, and there is no
        file whose contents could say so. A rate-limit hold stays.
        """
        with self._lock:
            if self._blocked_until <= self._clock() or self._kind != SESSION_FAILURE:
                return
            log.info("[%s] New session cookie, clearing tracker cooldown", self.name)
            self._reset_locked()

    def note_cookie_file_rewritten(self) -> None:
        """Re-stamp after the client rewrites the cookie file itself.

        Without this, the client's own `_save_cookies()` would look like an
        operator dropping in fresh cookies and would clear the cooldown.
        """
        with self._lock:
            self._cookie_stamp = self._cookie_digest()

    def _reset_locked(self) -> None:
        if self._failures:
            events.emit(events.GUARD, provider=self.name, state="ok")
        self._blocked_until = 0.0
        self._failures = 0
        self._reason = ""
        self._kind = SESSION_FAILURE
        self._error_run = 0
        self._cookie_stamp = self._cookie_digest()


class GuardedUnit3dClient(Unit3dClient):
    """Vendored UNIT3D client plus the cooldown guard.

    Every network entry point of the vendored client goes through
    `ensure_session()` or `check_session()`, so overriding those two plus
    `login()` and `search()` covers search, download, thank and keep-alive.
    `_is_logged_in()` is overridden as well, because that is the one place the
    vendored client asks "am I still signed in?" and it asks it of the response
    body alone.

    Names the guard adds to the client all start with `_guard`, so a future
    vendored client cannot shadow one of them with an attribute of its own.
    """

    def __init__(self, *args, guard: TrackerGuard | None = None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._guard = guard or TrackerGuard(self.name, self._cookie_path)
        self._guard.on_cookie_refresh = self._guard_drop_loaded_cookies
        # Status of the most recent response, or None when the current attempt
        # has not had one yet. The vendored client serialises its public entry
        # points on its own RLock, so this is the response of the call in hand.
        self._guard_last_status: int | None = None
        self._guard_arm_hook()

    def _guard_arm_hook(self) -> None:
        """Make sure the response hook is on the current session object."""
        hooks = self.session.hooks.setdefault("response", [])
        if self._guard_on_response not in hooks:
            hooks.append(self._guard_on_response)

    def _guard_on_response(self, response: requests.Response, *args, **kwargs):
        status = response.status_code
        self._guard_last_status = status
        if status == 429:
            self._guard.note_failure(
                "rate limited (HTTP 429)",
                retry_after=parse_retry_after(response.headers.get("Retry-After")),
                kind=RATE_LIMIT_FAILURE,
            )
        elif status >= 400:
            self._guard.note_request_error(status)
        else:
            self._guard.note_request_ok()
        return response

    def _guard_drop_loaded_cookies(self) -> None:
        """Make the next request re-read the cookie file.

        The guard clears a session cooldown when the cookie file's *contents*
        change, but the jar in memory is still the one loaded from the old
        file: the vendored `_ensure_cookies()` returns early for as long as
        `_cookies_loaded` is True. Clearing the flag is what turns "the
        cooldown is over" into "the next request carries the new cookies".

        Without it, recovery costs a password login and another backoff
        window: the session check goes out with the dead cookies, fails, the
        vendored client falls through to `login()`, that login is the one
        already known not to work unattended, the guard records it and parks
        the tracker again — and the vendored reload-and-recheck that would
        have saved it is refused by that fresh cooldown.
        """
        self._cookies_loaded = False

    def _save_cookies(self) -> None:
        super()._save_cookies()
        self._guard.note_cookie_file_rewritten()

    def use_cookie_header(self, header: str | None) -> None:
        # Prowlarr hands over the same header until somebody logs in again
        # there. A different one is a new session, and the reason to stop
        # refusing a tracker whose old session had died.
        changed = bool(header) and header != self._cookie_header
        super().use_cookie_header(header)
        if changed:
            self._guard.note_new_cookie()

    def _guard_blocked_reason(self) -> str | None:
        self._guard_arm_hook()
        return self._guard.blocked_reason()

    def _guard_saw_error_status(self) -> bool:
        """True when the last response of this attempt was an error status.

        It decides who records the failure. An attempt that ended on an error
        status is handed to the consecutive-error run, so one 404 or 500 does
        not park a tracker that is otherwise answering. An attempt that ended on
        a login page, or that never got a response at all, is a session failure
        and is parked at once.
        """
        return self._guard_last_status is not None and self._guard_last_status >= 400

    def _is_logged_in(self, html: str) -> bool:
        # A challenge page, a 403 and a 5xx carry neither the login form nor the
        # torrent list, so the vendored text check reads them as a live session.
        if self._guard_saw_error_status():
            return False
        return super()._is_logged_in(html)

    def ensure_session(self) -> bool:
        reason = self._guard_blocked_reason()
        if reason:
            # Returning here and not from the calls below is what keeps a
            # refused attempt from tearing down the loaded cookie jar and
            # re-reading the cookie file, over and over, for nothing.
            log.info("[%s] Skipping tracker request: %s", self.name, reason)
            return False
        self._guard_last_status = None
        ok = super().ensure_session()
        if ok:
            self._guard.note_success()
        elif self._guard_saw_error_status():
            log.warning(
                "[%s] Session attempt ended on HTTP %d (error run: %d)",
                self.name, self._guard_last_status, self._guard.error_run,
            )
        else:
            self._guard.note_failure("session could not be established")
        return ok

    def check_session(self) -> bool:
        if self._guard_blocked_reason():
            return False
        self._guard_last_status = None
        ok = super().check_session()
        # If the hook fired during the call, whatever came back was not a live
        # session however much it looked like one.
        return ok and not self._guard.blocked_reason()

    def login(self) -> bool:
        reason = self._guard_blocked_reason()
        if reason:
            log.warning("[%s] Skipping login: %s", self.name, reason)
            return False
        if not self._username or not self._password:
            # The vendored login returns False here without any request, and a
            # cookie-only tracker must not be penalised for that.
            return super().login()
        self._guard_last_status = None
        ok = super().login()
        if ok:
            self._guard.note_success()
        elif not self._guard_saw_error_status():
            self._guard.note_failure("login rejected")
        return ok

    def search(
        self,
        query: str,
        imdb_id: str | None = None,
        tmdb_id: int | None = None,
    ) -> list[TorrentResult]:
        results = super().search(query, imdb_id=imdb_id, tmdb_id=tmdb_id)
        if not results:
            # The vendored search reports every failure as "authentication
            # failed". Say what actually happened, so a rate limit is not read
            # as stale cookies.
            reason = self._guard.blocked_reason()
            if reason:
                self.last_search_error = reason
        return results


# Re-vendor note
# --------------
# `unit3d_client.py` came from an older companion tool, but the two copies have drifted
# and neither is a superset of the other. Refreshing this one is a merge, not a
# copy. What this copy has that the other does not, and must keep:
#
#   * `_synchronized` and the `threading.RLock` built in `__init__`. This service
#     searches from a thread pool; the other one runs a single poll loop and has
#     no lock. Dropping the lock would let two workers interleave inside
#     `ensure_session()` and, through it, inside this guard's per-attempt state.
#   * `search_with_status()`, the only way `search_service` learns *why* a search
#     came back empty. Nothing upstream calls it, and upstream does not have it.
#   * `keep_alive()`. Upstream deleted it; `scheduler.py` calls it on a timer to
#     hold the session open between items. A re-vendor that drops it takes the
#     scheduler's `AttributeError` with it — and the scheduler logs that and
#     carries on, so the sessions would just quietly stop being refreshed.
#     `VendoredContractTests` fails if either method goes missing.
#   * `_is_logged_in()`. This copy looks for one configurable logged-out marker
#     (default `name="password"`, the login form's field); upstream matches six
#     languages and a login-form shape. Upstream's is stricter and a fine thing
#     to adopt, but it is a behaviour change, not a refresh: an ordinary torrent
#     page whose text happens to contain "entrar" would start reading as
#     logged out.
#
# What upstream now has that has to be reconciled, not copied blind:
#
#   * upstream gates every request itself: `begin_cycle()`, a `blocked_reason`
#     *property*, `_block()`, `_blocked`, `_blocked_until`, `_failed_cycles`,
#     `_skip_cycles`, a `_request()` choke point that paces calls and raises
#     `TrackerUnavailable`, and a `_request_delay`. That gate is built around a
#     poll cycle: `_blocked` is only ever cleared by `begin_cycle()`.
#   * this service has no cycle. Items arrive one at a time from a chat, so
#     nothing would call `begin_cycle()` and the first `_block()` would park the
#     tracker until the process restarted.
#
# So `TrackerGuard` wins here: it is wall-clock based, it needs no cycle, and it
# is what the tests in `tests/test_tracker_guard.py` pin. If upstream's client is
# vendored in, either call `begin_cycle()` once at the start of each triage pass
# or leave upstream's gate permanently open, and keep the cooldown decision in
# this file rather than running two backoffs that cannot see each other.
#
# Also re-check this module if the refreshed client:
#   * builds a new `requests.Session` outside `__init__` (the hook would be lost
#     — `_guard_arm_hook()` re-attaches it on every guarded entry point, so this
#     is covered, but the test that counts requests is what proves it);
#   * renames or stops routing through `ensure_session`, `check_session`,
#     `login`, `search` or `_is_logged_in`, or drops `search_with_status` or
#     `keep_alive`, which `search_service` and `scheduler` call by name;
#   * renames the `_cookie_path`, `_username` or `_password` attributes, or the
#     `_cookies_loaded` flag, which is how a cookie refresh forces the jar to be
#     re-read from disk;
#   * sets an attribute in `__init__` whose name starts with `_guard`.
#     `GuardMemberCollisionTests` fails on that: upstream's `self._blocked = None`
#     would otherwise shadow a method here and every guarded call would die with
#     "TypeError: 'NoneType' object is not callable".
