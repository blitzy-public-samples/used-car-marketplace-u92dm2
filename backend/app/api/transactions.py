from fastapi import APIRouter, Depends, HTTPException
from app.schema.transaction import Transaction
# `User` annotates the current_user parameter of both handlers, and Python
# evaluates annotations when the `def` executes, so the absent import raised
# NameError while this module was still being imported -- no router could be
# mounted and the whole backend failed to boot. Mirrors app/api/listings.py.
from app.schema.user import User
from app.db.firestore import db
from app.api.auth import get_current_user
from app.services.payment import process_payment

# process_payment validates its currency argument against usd, eur and gbp,
# and Transaction declares no currency field to carry a caller's choice, so a
# default is named here rather than buried in the call as a bare literal.
# 'usd' is one of the three accepted values, so that validation can never
# reject the corrected call. Widening this needs a schema field: HCF-1.
_DEFAULT_CURRENCY = 'usd'

router = APIRouter()

@router.post('/transactions')
async def create_transaction(transaction: Transaction, current_user: User = Depends(get_current_user)):
    # HUMAN ASSISTANCE NEEDED
    # This function needs more implementation details and error handling
    # Verify that the current user is the buyer
    if current_user.id != transaction.buyer_id:
        raise HTTPException(status_code=403, detail="You are not authorized to create this transaction")

    # Check if the vehicle listing is still available
    # Transaction declares no vehicle_id; the listing reference field is
    # vehicle_listing_id, so the old read raised AttributeError on every
    # create request that got past the buyer check above -- which is also
    # why it fired before either payment defect below could be reached.
    vehicle_ref = db.collection('vehicles').document(
        transaction.vehicle_listing_id
    )
    vehicle = vehicle_ref.get()
    if not vehicle.exists or vehicle.to_dict().get('status') != 'available':
        raise HTTPException(status_code=400, detail="Vehicle is not available for purchase")

    # Process the payment using the payment service
    # process_payment is synchronous and takes (token, amount, currency). The
    # old `await process_payment(transaction.amount,
    # transaction.payment_method)` was wrong in three ways that surface one at
    # a time, each hidden behind the one before it. Evaluating the arguments
    # comes first, so transaction.payment_method raises AttributeError before
    # the call is made at all -- Transaction declares no such field. Give it
    # one, and the two-argument call raises TypeError for the missing
    # currency. Give it that, and the await raises TypeError on the plain
    # dict returned. stripe_payment_intent_id is the only
    # Stripe-credential-shaped field the schema declares, so it is the token.
    payment_result = process_payment(
        transaction.stripe_payment_intent_id,
        transaction.amount,
        _DEFAULT_CURRENCY,
    )
    # process_payment returns a plain dict, never an object with .success, and
    # .get keeps a malformed result a payment failure rather than a KeyError.
    if not payment_result.get('success'):
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
