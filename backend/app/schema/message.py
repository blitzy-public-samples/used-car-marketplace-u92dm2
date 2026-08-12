"""Pydantic model for the messaging domain.

``backend/app/api/messages.py`` imports ``Message`` from this module,
which was never written - so the messages router, and therefore the
``app.main`` application that registers it, could not be imported at
all. This module restores that import and gives the messaging domain
its first server-side request validation.

Pydantic v1 semantics apply: ``pydantic==1.10.13`` is pinned in
``backend/requirements.txt``, and the handler uses the v1 ``.dict()``
serialisation API.
"""
from pydantic import BaseModel
from typing import Any, Optional


class Message(BaseModel):
    """A message exchanged between two marketplace users.

    Only ``recipient_id`` and ``content`` are required; every other
    field is supplied by the server or the datastore and so carries a
    default, keeping the model constructible from a bare request body.

    Field names follow ``backend/app/api/messages.py``, the
    authoritative consumer. Pre-existing inconsistency: the client
    mirror ``frontend/src/schema/message.ts`` names the same two fields
    ``receiverId`` and ``createdAt`` instead of ``recipient_id`` and
    ``timestamp``. Recorded, not bridged with aliases - reconciling the
    two schemas is outside this change.
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
