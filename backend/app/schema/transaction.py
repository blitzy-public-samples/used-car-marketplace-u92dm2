from pydantic import BaseModel
from typing import Optional
from datetime import datetime

class Transaction(BaseModel):
    id: str
    buyer_id: str
    seller_id: str
    vehicle_listing_id: str
    amount: float
    status: str
    stripe_payment_intent_id: str
    created_at: datetime
    updated_at: datetime