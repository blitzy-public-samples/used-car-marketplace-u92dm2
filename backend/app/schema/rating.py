"""Pydantic contracts for the bidirectional peer reputation system.

A buyer rates the seller and the seller rates the buyer for a purchase
they both took part in, implementing F010 "Review and Rating System"
from ``documentation/Software Requirements Specifications (SRS).md``
(L431, sub-requirements L437-L441). F010-2 written review is carried by
``Rating.review``; F010-3 aggregate display by :class:`RatingAggregate`
together with ``rating_average``/``rating_count`` on
``backend/app/schema/user.py``; F010-4 moderation by
``moderation_status``/``moderation_reason``. F010-5 search-ranking
integration is deliberately out of scope - this module supplies the
enabling data and nothing more.

This module declares *shape*, never *policy*. The two authorization
gates - the rater must be a verified user, and both parties must be
counterparties of the same transaction - are enforced in
``backend/app/services/rating.py`` and ``backend/app/api/ratings.py``.
Document-ID composition, the eligibility guard sequence, the
incremental-average arithmetic and the publication transition all live
there too, not here.

VALIDATION IS STRICT, BECAUSE COERCION IS A SECURITY PROPERTY HERE
-------------------------------------------------------------------
Pydantic's default numeric coercion would accept ``4.9``, ``"5"`` and
``True`` as a score and silently turn them into ``4``, ``5`` and ``1``.
A vote is an integer or it is not a vote, so ``score`` is a strict
constrained int on both the request model and the persisted one, and
``direction``/``moderation_status``/the timestamps are validated
against the shapes the rest of the system can actually interpret. An
unconstrained string in a state field does not fail loudly - it makes
every state check that reads it quietly wrong.

REVIEW TEXT IS PLAIN TEXT
-------------------------------------------------------------------
``review`` carries no markup semantics of any kind. It is normalised to
plain text server-side before it is persisted - Unicode composed,
control characters removed, line endings and blank runs collapsed - and
the length bound is re-applied to the normalised value. Nothing in the
stored value may be interpreted as markup by a consumer: every render
path must output-encode it, which React does by default, and no
consumer may pass it to ``dangerouslySetInnerHTML`` or an equivalent.
The client also sanitises before submitting, but that is a convenience;
this module is the authoritative control.

Pydantic v1 semantics apply throughout. ``pydantic==1.10.13`` is pinned
in ``backend/requirements.txt`` because ``backend/app/core/config.py``
imports ``BaseSettings`` from ``pydantic``, which v2 removed.

Field names are snake_case and are load-bearing beyond this module.
``infrastructure/firestore.indexes.json`` indexes ``ratee_id``,
``is_published``, ``created_at``, ``transaction_id`` and ``rater_id``
by exact string; a mismatch there raises no error, it just yields an
index that silently serves nothing. ``frontend/src/schema/rating.ts``
mirrors every name 1:1 in camelCase.
"""
import unicodedata
from datetime import datetime
from enum import Enum
from typing import Any, Optional

from google.cloud import firestore
from pydantic import BaseModel, Field, conint, constr, validator

from app.core.config import settings

# The bound the whole feature votes within, declared once so the request
# model and the persisted model cannot drift apart. ``strict=True`` is
# the load-bearing part: without it Pydantic v1 coerces 4.9 to 4, "5"
# to 5 and True to 1, so the server would accept a vote that is not an
# integer at all. With it, only a genuine in-range int passes, and
# anything else surfaces as a 422 before any handler logic runs.
RatingScore = conint(
    strict=True,
    ge=settings.RATING_MIN,
    le=settings.RATING_MAX,
)

# Control characters are stripped from review text, except the two that
# are legitimate in prose. ``\r`` is not listed because line endings are
# normalised to ``\n`` first.
_ALLOWED_CONTROL_CHARACTERS = ('\n', '\t')


