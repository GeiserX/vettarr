"""Is the TMDB record the title the post asked for?

The title metadata and the parents guide are read from whatever TMDB matched,
so a wrong match hands the policy another film's ratings. These checks
confirm the match before any of that data counts. The policy itself, rules
and all, is `vettarr.checker.policy`.
"""

from __future__ import annotations

from difflib import SequenceMatcher

from vettarr.checker.tmdb_client import (
    MATCH_YEAR_DROPPED,
    TMDBResult,
    normalise,
)

IDENTITY_UNCONFIRMED = "could not confirm this is the same title"

# Two posts of the same title can be in flight at once, and neither is in any
# of the statuses the duplicate check looks at. Approving both downloads it
# twice, so the later one goes to a human.
CONCURRENT_DUPLICATE = "duplicate of an item still being processed"

# A parsed post title and a TMDB record title count as the same work above
# this similarity, measured on accent-free, punctuation-free text.
#
# Calibrated against the shapes that actually occur. Above the line and right:
# punctuation and accents only ("Alien Romulus" / "Alien: Romulus", 1.00), an
# "and" for an "&" (0.88), a dropped leading article (0.92). Below the line and
# wrong: "Batman" against "The Batman" (0.75), "Halloween" against "Halloween
# Kills" (0.75), "Wicked" against "Wicked: Parte I" (0.60), and the live miss
# that started this, "Contra la ley" against "Ley y orden" (0.25). A remake
# scores 1.00 against its original, which is why the year is checked too.
TITLE_MATCH_THRESHOLD = 0.85

# TMDB dates a film by its first release, which can sit either side of the year
# a release group stamps on the file. Only ever applied to a post that carries
# a year; a post without one is held to the stricter rule below.
YEAR_TOLERANCE = 1

# What confirms the match, in the words of what was actually checked. A post
# with a year is confirmed on similarity plus the year. A post without one has
# no second signal, so it has to be an exact title and the only record TMDB
# returned under that title.
SAME_TITLE_AND_YEAR = "same title and year as the TMDB match"
SAME_TITLE_NO_YEAR = "same title, post has no year, single exact match"


def title_similarity(a: str | None, b: str | None) -> float:
    """How alike two titles are, ignoring case, accents and punctuation."""
    na, nb = normalise(a), normalise(b)
    if not na or not nb:
        return 0.0
    return SequenceMatcher(None, na, nb).ratio()


def _yearless_problem(tmdb: TMDBResult, post_title: str) -> str | None:
    """Why a post with no year cannot be trusted to this record, or None.

    Similarity alone is not enough here. "Gladiator" scores 86% against
    "Gladiator II" and would have been approved as the sequel; so would
    "Vaiana" against "Vaiana 2", "Deadpool" against "Deadpool 2" and
    "El Padrino" against "El Padrino II". Two things are required instead:
    the title has to be exactly the record's, and TMDB has to have returned
    exactly one record under that title. The second one is what separates
    "Suspiria" the 1977 film from "Suspiria" the 2018 remake, and the two
    unrelated series both called "Shameless".
    """
    shown = tmdb.title or tmdb.original_title or "?"
    wanted = normalise(post_title)
    if wanted not in {normalise(tmdb.title), normalise(tmdb.original_title)}:
        return (
            f"the post carries no year, and '{post_title}' is not exactly "
            f"'{shown}'"
        )
    if tmdb.exact_title_hits != 1:
        return (
            f"the post carries no year, and TMDB returned "
            f"{tmdb.exact_title_hits} records called '{shown}'"
        )
    return None


def identity_problem(
    tmdb: TMDBResult,
    post_title: str | None,
    post_year: int | None,
) -> str | None:
    """Why this TMDB record may not be the thing the post asked for, or None.

    The parental guide is fetched with the matched record's IMDb id, so a wrong
    match hands the policy another film's content ratings. Nothing gets
    approved by itself until the match is confirmed.
    """
    if tmdb.match_mode == MATCH_YEAR_DROPPED:
        return "nothing matched with the year, so the year was dropped to find it"

    if not (post_title or "").strip():
        return "the post carries no title to compare"

    best = max(
        title_similarity(post_title, tmdb.title),
        title_similarity(post_title, tmdb.original_title),
    )
    if best < TITLE_MATCH_THRESHOLD:
        return (
            f"'{post_title}' and '{tmdb.title or tmdb.original_title or '?'}' "
            f"are only {best:.0%} alike"
        )

    if post_year is None:
        return _yearless_problem(tmdb, post_title)

    if tmdb.year is None:
        return f"the match carries no year and the post says {post_year}"
    if abs(tmdb.year - post_year) > YEAR_TOLERANCE:
        return f"the match is from {tmdb.year} and the post says {post_year}"

    return None


def identity_reason(post_year: int | None) -> str:
    """What the confirmation actually rested on, for the audit trail."""
    return SAME_TITLE_AND_YEAR if post_year is not None else SAME_TITLE_NO_YEAR
