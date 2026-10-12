"""Stats (``/stats``) and Activity (``/activity``): what the policy and the providers did.

Both pages read the ``events`` table. Stats sums the last 30 days of it:
decisions per day, why items were held, the outcome per media type, how often
a person agreed with shadow mode, and each provider's pauses and downloads.
Activity is the table itself, newest first, filtered and paged.

The sums are made here from plain rows, by functions that take rows and
return what the templates draw, so they are tested without a database. The
two things only Postgres can do cheaply (the provider of each download, the
page of the log) are the two queries at the end of each section.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable
from urllib.parse import urlencode

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import and_, func, not_, or_, select, text

from vettarr import dryrun, events
from vettarr.delivery import LABELS as TARGET_LABELS
from vettarr.checker.policy import AUTO_APPROVE_OFF
from vettarr.config.runtime import get_config
from vettarr.database import async_session
from vettarr.models import AutoDecision, Event, ItemStatus, MediaItem
from vettarr.web import cards, shell, words
from vettarr.web.ui import templates

router = APIRouter()

WINDOW_DAYS = 30
PER_PAGE = 50
TOP_REASONS = 8

# What was done with an item, in the order the chart stacks it and every list
# names it. The colour of each is a chart token in app.css (chart-approve ...).
OUTCOMES = ("approve", "review", "wait", "reject")
OUTCOME_LABELS = {
    "approve": "Approved",
    "review": "Held for you",
    "wait": "Waiting for data",
    "reject": "Rejected",
}
_OUTCOME_WORDS = {
    "approve": "approved",
    "review": "held for you",
    "wait": "waiting for data",
    "reject": "rejected",
}

# How the first reason of a shadow review starts (checker/policy.py, ``act``).
SHADOW_LEAD = "shadow mode:"

# A person approved an item when it sits in one of these and the policy did
# not put it there.
_AFTER_APPROVAL = frozenset({
    ItemStatus.APPROVED, ItemStatus.SEARCHING, ItemStatus.DOWNLOADING,
    ItemStatus.COMPLETED, ItemStatus.NO_MATCH, ItemStatus.ERROR,
})


def _local(at: datetime) -> datetime:
    """An event's time on the server's clock (``TZ``), which is the operator's."""
    return (at if at.tzinfo else at.replace(tzinfo=timezone.utc)).astimezone()


# --- decisions ----------------------------------------------------------------


@dataclass(frozen=True)
class Decided:
    """One ``decision`` event, as much of it as the sums read."""

    at: datetime
    item_id: int | None
    done: str
    media_type: str
    reasons: tuple[str, ...] = ()


def decided_from(rows: Iterable[Any]) -> list[Decided]:
    """Event rows (``at``, ``item_id``, ``payload``) as decisions, oldest first.

    ``done`` is what was done with the item; an event written before that key
    existed only has ``outcome``. An outcome this page does not know is left
    out, not counted as something else.
    """
    out = []
    for row in rows:
        payload = row.payload if isinstance(row.payload, dict) else {}
        done = payload.get("done") or payload.get("outcome")
        if done not in OUTCOMES:
            continue
        reasons = payload.get("reasons")
        out.append(Decided(
            at=row.at,
            item_id=row.item_id,
            done=done,
            media_type=str(payload.get("media_type") or "unknown"),
            reasons=tuple(str(r) for r in reasons) if isinstance(reasons, list) else (),
        ))
    return sorted(out, key=lambda d: d.at)


def changes(decided: list[Decided]) -> list[Decided]:
    """The decisions that changed something.

    An item waiting for data is decided again every hour and gets ``wait``
    each time; counted as they come, those repeats would bury every other
    number. A decision counts when it is the item's first here or differs
    from the one before it.
    """
    last: dict[int, str] = {}
    out = []
    for d in decided:
        if d.item_id is not None:
            if last.get(d.item_id) == d.done:
                continue
            last[d.item_id] = d.done
        out.append(d)
    return out


