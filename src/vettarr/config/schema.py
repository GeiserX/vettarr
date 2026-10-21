"""Pydantic models for config.yaml. docs/CONFIG.md is the prose contract.

Every block forbids unknown keys, so a typo is an error, not a silently
ignored setting. Secrets are ``SecretStr``: printing a model never shows them.
Checks that need more than one block (a source naming a format that does not
exist, a delivery target that is not configured) live in ``cross_check``.
"""

from __future__ import annotations

import re
from typing import Annotated, Any, ClassVar, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    StringConstraints,
    field_validator,
    model_validator,
)

from vettarr.config.signals import (
    GROUP_NEEDS,
    MEDIA_TYPES,
    OPERATORS,
    RESOLUTIONS,
    SCOPE_SIGNALS,
    SEVERITIES,
    SIGNALS,
    canonical,
)

Key = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9_-]*$", max_length=64)]
Word = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
MediaType = Literal["movie", "series", "audiobook", "ebook", "magazine", "comic", "music"]
Url = Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^https?://[^\s/]+")]

# A path on a tracker, joined to base_url as it is: without the leading slash
# `base_url + path` would name another host.
SitePath = Annotated[str, StringConstraints(pattern=r"^/")]

# What a torrent id may be made of. A link pattern's `id` goes into the
# download URL, the thank path and the thank body as it is, so it is never a
# slash, a dot, a `?` or an `&`.
TORRENT_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")

Loc = tuple[Any, ...]

QBIT_DEFAULT_ACCOUNT = "admin"
WEBHOOK_SECRET_MIN = 16


