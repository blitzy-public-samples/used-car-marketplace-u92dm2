import time
from typing import Any, Dict, Optional
from google.api_core.exceptions import (
    Aborted,
    Cancelled,
    DeadlineExceeded,
    InternalServerError,
    ResourceExhausted,
    RetryError,
    ServiceUnavailable,
)
from google.api_core.retry import Retry, if_exception_type
from google.cloud.firestore import Client, Transaction, transactional
from google.cloud.exceptions import NotFound
from app.core.config import settings

db = Client(project=settings.GOOGLE_CLOUD_PROJECT)

# How long any single datastore operation may take before it is abandoned.
#
# This is a REQUEST DEADLINE, not a performance target. The target is the
# 200 ms budget for 95% of API responses in
# ``documentation/Software Requirements Specifications (SRS).md``, which a
# healthy get-by-ID meets by two orders of magnitude; this number is the
# ceiling that decides how a datastore the process CANNOT REACH degrades.
# Without one the answer was "not at all": every handler in this
# application is synchronous, so it runs on Starlette's bounded
# threadpool, and a request that never returns holds one of those threads
# for the duration. Enough of them and an outage confined to the datastore
# becomes an outage of every endpoint, including the ones that would
# otherwise still work.
#
# Ten seconds is deliberately generous relative to the budget: it leaves
# room for the retry policy below to ride out a brief blip rather than
# turning a recoverable hiccup into a 503, while still bounding the
# blocked thread to something the pool recovers from.
#
# It bounds the whole of ONE operation, retries and stream resumptions
# included, because :func:`datastore_call` anchors an absolute deadline
# per call rather than sharing one policy - and the transaction's own
# Begin, Commit and Rollback are inside that guarantee too, through
# :class:`_BoundedTransaction`. Nothing in this module now falls back to
# the generated client's own unbounded loops.
DATASTORE_TIMEOUT_SECONDS = 10.0

# The conditions that mean "the datastore did not do this, and might if
# asked again", as opposed to a fault in what it was asked to do. Stated
# once, here, because two things consult the same classification: the
# retry policy below, and the resume decision the client's own query
# iterator makes.
#
# ``Aborted`` is deliberately absent - lock contention is resolved by
# rerunning a whole transaction, which is the transaction runner's job,
# not by retrying one read inside it.
DATASTORE_TRANSIENT_ERRORS = (
    DeadlineExceeded,
    InternalServerError,
    ResourceExhausted,
    ServiceUnavailable,
)

_IS_TRANSIENT = if_exception_type(*DATASTORE_TRANSIENT_ERRORS)


def _expiring_predicate(deadline: float):
    """Build a retry predicate that stops agreeing once time is up.

    A retry predicate normally answers one question - "is this fault
    transient?" - and that answer is what two SEPARATE loops in the
    pinned client (``google-cloud-firestore==2.13.1``) use to decide
    whether to go round again. This predicate answers a second question
    at the same time: "and is there any budget left?". Once the deadline
    passes it reports every fault as final, which is what turns both
    loops into bounded ones.

    The loop that makes this necessary is in ``Query.stream``. It wraps
    iteration in ``while True`` and, on a transient fault mid-stream,
    RESUMES with ``start_after(last_snapshot)`` and a FRESH request -
    which means a fresh per-attempt deadline every time. Bounding the
    attempt therefore bounds nothing overall: a datastore that fails
    each stream after one document can hold a worker thread for as long
    as it keeps failing that way. The resume is gated on
    ``retry._predicate(exc)``, so an expiring predicate is what ends it.

    Args:
        deadline: ``time.monotonic()`` value after which no fault is
            retried, whatever its class.

    Returns:
        A predicate taking one exception and returning whether to retry.
    """
    def should_retry(error: BaseException) -> bool:
        """Report whether ``error`` is transient and time remains."""
        return _IS_TRANSIENT(error) and time.monotonic() < deadline

    return should_retry


