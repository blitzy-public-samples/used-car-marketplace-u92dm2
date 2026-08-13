"""Router-level acceptance tests for the peer rating API. F-010.

This module is the HTTP acceptance gate for the bidirectional peer
rating feature. Its sibling ``test_rating_service.py`` covers the
service internals; everything here goes through the real FastAPI
application, over the real dependency chain, at the real resolved
paths.

THE TWO PRIMARY GATES
-----------------------------------------------------------------------
Two tests in this module carry the user's two must-requirements and are
non-negotiable:

* ``test_submit_rating_unverified_rater_forbidden`` - R1, only a
  verified user may rate.
* ``test_submit_rating_non_participant_forbidden`` - R2, only a
  counterparty of the same transaction may rate.

Both live in :class:`TestRatingSubmissionGates`.

Both assert BOTH halves: the 403 AND that no rating document exists
afterwards. The second half is not belt-and-braces. A gate that
answered 403 after persisting would satisfy the letter of the
requirement and defeat its purpose, and a status code alone cannot tell
those two implementations apart. Every other refusal in this module is
held to the same standard, and the duplicate case additionally asserts
that the ratee's aggregate did not move.

Both are also written so that DELETING the guard turns them red. The
unverified caller is the buyer of the transaction it cites - so it
passes every other guard, and removing the verification check makes the
request succeed - and the non-participant is verified, so removing the
participant check does the same. A negative test whose subject fails
two guards at once still passes with one of them deleted, which is a
false positive on the very requirement it exists to protect.

WHY THE REFUSALS ARE MATCHED AGAINST THE EXCEPTION'S OWN MESSAGE
-----------------------------------------------------------------------
R1 and R2 both answer 403, so a test asserting only the status cannot
say which gate refused the request. Each assertion therefore compares
``detail`` with the domain exception's own ``message`` attribute, which
is what the router publishes it from. That pins the CONTRACT - this
refusal came from this guard - without pinning prose: rewording a
message moves both sides at once, while swapping one guard's refusal
for another's is caught immediately.

HOW THE APPLICATION IS WIRED HERE
-----------------------------------------------------------------------
``TestClient(app)`` is constructed in ``setUp`` and used WITHOUT a
context manager, matching ``test_api.py:L9-L11``. That detail is
load-bearing rather than stylistic: Starlette fires lifespan events only
on ``__enter__``, and ``app/main.py`` registers a startup hook that
awaits ``initialize_db()``. Entering the client would make every test
here depend on that initializer, which has nothing to do with the
surface under test.

Callers authenticate with REAL bearer tokens, minted by
``conftest.auth_headers`` and resolved by the application's own
``get_current_user``. Overriding that dependency would have been
simpler and is deliberately not the default, because the whole R1
mechanism is that ``get_current_user`` re-reads the caller's document on
every request: an overridden dependency proves the guard reads
``is_verified`` off some object, whereas a real token proves it reads
the STORED flag - which is why
``test_submit_rating_after_verification_revoked_forbidden`` can
revoke verification behind an already-issued token and see the
next request refused.

``app.dependency_overrides`` is still used for the one caller a stored
document cannot express - an authenticated identity with no user
document at all - and :meth:`RatingAPITestCase.tearDown` clears the
overrides after every test regardless. ``app`` is a module-level
singleton shared by every test in the process, so a leaked override
would silently corrupt unrelated tests.

The unauthenticated tests install NO override and send no
``Authorization`` header, so the 401 comes from ``OAuth2PasswordBearer``
itself. Overriding the dependency there would remove the only thing
those tests check.

THE ERROR CONTRACT UNDER TEST
-----------------------------------------------------------------------
``RaterNotVerified`` 403, ``NotATransactionParticipant`` 403,
``TransactionNotFound`` 404, ``SelfRatingNotAllowed`` 422,
``TransactionNotCompleted`` 409, ``DuplicateRating`` 409. A bounded
score and a bounded review are refused with 422 by Pydantic before any
handler body runs, which is asserted directly by patching the service
entry point and requiring that it was never called.

SENTIMENT NEUTRALITY IS A COMPLIANCE GATE, NOT A FEATURE TEST
-----------------------------------------------------------------------
:class:`TestRatingModeration` asserts that a rejection withholds the
record and records a policy reason, that the aggregate keeps counting
every published rating whatever its score, and that a score of 1 is
treated exactly as a 5. Suppressing reviews on the basis of rating or
negative sentiment is what the FTC Rule on the Use of Consumer Reviews
and Testimonials (16 CFR Part 465) forbids, so "no score-based
suppression path exists" is asserted rather than assumed. Moderation is
also append-only: a transition is recorded, and the submitted score and
review are never rewritten.

RUNNABLE IN ISOLATION
-----------------------------------------------------------------------
Only modules that exist are imported, so this file runs on its own
without the three legacy test modules, which fail at collection on
``app.models``, ``app.database``, ``app.auth`` and ``backend.tasks``.
Their failure is a documented pre-existing condition and is not
repaired here. ``app.tasks.background_jobs`` is never imported: nothing
in this feature's correctness depends on the worker tier, because the
read paths publish opportunistically instead - which
``test_read_user_ratings_publishes_a_rating_whose_window_elapsed``
exercises.

Shared arrangement comes from ``conftest.py``: its builders, its autouse
per-test reset, and the raw-state inspection
(``fake_db.count``/``document_body``/``documents``) that makes every "no
document written" assertion possible. A query cannot express that
assertion, because it cannot distinguish a document that is absent from
one that was written and then filtered out of the result.
"""

import unittest
from datetime import timedelta
from unittest.mock import patch

from conftest import (
    RATINGS_COLLECTION,
    USERS_COLLECTION,
    admin_user,
    auth_headers,
    completed_transaction,
    degenerate_transaction,
    fake_db,
    non_participant_user,
    pending_transaction,
    rating_document_id,
    seed_rating,
    seed_transaction,
    seed_user,
    unverified_user,
    utc_now,
    verified_buyer,
    verified_seller,
)
from fastapi.testclient import TestClient

from app.api.auth import get_current_user
from app.api.ratings import (
    ADMIN_ONLY_DETAIL,
    RATING_NOT_FOUND_DETAIL,
    USER_NOT_FOUND_DETAIL,
)
from app.core.config import settings
from app.main import app
from app.schema.rating import ModerationStatus, RatingDirection
from app.services.rating import (
    DuplicateRating,
    NotATransactionParticipant,
    RaterNotVerified,
    SelfRatingNotAllowed,
    TransactionNotCompleted,
    TransactionNotFound,
)

# The mounted prefix, written once. Every request below is composed from
# it, so a test can never assert against a path the application does not
# serve.
RATINGS_PATH = '/api/ratings'

# The five routes this feature publishes, as (method, resolved path).
# Compared against what the application actually mounts rather than
# against the decorators, because the decorator is exactly what cannot
# reveal the defect this contract guards: all three older routers repeat
# their resource segment inside the router as well, so
# ``@router.post('/listings')`` under ``prefix='/api/listings'`` resolves
# to ``/api/listings/listings``.
EXPECTED_ROUTES = frozenset((
    ('POST', RATINGS_PATH),
    ('GET', RATINGS_PATH + '/user/{user_id}'),
    ('GET', RATINGS_PATH + '/transaction/{transaction_id}'),
    ('GET', RATINGS_PATH + '/eligibility/{transaction_id}'),
    ('PATCH', RATINGS_PATH + '/{rating_id}/moderation'),
))

