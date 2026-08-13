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
repaired here.

Nothing in this feature's correctness depends on the worker tier: the
read paths publish opportunistically, which
``test_read_user_ratings_publishes_a_rating_whose_window_elapsed``
exercises. ``app.tasks.background_jobs`` IS imported all the same, by
:class:`TestScheduledSweepThroughTheApi` and nowhere else, and the sweep
task is executed there and then observed over HTTP - because nothing in
this repository dispatches it, so its body would otherwise be code no
test ever runs. The import is function-local so the rest of this module
keeps its narrow dependency surface.

Shared arrangement comes from ``conftest.py``: its builders, its autouse
per-test reset, and the raw-state inspection
(``fake_db.count``/``document_body``/``documents``) that makes every "no
document written" assertion possible. A query cannot express that
assertion, because it cannot distinguish a document that is absent from
one that was written and then filtered out of the result.
"""

import asyncio
import json
import unittest
from datetime import timedelta
from unittest.mock import patch

from google.api_core.exceptions import (
    DeadlineExceeded,
    RetryError,
    ServiceUnavailable,
)
from jose import jwt

from conftest import (
    RATINGS_COLLECTION,
    TRANSACTIONS_COLLECTION,
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
from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient

from app.api.auth import get_current_user
from app.api.ratings import (
    ADMIN_ONLY_DETAIL,
    RATING_NOT_FOUND_DETAIL,
    USER_NOT_FOUND_DETAIL,
    VALIDATION_DETAIL_LIMIT,
    validation_failure_handler,
)
from app.core.config import settings
# The 503 sentence and its backoff hint come from the datastore module,
# where they are declared beside the call policy that produces the
# condition - so this suite asserts the value the router actually sends
# rather than a copy of it that could drift.
from app.db.firestore import (
    DATASTORE_RETRY_AFTER_SECONDS,
    DATASTORE_UNAVAILABLE_DETAIL,
)
from app.main import app
from app.schema.rating import (
    DOCUMENT_ID_MAX_LENGTH,
    ModerationStatus,
    RatingDirection,
)
from app.services.rating import (
    DuplicateRating,
    NotATransactionParticipant,
    RaterNotVerified,
    SelfRatingNotAllowed,
    TransactionInvariantError,
    TransactionNotCompleted,
    TransactionNotFound,
)

# The mounted prefix, written once. Every request below is composed from
# it, so a test can never assert against a path the application does not
# serve.
RATINGS_PATH = '/api/ratings'

# The six routes this feature publishes, as (method, resolved path).
# Compared against what the application actually mounts rather than
# against the decorators, because the decorator is exactly what cannot
# reveal the defect this contract guards: all three older routers repeat
# their resource segment inside the router as well, so
# ``@router.post('/listings')`` under ``prefix='/api/listings'`` resolves
# to ``/api/listings/listings``.
#
# ``/user/{user_id}/aggregate`` is a SIBLING of the full user read rather
# than a mode of it. The reputation badge renders beside every listing and
# needs two numbers, and serving it from the full read meant fetching a
# page of rating documents for a client that discarded all of them; a
# query flag would have made one response model mean two shapes.
EXPECTED_ROUTES = frozenset((
    ('POST', RATINGS_PATH),
    ('GET', RATINGS_PATH + '/user/{user_id}'),
    ('GET', RATINGS_PATH + '/user/{user_id}/aggregate'),
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

# ---------------------------------------------------------------------
# THE PUBLISHED WORDING, PINNED INDEPENDENTLY.
#
# Every one of these sentences is rendered verbatim by the interface -
# beside a disabled control, or as the explanation of a refusal - so the
# wording is part of this API's contract and not an implementation
# detail. It is written out here rather than read off the production
# constant it is compared against, and the difference matters: an
# assertion of the form ``detail == RaterNotVerified.message`` holds
# whatever that attribute says, so it proves the router and the service
# agree with each other while proving nothing at all about what a user is
# told. Swap two messages, or replace one with "Forbidden", and such an
# assertion stays green.
#
# Pinned here, a change to any of these sentences fails a test and has to
# be made deliberately in two places - which is the point, because
# changing what a user is told is a decision rather than a refactor. The
# tests below ALSO keep asserting that each detail is a non-empty
# human-readable string, so the two checks answer different questions:
# these literals say WHICH sentence, and that check says the response
# carries a sentence at all.
#
# The set is deliberately the load-bearing ones only: the two must-
# requirement refusals (R1 and R2), the three stored-state refusals a
# caller can act on, and the three the router composes itself. A message
# a user never sees does not need pinning here.
# ---------------------------------------------------------------------
UNVERIFIED_RATER_DETAIL = 'Only verified users can submit ratings'
NON_PARTICIPANT_DETAIL = 'You are not a party to this transaction'
DUPLICATE_RATING_DETAIL = 'You have already rated this transaction'
INCOMPLETE_TRANSACTION_DETAIL = (
    'Ratings require a completed transaction'
)
SELF_RATING_DETAIL = 'Self-rating is not permitted'
TRANSACTION_NOT_FOUND_DETAIL = 'Transaction not found'
ADMIN_ONLY_WORDING = 'Only administrators can moderate ratings'
USER_NOT_FOUND_WORDING = 'User not found'
RATING_NOT_FOUND_WORDING = 'Rating not found'

# What an unreachable datastore tells a caller, and how long to wait.
#
# Pinned for a further reason beyond the one above: this sentence is the
# one place a failure could leak infrastructure detail, so it is asserted
# EXACTLY rather than by substring. It names no provider, no host, no
# operation and no query - and a test that only checked for the word
# "temporary" would not notice a later revision appending the exception's
# own text to it.
DATASTORE_UNAVAILABLE_WORDING = (
    'The service could not reach its datastore. This is temporary - '
    'please retry shortly.'
)
RETRY_AFTER_SECONDS = '5'

# The exception classes whose published wording is pinned above, mapped
# to the literal. Asserted once, in
# ``TestPublishedRefusalWording``, so every OTHER test in this module can
# go on referring to the class - which keeps each individual assertion
# readable - without any of them silently depending on the class to be
# its own oracle.
#: What a caller is told when the transaction's own stored record cannot
#: support a rating - a malformed counterparty, a missing listing
#: reference, or a counterparty who has no account. It names no field and
#: no identifier, because none of it is the caller's doing and the
#: specifics belong in the log.
STORED_RECORD_UNRATABLE_DETAIL = (
    'This transaction is missing information a rating requires, so it '
    'cannot be rated until its record is repaired'
)


PINNED_DOMAIN_WORDING = (
    (RaterNotVerified, UNVERIFIED_RATER_DETAIL),
    (NotATransactionParticipant, NON_PARTICIPANT_DETAIL),
    (DuplicateRating, DUPLICATE_RATING_DETAIL),
    (TransactionNotCompleted, INCOMPLETE_TRANSACTION_DETAIL),
    (SelfRatingNotAllowed, SELF_RATING_DETAIL),
    (TransactionNotFound, TRANSACTION_NOT_FOUND_DETAIL),
    (TransactionInvariantError, STORED_RECORD_UNRATABLE_DETAIL),
)


def pinned_wording(failure):
    """Return the published sentence a named refusal must carry.

    Args:
        failure: One of the domain exception classes in
            :data:`PINNED_DOMAIN_WORDING`.

    Returns:
        The literal this module pins for it.

    Raises:
        AssertionError: The class has no pinned wording, which means a
            refusal is being asserted against nothing more than itself.
    """
    for candidate, wording in PINNED_DOMAIN_WORDING:
        if candidate is failure:
            return wording
    raise AssertionError(
        'No published wording is pinned for {0}, so a test asserting it '
        'would be comparing the implementation against itself. Add the '
        'sentence to PINNED_DOMAIN_WORDING.'.format(failure.__name__)
    )


def mint_token(claims):
    """Sign an arbitrary claim set with the application's own key.

    ``conftest.access_token`` always emits a well-formed ``sub`` and
    ``exp``, which is right for every test that wants a valid caller and
    useless for the ones below that need a MALFORMED credential: a token
    with no expiry, no subject, or a subject that is not a usable
    document ID. Those are the credentials an attacker or a mis-issuing
    service presents, so they have to be minted rather than described.

    Signed with the real key and algorithm on purpose. An unsigned or
    wrongly-signed token is refused by the signature check and would
    never reach the claim requirements this exists to exercise.

    Args:
        claims: The exact claim set to encode, with nothing added.

    Returns:
        An ``Authorization`` header dict carrying the encoded token.
    """
    token = jwt.encode(
        claims,
        settings.SECRET_KEY,
        algorithm=settings.ALGORITHM,
    )
    return {'Authorization': 'Bearer {0}'.format(token)}


# A perfectly usable document ID that NO fixture seeds a user at. It is
# what a transaction naming a counterparty who has no account looks like,
# and it has to be a value no arrangement here ever stores - otherwise
# the guard under test would be satisfied by accident.
ABSENT_USER_ID = 'test-absent-user-00000001'


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

    def request_for(self, path):
        """Build the smallest request object a handler needs.

        Used where a handler has to be exercised for a path no request
        through the client can reach - the other routers all
        authenticate before they validate, so their validation layer is
        unreachable from here. The scope carries only what the handler
        under test reads.

        Args:
            path: The request path.

        Returns:
            A ``starlette.requests.Request``.
        """
        return Request({
            'type': 'http',
            'method': 'POST',
            'path': path,
            'raw_path': path.encode(),
            'query_string': b'',
            'headers': [],
        })

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

    def read_user_aggregate(self, user_id, headers=None):
        """GET one user's reputation summary, without the reviews.

        The reputation badge's read. Public for the same reason as the
        full read above, and deliberately reachable with no credentials in
        these tests.

        Args:
            user_id: The rated user.
            headers: Credentials, if a test is proving they are not
                required.

        Returns:
            The ``httpx`` response.
        """
        return self.client.get(
            '{0}/user/{1}/aggregate'.format(RATINGS_PATH, user_id),
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

        The expected sentence is looked up in
        :data:`PINNED_DOMAIN_WORDING` - a literal written in this module -
        rather than read off ``failure.message``. Comparing a response
        against the very attribute that produced it proves the router and
        the service agree and says nothing about what the user is told,
        so two messages could be swapped, or one replaced with a bare
        "Forbidden", with every such assertion still passing.

        Args:
            response: The response to check.
            status: The expected status code.
            failure: The domain exception class the guard raises. Used to
                NAME the expected refusal, not to supply its wording.

        Raises:
            AssertionError: The status, or the published sentence, is not
                the one this contract specifies.
        """
        self.assertEqual(response.status_code, status)
        detail = response.json()['detail']
        self.assertEqual(detail, pinned_wording(failure))
        # The interface renders this verbatim, so it has to be a
        # sentence rather than a code. Asserted alongside the literal
        # because the two answer different questions: the literal says
        # which sentence, this says that a sentence is there at all.
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

    def test_submit_rating_with_an_unusable_counterparty_conflicts(self):
        """A stored participant that cannot name a user document.

        The request is entirely well formed and the caller is authorized;
        what refuses it is the stored state of the transaction, which is
        why 409 is the accurate answer - the same status
        ``TransactionNotCompleted`` gets, for the same reason.

        This used to be answered 500 AFTER the rating had been written:
        the counterparty was recorded first and the grammar-bound
        response model refused it afterwards, leaving a rating that could
        not be serialised, could not be published (``users/{ratee_id}``
        with a slash in it resolves a nested path) and answered a retry
        with a duplicate conflict. The no-write assertion is therefore
        half of this test rather than a flourish on it.
        """
        buyer = seed_user(verified_buyer())
        seed_user(verified_seller())
        # Written as a raw body: the ``Transaction`` model types every
        # field, so a document like this can only come from a datastore
        # with no server-side schema - which is what Firestore is.
        fake_db.seed('transactions', 'test-transaction-000009', {
            'id': 'test-transaction-000009',
            'buyer_id': buyer.id,
            'seller_id': 'seller/nested/path',
            'vehicle_listing_id': 'test-listing-0000000001',
            'amount': 12500.0,
            'status': 'completed',
            'stripe_payment_intent_id': 'pi_test_000000000000001',
        })

        response = self.submit(
            auth_headers(buyer), 'test-transaction-000009'
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.json()['detail'],
            TransactionInvariantError.message,
        )
        self.assert_no_rating_written()

        # The eligibility endpoint keeps its own 200 contract and reports
        # the same sentence, without echoing the unusable identifier.
        decision = self.client.get(
            '{0}/eligibility/{1}'.format(
                RATINGS_PATH, 'test-transaction-000009'
            ),
            headers=auth_headers(buyer),
        )
        self.assertEqual(decision.status_code, 200)
        body = decision.json()
        self.assertFalse(body['eligible'])
        self.assertEqual(body['reason'], TransactionInvariantError.message)
        self.assertIsNone(body['ratee_id'])
        self.assertIsNone(body['direction'])

    def test_submit_rating_without_a_counterparty_account_conflicts(self):
        """A rating is never recorded about somebody who is not there.

        The transaction is completed and well formed and its counterparty
        ID is usable; there is simply no user document at it. Answered
        201 previously, which created a rating that could never publish
        and never move an aggregate, because publication requires the
        ratee's document to credit the score to.
        """
        buyer = seed_user(verified_buyer())
        transaction = seed_transaction(
            completed_transaction(seller_id=ABSENT_USER_ID)
        )
        self.assertFalse(fake_db.exists(USERS_COLLECTION, ABSENT_USER_ID))

        response = self.submit(auth_headers(buyer), transaction.id)

        self.assert_refusal(response, 409, TransactionInvariantError)
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

    def test_submit_rating_survives_an_underscore_bearing_key(self):
        """Two legitimate ratings whose keys used to collide.

        A rating's document ID is its natural key -
        ``(transaction_id, rater_id)`` - and an underscore is a legal
        Firestore document-ID character, so a plain join of the two was
        AMBIGUOUS: ``('tx_alpha', rater)`` and ``('tx', 'alpha_' + rater)``
        composed the same key. The second submission below was then
        refused 409 as a duplicate of a rating by a different person on a
        different transaction, and the datastore backed that up.

        Driven through the endpoint rather than the service, because 409
        is what a caller actually saw.
        """
        rater = 'rater-underscore-000001'
        pairs = (
            ('tx-underscore_alpha', rater),
            ('tx-underscore', 'alpha_' + rater),
        )
        self.assertEqual(
            '{0}_{1}'.format(*pairs[0]),
            '{0}_{1}'.format(*pairs[1]),
        )

        created = []
        for transaction_id, rater_id in pairs:
            buyer = verified_buyer(user_id=rater_id)
            seller = verified_seller()
            self.arrange(
                buyer=buyer,
                seller=seller,
                transaction=completed_transaction(
                    transaction_id=transaction_id,
                    buyer_id=rater_id,
                ),
            )
            response = self.submit(
                auth_headers(buyer), transaction_id, score=4
            )
            self.assertEqual(response.status_code, 201)
            self.assertEqual(
                response.json()['id'],
                rating_document_id(transaction_id, rater_id),
            )
            created.append(response.json()['id'])

        self.assertEqual(len(set(created)), 2)
        self.assertEqual(fake_db.count(RATINGS_COLLECTION), 2)

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

    def test_submit_rating_reports_an_average_equal_to_its_scores(self):
        """The endpoint-level guard against a drifting running mean.

        Two scores cannot expose the defect this covers: their mean has
        a two-decimal form, so a fold that stored a ROUNDED average would
        answer correctly and be wrong on the next rating. Seven do.
        (1, 1, 1, 1, 1, 2, 1) has a mean of 8/7, which presents as 1.14,
        and the rounded recurrence reported 1.15 - a figure that is not
        the mean of any set of scores this seller received.

        Driven entirely through the API, so what is asserted is the
        number a reader is actually shown.
        """
        scores = [1, 1, 1, 1, 1, 2, 1]
        seller = seed_user(verified_seller())
        for index, score in enumerate(scores):
            buyer = seed_user(verified_buyer(
                user_id='test-buyer-{0:015d}'.format(index),
            ))
            transaction = seed_transaction(completed_transaction(
                transaction_id='test-transaction-{0:06d}'.format(index),
                buyer_id=buyer.id,
                seller_id=seller.id,
            ))
            self.reveal_pair(
                buyer,
                seller,
                transaction,
                buyer_score=score,
                seller_score=settings.RATING_MAX,
            )

        aggregate = self.read_user_ratings(seller.id).json()['aggregate']
        self.assertEqual(aggregate['count'], len(scores))
        self.assertEqual(
            aggregate['average'],
            round(sum(scores) / len(scores), 2),
        )
        self.assertEqual(aggregate['average'], 1.14)
        # The stored value is the exact mean, so the total it implies is
        # the sum of the scores themselves - which is what the next fold
        # reconstructs.
        stored_average, stored_count = self.stored_aggregate(seller.id)
        self.assertEqual(
            round(stored_average * stored_count),
            sum(scores),
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


# Test cases for the one error envelope every failure is rendered in.


class TestErrorEnvelope(RatingAPITestCase):
    """Every refusal this API answers has ONE body shape.

    The shape is ``{"detail": "<sentence>", "errors": [...]}``, with
    ``errors`` present only where request validation located specific
    fields. It matters because 422 is reachable two ways - the framework
    rejecting a field, and a domain refusal the service raises - and
    those two used to answer with incompatible bodies under the same
    status code: a string ``detail`` from one and a LIST of issue objects
    from the other. Only one of the two could be declared in the OpenAPI
    document, so a generated client had no type for the other, and the
    official client had to inspect the runtime type of ``detail`` to tell
    which it had been sent.

    Each test below therefore asserts the SHAPE rather than only the
    status code, and the last two assert the two properties that make the
    shape trustworthy: that it is what the published contract says, and
    that it stops at this feature's own paths.
    """

    def envelope(self, response, status=422):
        """Assert one response is the envelope and return it.

        Args:
            response: The response to check.
            status: The status code expected.

        Returns:
            The parsed body.
        """
        self.assertEqual(response.status_code, status)
        body = response.json()
        self.assertIsInstance(body, dict)
        # A sentence, always - never a list, never empty, never a code.
        self.assertIsInstance(body['detail'], str)
        self.assertTrue(body['detail'].strip())
        return body

    def field_errors(self, body):
        """Assert the per-field list is well formed and return it.

        Args:
            body: A parsed envelope carrying ``errors``.

        Returns:
            The list of field errors.
        """
        errors = body['errors']
        self.assertIsInstance(errors, list)
        self.assertTrue(errors)
        for problem in errors:
            self.assertEqual(
                sorted(problem.keys()), ['loc', 'msg', 'type']
            )
            self.assertIsInstance(problem['loc'], list)
            self.assertTrue(
                all(isinstance(part, str) for part in problem['loc'])
            )
            self.assertIsInstance(problem['msg'], str)
            self.assertIsInstance(problem['type'], str)
        return errors

    def test_body_validation_failure_is_the_envelope(self):
        """A rejected field yields one sentence AND its location."""
        buyer, _seller, transaction = self.arrange()

        response = self.submit(
            auth_headers(buyer),
            transaction.id,
            score=settings.RATING_MAX + 1,
        )

        body = self.envelope(response)
        self.assertIn('score', body['detail'])
        errors = self.field_errors(body)
        self.assertEqual(errors[0]['loc'], ['body', 'score'])
        self.assertTrue(errors[0]['type'])

    def test_validation_failure_names_every_offending_field(self):
        """Two problems in one body are both reported, not just one."""
        buyer, _seller, transaction = self.arrange()

        response = self.submit(
            auth_headers(buyer),
            transaction.id,
            score=settings.RATING_MAX + 1,
            review='x' * (settings.RATING_REVIEW_MAX_LENGTH + 1),
        )

        body = self.envelope(response)
        self.assertIn('score', body['detail'])
        self.assertIn('review', body['detail'])
        located = {tuple(problem['loc']) for problem in
                   self.field_errors(body)}
        self.assertIn(('body', 'score'), located)
        self.assertIn(('body', 'review'), located)

    def test_path_validation_failure_is_the_envelope(self):
        """A path parameter is validated into the same shape as a body."""
        buyer = seed_user(verified_buyer())

        response = self.read_user_ratings(
            'x' * (DOCUMENT_ID_MAX_LENGTH + 1), auth_headers(buyer)
        )

        body = self.envelope(response)
        self.assertIn('user_id', body['detail'])
        self.assertEqual(
            self.field_errors(body)[0]['loc'], ['path', 'user_id']
        )

    def test_domain_refusal_is_the_same_envelope_without_errors(self):
        """A well-formed request refused on the merits has no field.

        Self-rating answers the same 422 as a bad score, so the shape has
        to match - and there is no offending input to point at, because
        the caller sent nothing wrong: the transaction names them as both
        parties. ``errors`` is therefore absent or null, which is exactly
        what the declared model permits.
        """
        buyer = seed_user(verified_buyer())
        transaction = seed_transaction(degenerate_transaction())

        response = self.submit(auth_headers(buyer), transaction.id)

        body = self.envelope(response)
        self.assertEqual(body['detail'], SelfRatingNotAllowed.message)
        self.assertIsNone(body.get('errors'))

    def test_moderation_refusal_is_the_envelope(self):
        """The moderation matrix answers 422 in the same shape.

        A rejection with no policy reason is refused during request
        validation, which is what keeps a rating from being withheld
        first and justified afterwards - and it has to arrive in the same
        envelope as every other refusal from this API.
        """
        buyer, seller, transaction = self.arrange()
        admin = seed_user(admin_user())
        self.reveal_pair(buyer, seller, transaction)
        rating_id = rating_document_id(transaction.id, buyer.id)

        response = self.moderate(
            rating_id, auth_headers(admin), ModerationStatus.REJECTED
        )

        body = self.envelope(response)
        self.assertIn('reason', body['detail'])
        self.assertEqual(
            self.field_errors(body)[0]['loc'],
            ['body', 'moderation_reason'],
        )

    def test_authorization_refusals_are_the_same_envelope(self):
        """403 and 404 carry the same shape as 422, with no errors."""
        buyer, _seller, transaction = self.arrange(
            buyer=unverified_user()
        )

        refused = self.submit(auth_headers(buyer), transaction.id)
        missing = self.submit(
            auth_headers(seed_user(verified_buyer())), 'no-such-txn'
        )

        for response, status in ((refused, 403), (missing, 404)):
            body = self.envelope(response, status=status)
            self.assertIsNone(body.get('errors'))

    def test_every_ratings_failure_declares_the_envelope(self):
        """The published contract says one model for every failure.

        Read off ``app.openapi()`` rather than from the decorators,
        because the generated document is what a client is built from -
        and the specific defect this guards against is invisible in the
        source: FastAPI declares 422 as its own ``HTTPValidationError``
        unless an operation overrides it, so an endpoint can publish a
        body shape its handler never sends without anything in the code
        looking wrong.
        """
        spec = app.openapi()
        checked = 0
        for path, operations in spec['paths'].items():
            if not path.startswith(RATINGS_PATH):
                continue
            for method, operation in operations.items():
                for status, response in operation['responses'].items():
                    if not str(status).startswith(('4', '5')):
                        continue
                    schema = (
                        response.get('content', {})
                        .get('application/json', {})
                        .get('schema', {})
                    )
                    self.assertEqual(
                        schema.get('$ref'),
                        '#/components/schemas/ErrorDetail',
                        '{0} {1} declares {2} as {3}'.format(
                            method.upper(), path, status, schema
                        ),
                    )
                    checked += 1
        # Guards against the loop passing vacuously: five operations,
        # each declaring several failures.
        self.assertGreaterEqual(checked, 20)
        envelope = spec['components']['schemas']['ErrorDetail']
        self.assertEqual(envelope['required'], ['detail'])
        self.assertIn('errors', envelope['properties'])

    def test_the_envelope_stops_at_this_features_paths(self):
        """Other routers keep the framework's body, byte for byte.

        The handler is registered on the APPLICATION, so it sees
        validation failures from the listings, transactions and messages
        routers as well. Those are published contracts this feature does
        not own, and each declares 422 as ``HTTPValidationError``;
        reshaping their bodies while leaving that schema in place would
        recreate, for them, the exact mismatch this envelope removes.

        Asserted against the handler directly because those endpoints all
        authenticate before they validate, so no request through the
        client can reach their validation layer.
        """
        errors = [{
            'loc': ('body', 'title'),
            'msg': 'field required',
            'type': 'value_error.missing',
        }]
        failure = RequestValidationError(errors)

        outside = asyncio.get_event_loop().run_until_complete(
            validation_failure_handler(
                self.request_for('/api/listings/listings'), failure
            )
        )
        inside = asyncio.get_event_loop().run_until_complete(
            validation_failure_handler(
                self.request_for(RATINGS_PATH), failure
            )
        )

        outside_body = json.loads(outside.body)
        self.assertIsInstance(outside_body['detail'], list)
        self.assertNotIn('errors', outside_body)
        inside_body = json.loads(inside.body)
        self.assertIsInstance(inside_body['detail'], str)
        self.assertIn('title', inside_body['detail'])
        self.assertEqual(
            inside_body['errors'][0]['loc'], ['body', 'title']
        )

    def test_a_long_validation_failure_summarises_its_tail(self):
        """The sentence stays readable; the list stays complete.

        A body can fail in many places at once, and ``detail`` is
        rendered verbatim to a user, so it names a bounded number of
        fields and says how many more there are. Nothing is withheld:
        every problem is still in ``errors``.
        """
        buyer = seed_user(verified_buyer())
        payload = {'transaction_id': 'x' * 200, 'score': 42}
        payload.update({
            'review': 'y' * (settings.RATING_REVIEW_MAX_LENGTH + 1),
        })

        response = self.client.post(
            RATINGS_PATH, json=payload, headers=auth_headers(buyer)
        )

        body = self.envelope(response)
        errors = self.field_errors(body)
        self.assertGreaterEqual(len(errors), 3)
        self.assertLessEqual(
            body['detail'].count(';'), VALIDATION_DETAIL_LIMIT
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

    def test_read_user_aggregate_returns_the_summary_alone(self):
        """The badge's endpoint answers two fields and no list.

        The shape is the point. This read exists because the badge was
        being served the full envelope - a page of ratings with their
        review text, produced by a ratings query the client had no use
        for - and the saving is only real if the response genuinely
        carries neither.
        """
        buyer, seller, transaction = self.arrange()
        self.reveal_pair(
            buyer, seller, transaction, buyer_score=4, seller_score=5
        )

        response = self.read_user_aggregate(seller.id)

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body, {'average': 4.0, 'count': 1})
        self.assertNotIn('items', body)

    def test_read_user_aggregate_agrees_with_the_full_read(self):
        """One reputation, whichever endpoint a reader reaches first.

        A buyer meets the badge on a listing and the same figure on the
        seller's profile. Two endpoints reporting different numbers would
        be indistinguishable from one of them being wrong, so this asserts
        they are the same object.
        """
        buyer, seller, transaction = self.arrange()
        self.reveal_pair(
            buyer, seller, transaction, buyer_score=3, seller_score=3
        )

        summary = self.read_user_aggregate(seller.id).json()
        envelope = self.read_user_ratings(seller.id).json()

        self.assertEqual(summary, envelope['aggregate'])

    def test_read_user_aggregate_is_public_and_needs_no_credentials(self):
        """An unrated user is a state, not an error, and not a zero."""
        seller = seed_user(verified_seller())
        self.assertEqual(app.dependency_overrides, {})

        response = self.read_user_aggregate(seller.id)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'average': None, 'count': 0})

    def test_read_user_aggregate_for_unknown_user_not_found(self):
        # The same distinction the full read draws: "no such user" is a
        # 404, "never rated" is a 200 carrying null and zero.
        response = self.read_user_aggregate('no-such-user')

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()['detail'], USER_NOT_FOUND_DETAIL)
        self.assert_no_rating_written()

    def test_read_user_aggregate_publishes_a_rating_whose_window_elapsed(self):
        """Cheaper, and still never stale - the boundary of the saving.

        An aggregate-only MODE was removed from the full read because it
        answered from the user document without settling ratings that were
        already due, which made the badge the one figure allowed to wait
        on a worker that will never run. This endpoint drops the listing,
        not the settlement, and this is the case that says so: the rating
        below is past its window and nothing else will ever reveal it.
        """
        buyer, seller, transaction = self.arrange()
        overdue = utc_now() - timedelta(
            days=settings.RATING_WINDOW_DAYS + 1
        )
        seed_rating(
            transaction_id=transaction.id,
            rater_id=buyer.id,
            ratee_id=seller.id,
            score=2,
            created_at=overdue,
        )
        self.assertEqual(self.stored_aggregate(seller.id), (None, 0))

        body = self.read_user_aggregate(seller.id).json()

        self.assertEqual(body, {'average': 2.0, 'count': 1})
        # Settled durably rather than only for this response, and the
        # record itself moved - not just the number reported.
        self.assertTrue(
            self.stored_rating(transaction.id, buyer.id)['is_published']
        )
        self.assertEqual(self.stored_aggregate(seller.id), (2.0, 1))

    def test_read_user_aggregate_excludes_an_unrevealed_rating(self):
        """The double-blind reveal is not relaxed by the cheap path."""
        buyer, seller, transaction = self.arrange()
        seed_rating(
            transaction_id=transaction.id,
            rater_id=buyer.id,
            ratee_id=seller.id,
            score=5,
        )

        response = self.read_user_aggregate(seller.id)

        self.assertEqual(response.json(), {'average': None, 'count': 0})

    def test_read_user_aggregate_refuses_a_malformed_user_id(self):
        """Grammar is applied by the path parameter, before any read.

        ``DocumentId`` bounds the parameter, so an ID the datastore would
        refuse is a 422 from Pydantic rather than a request that reaches
        the service - the same treatment the full read gives it.
        """
        response = self.read_user_aggregate('__reserved__')

        self.assertEqual(response.status_code, 422)
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

    def test_eligibility_without_a_counterparty_account_reports_it(self):
        """An unratable record is reported, never raised, by this read.

        The interface asks this endpoint whether to offer the submission
        control, so every refusal the write path enforces has to come
        back as a decision it can render. A counterparty with no user
        document is one of them: the answer is ``eligible=false`` with
        the same sentence submission answers 409 with, and no
        counterparty context at all, because nothing can be rated
        against this transaction until an operator repairs it.
        """
        buyer = seed_user(verified_buyer())
        transaction = seed_transaction(
            completed_transaction(seller_id=ABSENT_USER_ID)
        )

        response = self.read_eligibility(
            transaction.id, auth_headers(buyer)
        )

        self.assertEqual(response.status_code, 200)
        decision = response.json()
        self.assertFalse(decision['eligible'])
        self.assertEqual(
            decision['reason'], TransactionInvariantError.message
        )
        self.assertIsNone(decision['ratee_id'])
        self.assertIsNone(decision['direction'])
        self.assertFalse(decision['already_rated'])
        self.assert_no_rating_written()

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

    def test_moderate_rating_audits_the_admin_and_not_the_reason(self):
        """The decision is attributable, and the words stay off the log.

        The endpoint is the only way moderation state moves, and it
        removes somebody's words from view, so who did it has to be
        recoverable - this codebase has no audit collection to
        reconstruct that from. The reason itself is persisted on the
        document and must never reach the log: it describes a violation
        in a review, so it can carry the abusive wording or the personal
        details that were the violation, and it keeps the newlines the
        content normaliser preserves.
        """
        _seller, admin, rating_id = self.arrange_published_rating(
            review='Something a moderator objected to'
        )

        with self.assertLogs('app.services.rating', level='INFO') as logs:
            response = self.moderate(
                rating_id,
                auth_headers(admin),
                ModerationStatus.REJECTED,
                POLICY_REASON,
            )

        self.assertEqual(response.status_code, 200)
        audit = [line for line in logs.output if 'Moderation' in line]
        self.assertEqual(len(audit), 1, logs.output)
        self.assertIn('actor={0}'.format(admin.id), audit[0])
        self.assertIn(rating_id, audit[0])
        self.assertIn('reason_recorded=True', audit[0])
        self.assertNotIn(POLICY_REASON, audit[0])
        self.assertNotIn('phone number', audit[0])
        # The reason is on the record the administrator just read back.
        self.assertEqual(
            response.json()['moderation_reason'], POLICY_REASON
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


# Test cases for the wording this API publishes, asserted against
# literals rather than against the implementation.


class TestPublishedRefusalWording(unittest.TestCase):
    """The sentences this API tells people, pinned to literal text.

    Every other refusal test in this module names the guard it expects by
    its exception class, which keeps those tests readable. This class is
    what stops that convenience from becoming circular: it is the single
    place the class is tied to the SENTENCE, so a message reworded,
    truncated to a code, or swapped with another guard's fails here.

    Without it the module's refusal assertions reduce to "the router
    publishes whatever the service says", which is true by construction
    and unfalsifiable - and the wording is not an internal detail: the
    submission form renders a ``detail`` verbatim, and the eligibility
    endpoint reuses the same sentence as the explanation beside a
    disabled control.
    """

    def test_every_pinned_refusal_carries_its_published_sentence(self):
        """The service's message is the sentence pinned here."""
        for failure, wording in PINNED_DOMAIN_WORDING:
            self.assertEqual(failure.message, wording, failure.__name__)

    def test_the_router_composes_the_sentences_pinned_here(self):
        """The three refusals the router words itself."""
        self.assertEqual(ADMIN_ONLY_DETAIL, ADMIN_ONLY_WORDING)
        self.assertEqual(USER_NOT_FOUND_DETAIL, USER_NOT_FOUND_WORDING)
        self.assertEqual(
            RATING_NOT_FOUND_DETAIL, RATING_NOT_FOUND_WORDING
        )

    def test_the_unavailability_sentence_leaks_no_infrastructure(self):
        """An outage is not an invitation to publish internals.

        Asserted as an exact literal AND as an absence, because the two
        catch different regressions: the literal catches a rewording, and
        the substring checks catch a later revision appending the
        exception's own text - a hostname, a project id, a query - to an
        otherwise-correct sentence.
        """
        self.assertEqual(
            DATASTORE_UNAVAILABLE_DETAIL,
            DATASTORE_UNAVAILABLE_WORDING,
        )
        lowered = DATASTORE_UNAVAILABLE_DETAIL.lower()
        for leak in (
            'firestore',
            'google',
            'grpc',
            'localhost',
            'traceback',
            'project',
            'collection',
            'query',
        ):
            self.assertNotIn(leak, lowered, leak)

    def test_each_pinned_sentence_is_fit_to_render(self):
        """Prose a user can act on, not a code or an identifier.

        The interface renders these verbatim, so each has to read as a
        sentence: capitalised, more than one word, and free of the
        underscores, colons and braces that mark an internal token.
        """
        published = [wording for _f, wording in PINNED_DOMAIN_WORDING]
        published.extend((
            ADMIN_ONLY_WORDING,
            USER_NOT_FOUND_WORDING,
            RATING_NOT_FOUND_WORDING,
            DATASTORE_UNAVAILABLE_WORDING,
        ))
        for wording in published:
            self.assertEqual(wording, wording.strip(), wording)
            self.assertTrue(wording[:1].isupper(), wording)
            for token in ('_', '{', '}', 'Error', 'Exception'):
                self.assertNotIn(token, wording, wording)


# Test cases for what this API does when the datastore cannot answer.


class TestDatastoreUnavailable(RatingAPITestCase):
    """Every route answers 503 with a retry hint, not a bare 500.

    A transient transport fault is the ABSENCE of an answer, and it must
    not be confused with an answer about the request: a caller told 4xx
    stops retrying something that was never wrong, and a caller told
    ``text/plain`` "Internal Server Error" - which is what an unhandled
    exception produces - gets no ``detail`` to parse and nothing to show.

    The fault is injected at the collaborator each handler calls, using
    the real ``google.api_core`` exception classes the production code
    classifies on, so the translation under test is the production one
    rather than a fixture's idea of it. Two different classes are used
    across the five routes deliberately: the classification is a tuple of
    seven exception types, and a test that only ever raised one would not
    notice six of them being dropped from it.

    Each case asserts three things, because a partially correct answer is
    the dangerous one: the status, the exact sanitized sentence, and the
    ``Retry-After`` header that makes the retry advice actionable.
    """

    def assert_unavailable(self, response):
        """Assert one response is the full 503 contract.

        Args:
            response: The response to check.
        """
        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.json()['detail'],
            DATASTORE_UNAVAILABLE_WORDING,
        )
        self.assertEqual(
            response.headers.get('Retry-After'),
            RETRY_AFTER_SECONDS,
        )

    def test_submit_rating_when_the_datastore_is_unreachable(self):
        """POST: 503, and nothing persisted."""
        buyer, _seller, transaction = self.arrange()

        with patch(
            'app.api.ratings.submit_rating',
            side_effect=ServiceUnavailable('backend unavailable'),
        ):
            response = self.submit(
                auth_headers(buyer), transaction.id, score=4
            )

        self.assert_unavailable(response)
        self.assert_no_rating_written()

    def test_read_user_ratings_when_the_datastore_is_unreachable(self):
        """The public read: 503 rather than an empty reputation.

        The important half is that it is not a 200 carrying "no ratings
        yet". A reputation that cannot be read is not a reputation of
        none, and reporting one as the other would understate a rated
        seller on the screen a buyer decides from.
        """
        _buyer, seller, _transaction = self.arrange()

        with patch(
            'app.api.ratings.get_user_reputation',
            side_effect=DeadlineExceeded('deadline exceeded'),
        ):
            response = self.read_user_ratings(seller.id)

        self.assert_unavailable(response)

    def test_read_transaction_ratings_when_unreachable(self):
        """The participant read: 503, with no partial list."""
        buyer, _seller, transaction = self.arrange()

        with patch(
            'app.api.ratings.list_transaction_ratings',
            side_effect=ServiceUnavailable('backend unavailable'),
        ):
            response = self.read_transaction_ratings(
                transaction.id, auth_headers(buyer)
            )

        self.assert_unavailable(response)

    def test_eligibility_when_the_datastore_is_unreachable(self):
        """Eligibility: 503, never a fabricated decision.

        This endpoint exists so the interface can disable a control with
        a reason, so the one thing it must not do on a fault is invent an
        answer - "you may not rate" would be indistinguishable from a
        refusal the server actually reached.
        """
        buyer, _seller, transaction = self.arrange()

        with patch(
            'app.api.ratings.require_eligibility',
            side_effect=DeadlineExceeded('deadline exceeded'),
        ):
            response = self.read_eligibility(
                transaction.id, auth_headers(buyer)
            )

        self.assert_unavailable(response)

    def test_moderation_when_the_datastore_is_unreachable(self):
        """PATCH: 503, and the moderation state untouched."""
        buyer, seller, transaction = self.arrange()
        admin = seed_user(admin_user())
        self.reveal_pair(buyer, seller, transaction)
        rating_id = rating_document_id(transaction.id, buyer.id)

        with patch(
            'app.api.ratings.moderate_rating',
            side_effect=ServiceUnavailable('backend unavailable'),
        ):
            response = self.moderate(
                rating_id,
                auth_headers(admin),
                ModerationStatus.REJECTED,
                reason=POLICY_REASON,
            )

        self.assert_unavailable(response)
        stored = fake_db.document_body(RATINGS_COLLECTION, rating_id)
        self.assertEqual(
            stored['moderation_status'], ModerationStatus.PENDING.value
        )
        self.assertIsNone(stored['moderation_reason'])

    def test_an_unreachable_datastore_fails_the_caller_lookup(self):
        """The FIRST datastore touch of every authenticated route.

        ``get_current_user`` re-reads the caller's document on every
        request, so an outage is noticed there before any handler runs.
        Left unhandled that made an outage of the datastore an outage of
        every endpoint answered as a bare 500; it is translated in the
        dependency itself, with the same sentence and the same header the
        router uses, so a client sees one shape whichever layer noticed.

        Exercised through the ELIGIBILITY route on purpose, because it is
        the only authenticated route whose handler never reads the
        ``users`` collection - it reads the transaction and the rating.
        So failing that one read can only be the dependency's read, which
        is what makes this a test of the dependency rather than of
        whichever layer happened to touch a user first.
        """
        buyer, _seller, transaction = self.arrange()
        headers = auth_headers(buyer)
        real_collection = fake_db.collection

        def fail_on_user_read(collection_id):
            """Fail the users read; serve every other collection."""
            if collection_id == USERS_COLLECTION:
                raise ServiceUnavailable('backend unavailable')
            return real_collection(collection_id)

        with patch.object(
            fake_db, 'collection', side_effect=fail_on_user_read
        ):
            response = self.read_eligibility(transaction.id, headers)

        self.assert_unavailable(response)
        self.assert_no_rating_written()

    def test_the_caller_lookup_is_what_that_fault_came_from(self):
        """The control for the case above: the route works otherwise.

        Without it, a route that answered 503 for some unrelated reason
        would satisfy the previous test. The same request, with the
        ``users`` read intact, has to succeed - which is what pins the
        fault to the one read that was broken.
        """
        buyer, _seller, transaction = self.arrange()

        response = self.read_eligibility(
            transaction.id, auth_headers(buyer)
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['eligible'])

    def test_a_transient_fault_is_never_reported_as_a_refusal(self):
        """503 and 403 must not be confused in either direction.

        The two are answered by adjacent branches of the same handler,
        and conflating them is a real failure rather than a theoretical
        one: a caller told 403 stops retrying a request that was
        perfectly valid, and one told 503 retries a request that will
        never be accepted. So the fault path is asserted NOT to carry any
        of the refusal sentences.
        """
        buyer, _seller, transaction = self.arrange()

        with patch(
            'app.api.ratings.submit_rating',
            side_effect=ServiceUnavailable('backend unavailable'),
        ):
            response = self.submit(
                auth_headers(buyer), transaction.id, score=4
            )

        self.assertEqual(response.status_code, 503)
        detail = response.json()['detail']
        for _failure, wording in PINNED_DOMAIN_WORDING:
            self.assertNotEqual(detail, wording)


