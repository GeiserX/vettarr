"""What the Inbox, Review and the item page show of an item, and how it is read.

The three pages show the same item three ways: a row, a card and the whole
page. What they must agree on is here: which statuses a tab holds and in what
order, what a status and a verdict are called, which buttons an item gets,
and the queries. Everything above ``# --- reading`` is pure, so a test builds
a ``Card`` or a ``Detail`` from rows it made itself.

A button shown here is an offer, not a permission: ``vettarr.actions``
decides, and answers 409 when the item has moved since the page was drawn.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html import escape
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

from fastapi import Request
from fastapi.responses import HTMLResponse
from sqlalchemy import Select, and_, func, or_, select

from vettarr import actions, delivery, dryrun, events, ingest
from vettarr.checker import policy
from vettarr.database import async_session
from vettarr.delivery import seerr as seerr_api
from vettarr.searcher import search_service
from vettarr.models import (
    DELETABLE_ON_REJECT,
    REJECTABLE_STATUSES,
    REVIEWABLE_STATUSES,
    AutoDecision,
    ContentCheck,
    Download,
    DownloadStatus,
    Event,
    ItemStatus,
    MediaItem,
    MediaType,
    TorrentSearch,
)
from vettarr.web import ui, words

PER_PAGE = 25
_S = ItemStatus

# --- tabs and filters ---------------------------------------------------------


@dataclass(frozen=True)
class Tab:
    key: str
    label: str
    #: None is every item.
    statuses: frozenset[ItemStatus] | None
    #: A queue is worked oldest first, like the bot's. What is finished reads newest first.
    oldest_first: bool
    empty: str


NEEDS_YOU = frozenset({_S.PENDING_REVIEW, _S.PARSE_FAILED, _S.ERROR})
TABS = (
    Tab("needs", "Needs you", NEEDS_YOU, True, "Nothing needs you."),
    Tab("waiting", "Waiting for data", frozenset({_S.AWAITING_DATA}), True, "Nothing is waiting for data."),
    Tab("no-release", "No release", frozenset({_S.NO_MATCH}), True, "No search came back empty."),
    Tab(
        "done", "Decided",
        frozenset({_S.APPROVED, _S.SEARCHING, _S.DOWNLOADING, _S.COMPLETED, _S.REJECTED}),
        False, "Nothing is decided yet.",
    ),
    Tab("all", "All", None, False, "No items yet."),
)
_TABS = {tab.key: tab for tab in TABS}


def tab_of(key: str | None) -> Tab:
    return _TABS.get(key or "", TABS[0])


@dataclass(frozen=True)
class Filters:
    """The Inbox's three filters. A value nothing matches is dropped, never an error."""

    type: str = ""
    source: str = ""
    outcome: str = ""

    @classmethod
    def read(cls, params: Mapping[str, Any]) -> Filters:
        def one(name: str) -> str:
            value = params.get(name) or ""
            if isinstance(value, (list, tuple)):
                value = value[0] if value else ""
            return str(value).strip()[:100]

        kind, outcome = one("type"), one("outcome")
        return cls(
            type=kind if kind in {t.value for t in MediaType} else "",
            source=one("source"),
            outcome=outcome if outcome in policy.OUTCOMES else "",
        )

    def clauses(self) -> list[Any]:
        out = []
        if self.type:
            out.append(MediaItem.media_type == MediaType(self.type))
        if self.source:
            out.append(MediaItem.source_key == self.source)
        if self.outcome:
            out.append(outcome_clause(self.outcome))
        return out

    def query(self, **more: Any) -> str:
        """The query string of a link that keeps these filters, with ``more`` on top."""
        values = {"type": self.type, "source": self.source, "outcome": self.outcome, **more}
        return urlencode({name: value for name, value in values.items() if value not in ("", None)})

    @property
    def any(self) -> bool:
        return bool(self.type or self.source or self.outcome)


# The 0.11 column, for a row decided before the policy engine wrote its own.
_LEGACY_OUTCOME = {"auto_approve": policy.APPROVE, "needs_review": policy.REVIEW, "wait": policy.WAIT}


def outcome_clause(outcome: str) -> Any:
    """Items whose verdict is ``outcome``, as ``Card.outcome`` reads it.

    A row decided before the policy engine has no ``policy_outcome``, and its
    verdict is its 0.11 ``auto_decision``. The chip shows that verdict, so
    the filter that names the same word has to find it.
    """
    current = MediaItem.policy_outcome == outcome
    legacy = [AutoDecision(old) for old, new in _LEGACY_OUTCOME.items() if new == outcome]
    if not legacy:
        return current
    return or_(current, and_(MediaItem.policy_outcome.is_(None), MediaItem.auto_decision.in_(legacy)))


