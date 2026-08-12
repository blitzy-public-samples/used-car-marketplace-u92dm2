"""HTTP surface for the bidirectional peer reputation system.

Publishes the five endpoints of F010 "Review and Rating System" from
``documentation/Software Requirements Specifications (SRS).md``
(L429-L441): rating submission (F010-1) and the written review it
carries (F010-2), the aggregate a profile displays (F010-3), and the
moderation transition (F010-4). F010-5, integration of ratings into
search-result ranking, is deliberately out of scope - nothing in this
module changes the semantics of ``GET /api/listings``.

WHAT THIS MODULE DOES, AND WHAT IT DELIBERATELY DOES NOT
-------------------------------------------------------------------
This is a boundary, not a brain. Exactly two kinds of decision are
taken here:

* the authorization comparisons the house convention places inside the
  handler - the administrator gate on moderation, and the participant
  gate on the per-transaction read; and
* the translation of a typed domain failure into an HTTP status code.

Everything else is delegated. Who may rate whom, how a rating document
is keyed, when it becomes visible and how an aggregate moves belong to
``app/services/rating.py``. Every validation bound - the 1..5 score,
the review length - belongs to ``app/schema/rating.py``, where it is
bound to ``settings`` and applied by Pydantic BEFORE any handler body
runs. Neither is reimplemented nor re-checked here: a second copy of a
bound is a second source of truth, and the two drift.

THE TWO NON-NEGOTIABLE GATES
-------------------------------------------------------------------
R1 - only a VERIFIED user may submit a rating. "Verified" means
account/email verification only, the flag F001-1 establishes; identity
and KYC verification are excluded by the project charter and nothing
here implements them.

R2 - only the two counterparties of the SAME transaction may rate each
other, and only once each.

Both are enforced by :func:`app.services.rating.submit_rating`, INSIDE
the Firestore transaction that writes and against documents re-read
under its lock, so nothing reaches the datastore until every guard has
passed. That placement is the whole point: a gate that answered 403
after persisting would satisfy the letter of the requirement and
defeat its purpose. This module therefore adds no write of its own on
the submission path, before the call or after it.

R1 stays revocable without token rotation because ``get_current_user``
re-reads the user document on every request. Nothing in this call path
may cache that lookup.

WHY THE READ PATHS DELEGATE INSTEAD OF QUERYING INLINE
-------------------------------------------------------------------
The existing routers query Firestore inline, and this one still does so
for the AUTHORIZATION evidence it needs: the transaction document
behind the participant gate, and the existence of the user behind the
public read's 404. Those are authorization decisions and they belong in
the handler.

The RESULT SETS are a different matter, and they are delegated to
``app/services/rating.py`` deliberately, because producing them is not
a query - it is policy:

* what a reader may see is decided by the double-blind publication
  state, by moderation state for review CONTENT only, and by an
  authorship override that still shows a rater their own withheld
  words together with the reason;
* the aggregate is read as one get-by-ID off the user document, which
  is what holds the 200 ms budget at SRS L451, and a stored pair that
  cannot be true is quarantined rather than presented to somebody as
  their counterparty's reputation;
* both read paths settle any publication that is already DUE before
  answering, because no worker will - no Celery task in this codebase
  is ever dispatched and no broker is provisioned, so the read path IS
  the fallback that makes the deferred reveal correct;
* the query shapes are pinned to the two composite indexes declared in
  ``infrastructure/firestore.indexes.json``, and every further
  predicate is applied in Python because no third index exists.

Restating any of that here would place a second, drifting copy of an
FTC-relevant visibility policy in the HTTP layer. The service module
names this router as the caller of those entry points, and they are
imported below under exactly that contract.

SENTIMENT NEUTRALITY IS A HARD CONSTRAINT ON THIS FILE
-------------------------------------------------------------------
There is no score threshold, no score-correlated filter and no
"hide low ratings" capability anywhere in this module, and none may be
added. A moderation transition may be justified only by a POLICY
violation - abuse, personally identifying information, profanity - and
never by the value of the score. The reason is recorded on the
document, and the service refuses a rejection that carries none. The
aggregate keeps counting every published rating whatever its value.
The FTC Rule on the Use of Consumer Reviews and Testimonials (16 CFR
Part 465) prohibits suppressing reviews on the basis of rating or
negative sentiment, so this is a legal constraint and not a stylistic
one. There is no comparison against ``score`` anywhere in this file.

Reputation records are append-only, so no endpoint here mutates a
submitted ``score`` or ``review``. The only mutation exposed is the
moderation-state transition, and its request model REFUSES any other
key rather than ignoring it - a caller must never be answered 200 for
an edit the server did not make.

EVERY PATH IS DECLARED RELATIVE TO THE MOUNT PREFIX
-------------------------------------------------------------------
``app/main.py`` mounts this router at ``prefix='/api/ratings'``, so the
declared paths are ``''``, ``'/user/{user_id}'``,
``'/transaction/{transaction_id}'``,
``'/eligibility/{transaction_id}'`` and
``'/{rating_id}/moderation'`` - and nothing here repeats the
``ratings`` segment. The three existing routers do repeat theirs, so
``@router.post('/listings')`` mounted at ``prefix='/api/listings'``
resolves to ``/api/listings/listings``. That defect is invisible at the
decorator and appears only once the prefix has been applied, which is
why the five resolved paths are verified by ENUMERATING the registered
routes rather than by reading the decorators. Repairing the other three
routers would change three published contracts and is out of scope;
this one simply does not inherit the defect.

Every path parameter is annotated with ``DocumentId`` rather than
``str``. These values are used to CONSTRUCT document paths, so the
grammar Firestore itself imposes is applied by Pydantic at the
boundary: an over-long, control-character-bearing or reserved
identifier is refused with a 422 before it can reach ``document()``.
The grammar lives in ``app/schema/rating.py`` and is not restated here.

Handlers are ``async def`` and consume the synchronous
``get_current_user`` through ``Depends``, which FastAPI runs in a
threadpool - the same arrangement the existing routers use. Pydantic v1
semantics apply throughout, matching the pin in
``backend/requirements.txt``.
"""
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.api.auth import get_current_user
from app.db.firestore import db
from app.schema.rating import (
    DocumentId,
    EligibilityDecision,
    ModerationStatus,
    Rating,
    RatingAggregate,
    RatingCreate,
)
from app.schema.user import User
from app.services.rating import (
    TRANSACTIONS_COLLECTION,
    USERS_COLLECTION,
    DuplicateRating,
    NotATransactionParticipant,
    RaterNotVerified,
    RatingError,
    SelfRatingNotAllowed,
    TransactionNotCompleted,
    TransactionNotFound,
    TransactionNotRatable,
    evaluate_eligibility,
    submit_rating,
)

