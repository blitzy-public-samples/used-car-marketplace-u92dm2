"""Business logic for the bidirectional peer reputation system.

A buyer rates the seller and the seller rates the buyer for a purchase
they both took part in. This module owns every policy decision behind
that capability, implementing F010 "Review and Rating System" from
``documentation/Software Requirements Specifications (SRS).md`` -
F010-1 submission, F010-2 written review, F010-3 the aggregate shown on
profiles and F010-4 moderation. F010-5, search-ranking integration, is
deliberately out of scope: this module supplies ``rating_average`` and
``rating_count`` and touches nothing about listing search.

Two authorization gates are non-negotiable and both live here:

* The rater must be a verified user. "Verified" means account/email
  verification only - the flag set by F001-1 "User registration with
  email verification". It is consumed, never established: identity and
  KYC verification are excluded by the project charter.
* The two parties must be counterparties of the SAME transaction, and
  each may rate the other exactly once.

WHY THE DESIGN LOOKS THE WAY IT DOES
-------------------------------------------------------------------
Three structural decisions carry most of the weight, and each is a
response to something the datastore does or does not guarantee.

1. ``ratee_id`` and ``direction`` are DERIVED from the cited
   transaction document, never accepted from a client. That is what
   makes counterparty spoofing and self-rating structurally impossible
   rather than merely validated against: there is no input a caller can
   supply that yields a ratee other than the real counterparty. A
   ``ratee_id`` that arrives anyway is treated as a claim to check, not
   a value to use.

2. Uniqueness is enforced by the DATASTORE, through a deterministic
   document ID (``{transaction_id}_{rater_id}``) written with
   create-only semantics. Firestore offers no unique constraints and no
   unique indexes, so a "does one already exist?" read followed by a
   write would race - two concurrent submissions could both observe an
   absent document and both write. Document-ID collision cannot race,
   however the two interleave. Any existence read in this module is for
   REPORTING only and never gates a write.

3. The aggregate on the user document is maintained by an INCREMENTAL
   running mean applied inside a Firestore transaction. Recomputing it
   by querying a user's ratings is impossible inside a transaction,
   because only get-by-ID reads may be locked. Denormalising it is also
   what keeps a profile read to a single get-by-ID, which the 200 ms
   response budget in the SRS requires.

WHERE THE TRANSACTION IS, AND WHY IT IS NOT AT THE INSERT
-------------------------------------------------------------------
Under the double-blind model a rating is created unpublished and its
aggregate contribution is DEFERRED, so the insert touches exactly one
document - and a single-document Firestore write is already atomic.
Wrapping it in a transaction would add a round trip and guarantee
nothing further. The invariant only spans documents at the publication
transition, which is where ``run_in_transaction`` is therefore used:
``publish_if_window_elapsed`` reads the rating and the ratee, then
writes both; ``publish_if_reciprocal`` does the same for both sides at
once. Either the flag flips and the aggregate moves, or neither does.

This is the deliberate opposite of the pattern in
``app/api/transactions.py``, which writes the transaction document and
then updates the vehicle document separately with no rollback, so a
failure between the two leaves the data permanently inconsistent.

ERROR REPORTING: TYPED EXCEPTIONS, NOT STATUS DICTIONARIES
-------------------------------------------------------------------
Every failure here is a typed exception deriving from
:class:`RatingError`. This is a deliberate departure from the
convention in ``app/services/payment.py``, which returns
``{"success": False, "error": ..., "status": ...}``. Do not "restore
consistency" by undoing it. That convention is the direct cause of a
live defect: ``app/api/transactions.py`` consumes the returned
dictionary as ``payment_result.success``, which raises ``AttributeError``
at runtime because a dict has no such attribute. A typed exception
cannot be silently mis-consumed the same way - ignoring one is
impossible, because it propagates.

This module owns no HTTP status codes and does not import ``fastapi``.
Mapping domain failures onto responses belongs to the router, which
translates ``RaterNotVerified`` and ``NotATransactionParticipant`` to
403, ``TransactionNotFound`` to 404, ``TransactionNotCompleted`` and
``DuplicateRating`` to 409, and ``SelfRatingNotAllowed`` to 422.

MODERATION IS SENTIMENT-NEUTRAL BY CONSTRUCTION
-------------------------------------------------------------------
There is no score threshold, no score-correlated filter and no
"hide low ratings" path anywhere in this module, and none may be added.
The aggregate counts every published rating whatever its value. The FTC
Rule on the Use of Consumer Reviews and Testimonials (16 CFR Part 465)
prohibits suppressing reviews on the basis of rating or negative
sentiment, so ``moderation_status`` may only ever move on a policy
violation, with the reason recorded on the document.

Reputation records are append-only. A submitted ``score`` or ``review``
is never rewritten; a correction is a ``moderation_status`` transition
carrying a ``moderation_reason``. There is no audit or history
collection in this codebase, which is precisely why.

Pydantic v1 semantics apply throughout, matching the pin in
``backend/requirements.txt``. Every function is synchronous, because the
Firestore client in ``app/db/firestore.py`` is.
"""
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from google.api_core.exceptions import Aborted, AlreadyExists
from google.cloud import firestore
from pydantic import ValidationError

from app.core.config import settings
from app.db.firestore import create_document_with_id, db, run_in_transaction
from app.schema.rating import (
    EligibilityDecision,
    ModerationStatus,
    Rating,
    RatingAggregate,
    RatingCreate,
    RatingDirection,
)
from app.schema.user import User

logger = logging.getLogger(__name__)

# Collection names are declared once here rather than repeated as string
# literals at each call site. ``ratings`` is new; the other two already
# exist and are read exactly as the rest of the application reads them.
RATINGS_COLLECTION = 'ratings'
USERS_COLLECTION = 'users'
TRANSACTIONS_COLLECTION = 'transactions'

# The only transaction state that authorizes a rating. This is the
# literal value app/api/transactions.py writes, compared
# case-insensitively so a differently-cased document still matches.
COMPLETED_STATUS = 'completed'

# Page sizes for the two collection reads. Both are bounded so no read
# path can degrade into an unbounded scan as the collection grows.
#
# Ratings received by one user, and ratings belonging to one
# transaction, are the ONLY two query shapes this module issues. Both
# are served by the composite indexes declared in
# infrastructure/firestore.indexes.json - (ratee_id, is_published,
# created_at DESC) and (transaction_id, rater_id) - and a query using a
# prefix of an index's fields for equality is served by that same
# index. No third shape is introduced, because no third index exists.
DEFAULT_RATINGS_PAGE_SIZE = 50
DEFAULT_SWEEP_PAGE_SIZE = 200

# Firestore answers a write that lost a lock race with ABORTED rather
# than with the outcome, and ABORTED means "retry", not "failed". Two
# people submitting at the same instant - or one person double-clicking
# - is exactly that race, so the create-only write is retried a bounded
# number of times. Reproduced empirically: eight simultaneous
# submissions of the same rating produced one success, six
# AlreadyExists and one "409 Transaction lock timeout", which without
# this retry would have surfaced as a server fault instead of the
# duplicate conflict it actually was.
CREATE_RETRY_ATTEMPTS = 4
CREATE_RETRY_BASE_DELAY_SECONDS = 0.05

