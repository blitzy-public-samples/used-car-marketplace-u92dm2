"""Contract tests for the rating models, their bounds and their views.

The third of the rating suites, and the one with no datastore and no
HTTP in it at all. ``test_rating_service.py`` owns the semantics behind
the acceptance gates and ``test_ratings_api.py`` owns the status codes;
what neither owns, and what this module exists for, is the layer both of
them stand on - ``app/schema/rating.py``, the ``ModerationUpdate``
request model in ``app/api/ratings.py``, and the additive fields on
``app/schema/user.py``.

WHY THIS IS A SUITE OF ITS OWN
-----------------------------------------------------------------------
Those models are not incidental plumbing. They are where this feature's
authoritative validation lives, and they are load-bearing in three ways
that a test exercising them only INCIDENTALLY - through a request that
happens to carry a valid body - cannot cover:

* THE BOUNDS ARE THE SERVER'S AUTHORITY. The scale, the review limit and
  the review's plain-text contract are enforced here and mirrored, as a
  convenience, on the client. A branch that is only ever reached with a
  well-formed body is a branch nothing is holding to its rule.
* THE IDENTIFIERS ARE A SECURITY BOUNDARY. ``transaction_id`` arrives
  from a caller and is used to COMPOSE document paths - the transaction
  the service reads, and, joined with the rater's ID, the rating it
  creates. A value carrying a slash makes Firestore resolve a nested
  path instead of the document the field appears to name; an oversized
  one mints a key the moderation endpoint then refuses; a control
  character forges any log line that renders it. The grammar that
  excludes all three is a boundary, and boundaries are tested at their
  edges.
* THE VIEWS DECIDE WHAT IS PUBLISHED. ``RatingView`` omits the
  moderation fields, and that omission is the whole reason a public read
  does not publish the moderation queue. It is enforced by a field list,
  so it is exactly the kind of thing a later change breaks silently -
  the endpoints keep working and start returning more than they should.

Every case here is a table of inputs against expected verdicts, walked
with ``subTest`` so one bad value names itself instead of hiding behind
the first failure. The tables are written out as literals rather than
generated from the constraints they test, which is the point: a table
derived from the model would agree with the model by construction.

The pydantic version is v1, matching the pin in
``backend/requirements.txt``. ``ValidationError`` is a subclass of
``ValueError`` there, so a validator's own ``ValueError`` and the model's
refusal are both caught as ``ValidationError`` when raised through a
model, and as ``ValueError`` when a normaliser is called directly.
"""
import unittest
from datetime import datetime, timezone

from google.cloud import firestore
from pydantic import BaseModel, ValidationError

from conftest import build_rating_document, utc_now

from app.api.ratings import ModerationUpdate
from app.core.config import settings
from app.schema.rating import (
    DOCUMENT_ID_MAX_LENGTH,
    MODERATION_REASON_MAX_LENGTH,
    RATEE_ID_CLAIM,
    RATING_ID_ESCAPE_FACTOR,
    RATING_ID_MAX_LENGTH,
    REVIEW_RAW_LENGTH_FACTOR,
    REVIEW_RAW_MAX_LENGTH,
    DocumentId,
    EligibilityDecision,
    ModeratedRatingView,
    ModerationStatus,
    Rating,
    RatingAggregate,
    RatingCreate,
    RatingDirection,
    RatingDocumentId,
    RatingView,
    as_plain_text,
    enum_value,
    to_moderated_rating_view,
    to_rating_view,
    to_rating_views,
)
from app.schema.user import User, is_valid_document_id

# The fields ``RatingView`` publishes, written out rather than read off
# the model. This is the omission that keeps a public read from
# publishing the moderation queue: the visibility decision moderation
# drives has already been applied - a withheld review arrives with
# ``review`` empty - so a reader needs the state itself for nothing, and
# a participant reader is the person most motivated to argue with it.
#
# Enforced by a field list in the model, which is precisely the kind of
# thing a later change widens without anybody noticing: the endpoints
# keep working and simply start returning more.
PUBLIC_VIEW_FIELDS = frozenset((
    'id',
    'transaction_id',
    'vehicle_listing_id',
    'rater_id',
    'ratee_id',
    'direction',
    'score',
    'review',
    'is_published',
    'created_at',
    'updated_at',
))

# The two fields only an administrator ever receives, and only from the
# moderation endpoint.
MODERATION_VIEW_FIELDS = frozenset((
    'moderation_status',
    'moderation_reason',
))

# Values a Firestore document ID may take, with the reason each is
# admitted. A SPACE is on this list deliberately: Firestore's grammar
# excludes the slash and the control characters and nothing else, so a
# client-side rule that also refused whitespace would be NARROWER than
# the server's and would block a reference the server would have
# accepted - the one asymmetry here that costs a legitimate rating.
VALID_DOCUMENT_IDS = (
    ('a plain identifier', 'test-transaction-000001'),
    ('a Firestore auto-id', 'kFcT7Yq2XjLm0pR9sVbN'),
    ('a single character', 'a'),
    ('an ordinary space', 'has space'),
    ('leading underscores only', '__x'),
    ('trailing underscores only', 'x__'),
    ('underscores around a word', '_x_'),
    ('at the length ceiling', 'a' * DOCUMENT_ID_MAX_LENGTH),
    ('a non-ASCII letter', 'identité'),
)

# Values it may NOT take, with the failure each one would cause. Every
# entry is a real failure mode rather than a tidiness rule.
# The GRAMMAR violations, which every identifier in this feature shares
# whatever its length ceiling. Kept separate from the length case below
# so the composite rating key - whose ceiling is more than twice as high -
# can be held to the same grammar without being held to the same length.
INVALID_DOCUMENT_ID_GRAMMAR = (
    ('empty', ''),
    ('a slash, read as a nested path', 'users/somebody'),
    ('a leading slash', '/somebody'),
    ('the current-directory segment', '.'),
    ('the parent-directory segment', '..'),
    ('a traversal attempt', '../users/somebody'),
    ("Firestore's reserved namespace", '__id__'),
    ('a newline, which forges a log line', 'caller\nADMIN'),
    ('a tab', 'caller\tADMIN'),
    ('a NUL byte', 'caller\x00ADMIN'),
    ('DEL', 'caller\x7fADMIN'),
    ('a trailing newline', 'caller\n'),
)

