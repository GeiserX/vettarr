from __future__ import annotations

import enum

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.sql import func, text


class Base(DeclarativeBase):
    pass


class MediaType(str, enum.Enum):
    MOVIE = "movie"
    SERIES = "series"
    AUDIOBOOK = "audiobook"
    EBOOK = "ebook"
    MAGAZINE = "magazine"
    COMIC = "comic"
    MUSIC = "music"
    UNKNOWN = "unknown"


class ItemStatus(str, enum.Enum):
    PENDING_PARSE = "pending_parse"
    PARSED = "parsed"
    CHECKING = "checking"
    CHECKED = "checked"
    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    SEARCHING = "searching"
    DOWNLOADING = "downloading"
    COMPLETED = "completed"
    NO_MATCH = "no_match"
    PARSE_FAILED = "parse_failed"
    ERROR = "error"
    # Waiting for parental-guide data before the auto-approval policy can decide.
    AWAITING_DATA = "awaiting_data"
    # Not a media post at all (a note, a social link). Terminal: never reviewed,
    # never re-parsed, and its Telegram message is never deleted by the service.
    IGNORED = "ignored"


# Statuses a human may act on from the web UI or the bot.
REVIEWABLE_STATUSES = frozenset({
    ItemStatus.PENDING_REVIEW,
    ItemStatus.NO_MATCH,
    ItemStatus.PARSE_FAILED,
    ItemStatus.ERROR,
    ItemStatus.AWAITING_DATA,
})

# An auto-approved item can still be taken back until the download finishes.
# SEARCHING is excluded on purpose: the search worker owns that row.
#
# CHECKING is included: a content lookup takes as long as TMDB and IMDb take,
# and the operator should not have to wait for it to say no.
#
# Exactly two paths end a CHECKING window, and both close it with an UPDATE
# conditional on the row still being CHECKING -- `_write_checked_result` for
# the first pass through the pipeline, `_write_recheck_result` for the hourly
# re-check, which also matches on its own claim marker. When that UPDATE
# matches nothing the worker drops what it found and leaves the item alone, so
# a rejection taken mid-lookup is the one that stands. This used to be written
# here as though it were true of both, while the first pass wrote its decision
# straight through the loaded row and overwrote the rejection.
REJECTABLE_STATUSES = REVIEWABLE_STATUSES | {
    ItemStatus.CHECKING,
    ItemStatus.APPROVED,
    ItemStatus.DOWNLOADING,
}

# Statuses a finished download may move an item to COMPLETED from. A rejection
# landing inside a poll iteration used to be turned straight back into
# COMPLETED, because the poller marked the item done by id and nothing else.
COMPLETABLE_STATUSES = frozenset({ItemStatus.DOWNLOADING})

# Statuses whose Telegram message may be deleted when the item is rejected.
# PARSE_FAILED and IGNORED are deliberately absent: a post the service could
# not read is not a post the service gets to destroy.
DELETABLE_ON_REJECT = REJECTABLE_STATUSES - {ItemStatus.PARSE_FAILED}


OP_APPROVAL = "operation:approval"
# Stamped on an item the hourly re-check has claimed, so a restart can tell
# its CHECKING rows from the pipeline's and put them back where they came from.
OP_RECHECK = "operation:recheck"
OP_MANUAL_SEARCH = "operation:manual-search"
OP_MANUAL_DOWNLOAD = "operation:manual-download"
OP_RESCAN = "operation:rescan"
OP_REPARSE = "operation:reparse"


class Recommendation(str, enum.Enum):
    APPROVE = "approve"
    REJECT = "reject"
    NEUTRAL = "neutral"


class AutoDecision(str, enum.Enum):
    AUTO_APPROVE = "auto_approve"
    NEEDS_REVIEW = "needs_review"
    WAIT = "wait"


class SeverityRating(str, enum.Enum):
    NONE = "none"
    MILD = "mild"
    MODERATE = "moderate"
    SEVERE = "severe"