def datastore_call(budget: Optional[float] = None) -> Dict[str, Any]:
    """Build the keyword arguments every datastore call must pass.

    Splat it at the call site: ``ref.get(**datastore_call())``. Every
    method this codebase uses accepts both keys -
    ``DocumentReference.get``/``create``/``set``/``update``/``delete``,
    ``Query.get``/``stream``, ``Transaction.get`` and the GAPIC
    ``begin_transaction``/``commit``/``rollback`` the bounded
    transaction below issues.

    A FUNCTION RATHER THAN A CONSTANT, AND THAT IS THE WHOLE POINT
    -------------------------------------------------------------------
    This was one shared mapping holding one shared ``Retry``. Sharing is
    what made the deadline unenforceable, because a policy object has no
    idea when the operation using it started: ``timeout=`` bounds the
    api-core retry loop measured from the moment that loop begins, so
    every RESTART of an operation got the full budget again. A fresh
    policy per call closes that, by anchoring an absolute deadline in
    the predicate itself - see :func:`_expiring_predicate` for the
    ``Query.stream`` resume loop this exists to bound.

    So one call to this function corresponds to ONE datastore operation,
    and the returned pair bounds that operation as a whole rather than
    each of its attempts. Do not hoist the result into a module-level
    constant or reuse it across operations; a stale deadline would
    refuse the first fault of the next call.

    Args:
        budget: Seconds this one operation may take in total, for the
            rare call that is worth less than the standard allowance -
            see :data:`DATASTORE_ROLLBACK_TIMEOUT_SECONDS`. Defaults to
            :data:`DATASTORE_TIMEOUT_SECONDS`, which is what every
            ordinary read and write uses.

    Returns:
        ``{'retry': Retry, 'timeout': float}`` - the per-attempt
        deadline and the policy that bounds the operation containing it.
        Exhaustion of the policy arrives as
        ``google.api_core.exceptions.RetryError``, which
        :data:`DATASTORE_UNAVAILABLE_ERRORS` classifies as transient.
    """
    if budget is None:
        budget = DATASTORE_TIMEOUT_SECONDS
    budget = float(budget)
    return {
        'retry': Retry(
            predicate=_expiring_predicate(time.monotonic() + budget),
            initial=0.1,
            maximum=1.0,
            multiplier=1.3,
            timeout=budget,
        ),
        'timeout': budget,
    }


# Provider faults that mean the datastore did not do the work and might
# do it if asked again, as opposed to a fault in what it was asked to do.
# ``AlreadyExists`` and ``NotFound`` are answers and are absent here on
# purpose; a ``ValueError`` or a ``TypeError`` from this codebase is a
# defect and is absent for the same reason.
#
# The HTTP layers translate exactly this tuple into a 503 - the auth
# dependency for the per-request user read, and the ratings router for
# every handler - so an unreachable datastore produces the same JSON
# envelope as every other failure instead of a bare "Internal Server
# Error".
#
# Built FROM the retryable set rather than restating it, so the two
# cannot drift: everything worth retrying is worth reporting as
# temporary. Three conditions are reportable without being retryable per
# call - ``RetryError`` is the exhaustion verdict of the policy itself,
# ``Aborted`` is contention that survived a transaction's own reruns, and
# ``Cancelled`` is a call the transport gave up on. All three still mean
# "try again" to a caller.
DATASTORE_UNAVAILABLE_ERRORS = DATASTORE_TRANSIENT_ERRORS + (
    Aborted,
    Cancelled,
    RetryError,
)

# What a caller is told when the datastore cannot be reached, and how
# long to wait. Declared here, beside the classification, so the auth
# dependency and the ratings router answer with one sentence rather than
# two that drift. It describes the condition without naming the provider,
# the host or the operation - an outage is not an invitation to publish
# infrastructure detail.
DATASTORE_UNAVAILABLE_DETAIL = (
    'The service could not reach its datastore. This is temporary - '
    'please retry shortly.'
)
DATASTORE_RETRY_AFTER_SECONDS = 5