# The grammar violations plus the component length ceiling: everything a
# single ``DocumentId`` refuses.
INVALID_DOCUMENT_IDS = INVALID_DOCUMENT_ID_GRAMMAR + (
    ('one past the length ceiling', 'a' * (DOCUMENT_ID_MAX_LENGTH + 1)),
)


class DocumentIdProbe(BaseModel):
    """A model with one ``DocumentId`` field, to exercise the type.

    The constrained type cannot be validated on its own - pydantic v1
    applies a ``constr`` through a model - so the grammar is probed
    through the smallest model that carries it. Declared here rather than
    borrowing a production model so a refusal can only be the type's.
    """

    value: DocumentId


class RatingDocumentIdProbe(BaseModel):
    """A model with one ``RatingDocumentId`` field."""

    value: RatingDocumentId


def rating_body(**overrides):
    """Return a valid stored rating body, with fields overridden.

    Args:
        **overrides: Any :func:`conftest.build_rating_document` keyword.

    Returns:
        The document body as a dict, valid unless an override breaks it.
    """
    return build_rating_document(**overrides)


def body_with(field, value):
    """Return a valid body with one field ASSIGNED, not defaulted.

    :func:`conftest.build_rating_document` reads ``None`` for several of
    its keywords as "use the default" - which is right for a fixture and
    wrong for the cases below that need the field to genuinely hold
    ``None``. Passing ``direction=None`` through the builder yields a
    perfectly valid buyer-to-seller document, so a test written that way
    would assert a refusal that never had anything to refuse.

    Args:
        field: The document field to set.
        value: The exact value to put there.

    Returns:
        The document body as a dict.
    """
    body = build_rating_document()
    body[field] = value
    return body


def body_without(field):
    """Return a valid body with one field ABSENT.

    Distinct from ``None``: a document the server has not stamped yet
    carries no ``created_at`` key at all, and absence and null reach the
    validators by different routes.

    Args:
        field: The document field to remove.

    Returns:
        The document body as a dict.
    """
    body = build_rating_document()
    body.pop(field, None)
    return body


def stamped_rating(**overrides):
    """Return a ``Rating`` carrying real timestamps.

    The view projections refuse a rating whose timestamps have not
    resolved, so anything asserting a projection needs a stamped record.

    Args:
        **overrides: Any :func:`conftest.build_rating_document` keyword.

    Returns:
        An ``app.schema.rating.Rating``.
    """
    moment = utc_now()
    overrides.setdefault('created_at', moment)
    overrides.setdefault('updated_at', moment)
    return Rating(**rating_body(**overrides))


class TestDocumentIdGrammar(unittest.TestCase):
    """The identifier grammar, at its edges. A security boundary.

    These values are used to compose Firestore paths, so what the
    grammar admits decides what the datastore is asked to address. The
    two directions fail differently and both matter: too permissive
    admits a value that addresses the wrong document or cannot be
    addressed at all, while too strict refuses a reference the server
    itself would have accepted and blocks a rating with no server
    involvement to notice it.
    """

    def test_a_usable_identifier_is_accepted(self):
        """Every legitimate shape, including an ordinary space."""
        for label, value in VALID_DOCUMENT_IDS:
            with self.subTest(label):
                self.assertEqual(
                    DocumentIdProbe(value=value).value, value
                )

    def test_an_unusable_identifier_is_refused(self):
        """Every shape that would address the wrong thing, or nothing."""
        for label, value in INVALID_DOCUMENT_IDS:
            with self.subTest(label):
                with self.assertRaises(ValidationError):
                    DocumentIdProbe(value=value)

    def test_the_identifier_type_is_strict_about_being_a_string(self):
        """A number is not an identifier, and is not coerced into one.

        Pydantic v1 coerces by default, so ``12345`` would otherwise
        arrive as the string ``'12345'`` - a document ID the caller never
        named.
        """
        for value in (12345, 12.5, True, None, ['a'], {'a': 'b'}):
            with self.subTest(value=repr(value)):
                with self.assertRaises(ValidationError):
                    DocumentIdProbe(value=value)

    def test_the_length_ceiling_is_inclusive(self):
        """128 is accepted and 129 is not - the edge, both sides.

        The ceiling is not Firestore's 1500 bytes: two IDs are joined
        with an underscore to form a rating's key, so each half has to
        leave room for the other.
        """
        DocumentIdProbe(value='a' * DOCUMENT_ID_MAX_LENGTH)
        with self.assertRaises(ValidationError):
            DocumentIdProbe(value='a' * (DOCUMENT_ID_MAX_LENGTH + 1))

    def test_the_composite_rating_key_has_room_for_both_halves(self):
        """A rating ID is two ESCAPED document IDs and a separator.

        Reusing the component bound for the composite is an
        off-by-a-factor that only shows up in production: a
        128-character transaction ID and a 128-character rater ID are
        both legitimately creatable, and their rating key is longer than
        either. ``POST`` would mint that document happily and the
        moderation endpoint would then refuse the very key it had just
        created - a rating that exists, is visible, and cannot be
        moderated.

        The factor is three rather than one because each half is
        percent-escaped before it is joined: the separator is legal
        inside a document ID, so ``%`` becomes ``%25`` and ``_`` becomes
        ``%5F`` to keep the composition injective, and one character can
        therefore become three. The bound follows the encoding rather
        than the raw component length, which is what keeps a legitimate
        pair of underscore-bearing identifiers ratable.
        """
        self.assertEqual(
            RATING_ID_MAX_LENGTH,
            2 * RATING_ID_ESCAPE_FACTOR * DOCUMENT_ID_MAX_LENGTH + 1,
        )
        # Two maximal ALPHANUMERIC halves, which is what Firestore's own
        # scatter-allocated identifiers are: the escaping is the identity
        # for them, so the composed key is 2 x 128 + 1 and sits well
        # inside the bound. The factor exists for the worst legal case,
        # not for the ordinary one.
        longest_unescaped = '{0}_{1}'.format(
            'a' * DOCUMENT_ID_MAX_LENGTH,
            'b' * DOCUMENT_ID_MAX_LENGTH,
        )
        self.assertEqual(
            len(longest_unescaped), 2 * DOCUMENT_ID_MAX_LENGTH + 1
        )
        self.assertLess(len(longest_unescaped), RATING_ID_MAX_LENGTH)
        self.assertEqual(
            RatingDocumentIdProbe(value=longest_unescaped).value,
            longest_unescaped,
        )
        # And the ceiling itself: exactly at it is accepted, one over is
        # refused.
        at_the_ceiling = 'a' * RATING_ID_MAX_LENGTH
        self.assertEqual(
            RatingDocumentIdProbe(value=at_the_ceiling).value,
            at_the_ceiling,
        )
        with self.assertRaises(ValidationError):
            RatingDocumentIdProbe(
                value='a' * (RATING_ID_MAX_LENGTH + 1)
            )

    def test_the_composite_key_keeps_the_component_grammar(self):
        """A longer bound, not a looser one.

        Every grammar rule still applies; only the LENGTH ceiling moves,
        which is why the length case is excluded here and asserted at its
        own edge above. An identifier of 129 characters is refused as a
        component and legitimately accepted as a composite, so folding
        the two tables together would assert the opposite of the
        contract.
        """
        for label, value in INVALID_DOCUMENT_ID_GRAMMAR:
            with self.subTest(label):
                with self.assertRaises(ValidationError):
                    RatingDocumentIdProbe(value=value)

    def test_the_predicate_agrees_with_the_constrained_type(self):
        """``is_valid_document_id`` and ``DocumentId`` are one rule.

        The predicate is what ``app/api/auth.py`` applies to the JWT
        subject before composing a path with it, and the constrained type
        is what the models apply. They are separate call sites of the same
        grammar, so they are asserted to agree on every value in both
        tables - the alternative is a token subject accepted by one layer
        and refused by the next.
        """
        for label, value in VALID_DOCUMENT_IDS:
            with self.subTest('valid: ' + label):
                self.assertTrue(is_valid_document_id(value))
        for label, value in INVALID_DOCUMENT_IDS:
            with self.subTest('invalid: ' + label):
                self.assertFalse(is_valid_document_id(value))

    def test_the_predicate_refuses_a_non_string_without_raising(self):
        """It answers False rather than raising on any type.

        It guards a value taken straight from a decoded JWT, so it is
        handed whatever the token carried. Raising there would turn a
        malformed credential into a 500 instead of the 401 it is.
        """
        for value in (None, 12345, 12.5, True, [], {}, object()):
            with self.subTest(value=type(value).__name__):
                self.assertFalse(is_valid_document_id(value))


