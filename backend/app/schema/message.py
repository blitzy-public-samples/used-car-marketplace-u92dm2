"""Pydantic models for the messaging domain.

``backend/app/api/messages.py:L3`` imports ``Message`` from this module,
which was never written, so that import raised ``ModuleNotFoundError``.
This module removes that one blocker and gives the messaging domain its
first server-side request validation.

It does NOT make ``app.main`` importable on its own, and should not be
read as claiming to. Two pre-existing obstacles in reference-only
modules remain on that path, both outside this change:

* ``app/api/listings.py:L14`` - and the same line in the transactions
  and messages routers - annotates ``current_user: User`` without
  importing ``User``, which raises ``NameError`` at import. This is the
  one the application fails on first.
* ``app/services/payment.py:L1`` does ``from stripe import Stripe``, a
  name ``stripe==7.9.0`` does not export, so the transactions router
  that imports ``process_payment`` cannot import either.

This module removes one obstacle on that path; those two are the rest of
it.

REQUEST STATE AND SERVER STATE ARE SEPARATE
-------------------------------------------------------------------
``id``, ``sender_id``, ``read`` and ``timestamp`` are owned by the
server, never by the caller. Two models express that split:
:class:`MessageCreate` is the request contract and carries only what a
client may legitimately state; :class:`Message` is the persisted and
response contract and carries the whole record.

That split is enforced by WHICH MODEL a boundary binds, not by hiding
fields on the response model. Every field of :class:`Message` is
therefore fully serialisable. Suppressing a field from ``.dict()`` to
keep a client from asserting it looks like defence but is not: ``read``
is the read-state of a message, so a ``read`` that never reaches
``.dict()`` can never be PERSISTED either, which makes the state
permanently unrecordable rather than merely unassertable by a caller -
and a response that omits it cannot tell a client whether a message has
been read. The request contract is the place to refuse a client-supplied
``read``, and :class:`MessageCreate` is that contract: it has no ``read``
field to supply.

KNOWN CONSEQUENCE, recorded rather than designed around
-------------------------------------------------------------------
``backend/app/api/messages.py`` is reference-only and cannot be edited
here, so it still builds its write payload from ``Message(...).dict()``
at :L17 instead of binding :class:`MessageCreate`. Because ``id`` is
serialisable, that payload carries an ``id`` key, and the read path at
:L35/:L37 rebuilds each message as ``Message(**msg.to_dict(),
id=msg.id)`` - which Python rejects with ``TypeError`` for the duplicate
keyword.

That is a pre-existing defect in the handler and the fix belongs there:
bind :class:`MessageCreate` on the write path, and stop passing ``id``
twice on the read path. It is deliberately NOT worked around here by
excluding ``id`` from serialisation. Doing so would trade one visible,
fixable handler bug for two invisible contract defects - an unrecordable
read-state and a response model that silently drops fields - and would
leave this schema permanently shaped around a bug in a module it does
not own. No messaging endpoint executes today in any case: ``messages.py``
raises ``NameError`` at import, because it annotates
``current_user: User`` without importing ``User``.

Pydantic v1 semantics apply: ``pydantic==1.10.13`` is pinned in
``backend/requirements.txt`` and the handler uses the v1 ``.dict()``
serialisation API.
"""
from typing import Any, Optional

from pydantic import BaseModel


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

    Every field is serialisable, including ``id`` and ``read``. This is
    the persisted and response contract, so a field it withholds from
    ``.dict()`` is a field that can be neither stored nor reported;
    ``read`` in particular is the message's read-state, which would be
    permanently unrecordable if it never reached a write. Keeping a
    caller from ASSERTING server state is the job of
    :class:`MessageCreate`, which simply has no such field. See the
    module docstring for the handler defect this deliberately does not
    paper over.

    Pre-existing inconsistency: the client mirror
    ``frontend/src/schema/message.ts`` names two of these fields
    ``receiverId`` and ``createdAt`` instead of ``recipient_id`` and
    ``timestamp``. Recorded, not bridged with aliases.
    """

    id: Optional[str] = None
    sender_id: Optional[str] = None
    recipient_id: str
    vehicle_listing_id: Optional[str] = None
    content: str
    read: bool = False
    # Permissive by necessity: the handler assigns
    # ``firestore.SERVER_TIMESTAMP``, a sentinel object rather than a
    # ``datetime``, which a strict ``datetime`` annotation rejects.
    # Reading the document back yields a real timestamp instead, so both
    # shapes - and absence - must be accepted.
    timestamp: Optional[Any] = None