def where(request: Request) -> tuple[Tab, Filters]:
    """The tab and filters of the page an htmx request came from."""
    params = parse_qs(urlsplit(request.headers.get("HX-Current-URL", "")).query)
    return tab_of((params.get("tab") or [""])[0]), Filters.read(params)


def items_query(tab: Tab, filters: Filters) -> Select:
    """A tab's items under the filters, in the tab's order."""
    query = select(MediaItem)
    if tab.statuses is not None:
        query = query.where(MediaItem.status.in_(sorted(tab.statuses, key=lambda s: s.value)))
    for clause in filters.clauses():
        query = query.where(clause)
    if tab.oldest_first:
        return query.order_by(MediaItem.created_at.asc(), MediaItem.id.asc())
    return query.order_by(MediaItem.created_at.desc(), MediaItem.id.desc())


def counts_query(filters: Filters) -> Select:
    query = select(MediaItem.status, func.count()).group_by(MediaItem.status)
    for clause in filters.clauses():
        query = query.where(clause)
    return query


def tab_counts(by_status: Mapping[ItemStatus, int]) -> dict[str, int]:
    return {
        tab.key: sum(n for status, n in by_status.items() if tab.statuses is None or status in tab.statuses)
        for tab in TABS
    }


# --- one item, as every page shows it ------------------------------------------

# label, the ContentCheck column, the signal the policy read, IMDb's category id
GUIDE = (
    ("Nudity", "sex_rating", "imdb.nudity", "NUDITY"),
    ("Violence", "violence_rating", "imdb.violence", "VIOLENCE"),
    ("Profanity", "profanity_rating", "imdb.profanity", "PROFANITY"),
    ("Frightening", "frightening_rating", "imdb.frightening", "FRIGHTENING"),
    ("Alcohol", "alcohol_rating", "imdb.alcohol", "ALCOHOL"),
)
SEVERITIES = ("none", "mild", "moderate", "severe")

# What a status is called, and its badge: ok, warn, flag or muted.
_STATUS = {
    _S.PENDING_REVIEW: ("Needs you", "warn"),
    _S.PARSE_FAILED: ("Could not be read", "flag"),
    _S.ERROR: ("Error", "flag"),
    _S.AWAITING_DATA: ("Waiting for data", "muted"),
    _S.NO_MATCH: ("No release", "warn"),
    _S.APPROVED: ("Approved, queued", "ok"),
    _S.SEARCHING: ("Searching", "ok"),
    _S.DOWNLOADING: ("Downloading", "ok"),
    _S.COMPLETED: ("Downloaded", "ok"),
    _S.REJECTED: ("Rejected", "flag"),
    _S.IGNORED: ("Not media", "muted"),
    _S.CHECKING: ("Being checked", "muted"),
}
_VERDICT = {
    policy.APPROVE: ("Policy: approve", "ok"),
    policy.REVIEW: ("Policy: review", "warn"),
    policy.REJECT: ("Policy: reject", "flag"),
    policy.WAIT: ("Policy: wait", "muted"),
}
_FINDINGS = frozenset({policy.RULE, policy.DUPLICATE, policy.IDENTITY})

# What a rejection does to the post, in the words the bot asks with.
REJECT_POST = {
    "delete": "Its post will be deleted.",
    "react": "Its post will get a reaction.",
    "none": "Its post stays where it is.",
}
REJECT_OWN_DOWNLOAD = "The download this app started is removed with its files."
REJECT_OTHER_DOWNLOAD = "Its torrent was in the download client before this app added it, and stays there."
# A Seerr request has no post: rejecting it declines the request, while it is still pending.
REJECT_SEERR_REQUEST = "Its Seerr request is declined."
REJECT_SEERR_APPROVED = "Its Seerr request is already approved, and stays as it is in Seerr."
REJECT_BULK_POSTS = {
    "delete": "Every post will be deleted.",
    "react": "Every post will get a reaction.",
    "none": "Every post stays where it is.",
}
REJECT_BULK_MIXED = "Each post is deleted, gets a reaction or stays, as its source's on_reject says."


def _value(enum_or_none: Any) -> Any:
    return getattr(enum_or_none, "value", enum_or_none)


def _utc(moment: Any) -> datetime | None:
    if not isinstance(moment, datetime):
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def ago(moment: Any, now: datetime | None = None) -> str:
    """``3 h ago``. Empty for no date."""
    at = _utc(moment)
    if at is None:
        return ""
    seconds = max(0, int(((now or datetime.now(timezone.utc)) - at).total_seconds()))
    for size, unit in ((86400, "d"), (3600, "h"), (60, "min")):
        if seconds >= size:
            return f"{seconds // size} {unit} ago"
    return "just now"


