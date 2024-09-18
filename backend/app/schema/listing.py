from pydantic import BaseModel
from typing import List, Optional
from datetime import datetime

class VehicleListing(BaseModel):
    id: str
    seller_id: str
    make: str
    model: str
    year: int
    mileage: int
    price: float
    condition: str
    photos: List[str]
    maintenance_records: List[dict]
    status: str
    created_at: datetime
    updated_at: datetime