def latest_per_item(decided: list[Decided]) -> list[Decided]:
    """Each item's newest decision; an event whose item is gone stands for itself."""
    newest: dict[int, Decided] = {}
    loose = []
    for d in decided:
        if d.item_id is None:
            loose.append(d)
        else:
            newest[d.item_id] = d
    return [*newest.values(), *loose]


@dataclass(frozen=True)
class Segment:
    outcome: str
    count: int
    y: float
    height: float
    title: str


@dataclass(frozen=True)
class Day:
    date: date
    counts: dict[str, int]
    x: float = 0.0
    segments: tuple[Segment, ...] = ()

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    @property
    def label(self) -> str:
        return f"{self.date.day} {self.date:%b}"


@dataclass(frozen=True)
class Chart:
    """Decisions per day, with the geometry of its bars.

    The SVG is ``width`` by ``height`` units and is stretched to the card, so
    the units are not pixels: a day is ``BAND`` wide whatever the screen. The
    numbers and dates around it are HTML, which keeps their size on a phone.
    """

    BAND = 10.0
    BAR = 6.0
    HEIGHT = 80.0
    GAP = 1.0  # between two segments of a bar, the card showing through

    days: tuple[Day, ...]
    top: int  # the value the full height stands for
    totals: dict[str, int] = field(default_factory=dict)

    @property
    def width(self) -> float:
        return self.BAND * len(self.days)

    @property
    def total(self) -> int:
        return sum(self.totals.values())

    @property
    def ticks(self) -> tuple[int, ...]:
        return (self.top, self.top // 2, 0) if self.top % 2 == 0 else (self.top, 0)

    @property
    def summary(self) -> str:
        """The chart in one sentence, for whoever cannot see it."""
        if not self.total:
            return f"Decisions per day, last {len(self.days)} days: none."
        parts = ", ".join(
            f"{self.totals[o]} {_OUTCOME_WORDS[o]}" for o in OUTCOMES if self.totals.get(o)
        )
        busiest = max(self.days, key=lambda d: d.total)
        return (
            f"Decisions per day, last {len(self.days)} days: {parts}. "
            f"The busiest day was {busiest.label} with {busiest.total}."
        )


def nice_top(value: int) -> int:
    """The axis top for a tallest bar of ``value``: even, so the middle line is a whole number.

    Up to ten it is the next even number; above, 1, 2 or 5 times a power of ten.
    """
    if value <= 10:
        return max(2, value + value % 2)
    power = 10 ** math.floor(math.log10(value))
    return next(step * power for step in (1, 2, 5, 10) if step * power >= value)


def chart_of(decided: list[Decided], today: date, days: int = WINDOW_DAYS) -> Chart:
    """The bar chart: ``days`` days ending today, each stacked by what was done."""
    first = today - timedelta(days=days - 1)
    counts: dict[date, Counter] = {first + timedelta(days=n): Counter() for n in range(days)}
    for d in changes(decided):
        day = _local(d.at).date()
        if day in counts:
            counts[day][d.done] += 1
    top = nice_top(max((sum(c.values()) for c in counts.values()), default=0))
    unit = Chart.HEIGHT / top
    drawn = []
    for n, (day, count) in enumerate(counts.items()):
        y = Chart.HEIGHT
        segments = []
        label = f"{day:%a} {day.day} {day:%b}"
        for outcome in OUTCOMES:
            c = count[outcome]
            if not c:
                continue
            full = c * unit
            y -= full
            # The gap is taken off the segment's own foot, never off a sliver.
            gap = Chart.GAP if full > 2 * Chart.GAP and segments else 0.0
            segments.append(Segment(
                outcome, c, round(y, 2), round(max(full - gap, 0.6), 2),
                f"{label}: {c} {_OUTCOME_WORDS[outcome]}",
            ))
        drawn.append(Day(
            day, {o: count[o] for o in OUTCOMES},
            x=n * Chart.BAND + (Chart.BAND - Chart.BAR) / 2, segments=tuple(segments),
        ))
    totals = {o: sum(d.counts[o] for d in drawn) for o in OUTCOMES}
    return Chart(tuple(drawn), top, totals)


@dataclass(frozen=True)
class Share:
    """One row with a bar: a reason, a media type's outcome."""

    label: str
    count: int
    percent: float


def _why(reasons: tuple[str, ...]) -> tuple[str, ...]:
    """The reasons that kept an item from being approved.

    A review the settings made (shadow mode, auto-approval switched off) leads
    with that and then lists what the policy found, which is often why it
    would have approved. Only the lead is why the item was held.
    """
    if reasons and (reasons[0].startswith(SHADOW_LEAD) or reasons[0] == AUTO_APPROVE_OFF):
        return reasons[:1]
    return reasons


def top_reasons(decided: list[Decided], limit: int = TOP_REASONS) -> list[Share]:
    """Why items were held, rejected or left waiting: each item's latest reasons, most common first."""
    held = [d for d in latest_per_item(decided) if d.done != "approve"]
    # In words, so five categories waiting for the same parents guide are one line.
    counts = Counter(reason for d in held for reason in dict.fromkeys(words.reason(r) for r in _why(d.reasons)))
    most = counts.most_common(limit)
    biggest = most[0][1] if most else 1
    return [Share(reason, n, round(100 * n / biggest, 1)) for reason, n in most]


@dataclass(frozen=True)
class TypeSplit:
    media_type: str
    total: int
    parts: tuple[Share, ...]  # one per outcome that happened, in OUTCOMES order

    @property
    def words(self) -> str:
        return " · ".join(f"{p.count} {_OUTCOME_WORDS[p.label]}" for p in self.parts)


def per_type(decided: list[Decided]) -> list[TypeSplit]:
    """What became of each media type's items, by each item's latest decision."""
    counts: dict[str, Counter] = {}
    for d in latest_per_item(decided):
        counts.setdefault(d.media_type, Counter())[d.done] += 1
    out = []
    for media_type, count in counts.items():
        total = sum(count.values())
        parts = tuple(
            Share(o, count[o], round(100 * count[o] / total, 1)) for o in OUTCOMES if count[o]
        )
        out.append(TypeSplit(media_type, total, parts))
    return sorted(out, key=lambda t: (-t.total, t.media_type))


# --- shadow agreement ---------------------------------------------------------


@dataclass(frozen=True)
class Shadow:
    """What a person did with the items shadow mode would have decided one way."""

    would: str
    total: int = 0
    approved: int = 0
    rejected: int = 0

    @property
    def undecided(self) -> int:
        return self.total - self.approved - self.rejected

    @property
    def sentence(self) -> str:
        verb = {"approve": "approved", "reject": "rejected", "wait": "waited for data on"}[self.would]
        said = [f"you approved {self.approved}", f"rejected {self.rejected}"]
        if self.undecided:
            said.append(f"{self.undecided} still wait{'s' if self.undecided == 1 else ''} for you")
        return f"Would have {verb} {self.total}: {', '.join(said)}."


@dataclass(frozen=True)
class Agreement:
    rows: tuple[Shadow, ...]
    agreed: int
    decided: int

    @property
    def total(self) -> int:
        return sum(r.total for r in self.rows)

    @property
    def percent(self) -> int | None:
        return round(100 * self.agreed / self.decided) if self.decided else None


def _person_did(status: Any, reviewed_at: Any, auto_decision: Any) -> str | None:
    """``approve`` or ``reject`` when a person decided the item, else None.

    An item the policy approved later (shadow mode was switched off and the
    item decided again) is nobody's decision.
    """
    if reviewed_at is None:
        return None
    if status == ItemStatus.REJECTED:
        return "reject"
    if status in _AFTER_APPROVAL and auto_decision != AutoDecision.AUTO_APPROVE:
        return "approve"
    return None


def agreement_of(rows: Iterable[Any]) -> Agreement:
    """Shadow agreement from ``would_have`` rows joined to their items, oldest first.

    Each row is (``item_id``, ``payload``, ``status``, ``reviewed_at``,
    ``auto_decision``). An item decided in shadow more than once counts by
    its latest; an item that is gone cannot be compared and is left out.
    """
    latest: dict[int, Any] = {}
    for row in rows:
        # A dry run's would-have is a write it did not send, not a decision.
        if row.item_id is not None and not dryrun.is_dry_run_event(row.payload):
            latest[row.item_id] = row
    counts = {would: Counter() for would in ("approve", "reject", "wait")}
    for row in latest.values():
        would = (row.payload or {}).get("outcome") if isinstance(row.payload, dict) else None
        if would not in counts:
            continue
        counts[would][_person_did(row.status, row.reviewed_at, row.auto_decision) or "undecided"] += 1
    shown = tuple(
        Shadow(would, sum(c.values()), c["approve"], c["reject"])
        for would, c in counts.items() if c
    )
    # Agreeing is doing what the policy would have: only an approval or a
    # rejection can be agreed with.
    agreed = counts["approve"]["approve"] + counts["reject"]["reject"]
    decided = sum(counts[w]["approve"] + counts[w]["reject"] for w in ("approve", "reject"))
    return Agreement(shown, agreed, decided)


# --- providers ----------------------------------------------------------------


@dataclass(frozen=True)
class GuardRow:
    """One provider's pauses: the times its guard stopped sending to it."""

    name: str
    pauses: int = 0
    expired: int = 0
    paused_minutes: int | None = None  # set while a pause is open
    last_at: datetime | None = None
    last_reason: str = ""

    @property
    def state(self) -> str:
        if self.paused_minutes is not None:
            return f"Paused, {self.paused_minutes} min left"
        return "Working"


def guard_history(rows: Iterable[Any], now: datetime, configured: Iterable[str] = ()) -> list[GuardRow]:
    """Each provider's pauses, from ``guard`` and ``cookie_expired`` rows, newest first.

    A provider of the config with no event is listed too: no trouble is an answer.
    """
    rows = list(rows)
    open_now = {f.name: f.minutes for f in shell.failing_from(
        [r for r in rows if r.kind == events.GUARD], now,
    )}
    found: dict[str, dict[str, Any]] = {name: {} for name in configured}
    for row in rows:
        payload = row.payload if isinstance(row.payload, dict) else {}
        name = payload.get("provider")
        if not name:
            continue
        entry = found.setdefault(str(name), {})
        if row.kind == events.COOKIE_EXPIRED:
            entry["expired"] = entry.get("expired", 0) + 1
        elif row.kind == events.GUARD and payload.get("state") == "cooldown":
            entry["pauses"] = entry.get("pauses", 0) + 1
            if "last_at" not in entry:
                entry["last_at"] = _local(row.at)
                entry["last_reason"] = str(payload.get("reason") or "not answering")
    return [
        GuardRow(
            name, entry.get("pauses", 0), entry.get("expired", 0), open_now.get(name),
            entry.get("last_at"), entry.get("last_reason", ""),
        )
        for name, entry in sorted(found.items())
    ]


@dataclass(frozen=True)
class DownloadRow:
    provider: str
    total: int = 0
    completed: int = 0
    running: int = 0
    failed: int = 0
    size: int = 0


DIRECT = "Not from a search"

# The provider of a download is the release it was picked from: the stored
# search's entry with the same link. A download with no such entry (the post's
# own magnet or .torrent, a search since deleted) has none.
_DOWNLOADS_SQL = text("""
    SELECT coalesce(r.value->>'provider', r.value->>'indexer') AS provider,
           d.status::text AS status,
           count(*) AS downloads,
           coalesce(sum((r.value->>'size')::numeric), 0) AS size
    FROM downloads d
    LEFT JOIN torrent_searches s ON s.id = d.torrent_search_id
    LEFT JOIN LATERAL (
        SELECT value
        FROM jsonb_array_elements(
            CASE WHEN jsonb_typeof(s.prowlarr_results) = 'array' THEN s.prowlarr_results ELSE '[]'::jsonb END
        )
        WHERE (d.download_url IS NOT NULL AND value->>'download_url' = d.download_url)
           OR (d.magnet_url IS NOT NULL AND value->>'magnet_url' = d.magnet_url)
        LIMIT 1
    ) r ON true
    GROUP BY 1, 2
""")


def downloads_of(rows: Iterable[Any]) -> list[DownloadRow]:
    """Download totals per provider from (``provider``, ``status``, ``downloads``, ``size``) rows.

    The size is what finished or is on its way; a failed or cancelled add
    brought nothing.
    """
    found: dict[str, Counter] = {}
    for row in rows:
        entry = found.setdefault(row.provider or DIRECT, Counter())
        status = str(row.status or "").lower()
        n = int(row.downloads or 0)
        entry["total"] += n
        if status == "completed":
            entry["completed"] += n
        elif status in ("downloading", "pending"):
            entry["running"] += n
        else:
            entry["failed"] += n
        if status in ("completed", "downloading"):
            entry["size"] += int(row.size or 0)
    out = [
        DownloadRow(name, c["total"], c["completed"], c["running"], c["failed"], c["size"])
        for name, c in found.items()
    ]
    # The providers by how much they brought; what came from no search goes last.
    return sorted(out, key=lambda r: (r.provider == DIRECT, -r.total, r.provider))


# --- the stats page -----------------------------------------------------------


def _providers() -> list[str]:
    try:
        return [p.key for p in get_config().providers]
    except Exception:
        return []


@router.get("/stats", response_class=HTMLResponse)
async def stats_page(request: Request):
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=WINDOW_DAYS)
    async with async_session() as session:
        decision_rows = (await session.execute(
            select(Event.at, Event.item_id, Event.payload)
            .where(Event.kind == events.DECISION, Event.at >= since)
            .order_by(Event.at, Event.id)
        )).all()
        shadow_rows = (await session.execute(
            select(
                Event.item_id, Event.payload,
                MediaItem.status, MediaItem.reviewed_at, MediaItem.auto_decision,
            )
            .join(MediaItem, MediaItem.id == Event.item_id)
            .where(Event.kind == events.WOULD_HAVE, Event.at >= since)
            .order_by(Event.at, Event.id)
        )).all()
        guard_rows = (await session.execute(
            select(Event.at, Event.kind, Event.payload)
            .where(Event.kind.in_((events.GUARD, events.COOKIE_EXPIRED)), Event.at >= since)
            .order_by(Event.at.desc(), Event.id.desc())
        )).all()
        download_rows = (await session.execute(_DOWNLOADS_SQL)).all()

    decided = decided_from(decision_rows)
    return templates.TemplateResponse(request, "stats.html", {
        "days": WINDOW_DAYS,
        "chart": chart_of(decided, _local(now).date()),
        "outcomes": [(o, OUTCOME_LABELS[o]) for o in OUTCOMES],
        "outcome_labels": OUTCOME_LABELS,
        "outcome_words": _OUTCOME_WORDS,
        "reasons": top_reasons(decided),
        "types": per_type(decided),
        "agreement": agreement_of(shadow_rows),
        "guards": guard_history(guard_rows, now, _providers()),
        "downloads": downloads_of(download_rows),
    })