class TestReviewTextContract(unittest.TestCase):
    """``as_plain_text``: the one review policy, applied everywhere.

    Both fields of author-supplied prose on a rating - the public review
    and the moderator's recorded reason - go through this function, and
    the client mirrors it step for step so a character counter in the
    interface and this bound agree about what "2000 characters" means.

    The ORDER is part of the contract rather than incidental. Applying a
    bound to raw text on one side and to normalised text on the other
    makes the two disagree by exactly the amount normalisation removes,
    which is largest for the users least able to diagnose it: anyone
    typing a diacritic-heavy language on a platform that emits decomposed
    Unicode.
    """

    def test_unicode_is_composed(self):
        """NFC, so equivalent strings compare and truncate alike.

        ``e`` plus a combining acute is two code points until it is
        composed into one. Measuring before composing over-counts text
        the server will store as shorter.
        """
        composed = as_plain_text('e\u0301clair')
        self.assertEqual(composed, '\u00e9clair')
        self.assertEqual(len(composed), 6)

    def test_line_endings_are_folded(self):
        """CRLF and CR both become the newline that gets stored."""
        self.assertEqual(as_plain_text('a\r\nb\rc'), 'a\nb\nc')

    def test_control_and_format_characters_are_dropped(self):
        """Invisible in a review, and destructive in a log or a CSV.

        Newline and tab survive because they are legitimate in prose;
        everything else in the C0 range, DEL and the format category
        goes - including the zero-width characters that make two
        different strings look identical.
        """
        self.assertEqual(as_plain_text('a\x00b\x1fc\x7fd'), 'abcd')
        self.assertEqual(as_plain_text('zero\u200bwidth'), 'zerowidth')
        self.assertEqual(as_plain_text('keep\nthis\ttoo'),
                         'keep\nthis\ttoo')

    def test_blank_line_runs_collapse_and_edges_are_trimmed(self):
        """Two blank lines is a paragraph break; five is not."""
        self.assertEqual(as_plain_text('a\n\n\n\n\nb'), 'a\n\nb')
        self.assertEqual(as_plain_text('  spaced  '), 'spaced')

    def test_text_that_normalises_to_nothing_becomes_none(self):
        """"No review" is one state, not several.

        Whitespace-only, control-character-only and ``None`` all arrive
        as the same absence, so nothing downstream has to distinguish an
        empty review from a missing one.
        """
        for value in (None, '', '   ', '\n\n', '\t', '\u200b'):
            with self.subTest(value=repr(value)):
                self.assertIsNone(as_plain_text(value))

    def test_markup_is_refused_rather_than_stripped(self):
        """The plain-text claim, made a property of the data.

        Stripping tags would silently edit somebody's review; refusing
        keeps the author's words intact and keeps the stored value
        genuinely free of markup semantics, which is what any consumer
        that does not escape by default - an HTML email, a PDF, a CSV -
        depends on.
        """
        for value in ('<b>bold</b>', 'a < b', 'a > b', '<script>'):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    as_plain_text(value)

    def test_a_bracket_cannot_hide_behind_a_stripped_character(self):
        """The check runs AFTER the removals, which is the point.

        ``<scr\\x00ipt>`` loses its NUL to the control-character step, so
        a check performed first would have seen no bracket and passed the
        result through with one.
        """
        with self.assertRaises(ValueError):
            as_plain_text('<scr\x00ipt>alert(1)</scr\x00ipt>')

    def test_the_semantic_bound_is_measured_on_the_stored_text(self):
        """The limit describes the value a reader will actually see.

        Text at the limit is accepted and one character past it is
        refused - and the measurement happens after normalisation, so a
        review that is only over the limit because of CRLF line endings
        or trailing whitespace is accepted.
        """
        limit = settings.RATING_REVIEW_MAX_LENGTH
        self.assertEqual(len(as_plain_text('a' * limit)), limit)
        with self.assertRaises(ValueError):
            as_plain_text('a' * (limit + 1))
        padded = '   {0}   '.format('a' * limit)
        self.assertEqual(len(as_plain_text(padded)), limit)

    def test_a_caller_may_bound_the_text_more_tightly(self):
        """The bound and the field name are parameters, not copies.

        The moderator's reason reuses this whole function with a smaller
        ceiling and its own label, so there is one normaliser rather than
        two that drift.
        """
        with self.assertRaises(ValueError) as raised:
            as_plain_text('x' * 11, max_length=10, label='Reason')
        self.assertIn('Reason', str(raised.exception))
        self.assertIn('10', str(raised.exception))

    def test_the_raw_ceiling_is_a_multiple_of_the_semantic_one(self):
        """Two different bounds, for two different jobs.

        The raw ceiling exists only to keep an unbounded body away from
        the normaliser; the semantic limit is the rule the author is held
        to. Deriving one from the other at import is what stops them
        drifting apart.
        """
        self.assertEqual(
            REVIEW_RAW_MAX_LENGTH,
            REVIEW_RAW_LENGTH_FACTOR
            * settings.RATING_REVIEW_MAX_LENGTH,
        )
        self.assertGreater(
            REVIEW_RAW_MAX_LENGTH, settings.RATING_REVIEW_MAX_LENGTH
        )