# A Firestore document ID, validated against the grammar Firestore itself
# imposes. This is a security boundary, not tidiness: ``transaction_id``
# arrives from the client and is used to CONSTRUCT document paths - both
# the transaction it looks up and, composed with the rater's ID, the
# rating it creates. An unconstrained string there admits three real
# failures. A value containing ``/`` is read by the client library as a
# nested path rather than an ID, so ``document('a/b')`` addresses
# something else entirely or raises outright. A control character or an
# oversized value produces a document whose key cannot be typed back,
# leaving an orphaned record no operator can find. And an ID matching
# ``__.*__`` collides with the namespace Firestore reserves for itself.
#
# The rules encoded below are Firestore's own: an ID may not contain a
# forward slash, may not be the single or double period, and may not
# match ``__.*__``. Two additions are this project's:
#
# * ASCII control characters (\x00-\x1F and \x7F) are excluded. They are
#   not forbidden by Firestore, but nothing in this system legitimately
#   produces one, and a newline inside an identifier is how a log line
#   gets forged.
# * The length ceiling is 128 rather than Firestore's 1500 bytes. Two IDs
#   are joined with an underscore to form a rating's key, so each half
#   must leave room for the other; 128 is far above the 20 characters a
#   Firestore auto-ID actually occupies, and holding the composite well
#   inside the limit means an over-long transaction ID is rejected with a
#   422 instead of surfacing as an infrastructure error at write time.
#
# ``\Z`` rather than ``$`` is deliberate and load-bearing: ``$`` also
# matches immediately before a trailing newline, so a pattern anchored
# with ``$`` would accept ``"abc\n"`` as a valid identifier. ``\Z``
# matches only at the true end of the string.
DOCUMENT_ID_MAX_LENGTH = 128

_DOCUMENT_ID_PATTERN = (
    r'^(?!\.\.?\Z)'          # not "." and not ".."
    r'(?!__.*__\Z)'          # not Firestore's reserved __.*__ namespace
    r'[^/\x00-\x1F\x7F]+\Z'  # no slash, no ASCII control characters
)

DocumentId = constr(
    strict=True,
    min_length=1,
    max_length=DOCUMENT_ID_MAX_LENGTH,
    regex=_DOCUMENT_ID_PATTERN,
)


class RatingDirection(str, Enum):
    """Which way along a transaction a rating travels.

    Defined here as the single source of truth so the service layer,
    the router and the tests share one definition instead of
    scattering string literals.

    The value is derived server-side from the cited transaction
    document and is never accepted from a client, which is what makes
    direction spoofing structurally impossible rather than merely
    validated against.

    Subclasses ``str`` so a member compares equal to, and serialises
    as, its wire value.
    """

    BUYER_TO_SELLER = 'buyer_to_seller'
    SELLER_TO_BUYER = 'seller_to_buyer'


class ModerationStatus(str, Enum):
    """Policy state governing whether a review may be displayed.

    Transitions are driven by policy violations only - abuse,
    personally identifying information, profanity - and NEVER by the
    score. A low score is never itself grounds for withholding, and
    the aggregate counts every published rating regardless of value.
    That constraint is not stylistic: the FTC Rule on the Use of
    Consumer Reviews and Testimonials (16 CFR Part 465) prohibits
    suppressing reviews on the basis of rating or negative sentiment.

    Review CONTENT is displayed only once it reaches ``APPROVED``,
    which is what "moderation before display" means; the score is
    displayed and counted for every published rating, because a number
    cannot carry a policy violation and suppressing it by sentiment is
    exactly what the rule forbids.
    """

    PENDING = 'pending'
    APPROVED = 'approved'
    REJECTED = 'rejected'


def enum_value(value: Any, enumeration: Any, label: str) -> str:
    """Return the wire value of ``value`` within ``enumeration``.

    Accepts either an enumeration member or its value string and always
    returns a plain ``str``, so an attribute never holds a member whose
    ``str()`` on Python 3.9 would render as ``'ClassName.MEMBER'`` and
    corrupt a write, a log line or a query.

    Args:
        value: Candidate member or value string.
        enumeration: The ``str``-subclassing ``Enum`` to check against.
        label: Human-readable field name, used in the error message.

    Returns:
        The canonical value string.

    Raises:
        ValueError: ``value`` is not a member of ``enumeration``.
    """
    if isinstance(value, enumeration):
        return str(value.value)
    permitted = sorted(member.value for member in enumeration)
    if isinstance(value, str) and value in permitted:
        return value
    raise ValueError(
        'Unknown {0}: {1!r}. Permitted values are {2}'.format(
            label,
            value,
            permitted,
        )
    )