# The keys the eligibility endpoint publishes. Asserted as a set so a
# field silently disappearing from the decision - the interface renders
# every one of them - fails here rather than in the browser.
ELIGIBILITY_KEYS = frozenset((
    'eligible',
    'reason',
    'ratee_id',
    'direction',
    'already_rated',
))

# A policy reason of the only kind a rejection may carry: something about
# the CONTENT of the review. Never about the score.
POLICY_REASON = 'Contains a phone number, which policy forbids'


class RatingAPITestCase(unittest.TestCase):
    """Shared wiring: the client, the callers, and the raw-state checks.

    Subclassed by every test class below so that arrangement is written
    once and each test reads as the behaviour it asserts.
    """

    def setUp(self):
        # The double is a process-wide singleton, so isolation comes
        # from clearing it rather than from rebuilding it. Under pytest
        # ``conftest``'s autouse fixture has already done this and the
        # second call is a no-op; doing it here as well is what keeps
        # the trailing ``unittest.main()`` guard honest, since a bare
        # ``unittest`` run gets no fixtures and would otherwise leak one
        # test's documents into the next.
        fake_db.reset()
        self.client = TestClient(app)

    def tearDown(self):
        # ``app`` is a module-level singleton shared by the whole
        # process. An override left installed here would authenticate
        # some later test as this test's caller, so it is cleared
        # unconditionally - including for the tests that installed none.
        app.dependency_overrides.clear()

    # Arrangement helpers.

    def credentials(self, user):
        """Store a caller and return real bearer credentials for them.

        Both halves are needed and neither is redundant: the token
        carries only ``sub`` and ``exp``, so every authorization fact -
        ``is_verified`` and ``role`` above all - is re-read from the
        stored document on each request.

        Args:
            user: The ``User`` to store and authenticate as.

        Returns:
            An ``Authorization`` header dict for ``TestClient``.
        """
        seed_user(user)
        return auth_headers(user)

    def override_caller(self, user):
        """Inject a caller the real dependency chain cannot produce.

        Used only where the caller under test has no stored user
        document, which the token path answers with a 401 before the
        rating guards are ever reached.

        Args:
            user: The ``User`` to resolve every request as.

        Returns:
            The ``user`` that was installed.
        """
        def resolve_caller():
            # Synchronous, matching ``get_current_user`` itself.
            return user

        app.dependency_overrides[get_current_user] = resolve_caller
        return user

    def arrange(self, buyer=None, seller=None, transaction=None):
        """Seed both parties of a completed transaction.

        Args:
            buyer: The buyer to store. Defaults to a verified buyer.
            seller: The seller to store. Defaults to a verified seller.
            transaction: The transaction to store. Defaults to a
                completed one naming both of the above.

        Returns:
            The stored ``(buyer, seller, transaction)``.
        """
        stored_buyer = seed_user(
            verified_buyer() if buyer is None else buyer
        )
        stored_seller = seed_user(
            verified_seller() if seller is None else seller
        )
        stored_transaction = seed_transaction(
            completed_transaction() if transaction is None else transaction
        )
        return stored_buyer, stored_seller, stored_transaction

    # Request helpers.

    def submit(self, headers, transaction_id, score=None, review=None,
               **claims):
        """POST one rating.

        Args:
            headers: Credentials, or ``None`` to send none at all.
            transaction_id: The transaction being rated.
            score: The vote. Defaults to the maximum permitted, taken
                from the settings rather than written as a literal.
            review: Optional free text. Omitted from the body entirely
                when ``None``, so "no review" is one state and not two.
            **claims: Extra body keys, for the paths that exist to
                refuse a client-supplied claim.

        Returns:
            The ``httpx`` response.
        """
        body = {
            'transaction_id': transaction_id,
            'score': settings.RATING_MAX if score is None else score,
        }
        if review is not None:
            body['review'] = review
        body.update(claims)
        return self.client.post(RATINGS_PATH, json=body, headers=headers)

    def read_user_ratings(self, user_id, headers=None):
        """GET one user's public reputation.

        Args:
            user_id: The rated user.
            headers: Credentials. Deliberately optional: this read is
                public, following the precedent both read handlers in
                ``app/api/listings.py`` set.

        Returns:
            The ``httpx`` response.
        """
        return self.client.get(
            '{0}/user/{1}'.format(RATINGS_PATH, user_id),
            headers=headers,
        )

    def read_transaction_ratings(self, transaction_id, headers):
        """GET the ratings on one transaction.

        Args:
            transaction_id: The transaction whose ratings are wanted.
            headers: Credentials, or ``None`` to send none.

        Returns:
            The ``httpx`` response.
        """
        return self.client.get(
            '{0}/transaction/{1}'.format(RATINGS_PATH, transaction_id),
            headers=headers,
        )

    def read_eligibility(self, transaction_id, headers):
        """GET the eligibility decision for one transaction.

        Args:
            transaction_id: The transaction the caller asks about.
            headers: Credentials, or ``None`` to send none.

        Returns:
            The ``httpx`` response.
        """
        return self.client.get(
            '{0}/eligibility/{1}'.format(RATINGS_PATH, transaction_id),
            headers=headers,
        )

    def moderate(self, rating_id, headers, status, reason=None):
        """PATCH one rating's moderation state.

        Args:
            rating_id: The composite rating document ID.
            headers: Credentials, or ``None`` to send none.
            status: Target :class:`ModerationStatus`, or a raw string
                for the paths that exist to refuse one.
            reason: The policy reason. Omitted when ``None``, which is
                how the "a rejection needs a reason" refusal is
                reached.

        Returns:
            The ``httpx`` response.
        """
        body = {
            'moderation_status': getattr(status, 'value', status),
        }
        if reason is not None:
            body['moderation_reason'] = reason
        return self.client.patch(
            '{0}/{1}/moderation'.format(RATINGS_PATH, rating_id),
            json=body,
            headers=headers,
        )

    def reveal_pair(self, buyer, seller, transaction, buyer_score=None,
                    seller_score=None, buyer_review=None):
        """Submit from both sides so the double-blind pair reveals.

        The only way to reach a PUBLISHED rating through the API
        without waiting out the rating window, and therefore the
        arrangement behind every assertion about published state.

        Args:
            buyer: The stored buyer.
            seller: The stored seller.
            transaction: The stored transaction.
            buyer_score: The buyer's vote.
            seller_score: The seller's vote.
            buyer_review: Optional review text from the buyer.

        Returns:
            The buyer's and the seller's responses, in that order.
        """
        first = self.submit(
            auth_headers(buyer),
            transaction.id,
            score=buyer_score,
            review=buyer_review,
        )
        second = self.submit(
            auth_headers(seller),
            transaction.id,
            score=seller_score,
        )
        return first, second

    # Raw-state assertions. These read the store directly, because a
    # query cannot distinguish a document that was never written from
    # one that was written and then filtered out of a result.

    def assert_no_rating_written(self):
        """Assert the refusal persisted nothing whatsoever."""
        self.assertEqual(fake_db.count(RATINGS_COLLECTION), 0)
        self.assertEqual(fake_db.documents(RATINGS_COLLECTION), {})

    def assert_rating_absent(self, transaction_id, rater_id):
        """Assert one specific rating document does not exist.

        Args:
            transaction_id: The transaction it would belong to.
            rater_id: The party who would have submitted it.
        """
        self.assertFalse(
            fake_db.exists(
                RATINGS_COLLECTION,
                rating_document_id(transaction_id, rater_id),
            )
        )

    def stored_rating(self, transaction_id, rater_id):
        """Return one stored rating body, or ``None``.

        Args:
            transaction_id: The transaction it belongs to.
            rater_id: The party who submitted it.

        Returns:
            The document body as stored, or ``None`` when absent.
        """
        return fake_db.document_body(
            RATINGS_COLLECTION,
            rating_document_id(transaction_id, rater_id),
        )

    def stored_aggregate(self, user_id):
        """Return one user's stored aggregate.

        Args:
            user_id: The rated user.

        Returns:
            ``(rating_average, rating_count)`` as stored.
        """
        body = fake_db.document_body(USERS_COLLECTION, user_id) or {}
        return body.get('rating_average'), body.get('rating_count')

    def assert_refusal(self, response, status, failure):
        """Assert a refusal came from one specific guard.

        Args:
            response: The response to check.
            status: The expected status code.
            failure: The domain exception class whose ``message`` the
                router publishes as ``detail``.
        """
        self.assertEqual(response.status_code, status)
        detail = response.json()['detail']
        self.assertEqual(detail, failure.message)
        # The interface renders this verbatim, so it has to be a
        # sentence rather than a code.
        self.assertIsInstance(detail, str)
        self.assertTrue(detail.strip())


