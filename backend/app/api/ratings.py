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
* the query shapes are pinned to the three composite indexes declared
  in ``infrastructure/firestore.indexes.json``, and every predicate
  beyond them - moderation state, for one - is applied in Python,
  because a query needing an index nobody declared fails outright
  instead of degrading.

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

EVERY HANDLER IS SYNCHRONOUS, AND THAT IS DELIBERATE
-------------------------------------------------------------------
The Firestore client this feature uses is blocking - ``app/db/firestore.py``
constructs a synchronous ``Client`` - so an ``async def`` handler would
execute that I/O on the event loop itself. One slow read or a moment of
datastore latency would then stall every other in-flight request for its
duration, which is precisely the failure the 200 ms budget at SRS L451
cannot survive under load. A ``def`` handler is dispatched to FastAPI's
threadpool, so the whole body - dependency, service call and response
projection - runs off the loop. ``get_current_user`` is synchronous for the
same reason. Do not make any handler here ``async`` without first making
the datastore access genuinely asynchronous.

RESPONSES ARE PURPOSE-BUILT VIEWS, NOT THE PERSISTED RECORD
-------------------------------------------------------------------
No endpoint returns the stored ``Rating``. The public read, the
participant read and the submission all return ``RatingView``, which
carries no moderation state at all - that state is operational, and the
visibility decision it drives has already been applied to what the caller
receives. Only the admin-gated moderation endpoint returns
``ModeratedRatingView``, to the caller who just set the state. The
projection is performed by ``app/schema/rating.py`` so the field set a
caller sees is decided in one place rather than at each handler.


