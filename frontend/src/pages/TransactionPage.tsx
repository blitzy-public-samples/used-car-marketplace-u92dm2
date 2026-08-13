import React, { useState, useEffect } from 'react';
import { useParams } from 'react-router-dom';
import TransactionDetails from '@/components/TransactionDetails';
import PaymentStatus from '@/components/PaymentStatus';
// RELATIVE path deliberately, NOT the `@/…` prefix the two lines above use.
// `tsconfig.json` declares six aliases — `@components/*`, `@pages/*`,
// `@utils/*`, `@styles/*`, `@hooks/*`, `@context/*` — and no bare `@/*`, and
// `vite.config.ts` mirrors exactly those six. That prefix therefore resolves in
// neither the type-checker nor the bundler, which is why the lines above sit in
// this project's pre-existing TS2307 baseline. Do not "fix" this line to match
// them for consistency; repairing them is a separate, out-of-scope change.
import RatingSubmissionForm from '../components/RatingSubmissionForm';
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

  /**
   * The transaction id handed to the rating form, read from the LOADED
   * TRANSACTION rather than from the route.
   *
   * `transactionId` above is destructured as `useParams<{ transactionId: string
   * }>()`, but `src/App.tsx` routes this page at `/transaction/:id`. The param
   * is named `id`, so `transactionId` is `undefined` at run time — a
   * pre-existing defect of this page, not of the rating feature. Repairing it
   * would ripple through this page's own data loading (the two
   * `fetchTransactionDetails` calls, the `processPayment` call and the effect's
   * dependency array) and through the route table in `App.tsx`, which still
   * uses the React Router v5 API against an installed v6; both are out of scope
   * for this change.
   *
   * It is accounted for HERE, at the one call site this change introduces,
   * instead. By this line the transaction has loaded and carries its own `id`,
   * so the correct value is available locally and no other line needs touching.
   * The `?? transactionId` fallback costs nothing and keeps this correct if the
   * route param is ever renamed to match the destructuring above.
   *
   * The explicit `string` annotation is what keeps the value precise: it is
   * assignable because `transaction` is `any`, and it means the `transactionId:
   * string` prop below is satisfied by a typed local rather than by `any`
   * flowing into the component boundary — without introducing an `any` of this
   * change's own.
   */
  const ratingTransactionId: string = transaction.id ?? transactionId;

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
        <>
          <div className="transaction-receipt">
            <h2>Transaction Receipt</h2>
            {/* Add receipt details here */}
          </div>
          {/*
            F010-1 "User rating submission interface" — the write surface of the
            bidirectional peer reputation system, mounted where the rating
            actually becomes meaningful: the moment this sale is complete.

            THE BRANCH CONDITION IS PLACEMENT, NOT AUTHORIZATION.
            Two rules govern who may rate — the rater must be a verified user,
            and both parties must be counterparties of the same transaction —
            and NEITHER is decided here. Both are enforced server-side, and the
            form asks the server itself: it calls
            `GET /api/ratings/eligibility/{transactionId}` on mount and renders
            its controls disabled, quoting the server's own reason, when the
            answer is no. This page deliberately makes no rating request, reads
            no identity, and computes no "is this user allowed" condition —
            duplicating that decision here would create a second, weaker copy of
            something the server has already settled, and would be free to drift
            away from it. Reaching this branch means only that a rating is now
            worth offering; whether it is permitted is answered elsewhere.

            NO HEADING IS ADDED AROUND THE FORM, ON PURPOSE.
            `RatingSubmissionForm` already renders its own
            `<section aria-labelledby>` titled by its own `<h2>Rate the other
            party</h2>`, unconditionally and in every state, alongside its
            `role="status"` and `role="alert"` live regions. That heading sits at
            exactly the right level — a sibling of the `<h2>`s above it, under
            this page's single `<h1>` — so the region is already findable by
            heading navigation with no level skipped. A second `<h2>` here would
            introduce nothing but a duplicate whose only content is another
            heading, which describes no section of its own (WCAG 2.4.6) and
            implies a structural level that does not exist (WCAG 1.3.1).

            The wrapper is kept because it does real visual work and nothing
            else: the form's root card carries no top margin and the receipt
            block above carries no bottom margin, so `mt-6` — Tailwind's default
            spacing scale, this project's only token source — is what separates
            them. The component exposes no `className`, so a wrapper is the only
            place that spacing can live.

            `onSubmitted` is omitted: the form owns its own confirmation,
            including telling the user that a submitted rating stays unpublished
            until the counterparty submits theirs or the rating window closes.
            This page derives nothing from the rating that would need refreshing.
          */}
          <div className="mt-6">
            <RatingSubmissionForm transactionId={ratingTransactionId} />
          </div>
        </>
      )}
    </div>
  );
};

export default TransactionPage;