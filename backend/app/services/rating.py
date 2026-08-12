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
   running mean applied inside a Firestore transaction. Note that a
   query CAN be read through a transaction on the pinned
   google-cloud-firestore 2.13.1 - verified empirically - so the reason
   for the incremental mean is not a client limitation. It is that
   recomputing from every rating cannot hold the SRS 200 ms budget as a
   popular seller's rating count grows, and that locking a query would
   widen the transaction's conflict footprint from one document to a
   whole result set, making contention and reruns far more likely.
   Denormalising is also what keeps a profile read to a single
   get-by-ID.

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
``DuplicateRating`` to 409, and ``SelfRatingNotAllowed`` and
``TransactionNotRatable`` to 422. Every one of them derives from
``RatingError``, so a router that also catches the base class handles
anything added here later rather than leaking a 500; each carries a
``message`` fit to be shown to the caller.

DATA THIS MODULE REFUSES TO WORK WITH
-------------------------------------------------------------------
A transaction document that does not satisfy its own contract is not
evidence of anything, so it authorizes nothing: rather than substitute
a placeholder for a missing participant or a missing listing reference
and persist a rating that points nowhere, the guard sequence refuses it
(``TransactionNotRatable``). The same principle governs the publication
transition, which re-derives every authorization fact from the locked
transaction and aborts rather than publish a rating it cannot prove.
Substituting a default for absent data would turn a data-integrity
problem into a permanent, invisible reputation error.

MODERATION IS SENTIMENT-NEUTRAL BY CONSTRUCTION
-------------------------------------------------------------------
There is no score threshold, no score-correlated filter and no
"hide low ratings" path anywhere in this module, and none may be added.
The aggregate counts every published rating whatever its value. The FTC
Rule on the Use of Consumer Reviews and Testimonials (16 CFR Part 465)
prohibits suppressing reviews on the basis of rating or negative
sentiment, so ``moderation_status`` may only ever move on a policy
violation, with the reason recorded on the document - a rejection
without one is refused outright rather than logged and accepted.

THE VISIBILITY POLICY, STATED ONCE
-------------------------------------------------------------------
Three rules decide what a reader gets, and they are applied by
``_is_withheld`` and ``_visible_projection`` alone:

1. Publication gates the RECORD. Nothing unpublished is visible to
   anybody but its author - that is the double-blind model.
2. Moderation gates the CONTENT, not the score. A published rating's
   score is always shown and always counted; its free-text review is
   shown only once ``moderation_status`` is ``approved``, which is what
   "moderation before display" means in practice. A number cannot carry
   abuse or personal data, and withholding one for being low is the
   suppression the FTC rule forbids; unreviewed prose can carry both,
   so it waits. A rating REJECTED for a policy violation is withheld
   entirely.
3. Authorship overrides both, for the author only. A rater always sees
   their own rating in full, including while it is unrevealed and
   including when it was withheld, together with the reason - otherwise
   a withheld review would vanish with no explanation to the person who
   wrote it.

``moderation_reason`` is an operator note and is never returned to
anyone but the author.

Reputation records are append-only. A submitted ``score`` or ``review``
is never rewritten; a correction is a ``moderation_status`` transition
carrying a ``moderation_reason``. There is no audit or history
collection in this codebase, which is precisely why.

