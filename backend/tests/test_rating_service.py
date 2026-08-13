"""Service-level tests for the peer rating system.

Exercises ``app/services/rating.py`` DIRECTLY, with no HTTP in the
picture. Its sibling ``test_ratings_api.py`` owns the router, so no
status code is asserted here: the service raises typed domain
exceptions and owns no HTTP vocabulary at all, and the score bound and
the review length limit belong to ``app/schema/rating.py`` rather than
to this layer. What this module owns is the SEMANTICS behind the
acceptance gates - the guard sequence and its order, server-side
derivation, the deterministic key, the aggregate arithmetic, atomicity,
and both publication paths.

Six engineering practices govern these tests, and every one of them is
asserted rather than assumed:

* Never trust client-supplied identity or relationship claims. A
  forged ``ratee_id`` and a forged ``direction`` are both submitted,
  and the PERSISTED document is inspected to prove neither reached
  storage.
* Datastore-enforced uniqueness over read-then-write checks. The
  deterministic key is read back out of raw state, and a real
  collision is provoked through the datastore rather than mocked.
* Atomic multi-document writes. The publication flip and the aggregate
  movement it implies are proved to commit together, and to leave
  NEITHER behind when the transaction fails part-way through.
* Sentiment-neutral moderation. The aggregate reflects published
  ratings only, at every instant, and counts a score of 1 exactly as
  it counts a 5. Rejecting a rating for a policy violation does not
  move it.
* Explicit negative tests for every authorization gate. Each guard has
  its own failing case, and the ORDER is proved by cases that fail two
  guards at once.
* Append-only reputation records. No test rewrites a score; the only
  mutation exercised is a moderation transition carrying a reason.

Every refusal asserts two things, not one: the typed exception, AND
that nothing was written. A gate that answered a refusal after
persisting would satisfy the letter of its requirement and defeat its
purpose, so the raw-state check is half of the assertion rather than a
flourish on it.

``conftest.py`` is imported explicitly below, BEFORE any ``app.*``
import, and the order is the whole point. Its bootstrap runs at import
time - it seeds the eight required settings, revokes ambient Google
credentials, blocks the network and installs the in-memory Firestore
double - and every one of those has to be in place before
``app.core.config`` evaluates ``settings = Settings()`` at module scope.
Under pytest the same module object is already loaded, so the import is
a no-op that costs nothing; run directly, through the
``unittest.main()`` guard at the foot of this file, it is what makes the
module work at all. Without it a direct run dies in
``app.core.config`` with a ``ValidationError`` naming eight settings,
and the guard would be an invitation to a failure that says nothing
about the tests. ``test_ratings_api.py`` does the same thing for the
same reason.

What conftest supplies is otherwise consumed through the application's
own symbols: ``app.db.firestore.db`` IS the double it installs, and its
autouse fixture clears that store and restores the rating tunables
around every test.

``app.tasks.background_jobs`` IS imported, and an earlier revision of
this docstring claimed it could not be. That claim was true once and is
not any more: the module used to read an undeclared
``settings.CELERY_BROKER_URL`` and to register a task body as an
``on_after_configure`` receiver, either of which broke the import. It
now names its own in-memory transport, so importing it provisions
nothing, dispatches nothing and reaches no network - and the scheduled
sweep it hosts is therefore testable rather than assumed. Leaving the
claim in place cost the feature's only proactive publication path its
coverage, which is exactly the kind of gap a stale comment produces.

The import happens inside :class:`TestScheduledSweepTask` and nowhere
else, so the rest of this module keeps its narrow dependency surface,
and no broker is involved: a Celery task object is callable, and calling
it runs its body in this process exactly as a worker would. Exercising
it matters because nothing in this repository dispatches it - there is
no ``.delay()`` or ``apply_async`` call anywhere - so its body is
otherwise unexecuted code. The sweep's decisions still all live in
``app/services/rating.py`` and are asserted there; the task is asserted
to be the thin delegation it documents itself as.

Pydantic v1 semantics throughout, matching the pin in
``backend/requirements.txt``.
"""
import json
import os
import time
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from google.api_core.exceptions import (
    Aborted,
    AlreadyExists,
    FailedPrecondition,
    InvalidArgument,
    RetryError,
    ServiceUnavailable,
)
from google.cloud import firestore
from google.cloud.firestore_v1.base_transaction import MAX_ATTEMPTS
from google.cloud.firestore_v1.transaction import (
    Transaction as FirestoreTransaction,
)
from pydantic import ValidationError

# FIRST, before any ``app.*`` import - see the module docstring. Importing
# conftest runs its bootstrap, which every import below depends on, and
# ``reset_server_timestamps`` is the isolation helper ``setUp`` shares with
# conftest's autouse fixture. Do not let an import sorter move this line
# after the application imports.
from conftest import reset_server_timestamps

from app.core.config import settings
from app.db.firestore import (
    DATASTORE_TIMEOUT_SECONDS,
    DATASTORE_UNAVAILABLE_ERRORS,
    _BoundedTransaction,
    _new_transaction,
    create_document,
    create_document_with_id,
    datastore_call,
    db,
    delete_document,
    get_document,
    query_documents,
    run_in_transaction,
    update_document,
)
from app.schema.rating import (
    DOCUMENT_ID_MAX_LENGTH,
    RATING_ID_MAX_LENGTH,
    EligibilityDecision,
    ModerationStatus,
    Rating,
    RatingCreate,
    RatingDirection,
)
from app.schema.transaction import Transaction
from app.schema.user import User
from app.services.rating import (
    RATINGS_COLLECTION,
    TRANSACTIONS_COLLECTION,
    USERS_COLLECTION,
    _encode_key_component,
    _is_valid_rating_id,
    _rating_document_id,
    DuplicateRating,
    NotATransactionParticipant,
    RaterNotVerified,
    SelfRatingNotAllowed,
    TransactionInvariantError,
    TransactionNotCompleted,
    TransactionNotFound,
    evaluate_eligibility,
    get_user_aggregate,
    get_user_reputation,
    moderate_rating,
    publish_expired_ratings,
    publish_if_reciprocal,
    publish_if_window_elapsed,
    submit_rating,
)
from app.tasks.background_jobs import publish_expired_rating_window

# Identities are spelled out rather than generated, so a failure names
# the party it concerns. Each one satisfies the document-ID grammar
# that ``app/schema/user.py`` and ``app/schema/rating.py`` enforce, so
# a fixture can never be refused for a reason no test intended.
BUYER_ID = 'test-buyer-000000000001'
SELLER_ID = 'test-seller-00000000001'
OTHER_SELLER_ID = 'test-seller-00000000002'
OUTSIDER_ID = 'test-outsider-000000001'
# The administrator a moderation decision is attributed to. Authorization
# is the router's concern, so this identity only ever reaches the audit
# line - which is exactly what the moderation logging tests assert.
ADMIN_ID = 'test-admin-000000000001'
TRANSACTION_ID = 'test-transaction-000001'
OTHER_TRANSACTION_ID = 'test-transaction-000002'
MISSING_TRANSACTION_ID = 'test-transaction-999999'
LISTING_ID = 'test-listing-0000000001'
PAYMENT_INTENT_ID = 'test-payment-intent-001'
AMOUNT = 12500.0

# The window every publication test drives explicitly. It is patched
# onto ``settings`` rather than assumed, so no test depends on the
# shipped default, and it is comfortably longer than "now" so nothing
# seeded at the current instant is ever accidentally overdue.
WINDOW_DAYS = 7

# How far in the past an overdue rating is seeded. Any value above
# WINDOW_DAYS would do; a wide margin keeps the intent obvious.
OVERDUE_DAYS = 30

COMPLETED = 'completed'
PENDING = 'pending'


def utc_now():
    """Return the current instant, timezone-aware and in UTC.

    Returns:
        An aware ``datetime``.
    """
    return datetime.now(timezone.utc)


class _ClockMeta(type):
    """Make :class:`FrozenClock` answer ``isinstance`` for a datetime.

    ``app/services/rating.py`` uses its ``datetime`` global for two
    different things - reading the clock, and ``isinstance(value,
    datetime)`` in ``_as_aware_datetime`` - so a stand-in has to satisfy
    both. A plain ``datetime`` subclass satisfies neither half safely:
    a real stored timestamp is NOT an instance of a subclass, so every
    timestamp would be read as unusable and every test would pass for
    the wrong reason. Delegating the instance check to the real class
    keeps that path behaving exactly as it does in production.
    """

    def __instancecheck__(cls, instance):
        """Report whether ``instance`` is a real ``datetime``."""
        return isinstance(instance, datetime)


class FrozenClock(metaclass=_ClockMeta):
    """A ``datetime`` stand-in whose ``now`` never moves.

    Patched over ``app.services.rating.datetime`` by
    :func:`frozen_clock` so the rating window's boundary can be asserted
    AT the deadline rather than approximately either side of it. Against
    a live clock that case is unreachable: ``_window_elapsed`` compares
    ``now >= created_at + window``, and by the time the call is made the
    clock has moved past whatever instant the test computed, so a
    boundary written as ``>`` would pass every test built on a real
    clock and reveal a rating a whole window early.

    ``min`` is mirrored because ``_sort_key`` falls back to
    ``datetime.min`` for an unstamped rating, and an attribute missing
    from a stand-in surfaces as an ``AttributeError`` in an unrelated
    test rather than as a clear failure here.
    """

    min = datetime.min
    instant = None

    @classmethod
    def now(cls, tz=None):
        """Return the frozen instant, ignoring ``tz`` as UTC.

        Args:
            tz: Accepted for signature compatibility with
                ``datetime.now(timezone.utc)``, which is the only form
                the service calls. The frozen instant is already
                timezone-aware and in UTC.

        Returns:
            The instant :func:`frozen_clock` pinned.
        """
        return cls.instant


def frozen_clock(instant):
    """Pin the service's clock to ``instant`` for the duration.

    Args:
        instant: An aware UTC ``datetime`` that every clock read inside
            the service will return.

    Returns:
        A context manager patching ``app.services.rating.datetime``.
    """
    FrozenClock.instant = instant
    return patch('app.services.rating.datetime', FrozenClock)


def build_user(
    user_id,
    is_verified=True,
    role='buyer',
    rating_average=None,
    rating_count=0,
):
    """Build a ``User`` model, which is what a service call takes.

    ``is_verified`` is ``StrictBool`` on the model, so it must be a real
    boolean - the strictness is the R1 gate's and is left intact.

    Args:
        user_id: Document ID, which is also the JWT subject.
        is_verified: The R1 authorization flag.
        role: ``'buyer'``, ``'seller'`` or ``'admin'``.
        rating_average: Denormalised average, or ``None`` when unrated.
        rating_count: Number of published ratings received.

    Returns:
        An ``app.schema.user.User``.
    """
    moment = utc_now()
    return User(
        id=user_id,
        email='{0}@example.test'.format(user_id),
        first_name='Test',
        last_name='User',
        role=role,
        created_at=moment,
        updated_at=moment,
        is_verified=is_verified,
        rating_average=rating_average,
        rating_count=rating_count,
    )


def seed_user(user):
    """Store a user document and return the model that was stored.

    ``id`` is REMOVED from the body, because a user document is keyed
    by the JWT subject and carries no ``id`` field - ``app/api/auth.py``
    takes the ID off the snapshot reference. Storing one here would let
    a test pass while the production read went unexercised.

    Args:
        user: The ``User`` to store.

    Returns:
        The same ``User``.
    """
    body = user.dict()
    body.pop('id', None)
    db.collection(USERS_COLLECTION).document(user.id).set(body)
    return user


def build_transaction(
    transaction_id=TRANSACTION_ID,
    buyer_id=BUYER_ID,
    seller_id=SELLER_ID,
    status=COMPLETED,
):
    """Build a ``Transaction``; only ``completed`` authorizes a rating.

    All nine fields are required by the model, so every one is
    defaulted: a test that cares only about the participant pair should
    not have to supply a payment intent to get a valid document.

    Args:
        transaction_id: Document ID.
        buyer_id: One half of the shared-transaction gate.
        seller_id: The other half.
        status: Transaction state.

    Returns:
        An ``app.schema.transaction.Transaction``.
    """
    moment = utc_now()
    return Transaction(
        id=transaction_id,
        buyer_id=buyer_id,
        seller_id=seller_id,
        vehicle_listing_id=LISTING_ID,
        amount=AMOUNT,
        status=status,
        stripe_payment_intent_id=PAYMENT_INTENT_ID,
        created_at=moment,
        updated_at=moment,
    )


def seed_transaction(transaction):
    """Store a transaction document and return the model.

    ``id`` is KEPT, unlike a user document: ``app/api/transactions.py``
    writes the allocated ID into the body before storing it, so a
    faithful transaction document carries one.

    Args:
        transaction: The ``Transaction`` to store.

    Returns:
        The same ``Transaction``.
    """
    reference = db.collection(TRANSACTIONS_COLLECTION).document(
        transaction.id
    )
    reference.set(transaction.dict())
    return transaction


def encoded_key_component(component):
    """Escape one key component, independently of the service.

    Written character by character on purpose. The service does the same
    job with two chained ``str.replace`` calls, and an oracle that
    copied that expression would agree with a bug in it - the ORDER of
    those replacements is load-bearing, since escaping ``_`` before
    ``%`` would let an input forge an escape sequence. A different
    algorithm reaching the same answer is what makes the agreement
    assertion worth anything.

    Args:
        component: A transaction ID or rater ID.

    Returns:
        The component with ``%`` and ``_`` percent-escaped.
    """
    escaped = []
    for character in component:
        if character == '%':
            escaped.append('%25')
        elif character == '_':
            escaped.append('%5F')
        else:
            escaped.append(character)
    return ''.join(escaped)


def rating_document_id(transaction_id, rater_id):
    """Compose the deterministic rating key.

    Spelled out here rather than imported from the service, so the
    assertions about the key are INDEPENDENT of the implementation they
    are checking. The key is the natural key itself, which is what
    turns document-ID collision into the one-vote-per-transaction rule.

    The composition is INJECTIVE: each component is escaped so the
    delimiter cannot occur inside one, because a plain join is ambiguous
    - ``('a_b', 'c')`` and ``('a', 'b_c')`` would compose the same key
    and one legitimate rating would be refused as the other's duplicate.
    For ordinary Firestore identifiers, which are alphanumeric, this is
    the identity and the key reads exactly as it always did.

    Args:
        transaction_id: Transaction the rating belongs to.
        rater_id: Party submitting the rating.

    Returns:
        The escaped components joined by a single underscore.
    """
    return '{0}_{1}'.format(
        encoded_key_component(transaction_id),
        encoded_key_component(rater_id),
    )


def build_rating_body(
    transaction_id=TRANSACTION_ID,
    rater_id=BUYER_ID,
    ratee_id=SELLER_ID,
    direction=None,
    score=5,
    review=None,
    is_published=False,
    moderation_status=None,
    moderation_reason=None,
    created_at=None,
):
    """Build a rating document body, field for field as stored.

    ``direction`` and ``moderation_status`` default from the
    application's own enumerations rather than from string literals, so
    a value renamed in ``app/schema/rating.py`` cannot leave these
    fixtures seeding documents the service will not parse.

    ``is_published`` defaults to ``False`` because that is the state a
    rating is created in, and ``created_at`` defaults to now so the
    publication window is OPEN - a past timestamp would make a rating
    look overdue and publish it before the test meant it to.

    Args:
        transaction_id: Transaction the rating belongs to.
        rater_id: Party who submitted it.
        ratee_id: Party being rated.
        direction: A ``RatingDirection`` value; buyer-to-seller when
            omitted.
        score: The vote.
        review: Optional free text.
        is_published: Whether the rating has been revealed.
        moderation_status: A ``ModerationStatus`` value; pending when
            omitted.
        moderation_reason: Policy reason for a rejection - never a
            score-based one.
        created_at: Creation instant.

    Returns:
        The document body as a dict, including its ``id``.
    """
    moment = utc_now() if created_at is None else created_at
    return {
        'id': rating_document_id(transaction_id, rater_id),
        'transaction_id': transaction_id,
        'vehicle_listing_id': LISTING_ID,
        'rater_id': rater_id,
        'ratee_id': ratee_id,
        'direction': (
            RatingDirection.BUYER_TO_SELLER.value
            if direction is None
            else direction
        ),
        'score': score,
        'review': review,
        'is_published': is_published,
        'moderation_status': (
            ModerationStatus.PENDING.value
            if moderation_status is None
            else moderation_status
        ),
        'moderation_reason': moderation_reason,
        'created_at': moment,
        'updated_at': moment,
    }


def seed_rating(body):
    """Store a rating body through ``set``, never through ``create``.

    Arranging a starting state must not consume the create-only
    precondition that the uniqueness tests exist to exercise
    themselves, so this is an overwrite rather than a create.

    Args:
        body: A rating document body carrying its own ``id``.

    Returns:
        The same body.
    """
    reference = db.collection(RATINGS_COLLECTION).document(body['id'])
    reference.set(body)
    return body


