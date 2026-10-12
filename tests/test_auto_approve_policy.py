"""The auto-approval policy, rule by rule, against an operator-shaped policy.

Every case here is a decision the operator would otherwise have to make by
hand, so the policy is tested as a pure function: no database, no network, no
clock. The rules come from tests/fixtures/policy.yaml, which has the
structure of a real operator's policy with neutral stand-in words.
"""

import logging
import os
import unittest
from datetime import datetime, timedelta, timezone

logging.disable(logging.CRITICAL)

for name, value in {
    "TELEGRAM_API_ID": "1",
    "TELEGRAM_API_HASH": "test",
    "TELEGRAM_PHONE": "+10000000000",
    "TELEGRAM_GROUP_ID": "-1",
    "TELEGRAM_BOT_TOKEN": "test",
    "TELEGRAM_OWNER_ID": "1",
    "TMDB_API_KEY": "test",
    "PROWLARR_URL": "http://prowlarr",
    "PROWLARR_API_KEY": "test",
    "QBITTORRENT_URL": "http://qbittorrent",
}.items():
    os.environ.setdefault(name, value)

import policy_fixture
from policy_fixture import FLAGGED_GENRE_ID, FLAGGED_KEYWORD, SUBJECT_A

from vettarr.checker import gather
from vettarr.checker.book_client import BookVerdict
from vettarr.checker.content_rules import (
    CONCURRENT_DUPLICATE,
    IDENTITY_UNCONFIRMED,
    SAME_TITLE_AND_YEAR,
    SAME_TITLE_NO_YEAR,
    YEAR_TOLERANCE,
)
from vettarr.checker.imdb_client import ParentsGuide
from vettarr.checker.policy import (
    APPROVE,
    AUTO_APPROVE_OFF,
    NO_TITLE,
    REJECT,
    REVIEW,
    WAIT,
    act,
    check_rule,
    evaluate,
)
from vettarr.checker.tmdb_client import (
    MATCH_NO_YEAR,
    MATCH_WITH_YEAR,
    MATCH_YEAR_DROPPED,
    TMDBResult,
)
from vettarr.config.schema import Rule
from vettarr.models import MediaType, SeverityRating

ALL_TYPES = ("movie", "series", "magazine", "ebook", "audiobook", "comic")

CREATED = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)

POST_TITLE = "Example"
POST_YEAR = 2024


def tmdb(**kwargs) -> TMDBResult:
    defaults = {
        "tmdb_id": 42,
        "imdb_id": "tt0000042",
        "title": POST_TITLE,
        "original_title": POST_TITLE,
        "year": POST_YEAR,
        "match_mode": MATCH_WITH_YEAR,
        "keywords": ["based on a novel"],
        # One record came back under exactly this title, which is what a post
        # with no year needs before it can be confirmed.
        "exact_title_hits": 1,
    }
    defaults.update(kwargs)
    return TMDBResult(**defaults)


def guide(
    sex=SeverityRating.NONE,
    violence=SeverityRating.NONE,
    frightening=SeverityRating.NONE,
    sex_votes=100,
    violence_votes=100,
    frightening_votes=100,
    fetched=True,
    breakdown=None,
) -> ParentsGuide:
    return ParentsGuide(
        sex_rating=sex,
        violence_rating=violence,
        frightening_rating=frightening,
        fetched=fetched,
        votes={
            "NUDITY": sex_votes,
            "VIOLENCE": violence_votes,
            "FRIGHTENING": frightening_votes,
        },
        breakdown=breakdown or {},
    )


class Result:
    """What the pipeline does with a decision, in the shape these tests read."""

    def __init__(self, applied, decision):
        self.applied = applied
        self.decision = applied.outcome
        self.policy_outcome = decision.outcome
        self.reasons = tuple(r.text for r in applied.reasons)

    @property
    def approved(self) -> bool:
        return self.decision == APPROVE


def decide(**kwargs) -> Result:
    args = {
        "media_type": MediaType.MOVIE,
        "enabled": True,
        "allowed_types": ALL_TYPES,
        "duplicate_of": None,
        "concurrent_duplicate": False,
        "tmdb": tmdb(),
        "parents_guide": guide(),
        "book": None,
        "created_at": CREATED,
        "now": CREATED + timedelta(minutes=5),
        "min_guide_votes": 5,
        "max_wait_hours": 24,
        "post_title": POST_TITLE,
        "post_year": POST_YEAR,
    }
    args.update(kwargs)
    cfg = policy_fixture.config(
        enabled=args["enabled"],
        allowed_types=args["allowed_types"],
        min_votes=args["min_guide_votes"],
        max_wait_hours=args["max_wait_hours"],
    )
    item = policy_fixture.item(
        media_type=args["media_type"], title=args["post_title"], year=args["post_year"],
        created_at=args["created_at"],
    )
    snapshot = gather.base_signals(
        item, cfg,
        tmdb=args["tmdb"], guide=args["parents_guide"], book=args["book"],
        book_looked_up=args["book"] is not None,
        duplicate_of=args["duplicate_of"],
        twin_of="earlier post" if args["concurrent_duplicate"] else None,
        now=args["now"] or CREATED,
    )
    decision = evaluate(item, snapshot, cfg.policy, now=args["now"])
    return Result(act(decision, cfg.policy.auto_approve), decision)