# Test cases for the published route contract, read off the application.


class TestRatingRouteContract(unittest.TestCase):
    """What the application actually mounts under ``/api/ratings``.

    Every path is taken from ``app.routes`` rather than from a
    decorator, because the decorator is precisely what cannot reveal the
    defect under test here. All three older routers declare their
    resource segment inside the router as well as in the mount prefix,
    so ``@router.post('/listings')`` mounted at ``prefix='/api/listings'``
    resolves to ``/api/listings/listings``. Those contracts are
    published and repairing them is out of scope; this class is the only
    check that the ratings router did not inherit the same shape.
    """

    def ratings_routes(self):
        """Return every mounted ratings route as (method, path).

        Returns:
            A set of ``(method, resolved path)`` pairs, with the
            framework's automatic ``HEAD`` companions discarded so the
            comparison describes the API rather than Starlette.
        """
        resolved = set()
        for route in app.routes:
            path = getattr(route, 'path', '') or ''
            if not path.startswith(RATINGS_PATH):
                continue
            for method in getattr(route, 'methods', None) or ():
                if method != 'HEAD':
                    resolved.add((method, path))
        return resolved

    def test_ratings_routes_resolve_at_the_published_paths(self):
        self.assertEqual(self.ratings_routes(), EXPECTED_ROUTES)

    def test_ratings_routes_carry_no_doubled_segment(self):
        routes = self.ratings_routes()
        # Guards against an empty comparison passing vacuously.
        self.assertEqual(len(routes), len(EXPECTED_ROUTES))
        for _method, path in sorted(routes):
            self.assertNotIn('/ratings/ratings', path)
            segments = [segment for segment in path.split('/') if segment]
            repeated = [
                earlier
                for earlier, later in zip(segments, segments[1:])
                if earlier == later
            ]
            self.assertEqual(repeated, [], path)

    def test_ratings_routes_are_reachable_at_those_paths(self):
        # The enumeration above proves what is registered; this proves
        # the registration is routable. An unauthenticated POST is
        # answered 401 by the security dependency, which is only
        # possible if the path matched - a 404 would mean the route
        # exists in the table and not on the wire.
        client = TestClient(app)
        response = client.post(RATINGS_PATH, json={})
        self.assertEqual(response.status_code, 401)


# Test cases for the two authorization gates on rating submission.


