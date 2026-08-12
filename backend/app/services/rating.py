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
   however the two interleave.

   The locked existence read in ``_submit_transaction_body`` DOES gate
   that write - it is what turns the ordinary duplicate into a clean
   ``DuplicateRating`` rather than a commit-time failure - but it is not
   what makes the rule safe. It cannot race, because it is taken under
   the very transaction that performs the create; and if two submissions
   reach the commit together anyway, the create-only collision refuses
   the second and is reported identically. The read is an optimisation
   layered over the guarantee, never a substitute for it.

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

WHERE THE TRANSACTIONS ARE
-------------------------------------------------------------------
EVERY write path in this module runs inside ``run_in_transaction``,
the insert included.

The insert is transactional for the sake of its READS rather than its
write. Under the double-blind model a rating is created unpublished and
its aggregate contribution is DEFERRED, so the write itself touches one
document, which Firestore would make atomic unaided. What needs the
transaction is that the R1 verification flag, the transaction's
participants and the target rating's existence are all read under a lock
and the create is performed against those same values. An unlocked
pre-check followed by an unguarded write would decide authorization from
state that could have changed in between - the one property
``submit_rating`` must not have.

The publication transition needs a transaction for the more familiar
reason, that its invariant spans two documents:
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
``DuplicateRating`` to 409, and ``SelfRatingNotAllowed`` to 422. Those
six ARE the client-facing vocabulary of this feature, in full, and each
carries a ``message`` fit to be shown to the caller.

The router's mapping is CLOSED over exactly those six: it catches the
tuple derived from its own status table and deliberately does not catch
the base ``RatingError``. So a subclass added here later is not quietly
answered 400 with an unreviewed message - it propagates, is logged with
a traceback and surfaces as the unclassified defect it is, until someone
maps it on purpose. Adding a failure mode therefore means editing both
files, and that is the intended cost.

Two further exceptions defined below are deliberately NOT part of that
vocabulary and are deliberately not ``RatingError`` subclasses:
``PublicationInvariantError`` and ``TransactionInvariantError``. Both
report a defect in persisted data rather than a refusal any caller
caused, so neither is mapped, neither is shown to a user, and adding a
seventh 4xx for either would widen the documented contract of an
endpoint with a state only an operator can clear.

DATA THIS MODULE REFUSES TO WORK WITH
-------------------------------------------------------------------
A transaction document that does not satisfy its own contract is not
evidence of anything, so it authorizes nothing: rather than substitute
a placeholder for a missing participant or a missing listing reference
and persist a rating that points nowhere, the guard sequence stops
(``TransactionInvariantError``). The same principle governs the publication
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
``_visible_projection`` alone:

1. Publication gates the RECORD. Nothing unpublished is visible to
   anybody but its author - that is the double-blind model.
2. Moderation decides how much of a published rating a reader gets, and
   it is driven by the CONTENT and never by the score. An ``approved``
   rating is shown in full; a ``pending`` one is shown with its review
   text withheld, which is what "moderation before display" means in
   practice; a ``rejected`` one is withheld from the reader entirely,
   because a record a moderator has ruled a policy violation must not
   keep appearing. A number cannot carry abuse or personal data, and
   withholding one for being LOW is the suppression the FTC rule
   forbids, so there is no score threshold anywhere in this module.
3. Authorship overrides both, for the author only. A rater always sees
   their own rating in full, including while it is unrevealed and
   including when its review was rejected, together with the reason -
   otherwise a withheld review would vanish with no explanation to the
   person who wrote it.

The AGGREGATE is deliberately not governed by rule 2. It counts every
published rating whatever its score and whatever its moderation state,
because moving a score on a content decision is exactly what would make
moderation sentiment-relevant. So ``rating_count`` can exceed the number
of records a reader is shown. That is the contract, stated on the
response model rather than reconciled by weakening either half - and the
alternative, keeping a withheld record on the page as a bare score, was
tried and rejected: a reader cannot tell a stripped review from a rating
whose author simply wrote nothing.

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
from typing import (
    Any,
    Dict,
    Iterable,
    List,
    NamedTuple,
    Optional,
    Tuple,
)

from google.api_core.exceptions import (
    Aborted,
    AlreadyExists,
    Cancelled,
    DeadlineExceeded,
    InternalServerError,
    NotFound,
    ResourceExhausted,
    RetryError,
    ServiceUnavailable,
)
from google.cloud import firestore
from pydantic import ValidationError

