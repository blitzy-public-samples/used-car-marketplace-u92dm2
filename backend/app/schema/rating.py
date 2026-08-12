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
the length bound is applied to the normalised value. Nothing in the
stored value may be interpreted as markup by a consumer: every render
path must output-encode it, which React does by default, and no
consumer may pass it to ``dangerouslySetInnerHTML`` or an equivalent.
The client also sanitises before submitting, but that is a convenience;
this module is the authoritative control.

The review therefore carries TWO bounds, and they are not the same
bound twice. ``settings.RATING_REVIEW_MAX_LENGTH`` is the semantic
limit, enforced by ``as_plain_text`` against the normalised text - the
value that is actually stored, and the value a reader counts.
``REVIEW_RAW_MAX_LENGTH`` is a much larger ceiling on the raw request
body, present only so an unbounded string cannot reach the normaliser.
Collapsing the two, by giving the field a ``max_length`` equal to the
semantic limit, would quietly make the raw length the binding
constraint: pydantic v1 evaluates a field length constraint before any
validator on that field, so decomposed Unicode, zero-width characters,
CRLF line endings and trailing whitespace would each consume a
reviewer's allowance even though none of them survives normalisation.

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
import logging
import unicodedata
from datetime import datetime
from enum import Enum
from typing import Any, List, Optional

from google.cloud import firestore
from pydantic import (
    BaseModel,
    Field,
    StrictBool,
    conint,
    constr,
    root_validator,
    validator,
)

from app.core.config import settings

logger = logging.getLogger(__name__)

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

# Characters that cannot appear in a value this module calls plain text.
#
# This is the enforcement of a claim the module makes about itself. The
# stored review is documented as carrying no markup semantics of any kind,
# and normalisation alone does not make that true: it composes Unicode and
# strips control characters, and leaves ``<script>alert(1)</script>``
# exactly as it arrived. React escapes it on today's only render path, so
# the claim held by accident rather than by construction - and an accident
# is not a control. The moment any consumer is added that does not escape
# by default (an HTML email, a PDF receipt, a CSV opened in a spreadsheet,
# ``dangerouslySetInnerHTML``), the stored value becomes the injection.
#
# So the value is REJECTED rather than rewritten. Stripping tags would be
# the wrong remedy twice over: it silently edits somebody's review, and it
# is a sanitiser - an allow-list of what to remove - which is exactly the
# kind of control that gets bypassed. Refusing is unambiguous, is
# reportable to the author, and cannot be worked around by a novel
# encoding.
#
# The set is exactly the two TAG DELIMITERS, and nothing wider. Without a
# ``<`` or a ``>`` in the stored value, no element, comment, CDATA section
# or processing instruction can be formed from it in HTML, XHTML, XML or
# SVG - which is the property that has to hold, and it holds whether or not
# the consumer escapes.
#
# Three characters are deliberately NOT in the set, because each would
# refuse ordinary prose to guard against a different consumer's bug:
#
# * ``&`` is permitted. An entity reference in an HTML text node decodes to
#   TEXT and is never re-parsed as markup, so ``&lt;script&gt;`` cannot
#   become an element on its own; only a literal ``<`` can, and that is
#   already refused. Rejecting ``&`` would protect only against a consumer
#   that decodes entities twice - a distinct defect in that consumer - at
#   the cost of refusing "A/C & heat both work", which is what a review of
#   a car actually says.
# * Quotes and apostrophes are permitted, for the same reason: they matter
#   only inside an unquoted HTML attribute, which is a consumer defect, and
#   they are unavoidable in prose.
#
# The cost of the rule is that "0-60 in >6s" must be written "over 6s". The
# error message names the offending character so that is discoverable
# rather than mysterious, and it is a small price for a stored value that
# cannot become markup anywhere.
_MARKUP_CHARACTERS = ('<', '>')