# The stored average is rounded to two decimals, matching how a
# reputation figure is presented ("4.5/5"). Rounding the stored value
# rather than only the displayed one is a deliberate consequence of the
# incremental mean: the running total is reconstructed from the stored
# average, so successive folds inherit at most a two-decimal rounding
# error. Recomputing exactly would require querying every rating, which
# a Firestore transaction cannot do.
AGGREGATE_PRECISION = 2


class RatingError(Exception):
    """Base class for every domain failure this service reports.

    Carries a human-readable ``message`` that is reused verbatim in two
    places, which is why the wording is part of the contract rather
    than incidental: the router uses it as the ``HTTPException``
    detail, and :attr:`EligibilityDecision.reason` carries it so the
    interface can explain why submission is unavailable instead of
    letting someone compose a rating and only then fail. Because both
    the reported reason and the enforced reason come from this one
    attribute, they cannot drift apart.

    Subclasses override the class-level ``message``. Passing a message
    to the constructor overrides it for that instance, which lets a
    caller add specificity without inventing a new type.
    """

    message = 'The rating request could not be completed'

    def __init__(self, message: Optional[str] = None) -> None:
        # ``type(self).message`` reads the subclass default; assigning
        # to ``self.message`` shadows it per instance.
        self.message = message or type(self).message
        super().__init__(self.message)


class RaterNotVerified(RatingError):
    """The caller's account is not verified. Requirement R1.

    Evaluated before the transaction is read, so an unverified caller
    learns that verification is the obstacle rather than receiving a
    participation error they cannot act on. Router maps this to 403.
    """

    message = 'Only verified users can submit ratings'


class TransactionNotFound(RatingError):
    """No transaction exists at the cited ID. Router maps this to 404."""

    message = 'Transaction not found'


class NotATransactionParticipant(RatingError):
    """The caller is neither the buyer nor the seller. Requirement R2.

    Also raised when a caller supplies a ``ratee_id`` that disagrees
    with the counterparty derived from the transaction, since that is a
    claim to rate somebody the transaction does not connect them to.
    Router maps this to 403.
    """

    message = 'You are not a party to this transaction'


class TransactionNotCompleted(RatingError):
    """The transaction has not reached its terminal state.

    A rating attests to a completed exchange, so an in-flight or failed
    transaction authorizes nothing. Router maps this to 409.
    """

    message = 'Ratings require a completed transaction'


class DuplicateRating(RatingError):
    """This rater has already rated this transaction.

    Raised only from a rejected create-only write, never from a
    preceding existence read, so it holds under concurrent submission.
    Router maps this to 409.
    """

    message = 'You have already rated this transaction'


class SelfRatingNotAllowed(RatingError):
    """The derived counterparty is the caller themselves.

    Unreachable through client input, because the counterparty is
    computed rather than accepted. It can only arise from a degenerate
    transaction whose buyer and seller are the same user. Router maps
    this to 422.
    """

    message = 'Self-rating is not permitted'


def rating_document_id(transaction_id: str, rater_id: str) -> str:
    """Compose the deterministic document ID for one rating.

    The natural key IS the document ID: one rating per rater per
    transaction, so a collision means a duplicate vote. This is the
    module's uniqueness mechanism, and it is exposed rather than inlined
    so the router, the publication paths and the tests all derive the
    same key from the same code.

    Both components are Firestore scatter-allocated identifiers, so the
    resulting key space is well distributed. That matters: Google's
    guidance warns that monotonically increasing document IDs create
    write hotspots, and a composite of two random IDs has none of that
    character.

    Args:
        transaction_id: Transaction that authorizes the rating.
        rater_id: ID of the authenticated user submitting it.

    Returns:
        ``"{transaction_id}_{rater_id}"``.
    """
    return '{0}_{1}'.format(transaction_id, rater_id)


def _load_transaction(transaction_id: str) -> Optional[Dict[str, Any]]:
    """Read the cited transaction document once, as a raw dict.

    Deliberately NOT parsed into ``app.schema.transaction.Transaction``.
    Every field of that model is required, including
    ``stripe_payment_intent_id``, so a real document missing any one of
    them would raise a validation error and surface as a server fault -
    turning a data-shape gap into an outage for a request that only
    needs three keys. Reading the dict and treating absent keys as
    absent values keeps the failure mode proportionate.

    One read is all the shared-transaction gate needs, because
    ``buyer_id`` and ``seller_id`` are sibling fields on this same
    document. No join, no fan-out query, no denormalized participant
    list.

    Args:
        transaction_id: Document ID in the ``transactions`` collection.

    Returns:
        The document body, or ``None`` when no such document exists or
        the ID is empty.
    """
    if not transaction_id:
        return None
    snapshot = (
        db.collection(TRANSACTIONS_COLLECTION)
        .document(transaction_id)
        .get()
    )
    if not snapshot.exists:
        return None
    return snapshot.to_dict() or {}


def _derive_counterparty(
    transaction: Dict[str, Any],
    caller_id: str,
) -> Tuple[Optional[str], Optional[str]]:
    """Derive the ratee and the rating direction from the transaction.

    This is the whole of requirement R0's implementation, and the reason
    self-rating and counterparty spoofing are structurally impossible:
    the counterparty is COMPUTED from the two participant fields of the
    transaction, so no request body can influence it.

    Args:
        transaction: Raw transaction document body.
        caller_id: ID of the authenticated caller.

    Returns:
        ``(ratee_id, direction)`` where ``direction`` is a
        :class:`RatingDirection` value string, or ``(None, None)`` when
        the caller is not one of the two participants.
    """
    buyer_id = transaction.get('buyer_id')
    seller_id = transaction.get('seller_id')
    if caller_id and caller_id == buyer_id:
        return seller_id, RatingDirection.BUYER_TO_SELLER.value
    if caller_id and caller_id == seller_id:
        return buyer_id, RatingDirection.SELLER_TO_BUYER.value
    return None, None