# --- activity -----------------------------------------------------------------

KIND_LABELS = {
    events.DECISION: "Decision",
    events.WOULD_HAVE: "Shadow decision",
    events.NEEDS_YOU: "Needs you",
    events.GUARD: "Provider",
    events.COOKIE_EXPIRED: "Session expired",
    events.BOT_ADDED: "Added by bot",
    events.DELIVERED: "Delivered",
    events.COMPLETED: "Completed",
    events.DELIVERY_FAILED: "Delivery failed",
    events.DECLINED: "Declined in Seerr",
    events.WEBHOOK_TEST: "Webhook test",
    events.BOT_CHAT: "Bot chat",
}
# How the kind's badge looks. The badge always carries the label; the colour repeats it.
_KIND_BADGE = {
    events.NEEDS_YOU: "badge-warn",
    events.COOKIE_EXPIRED: "badge-flag",
    events.DELIVERY_FAILED: "badge-flag",
    events.COMPLETED: "badge-ok",
}
# The two kinds the hourly re-check writes again for an item that is still waiting.
_REPEATING = (events.DECISION, events.WOULD_HAVE)
SINCE = (("", "Any time", None), ("day", "Last 24 hours", 1), ("week", "Last 7 days", 7), ("month", "Last 30 days", 30))
_SINCE_DAYS = {key: days for key, _label, days in SINCE}