# The read and moderation entry points of the service module, imported
# under the contract its own docstrings state: each one is documented
# there as being called by THIS router, which supplies the raw path
# parameter and performs the authorization the entry point deliberately
# does not ("Authorization is the router's concern"). They are
# module-internal to the service because nothing outside this feature
# may call them, and they are aliased here without the underscore so
# every call site below reads as the delegation it is.
#
# The alternative - reimplementing the queries in this file - was
# rejected on the merits. Each of these owns policy rather than a
# query: the double-blind visibility projection with its authorship
# override, the quarantining of an impossible aggregate, the bounded
# settlement of publications already due, the index-shaped query with
# its remaining predicates applied in Python, and the append-only
# moderation write that touches state and never content. A copy of any
# of that in the HTTP layer would be a second source of truth for a
# policy the FTC review-suppression rule constrains.
from app.services.rating import (
    _get_user_aggregate as get_user_aggregate,
)
from app.services.rating import (
    _list_ratings_for_transaction as list_ratings_for_transaction,
)
from app.services.rating import (
    _list_ratings_for_user as list_ratings_for_user,
)
from app.services.rating import (
    _moderate_rating as moderate_rating,
)

router = APIRouter()

# The role permitted to moderate. Compared inline exactly as
# ``app/api/listings.py`` compares it when authorizing a deletion,
# because authorization in this application is imperative and
# in-handler rather than middleware or a policy engine.
ADMIN_ROLE = 'admin'

# Details for the two refusals this module composes itself, as opposed
# to the ones it surfaces from a domain exception. Both are prose in the
# same register as the service's messages, because the interface renders
# a ``detail`` verbatim.
ADMIN_ONLY_DETAIL = 'Only administrators can moderate ratings'
USER_NOT_FOUND_DETAIL = 'User not found'
RATING_NOT_FOUND_DETAIL = 'Rating not found'

