from typing import Optional
from google.api_core.exceptions import Aborted
from google.cloud.firestore import Client, Transaction, transactional
from google.cloud.exceptions import NotFound
from app.core.config import settings

db = Client(project=settings.GOOGLE_CLOUD_PROJECT)

def get_document(collection: str, document_id: str) -> dict:
    try:
        doc_ref = db.collection(collection).document(document_id)
        doc = doc_ref.get()
        return doc.to_dict() if doc.exists else None
    except NotFound:
        return None

def create_document(collection: str, data: dict) -> str:
    doc_ref = db.collection(collection).add(data)
    return doc_ref[1].id

def update_document(collection: str, document_id: str, data: dict) -> bool:
    try:
        doc_ref = db.collection(collection).document(document_id)
        doc_ref.update(data)
        return True
    except NotFound:
        return False

def delete_document(collection: str, document_id: str) -> bool:
    try:
        doc_ref = db.collection(collection).document(document_id)
        doc_ref.delete()
        return True
    except NotFound:
        return False

# HUMAN ASSISTANCE NEEDED
# The following function might need additional error handling and optimization for production use
def query_documents(collection: str, filters: dict) -> list:
    query = db.collection(collection)
    for field, value in filters.items():
        query = query.where(field, '==', value)
    docs = query.stream()
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
        doc_ref.create(data)
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
       (``google-cloud-firestore==2.13.1``) ``Transaction.get`` accepts a
       ``Query`` as well as a ``DocumentReference``, so a transactional
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
        Exception: Anything ``fn`` raises propagates unchanged, after
            the transaction has been rolled back.
    """
    try:
        return transactional(fn)(db.transaction(), *args, **kwargs)
    except ValueError as error:
        cause = error.__cause__
        if isinstance(cause, Aborted):
            raise cause from error
        raise


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

    That separation leaves the declaration file with no runtime reader,
    which is why the test suite parses it and refuses any query shape it
    does not declare: index drift then fails a test run instead of
    surfacing as ``FailedPrecondition`` against a real deployment.
    ``scripts/deploy.sh`` is where the installation belongs, and it does
    not currently reach that step - repairing it is out of this feature's
    scope, so the check is the guarantee that the file stays correct.
    """
    return None