# How long abandoning a transaction may take, which is deliberately much
# less than an operation that does something.
#
# A ``Rollback`` is issued only on a path that has ALREADY failed, and
# the transaction runner issues one after every failure - so a rollback
# that spent the full allowance would double the time an outage holds a
# worker thread, to abandon work that is being abandoned anyway. It is
# also the one call whose failure costs nothing durable: Firestore
# releases a transaction's locks when it times out server-side, so an
# unacknowledged rollback resolves itself. Trying briefly and moving on
# is therefore the right trade, and it keeps the worst case for a
# completely unreachable datastore at one operation's budget plus this.
DATASTORE_ROLLBACK_TIMEOUT_SECONDS = 2.0


class _BoundedTransaction(Transaction):
    """A ``Transaction`` whose own three RPCs are bounded.

    The pinned client (``google-cloud-firestore==2.13.1``) bounds the
    reads and writes a caller issues, because those take ``retry`` and
    ``timeout``. It does NOT bound the three RPCs it issues itself, and
    the gap is not a slow path - it is an unbounded one:

    * ``Commit`` goes through the module-level ``_commit_with_retry``,
      whose body is ``while True: try: commit(); except
      ServiceUnavailable: pass`` followed by a sleep. There is no attempt
      ceiling and no deadline. Against a datastore returning
      ``UNAVAILABLE`` it retries forever.
    * ``BeginTransaction`` and ``Rollback`` are issued with no ``retry``
      and no ``timeout`` at all, so they fall back to the generated
      client's defaults - measured at roughly 45 seconds each to fail
      against an unreachable datastore.

    Every handler in this application is synchronous, so it runs on
    Starlette's bounded threadpool. A request that never returns holds
    one of those threads for as long as it hangs, and enough of them turn
    an outage confined to the datastore into an outage of every endpoint
    - including the ones that would otherwise still work. That is the
    availability failure this class exists to prevent.

    The three overrides below reissue exactly the same requests through
    exactly the same generated client, adding nothing but
    :func:`datastore_call`'s policy and deadline. Behaviour against a
    HEALTHY datastore is therefore unchanged; what changes is that an
    unreachable one produces a transient exception in seconds rather than
    a thread that never comes back. Contention retries stay the
    transaction runner's business and stay bounded by its own
    ``max_attempts``, which this class does not touch.

    Private attributes of the base class are used deliberately and are
    the reason this is a subclass rather than a wrapper: ``_id``,
    ``_write_pbs``, ``_options_protobuf`` and ``_clean_up`` are the
    transaction's own lifecycle state, and the runner
    (``_Transactional.__call__``) drives that lifecycle through
    ``_begin``/``_commit``/``_rollback``. Overriding those three methods
    is the only place a project-owned deadline can be inserted without
    reimplementing the runner.
    """

    def _begin(self, retry_id: Optional[bytes] = None) -> None:
        """Open the transaction, bounding ``BeginTransaction``.

        Args:
            retry_id: ID of a transaction being retried, which preserves
                its place in line under contention. Forwarded to the
                request exactly as the base class forwards it.

        Raises:
            ValueError: This transaction has already begun.
            google.api_core.exceptions.GoogleAPICallError: The call
                failed, or the deadline passed with it still failing.
        """
        if self.in_progress:
            raise ValueError(
                'This transaction has already begun with ID '
                '{0!r}.'.format(self._id)
            )
        response = self._client._firestore_api.begin_transaction(
            request={
                'database': self._client._database_string,
                'options': self._options_protobuf(retry_id),
            },
            metadata=self._client._rpc_metadata,
            **datastore_call(),
        )
        self._id = response.transaction

    def _rollback(self) -> None:
        """Abandon the transaction, bounding ``Rollback``.

        The runner calls this on ANY error, including the error that a
        bounded commit raises, so an unbounded rollback would give back
        the hang the bounded commit just prevented. It gets the shorter
        :data:`DATASTORE_ROLLBACK_TIMEOUT_SECONDS` allowance, for the
        reason recorded beside that constant.

        NOTHING IN PROGRESS IS NOT AN ERROR HERE, AND THAT IS A FIX
        ---------------------------------------------------------------
        The base class raises ``ValueError`` when asked to roll back a
        transaction that never began. That reads as strictness and
        behaves as data loss of a different kind: the runner rolls back
        inside ``except BaseException``, so when it is the BEGIN that
        failed, the ``ValueError`` raised here REPLACES the transient
        fault that brought us here. Measured against an unreachable
        emulator, a datastore outage surfaced as a bare ``ValueError``
        with no cause - so the HTTP layers, which classify transient
        provider faults into a 503 with ``Retry-After``, saw an
        unclassifiable defect and answered 500 instead.

        There is genuinely nothing to abandon in that state, so this
        returns quietly and lets the original exception propagate with
        its class intact. No RPC is issued, and the object is already
        clean.

        Raises:
            google.api_core.exceptions.GoogleAPICallError: The rollback
                failed for the whole of its (short) deadline. Local
                state is cleaned up regardless, exactly as the base
                class does, so the object cannot be left claiming to
                hold a transaction that is gone.
        """
        if not self.in_progress:
            return
        try:
            self._client._firestore_api.rollback(
                request={
                    'database': self._client._database_string,
                    'transaction': self._id,
                },
                metadata=self._client._rpc_metadata,
                **datastore_call(DATASTORE_ROLLBACK_TIMEOUT_SECONDS),
            )
        finally:
            self._clean_up()

    def _commit(self) -> list:
        """Commit the staged writes, bounding ``Commit``.

        Replaces the base class's unbounded ``_commit_with_retry`` loop
        with one bounded policy. Retrying ``Commit`` is safe here for the
        same reason it was safe there - the request carries a transaction
        ID, so a retry commits the same transaction rather than applying
        the writes twice - and the policy in :func:`datastore_call`
        retries the same ``UNAVAILABLE`` condition that loop did, plus
        the other transient conditions, up to a deadline.

        Returns:
            The write results, in the order the writes were staged.

        Raises:
            ValueError: No transaction is in progress.
            google.api_core.exceptions.GoogleAPICallError: The commit
                failed, or the deadline passed with it still failing.
                ``Aborted`` reaches the runner, which reruns the whole
                callable; ``RetryError`` and the rest reach the caller as
                the transient faults they are.
        """
        if not self.in_progress:
            raise ValueError(
                'No transaction is in progress, so there is nothing to '
                'commit.'
            )
        response = self._client._firestore_api.commit(
            request={
                'database': self._client._database_string,
                'writes': self._write_pbs,
                'transaction': self._id,
            },
            metadata=self._client._rpc_metadata,
            **datastore_call(),
        )
        self._clean_up()
        return list(response.write_results)