class TestEnumValueHelper(unittest.TestCase):
    """``enum_value``: a real ``str`` on the attribute, always.

    Pydantic v1 does not validate field defaults, so an enum-typed field
    would keep the raw member as its default - and on Python 3.9
    ``str()`` of such a member yields ``'ModerationStatus.PENDING'``
    rather than ``'pending'``, which silently corrupts any write, log
    line or query that stringifies it.
    """

    def test_a_member_and_its_value_both_resolve_to_the_value(self):
        """Either form in, the canonical string out."""
        self.assertEqual(
            enum_value(
                RatingDirection.SELLER_TO_BUYER,
                RatingDirection,
                'direction',
            ),
            'seller_to_buyer',
        )
        self.assertEqual(
            enum_value('buyer_to_seller', RatingDirection, 'direction'),
            'buyer_to_seller',
        )

    def test_the_result_is_a_plain_string(self):
        """Not an enum member that merely compares equal to one."""
        resolved = enum_value(
            ModerationStatus.PENDING, ModerationStatus, 'status'
        )
        self.assertIs(type(resolved), str)
        self.assertEqual(str(resolved), 'pending')

    def test_an_unknown_value_names_what_is_permitted(self):
        """The error is discoverable rather than merely a refusal."""
        with self.assertRaises(ValueError) as raised:
            enum_value('sideways', RatingDirection, 'rating direction')
        message = str(raised.exception)
        self.assertIn('rating direction', message)
        self.assertIn('buyer_to_seller', message)
        self.assertIn('seller_to_buyer', message)


class TestStoredRatingModel(unittest.TestCase):
    """``Rating``: the quarantine a stored document has to pass.

    A Firestore document is untyped, so this model is what stands between
    a body somebody wrote and the read and publication paths that branch
    on it. A body that fails here is dropped from reads and refused for
    publication, logged for repair - which is a far better outcome than
    publishing a score against an identifier nothing can address.
    """

    def test_a_faithful_document_body_validates(self):
        """The baseline every other case in this class deviates from."""
        rating = Rating(**rating_body(review='Punctual and honest'))
        self.assertEqual(rating.score, 5)
        self.assertEqual(
            rating.direction, RatingDirection.BUYER_TO_SELLER.value
        )
        self.assertEqual(
            rating.moderation_status, ModerationStatus.PENDING.value
        )
        self.assertFalse(rating.is_published)
        self.assertIsNone(rating.moderation_reason)

    def test_the_score_is_a_strict_bounded_integer(self):
        """Coercion here records a vote the caller never chose.

        Pydantic v1 would turn ``4.7`` into ``4`` - silently rounding a
        rating down - and ``True`` into ``1``, the lowest score on the
        scale.
        """
        minimum = settings.RATING_MIN
        maximum = settings.RATING_MAX
        for score in (minimum, maximum):
            with self.subTest(score=score):
                self.assertEqual(
                    Rating(**rating_body(score=score)).score, score
                )
        refused = (
            minimum - 1,
            maximum + 1,
            0,
            4.7,
            '4',
            True,
            None,
            float(maximum),
        )
        for score in refused:
            with self.subTest(score=repr(score)):
                with self.assertRaises(ValidationError):
                    Rating(**rating_body(score=score))

    def test_publication_state_is_a_strict_boolean(self):
        """Visibility must never turn on a truthiness accident.

        This field decides whether a rating is public and whether it
        counts toward a reputation, and the service branches on the raw
        stored value. Plain ``bool`` in pydantic v1 maps the STRINGS
        ``'true'`` and ``'false'`` onto real booleans - so a document
        whose flag is the string ``'false'`` would be read as published.
        """
        for value in ('true', 'false', 1, 0, None, 'yes'):
            with self.subTest(value=repr(value)):
                with self.assertRaises(ValidationError):
                    Rating(**rating_body(is_published=value))
        self.assertTrue(
            Rating(**rating_body(is_published=True)).is_published
        )

    def test_only_the_two_derived_directions_are_accepted(self):
        """An unrecognised direction is a malformed record."""
        for value in ('buyer_to_seller', 'seller_to_buyer'):
            with self.subTest(value=value):
                self.assertEqual(
                    Rating(**rating_body(direction=value)).direction,
                    value,
                )
        for value in ('sideways', 'BUYER_TO_SELLER', '', None, 1):
            with self.subTest(value=repr(value)):
                with self.assertRaises(ValidationError):
                    Rating(**body_with('direction', value))

    def test_only_the_three_moderation_states_are_accepted(self):
        """A state nothing recognises must not be misclassified."""
        for state in ModerationStatus:
            with self.subTest(state=state.value):
                self.assertEqual(
                    Rating(
                        **rating_body(moderation_status=state.value)
                    ).moderation_status,
                    state.value,
                )
        for value in ('quietly-hidden', 'APPROVED', '', None):
            with self.subTest(value=repr(value)):
                with self.assertRaises(ValidationError):
                    Rating(**body_with('moderation_status', value))

    def test_the_stored_review_is_held_to_the_plain_text_contract(self):
        """Normalised on the way in, and refused if it cannot be.

        Applied on anything read back as well as on anything written, so
        no consumer can receive text this policy has not passed.
        """
        rating = Rating(**rating_body(review='  spaced\r\nout  '))
        self.assertEqual(rating.review, 'spaced\nout')
        self.assertIsNone(Rating(**rating_body(review='   ')).review)
        with self.assertRaises(ValidationError):
            Rating(**rating_body(review='<b>markup</b>'))

    def test_the_moderation_reason_has_its_own_smaller_bound(self):
        """A policy citation is a phrase, not an essay.

        Unbounded, it is an unbounded write into a document Firestore
        caps at 1 MiB, and a newline in it forges a line in any log or
        export that renders it.
        """
        at_limit = 'x' * MODERATION_REASON_MAX_LENGTH
        self.assertEqual(
            Rating(
                **rating_body(moderation_reason=at_limit)
            ).moderation_reason,
            at_limit,
        )
        with self.assertRaises(ValidationError):
            Rating(**rating_body(
                moderation_reason='x' * (
                    MODERATION_REASON_MAX_LENGTH + 1
                )
            ))
        with self.assertRaises(ValidationError):
            Rating(**rating_body(moderation_reason='<b>policy</b>'))
        self.assertIsNone(
            Rating(**rating_body(moderation_reason='  ')).moderation_reason
        )
        self.assertLess(
            MODERATION_REASON_MAX_LENGTH,
            settings.RATING_REVIEW_MAX_LENGTH,
        )

    def test_only_the_timestamp_shapes_this_system_produces(self):
        """A real datetime, the write-time sentinel, or absence.

        All three are legitimate at different moments - the sentinel is
        what a payload about to be written carries, a real timestamp is
        what reading it back yields, and absence is the window between
        them. Anything else would flow into the rating-window comparison
        and the ordering key, where it raises at a distance or sorts
        nonsensically.
        """
        accepted = (
            None,
            utc_now(),
            datetime(2026, 1, 1, tzinfo=timezone.utc),
            firestore.SERVER_TIMESTAMP,
        )
        for value in accepted:
            with self.subTest(value=type(value).__name__):
                Rating(**body_with('created_at', value))
        # Absence is its own case, and reaches the validator by a
        # different route than an explicit null.
        Rating(**body_without('created_at'))
        for value in ('yesterday', 1700000000, 12.5, {'seconds': 1}):
            with self.subTest(value=repr(value)):
                with self.assertRaises(ValidationError):
                    Rating(**body_with('created_at', value))

    def test_every_identifier_that_becomes_a_path_is_constrained(self):
        """The fields the service hands to ``document()``.

        A stored value carrying a slash would make Firestore resolve a
        nested path instead of the document the field appears to name,
        and a stored ``ratee_id`` is what an aggregate write would be
        addressed to.

        Built with :func:`body_with` rather than through the builder's
        keywords, and that is load-bearing: the builder composes the
        document's own ``id`` with the production key encoder, which
        refuses a component that is not a usable document ID BEFORE a
        model is ever constructed. Passed through the keyword the helper
        would raise ``ValueError`` from the encoder and this test would
        pass on the wrong exception, proving nothing about the model's
        own field constraints. Assigning the field directly leaves the
        composed ``id`` valid and puts the malformed value exactly where
        the case describes it.
        """
        for field in ('transaction_id', 'rater_id', 'ratee_id'):
            for label, value in (
                ('a slash', 'users/somebody'),
                ('a control character', 'caller\nADMIN'),
                ('over the ceiling', 'a' * 129),
                ('empty', ''),
            ):
                with self.subTest(field=field, case=label):
                    with self.assertRaises(ValidationError):
                        Rating(**body_with(field, value))

    def test_the_listing_reference_is_deliberately_unconstrained(self):
        """A field this feature carries and never addresses.

        ``vehicle_listing_id`` is denormalised so a rating can be
        rendered with context; nothing here composes a path from it, so
        constraining it would refuse records over something this module
        has no stake in.
        """
        rating = Rating(**rating_body(vehicle_listing_id='any/thing'))
        self.assertEqual(rating.vehicle_listing_id, 'any/thing')


