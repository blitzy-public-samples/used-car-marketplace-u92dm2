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
