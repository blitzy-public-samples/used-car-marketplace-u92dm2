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
from pydantic import BaseModel, Field
from typing import Any, Optional
from enum import Enum

from app.core.config import settings


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
    """

    PENDING = 'pending'
    APPROVED = 'approved'
    REJECTED = 'rejected'


class Rating(BaseModel):
    """One directional rating, as persisted in ``ratings``.

    Append-only by design. A submitted score is never rewritten, so
    this model exposes no mechanism to do so; a correction is a
    ``moderation_status`` transition carrying a recorded reason.

    ``direction`` and ``moderation_status`` are typed as plain ``str``
    rather than as their enum classes on purpose. Pydantic v1 does not
    validate field defaults, so an enum-typed field would keep the raw
    member as its default; on Python 3.9 ``str()`` of such a member
    yields ``'ModerationStatus.PENDING'`` instead of ``'pending'``,
    which would silently corrupt any write, log or query that
    stringifies it. Writing the default as ``.value`` keeps a genuine
    ``str`` on the attribute and in ``.dict()``. Neither field takes
    untrusted input at this layer - both are server-derived - and the
    admin moderation endpoint validates its input against
    :class:`ModerationStatus`.
    """

    id: str
    transaction_id: str
    # Denormalised from the transaction so a rating can be rendered
    # with context without a second document read.
    vehicle_listing_id: str
    rater_id: str
    ratee_id: str
    direction: str
    score: int = Field(..., ge=settings.RATING_MIN, le=settings.RATING_MAX)
    review: Optional[str] = None
    # Created unpublished: under the double-blind model a rating
    # becomes visible only once the counterparty submits theirs or the
    # rating window elapses. The aggregate reflects published ratings
    # only, which is what removes the incentive for review extortion.
    is_published: bool = False
    moderation_status: str = ModerationStatus.PENDING.value
    moderation_reason: Optional[str] = None
    # Permissive by necessity, and a contract that
    # ``services/rating.py`` must follow: the write path assigns
    # ``firestore.SERVER_TIMESTAMP``, which is a sentinel object and
    # not a ``datetime``. A strict ``datetime`` annotation rejects it
    # with a ValidationError, so constructing a Rating from the payload
    # about to be written would fail. Reading the document back yields
    # a real ``DatetimeWithNanoseconds`` instead. All three shapes -
    # sentinel, real timestamp, and absence before the server has
    # stamped them - must be accepted.
    created_at: Optional[Any] = None
    updated_at: Optional[Any] = None


class RatingCreate(BaseModel):
    """The request body accepted by ``POST /api/ratings``.

    Carries three fields and no more. ``ratee_id`` and ``direction``
    are absent by design rather than by oversight: both are derived
    server-side from the cited transaction document, which is what
    makes counterparty spoofing and self-rating structurally
    impossible instead of merely validated against. Do not add them
    here for convenience or symmetry.

    These bounds are the authoritative ones - the client-side Zod
    mirror is a convenience, not a substitute. Because Pydantic
    validates the request body before the handler body runs, an
    out-of-range score or an over-long review surfaces as a 422 with
    no handler logic reached at all.
    """

    transaction_id: str
    score: int = Field(..., ge=settings.RATING_MIN, le=settings.RATING_MAX)
    review: Optional[str] = Field(
        None, max_length=settings.RATING_REVIEW_MAX_LENGTH
    )


class RatingAggregate(BaseModel):
    """Denormalised reputation summary for a single user.

    Mirrors ``rating_average``/``rating_count`` on
    ``backend/app/schema/user.py``, which are maintained inside the
    same transaction that writes a rating so that reading a profile
    costs one get-by-ID rather than a scan of the user's ratings.

    ``average`` is nullable and ``count`` defaults to zero so that "no
    ratings yet" remains a first-class state, distinguishable from a
    genuine average of zero. The reputation badge renders that
    distinction explicitly, so ``average`` must never default to 0.0.

    Reflects published ratings only, and includes every one of them
    whatever the score.
    """

    average: Optional[float] = None
    count: int = 0


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
