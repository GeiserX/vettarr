"""Policy (``/policy``): the rules that decide an item, edited as sentences.

The page is one form. ``/policy/edit`` changes its shape (a rule added, moved
or removed, a signal picked) and writes nothing; ``/policy/preview`` runs the
form's policy over the last stored items; ``POST /policy`` validates it and
writes it to the config file. All three answer with the editor again, so an
error always lands beside its field.

The form is read into a plain dict of strings (``read_form``), the same shape
``policy_form`` builds from the file, and the template reads nothing else.
``policy_yaml`` turns it into the ``policy`` block.

A rule that combines conditions (``all``, ``any``, ``not``) is not edited
here: the page shows its YAML as it loads (comments left out), it can be moved or removed,
and a save writes it back exactly as the file has it. ``scopes`` are not
edited here either: the page shows them the same way, and a save keeps the
block as the file has it.

A word or a name is shown as the file writes it (``${VAR}`` stays ``${VAR}``);
a number or a choice is shown as it loads. Either way a field that comes back
as it was shown is written as the file has it, so a save changes what was
changed and nothing else: a reference stays a reference, ``0.333`` stays
``0.333`` and ``"007"`` keeps its quotes.
"""

from __future__ import annotations

import io
import re
from html import escape
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.datastructures import FormData

from vettarr.checker.policy import OUTCOMES, Impact, preview_impact
from vettarr.config import loader, schema
from vettarr.config.loader import ConfigError
from vettarr.config.schema import Config, Rule, TypePolicy
from vettarr.config.signals import MEDIA_TYPES, OPERATORS, RESOLUTIONS, SEVERITIES, SIGNALS, canonical
from vettarr.config.writer import ConfigReadOnly
from vettarr.web import configfile
from vettarr.web.ui import is_htmx, templates, toast

router = APIRouter()

#: How many stored items "Preview impact" decides again.
PREVIEW_LIMIT = 200
SAVED = "Saved to the config file. " + configfile.RESTART

AUTO_APPROVE = (
    ("off", "Off", "Nothing is decided for you: every item waits in the inbox."),
    ("shadow", "Shadow", "The policy decides and records what it would have done, and still leaves every item to you."),
    ("on", "On", "The policy acts: it approves, holds for review or rejects by the rules below."),
)
MODES = (("rules", "by the rules"), ("always", "always approve"), ("never", "always hold for me"))
WHEN_MISSING = (("wait", "wait for it"), ("review", "hold for review"), ("approve", "approve anyway"))
# A rule's own choice, first the one that leaves it to the type.
RULE_WHEN_MISSING = (("", "do as the type says"), *WHEN_MISSING)
OPERATOR_WORDS = {
    "at_least": "is at least",
    "at_most": "is at most",
    "between": "is between",
    "is": "is",
    "any_of": "is any of",
    "none_of": "is none of",
    "matches_any": "matches any of",
}
ON_HIT = (("review", "hold it for review"), ("reject", "reject it"), ("approve", "approve it"), ("notify", "only note it"))
# The word on the chosen severity chip: "hold from" severe, "reject up to" mild.
MARK = {"review": "hold", "reject": "reject", "approve": "approve", "notify": "note"}
GROUPS = (
    ("parents_guide", "Parents guide (IMDb)"),
    ("title", "Title (TMDB)"),
    ("release", "The release"),
    ("library", "Library and state"),
    ("context", "Context"),
    ("books", "Books and magazines"),
    ("series", "Series"),
)
DEFAULT_SIGNAL = "imdb.violence"

_RULE = re.compile(r"^rule-(\d+)-(?:check|group)$")
_LIST = re.compile(r"^list-(\d+)-name$")
_KEY = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_WHOLE = re.compile(r"^-?\d+$")
_RULE_PATH = re.compile(r"^policy\.rules\[(\d+)\](?:\.([a-z_]+))?")
_LIST_PATH = re.compile(r"^policy\.lists\.([a-z0-9_-]+)")
_TYPE_PATH = re.compile(r"^policy\.types\.([a-z]+)(?:\.([a-z_]+))?")
_TOP_PATH = re.compile(r"^policy\.(auto_approve|min_votes)$")


