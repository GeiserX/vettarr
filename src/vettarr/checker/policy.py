"""The policy engine: ``evaluate(item, signals, policy)`` returns a ``Decision``.

Pure: no database, no network, no clock of its own. The pipeline gathers the
signals (``vettarr.checker.gather``), hands them here with the item and
the ``policy`` block of config.yaml, and acts on what comes back. The same
function re-runs stored signals for ``preview_impact``, so a decision can be
replayed without fetching anything again.

How a decision is reached, in order:

1. A duplicate of something already settled goes to a person.
2. The type policy: ``always`` approves, ``never`` goes to a person. So
   does a type in mode ``rules`` that no rule applies to: nothing was
   checked, and nothing unchecked is approved.
3. An item with no title goes to a person; a TMDB match that is not the
   title the post asked for goes to a person whenever a rule reads the TMDB
   record or IMDb data (that data would be another title's).
4. The rules that apply to the type, in file order. A rule whose condition is
   true *hits*: ``reject`` and ``review`` hits are all collected, ``notify``
   hits are recorded and change nothing, and the first ``approve`` hit ends
   the walk when no earlier rule hit or lacked its data. A rule that combines
   conditions (``all``, ``any``, ``not``) hits when the combination is true;
   it lacks its data when the combination cannot be settled without a
   missing part.
5. Any reject hit rejects; any review hit reviews; an approve hit approves.
6. Otherwise, a rule whose data is missing (or whose integration is off)
   hands the item to the type's ``when_data_missing``: ``approve``,
   ``review``, or ``wait`` until ``max_wait_hours`` have passed since the
   item arrived, and then review.
7. Otherwise nothing hit, and the item is approved.

The data a type needs is exactly what its rules read: a policy with no
keyword rule never waits for keywords.

A signal snapshot is a JSON-ready mapping, signal name to value:

- a number, a bool, a list of terms, ``"HH:MM"`` for ``time_window``,
  ``{"US": "R", ...}`` for ``certification``, ``"1080p"`` for ``resolution``
- voted signals: ``{"severity": "mild" | None, "votes": int, "breakdown": {...}}``
- ``{"partial": [...]}``: a list too thin to clear anything; a match in it
  still hits, no match counts as missing
- ``{"missing": "why"}`` or no key at all: the data is not in
- ``{"unavailable": "<integration>"}``: the integration that feeds it is off

plus three keys starting with ``_`` that are not signals: ``_duplicate``
(what this item duplicates), ``_identity`` (``{"ok": bool, "text": str}``,
whether the TMDB match is the title) and ``_no_title``.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from vettarr.checker.content_rules import IDENTITY_UNCONFIRMED
from vettarr.config.schema import Condition, Policy, Rule, TypePolicy
from vettarr.config.signals import MEDIA_TYPES, RESOLUTIONS, SEVERITIES, SIGNALS

APPROVE = "approve"
REVIEW = "review"
REJECT = "reject"
WAIT = "wait"
OUTCOMES = (APPROVE, REVIEW, REJECT, WAIT)

# Reason kinds. `rule` is a rule that hit; `missing` and `unavailable` a rule
# that could not be checked; `clear` what an approval rested on; `legacy` a
# reason written before reasons were structured.
RULE = "rule"
MISSING = "missing"
UNAVAILABLE = "unavailable"
NOTIFY = "notify"
DUPLICATE = "duplicate"
IDENTITY = "identity"
TYPE = "type"
SETTING = "setting"
SHADOW = "shadow"
EXPIRED = "expired"
CLEAR = "clear"
LEGACY = "legacy"

# Reasons a review came from the settings, not from anything found about the
# item. The backlog re-run re-decides exactly these.
SETTINGS_KINDS = frozenset({SETTING, SHADOW, TYPE})

META_DUPLICATE = "_duplicate"
META_IDENTITY = "_identity"
META_NO_TITLE = "_no_title"

AUTO_APPROVE_OFF = "auto-approval is switched off"
NO_TITLE = "the post carries no title"
# How 0.11 worded a review for a type outside its auto-approve list:
# "<type> is not in the auto-approve list".
LEGACY_NOT_IN_TYPE_LIST = " is not in the auto-approve list"
# Signals outside the title and parents-guide groups that are also read from
# the matched TMDB record, so a wrong match answers them for another title.
_TMDB_RECORD_SIGNALS = frozenset({"status", "episode_count", "in_library"})
# Reasons that say something was found about the item itself.
_FINDING_KINDS = frozenset({RULE, DUPLICATE, IDENTITY})

_SEVERITY_RANK = {name: n for n, name in enumerate(SEVERITIES)}
_RESOLUTION_RANK = {name: n for n, name in enumerate(RESOLUTIONS)}

# Rating scales, mildest first, for `certification: {at_least: CC:rating}`.
# A country not here, or a rating not on its scale, cannot be ordered, and
# the rule then counts as missing data for that item.
CERTIFICATION_SCALES: dict[str, tuple[str, ...]] = {
    "US": ("G", "TV-Y", "TV-Y7", "TV-G", "PG", "TV-PG", "PG-13", "TV-14", "R", "TV-MA", "NC-17", "X"),
    "GB": ("U", "PG", "12A", "12", "15", "18", "R18"),
    "ES": ("A", "APTA", "TP", "7", "10", "12", "13", "16", "18", "X"),
    "DE": ("0", "6", "12", "16", "18"),
    "FR": ("U", "TP", "10", "12", "16", "18"),
    "IT": ("T", "6+", "14+", "18+"),
    "NL": ("AL", "6", "9", "12", "14", "16", "18"),
    "BR": ("L", "10", "12", "14", "16", "18"),
    "CA": ("G", "PG", "14A", "18A", "R", "A"),
    "AU": ("G", "PG", "M", "MA15+", "R18+", "X18+"),
}


@dataclass(frozen=True)
class Reason:
    kind: str
    rule: str | None
    text: str

    def to_json(self) -> dict[str, Any]:
        return {"kind": self.kind, "rule": self.rule, "text": self.text}

    @classmethod
    def from_json(cls, data: Any) -> Reason:
        if isinstance(data, Mapping):
            return cls(str(data.get("kind") or LEGACY), data.get("rule"), str(data.get("text") or ""))
        return cls(LEGACY, None, str(data))


@dataclass(frozen=True)
class Decision:
    outcome: str
    reasons: tuple[Reason, ...] = field(default_factory=tuple)
    policy_version: str = ""

    @property
    def texts(self) -> list[str]:
        return [r.text for r in self.reasons]

    @property
    def hits(self) -> list[Reason]:
        return [r for r in self.reasons if r.kind == RULE]

    def reasons_json(self) -> list[dict[str, Any]]:
        return [r.to_json() for r in self.reasons]


def policy_version(policy: Policy) -> str:
    """A short hash of the policy block: equal blocks, equal versions."""
    dumped = policy.model_dump(mode="json", by_alias=True)
    blob = json.dumps(dumped, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


def legacy_reasons(reasons: Any, auto_reasons: Iterable[str] | None) -> list[Reason]:
    """Reasons as stored: structured when the row has them, else read from 0.11's words.

    A row decided before reasons were structured carries only the plain
    strings in ``auto_reasons``. Two of those say outright that a setting
    made the review, and 0.11 wrote each as the first reason of its row: the
    switch being off (``setting``) and a type outside the auto-approve list
    (``type``). Every other string becomes a ``legacy`` reason, which cannot
    say whether a setting or a finding sent the item to review.
    """
    if isinstance(reasons, list):
        return [Reason.from_json(r) for r in reasons]
    out = []
    for text in auto_reasons or ():
        text = str(text)
        if text == AUTO_APPROVE_OFF:
            kind = SETTING
        elif text.endswith(LEGACY_NOT_IN_TYPE_LIST):
            kind = TYPE
        else:
            kind = LEGACY
        out.append(Reason(kind, None, text))
    return out


def unavailable(integration: str, why: str | None = None) -> dict[str, str]:
    """The integration that feeds a signal is off; ``why`` says so in other words when it is not a setting."""
    return {"unavailable": integration, **({"why": why} if why else {})}


def missing(why: str) -> dict[str, str]:
    return {"missing": why}


# --- reading values ----------------------------------------------------------


def fold(text: Any) -> str:
    """Lowercase, accent-free, punctuation as spaces, single-spaced."""
    t = unicodedata.normalize("NFD", str(text))
    t = "".join(c for c in t if unicodedata.category(c) != "Mn").lower()
    t = re.sub(r"[^a-z0-9+:.\s-]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _words(value: Any, lists: Mapping[str, list[str]]) -> list[str]:
    if isinstance(value, str):
        return list(lists[value])
    return [str(v) for v in value]


def _loose(text: Any) -> str:
    """``fold`` with every punctuation mark a space: "sea-monster" reads "sea monster"."""
    return re.sub(r"\s+", " ", re.sub(r"[+:.\-]", " ", fold(text))).strip()


def _matches_pattern(words: list[str]) -> re.Pattern[str] | None:
    """Whole-word matcher for ``matches_any``; None when the list holds no word."""
    ordered = sorted({_loose(w) for w in words if _loose(w)}, key=len, reverse=True)
    if not ordered:
        return None
    return re.compile(r"(?<![a-z0-9])(?:" + "|".join(re.escape(w) for w in ordered) + r")(?![a-z0-9])")


def _share(breakdown: Mapping[str, Any] | None, threshold: str, above: bool) -> float | None:
    """Share of the votes at or above (or at or below) a severity; None without votes."""
    if not isinstance(breakdown, Mapping):
        return None
    line = _SEVERITY_RANK[threshold]
    total = inside = 0
    for label, votes in breakdown.items():
        rank = _SEVERITY_RANK.get(str(label).strip().lower())
        if rank is None or not isinstance(votes, int) or isinstance(votes, bool) or votes <= 0:
            continue
        total += votes
        if (rank >= line) if above else (rank <= line):
            inside += votes
    return inside / total if total > 0 else None


def _cert_rank(country: str, rating: str) -> int | None:
    scale = CERTIFICATION_SCALES.get(country.upper())
    if scale is not None:
        folded = [s.upper() for s in scale]
        if rating.upper() in folded:
            return folded.index(rating.upper())
    return None


def _split_cert(value: str) -> tuple[str, str]:
    country, _, rating = value.partition(":")
    return country.strip().upper(), rating.strip()


Outcome = tuple[bool | None, str]


def _check_voted(rule: Condition, value: Any, min_votes: int) -> Outcome:
    name = rule.check
    if not isinstance(value, Mapping):
        return None, f"{name}: no parents guide"
    votes = value.get("votes") or 0
    if votes < min_votes:
        return None, f"{name}: only {votes} votes, need {min_votes}"
    severity = value.get("severity")
    if severity not in _SEVERITY_RANK:
        return None, f"{name}: no severity recorded"
    threshold = rule.value
    above = rule.operator == "at_least"
    rank, line = _SEVERITY_RANK[severity], _SEVERITY_RANK[threshold]
    if (rank >= line) if above else (rank <= line):
        return True, f"{name}: {severity}"
    if rule.split_vote is not None:
        share = _share(value.get("breakdown"), threshold, above)
        if share is not None and share >= rule.split_vote:
            side = "or worse" if above else "or milder"
            return True, f"{name}: labelled {severity}, but {share:.0%} of the votes are {threshold} {side}"
    return False, ""


def _check_number(rule: Condition, value: Any) -> Outcome:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None, f"{rule.check}: no data"
    op, want = rule.operator, rule.value
    if op == "at_least":
        hit = value >= want
    elif op == "at_most":
        hit = value <= want
    else:
        hit = want[0] <= value <= want[1]
    shown = round(value, 2) if isinstance(value, float) else value
    return hit, f"{rule.check}: {shown}"


def _check_bool(rule: Condition, value: Any) -> Outcome:
    if not isinstance(value, bool):
        return None, f"{rule.check}: no data"
    return value is rule.value, f"{rule.check}: {'yes' if value else 'no'}"


def _check_terms(rule: Condition, value: Any, lists: Mapping[str, list[str]]) -> Outcome:
    partial = isinstance(value, Mapping) and "partial" in value
    items = value["partial"] if partial else value
    if not isinstance(items, list):
        return None, f"{rule.check}: no data"
    words = _words(rule.value, lists)
    op = rule.operator
    if not any(fold(w) for w in words):
        # An empty named list names nothing, so nothing is in it and nothing
        # is measured against it: the rule never hits. `config check` warns.
        return False, ""
    if op == "matches_any":
        pattern = _matches_pattern(words)
        found = [str(i) for i in items if pattern is not None and pattern.search(_loose(i))]
    else:
        wanted = {fold(w) for w in words}
        found = [str(i) for i in items if fold(i) in wanted]
    if op == "none_of":
        if partial or not items:
            return None, f"{rule.check}: too little data to rule anything out"
        if found:
            return False, ""
        return True, f"{rule.check}: {', '.join(str(i) for i in items)} is none of the listed"
    if found:
        return True, f"{rule.check}: {', '.join(found)}"
    if partial:
        return None, f"{rule.check}: only {len(items)} known, too few to clear it"
    return False, ""


def _check_certification(rule: Condition, value: Any, lists: Mapping[str, list[str]]) -> Outcome:
    if not isinstance(value, Mapping):
        return None, "certification: no data"
    certs = {str(k).upper(): str(v) for k, v in value.items() if v}
    op = rule.operator
    if op == "at_least":
        country, rating = _split_cert(rule.value)
        have = certs.get(country)
        if have is None:
            # No rating in the rule's country says nothing about the title.
            return None, f"certification: none for {country}"
        rank, line = _cert_rank(country, have), _cert_rank(country, rating)
        if rank is None or line is None:
            return None, f"certification: {country}:{have} cannot be ordered against {rating}"
        return rank >= line, f"certification: {country}:{have}"
    wanted = [_split_cert(v) for v in _words(rule.value, lists)]
    if not wanted:
        return False, ""
    found = [f"{c}:{certs[c]}" for c, r in wanted if c in certs and certs[c].upper() == r.upper()]
    if op == "any_of":
        return (True, f"certification: {', '.join(found)}") if found else (False, "")
    named = {c for c, _ in wanted}
    if not named & set(certs):
        return None, f"certification: none for {', '.join(sorted(named))}"
    if found:
        return False, ""
    shown = ", ".join(f"{c}:{certs[c]}" for c in sorted(named & set(certs)))
    return True, f"certification: {shown} is none of the listed"


def _check_resolution(rule: Condition, value: Any, lists: Mapping[str, list[str]]) -> Outcome:
    if value not in _RESOLUTION_RANK:
        return None, "resolution: no data"
    op = rule.operator
    if op in ("at_least", "at_most"):
        rank, line = _RESOLUTION_RANK[value], _RESOLUTION_RANK[rule.value]
        return (rank >= line if op == "at_least" else rank <= line), f"resolution: {value}"
    listed = {str(v) for v in _words(rule.value, lists)}
    if not listed:
        return False, ""
    inside = value in listed
    return (inside if op == "any_of" else not inside), f"resolution: {value}"


def _minutes(hhmm: str) -> int:
    hours, minutes = hhmm.split(":")
    return int(hours) * 60 + int(minutes)


def _check_time(rule: Condition, value: Any) -> Outcome:
    if not isinstance(value, str) or not re.fullmatch(r"\d{2}:\d{2}", value):
        return None, "time_window: no clock"
    start, end = (_minutes(v) for v in rule.value)
    now = _minutes(value)
    inside = start <= now <= end if start <= end else (now >= start or now <= end)
    return inside, f"time_window: {value}"


def check_rule(rule: Condition, value: Any, lists: Mapping[str, list[str]], min_votes: int) -> Outcome:
    """Whether one rule hits on one value: True, False, or None for missing data."""
    if isinstance(value, Mapping) and "unavailable" in value:
        if value.get("why"):
            return None, f"{rule.check}: {value['why']}"
        return None, f"{rule.check}: integrations.{value['unavailable']} is not enabled"
    if isinstance(value, Mapping) and "missing" in value:
        return None, f"{rule.check}: {value['missing']}"
    if value is None:
        return None, f"{rule.check}: no data"
    signal = SIGNALS[rule.check]
    if signal.voted:
        return _check_voted(rule, value, min_votes)
    kind = signal.kind
    if kind == "number":
        return _check_number(rule, value)
    if kind == "bool":
        return _check_bool(rule, value)
    if kind == "certification":
        return _check_certification(rule, value, lists)
    if kind == "resolution":
        return _check_resolution(rule, value, lists)
    if kind == "time":
        return _check_time(rule, value)
    return _check_terms(rule, value, lists)


def _condition(
    cond: Condition, signals: Mapping[str, Any], lists: Mapping[str, list[str]], min_votes_default: int
) -> tuple[bool | None, str, Condition | None]:
    """``check_condition``, plus the leaf whose missing data a missing outcome names."""
    if not cond.is_group:
        min_votes = cond.min_votes if cond.min_votes is not None else min_votes_default
        hit, text = check_rule(cond, signals.get(cond.check), lists, min_votes)
        return hit, text, cond if hit is None else None
    # Every child is checked, none skipped: False settles `all` and True
    # settles `any` whatever a missing child would say, and the text needs
    # every child that hit.
    outcomes = [_condition(child, signals, lists, min_votes_default) for child in cond.children]
    first_missing = next(((text, leaf) for hit, text, leaf in outcomes if hit is None), None)
    if cond.not_ is not None:
        hit, text, leaf = outcomes[0]
        if hit is None:
            return None, text, leaf
        if hit:
            return False, "", None
        child = cond.not_
        return True, text or f"not {child.check if not child.is_group else _group_word(child)}", None
    hits = [hit for hit, _, _ in outcomes]
    if cond.all_ is not None:
        if False in hits:
            return False, "", None
        if first_missing is None:
            return True, "; ".join(text for _, text, _ in outcomes if text), None
    else:
        if True in hits:
            return True, "; ".join(text for hit, text, _ in outcomes if hit and text), None
        if first_missing is None:
            return False, "", None
    return None, *first_missing


def _group_word(cond: Condition) -> str:
    return "all" if cond.all_ is not None else "any" if cond.any_ is not None else "not"


def check_condition(
    cond: Condition, signals: Mapping[str, Any], lists: Mapping[str, list[str]], min_votes_default: int
) -> Outcome:
    """Whether a rule's condition holds on a snapshot: True, False, or None for missing data.

    A leaf is ``check_rule`` on its own signal, with its own ``min_votes`` or
    the default. ``all`` is True when every condition is, False when any is;
    ``any`` is True when any condition is, False when every one is; ``not``
    swaps True and False. Anything else is missing, and says why with the
    first condition that is.
    """
    hit, text, _ = _condition(cond, signals, lists, min_votes_default)
    return hit, text


# --- the decision ------------------------------------------------------------


def _media_type(item: Any) -> str:
    media_type = getattr(item, "media_type", None)
    return str(getattr(media_type, "value", media_type) or "unknown")


def type_policy(policy: Policy, media_type: str) -> TypePolicy:
    if media_type not in MEDIA_TYPES:
        return TypePolicy(mode="never")
    return policy.type_policy(media_type)


def no_rules_text(media_type: str) -> str:
    return f"no rule applies to {media_type}, so nothing was checked"


def applicable_rules(policy: Policy, media_type: str) -> list[Rule]:
    """The rules that read a signal of this type, in file order."""
    return [rule for rule in policy.rules if media_type in rule.media_types]


def needed_signals(policy: Policy, media_type: str) -> set[str]:
    """The signals a decision about this type reads; nothing else is fetched."""
    if type_policy(policy, media_type).mode != "rules":
        return set()
    return {leaf.check for rule in applicable_rules(policy, media_type) for leaf in rule.leaves()}


def _as_utc(moment: datetime) -> datetime:
    return moment.replace(tzinfo=timezone.utc) if moment.tzinfo is None else moment


def evaluate(
    item: Any,
    signals: Mapping[str, Any],
    policy: Policy,
    *,
    now: datetime | None = None,
    default_min_votes: int = 20,
) -> Decision:
    """Decide one item from its signals. See the module docstring for the order."""
    version = policy_version(policy)
    media_type = _media_type(item)

    def done(outcome: str, *reasons: Reason) -> Decision:
        return Decision(outcome, tuple(reasons), version)

    duplicate = signals.get(META_DUPLICATE)
    if duplicate:
        return done(REVIEW, Reason(DUPLICATE, None, str(duplicate)))

    tp = type_policy(policy, media_type)
    if tp.mode == "always":
        return done(APPROVE, Reason(TYPE, None, f"{media_type} is always approved"))
    if tp.mode == "never":
        return done(REVIEW, Reason(TYPE, None, f"{media_type} is always reviewed by hand"))

    rules = applicable_rules(policy, media_type)
    if not rules:
        # Mode `rules` with nothing to check: approving would approve unseen.
        return done(REVIEW, Reason(TYPE, None, no_rules_text(media_type)))

    if signals.get(META_NO_TITLE):
        return done(REVIEW, Reason(IDENTITY, None, NO_TITLE))

    reads_title = any(
        SIGNALS[leaf.check].group in ("title", "parents_guide") or leaf.check in _TMDB_RECORD_SIGNALS
        for rule in rules for leaf in rule.leaves()
    )
    identity = signals.get(META_IDENTITY)
    if reads_title and isinstance(identity, Mapping) and not identity.get("ok"):
        return done(
            REVIEW,
            Reason(IDENTITY, None, IDENTITY_UNCONFIRMED),
            Reason(IDENTITY, None, str(identity.get("text") or "")),
        )

    min_votes_default = policy.min_votes if policy.min_votes is not None else default_min_votes
    blocks: list[tuple[str, Reason]] = []
    gaps: list[Reason] = []
    notes: list[Reason] = []
    approved_by: Reason | None = None
    for rule in rules:
        hit, text, short = _condition(rule, signals, policy.lists, min_votes_default)
        if hit is None:
            value = signals.get(short.check) if short is not None else None
            off = isinstance(value, Mapping) and "unavailable" in value
            gaps.append(Reason(UNAVAILABLE if off else MISSING, rule.id, text))
            continue
        if not hit:
            continue
        reason = Reason(RULE, rule.id, text)
        if rule.on_hit == "notify":
            notes.append(Reason(NOTIFY, rule.id, text))
        elif rule.on_hit == "approve":
            if not blocks and not gaps:
                approved_by = reason
                break
        else:
            blocks.append((rule.on_hit, reason))

    if any(on_hit == "reject" for on_hit, _ in blocks):
        ordered = [r for o, r in blocks if o == "reject"] + [r for o, r in blocks if o != "reject"]
        return done(REJECT, *ordered, *notes)
    if blocks:
        return done(REVIEW, *(r for _, r in blocks), *notes)
    if approved_by is not None:
        return done(APPROVE, approved_by, *notes)
    if gaps:
        if tp.when_data_missing == "approve":
            return done(APPROVE, *gaps, *notes)
        if tp.when_data_missing == "review":
            return done(REVIEW, *gaps, *notes)
        created = getattr(item, "created_at", None)
        if isinstance(created, datetime) and now is not None:
            if _as_utc(now) - _as_utc(created) >= timedelta(hours=tp.max_wait_hours):
                return done(
                    REVIEW,
                    Reason(EXPIRED, None, f"no data after {tp.max_wait_hours} hours"),
                    *gaps, *notes,
                )
        return done(WAIT, *gaps, *notes)

    cleared: list[Reason] = []
    if reads_title and isinstance(identity, Mapping) and identity.get("text"):
        cleared.append(Reason(CLEAR, None, str(identity["text"])))
    cleared.append(Reason(CLEAR, None, f"no rule hit ({len(rules)} checked)"))
    return done(APPROVE, *cleared, *notes)


@dataclass(frozen=True)
class Applied:
    """What the pipeline does with a decision under ``auto_approve``."""

    outcome: str
    reasons: tuple[Reason, ...]
    #: True when the decision was recorded but not acted on.
    shadow: bool = False


def act(decision: Decision, auto_approve: str) -> Applied:
    """``on`` acts on the decision; ``off`` and ``shadow`` hand everything to a person.

    A review is a review in every mode, so its reasons stay as they are. Any
    other outcome under ``off`` or ``shadow`` becomes a review whose first
    reason says why, and the backlog re-run can find it by that reason.

    One review is the switch's doing too: under ``off`` no lookup runs, so a
    review that rests only on data nobody fetched (``when_data_missing:
    review``) leads with the same reason, and is decided again once the
    switch is on.
    """
    if auto_approve == "off" and decision.outcome == REVIEW:
        found = any(r.kind in _FINDING_KINDS or r.kind == TYPE for r in decision.reasons)
        if not found and any(r.kind in (MISSING, UNAVAILABLE) for r in decision.reasons):
            return Applied(REVIEW, (Reason(SETTING, None, AUTO_APPROVE_OFF), *decision.reasons))
    if auto_approve == "on" or decision.outcome == REVIEW:
        return Applied(decision.outcome, decision.reasons, shadow=auto_approve == "shadow")
    if auto_approve == "shadow":
        verb = {APPROVE: "approved", REJECT: "rejected", WAIT: "waited for data"}[decision.outcome]
        lead = Reason(SHADOW, None, f"shadow mode: would have {verb}")
        return Applied(REVIEW, (lead, *decision.reasons), shadow=True)
    return Applied(REVIEW, (Reason(SETTING, None, AUTO_APPROVE_OFF), *decision.reasons))


def is_settings_review(reasons: Any, auto_reasons: Iterable[str] | None = None) -> bool:
    """True when a stored review came from the settings, not from anything found.

    ``auto_reasons`` is the row's 0.11 column, read only when the row has no
    structured ``reasons``: a row 0.11 held because its switch was off, or
    because the type was not on its list, is a settings review too
    (``legacy_reasons``).
    """
    stored = legacy_reasons(reasons, auto_reasons)
    return bool(stored) and stored[0].kind in SETTINGS_KINDS


def worth_rerunning(
    reasons: Any,
    auto_reasons: Iterable[str] | None,
    decided_version: str | None,
    media_type: Any,
    policy: Policy,
) -> bool:
    """Whether deciding a held item again could end anywhere else.

    Only a settings review qualifies, and only when the setting that made it
    has moved. Nothing under ``off``: every decision is held again with the
    same words. A type review only when the type would no longer be held by
    its mode: it is ``always`` now, or in ``rules`` with a rule that applies.
    A shadow review made by this same policy not while still in ``shadow``.
    """
    if policy.auto_approve == "off" or not is_settings_review(reasons, auto_reasons):
        return False
    kind = legacy_reasons(reasons, auto_reasons)[0].kind
    if kind == TYPE:
        name = str(getattr(media_type, "value", media_type) or "unknown")
        mode = type_policy(policy, name).mode
        return mode == "always" or (mode == "rules" and bool(applicable_rules(policy, name)))
    if kind == SHADOW and policy.auto_approve == "shadow":
        return decided_version != policy_version(policy)
    return True


# --- preview impact ----------------------------------------------------------


@dataclass
class Impact:
    """What a candidate policy would change over stored items."""

    counts: dict[str, int] = field(default_factory=lambda: {o: 0 for o in OUTCOMES})
    #: item_id, title, before, after, reasons (texts) of every changed item.
    changed: list[dict[str, Any]] = field(default_factory=list)
    compared: int = 0
    #: Rows with no stored signals or no stored outcome: decided before the
    #: policy engine, or never decided. Never re-fetched to compare them.
    not_comparable: int = 0


def compare(
    rows: Iterable[Any],
    policy: Policy,
    *,
    now: datetime | None = None,
    default_min_votes: int = 20,
) -> Impact:
    """Re-evaluate stored rows against ``policy``. Pure.

    Each row needs ``id``, ``title``, ``media_type``, ``created_at``,
    ``signals`` (the stored snapshot) and ``policy_outcome`` (what the policy
    decided when the row was decided).

    Each row is decided as of the moment it was decided (``auto_decided_at``),
    the moment its snapshot was read. Measured against today's clock, every
    stored wait older than ``max_wait_hours`` would read as expired, and an
    unchanged policy would report it as a change. ``now`` is used only for a
    row that does not record when it was decided.
    """
    impact = Impact()
    for row in rows:
        before = getattr(row, "policy_outcome", None)
        snapshot = getattr(row, "signals", None)
        if not isinstance(snapshot, Mapping) or before not in OUTCOMES:
            impact.not_comparable += 1
            continue
        decided_at = getattr(row, "auto_decided_at", None)
        at = decided_at if isinstance(decided_at, datetime) else now
        decision = evaluate(row, snapshot, policy, now=at, default_min_votes=default_min_votes)
        impact.compared += 1
        impact.counts[decision.outcome] += 1
        if decision.outcome != before:
            impact.changed.append({
                "item_id": getattr(row, "id", None),
                "title": getattr(row, "title", None),
                "before": before,
                "after": decision.outcome,
                "reasons": decision.texts,
            })
    return impact


async def preview_impact(policy_candidate: Policy | Mapping[str, Any], limit: int = 200) -> Impact:
    """Re-evaluate the last ``limit`` stored items under a candidate policy.

    Reads only what each item stored when it was decided: no TMDB, IMDb or
    catalogue request is made. A candidate given as a mapping is validated
    the way config.yaml's ``policy`` block is.
    """
    from sqlalchemy import select

    from vettarr.config.runtime import get_config
    from vettarr.config.schema import Config, cross_check
    from vettarr.database import async_session
    from vettarr.models import MediaItem

    cfg = get_config()
    if isinstance(policy_candidate, Policy):
        policy = policy_candidate
    else:
        policy = Policy.model_validate(dict(policy_candidate))
    candidate = Config(sources=cfg.sources, policy=policy)
    errors = [message for loc, message in cross_check(candidate) if loc and loc[0] == "policy"]
    if errors:
        raise ValueError("; ".join(errors))

    async with async_session() as session:
        rows = (
            await session.execute(select(MediaItem).order_by(MediaItem.id.desc()).limit(limit))
        ).scalars().all()
    return compare(
        rows, policy,
        now=datetime.now(timezone.utc),
        default_min_votes=cfg.integrations.imdb_guide.min_votes,
    )