# The router's error contract, most specific failure first. Every entry
# maps a typed domain exception raised by ``app/services/rating.py`` on
# to the status code the plan specifies for it, and the exception's own
# ``message`` becomes the response ``detail`` - which is what keeps an
# HTTP failure and the reason reported by the eligibility endpoint
# identical by construction instead of by two literals agreeing today.
#
# The ordering is part of the contract rather than cosmetic: the guard
# sequence in the service reports the most actionable failure first
# (unverified, then unknown transaction, then non-participant, then
# self-rating, then not completed, then duplicate), and translating them
# individually preserves that. Collapsing any pair, or catching a bare
# ``Exception``, would launder a genuine defect into a misleading 4xx.
DOMAIN_FAILURE_STATUS = (
    (RaterNotVerified, 403),
    (NotATransactionParticipant, 403),
    (TransactionNotFound, 404),
    (SelfRatingNotAllowed, 422),
    (TransactionNotRatable, 422),
    (TransactionNotCompleted, 409),
    (DuplicateRating, 409),
)

# Status for a domain failure that is a ``RatingError`` but none of the
# classes above - a subclass added to the service later. Answering 400
# is deliberate: every ``RatingError`` is by construction a refusal the
# caller caused and carries a message fit to show them, so leaking it as
# a 500 would misreport a client error as a server fault. A defect that
# is NOT a ``RatingError`` still propagates untouched and still becomes
# a 500, which is the behaviour a bug must have.
UNCLASSIFIED_DOMAIN_STATUS = 400


class UserRatingsResponse(BaseModel):
    """The public reputation view of one user. F010-3.

    Two fields rather than a bare list, because a reputation is not
    just its reviews: the aggregate is maintained on the user document
    and read by ID, so it is authoritative on its own and is reported
    beside the ratings rather than derived from them by the client.

    ``items`` carries the published ratings only, so an unreciprocated
    rating stays invisible until it is revealed - the double-blind
    model holds on the way out as well as on the way in. ``aggregate``
    counts published ratings and every one of them, whatever the score.

    A user with no ratings is a first-class state and not an error: an
    empty ``items`` beside ``average=None, count=0``. It is modelled
    explicitly so the reputation badge can render "No ratings yet"
    rather than an average of zero, which would be a different claim.
    """

    items: List[Rating] = Field(
        ...,
        description=(
            'Published ratings received by the user, newest first. '
            'Review text is present only once moderation has approved '
            'it; the score is always present.'
        ),
    )
    aggregate: RatingAggregate = Field(
        ...,
        description=(
            'Denormalised reputation summary over published ratings. '
            '``average`` is null and ``count`` is zero when the user '
            'has none.'
        ),
    )


class ModerationUpdate(BaseModel):
    """The request body accepted by the moderation endpoint. F010-4.

    Declares a state and a policy reason, and nothing else. That is the
    whole append-only contract of this feature expressed as a request
    shape: a submitted ``score`` or ``review`` is never rewritten, so
    neither is accepted here, and a correction is this transition
    carrying its justification instead of an edit that erases what was
    said. There is no audit or history collection in this codebase,
    which is exactly why the original text has to survive.

    ``moderation_status`` is typed as the enumeration rather than as a
    string so an unrecognised state is refused with a 422 by Pydantic
    before the handler body runs, and so the permitted values appear in
    the OpenAPI schema. An unconstrained string would be written
    straight on to the document and would silently disable every state
    check that reads it.

    ``moderation_reason`` is bounded and normalised by the service, not
    here, so the limit has one owner. It is optional on this model and
    MANDATORY in practice for a rejection: the service refuses to
    withhold a rating without a recorded policy basis, which surfaces
    as a 422. Enforcing it twice would put the rule in two places, and
    the router's copy would be the one that drifted.

    The reason must describe a POLICY violation - abuse, personally
    identifying information, profanity. A low score is never itself a
    violation, and this model offers no way to express one: it cannot
    see the score at all.
    """

    moderation_status: ModerationStatus = Field(
        ...,
        description=(
            'Target moderation state. Transitions may be justified '
            'only by a policy violation, never by the score.'
        ),
    )
    moderation_reason: Optional[str] = Field(
        None,
        description=(
            'The policy basis for the transition - required when '
            'rejecting, cleared when omitted.'
        ),
    )

    class Config:
        # Refuse an unrecognised key instead of dropping it. Pydantic's
        # default would silently ignore a body carrying ``score`` or
        # ``review`` and answer 200, leaving the caller believing an
        # edit had been applied that the append-only contract never
        # permits. Naming the offending key in a 422 is what makes the
        # contract honest.
        extra = 'forbid'