# --- the form ----------------------------------------------------------------


def _blank_rule(rule_id: str, check: str = DEFAULT_SIGNAL, operator: str | None = None) -> dict[str, Any]:
    return {
        "id": rule_id, "name": "", "check": check, "operator": operator or SIGNALS[check].operators[0],
        "severity": "severe", "number": "", "low": "", "high": "", "flag": "true", "list": "", "words": "",
        "cert": "", "resolution": RESOLUTIONS[0], "from": "", "to": "", "types": [], "on_hit": "review",
        "when_missing": "", "min_votes": "", "split": "", "errors": {},
    }


def _single(kind: str) -> str:
    """The form field that holds the one value of ``at_least`` and ``at_most`` for this kind of signal."""
    return {"severity": "severity", "resolution": "resolution", "certification": "cert"}.get(kind, "number")


def _group_form(rule_id: str, name: str = "", written: str = "") -> dict[str, Any]:
    """A rule that combines conditions: shown as ``written``, its YAML, and kept as the file has it."""
    return {"id": rule_id, "name": name, "group": True, "yaml": written, "errors": {}}


def _as_yaml(node: Any) -> str:
    buffer = io.StringIO()
    loader.yaml().dump(node, buffer)
    return buffer.getvalue()


def _rule_form(rule: Rule, raw: dict[str, Any]) -> dict[str, Any]:
    """A rule's form. ``raw`` is the rule as the file writes it: its words and its name are shown from there."""
    if rule.is_group:
        written = raw or rule.model_dump(mode="json", by_alias=True, exclude_none=True, exclude_defaults=True)
        return _group_form(str(raw.get("id") or rule.id), str(raw.get("name") or rule.name or ""), _as_yaml(written))
    signal, operator, value = SIGNALS[rule.check], rule.operator, rule.value
    form = _blank_rule(str(raw.get("id") or rule.id), rule.check, operator)
    form.update(
        name=str(raw.get("name") or rule.name or ""), types=list(rule.types), on_hit=rule.on_hit,
        when_missing=rule.when_missing or "",
        min_votes="" if rule.min_votes is None else str(rule.min_votes),
        split="" if rule.split_vote is None else f"{rule.split_vote * 100:g}",
    )
    if operator == "between":
        first, second = ("from", "to") if signal.kind == "time" else ("low", "high")
        form[first], form[second] = str(value[0]), str(value[1])
    elif operator == "is":
        form["flag"] = "true" if value else "false"
    elif operator in ("at_least", "at_most"):
        form[_single(signal.kind)] = str(value)
    elif isinstance(value, str):
        form["list"] = value
    else:
        written = raw.get(operator)
        form["words"] = "\n".join(str(v) for v in (written if isinstance(written, list) else value))
    return form


def policy_form(state: configfile.State) -> dict[str, Any]:
    """The editor's form for the policy in the file, or for the running one when there is no file to read."""
    policy = state.config.policy
    raw = state.raw("policy") if state.doc is not None else None
    raw = raw if isinstance(raw, dict) else {}
    raw_lists = raw.get("lists") if isinstance(raw.get("lists"), dict) else {}
    raw_rules = [r if isinstance(r, dict) else {} for r in raw.get("rules") or []]
    if len(raw_rules) != len(policy.rules):
        raw_rules = [{}] * len(policy.rules)
    return {
        "auto_approve": policy.auto_approve,
        "min_votes": "" if policy.min_votes is None else str(policy.min_votes),
        "types": {
            t: {"mode": tp.mode, "missing": tp.when_data_missing, "wait": str(tp.max_wait_hours), "errors": {}}
            for t in MEDIA_TYPES for tp in (policy.type_policy(t),)
        },
        "lists": [
            {"name": name, "words": "\n".join(str(w) for w in (raw_lists.get(name) or words)), "error": ""}
            for name, words in policy.lists.items()
        ],
        "rules": [_rule_form(rule, written) for rule, written in zip(policy.rules, raw_rules)],
        "errors": {}, "problems": [],
    }