# Test cases for a stored transaction too incomplete to rate.


class TestCorruptTransactionRecord(RatingAPITestCase):
    """A completed transaction missing the data a rating requires.

    The odd one out of this API's failure modes, and the reason it is
    asserted separately: the request is well formed, the caller is
    authorized and the transaction is completed, but the STORED record is
    missing the ``vehicle_listing_id`` that every ``Transaction``
    declares and every rating denormalises. Nothing the caller can send
    fixes it.

    It was once left unmapped, on the reasoning that a corrupt document
    is a server fault. The reasoning was half right and the outcome was
    not: an unmapped exception is not answered by this API at all - the
    framework renders it as ``text/plain`` "Internal Server Error", with
    no ``detail`` for a client to parse and nothing for an interface to
    show - and it put two endpoints outside the status set their own
    documentation publishes. 409 is the accurate answer: what refuses the
    request is the stored STATE of the transaction being cited, which is
    exactly why a non-completed transaction is also a 409.
    """

    def arrange_corrupt_transaction(self):
        """Seed a completed transaction with no listing reference.

        Returns:
            The stored ``(buyer, transaction)``.
        """
        buyer, _seller, transaction = self.arrange()
        stored = dict(
            fake_db.document_body(
                TRANSACTIONS_COLLECTION, transaction.id
            )
        )
        stored.pop('vehicle_listing_id', None)
        fake_db.seed(
            TRANSACTIONS_COLLECTION, transaction.id, stored
        )
        return buyer, transaction

    def test_submit_rating_for_a_corrupt_transaction_conflicts(self):
        """409 with a sentence, and no rating written."""
        buyer, transaction = self.arrange_corrupt_transaction()

        response = self.submit(
            auth_headers(buyer), transaction.id, score=4
        )

        self.assertEqual(response.status_code, 409)
        detail = response.json()['detail']
        self.assertIsInstance(detail, str)
        self.assertTrue(detail.strip())
        self.assert_no_rating_written()

    def test_the_conflict_names_no_field_document_or_datastore(self):
        """The caller is told the record needs repair, and no more.

        The specific defect is logged for an operator with the
        transaction named; the RESPONSE names neither the field nor the
        collection, because a caller cannot act on either and an attacker
        should not be handed a description of the stored shape.
        """
        buyer, transaction = self.arrange_corrupt_transaction()

        response = self.submit(
            auth_headers(buyer), transaction.id, score=4
        )

        lowered = response.json()['detail'].lower()
        for leak in (
            'vehicle_listing_id',
            'transactions',
            'firestore',
            'document',
            transaction.id.lower(),
        ):
            self.assertNotIn(leak, lowered, leak)

    def test_eligibility_for_a_corrupt_transaction_still_answers(self):
        """Eligibility keeps its 200/401/404 contract.

        The write path answers 409 here, and this endpoint must NOT:
        its whole purpose is to explain, up front, why a control is
        unavailable, so it reports the same sentence as an ineligible
        decision rather than failing the request. An interface that got a
        409 from the check it makes on mount would have nothing to
        render.
        """
        buyer, transaction = self.arrange_corrupt_transaction()

        response = self.read_eligibility(
            transaction.id, auth_headers(buyer)
        )

        self.assertEqual(response.status_code, 200)
        decision = response.json()
        self.assertFalse(decision['eligible'])
        self.assertIsInstance(decision['reason'], str)
        self.assertTrue(decision['reason'].strip())
        self.assertFalse(decision['already_rated'])
        self.assert_no_rating_written()