def domain_failure_detail(error: RatingError) -> str:
    """Return the caller-facing message carried by a domain failure.

    The service's exception message is reused verbatim rather than
    paired with a literal composed here. That is the mechanism by which
    an HTTP ``detail`` and the ``reason`` reported by the eligibility
    endpoint stay identical - both read this one attribute - and the
    interface renders each of them directly.

    Args:
        error: The domain failure being translated.

    Returns:
        The exception's message, falling back to the base class's
        wording when a subclass was constructed with an empty one. The
        fallback is read from ``RatingError`` rather than restated, so
        there is still exactly one copy of it, and it is a sentence in
        the same register - never a bare code or an internal detail.
    """
    message = getattr(error, 'message', None) or str(error)
    return message.strip() or RatingError.message


def domain_failure(error: RatingError) -> HTTPException:
    """Translate a typed domain failure into its HTTP response.

    The single place this router maps the service's vocabulary on to
    status codes, so the mapping cannot disagree with itself across
    endpoints.

    Args:
        error: The domain failure raised by the service layer.

    Returns:
        The ``HTTPException`` to raise, carrying the exception's own
        message as its ``detail``.
    """
    for failure, status_code in DOMAIN_FAILURE_STATUS:
        if isinstance(error, failure):
            return HTTPException(
                status_code=status_code,
                detail=domain_failure_detail(error),
            )
    return HTTPException(
        status_code=UNCLASSIFIED_DOMAIN_STATUS,
        detail=domain_failure_detail(error),
    )


@router.post(
    '',
    status_code=201,
    response_model=Rating,
    summary='Submit a rating for a completed transaction',
)
async def create_rating(
    payload: RatingCreate,
    current_user: User = Depends(get_current_user),
) -> Rating:
    """Record one directional rating. F010-1, F010-2.

    Resolves to ``POST /api/ratings``.

    The handler is a delegation and deliberately nothing more. Three
    fields of the request body reach the service - ``transaction_id``,
    ``score`` and an optional ``review`` - and the rater is always the
    authenticated caller, taken from ``current_user`` rather than from
    anything the client sent. ``ratee_id`` and ``direction`` are derived
    server-side from the cited transaction, which is what makes
    counterparty spoofing and self-rating structurally impossible rather
    than merely validated against. A ``ratee_id`` supplied anyway is a
    claim the service checks against the derived counterparty and
    refuses on a mismatch; it is never a value anyone uses.

    Both gates are enforced inside the Firestore transaction that
    writes, so a refusal writes nothing at all. There is no persistence
    in this handler, before the call or after it, and none may be added:
    the acceptance criteria for R1 and R2 assert not only the 403 but
    that no rating document exists afterwards.

    The score bound and the review length limit are enforced by
    ``app/schema/rating.py`` and surface as a 422 raised by Pydantic
    before this body runs. They are deliberately not re-checked here.

    Args:
        payload: Validated request body.
        current_user: The authenticated caller, re-read from the
            datastore on every request so a revoked verification takes
            effect immediately.

    Returns:
        The persisted rating. It is stored unpublished and contributes
        nothing to the ratee's aggregate until the counterparty submits
        theirs or the rating window closes - deferring the contribution
        is what removes the incentive for retaliatory rating.

    Raises:
        HTTPException: 401 when unauthenticated (raised by the
            dependency); 403 when the rater is unverified (R1) or is not
            a party to the transaction (R2); 404 when no such
            transaction exists; 409 when the transaction is not
            completed or this rater has already rated it; 422 for a
            degenerate transaction naming one user as both parties, or
            for a transaction missing the data a rating record needs.
    """
    try:
        return submit_rating(payload, current_user)
    except RatingError as error:
        raise domain_failure(error) from error