Pydantic v1 semantics apply throughout, matching the pin in
``backend/requirements.txt``. Every function is synchronous, because the
Firestore client in ``app/db/firestore.py`` is.
"""
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from google.api_core.exceptions import (
    Aborted,
    AlreadyExists,
    Cancelled,
    DeadlineExceeded,
    InternalServerError,
    ResourceExhausted,
    RetryError,
    ServiceUnavailable,
)
from google.cloud import firestore
from pydantic import ValidationError

from app.core.config import settings
from app.db.firestore import db, run_in_transaction
from app.schema.rating import (
    EligibilityDecision,
    ModerationStatus,
    Rating,
    RatingAggregate,
    RatingCreate,
    RatingDirection,
    as_plain_text,
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

# How far a visibility-filtered read may walk beyond what it returns.
# Moderation state cannot be filtered in the query without an index that
# is not declared, so withheld records are dropped after the read; this
# is the multiple of the requested page size that may be examined while
# collecting that many visible records, which bounds the work while
# still letting the walk see past a run of withheld ones.
MAX_VISIBILITY_SCAN_FACTOR = 10

# How many DUE ratings a single public read may settle before answering.
# Publication has no worker behind it, so a read that is about to report
# a reputation settles what is already due first - but it runs on the
# critical path of a public read against the SRS 200 ms budget, so the
# work is bounded and the remainder drains across successive reads.
SETTLE_BATCH_SIZE = 10

# Ceiling on documents a single sweep pass may examine. The sweeps walk
# a cursor rather than re-reading one unordered page, so a pass covers
# every unpublished rating up to this bound instead of revisiting the
# same records while later ones starve. A backlog larger than this
# drains across successive passes, and each pass resumes from the start
# of the ordering - which is stable, because a published rating leaves
# the query.
DEFAULT_SWEEP_SCAN_LIMIT = 5000

# Ceiling on a caller-supplied page size. A lower bound alone is not a
# bound: an unclamped ``limit`` is passed straight to Firestore and turns
# a public read into a collection scan, so both ends are closed by
# _page_size().
MAX_RATINGS_PAGE_SIZE = 200

# The Firestore document-ID grammar, mirrored from
# ``app.schema.rating.DocumentId`` so the two entry points that receive a
# raw path parameter - the eligibility check and the per-transaction
# listing - are held to the same rule as a validated request body. An
# unconstrained value reaches ``document()``, where a forward slash is
# read as a NESTED PATH rather than an identifier, so it must be refused
# before any lookup. ``\Z`` rather than ``$`` is load-bearing: ``$`` also
# matches just before a trailing newline, which would admit "abc\n".
DOCUMENT_ID_MAX_LENGTH = 128
_DOCUMENT_ID_RE = re.compile(
    r'^(?!\.\.?\Z)'          # not "." and not ".."
    r'(?!__.*__\Z)'          # not Firestore's reserved __.*__ namespace
    r'[^/\x00-\x1F\x7F]+\Z'  # no slash, no ASCII control characters
)

# Sentinel distinguishing "this argument was not supplied" from "it was
# supplied as None". ``None`` cannot carry that distinction here, because
# a transaction body of ``None`` is itself a decision: it means the
# transaction document does not exist. Used by _assess() so the
# transactional write path can hand over the snapshots it already holds
# under the lock instead of issuing a second, unlocked read.
_UNREAD = object()

# The stored average is rounded to two decimals, matching how a
# reputation figure is presented ("4.5/5"). Rounding the stored value
# rather than only the displayed one is a deliberate consequence of the
# incremental mean: the running total is reconstructed from the stored
# average, so successive folds inherit at most a two-decimal rounding
# error. Recomputing exactly would require querying every rating, which
# a Firestore transaction cannot do.
AGGREGATE_PRECISION = 2

# Provider faults that mean "this did not happen, try again" rather than
# "this cannot happen". Only these are treated as deferrable by the
# opportunistic publication paths; anything else is either a data
# invariant failure that needs an operator or a programming error that
# needs a developer, and conflating the three is how a permanent fault
# hides behind a log line that reads like routine deferred work.
#
# ABORTED is the lock-contention verdict, RetryError is the client
# giving up after its own retries, and the rest are the standard
# transient gRPC conditions.
TRANSIENT_PROVIDER_ERRORS = (
    Aborted,
    Cancelled,
    DeadlineExceeded,
    InternalServerError,
    ResourceExhausted,
    RetryError,
    ServiceUnavailable,
)


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


class PublicationInvariantError(Exception):
    """A rating cannot be published because its record is not provable.

    INTERNAL, and deliberately NOT a :class:`RatingError`: it is never
    the answer to a request. Publication is a background transition
    that happens to be triggered by requests, so every call site handles
    this itself and no router ever sees it.

    Raised from inside a publication transaction, which is what makes it
    safe: Firestore rolls the transaction back, so a rating whose
    authorization facts cannot be re-derived from its transaction, or
    whose counterparty has no user document to credit, stays exactly as
    it was - unpublished, contributing nothing, and available to be
    published later once the data is repaired. The alternative, marking
    it published and skipping the aggregate, is unrecoverable: the
    idempotency check would then refuse to revisit it, so the score
    would be permanently visible and permanently uncounted.
    """


# The only two failure classes an opportunistic publication may absorb.
# Everything else - a TypeError, an AttributeError, a mistake in this
# module - is a defect, and a defect that is logged as deferred work is a
# defect nobody ever fixes, so those are left to propagate.
DEFERRABLE_PUBLICATION_ERRORS = (
    TRANSIENT_PROVIDER_ERRORS + (PublicationInvariantError,)
)


def _log_publication_failure(subject: str, error: BaseException) -> None:
    """Record why a publication did not happen, at a matching severity.

    Deferred work and permanently blocked work read identically in a log
    unless they are written differently, and that is what lets a stalled
    reputation go unnoticed. A transient datastore fault will clear by
    itself, so it is a warning; a record that cannot be proved against
    its transaction will never publish until somebody repairs the data,
    so it is an error naming what is wrong.

    In both cases the durable retry state is the rating document itself,
    which stays unpublished and is revisited by the next read or sweep -
    no separate queue is needed to remember the work.

    Args:
        subject: Rating document ID, or a description of what was being
            published, for the log line.
        error: The absorbed failure.
    """
    if isinstance(error, PublicationInvariantError):
        logger.error(
            'Publication blocked for %s - the record cannot be proved '
            'against its transaction and needs repair: %s',
            subject,
            error,
        )
        return
    logger.warning(
        'Deferring publication for %s after a transient datastore '
        'fault: %s',
        subject,
        error,
    )


class TransactionNotRatable(RatingError):
    """The transaction document does not satisfy its own contract.

    Raised when a transaction that is otherwise eligible - it exists,
    names the caller as a participant and is completed - is missing data
    the rating record requires, specifically the
    ``vehicle_listing_id`` that every ``Transaction`` declares and that
    a rating denormalises so it can be displayed with context.

    The alternative would be to invent a value, and a rating carrying an
    empty listing reference is worse than no rating at all: it satisfies
    the model, persists silently, and can never be traced back to what
    was actually bought. Nothing is written, and the eligibility
    endpoint reports the same reason so the interface can explain it.

    Router maps this to 422 - the request is well formed but the data it
    cites cannot support the operation.
    """

    message = (
        'This transaction is missing information required to rate it'
    )


def _rating_document_id(transaction_id: str, rater_id: str) -> str:
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
    transaction: Any = _UNREAD,
    is_verified: Any = _UNREAD,
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
    6. The transaction carries the data a rating record requires - else
       :class:`TransactionNotRatable`. LAST, because "wait until the
       transaction completes" is something the caller can act on
       whereas a malformed document is not, so the actionable failure
       is reported first when both apply.

    Uniqueness is NOT assessed here. It is enforced by the datastore at
    the moment of the create-only write, because an existence read
    followed by a write would race.

    Nothing in this function writes.

    Args:
        transaction_id: Transaction the rating would be attached to.
        caller: Authenticated user, as resolved by the router.
        supplied_ratee_id: A counterparty the client claimed, if any.
        transaction: The transaction document body, when the caller
            already holds a copy read under a Firestore lock. Defaults
            to the ``_UNREAD`` sentinel, which makes this function issue
            its own get-by-ID. ``None`` means "read, and absent".
        is_verified: The rater's verification flag, when the caller has
            re-read it under a lock. Defaults to the ``_UNREAD``
            sentinel, which falls back to the value on ``caller``.
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
    if is_verified is _UNREAD:
        is_verified = getattr(caller, 'is_verified', False)
    if not is_verified:
        outcome['error'] = RaterNotVerified()
        return outcome

    # Guard 2 - the transaction must exist. A single get-by-ID.
    if transaction is _UNREAD:
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

    # Guard 5 - a rating attests to a completed exchange. Compared
    # case-insensitively so a differently-cased document still matches.
    status = transaction.get('status')
    status_text = str(status).strip().lower() if status else ''
    if status_text != COMPLETED_STATUS:
        outcome['error'] = TransactionNotCompleted()
        return outcome

    # Guard 6 - the transaction must actually carry what a rating
    # record needs. ``vehicle_listing_id`` is required on every
    # Transaction and is denormalised onto the rating so it can be
    # rendered with context; a document without one cannot supply it,
    # and substituting an empty string would persist a rating that
    # references nothing while satisfying the model. Refuse instead.
    listing_id = transaction.get('vehicle_listing_id')
    if not isinstance(listing_id, str) or not listing_id.strip():
        outcome['error'] = TransactionNotRatable()
        return outcome
    outcome['vehicle_listing_id'] = listing_id.strip()

    return outcome


def _page_size(limit: Any, default: int = DEFAULT_RATINGS_PAGE_SIZE) -> int:
    """Clamp a caller-supplied page size into a serviceable range.

    A lower bound alone is not a bound: ``limit=100000`` would be passed
    straight to Firestore and turn a public read into a collection scan.
    Both ends are therefore closed, and a value that is not a whole
    number at all falls back to the default rather than raising - a
    malformed page size is a request-shaping mistake, not a reason to
    refuse to serve the read.

    Args:
        limit: Candidate page size, of any type.
        default: Value to use when ``limit`` is not interpretable.

    Returns:
        An integer in ``[1, MAX_RATINGS_PAGE_SIZE]``.
    """
    try:
        requested = int(limit)
    except (TypeError, ValueError):
        requested = default
    if requested < 1:
        requested = 1
    return min(requested, MAX_RATINGS_PAGE_SIZE)


def _is_valid_document_id(value: Any) -> bool:
    """Report whether a value may be used as a Firestore document ID.

    The service-layer half of the same rule ``app.schema.rating``'s
    ``DocumentId`` type enforces on the request body. It is needed
    separately because two entry points here receive an ID as a bare
    argument rather than through a validated model: the eligibility
    check and the per-transaction listing are both handed a raw path
    parameter by the router.

    Args:
        value: Candidate identifier, of any type.

    Returns:
        ``True`` for a non-empty string within the length bound that
        contains no forward slash and no ASCII control character, is
        neither ``"."`` nor ``".."``, and does not fall inside the
        ``__*__`` namespace Firestore reserves.
    """
    if not isinstance(value, str) or not value:
        return False
    if len(value) > DOCUMENT_ID_MAX_LENGTH:
        return False
    return bool(_DOCUMENT_ID_RE.match(value))


def _is_valid_score(value: Any) -> bool:
    """Report whether a stored score is a usable vote.

    A genuine integer inside the configured bound, and nothing else.
    ``bool`` is excluded explicitly because it is a subclass of ``int``
    in Python, so ``True`` would otherwise pass as the score 1.

    Args:
        value: Candidate score, as stored.

    Returns:
        ``True`` when the value can be counted.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return False
    return settings.RATING_MIN <= value <= settings.RATING_MAX