class TestRatingSubmissionGates(RatingAPITestCase):
    """R1, R2 and every other refusal on ``POST /api/ratings``.

    Each test asserts the status code, which guard produced it, and that
    nothing was persisted.
    """

    def test_submit_rating_unverified_rater_forbidden(self):
        """R1 - PRIMARY GATE. Only a verified user may rate.

        The caller is the transaction's own buyer, so every other guard
        passes and the refusal is attributable to verification alone -
        which is also what makes this test go red if the verification
        guard is removed.
        """
        seed_user(verified_seller())
        transaction = seed_transaction(completed_transaction())
        caller = unverified_user()
        headers = self.credentials(caller)

        response = self.submit(headers, transaction.id)

        self.assert_refusal(response, 403, RaterNotVerified)
        self.assert_no_rating_written()
        self.assert_rating_absent(transaction.id, caller.id)

    def test_submit_rating_non_participant_forbidden(self):
        """R2 - PRIMARY GATE. Only a counterparty may rate.

        The caller is verified, so the verification guard passes and the
        refusal is attributable to participation alone.
        """
        buyer, seller, transaction = self.arrange()
        outsider = non_participant_user()
        headers = self.credentials(outsider)

        response = self.submit(headers, transaction.id)

        self.assert_refusal(response, 403, NotATransactionParticipant)
        self.assert_no_rating_written()
        self.assert_rating_absent(transaction.id, outsider.id)
        # Neither party's reputation moved either.
        self.assertEqual(self.stored_aggregate(seller.id), (None, 0))
        self.assertEqual(self.stored_aggregate(buyer.id), (None, 0))

    def test_submit_rating_with_mismatched_ratee_id_forbidden(self):
        """A client-supplied counterparty is a claim, never a value."""
        buyer, seller, transaction = self.arrange()
        headers = auth_headers(buyer)

        response = self.submit(
            headers,
            transaction.id,
            ratee_id=non_participant_user().id,
        )

        self.assert_refusal(response, 403, NotATransactionParticipant)
        self.assert_no_rating_written()
        # The rating was not quietly redirected to the real
        # counterparty, which is the failure mode a dropped claim would
        # have produced: a 201 for a relationship the server rejected.
        self.assertEqual(self.stored_aggregate(seller.id), (None, 0))

    def test_submit_rating_with_matching_ratee_id_is_accepted(self):
        # The counterpart of the test above, and what keeps it honest: a
        # claim that AGREES with the derived counterparty is not the
        # thing being refused.
        buyer, seller, transaction = self.arrange()

        response = self.submit(
            auth_headers(buyer),
            transaction.id,
            ratee_id=seller.id,
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()['ratee_id'], seller.id)

    def test_submit_rating_for_degenerate_transaction_unprocessable(self):
        """Self-rating is unreachable except through a bad document."""
        buyer = seed_user(verified_buyer())
        transaction = seed_transaction(degenerate_transaction())

        response = self.submit(auth_headers(buyer), transaction.id)

        self.assert_refusal(response, 422, SelfRatingNotAllowed)
        self.assert_no_rating_written()
        self.assertEqual(self.stored_aggregate(buyer.id), (None, 0))

    def test_submit_rating_twice_conflicts_and_leaves_the_aggregate(self):
        """A5 - the one-vote rule, and that it costs the ratee nothing.

        The first rating is arranged as PUBLISHED with the ratee's
        aggregate already reflecting it, so the second submission has
        something it could damage. Uniqueness is enforced by document-ID
        collision in the datastore, which is why the outcome holds under
        concurrent submission as well as sequential.
        """
        buyer, seller, transaction = self.arrange(
            seller=verified_seller(rating_average=3.0, rating_count=1),
        )
        seed_rating(
            transaction_id=transaction.id,
            rater_id=buyer.id,
            ratee_id=seller.id,
            score=3,
            is_published=True,
        )

        response = self.submit(
            auth_headers(buyer),
            transaction.id,
            score=settings.RATING_MAX,
        )

        self.assert_refusal(response, 409, DuplicateRating)
        self.assertEqual(self.stored_aggregate(seller.id), (3.0, 1))
        # Append-only: the stored vote is the first one, not the second.
        self.assertEqual(
            self.stored_rating(transaction.id, buyer.id)['score'], 3
        )
        self.assertEqual(fake_db.count(RATINGS_COLLECTION), 1)

    def test_submit_rating_for_incomplete_transaction_conflicts(self):
        """A rating attests to a completed exchange."""
        buyer = seed_user(verified_buyer())
        seed_user(verified_seller())
        transaction = seed_transaction(pending_transaction())

        response = self.submit(auth_headers(buyer), transaction.id)

        self.assert_refusal(response, 409, TransactionNotCompleted)
        self.assert_no_rating_written()

    def test_submit_rating_for_unknown_transaction_not_found(self):
        buyer = seed_user(verified_buyer())

        response = self.submit(auth_headers(buyer), 'no-such-transaction')

        self.assert_refusal(response, 404, TransactionNotFound)
        self.assert_no_rating_written()

    def test_submit_rating_without_credentials_unauthorized(self):
        """No override is installed, so the 401 is the real one.

        ``OAuth2PasswordBearer`` refuses the request before any handler
        or guard is reached. Overriding ``get_current_user`` here would
        destroy the only thing this test checks.
        """
        buyer, _seller, transaction = self.arrange()
        self.assertEqual(app.dependency_overrides, {})

        response = self.submit(None, transaction.id)

        self.assertEqual(response.status_code, 401)
        self.assertIsInstance(response.json()['detail'], str)
        self.assert_no_rating_written()
        self.assert_rating_absent(transaction.id, buyer.id)

    def test_submit_rating_with_expired_credentials_unauthorized(self):
        # A credential with no remaining life is one that cannot be
        # validated, so it is refused before any guard is reached.
        buyer, _seller, transaction = self.arrange()
        stale = auth_headers(buyer, expires_minutes=-1)

        response = self.submit(stale, transaction.id)

        self.assertEqual(response.status_code, 401)
        self.assert_no_rating_written()

    def test_submit_rating_after_verification_revoked_forbidden(self):
        """R1 is decided by the STORED flag, not by the token.

        The token is minted while the caller is verified and is never
        reissued; verification is then revoked on the user document. The
        very next request with that same token is refused, which is the
        property that makes the gate revocable without token rotation -
        and is unobservable if the dependency is overridden.
        """
        buyer, _seller, transaction = self.arrange()
        headers = auth_headers(buyer)
        seed_user(verified_buyer(is_verified=False))

        response = self.submit(headers, transaction.id)

        self.assert_refusal(response, 403, RaterNotVerified)
        self.assert_no_rating_written()

    def test_submit_rating_without_a_user_document_forbidden(self):
        """An identity with no stored account carries no verification.

        The one caller the token path cannot express - it answers 401
        before the rating guards run - so the dependency is overridden
        to reach the service's own defence. ``tearDown`` clears the
        override.
        """
        seed_user(verified_seller())
        transaction = seed_transaction(completed_transaction())
        ghost = self.override_caller(
            verified_buyer(user_id='ghost-account-000001')
        )

        response = self.submit(None, transaction.id)

        self.assert_refusal(response, 403, RaterNotVerified)
        self.assert_no_rating_written()
        self.assert_rating_absent(transaction.id, ghost.id)


# Test cases for successful submission, in both directions.


