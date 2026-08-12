"""Test bootstrap, shared builders and the in-memory Firestore double.

This is the repository's first ``conftest.py``. It exists because two
things in this application happen at IMPORT time, and both of them have
to be dealt with before any test module is loaded.

1. ``app/core/config.py`` evaluates ``settings = Settings()`` at module
   scope, and eight of its fields are required with no default. Import
   it without them and pydantic raises a ``ValidationError`` naming all
   eight. So the values are put into ``os.environ`` here, at module
   scope, before the first ``app.*`` import. pytest imports a directory's
   ``conftest.py`` before it collects anything in that directory, which
   is what makes this the right place - and why the seeding is NOT in a
   fixture, which would run far too late.

2. ``app/db/firestore.py`` builds ``db = Client(project=...)`` at module
   scope, and ``google.cloud.client.Client.__init__`` resolves
   credentials EAGERLY by calling ``google.auth.default()``. Where
   application default credentials happen to be present that call
   SUCCEEDS, so an unguarded test run does not fail safely - it builds a
   real Firestore client bound to a real Google Cloud project. The
   neutralisation below is therefore structural rather than hopeful: the
   ``Client`` symbol is replaced on its own source module BEFORE
   ``app.db.firestore`` is imported, so no client is ever constructed and
   ``google.auth`` is never reached, whether or not credentials resolve.

THE IMPORT-ORDER HAZARD
-----------------------------------------------------------------------
``from app.db.firestore import db`` binds the OBJECT, not the module
attribute. Rebinding ``app.db.firestore.db`` afterwards therefore does
not reach a module that has already imported it - that module keeps the
stale object. Five modules import ``db`` that way
(``app/api/auth.py``, ``app/api/listings.py``,
``app/api/transactions.py``, ``app/api/messages.py`` and
``app/services/rating.py``), so the double is installed BEFORE the first
``app.*`` import and each of them binds the double on its own first
import. :func:`rebind_firestore_holders` covers the remaining case of a
module that was somehow imported earlier, and is called as part of the
installation.

This is the single most common way a suite like this silently passes
while testing nothing, so :func:`install_firestore_double` asserts the
outcome rather than assuming it.

FIDELITY, NOT PERMISSIVENESS
-----------------------------------------------------------------------
The double reproduces the semantics the feature's guarantees rest on,
because a permissive stub would turn a proven guarantee into an
unproven claim:

* ``document(doc_id).create(data)`` is create-only and raises the real
  ``google.api_core.exceptions.AlreadyExists`` on a collision. The
  one-vote-per-transaction rule is enforced by document-ID collision and
  by nothing else, so a double that quietly overwrote would make that
  test meaningless.
* ``run_in_transaction`` commits or rolls back. Staged writes become
  visible together on success and vanish entirely if the callback
  raises, so a test can observe the absence of a partial write.
* Firestore's ordering rules are honoured: a document missing a filtered
  field matches no filter, a document missing an ``order_by`` field is
  excluded from the result, and ``__name__`` breaks ties in the
  direction of the last explicit sort.
* Anything the double does not implement raises loudly. Unsupported
  operators, filter types and field transforms are rejected with an
  explanatory error instead of being ignored.

The double implements exactly the surface this repository exercises.
Optional arguments the production code never passes (``retry``,
``timeout``, ``field_paths``, write ``option`` preconditions) are
deliberately absent, so passing one is an immediate ``TypeError``
instead of a silently dropped instruction.

PUBLIC API
-----------------------------------------------------------------------
Bootstrap
    ``REQUIRED_SETTINGS``, :func:`seed_required_settings`,
    :func:`ensure_backend_on_sys_path`, :func:`install_firestore_double`,
    :func:`rebind_firestore_holders`.

The double
    ``fake_db`` (the process-wide singleton), :func:`get_fake_db`, and
    the ``firestore_double`` fixture. Raw-state inspection, which the
    "no rating document was written" assertions need, is
    ``fake_db.raw()``, ``fake_db.documents(collection)``,
    ``fake_db.document_body(collection, doc_id)``,
    ``fake_db.document_ids(collection)``,
    ``fake_db.exists(collection, doc_id)`` and
    ``fake_db.count(collection)``. State is cleared between tests by the
    autouse :func:`reset_firestore_double` fixture, which also restores
    the rating tunables on ``settings``.

Builders (module-level callables, because ``unittest.TestCase`` methods
cannot receive pytest fixtures as arguments)
    :func:`build_user`, :func:`verified_buyer`, :func:`verified_seller`,
    :func:`unverified_user`, :func:`admin_user`,
    :func:`non_participant_user`, :func:`build_transaction`,
    :func:`completed_transaction`, :func:`pending_transaction`,
    :func:`degenerate_transaction`, :func:`build_rating_document`,
    :func:`rating_document_id`.

Seeding and authentication
    :func:`seed_user`, :func:`seed_transaction`, :func:`seed_rating`,
    :func:`user_document`, :func:`transaction_document`,
    :func:`access_token`, :func:`auth_headers`.

Deliberately NOT done here: no ``__init__.py``, ``pytest.ini``,
``setup.cfg``, ``tox.ini``, ``pyproject.toml`` or ``.flake8`` is
created, because the validation criteria assume bare ``pytest`` and
stock ``flake8``; and ``app.tasks.background_jobs`` is never imported,
because it reads a ``CELERY_BROKER_URL`` setting that is deliberately
not declared and would break collection.
"""

import copy
import functools
import os
import sys
import threading
import uuid
from collections import namedtuple
from datetime import datetime, timedelta, timezone

import pytest
from google.api_core.exceptions import AlreadyExists, NotFound
from google.cloud import firestore
from google.cloud.firestore_v1.base_transaction import MAX_ATTEMPTS
from jose import jwt

# The real client class, captured BEFORE the patch below replaces the
# name. :func:`rebind_firestore_holders` uses it to recognise a stale
# client instance precisely, rather than rebinding any attribute that
# happens to be called ``db``.
REAL_FIRESTORE_CLIENT = firestore.Client

# Collection names, mirroring app/services/rating.py so a rename there
# cannot leave the fixtures seeding documents nobody reads.
USERS_COLLECTION = 'users'
TRANSACTIONS_COLLECTION = 'transactions'
RATINGS_COLLECTION = 'ratings'

# The only transaction status that authorizes a rating, and one that
# does not, so the negative case is named rather than spelled inline.
COMPLETED_STATUS = 'completed'
PENDING_STATUS = 'pending'

ASCENDING = firestore.Query.ASCENDING
DESCENDING = firestore.Query.DESCENDING

# Firestore's name for a document's own ID inside a query. Ordering by
# it needs no composite index, which is why app/services/rating.py uses
# it as the cursor ordering for its sweeps.
DOCUMENT_ID_FIELD = '__name__'

# Every setting that app/core/config.py declares WITHOUT a default, and
# nothing else. The six defaulted fields - SENTRY_DSN, ALGORITHM, the
# four RATING_* tunables - are left alone on purpose, and ALLOWED_ORIGINS
# especially so: it is typed ``List[str]``, and pydantic v1 BaseSettings
# parses a complex-typed field from the environment as JSON, so exporting
# a bare URL for it would raise at import.
#
# Values are obviously synthetic and carry none of the recognisable
# prefixes a real credential would - not a Stripe live or test key
# prefix, not a webhook-signing prefix, not an AWS access-key prefix - so
# a secret scanner has nothing to find and nobody can mistake one of
# these for something that needs rotating. The
# signing key is 45 characters because SECRET_KEY is declared as
# ``constr(strict=True, min_length=32)``, and every value is non-blank
# because a validator rejects whitespace-only settings.
REQUIRED_SETTINGS = {
    'PROJECT_NAME': 'Used Car Marketplace (test)',
    'API_V1_STR': '/api',
    'SECRET_KEY': 'blitzy-test-only-signing-key-0123456789abcdef',
    'ACCESS_TOKEN_EXPIRE_MINUTES': '30',
    'GOOGLE_CLOUD_PROJECT': 'used-car-marketplace-test',
    'GOOGLE_CLOUD_STORAGE_BUCKET': 'used-car-marketplace-test-media',
    'STRIPE_API_KEY': 'stripe-api-key-placeholder-not-a-real-key',
    'STRIPE_WEBHOOK_SECRET': 'stripe-webhook-placeholder-not-a-secret',
}

# Second line of defence only. With an emulator host set, the real
# firestore client constructs against anonymous credentials and performs
# no I/O at construction, so even a client built by some path that
# bypassed the patched symbol could not reach a real project. The host
# points at a port nothing serves, so such a client fails immediately
# rather than reading or writing anything. ``setdefault`` leaves a real
# emulator configuration in place if the environment already has one.
EMULATOR_HOST_GUARD = 'localhost:0'

# Settings that a test is expected to drive directly - the window in
# particular, which app/services/rating.py re-reads on every call - and
# which the autouse fixture therefore snapshots and restores.
MUTABLE_RATING_SETTINGS = (
    'RATING_MIN',
    'RATING_MAX',
    'RATING_REVIEW_MAX_LENGTH',
    'RATING_WINDOW_DAYS',
)

# Every query operator Firestore accepts, matching
# ``google.cloud.firestore_v1.base_query._COMPARISON_OPERATORS`` exactly
# so an operator string that would be rejected in production is rejected
# here too.
SUPPORTED_OPERATORS = (
    '==',
    '!=',
    '<',
    '<=',
    '>',
    '>=',
    'in',
    'not-in',
    'array_contains',
    'array_contains_any',
)

# Module that every Firestore field transform lives in. Used to tell a
# sentinel apart from ordinary data so an unsupported transform is
# refused instead of being stored as though it were a value.
_TRANSFORMS_MODULE = 'google.cloud.firestore_v1.transforms'

# Distinct from ``None``, which is a value a document may legitimately
# store. This marks a field that is genuinely absent.
_MISSING = object()

# Firestore's value type ordering, used both for sorting and for range
# filters across mixed types. Ranks at or above _RANK_UNORDERED have no
# useful natural order here.
_RANK_NONE = 0
_RANK_BOOL = 1
_RANK_NUMBER = 2
_RANK_TIMESTAMP = 3
_RANK_STRING = 4
_RANK_BYTES = 5
_RANK_UNORDERED = 6

