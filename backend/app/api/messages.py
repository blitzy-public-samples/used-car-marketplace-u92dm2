from fastapi import APIRouter, Depends, HTTPException
from typing import List
from app.schema.message import Message
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