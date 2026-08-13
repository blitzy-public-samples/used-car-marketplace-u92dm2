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

``conftest.py`` is loaded implicitly by pytest and is deliberately NOT
imported here. What it supplies is consumed through the application's
own symbols instead: ``app.db.firestore.db`` IS the in-memory
datastore double it installs, and its autouse fixture clears that
store and restores the rating tunables around every test.
``app.tasks.background_jobs`` is never imported - it cannot be, on the
pinned configuration - so the window-expiry path is exercised through
``publish_if_window_elapsed``, which owns that arithmetic anyway.

Pydantic v1 semantics throughout, matching the pin in
``backend/requirements.txt``.
"""
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from google.api_core.exceptions import AlreadyExists
from pydantic import ValidationError

from app.core.config import settings
from app.db.firestore import (
    create_document_with_id,
    db,
    run_in_transaction,
)
from app.schema.rating import (
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
    DuplicateRating,
    NotATransactionParticipant,
    RaterNotVerified,
    SelfRatingNotAllowed,
    TransactionInvariantError,
    TransactionNotCompleted,
    TransactionNotFound,
    evaluate_eligibility,
    get_user_reputation,
    moderate_rating,
    publish_if_reciprocal,
    publish_if_window_elapsed,
    submit_rating,
)

# Identities are spelled out rather than generated, so a failure names
# the party it concerns. Each one satisfies the document-ID grammar
# that ``app/schema/user.py`` and ``app/schema/rating.py`` enforce, so
# a fixture can never be refused for a reason no test intended.
BUYER_ID = 'test-buyer-000000000001'
SELLER_ID = 'test-seller-00000000001'
OTHER_SELLER_ID = 'test-seller-00000000002'
OUTSIDER_ID = 'test-outsider-000000001'
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


def rating_document_id(transaction_id, rater_id):
    """Compose the deterministic rating key.

    Spelled out here rather than imported from the service, so the
    assertions about the key are INDEPENDENT of the implementation they
    are checking. The key is the natural key itself, which is what
    turns document-ID collision into the one-vote-per-transaction rule.

    Args:
        transaction_id: Transaction the rating belongs to.
        rater_id: Party submitting the rating.

    Returns:
        ``"{transaction_id}_{rater_id}"``.
    """
    return '{0}_{1}'.format(transaction_id, rater_id)


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
        """Seed the eligible cast for one test."""
        super().setUp()
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

        Args:
            user_id: The rated user.
            average: Expected ``rating_average``.
            count: Expected ``rating_count``.
        """
        body = self.stored_user(user_id)
        self.assertIsNotNone(body)
        self.assertEqual(body.get('rating_average'), average)
        self.assertEqual(body.get('rating_count'), count)


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

    def test_a_subsequent_rating_folds_a_running_mean(self):
        """The incremental mean, rounded to two decimals."""
        seed_user(build_user(
            SELLER_ID,
            rating_average=4.0,
            rating_count=2,
        ))
        body = self.seed_overdue_rating(score=5)
        self.assertTrue(self.publish_on_expiry(body))
        # (4.0 * 2 + 5) / 3 = 4.333..., which must be stored as 4.33.
        self.assert_aggregate(SELLER_ID, 4.33, 3)

    def test_the_running_mean_rounds_rather_than_truncates(self):
        """A value that must round UP, so truncation would be caught.

        Chosen deliberately over a value that divides evenly: an
        arithmetic bug in the rounding step is invisible to a case whose
        exact answer already has two decimals.
        """
        seed_user(build_user(
            SELLER_ID,
            rating_average=4.33,
            rating_count=3,
        ))
        body = self.seed_overdue_rating(score=5)
        self.assertTrue(self.publish_on_expiry(body))
        # (4.33 * 3 + 5) / 4 = 4.4975, which rounds to 4.5 - not 4.49.
        self.assert_aggregate(SELLER_ID, 4.5, 4)

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
        # (3.0 * 2 + 1) / 3 and (3.0 * 2 + 5) / 3 respectively.
        self.assert_aggregate(SELLER_ID, 2.33, 3)
        self.assert_aggregate(OTHER_SELLER_ID, 3.67, 3)
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

    Correctness must not depend on the worker tier, because there is
    none: no task in this codebase is ever dispatched and no broker is
    configured. The window path is therefore exercised through the
    service function that owns the arithmetic, and the read path is
    asserted to settle what is already due on its own - never through
    the Celery task, which cannot even be imported here.
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
    seeding is needed, which is why this class takes no fixtures: the
    autouse reset supplied by ``conftest.py`` already hands it an empty
    store.
    """

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


if __name__ == '__main__':
    unittest.main()
