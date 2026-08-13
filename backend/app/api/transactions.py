from fastapi import APIRouter, Depends, HTTPException
import logging
import uuid
from app.schema.transaction import Transaction
# `User` annotates the current_user parameter of both handlers, and Python
# evaluates annotations when the `def` executes, so the absent import raised
# NameError while this module was still being imported -- no router could be
# mounted and the whole backend failed to boot. Mirrors app/api/listings.py.
from app.schema.user import User
from app.db.firestore import db
from app.api.auth import get_current_user
from app.services.payment import process_payment

logger = logging.getLogger(__name__)

# process_payment validates its currency argument against usd, eur and gbp,
# and Transaction declares no currency field to carry a caller's choice, so a
# default is named here rather than buried in the call as a bare literal.
# 'usd' is one of the three accepted values, so that validation can never
# reject the corrected call. Widening this needs a schema field: HCF-1.
_DEFAULT_CURRENCY = 'usd'

# Ceiling on any single field of provider- or caller-supplied text that reaches
# a log line below. Neither this module nor an operator decides how long a
# payment provider's own message is, and a log line is a poor place to learn.
_MAX_PROVIDER_DETAIL_CHARS = 200


def _log_field(value: object) -> str:
    """Render one value as a bounded, single-line, printable log field.

    What is worth logging at the payment boundary is largely text this
    application did not author: the provider's own status and reason, and the
    vehicle id the caller sent. Those are logged because without them a
    declined card, a provider outage and a malformed provider response are
    indistinguishable to whoever is on call. But a value carrying a newline
    would print as a second, forged log record, and an unbounded one would
    push a real record out of a reader's view, so every field is clipped and
    flattened before it reaches a line. The payment token is not among the
    fields logged at all -- see the call sites.
    """
    if value is None:
        return 'none'
    text = str(value)
    clipped = text[:_MAX_PROVIDER_DETAIL_CHARS]
    printable = ''.join(
        character if character.isprintable() else ' ' for character in clipped
    )
    collapsed = ' '.join(printable.split()) or 'empty'
    if len(text) > _MAX_PROVIDER_DETAIL_CHARS:
        return collapsed + '...'
    return collapsed


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

    # One correlation id per request, generated before the provider is called
    # so that every record this handler emits about one purchase carries the
    # same identifier. Assigning it in middleware, so that every line of a
    # request carries it rather than only the ones written here, is task NT-29.
    correlation_id = str(uuid.uuid4())

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
    try:
        payment_result = process_payment(
            transaction.stripe_payment_intent_id,
            transaction.amount,
            _DEFAULT_CURRENCY,
        )
    except Exception as provider_failure:
        # process_payment catches Exception itself and answers with a dict, so
        # anything escaping it is the integration breaking rather than a
        # refused card -- HCF-3 is exactly that today. It has to be visible as
        # an application record: the caller is told nothing, and the server's
        # own traceback names no vehicle, buyer or correlation id. The
        # exception is then re-raised untouched, so the response stays the
        # detail-free 500 it was and the traceback is still printed exactly
        # once, by the server, rather than twice by repeating it here.
        logger.error(
            'payment provider call failed: exception=%s detail=%s '
            'vehicle=%s buyer=%s correlation_id=%s',
            type(provider_failure).__name__,
            _log_field(provider_failure),
            _log_field(transaction.vehicle_listing_id),
            _log_field(current_user.id),
            correlation_id,
            extra={
                'correlation_id': correlation_id,
                'vehicle_listing_id': transaction.vehicle_listing_id,
                'buyer_id': current_user.id,
                'payment_exception': type(provider_failure).__name__,
            },
        )
        raise
    # process_payment returns a plain dict, never an object with .success, and
    # .get keeps a malformed result a payment failure rather than a KeyError.
    if not payment_result.get('success'):
        # The 400 below deliberately tells the caller nothing about why, and
        # for a while nothing told the operator either: the one boundary in
        # this application that moves money emitted no record at all, so a
        # declined card, a provider outage and a malformed provider response
        # were indistinguishable, uncountable and unalertable. The provider's
        # own status and reason are recorded here with the correlation id, the
        # vehicle and the buyer -- but never the payment token, which is a
        # credential and is not needed to diagnose any of this. Each field is
        # named in the message because the default formatter renders no `extra`
        # keys (NT-29), and attached through `extra` as well so that the
        # structured sink that task builds gets them unflattened.
        logger.error(
            'payment failed: provider_status=%s provider_error=%s '
            'vehicle=%s buyer=%s correlation_id=%s',
            _log_field(payment_result.get('status')),
            _log_field(payment_result.get('error')),
            _log_field(transaction.vehicle_listing_id),
            _log_field(current_user.id),
            correlation_id,
            extra={
                'correlation_id': correlation_id,
                'vehicle_listing_id': transaction.vehicle_listing_id,
                'buyer_id': current_user.id,
                'payment_status': payment_result.get('status'),
                'payment_error': payment_result.get('error'),
            },
        )
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
