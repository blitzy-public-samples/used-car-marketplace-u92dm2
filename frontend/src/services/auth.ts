import { createApiInstance } from 'app/services/api';
import { setItem, getItem, removeItem } from 'app/utils/storage';

interface User {
  id: string;
  email: string;
  // Add other user properties as needed
}

export async function login(email: string, password: string): Promise<User> {
  const api = createApiInstance();
  const response = await api.post('/auth/login', { email, password });
  
  if (response.data && response.data.token) {
    setItem('authToken', response.data.token);
    return response.data.user;
  } else {
    throw new Error('Login failed: Invalid response from server');
  }
}

export async function logout(): Promise<void> {
  const api = createApiInstance();
  try {
    await api.post('/auth/logout');
  } catch (error) {
    console.error('Logout request failed:', error);
  } finally {
    removeItem('authToken');
  }
}

export async function getCurrentUser(): Promise<User | null> {
  const api = createApiInstance();
  try {
    const response = await api.get('/auth/me');
    return response.data.user;
  } catch (error) {
    console.error('Failed to fetch current user:', error);
    return null;
  }
}