class Block(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


# --- sources ---------------------------------------------------------------


class Source(Block):
    key: Key
    kind: Literal["userbot", "bot", "seerr"]
    # A chat id, an @username, or "private" (bot only: direct messages).
    chat: int | str | None = None
    backfill: Literal["off", "since_last", "full"] | None = None
    formats: list[Key] = Field(default_factory=list)
    allowed_senders: Literal["reviewers", "anyone"] | list[int] | None = None
    # Whose own torrent (a .torrent attachment, a magnet) is added as it is.
    direct_adds: Literal["reviewers", "anyone", "off"] | None = None
    on_download: Literal["delete", "react", "none"] = "none"
    on_reject: Literal["delete", "react", "none"] = "none"
    notify_on_review: bool = False
    poll_minutes: int | None = Field(default=None, ge=1)
    # Seerr only: what Seerr's webhook sends as its Authorization header.
    # Without it the source has no webhook route and is read by the poll alone.
    webhook_secret: SecretStr | None = None

    @field_validator("webhook_secret")
    @classmethod
    def _long_secret(cls, value: SecretStr | None) -> SecretStr | None:
        # The route it opens is outside the login: a short secret can be guessed.
        if value is None:
            return value
        secret = value.get_secret_value()
        if len(secret) < WEBHOOK_SECRET_MIN:
            raise ValueError(f"webhook_secret needs at least {WEBHOOK_SECRET_MIN} characters")
        # It is compared with an HTTP header, where these three never match:
        # the header is read without its spaces and without a leading
        # "Bearer ", and is not text beyond ASCII.
        if not (secret.isascii() and secret.isprintable()) or secret != secret.strip() \
                or secret[:7].lower() == "bearer ":
            raise ValueError(
                "webhook_secret must be plain ASCII with no space at either end, and must not start "
                "with 'Bearer ': Seerr sends it as a header, where such a secret never matches"
            )
        return value

    @field_validator("chat", mode="before")
    @classmethod
    def _numeric_chat(cls, value: Any) -> Any:
        if isinstance(value, str) and re.fullmatch(r"-?\d+", value.strip()):
            return int(value)
        return value

    @model_validator(mode="after")
    def _per_kind(self) -> Source:
        if self.kind == "seerr":
            if self.chat is not None:
                raise ValueError("a seerr source has no chat")
            if self.on_download != "none" or self.on_reject != "none":
                raise ValueError("a seerr source has no post to delete or react to; use none")
            if self.backfill not in (None, "off"):
                raise ValueError("a seerr source cannot backfill")
            self.backfill = "off"
            if self.poll_minutes is None:
                self.poll_minutes = 5
            if self.allowed_senders is None:
                self.allowed_senders = "anyone"
            if self.direct_adds not in (None, "off"):
                raise ValueError("a seerr source carries no torrent; direct_adds must be off")
            self.direct_adds = "off"
            return self
        if self.direct_adds is None:
            self.direct_adds = "reviewers"
        if self.poll_minutes is not None:
            raise ValueError("poll_minutes applies to seerr sources only")
        if self.webhook_secret is not None:
            raise ValueError("webhook_secret applies to seerr sources only")
        if self.chat is None or self.chat == "":
            raise ValueError(f"a {self.kind} source needs a chat")
        if isinstance(self.chat, str) and self.chat != "private" and not self.chat.startswith("@"):
            raise ValueError("chat is a numeric chat id, an @username, or private")
        if self.kind == "userbot":
            if self.chat == "private":
                raise ValueError("chat: private is for bot sources; a userbot reads a group or channel")
            if self.backfill is None:
                self.backfill = "since_last"
            if self.allowed_senders is None:
                self.allowed_senders = "anyone"
        else:
            if self.backfill not in (None, "off"):
                raise ValueError("a bot cannot read history; backfill must be off")
            self.backfill = "off"
            if self.allowed_senders is None:
                self.allowed_senders = "reviewers"
        return self


# --- formats ---------------------------------------------------------------


class FormatFields(Block):
    """Labels of the lines a card carries, e.g. ``Title: ...``."""

    title: list[Word] = Field(default_factory=list)
    link: list[Word] = Field(default_factory=list)
    year: list[Word] = Field(default_factory=list)
    type: list[Word] = Field(default_factory=list)


class FormatProfile(Block):
    """A post format as word lists. The app compiles the words into patterns.

    Words match case-insensitively and as whole words; a space inside a word
    matches any run of whitespace; ``/.../`` is a regular expression.
    """

    key: Key
    kind: Literal["card", "scene"] = "card"
    # A line holding one of these words decides the media type outright.
    type_headers: dict[MediaType, list[Word]] = Field(default_factory=dict)
    # Weaker evidence: phrases that suggest a type anywhere in the post.
    type_hints: dict[MediaType, list[Word]] = Field(default_factory=dict)
    # Category names as they appear in a header or a type line. One that
    # opens a line and is followed by an arrow or a dash also hints its type.
    category_words: dict[MediaType, list[Word]] = Field(default_factory=dict)
    # Markers that open a header line ("<marker> <category> -> <source>"); the
    # release name is on a line of its own.
    header_markers: list[Word] = Field(default_factory=list)
    fields: FormatFields = Field(default_factory=FormatFields)
    # Lines starting with one of these, or with a header marker, are
    # metadata, never the title.
    ignore_line_prefixes: list[Word] = Field(default_factory=list)
    # A post whose only link points at one of these hosts is not media.
    ignore_hosts: list[Word] = Field(default_factory=list)
    # Whole words that name a kind of media ("film", "book"): a message with
    # no release structure that holds one is a request, kept for review. With
    # none in any format of a source, every message with words in it is kept.
    media_words: list[Word] = Field(default_factory=list)


# --- providers -------------------------------------------------------------


class ProviderAuth(Block):
    from_: Literal["prowlarr", "cookie_file", "login"] = Field(alias="from")
    cookie_file: str | None = None
    username: str | None = None
    password: SecretStr | None = None

    @model_validator(mode="after")
    def _needs(self) -> ProviderAuth:
        if self.from_ == "cookie_file" and not self.cookie_file:
            raise ValueError("auth from cookie_file needs cookie_file")
        if self.from_ == "login" and not (self.username and self.password):
            raise ValueError("auth from login needs username and password")
        return self


class LinkExample(Block):
    url: str
    id: str


class ProviderLink(Block):
    pattern: str
    download_url: str
    examples: list[LinkExample] = Field(min_length=1)

    @field_validator("pattern")
    @classmethod
    def _compiles(cls, value: str) -> str:
        try:
            compiled = re.compile(value)
        except re.error as exc:
            raise ValueError(f"pattern does not compile: {exc}") from None
        if "id" not in compiled.groupindex:
            raise ValueError("pattern needs a named group (?P<id>...)")
        return value

    @field_validator("download_url")
    @classmethod
    def _placeholders(cls, value: str) -> str:
        names = set(re.findall(r"\{([^{}]*)\}", value))
        unknown = names - {"base", "id"}
        if unknown:
            raise ValueError(f"download_url knows only {{base}} and {{id}}, not {sorted(unknown)}")
        if "id" not in names:
            raise ValueError("download_url needs {id}")
        return value

    @model_validator(mode="after")
    def _examples_match(self) -> ProviderLink:
        compiled = re.compile(self.pattern)
        for n, example in enumerate(self.examples):
            match = compiled.search(example.url)
            if not match:
                raise ValueError(f"examples[{n}]: pattern does not match the example url")
            if match.group("id") != example.id:
                raise ValueError(f"examples[{n}]: the id the pattern reads is not the example's id")
            if not TORRENT_ID.fullmatch(example.id):
                raise ValueError(
                    f"examples[{n}]: an id is letters, digits, - and _ only; "
                    "a link whose id holds anything else is not read"
                )
        return self


class Thank(Block):
    enabled: bool = False
    path: SitePath | None = None
    # LIVEWIRE: load `path` and press the page's Livewire `store` button
    # (UNIT3D thanks only that way).
    method: Literal["GET", "POST", "PUT", "LIVEWIRE"] = "POST"
    body: str | dict[str, Any] | None = None

    @model_validator(mode="after")
    def _path(self) -> Thank:
        if self.enabled and not self.path:
            raise ValueError("thank needs a path, e.g. /torrents/{id}/thank")
        return self


class KeepAlive(Block):
    enabled: bool = False
    every_minutes: int = Field(default=360, ge=5)
    # Each wait is every_minutes plus or minus a random value up to this, so
    # the visits do not land on a fixed beat. Never below one minute.
    jitter_minutes: int = Field(default=0, ge=0)
    path: SitePath = "/"


class ProviderGuard(Block):
    """When the app stops contacting a tracker that keeps failing."""

    # Consecutive error responses (400 and above, not 429) that park the tracker.
    error_limit: int = Field(default=3, ge=1)
    # The first pause after a failure; each further failure doubles it.
    backoff_minutes: int = Field(default=15, ge=1)
    # The longest pause, and the cap on a Retry-After the tracker sends.
    max_backoff_minutes: int = Field(default=360, ge=1)


class Provider(Block):
    key: Key
    base_url: Url
    # Other hosts of the same site. Credentials are only ever sent to
    # base_url's host and these.
    aliases: list[Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9.-]+(:\d+)?$")]] = Field(
        default_factory=list
    )
    auth: ProviderAuth = Field(default_factory=lambda: ProviderAuth(**{"from": "prowlarr"}))
    links: list[ProviderLink] = Field(default_factory=list)
    # The tracker's software: the direct search after Prowlarr finds nothing,
    # and the login form `auth.from: login` fills in.
    search: Literal["none", "unit3d", "xbtit", "nexusphp"] = "none"
    logged_out_marker: str | None = None
    thank: Thank = Field(default_factory=Thank)
    keep_alive: KeepAlive = Field(default_factory=KeepAlive)
    guard: ProviderGuard = Field(default_factory=ProviderGuard)