def stamp(moment: Any) -> str:
    at = _utc(moment)
    if at is None:
        return ""
    at = at.astimezone(timezone.utc)
    return f"{at.day} {at:%b %Y, %H:%M} UTC"


@dataclass(frozen=True)
class ReasonLine:
    #: The rule's id when a rule made it, otherwise what kind of reason it is.
    tag: str
    #: Set when the reason is about a rule without being its hit (``missing``).
    note: str
    text: str
    finding: bool


@dataclass
class Card:
    item: MediaItem
    check: ContentCheck | None = None
    #: The latest stored search: ``search_id``, ``found``, and its best release's
    #: ``title``, ``size`` and ``seeders`` when it found one.
    search: dict[str, Any] | None = None
    download: Download | None = None
    #: "torrent file" or "magnet" when approving adds what the post carried.
    direct_add: str | None = None
    #: What its source's ``on_reject`` does to the post: delete, react or none.
    on_reject: str = "none"
    #: False when ``deliver`` sends the type to Radarr, Sonarr or Seerr, or
    #: nowhere: those pick their own release, so none is grabbed from here.
    to_client: bool = True
    #: False when nothing is set up to search with: no Prowlarr, and no
    #: provider with a search of its own.
    searchable: bool = True

    @property
    def id(self) -> int:
        return self.item.id

    @property
    def title(self) -> str:
        return self.item.title or "Untitled post"

    @property
    def media_type(self) -> str:
        return str(_value(self.item.media_type) or "unknown")

    @property
    def poster(self) -> str | None:
        if self.check is not None and self.check.poster_url:
            return self.check.poster_url
        if self.item.telegram_photo_path:
            return f"/photos/{self.item.telegram_photo_path}"
        return None

    @property
    def age(self) -> str:
        return ago(self.item.posted_at or self.item.created_at)

    @property
    def rejected_by_policy(self) -> bool:
        item = self.item
        return item.status is _S.REJECTED and item.reviewed_at is None and item.policy_outcome == policy.REJECT

    @property
    def seerr_request(self) -> bool:
        """The item is a request a Seerr user made: Seerr delivers it, and has no post for it."""
        return seerr_api.request_of(self.item.origin_key) is not None

    @property
    def progress(self) -> int | None:
        """The download's progress in percent, while there is a download."""
        if self.download is None:
            return None
        return max(0, min(100, round(float(self.download.progress or 0.0) * 100)))

    @property
    def downloading(self) -> bool:
        live = (DownloadStatus.PENDING, DownloadStatus.DOWNLOADING)
        return self.item.status is _S.DOWNLOADING and self.download is not None and self.download.status in live

    @property
    def status(self) -> tuple[str, str]:
        label, tone = _STATUS.get(self.item.status, ("Being read", "muted"))
        if self.item.status is _S.REJECTED:
            label = "Rejected by the policy" if self.rejected_by_policy else "Rejected by a person"
        elif self.downloading:
            label = f"Downloading {self.progress}%"
        return label, tone

    @property
    def failed(self) -> str | None:
        """What went wrong, for an item that stopped on an error or could not be read.

        Never an action's claim marker (``operation:...``): that is how the
        worker tells who holds an item, not a sentence.
        """
        message = str(self.item.error_message or "")
        if self.item.status in (_S.ERROR, _S.PARSE_FAILED) and message and not message.startswith("operation:"):
            return message
        return None

    @property
    def outcome(self) -> str | None:
        """What the policy decided, which under shadow mode is not what was done."""
        if self.item.policy_outcome in policy.OUTCOMES:
            return self.item.policy_outcome
        return _LEGACY_OUTCOME.get(str(_value(self.item.auto_decision) or ""))

    @property
    def verdict(self) -> tuple[str, str] | None:
        return _VERDICT.get(self.outcome or "")

    @property
    def reasons(self) -> list[ReasonLine]:
        """The decision's reasons in words. Shadow mode's line goes last: it says
        what the settings did, and the rest says what the policy found."""
        lines = []
        for reason in policy.legacy_reasons(self.item.reasons, self.item.auto_reasons):
            if not reason.text:
                continue
            text = words.reason(reason.text)
            note = reason.kind if reason.rule and reason.kind != policy.RULE else ""
            lines.append(ReasonLine(
                tag=reason.rule or reason.kind,
                # "Waiting for IMDb's parents guide" already says it is missing.
                note="" if text.startswith("Waiting for") else note,
                text=text,
                finding=reason.kind in _FINDINGS,
            ))
        return sorted(lines, key=lambda line: line.tag == policy.SHADOW)

    @property
    def row_reasons(self) -> list[ReasonLine]:
        """The reasons a row counts: one line per thing said, however many rules said it."""
        return list({line.text: line for line in reversed(self.reasons)}.values())[::-1]

    @property
    def lead(self) -> ReasonLine | None:
        """The one reason a row has room for: what was found, before what was merely noted."""
        reasons = self.reasons
        return next((r for r in reasons if r.finding), reasons[0] if reasons else None)

    @property
    def pills(self) -> list[tuple[str, str]]:
        """(category, severity) for every category the parents guide rated."""
        if self.check is None:
            return []
        rated = ((label, _value(getattr(self.check, column, None))) for label, column, _, _ in GUIDE)
        return [(label, severity) for label, severity in rated if severity]

    @property
    def row_pills(self) -> list[tuple[str, str]]:
        """What a row has room for: the categories rated above none, or one pill saying none is."""
        pills = self.pills
        flagged = [(label, severity) for label, severity in pills if severity != "none"]
        if flagged or not pills:
            return flagged
        return [("Parents guide", "none")]

    @property
    def guide_expected(self) -> bool:
        return self.media_type in (MediaType.MOVIE.value, MediaType.SERIES.value) and self.check is not None

    @property
    def best(self) -> dict[str, Any] | None:
        return self.search if self.search and self.search.get("title") else None

    # What the item offers. `actions` holds the same conditions and is the judge.

    @property
    def can_approve(self) -> bool:
        # A post with no title has nothing to fetch, unless it carries its own torrent.
        has_what = bool(self.item.title) or bool(self.direct_add)
        return has_what and (self.item.status in REVIEWABLE_STATUSES or self.rejected_by_policy)

    @property
    def approve_label(self) -> str:
        if self.rejected_by_policy:
            return "Approve anyway"
        # The policy already let it through, and the way out failed: approving tries it again.
        return "Retry" if self.item.status is _S.ERROR else "Approve"

    @property
    def unreadable(self) -> bool:
        """A post no format could read: the way on is teaching a format, or dismissing it."""
        return self.item.status is _S.PARSE_FAILED

    @property
    def reject_label(self) -> str:
        return "Dismiss" if self.unreadable else "Reject"

    @property
    def teach_href(self) -> str:
        """Setup's "Try a post", with this post in it."""
        return f"/setup?try={self.id}#try-a-post"

    @property
    def final(self) -> str:
        """For an item nothing more is done with, a line saying so; empty otherwise."""
        status = self.item.status
        if status is _S.REJECTED and not self.rejected_by_policy:
            return "Rejected by a person. Nothing more to do."
        if status is _S.COMPLETED:
            return "Downloaded. Nothing more to do."
        if status is _S.IGNORED:
            return "Not media, so it was never reviewed. Nothing more to do."
        return ""

    @property
    def can_reject(self) -> bool:
        return self.item.status in REJECTABLE_STATUSES

    @property
    def post_effect(self) -> str:
        """delete, react or none: what rejecting does to the post. A post nobody could read is kept."""
        return self.on_reject if self.item.status in DELETABLE_ON_REJECT and self.on_reject in REJECT_POST else "none"

    @property
    def reject_confirm(self) -> str:
        """What rejecting this item deletes, for the dialog that asks first."""
        if self.seerr_request:
            return REJECT_SEERR_REQUEST if self.download is None else REJECT_SEERR_APPROVED
        said = [REJECT_POST[self.post_effect]]
        if self.download is not None:
            target = self.download.delivery_target
            if target in delivery.REMOTE:
                said.append(f"What this app added to {delivery.LABELS[target]} for it is taken back.")
            else:
                said.append(REJECT_OWN_DOWNLOAD if self.download.created_torrent else REJECT_OTHER_DOWNLOAD)
        return " ".join(said)

    @property
    def can_search(self) -> bool:
        return self.searchable and self.item.status in REVIEWABLE_STATUSES and bool(self.item.title)

    @property
    def can_grab(self) -> bool:
        return self.to_client and self.item.status in REVIEWABLE_STATUSES

    @property
    def can_grab_best(self) -> bool:
        """Review's Grab takes the latest search's best release, so it needs one."""
        return self.can_grab and self.best is not None

    @property
    def can_rerun(self) -> bool:
        if self.item.status in (_S.PENDING_REVIEW, _S.AWAITING_DATA):
            return True
        # The policy's rejection of a Seerr request declined it there, for good.
        return self.rejected_by_policy and not self.seerr_request

    @property
    def selectable(self) -> bool:
        return self.can_approve or self.can_reject