@router.get(
    '/user/{user_id}',
    response_model=UserRatingsResponse,
    summary='Read the published ratings a user has received',
)
async def get_user_ratings(user_id: DocumentId) -> UserRatingsResponse:
    """Report one user's reputation. F010-3.

    Resolves to ``GET /api/ratings/user/{user_id}``.

    PUBLIC, with no authentication dependency of any kind. That is not
    an oversight and not a relaxation: it is the precedent both read
    handlers in ``app/api/listings.py`` set, and a reputation a buyer
    cannot read before deciding whether to transact would not serve the
    purpose the feature exists for. Only published ratings are
    returned, so nothing here reveals a rating its counterparty has not
    yet answered, and review text appears only once moderation has
    approved it.

    The listing is read first and the aggregate second, deliberately.
    Serving this endpoint settles any publication that is already due,
    so reading the aggregate afterwards reports the state including
    whatever was just revealed rather than the state before it.

    Args:
        user_id: The rated user. Validated against the Firestore
            document-ID grammar before it can reach ``document()``.

    Returns:
        The published ratings received, newest first, beside the
        denormalised aggregate. Both are empty for a user who has never
        been rated, which is a state and not an error.

    Raises:
        HTTPException: 404 when no such user exists. The distinction
            matters to the caller: "this user has no ratings" and "there
            is no such user" are different answers, and only the first
            is a 200.
    """
    # Existence is established here rather than inferred from an empty
    # result, and it is a get-by-ID against the same collection
    # ``get_current_user`` reads. The aggregate call below reads the same
    # document again; that is accepted rather than optimised away,
    # because folding the two would mean reimplementing the quarantine
    # the service applies to an impossible stored aggregate, and two
    # get-by-ID reads sit comfortably inside the SRS 200 ms budget.
    snapshot = db.collection(USERS_COLLECTION).document(user_id).get()
    if not snapshot.exists:
        raise HTTPException(
            status_code=404,
            detail=USER_NOT_FOUND_DETAIL,
        )
    items = list_ratings_for_user(user_id)
    aggregate = get_user_aggregate(user_id)
    return UserRatingsResponse(items=items, aggregate=aggregate)


@router.get(
    '/transaction/{transaction_id}',
    response_model=List[Rating],
    summary='Read the ratings attached to one transaction',
)
async def get_transaction_ratings(
    transaction_id: DocumentId,
    current_user: User = Depends(get_current_user),
) -> List[Rating]:
    """Report the ratings on one transaction, to its participants.

    Resolves to ``GET /api/ratings/transaction/{transaction_id}``.

    The participant guard is a structural clone of the one
    ``app/api/transactions.py`` already applies to reading a
    transaction, including the house ordering: a transaction that does
    not exist is a 404, and only an existing transaction the caller is
    not party to is a 403. Reversing that would let an outsider
    distinguish a transaction that exists from one that does not.

    Being a participant does not lift the double-blind model. A caller
    always sees their OWN rating in full - unrevealed, and with the
    recorded reason even when moderation rejected its review, because
    otherwise a withheld review would vanish with no explanation to the
    person who wrote it. The counterparty's stays hidden until it
    publishes. Returning both unconditionally would hand a participant
    exactly the early look the model exists to deny.

    Args:
        transaction_id: The transaction whose ratings are wanted.
            Validated against the document-ID grammar.
        current_user: The authenticated caller, whose participation is
            the authorization.

    Returns:
        The ratings this caller may see, newest first.

    Raises:
        HTTPException: 401 when unauthenticated (raised by the
            dependency); 404 when no such transaction exists; 403 when
            the caller is neither its buyer nor its seller.
    """
    snapshot = (
        db.collection(TRANSACTIONS_COLLECTION)
        .document(transaction_id)
        .get()
    )
    if not snapshot.exists:
        raise HTTPException(
            status_code=404,
            detail=TransactionNotFound.message,
        )
    transaction_data = snapshot.to_dict() or {}
    # Read with ``.get`` rather than by subscript, so a transaction
    # document missing a participant field fails CLOSED with a 403
    # instead of raising a ``KeyError`` that would surface as a 500. A
    # document that cannot name its parties authorizes nobody.
    participants = [
        transaction_data.get('buyer_id'),
        transaction_data.get('seller_id'),
    ]
    if not current_user.id or current_user.id not in participants:
        raise HTTPException(
            status_code=403,
            detail=NotATransactionParticipant.message,
        )
    return list_ratings_for_transaction(transaction_id, current_user.id)