class RatingFixtureMixin:
    """Arrangement and raw-state assertions shared by every concern.

    A plain mixin rather than a ``TestCase`` subclass, so that pytest
    does not collect it as an empty suite of its own.

    ``setUp`` seeds the ELIGIBLE cast - a verified buyer, a verified
    seller, and the completed transaction that connects them - because
    every negative case is a single deviation from that state. A test
    needing a different arrangement re-seeds the one document it cares
    about, which overwrites it; separating the deviation like that is
    what lets a refusal be attributed to the guard under test rather
    than to some other guard the fixture also happened to fail.
    """

    def setUp(self):
        """Clear the datastore double, then seed the eligible cast."""
        super().setUp()
        # The double is a process-wide singleton, so isolation comes from
        # clearing it rather than from rebuilding it. Under pytest
        # ``conftest``'s autouse fixture has already done this and these
        # two calls are no-ops; doing them here as well is what keeps the
        # trailing ``unittest.main()`` guard honest, since a bare
        # ``unittest`` run gets no fixtures and would otherwise carry one
        # test's documents - and its timestamp sequence - into the next.
        db.reset()
        reset_server_timestamps()
        self.buyer = seed_user(build_user(BUYER_ID, role='buyer'))
        self.seller = seed_user(build_user(SELLER_ID, role='seller'))
        self.transaction = seed_transaction(build_transaction())

    def submission(self, score=4, review=None, **claims):
        """Build a request body, optionally carrying a forged claim.

        Args:
            score: The vote to submit.
            review: Optional free text; omitted from the body entirely
                when ``None``, since an explicit null and an absent key
                are different requests.
            **claims: Extra keys, used to submit a counterparty claim
                the model deliberately does not declare.

        Returns:
            A ``RatingCreate``.
        """
        fields = {'transaction_id': TRANSACTION_ID, 'score': score}
        if review is not None:
            fields['review'] = review
        fields.update(claims)
        return RatingCreate(**fields)

    def seed_overdue_rating(self, **overrides):
        """Seed one unpublished rating whose window has already closed.

        Args:
            **overrides: Any :func:`build_rating_body` keyword.

        Returns:
            The stored document body.
        """
        overrides.setdefault(
            'created_at', utc_now() - timedelta(days=OVERDUE_DAYS)
        )
        return seed_rating(build_rating_body(**overrides))

    def seed_both_sides(self, buyer_score=4, seller_score=2):
        """Seed one unpublished rating from each party of the pair.

        The state a reciprocal reveal starts from: two ratings, aimed
        in opposite directions, neither visible and neither counted.

        Args:
            buyer_score: The buyer's vote on the seller.
            seller_score: The seller's vote on the buyer.

        Returns:
            The two stored bodies, buyer's first.
        """
        first = seed_rating(build_rating_body(score=buyer_score))
        second = seed_rating(build_rating_body(
            rater_id=SELLER_ID,
            ratee_id=BUYER_ID,
            direction=RatingDirection.SELLER_TO_BUYER.value,
            score=seller_score,
        ))
        return first, second

    def publish_on_expiry(self, body):
        """Publish one rating by closing the window around the call.

        The window is PATCHED rather than assumed, so the test states
        the boundary it depends on instead of inheriting a default that
        a later configuration change could move.

        Args:
            body: The rating to consider, as a body or a model.

        Returns:
            ``True`` when this call published it.
        """
        with patch.object(settings, 'RATING_WINDOW_DAYS', WINDOW_DAYS):
            return publish_if_window_elapsed(body)

    def stored_rating(self, rating_id):
        """Return one rating document straight from raw state.

        Args:
            rating_id: Rating document ID.

        Returns:
            The stored body, or ``None`` when absent.
        """
        return db.document_body(RATINGS_COLLECTION, rating_id)

    def stored_user(self, user_id):
        """Return one user document straight from raw state.

        Args:
            user_id: User document ID.

        Returns:
            The stored body, or ``None`` when absent.
        """
        return db.document_body(USERS_COLLECTION, user_id)

    def assert_no_rating_stored(self):
        """Assert the refusal wrote nothing, not merely that it failed.

        A query cannot express this: it cannot tell a document that is
        absent from one that was written and then filtered out of the
        result. Raw state answers it directly.
        """
        self.assertEqual(db.count(RATINGS_COLLECTION), 0)

    def assert_aggregate(self, user_id, average, count):
        """Assert a user's denormalised reputation, from raw state.

        The average is compared to twelve decimals rather than for
        identity, because the STORED value is the exact unrounded mean -
        a fraction such as ``8 / 7`` - and an expectation written as a
        literal decimal could never equal it. Twelve places is far
        tighter than any arithmetic error this fold could make and far
        looser than the last bit of a double.

        Args:
            user_id: The rated user.
            average: Expected ``rating_average``, or ``None``.
            count: Expected ``rating_count``.
        """
        body = self.stored_user(user_id)
        self.assertIsNotNone(body)
        stored = body.get('rating_average')
        if average is None:
            self.assertIsNone(stored)
        else:
            self.assertIsNotNone(stored)
            self.assertAlmostEqual(stored, average, places=12)
        self.assertEqual(body.get('rating_count'), count)

    def assert_aggregate_equals_scores(self, user_id, scores):
        """Assert a reputation against the SCORES it should summarise.

        The strongest form of the aggregate assertion, and the one the
        rounded running mean could not satisfy: the stored average is
        compared to the mean of the source scores themselves rather than
        to a value derived the same way the implementation derives it.
        The exact integer total is checked too, because that is what the
        next fold reconstructs.

        Args:
            user_id: The rated user.
            scores: Every published score they have received.
        """
        self.assert_aggregate(
            user_id,
            sum(scores) / len(scores),
            len(scores),
        )
        body = self.stored_user(user_id)
        self.assertEqual(
            round(body['rating_average'] * body['rating_count']),
            sum(scores),
        )

    def seed_overdue_rating_from_new_buyer(self, index, score):
        """Seed one more overdue rating aimed at the same seller.

        Each rating needs its own transaction and its own rater, because
        the natural key is that pair and publication re-derives the ratee
        from the transaction rather than trusting the stored field. This
        arranges all three so a test can build up a rating history.

        Args:
            index: Distinguishes this rating's transaction and rater.
            score: The vote.

        Returns:
            The stored rating body.
        """
        rater_id = 'test-buyer-{0:015d}'.format(index)
        transaction_id = 'test-transaction-{0:06d}'.format(index)
        seed_user(build_user(rater_id, role='buyer'))
        seed_transaction(build_transaction(
            transaction_id=transaction_id,
            buyer_id=rater_id,
        ))
        return self.seed_overdue_rating(
            transaction_id=transaction_id,
            rater_id=rater_id,
            score=score,
        )


class TestEligibilityMatrix(RatingFixtureMixin, unittest.TestCase):
    """The ordered guard sequence, branch by branch AND in order.

    Both must-requirements live here. R1 is "only a verified user may
    rate"; R2 is "only the two counterparties of the same transaction
    may rate each other". Each guard has a dedicated failing case, and
    each failing case asserts that nothing was written as well as which
    exception was raised.

    Membership in the matrix is not enough on its own: the plan fixes
    the guard ORDER so that the most specific failure wins and the
    caller receives something actionable. The four ordering tests below
    each fail two guards at once and assert that the earlier one is the
    one reported - which is the only way to distinguish a correct
    sequence from a set of checks that happen to include the right
    ones.
    """

    def test_unverified_rater_is_refused(self):
        """R1: an unverified caller cannot rate, and writes nothing."""
        caller = seed_user(build_user(BUYER_ID, is_verified=False))
        with self.assertRaises(RaterNotVerified):
            submit_rating(self.submission(), caller)
        self.assert_no_rating_stored()

    def test_revoked_verification_is_refused_at_the_write(self):
        """R1 is decided by the LOCKED read, not by the caller's copy.

        The caller here still carries ``is_verified=True`` - the value
        the request was authenticated with - while the stored document
        says otherwise. The stored document has to win, or revoking
        verification would not take effect until a token expired.
        """
        seed_user(build_user(BUYER_ID, is_verified=False))
        with self.assertRaises(RaterNotVerified):
            submit_rating(self.submission(), self.buyer)
        self.assert_no_rating_stored()

    def test_unknown_transaction_is_refused(self):
        """A rating must cite a transaction that exists."""
        payload = RatingCreate(
            transaction_id=MISSING_TRANSACTION_ID,
            score=4,
        )
        with self.assertRaises(TransactionNotFound):
            submit_rating(payload, self.buyer)
        self.assert_no_rating_stored()

    def test_non_participant_is_refused(self):
        """R2: a third party to the transaction cannot rate anyone.

        The caller is VERIFIED, so the refusal can only be attributed
        to participation - which is what makes this a test of R2 rather
        than of R1.
        """
        outsider = seed_user(build_user(OUTSIDER_ID))
        with self.assertRaises(NotATransactionParticipant):
            submit_rating(self.submission(), outsider)
        self.assert_no_rating_stored()

    def test_degenerate_transaction_refuses_self_rating(self):
        """Self-rating is only reachable from a degenerate document.

        The counterparty is computed from the transaction rather than
        accepted from the client, so no request body can produce
        ``ratee_id == rater_id``. A transaction naming the same user as
        both parties is the only way in, which is exactly why this
        branch needs a fixture that could not arise from the API.
        """
        seed_transaction(build_transaction(seller_id=BUYER_ID))
        with self.assertRaises(SelfRatingNotAllowed):
            submit_rating(self.submission(), self.buyer)
        self.assert_no_rating_stored()

    def test_incomplete_transaction_is_refused(self):
        """A rating attests to a completed exchange, nothing less."""
        seed_transaction(build_transaction(status=PENDING))
        with self.assertRaises(TransactionNotCompleted):
            submit_rating(self.submission(), self.buyer)
        self.assert_no_rating_stored()

    def test_second_submission_by_the_same_rater_is_refused(self):
        """One vote per rater per transaction, and the first one wins."""
        submit_rating(self.submission(score=4), self.buyer)
        with self.assertRaises(DuplicateRating):
            submit_rating(self.submission(score=1), self.buyer)
        self.assertEqual(db.count(RATINGS_COLLECTION), 1)
        stored = self.stored_rating(
            rating_document_id(TRANSACTION_ID, BUYER_ID)
        )
        self.assertEqual(stored['score'], 4)

    def test_transaction_without_a_listing_is_refused(self):
        """A malformed transaction is a data defect, not a refusal.

        ``vehicle_listing_id`` is required on every transaction and is
        denormalised onto the rating, so a document lacking it cannot
        supply one. The write path RAISES the internal invariant, which
        rolls its transaction back; the read path converts the same
        condition into a decision, because an interface still needs a
        sentence to show. Both halves are asserted here so the two
        contracts cannot drift apart.
        """
        reference = db.collection(TRANSACTIONS_COLLECTION).document(
            TRANSACTION_ID
        )
        reference.set({
            'id': TRANSACTION_ID,
            'buyer_id': BUYER_ID,
            'seller_id': SELLER_ID,
            'amount': AMOUNT,
            'status': COMPLETED,
            'stripe_payment_intent_id': PAYMENT_INTENT_ID,
        })
        with self.assertRaises(TransactionInvariantError):
            submit_rating(self.submission(), self.buyer)
        self.assert_no_rating_stored()
        decision = evaluate_eligibility(TRANSACTION_ID, self.buyer)
        self.assertFalse(decision.eligible)
        self.assertTrue(decision.reason)
        self.assertIsNone(decision.ratee_id)
        self.assertIsNone(decision.direction)

    def seed_raw_transaction(self, **fields):
        """Store a transaction document that the model would refuse.

        The ``Transaction`` model types every field, so a document with a
        malformed participant cannot be built through it - and a
        malformed participant is exactly what a datastore with no
        server-side schema can hold. Writing the body directly is the
        only way to arrange the state under test.

        Args:
            **fields: Overrides applied to an otherwise valid body.

        Returns:
            The body that was stored.
        """
        body = {
            'id': TRANSACTION_ID,
            'buyer_id': BUYER_ID,
            'seller_id': SELLER_ID,
            'vehicle_listing_id': LISTING_ID,
            'amount': AMOUNT,
            'status': COMPLETED,
            'stripe_payment_intent_id': PAYMENT_INTENT_ID,
        }
        body.update(fields)
        db.collection(TRANSACTIONS_COLLECTION).document(
            body['id']
        ).set(body)
        return body

    def test_an_unusable_counterparty_is_refused_before_any_write(self):
        """The orphan this prevents, in every shape that produced it.

        A stored ``seller_id`` that cannot name a user document passes
        every guard that looks at the transaction: it exists, it names
        the caller, it is completed. The counterparty used to be recorded
        on the rating anyway, so the document was CREATED and only then
        did the grammar-bound ``Rating`` model refuse to serialise it -
        a 500 for a rating that existed, a duplicate conflict on retry,
        and a record no publication could ever address because
        ``users/{ratee_id}`` with a slash in it resolves a nested path.

        Each value below is a different way of being unusable: a slash
        (which would resolve elsewhere), a control character (which
        forges a log line), an oversized value (whose composite key
        could not be moderated), a reserved-namespace ID, and a value
        that is not a string at all - a datastore with no schema can
        hold every one.
        """
        unusable = [
            'seller/nested/path',
            'seller\nid',
            's' * 200,
            '__reserved__',
            12345,
            None,
        ]
        for value in unusable:
            db.reset()
            self.buyer = seed_user(build_user(BUYER_ID, role='buyer'))
            self.seed_raw_transaction(seller_id=value)

            if value is None:
                # No counterparty at all is "nobody to rate", which is a
                # participation refusal rather than a malformed one.
                with self.assertRaises(NotATransactionParticipant):
                    submit_rating(self.submission(), self.buyer)
            else:
                with self.assertRaises(TransactionInvariantError):
                    submit_rating(self.submission(), self.buyer)

            self.assert_no_rating_stored()
            decision = evaluate_eligibility(TRANSACTION_ID, self.buyer)
            self.assertFalse(decision.eligible)
            self.assertTrue(decision.reason)
            # The unusable identifier is never echoed back either.
            self.assertIsNone(decision.ratee_id)
            self.assertIsNone(decision.direction)

    def test_an_unusable_counterparty_credits_no_reputation(self):
        """Refused before the write, so no aggregate can have moved.

        Asserted separately from the refusal itself, because a guard
        that raised AFTER staging its writes would satisfy the exception
        assertion and still leave the damage behind.
        """
        seed_user(build_user(SELLER_ID, role='seller'))
        self.seed_raw_transaction(seller_id='seller/nested/path')
        with self.assertRaises(TransactionInvariantError):
            submit_rating(self.submission(score=5), self.buyer)
        self.assert_no_rating_stored()
        self.assert_aggregate(SELLER_ID, None, 0)
        self.assertEqual(db.count(RATINGS_COLLECTION), 0)

    def test_a_malformed_counterparty_outranks_the_duplicate_check(self):
        """No key is composed from a value that cannot be one.

        The rating key is ``(transaction_id, rater_id)``, so it does not
        contain the ratee - but the existence read that reports a
        duplicate happens inside the same transaction as the create, and
        the guard has to refuse before either. A rating already stored
        for this pair must therefore be irrelevant to the outcome.
        """
        seed_rating(build_rating_body(score=3))
        self.seed_raw_transaction(seller_id='seller/nested/path')
        with self.assertRaises(TransactionInvariantError):
            submit_rating(self.submission(score=5), self.buyer)
        # The rating that was already there is untouched, and no second
        # one appeared.
        self.assertEqual(db.count(RATINGS_COLLECTION), 1)
        self.assertEqual(
            self.stored_rating(
                rating_document_id(TRANSACTION_ID, BUYER_ID)
            )['score'],
            3,
        )

    def test_unverified_non_participant_reports_verification(self):
        """Ordering: guard 1 outranks guard 3.

        This caller fails BOTH R1 and R2. Verification is the
        actionable failure - it is something the caller can go and fix
        - so it must be the one reported. Delete the verification guard
        and this test turns red with a participation error, which is
        what makes it a test of the order rather than of the set.
        """
        caller = seed_user(
            build_user(OUTSIDER_ID, is_verified=False)
        )
        with self.assertRaises(RaterNotVerified):
            submit_rating(self.submission(), caller)
        self.assert_no_rating_stored()

    def test_unknown_transaction_outranks_participation(self):
        """Ordering: guard 2 outranks guard 3.

        A caller who is a party to nothing, citing a transaction that
        does not exist, learns that the transaction is unknown. The
        alternative would tell them they are not a participant of a
        transaction that has no participants at all.
        """
        outsider = seed_user(build_user(OUTSIDER_ID))
        payload = RatingCreate(
            transaction_id=MISSING_TRANSACTION_ID,
            score=4,
        )
        with self.assertRaises(TransactionNotFound):
            submit_rating(payload, outsider)
        self.assert_no_rating_stored()

    def test_non_participant_on_pending_reports_participation(self):
        """Ordering: guard 3 outranks guard 5.

        An outsider must not be told to wait for a transaction to
        complete, because completing it would not make them eligible.
        """
        seed_transaction(build_transaction(status=PENDING))
        outsider = seed_user(build_user(OUTSIDER_ID))
        with self.assertRaises(NotATransactionParticipant):
            submit_rating(self.submission(), outsider)
        self.assert_no_rating_stored()

    def test_self_rating_on_pending_reports_self_rating(self):
        """Ordering: guard 4 outranks guard 5."""
        seed_transaction(build_transaction(
            seller_id=BUYER_ID,
            status=PENDING,
        ))
        with self.assertRaises(SelfRatingNotAllowed):
            submit_rating(self.submission(), self.buyer)
        self.assert_no_rating_stored()

    def test_eligible_decision_reports_every_field(self):
        """The decision is structured data, not a bare boolean.

        The interface disables its submission control from this answer,
        so every field it renders is asserted: the verdict, the absence
        of a reason when there is nothing to explain, the derived
        counterparty, the derived direction, and the duplicate report.
        """
        decision = evaluate_eligibility(TRANSACTION_ID, self.buyer)
        self.assertIsInstance(decision, EligibilityDecision)
        self.assertTrue(decision.eligible)
        self.assertIsNone(decision.reason)
        self.assertEqual(decision.ratee_id, SELLER_ID)
        self.assertEqual(
            decision.direction,
            RatingDirection.BUYER_TO_SELLER.value,
        )
        self.assertFalse(decision.already_rated)

    def test_eligible_decision_reports_the_reverse_direction(self):
        """The seller's decision names the buyer, and vice versa."""
        decision = evaluate_eligibility(TRANSACTION_ID, self.seller)
        self.assertTrue(decision.eligible)
        self.assertEqual(decision.ratee_id, BUYER_ID)
        self.assertEqual(
            decision.direction,
            RatingDirection.SELLER_TO_BUYER.value,
        )
        self.assertFalse(decision.already_rated)

    def test_already_rated_flips_after_a_submission(self):
        """The same question answers differently once a vote exists."""
        before = evaluate_eligibility(TRANSACTION_ID, self.buyer)
        self.assertTrue(before.eligible)
        self.assertFalse(before.already_rated)
        submit_rating(self.submission(), self.buyer)
        after = evaluate_eligibility(TRANSACTION_ID, self.buyer)
        self.assertTrue(after.already_rated)
        self.assertFalse(after.eligible)
        self.assertEqual(after.reason, DuplicateRating.message)
        self.assertEqual(after.ratee_id, SELLER_ID)
        self.assertEqual(
            after.direction,
            RatingDirection.BUYER_TO_SELLER.value,
        )

    def test_unverified_decision_explains_and_hides_the_ratee(self):
        """The reason is prose, and a refused caller learns no more.

        ``reason`` is rendered verbatim as the disabled-state
        explanation, so it is asserted to be the service's own sentence
        and to actually mention verification. ``ratee_id`` and
        ``direction`` stay empty because participation was never
        established - a rejected caller must not be handed the
        counterparty's identity.
        """
        caller = build_user(BUYER_ID, is_verified=False)
        decision = evaluate_eligibility(TRANSACTION_ID, caller)
        self.assertFalse(decision.eligible)
        self.assertEqual(decision.reason, RaterNotVerified.message)
        self.assertIn('verified', decision.reason)
        self.assertIsNone(decision.ratee_id)
        self.assertIsNone(decision.direction)
        self.assertFalse(decision.already_rated)

    def test_non_participant_decision_withholds_the_ratee(self):
        """An outsider's decision discloses nothing about the parties."""
        caller = build_user(OUTSIDER_ID)
        decision = evaluate_eligibility(TRANSACTION_ID, caller)
        self.assertFalse(decision.eligible)
        self.assertEqual(
            decision.reason,
            NotATransactionParticipant.message,
        )
        self.assertIsNone(decision.ratee_id)
        self.assertIsNone(decision.direction)
        self.assertFalse(decision.already_rated)

    def test_eligibility_check_writes_nothing(self):
        """An eligibility check is a question, never an event."""
        evaluate_eligibility(TRANSACTION_ID, self.buyer)
        evaluate_eligibility(TRANSACTION_ID, self.seller)
        self.assert_no_rating_stored()
        self.assert_aggregate(SELLER_ID, None, 0)
        self.assert_aggregate(BUYER_ID, None, 0)

    def test_slash_bearing_counterparty_is_refused(self):
        """A counterparty that cannot be a document ID writes nothing.

        ``ratee_id`` is derived from the transaction, so it is stored
        data rather than input, and a slash in it would make
        ``document()`` resolve a nested path instead of a user. Refusing
        it before the write is the whole point: the value used to pass
        every guard, get persisted, and only then fail the response
        model back in ``submit_rating`` - so the caller received a 500
        for a rating that had actually been recorded, and the retry that
        invites was answered as a duplicate.
        """
        seed_transaction(build_transaction(seller_id='bad/seller'))
        with self.assertRaises(TransactionInvariantError):
            submit_rating(self.submission(), self.buyer)
        self.assert_no_rating_stored()

    def test_non_string_counterparty_is_refused(self):
        """A non-string counterparty cannot address a user at all.

        Seeded through the raw document write rather than the model,
        because ``Transaction`` would coerce this to a string and the
        case under test is precisely a stored document that never went
        through it.
        """
        reference = db.collection(TRANSACTIONS_COLLECTION).document(
            TRANSACTION_ID
        )
        reference.set({
            'id': TRANSACTION_ID,
            'buyer_id': BUYER_ID,
            'seller_id': 12345,
            'vehicle_listing_id': LISTING_ID,
            'amount': AMOUNT,
            'status': COMPLETED,
            'stripe_payment_intent_id': PAYMENT_INTENT_ID,
        })
        with self.assertRaises(TransactionInvariantError):
            submit_rating(self.submission(), self.buyer)
        self.assert_no_rating_stored()

    def test_counterparty_without_a_user_document_is_refused(self):
        """A rating needs somebody to be about, proved before the write.

        The transaction is well formed and completed, and its
        counterparty ID is perfectly usable - it simply names no user.
        That used to be answered as a successful submission, and the
        rating it created could never publish: the publication
        transaction refuses without the ratee's document to credit, so
        the record stayed invisible and uncounted with no route back.

        Both halves of the contract are asserted, because the whole
        value of the eligibility endpoint is that it cannot disagree
        with the write path: the write raises the internal invariant and
        stores nothing, and the read reports the same condition as a
        decision carrying a reason and no counterparty context.
        """
        seed_transaction(build_transaction(seller_id=OTHER_SELLER_ID))
        self.assertIsNone(
            db.peek(USERS_COLLECTION, OTHER_SELLER_ID),
            'the fixture must not seed the counterparty for this case',
        )
        with self.assertRaises(TransactionInvariantError):
            submit_rating(self.submission(), self.buyer)
        self.assert_no_rating_stored()
        decision = evaluate_eligibility(TRANSACTION_ID, self.buyer)
        self.assertFalse(decision.eligible)
        self.assertEqual(decision.reason, TransactionInvariantError.message)
        self.assertIsNone(decision.ratee_id)
        self.assertIsNone(decision.direction)
        self.assertFalse(decision.already_rated)

    def test_counterparty_deleted_before_the_write_is_refused(self):
        """The existence proof is taken under the write's own lock.

        The counterparty exists when the fixture is built and is gone by
        the time the submission runs, which is the state an unlocked
        pre-check would have approved. The guard reads the document
        THROUGH the submission transaction, so the answer that permits
        the write is the answer that holds when it commits.
        """
        db.collection(USERS_COLLECTION).document(SELLER_ID).delete()
        with self.assertRaises(TransactionInvariantError):
            submit_rating(self.submission(), self.buyer)
        self.assert_no_rating_stored()

    def test_unusable_counterparty_outranks_incompleteness(self):
        """Ordering: guard 4 outranks guard 5.

        Completing a transaction cannot repair a participant field that
        is not a usable identifier, so "wait for it to complete" would
        not be the actionable answer here - and the unusable value must
        never reach ``outcome['ratee_id']``, where the eligibility
        endpoint would report a counterparty nothing can address.
        """
        seed_transaction(build_transaction(
            seller_id='bad/seller',
            status=PENDING,
        ))
        with self.assertRaises(TransactionInvariantError):
            submit_rating(self.submission(), self.buyer)
        self.assert_no_rating_stored()

    def test_incompleteness_outranks_the_missing_counterparty(self):
        """Ordering: guard 5 outranks guard 7.

        A transaction that has not completed is refused before anything
        is asked about its counterparty, both because "wait until it
        completes" is the actionable failure and because guard 7 is the
        only guard that spends a read - and there is nothing worth
        reading about a transaction that cannot be rated yet.
        """
        seed_transaction(build_transaction(
            seller_id=OTHER_SELLER_ID,
            status=PENDING,
        ))
        with self.assertRaises(TransactionNotCompleted):
            submit_rating(self.submission(), self.buyer)
        self.assert_no_rating_stored()


