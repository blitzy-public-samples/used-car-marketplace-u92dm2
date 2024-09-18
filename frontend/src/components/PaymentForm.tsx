import React, { useState } from 'react';
import { useStripe, useElements, CardElement } from '@stripe/react-stripe-js';
import { createPaymentIntent } from '@/services/payment';
import { formatCurrency } from '@/utils/formatting';

// HUMAN ASSISTANCE NEEDED
// The confidence level for this component is 0.6, which is below the threshold of 0.8.
// Please review and refine the following code to ensure it meets production standards.

interface PaymentFormProps {
  amount: number;
  onPaymentComplete: (result: { success: boolean; transactionId?: string; error?: string }) => void;
}

const PaymentForm: React.FC<PaymentFormProps> = ({ amount, onPaymentComplete }) => {
  const stripe = useStripe();
  const elements = useElements();
  const [isProcessing, setIsProcessing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleSubmit = async (event: React.FormEvent) => {
    event.preventDefault();

    if (!stripe || !elements) {
      return;
    }

    setIsProcessing(true);
    setError(null);

    try {
      const { clientSecret } = await createPaymentIntent(amount);

      const cardElement = elements.getElement(CardElement);

      if (!cardElement) {
        throw new Error('Card element not found');
      }

      const { error: stripeError, paymentIntent } = await stripe.confirmCardPayment(clientSecret, {
        payment_method: {
          card: cardElement,
        },
      });

      if (stripeError) {
        throw new Error(stripeError.message);
      }

      if (paymentIntent.status === 'succeeded') {
        onPaymentComplete({ success: true, transactionId: paymentIntent.id });
      } else {
        throw new Error('Payment failed');
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'An unknown error occurred');
      onPaymentComplete({ success: false, error: err instanceof Error ? err.message : 'An unknown error occurred' });
    } finally {
      setIsProcessing(false);
    }
  };

  return (
    <form onSubmit={handleSubmit}>
      <h2>Payment Details</h2>
      <p>Amount: {formatCurrency(amount)}</p>
      <CardElement />
      {error && <div className="error">{error}</div>}
      <button type="submit" disabled={!stripe || isProcessing}>
        {isProcessing ? 'Processing...' : 'Pay Now'}
      </button>
    </form>
  );
};

export default PaymentForm;