def reason_with(result: Result, fragment: str) -> bool:
    return any(fragment in reason for reason in result.reasons)


class MissingRatingGuardTests(unittest.TestCase):
    """The one check that stands between "unknown" and "clean"."""

    RULE = Rule.model_validate({"id": "n", "check": "imdb.nudity", "at_least": "moderate"})

    def test_an_unknown_rating_never_passes(self) -> None:
        for value in (None, {"severity": None, "votes": 100}, {"missing": "no guide"}):
            with self.subTest(value=value):
                hit, _ = check_rule(self.RULE, value, {}, 5)
                self.assertIsNone(hit)

    def test_a_known_allowed_rating_passes(self) -> None:
        hit, _ = check_rule(self.RULE, {"severity": "mild", "votes": 100}, {}, 5)
        self.assertIs(hit, False)

    def test_a_known_disallowed_rating_does_not_pass(self) -> None:
        hit, _ = check_rule(self.RULE, {"severity": "severe", "votes": 100}, {}, 5)
        self.assertIs(hit, True)


class SwitchTests(unittest.TestCase):
    """Deploying the image must change nothing until the switch is flipped."""

    def test_disabled_never_approves_a_clean_film(self) -> None:
        result = decide(enabled=False)
        self.assertEqual(result.decision, REVIEW)
        self.assertIn(AUTO_APPROVE_OFF, result.reasons)
        # The policy still decided, and says what it would have done.
        self.assertEqual(result.policy_outcome, APPROVE)

    def test_disabled_never_approves_a_magazine(self) -> None:
        result = decide(enabled=False, media_type=MediaType.MAGAZINE)
        self.assertEqual(result.decision, REVIEW)

    def test_type_outside_the_allow_list_is_reviewed(self) -> None:
        result = decide(allowed_types=("magazine",))
        self.assertEqual(result.decision, REVIEW)
        self.assertIn("movie is always reviewed by hand", result.reasons)

    def test_allow_list_is_case_insensitive(self) -> None:
        self.assertTrue(decide(allowed_types=("MOVIE",)).approved)


class DuplicateTests(unittest.TestCase):
    def test_duplicate_is_never_auto_approved(self) -> None:
        result = decide(duplicate_of="DB: Example")
        self.assertEqual(result.decision, REVIEW)
        self.assertEqual(result.reasons, ("duplicate of DB: Example",))

    def test_duplicate_magazine_is_never_auto_approved(self) -> None:
        result = decide(media_type=MediaType.MAGAZINE, duplicate_of="qBt: Example")
        self.assertEqual(result.decision, REVIEW)

    def test_a_concurrent_twin_is_reviewed(self) -> None:
        result = decide(concurrent_duplicate=True)
        self.assertEqual(result.decision, REVIEW)
        self.assertEqual(result.reasons, (CONCURRENT_DUPLICATE,))


class MagazineTests(unittest.TestCase):
    def test_magazines_are_always_allowed(self) -> None:
        result = decide(media_type=MediaType.MAGAZINE, tmdb=None, parents_guide=None)
        self.assertEqual(result.decision, APPROVE)
        self.assertIn("magazine is always approved", result.reasons)