class TestDerivationAndUniqueness(
    RatingFixtureMixin,
    unittest.TestCase,
):
    """Server-side derivation, the deterministic key, and duplicates.

    R0 - ratings flow in both directions - is implemented by DERIVING
    the counterparty and the direction from the transaction rather than
    accepting them, which is what makes counterparty spoofing and
    self-rating structurally impossible instead of merely validated
    against. These tests assert the derivation in both directions, and
    then attack it with a body that claims otherwise.

    The one-vote rule is enforced by document-ID collision, because a
    read-then-write existence check would race. Its assertions are
    therefore about the KEY: that the key is the natural key, that a
    taken key refuses the write, and that a refusal moves no reputation.
    """

    def test_buyer_submission_derives_the_seller_and_direction(self):
        """Caller is the buyer, so the seller is rated. R0."""
        rating = submit_rating(self.submission(), self.buyer)
        self.assertIsInstance(rating, Rating)
        self.assertEqual(rating.rater_id, BUYER_ID)
        self.assertEqual(rating.ratee_id, SELLER_ID)
        self.assertEqual(
            rating.direction,
            RatingDirection.BUYER_TO_SELLER.value,
        )
        stored = self.stored_rating(rating.id)
        self.assertEqual(stored['rater_id'], BUYER_ID)
        self.assertEqual(stored['ratee_id'], SELLER_ID)
        self.assertEqual(
            stored['direction'],
            RatingDirection.BUYER_TO_SELLER.value,
        )

    def test_seller_submission_derives_the_buyer_and_direction(self):
        """Caller is the seller, so the buyer is rated. The other half
        of R0, and the reason a single direction constant would not do.
        """
        rating = submit_rating(self.submission(), self.seller)
        self.assertEqual(rating.rater_id, SELLER_ID)
        self.assertEqual(rating.ratee_id, BUYER_ID)
        self.assertEqual(
            rating.direction,
            RatingDirection.SELLER_TO_BUYER.value,
        )
        stored = self.stored_rating(rating.id)
        self.assertEqual(stored['ratee_id'], BUYER_ID)
        self.assertEqual(
            stored['direction'],
            RatingDirection.SELLER_TO_BUYER.value,
        )

    def test_the_listing_is_denormalised_from_the_transaction(self):
        """Context travels with the rating, taken from the locked read.

        Nothing in the request body can influence it, which is why it
        is asserted against the transaction's own value rather than
        against anything the caller supplied.
        """
        rating = submit_rating(self.submission(), self.buyer)
        self.assertEqual(
            rating.vehicle_listing_id,
            self.transaction.vehicle_listing_id,
        )
        stored = self.stored_rating(rating.id)
        self.assertEqual(stored['vehicle_listing_id'], LISTING_ID)

    def test_forged_counterparty_claim_never_reaches_storage(self):
        """A hostile body claiming an unrelated ratee is REFUSED.

        ``RatingCreate`` declares no ``ratee_id``, and retains one only
        so that the claim is visible enough to be refused - dropping it
        silently would answer success to a request the server did not
        honour. The claim is evidence to check against the derived
        counterparty, never a value to use, so a disagreement is a
        refusal and the datastore is left empty. Asserting emptiness is
        the point: it proves the forgery never reached storage, which
        the return value alone could not show.
        """
        seed_user(build_user(OUTSIDER_ID))
        payload = self.submission(ratee_id=OUTSIDER_ID)
        self.assertIn('ratee_id', payload.__fields_set__)
        with self.assertRaises(NotATransactionParticipant):
            submit_rating(payload, self.buyer)
        self.assert_no_rating_stored()
        self.assert_aggregate(OUTSIDER_ID, None, 0)
        self.assert_aggregate(SELLER_ID, None, 0)

    def test_a_null_counterparty_claim_is_still_a_claim(self):
        """An explicit null is a claim, not an absence.

        Presence is read from ``__fields_set__`` rather than from the
        value, so ``ratee_id: null`` is a claim that disagrees with the
        derived counterparty. Testing truthiness instead would answer
        success here and silently redirect the rating.
        """
        payload = self.submission(ratee_id=None)
        self.assertIn('ratee_id', payload.__fields_set__)
        with self.assertRaises(NotATransactionParticipant):
            submit_rating(payload, self.buyer)
        self.assert_no_rating_stored()

    def test_an_empty_counterparty_claim_is_still_a_claim(self):
        """``''`` is falsy and is still a disagreement to refuse."""
        payload = self.submission(ratee_id='')
        with self.assertRaises(NotATransactionParticipant):
            submit_rating(payload, self.buyer)
        self.assert_no_rating_stored()

    def test_a_forged_direction_is_refused_by_the_contract(self):
        """``direction`` is derived, so a body may not carry one at all.

        Refused at the request boundary rather than in the service:
        every unrecognised key except the counterparty claim is
        rejected, which keeps the contract discoverable instead of
        letting a caller believe a field had an effect it never had.
        """
        with self.assertRaises(ValidationError):
            RatingCreate(
                transaction_id=TRANSACTION_ID,
                score=4,
                direction=RatingDirection.SELLER_TO_BUYER.value,
            )
        self.assert_no_rating_stored()

    def test_a_truthful_claim_still_persists_the_derived_value(self):
        """A claim that agrees is checked and then DISCARDED.

        The accepted path has to be asserted too, or "the forged claim
        was refused" would be satisfied by refusing every claim.
        """
        payload = self.submission(ratee_id=SELLER_ID)
        rating = submit_rating(payload, self.buyer)
        self.assertEqual(rating.ratee_id, SELLER_ID)
        stored = self.stored_rating(rating.id)
        self.assertEqual(stored['ratee_id'], SELLER_ID)
        self.assertEqual(stored['rater_id'], BUYER_ID)
        self.assertEqual(
            stored['direction'],
            RatingDirection.BUYER_TO_SELLER.value,
        )

    def test_the_rater_is_the_caller_and_nothing_else(self):
        """``rater_id`` comes from the authenticated caller alone."""
        rating = submit_rating(self.submission(), self.seller)
        self.assertEqual(rating.rater_id, self.seller.id)
        self.assertEqual(
            db.document_ids(RATINGS_COLLECTION),
            [rating_document_id(TRANSACTION_ID, SELLER_ID)],
        )

    def test_document_id_is_the_transaction_rater_composite(self):
        """The key IS the natural key, read back out of raw state.

        Composed independently here from the two identities, so this
        asserts the contract rather than echoing the implementation.
        """
        expected = '{0}_{1}'.format(TRANSACTION_ID, BUYER_ID)
        rating = submit_rating(self.submission(), self.buyer)
        self.assertEqual(expected, rating_document_id(
            TRANSACTION_ID, BUYER_ID
        ))
        self.assertEqual(rating.id, expected)
        self.assertEqual(
            db.document_ids(RATINGS_COLLECTION),
            [expected],
        )
        self.assertEqual(self.stored_rating(expected)['id'], expected)

    def test_a_taken_key_refuses_a_new_submission(self):
        """The collision IS the duplicate-vote signal.

        The create-only write is what rejects this, not a preceding
        existence check that could race. ``TestDatastoreDoubleFidelity``
        proves the same collision raises ``AlreadyExists`` at the
        create-only primitive; this asserts the service answers it with
        its own domain error and leaves the stored vote untouched.
        """
        seed_rating(build_rating_body(score=5))
        with self.assertRaises(DuplicateRating):
            submit_rating(self.submission(score=1), self.buyer)
        self.assertEqual(db.count(RATINGS_COLLECTION), 1)
        stored = self.stored_rating(
            rating_document_id(TRANSACTION_ID, BUYER_ID)
        )
        self.assertEqual(stored['score'], 5)

    def test_a_duplicate_moves_no_reputation(self):
        """A refused second vote must not touch the ratee's aggregate."""
        submit_rating(self.submission(score=4), self.buyer)
        self.assert_aggregate(SELLER_ID, None, 0)
        with self.assertRaises(DuplicateRating):
            submit_rating(self.submission(score=1), self.buyer)
        self.assert_aggregate(SELLER_ID, None, 0)
        self.assertEqual(db.count(RATINGS_COLLECTION), 1)

    def test_a_raced_duplicate_cannot_slip_past_the_lock(self):
        """The rule holds however two submissions interleave.

        The duplicate is planted at the one instant that matters - after
        the transaction has read the key as free and before it commits -
        so the datastore reaches its own verdict instead of the test
        injecting an exception. The submission is refused, the vote
        already there is the one that survives, and more than one commit
        was attempted, which is the evidence that the race genuinely
        happened rather than being arranged away.
        """
        planted = build_rating_body(score=5)

        def interfere(client):
            """Plant the duplicate just before the commit is checked."""
            client.seed(RATINGS_COLLECTION, planted['id'], planted)

        db.arm_commit_interference(interfere)
        attempts = db.commit_attempts
        with self.assertRaises(DuplicateRating):
            submit_rating(self.submission(score=1), self.buyer)
        self.assertGreater(db.commit_attempts, attempts)
        self.assertEqual(db.count(RATINGS_COLLECTION), 1)
        self.assertEqual(
            self.stored_rating(planted['id'])['score'],
            5,
        )
        self.assert_aggregate(SELLER_ID, None, 0)

    def test_both_parties_may_rate_the_same_transaction(self):
        """Two votes on one transaction are not a duplicate.

        Uniqueness is per rater per transaction, so the composite key
        differs by rater. Without this the constraint could be too
        strict and R0 would be broken by the very rule protecting R2.
        """
        first = submit_rating(self.submission(score=4), self.buyer)
        second = submit_rating(self.submission(score=2), self.seller)
        self.assertNotEqual(first.id, second.id)
        self.assertEqual(
            sorted(db.document_ids(RATINGS_COLLECTION)),
            sorted([
                rating_document_id(TRANSACTION_ID, BUYER_ID),
                rating_document_id(TRANSACTION_ID, SELLER_ID),
            ]),
        )

    def test_the_service_key_matches_an_independent_composition(self):
        """One encoder, checked against a differently-written oracle.

        The suite composes rating keys in dozens of places, so an
        encoding restated in the tests and drifting from production's
        would let every one of those assertions agree with itself and
        with nothing that is actually stored.
        """
        pairs = [
            (TRANSACTION_ID, BUYER_ID),
            ('a_b', 'c'),
            ('a', 'b_c'),
            ('a%', '25b'),
            ('%5F', '_'),
            ('a' + '_' * 7, '%' * 8),
        ]
        for transaction_id, rater_id in pairs:
            self.assertEqual(
                _rating_document_id(transaction_id, rater_id),
                rating_document_id(transaction_id, rater_id),
            )
        self.assertEqual(
            _encode_key_component('a_b%c'),
            encoded_key_component('a_b%c'),
        )

    def test_distinct_pairs_never_compose_the_same_key(self):
        """The ambiguity that made "duplicate" a false positive.

        An underscore is a LEGAL Firestore document-ID character, so a
        plain ``transaction_id + '_' + rater_id`` join is not injective:
        ``('a_b', 'c')`` and ``('a', 'b_c')`` both compose ``'a_b_c'``.
        Two legitimate, unrelated ratings would then address ONE
        document, and the second rater would be told - with the
        datastore agreeing - that they had already rated a transaction
        they had never seen.

        The pairs below are the ones that collided, plus the harder
        cases: a component ending in the delimiter against one starting
        with it, and components carrying the escape character itself, so
        an input cannot forge an escape sequence either.
        """
        pairs = [
            ('a_b', 'c'),
            ('a', 'b_c'),
            ('a_', 'b'),
            ('a', '_b'),
            ('a_b_c', 'd'),
            ('a', 'b_c_d'),
            ('a%5Fb', 'c'),
            ('a', '%5Fb_c'),
            ('a%', '25b'),
            ('a%25', 'b'),
        ]
        keys = {}
        for transaction_id, rater_id in pairs:
            key = _rating_document_id(transaction_id, rater_id)
            self.assertNotIn(
                key,
                keys,
                'pair {0} collides with {1} on key {2!r}'.format(
                    (transaction_id, rater_id), keys.get(key), key
                ),
            )
            keys[key] = (transaction_id, rater_id)
        self.assertEqual(len(keys), len(pairs))

    def test_an_ordinary_identifier_encodes_to_itself(self):
        """Escaping costs nothing for the IDs Firestore allocates.

        Firestore's own document IDs are alphanumeric, so the encoding
        is the identity for every key this system actually mints - which
        is what makes the injective composition a compatible change
        rather than one that strands ratings written under the previous
        scheme.
        """
        self.assertEqual(
            _rating_document_id(TRANSACTION_ID, BUYER_ID),
            '{0}_{1}'.format(TRANSACTION_ID, BUYER_ID),
        )
        self.assertEqual(
            _encode_key_component('nSk3Zq8vXbLpQr2mTd0y'),
            'nSk3Zq8vXbLpQr2mTd0y',
        )

    def test_a_composed_key_stays_within_its_declared_bound(self):
        """The worst case the moderation endpoint has to accept.

        A rating whose key exceeded ``RATING_ID_MAX_LENGTH`` would be
        created and then be unmoderatable and unreadable individually,
        because the path parameter and the service's own check both hold
        it to that bound. Escaping triples a component of underscores,
        so the bound has to allow for that.
        """
        # A component of NOTHING but underscores is refused earlier,
        # because ``__...__`` is Firestore's reserved namespace, so the
        # worst legal case is one other character followed by 127 of
        # them - which escaping turns into 382.
        worst = 'a' + '_' * (DOCUMENT_ID_MAX_LENGTH - 1)
        self.assertEqual(len(worst), DOCUMENT_ID_MAX_LENGTH)
        key = _rating_document_id(worst, worst)
        self.assertLessEqual(len(key), RATING_ID_MAX_LENGTH)
        self.assertTrue(_is_valid_rating_id(key))
        self.assertTrue(
            _is_valid_rating_id(
                _rating_document_id(TRANSACTION_ID, BUYER_ID)
            )
        )

    def test_two_underscore_bearing_pairs_both_get_a_rating(self):
        """The collision, driven through the service end to end.

        Both submissions are legitimate - different transactions,
        different raters - and under the ambiguous join the second was
        refused as a duplicate of the first, because the two pairs
        composed one key. Two documents must exist.
        """
        rater = 'rater-one-000000000001'
        first = ('tx_alpha', rater)
        second = ('tx', 'alpha_' + rater)
        self.assertEqual(
            '{0}_{1}'.format(*first),
            '{0}_{1}'.format(*second),
        )

        for transaction_id, rater_id in (first, second):
            caller = seed_user(build_user(rater_id, role='buyer'))
            seed_transaction(build_transaction(
                transaction_id=transaction_id,
                buyer_id=rater_id,
            ))
            rating = submit_rating(
                RatingCreate(transaction_id=transaction_id, score=4),
                caller,
            )
            self.assertEqual(
                rating.id,
                rating_document_id(transaction_id, rater_id),
            )

        self.assertEqual(db.count(RATINGS_COLLECTION), 2)
        self.assertEqual(
            sorted(db.document_ids(RATINGS_COLLECTION)),
            sorted([
                rating_document_id(*first),
                rating_document_id(*second),
            ]),
        )


