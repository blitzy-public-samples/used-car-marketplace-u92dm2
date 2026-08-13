/**
 * Tests for `../RatingSubmissionForm`, the rating submission surface of the
 * bidirectional peer reputation system — SRS F010-1 "User rating submission
 * interface" and F010-2 "Written review functionality"
 * (`documentation/Software Requirements Specifications (SRS).md`).
 *
 * WHAT THIS SUITE COVERS, AND WHY IT IS EXACTLY THREE CASES
 * -----------------------------------------------------------------------------
 * Three behaviours are in scope, and they were chosen because each is a place
 * where the form can silently do the wrong thing while still LOOKING correct:
 *
 *   1. INELIGIBLE — the server says the caller may not rate. The form must state
 *      the server's own reason in the live region assistive technology
 *      announces, and every control must be inert. A form that rendered the
 *      reason and left the button pressable would let a user compose a rating
 *      and only then be refused with a 403, which is the accessibility failure
 *      the eligibility endpoint exists to prevent.
 *
 *   2. OVER-LENGTH REVIEW — the local bound must stop the request, not merely
 *      colour the field. The proof is that no request is made at all.
 *
 *   3. OUT-OF-RANGE SCORE — proven through the SERVER, because the score control
 *      structurally cannot emit a value outside the scale (see below). The
 *      server's `detail` must reach the screen verbatim, and the submitted body
 *      must carry no identity claim.
 *
 * There is deliberately no success-path case and no moderation case here: this
 * file's mandate is the refusal paths, and a fourth case would be scope its
 * siblings and the backend suites already cover.
 *
 * WHY CASE 3 GOES THROUGH THE SERVER RATHER THAN FAKING A CLIENT-SIDE PATH
 * -----------------------------------------------------------------------------
 * `../StarRatingInput` renders exactly one option per score in
 * `RATING_MIN..RATING_MAX` and calls back with that option's own value, so there
 * is no interaction — click, keyboard, or otherwise — that produces a score
 * outside the scale. Adding a hidden numeric input to manufacture one would test
 * a control the product does not ship.
 *
 * So the out-of-range refusal is exercised where it genuinely lives: the server.
 * The backend bounds the field with `conint(strict=True, ge=settings.RATING_MIN,
 * le=settings.RATING_MAX)` (`backend/app/schema/rating.py`), so a score outside
 * the scale is refused by Pydantic with a 422 before any handler runs — which is
 * the enterprise practice "server-side validation is authoritative; client-side
 * validation is a convenience" as an executable assertion rather than a comment.
 *
 * WHAT IS MOCKED, AND WHAT IS EMPHATICALLY NOT
 * -----------------------------------------------------------------------------
 * Exactly one module is replaced, and within it only the two functions that
 * reach the network. Everything else in the graph is the production code:
 *
 *   MOCKED    `fetchRatingEligibility`, `submitRating` — the two HTTP calls.
 *   REAL      `readServerDetail`, `describeRequestFailure`, `RatingContractError`
 *             and `CONTRACT_ERROR_MESSAGE` from the same module, kept via
 *             `vi.importActual`. The component imports all six symbols, so a
 *             two-key factory would replace the module with one missing four of
 *             its exports and the form would throw on its first refusal. Keeping
 *             them real is also what makes case 3 honest: the rejection fixture
 *             has to satisfy the REAL `readServerDetail`, which gates on
 *             `axios.isAxiosError`, so a fixture that only looked plausible
 *             would surface the generic fallback and fail the assertion.
 *   REAL      `../../utils/validation` — the genuine Zod bound and the genuine
 *             DOMPurify sanitisation run here. Stubbing them would make case 2 a
 *             test of a stub.
 *   REAL      `../StarRatingInput` — the score is chosen by clicking the option
 *             a user would click, named the way a screen reader announces it.
 *   REAL      `../../schema/rating` — the shared bounds and the decision schema.
 *
 * No `../../utils/formatting` mock appears here, unlike `ReputationBadge.test.tsx`:
 * this component does not import that module, so nothing in this suite's graph
 * needs it.
 *
 * RENDERED WITH NO `<Provider>` AND NO ROUTER, ON PURPOSE
 * -----------------------------------------------------------------------------
 * The component reads no Redux state and no route parameter — its transaction
 * comes from a prop and its counterparty from the server — so a store wrapper
 * would add nothing except `../../store/index`, whose reducers are imported by
 * name against default-only exports, and `../../store/userSlice`, which never
 * exports the current-user selector three other components import from it.
 * Leaving both out of the module graph is a correctness property of this suite,
 * not a shortcut.
 *
 * `describe`, `it`, `expect` and `beforeEach` are injected globals
 * (`test.globals: true` in `../../../vite.config.ts`) typed by the ambient
 * `@types/jest` declarations TypeScript includes automatically, which is also
 * what makes jest-dom's matchers type-check — jest-dom v5 augments the matcher
 * interface those globals expose. Importing them from `vitest` instead would drop
 * that augmentation and turn every `toBeDisabled()` here into a type error, and a
 * triple-slash reference to `vitest/globals` would redeclare them against
 * `@types/jest` and produce TS2451. Neither is used, and the directive is
 * described rather than written out so it cannot be reintroduced by copying this
 * comment. `vi` is the one exception and is declared below.
 */