class TestRatingSubmissionSuccess(RatingAPITestCase):
    """A1, R0 and the double-blind model on the write path.

    The two directional tests are what prove the feature is
    bidirectional rather than buyer-to-seller with a field for it.
    """

    def test_submit_rating_from_buyer_derives_the_seller(self):
        """A1 - the buyer-to-seller direction, end to end."""
        buyer, seller, transaction = self.arrange()

        response = self.submit(
            auth_headers(buyer),
            transaction.id,
            score=4,
            review='Straightforward sale, car as described',
        )

        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(
            body['id'], rating_document_id(transaction.id, buyer.id)
        )
        self.assertEqual(body['rater_id'], buyer.id)
        # Server-derived, from the transaction rather than the request.
        self.assertEqual(body['ratee_id'], seller.id)
        self.assertEqual(
            body['direction'], RatingDirection.BUYER_TO_SELLER.value
        )
        self.assertEqual(body['score'], 4)
        self.assertEqual(
            body['review'], 'Straightforward sale, car as described'
        )
        self.assertEqual(body['transaction_id'], transaction.id)
        # Denormalised from the transaction so a rating can be shown
        # with context without a second read.
        self.assertEqual(
            body['vehicle_listing_id'], transaction.vehicle_listing_id
        )

        stored = self.stored_rating(transaction.id, buyer.id)
        self.assertEqual(stored['rater_id'], buyer.id)
        self.assertEqual(stored['ratee_id'], seller.id)
        self.assertEqual(
            stored['direction'], RatingDirection.BUYER_TO_SELLER.value
        )
        self.assertEqual(stored['score'], 4)
        # Created pending, which is what "moderation before display"
        # means for a review nobody has read yet.
        self.assertEqual(
            stored['moderation_status'], ModerationStatus.PENDING.value
        )
        self.assertIsNone(stored['moderation_reason'])
        self.assertEqual(fake_db.count(RATINGS_COLLECTION), 1)

    def test_submit_rating_from_seller_derives_the_buyer(self):
        """R0 - the reverse direction. Omitting this omits the feature."""
        buyer, seller, transaction = self.arrange()

        response = self.submit(auth_headers(seller), transaction.id, score=3)

        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(
            body['id'], rating_document_id(transaction.id, seller.id)
        )
        self.assertEqual(body['rater_id'], seller.id)
        self.assertEqual(body['ratee_id'], buyer.id)
        self.assertEqual(
            body['direction'], RatingDirection.SELLER_TO_BUYER.value
        )
        stored = self.stored_rating(transaction.id, seller.id)
        self.assertEqual(
            stored['direction'], RatingDirection.SELLER_TO_BUYER.value
        )
        self.assertEqual(stored['ratee_id'], buyer.id)

    def test_submit_rating_is_recorded_unpublished_and_deferred(self):
        """A10 - one side alone reveals nothing and moves nothing.

        The response reports the recorded-but-unpublished state
        explicitly rather than leaving the submission looking as though
        it had silently failed.
        """
        buyer, seller, transaction = self.arrange()

        response = self.submit(auth_headers(buyer), transaction.id, score=5)

        self.assertEqual(response.status_code, 201)
        self.assertFalse(response.json()['is_published'])
        self.assertFalse(
            self.stored_rating(transaction.id, buyer.id)['is_published']
        )
        # Excluded from the ratee's aggregate ...
        self.assertEqual(self.stored_aggregate(seller.id), (None, 0))
        # ... and absent from the public read.
        public = self.read_user_ratings(seller.id)
        self.assertEqual(public.status_code, 200)
        self.assertEqual(public.json()['items'], [])
        self.assertEqual(
            public.json()['aggregate'], {'average': None, 'count': 0}
        )

    def test_submit_rating_reciprocal_publishes_both_sides(self):
        """A11 - the reveal, and both deferred aggregates applying."""
        buyer, seller, transaction = self.arrange()

        first, second = self.reveal_pair(
            buyer, seller, transaction, buyer_score=4, seller_score=2
        )

        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 201)
        # The second response reports the reveal its own submission
        # triggered.
        self.assertTrue(second.json()['is_published'])
        buyer_rating = self.stored_rating(transaction.id, buyer.id)
        seller_rating = self.stored_rating(transaction.id, seller.id)
        self.assertTrue(buyer_rating['is_published'])
        self.assertTrue(seller_rating['is_published'])
        # Both deferred contributions applied, each to the party that
        # was rated rather than to the party that rated.
        self.assertEqual(self.stored_aggregate(seller.id), (4.0, 1))
        self.assertEqual(self.stored_aggregate(buyer.id), (2.0, 1))
        self.assertEqual(fake_db.count(RATINGS_COLLECTION), 2)

    def test_submit_rating_publication_is_visible_to_both_readers(self):
        # The same reveal, observed through the public API rather than
        # through stored state, because that is what a profile renders.
        buyer, seller, transaction = self.arrange()
        self.reveal_pair(
            buyer, seller, transaction, buyer_score=5, seller_score=1
        )

        seller_view = self.read_user_ratings(seller.id).json()
        buyer_view = self.read_user_ratings(buyer.id).json()

        self.assertEqual(seller_view['aggregate'], {
            'average': 5.0, 'count': 1,
        })
        self.assertEqual(buyer_view['aggregate'], {
            'average': 1.0, 'count': 1,
        })
        self.assertEqual(len(seller_view['items']), 1)
        self.assertEqual(len(buyer_view['items']), 1)
        self.assertEqual(
            seller_view['items'][0]['direction'],
            RatingDirection.BUYER_TO_SELLER.value,
        )
        self.assertEqual(
            buyer_view['items'][0]['direction'],
            RatingDirection.SELLER_TO_BUYER.value,
        )

    def test_submit_rating_averages_repeated_scores_for_one_ratee(self):
        # Two published ratings for the same seller, from two different
        # transactions, so the running average is exercised rather than
        # only the first-rating case.
        buyer, seller, first_transaction = self.arrange()
        second_transaction = seed_transaction(completed_transaction(
            transaction_id='test-transaction-000002',
        ))
        self.reveal_pair(
            buyer, seller, first_transaction, buyer_score=5, seller_score=5
        )
        self.reveal_pair(
            buyer, seller, second_transaction, buyer_score=2, seller_score=4
        )

        self.assertEqual(self.stored_aggregate(seller.id), (3.5, 2))
        self.assertEqual(
            self.read_user_ratings(seller.id).json()['aggregate'],
            {'average': 3.5, 'count': 2},
        )


# Test cases for request validation, which Pydantic applies first.


