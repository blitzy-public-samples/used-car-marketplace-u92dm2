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