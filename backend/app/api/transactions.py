from fastapi import APIRouter, Depends, HTTPException
from app.schema.transaction import Transaction
from app.db.firestore import db
from app.api.auth import get_current_user
from app.services.payment import process_payment

router = APIRouter()

@router.post('/transactions')
async def create_transaction(transaction: Transaction, current_user: User = Depends(get_current_user)):
    # HUMAN ASSISTANCE NEEDED
    # This function needs more implementation details and error handling
    # Verify that the current user is the buyer
    if current_user.id != transaction.buyer_id:
        raise HTTPException(status_code=403, detail="You are not authorized to create this transaction")

    # Check if the vehicle listing is still available
    vehicle_ref = db.collection('vehicles').document(transaction.vehicle_id)
    vehicle = vehicle_ref.get()
    if not vehicle.exists or vehicle.to_dict().get('status') != 'available':
        raise HTTPException(status_code=400, detail="Vehicle is not available for purchase")

    # Process the payment using the payment service
    payment_result = await process_payment(transaction.amount, transaction.payment_method)
    if not payment_result.success:
        raise HTTPException(status_code=400, detail="Payment processing failed")

    # Create a new transaction document in the database
    transaction_ref = db.collection('transactions').document()
    transaction_data = transaction.dict()
    transaction_data['id'] = transaction_ref.id
    transaction_data['status'] = 'completed'
    transaction_ref.set(transaction_data)

    # Update the vehicle listing status to 'sold'
    vehicle_ref.update({'status': 'sold'})

    # Return the created transaction
    return Transaction(**transaction_data)

@router.get('/transactions/{transaction_id}')
async def get_transaction(transaction_id: str, current_user: User = Depends(get_current_user)):
    # Query the database for the transaction with the given ID
    transaction_ref = db.collection('transactions').document(transaction_id)
    transaction = transaction_ref.get()

    if not transaction.exists:
        raise HTTPException(status_code=404, detail="Transaction not found")

    transaction_data = transaction.to_dict()

    # Verify that the current user is either the buyer or seller
    if current_user.id not in [transaction_data['buyer_id'], transaction_data['seller_id']]:
        raise HTTPException(status_code=403, detail="You are not authorized to view this transaction")

    # If authorized, return the transaction details
    return Transaction(**transaction_data)