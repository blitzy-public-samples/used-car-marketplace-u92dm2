import React, { useState, useEffect } from 'react';
import { useParams } from 'react-router-dom';
import TransactionDetails from '@/components/TransactionDetails';
import PaymentStatus from '@/components/PaymentStatus';
import { fetchTransactionDetails } from '@/services/api';
import { processPayment } from '@/services/payment';

// HUMAN ASSISTANCE NEEDED
// This component may need additional error handling and loading states.
// Consider adding proper TypeScript interfaces for the transaction and payment data.

const TransactionPage: React.FC = () => {
  const { transactionId } = useParams<{ transactionId: string }>();
  const [transaction, setTransaction] = useState<any>(null);
  const [paymentStatus, setPaymentStatus] = useState<string | null>(null);

  useEffect(() => {
    const loadTransactionDetails = async () => {
      try {
        const details = await fetchTransactionDetails(transactionId);
        setTransaction(details);
      } catch (error) {
        console.error('Failed to fetch transaction details:', error);
        // TODO: Handle error state
      }
    };

    loadTransactionDetails();
  }, [transactionId]);

  const handlePayment = async () => {
    if (transaction && transaction.status === 'pending') {
      try {
        const result = await processPayment(transactionId);
        setPaymentStatus(result.status);
        // Refresh transaction details after payment
        const updatedDetails = await fetchTransactionDetails(transactionId);
        setTransaction(updatedDetails);
      } catch (error) {
        console.error('Payment processing failed:', error);
        setPaymentStatus('failed');
      }
    }
  };

  if (!transaction) {
    return <div>Loading...</div>;
  }

  return (
    <div className="transaction-page">
      <h1>Transaction Details</h1>
      <TransactionDetails transaction={transaction} />
      <PaymentStatus status={transaction.status} />
      {transaction.status === 'pending' && (
        <button onClick={handlePayment}>Process Payment</button>
      )}
      {paymentStatus && (
        <div className="payment-result">
          <h2>Payment Result</h2>
          <p>Status: {paymentStatus}</p>
        </div>
      )}
      {transaction.status === 'completed' && (
        <div className="transaction-receipt">
          <h2>Transaction Receipt</h2>
          {/* Add receipt details here */}
        </div>
      )}
    </div>
  );
};

export default TransactionPage;