class TestAggregateArithmetic(RatingFixtureMixin, unittest.TestCase):
    """The running mean, its neutrality, and the atomicity it needs.

    The aggregate is denormalised onto the user document and maintained
    incrementally, because Firestore forbids querying inside a
    transaction - all reads before any write, and get-by-ID only - so
    "recompute from the user's ratings" is not available at the moment
    it would be needed. That makes the arithmetic worth asserting
    exactly rather than approximately, and it makes the first-rating
    case the one most likely to be wrong, since it divides by a count
    that was zero.

    Two invariants ride alongside the arithmetic. Only PUBLISHED
    ratings are counted, at every instant, which is what removes the
    incentive for review extortion. And every published rating counts
    the same, whatever its score - there is no threshold, no weighting
    and no floor, and none may be added.
    """

    def test_first_published_rating_moves_the_count_zero_to_one(self):
        """The 0 -> 1 transition, average ``None`` -> the score.

        "No ratings yet" has to stay distinguishable from a genuine
        average, so the average is ``None`` beforehand rather than
        ``0.0``, and becomes exactly the submitted score afterwards.
        """
        body = self.seed_overdue_rating(score=4)
        self.assert_aggregate(SELLER_ID, None, 0)
        self.assertTrue(self.publish_on_expiry(body))
        self.assert_aggregate(SELLER_ID, 4.0, 1)

    def test_a_subsequent_rating_folds_the_exact_mean(self):
        """The incremental mean, stored exactly rather than rounded.

        ``4.0`` over two ratings is a total of 8, so folding a 5 gives
        13/3. That value has no two-decimal representation, and storing
        one is what used to make the NEXT fold wrong - so the stored
        value is the fraction and the rounding happens on the way out.
        """
        seed_user(build_user(
            SELLER_ID,
            rating_average=4.0,
            rating_count=2,
        ))
        body = self.seed_overdue_rating(score=5)
        self.assertTrue(self.publish_on_expiry(body))
        self.assert_aggregate(SELLER_ID, 13 / 3, 3)
        self.assert_aggregate_equals_scores(SELLER_ID, [4, 4, 5])
        # ... and 4.33 is what a reader is shown.
        reputation = get_user_reputation(SELLER_ID)
        self.assertEqual(reputation.aggregate.average, 4.33)
        self.assertEqual(reputation.aggregate.count, 3)

    def test_the_presented_average_rounds_rather_than_truncates(self):
        """Presentation rounds UP where it should, storage does not.

        Chosen deliberately over a value that divides evenly: a bug in
        the rounding step is invisible to a case whose exact answer
        already has two decimals. Both halves are asserted, because
        rounding in the wrong PLACE is the defect being guarded against -
        the stored value must stay exact so later folds are unaffected.
        """
        seed_user(build_user(
            SELLER_ID,
            rating_average=13 / 3,
            rating_count=3,
        ))
        body = self.seed_overdue_rating(score=5)
        self.assertTrue(self.publish_on_expiry(body))
        # (13 + 5) / 4 = 4.5 exactly, from an exact base of 13.
        self.assert_aggregate(SELLER_ID, 4.5, 4)

        # 4.995 presents as 5.0 rather than 4.99: seven 5s and one 4.
        seed_user(build_user(
            OTHER_SELLER_ID,
            rating_average=4.875,
            rating_count=8,
        ))
        seed_transaction(build_transaction(
            transaction_id=OTHER_TRANSACTION_ID,
            seller_id=OTHER_SELLER_ID,
        ))
        second = self.seed_overdue_rating(
            transaction_id=OTHER_TRANSACTION_ID,
            ratee_id=OTHER_SELLER_ID,
            score=5,
        )
        self.assertTrue(self.publish_on_expiry(second))
        self.assert_aggregate(OTHER_SELLER_ID, 44 / 9, 9)
        self.assertEqual(
            get_user_reputation(OTHER_SELLER_ID).aggregate.average,
            4.89,
        )

    def test_the_stored_average_equals_the_mean_of_its_scores(self):
        """The sequence that proved the rounded recurrence wrong.

        (1, 1, 1, 1, 1, 2, 1) has a true mean of 8/7 = 1.142857..., so a
        reader is shown 1.14. Published one at a time, the previous fold
        stored 1.15 - each step reconstructing its total from the last
        step's rounded average and accumulating the error. The stored
        value must equal the mean of the source scores, and the presented
        one must be their mean rounded, not a rounded value's mean.
        """
        scores = [1, 1, 1, 1, 1, 2, 1]
        for index, score in enumerate(scores):
            body = self.seed_overdue_rating_from_new_buyer(index, score)
            self.assertTrue(self.publish_on_expiry(body))

        self.assert_aggregate_equals_scores(SELLER_ID, scores)
        self.assertEqual(
            get_user_reputation(SELLER_ID).aggregate.average,
            1.14,
        )

    def test_publication_grouping_cannot_change_the_average(self):
        """Two users, the same scores, revealed differently.

        The path dependence this removes: the seven scores below stored
        1.15 when published one at a time and 1.14 when published in one
        batch, so a reputation depended on how ratings happened to be
        revealed rather than on what they were. One user's are settled
        individually and the other's in a single sweep transaction, and
        the two must agree exactly.
        """
        scores = [1, 1, 1, 1, 1, 2, 1]

        for index, score in enumerate(scores):
            body = self.seed_overdue_rating_from_new_buyer(index, score)
            self.assertTrue(self.publish_on_expiry(body))
        individually = self.stored_user(SELLER_ID)['rating_average']

        # The second cast, aimed at a different seller, published in ONE
        # transaction by the sweep - which folds the whole group in a
        # single aggregate write.
        seed_user(build_user(OTHER_SELLER_ID, role='seller'))
        for index, score in enumerate(scores):
            rater_id = 'test-buyer-2{0:014d}'.format(index)
            transaction_id = 'test-transaction-2{0:05d}'.format(index)
            seed_user(build_user(rater_id, role='buyer'))
            seed_transaction(build_transaction(
                transaction_id=transaction_id,
                buyer_id=rater_id,
                seller_id=OTHER_SELLER_ID,
            ))
            self.seed_overdue_rating(
                transaction_id=transaction_id,
                rater_id=rater_id,
                ratee_id=OTHER_SELLER_ID,
                score=score,
            )
        with patch.object(settings, 'RATING_WINDOW_DAYS', WINDOW_DAYS):
            published = publish_expired_ratings()
        self.assertEqual(len(published), len(scores))

        in_one_batch = self.stored_user(OTHER_SELLER_ID)['rating_average']
        self.assertEqual(individually, in_one_batch)
        self.assert_aggregate_equals_scores(OTHER_SELLER_ID, scores)

    def test_a_long_sequence_stays_equal_to_its_scores(self):
        """Forty folds, still exactly the mean of what was folded.

        A short sequence can hide a drift that only compounds. The scores
        cycle through the whole scale so the running total is never a
        convenient number, and the assertion is against the source scores
        at every step rather than only at the end.
        """
        scores = []
        for index in range(40):
            score = settings.RATING_MIN + (
                index % (settings.RATING_MAX - settings.RATING_MIN + 1)
            )
            body = self.seed_overdue_rating_from_new_buyer(index, score)
            self.assertTrue(self.publish_on_expiry(body))
            scores.append(score)
            self.assert_aggregate_equals_scores(SELLER_ID, scores)

    def test_a_foreign_rounded_average_is_snapped_and_repaired(self):
        """A stored average this module did not write, handled honestly.

        A hand edit, or a value left behind by an older revision that
        rounded what it stored, cannot describe an integer total: 1.15
        over seven ratings implies 8.05. Publication must not stall on
        that - the ratings are real and the discrepancy is a hundredth of
        a star - so the total is snapped to the nearest whole one, the
        condition is reported for an operator, and the write that follows
        leaves the value exact.
        """
        seed_user(build_user(
            SELLER_ID,
            rating_average=1.15,
            rating_count=7,
        ))
        body = self.seed_overdue_rating(score=5)
        with self.assertLogs('app.services.rating', level='WARNING') as logs:
            self.assertTrue(self.publish_on_expiry(body))
        self.assertTrue(
            any('non-integer score total' in line for line in logs.output),
            logs.output,
        )
        # Snapped to 8, so the fold is (8 + 5) / 8 exactly.
        self.assert_aggregate(SELLER_ID, 13 / 8, 8)
        self.assert_aggregate_equals_scores(
            SELLER_ID, [1, 1, 1, 1, 1, 2, 1, 5]
        )

    def test_a_low_score_counts_exactly_as_a_high_score(self):
        """Sentiment neutrality: no suppression, weighting or floor.

        Two ratees start from the same reputation and receive the
        extreme scores. Both counts move by exactly one, and each
        average is the plain mean of what was folded in, so the 1 is
        counted as fully as the 5. Suppressing or discounting a low
        score is precisely what the FTC rule on consumer reviews
        prohibits, so this is a compliance assertion and not only a
        functional one.
        """
        seed_user(build_user(
            SELLER_ID,
            rating_average=3.0,
            rating_count=2,
        ))
        seed_user(build_user(
            OTHER_SELLER_ID,
            rating_average=3.0,
            rating_count=2,
        ))
        seed_transaction(build_transaction(
            transaction_id=OTHER_TRANSACTION_ID,
            seller_id=OTHER_SELLER_ID,
        ))
        low = self.seed_overdue_rating(score=1)
        high = self.seed_overdue_rating(
            transaction_id=OTHER_TRANSACTION_ID,
            ratee_id=OTHER_SELLER_ID,
            score=5,
        )
        self.assertTrue(self.publish_on_expiry(low))
        self.assertTrue(self.publish_on_expiry(high))
        # (3.0 * 2 + 1) / 3 and (3.0 * 2 + 5) / 3 respectively, stored
        # exactly and equidistant from the base they started at.
        self.assert_aggregate(SELLER_ID, 7 / 3, 3)
        self.assert_aggregate(OTHER_SELLER_ID, 11 / 3, 3)
        self.assertAlmostEqual(
            3.0 - self.stored_user(SELLER_ID)['rating_average'],
            self.stored_user(OTHER_SELLER_ID)['rating_average'] - 3.0,
            places=12,
        )
        self.assertEqual(
            self.stored_user(SELLER_ID)['rating_count'],
            self.stored_user(OTHER_SELLER_ID)['rating_count'],
        )

    def test_the_lowest_score_still_creates_a_reputation(self):
        """A single one-star rating is a reputation of 1.0, not of none.

        The 0 -> 1 transition must not be special-cased away for an
        unflattering score, which is the subtlest form the suppression
        the rule forbids could take.
        """
        body = self.seed_overdue_rating(score=1)
        self.assertTrue(self.publish_on_expiry(body))
        self.assert_aggregate(SELLER_ID, 1.0, 1)

    def test_an_unpublished_rating_defers_its_contribution(self):
        """The aggregate reflects published ratings only, at once.

        A submitted rating is stored, and contributes nothing until it
        is revealed. Asserted immediately after the submission, because
        "eventually correct" is the failure this model exists to avoid.
        """
        rating = submit_rating(self.submission(), self.buyer)
        stored = self.stored_rating(rating.id)
        self.assertIsNotNone(stored)
        self.assertFalse(stored['is_published'])
        self.assertFalse(rating.is_published)
        self.assert_aggregate(SELLER_ID, None, 0)

    def test_a_refused_submission_writes_nothing_at_all(self):
        """Guards run INSIDE the transaction, so a refusal rolls back.

        Nothing reaches the datastore on a refused write - neither the
        rating nor any movement in the ratee's reputation.
        """
        seed_transaction(build_transaction(status=PENDING))
        with self.assertRaises(TransactionNotCompleted):
            submit_rating(self.submission(), self.buyer)
        self.assert_no_rating_stored()
        self.assert_aggregate(SELLER_ID, None, 0)

    @patch('app.services.rating._fold_scores')
    def test_a_failed_aggregate_publishes_nothing(self, fold_scores):
        """Atomicity: the flip and the aggregate commit TOGETHER.

        The publication transaction stages both rating flips first and
        computes the aggregate afterwards, so making the arithmetic fail
        aborts the transaction with the flips already staged. Either
        Firestore rolls all of it back or the reveal is half-applied -
        one reputation moved, the other not, and a rating published
        without being counted, which is the one state that cannot be
        repaired later because the idempotency check would refuse to
        revisit it.

        This is the specific defect the transactions service exhibits
        when it writes a transaction and then updates a vehicle with no
        rollback, and the whole reason this feature has a transactional
        primitive at all.
        """
        fold_scores.side_effect = ValueError('refusing to fold')
        first, second = self.seed_both_sides()
        with self.assertRaises(ValueError):
            publish_if_reciprocal(TRANSACTION_ID)
        for body in (first, second):
            self.assertFalse(
                self.stored_rating(body['id'])['is_published']
            )
        self.assert_aggregate(SELLER_ID, None, 0)
        self.assert_aggregate(BUYER_ID, None, 0)
        self.assertTrue(fold_scores.called)


class TestPublicationModel(RatingFixtureMixin, unittest.TestCase):
    """The double-blind reveal, by both of the paths that trigger it.

    A mutual rating system in which either side can see the other's
    score before committing their own invites review extortion. The
    mitigation implemented here is a blind reveal: neither rating is
    visible, and neither counts, until both have been submitted - or
    until the window closes, so that a counterparty who simply never
    answers cannot suppress a verdict by declining to.

    Correctness must not depend on the worker tier, because nothing
    dispatches it: no task in this codebase is ever sent and no broker is
    configured. The window path is therefore exercised through the
    service function that owns the arithmetic, and the read path is
    asserted to settle what is already due on its own.
    :class:`TestScheduledSweepTask` separately executes the Celery task
    itself, so the scheduled route is covered without anything here
    depending on it.
    """

    def test_reciprocal_submission_publishes_both_sides(self):
        """Both flips and both aggregates apply, or neither does."""
        first, second = self.seed_both_sides(
            buyer_score=4,
            seller_score=2,
        )
        published = publish_if_reciprocal(TRANSACTION_ID)
        self.assertEqual(
            sorted(published),
            sorted([first['id'], second['id']]),
        )
        for body in (first, second):
            self.assertTrue(
                self.stored_rating(body['id'])['is_published']
            )
        self.assert_aggregate(SELLER_ID, 4.0, 1)
        self.assert_aggregate(BUYER_ID, 2.0, 1)

    def test_reciprocal_publication_commits_exactly_once(self):
        """Four writes, ONE transaction - the atomicity, positively.

        A half-applied reveal would show one side's verdict while
        withholding the other's, so the two reputations have to move
        inside the same commit rather than in two that happen to
        succeed.
        """
        self.seed_both_sides()
        attempts = db.commit_attempts
        publish_if_reciprocal(TRANSACTION_ID)
        self.assertEqual(db.commit_attempts, attempts + 1)

    def test_reciprocal_publication_is_idempotent(self):
        """Calling it again counts nothing twice."""
        self.seed_both_sides()
        publish_if_reciprocal(TRANSACTION_ID)
        self.assertEqual(publish_if_reciprocal(TRANSACTION_ID), [])
        self.assert_aggregate(SELLER_ID, 4.0, 1)
        self.assert_aggregate(BUYER_ID, 2.0, 1)

    def test_a_one_sided_pair_publishes_nothing(self):
        """One rating is not a pair, so nothing is revealed."""
        body = seed_rating(build_rating_body(score=4))
        self.assertEqual(publish_if_reciprocal(TRANSACTION_ID), [])
        self.assertFalse(
            self.stored_rating(body['id'])['is_published']
        )
        self.assert_aggregate(SELLER_ID, None, 0)

    def test_the_counterparty_submission_reveals_the_pair(self):
        """The blind reveal end to end, through ``submit_rating``.

        The first vote is stored and invisible and its ratee's
        reputation does not move. The second vote reveals both, and the
        rating returned by the second call reports its STORED
        visibility rather than only this call's own contribution.
        """
        first = submit_rating(self.submission(score=4), self.buyer)
        self.assertFalse(first.is_published)
        self.assert_aggregate(SELLER_ID, None, 0)
        self.assert_aggregate(BUYER_ID, None, 0)
        second = submit_rating(self.submission(score=2), self.seller)
        self.assertTrue(second.is_published)
        self.assertTrue(self.stored_rating(first.id)['is_published'])
        self.assertTrue(self.stored_rating(second.id)['is_published'])
        self.assert_aggregate(SELLER_ID, 4.0, 1)
        self.assert_aggregate(BUYER_ID, 2.0, 1)

    def test_a_rating_inside_its_window_stays_unpublished(self):
        """The near side of the boundary: not due, so not revealed."""
        body = seed_rating(build_rating_body(score=4))
        with patch.object(
            settings, 'RATING_WINDOW_DAYS', WINDOW_DAYS
        ):
            self.assertFalse(publish_if_window_elapsed(body))
        self.assertFalse(
            self.stored_rating(body['id'])['is_published']
        )
        self.assert_aggregate(SELLER_ID, None, 0)

    def test_a_rating_past_its_window_publishes_and_counts(self):
        """The far side of the boundary: due, so revealed and counted.

        Without this path an unanswered rating would stay invisible
        forever, which would hand a non-participating counterparty a
        veto over the verdict against them.
        """
        body = self.seed_overdue_rating(score=3)
        self.assertTrue(self.publish_on_expiry(body))
        self.assertTrue(
            self.stored_rating(body['id'])['is_published']
        )
        self.assert_aggregate(SELLER_ID, 3.0, 1)

    def test_window_publication_is_idempotent(self):
        """Publishing twice must not count the score twice."""
        body = self.seed_overdue_rating(score=3)
        self.assertTrue(self.publish_on_expiry(body))
        self.assertFalse(self.publish_on_expiry(body))
        self.assert_aggregate(SELLER_ID, 3.0, 1)

    def test_window_publication_trusts_only_the_stored_record(self):
        """A stale copy cannot suppress a reveal that is due.

        Only the rating's ID is trusted; publication state is re-read
        under the lock. So a caller holding a record that claims to be
        published already still gets the locked document's answer.
        """
        body = self.seed_overdue_rating(score=3)
        stale = dict(body)
        stale['is_published'] = True
        self.assertTrue(self.publish_on_expiry(stale))
        self.assertTrue(
            self.stored_rating(body['id'])['is_published']
        )
        self.assert_aggregate(SELLER_ID, 3.0, 1)

    @patch('app.services.rating._fold_scores')
    def test_window_publication_rolls_back_as_a_unit(
        self,
        fold_scores,
    ):
        """Atomicity on the window path too, not only the pair path."""
        fold_scores.side_effect = ValueError('refusing to fold')
        body = self.seed_overdue_rating(score=3)
        with self.assertRaises(ValueError):
            self.publish_on_expiry(body)
        self.assertFalse(
            self.stored_rating(body['id'])['is_published']
        )
        self.assert_aggregate(SELLER_ID, None, 0)

    def test_a_reputation_read_settles_what_is_already_due(self):
        """The opportunistic fallback, since no worker will ever run.

        A read about to report a reputation first publishes whatever is
        already overdue, so a rating does not wait indefinitely on a
        scheduled sweep that nothing dispatches. Exercised through the
        service's public read - never through the Celery task.
        """
        body = self.seed_overdue_rating(score=1)
        with patch.object(
            settings, 'RATING_WINDOW_DAYS', WINDOW_DAYS
        ):
            reputation = get_user_reputation(SELLER_ID)
        self.assertEqual(reputation.aggregate.count, 1)
        self.assertEqual(reputation.aggregate.average, 1.0)
        self.assertEqual(len(reputation.items), 1)
        self.assertEqual(reputation.items[0].score, 1)
        self.assertTrue(
            self.stored_rating(body['id'])['is_published']
        )
        self.assert_aggregate(SELLER_ID, 1.0, 1)

    def test_a_reputation_read_hides_a_rating_still_in_window(self):
        """The blind reveal holds on the way out as well as in.

        An unreciprocated rating inside its window is invisible to a
        reader and absent from the aggregate, so the counterparty
        cannot see what was said in time to retaliate.
        """
        seed_rating(build_rating_body(score=4))
        with patch.object(
            settings, 'RATING_WINDOW_DAYS', WINDOW_DAYS
        ):
            reputation = get_user_reputation(SELLER_ID)
        self.assertEqual(reputation.items, [])
        self.assertEqual(reputation.aggregate.count, 0)
        self.assertIsNone(reputation.aggregate.average)

    def test_a_mixed_pair_publishes_only_the_side_still_hidden(self):
        """One side already revealed, the other not - the ragged case.

        Reached whenever the two paths interleave, which is the ordinary
        way a pair ends up mixed: one rating published on window expiry,
        or on a reveal that partially preceded a repair, and its
        counterpart arriving afterwards. It is the state where both
        possible mistakes are available at once - skipping the reveal
        entirely because "one of them is already published", or
        re-publishing the revealed side and counting its score twice -
        so both are asserted against.

        The already-published rating still proves its side arrived,
        which is what the reveal is conditioned on, so it is not
        "missing"; it is simply not outstanding.
        """
        first = seed_rating(build_rating_body(
            score=4,
            is_published=True,
        ))
        # The seller has already been credited for that published
        # rating, exactly as the publishing transaction would have left
        # them, so a second contribution would show up as a count of 2.
        seed_user(build_user(
            SELLER_ID,
            role='seller',
            rating_average=4.0,
            rating_count=1,
        ))
        second = seed_rating(build_rating_body(
            rater_id=SELLER_ID,
            ratee_id=BUYER_ID,
            direction=RatingDirection.SELLER_TO_BUYER.value,
            score=2,
        ))
        published = publish_if_reciprocal(TRANSACTION_ID)
        self.assertEqual(published, [second['id']])
        self.assertTrue(self.stored_rating(second['id'])['is_published'])
        self.assertTrue(self.stored_rating(first['id'])['is_published'])
        # The revealed side is not counted again, and the newly revealed
        # side is counted once.
        self.assert_aggregate(SELLER_ID, 4.0, 1)
        self.assert_aggregate(BUYER_ID, 2.0, 1)
        # And the mixed state settles: a repeat call has nothing left to
        # do and moves neither reputation.
        self.assertEqual(publish_if_reciprocal(TRANSACTION_ID), [])
        self.assert_aggregate(SELLER_ID, 4.0, 1)
        self.assert_aggregate(BUYER_ID, 2.0, 1)

    def test_an_expired_rating_then_a_late_reciprocal_counts_once(self):
        """The two paths in sequence, which must not double count.

        A counterparty who answers AFTER the window has already revealed
        the first rating is a legitimate, expected sequence - the window
        closing does not close the transaction to rating. The risk is
        that the reciprocal reveal then treats the already-published
        rating as outstanding and folds its score into the ratee's
        average a second time, which is unrepairable: the idempotency
        check would refuse to revisit it.
        """
        first = self.seed_overdue_rating(score=4)
        self.assertTrue(self.publish_on_expiry(first))
        self.assert_aggregate(SELLER_ID, 4.0, 1)
        second = submit_rating(self.submission(score=2), self.seller)
        self.assertTrue(second.is_published)
        self.assertTrue(self.stored_rating(first['id'])['is_published'])
        self.assert_aggregate(SELLER_ID, 4.0, 1)
        self.assert_aggregate(BUYER_ID, 2.0, 1)

    def test_a_rating_with_no_creation_time_is_never_due(self):
        """An unstamped rating defers rather than revealing early.

        A document the server has not stamped yet carries no
        ``created_at`` at all, and there is no honest deadline to
        compare against. Reading that as "not yet due" is the safe
        direction - the rating stays private and repairable, where
        reading it as due would reveal a rating whose window may not
        have opened.
        """
        body = build_rating_body(score=3)
        body.pop('created_at')
        seed_rating(body)
        self.assertFalse(self.publish_on_expiry(body))
        self.assertFalse(
            self.stored_rating(body['id'])['is_published']
        )
        self.assert_aggregate(SELLER_ID, None, 0)

    def test_a_rating_with_an_unusable_creation_time_is_never_due(self):
        """A timestamp that is not a point in time is not a deadline.

        A string where a timestamp belongs would raise inside the window
        comparison rather than answering it, so it is classified as
        unusable and declined - the same treatment as an absent one. The
        record needs its timestamp repaired; no reader may guess at it.
        """
        body = build_rating_body(score=3)
        body['created_at'] = 'the day before yesterday'
        seed_rating(body)
        self.assertFalse(self.publish_on_expiry(body))
        self.assertFalse(
            self.stored_rating(body['id'])['is_published']
        )
        self.assert_aggregate(SELLER_ID, None, 0)