def read_form(data: FormData) -> dict[str, Any]:
    """The posted form, every value a string as it was typed."""

    def text(name: str) -> str:
        return str(data.get(name) or "").strip()

    def indexes(pattern: re.Pattern[str]) -> list[int]:
        return sorted({int(m[1]) for key in data.keys() if (m := pattern.match(key))})

    rules = []
    for i in indexes(_RULE):
        if data.get(f"rule-{i}-group"):
            rules.append(_group_form(text(f"rule-{i}-id")))
            continue
        check = canonical(text(f"rule-{i}-check")) or DEFAULT_SIGNAL
        signal = SIGNALS[check]
        operator = text(f"rule-{i}-operator")
        rule = _blank_rule(text(f"rule-{i}-id"), check, operator if operator in signal.operators else None)
        for name in ("name", "number", "low", "high", "list", "cert", "from", "to", "min_votes", "split"):
            rule[name] = text(f"rule-{i}-{name}")
        for name in ("severity", "resolution"):
            # Not on the form while the rule checked something else: the default stands.
            rule[name] = text(f"rule-{i}-{name}") or rule[name]
        rule["flag"] = text(f"rule-{i}-flag") or "true"
        rule["on_hit"] = text(f"rule-{i}-on_hit") or "review"
        rule["when_missing"] = text(f"rule-{i}-when_missing")
        rule["words"] = str(data.get(f"rule-{i}-words") or "")
        rule["types"] = [t for t in data.getlist(f"rule-{i}-types") if t in signal.types]
        rules.append(rule)
    return {
        "auto_approve": text("auto_approve") or "off",
        "min_votes": text("min_votes"),
        "types": {
            t: {"mode": text(f"type-{t}-mode") or "rules", "missing": text(f"type-{t}-missing") or "wait",
                "wait": text(f"type-{t}-wait"), "errors": {}}
            for t in MEDIA_TYPES
        },
        "lists": [
            {"name": text(f"list-{i}-name"), "words": str(data.get(f"list-{i}-words") or ""), "error": ""}
            for i in indexes(_LIST)
        ],
        "rules": rules,
        "errors": {}, "problems": [],
    }


def _free_name(taken: set[str], stem: str) -> str:
    return next(name for n in range(1, len(taken) + 2) if (name := f"{stem}-{n}") not in taken)


def change(form: dict[str, Any], op: str) -> str | None:
    """One step on the form's shape: ``add-rule``, ``remove-rule:2``, ``up:2``, ``down:2``, ``add-list``, ``remove-list:1``.

    Returns the id of the control the keyboard goes to next. The whole editor
    is swapped, and focus would otherwise fall back to the top of the page: a
    moved rule keeps the focus on its own button, a new rule or list gets it
    on its name, and after a removal it goes to what took its place.
    """
    name, _, arg = op.partition(":")
    rules, lists = form["rules"], form["lists"]
    index = int(arg) if arg.isdigit() else -1
    if name == "add-rule":
        rules.append(_blank_rule(_free_name({r["id"] for r in rules}, "rule")))
        return f"rule-{len(rules) - 1}-name"
    if name == "add-list":
        lists.append({"name": _free_name({entry["name"] for entry in lists}, "list"), "words": "", "error": ""})
        return f"list-{len(lists) - 1}-name"
    if name == "remove-rule" and 0 <= index < len(rules):
        del rules[index]
        return f"rule-{min(index, len(rules) - 1)}-name" if rules else "add-rule"
    if name == "remove-list" and 0 <= index < len(lists):
        del lists[index]
        return f"list-{min(index, len(lists) - 1)}-name" if lists else "add-list"
    if name == "up" and 0 < index < len(rules):
        rules[index - 1], rules[index] = rules[index], rules[index - 1]
        # The first rule's Move up is disabled: its Move down is the button left to press.
        return f"rule-{index - 1}-up" if index > 1 else "rule-0-down"
    if name == "down" and 0 <= index < len(rules) - 1:
        rules[index + 1], rules[index] = rules[index], rules[index + 1]
        return f"rule-{index + 1}-down" if index + 1 < len(rules) - 1 else f"rule-{index + 1}-up"
    return None