# --- the whole item ------------------------------------------------------------


@dataclass(frozen=True)
class GuideBar:
    label: str
    severity: str | None
    votes: int | None
    #: (severity, votes, percent of the bar), mildest first; empty without a stored breakdown.
    segments: tuple[tuple[str, int, float], ...] = ()
    #: Why there is no rating, in the policy's words.
    note: str = ""

    @property
    def spoken(self) -> str:
        counts = ", ".join(f"{name} {count}" for name, count, _ in self.segments)
        return f"{self.label} votes: {counts}"


def guide_bars(item: MediaItem, check: ContentCheck | None) -> list[GuideBar]:
    """One bar per IMDb category, from the snapshot the decision read.

    The snapshot holds every label's votes. A row decided before snapshots
    has only the rating and the total, and gets a bar with no segments.
    """
    signals = item.signals if isinstance(item.signals, Mapping) else {}
    bars = []
    for label, column, signal_name, category in GUIDE:
        severity = _value(getattr(check, column, None)) if check is not None else None
        votes = ((check.guide_votes or {}).get(category) if check is not None else None)
        breakdown: Mapping[str, Any] = {}
        note = ""
        signal = signals.get(signal_name)
        if isinstance(signal, Mapping):
            if "severity" in signal:
                severity = signal.get("severity") or severity
                votes = signal.get("votes", votes)
                breakdown = signal.get("breakdown") or {}
            elif signal.get("missing"):
                note = str(signal["missing"])
            elif signal.get("unavailable"):
                note = f"{signal['unavailable']} is switched off"
        counts = [(name, int(breakdown.get(name) or 0)) for name in SEVERITIES]
        total = sum(count for _, count in counts)
        segments = tuple((name, count, round(100 * count / total, 1)) for name, count in counts) if total else ()
        if severity or segments or note:
            bars.append(GuideBar(label, severity, int(votes) if votes is not None else None, segments, note))
    return bars