@router.get(
    '/eligibility/{transaction_id}',
    response_model=EligibilityDecision,
    summary='Report whether the caller may rate their counterparty',
)
async def get_rating_eligibility(
    transaction_id: DocumentId,
    current_user: User = Depends(get_current_user),
) -> EligibilityDecision:
    """Report the eligibility decision as structured data.

    Resolves to ``GET /api/ratings/eligibility/{transaction_id}``.

    This endpoint exists so the interface can disable the submission
    control WITH A REASON instead of letting somebody compose a rating
    and only then fail on a 403. It runs the identical guard sequence
    the write path enforces - the reason it reports is taken from the
    very exception that path would have raised - so what the user is
    told and what the server does cannot disagree.

    It is therefore authenticated but NOT verified-gated, which is the
    one place in this feature where R1 does not produce a 403: an
    unverified caller receives 200 with ``eligible=False`` and the
    verification reason, because reporting ineligibility is the entire
    value of the endpoint. Refusing the request would leave the
    interface unable to say why the control is unavailable, which is
    also what makes an unavailable state imperceptible to a user of
    assistive technology.

    The decision is deliberately not reduced to a boolean.
    ``ratee_id`` and ``direction`` are populated only once the caller is
    confirmed a participant, so a rejected caller learns nothing about
    the counterparty, and ``already_rated`` lets the interface
    distinguish "you have rated this" from "you may not rate this".

    Nothing here writes. An eligibility check is a question, not an
    event, so no publication is triggered from this path.

    Args:
        transaction_id: The transaction the caller is asking about.
        current_user: The authenticated caller.

    Returns:
        The full :class:`EligibilityDecision`.

    Raises:
        HTTPException: 401 when unauthenticated (raised by the
            dependency); 404 when no such transaction exists.
    """
    decision = evaluate_eligibility(transaction_id, current_user)
    # The service reports an unknown transaction as an ineligible
    # decision rather than raising, because it has no HTTP vocabulary.
    # The published contract for this endpoint is a 404, so the one
    # decision that means "no such transaction" is translated here. The
    # test is against the exception class's own message attribute, not a
    # literal repeated in this file, so the two cannot drift - and the
    # detail the caller receives is that same sentence.
    if not decision.eligible and decision.reason == (
        TransactionNotFound.message
    ):
        raise HTTPException(
            status_code=404,
            detail=TransactionNotFound.message,
        )
    return decision


@router.patch(
    '/{rating_id}/moderation',
    response_model=Rating,
    summary='Transition a rating between moderation states',
)
async def set_rating_moderation(
    rating_id: DocumentId,
    payload: ModerationUpdate,
    current_user: User = Depends(get_current_user),
) -> Rating:
    """Move a rating's moderation state, recording why. F010-4.

    Resolves to ``PATCH /api/ratings/{rating_id}/moderation``.

    The only mutation this router exposes, and it touches moderation
    state alone. ``score``, ``review``, ``rater_id``, ``ratee_id``,
    ``direction`` and ``transaction_id`` are never written by this path:
    reputation records are append-only, so a correction is this
    transition carrying a reason rather than an edit that erases what
    was said.

    A transition may be justified ONLY by a policy violation - abuse,
    personally identifying information, profanity - and never by the
    value of the score. A one-star rating is not a violation, and
    withholding one because it is unflattering is precisely what the FTC
    Rule on the Use of Consumer Reviews and Testimonials (16 CFR Part
    465) prohibits. Accordingly there is no score threshold and no
    score-correlated behaviour anywhere in this handler, the aggregate
    is not adjusted here at all - the service is its only writer, and it
    counts every published rating whatever its value - and a rejection
    withholds the review TEXT while the rating and its score continue to
    appear, so the ratings a reader can see still account for the
    average they are shown.

    The administrator gate is compared inline before anything is
    written, mirroring how ``app/api/listings.py`` authorizes a
    deletion.

    Args:
        rating_id: The rating document to transition, which is
            ``"{transaction_id}_{rater_id}"``. Validated against the
            document-ID grammar.
        payload: The target state and its policy reason.
        current_user: The authenticated caller, who must be an
            administrator.

    Returns:
        The rating in its new state.

    Raises:
        HTTPException: 401 when unauthenticated (raised by the
            dependency); 403 when the caller is not an administrator;
            404 when no rating exists at that ID; 422 when a rejection
            is requested without a policy reason, or when the reason
            exceeds the bound the service applies to it.
    """
    if current_user.role != ADMIN_ROLE:
        raise HTTPException(
            status_code=403,
            detail=ADMIN_ONLY_DETAIL,
        )
    try:
        rating = moderate_rating(
            rating_id,
            payload.moderation_status,
            payload.moderation_reason,
        )
    except ValueError as error:
        # The service refuses to withhold a rating without a recorded
        # policy basis, and bounds the reason it does record. Both are
        # reported as a 422 carrying its own explanation, because the
        # request is well formed and the caller can act on it. The rule
        # is enforced there and not duplicated here, so there is exactly
        # one statement of what a valid moderation decision is.
        raise HTTPException(status_code=422, detail=str(error)) from error
    if rating is None:
        raise HTTPException(
            status_code=404,
            detail=RATING_NOT_FOUND_DETAIL,
        )
    return rating