class TestPublicationDeadlineBoundary(
    RatingFixtureMixin,
    unittest.TestCase,
):
    """The rating window's edge, asserted AT it rather than around it.

    The window decides when an unanswered rating becomes visible, so its
    boundary is a product decision with a date attached: a rating
    published a day early is a rating the counterparty could still have
    retaliated against, and one published a day late is a verdict
    suppressed by inaction.

    Every case here pins the service's clock. That is not a convenience:
    ``_window_elapsed`` compares ``now >= created_at + window`` against a
    live clock, so the equality case is unreachable from a test that
    computes an instant and then calls - by the time the comparison runs,
    ``now`` has moved past it. An inclusive boundary written as ``>``
    therefore passes every test built on a real clock while withholding
    a rating for an entire extra window, and this is the only shape of
    test that can tell the two apart.
    """

    def seed_at(self, created_at, score=3):
        """Seed one unpublished rating created at a stated instant.

        Args:
            created_at: The rating's creation instant.
            score: The vote.

        Returns:
            The stored document body.
        """
        return seed_rating(build_rating_body(
            score=score,
            created_at=created_at,
        ))

    def test_one_microsecond_before_the_deadline_stays_private(self):
        """The near side of the edge: not due, so not revealed."""
        created = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        body = self.seed_at(created)
        deadline = created + timedelta(days=WINDOW_DAYS)
        with patch.object(
            settings, 'RATING_WINDOW_DAYS', WINDOW_DAYS
        ):
            with frozen_clock(deadline - timedelta(microseconds=1)):
                self.assertFalse(publish_if_window_elapsed(body))
        self.assertFalse(
            self.stored_rating(body['id'])['is_published']
        )
        self.assert_aggregate(SELLER_ID, None, 0)

    def test_exactly_at_the_deadline_publishes(self):
        """The edge itself, which is INCLUSIVE.

        The case a live clock cannot reach, and the one that
        distinguishes ``>=`` from ``>``. Its answer is not arbitrary: the
        window is the period during which a rating stays private, so the
        instant it ends is the instant the rating becomes visible.
        """
        created = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        body = self.seed_at(created, score=3)
        deadline = created + timedelta(days=WINDOW_DAYS)
        with patch.object(
            settings, 'RATING_WINDOW_DAYS', WINDOW_DAYS
        ):
            with frozen_clock(deadline):
                self.assertTrue(publish_if_window_elapsed(body))
        self.assertTrue(
            self.stored_rating(body['id'])['is_published']
        )
        self.assert_aggregate(SELLER_ID, 3.0, 1)

    def test_one_microsecond_after_the_deadline_publishes(self):
        """The far side of the edge: due, so revealed and counted."""
        created = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        body = self.seed_at(created, score=5)
        deadline = created + timedelta(days=WINDOW_DAYS)
        with patch.object(
            settings, 'RATING_WINDOW_DAYS', WINDOW_DAYS
        ):
            with frozen_clock(deadline + timedelta(microseconds=1)):
                self.assertTrue(publish_if_window_elapsed(body))
        self.assertTrue(
            self.stored_rating(body['id'])['is_published']
        )
        self.assert_aggregate(SELLER_ID, 5.0, 1)

    def test_a_zero_day_window_publishes_at_the_creation_instant(self):
        """The degenerate window, which must not become a negative one.

        ``RATING_WINDOW_DAYS`` is a configurable setting, and the
        arithmetic floors it at zero rather than trusting the value, so a
        window of zero means "visible immediately" and cannot be read as
        a deadline in the past that would reject a rating outright.
        """
        created = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        body = self.seed_at(created, score=2)
        with patch.object(settings, 'RATING_WINDOW_DAYS', 0):
            with frozen_clock(created):
                self.assertTrue(publish_if_window_elapsed(body))
        self.assert_aggregate(SELLER_ID, 2.0, 1)


class TestPublicationUnderContention(
    RatingFixtureMixin,
    unittest.TestCase,
):
    """What a concurrent writer does to the aggregate, and must not.

    The aggregate is a running mean maintained incrementally, so it is
    read, folded and written inside one transaction - and two
    transactions crediting the same user at the same instant are the
    textbook lost update: both read a count of N, both write N+1, and one
    score vanishes from a reputation permanently, with nothing anywhere
    reporting it.

    Firestore answers that with optimistic concurrency and the client
    reruns the callback, which is only safe because every publication
    body derives its facts from the locked documents on each attempt
    rather than from anything computed before the transaction opened.
    That safety is what these tests assert, and they assert it by
    ARMING the contention rather than racing for it: a test that had to
    win a race would be a flaky test, and a flaky test of a lost update
    is worse than none.
    """

    def test_contention_is_retried_and_counted_exactly_once(self):
        """A rejected commit is rerun, and the rerun does not double.

        One injected conflict, so the callback runs twice and commits
        once. The aggregate must reflect one contribution, not two -
        which is the assertion that would fail if the body had
        accumulated anything across attempts.
        """
        body = self.seed_overdue_rating(score=4)
        attempts = db.commit_attempts
        db.arm_commit_conflict(1)
        self.assertTrue(self.publish_on_expiry(body))
        self.assertEqual(db.commit_attempts, attempts + 2)
        self.assert_aggregate(SELLER_ID, 4.0, 1)
        self.assertTrue(
            self.stored_rating(body['id'])['is_published']
        )

    def test_a_concurrent_aggregate_change_survives_the_retry(self):
        """The lost update, and the proof it does not happen here.

        Another writer credits the ratee between this transaction's read
        and its commit. The double reaches its own verdict from the
        recorded read versions rather than being told to fail, so the
        commit is rejected exactly as Firestore would reject it, the
        callback reruns, and it re-reads the aggregate the other writer
        left. Both contributions therefore survive: had the transaction
        committed over the change, the concurrent one would have been
        silently erased.
        """
        body = self.seed_overdue_rating(score=4)

        def credit_concurrently(client):
            """Publish an unrelated rating's contribution mid-flight."""
            stored = dict(client.document_body(USERS_COLLECTION,
                                               SELLER_ID))
            stored['rating_average'] = 2.0
            stored['rating_count'] = 1
            client.seed(USERS_COLLECTION, SELLER_ID, stored)

        db.arm_commit_interference(credit_concurrently, 1)
        self.assertTrue(self.publish_on_expiry(body))
        # (2.0 * 1 + 4) / 2 = 3.0 over two ratings. A count of 1 here
        # would mean one of the two scores was lost.
        self.assert_aggregate(SELLER_ID, 3.0, 2)

    def test_contention_outlasting_the_retries_publishes_nothing(self):
        """Giving up must leave the rating due, not half revealed.

        Retries are finite. When they are exhausted the failure is
        reported as contention - which is what a caller needs in order
        to treat it as "try again" rather than as a refusal - and the
        record is exactly as it was, so the next read or the next sweep
        publishes it.
        """
        body = self.seed_overdue_rating(score=4)
        # Exactly enough to use up every attempt the client allows, and
        # no more: an over-armed conflict would survive this call and
        # abort the next transaction in the same test from a line nobody
        # would think to look at.
        db.arm_commit_conflict(MAX_ATTEMPTS)
        with self.assertRaises(Aborted):
            self.publish_on_expiry(body)
        self.assertFalse(
            self.stored_rating(body['id'])['is_published']
        )
        self.assert_aggregate(SELLER_ID, None, 0)


class TestWindowExpirySweep(RatingFixtureMixin, unittest.TestCase):
    """The scheduled sweep: ``publish_expired_ratings``.

    The proactive half of the window path, and the one the read paths
    cannot substitute for. A read publishes what is due for the user
    being read, so a rating aimed at a user whose profile nobody visits
    stays invisible indefinitely - the counterparty's silence becoming
    the veto the window exists to remove. This is the pass that closes
    that gap wholesale.

    Every case here goes through the PUBLIC entry point rather than the
    private walk beneath it, because that is what the Celery task calls
    and therefore what the contract is. The pass is asserted on four
    axes the review found unguarded: that it walks past its first page,
    that its ceiling bounds it, that it settles a group in one
    transaction rather than one per record, and that neither an
    unprovable record nor a transient fault can strand the rest.
    """

    def seed_due_ratings(self, count, score=3, ratee_id=SELLER_ID):
        """Seed ``count`` overdue ratings, one per transaction.

        One transaction each because the deterministic key is
        ``{transaction_id}_{rater_id}``: several ratings from one rater
        on one transaction cannot exist, so a multi-record sweep needs a
        transaction apiece. Every one names the same buyer as rater and
        the same seller as ratee, which is what makes the folding in
        :func:`_apply_publication` observable.

        Args:
            count: How many due ratings to seed.
            score: The vote on each.
            ratee_id: Who they credit.

        Returns:
            The stored rating document IDs, in creation order.
        """
        created = utc_now() - timedelta(days=OVERDUE_DAYS)
        document_ids = []
        for index in range(count):
            transaction_id = 'test-transaction-{0:06d}'.format(index)
            seed_transaction(build_transaction(
                transaction_id=transaction_id,
                seller_id=ratee_id,
            ))
            body = seed_rating(build_rating_body(
                transaction_id=transaction_id,
                ratee_id=ratee_id,
                score=score,
                created_at=created,
            ))
            document_ids.append(body['id'])
        return document_ids

    def sweep(self, limit=None):
        """Run one pass with the window pinned around the call.

        Args:
            limit: Ceiling on documents examined, or ``None`` for the
                service's own default.

        Returns:
            The IDs the pass published.
        """
        with patch.object(
            settings, 'RATING_WINDOW_DAYS', WINDOW_DAYS
        ):
            return publish_expired_ratings(limit)

    def test_the_sweep_publishes_every_due_rating_and_counts_them(self):
        """The ordinary pass: all of them revealed, folded once."""
        document_ids = self.seed_due_ratings(3, score=3)
        published = self.sweep()
        self.assertEqual(sorted(published), sorted(document_ids))
        for document_id in document_ids:
            self.assertTrue(
                self.stored_rating(document_id)['is_published']
            )
        self.assert_aggregate(SELLER_ID, 3.0, 3)

    def test_the_sweep_folds_one_group_into_a_single_commit(self):
        """Three records aimed at one user cost ONE transaction.

        Not a performance assertion - a correctness one. Three separate
        transactions would each read the same starting count of zero and
        each write a count of one, so two of the three scores would be
        lost. Folding them into a single aggregate write is what makes
        the arithmetic right, and the commit tally is how a test can
        tell that it happened.
        """
        self.seed_due_ratings(3, score=3)
        attempts = db.commit_attempts
        self.sweep()
        self.assertEqual(db.commit_attempts, attempts + 1)
        self.assert_aggregate(SELLER_ID, 3.0, 3)

    def test_the_sweep_settles_each_group_in_its_own_commit(self):
        """More records than a batch holds means more than one commit.

        The batch ceiling is patched down rather than five hundred
        records being seeded, so the intent is legible: five due records
        in batches of two is three groups, and three groups is three
        transactions. Each is independent, which is what lets the next
        case lose one without losing the others.
        """
        document_ids = self.seed_due_ratings(5, score=4)
        attempts = db.commit_attempts
        with patch('app.services.rating.SETTLE_BATCH_SIZE', 2):
            published = self.sweep()
        self.assertEqual(sorted(published), sorted(document_ids))
        self.assertEqual(db.commit_attempts, attempts + 3)
        self.assert_aggregate(SELLER_ID, 4.0, 5)

    def test_the_sweep_walks_past_its_first_page(self):
        """A cursor, not a repeated first page.

        A pass that re-read one limited page would publish that page and
        leave everything behind it permanently unreached, because the
        query is not ordered by due-ness: a record beyond the first page
        would starve while its neighbours were re-examined every pass.
        The page size is patched down so five due records span three
        pages, and all five must be published by ONE pass.
        """
        document_ids = self.seed_due_ratings(5, score=2)
        with patch('app.services.rating.DEFAULT_SWEEP_PAGE_SIZE', 2):
            published = self.sweep()
        self.assertEqual(sorted(published), sorted(document_ids))
        self.assert_aggregate(SELLER_ID, 2.0, 5)

    def test_the_sweep_ceiling_bounds_one_pass(self):
        """``limit`` caps documents examined; the rest drain later.

        A backlog must not turn one pass into an unbounded read, and it
        must not be stranded either - so the remainder is still due, and
        a second pass with the same ceiling publishes it.
        """
        document_ids = self.seed_due_ratings(4, score=5)
        first = self.sweep(limit=2)
        self.assertEqual(len(first), 2)
        second = self.sweep(limit=2)
        self.assertEqual(len(second), 2)
        self.assertEqual(
            sorted(first + second),
            sorted(document_ids),
        )
        self.assert_aggregate(SELLER_ID, 5.0, 4)

    def test_a_ceiling_below_one_still_publishes_something(self):
        """A mis-tuned ceiling does less work, never no work.

        Zero and negative values are raised to one rather than refused,
        because a configuration mistake is a reason to slow the sweep
        down and not a reason to stop revealing ratings.
        """
        self.seed_due_ratings(3, score=3)
        self.assertEqual(len(self.sweep(limit=0)), 1)
        self.assertEqual(len(self.sweep(limit=-5)), 1)
        self.assert_aggregate(SELLER_ID, 3.0, 2)

    def test_the_sweep_leaves_a_rating_inside_its_window_alone(self):
        """A candidate that is examined and correctly declined.

        The query asks only for unpublished ratings - filtering on the
        deadline too would need a composite index this project does not
        declare - so every not-yet-due rating IS read and then declined
        here. That is the cost the design accepts, and this is the
        assertion that it is declined rather than revealed.
        """
        body = seed_rating(build_rating_body(score=5))
        self.assertEqual(self.sweep(), [])
        self.assertFalse(
            self.stored_rating(body['id'])['is_published']
        )
        self.assert_aggregate(SELLER_ID, None, 0)

    def test_the_sweep_is_idempotent(self):
        """A published rating leaves the candidate set for good."""
        document_ids = self.seed_due_ratings(2, score=4)
        self.assertEqual(len(self.sweep()), len(document_ids))
        self.assertEqual(self.sweep(), [])
        self.assert_aggregate(SELLER_ID, 4.0, 2)

    def test_an_unprovable_record_costs_only_itself(self):
        """One poison document must not strand its whole group.

        A record whose stored ``ratee_id`` is not the counterparty its
        transaction implies cannot be published - crediting the named
        user would let one altered field redirect a score into any
        reputation - and it will still be unprovable next pass. So it is
        skipped and logged individually, INSIDE the same transaction its
        group commits in, and the rest of the group publishes.
        """
        document_ids = self.seed_due_ratings(2, score=4)
        tampered = dict(self.stored_rating(document_ids[0]))
        tampered['ratee_id'] = OUTSIDER_ID
        seed_rating(tampered)
        published = self.sweep()
        self.assertEqual(published, [document_ids[1]])
        self.assertFalse(
            self.stored_rating(document_ids[0])['is_published']
        )
        self.assertTrue(
            self.stored_rating(document_ids[1])['is_published']
        )
        # Only the provable record moved a reputation, and the
        # tampered-with ratee was never credited at all.
        self.assert_aggregate(SELLER_ID, 4.0, 1)
        self.assertIsNone(self.stored_user(OUTSIDER_ID))

    def test_a_transient_fault_defers_a_group_without_raising(self):
        """A pass absorbs a fault; the records stay due.

        Contention that outlasts the transaction's own retries is not a
        verdict about the records - it is an absence of one - so the
        sweep reports what it managed and leaves the rest exactly as
        they were. Raising instead would abort the pass and strand every
        group behind this one.
        """
        document_ids = self.seed_due_ratings(2, score=4)
        # Exactly the transaction's own attempt budget, so the ONE group
        # this pass forms is exhausted and nothing is left armed to
        # sabotage the second pass below - which is the half of this
        # assertion that proves the records were deferred rather than
        # lost.
        db.arm_commit_conflict(MAX_ATTEMPTS)
        self.assertEqual(self.sweep(), [])
        for document_id in document_ids:
            self.assertFalse(
                self.stored_rating(document_id)['is_published']
            )
        self.assert_aggregate(SELLER_ID, None, 0)
        # Still due, so the very next pass settles them.
        self.assertEqual(len(self.sweep()), len(document_ids))
        self.assert_aggregate(SELLER_ID, 4.0, 2)

    def test_the_sweep_reports_nothing_when_there_is_nothing(self):
        """An empty collection is an empty pass, not a failure."""
        self.assertEqual(self.sweep(), [])