_TIMESTAMP_LOCK = threading.Lock()
_LAST_TIMESTAMP = None

# Stand-in for ``google.cloud.firestore_v1.types.write.WriteResult``.
# Only ``update_time`` is ever meaningful to a caller, and
# ``app/db/firestore.py:create_document`` reads the second element of
# what ``add`` returns rather than this object at all.
FakeWriteResult = namedtuple('FakeWriteResult', ('update_time',))

# One pending mutation. ``kind`` is 'create', 'set', 'update' or
# 'delete'; ``merge`` applies to 'set' alone.
_StagedWrite = namedtuple(
    '_StagedWrite',
    ('kind', 'collection_id', 'document_id', 'data', 'merge'),
)


def utc_now():
    """Return the current instant as a timezone-aware UTC datetime.

    Every timestamp this module produces is aware, matching the real
    client, which returns ``DatetimeWithNanoseconds`` in UTC. Naive
    datetimes are the usual source of ``TypeError`` when a test compares
    a stored value against ``datetime.now(timezone.utc)``.

    Returns:
        The current UTC instant, with ``tzinfo`` set.
    """
    return datetime.now(timezone.utc)


def _server_timestamp():
    """Resolve ``SERVER_TIMESTAMP`` to a strictly increasing instant.

    Two properties matter and they pull in opposite directions.

    The value must be a REAL "now", not a fixed epoch, because
    ``app/services/rating.py`` decides whether an unreciprocated rating
    is due for publication by comparing ``created_at`` against
    ``RATING_WINDOW_DAYS`` before now. A timestamp anchored in the past
    would make every rating look overdue, publishing it immediately and
    quietly destroying the double-blind reveal the tests exist to prove.

    It must also increase strictly, because ``order_by('created_at',
    DESCENDING)`` is asserted on directly and the system clock's
    resolution is coarse enough for two writes to share a value. Ties
    would make "newest first" ambiguous, so a repeated reading is nudged
    forward by a microsecond.

    Returns:
        An aware UTC datetime strictly greater than every value this
        function has returned before.
    """
    global _LAST_TIMESTAMP
    with _TIMESTAMP_LOCK:
        moment = utc_now()
        if _LAST_TIMESTAMP is not None and moment <= _LAST_TIMESTAMP:
            moment = _LAST_TIMESTAMP + timedelta(microseconds=1)
        _LAST_TIMESTAMP = moment
        return moment


