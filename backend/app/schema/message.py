from pydantic import BaseModel
from typing import Any, Optional


class Message(BaseModel):
    # Created because app/api/messages.py imports it and main.py cannot
    # import without it. Field names follow what app/api/messages.py
    # already reads and writes (recipient_id, timestamp), which is the
    # frozen consumer; the remaining fields follow the documented Messages
    # collection in documentation/Technical Specifications.md.
    id: Optional[str] = None
    sender_id: Optional[str] = None
    recipient_id: str
    vehicle_listing_id: Optional[str] = None
    content: str
    read: bool = False
    # Accepts both the Firestore SERVER_TIMESTAMP sentinel written on send
    # and the DatetimeWithNanoseconds value returned on read.
    timestamp: Optional[Any] = None

    # id, sender_id, read and timestamp are server-owned, and it is worth
    # stating why nothing here forces them to a server value: this one model
    # is bound to the request body AND rebuilt from stored documents by
    # app/api/messages.py, on the send response and on every read. A
    # validator that discarded an incoming id would also discard the id that
    # handler passes on the way back out, and one that rejected a stored
    # value would turn a single bad document into a failed read of the whole
    # list. Until the frozen route logic can be changed (HCF-8), sender_id is
    # protected by the handler overwriting it, while a client-supplied id or
    # read flag is stored as sent -- recorded as a residual in
    # documentation/ONBOARDING.md rather than half-guarded here.