class DownloadStatus(str, enum.Enum):
    PENDING = "pending"
    DOWNLOADING = "downloading"
    COMPLETED = "completed"
    ERROR = "error"
    # The item was rejected while this download was live. Terminal: the
    # download poller only ever looks at DOWNLOADING rows, so a cancelled one
    # can never be flipped to COMPLETED and drag the item back with it.
    CANCELLED = "cancelled"


class MediaItem(Base):
    __tablename__ = "media_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    telegram_chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    raw_text: Mapped[str] = mapped_column(Text, nullable=False)
    #: The configured source the post came in through; NULL on rows written
    #: before sources existed, which belong to the first userbot source.
    source_key: Mapped[str | None] = mapped_column(Text)
    #: "<chat id>:<message id>" of the post this item stands for: the original
    #: of a forwarded channel post, otherwise the message itself. Unique, so a
    #: forward of a post already ingested is the same item, not a second one.
    origin_key: Mapped[str | None] = mapped_column(Text)
    #: The Telegram user id of whoever sent the message (for a forward, who
    #: forwarded it). NULL when no person did (a channel post, an anonymous
    #: admin) and on rows written before it was recorded.
    sender_id: Mapped[int | None] = mapped_column(BigInteger)
    #: When the message was sent, as Telegram dates it; `created_at` is when
    #: it was read, which for a backfilled post can be years later.
    posted_at: Mapped[str | None] = mapped_column(DateTime(timezone=True))

    media_type: Mapped[MediaType] = mapped_column(
        Enum(MediaType, name="mediatype"), default=MediaType.UNKNOWN
    )
    title: Mapped[str | None] = mapped_column(String(500))
    year: Mapped[int | None] = mapped_column(Integer)
    quality: Mapped[str | None] = mapped_column(String(50))
    source: Mapped[str | None] = mapped_column(String(50))
    size_text: Mapped[str | None] = mapped_column(String(100))
    season: Mapped[str | None] = mapped_column(String(20))
    release_group: Mapped[str | None] = mapped_column(String(100))
    external_links: Mapped[list | None] = mapped_column(JSONB, default=list)
    telegram_photo_path: Mapped[str | None] = mapped_column(String(200))

    status: Mapped[ItemStatus] = mapped_column(
        Enum(ItemStatus, name="itemstatus"), default=ItemStatus.PENDING_PARSE
    )
    auto_recommendation: Mapped[Recommendation] = mapped_column(
        Enum(Recommendation, name="recommendation"), default=Recommendation.NEUTRAL
    )
    auto_decision: Mapped[AutoDecision | None] = mapped_column(
        Enum(AutoDecision, name="autodecision")
    )
    auto_reasons: Mapped[list | None] = mapped_column(JSONB, default=list)
    auto_decided_at: Mapped[str | None] = mapped_column(DateTime(timezone=True))
    # The policy engine's record (checker/policy.py). `reasons` is a list of
    # {kind, rule, text}; NULL on rows decided before it, read as `legacy`.
    # `signals` is the snapshot the decision read, so a preview re-evaluates
    # the row without fetching anything. `policy_outcome` is what the policy
    # decided (approve, review, reject, wait), which under `shadow` or `off`
    # is not what was done.
    reasons: Mapped[list | None] = mapped_column(JSONB)
    policy_version: Mapped[str | None] = mapped_column(Text)
    signals: Mapped[dict | None] = mapped_column(JSONB)
    policy_outcome: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[str] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[str] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    reviewed_at: Mapped[str | None] = mapped_column(DateTime(timezone=True))

    content_check: Mapped[ContentCheck | None] = relationship(
        back_populates="media_item", uselist=False, cascade="all, delete-orphan"
    )
    torrent_searches: Mapped[list[TorrentSearch]] = relationship(
        back_populates="media_item", cascade="all, delete-orphan"
    )
    download: Mapped[Download | None] = relationship(
        back_populates="media_item", uselist=False, cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("telegram_message_id", "telegram_chat_id", name="uq_message_chat"),
        Index("ix_media_items_status", "status"),
        Index("ix_media_items_media_type", "media_type"),
        Index(
            "uq_media_items_origin_key", "origin_key", unique=True,
            postgresql_where=text("origin_key IS NOT NULL"),
        ),
    )