def _resolve_value(value, moment):
    """Return ``value`` with Firestore sentinels replaced, deep-copied.

    Only ``SERVER_TIMESTAMP`` is supported, and every other transform is
    refused. That refusal is the point: storing an ``Increment`` or an
    ``ArrayUnion`` object verbatim would leave a test asserting against
    a sentinel while believing it had asserted against a number, so the
    double says so instead of pretending.

    Nested dicts and lists are walked, because a sentinel is legal at
    any depth. The copy is deep so that a caller mutating the dict it
    passed in cannot reach into stored state afterwards.

    Args:
        value: Field value as supplied by the caller.
        moment: Instant to substitute for ``SERVER_TIMESTAMP``. One
            value is shared by every field in a commit, matching
            Firestore, which stamps a whole commit with one time.

    Returns:
        A sentinel-free deep copy of ``value``.

    Raises:
        TypeError: ``value`` is a field transform other than
            ``SERVER_TIMESTAMP``.
    """
    if value is firestore.SERVER_TIMESTAMP:
        return moment
    if type(value).__module__ == _TRANSFORMS_MODULE:
        raise TypeError(
            'The in-memory Firestore double supports the '
            'SERVER_TIMESTAMP sentinel only, and was handed {0!r}. '
            'Write a concrete value rather than relying on a '
            'server-side field transform.'.format(value)
        )
    if isinstance(value, dict):
        return dict(
            (key, _resolve_value(item, moment))
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return [_resolve_value(item, moment) for item in value]
    return copy.deepcopy(value)


def _type_rank(value):
    """Rank a value by Firestore's value type ordering.

    Firestore orders values across types before it orders them within a
    type, so a comparison never raises the way Python's would for, say,
    ``None < datetime``. ``bool`` is tested before the numeric types
    because it is a subclass of ``int`` and sorts before numbers.

    Args:
        value: Any stored or filter value.

    Returns:
        The integer rank of the value's type.
    """
    if value is None:
        return _RANK_NONE
    if isinstance(value, bool):
        return _RANK_BOOL
    if isinstance(value, (int, float)):
        return _RANK_NUMBER
    if isinstance(value, datetime):
        return _RANK_TIMESTAMP
    if isinstance(value, str):
        return _RANK_STRING
    if isinstance(value, bytes):
        return _RANK_BYTES
    return _RANK_UNORDERED


def _as_aware(value):
    """Normalise a naive datetime to UTC, leaving anything else alone.

    A seeded document may carry ``datetime.utcnow()``, which is naive.
    Comparing that against the aware timestamps the double writes would
    raise ``TypeError`` mid-sort, so a missing offset is read as UTC -
    the only interpretation consistent with everything else here.

    Args:
        value: Any stored or filter value.

    Returns:
        An aware datetime when given a naive one, else ``value``.
    """
    if isinstance(value, datetime) and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _compare(left, right):
    """Three-way compare two field values the way Firestore would.

    Values of different types are ordered by :func:`_type_rank`. Types
    with no useful natural order here - lists, maps and anything exotic -
    are compared by their ``repr``, which is arbitrary but stable, so a
    sort involving them terminates deterministically instead of raising.

    Args:
        left: First value.
        right: Second value.

    Returns:
        ``-1``, ``0`` or ``1`` as ``left`` sorts before, with, or after
        ``right``.
    """
    left_rank = _type_rank(left)
    right_rank = _type_rank(right)
    if left_rank != right_rank:
        return -1 if left_rank < right_rank else 1
    first = _as_aware(left)
    second = _as_aware(right)
    if left_rank == _RANK_UNORDERED:
        first = repr(first)
        second = repr(second)
    if first == second:
        return 0
    return -1 if first < second else 1


def _check_operator(op_string):
    """Reject a query operator Firestore would not accept.

    Args:
        op_string: Candidate operator.

    Raises:
        ValueError: The operator is not one Firestore supports.
    """
    if op_string not in SUPPORTED_OPERATORS:
        raise ValueError(
            'Unsupported Firestore query operator {0!r}. Supported '
            'operators are: {1}.'.format(
                op_string, ', '.join(SUPPORTED_OPERATORS)
            )
        )


def _matches(actual, op_string, expected):
    """Report whether one field value satisfies one filter.

    A document that does not carry the filtered field matches NO filter,
    including ``!=`` and ``not-in``. That is Firestore's rule rather than
    an approximation of it, and it is the behaviour the rating feature
    depends on: a user document written before ``is_verified`` existed
    must not be returned by a query for ``is_verified == False``, and a
    rating document written before ``is_published`` existed must not be
    revealed by a query for unpublished records.

    Args:
        actual: Stored value, or ``_MISSING`` when the field is absent.
        op_string: Already-validated Firestore operator.
        expected: Value the filter compares against.

    Returns:
        ``True`` when the document satisfies the filter.
    """
    if actual is _MISSING:
        return False
    if op_string == '==':
        return _compare(actual, expected) == 0
    if op_string == '!=':
        return _compare(actual, expected) != 0
    if op_string == '<':
        return _compare(actual, expected) < 0
    if op_string == '<=':
        return _compare(actual, expected) <= 0
    if op_string == '>':
        return _compare(actual, expected) > 0
    if op_string == '>=':
        return _compare(actual, expected) >= 0
    if op_string == 'in':
        return any(_compare(actual, item) == 0 for item in expected)
    if op_string == 'not-in':
        return all(_compare(actual, item) != 0 for item in expected)
    if not isinstance(actual, (list, tuple)):
        # Both array operators require an array-valued field; anything
        # else simply does not match, exactly as in Firestore.
        return False
    if op_string == 'array_contains':
        return any(_compare(item, expected) == 0 for item in actual)
    return any(
        _compare(item, candidate) == 0
        for item in actual
        for candidate in expected
    )


def _field_value(body, document_id, field_path):
    """Read one field from a document body by Firestore field path.

    ``__name__`` resolves to the document's own ID, which is what makes
    ``order_by('__name__')`` work. A dotted path walks nested maps.

    Args:
        body: Document body, or an empty dict for a missing document.
        document_id: The document's ID, for ``__name__``.
        field_path: Field name, dotted path, or ``__name__``.

    Returns:
        The value, or ``_MISSING`` when any segment is absent.
    """
    if field_path == DOCUMENT_ID_FIELD:
        return document_id
    current = body
    for segment in field_path.split('.'):
        if not isinstance(current, dict) or segment not in current:
            return _MISSING
        current = current[segment]
    return current


def _merge_field_paths(target, updates):
    """Apply ``update`` semantics: merge by field path, in place.

    ``update`` differs from ``set(merge=True)`` in exactly this respect -
    a dotted key addresses a nested field rather than naming a top-level
    one - so the distinction is implemented rather than flattened away.

    Args:
        target: Stored document body, mutated in place.
        updates: Sentinel-resolved field updates.
    """
    for field_path, value in updates.items():
        segments = field_path.split('.')
        cursor = target
        for segment in segments[:-1]:
            nested = cursor.get(segment)
            if not isinstance(nested, dict):
                nested = {}
                cursor[segment] = nested
            cursor = nested
        cursor[segments[-1]] = value


def generate_document_id():
    """Return a fresh random document ID shaped like a Firestore one.

    Twenty hexadecimal characters, matching the length of a real
    auto-allocated ID and satisfying the document-ID grammar
    ``app/schema/rating.py`` enforces: no slash, no control character,
    within the length bound, and outside the reserved ``__*__``
    namespace.

    Returns:
        A new document ID.
    """
    return uuid.uuid4().hex[:20]


class FakeDocumentSnapshot:
    """Stand-in for ``google.cloud.firestore.DocumentSnapshot``.

    Production code reads three things from a snapshot and all three
    matter here: ``exists``, ``to_dict()`` and ``id``.
    ``app/api/auth.py`` reads the ID off the snapshot rather than out of
    the body, because a user document is KEYED by the JWT subject and
    carries no ``id`` field of its own.

    A missing document is represented as a snapshot with ``exists``
    ``False`` whose ``to_dict()`` returns ``None`` - not as a raised
    error - because that is the contract every caller in this
    repository is written against.
    """

    def __init__(self, reference, body, read_time=None):
        """Build a snapshot over an already-copied document body.

        Args:
            reference: The :class:`FakeDocumentReference` read.
            body: Document body, or ``None`` for a missing document.
            read_time: Instant of the read. Defaults to now.
        """
        self.reference = reference
        self.id = reference.id
        self.exists = body is not None
        self.read_time = utc_now() if read_time is None else read_time
        self.create_time = self.read_time if self.exists else None
        self.update_time = self.create_time
        self._body = body

    def to_dict(self):
        """Return the document body, or ``None`` if it does not exist.

        The body is deep-copied on the way out, so a caller that mutates
        what it receives - and callers do, ``app/api/auth.py`` injects an
        ``id`` into it - cannot corrupt stored state.

        Returns:
            A fresh dict, or ``None``.
        """
        if self._body is None:
            return None
        return copy.deepcopy(self._body)

    def get(self, field_path):
        """Read one field by field path.

        Args:
            field_path: Field name, dotted path, or ``__name__``.

        Returns:
            A deep copy of the value.

        Raises:
            KeyError: The field is absent, matching the real snapshot.
        """
        value = _field_value(self._body or {}, self.id, field_path)
        if value is _MISSING:
            raise KeyError(
                'Document {0} has no field {1!r}'.format(
                    self.reference.path, field_path
                )
            )
        return copy.deepcopy(value)

    def __repr__(self):
        """Return a debugging representation naming the document."""
        return '<FakeDocumentSnapshot {0} exists={1}>'.format(
            self.reference.path, self.exists
        )


class FakeDocumentReference:
    """Stand-in for ``google.cloud.firestore.DocumentReference``.

    The write surface is deliberately narrow - ``set``, ``create``,
    ``update``, ``delete`` - and every one of them routes through the
    client's single atomic apply step, so a standalone write and a
    transactional one cannot diverge in their precondition handling.
    """

    def __init__(self, client, collection_id, document_id):
        """Bind a reference to one document.

        Args:
            client: The owning :class:`FakeFirestoreClient`.
            collection_id: Collection the document lives in.
            document_id: The document's ID.

        Raises:
            ValueError: The ID is empty, not a string, or contains a
                forward slash - which Firestore reads as a path
                separator rather than as part of an ID.
        """
        if not isinstance(document_id, str) or not document_id:
            raise ValueError(
                'A document ID must be a non-empty string, got '
                '{0!r}.'.format(document_id)
            )
        if '/' in document_id:
            raise ValueError(
                'A document ID may not contain "/", which Firestore '
                'reads as a path separator, got {0!r}.'.format(
                    document_id
                )
            )
        self._client = client
        self._collection_id = collection_id
        self.id = document_id
        self.path = '{0}/{1}'.format(collection_id, document_id)

    @property
    def collection_id(self):
        """Return the ID of the collection holding this document."""
        return self._collection_id

    @property
    def parent(self):
        """Return a reference to the collection holding this document."""
        return FakeCollectionReference(self._client, self._collection_id)

    def get(self, transaction=None):
        """Read the document, optionally through a transaction.

        A transactional read sees COMMITTED state, not the transaction's
        own staged writes, because Firestore transactions have no
        read-your-own-writes behaviour. Passing ``transaction`` also
        enforces Firestore's ordering rule that every read precede every
        write in the same transaction.

        Args:
            transaction: Open :class:`FakeTransaction`, or ``None``.

        Returns:
            A :class:`FakeDocumentSnapshot`, existing or not.
        """
        if transaction is not None:
            transaction.note_read(self.path)
        body = self._client.peek(self._collection_id, self.id)
        return FakeDocumentSnapshot(self, body)

    def set(self, document_data, merge=False):
        """Create or overwrite the document.

        Args:
            document_data: Body to write.
            merge: Merge into an existing body instead of replacing it.

        Returns:
            A :class:`FakeWriteResult` carrying the commit time.
        """
        return self._write('set', document_data, merge=merge)

    def create(self, document_data):
        """Create the document, refusing to overwrite an existing one.

        This is the primitive the whole one-vote-per-transaction rule
        rests on. Firestore offers no unique constraints, so a natural
        key encoded as a document ID plus create-only semantics IS the
        uniqueness guarantee, and a collision is the duplicate signal.

        Args:
            document_data: Body to write.

        Returns:
            A :class:`FakeWriteResult` carrying the commit time.

        Raises:
            google.api_core.exceptions.AlreadyExists: A document already
                exists at this ID in this collection.
        """
        return self._write('create', document_data)

    def update(self, field_updates):
        """Merge field updates into an existing document.

        Args:
            field_updates: Fields to write. A dotted key addresses a
                nested field.

        Returns:
            A :class:`FakeWriteResult` carrying the commit time.

        Raises:
            google.api_core.exceptions.NotFound: No document exists at
                this ID, matching Firestore, which will not create one
                through ``update``.
        """
        return self._write('update', field_updates)

    def delete(self):
        """Delete the document, succeeding whether or not it exists.

        Returns:
            A :class:`FakeWriteResult` carrying the commit time.
        """
        return self._write('delete', None)

    def _write(self, kind, data, merge=False):
        """Apply one standalone write through the client.

        Args:
            kind: 'create', 'set', 'update' or 'delete'.
            data: Body or field updates; ``None`` for a delete.
            merge: Merge flag, meaningful for 'set' alone.

        Returns:
            A :class:`FakeWriteResult` carrying the commit time.
        """
        staged = _StagedWrite(
            kind, self._collection_id, self.id, data, merge
        )
        return FakeWriteResult(self._client.apply([staged]))

    def __repr__(self):
        """Return a debugging representation naming the document."""
        return '<FakeDocumentReference {0}>'.format(self.path)


class FakeQuery:
    """A lazy, immutable Firestore query over one collection.

    Every chaining call returns a NEW query rather than mutating this
    one, which is what lets ``where``, ``order_by``, ``limit`` and the
    cursor methods compose in any order and be reused - exactly the
    pattern ``app/services/rating.py`` relies on when it builds one
    ordered query and then derives a limited page from it per round trip.

    Nothing is read until :meth:`get` or :meth:`stream` is called.
    Evaluation applies filters, then ordering, then the cursor, then the
    limit, in that order, because Firestore's ``limit`` is applied by the
    datastore after everything else.
    """

    def __init__(
        self,
        client,
        collection_id,
        filters=(),
        orders=(),
        limit=None,
        cursor=None,
    ):
        """Build a query. Prefer the chaining methods over this.

        Args:
            client: The owning :class:`FakeFirestoreClient`.
            collection_id: Collection to read.
            filters: Tuples of (field path, operator, value).
            orders: Tuples of (field path, direction).
            limit: Maximum documents to return, or ``None``.
            cursor: Cursor spec from :meth:`_cursor_spec`, or ``None``.
        """
        self._client = client
        self._collection_id = collection_id
        self._filters = tuple(filters)
        self._orders = tuple(orders)
        self._limit = limit
        self._cursor = cursor

    @property
    def collection_id(self):
        """Return the ID of the collection this query reads."""
        return self._collection_id

    def _derive(self, **changes):
        """Return a copy of this query with some of its spec replaced.

        Always builds a plain :class:`FakeQuery`, never
        ``type(self)`` - a derived query is no longer a collection
        reference, so it must not keep a collection's ``document`` and
        ``add`` methods.

        Args:
            **changes: Any of filters, orders, limit, cursor.

        Returns:
            A new :class:`FakeQuery`.
        """
        spec = {
            'filters': self._filters,
            'orders': self._orders,
            'limit': self._limit,
            'cursor': self._cursor,
        }
        spec.update(changes)
        return FakeQuery(self._client, self._collection_id, **spec)

    def where(self, field_path=None, op_string=None, value=None,
              filter=None):
        """Add an equality or range filter.

        Both call styles this repository uses are accepted: the legacy
        positional form ``where('status', '==', 'active')`` that
        ``app/api/listings.py`` and ``app/db/firestore.py`` use, and the
        keyword form ``where(filter=FieldFilter(...))`` that
        ``app/services/rating.py`` uses.

        Args:
            field_path: Field to filter on, for the positional form.
            op_string: Firestore operator, for the positional form.
            value: Value to compare against, for the positional form.
            filter: A ``google.cloud.firestore.FieldFilter``, for the
                keyword form. Named ``filter`` because that is the
                keyword the production call sites pass.

        Returns:
            A new :class:`FakeQuery` carrying the extra filter.

        Raises:
            TypeError: ``filter`` is not a ``FieldFilter``. Composite
                ``And``/``Or`` filters are not implemented, and are
                refused rather than silently ignored.
            ValueError: Both call styles were mixed, the field path is
                unusable, or the operator is not one Firestore accepts.
        """
        if filter is not None:
            if field_path is not None or op_string is not None:
                raise ValueError(
                    'Pass either filter=FieldFilter(...) or the '
                    'positional (field_path, op_string, value) form, '
                    'not both.'
                )
            if not isinstance(filter, firestore.FieldFilter):
                raise TypeError(
                    'The in-memory Firestore double supports '
                    'FieldFilter only, and was handed {0!r}. Composite '
                    'And/Or filters are not implemented.'.format(filter)
                )
            field_path = filter.field_path
            op_string = filter.op_string
            value = filter.value
        if not isinstance(field_path, str) or not field_path:
            raise ValueError(
                'A filter needs a non-empty field path, got '
                '{0!r}.'.format(field_path)
            )
        _check_operator(op_string)
        return self._derive(
            filters=self._filters + ((field_path, op_string, value),)
        )

    def order_by(self, field_path, direction=ASCENDING):
        """Add a sort order.

        Args:
            field_path: Field to sort on, or ``__name__`` for the
                document ID.
            direction: ``ASCENDING`` or ``DESCENDING``.

        Returns:
            A new :class:`FakeQuery` carrying the extra ordering.

        Raises:
            ValueError: The field path or direction is unusable.
        """
        if not isinstance(field_path, str) or not field_path:
            raise ValueError(
                'An ordering needs a non-empty field path, got '
                '{0!r}.'.format(field_path)
            )
        if direction not in (ASCENDING, DESCENDING):
            raise ValueError(
                'Sort direction must be {0!r} or {1!r}, got '
                '{2!r}.'.format(ASCENDING, DESCENDING, direction)
            )
        return self._derive(
            orders=self._orders + ((field_path, direction),)
        )

    def limit(self, count):
        """Cap the number of documents returned, replacing any cap.

        Args:
            count: Maximum documents to return.

        Returns:
            A new :class:`FakeQuery` carrying the limit.

        Raises:
            ValueError: ``count`` is not a non-negative integer.
        """
        if isinstance(count, bool) or not isinstance(count, int):
            raise ValueError(
                'A query limit must be an integer, got {0!r}.'.format(
                    count
                )
            )
        if count < 0:
            raise ValueError(
                'A query limit must not be negative, got {0}.'.format(
                    count
                )
            )
        return self._derive(limit=count)

    def start_after(self, document_fields_or_snapshot):
        """Begin strictly after a cursor position.

        Args:
            document_fields_or_snapshot: A
                :class:`FakeDocumentSnapshot`, or the ordering field
                values as a dict, list or tuple.

        Returns:
            A new :class:`FakeQuery` carrying the cursor.
        """
        return self._derive(
            cursor=self._cursor_spec(
                document_fields_or_snapshot, False
            )
        )

    def start_at(self, document_fields_or_snapshot):
        """Begin at a cursor position, including it.

        Args:
            document_fields_or_snapshot: A
                :class:`FakeDocumentSnapshot`, or the ordering field
                values as a dict, list or tuple.

        Returns:
            A new :class:`FakeQuery` carrying the cursor.
        """
        return self._derive(
            cursor=self._cursor_spec(document_fields_or_snapshot, True)
        )

    def get(self, transaction=None):
        """Evaluate the query and return an INDEXABLE list.

        The list shape is load-bearing:
        ``app/api/auth.py:authenticate_user`` does
        ``.where(...).limit(1).get()`` and then subscripts the result as
        ``user_doc[0]``. A double that returned a generator here would
        break authentication while looking correct.

        Args:
            transaction: Open :class:`FakeTransaction`, or ``None``.

        Returns:
            A list of :class:`FakeDocumentSnapshot`.
        """
        if transaction is not None:
            transaction.note_read(self._describe())
        return self._evaluate()

    def stream(self, transaction=None):
        """Evaluate the query and return an iterator of snapshots.

        Args:
            transaction: Open :class:`FakeTransaction`, or ``None``.

        Returns:
            An iterator of :class:`FakeDocumentSnapshot`, each exposing
            ``id`` and ``to_dict()``.
        """
        if transaction is not None:
            transaction.note_read(self._describe())
        return iter(self._evaluate())

    def _describe(self):
        """Return a short description of this query, for error text."""
        return 'query({0}, filters={1})'.format(
            self._collection_id, len(self._filters)
        )

    def _passes(self, body, document_id):
        """Report whether one document satisfies every filter.

        Args:
            body: Document body.
            document_id: Document ID, for ``__name__`` filters.

        Returns:
            ``True`` when all filters match.
        """
        for field_path, op_string, value in self._filters:
            actual = _field_value(body, document_id, field_path)
            if not _matches(actual, op_string, value):
                return False
        return True

    def _order_key(self, body, document_id):
        """Build the sort key for one document.

        Firestore excludes a document that lacks an ``order_by`` field
        from the result set entirely, and that is reproduced here rather
        than smoothed over: it is precisely why
        ``app/services/rating.py`` orders its sweeps by ``__name__``
        instead of by a data field.

        The document ID is appended as a final component because
        Firestore always breaks ties on ``__name__``, which is what makes
        an ordering total and a cursor unambiguous.

        Args:
            body: Document body.
            document_id: Document ID.

        Returns:
            A tuple of order values ending in the document ID, or
            ``None`` when the document lacks an ordered field.
        """
        values = []
        for field_path, _ in self._orders:
            value = _field_value(body, document_id, field_path)
            if value is _MISSING:
                return None
            values.append(value)
        values.append(document_id)
        return tuple(values)

    def _directions(self):
        """Return the sort direction of every key component.

        The implicit trailing ``__name__`` component follows the
        direction of the last explicit ordering, as it does in Firestore.

        Returns:
            A list of directions, one per order key component.
        """
        directions = [direction for _, direction in self._orders]
        directions.append(directions[-1])
        return directions

    def _cursor_spec(self, source, inclusive):
        """Turn a snapshot or field values into a cursor position.

        Args:
            source: A :class:`FakeDocumentSnapshot`, or the ordering
                field values as a dict, list or tuple.
            inclusive: ``True`` for ``start_at``, ``False`` for
                ``start_after``.

        Returns:
            A ``(values, document_id, inclusive)`` triple, where
            ``document_id`` may be ``_MISSING`` when the caller supplied
            bare field values.

        Raises:
            ValueError: The query carries no ordering, so no cursor
                position is defined.
            TypeError: ``source`` is not a supported cursor shape.
        """
        if not self._orders:
            raise ValueError(
                'A cursor is defined relative to an ordering: call '
                'order_by(...) before start_at/start_after.'
            )
        if isinstance(source, FakeDocumentSnapshot):
            body = source.to_dict() or {}
            return (
                tuple(
                    _field_value(body, source.id, field_path)
                    for field_path, _ in self._orders
                ),
                source.id,
                inclusive,
            )
        if isinstance(source, dict):
            return (
                tuple(
                    source.get(field_path, _MISSING)
                    for field_path, _ in self._orders
                ),
                source.get(DOCUMENT_ID_FIELD, _MISSING),
                inclusive,
            )
        if isinstance(source, (list, tuple)):
            return (tuple(source), _MISSING, inclusive)
        raise TypeError(
            'A cursor must be a document snapshot or the ordering '
            'field values as a dict, list or tuple, got '
            '{0!r}.'.format(source)
        )

    def _before_cursor(self, key):
        """Report whether a sort key falls short of the cursor.

        Args:
            key: The document's order key.

        Returns:
            ``True`` when the document sorts before the cursor position
            and must therefore be skipped.
        """
        values, document_id, inclusive = self._cursor
        directions = self._directions()
        outcome = 0
        for index, value in enumerate(values):
            outcome = _compare(key[index], value)
            if directions[index] == DESCENDING:
                outcome = -outcome
            if outcome:
                break
        if outcome == 0 and document_id is not _MISSING:
            outcome = _compare(key[-1], document_id)
            if directions[-1] == DESCENDING:
                outcome = -outcome
        if inclusive:
            return outcome < 0
        return outcome <= 0

    def _evaluate(self):
        """Run the query against current state.

        Returns:
            A list of :class:`FakeDocumentSnapshot` in query order.
        """
        rows = []
        for document_id, body in self._client.items(self._collection_id):
            if not self._passes(body, document_id):
                continue
            if self._orders:
                key = self._order_key(body, document_id)
                if key is None:
                    continue
            else:
                key = (document_id,)
            rows.append((key, document_id, body))
        if self._orders:
            directions = self._directions()

            def compare(left, right):
                """Order two rows by every key component in turn."""
                for index, direction in enumerate(directions):
                    outcome = _compare(left[0][index], right[0][index])
                    if direction == DESCENDING:
                        outcome = -outcome
                    if outcome:
                        return outcome
                return 0

            rows.sort(key=functools.cmp_to_key(compare))
        if self._cursor is not None:
            rows = [
                row for row in rows if not self._before_cursor(row[0])
            ]
        if self._limit is not None:
            rows = rows[:self._limit]
        return [
            FakeDocumentSnapshot(
                FakeDocumentReference(
                    self._client, self._collection_id, document_id
                ),
                body,
            )
            for _, document_id, body in rows
        ]


class FakeCollectionReference(FakeQuery):
    """Stand-in for ``google.cloud.firestore.CollectionReference``.

    A collection reference IS an unfiltered query over that collection,
    which is why it subclasses :class:`FakeQuery` - ``where``,
    ``order_by``, ``limit``, ``get`` and ``stream`` all work directly on
    it, as they do on the real client.
    """

    def __init__(self, client, collection_id):
        """Bind a reference to one collection.

        Args:
            client: The owning :class:`FakeFirestoreClient`.
            collection_id: Collection name.

        Raises:
            ValueError: The name is empty, not a string, or contains a
                forward slash. Subcollections are not implemented, and a
                path is refused rather than flattened into a name.
        """
        if not isinstance(collection_id, str) or not collection_id:
            raise ValueError(
                'A collection name must be a non-empty string, got '
                '{0!r}.'.format(collection_id)
            )
        if '/' in collection_id:
            raise ValueError(
                'Subcollections are not implemented by the in-memory '
                'Firestore double; pass a single collection name, not '
                'the path {0!r}.'.format(collection_id)
            )
        FakeQuery.__init__(self, client, collection_id)
        self.id = collection_id

    def document(self, document_id=None):
        """Return a reference to a document in this collection.

        Args:
            document_id: The ID to address. When omitted a fresh random
                ID is allocated and exposed as ``.id``, which is how
                ``app/api/transactions.py`` and ``app/api/listings.py``
                learn the ID they then store in the document body.

        Returns:
            A :class:`FakeDocumentReference`.
        """
        if document_id is None:
            document_id = generate_document_id()
        return FakeDocumentReference(
            self._client, self._collection_id, document_id
        )

    def add(self, document_data, document_id=None):
        """Create a document, allocating an ID when none is given.

        Args:
            document_data: Body to write.
            document_id: Optional explicit ID.

        Returns:
            A ``(write_result, document_reference)`` tuple.
            ``app/db/firestore.py:create_document`` reads the second
            element and takes ``.id`` off it.

        Raises:
            google.api_core.exceptions.AlreadyExists: An explicit ID was
                given and is already taken, matching the real ``add``,
                which is create-only.
        """
        reference = self.document(document_id)
        return (reference.create(document_data), reference)

    def __repr__(self):
        """Return a debugging representation naming the collection."""
        return '<FakeCollectionReference {0}>'.format(
            self._collection_id
        )


class FakeTransaction:
    """Stand-in for ``google.cloud.firestore.Transaction``.

    ``app/db/firestore.py:run_in_transaction`` drives this object with
    the REAL ``google.cloud.firestore.transactional`` decorator - only
    the client is replaced, not the decorator - so the production
    retry-and-rollback wrapper runs unchanged over this transaction. That
    is why the private members below exist and why their names are not
    negotiable: ``_Transactional`` calls ``_clean_up()``, ``_begin()``,
    ``_commit()`` and ``_rollback()``, and reads ``_id``, ``_read_only``
    and ``_max_attempts``.

    ATOMICITY
    -------------------------------------------------------------------
    Writes are STAGED, never applied as they are made. On a normal return
    the decorator calls :meth:`_commit`, which hands the whole batch to
    the client to apply in one indivisible step. If the callback raises,
    the decorator calls :meth:`_rollback` and every staged write is
    discarded, so a test can prove that a failed multi-document write
    left nothing behind.

    WHY :meth:`_rollback` MUST NOT RAISE
    -------------------------------------------------------------------
    ``_Transactional.__call__`` rolls back inside ``except
    BaseException:`` and then re-raises. An exception escaping the
    rollback would REPLACE the original one, and the original is often
    load-bearing: ``app/services/rating.py:submit_rating`` catches
    ``AlreadyExists`` to report a duplicate vote, and would never see it.
    So the rollback is tolerant and idempotent by design.
    """

    def __init__(self, client, max_attempts=MAX_ATTEMPTS, read_only=False):
        """Build a transaction. Obtain one from ``client.transaction()``.

        Args:
            client: The owning :class:`FakeFirestoreClient`.
            max_attempts: Attempts the decorator may make.
            read_only: Refuse writes when ``True``.
        """
        self._client = client
        self._max_attempts = max_attempts
        self._read_only = read_only
        self._id = None
        self._writes = []
        self._reads = []
        self._staged_creates = set()

    @property
    def in_progress(self):
        """Report whether this transaction has begun and not finished."""
        return self._id is not None

    def _clean_up(self):
        """Discard all state, ending any transaction in progress.

        Called by the decorator before every attempt, which is what makes
        a retry start from an empty staging area rather than replaying
        the previous attempt's writes.
        """
        self._id = None
        self._writes = []
        self._reads = []
        self._staged_creates = set()

    def _begin(self, retry_id=None):
        """Open the transaction.

        Args:
            retry_id: ID of the attempt being retried, carried by the
                decorator so a rerun keeps its place in line. Recorded
                in the new ID so a test can tell a retry apart from a
                first attempt.

        Raises:
            ValueError: The transaction has already begun, matching the
                real client.
        """
        if self.in_progress:
            raise ValueError(
                'The transaction has already begun. Current '
                'transaction ID: {0!r}.'.format(self._id)
            )
        suffix = 'retry' if retry_id is not None else 'first'
        self._id = 'fake-txn-{0}-{1}'.format(
            uuid.uuid4().hex[:12], suffix
        ).encode('ascii')

    def _commit(self):
        """Apply every staged write atomically, then close.

        Returns:
            A list of :class:`FakeWriteResult`, one per staged write,
            mirroring the real client's list of ``WriteResult``.

        Raises:
            ValueError: The transaction is not in progress.
            google.api_core.exceptions.AlreadyExists: A staged create
                collided. Nothing is applied - not the create and not
                any write staged beside it.
            google.api_core.exceptions.NotFound: A staged update
                addressed a document that does not exist. Nothing is
                applied.
        """
        if not self.in_progress:
            raise ValueError('Cannot commit an unstarted transaction.')
        staged = list(self._writes)
        moment = self._client.apply(staged)
        self._clean_up()
        return [FakeWriteResult(moment) for _ in staged]

    def _rollback(self):
        """Discard every staged write and close, without ever raising.

        Deliberately tolerant of being called on a transaction that is
        no longer in progress, where the real client would raise: see the
        class docstring for why an exception escaping here would destroy
        the original error.
        """
        self._clean_up()

    def note_read(self, subject):
        """Record a read and enforce Firestore's read/write ordering.

        Firestore requires every read in a transaction to precede every
        write in it. Enforcing that here turns a rule that would
        otherwise be violated silently - and only discovered against a
        real datastore - into an immediate, named failure.

        Args:
            subject: What is being read, for the error message.

        Raises:
            ValueError: The transaction has not begun, or has already
                staged a write.
        """
        if not self.in_progress:
            raise ValueError(
                'Cannot read through a transaction that has not begun.'
            )
        if self._writes:
            raise ValueError(
                'Firestore requires every read in a transaction to '
                'precede every write, and this transaction has already '
                'staged {0} write(s) before reading {1}. Move the read '
                'above the first write.'.format(
                    len(self._writes), subject
                )
            )
        self._reads.append(subject)

    def get(self, ref_or_query):
        """Read a document or a query through this transaction.

        Returns an ITERABLE in both cases, which is what the real
        ``Transaction.get`` does - it delegates to ``get_all`` for a
        document reference and to ``Query.stream`` for a query. No
        production code in this repository calls it; it is implemented
        because ``app/db/firestore.py`` documents it as available and a
        test may reasonably use it.

        Args:
            ref_or_query: A :class:`FakeDocumentReference` or
                :class:`FakeQuery`.

        Returns:
            An iterator of :class:`FakeDocumentSnapshot`.

        Raises:
            TypeError: ``ref_or_query`` is neither shape.
        """
        if isinstance(ref_or_query, FakeDocumentReference):
            return iter([ref_or_query.get(transaction=self)])
        if isinstance(ref_or_query, FakeQuery):
            return ref_or_query.stream(transaction=self)
        raise TypeError(
            'Transaction.get takes a document reference or a query, '
            'got {0!r}.'.format(ref_or_query)
        )

    def create(self, reference, document_data):
        """Stage a create-only write.

        The collision is detected here, when the write is staged, rather
        than at commit time as it would be on the wire. The outcome a
        caller sees is identical - the decorator rolls the transaction
        back and re-raises, so nothing is written either way - and
        detecting it early keeps the failure attributable to the create
        that caused it.

        Args:
            reference: Document to create.
            document_data: Body to write.

        Raises:
            google.api_core.exceptions.AlreadyExists: The document
                already exists, or this transaction already staged a
                create for it.
        """
        self._require_writable()
        if self._client.exists(reference.collection_id, reference.id):
            raise AlreadyExists(
                'Document already exists: {0}'.format(reference.path)
            )
        if reference.path in self._staged_creates:
            raise AlreadyExists(
                'Document already staged for creation in this '
                'transaction: {0}'.format(reference.path)
            )
        self._staged_creates.add(reference.path)
        self._stage('create', reference, document_data)

    def set(self, reference, document_data, merge=False):
        """Stage a create-or-overwrite write.

        Args:
            reference: Document to write.
            document_data: Body to write.
            merge: Merge into an existing body instead of replacing it.
        """
        self._require_writable()
        self._stage('set', reference, document_data, merge=merge)

    def update(self, reference, field_updates):
        """Stage a merge into an existing document.

        Args:
            reference: Document to update.
            field_updates: Fields to write; a dotted key addresses a
                nested field.
        """
        self._require_writable()
        self._stage('update', reference, field_updates)

    def delete(self, reference):
        """Stage a delete.

        Args:
            reference: Document to remove.
        """
        self._require_writable()
        self._stage('delete', reference, None)

    def _require_writable(self):
        """Refuse a write this transaction may not make.

        Raises:
            ValueError: The transaction has not begun, or is read-only.
        """
        if not self.in_progress:
            raise ValueError(
                'Cannot write through a transaction that has not begun.'
            )
        if self._read_only:
            raise ValueError('Cannot write in a read-only transaction.')

    def _stage(self, kind, reference, data, merge=False):
        """Add one pending mutation to the staging area.

        Args:
            kind: 'create', 'set', 'update' or 'delete'.
            reference: Document being written.
            data: Body or field updates; ``None`` for a delete.
            merge: Merge flag, meaningful for 'set' alone.
        """
        self._writes.append(
            _StagedWrite(
                kind,
                reference.collection_id,
                reference.id,
                data,
                merge,
            )
        )

    def __repr__(self):
        """Return a debugging representation of the staging area."""
        return (
            '<FakeTransaction in_progress={0} reads={1} '
            'staged_writes={2}>'.format(
                self.in_progress, len(self._reads), len(self._writes)
            )
        )


class FakeFirestoreClient:
    """In-memory stand-in for ``google.cloud.firestore.Client``.

    State is a nested dict, ``{collection: {document_id: body}}``, and
    Python's insertion-ordered dicts give an unordered query a stable,
    reproducible result order.

    Every read hands out deep copies and every write stores deep copies,
    so no test can reach into stored state by holding on to a dict it
    was given or one it passed in.

    Beyond the client surface the application uses, this class exposes
    the raw-state inspection the acceptance gates need. Assertions of
    the form "the request was refused AND no rating document was
    written" cannot be expressed through the query API alone, because a
    query cannot distinguish a document that is absent from one that was
    written and then filtered out of the result. :meth:`documents`,
    :meth:`document_body`, :meth:`exists` and :meth:`count` answer the
    question directly.
    """

    def __init__(self, project=None):
        """Build an empty client.

        Args:
            project: Project ID to report. Defaults to the seeded test
                project.
        """
        self.project = (
            REQUIRED_SETTINGS['GOOGLE_CLOUD_PROJECT']
            if project is None
            else project
        )
        self._data = {}
        self._lock = threading.RLock()

    def collection(self, collection_id):
        """Return a reference to one collection.

        An unknown collection is not an error - it reads as empty, which
        is how Firestore behaves, collections having no existence
        independent of their documents.

        Args:
            collection_id: Collection name.

        Returns:
            A :class:`FakeCollectionReference`.
        """
        return FakeCollectionReference(self, collection_id)

    def transaction(self, max_attempts=MAX_ATTEMPTS, read_only=False):
        """Return a new transaction.

        Args:
            max_attempts: Attempts the ``transactional`` decorator may
                make before giving up.
            read_only: Build a read-only transaction.

        Returns:
            A :class:`FakeTransaction`.
        """
        return FakeTransaction(
            self, max_attempts=max_attempts, read_only=read_only
        )

    def peek(self, collection_id, document_id):
        """Return a deep copy of one document body, or ``None``.

        Args:
            collection_id: Collection to read.
            document_id: Document to read.

        Returns:
            A fresh dict, or ``None`` when the document is absent.
        """
        with self._lock:
            body = self._data.get(collection_id, {}).get(document_id)
            return None if body is None else copy.deepcopy(body)

    def items(self, collection_id):
        """Return every document in a collection, as copies.

        Args:
            collection_id: Collection to read.

        Returns:
            A list of ``(document_id, body)`` pairs in insertion order.
        """
        with self._lock:
            return [
                (document_id, copy.deepcopy(body))
                for document_id, body
                in self._data.get(collection_id, {}).items()
            ]

    def apply(self, writes):
        """Apply a batch of writes atomically.

        Atomicity is achieved by building the whole next state on a deep
        copy and swapping it in only once every write has succeeded. A
        precondition failure part-way through therefore leaves the
        visible store exactly as it was, which is what lets a test prove
        that a rolled-back transaction wrote nothing at all.

        Every ``SERVER_TIMESTAMP`` in the batch resolves to ONE instant,
        matching Firestore, which stamps a whole commit with a single
        commit time.

        Args:
            writes: An iterable of ``_StagedWrite``.

        Returns:
            The instant the batch committed.

        Raises:
            google.api_core.exceptions.AlreadyExists: A create collided.
            google.api_core.exceptions.NotFound: An update addressed a
                document that does not exist.
            TypeError: A field carried an unsupported transform.
            ValueError: A write kind is unrecognised.
        """
        with self._lock:
            moment = _server_timestamp()
            working = copy.deepcopy(self._data)
            for write in writes:
                self._apply_one(working, write, moment)
            self._data = working
            return moment

    def _apply_one(self, working, write, moment):
        """Apply one write to a working copy of the store.

        Args:
            working: Mutable copy of all state.
            write: The ``_StagedWrite`` to apply.
            moment: Instant to resolve ``SERVER_TIMESTAMP`` to.

        Raises:
            google.api_core.exceptions.AlreadyExists: A create collided.
            google.api_core.exceptions.NotFound: An update addressed a
                document that does not exist.
            ValueError: The write kind is unrecognised.
        """
        collection = working.setdefault(write.collection_id, {})
        path = '{0}/{1}'.format(write.collection_id, write.document_id)
        if write.kind == 'delete':
            collection.pop(write.document_id, None)
            return
        if write.kind == 'create':
            if write.document_id in collection:
                raise AlreadyExists(
                    'Document already exists: {0}'.format(path)
                )
            collection[write.document_id] = _resolve_value(
                write.data, moment
            )
            return
        if write.kind == 'set':
            body = _resolve_value(write.data, moment)
            existing = collection.get(write.document_id)
            if write.merge and isinstance(existing, dict):
                existing.update(body)
            else:
                collection[write.document_id] = body
            return
        if write.kind == 'update':
            if write.document_id not in collection:
                raise NotFound(
                    'No document to update: {0}'.format(path)
                )
            _merge_field_paths(
                collection[write.document_id],
                _resolve_value(write.data, moment),
            )
            return
        raise ValueError(
            'Unrecognised write kind {0!r}.'.format(write.kind)
        )

    def seed(self, collection_id, document_id, body):
        """Write a document directly, bypassing create preconditions.

        This is the fixtures' way in. It is deliberately separate from
        :meth:`FakeDocumentReference.create`, so that arranging a test's
        starting state can never be mistaken for exercising the
        create-only rule the feature's uniqueness guarantee rests on.

        The write is routed through a document reference so that the
        collection name and document ID are held to the same validity
        rules as any other write - a seed that quietly accepted an ID
        Firestore would reject would set up a test against a state the
        application could never reach.

        Args:
            collection_id: Collection to write into.
            document_id: Document ID to write at.
            body: Document body. Sentinels are resolved as on any write.

        Returns:
            The ``document_id`` written.
        """
        reference = self.collection(collection_id).document(document_id)
        self.apply([
            _StagedWrite(
                'set', collection_id, reference.id, body, False
            )
        ])
        return reference.id

    def reset(self):
        """Discard all state, giving the next test an empty datastore."""
        with self._lock:
            self._data = {}

    def raw(self):
        """Return a deep copy of the entire store.

        Returns:
            ``{collection: {document_id: body}}``, safe to inspect and
            to mutate.
        """
        with self._lock:
            return copy.deepcopy(self._data)

    def documents(self, collection_id):
        """Return a deep copy of one collection, keyed by document ID.

        Args:
            collection_id: Collection to inspect.

        Returns:
            ``{document_id: body}``, empty for an unknown collection.
        """
        with self._lock:
            return copy.deepcopy(self._data.get(collection_id, {}))

    def document_body(self, collection_id, document_id):
        """Return one document body, or ``None`` when it is absent.

        Args:
            collection_id: Collection to inspect.
            document_id: Document to inspect.

        Returns:
            A fresh dict, or ``None``.
        """
        return self.peek(collection_id, document_id)

    def document_ids(self, collection_id):
        """Return the IDs held in one collection, in insertion order.

        Args:
            collection_id: Collection to inspect.

        Returns:
            A list of document IDs.
        """
        with self._lock:
            return list(self._data.get(collection_id, {}).keys())

    def exists(self, collection_id, document_id):
        """Report whether one document is present.

        Args:
            collection_id: Collection to inspect.
            document_id: Document to inspect.

        Returns:
            ``True`` when the document exists.
        """
        with self._lock:
            return document_id in self._data.get(collection_id, {})

    def count(self, collection_id):
        """Return how many documents a collection holds.

        Args:
            collection_id: Collection to inspect.

        Returns:
            The document count, ``0`` for an unknown collection.
        """
        with self._lock:
            return len(self._data.get(collection_id, {}))

    def __repr__(self):
        """Return a debugging representation summarising the store."""
        with self._lock:
            summary = ', '.join(
                '{0}={1}'.format(name, len(documents))
                for name, documents in sorted(self._data.items())
            )
        return '<FakeFirestoreClient project={0} {1}>'.format(
            self.project, summary or 'empty'
        )


# The process-wide double. One instance is shared by every test rather
# than one per test, because five ``app.*`` modules bind ``db`` by value
# at import and can never be handed a replacement afterwards. Isolation
# comes from clearing its state between tests, not from rebuilding it -
# see :func:`reset_firestore_double`.
fake_db = FakeFirestoreClient()


def get_fake_db():
    """Return the process-wide in-memory Firestore double.

    Returns:
        The shared :class:`FakeFirestoreClient`.
    """
    return fake_db


def firestore_client_factory(project=None, **kwargs):
    """Stand in for the ``google.cloud.firestore.Client`` constructor.

    Installed over the ``Client`` name on its own source module, so that
    ``app/db/firestore.py``'s module-level ``db = Client(project=...)``
    yields the double. No credentials are resolved and no channel is
    opened, which is what makes the neutralisation independent of whether
    application default credentials happen to be available.

    Args:
        project: Project the caller asked for. Recorded on the double so
            a test can assert what the application requested.
        **kwargs: Any other constructor argument, accepted and ignored -
            ``credentials``, ``database`` and the rest have no meaning
            for an in-memory store.

    Returns:
        The shared :class:`FakeFirestoreClient`.
    """
    if project is not None:
        fake_db.project = project
    return fake_db


def rebind_firestore_holders():
    """Repoint any already-imported ``app.*`` module's ``db`` symbol.

    ``from app.db.firestore import db`` binds the OBJECT, so a module
    imported before the double was installed holds a stale client that
    rebinding ``app.db.firestore.db`` cannot reach. This walks the
    already-imported ``app.*`` modules and replaces that symbol.

    The match is deliberately narrow - only a value that IS a Firestore
    client, real or fake - because ``app.db`` is itself an attribute
    named ``db`` on the ``app`` package, and overwriting that would break
    every ``app.db.firestore`` lookup in the process.

    Returns:
        The names of the modules that were rebound, which is empty in the
        normal case where the double was installed first.
    """
    rebound = []
    for name, module in list(sys.modules.items()):
        if module is None or not name.startswith('app.'):
            continue
        current = getattr(module, 'db', None)
        if current is None or current is fake_db:
            continue
        if isinstance(
            current, (REAL_FIRESTORE_CLIENT, FakeFirestoreClient)
        ):
            setattr(module, 'db', fake_db)
            rebound.append(name)
    return rebound


def ensure_backend_on_sys_path():
    """Make ``import app.*`` resolve however pytest was invoked.

    There is no ``__init__.py`` anywhere in this repository, so ``app``
    resolves as an implicit namespace package and only its parent
    directory needs to be importable. pytest inserts the test file's own
    directory, not ``backend/``, so ``import app.main`` would otherwise
    depend on the current working directory - it happens to work from
    ``backend/`` under ``python -m pytest`` and to fail from the
    repository root, which is how CI runs it.

    Returns:
        The absolute path to the ``backend`` directory.
    """
    backend_dir = os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))
    )
    if backend_dir not in sys.path:
        sys.path.insert(0, backend_dir)
    return backend_dir


