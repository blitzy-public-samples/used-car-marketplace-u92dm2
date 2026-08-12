from google.cloud.firestore import Client, transactional
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


def create_document_with_id(collection: str, document_id: str,
                            data: dict) -> str:
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

    Args:
        collection: Target Firestore collection name.
        document_id: Exact document ID to create; never generated or
            rewritten by this helper.
        data: Document body to persist.

    Returns:
        The ``document_id`` that was created.

    Raises:
        google.api_core.exceptions.AlreadyExists: If a document already
            exists at ``collection/document_id``.
    """
    db.collection(collection).document(document_id).create(data)
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
    2. Only get-by-ID reads are permitted. A query cannot be locked, so
       an aggregate cannot be recomputed by querying a collection from
       inside a transaction; it must be read from a known document ID.
       That is why aggregates here are denormalized onto the document
       they describe.
    3. Concurrency is optimistic. Before committing, Firestore checks
       whether anything the transaction touched has changed and reruns
       ``fn`` if it has, so ``fn`` must be safe to run more than once
       and must not act outside the transaction.

    Args:
        fn: Callable taking the ``Transaction`` as its first positional
            argument.
        *args: Additional positional arguments forwarded to ``fn``.
        **kwargs: Keyword arguments forwarded to ``fn``.

    Returns:
        Whatever ``fn`` returns on the attempt that commits.
    """
    return transactional(fn)(db.transaction(), *args, **kwargs)


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
    ``infrastructure/firestore.indexes.json`` and installed by the
    deploy script, not from application code.
    """
    return None