class TestRatingRequestValidation(RatingAPITestCase):
    """A7, A8 - the bounded score and the bounded review.

    Both are refused before the handler body runs, which each test
    asserts directly by patching the service entry point the router
    imported and requiring that it was never called. Every bound is
    derived from ``settings`` rather than written as a literal, so a
    retuned bound moves the tests with it.
    """

    def refuse(self, headers, transaction_id, **payload):
        """Submit an invalid body and assert nothing downstream ran.

        Args:
            headers: Credentials for the caller.
            transaction_id: The transaction being cited.
            **payload: The invalid ``submit`` arguments.

        Returns:
            The parsed response body, for a caller that wants to look
            at the reported location of the error.
        """
        with patch('app.api.ratings.submit_rating') as service:
            response = self.submit(headers, transaction_id, **payload)
        self.assertEqual(response.status_code, 422)
        service.assert_not_called()
        self.assert_no_rating_written()
        return response.json()

    def test_submit_rating_below_the_minimum_score_unprocessable(self):
        buyer, _seller, transaction = self.arrange()

        body = self.refuse(
            auth_headers(buyer),
            transaction.id,
            score=settings.RATING_MIN - 1,
        )

        self.assertIn('score', str(body['detail']))

    def test_submit_rating_above_the_maximum_score_unprocessable(self):
        buyer, _seller, transaction = self.arrange()

        body = self.refuse(
            auth_headers(buyer),
            transaction.id,
            score=settings.RATING_MAX + 1,
        )

        self.assertIn('score', str(body['detail']))

    def test_submit_rating_with_a_fractional_score_unprocessable(self):
        # The bound is strict, so a score that would have to be rounded
        # to fit is refused rather than silently recording a vote the
        # caller never chose.
        buyer, _seller, transaction = self.arrange()

        self.refuse(auth_headers(buyer), transaction.id, score=3.5)

    def test_submit_rating_with_a_string_score_unprocessable(self):
        buyer, _seller, transaction = self.arrange()

        self.refuse(auth_headers(buyer), transaction.id, score='4')

    def test_submit_rating_with_a_missing_score_unprocessable(self):
        buyer, _seller, transaction = self.arrange()

        with patch('app.api.ratings.submit_rating') as service:
            response = self.client.post(
                RATINGS_PATH,
                json={'transaction_id': transaction.id},
                headers=auth_headers(buyer),
            )

        self.assertEqual(response.status_code, 422)
        service.assert_not_called()
        self.assert_no_rating_written()

    def test_submit_rating_with_overlong_review_unprocessable(self):
        buyer, _seller, transaction = self.arrange()
        overlong = 'a' * (settings.RATING_REVIEW_MAX_LENGTH + 1)

        body = self.refuse(
            auth_headers(buyer), transaction.id, review=overlong
        )

        self.assertIn('review', str(body['detail']))

    def test_submit_rating_at_the_review_length_limit_is_accepted(self):
        # The counterpart that keeps the bound honest: one character
        # fewer is accepted, so the test above is measuring the limit
        # rather than rejecting all long text.
        buyer, _seller, transaction = self.arrange()
        at_limit = 'a' * settings.RATING_REVIEW_MAX_LENGTH

        response = self.submit(
            auth_headers(buyer), transaction.id, review=at_limit
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(len(response.json()['review']), len(at_limit))

    def test_submit_rating_with_markup_in_the_review_unprocessable(self):
        # The stored value is plain text, so a tag delimiter is refused
        # at the boundary rather than escaped by every reader.
        buyer, _seller, transaction = self.arrange()

        self.refuse(
            auth_headers(buyer),
            transaction.id,
            review='<script>alert(1)</script>',
        )

    def test_submit_rating_with_a_client_supplied_direction_refused(self):
        # ``direction`` is derived server-side, so the field is not part
        # of the request contract at all. Refusing it is what stops a
        # caller believing they set it.
        buyer, _seller, transaction = self.arrange()

        self.refuse(
            auth_headers(buyer),
            transaction.id,
            direction=RatingDirection.SELLER_TO_BUYER.value,
        )

    def test_submit_rating_with_a_client_supplied_rater_id_refused(self):
        buyer, _seller, transaction = self.arrange()

        self.refuse(
            auth_headers(buyer),
            transaction.id,
            rater_id=non_participant_user().id,
        )

    def test_submit_rating_with_a_client_supplied_publication_refused(self):
        # Nobody publishes their own rating.
        buyer, _seller, transaction = self.arrange()

        self.refuse(
            auth_headers(buyer), transaction.id, is_published=True
        )


# Test cases for the read paths, public and participant-only.


class TestRatingReads(RatingAPITestCase):
    """A12, A10 - what a reader may see, and what stays hidden.

    The public read carries no authentication dependency, following the
    precedent both read handlers in ``app/api/listings.py`` set, so
    every request in the public group below deliberately sends no
    ``Authorization`` header.
    """

    def test_read_user_ratings_is_public_and_needs_no_credentials(self):
        """A12 - an unrated user is a state, not an error."""
        seller = seed_user(verified_seller())

        response = self.read_user_ratings(seller.id)

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body['items'], [])
        # ``average: null`` beside ``count: 0`` is the ONLY signal that
        # means "no ratings"; an average of zero would be a different
        # claim, and an empty list alone is not one.
        self.assertIsNone(body['aggregate']['average'])
        self.assertEqual(body['aggregate']['count'], 0)

    def test_read_user_ratings_for_unknown_user_not_found(self):
        # Distinct from a user who exists and has no ratings, which is
        # the 200 above. The two are different answers to a caller.
        response = self.read_user_ratings('no-such-user')

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()['detail'], USER_NOT_FOUND_DETAIL)
        self.assert_no_rating_written()

    def test_read_user_ratings_excludes_an_unrevealed_rating(self):
        """A10 - the double-blind model holds on the way out."""
        buyer, seller, transaction = self.arrange()
        seed_rating(
            transaction_id=transaction.id,
            rater_id=buyer.id,
            ratee_id=seller.id,
            score=1,
        )

        body = self.read_user_ratings(seller.id).json()

        self.assertEqual(body['items'], [])
        self.assertEqual(
            body['aggregate'], {'average': None, 'count': 0}
        )

    def test_read_user_ratings_returns_published_ratings_newest_first(self):
        buyer, seller, first_transaction = self.arrange()
        second_transaction = seed_transaction(completed_transaction(
            transaction_id='test-transaction-000002',
        ))
        self.reveal_pair(
            buyer, seller, first_transaction, buyer_score=4, seller_score=4
        )
        self.reveal_pair(
            buyer, seller, second_transaction, buyer_score=5, seller_score=5
        )

        items = self.read_user_ratings(seller.id).json()['items']

        self.assertEqual(len(items), 2)
        self.assertEqual(
            [item['transaction_id'] for item in items],
            [second_transaction.id, first_transaction.id],
        )
        for item in items:
            self.assertTrue(item['is_published'])
            self.assertEqual(item['ratee_id'], seller.id)

    def test_read_user_ratings_publishes_a_rating_whose_window_elapsed(self):
        """The window fallback, which needs no worker to be running.

        No Celery broker is provisioned in this project and no task is
        ever dispatched, so publication cannot depend on the worker
        tier. The read settles anything already due instead, which is
        what makes an unreciprocated rating eventually visible.
        """
        buyer, seller, transaction = self.arrange()
        overdue = utc_now() - timedelta(
            days=settings.RATING_WINDOW_DAYS + 1
        )
        seed_rating(
            transaction_id=transaction.id,
            rater_id=buyer.id,
            ratee_id=seller.id,
            score=4,
            created_at=overdue,
        )
        self.assertEqual(self.stored_aggregate(seller.id), (None, 0))

        body = self.read_user_ratings(seller.id).json()

        self.assertEqual(len(body['items']), 1)
        self.assertTrue(body['items'][0]['is_published'])
        self.assertEqual(body['aggregate'], {'average': 4.0, 'count': 1})
        # Settled durably rather than only for this response.
        self.assertTrue(
            self.stored_rating(transaction.id, buyer.id)['is_published']
        )
        self.assertEqual(self.stored_aggregate(seller.id), (4.0, 1))

    def test_read_user_ratings_withholds_a_review_awaiting_moderation(self):
        # "Moderation before display": the rating is published and
        # counted, and its unread words are not shown yet.
        buyer, seller, transaction = self.arrange()
        self.reveal_pair(
            buyer,
            seller,
            transaction,
            buyer_score=5,
            seller_score=5,
            buyer_review='Words no moderator has read yet',
        )

        items = self.read_user_ratings(seller.id).json()['items']

        self.assertEqual(len(items), 1)
        self.assertIsNone(items[0]['review'])
        self.assertEqual(items[0]['score'], 5)

    def test_read_transaction_ratings_returns_the_callers_own_rating(self):
        buyer, _seller, transaction = self.arrange()
        self.submit(auth_headers(buyer), transaction.id, score=3)

        response = self.read_transaction_ratings(
            transaction.id, auth_headers(buyer)
        )

        self.assertEqual(response.status_code, 200)
        items = response.json()
        self.assertIsInstance(items, list)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['rater_id'], buyer.id)
        # The author sees their own rating before it is revealed to
        # anybody else, which is why its published flag is reported
        # rather than assumed.
        self.assertFalse(items[0]['is_published'])

    def test_read_transaction_ratings_hides_the_unrevealed_counterparty(self):
        """A10 - being a participant does not lift the double blind."""
        buyer, seller, transaction = self.arrange()
        self.submit(auth_headers(buyer), transaction.id, score=1)

        response = self.read_transaction_ratings(
            transaction.id, auth_headers(seller)
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])

    def test_read_transaction_ratings_shows_both_after_the_reveal(self):
        buyer, seller, transaction = self.arrange()
        self.reveal_pair(
            buyer, seller, transaction, buyer_score=4, seller_score=5
        )

        for caller in (buyer, seller):
            response = self.read_transaction_ratings(
                transaction.id, auth_headers(caller)
            )
            self.assertEqual(response.status_code, 200)
            raters = sorted(item['rater_id'] for item in response.json())
            self.assertEqual(raters, sorted([buyer.id, seller.id]))

    def test_read_transaction_ratings_as_non_participant_forbidden(self):
        _buyer, _seller, transaction = self.arrange()
        headers = self.credentials(non_participant_user())

        response = self.read_transaction_ratings(transaction.id, headers)

        self.assert_refusal(response, 403, NotATransactionParticipant)
        # A refused read is still a read: it writes nothing.
        self.assert_no_rating_written()

    def test_read_transaction_ratings_for_unknown_transaction_not_found(self):
        headers = self.credentials(verified_buyer())

        response = self.read_transaction_ratings('no-such-thing', headers)

        self.assert_refusal(response, 404, TransactionNotFound)
        self.assert_no_rating_written()

    def test_read_transaction_ratings_without_credentials_unauthorized(self):
        _buyer, _seller, transaction = self.arrange()
        self.assertEqual(app.dependency_overrides, {})

        response = self.read_transaction_ratings(transaction.id, None)

        self.assertEqual(response.status_code, 401)
        self.assert_no_rating_written()


