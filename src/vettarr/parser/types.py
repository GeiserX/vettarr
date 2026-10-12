from __future__ import annotations

from dataclasses import dataclass, field

from vettarr.models import MediaType


@dataclass
class ParsedItem:
    media_type: MediaType = MediaType.UNKNOWN
    title: str | None = None
    year: int | None = None
    quality: str | None = None
    source: str | None = None
    size_text: str | None = None
    season: str | None = None
    release_group: str | None = None
    external_links: list[str] = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        return self.title is not None and self.media_type != MediaType.UNKNOWN