class TestScheduledSweepTask(RatingFixtureMixin, unittest.TestCase):
    """``publish_expired_rating_window``: a delegation, and only that.

    The task is the conventional home for the sweep and owns none of its
    decisions - no query shape, no deadline arithmetic, no error policy.
    An earlier revision enumerated candidates itself with one unbounded
    read and no per-record handling, so this asserts the shape that
    replaced it: the service's public entry point is called, and the
    task's own contribution is to report a COUNT, because a task result
    is stored by a broker and a number is the bounded form of it.

    Correctness of the feature does not depend on this task running -
    nothing dispatches it and no broker is provisioned - which is why
    the sweep's behaviour is asserted directly in the class above and
    only the delegation is asserted here.
    """

    def test_the_task_reports_how_many_ratings_it_published(self):
        """The count, taken from the IDs the service moved."""
        with patch(
            'app.tasks.background_jobs.publish_expired_ratings',
            return_value=['first-rating-id', 'second-rating-id'],
        ) as sweep:
            self.assertEqual(publish_expired_rating_window(), 2)
        sweep.assert_called_once_with()

    def test_the_task_reports_zero_when_nothing_was_due(self):
        """Nothing due is a successful pass reporting zero."""
        with patch(
            'app.tasks.background_jobs.publish_expired_ratings',
            return_value=[],
        ):
            self.assertEqual(publish_expired_rating_window(), 0)

    def test_the_task_publishes_a_due_rating_end_to_end(self):
        """Unpatched, against the datastore double.

        Proves the task and the service are actually wired to each
        other, which the patched cases above deliberately do not: they
        would pass against a task calling something else entirely.
        """
        body = self.seed_overdue_rating(score=4)
        with patch.object(
            settings, 'RATING_WINDOW_DAYS', WINDOW_DAYS
        ):
            self.assertEqual(publish_expired_rating_window(), 1)
        self.assertTrue(
            self.stored_rating(body['id'])['is_published']
        )
        self.assert_aggregate(SELLER_ID, 4.0, 1)

    def test_the_task_is_registered_with_celery(self):
        """It is a task, not merely a function next to some.

        A schedule and a dispatcher address a task by name, so losing
        the decorator would leave a function nothing could ever call
        while every direct-call test above still passed.
        """
        self.assertEqual(
            publish_expired_rating_window.name,
            'app.tasks.background_jobs.publish_expired_rating_window',
        )

    def test_the_task_publishes_due_ratings_and_reports_the_count(self):
        """One pass, driven exactly as a worker would drive it."""
        from app.tasks.background_jobs import publish_expired_rating_window

        first = self.seed_overdue_rating(score=4)
        with patch.object(settings, 'RATING_WINDOW_DAYS', WINDOW_DAYS):
            published = publish_expired_rating_window()

        self.assertEqual(published, 1)
        self.assertTrue(self.stored_rating(first['id'])['is_published'])
        self.assert_aggregate_equals_scores(SELLER_ID, [4])

    def test_the_task_log_line_is_bounded(self):
        """The megabyte log line this replaced.

        A pass may publish up to the service's scan ceiling of thousands
        of ratings, and a rating ID is a composite of two document IDs -
        so joining every ID into one line produced a record that could
        approach a megabyte, slow the pass that emitted it and be
        truncated by whatever collected it. The count is the useful
        figure; the sample is for finding a document by hand.
        """
        from app.tasks import background_jobs

        expected = 3 * background_jobs.SWEEP_LOG_SAMPLE_SIZE
        for index in range(expected):
            rater_id = 'test-rater-{0:016d}'.format(index)
            transaction_id = 'tx-log-{0:06d}'.format(index)
            seed_user(build_user(rater_id, role='buyer'))
            seed_transaction(build_transaction(
                transaction_id=transaction_id,
                buyer_id=rater_id,
            ))
            seed_rating(build_rating_body(
                transaction_id=transaction_id,
                rater_id=rater_id,
                score=4,
                created_at=utc_now() - timedelta(days=OVERDUE_DAYS),
            ))

        with patch.object(settings, 'RATING_WINDOW_DAYS', WINDOW_DAYS):
            with self.assertLogs(
                'app.tasks.background_jobs', level='INFO'
            ) as logs:
                published = background_jobs.publish_expired_rating_window()

        self.assertEqual(published, expected)
        line = '\n'.join(logs.output)
        self.assertIn(str(expected), line)
        named = [
            document_id
            for document_id in db.document_ids(RATINGS_COLLECTION)
            if document_id in line
        ]
        self.assertEqual(
            len(named), background_jobs.SWEEP_LOG_SAMPLE_SIZE
        )
        self.assertIn('(...)', line)
        # Bounded in absolute terms too, not merely relative to the
        # sample: this is the property the old line lacked.
        self.assertLess(len(line), 1000)


class TestPublicationLiveness(RatingFixtureMixin, unittest.TestCase):
    """A bounded settlement pass must still reach every due rating.

    Both settlement walks are CEILINGED, because both can run on a
    request path, so neither can promise to clear an arbitrary backlog in
    one pass. What they must promise is progress: a rating that is
    already due must not be able to sit unpublished for ever while the
    ceiling is spent on other records.

    That property is entirely a function of the ORDER the due set is
    walked in, which is why these tests are about which records a capped
    pass chooses rather than about how many it gets through. Ordered
    newest-first, or by an order unrelated to age, a capped pass can
    re-examine the same records on every run and never reach the one that
    has waited longest - the exact failure both of these classes of test
    reproduce.

    The ceilings are patched down to small numbers so the arrangement
    stays readable; the production values are declared in the service
    beside the walks they bound and are not under test here.
    """

    def seed_aged_rating(self, index, days, ratee_id=SELLER_ID,
                         transaction_prefix='tx'):
        """Seed one unpublished rating of a given age for one ratee.

        Args:
            index: Distinguishes the rater and the transaction.
            days: How long ago it was created. Above ``WINDOW_DAYS`` it
                is due; below, it is not.
            ratee_id: Who the rating is aimed at.
            transaction_prefix: Lets a test control document-ID order
                independently of age, which is what separates
                "ordered by age" from "ordered by name".

        Returns:
            The stored rating body.
        """
        rater_id = 'test-rater-{0:016d}'.format(index)
        transaction_id = '{0}-{1:06d}'.format(transaction_prefix, index)
        seed_user(build_user(rater_id, role='buyer'))
        seed_transaction(build_transaction(
            transaction_id=transaction_id,
            buyer_id=rater_id,
            seller_id=ratee_id,
        ))
        return seed_rating(build_rating_body(
            transaction_id=transaction_id,
            rater_id=rater_id,
            ratee_id=ratee_id,
            score=4,
            created_at=utc_now() - timedelta(days=days),
        ))

    def published_ids(self):
        """Return the IDs of every published rating, from raw state."""
        return set(
            document_id
            for document_id, body
            in db.documents(RATINGS_COLLECTION).items()
            if body.get('is_published') is True
        )

    def test_per_user_settlement_publishes_the_oldest_due_first(self):
        """The starvation this ordering removes, on the read path.

        Five ratings are overdue and the pass may examine two. Ordered
        newest-first - which is what this did - the two youngest are
        settled and the oldest waits; and because a public read settles
        on every request while new ratings keep becoming due, the ceiling
        is refilled from the young end and the oldest can wait for ever.
        Oldest-first inverts it: the pass spends its budget at the front
        of the queue, so the record that has waited longest is always the
        next one published.
        """
        oldest = self.seed_aged_rating(1, OVERDUE_DAYS + 40)
        second = self.seed_aged_rating(2, OVERDUE_DAYS + 30)
        for index, age in ((3, OVERDUE_DAYS + 20), (4, OVERDUE_DAYS + 10)):
            self.seed_aged_rating(index, age)
        youngest = self.seed_aged_rating(5, OVERDUE_DAYS)

        with patch.object(settings, 'RATING_WINDOW_DAYS', WINDOW_DAYS):
            with patch('app.services.rating.SETTLE_SCAN_LIMIT', 2):
                with patch('app.services.rating.SETTLE_BATCH_SIZE', 2):
                    reputation = get_user_reputation(SELLER_ID)

        self.assertEqual(reputation.aggregate.count, 2)
        self.assertEqual(
            self.published_ids(),
            {oldest['id'], second['id']},
        )
        self.assertFalse(
            self.stored_rating(youngest['id'])['is_published']
        )

    def test_sustained_arrivals_cannot_starve_an_old_due_rating(self):
        """Newly due ratings must not push an older one back.

        The realistic version of the failure: ratings keep becoming due
        between reads, so every pass has a fresh supply of young
        candidates. Under a newest-first order the oldest is never
        reached however many times the read runs. Here each pass settles
        the oldest outstanding record, so a bounded number of reads
        clears a backlog that keeps growing at the young end.
        """
        oldest = self.seed_aged_rating(1, OVERDUE_DAYS + 100)
        self.seed_aged_rating(2, OVERDUE_DAYS + 90)

        with patch.object(settings, 'RATING_WINDOW_DAYS', WINDOW_DAYS):
            with patch('app.services.rating.SETTLE_SCAN_LIMIT', 1):
                with patch('app.services.rating.SETTLE_BATCH_SIZE', 1):
                    # Three reads, and between each one another rating
                    # becomes due - always younger than the backlog.
                    for arrival in range(3, 6):
                        get_user_reputation(SELLER_ID)
                        self.seed_aged_rating(arrival, OVERDUE_DAYS)

        self.assertIn(oldest['id'], self.published_ids())
        self.assertTrue(self.stored_rating(oldest['id'])['is_published'])

    def test_the_sweep_reaches_a_due_rating_behind_a_young_prefix(self):
        """The sweep's starvation, and why filtering in the query fixes it.

        The sweep used to ask for unpublished ratings and walk them by
        DOCUMENT NAME, deciding due-ness in Python. Document name has
        nothing to do with age, so a run of ratings still inside their
        window could fill the pass's ceiling and a due rating sorting
        after them was never reached - on this pass or on any other,
        because every pass examined the same prefix. The transaction IDs
        below put the young records first by name and the due one last,
        which is precisely that arrangement.
        """
        for index in range(1, 4):
            self.seed_aged_rating(
                index, 0, transaction_prefix='aaa-tx'
            )
        due = self.seed_aged_rating(
            9, OVERDUE_DAYS, transaction_prefix='zzz-tx'
        )
        self.assertLess(
            min(db.document_ids(RATINGS_COLLECTION)),
            due['id'],
        )

        with patch.object(settings, 'RATING_WINDOW_DAYS', WINDOW_DAYS):
            published = publish_expired_ratings(limit=2)

        self.assertEqual(published, [due['id']])
        self.assertEqual(self.published_ids(), {due['id']})
        self.assert_aggregate_equals_scores(SELLER_ID, [4])

    def test_the_sweep_publishes_the_oldest_due_ratings_first(self):
        """A capped sweep pass spends its budget at the front.

        Three ratings are due and the pass may examine one, so which one
        it picks is the whole behaviour: the oldest, every time.

        The transaction prefixes put document-name order in DIRECT
        OPPOSITION to age order - the oldest rating sorts last by name -
        because a test where the two coincide cannot tell an
        ordered-by-age walk from the ordered-by-name one this replaced.
        """
        oldest = self.seed_aged_rating(
            1, OVERDUE_DAYS + 40, transaction_prefix='zzz-tx'
        )
        middle = self.seed_aged_rating(
            2, OVERDUE_DAYS + 20, transaction_prefix='mmm-tx'
        )
        youngest = self.seed_aged_rating(
            3, OVERDUE_DAYS + 5, transaction_prefix='aaa-tx'
        )

        with patch.object(settings, 'RATING_WINDOW_DAYS', WINDOW_DAYS):
            first_pass = publish_expired_ratings(limit=1)
            second_pass = publish_expired_ratings(limit=1)

        self.assertEqual(first_pass, [oldest['id']])
        self.assertEqual(second_pass, [middle['id']])
        self.assertFalse(
            self.stored_rating(youngest['id'])['is_published']
        )

    def test_sustained_arrivals_cannot_starve_the_sweep(self):
        """The sweep's liveness under a due set that keeps growing.

        The counterpart of the per-user case, and the one a filtered
        query alone does not settle: every record here is genuinely due,
        so the whole question is which of them a capped pass picks. The
        names are arranged against the ages - the oldest rating sorts
        LAST and each arrival sorts FIRST - so a name-ordered walk keeps
        finding a newly arrived record to spend its single unit of budget
        on and never reaches the record that has waited longest, however
        many times it runs.
        """
        oldest = self.seed_aged_rating(
            1, OVERDUE_DAYS + 100, transaction_prefix='zzz-tx'
        )
        with patch.object(settings, 'RATING_WINDOW_DAYS', WINDOW_DAYS):
            for arrival in range(2, 5):
                self.seed_aged_rating(
                    arrival,
                    OVERDUE_DAYS,
                    transaction_prefix='aaa{0}-tx'.format(arrival),
                )
                publish_expired_ratings(limit=1)

        self.assertTrue(self.stored_rating(oldest['id'])['is_published'])

    def test_the_sweep_leaves_an_unstamped_rating_alone(self):
        """A rating with no timestamp is not due, and is not published.

        It is now absent from the sweep's result set rather than read and
        declined, because Firestore omits a document that lacks the
        ordered field. The outcome is the same one every path in this
        module reaches through ``_window_elapsed``: an unusable timestamp
        defers a reveal rather than risking an early one. Recorded as a
        test because the reason changed even though the answer did not.
        """
        body = build_rating_body(score=4)
        body.pop('created_at', None)
        seed_rating(body)

        with patch.object(settings, 'RATING_WINDOW_DAYS', WINDOW_DAYS):
            self.assertEqual(publish_expired_ratings(), [])

        self.assertFalse(self.stored_rating(body['id'])['is_published'])
        self.assert_aggregate(SELLER_ID, None, 0)

    def test_the_sweep_is_idempotent_across_passes(self):
        """A second pass finds nothing and moves nothing.

        Publishing removes a record from the due query permanently, which
        is what makes progress durable without a stored cursor - and what
        stops a repeated pass counting a score twice.
        """
        self.seed_aged_rating(1, OVERDUE_DAYS + 10)
        self.seed_aged_rating(2, OVERDUE_DAYS + 5)

        with patch.object(settings, 'RATING_WINDOW_DAYS', WINDOW_DAYS):
            first_pass = publish_expired_ratings()
            second_pass = publish_expired_ratings()

        self.assertEqual(len(first_pass), 2)
        self.assertEqual(second_pass, [])
        self.assert_aggregate_equals_scores(SELLER_ID, [4, 4])


class TestAggregateOnlyRead(RatingFixtureMixin, unittest.TestCase):
    """The reputation badge's read: cheap, but never stale.

    The badge renders beside every listing, and it needs an average and a
    count. It used to be served by the full user read - one settlement
    pass, then a query returning a page of rating documents with their
    review text, and a walk of as many as ten pages past records
    moderation had withheld in order to fill one - after which the client
    threw all of it away. Against the 200 ms budget this feature is held
    to, that is the difference between one document and hundreds, on the
    most frequent read in the product.

    The saving has an exact boundary, and these tests draw it. What was
    dropped is the LISTING. What was not dropped is SETTLEMENT: an
    aggregate-only mode existed once and was removed precisely because it
    answered from the user document without publishing ratings that were
    already due, which - being the mode the badge used - made the
    most-read reputation the one permitted to sit behind a worker that
    will never run. So the cost assertions here sit beside a settlement
    assertion, and neither is meaningful without the other.

    The cost is measured through the datastore double's round-trip
    ledger, which counts what a production client would have to send.
    Asserting it any other way - patching an internal function and
    checking it was not called - would test which code ran rather than
    what it cost, and would keep passing if the expense moved somewhere
    else.
    """

    def test_the_aggregate_read_costs_one_document_read(self):
        """One settlement query, then one get-by-ID. Nothing else."""
        self.seed_overdue_rating_from_new_buyer(1, 5)
        with patch.object(settings, 'RATING_WINDOW_DAYS', WINDOW_DAYS):
            publish_expired_ratings()
        db.reset_round_trips()

        with patch.object(settings, 'RATING_WINDOW_DAYS', WINDOW_DAYS):
            aggregate = get_user_aggregate(SELLER_ID)

        self.assertEqual(aggregate.count, 1)
        self.assertEqual(aggregate.average, 5.0)
        # The settlement query, which finds nothing due, and the one
        # document the answer is actually read from.
        self.assertEqual(db.round_trips('query', RATINGS_COLLECTION), 1)
        self.assertEqual(db.round_trips('get', USERS_COLLECTION), 1)
        self.assertEqual(db.round_trips('get', RATINGS_COLLECTION), 0)
        self.assertEqual(db.round_trips('write'), 0)
        self.assertEqual(db.round_trips(), 2)

    def test_it_reads_less_than_the_full_reputation_read(self):
        """The comparison that makes the saving a fact rather than a claim.

        Both calls answer with the same aggregate over the same state, so
        the only difference between them is what they had to read to do
        it. Measuring both here means a change that quietly reintroduced
        the listing read fails, whatever it was named.
        """
        for index in range(1, 4):
            self.seed_overdue_rating_from_new_buyer(index, 4)
        with patch.object(settings, 'RATING_WINDOW_DAYS', WINDOW_DAYS):
            publish_expired_ratings()

        db.reset_round_trips()
        with patch.object(settings, 'RATING_WINDOW_DAYS', WINDOW_DAYS):
            aggregate = get_user_aggregate(SELLER_ID)
        cheap = db.round_trips()
        cheap_rating_queries = db.round_trips('query', RATINGS_COLLECTION)

        db.reset_round_trips()
        with patch.object(settings, 'RATING_WINDOW_DAYS', WINDOW_DAYS):
            reputation = get_user_reputation(SELLER_ID)
        full = db.round_trips()
        full_rating_queries = db.round_trips('query', RATINGS_COLLECTION)

        # Same answer, and the aggregate is what the badge renders.
        self.assertEqual(aggregate, reputation.aggregate)
        self.assertEqual(len(reputation.items), 3)
        self.assertLess(cheap, full)
        # The cheap read queries the ratings collection ONCE - to settle -
        # and never to list. The full read queries it again to build the
        # page it returns, which is the read that was being paid for and
        # discarded.
        self.assertEqual(cheap_rating_queries, 1)
        self.assertLess(cheap_rating_queries, full_rating_queries)

    def test_the_aggregate_read_settles_what_is_due(self):
        """Cheap must not mean stale - this is the whole boundary.

        The rating below is unpublished and past its window, so nothing
        has revealed it and nothing ever will unless a read does. The
        aggregate this call reports has to include it, and the stored
        record has to have moved.
        """
        overdue = self.seed_overdue_rating(score=3)

        with patch.object(settings, 'RATING_WINDOW_DAYS', WINDOW_DAYS):
            aggregate = get_user_aggregate(SELLER_ID)

        self.assertEqual(aggregate.count, 1)
        self.assertEqual(aggregate.average, 3.0)
        self.assertTrue(self.stored_rating(overdue['id'])['is_published'])
        self.assert_aggregate_equals_scores(SELLER_ID, [3])

    def test_it_agrees_with_the_full_read_on_an_unsettled_state(self):
        """Two endpoints, one reputation, whoever is asked first.

        A buyer sees the badge and the profile page in the same session,
        and a figure that differed between them would be indistinguishable
        from a wrong figure. Both paths settle the same due set and read
        the same denormalised pair, so the second caller finds nothing
        left to do and reports what the first one revealed.
        """
        self.seed_overdue_rating(score=2)

        with patch.object(settings, 'RATING_WINDOW_DAYS', WINDOW_DAYS):
            first = get_user_aggregate(SELLER_ID)
            second = get_user_reputation(SELLER_ID).aggregate

        self.assertEqual(first, second)
        self.assertEqual(first.count, 1)

    def test_a_rating_still_inside_its_window_is_not_counted(self):
        """The double-blind reveal is not relaxed by the cheap path."""
        pending = seed_rating(build_rating_body(score=5))

        with patch.object(settings, 'RATING_WINDOW_DAYS', WINDOW_DAYS):
            aggregate = get_user_aggregate(SELLER_ID)

        self.assertIsNone(aggregate.average)
        self.assertEqual(aggregate.count, 0)
        self.assertFalse(self.stored_rating(pending['id'])['is_published'])

    def test_an_unrated_user_reports_no_ratings_rather_than_zero(self):
        """"Never rated" is a state, and zero would be a claim.

        The scale's floor is 1, so a zero average is not a low
        reputation - it is not a reputation at all. The badge renders its
        "No ratings yet" state off exactly this null.
        """
        with patch.object(settings, 'RATING_WINDOW_DAYS', WINDOW_DAYS):
            aggregate = get_user_aggregate(SELLER_ID)

        self.assertIsNotNone(aggregate)
        self.assertIsNone(aggregate.average)
        self.assertEqual(aggregate.count, 0)

    def test_an_unknown_user_is_distinguishable_from_an_unrated_one(self):
        """``None`` for no such user, so the router can answer 404."""
        with patch.object(settings, 'RATING_WINDOW_DAYS', WINDOW_DAYS):
            self.assertIsNone(get_user_aggregate('test-absent-000000001'))

    def test_an_unusable_user_id_costs_no_round_trip(self):
        """A malformed ID is a missing user, and is refused before the wire.

        Grammar first, for the reason every read path in this module
        applies it: an ID carrying a slash addresses a different resource
        and one in the reserved ``__…__`` namespace is refused by the
        datastore, so neither is worth a request.
        """
        for user_id in ('', 'has/slash', '__reserved__', '.', 'a' * 200):
            db.reset_round_trips()
            self.assertIsNone(get_user_aggregate(user_id))
            self.assertEqual(db.round_trips(), 0)