class VideoContentTests(unittest.TestCase):
    def test_clean_film_is_approved(self) -> None:
        self.assertTrue(decide().approved)

    def test_clean_series_is_approved(self) -> None:
        self.assertTrue(decide(media_type=MediaType.SERIES).approved)

    def test_mild_sex_is_approved(self) -> None:
        self.assertTrue(decide(parents_guide=guide(sex=SeverityRating.MILD)).approved)

    def test_moderate_sex_needs_review(self) -> None:
        result = decide(parents_guide=guide(sex=SeverityRating.MODERATE))
        self.assertEqual(result.decision, REVIEW)
        self.assertIn("imdb.nudity: moderate", result.reasons)

    def test_severe_sex_needs_review(self) -> None:
        result = decide(parents_guide=guide(sex=SeverityRating.SEVERE))
        self.assertEqual(result.decision, REVIEW)
        self.assertIn("imdb.nudity: severe", result.reasons)

    def test_moderate_violence_is_approved(self) -> None:
        self.assertTrue(
            decide(parents_guide=guide(violence=SeverityRating.MODERATE)).approved
        )

    def test_severe_violence_is_rejected(self) -> None:
        """The operator's violence rule rejects (decision 3); it never deletes the post."""
        result = decide(parents_guide=guide(violence=SeverityRating.SEVERE))
        self.assertEqual(result.decision, REJECT)
        self.assertIn("imdb.violence: severe", result.reasons)

    def test_a_flagged_genre_needs_review(self) -> None:
        result = decide(tmdb=tmdb(genre_ids=[FLAGGED_GENRE_ID]))
        self.assertEqual(result.decision, REVIEW)
        self.assertIn(f"genre: {FLAGGED_GENRE_ID}", result.reasons)

    def test_the_genre_rule_reads_films_only(self) -> None:
        """The rule names `types: [movie]`; a series with that genre id is not its business."""
        result = decide(media_type=MediaType.SERIES, tmdb=tmdb(genre_ids=[FLAGGED_GENRE_ID]))
        self.assertTrue(result.approved)

    def test_a_flagged_keyword_needs_review(self) -> None:
        result = decide(tmdb=tmdb(keywords=[f"old {FLAGGED_KEYWORD} keeper"]))
        self.assertEqual(result.decision, REVIEW)
        self.assertIn(f"keyword: old {FLAGGED_KEYWORD} keeper", result.reasons)

    def test_adult_certification_needs_review(self) -> None:
        result = decide(tmdb=tmdb(certifications={"ES": "18"}))
        self.assertEqual(result.decision, REVIEW)
        self.assertIn("certification: ES:18", result.reasons)

    def test_ordinary_certification_is_approved(self) -> None:
        self.assertTrue(decide(tmdb=tmdb(certifications={"ES": "12"})).approved)

    def test_several_rules_trip_at_once(self) -> None:
        result = decide(
            tmdb=tmdb(keywords=[FLAGGED_KEYWORD]),
            parents_guide=guide(sex=SeverityRating.SEVERE),
        )
        self.assertEqual(result.decision, REVIEW)
        self.assertEqual(len(result.reasons), 2)

    def test_a_reject_hit_outranks_a_review_hit(self) -> None:
        result = decide(
            tmdb=tmdb(keywords=[FLAGGED_KEYWORD]),
            parents_guide=guide(violence=SeverityRating.SEVERE),
        )
        self.assertEqual(result.decision, REJECT)
        self.assertEqual(result.reasons[0], "imdb.violence: severe")


class MissingDataTests(unittest.TestCase):
    """Missing data must never read as clean.

    Before the GraphQL fix every severity in the database was NULL, and a large
    share of the items that looked unflagged were rejected by hand."""

    def test_no_tmdb_match_waits(self) -> None:
        result = decide(tmdb=None, parents_guide=ParentsGuide())
        self.assertEqual(result.decision, WAIT)
        self.assertTrue(reason_with(result, "no TMDB match"))

    def test_tmdb_match_without_imdb_id_waits(self) -> None:
        result = decide(tmdb=tmdb(imdb_id=None), parents_guide=ParentsGuide())
        self.assertEqual(result.decision, WAIT)
        self.assertTrue(reason_with(result, "no TMDB match with an IMDb id"))

    def test_guide_not_fetched_waits(self) -> None:
        result = decide(parents_guide=ParentsGuide(error="http 403"))
        self.assertEqual(result.decision, WAIT)
        self.assertTrue(reason_with(result, "parents guide not fetched"))

    def test_no_guide_object_waits(self) -> None:
        self.assertEqual(decide(parents_guide=None).decision, WAIT)

    def test_empty_guide_never_approves(self) -> None:
        """A guide with every rating None is what every stored check used to look like."""
        result = decide(parents_guide=ParentsGuide(fetched=True))
        self.assertEqual(result.decision, WAIT)

    def test_missing_severity_with_enough_votes_waits(self) -> None:
        result = decide(parents_guide=guide(sex=None))
        self.assertEqual(result.decision, WAIT)
        self.assertIn("imdb.nudity: no severity recorded", result.reasons)

    def test_a_tripped_rule_beats_missing_data(self) -> None:
        result = decide(
            tmdb=tmdb(genre_ids=[FLAGGED_GENRE_ID]),
            parents_guide=ParentsGuide(),
        )
        self.assertEqual(result.decision, REVIEW)
        self.assertFalse(reason_with(result, "parents guide not fetched"))


class VoteThresholdTests(unittest.TestCase):
    def test_one_vote_short_waits(self) -> None:
        result = decide(parents_guide=guide(sex_votes=4), min_guide_votes=5)
        self.assertEqual(result.decision, WAIT)
        self.assertIn("imdb.nudity: only 4 votes, need 5", result.reasons)

    def test_exactly_the_threshold_is_enough(self) -> None:
        self.assertTrue(
            decide(parents_guide=guide(sex_votes=5, violence_votes=5), min_guide_votes=5).approved
        )

    def test_zero_votes_waits(self) -> None:
        result = decide(parents_guide=guide(sex_votes=0, violence_votes=0))
        self.assertEqual(result.decision, WAIT)
        self.assertEqual(len(result.reasons), 2)

    def test_violence_votes_are_counted_separately(self) -> None:
        result = decide(parents_guide=guide(violence_votes=1), min_guide_votes=5)
        self.assertEqual(result.decision, WAIT)
        self.assertIn("imdb.violence: only 1 votes, need 5", result.reasons)

    def test_a_thin_vote_count_cannot_hide_a_severe_rating(self) -> None:
        """Too few votes waits; it does not quietly approve."""
        result = decide(
            parents_guide=guide(sex=SeverityRating.SEVERE, sex_votes=1),
            min_guide_votes=5,
        )
        self.assertNotEqual(result.decision, APPROVE)