@dataclass(frozen=True)
class Moment:
    at: datetime
    text: str

    @property
    def when(self) -> str:
        return stamp(self.at)


_DECIDED = {
    policy.APPROVE: "to approve it",
    policy.REVIEW: "to hold it for a person",
    policy.REJECT: "to reject it",
    policy.WAIT: "to wait for data",
}
_WOULD_HAVE = {policy.APPROVE: "approved it", policy.REJECT: "rejected it", policy.WAIT: "waited for data"}


def dryrun_line(payload: Mapping[str, Any]) -> str:
    """A ``would_have`` a dry run wrote, as one line: what was not sent, and where."""
    target = str(payload.get("target") or "the outside world")
    if payload.get("reason") == dryrun.NOT_FETCHED:
        return f"Dry run: torrent not fetched from {target}"
    return f"Dry run: {target}: {payload.get('action') or 'a write'}, not sent"


def describe(kind: str, payload: Mapping[str, Any] | None) -> str:
    """An event as one line of an item's history."""
    payload = payload or {}
    why = "; ".join(words.reason(str(r)) for r in payload.get("reasons") or [])
    outcome = payload.get("outcome")
    if kind == events.DECISION:
        # What was done with the item. Under shadow mode that is not what the
        # policy decided, which the would-have line beside it says.
        text = f"The policy decided {_DECIDED.get(payload.get('done') or outcome, 'on it')}"
        return f"{text}: {why}" if why else text
    if kind == events.WOULD_HAVE and dryrun.is_dry_run_event(payload):
        return dryrun_line(payload)
    if kind == events.WOULD_HAVE:
        return f"Shadow mode: the policy would have {_WOULD_HAVE.get(outcome, 'decided it')}, and left it to a person"
    if kind == events.NEEDS_YOU:
        return "Sent to the review queue"
    if kind == events.BOT_ADDED:
        return "Sent to the bot"
    return kind.replace("_", " ").capitalize()


def history(item: MediaItem, rows: Iterable[Any], download: Download | None) -> list[Moment]:
    """The item's events, between the dates the item itself records. Oldest first."""
    moments: list[tuple[datetime | None, str]] = [
        (_utc(item.posted_at), f"Posted in {item.source_key}" if item.source_key else "Posted"),
        (_utc(item.created_at), "Read"),
    ]
    moments += [(_utc(row.at), describe(row.kind, row.payload)) for row in rows]
    # An approval the policy made is stamped with the decision's own time.
    reviewed, decided = _utc(item.reviewed_at), _utc(item.auto_decided_at)
    if reviewed is not None and reviewed != decided:
        moments.append((reviewed, "Rejected by a person" if item.status is _S.REJECTED else "Approved by a person"))
    if download is not None:
        moments.append((_utc(download.started_at), "Download started"))
        moments.append((_utc(download.completed_at), "Download finished"))
    return [Moment(at, text) for at, text in sorted((m for m in moments if m[0] is not None), key=lambda m: m[0])]