def _new_transaction(read_only: bool = False):
    """Open the transaction object the runners below drive.

    Asks the client for its own transaction first and substitutes
    :class:`_BoundedTransaction` only when what came back is the pinned
    client's real ``Transaction``. That order matters for one specific
    reason: the test suite installs an in-memory Firestore double by
    replacing the ``Client`` symbol with a FACTORY FUNCTION, so a type
    test against ``Client`` is not even legal there, while a double's own
    transaction object carries its own commit, rollback and conflict
    semantics that must not be replaced by RPC-issuing overrides. Asking
    the client, then checking what it produced, is correct for both.

    Args:
        read_only: Open a read-only transaction, which takes no locks
            and refuses writes.

    Returns:
        A transaction object bound to :data:`db`.
    """
    transaction = db.transaction(read_only=read_only)
    if not isinstance(transaction, Transaction):
        # A double, which supplies its own transaction semantics.
        return transaction
    return _BoundedTransaction(db, read_only=read_only)


def get_document(collection: str, document_id: str) -> dict:
    try:
        doc_ref = db.collection(collection).document(document_id)
        doc = doc_ref.get(**datastore_call())
        return doc.to_dict() if doc.exists else None
    except NotFound:
        return None


def create_document(collection: str, data: dict) -> str:
    doc_ref = db.collection(collection).add(data, **datastore_call())
    return doc_ref[1].id


