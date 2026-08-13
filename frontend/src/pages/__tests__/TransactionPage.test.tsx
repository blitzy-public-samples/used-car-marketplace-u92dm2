/**
 * Tests for `../TransactionPage` — specifically, for the one thing this feature
 * added to it: the placement of the rating submission surface (SRS F010-1).
 *
 * WHAT IS UNDER TEST, AND WHAT IS EMPHATICALLY NOT
 * -----------------------------------------------------------------------------
 * The page's contribution to the rating feature is a PLACEMENT DECISION and
 * nothing else. Three properties are therefore asserted, and they are the whole
 * scope of this suite:
 *
 *   1. The form is mounted when the transaction has reached `completed`, and in no
 *      other state. A rating is meaningless before the sale is done, and offering
 *      one is worse than withholding it: the server would refuse the submission
 *      with a 409 after the user had composed it.
 *
 *   2. The transaction it is given is the one the page LOADED, not the one the URL
 *      happened to say. The identifiers usually agree; when they do not, the
 *      document is the authority, and a rating filed against the wrong transaction
 *      would be unrecoverable — reputation records are append-only.
 *
 *   3. The page itself makes NO rating request and computes NO authorization. Both
 *      gates — the rater must be verified, and both parties must be counterparties
 *      of the same transaction — are the server's, and the form asks the server
 *      through the eligibility endpoint. A page that pre-judged either would
 *      create a second, weaker copy of a decision already settled, free to drift.
 *
 * The payment flow, the receipt block and the three legacy children are NOT under
 * test here. They predate this feature, and two of them cannot even be loaded (see
 * `../../testing/legacyChildDouble`).
 *
 * HOW THE PAGE IS MADE LOADABLE AT ALL
 * -----------------------------------------------------------------------------
 * `TransactionPage` imports two components that do not exist and one service that
 * cannot be transformed, all through the `@/…` prefix that this project's
 * tsconfig never declared. Vite fails at transform time on an unresolvable import,
 * before any module code runs, so `vi.mock` cannot substitute for them — the mock
 * registry is consulted during execution and execution never begins. Resolution is
 * the only layer that works, which is what the Vitest-only `test.alias` block in
 * `../../../vite.config.ts` provides. That block is read by Vitest alone, so the
 * pre-existing defect stays reported by `tsc` and by `vite build` exactly as
 * before.
 *
 * What remains is mocked normally and deliberately narrowly:
 *
 *   `@/services/api`        replaced, because its real module has no
 *                           `fetchTransactionDetails` export at all — the page
 *                           imports a function that does not exist, another
 *                           pre-existing defect. The double is what lets this
 *                           suite state the transaction each case is about.
 *   `react-router-dom`      only `useParams`, over the real module, so the page is
 *                           handed the parameter it asks for. Worth noting that in
 *                           the application it is NOT: `../App.tsx` declares this
 *                           route's parameter as `:id` while the page reads
 *                           `transactionId`, so the real value is `undefined`.
 *                           That mismatch is pre-existing and out of scope, and
 *                           mocking the parameter is what keeps this suite about
 *                           rating placement rather than about routing.
 *   `../../services/rating` only the two functions that reach the network, so the
 *                           REAL `RatingSubmissionForm` renders — the point of a
 *                           page test is that the composition is genuine.
 *
 * `describe`, `it`, `expect`, `beforeEach` and `afterEach` are injected globals
 * (`test.globals: true`) typed by the ambient `@types/jest` declarations, which is
 * also what makes jest-dom's matchers type-check. `vi` is declared below.
 */

import { render, screen, waitFor } from '@testing-library/react';

/** `vi` as a type only; the identifier resolves to Vitest's injected global. */
declare const vi: typeof import('vitest')['vi'];

/**
 * The route parameters the page will read, mutable so each case can state its own.
 *
 * Declared before the factories below because they close over it and read it only
 * when the page renders, not when the module is replaced.
 */