# --- integrations ----------------------------------------------------------


class Integration(Block):
    """A service the operator runs. A block that exists is enabled unless it says otherwise.

    Required fields are only required while the block is enabled, so a
    disabled block can keep ``${VAR}`` placeholders for variables not set yet.
    """

    enabled: bool = True
    REQUIRED: ClassVar[tuple[str, ...]] = ()

    @model_validator(mode="after")
    def _required_when_enabled(self) -> Integration:
        if self.enabled:
            missing = [name for name in self.REQUIRED if _blank(getattr(self, name))]
            if missing:
                raise ValueError(f"enabled, so it needs {', '.join(missing)}")
        return self


def _blank(value: Any) -> bool:
    if isinstance(value, SecretStr):
        value = value.get_secret_value()
    return value is None or (isinstance(value, str) and not value.strip())


class Prowlarr(Integration):
    REQUIRED = ("url", "api_key")
    url: Url | None = None
    api_key: SecretStr | None = None
    # Prowlarr category ids sent with a search, per media type.
    categories: dict[MediaType, list[int]] = Field(default_factory=dict)


class QBittorrent(Integration):
    REQUIRED = ("url",)
    url: Url | None = None
    # qBittorrent's own default web UI account name.
    username: str = QBIT_DEFAULT_ACCOUNT
    password: SecretStr | None = None
    categories: dict[MediaType, str] = Field(default_factory=dict)
    save_paths: dict[MediaType, str] = Field(default_factory=dict)


class Arr(Integration):
    REQUIRED = ("url", "api_key")
    url: Url | None = None
    api_key: SecretStr | None = None
    root_folder: str | None = None
    quality_profile: str | int | None = None


class Seerr(Integration):
    REQUIRED = ("url", "api_key")
    url: Url | None = None
    api_key: SecretStr | None = None
    # Seerr user the requests are made as (X-API-User). Without it a request
    # made with the API key is approved as the admin.
    user_id: int | None = None


class Tmdb(Integration):
    REQUIRED = ("api_key",)
    api_key: SecretStr | None = None
    language: str = "en-US"


class ImdbGuide(Integration):
    # A severity voted by fewer people than this is treated as missing.
    min_votes: int = Field(default=20, ge=0)


class GoogleBooks(Integration):
    api_key: SecretStr | None = None


class Jellyfin(Integration):
    REQUIRED = ("url", "api_key")
    url: Url | None = None
    api_key: SecretStr | None = None


class Integrations(Block):
    prowlarr: Prowlarr | None = None
    qbittorrent: QBittorrent | None = None
    radarr: Arr | None = None
    sonarr: Arr | None = None
    seerr: Seerr | None = None
    tmdb: Tmdb | None = None
    # Needs no key, so it is on unless a block turns it off.
    imdb_guide: ImdbGuide = Field(default_factory=ImdbGuide)
    google_books: GoogleBooks | None = None
    jellyfin: Jellyfin | None = None

    def enabled(self, name: str) -> bool:
        block = getattr(self, name, None)
        return block is not None and block.enabled


DeliverTarget = Literal["qbittorrent", "radarr", "sonarr", "seerr", "none"]


# --- policy ----------------------------------------------------------------


class TypePolicy(Block):
    mode: Literal["rules", "always", "never"] = "rules"
    when_data_missing: Literal["wait", "review", "approve"] = "wait"
    max_wait_hours: int = Field(default=24, ge=0)


_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_CERT = re.compile(r"^[A-Z]{2}:\S.*$")


_GROUPS: tuple[str, ...] = ("all", "any", "not")