class ContentCheck(Base):
    __tablename__ = "content_checks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    media_item_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("media_items.id", ondelete="CASCADE"), unique=True, nullable=False
    )

    tmdb_id: Mapped[int | None] = mapped_column(Integer)
    tmdb_title: Mapped[str | None] = mapped_column(String(500))
    imdb_id: Mapped[str | None] = mapped_column(String(20))
    poster_url: Mapped[str | None] = mapped_column(String(500))
    backdrop_url: Mapped[str | None] = mapped_column(String(500))
    genres: Mapped[list | None] = mapped_column(JSONB, default=list)
    keywords: Mapped[list | None] = mapped_column(JSONB, default=list)
    plot: Mapped[str | None] = mapped_column(Text)

    certifications: Mapped[dict | None] = mapped_column(JSONB, default=dict)
    sex_rating: Mapped[SeverityRating | None] = mapped_column(
        Enum(SeverityRating, name="severityrating")
    )
    violence_rating: Mapped[SeverityRating | None] = mapped_column(
        Enum(SeverityRating, name="severityrating", create_constraint=False)
    )
    profanity_rating: Mapped[SeverityRating | None] = mapped_column(
        Enum(SeverityRating, name="severityrating", create_constraint=False)
    )
    frightening_rating: Mapped[SeverityRating | None] = mapped_column(
        Enum(SeverityRating, name="severityrating", create_constraint=False)
    )
    alcohol_rating: Mapped[SeverityRating | None] = mapped_column(
        Enum(SeverityRating, name="severityrating", create_constraint=False)
    )
    # IMDb parental-guide vote counts, category id -> totalSeverityVotes.
    guide_votes: Mapped[dict | None] = mapped_column(JSONB, default=dict)

    # Whether a title rule matched, and the words that did. The columns keep
    # the names they had before the project went public (migrations only add).
    title_rule_hit: Mapped[bool] = mapped_column("has_magic", Boolean, default=False)
    title_rule_words: Mapped[list | None] = mapped_column("magic_indicators", JSONB, default=list)

    checked_at: Mapped[str] = mapped_column(DateTime(timezone=True), server_default=func.now())

    media_item: Mapped[MediaItem] = relationship(back_populates="content_check")


class TorrentSearch(Base):
    __tablename__ = "torrent_searches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    media_item_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("media_items.id", ondelete="CASCADE"), nullable=False
    )

    prowlarr_results: Mapped[list | None] = mapped_column(JSONB, default=list)
    result_count: Mapped[int] = mapped_column(Integer, default=0)
    best_match_guid: Mapped[str | None] = mapped_column(String(500))
    best_match_title: Mapped[str | None] = mapped_column(String(500))
    best_match_size: Mapped[int | None] = mapped_column(BigInteger)
    best_match_seeders: Mapped[int | None] = mapped_column(Integer)
    best_match_url: Mapped[str | None] = mapped_column(String(1000))

    searched_at: Mapped[str] = mapped_column(DateTime(timezone=True), server_default=func.now())

    media_item: Mapped[MediaItem] = relationship(back_populates="torrent_searches")
    download: Mapped[Download | None] = relationship(
        back_populates="torrent_search", uselist=False
    )