@dataclass(frozen=True)
class LogRow:
    at: datetime
    kind: str
    label: str
    badge: str
    text: str
    item_id: int | None
    title: str | None
    #: Where the fix is, for a provider's row: its card in Setup.
    fix: str | None = None

    @property
    def when(self) -> str:
        at = _local(self.at)
        return f"{at.day} {at:%b %H:%M}"

    @property
    def iso(self) -> str:
        return _local(self.at).isoformat(timespec="seconds")


_DECIDED = {
    "approve": "The policy approved it",
    "review": "The policy held it for you",
    "wait": "The policy is waiting for data",
    "reject": "The policy rejected it",
}


def _sentence(kind: str, payload: dict[str, Any]) -> str:
    """One event in words. Reads only what the payload carries; never raises."""
    reasons = payload.get("reasons")
    why = "; ".join(words.reason(str(r)) for r in reasons) if isinstance(reasons, list) and reasons else ""
    provider = str(payload.get("provider") or "A provider")
    reason = str(payload.get("reason") or "")
    if kind == events.DECISION:
        said = _DECIDED.get(payload.get("done") or payload.get("outcome"), "The policy decided it")
        return f"{said}: {why}." if why else f"{said}."
    if kind == events.WOULD_HAVE and dryrun.is_dry_run_event(payload):
        return f"{cards.dryrun_line(payload)}."
    if kind == events.WOULD_HAVE:
        would = {"approve": "approved it", "reject": "rejected it", "wait": "waited for data"}.get(
            payload.get("outcome"), "decided it",
        )
        return f"Shadow mode: the policy would have {would}, and left it to you."
    if kind == events.NEEDS_YOU:
        return f"Held for you: {why}." if why else "Held for you."
    if kind == events.GUARD:
        if payload.get("state") == "cooldown":
            minutes = max(1, round(int(payload.get("seconds") or 0) / 60))
            return f"{provider} paused for {minutes} min: {reason or 'not answering'}."
        return f"{provider} is working again."
    if kind == events.COOKIE_EXPIRED:
        return f"{provider}: the session ended{': ' + reason if reason else ''}. It needs a new cookie."
    if kind == events.BOT_ADDED:
        source = payload.get("source")
        return f"Added through the bot{' (' + str(source) + ')' if source else ''}."
    target = str(payload.get("target") or "")
    where = TARGET_LABELS.get(target, target)
    note = str(payload.get("note") or "").rstrip(".")
    failed = reason.rstrip(".")
    if kind == events.DELIVERED:
        return f"Handed to {where or 'its target'}."
    if kind == events.COMPLETED:
        if not where or target == "none":
            return f"Done: {note}." if note else "Done."
        return f"{where} has it: {note}." if note else f"{where} has it."
    if kind == events.DELIVERY_FAILED:
        if payload.get("retrying"):
            return f"{where or 'The target'} could not be asked{': ' + failed if failed else ''}. It is tried again."
        if not where or target == "none":
            return f"It could not be delivered{': ' + failed if failed else ''}."
        return f"{where} did not take it{': ' + failed if failed else ''}."
    if kind == events.DECLINED:
        return f"{failed}." if failed else "Its Seerr request was declined."
    if kind == events.WEBHOOK_TEST:
        source = payload.get("source")
        return f"Seerr's test notification reached {'the source ' + str(source) if source else 'a source'}."
    if kind == events.BOT_CHAT:
        if payload.get("state") == "left":
            return "The bot left a chat."
        title = payload.get("title")
        admin = " as an admin" if payload.get("is_admin") else ""
        return f"The bot was added to {str(title) if title else 'a chat'}{admin}."
    return ""