class Condition(Block):
    """One check, or ``all``, ``any`` or ``not`` over other conditions.

    A leaf checks one signal with one operator against one value, as a rule
    always has. A group combines conditions, which may be groups themselves.
    ``split_vote`` and ``min_votes`` sit on the voted leaf they belong to.
    """

    check: str | None = None
    at_least: Any = None
    at_most: Any = None
    between: list[Any] | None = None
    is_: bool | None = Field(default=None, alias="is")
    any_of: list[str | int] | str | None = None
    none_of: list[str | int] | str | None = None
    matches_any: list[str | int] | str | None = None
    split_vote: float | None = Field(default=None, gt=0, le=1)
    min_votes: int | None = Field(default=None, ge=0)
    all_: list[Condition] | None = Field(default=None, alias="all")
    any_: list[Condition] | None = Field(default=None, alias="any")
    not_: Condition | None = Field(default=None, alias="not")

    @field_validator("check")
    @classmethod
    def _known_signal(cls, value: str | None) -> str | None:
        if value is None:
            return value
        name = canonical(value)
        if name is None:
            raise ValueError(f"unknown signal {value!r}; known signals: {', '.join(sorted(SIGNALS))}")
        return name

    @field_validator("all_", "any_", mode="before")
    @classmethod
    def _a_list(cls, value: Any, info: Any) -> Any:
        if value is not None and (not isinstance(value, list) or not value):
            raise ValueError(f"{info.field_name.rstrip('_')} takes a list of at least one condition")
        return value

    @field_validator("not_", mode="before")
    @classmethod
    def _one(cls, value: Any) -> Any:
        if value is not None and not isinstance(value, (dict, Condition)):
            raise ValueError("not takes one condition")
        return value

    @property
    def is_group(self) -> bool:
        return self.check is None

    @property
    def children(self) -> list[Condition]:
        """A group's conditions, in file order; a leaf has none."""
        if self.not_ is not None:
            return [self.not_]
        return list(self.all_ or self.any_ or [])

    def leaves(self) -> list[Condition]:
        """Every leaf, depth first, in file order: a leaf is its own only leaf."""
        return [leaf for _, leaf in self.leaves_at(())]

    def leaves_at(self, loc: tuple[Any, ...]) -> list[tuple[tuple[Any, ...], Condition]]:
        """Every leaf with its location under ``loc``, as config.yaml writes it: ``("all", 1, "any", 0)``."""
        if not self.is_group:
            return [(loc, self)]
        if self.not_ is not None:
            return self.not_.leaves_at((*loc, "not"))
        name = "all" if self.all_ is not None else "any"
        return [found for n, child in enumerate(self.children) for found in child.leaves_at((*loc, name, n))]

    @property
    def signal_types(self) -> tuple[str, ...]:
        """The media types every leaf's signal applies to, in ``MEDIA_TYPES`` order."""
        shared = set(MEDIA_TYPES)
        for leaf in self.leaves():
            shared &= set(SIGNALS[leaf.check].types)
        return tuple(t for t in MEDIA_TYPES if t in shared)

    @property
    def operator(self) -> str:
        return next(op for op in OPERATORS if self._value_of(op) is not None)

    @property
    def value(self) -> Any:
        return self._value_of(self.operator)

    def _value_of(self, op: str) -> Any:
        return getattr(self, "is_" if op == "is" else op)

    @model_validator(mode="after")
    def _one_shape(self) -> Condition:
        given = [name for name, value in zip(_GROUPS, (self.all_, self.any_, self.not_)) if value is not None]
        if len(given) + (self.check is not None) != 1:
            raise ValueError("a rule is one check, or one of all, any, not")
        used = [op for op in OPERATORS if self._value_of(op) is not None]
        if self.is_group:
            if self.split_vote is not None or self.min_votes is not None:
                raise ValueError(
                    "split_vote and min_votes go on the voted condition inside all, any or not, not on the rule"
                )
            if used:
                raise ValueError(f"{', '.join(used)} goes with a check inside {given[0]}, not beside it")
            return self
        signal = SIGNALS[self.check]
        if len(used) != 1:
            raise ValueError(
                f"a rule has exactly one operator; {self.check} takes one of: {', '.join(signal.operators)}"
                + (f" (found {', '.join(used)})" if used else "")
            )
        op = used[0]
        if op not in signal.operators:
            raise ValueError(f"{self.check} does not take {op}; it takes: {', '.join(signal.operators)}")
        if not signal.voted and (self.split_vote is not None or self.min_votes is not None):
            raise ValueError(f"split_vote and min_votes apply to voted signals (imdb.*), not {self.check}")
        setattr(self, "is_" if op == "is" else op, _check_value(signal.kind, op, self._value_of(op)))
        return self


class Rule(Condition):
    id: Key
    name: str | None = None
    # Empty means every type the rule's signals apply to.
    types: list[MediaType] = Field(default_factory=list)
    on_hit: Literal["approve", "review", "reject", "notify"] = "review"

    @property
    def media_types(self) -> tuple[str, ...]:
        """The types this rule is checked for: its own ``types``, else every type its signals share."""
        return tuple(self.types) or self.signal_types

    @model_validator(mode="after")
    def _types_fit(self) -> Rule:
        shared = self.signal_types
        if not shared:
            raise ValueError("the conditions of this rule share no media type")
        outside = [t for t in self.types if t not in shared]
        if outside:
            what = self.check if not self.is_group else "this rule"
            raise ValueError(f"{what} applies to {', '.join(shared)}, not {', '.join(outside)}")
        return self