class WaitExpiryTests(unittest.TestCase):
    """The clock is injected, so the 24-hour rule is tested without waiting."""

    def test_just_under_the_limit_still_waits(self) -> None:
        result = decide(
            parents_guide=ParentsGuide(),
            now=CREATED + timedelta(hours=23, minutes=59),
        )
        self.assertEqual(result.decision, WAIT)

    def test_at_the_limit_it_is_handed_back(self) -> None:
        result = decide(parents_guide=ParentsGuide(), now=CREATED + timedelta(hours=24))
        self.assertEqual(result.decision, REVIEW)
        self.assertEqual(result.reasons[0], "no data after 24 hours")

    def test_the_limit_is_configurable(self) -> None:
        result = decide(
            parents_guide=ParentsGuide(),
            now=CREATED + timedelta(hours=7),
            max_wait_hours=6,
        )
        self.assertEqual(result.decision, REVIEW)
        self.assertEqual(result.reasons[0], "no data after 6 hours")

    def test_a_naive_timestamp_is_read_as_utc(self) -> None:
        result = decide(
            parents_guide=ParentsGuide(),
            created_at=CREATED.replace(tzinfo=None),
            now=CREATED + timedelta(hours=25),
        )
        self.assertEqual(result.decision, REVIEW)

    def test_without_a_clock_it_keeps_waiting(self) -> None:
        result = decide(parents_guide=ParentsGuide(), created_at=None, now=None)
        self.assertEqual(result.decision, WAIT)

    def test_expiry_does_not_touch_a_clean_item(self) -> None:
        self.assertTrue(decide(now=CREATED + timedelta(days=30)).approved)


