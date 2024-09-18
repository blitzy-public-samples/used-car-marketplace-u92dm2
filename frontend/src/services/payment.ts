import { loadStripe, Stripe, StripeElements } from '@stripe/stripe-js';
import { createApiInstance } from 'app/services/api';

const STRIPE_PUBLIC_KEY = process.env.REACT_APP_STRIPE_PUBLIC_KEY;

export const initializeStripe = async (): Promise<Stripe | null> => {
  try {
    const stripe = await loadStripe(STRIPE_PUBLIC_KEY);
    return stripe;
  } catch (error) {
    console.error('Failed to initialize Stripe:', error);
    return null;
  }
};

export const createPaymentIntent = async (amount: number, currency: string): Promise<{ clientSecret: string }> => {
  // HUMAN ASSISTANCE NEEDED
  // This function needs to be reviewed for production readiness
  const api = createApiInstance();
  try {
    const response = await api.post('/payments/create-intent', { amount, currency });
    return { clientSecret: response.data.clientSecret };
  } catch (error) {
    console.error('Failed to create payment intent:', error);
    throw error;
  }
};

// HUMAN ASSISTANCE NEEDED
// This function needs to be reviewed and possibly expanded for production readiness
export const processPayment = async (stripe: Stripe, elements: StripeElements, clientSecret: string): Promise<PaymentResult> => {
  try {
    const result = await stripe.confirmCardPayment(clientSecret, {
      payment_method: {
        card: elements.getElement('card'),
      },
    });

    if (result.error) {
      return { success: false, error: result.error.message };
    } else if (result.paymentIntent.status === 'succeeded') {
      return { success: true };
    } else {
      return { success: false, error: 'Payment failed' };
    }
  } catch (error) {
    console.error('Error processing payment:', error);
    return { success: false, error: 'An unexpected error occurred' };
  }
};

interface PaymentResult {
  success: boolean;
  error?: string;
}