def _coerce_aggregate(
    raw_average: Any,
    raw_count: Any,
    subject: str,
) -> Tuple[Optional[float], int]:
    """Return a stored aggregate pair only if it can possibly be true.

    The single gate every reader and every fold passes through, because
    the two fields are only meaningful together. Validating them
    independently lets impossible pairs through - a count of five beside
    a null average, a fractional count, an average of NaN or one outside
    the rating bound - and an impossible pair is not a cosmetic problem:
    it is rendered to a user as their counterparty's reputation, and
    folding a new score into it propagates the impossibility forever.

    The invariant enforced here is exactly the one
    :class:`app.schema.rating.RatingAggregate` declares: a non-negative
    whole count, a finite average inside ``RATING_MIN..RATING_MAX``, and
    ``count == 0`` if and only if ``average is None``.

    A pair that fails is QUARANTINED rather than repaired in place:
    it is logged at error level and reported as "no reputation yet",
    which is the only honest reading of a value that cannot be trusted,
    and which lets subsequent publications rebuild a correct aggregate
    from real scores instead of compounding a broken one. Nothing is
    written here; recomputing a stored pair from the ratings collection
    is an operator repair, and a Firestore transaction cannot query.

    Args:
        raw_average: ``rating_average`` as stored - typically ``None``
            or a float, but any value may be present.
        raw_count: ``rating_count`` as stored. Absent on user documents
            written before these fields existed, which is why a missing
            value is a legitimate zero rather than a fault.
        subject: Identifier used in the log line when a pair is
            quarantined, so the affected document can be found.

    Returns:
        ``(average, count)``, guaranteed to satisfy the invariant.
    """
    count: Optional[int] = None
    if raw_count is None:
        count = 0
    elif isinstance(raw_count, bool):
        count = None
    elif isinstance(raw_count, int):
        count = raw_count
    elif isinstance(raw_count, float) and raw_count.is_integer():
        # Firestore has one number type, so a whole value may arrive as
        # a float. A fractional one cannot be a count of anything.
        count = int(raw_count)

    average: Optional[float] = None
    average_present = raw_average is not None
    if average_present:
        if isinstance(raw_average, bool):
            average = None
        elif isinstance(raw_average, (int, float)):
            candidate = float(raw_average)
            if candidate == candidate and abs(candidate) != float('inf'):
                average = candidate

    valid = (
        count is not None
        and count >= 0
        and (average is None) == (count == 0)
        and (
            average is None
            or settings.RATING_MIN <= average <= settings.RATING_MAX
        )
        and (average is not None or not average_present)
    )
    if not valid:
        logger.error(
            'Quarantining impossible rating aggregate for %s: '
            'average=%r count=%r. Reporting no reputation until the '
            'stored pair is recomputed.',
            subject,
            raw_average,
            raw_count,
        )
        return None, 0
    return average, int(count or 0)


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

    An incremental mean is a deliberate choice, not a forced one: the
    pinned client CAN read a query through a transaction, so recomputing
    from the user's ratings is possible. It is rejected because it cannot
    hold the SRS 200 ms budget as a rating count grows, and because
    locking a whole result set rather than one document would make
    contention and transaction reruns far more likely.

    Every published rating is counted, whatever its score. There is no
    threshold and no sentiment weighting here, and none may be added.

    The base pair is validated by :func:`_coerce_aggregate` before it is
    folded into, so an impossible stored pair cannot contaminate the
    result. Every score folded in must itself be a valid vote; a stored
    value that is not is refused rather than counted as something.

    Args:
        current_average: Stored average, or ``None`` when the user has
            no published ratings yet. Already-validated values are
            revalidated here, cheaply, because this function is the
            single arithmetic entry point.
        current_count: Stored count. Absent values are treated as zero,
            because user documents written before these fields existed
            simply lack them - which is what makes this feature
            migration-free.
        scores: Scores becoming published in this operation. May be
            empty, in which case the aggregate is returned unchanged.

    Returns:
        ``(average, count)`` after the fold, satisfying the same
        invariant :func:`_coerce_aggregate` enforces. For the very first
        rating this is ``(float(score), 1)``: the average moves from
        ``None`` to exactly the submitted score.

    Raises:
        ValueError: A score being folded in is not a valid vote, or the
            resulting average falls outside the rating bound. Either
            means the caller's own validation failed, and a reputation
            must not be written from data this function cannot vouch
            for.
    """
    base_average, base_count = _coerce_aggregate(
        current_average,
        current_count,
        'aggregate fold',
    )

    added = list(scores)
    invalid = [score for score in added if not _is_valid_score(score)]
    if invalid:
        raise ValueError(
            'Refusing to fold invalid score(s) into an aggregate: '
            '{0!r}'.format(invalid)
        )

    total_count = base_count + len(added)
    if total_count <= 0:
        # No published ratings before and none now: "no ratings yet"
        # must stay distinguishable from a genuine average of zero.
        return None, 0

    running_total = (base_average or 0.0) * base_count + sum(added)
    average = round(running_total / total_count, AGGREGATE_PRECISION)
    if not settings.RATING_MIN <= average <= settings.RATING_MAX:
        # Unreachable with a validated base and validated scores, so
        # reaching it means an assumption above has broken. Refusing is
        # the only safe response: an out-of-range average would be
        # rejected by the schema on the way out anyway, after the write.
        raise ValueError(
            'Computed rating average {0} is outside {1}..{2}'.format(
                average,
                settings.RATING_MIN,
                settings.RATING_MAX,
            )
        )
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
    read off the ``settings`` singleton on every call rather than
    copied into a module-level constant at import, which is what keeps
    this module free of an import-time failure mode.

    That per-call read is not live reconfiguration and must not be
    mistaken for it. ``app/core/config.py`` evaluates ``Settings()``
    exactly once, when it is first imported, so editing the process
    environment or the ``.env`` file afterwards changes nothing until
    the process is restarted - or until something explicitly rebuilds
    or mutates that singleton. What the per-call read does buy is that
    assigning to the already-built object, as in
    ``settings.RATING_WINDOW_DAYS = 0``, is honoured by the very next
    call; that is how a test drives the window without a restart.

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


def _body_with_document_id(snapshot: Any) -> Dict[str, Any]:
    """Read a snapshot's body with its document ID as the authority.

    ``Rating.id`` is not decoration: it IS the deterministic natural key
    ``{transaction_id}_{rater_id}``, and the uniqueness guarantee rests
    on that key being the document's own name. So the document ID always
    wins - it is assigned unconditionally rather than filled in only
    when absent, because a body-level ``id`` that disagrees is either a
    foreign write or corruption, and trusting it would hand callers an
    identifier that addresses a different document (or none).

    A disagreement is not silently normalised away either; it is logged
    with both values so the record can be repaired.

    Args:
        snapshot: Firestore document snapshot.

    Returns:
        The document body with ``id`` set to the snapshot's own ID.
    """
    body = snapshot.to_dict() or {}
    stored_id = body.get('id')
    if stored_id is not None and stored_id != snapshot.id:
        logger.warning(
            'Rating document %s carries a conflicting stored id %r; '
            'using the document id and leaving the record for repair',
            snapshot.id,
            stored_id,
        )
    body['id'] = snapshot.id
    return body


def _ratings_from_snapshots(snapshots: Iterable[Any]) -> List[Rating]:
    """Parse an iterable of Firestore snapshots into rating models.

    The document ID is the authority for ``Rating.id``; see
    :func:`_body_with_document_id`.

    Args:
        snapshots: Firestore document snapshots.

    Returns:
        Successfully parsed models, in the order supplied.
    """
    parsed: List[Rating] = []
    for snapshot in snapshots:
        rating = _rating_from_dict(_body_with_document_id(snapshot))
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
    document_id = _rating_document_id(transaction_id, rater_id)
    snapshot = (
        db.collection(RATINGS_COLLECTION).document(document_id).get()
    )
    return bool(snapshot.exists)


def _submit_transaction_body(
    transaction: firestore.Transaction,
    transaction_id: str,
    caller: User,
    document_id: str,
    score: int,
    review: Optional[str],
    supplied_ratee_id: Optional[str],
) -> Dict[str, Any]:
    """Re-authorize under the lock, then create the rating. R1 + R2.

    The enforcement point for BOTH must-requirements, and the reason it
    is here rather than before the transaction opens: every fact the
    guards depend on is re-read THROUGH ``transaction``, so the
    authorization that permits the write is the authorization that holds
    at the instant it commits. Verification revoked, or a transaction
    reopened or reassigned, between the request arriving and the write
    landing cannot slip a rating through, because Firestore's optimistic
    concurrency reruns this callable when any document it read has
    changed and the guards are then evaluated against the new state.
    Without this, both gates would be advisory rather than enforced.

    All three reads precede the single write, which Firestore requires.

    A guard failure raises out of the callable, which rolls the
    transaction back, so a refused rating leaves nothing behind.

    Uniqueness is still enforced by the datastore, not by the existence
    read: the read makes the duplicate case report the domain error
    rather than an infrastructure one, and ``transaction.create``
    remains the guarantee that holds however two concurrent submissions
    interleave - hence ``AlreadyExists`` is still translated by the
    caller. This also removes the need for an application-level retry
    with a back-off sleep on the request path: contention is answered by
    the transaction runner's own bounded retry rather than by blocking
    the caller for hundreds of milliseconds against a 200 ms budget.

    Args:
        transaction: Active Firestore transaction, supplied positionally
            by :func:`app.db.firestore.run_in_transaction`.
        transaction_id: Transaction that authorizes the rating.
        caller: Authenticated user, used for identity only; the
            authorization-bearing ``is_verified`` flag is re-read here.
        document_id: Deterministic natural key for the rating.
        score: Validated score.
        review: Validated, normalised review text, if any.
        supplied_ratee_id: A counterparty the client claimed, if any.

    Returns:
        The document body that was created.

    Raises:
        RatingError: Any guard failed against the locked state. The
            transaction is rolled back, so nothing is written.
        DuplicateRating: A rating already exists at this key.
    """
    rater_id = getattr(caller, 'id', None) or ''

    # --- reads (all of them, before any write) ---
    user_snapshot = (
        db.collection(USERS_COLLECTION)
        .document(rater_id)
        .get(transaction=transaction)
    )
    if not user_snapshot.exists:
        # The caller authenticated against a document that has since
        # gone. Reported as unverified rather than as a missing user:
        # from this module's side the only question is whether the
        # account currently carries verification, and a deleted account
        # does not.
        raise RaterNotVerified()
    user_body = user_snapshot.to_dict() or {}

    transaction_snapshot = (
        db.collection(TRANSACTIONS_COLLECTION)
        .document(transaction_id)
        .get(transaction=transaction)
    )
    transaction_body = (
        (transaction_snapshot.to_dict() or {})
        if transaction_snapshot.exists
        else None
    )

    rating_ref = db.collection(RATINGS_COLLECTION).document(document_id)
    existing = rating_ref.get(transaction=transaction)

    # --- guards, against the locked reads ---
    # _assess remains the SINGLE source of the guard logic for all three
    # callers - the eligibility report, this write, and the locked
    # re-check - so the reason a user is shown cannot drift from the
    # reason a write is refused. Note that ``is_verified`` comes from the
    # locked user document, not from the caller object the router built.
    outcome = _assess(
        transaction_id,
        caller,
        supplied_ratee_id=supplied_ratee_id,
        transaction=transaction_body,
        is_verified=bool(user_body.get('is_verified', False)),
    )
    if outcome['error'] is not None:
        raise outcome['error']

    if existing.exists:
        raise DuplicateRating()

    # --- the single write ---
    # Field names here are load-bearing beyond this module: the declared
    # composite indexes reference them by exact string, and a mismatch
    # produces an index that silently serves nothing rather than an
    # error. They mirror app/schema/rating.py one for one.
    body: Dict[str, Any] = {
        'id': document_id,
        'transaction_id': transaction_id,
        # Denormalised from the same locked transaction read, so a
        # rating can be rendered with context without a second lookup.
        # Guard 6 has already proved it is a non-empty string, which is
        # why no placeholder is substituted here: a transaction that
        # cannot supply it is refused rather than recorded incompletely.
        'vehicle_listing_id': outcome['vehicle_listing_id'],
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
    transaction.create(rating_ref, body)
    return body


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

    # A raw path parameter never passes through ``RatingCreate``, so the
    # document-ID grammar is enforced here instead. A malformed value is
    # REPORTED as an unknown transaction rather than raised: this is a
    # read-only decision endpoint, and a value that cannot name a
    # document cannot name that transaction either.
    if not _is_valid_document_id(transaction_id):
        return EligibilityDecision(
            eligible=False,
            reason=TransactionNotFound.message,
            ratee_id=None,
            direction=None,
            already_rated=False,
        )

    # Guards first. The duplicate probe is REPORTING-ONLY, so issuing it
    # before the guards spent a network round trip on requests that fail
    # for a more fundamental reason - an unverified caller, or one who is
    # not a party to the transaction - and whose ``already_rated`` value
    # nobody can act on anyway.
    outcome = _assess(transaction_id, caller)
    error = outcome['error']
    if error is not None:
        return EligibilityDecision(
            eligible=False,
            reason=error.message,
            ratee_id=outcome['ratee_id'],
            direction=outcome['direction'],
            already_rated=False,
        )

    # Reached only once participation is established, which is the first
    # point at which the answer is meaningful to the caller.
    already_rated = _rating_exists(transaction_id, caller_id)

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

    Both gates are enforced INSIDE the Firestore transaction that
    writes, against documents re-read under its lock - see
    :func:`_submit_transaction_body`. Checking before the write and
    trusting the result would leave a window in which verification is
    revoked, or the transaction is reopened or reassigned, between the
    decision and the commit; the rating would then land on authority the
    datastore no longer granted. A refused rating rolls back, so nothing
    is written.

    The aggregate contribution is deferred, so this call moves only the
    rating document. The multi-document aggregate transaction belongs at
    the publication transition, where the invariant genuinely spans
    documents, and that is where :func:`publish_if_window_elapsed` and
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
    score = getattr(payload, 'score')
    if not _is_valid_score(score):
        # Unreachable through the router, where Pydantic rejects it with
        # a 422 before this runs. Checked anyway because this is the
        # only path that writes a score, and a score that is not a
        # bounded integer must never reach an aggregate.
        raise ValueError(
            'Score must be an integer between {0} and {1}'.format(
                settings.RATING_MIN,
                settings.RATING_MAX,
            )
        )
    # Normalised again at the write boundary rather than trusted from
    # the request model, so the plain-text contract holds for every
    # caller of this function, not only for bodies Pydantic validated.
    review = as_plain_text(getattr(payload, 'review', None))
    # RatingCreate deliberately has no ratee_id. If one arrives anyway,
    # it is a claim to be checked against the derived counterparty and
    # discarded either way - never a value to use.
    supplied_ratee_id = getattr(payload, 'ratee_id', None)

    rater_id = getattr(caller, 'id', None) or ''
    document_id = _rating_document_id(transaction_id, rater_id)

    # Every guard, and the create, inside ONE transaction. The body
    # re-reads the rater, the transaction and the target rating document
    # through the lock and re-runs the full guard sequence against those
    # snapshots, so no unlocked pre-check decides whether this write is
    # allowed. Create-only semantics still make the datastore the
    # authority on duplicates: the collision IS the duplicate-vote
    # signal, and it cannot race however two submissions interleave.
    try:
        body = run_in_transaction(
            _submit_transaction_body,
            transaction_id,
            caller,
            document_id,
            score,
            review,
            supplied_ratee_id,
        )
    except AlreadyExists:
        # Reached when the collision surfaces from the commit rather
        # than from the locked existence read - the two concurrent
        # submissions raced past the read. Same verdict either way.
        raise DuplicateRating()

    # The rating is committed by this point, so a failure to REVEAL it
    # must not be reported as a failure to RECORD it: the caller would
    # retry and receive a duplicate conflict for work that actually
    # succeeded. The rating simply stays unpublished, contributing
    # nothing, and the unpublished document IS the durable retry state -
    # the reciprocal check on the next read, or the window sweep, picks
    # it up. What differs per failure class is what the operator is
    # told, because "retry later" and "this will never succeed" must not
    # look alike in the log.
    published_ids: List[str] = []
    try:
        published_ids = publish_if_reciprocal(transaction_id)
    except DEFERRABLE_PUBLICATION_ERRORS as error:
        # A transient fault or an unprovable record, reported at the
        # severity its cause deserves by the shared helper so this path
        # and the sweeps cannot describe the same condition differently.
        _log_publication_failure(document_id, error)
    except Exception:
        # Not transient and not a data invariant, so it is a defect in
        # this module. Recorded at error level with a stack trace and an
        # explicit marker so it cannot be mistaken for routine deferred
        # work, but still not raised, for the reason above.
        logger.exception(
            'BUG: unexpected failure in the deferred publication check '
            'for rating %s. The rating is recorded and unpublished.',
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

    Every entry handed here has already been PROVED under the same
    transaction's lock by :func:`_validate_locked_rating`, so this
    function does not decide anything: it writes what has been proved.
    In particular there is no "publish it anyway without the aggregate"
    branch - a rating that cannot be aggregated never reaches this
    function, because publication without its aggregate movement is
    exactly the state that cannot be repaired afterwards.

    Args:
        transaction: Active Firestore transaction.
        pending: One entry per rating to publish, each carrying
            ``rating_ref``, ``rating_id``, ``ratee_id``, ``score``,
            ``user_ref``, ``current_average`` and ``current_count``,
            all read under this transaction. Entries sharing a
            ``ratee_id`` are folded into a single aggregate write, so no
            user document is written twice in one transaction - which
            Firestore would otherwise leave holding only the last
            write's value.

    Returns:
        IDs of the ratings that were published.

    Raises:
        ValueError: An entry's score or stored aggregate fails
            :func:`_fold_scores`. Propagated so the transaction rolls
            back rather than committing half of a reveal.
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

    # Group by ratee so a transaction that publishes two ratings aimed
    # at the same user folds both scores into one write instead of
    # losing one of them.
    grouped: Dict[str, Dict[str, Any]] = {}
    for entry in pending:
        bucket = grouped.setdefault(
            entry['ratee_id'],
            {
                'user_ref': entry['user_ref'],
                'current_average': entry.get('current_average'),
                'current_count': entry.get('current_count'),
                'scores': [],
            },
        )
        bucket['scores'].append(entry['score'])

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


def _locked_transaction(
    transaction: firestore.Transaction,
    transaction_id: str,
) -> Dict[str, Any]:
    """Read the authorizing transaction document under the lock.

    Publication is a SECOND trust boundary, not a continuation of the
    first. The rating document was authorized when it was created, but by
    the time it publishes it has been sitting in the datastore, and this
    is the moment its score is folded into somebody's reputation. So the
    authorization facts are re-read from their source rather than taken
    from the rating's own copy of them.

    Args:
        transaction: Active Firestore transaction.
        transaction_id: Transaction the rating claims to belong to.

    Returns:
        The transaction document body.

    Raises:
        PublicationInvariantError: The transaction does not exist, or
            does not name two distinct participants. Without it there is
            nothing to re-derive the counterparty from, so no
            publication can be justified.
    """
    if not transaction_id:
        raise PublicationInvariantError(
            'Rating carries no transaction reference'
        )
    snapshot = (
        db.collection(TRANSACTIONS_COLLECTION)
        .document(transaction_id)
        .get(transaction=transaction)
    )
    if not snapshot.exists:
        raise PublicationInvariantError(
            'Transaction {0} no longer exists'.format(transaction_id)
        )
    body = snapshot.to_dict() or {}
    buyer_id = body.get('buyer_id')
    seller_id = body.get('seller_id')
    if not buyer_id or not seller_id or buyer_id == seller_id:
        raise PublicationInvariantError(
            'Transaction {0} does not name two distinct '
            'participants'.format(transaction_id)
        )
    return body


def _validate_locked_rating(
    document_id: str,
    body: Dict[str, Any],
    transaction_body: Dict[str, Any],
    transaction_id: str,
) -> str:
    """Re-prove one rating against its transaction, and return the ratee.

    The authorization check that makes publication safe, and the reason
    the target user document is never chosen from the rating's own
    ``ratee_id`` alone: a stored ``ratee_id`` is data, and data can be
    wrong or tampered with. Trusting it would let a single altered field
    redirect a score into any user's reputation. Every fact is therefore
    re-derived here from the locked transaction and compared:

    * the document ID is the deterministic natural key for this
      transaction and this rater, so the record is where it claims to be;
    * the rater is one of the transaction's two participants;
    * the ratee is the OTHER participant, computed rather than read;
    * the direction matches the one that derivation implies;
    * the score is a bounded integer, so the aggregate cannot be moved
      by a value that was never a valid vote.

    Args:
        document_id: Rating document ID, as locked.
        body: Rating document body, as locked.
        transaction_body: The authorizing transaction, as locked.
        transaction_id: ID of that transaction.

    Returns:
        The re-derived ``ratee_id``, which is what the caller must credit.

    Raises:
        PublicationInvariantError: Any fact fails to re-prove.
    """
    rater_id = body.get('rater_id')
    if not rater_id:
        raise PublicationInvariantError(
            'Rating {0} names no rater'.format(document_id)
        )
    if document_id != _rating_document_id(transaction_id, rater_id):
        raise PublicationInvariantError(
            'Rating {0} is not the deterministic key for transaction '
            '{1} and rater {2}'.format(
                document_id, transaction_id, rater_id
            )
        )

    derived_ratee, derived_direction = _derive_counterparty(
        transaction_body,
        rater_id,
    )
    if not derived_ratee:
        raise PublicationInvariantError(
            'Rating {0} names a rater who is not a party to transaction '
            '{1}'.format(document_id, transaction_id)
        )
    if body.get('ratee_id') != derived_ratee:
        raise PublicationInvariantError(
            'Rating {0} stores ratee {1!r} but transaction {2} makes the '
            'counterparty {3!r}'.format(
                document_id,
                body.get('ratee_id'),
                transaction_id,
                derived_ratee,
            )
        )
    if body.get('direction') != derived_direction:
        raise PublicationInvariantError(
            'Rating {0} stores direction {1!r} but its participants '
            'imply {2!r}'.format(
                document_id,
                body.get('direction'),
                derived_direction,
            )
        )
    if not _is_valid_score(body.get('score')):
        raise PublicationInvariantError(
            'Rating {0} stores score {1!r}, which is not a vote between '
            '{2} and {3}'.format(
                document_id,
                body.get('score'),
                settings.RATING_MIN,
                settings.RATING_MAX,
            )
        )
    return derived_ratee


def _locked_user_state(
    transaction: firestore.Transaction,
    user_id: str,
) -> Dict[str, Any]:
    """Read the ratee's aggregate under the lock, requiring the document.

    A missing user document is an invariant failure rather than a case
    to work around. Publishing without crediting anybody would leave the
    score permanently visible and permanently uncounted, and the
    idempotency check would then refuse to revisit it - so there would be
    no way back. Refusing keeps the rating unpublished and repairable.

    Args:
        transaction: Active Firestore transaction.
        user_id: The rated user.

    Returns:
        ``user_ref``, ``current_average`` and ``current_count``, the last
        two already validated as a possible pair.

    Raises:
        PublicationInvariantError: No such user document.
    """
    user_ref = db.collection(USERS_COLLECTION).document(user_id)
    snapshot = user_ref.get(transaction=transaction)
    if not snapshot.exists:
        raise PublicationInvariantError(
            'Ratee {0} has no user document to credit'.format(user_id)
        )
    user_body = snapshot.to_dict() or {}
    # Absent on any user document written before these fields existed.
    # Treated as "no reputation yet", which is the correct reading and
    # why no backfill is required.
    average, count = _coerce_aggregate(
        user_body.get('rating_average'),
        user_body.get('rating_count', 0),
        'users/{0}'.format(user_id),
    )
    return {
        'user_ref': user_ref,
        'current_average': average,
        'current_count': count,
    }


def _locked_rating(
    transaction: firestore.Transaction,
    document_id: str,
) -> Optional[Dict[str, Any]]:
    """Read one rating under the lock, or report it as unpublishable.

    Args:
        transaction: Active Firestore transaction.
        document_id: Rating document ID.

    Returns:
        ``(ref, body)`` as a dict, or ``None`` when the document does not
        exist or is already published - both of which mean "nothing to do
        here" rather than "something is wrong". Re-reading publication
        state under the lock is what makes every publication path
        idempotent, so no score can be counted twice.
    """
    rating_ref = db.collection(RATINGS_COLLECTION).document(document_id)
    snapshot = rating_ref.get(transaction=transaction)
    if not snapshot.exists:
        return None
    body = snapshot.to_dict() or {}
    if body.get('is_published'):
        return None
    return {'rating_ref': rating_ref, 'body': body}


def _reciprocal_publication_body(
    transaction: firestore.Transaction,
    transaction_id: str,
) -> List[str]:
    """Prove reciprocity under lock, then reveal both sides at once.

    Kept as a top-level function rather than a closure because
    Firestore's optimistic concurrency reruns the callable on contention,
    so it must be free of state that would not survive a second run.

    The whole decision happens here, inside the transaction. Anything
    established outside it - by the query that noticed two ratings
    existed, say - is treated as a hint that may already be stale, never
    as grounds to write: a rating could have been deleted or altered
    between that query and this lock, and publishing one side alone is
    precisely the state the double-blind model exists to prevent. So the
    two expected documents are derived from the locked transaction's own
    participants and re-read here, and both must be present and provable
    before either is touched.

    Reads strictly precede writes, as Firestore requires. Only
    get-by-ID reads are used here - not because a query could not be
    locked, but because naming the two expected documents exactly keeps
    the conflict footprint at two documents.

    Args:
        transaction: Active Firestore transaction, supplied positionally
            by :func:`app.db.firestore.run_in_transaction`.
        transaction_id: Transaction whose two sides are being revealed.

    Returns:
        IDs of the ratings published by this transaction; empty when the
        counterparty has not rated, or when both sides were already
        published.

    Raises:
        PublicationInvariantError: A present rating cannot be re-proved
            against the locked transaction, or a ratee has no user
            document. The transaction rolls back, so nothing publishes.
    """
    transaction_body = _locked_transaction(transaction, transaction_id)
    expected = [
        _rating_document_id(transaction_id, transaction_body['buyer_id']),
        _rating_document_id(transaction_id, transaction_body['seller_id']),
    ]

    # Read phase 1 - both sides of the reveal must exist. A rating that
    # is already published is not "missing": it still proves its side
    # arrived, which is what the reveal is conditioned on.
    locked: Dict[str, Optional[Dict[str, Any]]] = {}
    for document_id in expected:
        rating_ref = db.collection(RATINGS_COLLECTION).document(
            document_id
        )
        snapshot = rating_ref.get(transaction=transaction)
        if not snapshot.exists:
            # The counterparty has not rated yet - the ordinary case,
            # and not an error.
            return []
        body = snapshot.to_dict() or {}
        _validate_locked_rating(
            document_id,
            body,
            transaction_body,
            transaction_id,
        )
        locked[document_id] = (
            None if body.get('is_published')
            else {'rating_ref': rating_ref, 'body': body}
        )

    outstanding = [
        (document_id, state)
        for document_id, state in locked.items()
        if state is not None
    ]
    if not outstanding:
        return []

    # Read phase 2 - one get-by-ID per distinct counterparty being
    # credited, still strictly before any write.
    user_state: Dict[str, Dict[str, Any]] = {}
    pending: List[Dict[str, Any]] = []
    for document_id, state in outstanding:
        body = state['body']
        ratee_id = body['ratee_id']
        if ratee_id not in user_state:
            user_state[ratee_id] = _locked_user_state(
                transaction,
                ratee_id,
            )
        entry = {
            'rating_ref': state['rating_ref'],
            'rating_id': document_id,
            'ratee_id': ratee_id,
            'score': body['score'],
        }
        entry.update(user_state[ratee_id])
        pending.append(entry)

    return _apply_publication(transaction, pending)


def _window_publication_body(
    transaction: firestore.Transaction,
    document_id: str,
) -> List[str]:
    """Publish one unreciprocated rating under lock, once it is due.

    Kept as a top-level function for the same rerun-safety reason as
    :func:`_reciprocal_publication_body`.

    The window check outside this function is an optimisation; the
    authoritative one is here, against the locked document, together
    with the same re-derivation of every authorization fact from the
    locked transaction. A rating publishing on window expiry gets no
    weaker a proof than one publishing on reciprocity.

    Args:
        transaction: Active Firestore transaction, supplied positionally
            by :func:`app.db.firestore.run_in_transaction`.
        document_id: Rating to consider.

    Returns:
        ``[document_id]`` when it published, otherwise an empty list.

    Raises:
        PublicationInvariantError: The rating cannot be re-proved against
            its transaction, or its ratee has no user document. The
            transaction rolls back, so nothing publishes.
    """
    state = _locked_rating(transaction, document_id)
    if state is None:
        return []
    body = state['body']
    if not _window_elapsed(body.get('created_at')):
        return []

    transaction_id = body.get('transaction_id')
    transaction_body = _locked_transaction(transaction, transaction_id)
    ratee_id = _validate_locked_rating(
        document_id,
        body,
        transaction_body,
        transaction_id,
    )
    entry = {
        'rating_ref': state['rating_ref'],
        'rating_id': document_id,
        'ratee_id': ratee_id,
        'score': body['score'],
    }
    entry.update(_locked_user_state(transaction, ratee_id))
    return _apply_publication(transaction, [entry])


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


def _reciprocal_possible(snapshots: Iterable[Any]) -> bool:
    """Report the NECESSARY conditions for a reciprocal reveal.

    Two distinct raters and at least one unrevealed side. Neither is
    sufficient - sufficiency is proved under the transaction lock by
    :func:`_reciprocal_publication_body`, which re-derives both expected
    documents from the transaction itself. This exists only so a caller
    that has already read the transaction's ratings can decide whether
    opening a transaction is worth it WITHOUT reading them again.

    Args:
        snapshots: Rating snapshots for one transaction.

    Returns:
        ``True`` when a reciprocal publication could possibly apply.
    """
    raters = set()
    unpublished = False
    for snapshot in snapshots:
        body = snapshot.to_dict() or {}
        rater_id = body.get('rater_id')
        if rater_id:
            raters.add(rater_id)
        if not body.get('is_published'):
            unpublished = True
    return len(raters) >= 2 and unpublished


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

    The decision is made ENTIRELY inside that transaction, by
    :func:`_reciprocal_publication_body`. The query below is an
    optimisation and nothing more - it avoids opening a transaction for a
    transaction nobody has finished rating - and its answer may already
    be stale by the time the lock is taken, which is exactly why nothing
    is written on the strength of it.

    Safe to call repeatedly. Ratings already published are re-read under
    the transaction's lock and skipped, so no score is ever counted
    twice.

    Args:
        transaction_id: Transaction to examine.

    Returns:
        IDs of the ratings published by this call, empty when the
        counterparty has not rated yet or when both sides were already
        published.

    Raises:
        PublicationInvariantError: A rating attached to this transaction
            cannot be re-proved under lock. Nothing is published.
    """
    if not transaction_id:
        return []

    # Cheap pre-check. Two distinct raters and at least one unpublished
    # side are necessary conditions for this call to do anything, so a
    # transaction is only opened when they hold. Sufficiency is decided
    # under the lock.
    if not _reciprocal_possible(
        _transaction_rating_snapshots(transaction_id)
    ):
        return []

    published = run_in_transaction(
        _reciprocal_publication_body,
        transaction_id,
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

    Raises:
        PublicationInvariantError: The rating cannot be re-proved
            against its transaction under lock, or its ratee has no user
            document. Nothing is published.
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
        _window_publication_body,
        document_id,
    )
    if published:
        logger.info(
            'Published rating %s on rating-window expiry',
            document_id,
        )
    return bool(published)