Pydantic v1 semantics apply throughout, matching the pin in
``backend/requirements.txt``.
"""
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, validator


from app.api.auth import get_current_user
from app.db.firestore import db
from app.schema.rating import (
    MODERATION_REASON_MAX_LENGTH,
    DocumentId,
    EligibilityDecision,
    ModeratedRatingView,
    ModerationStatus,
    RatingAggregate,
    RatingCreate,
    RatingDocumentId,
    RatingView,
    to_moderated_rating_view,
    to_rating_view,
    to_rating_views,
    as_plain_text,

)
from app.schema.user import User
# Only the service module's PUBLIC surface is imported. Nothing here
# reaches an underscored name: an earlier revision imported four of them
# and aliased away the underscore, which made this router a client of the
# service's internals - free to be broken by a refactor the service was
# entitled to make, and hiding, at every call site, the fact that it was
# reaching past a boundary.
#
# The alternative - reimplementing the queries in this file - was rejected
# on the merits. Each of these entry points owns policy rather than a
# query: the double-blind visibility projection with its authorship
# override, the quarantining of an impossible aggregate, the single bounded
# settlement of publications already due, the index-shaped query with its
# remaining predicates applied in Python, and the append-only moderation
# write that touches state and never content. A copy of any of that in the
# HTTP layer would be a second source of truth for a policy the FTC
# review-suppression rule constrains.
from app.services.rating import (
    MODERATION_REASON_CODES,
    TRANSACTIONS_COLLECTION,
    DuplicateRating,
    NotATransactionParticipant,
    RaterNotVerified,
    SelfRatingNotAllowed,
    TransactionNotCompleted,
    TransactionNotFound,
    get_user_reputation,
    list_transaction_ratings,
    moderate_rating,
    require_eligibility,

    submit_rating,
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

# The router's error contract: EXACTLY the six domain exceptions the plan
# specifies, and nothing else. Every entry maps one typed exception raised
# by ``app/services/rating.py`` on to its status code, and the exception's
# own ``message`` becomes the response ``detail`` - which is what keeps an
# HTTP failure and the reason reported by the eligibility endpoint
# identical by construction instead of by two literals agreeing today.
#
# SIX ENTRIES, matching the six domain refusals the service defines,
# exactly. That count is a contract and not an accident: these are the
# refusals a caller caused and can act on, and each one appears in the
# documented failure set of the endpoint that can produce it. The
# service's two invariant exceptions - ``PublicationInvariantError`` and
# ``TransactionInvariantError`` - are deliberately absent, because they
# report a defect in stored data that no caller provoked. Mapping either
# to a 4xx would tell a well-behaved user their request was at fault and
# would widen this router's documented contract with a state only an
# operator can clear; unmapped, they propagate as a 500, which is the
# honest answer. Do not add them here.
#
# The ordering is part of the contract rather than cosmetic: the guard
# sequence in the service reports the most actionable failure first
# (unverified, then unknown transaction, then non-participant, then
# self-rating, then not completed, then duplicate), and translating them
# individually preserves that. Collapsing any pair would launder a
# distinct refusal into a misleading one.
#
# WHY THE LIST IS CLOSED, AND WHY THERE IS NO FALLBACK
# -------------------------------------------------------------------
# An earlier revision added a seventh entry and, beneath it, caught the
# BASE ``RatingError`` and answered 400 for anything unrecognised. The
# reasoning was that every ``RatingError`` is a refusal the caller caused,
# so reporting one as a 500 would misattribute a client error. It is the
# wrong trade, in both halves.
#
# The seventh entry was ``TransactionNotRatable``, which fires when an
# otherwise-eligible transaction is missing the ``vehicle_listing_id``
# every ``Transaction`` declares. That is not something the caller did: it
# is a corrupt document in this system's own datastore, and reporting it as
# a 4xx tells the caller to fix a request that was already correct while
# leaving the real fault - bad data the operator has to repair - invisible.
# It propagates now, and a propagating exception is logged with a
# traceback and answered 500, which is what a server-side data fault is.
#
# The catch-all was worse, because it applied to exceptions that do not
# exist yet. Any subclass a future change adds to the service would have
# been answered 400 with its message shown to the caller, whether or not
# the caller could act on it and whether or not that message was fit to
# publish - and it would have done so silently, so nothing would ever
# prompt anyone to classify it. A new failure mode must be mapped here
# deliberately or surface as the unhandled defect it is.
DOMAIN_FAILURE_STATUS = (
    (RaterNotVerified, 403),
    (NotATransactionParticipant, 403),
    (TransactionNotFound, 404),
    (SelfRatingNotAllowed, 422),
    (TransactionNotCompleted, 409),
    (DuplicateRating, 409),
)

# The catch tuple, derived FROM the mapping above rather than written out
# beside it. That is what makes the two exhaustive with respect to each
# other by construction: a class can never be caught here without having a
# status, and adding a status can never be forgotten in the catch.
MAPPED_DOMAIN_FAILURES = tuple(
    failure for failure, _status in DOMAIN_FAILURE_STATUS
)


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

    items: List[RatingView] = Field(
        ...,
        description=(
            'One PAGE of published ratings received by the user, newest '
            'first. Review text is present only once moderation has '
            'approved it; the score is always present. Moderation state '
            'is never present - it is operational, and the visibility '
            'decision it drives has already been applied.'
        ),
    )
    aggregate: RatingAggregate = Field(
        ...,
        description=(
            'Denormalised reputation summary over ALL published ratings, '
            'not only this page - a reputation is not a property of a '
            'page. ``average`` is null and ``count`` is zero when the '
            'user has none.'
        ),
    )
    next_cursor: Optional[str] = Field(
        None,
        description=(
            'Pass as ``after`` to read the following page. Null on the '
            'last page.'
        ),
    )
    has_more: bool = Field(
        ...,
        description=(
            'Whether another page exists. Reported explicitly because '
            '``aggregate.count`` may exceed the number of items on this '
            'page, and a reader must be able to tell that the remainder '
            'is reachable rather than missing.'
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

    ``moderation_reason`` is a POLICY CODE, and the allow-list it is
    checked against lives in the service so the rule has one owner. It is
    optional on this model and MANDATORY in practice for a rejection: the
    service refuses to withhold a rating without a recorded policy basis,
    which surfaces as a 422. Enforcing it twice would put the rule in two
    places, and the router's copy would be the one that drifted.
    Both that requirement and the reason's plain-text bound are applied by
    the validator below, during REQUEST validation, before the handler body
    runs - so a malformed moderation request is refused where every other
    malformed request to this router is refused, and a rating is never
    withheld first and justified afterwards. The rule is not duplicated:
    the validator reuses the same ``as_plain_text`` normaliser and the same
    ``MODERATION_REASON_MAX_LENGTH`` bound the service applies, and the
    service remains the owner of the policy-code allow-list for any future
    caller that does not come through this model.


    Constraining the reason to a code rather than accepting free text is
    what makes sentiment neutrality structural. A low score is never
    itself a violation, and there is no code for one - so "low score"
    cannot be recorded as a justification even by an administrator who
    wants to. The human specifics go in ``moderation_note``, which is
    where a description belongs and which decides nothing.
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
            'The policy basis for the transition, as one of the codes '
            '{0}. Required when rejecting, cleared when omitted. A low '
            'score is not a policy violation and no code expresses '
            'one.'.format(list(MODERATION_REASON_CODES))
        ),
    )
    moderation_note: Optional[str] = Field(
        None,
        description=(
            'Optional operator note recording the specifics behind the '
            'code. Bounded plain text; it justifies nothing on its own.'
        ),
    )

    # ``always=True`` so the validator also runs when the key is absent
    # from the body and the default ``None`` is used. Without it a
    # rejection carrying no reason key at all would never be validated
    # and would pass straight through - the exact case the requirement
    # below exists for. Matches how ``app/schema/rating.py`` declares its
    # own always-on validators.
    @validator('moderation_reason', always=True)
    def _reason_is_bounded_and_present_when_rejecting(
        cls,
        value: Optional[str],
        values: dict,
    ) -> Optional[str]:
        """Normalise the policy reason and require one for a rejection.

        Declared on ``moderation_reason`` rather than as a root
        validator because Pydantic v1 validates fields in declaration
        order and exposes the already-validated ones in ``values`` -
        ``moderation_status`` is declared first, so it is available here.
        If it failed its own enumeration check it is absent from
        ``values``, and this validator then defers rather than raising a
        second, vaguer error over the top of the specific one already
        being reported.

        Normalised with the shared ``as_plain_text``, so the reason is
        held to the same bounded plain-text contract as a review: an
        unbounded moderator note is an unbounded write into a document
        Firestore caps at 1 MiB, and an embedded newline is a forged line
        in any log or export that renders it. Whitespace-only text
        normalises to ``None``, which is what makes the requirement below
        meaningful - a reason of "   " is no reason.

        Args:
            value: The submitted reason, if any.
            values: Fields validated before this one.

        Returns:
            The normalised reason, or ``None`` when none was given and
            none is required.

        Raises:
            ValueError: The reason exceeds the bound, or a rejection was
                requested without one. Pydantic reports either as a 422
                naming this field, before the handler runs, so a rating
                is never withheld first and justified afterwards.
        """
        recorded = as_plain_text(
            value,
            max_length=MODERATION_REASON_MAX_LENGTH,
            label='Moderation reason',
        )
        target = values.get('moderation_status')
        if target == ModerationStatus.REJECTED and recorded is None:
            raise ValueError(
                'Rejecting a rating requires a policy reason describing '
                'the violation. A low score is never itself a violation.'
            )
        return recorded

    class Config:
        # Refuse an unrecognised key instead of dropping it. Pydantic's
        # default would silently ignore a body carrying ``score`` or
        # ``review`` and answer 200, leaving the caller believing an
        # edit had been applied that the append-only contract never
        # permits. Naming the offending key in a 422 is what makes the
        # contract honest.
        extra = 'forbid'


def domain_failure_detail(error: Exception) -> str:
    """Return the caller-facing message carried by a domain failure.

    The service's exception message is reused verbatim rather than
    paired with a literal composed here. That is the mechanism by which
    an HTTP ``detail`` and the ``reason`` reported by the eligibility
    endpoint stay identical - both read this one attribute - and the
    interface renders each of them directly.

    Args:
        error: The domain failure being translated.

    Returns:
        The exception's message, falling back to its ``str()`` when a
        subclass was constructed with an empty one. Each of the six
        mapped classes declares a message, so the fallback is a
        belt-and-braces guard rather than a path anything takes.
    """
    message = getattr(error, 'message', None) or str(error)
    return message.strip() or 'The rating could not be recorded'


def domain_failure(error: Exception) -> HTTPException:
    """Translate one of the six mapped domain failures into its response.

    The single place this router maps the service's vocabulary on to
    status codes, so the mapping cannot disagree with itself across
    endpoints.

    Args:
        error: A domain failure caught through
            :data:`MAPPED_DOMAIN_FAILURES`, so it is always an instance of
            one of the classes in :data:`DOMAIN_FAILURE_STATUS`.

    Returns:
        The ``HTTPException`` to raise, carrying the exception's own
        message as its ``detail``.

    Raises:
        Exception: ``error`` itself, re-raised unchanged, if it is somehow
            not one of the mapped classes. Unreachable while every call
            site catches through ``MAPPED_DOMAIN_FAILURES``, and it is
            written this way rather than as a default status because the
            correct answer to "an exception this router does not classify"
            is to let it be seen, never to invent a 4xx for it.
    """
    for failure, status_code in DOMAIN_FAILURE_STATUS:
        if isinstance(error, failure):
            return HTTPException(
                status_code=status_code,
                detail=domain_failure_detail(error),
            )
    raise error


@router.post(
    '',
    status_code=201,
    response_model=RatingView,
    summary='Submit a rating for a completed transaction',
)
def create_rating(
    payload: RatingCreate,
    current_user: User = Depends(get_current_user),
) -> RatingView:
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
            degenerate transaction naming one user as both parties.

            A transaction that exists, names the caller and is completed
            but is MISSING the data a rating record requires is
            deliberately NOT translated here. It is a corrupt document in
            this system's own datastore, so it propagates and is answered
            500 with a logged traceback - the caller's request was
            correct and there is nothing for them to fix.

    """
    # SYNCHRONOUS on purpose. The Firestore client this feature uses is
    # blocking, so an ``async def`` handler would run that I/O on the event
    # loop itself and stall every other in-flight request for its duration -
    # under load or latency, one slow write becomes everybody's slow
    # request. A ``def`` handler is dispatched to FastAPI's threadpool, so
    # the whole body is off the loop, and the ``Depends`` dependency is
    # already synchronous for the same reason. Do not add ``async`` here
    # without first making the datastore access genuinely asynchronous.
    try:
        return to_rating_view(submit_rating(payload, current_user))
    except MAPPED_DOMAIN_FAILURES as error:
        raise domain_failure(error) from error