class TestRatingCreateContract(unittest.TestCase):
    """``RatingCreate``: three fields, and one tolerated claim.

    The request body, and the narrowest surface in the feature. Neither
    ``ratee_id`` nor ``direction`` is declared, because both are derived
    server-side from the cited transaction - which is what makes
    counterparty spoofing and self-rating structurally impossible rather
    than merely validated against.
    """

    def test_the_model_declares_exactly_three_fields(self):
        """A fourth declared field would be a fourth thing to trust."""
        self.assertEqual(
            set(RatingCreate.__fields__),
            {'transaction_id', 'score', 'review'},
        )

    def test_a_minimal_body_is_accepted_without_a_review(self):
        """A score with no words is a complete submission."""
        payload = RatingCreate(transaction_id='t-1', score=4)
        self.assertIsNone(payload.review)
        self.assertNotIn('review', payload.__fields_set__)

    def test_the_counterparty_claim_is_retained_to_be_refused(self):
        """Visible, so the service can compare and reject it.

        Pydantic's default would DROP the key, and dropping it is the
        one outcome that must not happen: a caller asserting a
        relationship the transaction does not establish would be answered
        201 with a silently redirected rating rather than a refusal. It
        is retained and never used - the write path names every field it
        persists.
        """
        payload = RatingCreate(**{
            'transaction_id': 't-1',
            'score': 4,
            RATEE_ID_CLAIM: 'somebody-else',
        })
        self.assertEqual(
            getattr(payload, RATEE_ID_CLAIM), 'somebody-else'
        )
        self.assertNotIn(RATEE_ID_CLAIM, RatingCreate.__fields__)

    def test_every_other_unrecognised_key_is_refused_by_name(self):
        """An undiscoverable contract invites a caller to guess.

        Blanket tolerance would accept ``is_published``,
        ``moderation_status`` or a hundred kilobytes of invented keys and
        answer 201, leaving the caller believing a field had an effect it
        never had.
        """
        for key in (
            'rater_id',
            'direction',
            'is_published',
            'moderation_status',
            'moderation_reason',
            'id',
            'anything_at_all',
        ):
            with self.subTest(key=key):
                with self.assertRaises(ValidationError) as raised:
                    RatingCreate(**{
                        'transaction_id': 't-1',
                        'score': 4,
                        key: 'value',
                    })
                self.assertIn(key, str(raised.exception))

    def test_the_transaction_reference_must_be_path_safe(self):
        """The one value that travels from a client into a path."""
        for label, value in INVALID_DOCUMENT_IDS:
            with self.subTest(label):
                with self.assertRaises(ValidationError):
                    RatingCreate(transaction_id=value, score=4)

    def test_the_request_score_shares_the_stored_score_bound(self):
        """One bound, so request and record cannot drift apart."""
        for score in (settings.RATING_MIN, settings.RATING_MAX):
            with self.subTest(score=score):
                self.assertEqual(
                    RatingCreate(
                        transaction_id='t-1', score=score
                    ).score,
                    score,
                )
        for score in (
            settings.RATING_MIN - 1,
            settings.RATING_MAX + 1,
            4.7,
            '4',
            True,
            None,
        ):
            with self.subTest(score=repr(score)):
                with self.assertRaises(ValidationError):
                    RatingCreate(transaction_id='t-1', score=score)

    def test_the_submitted_review_is_normalised_before_it_is_bound(self):
        """Sanitise-then-measure, in that order.

        The order is what lets the server accept a review that is only
        over the limit because of decomposed Unicode, zero-width
        characters, CRLF line endings or trailing whitespace - and it is
        why the interface's character counter agrees with this bound.
        """
        payload = RatingCreate(
            transaction_id='t-1',
            score=4,
            review='  tidy\r\n\r\n\r\n\r\nprose  ',
        )
        self.assertEqual(payload.review, 'tidy\n\nprose')

    def test_a_decomposed_review_is_measured_after_composition(self):
        """The case a naive length check gets wrong.

        Each ``e`` plus combining acute is two code points and one
        composed character, so text that measures twice the limit before
        composition fits comfortably after it. Refusing it would have the
        server enforcing a stricter rule than it documents, against
        exactly the users least able to explain it.
        """
        decomposed = 'e\u0301' * settings.RATING_REVIEW_MAX_LENGTH
        self.assertEqual(
            len(decomposed), 2 * settings.RATING_REVIEW_MAX_LENGTH
        )
        payload = RatingCreate(
            transaction_id='t-1', score=4, review=decomposed
        )
        self.assertEqual(
            len(payload.review), settings.RATING_REVIEW_MAX_LENGTH
        )

    def test_the_raw_ceiling_refuses_a_body_before_it_is_normalised(self):
        """An unbounded body never reaches the normaliser at all."""
        with self.assertRaises(ValidationError):
            RatingCreate(
                transaction_id='t-1',
                score=4,
                review='a' * (REVIEW_RAW_MAX_LENGTH + 1),
            )

    def test_an_over_long_review_is_refused_after_normalisation(self):
        """Inside the raw ceiling and over the semantic limit."""
        with self.assertRaises(ValidationError):
            RatingCreate(
                transaction_id='t-1',
                score=4,
                review='a' * (settings.RATING_REVIEW_MAX_LENGTH + 1),
            )

    def test_a_review_carrying_markup_is_refused(self):
        """The plain-text contract, at the request boundary."""
        with self.assertRaises(ValidationError):
            RatingCreate(
                transaction_id='t-1',
                score=4,
                review='<script>alert(1)</script>',
            )