class BookTests(unittest.TestCase):
    def test_clean_book_is_approved(self) -> None:
        verdict = BookVerdict(
            matched=True, source="openlibrary", matched_title="El nombre del viento",
            subjects=["Fiction", "Adventure", "Spanish literature"],
        )
        result = decide(media_type=MediaType.EBOOK, book=verdict, allowed_types=ALL_TYPES)
        self.assertEqual(result.decision, APPROVE)
        self.assertIn("no rule hit (3 checked)", result.reasons)

    def test_a_listed_subject_needs_review(self) -> None:
        verdict = BookVerdict(
            matched=True, source="openlibrary", matched_title="X",
            subjects=[SUBJECT_A, "Essays", "History"],
        )
        result = decide(media_type=MediaType.EBOOK, book=verdict)
        self.assertEqual(result.decision, REVIEW)
        self.assertIn(f"subject: {SUBJECT_A}", result.reasons)

    def test_a_subject_inside_a_longer_one_needs_review(self) -> None:
        verdict = BookVerdict(
            matched=True, source="googlebooks", matched_title="X",
            subjects=["Knitting patterns", "Adventure", "Fiction"],
        )
        result = decide(media_type=MediaType.COMIC, book=verdict)
        self.assertEqual(result.decision, REVIEW)
        self.assertIn("subject: Knitting patterns", result.reasons)

    def test_no_confident_match_approves_the_book(self) -> None:
        """Review is for what a check flags. A book no catalogue knows is the
        normal case, not a flag: the operator's books approve on missing data."""
        result = decide(media_type=MediaType.AUDIOBOOK, book=BookVerdict())
        self.assertEqual(result.decision, APPROVE)
        self.assertIn(
            "subject: no catalogue classification (no confident title match)", result.reasons
        )

    def test_a_match_with_no_subjects_approves_the_book(self) -> None:
        verdict = BookVerdict(matched=True, matched_title="X", subjects=[])
        result = decide(media_type=MediaType.EBOOK, book=verdict)
        self.assertEqual(result.decision, APPROVE)
        self.assertIn("subject: only 0 known, too few to clear it", result.reasons)

    def test_a_failed_lookup_approves_the_book(self) -> None:
        """Keyless Google Books answers 429 all day. The detail stays in the
        reason so the audit trail says why the catalogue was silent."""
        result = decide(media_type=MediaType.EBOOK, book=BookVerdict(error="http 429"))
        self.assertEqual(result.decision, APPROVE)
        self.assertIn("subject: no catalogue classification (lookup failed: http 429)", result.reasons)

    def test_no_lookup_at_all_approves_the_book(self) -> None:
        result = decide(media_type=MediaType.EBOOK, book=None)
        self.assertEqual(result.decision, APPROVE)
        self.assertIn("subject: no catalogue classification (no lookup was run)", result.reasons)

    def test_a_thin_subject_list_approves_the_book(self) -> None:
        """Google Books often answers with one coarse category. "Fiction" on
        its own rules out nothing, and nothing is not a flag."""
        for subjects in ([], ["Fiction"], ["Fiction", "Adventure"]):
            with self.subTest(subjects=subjects):
                verdict = BookVerdict(
                    matched=True, source="googlebooks", matched_title="X",
                    subjects=list(subjects),
                )
                result = decide(
                    media_type=MediaType.EBOOK, book=verdict, allowed_types=ALL_TYPES
                )
                self.assertEqual(result.decision, APPROVE)
                self.assertIn(
                    f"subject: only {len(subjects)} known, too few to clear it",
                    result.reasons,
                )

    def test_a_flagged_subject_needs_review_however_thin_the_list(self) -> None:
        """One subject is too few to clear a book, but plenty to flag one."""
        verdict = BookVerdict(
            matched=True, source="googlebooks", matched_title="X", subjects=[SUBJECT_A],
        )
        result = decide(media_type=MediaType.EBOOK, book=verdict)
        self.assertEqual(result.decision, REVIEW)
        self.assertEqual(result.reasons, (f"subject: {SUBJECT_A}",))

    def test_the_title_is_not_read_for_flags(self) -> None:
        """A word in the title says too little to hold a book on, so only the
        catalogue's subjects can flag one."""
        for title in (
            "Beekeeping para principiantes (MP3-Narrated) - Ana Ejemplo",
            "Knitting basics - Autor Ejemplo",
            "Origami facil - Autor Ejemplo",
        ):
            with self.subTest(title=title):
                result = decide(
                    media_type=MediaType.AUDIOBOOK,
                    book=BookVerdict(error="http 429"),
                    post_title=title,
                )
                self.assertEqual(result.decision, APPROVE)
                self.assertTrue(
                    all("lookup failed: http 429" in reason for reason in result.reasons)
                )

    def test_no_title_at_all_cannot_be_approved(self) -> None:
        """Nothing to read is not the same as nothing flagged."""
        for title in (None, "", "   "):
            with self.subTest(title=title):
                result = decide(media_type=MediaType.EBOOK, book=None, post_title=title)
                self.assertEqual(result.decision, REVIEW)
                self.assertEqual(result.reasons[0], NO_TITLE)

    def test_three_subjects_is_enough(self) -> None:
        verdict = BookVerdict(
            matched=True, source="openlibrary", matched_title="X",
            subjects=["Fiction", "Adventure", "Spanish literature"],
        )
        self.assertTrue(
            decide(
                media_type=MediaType.EBOOK, book=verdict, allowed_types=ALL_TYPES
            ).approved
        )

    def test_books_are_off_unless_they_are_asked_for(self) -> None:
        """A type set to `never` is a review even for a spotless verdict."""
        verdict = BookVerdict(
            matched=True, source="openlibrary", matched_title="X",
            subjects=["Fiction", "Adventure", "Spanish literature"],
        )
        result = decide(
            media_type=MediaType.EBOOK,
            book=verdict,
            allowed_types=("movie", "series", "magazine"),
        )
        self.assertEqual(result.decision, REVIEW)

    def test_books_never_wait(self) -> None:
        """There is no book equivalent of the parental guide arriving later."""
        for verdict in (None, BookVerdict(), BookVerdict(error="timeout")):
            for title in ("Un libro cualquiera", "Origami facil"):
                with self.subTest(verdict=verdict, title=title):
                    result = decide(
                        media_type=MediaType.EBOOK, book=verdict, post_title=title,
                        now=CREATED + timedelta(days=30),
                    )
                    self.assertNotEqual(result.decision, WAIT)


class HandReviewedTypeTests(unittest.TestCase):
    def test_music_is_always_reviewed(self) -> None:
        result = decide(
            media_type=MediaType.MUSIC, allowed_types=(*ALL_TYPES, "music")
        )
        self.assertEqual(result.decision, REVIEW)
        self.assertIn("music is always reviewed by hand", result.reasons)

    def test_unknown_is_always_reviewed(self) -> None:
        result = decide(
            media_type=MediaType.UNKNOWN, allowed_types=(*ALL_TYPES, "unknown")
        )
        self.assertEqual(result.decision, REVIEW)