def log_row(event: Any, item_title: str | None = None) -> LogRow:
    payload = event.payload if isinstance(event.payload, dict) else {}
    # A bot_chat event's title is a chat's, not an item's: the sentence names it.
    title = item_title or (payload.get("title") if event.kind != events.BOT_CHAT else None)
    label = KIND_LABELS.get(event.kind, event.kind)
    badge = _KIND_BADGE.get(event.kind, "badge-kind")
    if event.kind == events.WOULD_HAVE and dryrun.is_dry_run_event(payload):
        label, badge = "Dry run", "badge-warn"
    if event.kind == events.DECISION and (payload.get("done") or payload.get("outcome")) == "review":
        # The review-queue event the policy writes beside it is left out of the log: this row says both.
        label, badge = KIND_LABELS[events.NEEDS_YOU], _KIND_BADGE[events.NEEDS_YOU]
    provider = payload.get("provider")
    fix = f"/setup#provider-{provider}" if provider and event.kind in (events.GUARD, events.COOKIE_EXPIRED) else None
    return LogRow(
        at=event.at,
        kind=event.kind,
        label=label,
        badge=badge,
        text=_sentence(event.kind, payload),
        item_id=event.item_id,
        title=str(title) if title else None,
        fix=fix,
    )


def policy_holds():
    """The review-queue events the policy wrote beside a decision, which says the same.

    The policy's carries the decision's payload (``outcome``); one a delivery
    wrote, for an item it hands back to a person, does not, and stays.
    """
    return and_(Event.kind.in_([events.NEEDS_YOU]), Event.payload["outcome"].astext.is_not(None))