import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import RatingSubmissionForm from '../RatingSubmissionForm';
import { fetchRatingEligibility, submitRating } from '../../services/rating';
import {
  EligibilityDecisionSchema,
  RATING_MAX,
  RATING_MIN,
  REVIEW_MAX_LENGTH,
} from '../../schema/rating';
import type { EligibilityDecision } from '../../schema/rating';

/**
 * `vi` as a TYPE ONLY, resolved to Vitest's injected global at runtime.
 *
 * `@types/jest` supplies `describe`/`it`/`expect`/`beforeEach` but knows nothing
 * about `vi`, and `vitest/globals` cannot be added to this project's `types`
 * without colliding with those same declarations. `typeof import(...)` is a type
 * position, so this line emits no runtime import: the identifier resolves to the
 * global Vitest injects, and Vitest still finds `vi.mock(...)` syntactically and
 * hoists it above the imports below.
 */
declare const vi: typeof import('vitest')['vi'];

vi.mock('../../services/rating', async () => {
  const actual =
    await vi.importActual<typeof import('../../services/rating')>(
      '../../services/rating',
    );

  return {
    ...actual,
    fetchRatingEligibility: vi.fn(),
    submitRating: vi.fn(),
  };
});

/**
 * Typed handles on the two replaced functions.
 *
 * `vi.mocked` rather than a cast: the mock keeps the real signature, so
 * `mockResolvedValue` is checked against `Promise<EligibilityDecision>` and a
 * malformed fixture becomes a compile error instead of a confusing runtime one.
 */
const mockedFetchEligibility = vi.mocked(fetchRatingEligibility);
const mockedSubmitRating = vi.mocked(submitRating);

/** The transaction under test, passed as the component's only required prop. */
const TRANSACTION_ID = 'txn-1';

/**
 * The score the tests select: the middle of the scale, computed from the shared
 * bounds so it stays a genuine interior choice at any scale and this file
 * contains no hardcoded `3`, `1` or `5`.
 */
const SELECTED_SCORE = Math.floor((RATING_MIN + RATING_MAX) / 2);

/**
 * The accessible name `../StarRatingInput` gives each option — the same
 * derivation the component performs, so the two cannot disagree about how an
 * option is announced. Selecting a score by this name means the test picks the
 * star the way a screen-reader user does.
 */
const optionName = (score: number): string =>
  `Rate ${score} out of ${RATING_MAX}`;

/** The submit control, matched by its accessible name rather than a class. */
const SUBMIT_BUTTON_NAME = /submit rating/i;

/** The review field, matched by the accessible name its `<label>` provides. */
const REVIEW_FIELD_NAME = /review/i;

/**
 * A review that exceeds the shared bound by exactly one character.
 *
 * Derived from `REVIEW_MAX_LENGTH` rather than written as a literal 2001, so it
 * follows the constant if the bound ever moves. One character over is the
 * interesting case: an off-by-one in the comparison passes every test built on a
 * grossly over-long value and fails this one.
 */
const OVER_LONG_REVIEW = 'a'.repeat(REVIEW_MAX_LENGTH + 1);

/**
 * A review the production path leaves untouched: plain text, no angle brackets
 * for DOMPurify to strip, no surrounding whitespace or repeated blank lines for
 * the normaliser to collapse. That invariance is what lets case 3 assert the
 * submitted body by exact equality — the string that goes into the field is the
 * string that must reach the request.
 */
