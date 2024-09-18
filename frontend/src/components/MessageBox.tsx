import React, { useState, useEffect } from 'react';
import { fetchMessages, sendMessage } from '@/services/api';
import { useSelector } from 'react-redux';
import { selectCurrentUser } from '@/store/userSlice';

// HUMAN ASSISTANCE NEEDED
// The following component may need additional refinement for production readiness.
// Consider implementing error handling, loading states, and optimizing performance.

const MessageBox: React.FC<{ listingId: string; recipientId: string }> = ({ listingId, recipientId }) => {
  const [messages, setMessages] = useState<Array<{ id: string; sender: string; content: string; timestamp: Date; read: boolean }>>([]);
  const [newMessage, setNewMessage] = useState('');
  const currentUser = useSelector(selectCurrentUser);

  useEffect(() => {
    const loadMessages = async () => {
      try {
        const fetchedMessages = await fetchMessages(listingId);
        setMessages(fetchedMessages);
      } catch (error) {
        console.error('Failed to fetch messages:', error);
        // TODO: Implement proper error handling
      }
    };

    loadMessages();
    // TODO: Implement real-time updates (e.g., using WebSockets)
  }, [listingId]);

  const handleSendMessage = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!newMessage.trim()) return;

    try {
      const sentMessage = await sendMessage(listingId, recipientId, newMessage);
      setMessages([...messages, sentMessage]);
      setNewMessage('');
    } catch (error) {
      console.error('Failed to send message:', error);
      // TODO: Implement proper error handling
    }
  };

  const updateMessageReadStatus = (messageId: string) => {
    // TODO: Implement logic to update message read status
  };

  return (
    <div className="message-box">
      <div className="message-history">
        {messages.map((message) => (
          <div
            key={message.id}
            className={`message ${message.sender === currentUser.id ? 'sent' : 'received'}`}
            onClick={() => updateMessageReadStatus(message.id)}
          >
            <p>{message.content}</p>
            <small>{new Date(message.timestamp).toLocaleString()}</small>
            {message.sender !== currentUser.id && !message.read && <span className="unread-indicator" />}
          </div>
        ))}
      </div>
      <form onSubmit={handleSendMessage}>
        <input
          type="text"
          value={newMessage}
          onChange={(e) => setNewMessage(e.target.value)}
          placeholder="Type your message..."
        />
        <button type="submit">Send</button>
      </form>
    </div>
  );
};

export default MessageBox;