def post_link(item: MediaItem, source: Any | None) -> str | None:
    """A link that opens the post in Telegram, for a chat that has one.

    A public chat is linked by its @username, a group or channel by its id.
    A private chat with the bot has no address to link to.
    """
    chat = getattr(source, "chat", None)
    if isinstance(chat, str) and chat.startswith("@") and len(chat) > 1:
        return f"https://t.me/{chat[1:]}/{item.telegram_message_id}"
    internal = str(item.telegram_chat_id)
    if internal.startswith("-100") and len(internal) > 4:
        return f"https://t.me/c/{internal[4:]}/{item.telegram_message_id}"
    return None


@dataclass
class Detail:
    card: Card
    #: Every stored search, newest first, each with every release it stored.
    searches: list[TorrentSearch] = field(default_factory=list)
    guide: list[GuideBar] = field(default_factory=list)
    history: list[Moment] = field(default_factory=list)
    post_link: str | None = None
    #: Where the item went, or where approving sends it; None for the download client.
    delivery: str | None = None

    @property
    def read(self) -> list[tuple[str, str]]:
        return signals_read(self.card.item.signals)

    @property
    def certifications(self) -> list[tuple[str, str]]:
        check = self.card.check
        stored = (check.certifications if check is not None else None) or {}
        return sorted((str(country), str(rating)) for country, rating in stored.items() if rating)

    @property
    def links(self) -> list[str]:
        """The links the post carried, as links only when they are web addresses."""
        return [str(link) for link in self.card.item.external_links or [] if str(link).startswith(("http://", "https://"))]

    @property
    def decided(self) -> str:
        return stamp(self.card.item.auto_decided_at)


def _said(value: Any) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (list, tuple)):
        return ", ".join(_said(v) for v in value) or "none"
    if isinstance(value, Mapping):
        return ", ".join(f"{name} {_said(v)}" for name, v in value.items()) or "none"
    return "none" if value is None else str(value)


def signals_read(signals: Any) -> list[tuple[str, str]]:
    """The stored snapshot as (signal, what it said): everything the decision read.

    A signal that had no value says why in words, so a reviewer can tell a
    rule that did not hit from a rule that had nothing to read.
    """
    if not isinstance(signals, Mapping):
        return []
    out = []
    for name in sorted(signals):
        value = signals[name]
        if isinstance(value, Mapping) and value.get("missing"):
            said = f"missing: {value['missing']}"
        elif isinstance(value, Mapping) and value.get("unavailable"):
            said = f"not read: {value.get('why') or str(value['unavailable']) + ' is switched off'}"
        elif isinstance(value, Mapping) and "severity" in value:
            said = f"{value.get('severity') or 'no rating'} ({value.get('votes') or 0} votes)"
        else:
            said = _said(value)
        out.append((str(name), said))
    return out


def search_summary(search: TorrentSearch | Any) -> dict[str, Any]:
    return {
        "search_id": search.id,
        "found": search.result_count or 0,
        "title": search.best_match_title,
        "size": search.best_match_size,
        "seeders": search.best_match_seeders,
    }


def detail_of(
    item: MediaItem,
    *,
    check: ContentCheck | None = None,
    searches: Sequence[TorrentSearch] = (),
    download: Download | None = None,
    rows: Iterable[Any] = (),
    source: Any | None = None,
    direct_add: str | None = None,
    to_client: bool = True,
    delivery: str | None = None,
    searchable: bool = True,
) -> Detail:
    """The whole item from its rows. ``searches`` newest first."""
    card = Card(
        item, check, search_summary(searches[0]) if searches else None, download, direct_add,
        getattr(source, "on_reject", "none"), to_client, searchable,
    )
    return Detail(
        card, list(searches), guide_bars(item, check), history(item, rows, download), post_link(item, source),
        delivery,
    )


# --- Review's order ------------------------------------------------------------


# Review's order: the items a person decides first, then the ones that stopped
# on an error or could not be read, each part oldest first.
_TIER = {_S.PENDING_REVIEW: 0}


def tier(status: Any) -> int:
    return _TIER.get(status, 1)


def _key(item_id: int, created_at: Any, part: int = 0) -> tuple[int, datetime, int]:
    return (part, _utc(created_at) or datetime.min.replace(tzinfo=timezone.utc), item_id)