class IdentityTests(unittest.TestCase):
    """The parental guide belongs to whatever TMDB matched, so the match has
    to be the thing the post asked for before any of it counts.

    The live miss: a post titled "Contra la ley" resolved to the long-running
    American series "Law & Order", and the policy would have read that show's
    ratings as this film's.
    """

    def test_the_same_title_and_year_is_confirmed(self) -> None:
        self.assertTrue(decide().approved)

    def test_a_different_title_is_never_approved(self) -> None:
        result = decide(
            post_title="Contra la ley",
            tmdb=tmdb(title="Ley y orden", original_title="Law & Order", year=1990),
            post_year=None,
        )
        self.assertEqual(result.decision, REVIEW)
        self.assertEqual(result.reasons[0], IDENTITY_UNCONFIRMED)

    def test_a_failed_identity_check_never_waits(self) -> None:
        """Even with no parental-guide data at all, this is a decision for a
        human, not something to sit on for a day."""
        result = decide(
            post_title="Contra la ley",
            tmdb=tmdb(title="Ley y orden", original_title="Law & Order"),
            parents_guide=ParentsGuide(),
            post_year=None,
        )
        self.assertEqual(result.decision, REVIEW)
        self.assertEqual(result.reasons[0], IDENTITY_UNCONFIRMED)

    def test_the_original_title_counts_when_the_localised_one_does_not(self) -> None:
        result = decide(
            post_title="The Lord of the Rings The Fellowship of the Ring",
            post_year=2001,
            tmdb=tmdb(
                title="El Señor de los Anillos: La Comunidad del Anillo",
                original_title="The Lord of the Rings: The Fellowship of the Ring",
                year=2001,
            ),
        )
        self.assertTrue(result.approved)

    def test_accents_and_punctuation_do_not_break_a_match(self) -> None:
        result = decide(
            post_title="Mision Imposible Sentencia Mortal",
            post_year=2023,
            tmdb=tmdb(
                title="Misión: Imposible - Sentencia Mortal",
                original_title="Mission: Impossible - Dead Reckoning",
                year=2023,
            ),
        )
        self.assertTrue(result.approved)

    def test_a_year_one_off_is_still_the_same_film(self) -> None:
        """TMDB dates a film by its first release; a group stamps the local one."""
        self.assertTrue(decide(post_year=2023, tmdb=tmdb(year=2024)).approved)

    def test_a_remake_is_not_its_original(self) -> None:
        result = decide(
            post_title="Dune", post_year=1984,
            tmdb=tmdb(title="Dune", original_title="Dune", year=2021),
        )
        self.assertEqual(result.decision, REVIEW)
        self.assertEqual(result.reasons[0], IDENTITY_UNCONFIRMED)
        self.assertIn("2021", result.reasons[1])

    def test_the_same_name_in_another_year_is_another_work(self) -> None:
        result = decide(
            post_title="Nosferatu", post_year=1922,
            tmdb=tmdb(title="Nosferatu", original_title="Nosferatu", year=2024),
        )
        self.assertEqual(result.decision, REVIEW)

    def test_a_sequel_is_not_the_film_that_was_asked_for(self) -> None:
        for post, matched in (
            ("Batman", "The Batman"),
            ("Halloween", "Halloween Kills"),
            ("Wicked", "Wicked: Parte I"),
            ("Dune", "Dune: Parte dos"),
        ):
            with self.subTest(post=post):
                result = decide(
                    post_title=post, post_year=None,
                    tmdb=tmdb(title=matched, original_title=matched),
                )
                self.assertEqual(result.decision, REVIEW)
                self.assertEqual(result.reasons[0], IDENTITY_UNCONFIRMED)

    def test_a_post_without_a_year_needs_the_title_to_be_exact(self) -> None:
        """Similarity is the only signal left once the year is gone, and it is
        not enough: every one of these scores above the 85% line."""
        for post, matched in (
            ("Gladiator", "Gladiator II"),
            ("Vaiana", "Vaiana 2"),
            ("Deadpool", "Deadpool 2"),
            ("El Padrino", "El Padrino II"),
        ):
            with self.subTest(post=post):
                result = decide(
                    post_title=post, post_year=None,
                    tmdb=tmdb(title=matched, original_title=matched),
                )
                self.assertEqual(result.decision, REVIEW)
                self.assertEqual(result.reasons[0], IDENTITY_UNCONFIRMED)
                self.assertIn("no year", result.reasons[1])

    def test_a_sequel_post_is_not_the_film_it_is_a_sequel_to(self) -> None:
        """The same pairs the other way round: the post names the sequel and
        TMDB answers with the original."""
        for post, matched in (
            ("Vaiana 2", "Vaiana"),
            ("Deadpool 2", "Deadpool"),
            ("El Padrino II", "El Padrino"),
        ):
            with self.subTest(post=post):
                result = decide(
                    post_title=post, post_year=None,
                    tmdb=tmdb(title=matched, original_title=matched),
                )
                self.assertEqual(result.decision, REVIEW)
                self.assertEqual(result.reasons[0], IDENTITY_UNCONFIRMED)

    def test_an_exact_title_with_two_records_behind_it_is_ambiguous(self) -> None:
        """Suspiria is a 1977 film and a 2018 remake, both called Suspiria.
        Nothing in a post without a year says which one it means."""
        result = decide(
            post_title="Suspiria", post_year=None,
            tmdb=tmdb(
                title="Suspiria", original_title="Suspiria",
                year=2018, exact_title_hits=2,
            ),
        )
        self.assertEqual(result.decision, REVIEW)
        self.assertEqual(result.reasons[0], IDENTITY_UNCONFIRMED)
        self.assertIn("2 records", result.reasons[1])

    def test_two_series_of_the_same_name_are_ambiguous(self) -> None:
        """Nearly half the series posts carry no year, and Shameless is two
        different shows."""
        result = decide(
            media_type=MediaType.SERIES,
            post_title="Shameless", post_year=None,
            tmdb=tmdb(
                title="Shameless", original_title="Shameless",
                year=2011, exact_title_hits=2,
            ),
        )
        self.assertEqual(result.decision, REVIEW)
        self.assertEqual(result.reasons[0], IDENTITY_UNCONFIRMED)

    def test_an_exact_title_with_one_record_behind_it_is_confirmed(self) -> None:
        result = decide(
            post_title="Example", post_year=None,
            tmdb=tmdb(year=1999, exact_title_hits=1),
        )
        self.assertTrue(result.approved)

    def test_a_yearless_match_says_what_was_actually_checked(self) -> None:
        """The audit trail used to claim the year had been compared."""
        result = decide(post_year=None, tmdb=tmdb(exact_title_hits=1))
        self.assertEqual(result.reasons[0], SAME_TITLE_NO_YEAR)
        self.assertNotIn(SAME_TITLE_AND_YEAR, result.reasons)

    def test_a_match_with_a_year_still_says_the_year_was_checked(self) -> None:
        self.assertEqual(decide().reasons[0], SAME_TITLE_AND_YEAR)

    def test_the_exact_title_ignores_accents_and_punctuation(self) -> None:
        """"Mision Imposible" off a release name is the same title as TMDB's."""
        result = decide(
            post_title="Mision: Imposible", post_year=None,
            tmdb=tmdb(
                title="Misión: Imposible", original_title="Mission: Impossible",
                exact_title_hits=1,
            ),
        )
        self.assertTrue(result.approved)

    def test_an_exact_original_title_confirms_it_too(self) -> None:
        result = decide(
            post_title="Mission Impossible", post_year=None,
            tmdb=tmdb(
                title="Misión: Imposible", original_title="Mission: Impossible",
                exact_title_hits=1,
            ),
        )
        self.assertTrue(result.approved)

    def test_a_post_with_a_year_keeps_the_one_year_tolerance(self) -> None:
        """The stricter yearless rule must not leak into posts that do carry a
        year: TMDB's date and the release group's still differ by one."""
        self.assertEqual(YEAR_TOLERANCE, 1)
        self.assertTrue(
            decide(post_year=2023, tmdb=tmdb(year=2024)).approved
        )

    def test_a_match_with_no_year_cannot_answer_a_post_that_has_one(self) -> None:
        result = decide(post_year=2024, tmdb=tmdb(year=None))
        self.assertEqual(result.decision, REVIEW)
        self.assertEqual(result.reasons[0], IDENTITY_UNCONFIRMED)

    def test_a_match_found_only_by_dropping_the_year_is_refused(self) -> None:
        """The fallback search throws away the one field that distinguishes a
        remake from its original, so its answer is never trusted."""
        result = decide(tmdb=tmdb(match_mode=MATCH_YEAR_DROPPED))
        self.assertEqual(result.decision, REVIEW)
        self.assertEqual(result.reasons[0], IDENTITY_UNCONFIRMED)
        self.assertIn("year was dropped", result.reasons[1])

    def test_a_match_made_without_a_year_is_fine(self) -> None:
        self.assertTrue(
            decide(post_year=None, tmdb=tmdb(match_mode=MATCH_NO_YEAR)).approved
        )

    def test_a_post_with_no_title_cannot_be_confirmed(self) -> None:
        result = decide(post_title=None)
        self.assertEqual(result.decision, REVIEW)
        self.assertEqual(result.reasons[0], NO_TITLE)

    def test_the_identity_check_is_not_reached_without_a_match(self) -> None:
        """No TMDB match at all is still missing data, not a wrong match."""
        result = decide(tmdb=None, parents_guide=ParentsGuide())
        self.assertEqual(result.decision, WAIT)