class Download(Base):
    __tablename__ = "downloads"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    media_item_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("media_items.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    torrent_search_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("torrent_searches.id", ondelete="SET NULL")
    )

    qbittorrent_hash: Mapped[str | None] = mapped_column(String(100))
    #: True only when this item's own add is what put the torrent into
    #: qBittorrent. Rejecting an item deletes the torrent and its files, and
    #: that is only ours to do when we are the ones who added it. Rows written
    #: before this column existed default to False, which keeps a library copy
    #: that predates the column safe.
    #:
    #: For a delivery to Radarr, Sonarr or Seerr it says the same about what
    #: is there: True only when this item's delivery added the entry, turned
    #: its monitoring on, or made the request. Rejecting the item takes back
    #: only that.
    created_torrent: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    #: Where the approved item was handed, when that is not the download
    #: client: radarr, sonarr or seerr. NULL for the download client, on the
    #: rows written before delivery targets existed as on the ones after.
    delivery_target: Mapped[str | None] = mapped_column(Text)
    #: What the target calls it: Radarr's movie id, Sonarr's series id or
    #: Seerr's request id. NULL for the download client, whose handle is
    #: `qbittorrent_hash`.
    remote_id: Mapped[str | None] = mapped_column(Text)
    #: What this item's delivery turned on in Sonarr: `{"series": bool,
    #: "seasons": [numbers]}`. A series has one monitoring flag of its own and
    #: one per season, and `created_torrent` cannot say which of them were on
    #: before. Rejecting the item turns off exactly these. NULL everywhere
    #: else.
    remote_changes: Mapped[dict | None] = mapped_column(JSONB)
    magnet_url: Mapped[str | None] = mapped_column(Text)
    download_url: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str | None] = mapped_column(String(50))
    save_path: Mapped[str | None] = mapped_column(String(500))
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[DownloadStatus] = mapped_column(
        Enum(DownloadStatus, name="downloadstatus"), default=DownloadStatus.PENDING
    )

    started_at: Mapped[str | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[str | None] = mapped_column(DateTime(timezone=True))

    media_item: Mapped[MediaItem] = relationship(back_populates="download")
    torrent_search: Mapped[TorrentSearch | None] = relationship(back_populates="download")


class Event(Base):
    """One thing that happened: a decision, a guard change, an item that needs you.

    Written by `vettarr.events`, POSTed by `vettarr.notify`.
    """

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    at: Mapped[str] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    item_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("media_items.id", ondelete="SET NULL")
    )
    payload: Mapped[dict | None] = mapped_column(JSONB)

    __table_args__ = (Index("ix_events_at", "at"),)


def record_torrent_origin(download: Download, add_result) -> None:
    """Write down what qBittorrent did with the add, and where it came from.

    qBittorrent answers an add with the hash of a torrent it was already
    holding, so a recorded hash is not proof we put it there. `reject_item`
    reads `created_torrent` to decide whether the torrent and its files are
    ours to delete, which makes this two-line pair the difference between
    cleaning up after ourselves and deleting somebody's library copy.

    Every path that adds a torrent -- the search worker's Prowlarr and direct
    tracker paths, the web UI and the bot -- goes through here, so the rule
    lives in one place instead of four.
    """
    download.qbittorrent_hash = add_result.hash if add_result else None
    download.created_torrent = bool(add_result and add_result.created)
    # The row is the download client's from here on. An item handed to
    # Radarr, Sonarr or Seerr before keeps its row, and what that hand-over
    # wrote would send the poll there and make a rejection take back an entry
    # this torrent never made.
    download.delivery_target = None
    download.remote_id = None
    download.remote_changes = None


def record_delivery(
    download: Download, target: str, remote_id, created: bool, changes: dict | None = None,
) -> None:
    """Write down where an item was handed, and whether the handing made anything there.

    The download client's adds go through `record_torrent_origin`; this is
    the same pair of facts for Radarr, Sonarr and Seerr. `created` is what
    `reject_item` reads before it takes anything back: an entry that was
    already there and already monitored is not this item's to unmonitor.
    `changes` is Sonarr's finer record of the same (`Download.remote_changes`).

    An item handed to the same entry a second time (approved again after the
    target lost touch with it) finds there what its first hand-over made.
    That is still this item's, so the record is added to, never overwritten.
    """
    remote_id = str(remote_id) if remote_id is not None else None
    if download.delivery_target == target and download.remote_id == remote_id and download.created_torrent:
        before = download.remote_changes or {}
        if changes or before:
            changes = {
                "series": bool(before.get("series") or (changes or {}).get("series")),
                "seasons": sorted({*(before.get("seasons") or []), *((changes or {}).get("seasons") or [])}),
            }
        created = True
    download.delivery_target = target
    download.remote_id = remote_id
    download.qbittorrent_hash = None
    download.created_torrent = bool(created)
    download.remote_changes = changes if created else None