def repeats():
    """The ids of the decisions that only say again what the one before them said.

    The hourly re-check decides every waiting item again and writes a
    ``decision`` (and in shadow mode a ``would_have``) each time. Listed as
    they come, those repeats bury everything else in the log; the chart leaves
    them out by the same rule (``changes``): an event is a repeat when the
    item's previous event of the same kind did the same thing.
    """
    done = Event.payload["done"].astext
    outcome = Event.payload["outcome"].astext
    order = (Event.at, Event.id)
    marked = (
        select(
            Event.id.label("id"), done.label("done"), outcome.label("outcome"),
            func.lag(done).over(partition_by=(Event.item_id, Event.kind), order_by=order).label("done_before"),
            func.lag(outcome).over(partition_by=(Event.item_id, Event.kind), order_by=order).label("outcome_before"),
            func.row_number().over(partition_by=(Event.item_id, Event.kind), order_by=order).label("n"),
        )
        # A dry run's would-haves are each a different write, never a repeat.
        .where(Event.kind.in_(_REPEATING), Event.item_id.is_not(None), Event.payload["dry_run"].astext.is_(None))
        .subquery()
    )
    return select(marked.c.id).where(
        marked.c.n > 1,
        marked.c.done.is_not_distinct_from(marked.c.done_before),
        marked.c.outcome.is_not_distinct_from(marked.c.outcome_before),
    )