def seed_required_settings():
    """Put the required settings into ``os.environ``.

    Must run before the first ``app.*`` import: ``app/core/config.py``
    evaluates ``Settings()`` at module scope and eight of its fields have
    no default.

    ``setdefault`` rather than assignment, so a real environment - a
    developer who sourced ``backend/.env``, or CI - keeps its own values
    and these apply only where nothing is configured.

    Returns:
        The names of the variables this call actually set, which is empty
        when the environment already supplied all of them.
    """
    seeded = []
    for name, value in REQUIRED_SETTINGS.items():
        if name not in os.environ:
            os.environ[name] = value
            seeded.append(name)
    if 'FIRESTORE_EMULATOR_HOST' not in os.environ:
        os.environ['FIRESTORE_EMULATOR_HOST'] = EMULATOR_HOST_GUARD
        seeded.append('FIRESTORE_EMULATOR_HOST')
    return seeded


def install_firestore_double():
    """Neutralise the Firestore client and verify that it worked.

    Order is the whole point of this function:

    1. Replace ``Client`` on ``google.cloud.firestore`` FIRST, so that
       ``app/db/firestore.py``'s ``from google.cloud.firestore import
       Client`` binds the factory and its module-level construction
       yields the double instead of resolving credentials.
    2. Import the data layer, which now cannot build a real client.
    3. Set ``app.db.firestore.db`` explicitly, so the singleton is
       unambiguous however it came to exist.
    4. Rebind any module that imported ``db`` earlier.

    Step 2's result is then CHECKED rather than assumed. If the data
    layer had already been imported, its ``db`` is a real client bound to
    a real project and every test that followed would exercise that
    client while appearing to pass - the exact failure this whole
    arrangement exists to prevent - so it is reported as an error instead
    of being quietly repaired.

    Returns:
        The shared :class:`FakeFirestoreClient`.

    Raises:
        RuntimeError: ``app.db.firestore`` was imported before this ran.
    """
    firestore.Client = firestore_client_factory
    from app.db import firestore as data_layer
    constructed = data_layer.db
    data_layer.db = fake_db
    rebound = rebind_firestore_holders()
    if not isinstance(constructed, FakeFirestoreClient):
        raise RuntimeError(
            'app.db.firestore was imported before the in-memory '
            'Firestore double was installed, so it constructed a real '
            'client of type {0}. Every test after this point would run '
            'against a real project. Ensure nothing imports app.* '
            'before tests/conftest.py runs. Modules rebound: '
            '{1}.'.format(type(constructed).__name__, rebound or 'none')
        )
    return fake_db