class KeywordDataTests(unittest.TestCase):
    """An empty TMDB keyword list is silence. It used to read as a clean answer."""

    def test_no_keywords_waits_instead_of_approving(self) -> None:
        result = decide(tmdb=tmdb(keywords=[]))
        self.assertEqual(result.decision, WAIT)
        self.assertIn("keyword: TMDB lists no keywords", result.reasons)

    def test_no_keywords_is_handed_back_at_the_expiry(self) -> None:
        result = decide(
            tmdb=tmdb(keywords=[]),
            now=CREATED + timedelta(hours=24),
        )
        self.assertEqual(result.decision, REVIEW)
        self.assertEqual(result.reasons[0], "no data after 24 hours")

    def test_a_genre_hit_still_blocks_before_anything_is_missing(self) -> None:
        result = decide(tmdb=tmdb(keywords=[], genre_ids=[FLAGGED_GENRE_ID]))
        self.assertEqual(result.decision, REVIEW)
        self.assertIn(f"genre: {FLAGGED_GENRE_ID}", result.reasons)

    def test_without_a_keyword_rule_nothing_waits_for_keywords(self) -> None:
        """Required data is what the rules read: the old unconditional wait
        for keywords is gone with the rule that needed them."""
        cfg = policy_fixture.config()
        rules = [r for r in cfg.policy.rules if r.check != "keyword"]
        policy = cfg.policy.model_copy(update={"rules": rules})
        item = policy_fixture.item(created_at=CREATED)
        snapshot = gather.base_signals(
            item, cfg, tmdb=tmdb(keywords=[]), guide=guide(), book=None, book_looked_up=False,
            duplicate_of=None, twin_of=None, now=CREATED,
        )
        self.assertEqual(evaluate(item, snapshot, policy, now=CREATED).outcome, APPROVE)