def as_plain_text(value: Optional[str]) -> Optional[str]:
    """Normalise user-authored text to the plain-text contract.

    The server-side half of the review content policy, applied at the
    request boundary and again on anything read back, so no consumer
    can receive review text this function has not passed. It does not
    escape or rewrite the author's words - the stored value is plain
    text that every render path must output-encode - it removes what
    prose cannot legitimately contain:

    * Unicode is composed (NFC), so visually identical strings compare
      and truncate consistently.
    * ``\\r\\n`` and ``\\r`` become ``\\n``.
    * Control characters other than newline and tab are dropped. They
      are invisible in a review yet can break log lines, terminal
      output and CSV exports.
    * Runs of three or more newlines collapse to two, and surrounding
      whitespace is trimmed.
    * Text that is empty once normalised becomes ``None``, so "no
      review" is one state rather than two.

    The length bound is re-applied afterwards rather than trusted from
    before, because normalisation is not guaranteed to shorten a string.

    Args:
        value: Raw text, or ``None``.

    Returns:
        The normalised text, or ``None`` when there is nothing left.

    Raises:
        ValueError: The normalised text exceeds
            ``settings.RATING_REVIEW_MAX_LENGTH``.
    """
    if value is None:
        return None
    text = unicodedata.normalize('NFC', str(value))
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    text = ''.join(
        character for character in text
        if character in _ALLOWED_CONTROL_CHARACTERS
        or unicodedata.category(character) not in ('Cc', 'Cf')
    )
    while '\n\n\n' in text:
        text = text.replace('\n\n\n', '\n\n')
    text = text.strip()
    if not text:
        return None
    limit = int(settings.RATING_REVIEW_MAX_LENGTH)
    if len(text) > limit:
        raise ValueError(
            'Review must be at most {0} characters'.format(limit)
        )
    return text


class Rating(BaseModel):
    """One directional rating, as persisted in ``ratings``.

    Append-only by design. A submitted score is never rewritten, so
    this model exposes no mechanism to do so; a correction is a
    ``moderation_status`` transition carrying a recorded reason.

    ``direction`` and ``moderation_status`` are typed as plain ``str``
    rather than as their enum classes on purpose. Pydantic v1 does not
    validate field defaults by default, so an enum-typed field would
    keep the raw member as its default; on Python 3.9 ``str()`` of such
    a member yields ``'ModerationStatus.PENDING'`` instead of
    ``'pending'``, which would silently corrupt any write, log or query
    that stringifies it. Writing the default as ``.value`` keeps a
    genuine ``str`` on the attribute and in ``.dict()``. Both fields
    are nevertheless VALIDATED against their enumerations below,
    including their defaults, so an unrecognised state cannot reach the
    read and publication paths that branch on it - and a document
    carrying one is reported as malformed rather than misclassified.
    """

    id: str
    transaction_id: str
    # Denormalised from the transaction so a rating can be rendered
    # with context without a second document read.
    vehicle_listing_id: str
    rater_id: str
    ratee_id: str
    direction: str
    score: RatingScore
    review: Optional[str] = Field(
        None, max_length=settings.RATING_REVIEW_MAX_LENGTH
    )
    # Created unpublished: under the double-blind model a rating
    # becomes visible only once the counterparty submits theirs or the
    # rating window elapses. The aggregate reflects published ratings
    # only, which is what removes the incentive for review extortion.
    is_published: bool = False
    moderation_status: str = ModerationStatus.PENDING.value
    moderation_reason: Optional[str] = None
    # Permissive ANNOTATION by necessity, constrained by the validator
    # below. The write path assigns ``firestore.SERVER_TIMESTAMP``,
    # which is a sentinel object and not a ``datetime``; a strict
    # ``datetime`` annotation rejects it with a ValidationError, so
    # constructing a Rating from the payload about to be written would
    # fail. Reading the document back yields a real
    # ``DatetimeWithNanoseconds`` instead. All three shapes - sentinel,
    # real timestamp, and absence before the server has stamped them -
    # are accepted, and nothing else is: an arbitrary value here would
    # reach the window arithmetic and the ordering key.
    created_at: Optional[Any] = None
    updated_at: Optional[Any] = None

    @validator('direction', always=True)
    def _validate_direction(cls, value: Any) -> str:
        """Constrain ``direction`` to the two derived values."""
        return enum_value(value, RatingDirection, 'rating direction')

    @validator('moderation_status', always=True)
    def _validate_moderation_status(cls, value: Any) -> str:
        """Constrain ``moderation_status`` to its three states."""
        return enum_value(value, ModerationStatus, 'moderation status')

    @validator('review')
    def _validate_review(cls, value: Optional[str]) -> Optional[str]:
        """Hold stored review text to the plain-text contract."""
        return as_plain_text(value)

    @validator('created_at', 'updated_at', always=True)
    def _validate_timestamp(cls, value: Any) -> Any:
        """Accept only the timestamp shapes this system produces.

        A real ``datetime`` (what Firestore returns), the server-side
        sentinel (what the write path sends), or absence. Anything else
        would flow into the rating-window comparison and the ordering
        key, where it would raise at a distance or sort nonsensically.
        """
        if value is None or isinstance(value, datetime):
            return value
        if value is firestore.SERVER_TIMESTAMP:
            return value
        raise ValueError(
            'Timestamp must be a datetime, the Firestore server '
            'timestamp sentinel, or absent'
        )