def update_document(collection: str, document_id: str, data: dict) -> bool:
    try:
        doc_ref = db.collection(collection).document(document_id)
        doc_ref.update(data, **datastore_call())
        return True
    except NotFound:
        return False


def delete_document(collection: str, document_id: str) -> bool:
    try:
        doc_ref = db.collection(collection).document(document_id)
        doc_ref.delete(**datastore_call())
        return True
    except NotFound:
        return False


# HUMAN ASSISTANCE NEEDED
# The following function might need additional error handling and
# optimization for production use
def query_documents(collection: str, filters: dict) -> list:
    query = db.collection(collection)
    for field, value in filters.items():
        query = query.where(field, '==', value)
    docs = query.stream(**datastore_call())
    return [doc.to_dict() for doc in docs]


def create_document_with_id(
    collection: str,
    document_id: str,
    data: dict,
    transaction: Optional[Transaction] = None,
) -> str:
    """Create a document at a caller-supplied ID, failing on collision.

    ``create_document`` above cannot serve this purpose: it calls
    ``.add(data)`` and lets Firestore allocate a random identifier, so a
    deterministic natural key is structurally impossible with it. This
    helper writes to the exact ``document_id`` it is handed, which is
    what allows a natural key to double as a uniqueness constraint.

    The write goes through ``DocumentReference.create()``, whose
    create-only semantics are the only uniqueness guarantee Firestore
    offers - the product has no unique constraints and no unique
    indexes. A read-then-write "does one already exist?" check would
    race, since two concurrent callers could both observe an absent
    document and both write; ``.create()`` rejects the second write
    however the two interleave.

    A collision raises ``google.api_core.exceptions.AlreadyExists``, and
    that exception is deliberately NOT caught here. The collision is the
    signal the caller needs: swallowing it - the way the sibling helpers
    in this module swallow ``NotFound`` - would report a rejected
    duplicate as a success. Callers map it to their own domain error.

    ``document_id`` is opaque here; composing or validating a natural
    key belongs to the caller that owns what the key means.

    Pass ``transaction`` to enrol the create in a transaction that is
    already open, so a uniqueness constraint and whatever else must hold
    with it commit together or not at all. Without that argument the
    create is its own standalone write. Both routes go through
    create-only semantics and both surface a collision identically, so
    this remains the ONE primitive that owns create-only writes: a
    caller inside a transaction has no reason to reach past it to
    ``transaction.create`` and reimplement the same rule, which is how
    two subtly different versions of a uniqueness guarantee start to
    exist side by side.

    Args:
        collection: Target Firestore collection name.
        document_id: Exact document ID to create; never generated or
            rewritten by this helper.
        data: Document body to persist.
        transaction: Open ``Transaction`` to enrol the create in, or
            ``None`` for a standalone write.

    Returns:
        The ``document_id`` that was created. Note that when
        ``transaction`` is supplied the write is only STAGED at this
        point and becomes durable when that transaction commits.

    Raises:
        google.api_core.exceptions.AlreadyExists: If a document already
            exists at ``collection/document_id``. Raised on both routes;
            inside a transaction it also aborts that transaction, so no
            other write staged alongside it is committed either.
    """
    doc_ref = db.collection(collection).document(document_id)
    if transaction is None:
        # Bounded like every other call this application makes: see
        # :func:`datastore_call`. The transactional branch takes no such
        # arguments because it does not issue an RPC - the create is
        # STAGED and travels with the transaction's commit, which
        # :class:`_BoundedTransaction` bounds instead.
        doc_ref.create(data, **datastore_call())
    else:
        transaction.create(doc_ref, data)
    return document_id