let routeParams: Record<string, string | undefined> = {};

vi.mock('react-router-dom', async () => {
  const actual =
    await vi.importActual<typeof import('react-router-dom')>('react-router-dom');

  return { ...actual, useParams: () => routeParams };
});

/**
 * The replaced legacy service, created through `vi.hoisted` so the handle exists
 * before the hoisted `vi.mock` factory below runs.
 *
 * The alternative — importing the mocked module to get a handle — cannot be used:
 * `@/services/api` is not a specifier `tsconfig.json` declares, so `tsc` reports
 * it as an unresolved module and this file would ADD type errors to a baseline it
 * is required to leave untouched. Bypassing the import also states the situation
 * more honestly: the real module exports no `fetchTransactionDetails` at all, so
 * there was never a signature to infer from.
 */
const legacyApi = vi.hoisted(() => ({
  fetchTransactionDetails: vi.fn(),
}));

vi.mock('@/services/api', () => legacyApi);

vi.mock('../../services/rating', async () => {
  const actual =
    await vi.importActual<typeof import('../../services/rating')>(
      '../../services/rating',
    );

  return {
    ...actual,
    fetchRatingEligibility: vi.fn(),
    submitRating: vi.fn(),
    fetchUserRatings: vi.fn(),
    fetchUserReputation: vi.fn(),
    fetchTransactionRatings: vi.fn(),
    moderateRating: vi.fn(),
  };
});

import TransactionPage from '../TransactionPage';
import {
  fetchRatingEligibility,
  fetchTransactionRatings,
  fetchUserRatings,
  fetchUserReputation,
  moderateRating,
  submitRating,
} from '../../services/rating';
import type { EligibilityDecision } from '../../schema/rating';

/** The handle on the replaced legacy call, from the hoisted registry above. */
const mockedFetchTransaction = legacyApi.fetchTransactionDetails;

const mockedFetchEligibility = vi.mocked(fetchRatingEligibility);
const mockedSubmitRating = vi.mocked(submitRating);

/** The heading `RatingSubmissionForm` renders unconditionally, in every state. */
const FORM_HEADING = /rate the other party/i;

/** The identifier carried by the loaded transaction DOCUMENT. */
const DOCUMENT_ID = 'txn-from-document';

/** The identifier carried by the URL, deliberately different from the document's. */
const ROUTE_ID = 'txn-from-route';

/** The server's answer for a caller who may rate. */
const ELIGIBLE: EligibilityDecision = {
  eligible: true,
  reason: null,
  rateeId: 'user-seller-42',
  direction: 'buyer_to_seller',
  alreadyRated: false,
};

/**
 * The transaction shape this page reads.
 *
 * Only `id` and `status` matter to it; the rest of the document is handed to the
 * children that stand in for absent components and is never inspected here.
 */
const transaction = (
  overrides: { id?: string | undefined; status?: string } = {},
): Record<string, unknown> => ({
  id: DOCUMENT_ID,
  status: 'completed',
  ...overrides,
});

/** Every rating call the client exposes, so "no rating traffic" can be asserted. */
const allRatingCalls = () => [
  mockedFetchEligibility,
  mockedSubmitRating,
  vi.mocked(fetchUserRatings),
  vi.mocked(fetchUserReputation),
  vi.mocked(fetchTransactionRatings),
  vi.mocked(moderateRating),
];

