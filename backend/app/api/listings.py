from fastapi import APIRouter, Depends, HTTPException
from typing import List, Optional
import json
import logging
import uuid
from app.schema.listing import VehicleListing
from app.schema.user import User
from app.db.firestore import db
from app.api.auth import get_current_user
from app.services.ai_vision import analyze_vehicle_photo
from app.services.document_processing import process_maintenance_document

logger = logging.getLogger(__name__)

# Bounds applied to maintenance input before processing and persistence: the
# maximum record count parsed, the maximum aggregate decoded content bytes, and
# the maximum serialized listing size written to one Firestore document.
_MAX_MAINTENANCE_RECORDS = 20
_MAX_MAINTENANCE_CONTENT_BYTES = 900 * 1024
_MAX_SERIALIZED_LISTING_BYTES = 1_000_000

router = APIRouter()

# HUMAN ASSISTANCE NEEDED
# The confidence level for this function is below 0.8. Please review and adjust as necessary.
@router.post('/listings')
async def create_listing(listing: VehicleListing, current_user: User = Depends(get_current_user)):
    # Validate the current user's role (must be a seller)
    if current_user.role != 'seller':
        raise HTTPException(status_code=403, detail="Only sellers can create listings")

    # Analyze vehicle photos using AI vision service
    photo_analysis = await analyze_vehicle_photo(listing.photos)

    # Process maintenance documents per record. process_maintenance_document is
    # synchronous, so it is called without await, and failures are handled here.
    # The record count and aggregate content size are bounded, and each content
    # value must be non-empty bytes-like after string encoding.
    correlation_id = str(uuid.uuid4())
    records = listing.maintenance_records
    if len(records) > _MAX_MAINTENANCE_RECORDS:
        logger.warning(
            "too many maintenance records",
            extra={"correlation_id": correlation_id},
        )
        raise HTTPException(
            status_code=422,
            detail="Unable to process maintenance documents",
        )
    maintenance_data = []
    total_content_bytes = 0
    try:
        for record in records:
            content = record.get('content')
            if content is None:
                continue
            if isinstance(content, str):
                content = content.encode('utf-8')
            elif isinstance(content, bytearray):
                content = bytes(content)
            if not isinstance(content, bytes) or not content:
                raise ValueError("maintenance content must be non-empty bytes")
            total_content_bytes += len(content)
            if total_content_bytes > _MAX_MAINTENANCE_CONTENT_BYTES:
                raise ValueError("aggregate maintenance content is too large")
            doc_format = record.get('format', 'pdf')
            maintenance_data.append(
                process_maintenance_document(content, doc_format)
            )
    except Exception:
        logger.exception(
            "maintenance processing failed",
            extra={"correlation_id": correlation_id},
        )
        raise HTTPException(
            status_code=422,
            detail="Unable to process maintenance documents",
        )

    # Create a new listing document in the database
    listing_data = listing.dict()
    listing_data['seller_id'] = current_user.id
    listing_data['photo_analysis'] = photo_analysis
    listing_data['maintenance_data'] = maintenance_data

    # Reject a serialized listing that exceeds Firestore's per-document size
    # limit before writing it to the database.
    serialized_size = len(json.dumps(listing_data, default=str).encode())
    if serialized_size > _MAX_SERIALIZED_LISTING_BYTES:
        logger.warning(
            "listing exceeds serialized size budget",
            extra={"correlation_id": correlation_id},
        )
        raise HTTPException(
            status_code=422,
            detail="Listing payload is too large to store",
        )

    doc_ref = db.collection('listings').document()
    doc_ref.set(listing_data)

    # Return the created listing
    created_listing = VehicleListing(**listing_data)
    created_listing.id = doc_ref.id
    return created_listing

@router.get('/listings')
async def get_listings(
    make: Optional[str] = None,
    model: Optional[str] = None,
    year: Optional[int] = None,
    min_price: Optional[float] = None,
    max_price: Optional[float] = None
) -> List[VehicleListing]:
    # Query the database for vehicle listings
    query = db.collection('listings')

    # Apply filters based on the provided parameters
    if make:
        query = query.where('make', '==', make)
    if model:
        query = query.where('model', '==', model)
    if year:
        query = query.where('year', '==', year)
    if min_price:
        query = query.where('price', '>=', min_price)
    if max_price:
        query = query.where('price', '<=', max_price)

    # Execute the query and convert results to VehicleListing objects
    listings = [VehicleListing(**doc.to_dict()) for doc in query.stream()]
    return listings

@router.get('/listings/{listing_id}')
async def get_listing(listing_id: str) -> VehicleListing:
    # Query the database for the listing with the given ID
    doc_ref = db.collection('listings').document(listing_id)
    doc = doc_ref.get()

    # If found, return the listing details
    if doc.exists:
        listing_data = doc.to_dict()
        listing_data['id'] = doc.id
        return VehicleListing(**listing_data)
    
    # If not found, raise a 404 HTTPException
    raise HTTPException(status_code=404, detail="Listing not found")

@router.put('/listings/{listing_id}')
async def update_listing(listing_id: str, listing: VehicleListing, current_user: User = Depends(get_current_user)) -> VehicleListing:
    # Verify that the current user is the owner of the listing
    doc_ref = db.collection('listings').document(listing_id)
    doc = doc_ref.get()

    if not doc.exists:
        raise HTTPException(status_code=404, detail="Listing not found")

    if doc.to_dict()['seller_id'] != current_user.id:
        raise HTTPException(status_code=403, detail="You don't have permission to update this listing")

    # Update the listing document in the database
    listing_data = listing.dict(exclude_unset=True)
    doc_ref.update(listing_data)

    # Return the updated listing
    updated_doc = doc_ref.get()
    updated_listing = VehicleListing(**updated_doc.to_dict())
    updated_listing.id = listing_id
    return updated_listing

@router.delete('/listings/{listing_id}')
async def delete_listing(listing_id: str, current_user: User = Depends(get_current_user)) -> dict:
    # Verify that the current user is the owner of the listing or an admin
    doc_ref = db.collection('listings').document(listing_id)
    doc = doc_ref.get()

    if not doc.exists:
        raise HTTPException(status_code=404, detail="Listing not found")

    if doc.to_dict()['seller_id'] != current_user.id and current_user.role != 'admin':
        raise HTTPException(status_code=403, detail="You don't have permission to delete this listing")

    # Delete the listing document from the database
    doc_ref.delete()

    # Return a confirmation message
    return {"message": "Listing deleted successfully"}