# Identifiers the builders default to. Each satisfies the document-ID
# grammar app/schema/rating.py enforces - within 128 characters, no
# slash, no control character, outside the reserved ``__*__`` namespace -
# and each names its role, so a failure message points at the party it
# concerns instead of at an opaque hash.
DEFAULT_BUYER_ID = 'test-buyer-000000000001'
DEFAULT_SELLER_ID = 'test-seller-00000000001'
DEFAULT_UNVERIFIED_ID = 'test-unverified-0000001'
DEFAULT_ADMIN_ID = 'test-admin-000000000001'
DEFAULT_OUTSIDER_ID = 'test-outsider-000000001'
DEFAULT_TRANSACTION_ID = 'test-transaction-000001'
DEFAULT_LISTING_ID = 'test-listing-0000000001'
DEFAULT_PAYMENT_INTENT_ID = 'test-payment-intent-001'
DEFAULT_AMOUNT = 12500.0
DEFAULT_SCORE = 5


def _merged(defaults, overrides):
    """Overlay caller overrides on a named shape's defaults.

    Args:
        defaults: The shape's own defaults.
        overrides: Keyword overrides, named as :func:`build_user` or
            :func:`build_transaction` parameters.

    Returns:
        A new dict of keyword arguments.
    """
    merged = dict(defaults)
    merged.update(overrides)
    return merged