# Test cases for the eligibility decision the interface renders.


class TestRatingEligibility(RatingAPITestCase):
    """The decision endpoint, which exists so a control can say why.

    Every outcome except a missing transaction is a 200 carrying a
    reason, including an unverified caller - the one place in this
    feature where R1 does not produce a 403, because refusing the
    request would leave the interface unable to explain itself, and an
    unexplained disabled control is invisible to a user of assistive
    technology.
    """

    def test_eligibility_for_a_verified_participant_is_eligible(self):
        buyer, seller, transaction = self.arrange()

        response = self.read_eligibility(transaction.id, auth_headers(buyer))

        self.assertEqual(response.status_code, 200)
        decision = response.json()
        self.assertEqual(set(decision), set(ELIGIBILITY_KEYS))
        self.assertTrue(decision['eligible'])
        self.assertIsNone(decision['reason'])
        self.assertEqual(decision['ratee_id'], seller.id)
        self.assertEqual(
            decision['direction'], RatingDirection.BUYER_TO_SELLER.value
        )
        self.assertFalse(decision['already_rated'])
        # A question, not an event: asking must not publish or record
        # anything.
        self.assert_no_rating_written()
        self.assertEqual(self.stored_aggregate(seller.id), (None, 0))

    def test_eligibility_for_the_seller_reports_the_reverse_direction(self):
        buyer, seller, transaction = self.arrange()

        decision = self.read_eligibility(
            transaction.id, auth_headers(seller)
        ).json()

        self.assertTrue(decision['eligible'])
        self.assertEqual(decision['ratee_id'], buyer.id)
        self.assertEqual(
            decision['direction'], RatingDirection.SELLER_TO_BUYER.value
        )

    def test_eligibility_for_an_unverified_caller_reports_the_reason(self):
        seed_user(verified_seller())
        transaction = seed_transaction(completed_transaction())
        headers = self.credentials(unverified_user())

        response = self.read_eligibility(transaction.id, headers)

        self.assertEqual(response.status_code, 200)
        decision = response.json()
        self.assertFalse(decision['eligible'])
        self.assertEqual(decision['reason'], RaterNotVerified.message)
        self.assertTrue(decision['reason'].strip())
        # A refused caller learns nothing about the counterparty.
        self.assertIsNone(decision['ratee_id'])
        self.assertIsNone(decision['direction'])
        self.assertFalse(decision['already_rated'])

    def test_eligibility_for_a_non_participant_reports_the_reason(self):
        _buyer, _seller, transaction = self.arrange()
        headers = self.credentials(non_participant_user())

        decision = self.read_eligibility(transaction.id, headers).json()

        self.assertFalse(decision['eligible'])
        self.assertEqual(
            decision['reason'], NotATransactionParticipant.message
        )
        self.assertIsNone(decision['ratee_id'])

    def test_eligibility_for_an_incomplete_transaction_reports_the_reason(
        self,
    ):
        buyer = seed_user(verified_buyer())
        seller = seed_user(verified_seller())
        transaction = seed_transaction(pending_transaction())

        decision = self.read_eligibility(
            transaction.id, auth_headers(buyer)
        ).json()

        self.assertFalse(decision['eligible'])
        self.assertEqual(
            decision['reason'], TransactionNotCompleted.message
        )
        # Participation was established before this guard, so the
        # interface can already name the counterparty it will rate once
        # the transaction completes.
        self.assertEqual(decision['ratee_id'], seller.id)
        self.assertFalse(decision['already_rated'])

    def test_eligibility_after_submitting_reports_already_rated(self):
        buyer, seller, transaction = self.arrange()
        self.assertEqual(
            self.submit(
                auth_headers(buyer), transaction.id, score=5
            ).status_code,
            201,
        )

        decision = self.read_eligibility(
            transaction.id, auth_headers(buyer)
        ).json()

        self.assertFalse(decision['eligible'])
        self.assertTrue(decision['already_rated'])
        self.assertEqual(decision['reason'], DuplicateRating.message)
        # Distinguishing "you have rated this" from "you may not rate
        # this" is the whole reason the flag is separate from the
        # boolean.
        self.assertEqual(decision['ratee_id'], seller.id)

    def test_eligibility_for_unknown_transaction_not_found(self):
        headers = self.credentials(verified_buyer())

        response = self.read_eligibility('no-such-transaction', headers)

        self.assert_refusal(response, 404, TransactionNotFound)
        self.assert_no_rating_written()

    def test_eligibility_without_credentials_unauthorized(self):
        _buyer, _seller, transaction = self.arrange()
        self.assertEqual(app.dependency_overrides, {})

        response = self.read_eligibility(transaction.id, None)

        self.assertEqual(response.status_code, 401)
        self.assert_no_rating_written()

    def test_eligibility_reports_what_the_write_path_enforces(self):
        # The reason shown and the refusal enforced are the same
        # sentence, taken from the same exception. If they could drift,
        # an interface would explain one thing while the server did
        # another.
        seed_user(verified_seller())
        transaction = seed_transaction(completed_transaction())
        headers = self.credentials(unverified_user())

        decision = self.read_eligibility(transaction.id, headers).json()
        refusal = self.submit(headers, transaction.id)

        self.assertEqual(refusal.status_code, 403)
        self.assertEqual(decision['reason'], refusal.json()['detail'])
        self.assert_no_rating_written()


# Test cases for moderation, its authorization, and its neutrality.