# Headroom the RAW submitted review is allowed over the bound that
# actually applies to it, expressed as a multiple of
# ``settings.RATING_REVIEW_MAX_LENGTH``.
#
# Two bounds govern the review, and they answer different questions.
# ``as_plain_text`` applies the SEMANTIC bound to the NORMALISED value -
# the text that is actually persisted and read back - and that is the
# limit the product means by "2000 characters". The ceiling below is a
# denial-of-service guard on the raw request body, and nothing more: it
# stops an unbounded string reaching the normaliser at all.
#
# Both are needed, and putting the semantic bound on the raw value
# instead - which is what a ``max_length`` equal to
# ``RATING_REVIEW_MAX_LENGTH`` on the field does - silently makes the
# raw length the binding constraint, because pydantic v1 evaluates a
# field's length constraint BEFORE any ``@validator`` on that field. The
# normaliser then never sees a value it could have brought into range,
# and every one of these legitimate submissions is refused despite
# normalising to 2000 characters or fewer: text typed on macOS or iOS,
# which emits decomposed (NFD) Unicode, so each accented letter costs
# two code points until it is composed; text pasted with trailing
# whitespace, zero-width characters or CRLF line endings; and text with
# long runs of blank lines that collapse. A reviewer writing in a
# diacritic-heavy language would lose roughly half of a stated
# allowance, and the client's character counter - which counts what the
# reader sees - would disagree with the server about what "2000
# characters" means.
#
# The factor is 4 because a decomposed character can carry more than one
# combining mark (Vietnamese, for instance, reaches three code points
# for one letter), so a raw string up to four times the bound can still
# normalise into range, while anything beyond that is not prose that got
# longer in transit - it is a payload.
#
# MIRRORED CLIENT-SIDE, and that is what makes this two-bound design a
# contract rather than a server-only nicety.
# ``frontend/src/schema/rating.ts`` declares the same factor, derives the
# same ceiling, and ports ``as_plain_text``'s normalisation as
# ``normalizeReviewText`` so its Zod field applies these two bounds in
# this order against the same string. Before it did, the client bounded
# the RAW text at the semantic limit and was strictly stricter than the
# server: it refused reviews this module would have accepted, and its
# character counter and this bound disagreed about what "2000
# characters" means. Change either side and change both.
REVIEW_RAW_LENGTH_FACTOR = 4

# The raw ceiling itself. Bound at import, from the same setting the
# semantic limit comes from, so the two can never drift apart.
REVIEW_RAW_MAX_LENGTH = (
    REVIEW_RAW_LENGTH_FACTOR * settings.RATING_REVIEW_MAX_LENGTH
)


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

# A rating's document ID, which is NOT a plain ``DocumentId``: it is the
# COMPOSITE ``"{transaction_id}_{rater_id}"`` that
# ``app/services/rating.py:_rating_document_id`` builds, so it can be as
# long as both components plus the joining underscore.
#
# It needs a contract of its own because reusing the component bound for
# the composite is an off-by-a-factor-of-two that only shows up in
# production. ``DocumentId`` admits a 128-character transaction ID and a
# 128-character rater ID, both legitimately creatable, whose rating key is
# 257 characters. ``POST /api/ratings`` would create that document happily
# and ``PATCH /api/ratings/{rating_id}/moderation`` would then refuse the
# very key it had just minted with a 422 - a rating that exists, is
# visible, and cannot be moderated. The service's own
# ``_is_valid_rating_id`` mirrors this bound for the same reason, so a
# reachable rating is not reported as "not found" one layer deeper.
#
# The grammar is otherwise identical. It is the same slash-free,
# control-character-free rule, and the composite cannot collide with
# Firestore's reserved ``__.*__`` namespace or with ``.``/``..`` while
# each half is already refused those forms. 257 bytes is far inside
# Firestore's own 1500-byte key limit, so nothing here trades one bound
# for a worse one.
RATING_ID_MAX_LENGTH = 2 * DOCUMENT_ID_MAX_LENGTH + 1