describe('TransactionPage', () => {
  beforeEach(() => {
    routeParams = { transactionId: ROUTE_ID };

    mockedFetchTransaction.mockReset();
    allRatingCalls().forEach((call) => call.mockReset());

    // The page logs a failed transaction fetch, and one case below provokes it.
    vi.spyOn(console, 'error').mockImplementation(() => undefined);
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('shows nothing but a wait until the transaction is loaded, and asks about no rating', async () => {
    mockedFetchTransaction.mockImplementation(
      () => new Promise(() => undefined),
    );

    render(<TransactionPage />);

    expect(screen.getByText('Loading...')).toBeInTheDocument();

    // The rating surface is not merely disabled while the page loads, it is
    // absent: the page does not yet know whether this sale is complete, and the
    // form asking about eligibility before then would be a request made on a
    // guess.
    expect(screen.queryByRole('heading', { name: FORM_HEADING })).toBeNull();
    expect(mockedFetchEligibility).not.toHaveBeenCalled();
  });

  it('mounts the rating surface once the transaction is completed', async () => {
    mockedFetchTransaction.mockResolvedValue(transaction());
    mockedFetchEligibility.mockResolvedValue(ELIGIBLE);

    render(<TransactionPage />);

    // Found by HEADING, which is how a screen-reader user finds it: the form
    // renders its own `<section aria-labelledby>` titled by this `<h2>`.
    expect(
      await screen.findByRole('heading', { name: FORM_HEADING }),
    ).toBeInTheDocument();

    // The real form is mounted, not a placeholder: its controls are present.
    expect(
      screen.getByRole('button', { name: /submit rating/i }),
    ).toBeInTheDocument();
    expect(screen.getByRole('radiogroup')).toBeInTheDocument();
  });

  it('rates the transaction it loaded, not the one the URL named', async () => {
    // The two identifiers deliberately disagree. They normally would not; when
    // they do, the document is the authority — a rating filed against the wrong
    // transaction cannot be withdrawn, because reputation records are append-only.
    mockedFetchTransaction.mockResolvedValue(transaction({ id: DOCUMENT_ID }));
    mockedFetchEligibility.mockResolvedValue(ELIGIBLE);

    render(<TransactionPage />);
    await screen.findByRole('heading', { name: FORM_HEADING });

    await waitFor(() => {
      expect(mockedFetchEligibility).toHaveBeenCalledWith(DOCUMENT_ID);
    });
    expect(mockedFetchEligibility).not.toHaveBeenCalledWith(ROUTE_ID);
  });

  it('falls back to the identifier from the URL when the document carries none', async () => {
    // A document with no `id` is not a shape this backend returns, but the page
    // guards for it, and the guard has to resolve to something a rating can cite:
    // handing the form `undefined` would make it ask the server about the
    // transaction called "undefined".
    mockedFetchTransaction.mockResolvedValue(transaction({ id: undefined }));
    mockedFetchEligibility.mockResolvedValue(ELIGIBLE);

    render(<TransactionPage />);
    await screen.findByRole('heading', { name: FORM_HEADING });

    await waitFor(() => {
      expect(mockedFetchEligibility).toHaveBeenCalledWith(ROUTE_ID);
    });
  });

  /**
   * Every state that is not `completed`, including the two the payment flow uses
   * and one this backend does not emit at all.
   *
   * The table exists because the gate is an EQUALITY on one value: a condition
   * written as "not pending" rather than "is completed" passes for `pending` and
   * fails for everything else, which is exactly what a single-status test misses.
   */
  const UNRATEABLE_STATUSES = ['pending', 'processing', 'failed', 'cancelled', ''];

  UNRATEABLE_STATUSES.forEach((status) => {
    it(`offers no rating surface while the transaction is "${status}"`, async () => {
      mockedFetchTransaction.mockResolvedValue(transaction({ status }));

      render(<TransactionPage />);

      // Waited for rather than asserted immediately, so the page has genuinely
      // rendered its loaded state before the absence is checked.
      await waitFor(() => {
        expect(screen.queryByText('Loading...')).toBeNull();
      });

      expect(screen.queryByRole('heading', { name: FORM_HEADING })).toBeNull();
      expect(
        screen.queryByRole('button', { name: /submit rating/i }),
      ).toBeNull();

      // And nothing was asked of the rating API. An unmounted form makes no
      // request, so this also proves the page holds no eligibility state of its own.
      expect(mockedFetchEligibility).not.toHaveBeenCalled();
    });
  });

  it('makes no rating request of its own, in any state', async () => {
    mockedFetchTransaction.mockResolvedValue(transaction());
    mockedFetchEligibility.mockResolvedValue(ELIGIBLE);

    render(<TransactionPage />);
    await screen.findByRole('heading', { name: FORM_HEADING });

    await waitFor(() => {
      expect(mockedFetchEligibility).toHaveBeenCalledTimes(1);
    });

    // The ONE call is the form's own eligibility question, for the transaction the
    // page loaded. Every other rating call is untouched: the page reads no
    // reputation, lists no ratings, submits nothing and moderates nothing. A page
    // that fetched a rating to decide whether to show the form would be duplicating
    // the eligibility decision the server already owns.
    expect(mockedFetchEligibility).toHaveBeenCalledWith(DOCUMENT_ID);
    expect(mockedSubmitRating).not.toHaveBeenCalled();
    expect(vi.mocked(fetchUserRatings)).not.toHaveBeenCalled();
    expect(vi.mocked(fetchUserReputation)).not.toHaveBeenCalled();
    expect(vi.mocked(fetchTransactionRatings)).not.toHaveBeenCalled();
    expect(vi.mocked(moderateRating)).not.toHaveBeenCalled();
  });

  it('adds no heading of its own around the form, leaving one level and no duplicate', async () => {
    mockedFetchTransaction.mockResolvedValue(transaction());
    mockedFetchEligibility.mockResolvedValue(ELIGIBLE);

    render(<TransactionPage />);
    await screen.findByRole('heading', { name: FORM_HEADING });

    const headings = screen
      .getAllByRole('heading')
      .map((heading) => `${heading.tagName}:${heading.textContent ?? ''}`);

    // One `<h1>` for the page and `<h2>`s beneath it, with the form's own heading
    // among them — so the region is reachable by heading navigation with no level
    // skipped (WCAG 1.3.1) and no second heading whose only content is another
    // heading (WCAG 2.4.6).
    expect(headings[0]).toBe('H1:Transaction Details');
    expect(headings).toContain('H2:Rate the other party');
    expect(
      headings.filter((heading) => heading.endsWith('Rate the other party')),
    ).toHaveLength(1);
    expect(
      headings.filter((heading) => heading.startsWith('H1:')),
    ).toHaveLength(1);
  });

  it('keeps the rating surface absent when the transaction cannot be loaded', async () => {
    mockedFetchTransaction.mockRejectedValue(new Error('transaction unreachable'));

    render(<TransactionPage />);

    // The page has no error state of its own — it logs and stays on the wait,
    // which is pre-existing behaviour. What matters for the rating feature is that
    // an unknown transaction is never treated as a completed one.
    await waitFor(() => {
      expect(screen.getByText('Loading...')).toBeInTheDocument();
    });

    expect(screen.queryByRole('heading', { name: FORM_HEADING })).toBeNull();
    expect(mockedFetchEligibility).not.toHaveBeenCalled();
  });

  it('re-reads the transaction, and re-asks about eligibility, when the route changes', async () => {
    mockedFetchTransaction.mockResolvedValue(transaction());
    mockedFetchEligibility.mockResolvedValue(ELIGIBLE);

    const { rerender } = render(<TransactionPage />);
    await screen.findByRole('heading', { name: FORM_HEADING });

    await waitFor(() => {
      expect(mockedFetchEligibility).toHaveBeenCalledTimes(1);
    });

    // A different transaction document arrives for the new route.
    const secondId = 'txn-from-second-document';
    mockedFetchTransaction.mockResolvedValue(transaction({ id: secondId }));
    routeParams = { transactionId: 'txn-second-route' };

    rerender(<TransactionPage />);

    // The form is keyed on the transaction it is given, so a new one restarts its
    // eligibility question rather than showing the previous transaction's answer.
    await waitFor(() => {
      expect(mockedFetchEligibility).toHaveBeenCalledWith(secondId);
    });
  });
});