def build_user(
    user_id=DEFAULT_BUYER_ID,
    email=None,
    first_name='Test',
    last_name='User',
    role='buyer',
    is_verified=True,
    rating_average=None,
    rating_count=0,
    created_at=None,
    updated_at=None,
):
    """Build a ``User`` model instance.

    Returns the MODEL rather than a dict, because that is what a service
    call takes: ``submit_rating(payload, caller)`` is annotated for
    ``User``. Use :func:`seed_user` when a stored document is wanted too.

    ``is_verified`` is declared ``StrictBool`` on the model, so it must
    be a real ``bool`` - ``1`` and ``'true'`` are rejected, which is the
    point of the strict type and is left intact here.

    Args:
        user_id: Document ID, which is also the JWT subject.
        email: Address to store. Defaults to one derived from the ID.
        first_name: Given name.
        last_name: Family name.
        role: ``'buyer'``, ``'seller'`` or ``'admin'``.
        is_verified: The R1 authorization flag.
        rating_average: Denormalised aggregate, or ``None`` when the user
            has never been rated.
        rating_count: Number of published ratings received.
        created_at: Creation instant. Defaults to now.
        updated_at: Update instant. Defaults to ``created_at``.

    Returns:
        A ``app.schema.user.User``.
    """
    from app.schema.user import User
    moment = utc_now() if created_at is None else created_at
    return User(
        id=user_id,
        email=(
            '{0}@example.test'.format(user_id) if email is None else email
        ),
        first_name=first_name,
        last_name=last_name,
        role=role,
        created_at=moment,
        updated_at=moment if updated_at is None else updated_at,
        is_verified=is_verified,
        rating_average=rating_average,
        rating_count=rating_count,
    )