class TestOperationCost(RatingFixtureMixin, unittest.TestCase):
    """What each path COSTS, counted rather than claimed.

    Several of this feature's decisions are justified by cost and are
    invisible in a result. A submission that wrote the rating and then
    updated the aggregate in a second operation returns the same object as
    one that commits both together; settling ten ratings for one user one
    transaction at a time reaches the same total as folding them into one
    write. The first difference is atomicity, the second is nine
    superfluous round trips AND a lost-update race between transactions
    that each read the same starting count - and neither is observable
    without counting.

    So the counts are asserted here, off the datastore double's round-trip
    ledger, which records every get, every query evaluation and every
    write batch. The ledger is cleared after the arrangement so a number
    means "this call did N" rather than "this test did N".

    These are upper bounds on work, not a specification of a call
    sequence. A change that legitimately reads one document fewer should
    not fail; a change that turns one batched write into several must.
    """

    def test_a_submission_commits_exactly_one_write_batch(self):
        """The rating and its deferred aggregate are ONE commit.

        The transactions service in this codebase writes a document and
        then updates another in a separate, unrolled-back operation, and
        this feature exists partly to not repeat that. One batch is what
        makes the rating and the reputation it will move either both
        present or both absent.
        """
        db.reset_round_trips()

        rating = submit_rating(self.submission(score=4), self.buyer)

        self.assertIsNotNone(rating)
        self.assertEqual(db.round_trips('write'), 1)
        # Reads are bounded too, and small: the locked rater, the
        # transaction, the target rating and the ratee's document.
        self.assertLessEqual(db.round_trips('get'), 6)
        # ONE query, and only one: the reciprocal check that follows a
        # successful create, which asks whether the counterparty has
        # already rated this transaction. It finds nothing here, so no
        # second commit follows it.
        self.assertLessEqual(db.round_trips('query'), 1)

    def test_a_reciprocal_reveal_is_one_write_batch(self):
        """Two ratings and two aggregates, revealed in one commit.

        A partial reveal is the failure this rules out: one side visible
        and counted while the other is not would show a reader half of a
        double-blind exchange, which is the state the model exists to
        prevent.
        """
        self.seed_both_sides(buyer_score=4, seller_score=2)
        db.reset_round_trips()

        published = publish_if_reciprocal(TRANSACTION_ID)

        self.assertEqual(len(published), 2)
        self.assertEqual(db.round_trips('write'), 1)

    def test_settling_a_group_costs_one_write_for_the_whole_group(self):
        """Five due ratings for one user, one aggregate write.

        This is the assertion behind ``SETTLE_BATCH_SIZE``. Five separate
        transactions would each read the same starting count and each
        write the same document, so four of the five increments could be
        lost - and against a 200 ms budget it is five round trips where
        one will do. The count below is what proves the fold happened.
        """
        for index in range(1, 6):
            self.seed_overdue_rating_from_new_buyer(index, 4)
        db.reset_round_trips()

        with patch.object(settings, 'RATING_WINDOW_DAYS', WINDOW_DAYS):
            published = publish_expired_ratings()

        self.assertEqual(len(published), 5)
        self.assertEqual(db.round_trips('write'), 1)
        self.assert_aggregate_equals_scores(SELLER_ID, [4, 4, 4, 4, 4])

    def test_a_refusal_costs_no_write_at_all(self):
        """A gate that answered after persisting would satisfy the letter
        of its requirement and defeat its purpose. Counted, not inferred
        from an empty collection: a write that was applied and then
        removed would leave the same absence behind.
        """
        seed_user(build_user(BUYER_ID, is_verified=False))
        db.reset_round_trips()

        with self.assertRaises(RaterNotVerified):
            submit_rating(self.submission(), self.buyer)

        self.assertEqual(db.round_trips('write'), 0)
        self.assert_no_rating_stored()

    def test_a_public_read_does_not_grow_a_query_per_rating(self):
        """The listing read is paginated, not one query per record.

        Twelve published ratings, read through the endpoint's own service
        entry point. The point is the SHAPE of the cost: a bounded number
        of queries whatever the history length, rather than a walk that
        issues one round trip per rating.
        """
        for index in range(1, 13):
            self.seed_overdue_rating_from_new_buyer(index, 4)
        with patch.object(settings, 'RATING_WINDOW_DAYS', WINDOW_DAYS):
            publish_expired_ratings()
        db.reset_round_trips()

        with patch.object(settings, 'RATING_WINDOW_DAYS', WINDOW_DAYS):
            reputation = get_user_reputation(SELLER_ID)

        self.assertEqual(len(reputation.items), 12)
        self.assertEqual(reputation.aggregate.count, 12)
        # One settlement query finding nothing due, plus the paginated
        # listing walk. Twelve records fit inside one page.
        self.assertLessEqual(db.round_trips('query'), 3)
        self.assertEqual(db.round_trips('write'), 0)


class TestIndexDeclarations(unittest.TestCase):
    """The declaration file, checked as the deployment input it is.

    Nothing under ``app/`` reads
    ``infrastructure/firestore.indexes.json``: it is consumed by
    ``scripts/deploy.sh``, which derives one gcloud invocation per entry.
    So a query shape whose index is missing, or an exemption that
    silently disappeared, cannot fail anywhere before a deployment - and
    a query needing an undeclared composite index does not run slowly, it
    fails outright with ``FailedPrecondition`` on whatever path issues
    it.

    ``conftest`` already refuses, at runtime, any query shape the file
    does not declare. This class asserts the file's own contents from the
    other direction: that the four shapes this feature issues are
    declared with the DIRECTIONS they are issued in, and that the two
    large never-queried fields are exempt from automatic single-field
    indexing.
    """

    def declaration(self):
        """Return the parsed declaration file.

        Read from disk here rather than through ``conftest``, so this
        asserts the artefact the deployment consumes rather than a
        representation of it.

        Returns:
            The parsed JSON document.
        """
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__)
            ))),
            'infrastructure',
            'firestore.indexes.json',
        )
        with open(path) as handle:
            return json.load(handle)

    def test_every_query_shape_this_feature_issues_is_declared(self):
        """The four composite indexes, with their directions."""
        declared = set()
        for index in self.declaration()['indexes']:
            declared.add((
                index['collectionGroup'],
                tuple(
                    (field['fieldPath'], field['order'])
                    for field in index['fields']
                ),
            ))

        expected = {
            # published ratings received by one user, newest first
            ('ratings', (
                ('ratee_id', 'ASCENDING'),
                ('is_published', 'ASCENDING'),
                ('created_at', 'DESCENDING'),
            )),
            # the due unpublished ratings of one user, OLDEST first
            ('ratings', (
                ('ratee_id', 'ASCENDING'),
                ('is_published', 'ASCENDING'),
                ('created_at', 'ASCENDING'),
            )),
            # every due unpublished rating, OLDEST first - the sweep
            ('ratings', (
                ('is_published', 'ASCENDING'),
                ('created_at', 'ASCENDING'),
            )),
            # one transaction's ratings, ordered by rater
            ('ratings', (
                ('transaction_id', 'ASCENDING'),
                ('rater_id', 'ASCENDING'),
            )),
        }
        self.assertEqual(declared, expected)

    def test_large_unqueried_fields_are_exempt_from_indexing(self):
        """The write cost the four composite indexes are paid for with.

        Firestore indexes every field of every document automatically, in
        both directions, unless told otherwise. ``review`` and
        ``moderation_reason`` are the two large text fields on a rating
        and no query in this feature filters or orders by either, so
        every rating write was paying to maintain index entries nothing
        would ever read - in write latency and in storage, permanently.
        """
        overrides = {
            override['fieldPath']: override
            for override in self.declaration()['fieldOverrides']
        }
        self.assertEqual(
            set(overrides), {'review', 'moderation_reason'}
        )
        for field_path, override in overrides.items():
            self.assertEqual(override['collectionGroup'], 'ratings')
            # An empty list means "no single-field indexes at all", which
            # deploy.sh turns into `--clear-indexes`.
            self.assertEqual(override['indexes'], [])

    def test_an_undeclared_shape_is_refused(self):
        """The three assertions above are only worth their enforcement.

        ``conftest`` refuses a query whose shape no declared index
        serves, and the assertions in this class are meaningful only if
        that refusal actually happens - a guard that accepted everything
        would let the declaration file drift away from the queries while
        every test stayed green. So a shape deliberately absent from the
        file is issued here and the refusal is asserted: two equality
        filters and an ordering on a third field, which is exactly the
        kind of composite requirement Firestore will not serve without a
        declaration.
        """
        query = (
            db.collection(RATINGS_COLLECTION)
            .where(filter=firestore.FieldFilter('ratee_id', '==', SELLER_ID))
            .where(filter=firestore.FieldFilter('rater_id', '==', BUYER_ID))
            .order_by('score', direction=firestore.Query.DESCENDING)
        )
        with self.assertRaises(FailedPrecondition):
            list(query.stream())

    def test_no_queried_field_is_exempted(self):
        """An exemption on a queried field would break that query.

        The same file that removes an index can remove one a query needs,
        and the failure mode is identical to a missing composite index:
        ``FailedPrecondition`` at request time. So the fields this
        feature filters and orders by are asserted NOT to be exempt.
        """
        exempt = set(
            override['fieldPath']
            for override in self.declaration()['fieldOverrides']
        )
        for field_path in (
            'ratee_id',
            'rater_id',
            'transaction_id',
            'is_published',
            'created_at',
        ):
            self.assertNotIn(field_path, exempt)


class TestModerationDisclosure(RatingFixtureMixin, unittest.TestCase):
    """A moderation reason must not be copied into the log.

    The reason is free text a moderator writes ABOUT a review, so it is
    the field most likely to quote the very material the rejection exists
    to withhold - a phone number, an address, a slur, someone's name -
    and the field a moderator is most likely to paste from. A log has a
    different audience, a different retention period and no access
    control of its own, and a line written there survives the reason
    being edited and the rating being deleted.

    What the transition needs to leave behind is which rating moved,
    where it moved to, and whether a justification was recorded. All
    three are non-disclosing. The text itself lives on the document and
    in the admin response, which is where the access control is.
    """

    REASON = 'Contains a phone number: 555-0143, and the seller home town'

    def test_the_moderation_log_line_does_not_quote_the_reason(self):
        """Neither the reason nor any run of words from it appears."""
        body = seed_rating(build_rating_body(
            review='A review that broke a policy.'
        ))

        with self.assertLogs('app.services.rating', level='INFO') as logs:
            moderated = moderate_rating(
                body['id'], ModerationStatus.REJECTED, self.REASON
            )

        self.assertEqual(
            moderated.moderation_status, ModerationStatus.REJECTED
        )
        line = '\n'.join(logs.output)
        self.assertNotIn(self.REASON, line)
        # Not merely the whole string: no three-word run of it either,
        # which is what a truncated or reformatted copy would look like.
        words = self.REASON.split()
        for start in range(len(words) - 2):
            self.assertNotIn(' '.join(words[start:start + 3]), line)
        # And the review body is not quoted either.
        self.assertNotIn(body['review'], line)

    def test_the_moderation_log_line_still_reports_the_transition(self):
        """Non-disclosure must not cost the operator the audit trail.

        The line has to remain useful: the rating it moved, the state it
        moved to, and that a reason exists at all - so a transition can
        be audited and the document found without the log holding the
        text.
        """
        body = seed_rating(build_rating_body(review='Policy violation.'))

        with self.assertLogs('app.services.rating', level='INFO') as logs:
            moderate_rating(
                body['id'], ModerationStatus.REJECTED, self.REASON
            )

        line = '\n'.join(logs.output)
        self.assertIn(body['id'], line)
        self.assertIn(ModerationStatus.REJECTED.value, line)
        self.assertIn('reason_length={0}'.format(len(self.REASON)), line)
        self.assertIn('reason_recorded=True', line)

    def test_a_transition_without_a_reason_reports_none_recorded(self):
        """"No reason recorded" is a different fact and is reported as one.

        Approval carries no reason - the service refuses one on any state
        that displays the review - so the same line has to distinguish
        "none recorded" from "recorded, and withheld from this log".
        """
        body = seed_rating(build_rating_body(review='A fine review.'))

        with self.assertLogs('app.services.rating', level='INFO') as logs:
            moderate_rating(body['id'], ModerationStatus.APPROVED)

        line = '\n'.join(logs.output)
        self.assertIn(body['id'], line)
        self.assertIn(ModerationStatus.APPROVED.value, line)
        self.assertIn('reason_recorded=False', line)
        self.assertIn('reason_length=0', line)


class TestModerationSemantics(RatingFixtureMixin, unittest.TestCase):
    """Moderation is a policy decision, never a sentiment one.

    A transition may only be justified by a policy violation in the
    review CONTENT - abuse, personally identifying information,
    profanity. It may never be driven by the score: a one-star rating is
    not a violation, and withholding one because it is unflattering is
    exactly what the FTC rule on the use of consumer reviews and
    testimonials prohibits. The aggregate therefore keeps counting every
    published rating whatever its value, before and after moderation.

    Reputation records are append-only. The score and the review text
    are never rewritten, so a correction is this state transition
    carrying a recorded reason rather than an edit that erases what was
    said - which matters all the more because this codebase has no audit
    collection for the original text to survive in.
    """

    def publish_low_rating(self, review=None):
        """Publish a one-star rating and return its stored body.

        Args:
            review: Optional free text to store with it.

        Returns:
            The rating body as it was seeded.
        """
        body = self.seed_overdue_rating(score=1, review=review)
        self.assertTrue(self.publish_on_expiry(body))
        return body

    def test_moderation_audit_line_carries_no_free_text(self):
        """The reason is persisted, never logged, and the actor is named.

        A moderation reason describes a policy violation in somebody's
        review, so it can quote abusive wording or the very personally
        identifying information that was the violation - and logs are
        shipped, aggregated and retained well beyond the document. It
        also survives normalisation with its newlines intact, because a
        review is prose, so rendering it into a log line would let a
        stored value forge additional lines.

        So the audit line is asserted twice over: the text must not
        appear in it in any form, and what an auditor actually needs -
        which rating moved to which state, by whom, and whether a
        justification was recorded - must be there.
        """
        body = self.publish_low_rating(review='Call me on 555-0100')
        reason = 'Contains a phone number\nWARNING: forged log line'

        with self.assertLogs('app.services.rating', level='INFO') as logs:
            moderate_rating(
                body['id'],
                ModerationStatus.REJECTED,
                reason,
                actor_id=ADMIN_ID,
            )

        audit = [line for line in logs.output if 'Moderation' in line]
        self.assertEqual(len(audit), 1, logs.output)
        line = audit[0]
        self.assertNotIn('phone number', line)
        self.assertNotIn('forged log line', line)
        self.assertNotIn('555-0100', line)
        # Nothing the moderator wrote reaches the log, so no newline the
        # normaliser preserved can appear in it either.
        self.assertEqual(line.count('\n'), 0)
        self.assertIn(body['id'], line)
        self.assertIn(ModerationStatus.REJECTED.value, line)
        self.assertIn(ADMIN_ID, line)
        self.assertIn('reason_recorded=True', line)
        # The reason itself is on the DOCUMENT, which is the record of it.
        stored = self.stored_rating(body['id'])
        self.assertIn('Contains a phone number', stored['moderation_reason'])

    def test_an_unattributed_moderation_is_logged_as_such(self):
        """A decision with no actor is named as unattributed, not blank.

        The parameter is optional so a caller that is not an HTTP
        request can still moderate, and such a decision must be
        distinguishable in the audit trail rather than silently ascribed
        to nobody.
        """
        body = self.publish_low_rating()

        with self.assertLogs('app.services.rating', level='INFO') as logs:
            moderate_rating(body['id'], ModerationStatus.APPROVED)

        audit = [line for line in logs.output if 'Moderation' in line]
        self.assertEqual(len(audit), 1, logs.output)
        self.assertIn('actor=unattributed', audit[0])
        self.assertIn('reason_recorded=False', audit[0])

    def test_a_rejection_records_its_policy_reason(self):
        """The reason is on the record, so a refusal is accountable."""
        body = self.publish_low_rating(review='Call me on 555-0100')
        result = moderate_rating(
            body['id'],
            ModerationStatus.REJECTED,
            'Contains personally identifying information',
        )
        self.assertEqual(
            result.moderation_status,
            ModerationStatus.REJECTED.value,
        )
        self.assertEqual(
            result.moderation_reason,
            'Contains personally identifying information',
        )
        stored = self.stored_rating(body['id'])
        self.assertEqual(
            stored['moderation_status'],
            ModerationStatus.REJECTED.value,
        )
        self.assertEqual(
            stored['moderation_reason'],
            'Contains personally identifying information',
        )

    def test_moderation_never_rewrites_the_score_or_review(self):
        """Append-only: a correction is a transition, not an edit."""
        body = self.publish_low_rating(review='Call me on 555-0100')
        moderate_rating(
            body['id'],
            ModerationStatus.REJECTED,
            'Contains personally identifying information',
        )
        stored = self.stored_rating(body['id'])
        self.assertEqual(stored['score'], 1)
        self.assertEqual(stored['review'], 'Call me on 555-0100')
        self.assertEqual(stored['rater_id'], BUYER_ID)
        self.assertEqual(stored['ratee_id'], SELLER_ID)
        self.assertTrue(stored['is_published'])

    def test_a_rejection_leaves_the_aggregate_untouched(self):
        """Withholding the words does not withdraw the vote.

        The reputation counts every published rating, so a moderated
        record still accounts for the average a reader is shown. Any
        other behaviour would make moderation a lever on the score,
        which is the suppression the rule forbids.
        """
        body = self.publish_low_rating(review='Call me on 555-0100')
        self.assert_aggregate(SELLER_ID, 1.0, 1)
        moderate_rating(
            body['id'],
            ModerationStatus.REJECTED,
            'Contains personally identifying information',
        )
        self.assert_aggregate(SELLER_ID, 1.0, 1)

    def test_approval_leaves_the_aggregate_untouched(self):
        """Clearing the words does not inflate the vote either."""
        body = self.publish_low_rating(review='Rude and late')
        self.assert_aggregate(SELLER_ID, 1.0, 1)
        result = moderate_rating(body['id'], ModerationStatus.APPROVED)
        self.assertEqual(
            result.moderation_status,
            ModerationStatus.APPROVED.value,
        )
        self.assertIsNone(result.moderation_reason)
        self.assert_aggregate(SELLER_ID, 1.0, 1)

    def test_a_rejection_without_a_reason_is_refused(self):
        """Nothing is withheld first and justified afterwards."""
        body = self.publish_low_rating()
        with self.assertRaises(ValueError):
            moderate_rating(body['id'], ModerationStatus.REJECTED)
        stored = self.stored_rating(body['id'])
        self.assertEqual(
            stored['moderation_status'],
            ModerationStatus.PENDING.value,
        )
        self.assertIsNone(stored['moderation_reason'])

    def test_a_reason_on_a_displaying_state_is_refused(self):
        """A rating that is displayed carries no violation on record."""
        body = self.publish_low_rating()
        with self.assertRaises(ValueError):
            moderate_rating(
                body['id'],
                ModerationStatus.APPROVED,
                'Contains personally identifying information',
            )
        stored = self.stored_rating(body['id'])
        self.assertEqual(
            stored['moderation_status'],
            ModerationStatus.PENDING.value,
        )
        self.assertIsNone(stored['moderation_reason'])

    def test_an_unrecognised_moderation_state_is_refused(self):
        """An unknown state would disable every check that reads it.

        ``'low_score'`` is used deliberately: a state named after a
        score is the shape a sentiment-driven policy would take, and
        there is nowhere for it to be written.
        """
        body = self.publish_low_rating()
        with self.assertRaises(ValueError):
            moderate_rating(body['id'], 'low_score')
        self.assertEqual(
            self.stored_rating(body['id'])['moderation_status'],
            ModerationStatus.PENDING.value,
        )

    def test_moderating_an_unknown_rating_reports_nothing(self):
        """No rating at that ID means no decision and no write."""
        result = moderate_rating(
            rating_document_id(TRANSACTION_ID, BUYER_ID),
            ModerationStatus.APPROVED,
        )
        self.assertIsNone(result)
        self.assert_no_rating_stored()


