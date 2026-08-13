import re
from pydantic import BaseModel, StrictBool, constr
from typing import Any, Optional
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

# The grammar itself, stated ONCE and shared by the two ways it is
# applied: through Pydantic, as the ``DocumentId`` type below, and
# directly, through ``is_valid_document_id`` further down. Restating the
# pattern beside the predicate would give this module two grammars that
# agree only until one of them is edited.
DOCUMENT_ID_REGEX = (
    r'^(?!\.\.?\Z)'          # not "." and not ".."
    r'(?!__.*__\Z)'          # not Firestore's reserved __.*__ namespace
    r'[^/\x00-\x1F\x7F]+\Z'  # no slash, no ASCII control characters
)

DocumentId = constr(
    strict=True,
    min_length=1,
    max_length=DOCUMENT_ID_MAX_LENGTH,
    regex=DOCUMENT_ID_REGEX,
)

# Compiled once at import. ``get_current_user`` consults the predicate
# below on EVERY authenticated request, so the pattern is not recompiled
# per call - and ``re``'s internal cache is not something to rely on for
# a hot path.
_DOCUMENT_ID_PATTERN = re.compile(DOCUMENT_ID_REGEX)


def is_valid_document_id(value: Any) -> bool:
    """Report whether a bare value may be used as a Firestore document ID.

    ``DocumentId`` above covers every value that arrives through a model.
    This predicate covers the values that do NOT, and there is one that
    matters: the JWT ``sub`` claim, which ``app/api/auth.py`` reads
    straight out of a decoded token and hands to ``document()`` in order
    to find the caller's user record.

    That path had to be closed rather than left to the model. Firestore
    reads a forward slash in a document ID as a PATH SEPARATOR, so a
    claim of ``users/someone`` addresses a nested collection instead of a
    document, and the client rejects the resulting odd-length path with a
    ``ValueError`` - before ``User`` is ever constructed, so the grammar
    on ``User.id`` never gets to refuse it. A dot segment (``.`` or
    ``..``) travels further and is refused by the datastore itself as an
    ``InvalidArgument``. Both surfaced as a 500 on every protected
    endpoint, which told a caller holding an unusable credential that the
    server was broken; the honest answer is that the credential does not
    identify anybody, which is a 401.

    The rules applied are exactly ``DocumentId``'s, from the same
    ``DOCUMENT_ID_REGEX``, plus its type and length bounds - so a value
    this predicate accepts is a value the model would also accept, and a
    caller cannot be admitted here only to be refused a moment later.

    Args:
        value: Candidate identifier, of any type. A non-string is not a
            document ID: this mirrors ``constr(strict=True)``, which
            refuses to coerce, so an integer or ``None`` claim is
            rejected rather than stringified into a path component.

    Returns:
        ``True`` only when the value is a string that satisfies the
        grammar and both length bounds.
    """
    if not isinstance(value, str):
        return False
    if not 1 <= len(value) <= DOCUMENT_ID_MAX_LENGTH:
        return False
    return _DOCUMENT_ID_PATTERN.match(value) is not None


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
    # The EXACT mean of every published score this user has received,
    # unrounded, so that ``rating_average * rating_count`` recovers their
    # exact total. That is what the incremental fold in
    # ``app/services/rating.py`` reconstructs, and storing a rounded
    # value here made each fold inherit the previous one's rounding
    # error - two users with identical ratings could end up with
    # different averages. Presentation rounding belongs to the response
    # projection, not to the stored field.
    #
    # ``None``, never 0.0, for a user nobody has rated: the scale starts
    # at 1, so a zero would read as an earned reputation rather than as
    # the absence of one.
    rating_average: Optional[float] = None
    rating_count: int = 0