const ACCEPTED_REVIEW = 'The paperwork was in order and the handover was punctual.';

/**
 * The fragment of the component's over-length message that must be on screen.
 *
 * A fragment interpolated from the bound rather than the whole sentence: the
 * assertion then holds if the surrounding copy is reworded, and still fails if
 * the message stops naming the limit or stops appearing at all.
 */
const OVER_LIMIT_FRAGMENT = `over the ${REVIEW_MAX_LENGTH}-character limit`;

/**
 * The character counter in its over-limit state, e.g. `2001 / 2000 characters`.
 *
 * A regular expression so incidental whitespace around the separator does not
 * break it, with both numbers derived from the bound.
 */
const COUNTER_PATTERN = new RegExp(
  `${REVIEW_MAX_LENGTH + 1}\\s*/\\s*${REVIEW_MAX_LENGTH} characters`,
);

/**
 * The status a score outside the scale comes back with.
 *
 * `backend/app/schema/rating.py` bounds `score` with `conint(strict=True,
 * ge=settings.RATING_MIN, le=settings.RATING_MAX)`, so such a request is refused
 * by Pydantic before any handler runs, and FastAPI reports that as a 422.
 */
const OUT_OF_RANGE_STATUS = 422;

/**
 * The server's own words for that refusal, which is what must reach the screen.
 *
 * This is Pydantic v1's message for an exceeded `le` bound, with the ceiling
 * interpolated from the shared constant rather than written as a literal, so the
 * fixture follows the scale instead of pinning a number to it.
 *
 * On the wire a Pydantic-raised 422 carries `detail` as an ARRAY of issue objects
 * — `[{loc, msg, type}]` — because the backend registers no custom
 * `RequestValidationError` handler, and `readServerDetail` joins those `msg`
 * values into one string. A router-raised refusal (403, 404, 409, and the
 * self-rating 422) carries `detail` as a string already. The fixture below uses
 * the string form: it is the shape `readServerDetail` returns in both cases and
 * the shape the router itself sends, so it exercises the branch every refusal
 * this form can receive passes through.
 */
const OUT_OF_RANGE_DETAIL = `ensure this value is less than or equal to ${RATING_MAX}`;

/**
 * The shape a refused request arrives in.
 *
 * `../../services/rating` rejects with the ORIGINAL axios error rather than
 * wrapping it, so the component sees exactly this: the `isAxiosError` marker,
 * which is the property `axios.isAxiosError` itself tests and therefore the one
 * the REAL `readServerDetail` gates on, plus the server's response.
 *
 * Declared locally rather than imported from axios: a hand-built `AxiosError`
 * would need a config, a request and a full response to satisfy its type, none
 * of which this component reads. Naming the four fields that matter documents the
 * contract precisely, and keeps `any` out of the file.
 */
interface ApiErrorLike {
  isAxiosError: true;
  response: {
    data: { detail: string };
    status: number;
  };
}

/** Builds a refused-request rejection value for `submitRating`. */
const refusal = (status: number, detail: string): ApiErrorLike => ({
  isAxiosError: true,
  response: { data: { detail }, status },
});

/**
 * The server's decision for a caller who may rate: the counterparty is named,
 * the direction is set, and there is nothing to explain.
 *
 * These are not arbitrary values — `EligibilityDecisionSchema` refuses an
 * eligible decision that omits `rateeId` or `direction`, or that claims
 * `alreadyRated`, so this is the only shape an eligible decision can take.
 */
const ELIGIBLE_DECISION: EligibilityDecision = {
  eligible: true,
  reason: null,
  rateeId: 'user-seller-42',
  direction: 'buyer_to_seller',
  alreadyRated: false,
};

/**
 * Builds an eligibility decision, VALIDATED THROUGH THE PRODUCTION SCHEMA.
 *
 * The `parse` is the point: `../../services/rating` decodes every decision with
 * this same schema, so a fixture that would not survive decoding cannot be used
 * here. That closes the gap where a test passes against a decision the server
 * could never send — an ineligible decision with no reason, or an eligible one
 * that also claims the caller already rated — and flatters the component with
 * behaviour nobody will ever exercise.
 *
 * @param overrides Fields to change on the eligible baseline.
 */