# Test cases for the credentials this API accepts and refuses.


class TestCredentialHardening(RatingAPITestCase):
    """What a token has to carry before it names a caller.

    ``get_current_user`` is the only gate in front of every protected
    rating route, and the JWT it reads carries just two claims - so each
    of the ways that token can be wrong is a way into the feature. All of
    them are answered with the same 401, deliberately: a caller learns
    that the credential could not be validated and nothing about which
    part of it the server disliked.

    Every token here is signed with the REAL key, so none of these tests
    is answered by the signature check on its way past the requirements
    they exist to exercise.
    """

    def setUp(self):
        super().setUp()
        self.buyer, _seller, self.transaction = self.arrange()

    def assert_rejected(self, headers):
        """Assert a credential is refused and reaches no handler.

        Args:
            headers: The ``Authorization`` header to send.
        """
        response = self.submit(headers, self.transaction.id, score=4)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(
            response.headers.get('WWW-Authenticate'), 'Bearer'
        )
        self.assert_no_rating_written()
        return response

    def test_a_token_with_no_expiry_is_refused(self):
        """An unexpiring credential cannot be revoked by time.

        There is no deny-list in this system, so time is the only thing
        that ends a token's life. python-jose does not require ``exp`` by
        default, so a token issued without one was accepted forever -
        which is why the requirement is stated explicitly rather than
        left to the library's default.
        """
        self.assert_rejected(mint_token({'sub': self.buyer.id}))

    def test_a_token_with_no_subject_is_refused(self):
        """A credential that names nobody cannot name a caller.

        Refused at the decode, not later as a ``None`` that would be
        interpolated into a document path.
        """
        self.assert_rejected(mint_token({
            'exp': utc_now() + timedelta(minutes=30),
        }))

    def test_a_token_whose_subject_is_not_a_string_is_refused(self):
        """A numeric subject is not a document ID."""
        self.assert_rejected(mint_token({
            'sub': 12345,
            'exp': utc_now() + timedelta(minutes=30),
        }))

    def test_a_subject_containing_a_slash_is_refused(self):
        """The path-injection case, and the reason the grammar exists.

        Firestore reads a slash as a path separator, so a subject of
        ``users/somebody`` addresses something other than the document it
        appears to name - or raises inside the client. Either way it is a
        credential that does not name a user, so it is refused before it
        is used to compose a path rather than surfacing as a 500 on every
        protected endpoint.
        """
        self.assert_rejected(mint_token({
            'sub': 'users/{0}'.format(self.buyer.id),
            'exp': utc_now() + timedelta(minutes=30),
        }))

    def test_relative_path_subjects_are_refused(self):
        """``.`` and ``..`` are path segments, not identifiers."""
        for subject in ('.', '..', '../users/somebody'):
            with self.subTest(subject=subject):
                self.assert_rejected(mint_token({
                    'sub': subject,
                    'exp': utc_now() + timedelta(minutes=30),
                }))

    def test_a_reserved_namespace_subject_is_refused(self):
        """``__.*__`` is the namespace Firestore keeps for itself."""
        self.assert_rejected(mint_token({
            'sub': '__id__',
            'exp': utc_now() + timedelta(minutes=30),
        }))

    def test_an_oversized_subject_is_refused(self):
        """A key too long to be typed back is not an identifier.

        Refused twice over, and the redundancy is deliberate rather than
        accidental: the dependency's grammar caps a subject at the same
        128 characters ``User.id`` does, so even a stored document at such
        a key could not produce a caller. What this asserts is the
        OUTCOME - one 401, no handler reached - which is the contract, and
        it holds whichever of the two layers answers first.
        """
        self.assert_rejected(mint_token({
            'sub': 'x' * 200,
            'exp': utc_now() + timedelta(minutes=30),
        }))

    def test_a_subject_carrying_a_control_character_is_refused(self):
        """A newline in an identifier is how a log line is forged.

        Layered in the same way as the oversized case above: the grammar
        excludes the C0 range and DEL, and no document can exist at such
        a key in any event. The assertion is the published outcome.
        """
        self.assert_rejected(mint_token({
            'sub': 'caller\nADMIN',
            'exp': utc_now() + timedelta(minutes=30),
        }))

    def test_an_empty_subject_is_refused(self):
        """The empty string names no document."""
        self.assert_rejected(mint_token({
            'sub': '',
            'exp': utc_now() + timedelta(minutes=30),
        }))

    def test_a_malformed_user_document_cannot_authenticate(self):
        """A stored document that cannot satisfy the model is a 401.

        Not a 500, and not a partially-built caller either. The
        credential may be perfectly valid; what is missing is an identity
        the server can establish from its own record, so the answer is
        the same "could not validate credentials" as a token naming
        nobody - and the stored shape is never described in the response.
        """
        stored = dict(
            fake_db.document_body(USERS_COLLECTION, self.buyer.id)
        )
        # ``is_verified`` is StrictBool precisely so a string cannot be
        # read as a verification flag by truthiness. A document carrying
        # one is malformed, and must not authenticate as verified.
        stored['is_verified'] = 'true'
        fake_db.seed(USERS_COLLECTION, self.buyer.id, stored)

        response = self.assert_rejected(auth_headers(self.buyer))

        lowered = response.json()['detail'].lower()
        for leak in ('is_verified', 'validation', 'pydantic', 'field'):
            self.assertNotIn(leak, lowered, leak)

    def test_a_token_naming_an_unknown_user_is_refused(self):
        """A well-formed subject with no document behind it.

        The subject satisfies the grammar, so this is the branch AFTER
        the path check: the read happens and finds nothing.
        """
        self.assert_rejected(auth_headers('test-ghost-000000000001'))

    def test_a_valid_credential_still_reaches_the_handler(self):
        """The positive control for every refusal above.

        Without it, a dependency that rejected EVERY credential would
        satisfy all eleven cases in this class. The same arrangement,
        with a well-formed token, has to be accepted.
        """
        response = self.submit(
            auth_headers(self.buyer), self.transaction.id, score=4
        )

        self.assertEqual(response.status_code, 201)
        self.assertIsNotNone(
            self.stored_rating(self.transaction.id, self.buyer.id)
        )