class TestModerationUpdateContract(unittest.TestCase):
    """``ModerationUpdate``: a state, a reason, and the matrix.

    The append-only contract expressed as a request shape. A submitted
    score or review is never rewritten, so neither is accepted here, and
    a correction is a transition carrying its justification.

    The reason matrix is enforced during REQUEST validation rather than
    in the handler, so a rating is never withheld first and justified
    afterwards - and never displayed with a violation recorded against
    it.
    """

    def test_a_rejection_requires_a_policy_reason(self):
        """Withholding a review without recording why is not allowed."""
        with self.assertRaises(ValidationError) as raised:
            ModerationUpdate(
                moderation_status=ModerationStatus.REJECTED.value
            )
        self.assertIn('policy reason', str(raised.exception))

    def test_a_whitespace_only_reason_is_no_reason(self):
        """Normalisation is what makes the requirement meaningful."""
        for value in ('   ', '\n', '\t'):
            with self.subTest(value=repr(value)):
                with self.assertRaises(ValidationError):
                    ModerationUpdate(
                        moderation_status=(
                            ModerationStatus.REJECTED.value
                        ),
                        moderation_reason=value,
                    )

    def test_a_rejection_with_a_reason_is_accepted(self):
        """The only state that may carry one."""
        update = ModerationUpdate(
            moderation_status=ModerationStatus.REJECTED.value,
            moderation_reason='  Contains a phone number  ',
        )
        self.assertEqual(
            update.moderation_status, ModerationStatus.REJECTED
        )
        self.assertEqual(
            update.moderation_reason, 'Contains a phone number'
        )

    def test_a_reason_is_refused_on_a_state_that_displays(self):
        """A record that contradicts itself is refused outright.

        Neither ``approved`` nor ``pending`` withholds the rating, so a
        policy violation recorded against either would say two
        incompatible things about the same document.
        """
        for state in (
            ModerationStatus.APPROVED,
            ModerationStatus.PENDING,
        ):
            with self.subTest(state=state.value):
                with self.assertRaises(ValidationError):
                    ModerationUpdate(
                        moderation_status=state.value,
                        moderation_reason='Contains a phone number',
                    )

    def test_a_state_without_a_reason_is_accepted(self):
        """Approving and un-deciding need no justification."""
        for state in (
            ModerationStatus.APPROVED,
            ModerationStatus.PENDING,
        ):
            with self.subTest(state=state.value):
                update = ModerationUpdate(
                    moderation_status=state.value
                )
                self.assertIsNone(update.moderation_reason)

    def test_the_reason_is_bounded_plain_text(self):
        """The same normaliser and the same bound as the service."""
        with self.assertRaises(ValidationError):
            ModerationUpdate(
                moderation_status=ModerationStatus.REJECTED.value,
                moderation_reason='x' * (
                    MODERATION_REASON_MAX_LENGTH + 1
                ),
            )
        with self.assertRaises(ValidationError):
            ModerationUpdate(
                moderation_status=ModerationStatus.REJECTED.value,
                moderation_reason='<b>policy</b>',
            )
        at_limit = 'x' * MODERATION_REASON_MAX_LENGTH
        self.assertEqual(
            ModerationUpdate(
                moderation_status=ModerationStatus.REJECTED.value,
                moderation_reason=at_limit,
            ).moderation_reason,
            at_limit,
        )

    def test_an_unrecognised_state_is_refused(self):
        """Typed as the enumeration, so the states are discoverable.

        An unconstrained string would be written straight on to the
        document and would silently disable every check that reads it.
        """
        for value in ('quietly-hidden', 'REJECTED', '', None, 1):
            with self.subTest(value=repr(value)):
                with self.assertRaises(ValidationError):
                    ModerationUpdate(moderation_status=value)

    def test_the_state_is_required(self):
        """There is no default transition."""
        with self.assertRaises(ValidationError):
            ModerationUpdate()

    def test_no_other_key_is_accepted(self):
        """A body carrying a score or a review is a rewrite attempt.

        Silently ignoring it would answer 200 and leave the caller
        believing an edit had been applied that the append-only contract
        never permits.
        """
        for key in ('score', 'review', 'is_published', 'id'):
            with self.subTest(key=key):
                with self.assertRaises(ValidationError) as raised:
                    ModerationUpdate(**{
                        'moderation_status': (
                            ModerationStatus.APPROVED.value
                        ),
                        key: 'value',
                    })
                self.assertIn(key, str(raised.exception))