def _publish_expired_ratings(
    limit: int = DEFAULT_SWEEP_SCAN_LIMIT,
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

    The walk is a cursor over the unpublished ratings ordered by document
    name, so a pass covers all of them up to ``limit`` rather than
    re-reading one page. That matters because most unpublished ratings
    are NOT yet due: with a fixed first page, a due rating sitting behind
    a run of not-yet-due ones would never be reached, and the same
    not-due records would be re-examined on every pass forever.

    An operational note rather than a requirement: a composite index on
    ``(is_published ASC, created_at ASC)`` would let this query ask only
    for records already past their deadline, replacing the scan with a
    targeted read. It is not used here because the index set this feature
    declares does not include it, and a query that needs an undeclared
    composite index fails outright at runtime rather than degrading.

    Args:
        limit: Ceiling on the number of unpublished ratings examined in
            one pass, so no single call becomes an unbounded read. A
            larger backlog drains across successive passes.

    Returns:
        How many ratings this pass published.
    """
    query = (
        db.collection(RATINGS_COLLECTION)
        .where(
            filter=firestore.FieldFilter('is_published', '==', False)
        )
        .order_by('__name__')
    )

    published = 0
    examined = 0
    deferred = 0
    for snapshot in _paginate(
        query,
        DEFAULT_SWEEP_PAGE_SIZE,
        max(1, int(limit)),
    ):
        examined += 1
        body = _body_with_document_id(snapshot)
        try:
            if publish_if_window_elapsed(body):
                published += 1
        except DEFERRABLE_PUBLICATION_ERRORS as error:
            # One record that cannot publish must not abort the sweep,
            # but it is not silently equivalent to one that simply is not
            # due: it is counted and reported at a severity matching its
            # cause. Anything outside these two classes is a defect in
            # this module and is left to propagate.
            deferred += 1
            _log_publication_failure(snapshot.id, error)
    logger.info(
        'Rating window sweep examined %d unpublished rating(s), '
        'published %d, deferred %d',
        examined,
        published,
        deferred,
    )
    return published


def _publish_due_for_ratee(user_id: str) -> None:
    """Settle the ratings already DUE for one user, and nothing more.

    Publication cannot be left to the background worker, because there is
    none: no task in this codebase is ever dispatched and no broker is
    configured. A reputation that only became correct once a worker ran
    would simply never become correct. So a read that is about to report
    a user's reputation settles what is already due first.

    What this deliberately does NOT do is drain a backlog. It runs on the
    critical path of a public read against a 200 ms budget, so the work
    it may do is bounded twice over:

    * the query asks the datastore for DUE records only, using a range
      filter on ``created_at`` against the window deadline, so a user
      with a hundred ratings still inside their window costs ONE query
      returning nothing rather than a hundred candidate documents and a
      transaction each;
    * it takes at most ``SETTLE_BATCH_SIZE`` of them, OLDEST FIRST.

    Oldest-first ordering is what makes the small bound safe rather than
    merely cheap: each read settles the longest-overdue records, so a
    backlog larger than one page drains across successive reads instead
    of letting the same records be passed over forever.

    The reciprocal reveal is NOT attempted here. It belongs to the
    submission that completes the pair (:func:`submit_rating`) and to the
    per-transaction read (:func:`_settle_transaction`), both of
    which already know which transaction they concern. Doing it from a
    per-user read cost one extra query per unresolved transaction, which
    is the fan-out this budget cannot afford. Nothing is lost: a pair
    whose reveal failed transiently is still revealed by either of those
    two paths, and window expiry publishes it regardless.

    The query shape - two equality filters, then a range and an order on
    ``created_at`` - is served by the declared
    ``(ratee_id ASC, is_published ASC, created_at DESC)`` composite
    index: the two leading clauses are equalities, for which index
    direction is immaterial, and Firestore scans the trailing field in
    either direction. The order here is ASCENDING even though the index
    declares DESCENDING, because oldest-first is what prevents
    starvation. If a deployment ever disagrees it says so loudly rather
    than silently returning nothing, because a missing index raises
    ``FailedPrecondition`` and this module lets that propagate.

    A rating whose ``created_at`` is absent or unusable never matches the
    range filter and so is never settled here - the same "unusable
    timestamp means not yet due" reading :func:`_window_elapsed` applies,
    deferring a reveal rather than risking an early one. The global sweep
    orders by ``__name__`` instead, so such a record is still reachable
    there and cannot be stranded.

    Args:
        user_id: The rated user whose due ratings are settled.

    Raises:
        Exception: A permanent failure propagates, so a read cannot
            quietly report a stale reputation forever because an index is
            missing. Transient faults are absorbed per record.
    """
    if not _is_valid_document_id(user_id):
        return
    window_days = max(0, int(settings.RATING_WINDOW_DAYS))
    deadline = datetime.now(timezone.utc) - timedelta(days=window_days)
    query = (
        db.collection(RATINGS_COLLECTION)
        .where(filter=firestore.FieldFilter('ratee_id', '==', user_id))
        .where(
            filter=firestore.FieldFilter('is_published', '==', False)
        )
        .where(
            filter=firestore.FieldFilter('created_at', '<=', deadline)
        )
        .order_by('created_at')
        .limit(SETTLE_BATCH_SIZE)
    )
    for snapshot in query.stream():
        body = _body_with_document_id(snapshot)
        try:
            publish_if_window_elapsed(body)
        except DEFERRABLE_PUBLICATION_ERRORS as error:
            # One record that cannot publish must not stop the read from
            # answering, but it is reported at the severity its cause
            # deserves. Anything outside these classes propagates.
            _log_publication_failure(snapshot.id, error)


def _settle_transaction(transaction_id: str) -> Tuple[List[Any], bool]:
    """Settle one transaction's publications from a SINGLE read.

    The per-transaction view previously read the same bounded query three
    times to serve one request: once for the reciprocal pre-check, once
    for the window pass, and once more to serialise the result. This
    reads it once and reports whether anything actually changed, so the
    caller re-reads only when it must.

    The reciprocal reveal is attempted only when the necessary conditions
    hold on the snapshots already in hand, so the transaction - and the
    extra read inside :func:`publish_if_reciprocal` - is opened only when
    there is genuinely a pair to reveal. Sufficiency is still proved
    under the lock, which is where it has to be proved.

    Args:
        transaction_id: Transaction whose ratings are settled.

    Returns:
        ``(snapshots, changed)`` - the snapshots as they were read, and
        whether any publication was applied. When ``changed`` is false
        the snapshots are still current and can be serialised directly.
    """
    snapshots = _transaction_rating_snapshots(transaction_id)
    changed = False

    if _reciprocal_possible(snapshots):
        try:
            if publish_if_reciprocal(transaction_id):
                changed = True
        except DEFERRABLE_PUBLICATION_ERRORS as error:
            _log_publication_failure(
                'transaction {0}'.format(transaction_id),
                error,
            )

    if not changed:
        # Window expiry only needs considering while a side is still
        # unrevealed; a reciprocal reveal has already published both.
        for snapshot in snapshots:
            body = _body_with_document_id(snapshot)
            if body.get('is_published'):
                continue
            try:
                if publish_if_window_elapsed(body):
                    changed = True
            except DEFERRABLE_PUBLICATION_ERRORS as error:
                _log_publication_failure(snapshot.id, error)

    return snapshots, changed


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

    One half of the visibility policy, and it is driven purely by
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


def _visible_projection(rating: Rating) -> Rating:
    """Project a rating into what a reader other than its author may see.

    The other half of the visibility policy, and the one that implements
    "moderation before display". The two halves of a rating are treated
    differently because they carry different risk:

    * The SCORE is shown, and counted, for every published rating
      whatever its value. A number cannot contain abuse, profanity or
      somebody's phone number, and withholding it on the strength of
      being low is precisely the review suppression the FTC rule
      forbids. So moderation state never hides a score.
    * The free-text REVIEW is user-authored content and is the only part
      a policy violation can live in, so it is withheld until moderation
      has approved it. Before that - while ``moderation_status`` is
      ``pending`` - the reader gets the rating with no review text,
      which is what stops unreviewed content, including PII, being
      published by default.

    ``moderation_reason`` is redacted too: it is an internal note
    recording why a moderator acted, written for operators rather than
    for the public, and the rating's author sees it through their own
    view instead.

    Args:
        rating: Rating as stored.

    Returns:
        The same rating when its content is approved, otherwise a copy
        with the unapproved content removed. The original is never
        mutated - these models are also the values the aggregate and the
        publication paths read.
    """
    if rating.moderation_status == ModerationStatus.APPROVED.value:
        return rating.copy(update={'moderation_reason': None})
    return rating.copy(update={'review': None, 'moderation_reason': None})


def _paginate(
    query: Any,
    page_size: int,
    scan_limit: int,
) -> Iterable[Any]:
    """Walk an ORDERED query page by page, advancing a cursor.

    Firestore's ``limit`` is applied by the datastore, before this
    process can filter anything, so a single limited page is not a way
    to read "the first N that matter" - it is a way to read the first N
    documents, some of which may be discarded here. Anything the
    discarded ones would have revealed is then invisible, and because an
    unordered limited query returns the same first page every time, a
    record beyond it is never reached at all: it starves indefinitely
    while its neighbours are re-examined on every pass.

    Cursor pagination removes both problems. Each page starts after the
    last document of the previous one, so every matching document is
    seen exactly once and the walk terminates.

    ``query`` MUST carry an ordering, because a cursor is defined
    relative to one. Ordering by ``__name__`` is the cheapest choice
    when no other order is needed: Firestore's automatic single-field
    indexes are keyed by (value, document name), so an equality-filtered
    query ordered by name is served without any composite index - and,
    unlike ordering by a data field, it cannot silently exclude
    documents that lack that field.

    Args:
        query: An ordered Firestore query, without a ``limit``.
        page_size: Documents to fetch per round trip.
        scan_limit: Hard ceiling on documents examined, so no single
            call can degrade into an unbounded read of a large
            collection.

    Yields:
        Document snapshots, in the query's order.
    """
    examined = 0
    cursor = None
    while examined < scan_limit:
        window = min(page_size, scan_limit - examined)
        page_query = query.limit(window)
        if cursor is not None:
            page_query = page_query.start_after(cursor)
        page = list(page_query.stream())
        if not page:
            return
        for snapshot in page:
            yield snapshot
        examined += len(page)
        cursor = page[-1]
        if len(page) < window:
            return


def _list_ratings_for_user(
    user_id: str,
    limit: int = DEFAULT_RATINGS_PAGE_SIZE,
) -> List[Rating]:
    """List the published ratings a user has received, newest first.

    Satisfies the reputation half of F010-3, and is the public read: no
    authentication is required of the caller, matching how listings are
    read. Only published ratings are returned, so the double-blind model
    holds on the way out as well as on the way in - an unreciprocated
    rating is invisible here until it is revealed.

    Review CONTENT is returned only once moderation has approved it, so
    unreviewed text is never displayed; the score of every published
    rating is returned regardless, because suppressing a score by
    sentiment is prohibited. See :func:`_visible_projection`.

    Any rating already due for publication is settled first, so this
    read never reports a reputation that is merely waiting for a worker
    that will not run.

    Args:
        user_id: The rated user.
        limit: Maximum number of VISIBLE ratings to return - not a
            budget the datastore may spend on records this function goes
            on to hide.

    Returns:
        Visible ratings ordered by creation time descending, each
        projected for public display. Empty when the user has none.
    """
    if not user_id:
        return []

    _publish_due_for_ratee(user_id)

    wanted = _page_size(limit)
    # Exactly the declared (ratee_id, is_published, created_at DESC)
    # composite index. The equality-only helper in app/db/firestore.py
    # cannot express the ordering, which is why the client is used
    # directly here.
    query = (
        db.collection(RATINGS_COLLECTION)
        .where(filter=firestore.FieldFilter('ratee_id', '==', user_id))
        .where(filter=firestore.FieldFilter('is_published', '==', True))
        .order_by('created_at', direction=firestore.Query.DESCENDING)
    )

    # Withheld ratings are filtered here rather than with a third
    # equality clause, because a third filtered field would need a
    # composite index that is not declared - and an undeclared index
    # does not fail loudly, it just returns nothing. Filtering after the
    # read is only safe if the read can continue: hence the cursor walk,
    # which keeps going until `wanted` VISIBLE ratings are collected.
    # Without it, a page filled with withheld records would answer "this
    # user has no reviews" while approved ones sat just beyond it.
    visible: List[Rating] = []
    pages = _paginate(
        query,
        DEFAULT_RATINGS_PAGE_SIZE,
        wanted * MAX_VISIBILITY_SCAN_FACTOR,
    )
    for snapshot in pages:
        rating = _rating_from_dict(_body_with_document_id(snapshot))
        if rating is None or _is_withheld(rating):
            continue
        visible.append(_visible_projection(rating))
        if len(visible) >= wanted:
            break
    return visible


def _get_user_aggregate(user_id: str) -> RatingAggregate:
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
        genuine average of zero. A stored pair that cannot possibly be
        true - a positive count with no average, a fractional count, a
        non-finite or out-of-range average - is quarantined by
        :func:`_coerce_aggregate` and reported the same way, rather than
        presented to a user as their counterparty's reputation.
    """
    if not user_id:
        return RatingAggregate()

    _publish_due_for_ratee(user_id)

    snapshot = db.collection(USERS_COLLECTION).document(user_id).get()
    if not snapshot.exists:
        return RatingAggregate()

    body = snapshot.to_dict() or {}
    average, count = _coerce_aggregate(
        body.get('rating_average'),
        body.get('rating_count', 0),
        'users/{0}'.format(user_id),
    )
    return RatingAggregate(average=average, count=count)


def _list_ratings_for_transaction(
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

    Two distinct rules therefore apply, and which one a rating gets
    depends only on authorship:

    * The caller's OWN rating is returned in full - unrevealed, and even
      when moderation withheld it, together with the recorded reason.
      Anything else makes a withheld review vanish without explanation,
      leaving its author unable to tell whether it was ever received.
    * Anyone else's is subject to the same policy as the public read:
      visible only once published and not withheld, with unapproved
      review content and the internal moderation note removed. See
      :func:`_visible_projection`.

    Args:
        transaction_id: Transaction whose ratings are wanted.
        caller_id: The authenticated caller, when there is one. Their own
            rating is always included in full. Omit it for an
            unprivileged view, which then contains published,
            non-withheld ratings with approved content only.

    Returns:
        Visible ratings ordered by creation time descending.
    """
    # Raw-path-parameter guard FIRST, exactly as on the eligibility
    # endpoint: a value that is not a usable document ID cannot identify
    # a transaction, and must not reach a query - or a publication
    # attempt - built from it.
    if not _is_valid_document_id(transaction_id):
        return []

    # One read serves both the settlement and the answer; a second read
    # is issued only when settlement actually published something and the
    # snapshots in hand are therefore stale.
    snapshots, changed = _settle_transaction(transaction_id)
    if changed:
        snapshots = _transaction_rating_snapshots(transaction_id)

    ratings = _ratings_from_snapshots(snapshots)

    visible: List[Rating] = []
    for rating in ratings:
        if caller_id and rating.rater_id == caller_id:
            visible.append(rating)
            continue
        if not rating.is_published:
            continue
        if _is_withheld(rating):
            continue
        visible.append(_visible_projection(rating))

    visible.sort(key=_sort_key, reverse=True)
    return visible


def _get_rating(rating_id: str) -> Optional[Rating]:
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
    return _rating_from_dict(_body_with_document_id(snapshot))


def _moderate_rating(
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
        reason: The policy basis for the transition. MANDATORY when
            withholding a rating, because a rejection with no recorded
            basis is a moderation decision nobody can audit - and an
            unauditable one is indistinguishable from suppressing an
            unflattering review, which is exactly what the FTC rule
            prohibits. Recorded as given, and cleared when omitted, so
            approving a rating does not leave a stale rejection reason
            behind.

    Returns:
        The rating in its new state, or ``None`` when no rating exists at
        that ID - which the router reports as not found.

    Raises:
        ValueError: ``status`` is not a recognised moderation state, or a
            rejection was requested without a policy reason. Raised
            before anything is written, so a rating is never withheld
            first and justified afterwards.
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

    # Validated BEFORE the document is even read, so there is no path on
    # which a rating is withheld and the justification is left for
    # later. A rejection is the one transition that removes somebody's
    # words from view; it carries its policy basis or it does not happen.
    recorded_reason = (reason or '').strip() or None
    if (
        status_value == ModerationStatus.REJECTED.value
        and recorded_reason is None
    ):
        raise ValueError(
            'Rejecting a rating requires a policy reason describing the '
            'violation. A low score is never itself a violation.'
        )

    if not rating_id:
        return None

    rating_ref = db.collection(RATINGS_COLLECTION).document(rating_id)
    snapshot = rating_ref.get()
    if not snapshot.exists:
        return None

    rating_ref.update({
        'moderation_status': status_value,
        'moderation_reason': recorded_reason,
        'updated_at': firestore.SERVER_TIMESTAMP,
    })

    body = _body_with_document_id(snapshot)
    body['moderation_status'] = status_value
    body['moderation_reason'] = recorded_reason
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