def _check_value(kind: str, op: str, value: Any) -> Any:
    if op == "between":
        if len(value) != 2:
            raise ValueError("between takes [low, high]")
        if kind == "time":
            for v in value:
                if not isinstance(v, str) or not _HHMM.match(v):
                    raise ValueError("time_window takes HH:MM")
            return value
        if kind != "number":
            raise ValueError("between takes numbers")
        low, high = (_number(v) for v in value)
        if low > high:
            raise ValueError("between takes [low, high] with low <= high")
        return [low, high]
    if op == "is":
        return value
    if op in ("at_least", "at_most"):
        if kind == "severity":
            word = str(value).lower()
            if word not in SEVERITIES:
                raise ValueError(f"severity is one of {', '.join(SEVERITIES)}")
            return word
        if kind == "resolution":
            if str(value) not in RESOLUTIONS:
                raise ValueError(f"resolution is one of {', '.join(RESOLUTIONS)}")
            return str(value)
        if kind == "certification":
            return _certification(value)
        return _number(value)
    # any_of, none_of, matches_any: a list, or the name of a list under policy.lists.
    if isinstance(value, str):
        return value
    if not value:
        raise ValueError(f"{op} needs at least one value")
    if kind == "certification":
        return [_certification(v) for v in value]
    if kind == "resolution":
        for v in value:
            if str(v) not in RESOLUTIONS:
                raise ValueError(f"resolution is one of {', '.join(RESOLUTIONS)}")
    return value


def _number(value: Any) -> float | int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("expected a number")
    return value


def _certification(value: Any) -> str:
    if not isinstance(value, str) or not _CERT.match(value):
        raise ValueError("a certification is COUNTRY:rating, e.g. ES:18")
    return value


class TypeOverride(Block):
    """A scope's change to a type policy: only the fields it gives change."""

    mode: Literal["rules", "always", "never"] | None = None
    when_data_missing: Literal["wait", "review", "approve"] | None = None
    max_wait_hours: int | None = Field(default=None, ge=0)


class Scope(Block):
    """A policy for the items its ``when`` picks: by source, sender or clock.

    ``when`` reads only what is known before any lookup. ``types`` changes the
    base type policies field by field, ``rules`` are checked before the base
    rules, and ``skip`` leaves base rules out by id.
    """

    id: Key
    name: str | None = None
    when: Condition
    types: dict[MediaType, TypeOverride] = Field(default_factory=dict)
    rules: list[Rule] = Field(default_factory=list)
    skip: list[Key] = Field(default_factory=list)

    @field_validator("when")
    @classmethod
    def _known_before_lookup(cls, value: Condition) -> Condition:
        for leaf in value.leaves():
            if leaf.check not in SCOPE_SIGNALS:
                raise ValueError(
                    f"a scope's when reads what is known before any lookup ({', '.join(SCOPE_SIGNALS)}), "
                    f"not {leaf.check}"
                )
        return value


class Policy(Block):
    auto_approve: Literal["off", "shadow", "on"] = "off"
    # Default minimum votes for voted rules; unset falls back to
    # integrations.imdb_guide.min_votes.
    min_votes: int | None = Field(default=None, ge=0)
    types: dict[MediaType, TypePolicy] = Field(default_factory=dict)
    lists: dict[Key, list[Word]] = Field(default_factory=dict)
    rules: list[Rule] = Field(default_factory=list)
    # Checked in order; the first whose `when` holds decides the item.
    scopes: list[Scope] = Field(default_factory=list)

    @field_validator("auto_approve", mode="before")
    @classmethod
    def _yaml_bool(cls, value: Any) -> Any:
        # `on` and `off` are booleans in YAML 1.1 parsers; accept both shapes.
        if value is True:
            return "on"
        if value is False:
            return "off"
        return value

    def type_policy(self, media_type: str, scope: Scope | None = None) -> TypePolicy:
        """The type's policy, with the fields ``scope`` gives for it in place of the base ones."""
        base = self.types.get(media_type) or TypePolicy()
        override = scope.types.get(media_type) if scope is not None else None
        if override is None:
            return base
        return base.model_copy(update=override.model_dump(exclude_none=True))


# --- release words ---------------------------------------------------------

LanguageCode = Annotated[str, StringConstraints(pattern=r"^[a-z]{2,3}$")]


def _word_compiles(word: str) -> str:
    """A ``/regex/`` release word must compile the way it runs.

    It runs inside a group, joined to the other words of its list, and the
    subtitle pattern reads what follows through a group of its own. So a word
    is compiled inside a group, where a global flag such as ``(?i)`` fails,
    and may not capture: a group of the word's would be read as that tail, and
    a name written twice in one list would not compile once joined.
    """
    if len(word) > 2 and word.startswith("/") and word.endswith("/"):
        try:
            # Alone first: a stray parenthesis must not close the group around it.
            re.compile(word[1:-1])
            compiled = re.compile(f"(?:{word[1:-1]})", re.IGNORECASE)
        except re.error as exc:
            raise ValueError(f"pattern does not compile: {exc}") from None
        if compiled.groups:
            raise ValueError("a release word may not capture; write its group as (?:...)")
    return word


ReleaseWord = Annotated[Word, AfterValidator(_word_compiles)]


def _default_languages() -> dict[str, list[str]]:
    return {
        "es": ["spanish", "spa", "esp"],
        "en": ["english", "eng"],
        "fr": ["french", "truefrench", "vff", "vfq"],
        "de": ["german", "ger"],
        "it": ["italian", "ita"],
        "pt": ["portuguese"],
        "ja": ["japanese", "jap", "jpn"],
        "ko": ["korean", "kor"],
        "ca": ["catalan"],
    }