# --- the form as the policy block ----------------------------------------------


def _term(word: str) -> str | int:
    """A typed word as the file holds it: a number only when it reads back as typed, so 007 stays 007."""
    return int(word) if _WHOLE.match(word) and str(int(word)) == word else word


def _words(text: str, old: Any, term: Any = str) -> list[Any]:
    """The words typed one per line. One the file already has is the file's own, so its spelling and its quotes stay."""
    written: dict[str, Any] = {}
    for value in old if isinstance(old, list) else ():
        written.setdefault(str(value), value)
    return [written[word] if word in written else term(word) for word in configfile.words(text)]


def _value(rule: dict[str, Any], old: Any = None, shown: dict[str, Any] | None = None) -> Any:
    """The value a rule's fields stand for.

    ``old`` is what the file has under the same operator and ``shown`` the
    form the page showed for it: a part that came back as shown is the file's.
    """
    kind, operator = SIGNALS[rule["check"]].kind, rule["operator"]

    def as_shown(field: str) -> bool:
        return old is not None and shown is not None and rule[field] == shown[field]

    if operator == "between":
        names = ("from", "to") if kind == "time" else ("low", "high")
        had = old if isinstance(old, list) and len(old) == 2 else None
        return [
            had[n] if had is not None and as_shown(name)
            else rule[name] if kind == "time" else configfile.number(rule[name])
            for n, name in enumerate(names)
        ]
    if operator == "is":
        return old if as_shown("flag") else rule["flag"] == "true"
    if operator in ("at_least", "at_most"):
        field = _single(kind)
        if as_shown(field):
            return old
        return configfile.number(rule[field]) if field == "number" else rule[field]
    if rule["list"]:
        return old if isinstance(old, str) and as_shown("list") else rule["list"]
    return _words(rule["words"], old, _term)


def _rule_yaml(rule: dict[str, Any], old: dict[str, Any], shown: dict[str, Any] | None) -> dict[str, Any]:
    """A rule as the file gets it. ``old`` is the rule in the file and ``shown`` the form the page showed for it."""
    signal, operator = SIGNALS[rule["check"]], rule["operator"]

    def kept(key: str, *fields: str) -> bool:
        """The file has ``key`` and its fields came back as the page showed them."""
        return key in old and shown is not None and all(rule[name] == shown[name] for name in fields)

    out: dict[str, Any] = {"id": rule["id"]}
    if rule["name"]:
        out["name"] = rule["name"]
    if rule["types"]:
        # The boxes come back in the page's order, whatever order the file lists them in.
        same = "types" in old and shown is not None and sorted(rule["types"]) == sorted(shown["types"])
        out["types"] = old["types"] if same else list(rule["types"])
    # The file may name a signal by where it comes from (tmdb.keyword); that name stays.
    out["check"] = old["check"] if kept("check", "check") else rule["check"]
    out[operator] = _value(rule, old[operator] if kept(operator, "check", "operator") else None, shown)
    if signal.voted and rule["split"]:
        share = configfile.number(rule["split"])
        out["split_vote"] = (
            old["split_vote"] if kept("split_vote", "split")
            # Rounded: 33.3 / 100 is 0.33299999999999996 as a float.
            else round(share / 100, 8) if isinstance(share, (int, float)) else share
        )
    if signal.voted and rule["min_votes"]:
        out["min_votes"] = old["min_votes"] if kept("min_votes", "min_votes") else configfile.number(rule["min_votes"])
    if rule["on_hit"] != "review" or "on_hit" in old:
        out["on_hit"] = old["on_hit"] if kept("on_hit", "on_hit") else rule["on_hit"]
    if rule["when_missing"] or kept("when_missing", "when_missing"):
        out["when_missing"] = old["when_missing"] if kept("when_missing", "when_missing") else rule["when_missing"]
    return out