class TestAggregateContract(unittest.TestCase):
    """``RatingAggregate``: two fields validated as one fact.

    Either field alone is satisfiable by an impossible pair - a positive
    count beside a null average, or an average of NaN - and either would
    be rendered to a user as their counterparty's reputation.
    """

    def test_the_unrated_state_is_first_class(self):
        """No ratings yet is null and zero, never zero and zero.

        The scale starts at 1, so an average of 0 is not a low
        reputation - it is not a reputation at all. Defaulting it would
        show a user who has never been rated as having earned one star.
        """
        aggregate = RatingAggregate()
        self.assertIsNone(aggregate.average)
        self.assertEqual(aggregate.count, 0)

    def test_a_rated_state_carries_both_halves(self):
        """The ordinary case, at both ends of the scale."""
        for average in (
            float(settings.RATING_MIN),
            4.33,
            float(settings.RATING_MAX),
        ):
            with self.subTest(average=average):
                aggregate = RatingAggregate(average=average, count=3)
                self.assertEqual(aggregate.average, average)
                self.assertEqual(aggregate.count, 3)

    def test_the_two_fields_must_describe_the_same_reality(self):
        """The bi-implication, in both directions."""
        with self.assertRaises(ValidationError):
            RatingAggregate(average=None, count=1)
        with self.assertRaises(ValidationError):
            RatingAggregate(average=4.5, count=0)

    def test_a_non_finite_average_is_refused(self):
        """NaN and infinity survive a float annotation.

        Once stored they propagate through every later fold and every
        rendering, and NaN compares false with itself - so a reputation
        carrying one can never be corrected by comparison.
        """
        for average in (
            float('nan'),
            float('inf'),
            float('-inf'),
        ):
            with self.subTest(average=repr(average)):
                with self.assertRaises(ValidationError):
                    RatingAggregate(average=average, count=1)

    def test_an_average_outside_the_scale_is_refused(self):
        """An average is a mean of scores, so it lies within them."""
        for average in (
            settings.RATING_MIN - 0.5,
            settings.RATING_MAX + 0.5,
            0.0,
            -1.0,
        ):
            with self.subTest(average=average):
                with self.assertRaises(ValidationError):
                    RatingAggregate(average=average, count=1)

    def test_the_count_is_a_strict_non_negative_integer(self):
        """A count is a whole number of ratings, never a rounded one."""
        for count in (-1, 1.0, True, '2', None):
            with self.subTest(count=repr(count)):
                with self.assertRaises(ValidationError):
                    RatingAggregate(average=4.0, count=count)


class TestEligibilityContract(unittest.TestCase):
    """``EligibilityDecision``: the structured answer a form renders."""

    def test_a_decision_reports_five_fields(self):
        """The interface renders every one of them."""
        self.assertEqual(
            set(EligibilityDecision.__fields__),
            {
                'eligible',
                'reason',
                'ratee_id',
                'direction',
                'already_rated',
            },
        )

    def test_the_counterparty_fields_stay_absent_until_derived(self):
        """Null until the caller is confirmed a participant.

        A decision that never got that far cannot name a counterparty,
        and inventing one would tell a caller who they are about to rate
        on the strength of nothing.
        """
        decision = EligibilityDecision(eligible=False, already_rated=False)
        self.assertIsNone(decision.reason)
        self.assertIsNone(decision.ratee_id)
        self.assertIsNone(decision.direction)

    def test_a_reported_direction_is_one_of_the_derived_two(self):
        """Reported, not accepted: the server derived it."""
        for value in ('buyer_to_seller', 'seller_to_buyer'):
            with self.subTest(value=value):
                decision = EligibilityDecision(
                    eligible=True,
                    already_rated=False,
                    ratee_id='seller-1',
                    direction=value,
                )
                self.assertEqual(decision.direction, value)
        with self.assertRaises(ValidationError):
            EligibilityDecision(
                eligible=True, already_rated=False, direction='sideways'
            )

    def test_the_reason_is_prose_rather_than_a_code(self):
        """Rendered verbatim beside a disabled control.

        Unconstrained by any enumeration on purpose - composing the
        sentence belongs to the service, and the same sentence is reused
        as the ``detail`` of the matching refusal so the two cannot
        disagree.
        """
        decision = EligibilityDecision(
            eligible=False,
            already_rated=True,
            reason='You have already rated this transaction',
        )
        self.assertIsInstance(decision.reason, str)
        self.assertIn(' ', decision.reason)