def _assess(
    transaction_id: str,
    caller: User,
    supplied_ratee_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Run the ordered eligibility guard sequence exactly once.

    This is the SINGLE source of the guard logic. Both
    :func:`evaluate_eligibility`, which reports the outcome, and
    :func:`submit_rating`, which enforces it, call this function, so the
    reason a user is shown can never disagree with the reason a write is
    refused - which is the specific failure the eligibility endpoint
    exists to prevent.

    The order is itself part of the contract, because the most specific
    failure must win so the caller receives something actionable:

    1. ``caller.is_verified`` - else :class:`RaterNotVerified`. FIRST,
       before any read, so an unverified caller who also happens not to
       be a participant learns about verification rather than about
       participation.
    2. The transaction exists - else :class:`TransactionNotFound`.
    3. The caller is the buyer or the seller - else
       :class:`NotATransactionParticipant`.
    4. The derived counterparty agrees with any supplied claim - else
       :class:`NotATransactionParticipant`; and differs from the caller
       - else :class:`SelfRatingNotAllowed`.
    5. The transaction is ``completed`` - else
       :class:`TransactionNotCompleted`.

    Uniqueness is NOT assessed here. It is enforced by the datastore at
    the moment of the create-only write, because an existence read
    followed by a write would race.

    Nothing in this function writes.

    Args:
        transaction_id: Transaction the rating would be attached to.
        caller: Authenticated user, as resolved by the router.
        supplied_ratee_id: A counterparty the client claimed, if any.
            Checked against the derived value and otherwise discarded;
            never used as the ratee.

    Returns:
        A dict with keys ``error`` (a :class:`RatingError` instance, or
        ``None`` when every guard passed), ``ratee_id``, ``direction``
        and ``vehicle_listing_id``. The three context keys are ``None``
        on any decision that stopped before the caller was confirmed a
        participant, so a rejected caller learns nothing about the
        counterparty.
    """
    outcome: Dict[str, Any] = {
        'error': None,
        'ratee_id': None,
        'direction': None,
        'vehicle_listing_id': None,
    }

    # Guard 1 - R1. Evaluated before anything is read, both because it
    # is the cheapest check and because its ordering is load-bearing.
    # ``get_current_user`` re-reads the user document on every request,
    # so revoking verification takes effect on the caller's very next
    # call with no token rotation.
    if not getattr(caller, 'is_verified', False):
        outcome['error'] = RaterNotVerified()
        return outcome

    # Guard 2 - the transaction must exist. A single get-by-ID.
    transaction = _load_transaction(transaction_id)
    if transaction is None:
        outcome['error'] = TransactionNotFound()
        return outcome

    # Guard 3 - R2. Structurally the same check the transactions router
    # already performs before disclosing a transaction: membership of
    # the participant pair, with a typed exception in place of its
    # HTTPException. A document with no participant IDs at all yields
    # "not a participant" rather than a key error.
    caller_id = getattr(caller, 'id', None)
    participants = [
        transaction.get('buyer_id'),
        transaction.get('seller_id'),
    ]
    if not caller_id or caller_id not in participants:
        outcome['error'] = NotATransactionParticipant()
        return outcome

    # Guard 4 - derive the counterparty, then validate any claim about
    # it. A supplied ratee_id is evidence to check, never a value to
    # use; disagreement means the caller is asserting a relationship the
    # transaction does not establish.
    ratee_id, direction = _derive_counterparty(transaction, caller_id)
    if supplied_ratee_id and supplied_ratee_id != ratee_id:
        outcome['error'] = NotATransactionParticipant()
        return outcome
    if not ratee_id:
        # The transaction names this caller as one participant but the
        # other is missing from the document. There is nobody to rate.
        outcome['error'] = NotATransactionParticipant()
        return outcome
    if ratee_id == caller_id:
        # Only reachable from a degenerate transaction whose buyer and
        # seller are the same user, never from client input.
        outcome['error'] = SelfRatingNotAllowed()
        return outcome

    outcome['ratee_id'] = ratee_id
    outcome['direction'] = direction
    outcome['vehicle_listing_id'] = transaction.get('vehicle_listing_id')

    # Guard 5 - a rating attests to a completed exchange. Compared
    # case-insensitively so a differently-cased document still matches.
    status = transaction.get('status')
    status_text = str(status).strip().lower() if status else ''
    if status_text != COMPLETED_STATUS:
        outcome['error'] = TransactionNotCompleted()
        return outcome

    return outcome


def _fold_scores(
    current_average: Optional[float],
    current_count: Optional[int],
    scores: Iterable[int],
) -> Tuple[Optional[float], int]:
    """Fold newly published scores into a running mean.

    The ONE place the aggregate arithmetic lives. The create path, the
    reciprocal-publication path and the window-expiry path all route
    through here, so they cannot diverge - and divergence would be
    invisible until a user's reputation was already wrong.

    An incremental mean is not a preference, it is the only option: a
    Firestore transaction can lock get-by-ID reads but not a query, so
    the average cannot be recomputed from the user's ratings inside the
    transaction that must apply it atomically.

    Every published rating is counted, whatever its score. There is no
    threshold and no sentiment weighting here, and none may be added.

    Args:
        current_average: Stored average, or ``None`` when the user has
            no published ratings yet.
        current_count: Stored count. Absent, negative or non-numeric
            values are treated as zero, because user documents written
            before these fields existed simply lack them - which is
            what makes this feature migration-free.
        scores: Scores becoming published in this operation. May be
            empty, in which case the aggregate is returned unchanged.

    Returns:
        ``(average, count)`` after the fold. For the very first rating
        this is ``(float(score), 1)``: the average moves from ``None``
        to exactly the submitted score.
    """
    base_count = 0
    if isinstance(current_count, (int, float)) and current_count > 0:
        base_count = int(current_count)

    base_average = 0.0
    if isinstance(current_average, (int, float)):
        base_average = float(current_average)

    added = [int(score) for score in scores]
    total_count = base_count + len(added)
    if total_count <= 0:
        # No published ratings before and none now: "no ratings yet"
        # must stay distinguishable from a genuine average of zero.
        return None, 0

    running_total = base_average * base_count + sum(added)
    average = round(running_total / total_count, AGGREGATE_PRECISION)
    return average, total_count


def _as_aware_datetime(value: Any) -> Optional[datetime]:
    """Coerce a stored timestamp into a timezone-aware ``datetime``.

    Three shapes reach this function and all three must be survivable.
    Firestore returns ``DatetimeWithNanoseconds`` already carrying UTC,
    which passes through. ``firestore.SERVER_TIMESTAMP`` is a sentinel
    object, not a datetime, and appears whenever an unwritten payload is
    inspected. And the field is simply absent on a document the server
    has not stamped yet.

    Returning ``None`` for the latter two is what keeps the window
    arithmetic from raising: comparing a naive datetime with an aware one
    raises ``TypeError``, and comparing a sentinel with anything raises
    outright, so neither is ever allowed to reach a comparison.

    Args:
        value: Candidate timestamp of any shape.

    Returns:
        A timezone-aware ``datetime``, or ``None`` when the value is not
        a usable point in time. A naive datetime is interpreted as UTC,
        which is what Firestore stores.
    """
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _window_elapsed(created_at: Any) -> bool:
    """Report whether the rating window has closed for a timestamp.

    The single home of the window arithmetic. ``RATING_WINDOW_DAYS`` is
    read here at call time rather than captured at import, so this
    module adds no import-time failure mode and a reconfigured window
    takes effect without a restart.

    Args:
        created_at: When the rating was created, in any of the shapes
            :func:`_as_aware_datetime` accepts.

    Returns:
        ``True`` only when the timestamp is usable AND at least
        ``settings.RATING_WINDOW_DAYS`` have passed since it. An
        unusable timestamp reports ``False`` - not yet elapsed - which
        is the safe direction: it defers publication rather than
        revealing a rating early.
    """
    moment = _as_aware_datetime(created_at)
    if moment is None:
        return False
    window_days = max(0, int(settings.RATING_WINDOW_DAYS))
    deadline = moment + timedelta(days=window_days)
    return datetime.now(timezone.utc) >= deadline


def _rating_from_dict(data: Dict[str, Any]) -> Optional[Rating]:
    """Build a :class:`Rating` from a document body, tolerantly.

    A document that cannot be parsed - a score outside the bound written
    by some earlier code path, say, or a missing required field - is
    logged and skipped rather than allowed to fail the whole read. One
    malformed record must not make a user's entire reputation
    unreadable.

    Args:
        data: Document body as stored.

    Returns:
        The parsed model, or ``None`` when the body does not satisfy the
        contract.
    """
    try:
        return Rating(**data)
    except ValidationError:
        logger.warning(
            'Skipping malformed rating document %s',
            data.get('id'),
        )
        return None


def _ratings_from_snapshots(snapshots: Iterable[Any]) -> List[Rating]:
    """Parse an iterable of Firestore snapshots into rating models.

    The stored ``id`` field is reconciled with the document ID so a
    document written without it still yields a usable model.

    Args:
        snapshots: Firestore document snapshots.

    Returns:
        Successfully parsed models, in the order supplied.
    """
    parsed: List[Rating] = []
    for snapshot in snapshots:
        body = snapshot.to_dict() or {}
        body.setdefault('id', snapshot.id)
        rating = _rating_from_dict(body)
        if rating is not None:
            parsed.append(rating)
    return parsed


def _rating_exists(transaction_id: str, rater_id: str) -> bool:
    """Report whether this rater has already rated this transaction.

    REPORTING ONLY. This read must never gate a write: two concurrent
    submissions could both observe an absent document and both proceed.
    The create-only write is the uniqueness enforcement point, and it
    cannot race. This function exists so the interface can say "you have
    already rated this transaction" up front, which is a presentation
    concern rather than an authorization one.

    Args:
        transaction_id: Transaction the rating would belong to.
        rater_id: ID of the prospective rater.

    Returns:
        ``True`` when a rating document already exists at the natural
        key. ``False`` when it does not, or when either component of
        the key is missing.
    """
    if not transaction_id or not rater_id:
        return False
    document_id = rating_document_id(transaction_id, rater_id)
    snapshot = (
        db.collection(RATINGS_COLLECTION).document(document_id).get()
    )
    return bool(snapshot.exists)


def _create_rating_document(
    document_id: str,
    body: Dict[str, Any],
) -> None:
    """Write the rating create-only, translating a collision.

    The uniqueness enforcement point. Nothing preceding this decides
    whether a duplicate exists - Firestore does, by refusing to create a
    document that is already there, which is the one guarantee that
    holds however two concurrent submissions interleave.

    A lock race is handled separately from a collision, because they
    mean opposite things. ``AlreadyExists`` is a verdict: somebody else
    already rated, and that is final. ``Aborted`` is not a verdict at
    all - Firestore is saying it could not decide in time and the write
    should be retried. Treating the second as the first would report a
    duplicate that may never have existed; letting it escape untouched
    would report a server fault for what is really a retryable
    contention blip. So it is retried, and the retry then produces the
    genuine verdict either way: the create succeeds, or it collides.

    Retrying is safe precisely because the write is create-only and the
    key is deterministic. An aborted write did not commit, and a
    successful retry cannot produce a second document.

    Args:
        document_id: Deterministic natural key for the rating.
        body: Document body to persist.

    Raises:
        DuplicateRating: A rating already exists at this key.
        google.api_core.exceptions.Aborted: Contention persisted across
            every attempt. Left to propagate deliberately - it is a
            transient infrastructure condition rather than a domain
            outcome, and the operation is safe to retry again.
    """
    for attempt in range(CREATE_RETRY_ATTEMPTS):
        try:
            create_document_with_id(
                RATINGS_COLLECTION,
                document_id,
                body,
            )
            return
        except AlreadyExists:
            raise DuplicateRating()
        except Aborted:
            if attempt == CREATE_RETRY_ATTEMPTS - 1:
                logger.warning(
                    'Rating %s abandoned after %d contended attempts',
                    document_id,
                    CREATE_RETRY_ATTEMPTS,
                )
                raise
            # Exponential back-off. No jitter: the contenders here are a
            # single transaction's two participants, so the herd is two
            # writers at most and spreading them needs nothing more.
            time.sleep(CREATE_RETRY_BASE_DELAY_SECONDS * (2 ** attempt))


def evaluate_eligibility(
    transaction_id: str,
    caller: User,
) -> EligibilityDecision:
    """Report whether the caller may rate their counterparty.

    Runs the identical guard sequence :func:`submit_rating` enforces,
    but reports the outcome instead of raising it. The reason string is
    taken from the very exception the write path would have raised, so
    what the interface explains and what the server enforces are the
    same sentence by construction.

    That matters beyond tidiness: an interface that cannot say why an
    action is unavailable leaves the user to compose a rating and
    discover the refusal afterwards, which is exactly the failure mode
    this endpoint exists to remove.

    This function performs no writes. Publication is never triggered
    from here, deliberately - an eligibility check is a question, not an
    event.

    Args:
        transaction_id: Transaction the caller is asking about.
        caller: Authenticated user, as resolved by the router.

    Returns:
        An :class:`EligibilityDecision`. ``ratee_id`` and ``direction``
        are populated only once the caller is confirmed a participant of
        a transaction that names both parties, so a rejected caller
        learns nothing about the counterparty.
    """
    caller_id = getattr(caller, 'id', None) or ''

    # Probed unconditionally so ``already_rated`` is always truthful,
    # including for a participant whose verification was revoked after
    # they rated. Cheap: one get-by-ID on a read-only endpoint.
    already_rated = _rating_exists(transaction_id, caller_id)

    outcome = _assess(transaction_id, caller)
    error = outcome['error']
    if error is not None:
        return EligibilityDecision(
            eligible=False,
            reason=error.message,
            ratee_id=outcome['ratee_id'],
            direction=outcome['direction'],
            already_rated=already_rated,
        )

    if already_rated:
        # Reported, not enforced. The write path reaches the same
        # conclusion from the datastore's rejection instead.
        return EligibilityDecision(
            eligible=False,
            reason=DuplicateRating.message,
            ratee_id=outcome['ratee_id'],
            direction=outcome['direction'],
            already_rated=True,
        )

    return EligibilityDecision(
        eligible=True,
        reason=None,
        ratee_id=outcome['ratee_id'],
        direction=outcome['direction'],
        already_rated=False,
    )


def submit_rating(payload: RatingCreate, caller: User) -> Rating:
    """Record one directional rating for a completed transaction.

    Both authorization gates are enforced before anything is persisted,
    which is the point of the ordering: a guard that rejected a caller
    only after writing would satisfy the letter of the requirement and
    defeat its purpose. Nothing reaches the datastore until every guard
    has passed.

    The rating is stored unpublished and contributes nothing to the
    ratee's aggregate yet. Under the double-blind model it becomes
    visible when the counterparty submits theirs or when the rating
    window closes, and only then does the aggregate move. Deferring the
    contribution is what removes the incentive for review extortion -
    neither side can see what the other said in time to retaliate.

    Because the contribution is deferred, this insert touches exactly
    one document, and a single-document Firestore write is already
    atomic. The multi-document transaction belongs at the publication
    transition, where the invariant genuinely spans documents, and that
    is where :func:`publish_if_window_elapsed` and
    :func:`publish_if_reciprocal` apply it.

    Args:
        payload: Validated request body carrying ``transaction_id``,
            ``score`` and an optional ``review``. The score bound and
            the review length limit are enforced by the schema before
            this function runs and are deliberately not re-checked here.
        caller: Authenticated user, as resolved by the router. Supplies
            ``rater_id``; nothing in the payload can influence it.

    Returns:
        The persisted rating. Its ``created_at``/``updated_at`` carry a
        real UTC timestamp rather than the server-side sentinel that was
        written, because the sentinel is not a serialisable value; the
        authoritative times are whatever the server stamped.
        ``is_published`` reflects whether the counterparty's rating was
        already waiting, in which case both went live during this call.

    Raises:
        RaterNotVerified: The caller's account is not verified (R1).
        TransactionNotFound: No such transaction.
        NotATransactionParticipant: The caller is not a party to it, or
            claimed a counterparty that disagrees with the transaction
            (R2).
        SelfRatingNotAllowed: The transaction names the caller as both
            parties.
        TransactionNotCompleted: The transaction is not ``completed``.
        DuplicateRating: This rater has already rated this transaction.
    """
    transaction_id = getattr(payload, 'transaction_id', None) or ''
    score = int(getattr(payload, 'score'))
    review = getattr(payload, 'review', None)
    # RatingCreate deliberately has no ratee_id. If one arrives anyway,
    # it is a claim to be checked against the derived counterparty and
    # discarded either way - never a value to use.
    supplied_ratee_id = getattr(payload, 'ratee_id', None)

    outcome = _assess(
        transaction_id,
        caller,
        supplied_ratee_id=supplied_ratee_id,
    )
    if outcome['error'] is not None:
        raise outcome['error']

    rater_id = getattr(caller, 'id')
    document_id = rating_document_id(transaction_id, rater_id)

    # Field names here are load-bearing beyond this module: the declared
    # composite indexes reference them by exact string, and a mismatch
    # produces an index that silently serves nothing rather than an
    # error. They mirror app/schema/rating.py one for one.
    body: Dict[str, Any] = {
        'id': document_id,
        'transaction_id': transaction_id,
        # Denormalised from the same transaction read, so a rating can
        # be rendered with context without a second lookup. Coerced to
        # a string because the field is required by the contract even
        # when the source document omits it.
        'vehicle_listing_id': outcome['vehicle_listing_id'] or '',
        'rater_id': rater_id,
        'ratee_id': outcome['ratee_id'],
        'direction': outcome['direction'],
        'score': score,
        'review': review,
        'is_published': False,
        'moderation_status': ModerationStatus.PENDING.value,
        'moderation_reason': None,
        'created_at': firestore.SERVER_TIMESTAMP,
        'updated_at': firestore.SERVER_TIMESTAMP,
    }

    # Create-only. The collision IS the duplicate-vote signal, and it is
    # the enforcement point precisely because it cannot race: whichever
    # way two concurrent submissions interleave, the second is rejected.
    _create_rating_document(document_id, body)

    published_ids: List[str] = []
    try:
        published_ids = publish_if_reciprocal(transaction_id)
    except Exception:
        # The rating is committed; a failure to reveal it must not
        # present as a failure to record it, or a retry would return a
        # duplicate conflict for work that actually succeeded. The
        # rating stays unpublished and contributes nothing, and the
        # reciprocal check on the next read, or the window sweep,
        # publishes it. Logged rather than swallowed silently.
        logger.exception(
            'Deferred publication check failed for rating %s',
            document_id,
        )

    stamped = datetime.now(timezone.utc)
    result = dict(body)
    result['is_published'] = document_id in published_ids
    result['created_at'] = stamped
    result['updated_at'] = stamped
    return Rating(**result)


def _rating_field(rating: Any, name: str, default: Any = None) -> Any:
    """Read one field from a rating supplied as a dict or as a model.

    The publication entry points accept either shape so a caller that
    streamed raw documents - the background sweep, for instance - can
    hand over exactly what it has without converting first.

    Args:
        rating: Rating record, as a mapping or as a model.
        name: Field name to read.
        default: Value to return when the field is absent.

    Returns:
        The field value, or ``default``.
    """
    if isinstance(rating, dict):
        return rating.get(name, default)
    return getattr(rating, name, default)


def _apply_publication(
    transaction: firestore.Transaction,
    pending: List[Dict[str, Any]],
) -> List[str]:
    """Flip ratings to published and apply their deferred aggregates.

    The write half of every publication path, shared so the reciprocal
    transition and the window transition cannot diverge. Both halves
    commit inside the caller's transaction: either every flag moves and
    every aggregate moves with it, or nothing does. That is the whole
    reason a transaction is used here rather than at the insert - this
    is the only point where the invariant spans documents.

    All reads have already been performed by the caller, because
    Firestore rejects a read issued after the transaction's first write.

    Args:
        transaction: Active Firestore transaction.
        pending: One entry per rating to publish, each carrying
            ``rating_ref``, ``rating_id``, ``ratee_id``, ``score``,
            ``user_ref``, ``user_exists``, ``current_average`` and
            ``current_count``. Entries sharing a ``ratee_id`` are folded
            into a single aggregate write, so no user document is ever
            written twice in one transaction - which Firestore would
            otherwise leave with only the last write's value.

    Returns:
        IDs of the ratings that were published.
    """
    if not pending:
        return []

    stamp = {
        'is_published': True,
        'updated_at': firestore.SERVER_TIMESTAMP,
    }

    published: List[str] = []
    for entry in pending:
        transaction.update(entry['rating_ref'], dict(stamp))
        published.append(entry['rating_id'])

    # Group by ratee so a transaction that publishes both directions of
    # a degenerate transaction - the same user on both sides - folds
    # both scores into one write instead of losing one of them.
    grouped: Dict[str, Dict[str, Any]] = {}
    for entry in pending:
        ratee_id = entry.get('ratee_id')
        score = entry.get('score')
        if not ratee_id or not entry.get('user_exists'):
            # No counterparty document to credit. The rating still
            # publishes; there is simply no profile to aggregate onto,
            # and inventing a user document to hold a counter would
            # create a record that fails the user contract.
            continue
        if score is None:
            continue
        bucket = grouped.setdefault(
            ratee_id,
            {
                'user_ref': entry['user_ref'],
                'current_average': entry.get('current_average'),
                'current_count': entry.get('current_count'),
                'scores': [],
            },
        )
        bucket['scores'].append(score)

    for bucket in grouped.values():
        average, count = _fold_scores(
            bucket['current_average'],
            bucket['current_count'],
            bucket['scores'],
        )
        transaction.update(
            bucket['user_ref'],
            {
                'rating_average': average,
                'rating_count': count,
                'updated_at': firestore.SERVER_TIMESTAMP,
            },
        )

    return published


def _collect_pending(
    transaction: firestore.Transaction,
    document_ids: List[str],
    require_window: bool,
) -> List[Dict[str, Any]]:
    """Lock the candidate documents and decide which may publish.

    Performs every read the publication needs, in the order Firestore
    demands: all ratings, then all counterparty user documents, and only
    then does the caller write. Re-reading under the lock is what makes
    publication idempotent - a rating another request already published
    is filtered out here, so its score can never be counted twice.

    Args:
        transaction: Active Firestore transaction.
        document_ids: Rating document IDs to consider.
        require_window: When ``True``, a rating publishes only if its
            window has closed; when ``False``, eligibility was
            established by the caller (the counterparty has submitted).

    Returns:
        Entries in the shape :func:`_apply_publication` expects, one per
        rating that may publish now.
    """
    candidates: List[Dict[str, Any]] = []
    for document_id in document_ids:
        rating_ref = (
            db.collection(RATINGS_COLLECTION).document(document_id)
        )
        snapshot = rating_ref.get(transaction=transaction)
        if not snapshot.exists:
            continue
        body = snapshot.to_dict() or {}
        if body.get('is_published'):
            continue
        if require_window and not _window_elapsed(body.get('created_at')):
            continue
        candidates.append({
            'rating_ref': rating_ref,
            'rating_id': document_id,
            'ratee_id': body.get('ratee_id'),
            'score': body.get('score'),
        })

    if not candidates:
        return []

    # Second read phase: one get-by-ID per distinct counterparty. Still
    # strictly before any write.
    user_state: Dict[str, Dict[str, Any]] = {}
    for candidate in candidates:
        ratee_id = candidate.get('ratee_id')
        if not ratee_id or ratee_id in user_state:
            continue
        user_ref = db.collection(USERS_COLLECTION).document(ratee_id)
        user_snapshot = user_ref.get(transaction=transaction)
        user_body = (
            user_snapshot.to_dict() or {} if user_snapshot.exists else {}
        )
        user_state[ratee_id] = {
            'user_ref': user_ref,
            'user_exists': bool(user_snapshot.exists),
            # Absent on any user document written before these fields
            # existed. Treated as "no reputation yet", which is the
            # correct reading and why no backfill is required.
            'current_average': user_body.get('rating_average'),
            'current_count': user_body.get('rating_count', 0),
        }

    for candidate in candidates:
        state = user_state.get(candidate.get('ratee_id') or '')
        if state is None:
            candidate['user_ref'] = None
            candidate['user_exists'] = False
            candidate['current_average'] = None
            candidate['current_count'] = 0
        else:
            candidate.update(state)
    return candidates


def _publish_transaction_body(
    transaction: firestore.Transaction,
    document_ids: List[str],
    require_window: bool,
) -> List[str]:
    """Read, then write, one publication inside a single transaction.

    Kept as a top-level function rather than a closure because
    Firestore's optimistic concurrency reruns the callable on contention,
    so it must be free of state that would not survive a second run.

    Args:
        transaction: Active Firestore transaction, supplied positionally
            by :func:`app.db.firestore.run_in_transaction`.
        document_ids: Rating document IDs to consider.
        require_window: Whether the rating window must have closed.

    Returns:
        IDs of the ratings published by this transaction.
    """
    pending = _collect_pending(transaction, document_ids, require_window)
    return _apply_publication(transaction, pending)


def _transaction_rating_snapshots(transaction_id: str) -> List[Any]:
    """Fetch every rating attached to one transaction.

    A single-equality query, served by the declared
    ``(transaction_id, rater_id)`` composite index and by Firestore's
    automatic single-field index alike. Bounded even though the natural
    key permits at most one rating per participant, so a corrupted
    collection cannot turn this into an unbounded read.

    Args:
        transaction_id: Transaction whose ratings are wanted.

    Returns:
        Document snapshots, unordered.
    """
    if not transaction_id:
        return []
    query = (
        db.collection(RATINGS_COLLECTION)
        .where(
            filter=firestore.FieldFilter(
                'transaction_id', '==', transaction_id
            )
        )
        .limit(DEFAULT_RATINGS_PAGE_SIZE)
    )
    return list(query.stream())


def publish_if_reciprocal(transaction_id: str) -> List[str]:
    """Publish both sides once both have rated. The blind reveal.

    A mutual rating system in which each side sees the other's score
    before committing their own invites review extortion - the threat of
    a bad review in exchange for a good one. The established mitigation,
    and the one implemented here, is a double-blind reveal: neither
    rating is visible, and neither counts toward a reputation, until both
    have been submitted.

    When both sides are present, every rating still unpublished flips and
    every deferred aggregate applies inside ONE transaction, so the two
    reputations move together or not at all. A half-applied reveal would
    show one side's verdict while withholding the other's, which is the
    precise state the model exists to prevent.

    Safe to call repeatedly. Ratings already published are re-read under
    the transaction's lock and skipped, so no score is ever counted
    twice.

    Args:
        transaction_id: Transaction to examine.

    Returns:
        IDs of the ratings published by this call, empty when the
        counterparty has not rated yet or when both sides were already
        published.
    """
    snapshots = _transaction_rating_snapshots(transaction_id)
    if len(snapshots) < 2:
        return []

    document_ids: List[str] = []
    raters = set()
    unpublished = False
    for snapshot in snapshots:
        body = snapshot.to_dict() or {}
        document_ids.append(snapshot.id)
        rater_id = body.get('rater_id')
        if rater_id:
            raters.add(rater_id)
        if not body.get('is_published'):
            unpublished = True

    # Both directions must exist. Two documents from the same rater
    # cannot happen through this service - the natural key forbids it -
    # but the reveal is gated on distinct raters rather than on a
    # document count so a corrupted collection cannot trigger it.
    if len(raters) < 2 or not unpublished:
        return []

    published = run_in_transaction(
        _publish_transaction_body,
        document_ids,
        False,
    )
    if published:
        logger.info(
            'Published %d rating(s) for transaction %s on reciprocal '
            'submission',
            len(published),
            transaction_id,
        )
    return list(published or [])


def publish_if_window_elapsed(rating: Any) -> bool:
    """Publish one unreciprocated rating once its window has closed.

    The second of the two publication paths. Without it a rating whose
    counterparty never responds would stay invisible forever, so a
    non-participating counterparty could suppress a verdict simply by
    declining to answer.

    This is the only place the window arithmetic is applied, which is why
    the background sweep delegates here instead of repeating it: a second
    copy of the deadline calculation would eventually disagree with this
    one.

    Idempotent. The rating is re-read under the transaction's lock and
    skipped if already published, so calling this twice cannot increment
    an aggregate twice.

    Args:
        rating: The rating to consider, as a mapping or a model. Only
            its ID is trusted; publication state, score, counterparty
            and creation time are re-read from the locked document.

    Returns:
        ``True`` when this call published the rating, ``False`` when it
        was already published, does not exist, or its window is still
        open. An unusable ``created_at`` - absent, or the unwritten
        server-side sentinel - counts as "still open", which defers the
        reveal rather than risking an early one.
    """
    document_id = _rating_field(rating, 'id')
    if not document_id:
        return False
    if _rating_field(rating, 'is_published'):
        return False
    # Cheap pre-check outside the transaction, purely to avoid opening
    # one for a rating that plainly is not due. The authoritative check
    # happens again under the lock.
    if not _window_elapsed(_rating_field(rating, 'created_at')):
        return False

    published = run_in_transaction(
        _publish_transaction_body,
        [document_id],
        True,
    )
    if published:
        logger.info(
            'Published rating %s on rating-window expiry',
            document_id,
        )
    return bool(published)


def publish_expired_ratings(
    limit: int = DEFAULT_SWEEP_PAGE_SIZE,
) -> int:
    """Publish every unreciprocated rating whose window has closed.

    The sweep behind the deferred reveal, exposed here so the Celery task
    that schedules it stays a thin delegation and every query shape and
    every deadline calculation remains in this module.

    Correctness does not depend on that task ever running. No task in
    this codebase is dispatched, no broker is configured, and the read
    paths therefore publish opportunistically when they encounter a rating
    that is due. This function is the scheduled equivalent of the same
    work, not a prerequisite for it.

    Args:
        limit: Maximum number of unpublished ratings to examine in one
            pass. Bounded so a large backlog is drained across several
            runs rather than in one unbounded read.

    Returns:
        How many ratings this pass published.
    """
    query = (
        db.collection(RATINGS_COLLECTION)
        .where(
            filter=firestore.FieldFilter('is_published', '==', False)
        )
        .limit(max(1, int(limit)))
    )

    published = 0
    for snapshot in query.stream():
        body = snapshot.to_dict() or {}
        body.setdefault('id', snapshot.id)
        try:
            if publish_if_window_elapsed(body):
                published += 1
        except Exception:
            # One unpublishable record must not abort the sweep; the
            # next pass will retry it.
            logger.exception(
                'Could not publish expired rating %s',
                snapshot.id,
            )
    return published


def _publish_due_for_ratee(user_id: str) -> None:
    """Bring one user's pending ratings up to date before reading them.

    Publication cannot be left to the background worker, because there
    is none: no task in this codebase is ever dispatched and no broker is
    configured. A reputation that only became correct once a worker ran
    would simply never become correct. So a read that is about to report
    a user's reputation first settles anything already due.

    Both publication paths are attempted - the window first, since that
    is the one a read is obliged to apply, and then the reciprocal
    check for whatever remains, which repairs a reveal that failed
    earlier rather than leaving it stranded until the window closes.

    Failures are logged and swallowed on purpose: a read must still
    return the data it can, and every publication path is idempotent, so
    the next read simply retries.

    Args:
        user_id: The rated user whose pending ratings are settled.
    """
    if not user_id:
        return
    query = (
        db.collection(RATINGS_COLLECTION)
        .where(filter=firestore.FieldFilter('ratee_id', '==', user_id))
        .where(
            filter=firestore.FieldFilter('is_published', '==', False)
        )
        .limit(DEFAULT_SWEEP_PAGE_SIZE)
    )

    unresolved = set()
    for snapshot in query.stream():
        body = snapshot.to_dict() or {}
        body.setdefault('id', snapshot.id)
        try:
            if publish_if_window_elapsed(body):
                continue
        except Exception:
            logger.exception(
                'Window publication failed for rating %s',
                snapshot.id,
            )
            continue
        transaction_id = body.get('transaction_id')
        if transaction_id:
            unresolved.add(transaction_id)

    for transaction_id in unresolved:
        try:
            publish_if_reciprocal(transaction_id)
        except Exception:
            logger.exception(
                'Reciprocal publication failed for transaction %s',
                transaction_id,
            )


def _publish_due_for_transaction(transaction_id: str) -> None:
    """Settle any pending publication for one transaction.

    The transaction-scoped counterpart of :func:`_publish_due_for_ratee`,
    used before listing a transaction's ratings so a participant is never
    shown a stale unpublished state for a reveal that is already due.

    Args:
        transaction_id: Transaction whose ratings are settled.
    """
    if not transaction_id:
        return
    try:
        publish_if_reciprocal(transaction_id)
    except Exception:
        logger.exception(
            'Reciprocal publication failed for transaction %s',
            transaction_id,
        )

    for snapshot in _transaction_rating_snapshots(transaction_id):
        body = snapshot.to_dict() or {}
        if body.get('is_published'):
            continue
        body.setdefault('id', snapshot.id)
        try:
            publish_if_window_elapsed(body)
        except Exception:
            logger.exception(
                'Window publication failed for rating %s',
                snapshot.id,
            )


def _sort_key(rating: Rating) -> datetime:
    """Order ratings newest first, tolerating an unstamped timestamp.

    Args:
        rating: Rating to key.

    Returns:
        Its creation time, or the earliest representable instant when
        the server has not stamped it yet, so an unstamped record sorts
        last instead of raising.
    """
    moment = _as_aware_datetime(rating.created_at)
    if moment is None:
        return datetime.min.replace(tzinfo=timezone.utc)
    return moment


def _is_withheld(rating: Rating) -> bool:
    """Report whether moderation has withheld a rating from display.

    The ONLY display filter in this module, and it is driven purely by
    moderation state. There is deliberately no score threshold, no
    score-correlated filter and no "hide low ratings" path anywhere
    here, and none may be added: the FTC Rule on the Use of Consumer
    Reviews and Testimonials (16 CFR Part 465) prohibits suppressing
    reviews on the basis of rating or negative sentiment, so a one-star
    rating is displayed and counted exactly like a five-star one.
    ``moderation_reason`` exists so that a rating withheld for an actual
    policy violation carries that justification on the record.

    Args:
        rating: Rating to test.

    Returns:
        ``True`` only when moderation rejected it for a policy reason.
    """
    return rating.moderation_status == ModerationStatus.REJECTED.value


def list_ratings_for_user(
    user_id: str,
    limit: int = DEFAULT_RATINGS_PAGE_SIZE,
) -> List[Rating]:
    """List the published ratings a user has received, newest first.

    Satisfies the reputation half of F010-3, and is the public read: no
    authentication is required of the caller, matching how listings are
    read. Only published ratings are returned, so the double-blind model
    holds on the way out as well as on the way in - an unreciprocated
    rating is invisible here until it is revealed.

    Any rating already due for publication is settled first, so this
    read never reports a reputation that is merely waiting for a worker
    that will not run.

    Args:
        user_id: The rated user.
        limit: Maximum number of ratings to return.

    Returns:
        Published, non-withheld ratings ordered by creation time
        descending. Empty when the user has none.
    """
    if not user_id:
        return []

    _publish_due_for_ratee(user_id)

    # Exactly the declared (ratee_id, is_published, created_at DESC)
    # composite index. The equality-only helper in app/db/firestore.py
    # cannot express the ordering, which is why the client is used
    # directly here.
    query = (
        db.collection(RATINGS_COLLECTION)
        .where(filter=firestore.FieldFilter('ratee_id', '==', user_id))
        .where(filter=firestore.FieldFilter('is_published', '==', True))
        .order_by('created_at', direction=firestore.Query.DESCENDING)
        .limit(max(1, int(limit)))
    )

    ratings = _ratings_from_snapshots(query.stream())
    # Withheld ratings are filtered in Python rather than with a third
    # equality clause, because a third filtered field would require a
    # composite index that is not declared - and an undeclared index
    # does not fail loudly, it just returns nothing.
    return [rating for rating in ratings if not _is_withheld(rating)]


def get_user_aggregate(user_id: str) -> RatingAggregate:
    """Read a user's denormalised reputation summary.

    One get-by-ID, never a scan. That is why the aggregate is maintained
    on the user document at all: the SRS requires API responses within
    200 ms for 95% of requests, and recomputing a mean by reading every
    rating a popular seller has received would not hold to that as the
    collection grows.

    Reflects published ratings only, and includes every one of them
    whatever the score.

    Args:
        user_id: The rated user.

    Returns:
        A :class:`RatingAggregate`. ``average`` stays ``None`` and
        ``count`` stays ``0`` for a user with no published ratings, for a
        user document predating these fields, and for a user that does
        not exist - so "no ratings yet" remains distinguishable from a
        genuine average of zero.
    """
    if not user_id:
        return RatingAggregate()

    _publish_due_for_ratee(user_id)

    snapshot = db.collection(USERS_COLLECTION).document(user_id).get()
    if not snapshot.exists:
        return RatingAggregate()

    body = snapshot.to_dict() or {}
    raw_average = body.get('rating_average')
    raw_count = body.get('rating_count', 0)

    average: Optional[float] = None
    if isinstance(raw_average, (int, float)):
        average = float(raw_average)
    count = 0
    if isinstance(raw_count, (int, float)) and raw_count > 0:
        count = int(raw_count)

    return RatingAggregate(average=average, count=count)


def list_ratings_for_transaction(
    transaction_id: str,
    caller_id: Optional[str] = None,
) -> List[Rating]:
    """List the ratings attached to one transaction, newest first.

    Intended for the participant view, whose authorization the router
    performs. The double-blind model is preserved even for a
    participant: a caller sees their own rating whether or not it has
    been revealed, because it is theirs, but the counterparty's stays
    hidden until it publishes. Returning both unconditionally here would
    hand a participant exactly the early look the model exists to deny.

    Args:
        transaction_id: Transaction whose ratings are wanted.
        caller_id: The authenticated caller, when there is one. Their own
            rating is always included. Omit it for an unprivileged view,
            which then contains published, non-withheld ratings only.

    Returns:
        Visible ratings ordered by creation time descending.
    """
    if not transaction_id:
        return []

    _publish_due_for_transaction(transaction_id)

    ratings = _ratings_from_snapshots(
        _transaction_rating_snapshots(transaction_id)
    )

    visible: List[Rating] = []
    for rating in ratings:
        if caller_id and rating.rater_id == caller_id:
            # A rater always sees what they wrote, including while it is
            # unrevealed and including when it was withheld - otherwise
            # a withheld review would vanish without explanation.
            visible.append(rating)
            continue
        if not rating.is_published:
            continue
        if _is_withheld(rating):
            continue
        visible.append(rating)

    visible.sort(key=_sort_key, reverse=True)
    return visible


def get_rating(rating_id: str) -> Optional[Rating]:
    """Read one rating by its document ID.

    Args:
        rating_id: Rating document ID, which is
            ``"{transaction_id}_{rater_id}"``.

    Returns:
        The rating, or ``None`` when no such document exists or its
        stored body does not satisfy the contract.
    """
    if not rating_id:
        return None
    snapshot = db.collection(RATINGS_COLLECTION).document(rating_id).get()
    if not snapshot.exists:
        return None
    body = snapshot.to_dict() or {}
    body.setdefault('id', snapshot.id)
    return _rating_from_dict(body)


def moderate_rating(
    rating_id: str,
    status: Any,
    reason: Optional[str] = None,
) -> Optional[Rating]:
    """Move a rating's moderation state, recording why. F010-4.

    The only mutation this module performs on an existing rating, and it
    touches moderation state alone. Reputation records are append-only:
    ``score`` and ``review`` are never rewritten, so a correction is this
    state transition carrying a reason rather than an edit that erases
    what was said. There is no audit collection in this codebase, which
    is exactly why the original text has to survive.

    A transition may only ever be justified by a POLICY violation -
    abuse, personally identifying information, profanity. It may never be
    driven by the score. A one-star rating is not a violation, and
    withholding one because it is unflattering is precisely what the FTC
    Rule on the Use of Consumer Reviews and Testimonials (16 CFR Part
    465) prohibits. The aggregate continues to count every published
    rating whatever its value.

    Authorization is the router's concern; it restricts this to
    administrators the same way listing deletion is restricted.

    Args:
        rating_id: Rating document ID to transition.
        status: Target state - a :class:`ModerationStatus` member or its
            value string. Validated here, because an unrecognised value
            would otherwise be written straight onto the document and
            silently disable every state check that reads it.
        reason: The policy basis for the transition. Recorded as given
            and cleared when omitted, so approving a rating does not
            leave a stale rejection reason behind.

    Returns:
        The rating in its new state, or ``None`` when no rating exists at
        that ID - which the router reports as not found.

    Raises:
        ValueError: ``status`` is not a recognised moderation state.
    """
    if isinstance(status, ModerationStatus):
        status_value = status.value
    else:
        status_value = str(status or '').strip().lower()

    permitted = {member.value for member in ModerationStatus}
    if status_value not in permitted:
        raise ValueError(
            'Unknown moderation status: {0!r}'.format(status)
        )

    if not rating_id:
        return None

    rating_ref = db.collection(RATINGS_COLLECTION).document(rating_id)
    snapshot = rating_ref.get()
    if not snapshot.exists:
        return None

    if (
        status_value == ModerationStatus.REJECTED.value
        and not (reason or '').strip()
    ):
        # Not fatal - an administrator may withhold first and document
        # afterwards - but a rejection with no recorded policy basis is
        # exactly what makes a moderation decision unauditable, so it is
        # surfaced rather than accepted quietly.
        logger.warning(
            'Rating %s rejected with no recorded policy reason',
            rating_id,
        )

    rating_ref.update({
        'moderation_status': status_value,
        'moderation_reason': reason,
        'updated_at': firestore.SERVER_TIMESTAMP,
    })

    body = snapshot.to_dict() or {}
    body.setdefault('id', snapshot.id)
    body['moderation_status'] = status_value
    body['moderation_reason'] = reason
    # As on the create path, the returned model carries a real UTC
    # timestamp because the value actually written is a server-side
    # sentinel rather than a serialisable one.
    body['updated_at'] = datetime.now(timezone.utc)
    logger.info(
        'Rating %s moderation status set to %s',
        rating_id,
        status_value,
    )
    return _rating_from_dict(body)