def log_query(kind: str = "", item: str = "", since: str = "", waiting: bool = False, now: datetime | None = None):
    """The log's rows, unordered: events with their item's title, under the filters.

    ``item`` is an item's number or part of a title (or of a provider's name).
    ``waiting`` keeps the events of items still waiting for a person, by the
    same query the nav badge counts (``shell.waiting_items``).
    """
    query = (
        select(Event, MediaItem.title)
        .outerjoin(MediaItem, MediaItem.id == Event.item_id)
        .where(Event.id.not_in(repeats()))
    )
    if kind in events.KINDS:
        query = query.where(Event.kind == kind)
    if kind != events.NEEDS_YOU:
        query = query.where(not_(policy_holds()))
    # Postgres refuses a NUL in a text parameter.
    item = item.replace("\x00", "").strip()
    number = item.lstrip("#")
    words = or_(
        MediaItem.title.icontains(item, autoescape=True),
        Event.payload["title"].astext.icontains(item, autoescape=True),
        Event.payload["provider"].astext.icontains(item, autoescape=True),
    )
    # Nine digits at most: a longer number is no item's id, and Postgres refuses it as one.
    # A number is an item's id or a title made of digits (1917, 2012).
    if number.isascii() and number.isdecimal() and len(number) <= 9:
        query = query.where(or_(Event.item_id == int(number), words))
    elif item:
        query = query.where(words)
    days = _SINCE_DAYS.get(since)
    if days:
        query = query.where(Event.at >= (now or datetime.now(timezone.utc)) - timedelta(days=days))
    if waiting:
        query = query.where(Event.item_id.in_(shell.waiting_items()))
    return query