def step(queue: Sequence[tuple[Any, ...]], anchor: tuple[Any, ...] | None, forward: bool) -> int | None:
    """The item after (or before) ``anchor`` in ``queue``, wrapping round.

    ``queue`` is (id, created_at[, part]) in Review's order. The anchor need
    not be in it: an item just decided has left the queue, and the next one
    is still the one queued after it. With one item waiting, that item is its
    own answer.
    """
    if not queue:
        return None
    if anchor is None:
        return queue[0][0]
    at = _key(*anchor)
    if forward:
        return next((entry[0] for entry in queue if _key(*entry) > at), queue[0][0])
    return next((entry[0] for entry in reversed(queue) if _key(*entry) < at), queue[-1][0])


@dataclass
class Review:
    card: Card | None
    #: 1-based place of the card in the queue; None when the card is not waiting.
    position: int | None
    total: int


# --- what a route answers --------------------------------------------------------

_INLINE = ("approve", "reject", "search")


async def act(action: str, item_id: int, posts: Any) -> actions.ActionResult | None:
    """Run approve, reject or search. None for any other word."""
    if action not in _INLINE:
        return None
    if action == "reject":
        return await actions.reject(item_id, posts)
    return await getattr(actions, action)(item_id)


def refusal(result: actions.ActionResult) -> HTMLResponse | None:
    """The 409, 404 or 422 for an action that did not run; its body is the toast."""
    refused = ui.refused(result)
    if refused is not None:
        return refused
    if isinstance(result, actions.SearchOutcome) and result.status is actions.Status.FAILED and not result.ran:
        # The search never ran: no title to search for, or nothing set up to search with.
        return HTMLResponse(escape(result.message), status_code=422)
    return None


def toast_kind(result: actions.ActionResult) -> str:
    # A rejection that could not tell Radarr, Sonarr or Seerr ran, and its
    # toast is still an error: that target still has the title. So is one
    # whose post could not be changed, after the dialog said what would be done to it.
    unfinished = getattr(result, "remote_failed", False) or getattr(result, "post_unchanged", False)
    return "ok" if result.ok and not unfinished else "error"


def announce(text: str, kind: str = "ok", *changed: int) -> dict[str, str]:
    """Headers for a toast, plus an ``item-<id>`` event for each item that changed.

    A row or a card behind the item drawer listens for its own item's event
    and reads itself again.
    """
    trigger: dict[str, Any] = {"toast": {"text": text, "kind": kind}}
    trigger.update({f"item-{item_id}": True for item_id in changed})
    return {"HX-Trigger": json.dumps(trigger)}


def render(template: str, **context: Any) -> str:
    """A partial as text, for an answer made of more than one."""
    return ui.templates.get_template(template).render(**context)


# --- reading -------------------------------------------------------------------


@dataclass
class Inbox:
    cards: list[Card]
    counts: dict[str, int]
    total: int
    page: int
    pages: int
    sources: list[str]

    @property
    def bulk_reject_confirm(self) -> str:
        """What rejecting the ticked rows deletes, as far as one sentence can say it for all of them."""
        rejectable = [card for card in self.cards if card.can_reject]
        effects = {card.post_effect for card in rejectable}
        said = [REJECT_BULK_POSTS[effects.pop()] if len(effects) == 1 else REJECT_BULK_MIXED]
        if any(card.download is not None for card in rejectable):
            said.append("A download this app started is removed with its files.")
        return " ".join(said)


def _handed(item: MediaItem) -> tuple[str | None, str, bool]:
    """(direct_add, on_reject, to_client) of an item, from the config."""
    source = ingest.source_for(item.source_key, item.telegram_chat_id)
    # Only the download client takes a post's own torrent or a picked release,
    # and a Seerr request never goes there: its delivery is Seerr, whatever `deliver` says.
    to_client = (
        seerr_api.request_of(item.origin_key) is None
        and delivery.target_for(item.media_type) == delivery.QBITTORRENT
    )
    return (ingest.direct_add_of(item) if to_client else None), getattr(source, "on_reject", "none"), to_client


async def _cards(session, items: Sequence[MediaItem]) -> list[Card]:
    ids = [item.id for item in items]
    if not ids:
        return []
    checks = {
        check.media_item_id: check
        for check in (await session.execute(
            select(ContentCheck).where(ContentCheck.media_item_id.in_(ids))
        )).scalars()
    }
    # Oldest first, so the newest search of an item is the one left in the dict.
    # The columns only: a row's stored releases are read by the item page.
    searches = {
        row.media_item_id: search_summary(row)
        for row in (await session.execute(
            select(
                TorrentSearch.id, TorrentSearch.media_item_id, TorrentSearch.result_count,
                TorrentSearch.best_match_title, TorrentSearch.best_match_size, TorrentSearch.best_match_seeders,
            )
            .where(TorrentSearch.media_item_id.in_(ids))
            .order_by(TorrentSearch.searched_at.asc(), TorrentSearch.id.asc())
        )).all()
    }
    downloads = {
        download.media_item_id: download
        for download in (await session.execute(
            select(Download).where(Download.media_item_id.in_(ids))
        )).scalars()
    }
    searchable = search_service.has_search()
    return [
        Card(item, checks.get(item.id), searches.get(item.id), downloads.get(item.id), *_handed(item), searchable)
        for item in items
    ]


