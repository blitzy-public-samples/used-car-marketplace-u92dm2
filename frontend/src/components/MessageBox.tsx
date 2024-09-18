import React, { useState, useEffect } from 'react';
import { fetchMessages, sendMessage } from '@/services/api';
import { useSelector } from 'react-redux';
import { selectCurrentUser } from '@/store/userSlice';

// HUMAN ASSISTANCE NEEDED
// This component may need additional styling and error handling for production readiness.
// Consider implementing real-time updates using WebSockets or polling.

const MessageBox: React.FC<{ listingId: string; recipientId: string }> = ({ listingId, recipientId }) => {
  const [messages, setMessages] = useState<Array<{ id: string; sender: string; content: string; timestamp: string; read: boolean }>>([]);
  const [newMessage, setNewMessage] = useState('');
  const currentUser = useSelector(selectCurrentUser);

  useEffect(() => {
    const loadMessages = async () => {
      try {
        const fetchedMessages = await fetchMessages(listingId);
        setMessages(fetchedMessages);
      } catch (error) {
        console.error('Failed to fetch messages:', error);
        // TODO: Add proper error handling
      }
    };

    loadMessages();
    // TODO: Implement real-time updates or polling
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
      // TODO: Add proper error handling
    }
  };

  return (
    <div className="message-box">
      <div className="message-history">
        {messages.map((message) => (
          <div key={message.id} className={`message ${message.sender === currentUser.id ? 'sent' : 'received'}`}>
            <p>{message.content}</p>
            <small>{new Date(message.timestamp).toLocaleString()}</small>
            {message.sender !== currentUser.id && !message.read && <span className="unread-indicator">Unread</span>}
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