class ReleaseWords(Block):
    """How a post or a release name says a language, subtitles or a whole season.

    The defaults are English. A list written in the file replaces its default
    whole, so it holds every word the operator's posts use, English ones
    included. Words match case-insensitively and never inside a longer word;
    a space matches any run of whitespace; ``/.../`` is a regular expression.
    """

    # Language code -> the words that name it: the `audio_language` signal,
    # and the languages read after a subtitle word.
    languages: dict[LanguageCode, list[ReleaseWord]] = Field(default_factory=_default_languages)
    # A word that opens a list of subtitle languages ("Subs: English, French").
    subtitles: list[ReleaseWord] = Field(default_factory=lambda: ["sub", "subs", "subtitle", "subtitles"])
    # Language code -> tags that alone say the release is subtitled in it.
    subtitle_tags: dict[LanguageCode, list[ReleaseWord]] = Field(default_factory=dict)
    # A phrase that says a post is a whole season. `[PACK]` and a bare `S01`
    # are read by the app whatever this says.
    full_season: list[ReleaseWord] = Field(default_factory=lambda: ["complete", r"/\bseason\s+\d+\b/"])
    # The words a bracket of file formats is made of: "(MP3)", "[EPUB, PDF]".
    # A book's title is searched without such a bracket.
    format_tags: list[ReleaseWord] = Field(
        default_factory=lambda: ["mp3", "m4a", "m4b", "epub", "pdf", "mobi", "azw", "azw3", "cbr", "cbz"],
    )


# --- notify, web, telegram -------------------------------------------------


class Notify(Block):
    # An Apprise URL or a plain http(s) webhook. Usually carries a token.
    url: SecretStr


class WebAuth(Block):
    password_hash: SecretStr | None = None
    # Header set by a trusted reverse proxy carrying the signed-in user.
    trusted_header: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _one(self) -> WebAuth:
        if (self.password_hash is None) == (self.trusted_header is None):
            raise ValueError("auth takes exactly one of password_hash or trusted_header")
        return self


class Web(Block):
    bind: str = "0.0.0.0"
    port: int = Field(default=4850, ge=1, le=65535)
    auth: WebAuth | None = None
    # Public address, used for links in bot messages.
    base_url: Url | None = None


class Telegram(Block):
    api_id: int | None = None
    api_hash: SecretStr | None = None
    # Only asked for when the userbot session has to log in again.
    phone: SecretStr | None = None
    session_path: str = "/data/sessions/vettarr"
    bot_token: SecretStr | None = None
    # Telegram user ids allowed to approve, reject and use the bot.
    reviewers: list[int] = Field(default_factory=list)


# --- the whole file --------------------------------------------------------


class Config(Block):
    # Nothing outside this database is changed: every write to a client, a
    # tracker or Telegram becomes a `would_have` event (vettarr.dryrun).
    dry_run: bool = False
    sources: list[Source] = Field(default_factory=list)
    formats: list[FormatProfile] = Field(default_factory=list)
    providers: list[Provider] = Field(default_factory=list)
    integrations: Integrations = Field(default_factory=Integrations)
    deliver: dict[MediaType, DeliverTarget] = Field(default_factory=dict)
    policy: Policy = Field(default_factory=Policy)
    release_words: ReleaseWords = Field(default_factory=ReleaseWords)
    notify: Notify | None = None
    web: Web | None = None
    telegram: Telegram | None = None

    def deliver_to(self, media_type: str) -> str:
        return self.deliver.get(media_type, "none")


_DELIVER_TYPES = {
    "radarr": ("movie",),
    "sonarr": ("series",),
    "seerr": ("movie", "series"),
}