class RatingCreate(BaseModel):
    """The request body accepted by ``POST /api/ratings``.

    Declares three fields and no more. ``ratee_id`` and ``direction``
    are absent by design rather than by oversight: both are derived
    server-side from the cited transaction document, which is what
    makes counterparty spoofing and self-rating structurally
    impossible instead of merely validated against. Do not declare
    them here for convenience or symmetry.

    ``extra = 'allow'`` is deliberate and is not a relaxation. A client
    that supplies ``ratee_id`` anyway is making a claim about who it
    believes the counterparty is, and that claim has to be VISIBLE to
    be refused: with Pydantic's default ``ignore`` it would be dropped
    before the service could compare it, so a caller asserting a
    relationship the transaction does not establish would be answered
    with a silently redirected rating instead of a rejection. Retaining
    it lets ``services/rating.py`` check it against the derived
    counterparty and refuse a mismatch. Retention is safe because
    nothing here is trusted: the write path names every field it
    persists explicitly and never serialises this model, so an extra
    key cannot reach the datastore.

    These bounds are the authoritative ones - the client-side Zod
    mirror is a convenience, not a substitute. Because Pydantic
    validates the request body before the handler body runs, an
    out-of-range or non-integer score, or an over-long review, surfaces
    as a 422 with no handler logic reached at all.

    Two properties of this model are security properties rather than
    house style, because this is the one place in the feature where a
    value crosses from a client into the datastore:

    * ``transaction_id`` is a validated Firestore document ID rather
      than a bare ``str``. It is used to CONSTRUCT document paths - the
      transaction it looks up, and, composed with the rater's ID, the
      rating it creates - so an unconstrained string admits a value
      containing ``/`` being read as a nested path, a control character
      forging a log line, and an oversized key leaving an orphaned
      record. See ``DocumentId`` above for the encoded grammar.
    * ``score`` is STRICT, via the shared ``RatingScore`` the persisted
      model uses too, so the request contract and the stored contract
      cannot drift apart. Pydantic v1 coerces by default: ``True``
      becomes ``1``, ``"4"`` becomes ``4`` and ``4.7`` becomes ``4``.
      Every one of those records a score the caller never chose - the
      last silently rounding a rating down.

    Unknown keys are RETAINED rather than dropped; ``Config`` below
    explains why that is the safe choice for this particular model.
    """

    transaction_id: DocumentId
    score: RatingScore
    review: Optional[str] = Field(
        None, max_length=settings.RATING_REVIEW_MAX_LENGTH
    )

    class Config:
        # Pydantic's default is to DROP unrecognised keys silently,
        # which is the wrong behaviour for exactly one field:
        # ``ratee_id``. The counterparty is derived server-side, so a
        # body carrying its own ``ratee_id`` asserts a relationship it
        # does not get to assert - and silently discarding that
        # assertion would answer 201 to a request the server did not
        # honour, leaving the caller believing they rated somebody they
        # did not.
        #
        # Retaining the key is what keeps the claim OBSERVABLE, so
        # ``submit_rating`` can compare it against the derived
        # counterparty and raise ``NotATransactionParticipant`` on a
        # mismatch - the 403 the plan's acceptance criterion A4
        # specifies. Refusing the body outright would answer 422
        # instead and leave that guard permanently unreachable.
        #
        # This is safe because ``__fields__`` remains exactly
        # {transaction_id, score, review}: the write path names every
        # persisted field explicitly and never serialises this model,
        # so a retained extra key cannot reach the datastore.
        extra = 'allow'

    @validator('review')
    def _validate_review(cls, value: Optional[str]) -> Optional[str]:
        """Normalise submitted review text to plain text.

        The authoritative application of the content policy: it runs at
        the request boundary, so no submission path can bypass it.
        """
        return as_plain_text(value)


