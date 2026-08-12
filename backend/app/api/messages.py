from fastapi import APIRouter, Depends, HTTPException
from typing import List
from app.schema.message import Message
# Required because both handlers annotate ``current_user: User``, and a
# parameter annotation is evaluated at function-definition time: without
# the name bound here the module raises NameError while being imported,
# which stops app/main.py - and therefore every API surface - from
# loading at all, so this router would contribute no route.
from app.schema.user import User
# ``send_message`` below stamps its document with
# ``firestore.SERVER_TIMESTAMP`` and the name was never bound, so the
# handler raised NameError on every call. That was invisible while this
# router could not be imported at all; now that it is registered for
# real, the endpoint is reachable and the missing import would be a
# guaranteed 500 on the first message sent.
from google.cloud import firestore
from app.db.firestore import db
from app.api.auth import get_current_user

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