def cross_check(cfg: Config) -> list[tuple[Loc, str]]:
    """Errors that need more than one block. Each comes with its location."""
    errors: list[tuple[Loc, str]] = []

    for block in ("sources", "formats", "providers"):
        seen: set[str] = set()
        for n, item in enumerate(getattr(cfg, block)):
            if item.key in seen:
                errors.append(((block, n, "key"), f"duplicate key {item.key!r}"))
            seen.add(item.key)

    from vettarr.parser.formats import compile_errors

    errors.extend(compile_errors(cfg.formats))
    format_keys = {f.key for f in cfg.formats}
    tg = cfg.telegram
    for n, source in enumerate(cfg.sources):
        for m, key in enumerate(source.formats):
            if key not in format_keys:
                known = ", ".join(sorted(format_keys)) or "none defined"
                errors.append((("sources", n, "formats", m), f"no format {key!r}; formats: {known}"))
        if source.kind == "userbot" and not (tg and tg.api_id and tg.api_hash):
            errors.append((("sources", n, "kind"), "a userbot source needs telegram.api_id and telegram.api_hash"))
        if source.kind == "bot" and not (tg and tg.bot_token):
            errors.append((("sources", n, "kind"), "a bot source needs telegram.bot_token"))
        if source.kind == "seerr" and not cfg.integrations.enabled("seerr"):
            errors.append((("sources", n, "kind"), "a seerr source needs integrations.seerr"))

    for media_type, target in cfg.deliver.items():
        if target == "none":
            continue
        if not cfg.integrations.enabled(target):
            errors.append((("deliver", media_type), f"deliver to {target} needs integrations.{target} enabled"))
            continue
        allowed = _DELIVER_TYPES.get(target)
        if allowed and media_type not in allowed:
            errors.append((("deliver", media_type), f"{target} takes {', '.join(allowed)} only"))
        if target == "seerr" and cfg.integrations.seerr.user_id is None:
            errors.append((("integrations", "seerr"), "deliver to seerr needs user_id (the X-API-User)"))

    if cfg.web is not None and cfg.web.auth is not None and cfg.web.auth.password_hash is not None:
        from vettarr.web import passwords

        problem = passwords.problem(cfg.web.auth.password_hash.get_secret_value())
        if problem:
            errors.append((("web", "auth", "password_hash"), problem))

    for n, provider in enumerate(cfg.providers):
        if provider.auth.from_ == "prowlarr" and not cfg.integrations.enabled("prowlarr"):
            errors.append((("providers", n, "auth"), "auth from prowlarr needs integrations.prowlarr enabled"))

    policy = cfg.policy
    source_keys = {s.key for s in cfg.sources}

    def leaf_errors(found: list[tuple[Loc, Condition]]) -> None:
        for loc, leaf in found:
            if isinstance(leaf.value, str) and leaf.operator in ("any_of", "none_of", "matches_any"):
                if leaf.value not in policy.lists:
                    known = ", ".join(sorted(policy.lists)) or "none defined"
                    errors.append(
                        ((*loc, leaf.operator), f"no list {leaf.value!r} under policy.lists; lists: {known}")
                    )
                    continue
            if leaf.check == "source":
                values = policy.lists[leaf.value] if isinstance(leaf.value, str) else leaf.value
                if any(str(v) not in source_keys for v in values):
                    known = ", ".join(sorted(source_keys)) or "none defined"
                    errors.append(
                        ((*loc, leaf.operator),
                         f"source takes the keys of configured sources ({known}); the release's origin "
                         "(CAM, TS, WEB-DL, ...) is release_source")
                    )

    seen_rules: set[str] = set()
    for n, rule in enumerate(policy.rules):
        if rule.id in seen_rules:
            errors.append((("policy", "rules", n, "id"), f"duplicate rule id {rule.id!r}"))
        seen_rules.add(rule.id)
        leaf_errors(rule.leaves_at(("policy", "rules", n)))
    seen_scopes: set[str] = set()
    for n, scope in enumerate(policy.scopes):
        at = ("policy", "scopes", n)
        if scope.id in seen_scopes:
            errors.append(((*at, "id"), f"duplicate scope id {scope.id!r}"))
        seen_scopes.add(scope.id)
        leaf_errors(scope.when.leaves_at((*at, "when")))
        own: set[str] = set()
        for m, rule in enumerate(scope.rules):
            if rule.id in own:
                errors.append(((*at, "rules", m, "id"), f"duplicate rule id {rule.id!r}"))
            elif rule.id in seen_rules:
                errors.append(
                    ((*at, "rules", m, "id"),
                     f"rule id {rule.id!r} is already a base rule; a scope's rule needs its own id")
                )
            own.add(rule.id)
            leaf_errors(rule.leaves_at((*at, "rules", m)))
        for m, key in enumerate(scope.skip):
            if key not in seen_rules:
                known = ", ".join(r.id for r in policy.rules) or "none defined"
                errors.append(((*at, "skip", m), f"skip names no base rule {key!r}; base rules: {known}"))
    return errors