# Test cases for the scheduled sweep, observed the way a reader would.
class TestScheduledSweepThroughTheApi(RatingAPITestCase):
    """The Celery task, run and then observed over HTTP.

    The window sweep has two routes in this design, and only one of them
    had endpoint coverage. A read settles what is already due, which
    ``test_read_user_ratings_publishes_a_rating_whose_window_elapsed``
    proves; the scheduled task settles the same set without any reader
    present, and that route was asserted nowhere - which matters because
    nothing in this repository dispatches it, so its body is otherwise
    unexecuted code that only a deployment with a worker would ever run.

    The task is invoked directly, which is exactly what a worker does: a
    Celery task object is callable and runs its body in this process, with
    no broker involved.
    """

    def test_the_sweep_makes_an_expired_rating_visible_without_a_reader(self):
        """Published by the TASK, then confirmed over HTTP.

        The ordering is the whole point. The stored state is asserted
        immediately after the task and BEFORE any request, so the reveal
        cannot be attributed to a read settling it - which is what the
        existing read-path test covers and what this one must exclude.
        """
        from app.tasks.background_jobs import publish_expired_rating_window

        buyer, seller, transaction = self.arrange()
        overdue = utc_now() - timedelta(
            days=settings.RATING_WINDOW_DAYS + 1
        )
        seed_rating(
            transaction_id=transaction.id,
            rater_id=buyer.id,
            ratee_id=seller.id,
            score=5,
            created_at=overdue,
        )
        self.assertEqual(self.stored_aggregate(seller.id), (None, 0))

        published = publish_expired_rating_window()

        self.assertEqual(published, 1)
        # Before any request: the task did this, not a reader.
        self.assertTrue(
            self.stored_rating(transaction.id, buyer.id)['is_published']
        )
        self.assertEqual(self.stored_aggregate(seller.id), (5.0, 1))

        body = self.read_user_ratings(seller.id).json()
        self.assertEqual(len(body['items']), 1)
        self.assertTrue(body['items'][0]['is_published'])
        self.assertEqual(body['aggregate'], {'average': 5.0, 'count': 1})
        self.assertEqual(
            self.read_user_aggregate(seller.id).json(),
            {'average': 5.0, 'count': 1},
        )

    def test_the_sweep_leaves_an_unexpired_rating_invisible(self):
        """A rating inside its window is not revealed by the sweep either.

        The double-blind model is not a property of one path: the task
        applies the same deadline the readers do, so running it does not
        become a way to reveal a rating early.
        """
        from app.tasks.background_jobs import publish_expired_rating_window

        buyer, seller, transaction = self.arrange()
        seed_rating(
            transaction_id=transaction.id,
            rater_id=buyer.id,
            ratee_id=seller.id,
            score=1,
        )

        self.assertEqual(publish_expired_rating_window(), 0)

        self.assertFalse(
            self.stored_rating(transaction.id, buyer.id)['is_published']
        )
        self.assertEqual(
            self.read_user_aggregate(seller.id).json(),
            {'average': None, 'count': 0},
        )