def verified_buyer(**overrides):
    """Build the verified buyer side of a transaction.

    Args:
        **overrides: Any :func:`build_user` keyword.

    Returns:
        A verified ``User`` with role ``'buyer'``.
    """
    return build_user(**_merged(
        {
            'user_id': DEFAULT_BUYER_ID,
            'role': 'buyer',
            'is_verified': True,
        },
        overrides,
    ))


def verified_seller(**overrides):
    """Build the verified seller side of a transaction.

    Args:
        **overrides: Any :func:`build_user` keyword.

    Returns:
        A verified ``User`` with role ``'seller'``.
    """
    return build_user(**_merged(
        {
            'user_id': DEFAULT_SELLER_ID,
            'role': 'seller',
            'is_verified': True,
        },
        overrides,
    ))


def unverified_user(**overrides):
    """Build an unverified user - the R1 negative case.

    A participant of the transaction in every other respect, so a test
    using this isolates the verification gate rather than tripping the
    participant gate by accident.

    Args:
        **overrides: Any :func:`build_user` keyword.

    Returns:
        A ``User`` whose ``is_verified`` is ``False``.
    """
    return build_user(**_merged(
        {
            'user_id': DEFAULT_UNVERIFIED_ID,
            'role': 'buyer',
            'is_verified': False,
        },
        overrides,
    ))


def admin_user(**overrides):
    """Build an administrator - the moderation gate's positive case.

    Args:
        **overrides: Any :func:`build_user` keyword.

    Returns:
        A verified ``User`` with role ``'admin'``.
    """
    return build_user(**_merged(
        {
            'user_id': DEFAULT_ADMIN_ID,
            'role': 'admin',
            'is_verified': True,
        },
        overrides,
    ))


def non_participant_user(**overrides):
    """Build a verified third party - the R2 negative case.

    Verified, so the verification gate passes and the shared-transaction
    gate is what a test using this actually exercises, but a party to no
    transaction the fixtures create.

    Args:
        **overrides: Any :func:`build_user` keyword.

    Returns:
        A verified ``User`` who is neither buyer nor seller.
    """
    return build_user(**_merged(
        {
            'user_id': DEFAULT_OUTSIDER_ID,
            'role': 'buyer',
            'is_verified': True,
        },
        overrides,
    ))


def user_document(user):
    """Return the document body for a user, as Firestore stores it.

    ``id`` is REMOVED. A user document is keyed by the JWT subject and
    carries no ``id`` field, which is why ``app/api/auth.py`` takes the
    ID off the snapshot reference rather than out of the body. Storing an
    ``id`` here would let a test pass while the production code path that
    depends on the reference went unexercised.

    Args:
        user: A ``User`` model.

    Returns:
        The document body as a dict.
    """
    body = user.dict()
    body.pop('id', None)
    return body


def seed_user(user=None, **overrides):
    """Write a user document into the double and return the model.

    Args:
        user: An already-built ``User``. When omitted one is built from
            ``overrides``.
        **overrides: Any :func:`build_user` keyword, when ``user`` is not
            given.

    Returns:
        The ``User`` that was stored.

    Raises:
        TypeError: Both a built user and overrides were supplied, which
            would silently ignore the overrides.
    """
    if user is None:
        user = build_user(**overrides)
    elif overrides:
        raise TypeError(
            'seed_user takes either a built User or keyword overrides, '
            'not both.'
        )
    fake_db.seed(USERS_COLLECTION, user.id, user_document(user))
    return user


def build_transaction(
    transaction_id=DEFAULT_TRANSACTION_ID,
    buyer_id=DEFAULT_BUYER_ID,
    seller_id=DEFAULT_SELLER_ID,
    vehicle_listing_id=DEFAULT_LISTING_ID,
    amount=DEFAULT_AMOUNT,
    status=COMPLETED_STATUS,
    stripe_payment_intent_id=DEFAULT_PAYMENT_INTENT_ID,
    created_at=None,
    updated_at=None,
):
    """Build a ``Transaction`` model instance.

    All nine fields of the model are required, including
    ``stripe_payment_intent_id``, so every one is defaulted here - a
    caller that only cares about the two participant IDs should not have
    to supply a payment intent to get a valid model.

    Args:
        transaction_id: Document ID.
        buyer_id: The buyer, one half of the shared-transaction gate.
        seller_id: The seller, the other half.
        vehicle_listing_id: Listing the transaction settled.
        amount: Settled amount.
        status: Transaction state; only ``'completed'`` authorizes a
            rating.
        stripe_payment_intent_id: Payment reference.
        created_at: Creation instant. Defaults to now.
        updated_at: Update instant. Defaults to ``created_at``.

    Returns:
        An ``app.schema.transaction.Transaction``.
    """
    from app.schema.transaction import Transaction
    moment = utc_now() if created_at is None else created_at
    return Transaction(
        id=transaction_id,
        buyer_id=buyer_id,
        seller_id=seller_id,
        vehicle_listing_id=vehicle_listing_id,
        amount=amount,
        status=status,
        stripe_payment_intent_id=stripe_payment_intent_id,
        created_at=moment,
        updated_at=moment if updated_at is None else updated_at,
    )