class RatingAggregate(BaseModel):
    """Denormalised reputation summary for a single user.

    Mirrors ``rating_average``/``rating_count`` on
    ``backend/app/schema/user.py``, which are maintained inside the
    same transaction that publishes a rating so that reading a profile
    costs one get-by-ID rather than a scan of the user's ratings.

    ``average`` is nullable and ``count`` defaults to zero so that "no
    ratings yet" remains a first-class state, distinguishable from a
    genuine average of zero. The reputation badge renders that
    distinction explicitly, so ``average`` must never default to 0.0.

    The two fields are validated TOGETHER, because either one alone is
    satisfiable by an impossible pair: a positive count beside a null
    average, or an average of NaN, describes a reputation that cannot
    exist and would be rendered to a user as fact. The invariant is
    ``count == 0`` if and only if ``average is None``, with the average
    finite and inside the rating bound whenever there is one.

    Reflects published ratings only, and includes every one of them
    whatever the score.
    """

    average: Optional[float] = None
    count: conint(strict=True, ge=0) = 0

    @validator('average')
    def _validate_average(cls, value: Optional[float]) -> Optional[float]:
        """Require a finite average inside the rating bound."""
        if value is None:
            return None
        number = float(value)
        # NaN and infinity survive a float annotation and then
        # propagate through every later fold and every rendering.
        if number != number or number in (float('inf'), float('-inf')):
            raise ValueError('Average must be a finite number')
        if not settings.RATING_MIN <= number <= settings.RATING_MAX:
            raise ValueError(
                'Average must be between {0} and {1}'.format(
                    settings.RATING_MIN,
                    settings.RATING_MAX,
                )
            )
        return number

    @validator('count', always=True)
    def _validate_count(cls, value: int, values: dict) -> int:
        """Require count and average to describe the same reality."""
        has_average = values.get('average') is not None
        if value > 0 and not has_average:
            raise ValueError(
                'A positive rating count requires an average'
            )
        if value == 0 and has_average:
            raise ValueError(
                'An average requires a positive rating count'
            )
        return value


class EligibilityDecision(BaseModel):
    """Outcome of the rating eligibility guard sequence.

    Returned by ``GET /api/ratings/eligibility/{transaction_id}`` so
    the interface can disable the submission control with a specific
    explanation, rather than letting someone compose a rating and only
    then fail on a 403. Reporting the reason up front is what makes
    the unavailable state perceivable instead of merely inert.

    ``reason`` is prose, not an error code. It is rendered verbatim as
    the disabled-state explanation and reused as the ``HTTPException``
    detail, so it holds sentences such as "Only verified users can
    submit ratings" or "You have already rated this transaction". It is
    deliberately unconstrained by any enum - composing the sentence
    belongs to ``services/rating.py``. ``ratee_id`` and ``direction``
    are populated only once the caller is confirmed a participant, and
    stay ``None`` on the decisions that never got that far.
    """

    eligible: bool
    reason: Optional[str] = None
    ratee_id: Optional[str] = None
    direction: Optional[str] = None
    already_rated: bool

    @validator('direction')
    def _validate_direction(cls, value: Optional[str]) -> Optional[str]:
        """Constrain a reported direction to the derived values."""
        if value is None:
            return None
        return enum_value(value, RatingDirection, 'rating direction')