async def _counts(session, filters: Filters) -> dict[str, int]:
    return tab_counts(dict((await session.execute(counts_query(filters))).all()))


async def load_inbox(tab: Tab, filters: Filters, page: int = 1, per_page: int = PER_PAGE) -> Inbox:
    async with async_session() as session:
        counts = await _counts(session, filters)
        total = counts[tab.key]
        pages = max(1, math.ceil(total / per_page))
        page = min(max(1, page), pages)
        items = (await session.execute(
            items_query(tab, filters).offset((page - 1) * per_page).limit(per_page)
        )).scalars().all()
        cards = await _cards(session, items)
        sources = (await session.execute(
            select(MediaItem.source_key).where(MediaItem.source_key.is_not(None))
            .distinct().order_by(MediaItem.source_key)
        )).scalars().all()
    return Inbox(cards, counts, total, page, pages, list(sources))


async def load_rows(ids: Sequence[int], filters: Filters) -> tuple[list[Card], dict[str, int]]:
    """The cards of ``ids``, in that order, and the tab counts under the filters."""
    async with async_session() as session:
        items = {
            item.id: item
            for item in (await session.execute(select(MediaItem).where(MediaItem.id.in_(list(ids))))).scalars()
        } if ids else {}
        cards = await _cards(session, [items[i] for i in ids if i in items])
        return cards, await _counts(session, filters)


async def load_detail(item_id: int) -> Detail | None:
    async with async_session() as session:
        item = await session.get(MediaItem, item_id)
        if item is None:
            return None
        check = (await session.execute(
            select(ContentCheck).where(ContentCheck.media_item_id == item_id)
        )).scalar_one_or_none()
        searches = (await session.execute(
            select(TorrentSearch).where(TorrentSearch.media_item_id == item_id)
            .order_by(TorrentSearch.searched_at.desc(), TorrentSearch.id.desc())
        )).scalars().all()
        download = (await session.execute(
            select(Download).where(Download.media_item_id == item_id)
        )).scalars().first()
        rows = (await session.execute(
            select(Event).where(Event.item_id == item_id).order_by(Event.at.asc(), Event.id.asc())
        )).scalars().all()
    direct_add, _on_reject, to_client = _handed(item)
    return detail_of(
        item, check=check, searches=searches, download=download, rows=rows,
        source=ingest.source_for(item.source_key, item.telegram_chat_id),
        direct_add=direct_add, to_client=to_client, delivery=delivery.where(item, download),
        searchable=search_service.has_search(),
    )


async def load_download(item_id: int) -> Card | None:
    """The item with its download and nothing else, for the progress poll."""
    async with async_session() as session:
        item = await session.get(MediaItem, item_id)
        if item is None:
            return None
        download = (await session.execute(
            select(Download).where(Download.media_item_id == item_id)
        )).scalars().first()
    return Card(item, download=download)


async def load_review(*, item: int | None = None, after: int | None = None, before: int | None = None) -> Review:
    """The card Review shows: ``item`` itself, or the one after or before an item.

    With none of the three, the first item waiting. The items to decide come
    first, then the ones that stopped on an error or could not be read, so
    the fast loop starts with real decisions.
    """
    async with async_session() as session:
        rows = (await session.execute(
            select(MediaItem.id, MediaItem.created_at, MediaItem.status)
            .where(MediaItem.status.in_(sorted(NEEDS_YOU, key=lambda s: s.value)))
        )).all()
        queue = sorted(((row.id, row.created_at, tier(row.status)) for row in rows), key=lambda e: _key(*e))
        target = item
        anchor_id = after if after is not None else before
        if anchor_id is not None:
            anchor = await session.get(MediaItem, anchor_id)
            # An item just decided has left the queue: it is placed where it was, among the decisions.
            part = tier(anchor.status) if anchor is not None and anchor.status in NEEDS_YOU else 0
            target = step(
                queue, (anchor.id, anchor.created_at, part) if anchor is not None else None, after is not None,
            )
        elif target is None:
            target = step(queue, None, True)
        row = await session.get(MediaItem, target) if target is not None else None
        cards = await _cards(session, [row]) if row is not None else []
    ids = [entry[0] for entry in queue]
    card = cards[0] if cards else None
    position = ids.index(card.id) + 1 if card is not None and card.id in ids else None
    return Review(card, position, len(ids))
