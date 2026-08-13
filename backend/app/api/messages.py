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
# Working is a further step: two pre-existing defects inside their frozen
# bodies keep them non-functional -- send_message returns the
# SERVER_TIMESTAMP sentinel, which the response encoder cannot serialize, and
# the persisted document already carries an `id` key, so get_messages'
# Message(**msg.to_dict(), id=msg.id) hydrations raise TypeError once any
# message exists. Both are recorded for authorization in
# documentation/ONBOARDING.md; repairing them means changing route logic this
# import-only fix is not scoped to touch.

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