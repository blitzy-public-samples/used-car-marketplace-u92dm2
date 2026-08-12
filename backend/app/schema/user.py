from pydantic import BaseModel, StrictBool, constr
from typing import Optional
from datetime import datetime

# The Firestore document-ID grammar, applied to ``User.id`` below.
#
# This is a security boundary rather than tidiness. A user document is
# KEYED by the JWT ``sub`` claim and this field carries that key onward,
# and downstream code uses it as a PATH COMPONENT: app/services/rating.py
# composes it with a transaction ID to form a rating's document ID, and
# hands it to ``document()`` when crediting a reputation aggregate. An
# unconstrained string there admits three real failures - a forward slash
# is read by Firestore as a nested path, so the value addresses something
# other than the document it appears to name; an oversized value produces a
# rating key the moderation endpoint refuses, leaving a rating that exists,
# is visible, and cannot be moderated; and a control character forges a
# line in anything that logs the identifier.
#
# The rules are Firestore's own - no forward slash, not "." or "..", not
# the reserved ``__.*__`` namespace - plus two of this project's: ASCII
# control characters are excluded because nothing here legitimately
# produces one, and the length ceiling is 128 rather than Firestore's 1500
# bytes because two such IDs are joined with an underscore to form a
# rating's key, so each half must leave room for the other. The pattern is
# deliberately identical to ``app/schema/rating.py``'s ``DocumentId``, and
# is restated here rather than imported: that module reads
# ``app.core.config``, so importing it would make this model - which every
# authenticated request constructs - depend on the settings singleton.
#
# ``\Z`` rather than ``$`` is load-bearing: ``$`` also matches immediately
# before a trailing newline, so an anchor of ``$`` would accept "abc\n".
#
# The consequence for a malformed stored document is deliberate and safe:
# ``app/api/auth.py:get_current_user`` catches ``ValidationError`` and
# raises its 401, so such a record authenticates as nobody rather than
# reaching a handler that would compose a path out of it.
DOCUMENT_ID_MAX_LENGTH = 128

DocumentId = constr(
    strict=True,
    min_length=1,
    max_length=DOCUMENT_ID_MAX_LENGTH,
    regex=(
        r'^(?!\.\.?\Z)'          # not "." and not ".."
        r'(?!__.*__\Z)'          # not Firestore's reserved __.*__ namespace
        r'[^/\x00-\x1F\x7F]+\Z'  # no slash, no ASCII control characters
    ),
)


class User(BaseModel):
    id: DocumentId
    email: str
    first_name: str
    last_name: str
    role: str
    created_at: datetime
    updated_at: datetime
    # StrictBool, not bool: this flag is the R1 authorization gate, and
    # Pydantic's default coercion would let a stored 1, "true" or "yes"
    # satisfy it. A verification decision must be an actual boolean, so a
    # non-boolean value fails validation instead of being read as True -
    # which means a malformed document authenticates as nobody rather
    # than as a verified user. The default stays False and defaults are
    # not validated, so documents written before this field existed
    # still deserialize: that is the no-migration guarantee.
    is_verified: StrictBool = False
    rating_average: Optional[float] = None
    rating_count: int = 0
