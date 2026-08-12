"""Pydantic models for the messaging domain.

``backend/app/api/messages.py:L3`` imports ``Message`` from this module,
which was never written, so that import raised ``ModuleNotFoundError``.
This module removes that one blocker and gives the messaging domain its
first server-side request validation.

It does NOT make ``app.main`` importable on its own, and should not be
read as claiming to. Importing the application still fails earlier, at
``app/api/listings.py:L14``, because every existing router annotates
``current_user: User`` without importing ``User`` - a pre-existing
``NameError`` in three reference-only modules that is outside this
change. This module removes one obstacle on that path; the annotation
imports are the remaining one.

REQUEST STATE AND SERVER STATE ARE SEPARATE
-------------------------------------------------------------------
``id``, ``sender_id``, ``read`` and ``timestamp`` are owned by the
server, never by the caller. Two models express that split:
:class:`MessageCreate` is the request contract and carries only what a
client may legitimately state; :class:`Message` is the persisted and
response contract and carries the whole record.

The router is not able to bind :class:`MessageCreate` yet - it is
reference-only at this stage and cannot be edited here - so
:class:`Message` additionally guarantees the same property on its own,
by excluding the two server-owned fields that would otherwise do harm
from everything ``.dict()`` exports. That matters because the handler
builds its write payload as ``message.dict()``:

* ``id`` excluded - otherwise the payload carries an ``id`` key (``None``
  when the client omits it, the client's own value when it does not).
  Persisting it both stores a bogus identifier and breaks every
  subsequent read, because the handler then reconstructs each message as
  ``Message(**msg.to_dict(), id=msg.id)`` and Python rejects the
  duplicate keyword with ``TypeError`` before validation is even
  reached. With ``id`` absent from the stored body, the snapshot ID is
  injected exactly once.
* ``read`` excluded - otherwise a sender can mark their own message as
  already read by including ``read: true`` in the request body.

``sender_id`` and ``timestamp`` stay exported because the handler
assigns both unconditionally after calling ``.dict()``, so a client
value cannot survive into the datastore; excluding them as well would
strip them from responses for no security gain.

Pydantic v1 semantics apply: ``pydantic==1.10.13`` is pinned in
``backend/requirements.txt``, field-level ``exclude`` is a v1 feature,
and the handler uses the v1 ``.dict()`` serialisation API.
"""
from pydantic import BaseModel, Field
from typing import Any, Optional


class MessageCreate(BaseModel):
    """What a client may state when sending a message.

    The complete set of client-owned fields, and nothing else. Server
    state - the identifier, the sender, the read flag and the timestamp
    - is absent by design rather than by oversight, so a request cannot
    assert any of it. This is the contract the messages router should
    bind to its request body when it is repaired.

    Field names follow ``backend/app/api/messages.py``, the
    authoritative consumer. Pre-existing inconsistency: the client
    mirror ``frontend/src/schema/message.ts`` names the same field
    ``receiverId`` rather than ``recipient_id``. Recorded, not bridged
    with aliases - reconciling the two schemas is outside this change.
    """

    recipient_id: str
    vehicle_listing_id: Optional[str] = None
    content: str


class Message(BaseModel):
    """A message exchanged between two marketplace users, as persisted.

    Only ``recipient_id`` and ``content`` are required; every other
    field is supplied by the server or the datastore and so carries a
    default, keeping the model constructible from a bare request body
    and from a stored document alike.

    ``id`` and ``read`` are declared ``exclude=True``: they remain
    readable and assignable attributes, and they are still populated
    when a stored document supplies them, but they never appear in
    ``.dict()``. See the module docstring - that is what keeps a
    client-supplied identifier or read state out of the datastore and
    what stops the read path passing ``id`` twice.

    Pre-existing inconsistency: the client mirror
    ``frontend/src/schema/message.ts`` names two of these fields
    ``receiverId`` and ``createdAt`` instead of ``recipient_id`` and
    ``timestamp``. Recorded, not bridged with aliases.
    """

    id: Optional[str] = Field(None, exclude=True)
    sender_id: Optional[str] = None
    recipient_id: str
    vehicle_listing_id: Optional[str] = None
    content: str
    read: bool = Field(False, exclude=True)
    # Permissive by necessity: the handler assigns
    # ``firestore.SERVER_TIMESTAMP``, a sentinel object rather than a
    # ``datetime``, which a strict ``datetime`` annotation rejects.
    # Reading the document back yields a real timestamp instead, so both
    # shapes - and absence - must be accepted.
    timestamp: Optional[Any] = None