class FrighteningTests(unittest.TestCase):
    """Added after the backtest: the one real content miss was a film nobody
    had flagged for sex or violence but everybody had flagged as frightening."""

    def test_severe_frightening_blocks(self) -> None:
        result = decide(parents_guide=guide(frightening=SeverityRating.SEVERE))
        self.assertEqual(result.decision, REVIEW)
        self.assertIn("imdb.frightening: severe", result.reasons)

    def test_moderate_frightening_is_fine(self) -> None:
        self.assertTrue(
            decide(parents_guide=guide(frightening=SeverityRating.MODERATE)).approved
        )

    def test_frightening_votes_are_counted_separately(self) -> None:
        result = decide(parents_guide=guide(frightening_votes=2), min_guide_votes=5)
        self.assertEqual(result.decision, WAIT)
        self.assertIn("imdb.frightening: only 2 votes, need 5", result.reasons)


class CertificationTests(unittest.TestCase):
    def test_an_unrated_us_release_is_not_an_adult_one(self) -> None:
        """NR means nobody submitted the film, not that it is adult."""
        self.assertTrue(decide(tmdb=tmdb(certifications={"US": "NR"})).approved)

    def test_the_real_us_adult_ratings_still_block(self) -> None:
        for cert in ("NC-17", "X"):
            with self.subTest(cert=cert):
                result = decide(tmdb=tmdb(certifications={"US": cert}))
                self.assertEqual(result.decision, REVIEW)
                self.assertIn(f"certification: US:{cert}", result.reasons)


class SeverityBreakdownTests(unittest.TestCase):
    """The most-voted label hides a split vote. IMDb publishes the split, so
    a category also blocks when a large minority voted it worse."""

    @staticmethod
    def _spread(none, mild, moderate, severe):
        return {"none": none, "mild": mild, "moderate": moderate, "severe": severe}

    def test_a_mild_label_with_a_big_moderate_minority_blocks(self) -> None:
        # The Shawshank Redemption's own numbers: labelled Mild, 45% of the
        # votes cast are Moderate or Severe.
        result = decide(
            parents_guide=guide(
                sex=SeverityRating.MILD,
                breakdown={"NUDITY": self._spread(206, 714, 605, 147)},
            )
        )
        self.assertEqual(result.decision, REVIEW)
        self.assertIn("45% of the votes are moderate or worse", result.reasons[0])

    def test_a_small_minority_does_not_block(self) -> None:
        result = decide(
            parents_guide=guide(
                sex=SeverityRating.MILD,
                breakdown={"NUDITY": self._spread(400, 400, 100, 0)},
            )
        )
        self.assertTrue(result.approved)

    def test_exactly_forty_per_cent_blocks(self) -> None:
        result = decide(
            parents_guide=guide(
                sex=SeverityRating.MILD,
                breakdown={"NUDITY": self._spread(0, 600, 400, 0)},
            )
        )
        self.assertEqual(result.decision, REVIEW)

    def test_one_vote_under_the_line_does_not_block(self) -> None:
        result = decide(
            parents_guide=guide(
                sex=SeverityRating.MILD,
                breakdown={"NUDITY": self._spread(0, 601, 399, 0)},
            )
        )
        self.assertTrue(result.approved)

    def test_violence_is_measured_against_severe(self) -> None:
        """Moderate violence is allowed, so only the severe votes count."""
        approved = decide(
            parents_guide=guide(
                violence=SeverityRating.MODERATE,
                breakdown={"VIOLENCE": self._spread(0, 100, 600, 300)},
            )
        )
        self.assertTrue(approved.approved)
        blocked = decide(
            parents_guide=guide(
                violence=SeverityRating.MODERATE,
                breakdown={"VIOLENCE": self._spread(0, 100, 400, 500)},
            )
        )
        self.assertEqual(blocked.decision, REJECT)

    def test_no_breakdown_leaves_the_label_in_charge(self) -> None:
        self.assertTrue(decide(parents_guide=guide(sex=SeverityRating.MILD)).approved)

    def test_an_all_zero_breakdown_is_ignored(self) -> None:
        result = decide(
            parents_guide=guide(
                sex=SeverityRating.MILD,
                breakdown={"NUDITY": self._spread(0, 0, 0, 0)},
            )
        )
        self.assertTrue(result.approved)


if __name__ == "__main__":
    unittest.main()