@router.get(
    '/user/{user_id}',
    response_model=UserRatingsResponse,
    summary='Read the published ratings a user has received',
)
def get_user_ratings(
    user_id: DocumentId,
    limit: Optional[int] = Query(
        None,
        ge=1,
        description=(
            'Page size. Omitted uses the service default, and any value '
            'is clamped into a serviceable range rather than trusted.'
        ),
    ),
    after: Optional[RatingDocumentId] = Query(
        None,
        description=(
            "The previous page's ``next_cursor``. Omitted starts at the "
            'newest rating.'
        ),
    ),
    aggregate_only: bool = Query(
        False,
        description=(
            'Answer from the user document alone - the aggregate with '
            'an empty items list. One read, for a reputation badge '
            'that needs the two numbers and not the reviews.'
        ),
    ),
) -> UserRatingsResponse:
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

    THE PAGE AND THE AGGREGATE COME FROM ONE OPERATION
    -------------------------------------------------------------------
    Both halves are taken from a single ``get_user_reputation`` call, and
    that is a correctness requirement rather than a tidiness one. Serving
    this endpoint settles any publication already due, because no worker
    will; when the list and the aggregate were fetched through two separate
    service calls, each settled independently and the second pass could
    publish a rating AFTER the list had been materialised. The response
    then carried a count that included a rating the accompanying list did
    not - a profile reading "4 reviews" above three of them, with nothing
    to tell the reader which number was true.

    THE PAGE IS EXPLICIT
    -------------------------------------------------------------------
    The read is bounded, and the bound is now REPORTED. ``next_cursor``
    and ``has_more`` accompany every page, so a user with more ratings than
    one page is not left with an average computed from records the response
    gave no way to reach. ``aggregate`` still covers every published rating
    rather than the page, because a reputation is not a property of a page.

    Serving this endpoint settles any publication that is already due,
    and settles it EXACTLY ONCE: the listing and the aggregate are both
    derived from the state that single pass leaves, so the count can
    never describe a later instant than the list beside it.

    ``aggregate_only=true`` answers from the user document alone - the
    aggregate with an empty ``items`` list, in one read, with no
    settlement. It is the mode a reputation badge wants: that surface
    needs two numbers and appears beside every listing, so downloading a
    page of reviews to render it is pure waste. It is a MODE of this
    endpoint rather than a sixth route because the five resolved paths
    are a published contract, and a query parameter extends it without
    changing it. The response shape is identical either way, so no
    client-side schema has to know which mode produced it.


    Args:
        user_id: The rated user. Validated against the Firestore
            document-ID grammar before it can reach ``document()``.
        limit: Page size, clamped by the service.
        after: Cursor from a previous page. Validated against the
            COMPOSITE rating-ID grammar, because a rating's key is
            ``"{transaction_id}_{rater_id}"`` and is longer than either
            component.

        aggregate_only: Answer from the user document alone, skipping
            settlement and the listing query.

    Returns:
        One page of published ratings received, newest first, beside the
        denormalised aggregate and the continuation metadata. The page is
        empty and the aggregate zero for a user who has never been rated,
        which is a state and not an error.


    Raises:
        HTTPException: 404 when no such user exists. The distinction
            matters to the caller: "this user has no ratings" and "there
            is no such user" are different answers, and only the first
            is a 200.
    """
    # No Firestore read happens in this handler. Existence, the
    # aggregate and the listing all come out of ONE settlement pass in
    # the service, which reads the user document once and re-reads it
    # only when settlement actually moved the aggregate. A ``None`` means
    # there is no such user, and turning that into a status code is this
    # layer's whole job here.
    reputation = get_user_reputation(
        user_id,
        limit=limit,
        after=after,
        include_items=not aggregate_only,
    )
    if reputation is None:
        raise HTTPException(
            status_code=404,
            detail=USER_NOT_FOUND_DETAIL,
        )
    return UserRatingsResponse(
        items=to_rating_views(reputation.items),
        aggregate=reputation.aggregate,
        next_cursor=reputation.next_cursor,
        has_more=reputation.has_more,
    )


@router.get(
    '/transaction/{transaction_id}',
    response_model=List[RatingView],
    summary='Read the ratings attached to one transaction',
)
def get_transaction_ratings(
    transaction_id: DocumentId,
    current_user: User = Depends(get_current_user),
) -> List[RatingView]:
    """Report the ratings on one transaction, to its participants.

    Resolves to ``GET /api/ratings/transaction/{transaction_id}``.

    The participant guard is a structural clone of the one
    ``app/api/transactions.py`` already applies to reading a
    transaction, including the house ordering: a transaction that does
    not exist is a 404, and only an existing transaction the caller is
    not party to is a 403.

    Note precisely what that ordering does and does not achieve. It does
    NOT hide existence from an outsider - an existing transaction answers
    403 while a missing one answers 404, so the two are distinguishable.
    It is followed because it is the behaviour the transactions router
    already publishes for the same resource, and consistency between two
    endpoints over one resource is worth more here than a partial
    obfuscation: transaction IDs are opaque scatter-allocated identifiers,
    so enumerating them is not a practical attack, and the 403 itself
    already reveals that the caller is not a party.

    Being a participant does not lift the double-blind model. A caller
    always sees their OWN rating in full - including its review text even
    when moderation rejected it, because otherwise a withheld review would
    vanish with no explanation to the person who wrote it. The
    counterparty's stays hidden until it publishes. Returning both
    unconditionally would hand a participant exactly the early look the
    model exists to deny.

    Moderation state is not returned to anybody here, author included. The
    response model carries no such field: the decision it drives has
    already been applied to what the caller receives, so the state itself
    is operational and reaches only the administrator endpoint.

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
    return to_rating_views(
        list_transaction_ratings(transaction_id, current_user.id)
    )