from app.core.config import settings
from app.db.firestore import (
    create_document_with_id,
    db,
    run_in_read_only_transaction,
    run_in_transaction,
)
from app.schema.rating import (
    MODERATION_REASON_MAX_LENGTH,
    RATEE_ID_CLAIM,
    RATING_ID_MAX_LENGTH,
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

# Firestore's name for a document's own ID, usable as an ordering field.
# Ordering by it is how a query that needs no data ordering still gets a
# TOTAL order - which a cursor requires - without a composite index and
# without excluding documents that lack some data field.
DOCUMENT_ID_FIELD = '__name__'

# The only transaction state that authorizes a rating. This is the
# literal value app/api/transactions.py writes, compared
# case-insensitively so a differently-cased document still matches.
COMPLETED_STATUS = 'completed'

# Page sizes for the collection reads. Both are bounded so no read path
# can degrade into an unbounded scan as the collection grows.
#
# This module issues exactly TWO shapes that need a composite index, and
# each is served by one of the TWO indexes declared in
# infrastructure/firestore.indexes.json. Nothing here may introduce a
# third without declaring the index for it, because a query needing an
# undeclared composite index fails outright at runtime rather than
# degrading:
#
#   ratings received by one user, and the due subset of them
#     -> (ratee_id ASC, is_published ASC, created_at DESC)
#   ratings belonging to one transaction, ordered by rater
#     -> (transaction_id ASC, rater_id ASC)
#
# A query using a prefix of an index's fields for equality is served by
# that same index, which is why the first index serves both the
# published listing and the due-settlement read.
#
# The third shape this module reads - the global sweep for ratings whose
# window has closed - needs NO composite index, and that is a deliberate
# design constraint rather than an accident. It filters on
# ``is_published`` alone and walks the automatic single-field index by
# document name, deciding due-ness in Python. Adding the deadline to the
# query would read better and cost an index: an equality filter combined
# with a range on a different field requires a composite index, and this
# project declares two. See :func:`_publish_expired_ratings`.
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
#
# Declared here, beside the query it bounds, rather than exposed as a
# setting. The number is only legible next to the walk that spends it:
# it trades how much of a backlog one pass clears against the memory and
# time that pass costs, and both sides of that trade are properties of
# this code rather than of a deployment. An operator who needs a
# different value needs to see the walk to choose one.
DEFAULT_SWEEP_SCAN_LIMIT = 5000

# Ceiling on documents the per-user settlement on a READ path may
# examine in one pass. Much smaller than the sweep's, because this one
# runs on the critical path of a public read against the SRS 200 ms
# budget: it is large enough to walk the whole due set for any realistic
# user in one pass - which is what stops the oldest due record starving
# behind newer ones - and small enough that a pathological backlog
# cannot turn one read into an unbounded scan. The remainder drains
# across successive reads, and the scheduled sweep clears it wholesale.
SETTLE_SCAN_LIMIT = 100

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

# Sentinel meaning "the transaction has NOT been read yet and _assess must
# not read it": stop after every guard the caller already has evidence for.
#
# Distinct from ``_UNREAD``, which means the opposite - "read it yourself" -
# and from ``None``, which means "read, and absent". Three states, three
# markers, because collapsing any pair changes a verdict:
# ``_UNREAD`` would make _assess issue an unlocked read from inside a
# transaction, and ``None`` would report TransactionNotFound for a
# transaction nobody has looked for yet.
#
# This exists so ``_submit_transaction_body`` can interleave its locked
# reads with the guards those reads enable while _assess remains the ONE
# place any guard is expressed. See that function for why the interleaving
# is what keeps the published error order independent of read failures.
_DEFERRED = object()

# Sentinel distinguishing "the request body carried no counterparty
# claim" from "it carried one whose value happens to be falsy". This
# needs a sentinel of its own for the same reason ``_UNREAD`` does, but
# the stakes are higher: ``None``, ``''``, ``0`` and ``{}`` are all
# things a client can actually put in a ``ratee_id`` field, and every one
# of them is a claim that disagrees with the derived counterparty. Using
# a falsy value as the "absent" marker would make each of them skip the
# comparison entirely and be answered 201, so a caller asserting a
# relationship the transaction does not establish would have their rating
# silently redirected to the real counterparty instead of refused.
#
# ``RatingCreate`` reports presence through ``__fields_set__``, which
# records an extra key whatever its value; ``submit_rating`` translates
# that into this sentinel.
_NO_CLAIM = object()

# The stored average is rounded to two decimals, matching how a
# reputation figure is presented ("4.5/5"). Rounding the stored value
# rather than only the displayed one is a deliberate consequence of the
# incremental mean: the running total is reconstructed from the stored
# average, so successive folds inherit at most a two-decimal rounding
# error. Recomputing exactly would require querying every rating, which
# a Firestore transaction cannot do.
AGGREGATE_PRECISION = 2

# The one moderation state on which a reason may be recorded, and the one
# on which a reason is REQUIRED. Both halves of that sentence are enforced
# by :func:`_moderate_rating`, and together they are the whole
# reason/state matrix:
#
#   rejected  -> a reason is mandatory. A rejection is the transition that
#                removes somebody's words from view, so it either cites
#                the policy violation in the CONTENT that justifies it or
#                it does not happen.
#   approved  -> no reason. The review is displayed; a stored
#   pending      justification beside a displayed review is a record that
#                contradicts itself, and a stale one left over from an
#                earlier rejection is worse - it reads as a live finding.
#                Supplying one is refused, and any previously stored
#                reason is cleared by the same write that moves the state.
#
# Sentiment-neutrality does NOT come from the vocabulary the reason is
# written in; it comes from the score never entering the decision. There
# is no score threshold, no score-correlated filter and no "hide low
# ratings" path anywhere in this module, and none may be added. An
# allow-list of policy codes was tried here and removed: it widened the
# documented contract with a second field for the specifics, and it could
# not record WHICH phrase or WHOSE phone number was at issue - which is
# what a moderator's decision actually turns on, and what an operator
# reviewing that decision needs to see. The FTC Rule on the Use of
# Consumer Reviews and Testimonials (16 CFR Part 465) prohibits
# suppressing reviews on the basis of rating or negative sentiment, and it
# is the transition rule above - plus the aggregate counting every
# published rating whatever its value - that holds that line.
MODERATION_REASON_REQUIRED_STATUS = ModerationStatus.REJECTED.value

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


class TransactionInvariantError(Exception):
    """A transaction document does not satisfy its own contract.

    Raised when a transaction that is otherwise eligible - it exists,
    names the caller as a participant and is completed - is missing data
    the rating record requires, specifically the ``vehicle_listing_id``
    that every ``Transaction`` declares and that a rating denormalises so
    it can be displayed with context.

    The alternative would be to invent a value, and a rating carrying an
    empty listing reference is worse than no rating at all: it satisfies
    the model, persists silently, and can never be traced back to what
    was actually bought. So nothing is written - the guard sequence
    refuses, and when this is raised from inside the submission
    transaction Firestore rolls that transaction back.

    INTERNAL, and deliberately NOT a :class:`RatingError`, exactly like
    :class:`PublicationInvariantError` above. The distinction is the
    whole point of this class rather than a naming preference:

    * A ``RatingError`` is a refusal THE CALLER CAUSED and can act on -
      get verified, complete the transaction, stop rating a stranger -
      so it carries prose fit to show them and the router maps it to a
      4xx. There are exactly six of those, and they are the complete
      client-facing vocabulary of this feature.
    * This is a defect in PERSISTED DATA that no caller did anything to
      provoke and none can repair. Reporting it as a refusal would tell
      a well-behaved user their request was at fault, and would widen the
      documented failure contract of two endpoints with a state that only
      an operator can clear.

    It is therefore left unmapped by the router, which catches
    ``RatingError`` alone, so it surfaces as a 500 and is logged at the
    point it is detected with the offending transaction named. That is
    the honest signal: the request was fine, the stored data is not.

    """


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

    BOTH COMPONENTS ARE VALIDATED HERE, NOT ONLY UPSTREAM
    ---------------------------------------------------------------
    The value this returns is used as a Firestore document ID, so a
    component carrying a forward slash would not produce a badly named
    rating - it would produce a NESTED PATH, and the create would land
    somewhere other than where every reader looks for it. An oversized
    component produces a key that cannot be moderated later, because the
    moderation endpoint holds its path parameter to the composite bound.
    Both components have already been proved by the time any caller
    reaches this in the current code - the transaction ID by
    ``RatingCreate`` or by the guard sequence, the rater ID by
    ``User.id`` and by guard 1 - so this is a backstop rather than the
    gate. It is here because the consequence of composing an unchecked
    key is silent and permanent, and a backstop over that is cheap.

    Args:
        transaction_id: Transaction that authorizes the rating.
        rater_id: ID of the authenticated user submitting it.

    Returns:
        ``"{transaction_id}_{rater_id}"``.

    Raises:
        ValueError: Either component cannot be used as a Firestore
            document ID. Unreachable through the guarded paths, and
            deliberately loud rather than defensive-and-silent: nothing
            here can decide what a caller should be told instead.
    """
    for label, component in (
        ('transaction_id', transaction_id),
        ('rater_id', rater_id),
    ):
        if not _is_valid_document_id(component):
            raise ValueError(
                'Cannot compose a rating key from {0}={1!r}: it is not a '
                'usable Firestore document ID.'.format(label, component)
            )
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
        The document body, or ``None`` when no such document exists or the
        ID cannot name one. A value that cannot name a document is
        reported as absence rather than raised, because that is exactly
        what it means: no document lives at an unusable key, and the guard
        sequence turns absence into ``TransactionNotFound`` in the
        contract's own order.
    """
    # Grammar applied before the value reaches ``document()``: Firestore
    # reads a forward slash as a path separator, so an unvalidated value
    # can address a document other than the one it appears to name.
    if not _is_valid_document_id(transaction_id):
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
    supplied_ratee_id: Any = _NO_CLAIM,
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

    1. ``caller.is_verified`` is literally ``True`` - else
       :class:`RaterNotVerified`. FIRST, before any read, so an
       unverified caller who also happens not to be a participant learns
       about verification rather than about participation.
    2. The transaction exists - else :class:`TransactionNotFound`.
    3. The caller is the buyer or the seller - else
       :class:`NotATransactionParticipant`.
    4. The derived counterparty agrees with any supplied claim - else
       :class:`NotATransactionParticipant`; and differs from the caller
       - else :class:`SelfRatingNotAllowed`.
    5. The transaction is ``completed`` - else
       :class:`TransactionNotCompleted`.
    6. The transaction carries the data a rating record requires - else
       :class:`TransactionInvariantError`, which is RAISED rather than
       returned, because it reports a defect in stored data rather than
       a refusal the caller caused. LAST, because "wait until the
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
        supplied_ratee_id: A counterparty the client claimed. Defaults
            to the ``_NO_CLAIM`` sentinel, which means the body carried
            no such key. ANY other value - including ``None``, ``''``,
            ``0`` and ``{}`` - is treated as a claim and must equal the
            derived counterparty. Checked and then discarded; never used
            as the ratee.
        transaction: The transaction document body, when the caller
            already holds a copy read under a Firestore lock. Defaults
            to the ``_UNREAD`` sentinel, which makes this function issue
            its own get-by-ID. ``None`` means "read, and absent". The
            ``_DEFERRED`` sentinel means "not read yet, and do not read
            it": the sequence stops after guard 1 and reports no error,
            so a caller holding only the user document can decide the
            verification guard before issuing its next read.
        is_verified: The rater's verification flag, when the caller has
            re-read it under a lock. Defaults to the ``_UNREAD``
            sentinel, which falls back to the value on ``caller``. Must
            be literally ``True`` to pass; any other value, of any type,
            is refused.

    Returns:
        A dict with keys ``error`` (a :class:`RatingError` instance, or
        ``None`` when every guard passed), ``ratee_id``, ``direction``
        and ``vehicle_listing_id``. The three context keys are ``None``
        on any decision that stopped before the caller was confirmed a
        participant, so a rejected caller learns nothing about the
        counterparty.

    Raises:
        TransactionInvariantError: Guard 6 - the transaction document is
            missing ``vehicle_listing_id``, which every ``Transaction``
            declares. Not a caller-facing refusal, so it is raised
            instead of being returned in ``error``; see that class.
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
    #
    # An identity that cannot name a user document is refused HERE, in the
    # verification guard, rather than later where it would be composed
    # into a rating key or passed to ``document()``. Reported as "not
    # verified" because that is what it is from this module's side: the
    # account cannot be read, so it carries no verification - the same
    # reading ``_submit_transaction_body`` already gives a caller whose
    # user document has been deleted. ``User.id`` is grammar-bound, so
    # this is unreachable through the API and is the backstop for any
    # other caller of this module.
    caller_identity = getattr(caller, 'id', None)
    if not _is_valid_document_id(caller_identity):
        outcome['error'] = RaterNotVerified()
        return outcome
    if is_verified is _UNREAD:
        is_verified = getattr(caller, 'is_verified', False)
    # Literal ``True`` and nothing else. A truthiness test would let the
    # STRING 'false' through this gate - a non-empty string is truthy -
    # and a stored document is not typed, so 'false', 'no', 0 and 1 can
    # all reach here from a hand-edited or mis-migrated user record. The
    # flag is the authorization decision for R1, so anything that is not
    # unambiguously the boolean True is refused: an unrecognisable
    # verification state is not evidence of verification.
    if is_verified is not True:
        outcome['error'] = RaterNotVerified()
        return outcome

    # A caller that has not yet read the transaction, and does not want
    # this function to read it, stops here with every guard it had
    # evidence for already decided and no error recorded. That is not a
    # pass: ``outcome['ratee_id']`` and friends stay ``None``, and the
    # caller is expected to call again once it holds the document. Used by
    # the transactional write path so its locked reads can be interleaved
    # with the guards they enable.
    if transaction is _DEFERRED:
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
    # Presence is tested against the sentinel, never by truthiness. A
    # claim of ``''``, ``None``, ``0`` or ``{}`` is still a claim, and
    # none of them equals the derived counterparty, so each is a
    # disagreement to refuse rather than an absence to ignore. Testing
    # truthiness here would answer 201 to a body asserting a
    # relationship the transaction does not establish, and silently
    # redirect the rating to the real counterparty.
    if (supplied_ratee_id is not _NO_CLAIM
            and supplied_ratee_id != ratee_id):
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
    #
    # RAISED rather than reported, which is what separates it from the
    # five guards above. Those are refusals the caller caused and can
    # act on, so they travel back in ``outcome['error']`` as prose the
    # interface shows. This is a defect in stored data that no caller
    # provoked and none can repair, so it is an internal invariant: it
    # is logged with the transaction named so an operator can find it,
    # and it propagates - out of the eligibility read, and out of the
    # submission transaction, which Firestore then rolls back, so
    # nothing is ever written against a transaction this incomplete.
    listing_id = transaction.get('vehicle_listing_id')
    if not isinstance(listing_id, str) or not listing_id.strip():
        logger.error(
            'Transaction %s is missing vehicle_listing_id, which every '
            'Transaction declares and a rating record requires. No '
            'rating can be recorded against it until the document is '
            'repaired.',
            transaction_id,
        )
        raise TransactionInvariantError(
            'Transaction {0} carries no vehicle_listing_id'.format(
                transaction_id
            )
        )
    outcome['vehicle_listing_id'] = listing_id.strip()

    return outcome


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


def _is_valid_rating_id(value: Any) -> bool:
    """Report whether a value may be used as a RATING document ID.

    Separate from :func:`_is_valid_document_id` because a rating's ID is
    not a component ID: it is the composite ``"{transaction_id}_{rater_id}"``
    that :func:`_rating_document_id` builds, so it may be as long as both
    halves plus the joining underscore.

    Applying the component bound to the composite - which is what this
    module did until now - makes a rating that genuinely exists
    unreachable. Two 128-character participant IDs are both legitimately
    creatable, ``submit_rating`` writes their 257-character key without
    complaint, and every later lookup of that key then failed this check
    and returned ``None``, which the router reports as 404. A rating that
    is visible on a profile and cannot be moderated or read individually
    is worse than one that was refused outright, because nothing about it
    looks broken.

    The grammar is otherwise identical, and the bound mirrors
    ``app.schema.rating.RATING_ID_MAX_LENGTH`` so the router's path
    validation and this check cannot disagree about which keys exist.

    Args:
        value: Candidate rating document ID, of any type.

    Returns:
        ``True`` for a non-empty string within the composite bound that
        satisfies the shared document-ID grammar.
    """
    if not isinstance(value, str) or not value:
        return False
    if len(value) > RATING_ID_MAX_LENGTH:
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


def _locked_publication_flag(document_id: str, body: Dict[str, Any]) -> bool:
    """Read a rating's publication state as a literal boolean.

    Every point that decides, under a lock, whether to publish a rating
    goes through here. A truthiness test would be wrong in the most
    damaging possible direction: a stored ``is_published`` of the STRING
    ``'false'`` is truthy, so the rating would be read as already
    published and skipped - permanently. It would stay invisible, never
    count toward its ratee's reputation, and the idempotency check that
    makes publication safe to retry would keep refusing to revisit it, so
    nothing would ever correct it.

    An absent field is a legitimate ``False``. It is what
    :class:`app.schema.rating.Rating` defaults to, and treating it as
    unpublished merely means the rating is a candidate for publication,
    which is the safe reading. Any value that is PRESENT but not a
    boolean is a corrupt record, and this raises rather than guessing:
    guessing "published" strands the rating forever, and guessing
    "unpublished" risks counting a score twice.

    Args:
        document_id: Rating document ID, for the error message.
        body: The rating document body as stored.

    Returns:
        The publication flag as a genuine ``bool``.

    Raises:
        PublicationInvariantError: The stored flag is present and is not
            a boolean.
    """
    published = body.get('is_published', False)
    if published is True or published is False:
        return published
    raise PublicationInvariantError(
        'Rating {0} stores is_published {1!r}, which is not a boolean. '
        'Refusing to infer a visibility state from it.'.format(
            document_id,
            published,
        )
    )


def _coerce_aggregate(
    raw_average: Any,
    raw_count: Any,
    subject: str,
    strict: bool = False,
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

    What happens to a pair that fails depends on what the caller is about
    to do with it, and the difference is not a nicety - it is the
    difference between a display glitch and permanent data loss.

    * READ paths (``strict=False``, the default) QUARANTINE it: the pair
      is logged at error level and reported as "no reputation yet", which
      is the only honest reading of a value that cannot be trusted. The
      stored document is left exactly as it is, so nothing is lost and an
      operator can still recompute it.
    * PUBLICATION paths (``strict=True``) ABORT. They must, because they
      do not merely read the pair - they fold a new score into it and
      WRITE THE RESULT BACK. Quarantining there would hand the fold a
      base of ``(None, 0)``, so a user whose stored pair was unusable but
      whose ratings were real would have their whole history replaced by
      a count of one. The publication would then be marked done and the
      idempotency check would refuse to revisit it, leaving no way back.
      Refusing instead keeps the rating unpublished and repairable, which
      is the same stance :func:`_locked_user_state` already takes for a
      missing user document.

    Nothing is written here on either path. Recomputing a stored pair
    from the ratings collection is an operator repair, deliberately not
    attempted inline: it would need to read every rating a user has
    received, which is unbounded work inside a request.

    Args:
        raw_average: ``rating_average`` as stored - typically ``None``
            or a float, but any value may be present.
        raw_count: ``rating_count`` as stored. Absent on user documents
            written before these fields existed, which is why a missing
            value is a legitimate zero rather than a fault.
        subject: Identifier used in the log line, or in the raised error,
            so the affected document can be found.
        strict: ``True`` for a caller that will write the folded result
            back, which must abort rather than quarantine.

    Returns:
        ``(average, count)``, guaranteed to satisfy the invariant.

    Raises:
        PublicationInvariantError: ``strict`` is set and the stored pair
            cannot be true.
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
        if strict:
            raise PublicationInvariantError(
                'Stored rating aggregate for {0} cannot be true: '
                'average={1!r} count={2!r}. Refusing to publish, because '
                'folding a new score into an unusable pair would '
                'overwrite it and destroy the ratings it '
                'represents.'.format(subject, raw_average, raw_count)
            )
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
    # STRICT: every caller of this function writes the result back, so an
    # unusable base must abort rather than silently become (None, 0) and
    # overwrite a real history. Defence in depth - the locked read that
    # supplies this pair is already strict - because this is the single
    # place the arithmetic happens, so guarding here covers any future
    # caller that does not come through that read.
    base_average, base_count = _coerce_aggregate(
        current_average,
        current_count,
        'aggregate fold',
        strict=True,
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


def _window_deadline() -> datetime:
    """The newest ``created_at`` that is already DUE for publication.

    The query-side face of :func:`_window_elapsed`, which is the
    predicate form of the same arithmetic. A rating is due when
    ``created_at + window <= now``, and therefore when ``created_at`` is
    at or before ``now - window``. Firestore can only filter on the
    stored field, so the window has to be moved to the other side of the
    comparison before it can be a range filter - and the two forms then
    have to agree, which is why both are computed here and in
    :func:`_window_elapsed` from the same per-call read of
    ``settings.RATING_WINDOW_DAYS`` rather than independently.

    Returns:
        An aware UTC datetime. A rating created at or before it is due.
    """
    window_days = max(0, int(settings.RATING_WINDOW_DAYS))
    return datetime.now(timezone.utc) - timedelta(days=window_days)


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
    # Grammar rather than emptiness, because the two values are COMPOSED
    # into a document key here: a slash in either would make the composed
    # ID a nested path and the probe would answer about a document nobody
    # can otherwise reach. An unusable key names no rating, which is the
    # same answer as "no rating exists".
    if not _is_valid_document_id(transaction_id):
        return False
    if not _is_valid_document_id(rater_id):
        return False
    document_id = _rating_document_id(transaction_id, rater_id)
    snapshot = (
        db.collection(RATINGS_COLLECTION).document(document_id).get()
    )
    return bool(snapshot.exists)


def _reported_publication_state(
    document_id: str,
    published_ids: Iterable[str],
) -> bool:
    """Report a just-created rating's TRUE visibility to its author.

    "Did this call publish it?" and "is it published?" are different
    questions, and answering the first while appearing to answer the
    second is wrong in exactly one situation - the one that matters
    here. When both counterparties submit at the same instant, one call
    performs the reveal and the other finds the work already done, so
    :func:`publish_if_reciprocal` returns nothing to it. Reading that
    empty result as "unpublished" tells the second author their rating
    is still hidden while the datastore says otherwise, and the two
    submissions - identical in every respect that matters - would be
    answered differently purely on timing.

    So the list is consulted first, because when this call did the
    publishing no read can add anything, and only otherwise is the
    stored flag read. That read costs one get-by-ID on a write path,
    which is the price of the response describing the record rather
    than describing this call's part in producing it.

    What it reports is the state at the instant it reads, which is the
    most any response can honestly claim: two exactly simultaneous
    submissions do not share an instant, so a caller whose reciprocal
    check ran before the counterparty's rating was visible has nothing
    to reveal and nothing revealed to find, and ``False`` is correct for
    it. Publication never reverses, so a ``True`` here stays true.

    NOTHING escapes this function. It runs after the rating has already
    committed, and the whole point of the deferred publication design is
    that a failure to REVEAL a rating is never reported as a failure to
    RECORD it - see :func:`submit_rating`. Raising here would undo that
    at the last step: the caller would see an error for work that
    succeeded, retry, and be answered with a duplicate conflict. Every
    failure therefore degrades to ``False``, which is the safe direction
    - it under-reports visibility for one response, and the very next
    read of the rating or of the eligibility decision reports the truth.

    Args:
        document_id: The rating's deterministic document ID.
        published_ids: IDs published by this call's reciprocal check.

    Returns:
        ``True`` when the rating is published, ``False`` when it is not
        or when its state could not be established.
    """
    if document_id in set(published_ids or ()):
        return True
    try:
        snapshot = (
            db.collection(RATINGS_COLLECTION).document(document_id).get()
        )
        if not snapshot.exists:
            # The rating was committed a moment ago, so this is either a
            # read served before the write was visible or a foreign
            # deletion. Neither is a reason to fail the response.
            return False
        return _locked_publication_flag(
            document_id,
            snapshot.to_dict() or {},
        )
    except DEFERRABLE_PUBLICATION_ERRORS as error:
        # A transient fault, or a stored flag that is not a boolean and
        # must never be guessed at. Reported at the severity its cause
        # deserves by the shared helper, exactly as the publication
        # paths report it.
        _log_publication_failure(document_id, error)
        return False
    except Exception:
        # Not transient and not a data invariant, so it is a defect in
        # this module. Recorded with a stack trace and an explicit
        # marker so it cannot pass for routine deferred work - but still
        # not raised, because the rating itself is safely recorded.
        logger.exception(
            'BUG: unexpected failure while reading the publication '
            'state of rating %s. The rating is recorded; reporting it '
            'as unpublished.',
            document_id,
        )
        return False


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
    landing cannot slip a rating through: the documents these guards read
    are locked for the life of the transaction, and if contention aborts
    the commit the client reruns this callable so the guards are
    re-evaluated against the state that actually holds. Without this,
    both gates would be advisory rather than enforced.

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

    # Reads and guards are INTERLEAVED, in the contract's own order: each
    # read is followed immediately by every guard that read enables, and
    # only then is the next read issued. All three reads still precede the
    # single write, which is what Firestore requires.
    #
    # Doing all three reads first and then evaluating the whole guard
    # sequence - which is what this function used to do - silently makes
    # the error contract depend on the datastore. The guard order is
    # published: an unverified caller must be told about verification even
    # if they are also not a participant, because that is the actionable
    # failure. But if a LATER read fails - a transient Firestore fault, a
    # lock contention verdict the runner exhausts, an unexpected provider
    # error - it raises before any guard has run, so a caller who should
    # have received the contractually prior 403 receives a 500 instead.
    # The very case the ordering exists for is the one that broke it.
    #
    # Interleaving removes that dependency: by the time a read can fail,
    # every guard that outranks it has already been decided.

    # --- read 1: the rater, for R1 ---
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

    # --- guard 1 (R1), against read 1 ---
    # _assess remains the SINGLE source of the guard logic for all three
    # callers - the eligibility report, this write, and the locked
    # re-check - so the reason a user is shown cannot drift from the
    # reason a write is refused. It is invoked twice here, once per read,
    # and each invocation evaluates only the guards whose evidence is in
    # hand: the first passes ``transaction=_UNREAD`` DEFERRED, which stops
    # the sequence at the verification decision without issuing a read of
    # its own. No comparison is duplicated by that split.
    #
    # The flag is forwarded RAW, deliberately not coerced with ``bool()``.
    # Coercing it here would defeat the gate it feeds: ``bool('false')``
    # is ``True``, so a user document whose flag had been written as the
    # string 'false' - by a hand edit, a mis-migration, or any writer
    # that did not go through the Pydantic model - would be granted the
    # verified privilege by the very call meant to check it. _assess
    # requires literal ``True``, so the raw value is what it needs to see.
    verification = _assess(
        transaction_id,
        caller,
        supplied_ratee_id=supplied_ratee_id,
        transaction=_DEFERRED,
        is_verified=user_body.get('is_verified', False),
    )
    if verification['error'] is not None:
        raise verification['error']

    # --- read 2: the transaction, for R2 and the state guards ---
    # An ID that cannot name a document is treated as ABSENCE rather than
    # passed to ``document()``, where a forward slash would be read as a
    # nested path. Absence is the honest reading - no document lives at an
    # unusable key - and it keeps the published guard order intact: the
    # caller learns about verification first and about the unknown
    # transaction second, exactly as they would for a well-formed ID that
    # names nothing.
    if _is_valid_document_id(transaction_id):
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
    else:
        transaction_body = None

    # --- guards 2 to 6 (R2, self-rating, completed, ratable) ---
    outcome = _assess(
        transaction_id,
        caller,
        supplied_ratee_id=supplied_ratee_id,
        transaction=transaction_body,
        is_verified=user_body.get('is_verified', False),
    )
    if outcome['error'] is not None:
        raise outcome['error']

    # --- read 3: the target key, for the duplicate report ---
    rating_ref = db.collection(RATINGS_COLLECTION).document(document_id)
    existing = rating_ref.get(transaction=transaction)
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
        # Written explicitly as null rather than omitted, so a new rating
        # has the same document shape as a moderated one and no consumer
        # has to distinguish "no reason" from "field absent".
        'moderation_reason': None,
        'created_at': firestore.SERVER_TIMESTAMP,
        'updated_at': firestore.SERVER_TIMESTAMP,
    }
    # Routed through the data layer's create-only primitive rather than
    # calling ``transaction.create`` directly, so exactly ONE function in
    # the codebase owns create-only writes. Reaching past it would leave
    # two implementations of the same uniqueness rule - and the data
    # layer's, having no caller, would be free to drift out of agreement
    # with the one that actually runs. Enrolling it in this transaction
    # keeps the create atomic with the aggregate write beside it, and
    # ``AlreadyExists`` still propagates, which is what makes the
    # datastore the authority on duplicate votes.
    create_document_with_id(
        RATINGS_COLLECTION,
        document_id,
        body,
        transaction=transaction,
    )
    return body


def evaluate_eligibility(
    transaction_id: str,
    caller: User,
    transaction: Any = _UNREAD,
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
        transaction: The transaction document body, when the caller
            already holds a copy. Defaults to the ``_UNREAD`` sentinel,
            which makes the guard sequence issue its own get-by-ID.
            :func:`require_eligibility` supplies it so the endpoint costs
            one read rather than two.

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
    outcome = _assess(transaction_id, caller, transaction=transaction)
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
        ``is_published`` reports the rating's STORED visibility, so it
        is ``True`` both when this call revealed the pair and when the
        counterparty's simultaneous call got there first. It degrades to
        ``False`` rather than raising if that state cannot be
        established, so a client that needs certainty should read the
        rating or its eligibility decision back.

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
    #
    # Presence is read from ``__fields_set__``, not from the value.
    # ``getattr(payload, 'ratee_id', None)`` cannot express this contract:
    # it returns ``None`` both when the key was absent and when the client
    # sent ``ratee_id: null``, and those must reach opposite verdicts -
    # absence is fine, an explicit null is a claim that disagrees with the
    # derived counterparty. ``RatingCreate`` retains the key under
    # ``extra = 'allow'`` and Pydantic records it in ``__fields_set__``
    # whatever its value, including ``''``, ``None``, ``0`` and ``{}``, so
    # that set is the only faithful presence signal available.
    if RATEE_ID_CLAIM in getattr(payload, '__fields_set__', ()):
        supplied_ratee_id = getattr(payload, RATEE_ID_CLAIM, None)
    else:
        supplied_ratee_id = _NO_CLAIM

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
    # The STORED state, not this call's contribution to it. Under an
    # exact reciprocal race the counterparty's call performs the reveal
    # and this one is handed an empty list, so the list alone would
    # report a published rating as hidden; see
    # :func:`_reported_publication_state`, which never raises.
    result['is_published'] = _reported_publication_state(
        document_id,
        published_ids,
    )
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
    # The value comes off a STORED rating document, so it is data rather
    # than input, and data can be wrong: a slash in it would make
    # ``document()`` resolve a nested path instead of the transaction, and
    # an oversized or control-bearing value forges the log line that
    # reports it. Quarantined as a publication invariant failure, which
    # leaves the rating unpublished, repairable, and named in the log.
    if not _is_valid_document_id(transaction_id):
        raise PublicationInvariantError(
            'Rating carries no usable transaction reference: {0!r}'.format(
                transaction_id
            )
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
    # A stored rater ID is data, and this one is about to be composed
    # into a document key and compared against the record's own name, so
    # it is held to the component grammar rather than merely to
    # non-emptiness. A slash in it would make the composed key a nested
    # path; an oversized one would produce a key the moderation endpoint
    # cannot accept. Either way the record is quarantined - left
    # unpublished, repairable, and named in the log - instead of
    # publishing against an identifier nothing can address.
    rater_id = body.get('rater_id')
    if not _is_valid_document_id(rater_id):
        raise PublicationInvariantError(
            'Rating {0} names no usable rater: {1!r}'.format(
                document_id, rater_id
            )
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
        PublicationInvariantError: No such user document, or a ``user_id``
            that cannot name one. The second case is quarantined rather
            than looked up, because a stored participant ID carrying a
            slash would make ``document()`` resolve a nested path and
            credit a score to something other than a user.
    """
    if not _is_valid_document_id(user_id):
        raise PublicationInvariantError(
            'Ratee {0!r} is not a usable user document ID'.format(user_id)
        )
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
    #
    # STRICT here, unlike on the read paths. This pair is about to be
    # folded and written back, so an impossible stored pair must stop the
    # publication rather than be quarantined to (None, 0): quarantining
    # would silently replace a real history with a count of one, and the
    # publication would then be recorded as done and never revisited.
    average, count = _coerce_aggregate(
        user_body.get('rating_average'),
        user_body.get('rating_count', 0),
        'users/{0}'.format(user_id),
        strict=True,
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
    if _locked_publication_flag(document_id, body):
        return None
    return {'rating_ref': rating_ref, 'body': body}


def _reciprocal_publication_body(
    transaction: firestore.Transaction,
    transaction_id: str,
) -> List[str]:
    """Prove reciprocity under lock, then reveal both sides at once.

    Kept as a top-level function rather than a closure because
    the client reruns the callable when contention aborts a commit, so it
    must be free of state that would not survive a second run.

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
            None if _locked_publication_flag(document_id, body)
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


def _window_publication_batch_body(
    transaction: firestore.Transaction,
    document_ids: List[str],
) -> List[str]:
    """Publish a BATCH of due ratings under ONE lock.

    The batched form of :func:`_window_publication_body`, and it exists
    for cost rather than convenience. Settling a page of due records one
    transaction at a time costs a transaction, at least three document
    reads and two writes PER RECORD, and a page of ten aimed at the same
    user rewrites that user's aggregate ten times over. Here the page
    costs ONE transaction: one read per rating, one per distinct
    authorizing transaction, one per distinct ratee, and
    :func:`_apply_publication` folds every score aimed at one user into a
    SINGLE aggregate write.

    Folding is not merely cheaper, it is the only correct arithmetic. Ten
    separate transactions each reading the same starting count is a
    lost-update race, and ten writes to one document inside one
    transaction would leave only the last write's value.

    Kept a top-level function for the same rerun-safety reason as
    :func:`_reciprocal_publication_body`: the client reruns the callable
    when contention aborts a commit, so it holds no state that would not
    survive a second run. Both caches below are built inside the call.

    Reads strictly precede writes, as Firestore requires - the loop only
    reads, and every write happens in :func:`_apply_publication` after
    it. A write added inside the loop would break the transaction.

    ISOLATION IS PER RECORD FOR AN INVARIANT, PER BATCH FOR A FAULT
    ---------------------------------------------------------------
    A record that cannot be re-proved against its transaction is skipped
    and logged, so one unpublishable record does not cost the other nine
    theirs. That is a decision about a PERMANENT condition: it will still
    be unpublishable next pass, and it stays unpublished and repairable
    either way.

    A transient datastore fault is deliberately NOT caught here, because
    it belongs to the transaction rather than to one record: the client
    reruns the whole callable, and if it ultimately gives up the caller
    absorbs it for the batch and every record in it is simply still due
    next pass. A ``ValueError`` from folding an impossible aggregate is
    left to propagate for the same reason - the batch rolls back instead
    of committing part of a reveal, and nothing is lost because every
    record stays exactly as it was.

    Args:
        transaction: Active Firestore transaction, supplied positionally
            by :func:`app.db.firestore.run_in_transaction`.
        document_ids: Rating document IDs to consider. The caller's query
            established them as candidates; that query is a hint, so each
            is re-checked here under the lock.

    Returns:
        IDs of the ratings this batch published, which may be fewer than
        were offered.

    Raises:
        Exception: Anything :func:`_apply_publication` raises, and any
            transient datastore fault, so the whole batch rolls back.
    """
    transaction_bodies: Dict[str, Dict[str, Any]] = {}
    user_state: Dict[str, Dict[str, Any]] = {}
    pending: List[Dict[str, Any]] = []
    for document_id in document_ids:
        try:
            state = _locked_rating(transaction, document_id)
            if state is None:
                continue
            body = state['body']
            if not _window_elapsed(body.get('created_at')):
                continue
            transaction_id = body.get('transaction_id')
            transaction_body = transaction_bodies.get(transaction_id)
            if transaction_body is None:
                transaction_body = _locked_transaction(
                    transaction,
                    transaction_id,
                )
                transaction_bodies[transaction_id] = transaction_body
            ratee_id = _validate_locked_rating(
                document_id,
                body,
                transaction_body,
                transaction_id,
            )
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
        except PublicationInvariantError as error:
            # Permanent for this record, so skipping it is a decision
            # rather than a deferral - and it is logged as such.
            _log_publication_failure(document_id, error)
            continue
        pending.append(entry)
    return _apply_publication(transaction, pending)


def _transaction_rating_snapshots(transaction_id: str) -> List[Any]:
    """Fetch every rating attached to one transaction, by rater.

    One equality filter ordered by a second field, which is exactly the
    shape the declared ``(transaction_id ASC, rater_id ASC)`` composite
    index in ``infrastructure/firestore.indexes.json`` serves - and the
    only shape that needs it. The ordering is not decoration: without it
    this query used one equality filter alone, Firestore's automatic
    single-field index answered it, and the declared composite index had
    no consumer at all. A declared index nobody queries through is
    indistinguishable from a missing one until something needs it, so the
    query and the declaration are kept in step here rather than left to
    agree by coincidence.

    Ordering by ``rater_id`` also removes the last unspecified case from
    the transaction read. That endpoint answers newest first, and
    :func:`_sort_key` keys on the timestamp ALONE, so where two ratings
    share a creation instant - which two participants submitting together
    can produce, since the stamp is server-side - the stable sort leaves
    the order of THIS query's result deciding the tie. Unordered, that
    tie broke on whatever Firestore returned; now it breaks on rater id,
    the same way on every read.

    Bounded regardless, so a corrupted collection cannot turn this into
    an unbounded read.


    Args:
        transaction_id: Transaction whose ratings are wanted.

    Returns:
        Document snapshots, ordered by rater id ascending. Empty for a
        ``transaction_id`` that cannot name a document, which no rating
        can reference.

    """
    if not _is_valid_document_id(transaction_id):
        return []
    query = (
        db.collection(RATINGS_COLLECTION)
        .where(
            filter=firestore.FieldFilter(
                'transaction_id', '==', transaction_id
            )
        )
        .order_by('rater_id')
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
        # Only a literal ``True`` counts as revealed. Anything else -
        # including a corrupt non-boolean - is treated as still
        # unrevealed, which errs toward OPENING the transaction. That is
        # the right bias for a hint: the locked path re-reads the flag
        # and is the only thing entitled to decide, and it raises on a
        # corrupt value rather than skipping it silently. Erring the
        # other way would let a record with is_published='false' - a
        # truthy string - suppress the reveal here and never be looked
        # at again.
        if body.get('is_published') is not True:
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

    Idempotent, and idempotent in exactly ONE place: the rating is re-read
    under the transaction's lock and skipped there if already published,
    so calling this twice cannot increment an aggregate twice. Publication
    state on the SUPPLIED record is ignored entirely, so a caller holding
    a stale copy cannot suppress a reveal that is due.

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
    # Publication state is deliberately NOT pre-checked from the supplied
    # record. It used to be, and that made this function's contract
    # dishonest: the docstring promises that only the ID is trusted, yet
    # a caller holding a snapshot read moments earlier - or a stale model
    # from a previous request - could return False here purely because
    # that copy said published, without the locked document ever being
    # consulted. A truthy non-boolean in the copy did the same thing. The
    # locked read in ``_window_publication_body`` is the only thing
    # entitled to decide, and it is already idempotent, so there is
    # nothing to gain by guessing ahead of it.
    #
    # ``created_at`` is still pre-checked, and that is a different case:
    # it is immutable once stamped, so a stale copy cannot say "not due"
    # about a rating that is due - only the reverse, which the locked
    # check then catches. It saves opening a transaction for the many
    # ratings that plainly are not due yet.
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
) -> List[str]:
    """Publish every unreciprocated rating whose window has closed.

    The sweep behind the deferred reveal, exposed here so the Celery task
    that schedules it stays a thin delegation and every query shape and
    every deadline calculation remains in this module.

    Correctness does not depend on that task ever running. No task in
    this codebase is dispatched, no broker is configured, and the read
    paths therefore publish opportunistically when they encounter a rating
    that is due. This function is the scheduled equivalent of the same
    work, not a prerequisite for it.

    ONE EQUALITY FILTER, AND THE DEADLINE APPLIED IN PYTHON
    ---------------------------------------------------------------
    The query asks for unpublished ratings and nothing else, walked by
    document name, with due-ness decided here by
    :func:`_window_elapsed`. Asking the datastore for "unpublished AND
    past the deadline" would read better and would cost an index this
    project does not have: an equality filter combined with a range on a
    DIFFERENT field requires a composite index, and
    infrastructure/firestore.indexes.json declares exactly two - both
    shaped for the read paths, neither able to serve
    ``(is_published, created_at)``. A query needing an undeclared
    composite index does not run slowly, it fails outright with
    ``FailedPrecondition``, so the shape that needs no declaration is the
    shape this sweep uses. Filtering on ``is_published`` alone is served
    by Firestore's automatic single-field index.

    What that costs is reading not-yet-due candidates: on a healthy
    system most unpublished ratings are inside their window, and each
    pass pays for those documents to discover they are not actionable.
    The ceiling below bounds that cost, and it is the right trade against
    a third index, because this sweep runs on a worker rather than on a
    request path - and because nothing dispatches it, the read paths
    carry the real burden with the far narrower per-user query.

    Ordering by document NAME rather than by ``created_at`` is what keeps
    the walk index-free and total. It also cannot silently exclude a
    record: Firestore drops a document that lacks an ordered field from
    the result set entirely, so ordering by a timestamp would hide a
    rating that has not been stamped, while every document has a name.

    Progress is durable without any stored cursor. Publishing a record
    removes it from this query permanently, so each pass faces a strictly
    smaller PUBLISHABLE set than the last; and because a pass walks a
    cursor to the ceiling rather than re-reading one page, a due record
    cannot starve behind a run of not-yet-due ones.

    The walk is bounded twice over - the cursor pagination of
    :func:`_paginate` caps documents examined at ``limit``, and
    :func:`_chunked` caps each transaction at ``SETTLE_BATCH_SIZE``
    records. Settling a group of N in one transaction rather than N
    transactions is not only cheaper: two ratings in the same group aimed
    at the same user are folded into a single aggregate write by
    :func:`_apply_publication`, where separate transactions would each
    read the same starting count and lose an update.

    A rating whose ``created_at`` is absent or unusable is examined and
    then declined, because :func:`_window_elapsed` reads an unusable
    timestamp as "not yet due" - deferring a reveal rather than risking
    an early one. Nothing is stranded by that: no path would publish
    such a record, because every one of them decides due-ness through
    that same function. The record needs its timestamp repaired, not
    another reader.

    Args:
        limit: Ceiling on the number of unpublished ratings examined in
            one pass, so no single call becomes an unbounded read. A
            larger backlog drains across successive passes.

    Returns:
        The IDs of the ratings this pass published, which the Celery task
        reports as its result.
    """
    query = (
        db.collection(RATINGS_COLLECTION)
        .where(
            filter=firestore.FieldFilter('is_published', '==', False)
        )
        .order_by(DOCUMENT_ID_FIELD)
    )

    published: List[str] = []
    examined = 0
    deferred = 0
    for batch in _chunked(
        _due_snapshots(
            _paginate(query, DEFAULT_SWEEP_PAGE_SIZE, max(1, int(limit)))
        ),
        SETTLE_BATCH_SIZE,
    ):
        examined += len(batch)
        document_ids = [snapshot.id for snapshot in batch]
        try:
            published.extend(
                run_in_transaction(
                    _window_publication_batch_body,
                    document_ids,
                )
            )
        except DEFERRABLE_PUBLICATION_ERRORS as error:
            # One group that cannot commit must not abort the sweep, and
            # it is not silently equivalent to a group that simply was
            # not due: it is counted and reported at a severity matching
            # its cause. Anything outside these two classes is a defect
            # in this module and is left to propagate.
            #
            # A record that cannot be re-proved never reaches here - the
            # batch body skips and logs it individually, so one such
            # record costs the rest of its group nothing. What reaches
            # here is a fault against the whole transaction, and those
            # records are simply still due on the next pass.
            deferred += len(document_ids)
            _log_publication_failure(
                'batch [{0}]'.format(', '.join(document_ids)),
                error,
            )
    logger.info(
        'Rating window sweep examined %d due rating(s), published %d, '
        'deferred %d',
        examined,
        len(published),
        deferred,
    )
    return published


def publish_expired_ratings(limit: Optional[int] = None) -> List[str]:
    """Run one bounded pass of the rating-window sweep. PUBLIC.

    The supported entry point for the scheduled sweep, so
    ``app/tasks/background_jobs.py`` can schedule the work without
    reaching into this module's internals and without owning a query
    shape, a deadline calculation or an error policy of its own. A task
    that enumerated candidates itself would be a second implementation of
    the same walk, free to drift out of agreement with the one the read
    paths use - and, being unbounded, free to load an entire collection
    into a worker.

    Everything that makes the pass safe lives behind this call:

    * the walk is a CURSOR over the unpublished ratings ordered by
      document name, so each matching document is seen once per pass and
      a due rating cannot starve behind a run of not-yet-due ones;
    * the pass is CEILINGED at ``limit`` documents examined, so a large
      backlog drains across successive passes instead of exhausting the
      worker's memory or its time budget;
    * a record that cannot publish is handled PER RECORD - a transient
      datastore fault or an unprovable record is logged at the severity
      its cause deserves and the pass continues, so one poison document
      cannot abort the sweep and strand everything behind it.

    Correctness does not depend on this ever running. No task in this
    codebase is dispatched and no broker is provisioned, so the read paths
    publish opportunistically when they meet a rating that is due; this is
    the scheduled equivalent of that work, never a prerequisite for it.

    Args:
        limit: Ceiling on documents examined in one pass. ``None`` uses
            :data:`DEFAULT_SWEEP_SCAN_LIMIT`, which is declared beside
            the walk it bounds. A value below 1 is raised to 1 rather
            than refused, because a mis-tuned ceiling is a reason to do
            less work, not a reason to stop publishing.

    Returns:
        The IDs of the ratings this pass published. Empty
        when nothing was due. IDs rather than a bare count because the
        caller that logs this pass is the only record that it happened -
        no audit collection exists - so naming the records makes a
        publication traceable to the documents it moved, and the count is
        still one ``len`` away.
    """
    ceiling = DEFAULT_SWEEP_SCAN_LIMIT if limit is None else limit
    return _publish_expired_ratings(max(1, int(ceiling)))


def _publish_due_for_ratee(user_id: str) -> bool:

    """Settle the ratings already DUE for one user, and nothing more.

    Publication cannot be left to the background worker, because there is
    none: no task in this codebase is ever dispatched and no broker is
    configured. A reputation that only became correct once a worker ran
    would simply never become correct. So a read that is about to report
    a user's reputation settles what is already due first.

    IT WALKS THE WHOLE DUE SET, NOT ONE PAGE OF IT
    ---------------------------------------------------------------
    The work is bounded by a scan ceiling rather than by a single page,
    and that distinction is a correctness one. An earlier revision took
    the ``SETTLE_BATCH_SIZE`` NEWEST due records and reversed that page in
    memory, which looks equivalent and is not: under sustained arrivals
    more than a page can become due between two reads, and the oldest
    record then sits behind a permanently replenished run of newer ones
    and is never reached. Overdue is exactly the state that must not be
    able to persist, so the walk advances a cursor over every due record
    it finds, up to :data:`SETTLE_SCAN_LIMIT` documents examined.

    Bounded three ways, because this runs on the critical path of a public
    read against the SRS 200 ms budget:

    * the query asks the datastore for DUE records only, using a range
      filter on ``created_at`` against the window deadline, so a user
      with a hundred ratings still inside their window costs ONE query
      returning nothing rather than a hundred candidate documents and a
      transaction each - and on a healthy system that is the case every
      time;
    * the cursor walk stops after :data:`SETTLE_SCAN_LIMIT` documents,
      so even a pathological backlog cannot turn one read into an
      unbounded scan; the remainder drains across successive reads and
      the scheduled sweep clears it wholesale;
    * each group of at most ``SETTLE_BATCH_SIZE`` settles inside ONE
      transaction rather than one per record. Every rating here is aimed
      at the SAME user, so a transaction each would rewrite that user's
      aggregate once per record - which is both a lost-update race across
      separate transactions and, against a 200 ms budget, ten round trips
      where one will do. :func:`_window_publication_batch_body` folds them
      into a single aggregate write and skips a record that cannot be
      re-proved without costing the rest of its group theirs.

    A backlog larger than the ceiling still drains: the query selects only
    records that are BOTH unpublished AND past their deadline, and
    settling a record removes it from that set permanently, so each read
    faces a strictly smaller candidate set than the last.

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
    index, and the ordering here is DESCENDING so it matches that
    declaration exactly rather than relying on the datastore to scan the
    trailing field backwards. That reliance is the thing worth avoiding:
    it is an assumption about index-direction semantics that the local
    emulator cannot falsify, because the emulator serves any query
    without a declared index at all. A deployment where indexes ARE
    enforced would be the first place it was tested, and a wrong answer
    there surfaces as ``FailedPrecondition`` on a public read path.
    Matching the declared direction removes the question. Ordering is a
    free choice here anyway - see the draining argument above - so there
    is no reason to spend it on an assumption.

    A rating whose ``created_at`` is absent or unusable never matches the
    range filter and so is never settled here - the same "unusable
    timestamp means not yet due" reading :func:`_window_elapsed` applies,
    deferring a reveal rather than risking an early one. The global sweep
    does examine such a record, because it filters on the deadline in
    Python rather than in its query, and then declines it for the same
    reason. Nothing is stranded either way: every path decides due-ness
    through :func:`_window_elapsed`, so the record needs its timestamp
    repaired rather than another reader.

    Args:
        user_id: The rated user whose due ratings are settled.

    Returns:
        Whether anything actually published. The caller needs this
        because the user document it has already read carries the
        aggregate: a ``False`` means that snapshot is still current and
        no second read of it is warranted, and only a ``True`` costs one.

    Raises:
        Exception: A permanent failure propagates, so a read cannot
            quietly report a stale reputation forever because an index is
            missing. A transient fault is absorbed for the batch, which
            is the granularity it occurs at - the records stay due and
            the next read settles them.
    """
    if not _is_valid_document_id(user_id):
        return False
    deadline = _window_deadline()
    query = (
        db.collection(RATINGS_COLLECTION)
        .where(filter=firestore.FieldFilter('ratee_id', '==', user_id))
        .where(
            filter=firestore.FieldFilter('is_published', '==', False)
        )
        .where(
            filter=firestore.FieldFilter('created_at', '<=', deadline)
        )
        .order_by('created_at', direction=firestore.Query.DESCENDING)
    )
    changed = False
    for batch in _chunked(
        _paginate(query, SETTLE_BATCH_SIZE, SETTLE_SCAN_LIMIT),
        SETTLE_BATCH_SIZE,
    ):
        document_ids = [snapshot.id for snapshot in batch]
        try:
            published = run_in_transaction(
                _window_publication_batch_body,
                document_ids,
            )
        except DEFERRABLE_PUBLICATION_ERRORS as error:
            # A group that cannot publish must not stop the read from
            # answering, and must not stop the walk either: the remaining
            # groups are independent, so one transient fault costs only
            # its own records, which stay due for the next read.
            #
            # An invariant failure on a SINGLE record never reaches here -
            # the batch body skips and logs it per record. What reaches
            # here is a fault affecting the whole transaction.
            _log_publication_failure(
                'ratings due for user {0}'.format(user_id),
                error,
            )
            continue
        if published:
            changed = True
    return changed


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
            # As in ``_reciprocal_possible``, only a literal ``True``
            # skips the candidate. A corrupt flag is passed through to
            # the locked path, which raises on it, rather than being
            # quietly skipped here and never revisited.
            if body.get('is_published') is True:
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


def _visible_projection(rating: Rating) -> Optional[Rating]:
    """Project a rating into what a reader other than its author may see.

    The whole of the moderation half of the visibility policy, and the
    one place "moderation before display" is implemented. Every published
    rating reaches it - none is dropped beforehand - so the case it
    governs is a rating that MIGHT be shown, and the question is whether
    and how much of it.

    Three outcomes, decided by ``moderation_status`` alone and never by
    the score:

    * ``approved`` - the rating is returned in full. Moderation has read
      the words and cleared them.
    * ``pending`` - the rating is returned with no review text. That is
      what stops unreviewed content, including PII, from being published
      by default, and it is the meaning of "moderation before display".
    * ``rejected`` - the rating is WITHHELD. A moderator found a policy
      violation in it, so it is removed from the reader's view entirely
      rather than shown with its words stripped out.

    The score is never the reason for any of this. There is deliberately
    no score threshold, no score-correlated filter and no "hide low
    ratings" path anywhere in this module, and none may be added: a
    number cannot contain abuse, profanity or somebody's phone number,
    and withholding one for being low is precisely the review suppression
    the FTC rule forbids.

    WHY WITHHOLDING THE WHOLE RECORD, AND WHAT IT COSTS
    ---------------------------------------------------------------
    A rejected rating was previously returned score-only, on the argument
    that the aggregate counts every published rating so removing the
    record would leave a profile showing fewer ratings than its average
    was computed from. That cost is real and it is accepted, because the
    alternative is worse: a record a moderator has ruled a policy
    violation would keep appearing on the page, and a reader has no way
    to tell a stripped review from a rating whose author simply wrote
    nothing.

    So the two halves of a reputation answer different questions, and
    they are documented as answering them rather than reconciled by
    weakening either:

    * ``rating_average``/``rating_count`` cover EVERY published rating,
      whatever its score and whatever its moderation state. The
      aggregate is maintained at publication and never adjusted
      afterwards, because a moderation state is a decision about CONTENT
      and letting it move a score would make moderation
      sentiment-relevant - exactly what must never happen.
    * the visible LIST covers what a reader may be shown, so a withheld
      record is absent from it.

    ``aggregate.count`` may therefore exceed the number of items
    returned. That is a property of the contract, not a discrepancy, and
    it is stated on the response model so no client renders it as one.

    ``moderation_reason`` is redacted on every path, approved or not: it
    is an internal note recording why a moderator acted, written for
    operators rather than for the public. The rating's author sees it
    through their own view instead.

    RELATIONSHIP METADATA IS DELIBERATELY RETAINED
    ---------------------------------------------------------------
    The projected rating keeps ``transaction_id``, ``rater_id``,
    ``ratee_id``, ``direction`` and ``vehicle_listing_id``. That is an
    explicit decision rather than an oversight, and it is worth stating
    because it is the kind of field set that looks like an information
    leak on inspection:

    * ``ratee_id`` and ``direction`` are the subject of the read. A
      caller asking for the ratings a user received already knows who
      that user is, and which direction along a transaction the rating
      travelled follows from their role in it.
    * ``rater_id`` is what makes a public rating attributable, which is
      the point of a reputation system: an unattributable score cannot be
      weighed for context, and the specification's own authenticity
      requirement is that a rating comes from somebody who actually
      transacted.
    * ``vehicle_listing_id`` is denormalised onto the rating precisely so
      it can be rendered with context - "rated after buying this car" -
      without a second document read. Removing it would defeat the reason
      the field exists on the model at all.
    * ``transaction_id`` is the natural key's first component and the
      evidence a rating is anchored to a real exchange.

    A partial redaction here would also be illusory rather than
    protective, which is the deciding argument: the rating's document ID
    IS ``{transaction_id}_{rater_id}``, and ``id`` is returned. Blanking
    the two fields while returning the value they compose would cost the
    interface real capability and conceal nothing. Nothing sensitive is
    exposed either way - these are opaque Firestore identifiers, not
    names, emails or contact details, and none of them addresses a
    document a caller can read without passing that endpoint's own
    authorization.

    Args:
        rating: Rating as stored.

    Returns:
        A copy with unapproved content and the internal moderation
        reason removed, or ``None`` when the rating is withheld and this
        reader may not see it at all. The original is never mutated -
        these models are also the values the aggregate and the
        publication paths read.
    """
    if rating.moderation_status == ModerationStatus.REJECTED.value:
        return None
    redacted = {'moderation_reason': None}
    if rating.moderation_status == ModerationStatus.APPROVED.value:
        return rating.copy(update=redacted)
    return rating.copy(update=dict(redacted, review=None))


def _paginate(
    query: Any,
    page_size: int,
    scan_limit: int,
    transaction: Any = None,
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
        transaction: Open transaction to read every page through, so a
            multi-page walk observes ONE instant rather than one per
            page. ``None`` reads each page independently, which is
            correct for the settlement walks - they write between pages,
            and a record that left the query is a record they no longer
            need.

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
        page = list(page_query.stream(transaction=transaction))
        if not page:
            return
        for snapshot in page:
            yield snapshot
        examined += len(page)
        cursor = page[-1]
        if len(page) < window:
            return


def _due_snapshots(snapshots: Iterable[Any]) -> Iterable[Any]:
    """Keep only the snapshots whose publication window has closed.

    The deadline half of the sweep's predicate, applied here rather than
    in the query. Combining an equality filter on ``is_published`` with a
    range on ``created_at`` would require a composite index that
    infrastructure/firestore.indexes.json does not declare - and a query
    needing an undeclared composite index fails outright rather than
    running slowly - so the datastore answers the part it can index and
    this decides the rest. See :func:`_publish_expired_ratings`.

    Lazy, so the sweep still interleaves settlement with the cursor walk
    instead of materialising every candidate first.

    Args:
        snapshots: Document snapshots of unpublished ratings, in the
            order the walk produced them.

    Yields:
        The subset whose window has elapsed, in the same order. A record
        with an absent or unusable ``created_at`` is not due - see
        :func:`_window_elapsed`, which defers a reveal rather than
        risking an early one.
    """
    for snapshot in snapshots:
        body = snapshot.to_dict() or {}
        if _window_elapsed(body.get('created_at')):
            yield snapshot


def _chunked(items: Iterable[Any], size: int) -> Iterable[List[Any]]:
    """Group a stream into bounded lists, lazily and in order.

    Laziness is the point rather than a detail. The sweep hands this a
    :func:`_paginate` cursor walk and settles each group before asking
    for the next, so the work interleaves with the walk instead of
    materialising every candidate first - which is what keeps a backlog
    of thousands from becoming one list in memory, and what lets each
    settled group leave the query before the next page is fetched.

    Args:
        items: Any iterable, consumed once.
        size: Maximum length of each group. Values below one are treated
            as one, so a misconfigured bound cannot yield empty groups
            forever.

    Yields:
        Lists of at most ``size`` items, in the order received. The last
        group may be shorter; no empty group is ever yielded.
    """
    bound = max(1, int(size))
    batch: List[Any] = []
    for item in items:
        batch.append(item)
        if len(batch) >= bound:
            yield batch
            batch = []
    if batch:
        yield batch


def _list_ratings_for_user(
    user_id: str,
    transaction: Any = None,
) -> List[Rating]:
    """List the published ratings a user has received, newest first.

    Satisfies the reputation half of F010-3, and is the public read: no
    authentication is required of the caller, matching how listings are
    read. Only published ratings are returned, so the double-blind model
    holds on the way out as well as on the way in - an unreciprocated
    rating is invisible here until it is revealed.

    What a reader may see of each one is decided by
    :func:`_visible_projection`: an approved rating in full, a pending
    one with its review text withheld, and a rejected one not at all.
    The score is never withheld for its value, and the aggregate on the
    user document keeps counting every published rating whatever its
    state - so ``count`` may exceed the number of records returned here.
    That is the contract rather than a discrepancy; see
    :func:`_visible_projection` for why withholding the record is the
    right side of that trade.

    ONE BOUNDED READ, WITH NO WIRE-LEVEL CURSOR
    -------------------------------------------------------------------
    The read is bounded at :data:`DEFAULT_RATINGS_PAGE_SIZE` visible
    records and reports no continuation token, because the endpoint's
    contract is ``{items, aggregate}`` and nothing else. A cursor was
    tried here and removed: it widened the published API, and the cursor
    itself was a caller-supplied rating ID that this function looked up
    and positioned a public query from - so any rating ID, including one
    belonging to a different user or one still unpublished, could be
    handed in and its existence inferred from the response. A read with
    no cursor cannot be asked that question at all.

    The bound is a real limit and is documented as one: a user with more
    published ratings than a page has ratings this endpoint does not
    return, while ``aggregate`` still describes all of them. Paging is a
    contract change rather than a fix, and it belongs to whoever plans
    one.

    Args:
        user_id: The rated user.
        transaction: Open transaction to read through, so this list and
            the aggregate beside it can be taken from ONE pinned
            snapshot. ``None`` reads independently, which is only
            correct for a caller that reads nothing else.

    Returns:
        The visible ratings, newest first, each projected for public
        display.
    """
    # The document ID grammar is applied here, before ``user_id`` reaches
    # any ``document()`` call downstream, because this is a CALLER-FACING
    # entry point: the router hands it a raw path parameter that has
    # never passed through a validated model. An emptiness check alone is
    # not enough - a value containing ``/`` is read by Firestore as a
    # nested path, so ``users/x/private/y`` addresses a different
    # document than the one the URL appears to name, and a control
    # character in an ID forges a line in anything that logs it. Refusing
    # the value outright is equivalent to "no such user" from the
    # caller's point of view and gives away nothing.
    #
    # Kept here even though ``get_user_reputation`` validates the same
    # value before this is reached, because this remains an entry point in
    # its own right and a check that only exists in a caller is a check
    # the next caller will not have.
    if not _is_valid_document_id(user_id):
        return []

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

    # Moderation is NOT filtered in the query, and cannot be: adding a
    # third equality filter to this shape would need an index that is not
    # declared, and a query needing an undeclared composite index fails
    # outright. So the walk collects VISIBLE records - dropping the
    # withheld ones and any stored body that cannot satisfy the Rating
    # contract - and the scan factor bounds how far past the page it may
    # look while doing so. Without a walk, a page filled with withheld or
    # unusable records would answer "this user has no reviews" while
    # valid ones sat just beyond it.
    visible: List[Rating] = []
    pages = _paginate(
        query,
        DEFAULT_RATINGS_PAGE_SIZE,
        DEFAULT_RATINGS_PAGE_SIZE * MAX_VISIBILITY_SCAN_FACTOR,
        transaction=transaction,
    )
    for snapshot in pages:
        rating = _rating_from_dict(_body_with_document_id(snapshot))
        if rating is None:
            continue
        projected = _visible_projection(rating)
        if projected is None:
            continue
        visible.append(projected)
        if len(visible) >= DEFAULT_RATINGS_PAGE_SIZE:
            break
    return visible


class UserReputation(NamedTuple):
    """One user's reputation as a single coherent answer.

    Returned by :func:`get_user_reputation`. The two fields belong
    together because they are established from ONE pinned snapshot: the
    list and the aggregate describe the same instant, so the envelope
    cannot contradict itself.

    Attributes:
        items: The visible ratings, newest first, bounded by
            :data:`DEFAULT_RATINGS_PAGE_SIZE`.
        aggregate: The denormalised summary over ALL published ratings,
            including any whose review a moderator withheld - a
            reputation is not a property of the records a reader may be
            shown. ``count`` may therefore exceed ``len(items)``; see
            :func:`_visible_projection`.
    """

    items: List[Rating]
    aggregate: RatingAggregate


def _reputation_snapshot_body(
    transaction: firestore.Transaction,
    user_id: str,
) -> Optional[Tuple[List[Rating], RatingAggregate]]:
    """Read a user's existence, ratings and aggregate at ONE instant.

    The body of the read-only transaction behind
    :func:`get_user_reputation`. All three answers come from the same
    pinned snapshot, which is the point: the aggregate lives on the user
    document and the ratings live in another collection, so two
    independent reads can straddle a publication commit and return a
    count that includes a rating the accompanying list does not. That is
    not a stale response but a self-contradictory one - a profile reading
    "4 reviews" above three of them, with nothing to tell the reader
    which number is true.

    Reads only. Enrolled through
    :func:`app.db.firestore.run_in_read_only_transaction`, so a write
    added here later raises rather than committing.

    Args:
        transaction: Active read-only Firestore transaction, supplied
            positionally by the runner.
        user_id: The rated user.

    Returns:
        ``(items, aggregate)``, or ``None`` when there is no such user.
    """
    snapshot = (
        db.collection(USERS_COLLECTION)
        .document(user_id)
        .get(transaction=transaction)
    )
    if not snapshot.exists:
        return None
    items = _list_ratings_for_user(user_id, transaction=transaction)
    return items, _aggregate_from_snapshot(user_id, snapshot)


def get_user_reputation(user_id: str) -> Optional[UserReputation]:
    """Report one user's reputation as ONE coordinated read. PUBLIC. F010-3.

    The supported entry point for the public reputation endpoint, and the
    reason it exists is correctness rather than convenience.

    SETTLE FIRST, THEN READ ONE SNAPSHOT
    -------------------------------------------------------------------
    Publication has no worker behind it, so a read that is about to
    report a reputation first settles whatever is already due - EVERY
    such read, with no mode that skips it. A mode that answered from the
    user document alone was tried and removed: it was the mode the
    reputation badge used, so the one surface a reader sees most often
    was the one that could report a rating as unpublished indefinitely
    while a worker that will never run was nominally responsible for
    revealing it. Settlement is what makes the deferred reveal correct,
    and correctness is not an opt-in.

    Then BOTH halves of the answer are read inside a single read-only
    transaction. Settling first and reading once, in that order, is what
    makes the envelope coherent by construction: the snapshot observes
    everything this pass revealed, and the list and the aggregate observe
    each other's instant. Two independent reads could straddle a
    concurrent publication and report a count the list does not account
    for.

    Nothing about visibility is relaxed: only published ratings appear,
    review content appears only once moderation has approved it, a
    rejected rating is withheld, and the aggregate counts every published
    rating whatever its score and whatever its moderation state.

    Args:
        user_id: The rated user.

    Returns:
        A :class:`UserReputation`, or ``None`` when there is no such user.
        ``None`` is deliberately distinct from an empty list beside
        ``average=None, count=0``: "this user has no ratings" and "there
        is no such user" are different answers and only the first is a
        200. Establishing existence inside the snapshot rather than in the
        router is what keeps the whole read to one coherent operation.
    """
    # Grammar applied before the value reaches ``document()``. See
    # ``_list_ratings_for_user`` for the full reasoning.
    if not _is_valid_document_id(user_id):
        return None

    # The single settlement pass for this request, before the snapshot is
    # taken so the snapshot includes whatever it revealed. Bounded, and it
    # raises only on a permanent fault - a transient one is absorbed per
    # group by the settlement itself.
    #
    # It runs before existence is established, which costs one bounded
    # query for a user that does not exist and buys the ordering above.
    # The alternative - read the user, settle, read again - is the
    # two-instant shape this function exists to avoid.
    _publish_due_for_ratee(user_id)

    answer = run_in_read_only_transaction(
        _reputation_snapshot_body,
        user_id,
    )
    if answer is None:
        return None
    items, aggregate = answer
    return UserReputation(items, aggregate)


def require_eligibility(
    transaction_id: str,
    caller: User,
) -> EligibilityDecision:
    """Report eligibility, RAISING when the transaction does not exist.

    The supported entry point for the eligibility endpoint, and it exists
    to give the router a TYPED signal for the one decision it has to
    translate into a status code.

    :func:`evaluate_eligibility` reports every outcome as a decision,
    including "no such transaction", because it has no HTTP vocabulary and
    because the interface renders the reason verbatim. The endpoint's
    published contract, though, is a 404 for that one case - so the router
    had to recognise it, and it did so by comparing ``decision.reason``
    against ``TransactionNotFound.message``. That made an HTTP status
    depend on the exact wording of a user-facing sentence: rephrasing the
    message for clarity, or localising it, would silently turn a 404 into a
    200 carrying ``eligible=False``. A caller polling for the transaction
    to appear would then never see it appear.

    Raising the typed exception instead routes through the same
    six-exception mapping every other endpoint uses, so the status and the
    detail stay correct however the prose changes.

    THE GUARD ORDER IS THE SAME ONE THE WRITE PATH ENFORCES
    -------------------------------------------------------------------
    R1 is decided FIRST, before this function looks for the transaction at
    all, and it is decided by :func:`_assess` - the single implementation
    every caller shares - invoked with the transaction DEFERRED so the
    sequence stops at the verification verdict without issuing a read.

    An earlier revision read the transaction first and raised
    ``TransactionNotFound`` before verification was considered, on the
    reasoning that a question about a transaction that does not exist has
    no answer to give about the caller. That inverted the feature's
    published guard order for one endpoint: an unverified caller citing an
    unknown transaction received a 404 here and a 403 from the write path,
    so the endpoint whose entire purpose is to predict what submission will
    do could disagree with it - and the interface would show "no such
    transaction" to somebody whose actual, actionable problem is that their
    account is not verified. The most actionable failure wins, everywhere,
    which is what makes the two paths one contract rather than two.

    Args:
        transaction_id: The transaction the caller is asking about.
        caller: Authenticated user.

    Returns:
        The full decision for every outcome except a missing transaction -
        including an unverified caller, who receives ``eligible=False``
        with the verification reason rather than a refusal, because
        reporting ineligibility is the whole value of the endpoint.

    Raises:
        TransactionNotFound: The caller is verified and there is no such
            transaction, or the ``transaction_id`` cannot name a document
            at all.
    """
    # Guard 1 (R1) first, from the shared guard sequence, with no read.
    verification = _assess(transaction_id, caller, transaction=_DEFERRED)
    if verification['error'] is not None:
        return EligibilityDecision(
            eligible=False,
            reason=verification['error'].message,
            ratee_id=None,
            direction=None,
            already_rated=False,
        )

    # Guard 2 - existence - is the one this endpoint reports as a status
    # code rather than as a decision. A value that cannot name a document
    # cannot name this transaction either, so it is the same verdict.
    if not _is_valid_document_id(transaction_id):
        raise TransactionNotFound()
    # ONE read, handed to the guard sequence below rather than repeated.
    transaction = _load_transaction(transaction_id)
    if transaction is None:
        raise TransactionNotFound()
    return evaluate_eligibility(
        transaction_id,
        caller,
        transaction=transaction,
    )


def list_transaction_ratings(
    transaction_id: str,
    caller: User,
) -> List[Rating]:
    """List the ratings on one transaction, for a participant. PUBLIC.

    The supported entry point for the per-transaction read, and it owns
    the AUTHORIZATION as well as the projection. That is deliberate, and it
    is a change from the router doing the gate itself.

    AUTHORIZATION IS HELD ACROSS THE READ, NOT TAKEN BEFORE IT
    -------------------------------------------------------------------
    The gate is the participant check the transactions router already
    applies to reading a transaction: the caller must be its buyer or its
    seller. What matters here is WHEN it holds. Previously the router read
    the transaction, decided the caller was a participant, and then called
    this module, which settled publications and ran its own query - so the
    decision was taken against one snapshot and the data was returned from
    another. Between the two, the transaction could be reassigned to
    different parties, and the response would carry ratings the caller at
    that point had no right to see.

    So the check is applied twice around the read, against two independent
    reads of the authorizing document, and BOTH must pass:

    1. before anything is read, so an outsider cannot even provoke the
       settlement writes the read performs;
    2. again after the ratings are in hand and before they are returned,
       so a caller whose participation ended while the read was in flight
       is refused rather than served.

    A read-only transaction cannot serve here the way it does for the
    reputation read, because settlement WRITES - and a write path cannot
    run inside a read-only snapshot. Re-authorizing is the form the same
    guarantee takes when the operation is not read-only, and it is the
    cheaper of the two anyway: one extra get-by-ID.

    The double-blind projection is unchanged: the caller's own rating is
    returned whether or not it has been revealed, and the counterparty's
    only once it publishes - and not at all if moderation withheld it.

    Args:
        transaction_id: Transaction whose ratings are wanted.
        caller: The authenticated caller, whose participation is the
            authorization and whose own rating is always included.

    Returns:
        The ratings this caller may see, newest first.

    Raises:
        TransactionNotFound: No such transaction, or a ``transaction_id``
            that cannot name a document. The router reports it as 404.
        NotATransactionParticipant: The caller is neither the buyer nor the
            seller - on either check. The router reports it as 403.
    """
    caller_id = getattr(caller, 'id', None)
    # A raw path parameter has passed no validated model, so the grammar is
    # applied before the value can reach ``document()``: a slash makes
    # Firestore read it as a nested path, addressing something other than
    # the document the URL appears to name.
    if not _is_valid_document_id(transaction_id):
        raise TransactionNotFound()

    _require_participant(transaction_id, caller_id)

    # One read serves both the settlement and the answer; a second read
    # is issued only when settlement actually published something and the
    # snapshots in hand are therefore stale.
    snapshots, changed = _settle_transaction(transaction_id)
    if changed:
        snapshots = _transaction_rating_snapshots(transaction_id)

    # Re-authorized against a FRESH read of the authorizing document,
    # immediately before the data leaves this function. See above.
    _require_participant(transaction_id, caller_id)

    return _project_transaction_ratings(snapshots, caller_id)


def _require_participant(
    transaction_id: str,
    caller_id: Optional[str],
) -> Dict[str, Any]:
    """Prove the caller is a party to the transaction, or refuse.

    A structural clone of the guard ``app/api/transactions.py`` applies
    before disclosing a transaction, with a typed exception in place of its
    ``HTTPException``, and it keeps that module's ordering: a transaction
    that does not exist is "not found", and only an existing transaction
    the caller is not party to is "not a participant".

    Note what that ordering does and does not achieve. It does NOT hide
    existence from an outsider - the two verdicts are distinguishable - and
    it is followed because it is the behaviour already published for the
    same resource. Transaction IDs are opaque scatter-allocated
    identifiers, so enumerating them is not a practical attack, and a
    refusal already reveals that the caller is not a party.

    Args:
        transaction_id: Transaction to authorize against.
        caller_id: The authenticated caller's ID.

    Returns:
        The transaction document body, for a caller that needs it.

    Raises:
        TransactionNotFound: No such transaction.
        NotATransactionParticipant: The caller is not one of its parties.
    """
    transaction = _load_transaction(transaction_id)
    if transaction is None:
        raise TransactionNotFound()
    # Read with ``.get`` rather than by subscript, so a transaction
    # document missing a participant field fails CLOSED instead of raising
    # a ``KeyError``. A document that cannot name its parties authorizes
    # nobody.
    participants = [
        transaction.get('buyer_id'),
        transaction.get('seller_id'),
    ]
    if not caller_id or caller_id not in participants:
        raise NotATransactionParticipant()
    return transaction


def moderate_rating(
    rating_id: str,
    status: Any,
    reason: Optional[str] = None,
) -> Optional[Rating]:
    """Move a rating's moderation state, recording why. PUBLIC. F010-4.

    The supported entry point for the admin-only moderation endpoint.
    Authorization is the router's concern; every rule about what a valid
    moderation decision IS lives behind this call.

    Args:
        rating_id: Rating document ID to transition.
        status: Target state - a :class:`ModerationStatus` member or its
            value string.
        reason: The policy reason justifying the transition, describing the
            violation in the review CONTENT. Required when rejecting and
            refused on any other state - see :func:`_moderate_rating` for
            both halves of that matrix.

    Returns:
        The rating in its new state, or ``None`` when no rating exists at
        that ID.

    Raises:
        ValueError: The status is unrecognised, a rejection carries no
            reason, or a reason accompanies a state that displays the
            review.
    """
    return _moderate_rating(rating_id, status, reason)


def _aggregate_from_snapshot(
    user_id: str,
    snapshot: Any,
) -> RatingAggregate:
    """Read a user's denormalised reputation summary off a snapshot.

    Takes the snapshot rather than fetching one, which is the point: the
    caller has already read the user document to establish that the user
    exists, and the aggregate lives on that same document. Fetching it
    again here would be a second get-by-ID for data already in hand and,
    worse, a second read taken at a different instant from the listing
    the result is returned beside.


    One get-by-ID, never a scan. That is why the aggregate is maintained
    on the user document at all: the SRS requires API responses within
    200 ms for 95% of requests, and recomputing a mean by reading every
    rating a popular seller has received would not hold to that as the
    collection grows.

    Reflects published ratings only, and includes every one of them
    whatever the score.

    Args:
        user_id: The rated user, used to name the subject if a stored
            pair has to be quarantined.
        snapshot: The user document as the caller read it. A ``None`` or
            non-existent snapshot reports "no ratings yet".


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
    if snapshot is None or not snapshot.exists:

        return RatingAggregate()

    body = snapshot.to_dict() or {}
    average, count = _coerce_aggregate(
        body.get('rating_average'),
        body.get('rating_count', 0),
        'users/{0}'.format(user_id),
    )
    return RatingAggregate(average=average, count=count)


def _project_transaction_ratings(
    snapshots: Iterable[Any],
    caller_id: Optional[str] = None,
) -> List[Rating]:
    """Project one transaction's ratings for one reader, newest first.

    The visibility half of the per-transaction read, split from the
    authorization and the settlement so each has one owner. The
    double-blind model is preserved even for a participant: a caller sees
    their own rating whether or not it has been revealed, because it is
    theirs, but the counterparty's stays hidden until it publishes.
    Returning both unconditionally would hand a participant exactly the
    early look the model exists to deny.

    Two distinct rules therefore apply, and which one a rating gets
    depends only on authorship:

    * The caller's OWN rating is returned in full - unrevealed, and even
      when moderation rejected its review, together with the recorded
      reason. Anything else makes a withheld review vanish without
      explanation, leaving its author unable to tell whether it was ever
      received.
    * Anyone else's is subject to the same policy as the public read:
      visible once published, with unapproved review content and the
      internal moderation reason removed, and withheld entirely once
      moderation has rejected it. See :func:`_visible_projection`.

    Args:
        snapshots: Rating document snapshots for one transaction.
        caller_id: The authenticated caller, when there is one. Their own
            rating is always included in full. Omit it for an
            unprivileged view, which then contains published ratings with
            approved review content only.

    Returns:
        Visible ratings ordered by creation time descending.
    """
    visible: List[Rating] = []
    for rating in _ratings_from_snapshots(snapshots):
        if caller_id and rating.rater_id == caller_id:
            visible.append(rating)
            continue
        if not rating.is_published:
            continue
        projected = _visible_projection(rating)
        # ``None`` means the record is withheld from this reader - a
        # moderator ruled a policy violation in it. The authorship branch
        # above has already returned the caller's OWN withheld rating in
        # full, so the only records dropped here are somebody else's.
        if projected is None:
            continue
        visible.append(projected)

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
    # Grammar applied before the value reaches ``document()``. See
    # ``_list_ratings_for_user`` for the full reasoning, and
    # ``_is_valid_rating_id`` for why a rating ID is held to the COMPOSITE
    # bound rather than the component one.
    if not _is_valid_rating_id(rating_id):
        return None
    snapshot = db.collection(RATINGS_COLLECTION).document(rating_id).get()
    if not snapshot.exists:
        return None
    return _rating_from_dict(_body_with_document_id(snapshot))


def _moderation_transaction_body(
    transaction: firestore.Transaction,
    rating_id: str,
    status_value: str,
    reason: Optional[str],
) -> Optional[Dict[str, Any]]:
    """Read, validate and write the moderation state under one lock.

    All of it inside a single Firestore transaction, which is what removes
    the race the read-then-update form carried. Under that form the
    document was read, then updated as a separate operation, so two
    concurrent moderators produced last-write-wins with no indication that
    either had been overwritten, a rating deleted between the two steps
    surfaced as a provider error rather than as "not found", and a body
    that could not satisfy the :class:`Rating` contract was MUTATED first
    and only then reported as missing - a write applied to a record the
    caller was told did not exist.

    Reading through ``transaction`` locks the document for the life of the
    transaction, so the state the validation saw is the state the write
    lands on; if a concurrent write intervenes, the runner re-executes this
    callable against the state that actually holds.

    The read precedes the write, which Firestore requires.

    Args:
        transaction: Active Firestore transaction, supplied positionally
            by :func:`app.db.firestore.run_in_transaction`.
        rating_id: Rating document to transition.
        status_value: Validated target state.
        reason: Validated policy reason, or ``None`` - which is the only
            value permitted on a state other than ``rejected``, and is
            written explicitly so a transition out of a rejection clears
            the justification in the SAME write that moves the state.
            There is no instant at which a displayed review has a
            violation recorded against it.

    Returns:
        The updated document body, or ``None`` when no rating exists at
        that ID or its stored body cannot satisfy the contract - in both
        cases nothing has been written, because the transaction is
        abandoned rather than committed.
    """
    rating_ref = db.collection(RATINGS_COLLECTION).document(rating_id)
    snapshot = rating_ref.get(transaction=transaction)
    if not snapshot.exists:
        return None

    body = _body_with_document_id(snapshot)
    # Proved BEFORE the write, not after it. A document whose body cannot
    # be interpreted is not a document to moderate: writing a new state
    # onto it would leave a record that is both unreadable and freshly
    # stamped, and the caller would be told it did not exist.
    if _rating_from_dict(body) is None:
        logger.error(
            'Refusing to moderate rating %s: its stored body does not '
            'satisfy the Rating contract and needs repair.',
            rating_id,
        )
        return None

    updates: Dict[str, Any] = {
        'moderation_status': status_value,
        'moderation_reason': reason,
        'updated_at': firestore.SERVER_TIMESTAMP,
    }
    transaction.update(rating_ref, updates)

    body.update(updates)
    # The written value is a server-side sentinel, which is not
    # serialisable, so the body handed back carries a real UTC timestamp -
    # the same substitution the create path makes.
    body['updated_at'] = datetime.now(timezone.utc)
    return body


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

    What a rejection withholds is therefore the REVIEW TEXT, not the
    rating. A published rejected rating keeps appearing in its ratee's
    list with its score intact and its words removed, so the ratings a
    reader can see still account for the average they are shown; only
    its author sees the text and the recorded reason. See
    :func:`_visible_projection`.

    Authorization is the router's concern; it restricts this to
    administrators the same way listing deletion is restricted.

    Args:
        rating_id: Rating document ID to transition.
        status: Target state - a :class:`ModerationStatus` member or its
            value string. Validated here, because an unrecognised value
            would otherwise be written straight onto the document and
            silently disable every state check that reads it.
        reason: The policy basis for the transition, as bounded plain
            text describing the violation in the CONTENT. MANDATORY when
            rejecting, and REFUSED on any other state - see
            :data:`MODERATION_REASON_REQUIRED_STATUS` for both halves of
            that matrix and why it is a matrix rather than a default.
            Any previously stored reason is cleared by the transition
            that leaves ``rejected``, in the same write.

    Returns:
        The rating in its new state, or ``None`` when no rating exists at
        that ID or its stored body cannot satisfy the contract - which the
        router reports as not found. Nothing is written on either path.

    Raises:
        ValueError: ``status`` is not a recognised moderation state, a
            rejection was requested without a reason, or a reason was
            supplied for a state that may not carry one. Raised before
            anything is read, let alone written, so a rating is never
            withheld first and justified afterwards - and never displayed
            with a violation recorded against it.
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

    # The reason is held to the same bounded plain-text contract as the
    # review itself, rather than merely stripped. A bare ``.strip()``
    # leaves embedded control characters, un-normalised Unicode and an
    # unbounded length in a value that is PERSISTED on the rating document
    # - so an unbounded reason is an unbounded write into a document
    # Firestore caps at 1 MiB, and a newline in it is a forged line in any
    # log or export that renders it. Whitespace-only normalises to
    # ``None``, which is what makes the requirement below meaningful: a
    # reason of "   " is no reason.
    recorded_reason = as_plain_text(
        reason,
        max_length=MODERATION_REASON_MAX_LENGTH,
        label='Moderation reason',
    )

    # The reason/state matrix, enforced in BOTH directions and BEFORE the
    # document is even read - so there is no path on which a rating is
    # withheld and the justification is left for later, and none on which a
    # displayed review carries a violation.
    withholding = status_value == MODERATION_REASON_REQUIRED_STATUS
    if withholding and recorded_reason is None:
        raise ValueError(
            'Rejecting a rating requires a reason describing the policy '
            'violation in the CONTENT - abuse, personally identifying '
            'information, profanity. A low score is never itself a '
            'violation and cannot be a reason.'
        )
    if not withholding and recorded_reason is not None:
        raise ValueError(
            'A moderation reason may only accompany a rejection. State '
            '{0!r} displays the review, so recording a policy violation '
            'against it would leave a record that contradicts '
            'itself.'.format(status_value)
        )

    # The document ID grammar is applied before the value reaches
    # ``document()``. A rating's ID is the COMPOSITE key, so it is held to
    # the composite bound - see ``_is_valid_rating_id``.
    if not _is_valid_rating_id(rating_id):
        return None

    # Read, validate and write in ONE transaction. See
    # ``_moderation_transaction_body`` for the three distinct races the
    # previous read-then-update form carried.
    try:
        body = run_in_transaction(
            _moderation_transaction_body,
            rating_id,
            status_value,
            recorded_reason,
        )
    except NotFound:
        # The document was deleted between the locked read and the commit.
        # Translated precisely rather than surfacing as a 500: "there is no
        # such rating" is exactly what happened, and it is the same answer
        # the caller would have received a moment earlier.
        logger.info(
            'Rating %s disappeared while its moderation state was being '
            'set; reporting it as not found.',
            rating_id,
        )
        return None
    if body is None:
        return None

    logger.info(
        'Rating %s moderation status set to %s (reason=%s)',
        rating_id,
        status_value,
        recorded_reason or 'none',
    )
    return _rating_from_dict(body)
