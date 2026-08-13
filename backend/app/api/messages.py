from fastapi import APIRouter, Depends, HTTPException
from typing import List
# The SERVER_TIMESTAMP sentinel that send_message writes lives on the Google
# Cloud package, not on this project's own app.db.firestore module -- that one
# supplies the `db` client imported below. It was used here without ever being
# imported, and because that use sits inside a handler body the NameError it
# raised was a request-time one, thrown when send_message ran, not when this
# module was imported. So it broke sending rather than booting.
from google.cloud import firestore
from app.schema.message import Message
# `User`, by contrast, is the import-time blocker this module contributed to
# the boot failure: it annotates the current_user parameter of both handlers,
# and Python evaluates annotations when the `def` executes, so the name had to
# resolve while this module was still being imported. Its absence therefore
# stopped the router being mounted at all. Mirrors app/api/listings.py.
from app.schema.user import User
from app.db.firestore import db
from app.api.auth import get_current_user

# Both routes below now register on a router main.py can import and mount.
# Reachable is a further step: an unmodified checkout still cannot serve them,
# because app/services/payment.py's `from stripe import Stripe` aborts the
# import chain (HCF-3) before any server starts, so they answer only once that
# unrelated blocker is repaired or stood in for. And even then two pre-existing
# defects inside their bodies keep them non-functional: send_message returns
# the SERVER_TIMESTAMP sentinel, which the response encoder cannot serialize,
# and the persisted document already carries an `id` key, so get_messages' two
# Message(**msg.to_dict(), id=msg.id) hydrations raise TypeError once any
# message exists. Both are recorded for authorization in
# documentation/ONBOARDING.md; repairing them means changing route logic that
# this import-only fix is not scoped to touch.
#
# Two consequences of that, worth knowing before this logic is opened up. The
# message body is now bounded by app/schema/message.py, which is what keeps an
# unbounded write out of Firestore, but `id` and `read` still arrive from the
# caller and are stored as sent: the model cannot discard them, because
# get_messages rebuilds the same model from stored documents. And send_message
# writes before it fails, so a client retrying a request it never saw the
# answer to duplicates the message -- which is why the fix for those two
# defects is also where idempotency belongs. Separately, the 404 at line 33
# tells any authenticated caller whether a given user id exists; a uniform
# not-found answer would need this branch changed too.

router = APIRouter()

@router.post('/messages')
async def send_message(message: Message, current_user: User = Depends(get_current_user)):
    # Validate the recipient user exists
    recipient = db.collection('users').document(message.recipient_id).get()
    if not recipient.exists:
        raise HTTPException(status_code=404, detail="Recipient user not found")

    # Create a new message document in the database
    message_data = message.dict()
    message_data['sender_id'] = current_user.id
    message_data['timestamp'] = firestore.SERVER_TIMESTAMP
    message_ref = db.collection('messages').add(message_data)

    # Return the sent message
    sent_message = Message(**message_data)
    sent_message.id = message_ref[1].id
    return sent_message

@router.get('/messages')
async def get_messages(current_user: User = Depends(get_current_user)) -> List[Message]:
    # Query the database for messages where the current user is either the sender or recipient
    messages = []
    sent_messages = db.collection('messages').where('sender_id', '==', current_user.id).stream()
    received_messages = db.collection('messages').where('recipient_id', '==', current_user.id).stream()

    for msg in sent_messages:
        messages.append(Message(**msg.to_dict(), id=msg.id))
    for msg in received_messages:
        messages.append(Message(**msg.to_dict(), id=msg.id))

    # Sort messages by timestamp
    messages.sort(key=lambda x: x.timestamp, reverse=True)

    return messages