def warnings(cfg: Config) -> list[tuple[Loc, str]]:
    """Valid but probably not what the operator meant."""
    out: list[tuple[Loc, str]] = []
    policy = cfg.policy
    # The base rules, then each scope's own, each list warned about on its own.
    rule_lists = [(("policy", "rules"), policy.rules)]
    rule_lists += [(("policy", "scopes", n, "rules"), scope.rules) for n, scope in enumerate(policy.scopes)]
    for at, rules in rule_lists:
        approve_first: dict[str, str] = {}
        for n, rule in enumerate(rules):
            warned: set[str] = set()
            for loc, leaf in rule.leaves_at((*at, n)):
                need = GROUP_NEEDS.get(SIGNALS[leaf.check].group)
                if need and not cfg.integrations.enabled(need) and leaf.check not in warned:
                    warned.add(leaf.check)
                    out.append(
                        ((*at, n), f"rule {rule.id} reads {leaf.check} but integrations.{need} is not "
                         "enabled; the rule sees missing data")
                    )
                if (
                    isinstance(leaf.value, str)
                    and leaf.operator in ("any_of", "none_of", "matches_any")
                    and not policy.lists.get(leaf.value)
                ):
                    out.append(
                        ((*loc, leaf.operator),
                         f"rule {rule.id} points at the empty list {leaf.value!r}; it never hits")
                    )
            types = set(rule.media_types)
            if rule.on_hit == "approve":
                for media_type in types:
                    approve_first.setdefault(media_type, rule.id)
            elif rule.on_hit in ("review", "reject"):
                skipped = sorted({approve_first[t] for t in types if t in approve_first})
                if skipped:
                    out.append(
                        ((*at, n),
                         f"rule {rule.id} comes after the approve rule {', '.join(skipped)}; when that one hits "
                         f"first, {rule.id} is never checked. Put rules that review or reject first")
                    )
    # A scope's when that points at an empty list never applies.
    for n, scope in enumerate(policy.scopes):
        for loc, leaf in scope.when.leaves_at(("policy", "scopes", n, "when")):
            if (
                isinstance(leaf.value, str)
                and leaf.operator in ("any_of", "none_of", "matches_any")
                and not policy.lists.get(leaf.value)
            ):
                out.append(
                    ((*loc, leaf.operator),
                     f"scope {scope.id} points at the empty list {leaf.value!r}; it never applies")
                )
    if policy.auto_approve != "off":
        # The base policy, then each scope with its own types, rules and skip.
        for at, scope in [(("policy", "types"), None),
                          *((("policy", "scopes", n), scope) for n, scope in enumerate(policy.scopes))]:
            rules = policy.rules if scope is None else [
                *scope.rules, *(rule for rule in policy.rules if rule.id not in scope.skip)
            ]
            unchecked = [
                media_type for media_type in MEDIA_TYPES
                if policy.type_policy(media_type, scope).mode == "rules"
                and not any(media_type in rule.media_types for rule in rules)
            ]
            if unchecked:
                under = "" if scope is None else f"under the scope {scope.id}, "
                out.append(
                    (at,
                     f"{under}no rule applies to {', '.join(unchecked)}, so every such item is held for a person; "
                     "give the type a rule, or set its mode to always or never to say which you mean")
                )
    for n, provider in enumerate(cfg.providers):
        if provider.auth.from_ == "login" and provider.search == "none":
            out.append(
                (("providers", n, "auth"), "auth from login fills in the login form of the software `search` "
                 "names; with search: none the app cannot log in and uses cookie_file alone")
            )
        keep = provider.keep_alive
        if keep.enabled and keep.jitter_minutes >= keep.every_minutes:
            out.append(
                (("providers", n, "keep_alive"), "jitter_minutes is not below every_minutes, so many waits are "
                 "cut to the 1-minute floor and the visits come back to back; keep the jitter below the interval")
            )
    delivered = {target for target in cfg.deliver.values() if target != "none"}
    takers = [name for name in ("qbittorrent", "radarr", "sonarr") if cfg.integrations.enabled(name)]
    if takers and not cfg.deliver:
        out.append(
            (("deliver",), "deliver is empty, so no approved item is delivered anywhere, while "
             f"{', '.join(takers)} {'is' if len(takers) == 1 else 'are'} on. List each media type "
             "under deliver, with none for a type that should only be decided")
        )
    unlisted = [media_type for media_type in MEDIA_TYPES if media_type not in cfg.deliver]
    if cfg.deliver and unlisted:
        out.append(
            (("deliver",), f"deliver lists no target for {', '.join(unlisted)}, so an approved item of "
             f"{'that type' if len(unlisted) == 1 else 'those types'} comes back to you undelivered. "
             "Write none for a type that should only be decided")
        )
    for target in ("radarr", "sonarr"):
        if target not in delivered:
            continue
        block = getattr(cfg.integrations, target)
        unset = [name for name in ("root_folder", "quality_profile") if not getattr(block, name)]
        if unset:
            out.append(
                (("integrations", target), f"deliver uses {target}, and adding a title there needs "
                 f"{' and '.join(unset)}; without, every such item comes back to you")
            )
    if cfg.web is not None and cfg.web.auth is None:
        out.append((("web",), "the web UI has no auth; anyone who reaches the port can approve and reject"))
    if cfg.telegram and cfg.telegram.bot_token and not cfg.telegram.reviewers:
        out.append((("telegram",), "telegram.bot_token starts the bot but telegram.reviewers is empty; "
                    "the bot answers no one"))
    for n, source in enumerate(cfg.sources):
        if source.allowed_senders == "reviewers" and not (cfg.telegram and cfg.telegram.reviewers):
            out.append((("sources", n, "allowed_senders"), "allowed_senders is reviewers but telegram.reviewers is empty"))
    seerr_sources = [n for n, source in enumerate(cfg.sources) if source.kind == "seerr"]
    for n in seerr_sources:
        source = cfg.sources[n]
        if not cfg.integrations.enabled("tmdb"):
            out.append(
                (("sources", n), "a seerr source names each request from its TMDB id, and integrations.tmdb "
                 "is not enabled; no request is taken until it is")
            )
        if source.allowed_senders == "reviewers":
            out.append(
                (("sources", n, "allowed_senders"), "the sender of a Seerr request is a Seerr user id, never a "
                 "Telegram reviewer; no request is taken. Use anyone, or a list of Seerr user ids")
            )
        if source.webhook_secret is not None and cfg.web is None:
            out.append(
                (("sources", n, "webhook_secret"), "the webhook is served by the web UI, and there is no web "
                 "block; the source is read by the poll alone")
            )
    if len(seerr_sources) > 1:
        out.append(
            (("sources", seerr_sources[1]), "more than one seerr source reads the same Seerr; each request "
             "becomes the item of whichever source sees it first")
        )
    return out
