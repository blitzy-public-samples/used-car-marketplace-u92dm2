from pydantic import BaseModel, validator
from typing import Any, Optional

# Bounds on the message body. It reaches Firestore through
# app/api/messages.py's message.dict() with nothing in between, so an
# unbounded body is a request that writes as much as the caller cares to
# send: Firestore refuses a document over 1 MiB with an error the frozen
# handler does not catch, and everything under that limit is stored. The
# character cap is the readable limit; the byte cap is the one that actually
# protects the write, because a character outside ASCII costs up to four
# bytes. A validator rather than a check in the handler, so an oversized or
# blank body is a 422 before the recipient lookup and before the write.
_MAX_CONTENT_CHARACTERS = 4000
_MAX_CONTENT_BYTES = 16 * 1024


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

    @validator('content')
    def _content_is_present_and_bounded(cls, value: str) -> str:
        content = value.strip()
        if not content:
            raise ValueError('content must not be blank')
        if len(content) > _MAX_CONTENT_CHARACTERS:
            raise ValueError(
                'content must not exceed %d characters'
                % _MAX_CONTENT_CHARACTERS
            )
        if len(content.encode('utf-8')) > _MAX_CONTENT_BYTES:
            raise ValueError(
                'content must not exceed %d bytes when UTF-8 encoded'
                % _MAX_CONTENT_BYTES
            )
        return content

    # id, sender_id, read and timestamp are server-owned, and it is worth
    # stating why they are not forced to a server value here: this one model
    # is bound to the request body AND rebuilt from stored documents by
    # app/api/messages.py, on the send response and on every read. A
    # validator that discarded an incoming id would also discard the id that
    # handler passes on the way back out, which would turn a client-side
    # nuisance into wrong data. Until the frozen route logic can be changed
    # (HCF-8), sender_id is protected by the handler overwriting it, while a
    # client-supplied id or read flag is stored as sent -- recorded as a
    # residual in documentation/ONBOARDING.md rather than half-guarded here.