RatingDocumentId = constr(
    strict=True,
    min_length=1,
    max_length=RATING_ID_MAX_LENGTH,
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


# Bound on ``moderation_reason``, the free text a moderator records when
# withholding a review. It is deliberately much smaller than the review
# bound: a policy citation is a short phrase ("contains a phone number"),
# not prose, and the field is written by staff rather than by the public.
#
# It needs a bound at all for the same reason the review does. The value
# is persisted on the rating document and is read back by the author of
# that rating, so an unbounded moderator note is an unbounded write into
# a document whose size Firestore caps at 1 MiB, and an unbounded string
# on a response. 500 characters is ample for a policy reference and
# leaves the document nowhere near that cap.
MODERATION_REASON_MAX_LENGTH = 500

# The single unrecognised key ``RatingCreate`` tolerates in a request
# body. Named once here so the request contract and the service guard
# that refuses a mismatched claim refer to the same symbol rather than
# repeating a string literal that could drift apart.
#
# It is tolerated so that the claim can be REFUSED rather than ignored;
# see ``RatingCreate`` for why those are different outcomes.
RATEE_ID_CLAIM = 'ratee_id'


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


def as_plain_text(
    value: Optional[str],
    max_length: Optional[int] = None,
    label: str = 'Review',
) -> Optional[str]:
    """Normalise author-supplied text to the plain-text contract.

    The server-side half of the content policy, applied at the request
    boundary and again on anything read back, so no consumer can
    receive text this function has not passed. It does not
    escape or rewrite the author's words - the stored value is plain
    text that every render path must output-encode - it removes what
    prose cannot legitimately contain, and REFUSES what would make the
    plain-text claim false:

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
    * Text still containing a tag delimiter - ``<`` or ``>`` - is
      REFUSED. This is what makes "the stored value carries no markup
      semantics" a property of the data rather than a hope about its
      consumers, and it is checked after the removals above so an
      obfuscation such as ``<scr\\x00ipt>`` cannot slip a bracket through
      by hiding it behind a stripped character.

    The length bound is applied HERE, to the normalised text, and not to
    the raw input: the normalised value is what gets persisted, read
    back and counted by a reader, so it is the only value the limit can
    honestly describe. Normalisation is not guaranteed to shorten a
    string either, so the check cannot be inferred from the input
    length in either direction. The raw input carries a separate, far
    larger ceiling declared on the fields below
    (``REVIEW_RAW_MAX_LENGTH``), whose only job is to keep an unbounded
    body away from this function.

    THIS IS THE ONE REVIEW POLICY, AND THE CLIENT MIRRORS IT
    -------------------------------------------------------------------
    ``frontend/src/schema/rating.ts`` performs the SAME normalisation in
    the same order before applying the same semantic bound, so a character
    counter in the interface and this function agree on what "2000
    characters" means. That agreement is the whole reason the order is
    specified rather than incidental: applying a bound to raw text on one
    side and to normalised text on the other makes the two disagree by
    exactly the amount that normalisation removes, which is largest for
    precisely the users least able to diagnose it - anyone typing a
    diacritic-heavy language on a platform that emits decomposed Unicode.
    The client is a convenience and this function is the authority; they
    are written to reach the same verdict all the same.

    The same normalisation serves every field of author-supplied prose
    on a rating - the public review and the moderator's reason - because
    both are persisted, both are read back, and neither can legitimately
    contain a control character. Only the bound and the name in the error
    message differ, so both are parameters rather than duplicated logic.

    Args:
        value: Raw text, or ``None``.
        max_length: Maximum length permitted after normalisation.
            Defaults to ``settings.RATING_REVIEW_MAX_LENGTH``.
        label: Human-readable field name for the error message.

    Returns:
        The normalised text, or ``None`` when there is nothing left.

    Raises:
        ValueError: The normalised text contains a markup character, or
            exceeds the applicable bound.
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
    # Checked AFTER the removals, so a bracket cannot be smuggled through
    # behind a character that normalisation was going to strip anyway.
    present = [
        character for character in _MARKUP_CHARACTERS if character in text
    ]
    if present:
        raise ValueError(
            '{0} must be plain text and must not contain {1}'.format(
                label,
                ' or '.join(repr(character) for character in present),
            )
        )
    limit = int(
        settings.RATING_REVIEW_MAX_LENGTH if max_length is None
        else max_length
    )
    if len(text) > limit:
        raise ValueError(
            '{0} must be at most {1} characters'.format(label, limit)
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
    # ``max_length`` here is the RAW denial-of-service ceiling, not the
    # review limit. The limit that means "2000 characters" is applied by
    # ``as_plain_text`` in the validator below, to the normalised text
    # this field actually stores; see ``REVIEW_RAW_MAX_LENGTH``.
    review: Optional[str] = Field(None, max_length=REVIEW_RAW_MAX_LENGTH)
    # Created unpublished: under the double-blind model a rating
    # becomes visible only once the counterparty submits theirs or the
    # rating window elapses. The aggregate reflects published ratings
    # only, which is what removes the incentive for review extortion.
    #
    # ``StrictBool`` rather than ``bool``, for the same reason
    # ``RatingScore`` is a strict int. Plain ``bool`` in Pydantic v1
    # COERCES: it accepts the strings 'false', 'no' and '0' and the ints
    # 0 and 1, and - the case that matters - it maps the string 'true'
    # AND the string 'false' onto real booleans while rejecting neither.
    # This field decides whether a rating is visible to the public and
    # whether it counts toward a reputation, and the service branches on
    # the raw stored value. A document whose ``is_published`` is the
    # STRING 'false' must therefore be reported as malformed, never
    # silently interpreted - in either direction. Strict typing makes
    # that a validation failure at the boundary instead of a visibility
    # decision taken on a truthiness accident.
    is_published: StrictBool = False
    moderation_status: str = ModerationStatus.PENDING.value
    # The POLICY CODE justifying a moderation decision, never free text and
    # never anything derived from the score. The service constrains it to an
    # allow-list (``MODERATION_REASON_CODES``), which is what makes
    # sentiment-neutrality a property the system holds rather than one it
    # asks its operators to remember: there is no expressible way to record
    # "low score" as a reason. Machine-readable on purpose, so a moderation
    # history can be audited by grouping rather than by reading prose.
    moderation_reason: Optional[str] = None
    # The human specifics behind the code - which phrase was abusive, whose
    # phone number appeared. Separate from the code precisely so the code
    # can stay closed while the detail stays free, and OPERATIONAL: it
    # reaches only the administrator response model, never a public or
    # participant read.
    moderation_note: Optional[str] = None
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

    @validator('moderation_reason')
    def _validate_moderation_reason(
        cls,
        value: Optional[str],
    ) -> Optional[str]:
        """Hold the moderator's reason code to a bounded plain-text contract.

        The value is a policy code the service picks from an allow-list, so
        this is a backstop rather than the constraint: it keeps a stored
        document that predates the allow-list, or one written outside the
        service, from carrying control characters or an unbounded string
        into a response. Whitespace-only text normalises to ``None``, which
        keeps "no reason recorded" a single state rather than two.
        """
        return as_plain_text(
            value,
            max_length=MODERATION_REASON_MAX_LENGTH,
            label='Moderation reason',
        )

    @validator('moderation_note')
    def _validate_moderation_note(
        cls,
        value: Optional[str],
    ) -> Optional[str]:
        """Hold the operator's note to a bounded plain-text contract.

        Staff-authored rather than public, but still persisted on this
        document and still returned to an administrator, so it gets the
        same normalisation as the review and a bound of its own: an
        unbounded note is an unbounded write into a document Firestore
        caps at 1 MiB, and a newline in it forges a line in any log or
        export that renders it.
        """
        return as_plain_text(
            value,
            max_length=MODERATION_REASON_MAX_LENGTH,
            label='Moderation note',
        )

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


class RatingView(BaseModel):
    """One rating as an API RESPONSE. The least-data public projection.

    Deliberately a different model from :class:`Rating`, because a
    persistence record and a response are answerable to different
    questions, and using one object for both got both wrong at once.

    WHAT IT OMITS, AND WHY THAT IS THE POINT
    -------------------------------------------------------------------
    ``moderation_status`` and ``moderation_reason`` are absent. They are
    OPERATIONAL state: they record that a moderator acted and the policy
    basis they cited, written for the people who run the platform. Serving
    them on a public read publishes the moderation queue - a reader can
    see which reviews were withheld and read the internal note explaining
    why - and serving them to a participant hands the same information to
    the person most motivated to argue with it. Neither read needs them:
    the visibility decision has ALREADY been applied by
    ``app/services/rating.py:_visible_projection`` before a view is built,
    so a withheld review arrives with ``review`` empty, which is the only
    fact a reader acts on. An author still sees their own words in full
    through the per-transaction read, so nothing vanishes unexplained.

    The moderation fields are reachable only through
    :class:`ModeratedRatingView`, which the admin-only moderation endpoint
    returns. That is the "deliberately specified user-facing explanation"
    boundary: an administrator asked for the state and gets it; nobody
    else is told.

    WHAT IT KEEPS, ALSO DELIBERATELY
    -------------------------------------------------------------------
    ``transaction_id``, ``rater_id``, ``ratee_id``, ``direction`` and
    ``vehicle_listing_id`` are retained. This is a decision rather than an
    oversight, and it is worth stating because it looks like a leak:

    * ``rater_id`` is what makes a rating ATTRIBUTABLE, which is the point
      of a reputation system - an unattributable score cannot be weighed
      for context, and the specification's authenticity requirement is
      precisely that a rating comes from somebody who actually transacted.
    * ``ratee_id`` and ``direction`` are the subject of the read; a caller
      asking for the ratings a user received already knows who that is.
    * ``vehicle_listing_id`` exists on the record so a rating can be shown
      with context - "rated after buying this car" - without a second
      document read. Removing it defeats the field.
    * ``transaction_id`` is the evidence the rating is anchored to a real
      exchange, and it is also the first half of ``id``.

    That last point decides it: the document ID IS
    ``"{transaction_id}_{rater_id}"`` and ``id`` is returned, so blanking
    the two fields while returning the value they compose would cost the
    interface real capability and conceal nothing. All of these are opaque
    Firestore identifiers - never names, emails or contact details - and
    none addresses a document a caller can read without passing that
    endpoint's own authorization.

    TIMESTAMPS ARE REQUIRED HERE AND OPTIONAL ON :class:`Rating`
    -------------------------------------------------------------------
    That asymmetry is the second reason this model exists. ``Rating`` must
    tolerate ``firestore.SERVER_TIMESTAMP``, an unserialisable sentinel,
    because the write path constructs a model from the body it is about to
    write; and it must tolerate absence, because a stored document is not
    typed. A RESPONSE has neither excuse: the service substitutes a real
    UTC datetime on every value it hands back, and the official client
    (``frontend/src/schema/rating.ts``) declares both fields as a required
    ``z.date()`` and throws on null. Typing them ``Optional[Any]`` on the
    response made a response the server may legally emit and the client
    will always reject - the worst kind of contract, because both sides
    are behaving as specified. Requiring a real ``datetime`` here means
    such a response cannot be constructed in the first place.
    """

    id: str
    transaction_id: str
    vehicle_listing_id: str
    rater_id: str
    ratee_id: str
    direction: str
    score: RatingScore
    review: Optional[str] = None
    is_published: StrictBool
    created_at: datetime
    updated_at: datetime

    @validator('direction')
    def _validate_direction(cls, value: Any) -> str:
        """Constrain ``direction`` to the two derived values."""
        return enum_value(value, RatingDirection, 'rating direction')


class ModeratedRatingView(RatingView):
    """One rating as an ADMINISTRATOR's response. F010-4.

    :class:`RatingView` plus the two moderation fields, returned by
    ``PATCH /api/ratings/{rating_id}/moderation`` and by nothing else.

    The endpoint is gated on ``role == 'admin'``, so this is the one place
    moderation state crosses the API boundary - and it crosses it to the
    caller who just set it, which is the only audience for whom the state
    and its policy basis are the answer to the question asked. Every other
    read returns :class:`RatingView`.

    ``moderation_reason`` is a POLICY CODE and never anything derived from
    the score - the service constrains it to an allow-list, so a
    score-based justification is not expressible. ``moderation_note``
    carries the human specifics behind that code. Both exist so a withheld
    review carries its justification on the record, which is what makes
    the sentiment-neutrality of the decision auditable rather than merely
    asserted.
    """

    moderation_status: str
    moderation_reason: Optional[str] = None
    moderation_note: Optional[str] = None

    @validator('moderation_status')
    def _validate_moderation_status(cls, value: Any) -> str:
        """Constrain ``moderation_status`` to its three states."""
        return enum_value(value, ModerationStatus, 'moderation status')


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

    That tolerance is deliberately NARROW. ``extra = 'allow'`` on its own
    would accept any key a caller invented, so a body could carry
    ``is_published``, ``moderation_status``, ``rater_id`` or a hundred
    kilobytes of arbitrary keys and still be answered 201. None of those
    could ever be persisted - the write path names its fields - but
    accepting them makes the request contract undiscoverable and invites
    a caller to believe a field had an effect it never had.
    ``_reject_unsupported_keys`` below therefore refuses every
    unrecognised key EXCEPT ``ratee_id``, so the model both keeps the one
    claim it must be able to refuse and rejects everything else at the
    boundary with a 422 that names the offending keys.

    These bounds are the authoritative ones - the client-side Zod
    mirror is a convenience, not a substitute. Because Pydantic
    validates the request body before the handler body runs, an
    out-of-range or non-integer score, or a review that is over-long
    once normalised, surfaces as a 422 with no handler logic reached at
    all. "Once normalised" is the operative phrase: the limit is
    measured on the text that would be stored, so the server and the
    client's character counter agree about what it means.

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

    Exactly ONE unrecognised key is tolerated - ``ratee_id``, the
    counterparty claim - and every other unrecognised key is REFUSED.
    ``Config`` and ``_reject_unsupported_keys`` below explain why that
    asymmetry, rather than blanket acceptance or blanket refusal, is the
    correct contract for this model.
    """

    transaction_id: DocumentId
    score: RatingScore
    # The RAW ceiling, which exists only to keep an unbounded body away
    # from the normaliser. The bound the caller is actually held to is
    # applied to the NORMALISED value by ``as_plain_text`` in the
    # validator below, so a review that arrives long only because of
    # decomposed Unicode, zero-width characters, CRLF line endings or
    # trailing whitespace is accepted when the text itself fits - and
    # the client's character counter agrees with the server about what
    # the limit means. See ``REVIEW_RAW_MAX_LENGTH``.
    review: Optional[str] = Field(None, max_length=REVIEW_RAW_MAX_LENGTH)

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
        #
        # ``allow`` is the widest of the three settings, and it is
        # NARROWED back down by ``_reject_unsupported_keys`` below,
        # which permits only ``ratee_id`` through. The pair is what
        # expresses "retain one specific claim, refuse everything
        # else"; Pydantic v1 has no per-key extra policy, so the
        # permissive setting plus an explicit allow-list is the only
        # way to say it.
        extra = 'allow'

    @root_validator(pre=True)
    def _reject_unsupported_keys(cls, values: Any) -> Any:
        """Refuse every unrecognised key except the counterparty claim.

        Runs ``pre`` so it sees the body as submitted, before coercion
        and before ``extra = 'allow'`` has retained anything.

        The allow-list is deliberately one entry long. ``ratee_id`` is
        tolerated because the service must be able to compare it against
        the derived counterparty and answer 403 on a mismatch - a guard
        that is unreachable if the key is dropped, and equally
        unreachable if the whole body is refused with a 422. Every other
        unrecognised key is a caller mistake or a probe, and naming the
        offending keys in the error is what makes the contract
        discoverable rather than mysteriously permissive.

        Args:
            values: The raw submitted body, ordinarily a mapping.

        Returns:
            ``values`` unchanged when every key is supported.

        Raises:
            ValueError: One or more unsupported keys were supplied.
        """
        if not isinstance(values, dict):
            return values
        declared = set(cls.__fields__)
        permitted = declared | {RATEE_ID_CLAIM}
        unsupported = sorted(
            str(key) for key in values if str(key) not in permitted
        )
        if unsupported:
            raise ValueError(
                'Unsupported field(s): {0}. Permitted fields are '
                '{1}'.format(
                    ', '.join(unsupported),
                    sorted(declared),
                )
            )
        return values

    @validator('review')
    def _validate_review(cls, value: Optional[str]) -> Optional[str]:
        """Normalise submitted review text to plain text.

        The authoritative application of the content policy: it runs at
        the request boundary, so no submission path can bypass it.
        """
        return as_plain_text(value)


def to_rating_view(rating: Rating) -> RatingView:
    """Project one persisted rating into its public API response.

    The single place the persistence model becomes a response, so the
    field set a caller receives is decided once rather than at each
    endpoint. Fields absent from :class:`RatingView` are dropped here
    simply by not being named - there is no blank-out step that a later
    field could be forgotten from.

    Args:
        rating: The rating as stored, already passed through the
            service's visibility projection.

    Returns:
        The response projection.

    Raises:
        ValueError: The rating carries no usable ``created_at`` or
            ``updated_at``. Deliberately loud rather than substituted: a
            fabricated "now" would be presented to a reader as the moment
            somebody rated them, and the alternative of emitting null is
            a response the official client refuses. Every path that
            produces a response stamps both values, so this reports a
            genuine data fault rather than a routine case.
    """
    return RatingView(
        id=rating.id,
        transaction_id=rating.transaction_id,
        vehicle_listing_id=rating.vehicle_listing_id,
        rater_id=rating.rater_id,
        ratee_id=rating.ratee_id,
        direction=rating.direction,
        score=rating.score,
        review=rating.review,
        is_published=rating.is_published,
        created_at=rating.created_at,
        updated_at=rating.updated_at,
    )


def to_rating_views(ratings: Any) -> List[RatingView]:
    """Project many persisted ratings, skipping any that cannot be shown.

    A list read must not fail whole because one stored document is
    unstampable. Such a record is dropped from the page rather than
    substituted or raised, which is the same treatment the service already
    gives a document whose body cannot satisfy :class:`Rating` - the read
    answers with what it can prove, and the unusable record stays visible
    to an operator through the log rather than to a reader as a fiction.

    Args:
        ratings: Iterable of ratings as stored.

    Returns:
        The response projections, in the order given.
    """
    views: List[RatingView] = []
    for rating in ratings:
        try:
            views.append(to_rating_view(rating))
        except ValueError:
            logger.error(
                'Omitting rating %s from a response: it carries no usable '
                'created_at/updated_at, so it cannot be represented as a '
                'response and needs repair.',
                getattr(rating, 'id', '<unknown>'),
            )
    return views


def to_moderated_rating_view(rating: Rating) -> ModeratedRatingView:
    """Project one persisted rating into the administrator's response.

    Args:
        rating: The rating as stored, in its new moderation state.

    Returns:
        The response projection, carrying the moderation state and its
        recorded policy basis.

    Raises:
        ValueError: The rating carries no usable timestamps; see
            :func:`to_rating_view`.
    """
    return ModeratedRatingView(
        **to_rating_view(rating).dict(),
        moderation_status=rating.moderation_status,
        moderation_reason=rating.moderation_reason,
        moderation_note=rating.moderation_note,
    )


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