class TestResponseProjections(unittest.TestCase):
    """The view mappers: what a caller receives, decided once.

    Projection is the boundary between a persistence record and a
    response, and it is enforced by a field list - so a widened view is
    exactly the kind of change that keeps every endpoint working while
    quietly publishing more than it should.
    """

    def test_the_public_view_publishes_no_moderation_state(self):
        """The omission that keeps the moderation queue private.

        Serving these fields on a public read lets a reader see which
        reviews were withheld and read the internal note explaining why;
        serving them to a participant hands the same information to the
        person most motivated to argue with it. Neither needs them: the
        visibility decision has already been applied.
        """
        self.assertEqual(set(RatingView.__fields__), PUBLIC_VIEW_FIELDS)
        for field in MODERATION_VIEW_FIELDS:
            self.assertNotIn(field, RatingView.__fields__)

    def test_the_moderated_view_adds_exactly_the_two_fields(self):
        """Reachable only from the admin-only moderation endpoint."""
        self.assertEqual(
            set(ModeratedRatingView.__fields__),
            PUBLIC_VIEW_FIELDS | MODERATION_VIEW_FIELDS,
        )

    def test_the_projection_carries_every_value_unchanged(self):
        """A mapping, not a transformation."""
        rating = stamped_rating(review='Careful driver, tidy paperwork')
        view = to_rating_view(rating)
        for field in PUBLIC_VIEW_FIELDS:
            with self.subTest(field=field):
                self.assertEqual(
                    getattr(view, field), getattr(rating, field)
                )

    def test_a_withheld_review_projects_as_no_review(self):
        """The only fact a reader acts on.

        A rating whose review moderation withheld arrives with ``review``
        empty and nothing else to explain it, which is why the state
        itself is not published.
        """
        view = to_rating_view(stamped_rating(review=None))
        self.assertIsNone(view.review)

    def test_an_unstampable_rating_is_refused_rather_than_invented(self):
        """A fabricated "now" would be shown as when somebody rated.

        Emitting null instead is no better: it is a response the official
        client refuses. Every path that produces a response stamps both
        values, so this reports a genuine data fault.
        """
        for field in ('created_at', 'updated_at'):
            with self.subTest(field=field):
                rating = Rating(**body_with(field, None))
                with self.assertRaises(ValueError):
                    to_rating_view(rating)
            with self.subTest(field=field, case='absent'):
                rating = Rating(**body_without(field))
                with self.assertRaises(ValueError):
                    to_rating_view(rating)

    def test_the_write_time_sentinel_is_not_a_response_timestamp(self):
        """A payload about to be written is not yet a response.

        The sentinel carries no value until the server resolves it, so a
        record still holding one cannot be represented as a response.
        """
        rating = stamped_rating(created_at=firestore.SERVER_TIMESTAMP)
        with self.assertRaises(ValueError):
            to_rating_view(rating)

    def test_a_list_read_drops_an_unusable_record_and_keeps_order(self):
        """One bad document must not fail a whole page.

        The unusable record stays visible to an operator through the log
        rather than to a reader as a fiction, and the rest of the page
        answers with what it can prove - in the order it was given, since
        the ordering is the datastore's and re-sorting here would
        override an indexed one.
        """
        first = stamped_rating(score=5)
        unusable_body = body_with('created_at', None)
        unusable_body['transaction_id'] = 'test-transaction-000002'
        unusable_body['id'] = '{0}_{1}'.format(
            unusable_body['transaction_id'], unusable_body['rater_id']
        )
        unusable = Rating(**unusable_body)
        third = stamped_rating(
            transaction_id='test-transaction-000003',
            score=1,
        )
        views = to_rating_views([first, unusable, third])
        self.assertEqual([view.id for view in views],
                         [first.id, third.id])

    def test_a_list_read_of_nothing_is_an_empty_list(self):
        """A user with no visible ratings is not an error."""
        self.assertEqual(to_rating_views([]), [])

    def test_the_moderated_projection_carries_the_state_and_reason(self):
        """What an administrator asked for, and only they receive."""
        rating = stamped_rating(
            review='Contains a phone number',
            moderation_status=ModerationStatus.REJECTED.value,
            moderation_reason='Personally identifying information',
        )
        view = to_moderated_rating_view(rating)
        self.assertEqual(
            view.moderation_status, ModerationStatus.REJECTED.value
        )
        self.assertEqual(
            view.moderation_reason,
            'Personally identifying information',
        )
        for field in PUBLIC_VIEW_FIELDS:
            with self.subTest(field=field):
                self.assertEqual(
                    getattr(view, field), getattr(rating, field)
                )


class TestUserRatingFields(unittest.TestCase):
    """The three fields this feature adds to ``User``.

    All three carry defaults, and that is what makes a migration
    unnecessary: a user document written before this feature existed
    deserialises cleanly, and each default is the semantically correct
    value for such a user - not verified, no average, no ratings.
    """

    def base(self, **overrides):
        """Return the required ``User`` fields, with overrides.

        Args:
            **overrides: Field values to replace.

        Returns:
            A kwargs dict for ``User``.
        """
        moment = utc_now()
        fields = {
            'id': 'test-buyer-000000000001',
            'email': 'buyer@example.test',
            'first_name': 'Test',
            'last_name': 'User',
            'role': 'buyer',
            'created_at': moment,
            'updated_at': moment,
        }
        fields.update(overrides)
        return fields

    def test_a_document_predating_the_feature_deserialises(self):
        """The reason there is no migration and no backfill."""
        user = User(**self.base())
        self.assertFalse(user.is_verified)
        self.assertIsNone(user.rating_average)
        self.assertEqual(user.rating_count, 0)

    def test_verification_is_a_strict_boolean(self):
        """The R1 gate must not turn on a truthiness accident.

        This one flag decides who may rate. Plain ``bool`` in pydantic v1
        accepts the strings ``'true'`` AND ``'false'`` and the integers 0
        and 1, so a document carrying the string ``'false'`` would
        authorise its owner to rate. Strictness makes such a document a
        malformed record - which the auth dependency answers as a 401 -
        rather than a verification decision taken by accident.
        """
        self.assertTrue(User(**self.base(is_verified=True)).is_verified)
        self.assertFalse(
            User(**self.base(is_verified=False)).is_verified
        )
        for value in ('true', 'false', 1, 0, 'yes', None):
            with self.subTest(value=repr(value)):
                with self.assertRaises(ValidationError):
                    User(**self.base(is_verified=value))

    def test_the_denormalised_aggregate_is_carried_as_stored(self):
        """Read straight off the user document, in one get-by-ID.

        Denormalised precisely so rendering a profile costs one read
        rather than a scan of that user's ratings, which is what keeps a
        popular seller's profile inside the response budget.
        """
        user = User(**self.base(rating_average=4.33, rating_count=3))
        self.assertEqual(user.rating_average, 4.33)
        self.assertEqual(user.rating_count, 3)

    def test_the_user_id_is_held_to_the_document_grammar(self):
        """It is the JWT subject and the document key at once."""
        for label, value in INVALID_DOCUMENT_IDS:
            with self.subTest(label):
                with self.assertRaises(ValidationError):
                    User(**self.base(id=value))

    def test_no_credential_field_is_declared_on_the_model(self):
        """``User`` is the shape every router annotates.

        A password hash on this model would become an attribute of the
        response every protected endpoint returns. The auth module reads
        the stored hash from the RAW document and removes it before
        constructing this model, which only works because the model has
        nowhere to put it.
        """
        for field in ('password', 'password_hash', 'hashed_password'):
            self.assertNotIn(field, User.__fields__)


if __name__ == '__main__':
    unittest.main()
