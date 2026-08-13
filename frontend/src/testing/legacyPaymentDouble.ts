/**
 * Stand-in for `@/services/payment`, which cannot be loaded in a test.
 *
 * WHY THE REAL MODULE CANNOT BE USED
 * -----------------------------------------------------------------------------
 * `src/services/payment.ts` imports `@stripe/stripe-js`, which is not a declared
 * dependency of this package, and `app/services/api`, which resolves to nothing.
 * Either one fails at TRANSFORM time, before any module code runs, so `vi.mock`
 * cannot help: the mock registry is consulted during execution and execution never
 * begins. `TransactionPage` imports `processPayment` from it at module scope, so
 * without this double that page cannot be loaded by any test at all.
 *
 * Both defects are pre-existing and out of scope for the rating feature — the
 * undeclared Stripe packages are listed as such in the plan, and the payment flow
 * is F004's, not F010's.
 *
 * WHY THESE FUNCTIONS REJECT RATHER THAN RETURN
 * -----------------------------------------------------------------------------
 * No rating test exercises payment: the rating surface is mounted in the
 * `completed` branch, and payment happens in the `pending` one. So being CALLED is
 * itself the failure, and a double that quietly returned a plausible receipt would
 * let a test drift into exercising a flow whose real implementation does not load —
 * passing on the strength of this file's behaviour rather than the product's.
 * Rejecting names what happened instead.
 *
 * This module is never imported by application code and is absent from the
 * production bundle. It lives under `src/` so `tsc --noEmit` type-checks it, and it
 * is not named `*.test.ts`, so Vitest does not collect it as a suite.
 */

/** The message both stand-ins reject with, so a stray call is self-explaining. */
const UNAVAILABLE =
  'The payment service is a test stand-in and must not be called: no rating flow ' +
  'reaches payment, and the real module cannot be loaded because it imports ' +
  'undeclared Stripe packages.';

/**
 * Stand-in for `processPayment`, imported by `TransactionPage`.
 *
 * @throws Always, because reaching it means a test wandered into the payment flow.
 */
export const processPayment = (): Promise<never> =>
  Promise.reject(new Error(UNAVAILABLE));

/**
 * Stand-in for `createPaymentIntent`, imported by `PaymentForm`.
 *
 * Present for completeness: `PaymentForm` is itself stood in for, so nothing
 * currently reaches this, and a missing export would fail at resolution rather
 * than saying why.
 *
 * @throws Always, for the same reason as above.
 */
export const createPaymentIntent = (): Promise<never> =>
  Promise.reject(new Error(UNAVAILABLE));

/**
 * Stand-in for `initializeStripe`, the module's third export.
 *
 * @throws Always, for the same reason as above.
 */
export const initializeStripe = (): Promise<never> =>
  Promise.reject(new Error(UNAVAILABLE));