class TestRatingModeration(RatingAPITestCase):
    """A13 - moderation is admin-only, append-only and score-blind.

    The neutrality assertions here are a compliance gate rather than a
    feature test. Suppressing reviews on the basis of rating or negative
    sentiment is what the FTC Rule on the Use of Consumer Reviews and
    Testimonials (16 CFR Part 465) forbids, so "a low score is never
    itself a reason to withhold anything" is asserted directly.
    """

    def arrange_published_rating(self, score=1, review=None):
        """Publish one buyer-to-seller rating and return its context.

        Args:
            score: The buyer's vote. Defaults to the minimum, because
                the low score is what the neutrality assertions are
                about.
            review: Optional review text from the buyer.

        Returns:
            ``(seller, admin, rating_id)``.
        """
        buyer, seller, transaction = self.arrange()
        admin = seed_user(admin_user())
        self.reveal_pair(
            buyer,
            seller,
            transaction,
            buyer_score=score,
            seller_score=settings.RATING_MAX,
            buyer_review=review,
        )
        return seller, admin, rating_document_id(transaction.id, buyer.id)

    def test_moderate_rating_as_non_admin_forbidden(self):
        seller, _admin, rating_id = self.arrange_published_rating(
            review='Words awaiting a decision'
        )
        # The rated party is exactly the caller with a motive, and the
        # role is read from the stored document rather than a header.
        response = self.moderate(
            rating_id, auth_headers(seller), ModerationStatus.REJECTED,
            reason=POLICY_REASON,
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()['detail'], ADMIN_ONLY_DETAIL)
        stored = fake_db.document_body(RATINGS_COLLECTION, rating_id)
        self.assertEqual(
            stored['moderation_status'], ModerationStatus.PENDING.value
        )
        self.assertIsNone(stored['moderation_reason'])

    def test_moderate_rating_without_credentials_unauthorized(self):
        _seller, _admin, rating_id = self.arrange_published_rating()
        self.assertEqual(app.dependency_overrides, {})

        response = self.moderate(rating_id, None, ModerationStatus.APPROVED)

        self.assertEqual(response.status_code, 401)
        self.assertEqual(
            fake_db.document_body(
                RATINGS_COLLECTION, rating_id
            )['moderation_status'],
            ModerationStatus.PENDING.value,
        )

    def test_moderate_rating_as_admin_approves_and_reveals_the_review(self):
        seller, admin, rating_id = self.arrange_published_rating(
            review='Sold me a car with an undisclosed fault'
        )

        response = self.moderate(
            rating_id, auth_headers(admin), ModerationStatus.APPROVED
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(
            body['moderation_status'], ModerationStatus.APPROVED.value
        )
        self.assertIsNone(body['moderation_reason'])
        items = self.read_user_ratings(seller.id).json()['items']
        self.assertEqual(len(items), 1)
        self.assertEqual(
            items[0]['review'],
            'Sold me a car with an undisclosed fault',
        )

    def test_moderate_rating_rejection_requires_a_policy_reason(self):
        _seller, admin, rating_id = self.arrange_published_rating(
            review='Something a moderator objected to'
        )

        response = self.moderate(
            rating_id, auth_headers(admin), ModerationStatus.REJECTED
        )

        self.assertEqual(response.status_code, 422)
        # Nothing is withheld first and justified afterwards.
        self.assertEqual(
            fake_db.document_body(
                RATINGS_COLLECTION, rating_id
            )['moderation_status'],
            ModerationStatus.PENDING.value,
        )

    def test_moderate_rating_refuses_a_reason_on_a_displayed_state(self):
        # A state that does not withhold the rating cannot carry a
        # violation recorded against it.
        _seller, admin, rating_id = self.arrange_published_rating()

        response = self.moderate(
            rating_id,
            auth_headers(admin),
            ModerationStatus.APPROVED,
            reason=POLICY_REASON,
        )

        self.assertEqual(response.status_code, 422)
        self.assertEqual(
            fake_db.document_body(
                RATINGS_COLLECTION, rating_id
            )['moderation_status'],
            ModerationStatus.PENDING.value,
        )

    def test_moderate_rating_rejection_withholds_and_records_the_reason(self):
        """A13 - withheld from display, with the policy basis on record.

        The aggregate is deliberately NOT adjusted: a moderation
        decision is about content, and letting it move a score would
        make moderation sentiment-relevant. ``count`` exceeding the
        number of items is therefore the contract rather than a
        discrepancy.
        """
        seller, admin, rating_id = self.arrange_published_rating(
            score=settings.RATING_MIN, review='Call me on 555 0100'
        )
        before = self.stored_aggregate(seller.id)

        response = self.moderate(
            rating_id,
            auth_headers(admin),
            ModerationStatus.REJECTED,
            reason=POLICY_REASON,
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(
            body['moderation_status'], ModerationStatus.REJECTED.value
        )
        self.assertEqual(body['moderation_reason'], POLICY_REASON)
        stored = fake_db.document_body(RATINGS_COLLECTION, rating_id)
        self.assertEqual(stored['moderation_reason'], POLICY_REASON)
        public = self.read_user_ratings(seller.id).json()
        self.assertEqual(public['items'], [])
        self.assertEqual(self.stored_aggregate(seller.id), before)
        self.assertEqual(
            public['aggregate'],
            {'average': float(settings.RATING_MIN), 'count': 1},
        )

    def test_moderate_rating_state_is_never_disclosed_publicly(self):
        # The moderation fields reach the administrator who just set
        # them and nobody else; the decision they drive has already been
        # applied to what a reader receives.
        seller, admin, rating_id = self.arrange_published_rating(
            review='An approved review'
        )
        self.moderate(
            rating_id, auth_headers(admin), ModerationStatus.APPROVED
        )

        item = self.read_user_ratings(seller.id).json()['items'][0]

        self.assertNotIn('moderation_status', item)
        self.assertNotIn('moderation_reason', item)

    def test_moderate_rating_does_not_rewrite_the_score_or_review(self):
        """Append-only: a correction is a transition, not an edit.

        There is no audit or history collection in this codebase, which
        is exactly why the original words have to survive the decision.
        """
        _seller, admin, rating_id = self.arrange_published_rating(
            score=settings.RATING_MIN, review='The original words'
        )
        original = fake_db.document_body(RATINGS_COLLECTION, rating_id)

        self.moderate(
            rating_id, auth_headers(admin), ModerationStatus.APPROVED
        )
        self.moderate(
            rating_id,
            auth_headers(admin),
            ModerationStatus.REJECTED,
            reason=POLICY_REASON,
        )

        stored = fake_db.document_body(RATINGS_COLLECTION, rating_id)
        for field in (
            'score',
            'review',
            'rater_id',
            'ratee_id',
            'direction',
            'transaction_id',
            'vehicle_listing_id',
            'created_at',
        ):
            self.assertEqual(stored[field], original[field], field)

    def test_moderate_rating_never_withholds_for_a_low_score(self):
        """A13 - no score-based suppression path exists.

        The minimum and the maximum score are submitted in the same
        transaction, so the two are treated by identical code with
        nothing but the number differing. Both are published, both are
        listed, and both are counted.
        """
        buyer, seller, transaction = self.arrange()
        self.reveal_pair(
            buyer,
            seller,
            transaction,
            buyer_score=settings.RATING_MIN,
            seller_score=settings.RATING_MAX,
        )

        lowest = self.read_user_ratings(seller.id).json()
        highest = self.read_user_ratings(buyer.id).json()

        self.assertEqual(len(lowest['items']), len(highest['items']))
        self.assertEqual(lowest['items'][0]['score'], settings.RATING_MIN)
        self.assertEqual(highest['items'][0]['score'], settings.RATING_MAX)
        self.assertEqual(
            lowest['aggregate']['count'], highest['aggregate']['count']
        )
        self.assertEqual(
            lowest['aggregate']['average'], float(settings.RATING_MIN)
        )
        self.assertEqual(
            highest['aggregate']['average'], float(settings.RATING_MAX)
        )
        # Neither was quietly held back from publication either.
        for rater in (buyer, seller):
            self.assertTrue(
                self.stored_rating(transaction.id, rater.id)['is_published']
            )

    def test_moderate_rating_for_unknown_rating_not_found(self):
        admin = seed_user(admin_user())

        response = self.moderate(
            rating_id='no-such-transaction_no-such-rater',
            headers=auth_headers(admin),
            status=ModerationStatus.APPROVED,
        )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()['detail'], RATING_NOT_FOUND_DETAIL)
        self.assert_no_rating_written()

    def test_moderate_rating_with_unrecognised_state_unprocessable(self):
        _seller, admin, rating_id = self.arrange_published_rating()

        response = self.moderate(
            rating_id, auth_headers(admin), 'quietly-hidden'
        )

        self.assertEqual(response.status_code, 422)
        self.assertEqual(
            fake_db.document_body(
                RATINGS_COLLECTION, rating_id
            )['moderation_status'],
            ModerationStatus.PENDING.value,
        )


if __name__ == "__main__":
    unittest.main()