@router.get(
    '/eligibility/{transaction_id}',
    response_model=EligibilityDecision,
    summary='Report whether the caller may rate their counterparty',
)
def get_rating_eligibility(
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
    # ``require_eligibility`` RAISES ``TransactionNotFound`` for the one
    # decision this endpoint's contract turns into a 404, and it is
    # translated through the same six-exception mapping every other
    # endpoint uses.
    #
    # An earlier revision instead compared ``decision.reason`` against
    # ``TransactionNotFound.message``, which made an HTTP status code
    # depend on the exact wording of a sentence written for a human to
    # read. Rephrasing that sentence for clarity, or localising it, would
    # silently have turned this 404 into a 200 carrying
    # ``eligible=False`` - and a caller polling for a transaction to
    # appear would then never have seen it appear. A typed signal cannot
    # be broken by an edit to prose.
    try:
        return require_eligibility(transaction_id, current_user)
    except MAPPED_DOMAIN_FAILURES as error:
        raise domain_failure(error) from error


@router.patch(
    '/{rating_id}/moderation',
    response_model=ModeratedRatingView,
    summary='Transition a rating between moderation states',
)
def set_rating_moderation(
    rating_id: RatingDocumentId,

    payload: ModerationUpdate,
    current_user: User = Depends(get_current_user),
) -> ModeratedRatingView:
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

    This is the ONE endpoint whose response carries moderation state, and
    it is admin-gated, which is what makes that appropriate: the caller
    just set the state and is entitled to read back what was recorded.
    Every other endpoint returns a view without those fields.

    Args:
        rating_id: The rating document to transition, which is
            ``"{transaction_id}_{rater_id}"``. Validated against the
            COMPOSITE rating-ID grammar rather than the component one -
            the key is as long as both participant IDs plus the joining
            underscore, so holding it to the component bound would refuse
            a key this API had itself created.
        payload: The target state, its policy reason code and an optional
            operator note.
        current_user: The authenticated caller, who must be an
            administrator.

    Returns:
        The rating in its new state, with the recorded policy code and
        note.

    Raises:
        HTTPException: 401 when unauthenticated (raised by the
            dependency); 403 when the caller is not an administrator;
            404 when no rating exists at that ID, or its stored body
            cannot be interpreted - nothing is written on either path;
            422 when a rejection is requested without a policy reason,
            when the reason is not one of the permitted policy codes, or
            when the note exceeds the bound the service applies to it.

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
            payload.moderation_note,
        )
    except ValueError as error:
        # The service refuses to withhold a rating without a recorded
        # policy basis, constrains that basis to an allow-list of policy
        # codes, and bounds the note it records. All three are reported as
        # a 422 carrying the service's own explanation - which lists the
        # permitted codes - because the request is well formed and the
        # caller can act on it. The rules are enforced there and not
        # duplicated here, so there is exactly one statement of what a
        # valid moderation decision is.
        raise HTTPException(status_code=422, detail=str(error)) from error

    if rating is None:
        raise HTTPException(
            status_code=404,
            detail=RATING_NOT_FOUND_DETAIL,
        )
    return to_moderated_rating_view(rating)