def policy_yaml(form: dict[str, Any], old: dict[str, Any], shown: dict[str, Any]) -> dict[str, Any]:
    """The ``policy`` block the form stands for.

    ``old`` is the block in the file and ``shown`` the form the page showed
    for it (``policy_form``). ``old`` is read for what to leave unsaid, and a
    field that came back as it was shown is written as ``old`` has it.
    """
    out: dict[str, Any] = {}
    for key in ("auto_approve", "min_votes"):
        if key in old and form[key] == shown[key]:
            out[key] = old[key]
        elif key == "auto_approve":
            out[key] = form[key]
        elif form[key]:
            out[key] = configfile.number(form[key])
    default = TypePolicy()
    old_types = old.get("types") or {}
    types: dict[str, Any] = {}
    for media_type in MEDIA_TYPES:
        row, was, had = form["types"][media_type], shown["types"][media_type], old_types.get(media_type) or {}
        entry: dict[str, Any] = {}
        if "mode" in had and row["mode"] == was["mode"]:
            entry["mode"] = had["mode"]
        elif "mode" in had or row["mode"] != default.mode:
            entry["mode"] = row["mode"]
        for key, field, value, usual in (
            ("when_data_missing", "missing", row["missing"], default.when_data_missing),
            ("max_wait_hours", "wait", configfile.number(row["wait"]), default.max_wait_hours),
        ):
            if key in had and row[field] == was[field]:
                entry[key] = had[key]
            # Only a type decided by rules waits for data; elsewhere the two say nothing.
            elif key in had or (row["mode"] == "rules" and value != usual):
                entry[key] = value
        if media_type in old_types or entry:
            types[media_type] = entry
    if types or "types" in old:
        out["types"] = types
    old_lists = old.get("lists") or {}
    lists = {e["name"]: _words(e["words"], old_lists.get(e["name"])) for e in form["lists"] if e["name"]}
    if lists or "lists" in old:
        out["lists"] = lists
    old_rules = {str(r.get("id")): r for r in old.get("rules") or [] if isinstance(r, dict)}
    shown_rules = {rule["id"]: rule for rule in shown["rules"] if not rule.get("group")}
    for rule in form["rules"]:
        # A combined rule the file no longer has was renamed or removed by hand while the page was open.
        if rule.get("group") and rule["id"] not in old_rules:
            form["problems"].append(
                f"rule {rule['id']} is a combined rule config.yaml no longer has; reload the page"
            )
    out["rules"] = [
        # A combined rule is written as the file has it.
        old_rules[rule["id"]] if rule.get("group") else
        _rule_yaml(rule, old_rules.get(rule["id"], {}), shown_rules.get(rule["id"]))
        for rule in form["rules"] if not rule.get("group") or rule["id"] in old_rules
    ]
    # Not on the form: kept as the file has it.
    if "scopes" in old:
        out["scopes"] = old["scopes"]
    return out


def _own_errors(form: dict[str, Any]) -> bool:
    """What the schema cannot say about a list's name, put on the list. True when there is any."""
    seen: set[str] = set()
    for entry in form["lists"]:
        name = entry["name"]
        if not _KEY.match(name):
            entry["error"] = "A list needs a name: lowercase letters, digits, - and _."
        elif name in seen:
            entry["error"] = f"Two lists are named {name}."
        seen.add(name)
    return any(entry["error"] for entry in form["lists"])