def run_in_transaction(fn, *args, **kwargs):
    """Run ``fn`` inside a single Firestore transaction.

    This is the module's atomicity primitive: every write ``fn`` makes
    commits together or not at all. It exists so an invariant spanning
    more than one document - a record plus a counter denormalized onto
    another document, say - can never be left half applied, which is
    what an unguarded sequence of separate writes risks.

    ``fn`` is invoked as ``fn(transaction, *args, **kwargs)``: the
    ``Transaction`` arrives as its first positional argument and every
    read and write inside ``fn`` must go through it. Whatever ``fn``
    returns is returned to the caller; if ``fn`` raises, the transaction
    is rolled back and nothing it wrote is committed. A fresh wrapper is
    built per call because the wrapper carries per-attempt retry state
    and so cannot be shared between invocations.

    Three Firestore constraints govern the body of ``fn``, recorded here
    so they travel with the primitive:

    1. All reads must precede all writes. A read issued after the first
       write of the same transaction is rejected.
    2. Every read must go through the transaction to be locked, using
       either ``ref.get(transaction=transaction)`` or
       ``transaction.get(ref_or_query)``. On the pinned client
       (``google-cloud-firestore==2.13.1``) ``Transaction.get`` is typed
       ``ref_or_query: DocumentReference | Query``, so a transactional
       read is NOT restricted to get-by-ID.
       Aggregates in this codebase are nonetheless denormalized onto the
       document they describe and read by ID, and that is a deliberate
       choice rather than a limitation: recomputing a mean by reading
       every rating a popular seller has received grows without bound,
       and the SRS requires API responses within 200 ms for 95% of
       requests. A locked query would also widen the transaction's
       conflict footprint from one document to a whole result set,
       making contention - and therefore reruns - far more likely.
    3. A transactional read takes a LOCK on what it read, and that lock
       is held until the transaction commits, fails or times out; while
       held it blocks other transactions, batched writes and
       non-transactional writes from changing that document. This is a
       server-client transaction against Firestore in Native mode,
       whose Standard edition applies pessimistic concurrency controls
       by default, and the concurrency mode is a database-level setting
       rather than something this code selects. So do not reason about
       the body of ``fn`` as though its reads were merely version-checked
       at commit time: keep the set of documents it reads as small as
       the invariant allows, and keep it short, because everything it
       reads is unavailable to other writers for the duration.
    4. ``fn`` MUST be safe to run more than once. Under contention a
       commit is rejected with ``ABORTED`` and the client reruns ``fn``
       from the top, so it must be free of side effects that a rollback
       cannot undo - no email, no payment call, no mutation of module
       state - and it must not depend on values computed on a previous
       attempt.
    5. Retries are finite, and their exhaustion is reported oddly. Once
       the client's attempts are used up it gives up, and on the pinned
       release (``google-cloud-firestore==2.13.1``) it reports that by
       raising ``ValueError`` CHAINED FROM the final ``Aborted`` rather
       than raising ``Aborted`` itself - see
       ``firestore_v1/transaction.py``, ``raise ValueError(msg) from
       last_exc``. A caller that catches the retryable Google API
       exceptions would therefore miss contention exhaustion entirely
       and see an opaque ``ValueError``. This function normalises that
       case back into the underlying ``Aborted`` so contention
       exhaustion is catchable as the transient transport fault it is.

    The normalisation is scoped as narrowly as it can be: only a
    ``ValueError`` whose ``__cause__`` IS an ``Aborted`` is translated.
    A ``ValueError`` that ``fn`` itself raised - a domain validation
    failure, say - has no such cause and propagates untouched, which
    matters because those carry meaning the caller has to see.

    Args:
        fn: Callable taking the ``Transaction`` as its first positional
            argument.
        *args: Additional positional arguments forwarded to ``fn``.
        **kwargs: Keyword arguments forwarded to ``fn``.

    Returns:
        Whatever ``fn`` returns on the attempt that commits.

    Raises:
        google.api_core.exceptions.Aborted: Contention persisted through
            every retry the client allows.
        google.api_core.exceptions.GoogleAPICallError: The transaction's
            own ``BeginTransaction``, ``Commit`` or ``Rollback`` failed
            for the whole of its deadline. Bounded rather than endless -
            see :class:`_BoundedTransaction`, which the transaction
            object comes from.
        Exception: Anything ``fn`` raises propagates unchanged, after
            the transaction has been rolled back.
    """
    try:
        return transactional(fn)(_new_transaction(), *args, **kwargs)
    except ValueError as error:
        cause = error.__cause__
        if isinstance(cause, Aborted):
            raise cause from error
        raise


