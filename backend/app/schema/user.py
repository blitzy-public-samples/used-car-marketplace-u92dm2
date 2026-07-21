from pydantic import BaseModel
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
    hashed_password: Optional[str] = None  # bcrypt hash stored on the Firestore user document

    @classmethod
    def from_dict(cls, data: dict) -> "User":
        # Build a User from a Firestore document dict; unknown keys are ignored.
        return cls(**data)