def attach(form: dict[str, Any], errors: dict[str, str]) -> None:
    """Put each error beside what it is about: a rule's field, a list, a type's row."""
    by_name = {entry["name"]: entry for entry in form["lists"]}
    for path, message in errors.items():
        if (m := _RULE_PATH.match(path)) and int(m[1]) < len(form["rules"]):
            field = m[2] or ""
            if field in OPERATORS:
                field = "value"
            form["rules"][int(m[1])]["errors"][{"split_vote": "split"}.get(field, field)] = message
        elif (m := _LIST_PATH.match(path)) and m[1] in by_name:
            by_name[m[1]]["error"] = by_name[m[1]]["error"] or message
        elif (m := _TYPE_PATH.match(path)) and m[1] in form["types"]:
            form["types"][m[1]]["errors"][m[2] or ""] = message
        elif m := _TOP_PATH.match(path):
            form["errors"][m[1]] = message
        else:
            form["problems"].append(message if path == configfile.TOP else f"{path}: {message}")


def build(state: configfile.State, form: dict[str, Any]) -> tuple[str, Config | None]:
    """The file's text with the form's policy in it, and its config; None, with the errors on the form, when it is not valid."""
    doc = state.candidate()
    old = loader.plain(doc.get("policy") or {})
    configfile.put(doc, "policy", policy_yaml(form, old if isinstance(old, dict) else {}, policy_form(state)))
    text, config, errors = configfile.validate(doc)
    attach(form, errors)
    if _own_errors(form) or form["problems"]:
        config = None
    return text, config


# --- the answers ---------------------------------------------------------------


def _scopes(state: configfile.State) -> str:
    """The file's ``scopes`` block as YAML, as it loads; empty when there is none."""
    raw = state.raw("policy") if state.doc is not None else None
    if isinstance(raw, dict):
        written = raw.get("scopes")
    else:
        written = [s.model_dump(mode="json", by_alias=True, exclude_none=True, exclude_defaults=True)
                   for s in state.config.policy.scopes]
    return _as_yaml(written) if written else ""


def _warnings(config: Config) -> list[str]:
    return [message for loc, message in schema.warnings(config) if loc and loc[0] == "policy"]


def _context(
    state: configfile.State, form: dict[str, Any], config: Config | None, impact: Impact | None,
    focus: str | None = None, previewing: bool = False,
) -> dict:
    shown = config or state.config
    invalid = sum(
        bool(rule["errors"]) for rule in form["rules"]
    ) + sum(bool(e["error"]) for e in form["lists"]) + sum(bool(t["errors"]) for t in form["types"].values()) + len(
        form["errors"]
    ) + len(form["problems"])
    return {
        "state": state, "form": form, "impact": impact, "invalid": invalid, "focus": focus, "previewing": previewing,
        "scopes": _scopes(state),
        "warnings": _warnings(config) if config is not None else [],
        "auto_approve_options": AUTO_APPROVE, "modes": MODES, "when_missing": WHEN_MISSING,
        "rule_when_missing": RULE_WHEN_MISSING,
        "operator_words": OPERATOR_WORDS, "on_hit": ON_HIT, "mark": MARK, "signals": SIGNALS,
        "groups": [(label, [s.name for s in SIGNALS.values() if s.group == group]) for group, label in GROUPS],
        "severities": SEVERITIES, "resolutions": RESOLUTIONS, "media_types": MEDIA_TYPES, "outcomes": OUTCOMES,
        "default_votes": (
            form["min_votes"] if _WHOLE.match(form["min_votes"]) else shown.integrations.imdb_guide.min_votes
        ),
        "preview_limit": PREVIEW_LIMIT,
    }


def first_error(form: dict[str, Any]) -> str:
    """Where the first thing to fix is and what it says, for the toast of a refused save."""
    for n, rule in enumerate(form["rules"], start=1):
        if rule["errors"]:
            return f"rule {n}: {next(iter(rule['errors'].values()))}"
    for entry in form["lists"]:
        if entry["error"]:
            return f"the list {entry['name'] or 'with no name'}: {entry['error']}"
    for media_type, row in form["types"].items():
        if row["errors"]:
            return f"{media_type}: {next(iter(row['errors'].values()))}"
    for name, message in form["errors"].items():
        return f"{name.replace('_', ' ')}: {message}"
    return form["problems"][0] if form["problems"] else "a value is not valid"