def run_in_read_only_transaction(fn, *args, **kwargs):
    """Run ``fn`` against ONE pinned snapshot of the datastore.

    The read counterpart of :func:`run_in_transaction`, and it exists for
    a different reason: not atomicity of writes, but CONSISTENCY of
    reads. A response assembled from two independent reads can straddle a
    commit that happened between them, so it can report a count taken
    after a change beside a list taken before it - not a stale answer but
    a self-contradictory one. Every read inside ``fn`` observes the same
    instant, so an envelope built here cannot disagree with itself.

    ``fn`` is invoked as ``fn(transaction, *args, **kwargs)`` exactly as
    for the write primitive, and every read inside it must go through
    that transaction - ``ref.get(transaction=transaction)``,
    ``query.stream(transaction=transaction)`` or
    ``transaction.get(ref_or_query)`` - because an unenrolled read is
    simply a separate read at a separate instant, which is the very thing
    this call exists to prevent.

    The transaction is READ-ONLY, which is a stronger statement than "it
    happens not to write":

    * a write attempted inside ``fn`` raises rather than committing, so
      the guarantee cannot be eroded by a later edit that adds one;
    * Firestore takes no locks for it, so a public read path does not
      contend with the write paths it runs beside;
    * and the client's own retry loop does not retry it, because there is
      no contention verdict to retry - a read-only transaction reads a
      consistent snapshot instead of competing for one.

    Verified against the pinned client (``google-cloud-firestore==2.13.1``)
    and a live Firestore emulator: document reads, ``Query.stream`` and
    ``Transaction.get`` all work through a read-only transaction, and it
    commits cleanly with no writes staged.

    Args:
        fn: Callable taking the ``Transaction`` as its first positional
            argument. It must only read.
        *args: Additional positional arguments forwarded to ``fn``.
        **kwargs: Keyword arguments forwarded to ``fn``.

    Returns:
        Whatever ``fn`` returns.

    Raises:
        ValueError: ``fn`` attempted a write. The transaction is read-only
            and the client refuses it.
        google.api_core.exceptions.GoogleAPICallError: ``BeginTransaction``
            or ``Commit`` failed for the whole of its deadline. Bounded
            by :class:`_BoundedTransaction`, which matters as much here as
            on the write path: this runs on a PUBLIC read.
        Exception: Anything ``fn`` raises propagates unchanged.
    """
    return transactional(fn)(
        _new_transaction(read_only=True),
        *args,
        **kwargs
    )


async def initialize_db() -> None:
    """Satisfy this module's startup contract, awaited by ``app.main``.

    ``app/main.py`` imports this coroutine and awaits it from the
    FastAPI ``startup`` event, so it has to be awaitable: a plain
    function returning ``None`` would make ``await initialize_db()``
    raise ``TypeError`` and abort startup.

    There is deliberately nothing to do. The ``Client`` at the top of
    this module is constructed eagerly at import time, so it already
    exists before any startup hook runs, and it stays exactly where it
    is - five modules import that symbol at module scope, so it is
    neither moved, deferred nor duplicated into here. Firestore is also
    schemaless: no schema to create, no migration to apply, no schema
    version to check. The composite indexes are declared in
    ``infrastructure/firestore.indexes.json`` and installed as a
    deployment step, not from application code.

    That separation leaves the declaration file with no runtime reader
    inside the application, which is why the test suite parses it and
    refuses any query shape it does not declare: index drift then fails a
    test run instead of surfacing as ``FailedPrecondition`` against a
    real deployment. ``scripts/deploy.sh`` is where the installation
    happens, and it derives one
    ``gcloud firestore indexes composite create`` invocation per declared
    index from that same file, so the declaration is both the deployment
    input and the shape the suite enforces.
    """
    return None
