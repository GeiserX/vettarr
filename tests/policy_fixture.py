"""The operator-shaped policy the tests decide against, and ways to vary it.

`tests/fixtures/policy.yaml` holds a policy with the structure of a real
operator's (the same rule kinds, order and actions) and neutral stand-in
words. `config()` loads it, optionally with the 0.11 knobs the older tests
still speak in: the switch, the type list, the vote threshold and the wait.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from vettarr.config import Settings
from vettarr.config.loader import load
from vettarr.config.schema import Config, TypePolicy
from vettarr.config.signals import MEDIA_TYPES

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "policy.yaml"

# Placeholder words from the fixture, for tests that need a hit.
FLAGGED_KEYWORD = "lighthouse"
FLAGGED_GENRE_ID = 10402
SUBJECT_A = "Beekeeping"


def base_config() -> Config:
    return load(FIXTURE, {})


def config(
    *,
    enabled: bool | str = True,
    allowed_types=None,
    min_votes: int | None = 5,
    max_wait_hours: int = 24,
    **policy_changes,
) -> Config:
    cfg = base_config()
    policy = cfg.policy
    mode = enabled if isinstance(enabled, str) else ("on" if enabled else "off")
    types = dict(policy.types)
    if allowed_types is not None:
        allowed = {t.lower() for t in allowed_types}
        for media_type in MEDIA_TYPES:
            if media_type not in allowed:
                types[media_type] = TypePolicy(mode="never")
    for media_type, tp in list(types.items()):
        if tp.mode == "rules" and tp.when_data_missing == "wait":
            types[media_type] = tp.model_copy(update={"max_wait_hours": max_wait_hours})
    policy = policy.model_copy(update={
        "auto_approve": mode, "min_votes": min_votes, "types": types, **policy_changes,
    })
    return cfg.model_copy(update={"policy": policy})


def settings(**kwargs) -> Settings:
    return Settings(config(**kwargs))


def settings_stub(
    auto_approve_enabled: bool | str = True,
    auto_approve_types=None,
    min_guide_votes: int = 5,
    auto_approve_max_wait_hours: int = 24,
    **_ignored,
) -> Settings:
    """The 0.11 settings knobs, mapped onto the fixture policy."""
    return settings(
        enabled=auto_approve_enabled,
        allowed_types=auto_approve_types,
        min_votes=min_guide_votes,
        max_wait_hours=auto_approve_max_wait_hours,
    )


def item(**fields) -> SimpleNamespace:
    base = {
        "id": 1, "media_type": "movie", "title": "Example", "year": 2024,
        "created_at": None, "raw_text": "", "quality": None, "source": None,
        "size_text": None, "season": None, "release_group": None,
        "external_links": [], "telegram_chat_id": -1,
    }
    base.update(fields)
    return SimpleNamespace(**base)
