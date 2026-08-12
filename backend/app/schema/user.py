from pydantic import BaseModel, StrictBool
from typing import Optional
from datetime import datetime

class User(BaseModel):
    id: str
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
