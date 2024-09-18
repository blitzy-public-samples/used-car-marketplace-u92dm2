import React, { useState, useEffect } from 'react';
import { useParams } from 'react-router-dom';
import TransactionDetails from '@/components/TransactionDetails';
import PaymentStatus from '@/components/PaymentStatus';
import { fetchTransactionDetails } from '@/services/api';
import { processPayment } from '@/services/payment';

// HUMAN ASSISTANCE NEEDED
// This component may need additional error handling and loading states.
// Consider adding more robust error handling for API calls and payment processing.
// Implement proper loading indicators for better user experience.

const TransactionPage: React.FC = () => {
  const { transactionId } = useParams<{ transactionId: string }>();
  const [transaction, setTransaction] = useState<any>(null);
  const [paymentStatus, setPaymentStatus] = useState<'pending' | 'success' | 'failed'>('pending');

  useEffect(() => {
    const loadTransactionDetails = async () => {
      try {
        const details = await fetchTransactionDetails(transactionId);
        setTransaction(details);
        setPaymentStatus(details.status);
      } catch (error) {
        console.error('Error fetching transaction details:', error);
        setPaymentStatus('failed');
      }
    };

    loadTransactionDetails();
  }, [transactionId]);

  const handlePayment = async () => {
    try {
      const result = await processPayment(transactionId);
      setPaymentStatus(result.status);
      // Update transaction details after payment processing
      const updatedDetails = await fetchTransactionDetails(transactionId);
      setTransaction(updatedDetails);
    } catch (error) {
      console.error('Error processing payment:', error);
      setPaymentStatus('failed');
    }
  };

  if (!transaction) {
    return <div>Loading...</div>;
  }

  return (
    <div className="transaction-page">
      <h1>Transaction Details</h1>
      <TransactionDetails transaction={transaction} />
      <PaymentStatus status={paymentStatus} />
      {paymentStatus === 'pending' && (
        <button onClick={handlePayment}>Process Payment</button>
      )}
      {paymentStatus === 'success' && (
        <div className="transaction-receipt">
          <h2>Transaction Receipt</h2>
          {/* Add receipt details here */}
        </div>
      )}
    </div>
  );
};

export default TransactionPage;