def page_link(page: int, kind: str, item: str, since: str, waiting: bool) -> str:
    """``/activity`` with the filters as they are and this page."""
    params = {"kind": kind, "item": item.strip(), "since": since, "waiting": "1" if waiting else "", "page": page if page > 1 else ""}
    query = urlencode({k: v for k, v in params.items() if v})
    return "/activity" + (f"?{query}" if query else "")


@router.get("/activity", response_class=HTMLResponse)
async def activity_page(
    request: Request,
    kind: str = "",
    item: str = Query("", max_length=200),
    since: str = "",
    waiting: str = "",
    page: str = "1",
):
    # Not a number, or past the end: the nearest page there is, never an error.
    try:
        page = max(1, int(page[:9]))
    except ValueError:
        page = 1
    kind = kind if kind in events.KINDS else ""
    item = item.replace("\x00", "")
    since = since if since in _SINCE_DAYS else ""
    only_waiting = waiting == "1"
    query = log_query(kind, item, since, only_waiting)
    async with async_session() as session:
        total = (await session.execute(
            select(func.count()).select_from(query.subquery())
        )).scalar() or 0
        pages = max(1, math.ceil(total / PER_PAGE))
        page = min(page, pages)
        found = (await session.execute(
            query.order_by(Event.at.desc(), Event.id.desc())
            .offset((page - 1) * PER_PAGE).limit(PER_PAGE)
        )).all()

    def link(n: int) -> str:
        return page_link(n, kind, item, since, only_waiting)

    return templates.TemplateResponse(request, "activity.html", {
        "rows": [log_row(event, title) for event, title in found],
        "total": total,
        "page": page,
        "pages": pages,
        "previous": link(page - 1) if page > 1 else None,
        "next": link(page + 1) if page < pages else None,
        "kinds": [(k, KIND_LABELS[k]) for k in events.KINDS],
        "sinces": [(key, label) for key, label, _days in SINCE],
        "kind": kind,
        "item": item.strip(),
        "since": since,
        "waiting": only_waiting,
        "filtered": bool(kind or item.strip() or since or only_waiting),
        "waiting_link": page_link(1, "", "", "", True),
    })