def _refused_answer(form: dict[str, Any], lead: str) -> dict[str, str]:
    """A save or a preview that was not done: a toast saying so, and no scroll to the result.

    The answer focuses the first field marked invalid (``autofocus``), and
    that is where the page goes: the notice under the form sat under the
    action bar, out of sight on a phone.
    """
    said = first_error(form).rstrip(".")
    headers = toast(f"{lead} Fix {said[:1].lower()}{said[1:]}.", "error")
    headers["HX-Reswap"] = "outerHTML"
    return headers


def _render(
    request: Request,
    state: configfile.State,
    form: dict[str, Any],
    config: Config | None = None,
    impact: Impact | None = None,
    headers: dict[str, str] | None = None,
    focus: str | None = None,
    previewing: bool = False,
) -> HTMLResponse:
    groups = {rule["id"]: rule for rule in policy_form(state)["rules"] if rule.get("group")}
    for rule in form["rules"]:
        # A posted combined rule carries its id only; what it says is read from the file.
        if rule.get("group") and not rule["yaml"] and rule["id"] in groups:
            rule.update(name=groups[rule["id"]]["name"], yaml=groups[rule["id"]]["yaml"])
    context = _context(state, form, config, impact, focus, previewing)
    if is_htmx(request):
        return templates.TemplateResponse(request, "policy/_editor.html", context, headers=headers)
    # A plain form post: the whole page, and a status that says the form was not accepted.
    return templates.TemplateResponse(
        request, "policy.html", context, status_code=422 if context["invalid"] else 200
    )


def _refused(state: configfile.State) -> HTMLResponse:
    return HTMLResponse(escape(state.refusal), status_code=409)


@router.get("/policy", response_class=HTMLResponse)
async def policy_page(request: Request):
    state = configfile.load()
    return _render(request, state, policy_form(state), state.config)


@router.post("/policy/edit", response_class=HTMLResponse)
async def policy_edit(request: Request):
    """Add, remove or move a rule or a list, or show the fields a newly picked signal takes. Writes nothing."""
    state = configfile.load()
    if not state.writable:
        return _refused(state)
    data = await request.form()
    form = read_form(data)
    focus = change(form, str(data.get("op") or ""))
    return _render(request, state, form, focus=focus)


@router.post("/policy/preview", response_class=HTMLResponse)
async def policy_preview(request: Request):
    """What the form's policy would decide for the last stored items. Writes nothing.

    Managed by file, the form cannot change, so the preview is of the policy
    the file holds: what it decides for the items stored now.
    """
    state = configfile.load()
    if not state.writable:
        return _render(
            request, state, policy_form(state), state.config,
            await preview_impact(state.config.policy, PREVIEW_LIMIT), previewing=True,
        )
    form = read_form(await request.form())
    _, config = build(state, form)
    impact = None
    if config is not None:
        try:
            impact = await preview_impact(config.policy, PREVIEW_LIMIT)
        except ValueError as exc:
            form["problems"].append(str(exc))
            config = None
    headers = _refused_answer(form, "Nothing to preview yet.") if config is None else None
    return _render(request, state, form, config, impact, headers=headers, previewing=True)


@router.post("/policy", response_class=HTMLResponse)
async def policy_save(request: Request):
    state = configfile.load()
    if not state.writable:
        return _refused(state)
    form = read_form(await request.form())
    text, config = build(state, form)
    if config is None:
        return _render(request, state, form, headers=_refused_answer(form, "Nothing saved."))
    try:
        configfile.write(state, text)
    except ConfigReadOnly as exc:
        return HTMLResponse(escape(str(exc)), status_code=409)
    except ConfigError as exc:
        form["problems"].extend(exc.errors)
        return _render(request, state, form, headers=_refused_answer(form, "Nothing saved."))
    if not is_htmx(request):
        return RedirectResponse("/policy", status_code=303)
    state = configfile.load()
    return _render(request, state, policy_form(state), state.config, headers=toast(SAVED))