# Test cases for the answer given when the datastore cannot be reached.
class TestDatastoreUnavailableResponses(RatingAPITestCase):
    """503 with a ``Retry-After``, on every route, when the provider fails.

    ``app/db/firestore.py`` bounds each datastore operation with a
    wall-clock budget, so an unreachable datastore now ends as a raised
    transient error instead of a hung request. That changes what these
    handlers must do with it, and the answer has to be the same on all six
    routes: 503, the shared detail sentence, and a ``Retry-After`` header
    so a client backs off rather than hammering a datastore that is
    already struggling. A route that answered 500 instead would look like
    a defect in this application and would carry no backoff hint.

    The fault is injected at the SERVICE BOUNDARY the router imports,
    which is where a real one arrives from: the wrapper raises after its
    budget expires, the service lets a permanent-looking fault propagate,
    and this layer's only job is the status code. What the wrapper itself
    does under an unreachable provider - bounded attempts, bounded wall
    clock, no hang - is proved separately in ``test_rating_service.py``,
    against a stub GAPIC client rather than against the in-memory double,
    which has no network to fail.

    Both shapes are exercised: ``ServiceUnavailable``, the raw gRPC
    condition, and ``RetryError``, which is what the bounded retry policy
    raises when it gives up. Those are the two a deployment actually sees.
    """

    UNAVAILABLE = ServiceUnavailable('backend unreachable')

    def assert_unavailable(self, response):
        """Assert one response is the shared 503 contract.

        Args:
            response: The ``httpx`` response under test.
        """
        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.json()['detail'], DATASTORE_UNAVAILABLE_DETAIL
        )
        self.assertEqual(
            response.headers.get('Retry-After'),
            str(DATASTORE_RETRY_AFTER_SECONDS),
        )

    def test_submission_answers_503(self):
        buyer, _seller, transaction = self.arrange()

        with patch(
            'app.api.ratings.submit_rating', side_effect=self.UNAVAILABLE
        ):
            response = self.submit(
                self.credentials(buyer), transaction.id, score=4
            )

        self.assert_unavailable(response)
        self.assert_no_rating_written()

    def test_the_user_read_answers_503(self):
        seller = seed_user(verified_seller())

        with patch(
            'app.api.ratings.get_user_reputation',
            side_effect=self.UNAVAILABLE,
        ):
            response = self.read_user_ratings(seller.id)

        self.assert_unavailable(response)

    def test_the_aggregate_read_answers_503(self):
        seller = seed_user(verified_seller())

        with patch(
            'app.api.ratings.get_user_aggregate',
            side_effect=self.UNAVAILABLE,
        ):
            response = self.read_user_aggregate(seller.id)

        self.assert_unavailable(response)

    def test_the_transaction_read_answers_503(self):
        buyer, _seller, transaction = self.arrange()

        with patch(
            'app.api.ratings.list_transaction_ratings',
            side_effect=self.UNAVAILABLE,
        ):
            response = self.read_transaction_ratings(
                transaction.id, self.credentials(buyer)
            )

        self.assert_unavailable(response)

    def test_the_eligibility_read_answers_503(self):
        buyer, _seller, transaction = self.arrange()

        with patch(
            'app.api.ratings.require_eligibility',
            side_effect=self.UNAVAILABLE,
        ):
            response = self.read_eligibility(
                transaction.id, self.credentials(buyer)
            )

        self.assert_unavailable(response)

    def test_moderation_answers_503(self):
        buyer, seller, transaction = self.arrange()
        admin = seed_user(admin_user())
        self.reveal_pair(
            buyer,
            seller,
            transaction,
            buyer_score=settings.RATING_MIN,
            seller_score=settings.RATING_MAX,
        )
        rating_id = rating_document_id(transaction.id, buyer.id)

        with patch(
            'app.api.ratings.moderate_rating', side_effect=self.UNAVAILABLE
        ):
            response = self.moderate(
                rating_id, auth_headers(admin), ModerationStatus.APPROVED
            )

        self.assert_unavailable(response)

    def test_a_giving_up_retry_is_also_a_503(self):
        """``RetryError`` is the shape a bounded policy actually raises.

        The client's retry gives up by raising ``RetryError`` wrapping the
        last failure, not by re-raising the gRPC error, so a handler that
        recognised only ``ServiceUnavailable`` would answer 500 for the
        very outage the bound exists to survive.
        """
        seller = seed_user(verified_seller())
        # ``RetryError(message, cause)`` - both positional, matching the
        # signature the installed google-api-core declares.
        exhausted = RetryError(
            'deadline of 10.0s exceeded', self.UNAVAILABLE
        )

        with patch(
            'app.api.ratings.get_user_aggregate', side_effect=exhausted
        ):
            response = self.read_user_aggregate(seller.id)

        self.assert_unavailable(response)

    def test_an_unauthenticated_request_is_still_401(self):
        """An outage does not change who may ask.

        Authentication is resolved before any handler body runs, so a
        request with no credentials is refused for that reason and never
        reaches the injected fault. A 503 here would mean the router had
        started doing work for an anonymous caller.
        """
        _buyer, _seller, transaction = self.arrange()

        with patch(
            'app.api.ratings.submit_rating', side_effect=self.UNAVAILABLE
        ):
            response = self.submit(None, transaction.id, score=4)

        self.assertEqual(response.status_code, 401)
        self.assert_no_rating_written()


if __name__ == "__main__":
    unittest.main()