def completed_transaction(**overrides):
    """Build a completed transaction - the only ratable state.

    Args:
        **overrides: Any :func:`build_transaction` keyword.

    Returns:
        A ``Transaction`` whose status is ``'completed'``.
    """
    return build_transaction(**_merged(
        {'status': COMPLETED_STATUS}, overrides
    ))


def pending_transaction(**overrides):
    """Build a transaction that has not completed.

    Args:
        **overrides: Any :func:`build_transaction` keyword.

    Returns:
        A ``Transaction`` whose status is ``'pending'``.
    """
    return build_transaction(**_merged(
        {'status': PENDING_STATUS}, overrides
    ))


def degenerate_transaction(**overrides):
    """Build a transaction whose buyer and seller are the same party.

    The only way a derived counterparty can equal the rater, since
    ``ratee_id`` is computed from the transaction rather than accepted
    from a client. This is what makes the self-rating branch reachable at
    all, and therefore testable.

    Args:
        **overrides: Any :func:`build_transaction` keyword.

    Returns:
        A completed ``Transaction`` with ``buyer_id == seller_id``.
    """
    return build_transaction(**_merged(
        {
            'status': COMPLETED_STATUS,
            'buyer_id': DEFAULT_BUYER_ID,
            'seller_id': DEFAULT_BUYER_ID,
        },
        overrides,
    ))


def transaction_document(transaction):
    """Return the document body for a transaction, as stored.

    ``id`` is KEPT, unlike a user document: ``app/api/transactions.py``
    writes the allocated ID into the body before storing it, so a
    faithful transaction document carries one.

    Args:
        transaction: A ``Transaction`` model.

    Returns:
        The document body as a dict.
    """
    return transaction.dict()


def seed_transaction(transaction=None, **overrides):
    """Write a transaction document into the double and return the model.

    Args:
        transaction: An already-built ``Transaction``. When omitted one
            is built from ``overrides``.
        **overrides: Any :func:`build_transaction` keyword, when
            ``transaction`` is not given.

    Returns:
        The ``Transaction`` that was stored.

    Raises:
        TypeError: Both a built transaction and overrides were supplied.
    """
    if transaction is None:
        transaction = build_transaction(**overrides)
    elif overrides:
        raise TypeError(
            'seed_transaction takes either a built Transaction or '
            'keyword overrides, not both.'
        )
    fake_db.seed(
        TRANSACTIONS_COLLECTION,
        transaction.id,
        transaction_document(transaction),
    )
    return transaction


def rating_document_id(transaction_id, rater_id):
    """Compose the deterministic rating key.

    Mirrors ``app/services/rating.py:_rating_document_id`` exactly. The
    key is the natural key itself, which is what turns document-ID
    collision into the one-vote-per-transaction constraint.

    Args:
        transaction_id: Transaction the rating belongs to.
        rater_id: Party submitting the rating.

    Returns:
        The rating document ID.
    """
    return '{0}_{1}'.format(transaction_id, rater_id)


def build_rating_document(
    transaction_id=DEFAULT_TRANSACTION_ID,
    rater_id=DEFAULT_BUYER_ID,
    ratee_id=DEFAULT_SELLER_ID,
    direction=None,
    score=DEFAULT_SCORE,
    review=None,
    vehicle_listing_id=DEFAULT_LISTING_ID,
    is_published=False,
    moderation_status=None,
    moderation_reason=None,
    created_at=None,
    updated_at=None,
):
    """Build a rating document body, field for field as stored.

    ``direction`` and ``moderation_status`` default from the
    application's own enums rather than from string literals repeated
    here, so a value renamed in ``app/schema/rating.py`` cannot leave the
    fixtures seeding documents the service will not parse.

    ``is_published`` defaults to ``False`` because that is the state a
    rating is created in: under the double-blind model a rating
    contributes nothing to its ratee's aggregate until its counterpart
    arrives or the window closes.

    Args:
        transaction_id: Transaction the rating belongs to.
        rater_id: Party who submitted it.
        ratee_id: Party being rated.
        direction: ``'buyer_to_seller'`` or ``'seller_to_buyer'``.
            Defaults to buyer-to-seller.
        score: The vote.
        review: Optional free text.
        vehicle_listing_id: Listing denormalised from the transaction.
        is_published: Whether the rating is revealed.
        moderation_status: Defaults to ``'pending'``.
        moderation_reason: Policy reason for a rejection; never a
            score-based one.
        created_at: Creation instant. Defaults to now, so the publication
            window is OPEN - a timestamp in the past would make the
            rating look overdue and publish it immediately.
        updated_at: Update instant. Defaults to ``created_at``.

    Returns:
        The document body as a dict, including its ``id``.
    """
    from app.schema.rating import ModerationStatus, RatingDirection
    moment = utc_now() if created_at is None else created_at
    return {
        'id': rating_document_id(transaction_id, rater_id),
        'transaction_id': transaction_id,
        'vehicle_listing_id': vehicle_listing_id,
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
        'updated_at': moment if updated_at is None else updated_at,
    }


def seed_rating(body=None, **overrides):
    """Write a rating document into the double and return its body.

    Seeded through the fixtures' direct write rather than through
    ``create``, so arranging a starting state never consumes the
    create-only precondition the uniqueness tests need to exercise
    themselves.

    Args:
        body: An already-built document body. When omitted one is built
            from ``overrides``.
        **overrides: Any :func:`build_rating_document` keyword, when
            ``body`` is not given.

    Returns:
        The document body that was stored, including its ``id``.

    Raises:
        TypeError: Both a built body and overrides were supplied.
    """
    if body is None:
        body = build_rating_document(**overrides)
    elif overrides:
        raise TypeError(
            'seed_rating takes either a built document body or keyword '
            'overrides, not both.'
        )
    fake_db.seed(RATINGS_COLLECTION, body['id'], body)
    return body


def access_token(user, expires_minutes=None):
    """Mint a bearer token for a user or a raw user ID.

    Signed with the configured key and algorithm, so a request carrying
    it satisfies the real ``get_current_user`` dependency rather than
    bypassing it. The token deliberately carries only ``sub`` and
    ``exp``, which is the whole shape the application issues: every
    authorization fact behind the R1 gate - ``is_verified`` above all - is
    re-read from the user document on each request. A test therefore
    needs a SEEDED user document as well as a token, see
    :func:`seed_user`, and that is the point: it is the stored flag, not
    the token, that the gate consults, so revoking verification takes
    effect on the caller's very next request.

    The claims are built here rather than by borrowing the application's
    own token helper, so this fixture depends on nothing but the settings
    both sides already share. That also makes the lifetime controllable,
    which the helper's fixed default would not.

    Args:
        user: A ``User`` model, or a user ID string.
        expires_minutes: Lifetime in minutes. Defaults to the configured
            ``ACCESS_TOKEN_EXPIRE_MINUTES``; pass a negative value to
            mint an already-expired token.

    Returns:
        An encoded JWT.

    Raises:
        ValueError: No usable subject could be derived.
    """
    from app.core.config import settings
    subject = getattr(user, 'id', user)
    if not isinstance(subject, str) or not subject:
        raise ValueError(
            'A token needs a user or a non-empty user ID, got '
            '{0!r}.'.format(user)
        )
    minutes = (
        settings.ACCESS_TOKEN_EXPIRE_MINUTES
        if expires_minutes is None
        else expires_minutes
    )
    claims = {
        'sub': subject,
        'exp': utc_now() + timedelta(minutes=minutes),
    }
    return jwt.encode(
        claims, settings.SECRET_KEY, algorithm=settings.ALGORITHM
    )


def auth_headers(user, expires_minutes=None):
    """Build the ``Authorization`` header for a user.

    Args:
        user: A ``User`` model, or a user ID string.
        expires_minutes: Lifetime in minutes, as for
            :func:`access_token`.

    Returns:
        A headers dict ready for ``TestClient``.
    """
    return {
        'Authorization': 'Bearer {0}'.format(
            access_token(user, expires_minutes)
        )
    }


@pytest.fixture(autouse=True)
def reset_firestore_double():
    """Give every test a clean datastore and untouched rating settings.

    Autouse, which is what makes it apply to the ``unittest.TestCase``
    classes in this suite: those cannot receive fixtures as method
    arguments, but autouse fixtures wrap their tests all the same.

    Two kinds of state are reset. The datastore is cleared before and
    after each test, so no test can depend on - or be broken by -
    another's writes, and in particular so that two tests may write the
    same deterministic rating ID in sequence. The rating tunables on the
    ``settings`` singleton are snapshotted and restored, because
    ``app/services/rating.py`` re-reads ``RATING_WINDOW_DAYS`` on every
    call precisely so that a test can drive the publication window by
    assigning to it - and an assignment left in place would silently
    change the meaning of every test that ran afterwards.

    Yields:
        The shared :class:`FakeFirestoreClient`, for a test that wants it
        without declaring a second fixture.
    """
    from app.core.config import settings
    saved = dict(
        (name, getattr(settings, name))
        for name in MUTABLE_RATING_SETTINGS
    )
    fake_db.reset()
    try:
        yield fake_db
    finally:
        fake_db.reset()
        for name, value in saved.items():
            setattr(settings, name, value)


@pytest.fixture
def firestore_double():
    """Return the in-memory Firestore double.

    Returns:
        The shared :class:`FakeFirestoreClient`, already emptied by
        :func:`reset_firestore_double`.
    """
    return fake_db


# ---------------------------------------------------------------------
# Bootstrap. This runs as pytest imports this module, which is BEFORE
# any test module is imported, and the order of these three calls is the
# load-bearing part: the path has to resolve before anything can be
# imported, the settings have to exist before app.core.config is
# imported, and the Firestore client has to be neutralised before
# app.db.firestore is imported.
# ---------------------------------------------------------------------
ensure_backend_on_sys_path()
seed_required_settings()
install_firestore_double()