const decision = (
  overrides: Partial<EligibilityDecision> = {},
): EligibilityDecision =>
  EligibilityDecisionSchema.parse({ ...ELIGIBLE_DECISION, ...overrides });

/**
 * Waits until the eligibility request has settled and the controls have come
 * alive.
 *
 * The form fetches on mount and renders every control disabled until the answer
 * arrives, so an assertion made straight after `render` would inspect the loading
 * state. Waiting for an option to become ENABLED — rather than for a message to
 * disappear — is what proves the eligible decision was applied, and it is the
 * same condition a user waits for.
 */
const awaitEnabledControls = async (): Promise<void> => {
  await waitFor(() => {
    expect(
      screen.getByRole('radio', { name: optionName(SELECTED_SCORE) }),
    ).toBeEnabled();
  });
};

/**
 * The `<form>` the controls live in.
 *
 * Needed because the over-length case has to submit a form whose submit BUTTON is
 * correctly disabled, so there is nothing to click: dispatching the submit event
 * directly is the only way to reach the handler's own gate and prove it refuses
 * too. Reached from a control found by role, so the query stays anchored to the
 * accessibility tree, and narrowed rather than asserted so no non-null assertion
 * is needed.
 *
 * The form is deliberately not queried by `role="form"`, which exists only for a
 * form with an accessible name; this one is named by the section around it.
 */
const formOf = (element: HTMLElement): HTMLFormElement => {
  const form = element.closest('form');

  if (form === null) {
    throw new Error(
      'Expected the rating controls to be inside a <form>, and they were not.',
    );
  }

  return form;
};