class TestDatastoreDoubleFidelity(unittest.TestCase):
    """Prove the datastore double before trusting anything above it.

    Two claims in this module rest entirely on the in-memory datastore
    behaving the way Firestore does. Uniqueness is enforced by
    create-only semantics, so a second create at a taken ID has to be
    REJECTED rather than quietly overwriting. Atomicity is enforced by
    transaction rollback, so a callback that writes and then fails has
    to leave nothing visible.

    If either of those did not hold, every uniqueness and atomicity
    assertion in the classes above would pass while proving nothing at
    all - so they are asserted here directly rather than assumed. No
    seeding is needed, so this class arranges nothing; it only clears the
    shared double, for the same reason the mixin above does.
    """

    def setUp(self):
        """Clear the datastore double before each fidelity check."""
        db.reset()
        reset_server_timestamps()

    def test_a_second_create_at_the_same_id_is_rejected(self):
        """The collision, raised by the create-only primitive itself.

        ``create_document_with_id`` deliberately does not catch this:
        the collision is the signal its callers need, and swallowing it
        would report a rejected duplicate as a success.
        """
        body = build_rating_body()
        create_document_with_id(
            RATINGS_COLLECTION,
            body['id'],
            body,
        )
        with self.assertRaises(AlreadyExists):
            create_document_with_id(
                RATINGS_COLLECTION,
                body['id'],
                dict(body, score=1),
            )
        self.assertEqual(db.count(RATINGS_COLLECTION), 1)
        stored = db.document_body(RATINGS_COLLECTION, body['id'])
        self.assertEqual(stored['score'], body['score'])

    def test_every_data_layer_helper_reaches_the_double(self):
        """Signature parity, exercised rather than eyeballed.

        Every helper in ``app/db/firestore.py`` passes the per-operation
        ``retry``/``timeout`` pair, so a double whose method signatures
        drifted from production's would make the suite refuse exactly
        the calls production makes - and ``delete`` was that case: it
        took neither argument while ``delete_document`` passed both.

        Driving the real helpers through the double is what keeps that
        from recurring, because the arguments come from production code
        rather than from the test's idea of them.
        """
        collection = 'adhoc_fidelity'
        document_id = create_document(collection, {'score': 4})
        self.assertEqual(
            get_document(collection, document_id),
            {'score': 4},
        )
        self.assertTrue(
            update_document(collection, document_id, {'score': 5})
        )
        self.assertEqual(
            query_documents(collection, {'score': 5}),
            [{'score': 5}],
        )
        self.assertTrue(delete_document(collection, document_id))
        self.assertIsNone(get_document(collection, document_id))
        self.assertEqual(db.count(collection), 0)

    def test_a_failed_transaction_leaves_no_partial_write(self):
        """Rollback, proved with a write already staged when it fails.

        The write is issued BEFORE the failure, so a datastore that
        applied writes as they arrived would leave the document behind
        and this assertion would catch it.
        """
        def write_then_fail(transaction):
            """Stage one write, then refuse the whole transaction."""
            body = build_rating_body()
            reference = db.collection(RATINGS_COLLECTION).document(
                body['id']
            )
            transaction.set(reference, body)
            raise RuntimeError('refused after staging a write')

        with self.assertRaises(RuntimeError):
            run_in_transaction(write_then_fail)
        self.assertEqual(db.count(RATINGS_COLLECTION), 0)

    def test_the_transaction_receives_itself_first_positionally(self):
        """``run_in_transaction`` calls ``fn(transaction, *args)``.

        Matching the real signature matters because every publication
        body in the service relies on it, so a double that passed the
        transaction some other way would let those bodies pass here and
        fail in production.
        """
        seen = {}

        def record(transaction, first, second=None):
            """Record what the runner passed, then write nothing."""
            seen['transaction'] = transaction
            seen['first'] = first
            seen['second'] = second
            return 'committed'

        result = run_in_transaction(record, 'alpha', second='beta')
        self.assertEqual(result, 'committed')
        self.assertIsNotNone(seen['transaction'])
        for operation in ('get', 'set', 'update', 'create'):
            self.assertTrue(hasattr(seen['transaction'], operation))
        self.assertEqual(seen['first'], 'alpha')
        self.assertEqual(seen['second'], 'beta')
        self.assertEqual(db.count(RATINGS_COLLECTION), 0)


class FailingFirestoreApi:
    """A generated-client stub that fails every RPC, and counts them.

    The in-memory datastore double cannot express an outage: it performs
    no I/O, so it has no deadline to exceed and no transient fault to
    retry. Availability behaviour is therefore proved here instead, at
    the only layer where it lives - ``app/db/firestore.py``'s bounded
    wrapper - with a stub standing in for
    ``Client._firestore_api``.

    Every method raises the condition the pinned client retries
    endlessly on its own (``ServiceUnavailable``), so a wrapper that did
    not bound its calls would never return.
    """

    def __init__(self, error=None):
        """Record the fault to raise and start the call counters.

        Args:
            error: Exception to raise from every RPC. Defaults to
                ``ServiceUnavailable``, which is the condition
                ``_commit_with_retry`` loops on forever.
        """
        self.error = error or ServiceUnavailable('datastore unreachable')
        self.calls = {'begin_transaction': 0, 'commit': 0, 'rollback': 0}

    def begin_transaction(self, request=None, metadata=None, **kwargs):
        """Fail a ``BeginTransaction``, counting the attempt."""
        return self._fail('begin_transaction')

    def commit(self, request=None, metadata=None, **kwargs):
        """Fail a ``Commit``, counting the attempt."""
        return self._fail('commit')

    def rollback(self, request=None, metadata=None, **kwargs):
        """Fail a ``Rollback``, counting the attempt."""
        return self._fail('rollback')

    def _fail(self, name):
        """Count one attempt at ``name`` and raise the stored fault."""
        self.calls[name] += 1
        raise self.error


class RecordingFirestoreApi:
    """A generated-client stub that succeeds and records requests.

    The healthy counterpart of :class:`FailingFirestoreApi`, used to
    prove the bounded overrides still issue the SAME requests the base
    class issues - the same transaction ID, the same staged writes - so
    the deadline is the only thing they add.
    """

    def __init__(self, transaction_id=b'transaction-id'):
        """Start empty, answering begins with ``transaction_id``."""
        self.transaction_id = transaction_id
        self.requests = {}

    def begin_transaction(self, request=None, metadata=None, **kwargs):
        """Answer a begin, recording the request and the call kwargs."""
        self.requests['begin_transaction'] = (request, kwargs)
        return SimpleNamespace(transaction=self.transaction_id)

    def commit(self, request=None, metadata=None, **kwargs):
        """Answer a commit with one write result per staged write."""
        self.requests['commit'] = (request, kwargs)
        results = [
            SimpleNamespace(update_time=None)
            for _ in request.get('writes') or ()
        ]
        return SimpleNamespace(write_results=results)

    def rollback(self, request=None, metadata=None, **kwargs):
        """Answer a rollback, recording the request."""
        self.requests['rollback'] = (request, kwargs)
        return None


class StubFirestoreClient:
    """The minimum of a Firestore client a transaction object touches.

    ``Transaction`` reaches for exactly three attributes of its client
    when issuing its own RPCs, so a stub carrying those three is enough
    to drive :class:`app.db.firestore._BoundedTransaction` without a
    project, a channel or a credential.
    """

    def __init__(self, firestore_api):
        """Bind the generated-client stub the transaction will call."""
        self._firestore_api = firestore_api
        self._database_string = 'projects/test/databases/(default)'
        self._rpc_metadata = ()

    def transaction(self, **kwargs):
        """Hand back a REAL transaction, as the real client does.

        This is what lets a test stand in for the client while still
        exercising ``_new_transaction``'s substitution: the object it
        inspects has to be the pinned client's own ``Transaction`` for
        the bounded subclass to be chosen.
        """
        return FirestoreTransaction(self, **kwargs)


class BoundedDatastoreCallTests(unittest.TestCase):
    """Prove one datastore operation cannot outlive its budget.

    ``datastore_call()`` exists because a shared retry policy cannot
    bound an operation: the pinned client's ``Query.stream`` resumes a
    failed stream in a ``while True`` loop, starting a FRESH request -
    and therefore a fresh per-attempt deadline - each time round, and it
    decides whether to go round again by asking ``retry._predicate``.
    So the predicate is the only place an overall bound can live, and
    these tests assert it holds there.

    Every handler in this application is synchronous, so an unbounded
    loop holds one of Starlette's threadpool threads for the duration of
    an outage. That is why this is a correctness test rather than a
    performance one.
    """

    def test_a_transient_fault_is_retried_while_budget_remains(self):
        """The predicate agrees with a retry inside the budget."""
        call = datastore_call()
        self.assertTrue(
            call['retry']._predicate(ServiceUnavailable('blip'))
        )
        self.assertEqual(call['timeout'], DATASTORE_TIMEOUT_SECONDS)

    def test_a_permanent_fault_is_never_retried(self):
        """A fault that will not fix itself is refused immediately.

        ``InvalidArgument`` is a defect in the request rather than a
        condition of the datastore, and ``Aborted`` is contention that
        the transaction runner reruns whole rather than retrying one
        call inside it.
        """
        call = datastore_call()
        for error in (
            InvalidArgument('bad request'),
            Aborted('contention'),
        ):
            self.assertFalse(call['retry']._predicate(error))

    def test_the_budget_expiring_ends_the_stream_resume_loop(self):
        """Once time is up, even a transient fault reports as final.

        This IS the bound on ``Query.stream``'s resume loop: that loop
        continues only while this predicate answers ``True``.
        """
        with patch('app.db.firestore.DATASTORE_TIMEOUT_SECONDS', 0.05):
            call = datastore_call()
            self.assertTrue(
                call['retry']._predicate(ServiceUnavailable('blip'))
            )
            time.sleep(0.06)
            self.assertFalse(
                call['retry']._predicate(ServiceUnavailable('blip'))
            )

    def test_every_operation_receives_its_own_budget(self):
        """A spent budget cannot be inherited by the next operation.

        The reason this is a function rather than a module-level
        constant: a shared policy carries whichever deadline was set
        first, so the second operation would start already exhausted.
        """
        with patch('app.db.firestore.DATASTORE_TIMEOUT_SECONDS', 0.05):
            first = datastore_call()
            time.sleep(0.06)
            second = datastore_call()
            self.assertFalse(
                first['retry']._predicate(ServiceUnavailable('blip'))
            )
            self.assertTrue(
                second['retry']._predicate(ServiceUnavailable('blip'))
            )

    def test_a_permanently_failing_call_gives_up_in_finite_time(self):
        """The policy applied to a call that never succeeds terminates.

        Exhaustion arrives as ``RetryError``, which
        ``DATASTORE_UNAVAILABLE_ERRORS`` classifies as transient, so the
        HTTP layers answer 503 rather than hanging or 500-ing.
        """
        attempts = {'count': 0}

        def always_unavailable():
            """Fail the way an unreachable datastore fails."""
            attempts['count'] += 1
            raise ServiceUnavailable('datastore unreachable')

        with patch('app.db.firestore.DATASTORE_TIMEOUT_SECONDS', 0.4):
            call = datastore_call()
            started = time.monotonic()
            with self.assertRaises(RetryError) as caught:
                call['retry'](always_unavailable)()
            elapsed = time.monotonic() - started

        self.assertLess(elapsed, 5)
        self.assertGreaterEqual(attempts['count'], 2)
        self.assertLess(attempts['count'], 50)
        self.assertIsInstance(caught.exception, DATASTORE_UNAVAILABLE_ERRORS)


class BoundedTransactionTests(unittest.TestCase):
    """Prove Begin, Commit and Rollback cannot hang.

    On the pinned client these three are the unbounded RPCs: ``Commit``
    runs through a ``while True`` loop with no attempt ceiling and no
    deadline, and ``BeginTransaction`` and ``Rollback`` are issued with
    no retry and no timeout at all.
    ``app.db.firestore._BoundedTransaction`` reissues each of them with
    an explicit policy, and these tests assert that a datastore which
    fails every one of them produces an exception in a bounded time
    instead of a thread that never returns.

    A failure here would not look like a failure in production - it
    would look like a hang - which is why the assertions are on elapsed
    time and attempt counts rather than on a return value.
    """

    def _failing_transaction(self, read_only=False, error=None):
        """Build a bounded transaction over a stub that always fails.

        Args:
            read_only: Open it read-only, which is the mode the public
                reputation read uses.
            error: Fault every RPC should raise.

        Returns:
            ``(transaction, api)`` so a test can assert on the stub's
            recorded attempt counts.
        """
        api = FailingFirestoreApi(error=error)
        transaction = _BoundedTransaction(
            StubFirestoreClient(api),
            read_only=read_only,
        )
        return transaction, api

    def test_begin_gives_up_within_its_deadline(self):
        """An unreachable datastore refuses a begin, and says so."""
        transaction, api = self._failing_transaction()
        with patch('app.db.firestore.DATASTORE_TIMEOUT_SECONDS', 0.3):
            started = time.monotonic()
            with self.assertRaises(DATASTORE_UNAVAILABLE_ERRORS):
                transaction._begin()
            elapsed = time.monotonic() - started
        self.assertLess(elapsed, 5)
        self.assertGreaterEqual(api.calls['begin_transaction'], 1)
        self.assertLess(api.calls['begin_transaction'], 50)
        self.assertFalse(transaction.in_progress)

    def test_commit_gives_up_instead_of_retrying_forever(self):
        """The unbounded loop this override exists to replace.

        The base class's ``_commit_with_retry`` would still be inside
        ``while True`` when this assertion timed out.
        """
        transaction, api = self._failing_transaction()
        transaction._id = b'transaction-id'
        with patch('app.db.firestore.DATASTORE_TIMEOUT_SECONDS', 0.3):
            started = time.monotonic()
            with self.assertRaises(DATASTORE_UNAVAILABLE_ERRORS):
                transaction._commit()
            elapsed = time.monotonic() - started
        self.assertLess(elapsed, 5)
        self.assertGreaterEqual(api.calls['commit'], 1)
        self.assertLess(api.calls['commit'], 50)

    def test_rollback_gives_up_within_its_deadline(self):
        """A bounded rollback, because the runner always calls it.

        The runner rolls back on ANY error, so an unbounded rollback
        would hand back the hang a bounded commit just prevented.
        """
        transaction, api = self._failing_transaction()
        transaction._id = b'transaction-id'
        with patch(
            'app.db.firestore.DATASTORE_ROLLBACK_TIMEOUT_SECONDS', 0.3
        ):
            started = time.monotonic()
            with self.assertRaises(DATASTORE_UNAVAILABLE_ERRORS):
                transaction._rollback()
            elapsed = time.monotonic() - started
        self.assertLess(elapsed, 5)
        self.assertGreaterEqual(api.calls['rollback'], 1)
        # Local state is released even though the RPC failed, so the
        # object cannot be left claiming a transaction that is gone.
        self.assertFalse(transaction.in_progress)

    def test_rollback_of_a_transaction_that_never_began_is_quiet(self):
        """The repair that keeps an outage classifiable as one.

        The runner rolls back inside ``except BaseException``, so when
        it is the BEGIN that failed, an exception raised here would
        REPLACE the transient fault that caused it - which is what made
        an unreachable datastore surface as a bare ``ValueError``, and
        therefore as a 500 rather than the 503 the HTTP layers give
        every other transient provider fault.
        """
        transaction, api = self._failing_transaction()
        self.assertIsNone(transaction._rollback())
        self.assertEqual(api.calls['rollback'], 0)

    def test_an_outage_during_begin_stays_a_transient_fault(self):
        """End to end through the runner, which is where it broke.

        ``run_in_transaction`` must report an unreachable datastore as
        the transient fault it is, so the router can answer 503. The
        callable is never entered, and nothing is written.
        """
        api = FailingFirestoreApi()
        client = StubFirestoreClient(api)
        entered = {'body': False}

        def body(transaction):
            """Record that the callable ran, which it must not."""
            entered['body'] = True
            return 'committed'

        with patch('app.db.firestore.db', client):
            with patch(
                'app.db.firestore.DATASTORE_TIMEOUT_SECONDS', 0.3
            ):
                started = time.monotonic()
                with self.assertRaises(DATASTORE_UNAVAILABLE_ERRORS):
                    run_in_transaction(body)
                elapsed = time.monotonic() - started

        self.assertLess(elapsed, 5)
        self.assertFalse(entered['body'])
        self.assertGreaterEqual(api.calls['begin_transaction'], 1)

    def test_a_healthy_transaction_issues_the_same_requests(self):
        """The deadline is all the overrides add.

        Same database, same transaction ID, same staged writes - with
        ``retry`` and ``timeout`` now supplied on every one of the three
        calls the base class left unbounded.
        """
        api = RecordingFirestoreApi()
        transaction = _BoundedTransaction(StubFirestoreClient(api))
        transaction._begin()
        self.assertTrue(transaction.in_progress)
        self.assertEqual(transaction._id, api.transaction_id)

        write = object()
        transaction._write_pbs.append(write)
        results = transaction._commit()

        request, kwargs = api.requests['commit']
        self.assertEqual(request['transaction'], api.transaction_id)
        self.assertEqual(request['writes'], [write])
        self.assertEqual(len(results), 1)
        self.assertFalse(transaction.in_progress)
        for name in ('begin_transaction', 'commit'):
            _, call_kwargs = api.requests[name]
            self.assertIn('retry', call_kwargs)
            self.assertIn('timeout', call_kwargs)

    def test_a_lifecycle_violation_is_still_refused(self):
        """The base class's own preconditions are preserved."""
        api = RecordingFirestoreApi()
        transaction = _BoundedTransaction(StubFirestoreClient(api))
        with self.assertRaises(ValueError):
            transaction._commit()
        transaction._begin()
        with self.assertRaises(ValueError):
            transaction._begin()
        self.assertNotIn('commit', api.requests)

    def test_a_real_client_transaction_is_replaced_by_the_bounded_one(
        self,
    ):
        """The selection that makes the bound reach production.

        A real ``Transaction`` is substituted; anything else - the
        in-memory double, which carries its own commit and rollback
        semantics - is handed back untouched.
        """
        client = StubFirestoreClient(RecordingFirestoreApi())
        self.assertIsInstance(client.transaction(), FirestoreTransaction)
        with patch('app.db.firestore.db', client):
            self.assertIsInstance(_new_transaction(), _BoundedTransaction)
            self.assertIsInstance(
                _new_transaction(read_only=True),
                _BoundedTransaction,
            )

        # The unpatched case: the suite's own datastore double.
        self.assertNotIsInstance(_new_transaction(), _BoundedTransaction)
        self.assertIs(type(_new_transaction()), type(db.transaction()))


if __name__ == '__main__':
    unittest.main()