describe('RatingSubmissionForm', () => {
  /**
   * Both handles are RESET rather than merely cleared, so neither the recorded
   * calls nor the resolved or rejected value set by one test can reach another.
   * Every test below states its own server behaviour explicitly.
   */
  beforeEach(() => {
    mockedFetchEligibility.mockReset();
    mockedSubmitRating.mockReset();
  });

  it('states the server reason in the status region and leaves every control inert when the caller is not eligible', async () => {
    // The server's own copy for this refusal, verbatim: `DuplicateRating` in
    // `backend/app/services/rating.py` carries this exact message, and the
    // service reuses it for both the eligibility `reason` and the 409 `detail`.
    const reason = 'You have already rated this transaction';

    mockedFetchEligibility.mockResolvedValue(
      decision({ eligible: false, reason, alreadyRated: true }),
    );

    render(<RatingSubmissionForm transactionId={TRANSACTION_ID} />);

    // Asserted THROUGH `role="status"`, not merely somewhere on the page: a
    // reason rendered outside the live region is never announced, which leaves a
    // screen-reader user with a disabled control and no explanation — the WCAG
    // 2.1 AA failure this form exists to avoid (SRS: "WCAG 2.1 Level AA
    // compliance", "Support for screen readers and keyboard navigation").
    await waitFor(() => {
      expect(screen.getByRole('status')).toHaveTextContent(reason);
    });

    // The form asked about THIS transaction. Without this the assertions above
    // would also pass for a form that asked about the wrong one.
    expect(mockedFetchEligibility).toHaveBeenCalledWith(TRANSACTION_ID);

    // Every control is inert, so there is no way to compose a rating that the
    // server has already said it will refuse. All three are found by role, so
    // each query doubles as an accessibility assertion.
    expect(
      screen.getByRole('button', { name: SUBMIT_BUTTON_NAME }),
    ).toBeDisabled();
    expect(
      screen.getByRole('textbox', { name: REVIEW_FIELD_NAME }),
    ).toBeDisabled();
    expect(
      screen.getByRole('radio', { name: optionName(SELECTED_SCORE) }),
    ).toBeDisabled();

    // Nothing was sent. An ineligible caller must not reach the write endpoint
    // at all, rather than being refused there.
    expect(mockedSubmitRating).not.toHaveBeenCalled();
  });

  it('refuses a review longer than the shared limit and makes no request', async () => {
    const user = userEvent.setup();

    mockedFetchEligibility.mockResolvedValue(decision());

    render(<RatingSubmissionForm transactionId={TRANSACTION_ID} />);
    await awaitEnabledControls();

    // A valid score, chosen through the real star control, so the only thing
    // wrong with this submission is the review's length.
    await user.click(
      screen.getByRole('radio', { name: optionName(SELECTED_SCORE) }),
    );

    const reviewField = screen.getByRole('textbox', {
      name: REVIEW_FIELD_NAME,
    });

    // One change event carrying the whole value, rather than 2001 keystrokes:
    // typing character by character is prohibitively slow and tests the
    // browser's input handling rather than this form's bound. The real
    // `prepareReviewText` — DOMPurify sanitisation, Unicode normalisation and a
    // code-point count — runs on it, so the refusal below comes from the
    // production measurement and not from a stub.
    fireEvent.change(reviewField, { target: { value: OVER_LONG_REVIEW } });

    // The user is TOLD, in the announced region, and told again by the counter,
    // and the field itself reports the state to assistive technology. Three
    // independent channels, none of which is colour.
    expect(screen.getByRole('status')).toHaveTextContent(OVER_LIMIT_FRAGMENT);
    expect(screen.getByText(COUNTER_PATTERN)).toBeInTheDocument();
    expect(reviewField).toHaveAttribute('aria-invalid', 'true');

    // The visible gate: there is nothing to press while the review is too long.
    expect(
      screen.getByRole('button', { name: SUBMIT_BUTTON_NAME }),
    ).toBeDisabled();

    // The gate behind it. Submitting the form directly bypasses the disabled
    // button exactly as a stray Enter key or a re-enabled control would, and the
    // handler must still refuse. A form that relied on the disabled attribute
    // alone would send the request here.
    fireEvent.submit(formOf(reviewField));

    // Yields to the microtask queue before the final assertion, so a request
    // that had been started and awaited would already have been recorded.
    await waitFor(() => {
      expect(screen.getByRole('status')).toHaveTextContent(OVER_LIMIT_FRAGMENT);
    });

    // The point of the case: the client bound stops the REQUEST, rather than
    // merely styling the field and letting the server refuse it.
    expect(mockedSubmitRating).not.toHaveBeenCalled();
  });

  it('renders the server refusal of an out-of-range score verbatim and submits no identity claim', async () => {
    const user = userEvent.setup();

    mockedFetchEligibility.mockResolvedValue(decision());
    mockedSubmitRating.mockRejectedValue(
      refusal(OUT_OF_RANGE_STATUS, OUT_OF_RANGE_DETAIL),
    );

    render(<RatingSubmissionForm transactionId={TRANSACTION_ID} />);
    await awaitEnabledControls();

    await user.click(
      screen.getByRole('radio', { name: optionName(SELECTED_SCORE) }),
    );

    fireEvent.change(
      screen.getByRole('textbox', { name: REVIEW_FIELD_NAME }),
      { target: { value: ACCEPTED_REVIEW } },
    );

    const submitButton = screen.getByRole('button', {
      name: SUBMIT_BUTTON_NAME,
    });

    await user.click(submitButton);

    // VERBATIM: the same string in the fixture and in the assertion. The server
    // is the only party that can explain a specific refusal, so a form that
    // paraphrased it — or fell back to its own generic copy — would tell the user
    // something other than what happened.
    expect(await screen.findByText(OUT_OF_RANGE_DETAIL)).toBeInTheDocument();

    // Exactly one attempt: a refusal must not be retried behind the user's back.
    expect(mockedSubmitRating).toHaveBeenCalledTimes(1);

    // EXACT-SHAPE equality rather than `objectContaining`, deliberately. The
    // body carries the transaction, the score and the review and NOTHING else:
    // `raterId`, `rateeId` and `direction` are derived server-side from the
    // authenticated caller and the cited transaction, which is what makes
    // direction spoofing and self-rating structurally impossible rather than
    // merely validated against. An extra client-supplied identity field has to
    // fail this assertion, and only exact equality does that. The eligibility
    // decision above hands the component `rateeId` and `direction`; this proves
    // it treats them as display context and sends neither back.
    expect(mockedSubmitRating).toHaveBeenCalledWith({
      transactionId: TRANSACTION_ID,
      score: SELECTED_SCORE,
      review: ACCEPTED_REVIEW,
    });

    // The refusal is recoverable: the control comes back, so a user who fixes
    // what the server objected to can try again. A form left permanently
    // disabled after one 422 would strand them.
    await waitFor(() => {
      expect(submitButton).toBeEnabled();
    });
  });
});

