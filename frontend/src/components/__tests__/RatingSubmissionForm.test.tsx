/**
 * Tests for `../RatingSubmissionForm`, the rating submission surface of the
 * bidirectional peer reputation system — SRS F010-1 "User rating submission
 * interface" and F010-2 "Written review functionality"
 * (`documentation/Software Requirements Specifications (SRS).md`).
 *
 * WHAT THIS SUITE COVERS
 * -----------------------------------------------------------------------------
 * The component is a state machine with five observable phases — checking,
 * eligible, ineligible, in flight, submitted — plus a recovery path out of each
 * failure. Every phase can go wrong while still LOOKING correct, which is why the
 * suite is organised by phase rather than by function:
 *
 *   ELIGIBILITY CHECK      the request the form makes before it will accept
 *                          anything, the four answers it can get (eligible,
 *                          ineligible with a reason, ineligible without one, and
 *                          "the check itself failed"), the retry that exists only
 *                          for the last of those, and the late answer that must be
 *                          discarded when the transaction has since changed.
 *
 *   SUBMISSION LIFECYCLE   one request per submission, the in-flight state that
 *                          enforces it, the two confirmations the double-blind
 *                          reveal requires, the removal of the controls once a
 *                          rating exists, the parent callback in its three
 *                          shapes, and where focus lands afterwards.
 *
 *   LOCAL REFUSALS         the three states in which this form declines to send
 *                          anything at all. Each is proven by the ABSENCE of a
 *                          request, because a client-side rule that merely
 *                          colours a field is not a rule.
 *
 *   SERVER REFUSALS        every status this endpoint can answer with, rendered
 *                          in the server's own words; both shapes FastAPI emits
 *                          `detail` in; and the contract failure whose internals
 *                          must never reach the screen.
 *
 *   OUTBOUND PAYLOAD       exactly what goes on the wire: the sanitised,
 *                          normalised, NFC-composed review the counter measured,
 *                          and no identity claim of any kind.
 *
 * WHY THE OUT-OF-RANGE SCORE IS PROVEN THROUGH THE SERVER
 * -----------------------------------------------------------------------------
 * `../StarRatingInput` renders exactly one option per score in
 * `RATING_MIN..RATING_MAX` and calls back with that option's own value, so there
 * is no interaction — click, keyboard, or otherwise — that produces a score
 * outside the scale. Adding a hidden numeric input to manufacture one would test
 * a control the product does not ship.
 *
 * So that refusal is exercised where it genuinely lives: the server. The backend
 * bounds the field with `conint(strict=True, ge=settings.RATING_MIN,
 * le=settings.RATING_MAX)` (`backend/app/schema/rating.py`), so a score outside
 * the scale is refused by Pydantic before any handler runs — which is the
 * enterprise practice "server-side validation is authoritative; client-side
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
 *             them real is also what makes the refusal cases honest: each
 *             rejection fixture has to satisfy the REAL `readServerDetail`, which
 *             gates on `axios.isAxiosError`, so a fixture that only looked
 *             plausible would surface the generic fallback and fail its
 *             assertion.
 *   REAL      `../../utils/validation` — the genuine Zod bound, the genuine
 *             DOMPurify sanitisation and the genuine NFC normalisation run here.
 *             Stubbing them would turn every payload and length assertion below
 *             into a test of a stub.
 *   REAL      `../StarRatingInput` — the score is chosen by clicking the option
 *             a user would click, named the way a screen reader announces it.
 *   REAL      `../../schema/rating` — the shared bounds, the decision schema and
 *             the rating schema, which every fixture here is parsed through.
 *
 * No `../../utils/formatting` mock appears here, unlike `ReputationBadge.test.tsx`:
 * this component does not import that module, so nothing in this suite's graph
 * needs it.
 *
 * EVERY FIXTURE IS PARSED BY THE PRODUCTION SCHEMA
 * -----------------------------------------------------------------------------
 * `decision()` and `created()` below both `parse` through the schema the service
 * itself decodes responses with. That is not ceremony: it closes the gap where a
 * test passes against a response the server could never send — an eligible
 * decision missing its counterparty, a rating with a fractional score — and
 * flatters the component with behaviour nobody will ever reach.
 *
 * PUBLISHED WORDING IS PINNED AS LITERALS, NOT IMPORTED
 * -----------------------------------------------------------------------------
 * The sentences a user reads are declared here as literal strings and compared by
 * exact equality. Importing them from the component would prove only that it is
 * self-consistent: swap two messages, or replace one with "Error", and an
 * import-based assertion stays green while the interface starts lying. The one
 * deliberate exception is `CONTRACT_ERROR_MESSAGE`, which the service exports
 * precisely so that its consumers render that exact text; there the independent
 * assertion is the one that matters — that the endpoint label and the schema
 * diagnostics do NOT appear on screen.
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
 * `describe`, `it`, `expect`, `beforeEach` and `afterEach` are injected globals
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

import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import RatingSubmissionForm from '../RatingSubmissionForm';
import {
  CONTRACT_ERROR_MESSAGE,
  RatingContractError,
  fetchRatingEligibility,
  submitRating,
} from '../../services/rating';
import {
  EligibilityDecisionSchema,
  RATING_MAX,
  RATING_MIN,
  REVIEW_MAX_LENGTH,
  RatingSchema,
} from '../../schema/rating';
import type { EligibilityDecision, Rating } from '../../schema/rating';

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

/** A second transaction, used by the cases that change the prop mid-life. */
const OTHER_TRANSACTION_ID = 'txn-2';

/**
 * The score the tests select: the middle of the scale, computed from the shared
 * bounds so it stays a genuine interior choice at any scale and this file
 * contains no hardcoded `3`, `1` or `5`.
 */
const SELECTED_SCORE = Math.floor((RATING_MIN + RATING_MAX) / 2);

/**
 * A DIFFERENT in-scale score, used as the value the server reports back.
 *
 * The confirmation must echo what was RECORDED, which is the server's number
 * rather than the local selection — the component reads it from the response for
 * exactly that reason. Asserting it with a value the user did not choose is the
 * only way to tell the two apart; with one score both readings look identical.
 */
const ECHOED_SCORE = RATING_MAX;

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

/** The submit control while a submission is in flight. */
const IN_FLIGHT_BUTTON_NAME = /submitting/i;

/** The recovery control that appears only when the eligibility CHECK failed. */
const RETRY_BUTTON_NAME = /check again/i;

/** The recovery control while a re-check is in flight. */
const RETRY_IN_FLIGHT_NAME = /checking\.\.\./i;

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

/** A review of exactly the bound, which must be accepted rather than refused. */
const AT_LIMIT_REVIEW = 'a'.repeat(REVIEW_MAX_LENGTH);

/**
 * A review the production path leaves untouched: plain text, no angle brackets
 * for DOMPurify to strip, no surrounding whitespace or repeated blank lines for
 * the normaliser to collapse. That invariance is what lets the payload cases
 * assert by exact equality — the string that goes into the field is the string
 * that must reach the request.
 */
const ACCEPTED_REVIEW = 'The paperwork was in order and the handover was punctual.';

/**
 * A review that exercises every transformation the submission path performs, in
 * one value, and its exact expected result.
 *
 * Each element is there for a reason:
 *   `  ` … `  `        surrounding whitespace the normaliser trims
 *   `<b>` … `</b>`     markup DOMPurify removes, keeping the text between it
 *   `\u0007`           a C0 control character (BEL) that is stripped
 *   `\r\n\r\n\r\n`     CRLF runs folded to `\n` and then collapsed to one blank
 *                      line, because three or more newlines become two
 *   `\u200B`           a zero-width space, a FORMAT character, also stripped
 *
 * The expected value is written out in full rather than computed by calling the
 * production helper: deriving it from `prepareReviewText` would assert that the
 * function agrees with itself.
 */
const MESSY_REVIEW = '  <b>Great</b>\u0007 car\r\n\r\n\r\nreally\u200Bgood  ';
const MESSY_REVIEW_ON_THE_WIRE = 'Great car\n\nreallygood';

/**
 * A review made entirely of markup, and therefore of nothing once sanitised.
 *
 * The security case: `review` is optional on the wire, so a value that reduces to
 * the empty string must be sent as ABSENT rather than as `""`. The `<script>`
 * body is what makes the assertion meaningful — if any part of it reached the
 * request, the exact-shape assertion fails.
 */
const MARKUP_ONLY_REVIEW = '<script>alert(1)</script>';

/**
 * A decomposed grapheme (`e` + U+0301 COMBINING ACUTE ACCENT) and its composed
 * form.
 *
 * The server composes a review to NFC before measuring and storing it, so the
 * client must too: measured raw, this is 7 code points and would be counted and
 * stored differently from the 6 the server records.
 */
const DECOMPOSED_REVIEW = 'e\u0301clair';
const COMPOSED_REVIEW = '\u00E9clair';

/**
 * The fragment of the component's over-length message that must be on screen.
 *
 * A fragment interpolated from the bound rather than the whole sentence: the
 * assertion then holds if the surrounding copy is reworded, and still fails if
 * the message stops naming the limit or stops appearing at all.
 */
const OVER_LIMIT_FRAGMENT = `over the ${REVIEW_MAX_LENGTH}-character limit`;

/** The character counter's exact text for a review of `length` code points. */
const counterText = (length: number): string =>
  `${length} / ${REVIEW_MAX_LENGTH} characters`;

/** The counter's text once the bound is exceeded, which adds a worded suffix. */
const overLimitCounterText = (length: number): string =>
  `${counterText(length)} (over the limit)`;

/* -------------------------------------------------------------------------- */
/* Published wording, pinned independently of the implementation.             */
/* -------------------------------------------------------------------------- */

/** While the eligibility answer has not arrived yet. */
const CHECKING_MESSAGE = 'Checking whether you can rate this transaction...';

/** When the check itself failed, which is NOT the same as being refused. */
const CHECK_FAILED_MESSAGE =
  'We could not check whether you can rate this transaction. Please try again.';

/** When the server refuses but sends no reason — a contract backstop. */
const NO_REASON_MESSAGE = 'You cannot rate this transaction right now.';

/** When a submit arrives with no score chosen. */
const SCORE_REQUIRED_MESSAGE =
  `Choose a score from ${RATING_MIN} to ${RATING_MAX} before submitting.`;

/** When the transaction reference itself fails the shared schema. */
const INVALID_REFERENCE_MESSAGE =
  'This transaction cannot be rated because its reference is not valid.';

/** The confirmation's first sentence, which echoes the RECORDED score. */
const recordedSentence = (score: number): string =>
  `Your ${score}-star rating has been recorded.`;

/** The confirmation's second sentence when the rating is already visible. */
const PUBLISHED_SUFFIX = ' It is now visible on their profile.';

/**
 * The confirmation's second sentence while the rating is still withheld.
 *
 * Pinned in full because it is the load-bearing half of the double-blind model:
 * a brand-new rating is genuinely invisible, so silence here would read as a
 * failed submission, and a promise that "both become visible" would be false on
 * the window-expiry path where only this rating exists to reveal.
 */
const UNPUBLISHED_SUFFIX =
  ' It stays private for now. If the other party rates you too, both ratings' +
  ' become visible at the same time; if they never do, yours becomes visible on' +
  ' its own once the rating window closes.';

/** The sentence naming the counterparty's role, per derived direction. */
const BUYER_TO_SELLER_LABEL = 'You are rating the seller for this purchase.';
const SELLER_TO_BUYER_LABEL = 'You are rating the buyer for this purchase.';

/** The log lines the component writes, which the tests locate by first argument. */
const ELIGIBILITY_LOG = 'Failed to fetch rating eligibility';
const SUBMISSION_LOG = 'Failed to submit rating';
const CALLBACK_LOG = 'A rating was submitted but the parent refresh failed';

/**
 * The server's own words for that refusal, which is what must reach the screen.
 *
 * This is Pydantic v1's message for an exceeded `le` bound, with the ceiling
 * interpolated from the shared constant rather than written as a literal, so the
 * fixture follows the scale instead of pinning a number to it.
 *
 * Every failure the ratings API answers — a router-raised refusal (401, 403, 404,
 * 409 and the self-rating 422) and a request the framework rejected alike —
 * carries `detail` as a STRING in one envelope, `{ detail, errors? }`, with the
 * offending fields named in that sentence. So the fixture below is a string, which
 * is both what the server sends and what `readServerDetail` returns; on a
 * validation failure the real server's sentence would additionally be prefixed
 * with the field name ("score: …"), which changes nothing this form does with it.
 */
const OUT_OF_RANGE_DETAIL = `ensure this value is less than or equal to ${RATING_MAX}`;

/* -------------------------------------------------------------------------- */
/* Fixtures                                                                   */
/* -------------------------------------------------------------------------- */

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
 * of which this component reads. Naming the fields that matter documents the
 * contract precisely, and keeps `any` out of the file.
 *
 * `detail` is typed as either shape FastAPI emits, because both reach this
 * component: a router-raised refusal sends a string, while a request Pydantic
 * rejected before any handler ran sends an array of issue objects.
 */
interface ValidationIssueLike {
  loc: readonly string[];
  msg: string;
  type: string;
}

interface ApiErrorLike {
  isAxiosError: true;
  response: {
    data: { detail: string | readonly ValidationIssueLike[] };
    status: number;
  };
}

/** Builds a refused-request rejection value carrying a string `detail`. */
const refusal = (status: number, detail: string): ApiErrorLike => ({
  isAxiosError: true,
  response: { data: { detail }, status },
});

/**
 * Builds the rejection FastAPI produces when the request body fails validation
 * before any handler runs: `detail` is an ARRAY of issue objects.
 */
const validationRefusal = (
  issues: readonly ValidationIssueLike[],
): ApiErrorLike => ({
  isAxiosError: true,
  response: { data: { detail: issues }, status: 422 },
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
 * The rating a successful submission returns, as the server composes it: the
 * document ID is `{transactionId}_{raterId}`, the counterparty and direction are
 * server-derived, and a new rating is unpublished.
 */
const CREATED_RATING: Rating = {
  id: `${TRANSACTION_ID}_user-buyer-7`,
  transactionId: TRANSACTION_ID,
  vehicleListingId: 'listing-9',
  raterId: 'user-buyer-7',
  rateeId: 'user-seller-42',
  direction: 'buyer_to_seller',
  score: SELECTED_SCORE,
  review: null,
  isPublished: false,
  createdAt: new Date('2024-05-01T10:00:00.000Z'),
  updatedAt: new Date('2024-05-01T10:00:00.000Z'),
};

/**
 * Builds a created rating, validated through `RatingSchema` for the same reason
 * `decision()` validates its own fixtures: the service decodes every response
 * with this schema, so anything it would refuse is not a response the component
 * can receive.
 *
 * @param overrides Fields to change on the freshly created baseline.
 */
const created = (overrides: Partial<Rating> = {}): Rating =>
  RatingSchema.parse({ ...CREATED_RATING, ...overrides });

/**
 * A promise whose settlement this test controls.
 *
 * The in-flight cases need the request to STAY pending while they inspect the
 * interface, which `mockResolvedValue` cannot express — it settles on the next
 * microtask, so the in-flight state exists for no observable moment.
 */
interface Deferred<T> {
  promise: Promise<T>;
  resolve: (value: T) => void;
  reject: (reason: unknown) => void;
}

const deferred = <T,>(): Deferred<T> => {
  let resolve: (value: T) => void = () => undefined;
  let reject: (reason: unknown) => void = () => undefined;

  const promise = new Promise<T>((resolveFn, rejectFn) => {
    resolve = resolveFn;
    reject = rejectFn;
  });

  return { promise, resolve, reject };
};

/* -------------------------------------------------------------------------- */
/* Queries                                                                    */
/* -------------------------------------------------------------------------- */

/** The polite live region, which carries state and outcome. */
const statusRegion = (): HTMLElement => screen.getByRole('status');

/** The assertive live region, which carries refusals and nothing else. */
const alertRegion = (): HTMLElement => screen.getByRole('alert');

const submitButton = (): HTMLElement =>
  screen.getByRole('button', { name: SUBMIT_BUTTON_NAME });

const reviewField = (): HTMLElement =>
  screen.getByRole('textbox', { name: REVIEW_FIELD_NAME });

const scoreOption = (score: number = SELECTED_SCORE): HTMLElement =>
  screen.getByRole('radio', { name: optionName(score) });

/**
 * The character counter, found THROUGH the review field's accessible
 * description.
 *
 * Resolving `aria-describedby` rather than querying the text is deliberate: it
 * asserts that the counter is actually associated with the field — the property
 * that makes the count available to a screen reader on demand — and it keeps
 * working when the counter's own text is what is under test.
 */
const counterFor = (field: HTMLElement): HTMLElement => {
  const id = field.getAttribute('aria-describedby');
  const counter = id === null ? null : document.getElementById(id);

  if (counter === null) {
    throw new Error(
      'Expected the review field to be described by a character counter, and it was not.',
    );
  }

  return counter;
};

/**
 * The `<form>` the controls live in.
 *
 * Needed because the local-refusal cases have to submit a form whose submit
 * BUTTON is correctly disabled, so there is nothing to click: dispatching the
 * submit event directly is the only way to reach the handler's own gate and prove
 * it refuses too. Reached from a control found by role, so the query stays
 * anchored to the accessibility tree, and narrowed rather than asserted so no
 * non-null assertion is needed.
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
    expect(scoreOption()).toBeEnabled();
  });
};

/** Waits until the submission confirmation has replaced the controls. */
const awaitConfirmation = async (): Promise<void> => {
  await waitFor(() => {
    expect(screen.queryByRole('button', { name: SUBMIT_BUTTON_NAME })).toBeNull();
  });
};

/**
 * Drop focus the way a browser does when the focused control becomes disabled.
 *
 * THIS IS NOT A CONTRIVANCE, IT IS A jsdom GAP, AND WITHOUT IT TWO CASES BELOW
 * ASSERT NOTHING. A real browser runs the unfocusing steps when an element that
 * holds focus is disabled: it blurs with a `relatedTarget` of `null` and falls
 * back to `document.body`. That loss is precisely what the component's failure
 * paths exist to repair. jsdom does not implement that step, so the disabled
 * button keeps focus for the whole request, `focusWasDropped()` reports false, the
 * restoration is correctly skipped — and a test asserting "focus is on the submit
 * button" then passes because focus never left it, whether or not the component
 * would have put it back. Deleting the restoration entirely leaves such a test
 * green, which was verified by doing exactly that.
 *
 * Calling this while the control is disabled reproduces the browser's state, so
 * the assertion that follows measures the component rather than jsdom.
 *
 * @throws When nothing is focused, which means the test is not in the state it
 *   believes it is in and the assertion afterwards would be meaningless.
 */
const dropFocusAsABrowserWould = (): void => {
  const active = document.activeElement;

  if (active === null || active === document.body) {
    throw new Error(
      'Expected a focused control to blur, and found none — the interaction under test did not leave focus where it was assumed to.',
    );
  }

  const control = active as HTMLElement & { disabled?: boolean };

  /*
   * jsdom refuses to blur an element it considers unfocusable, and a DISABLED
   * control is unfocusable — so `blur()` on the very element whose disabling
   * caused the loss is a silent no-op, as is `document.body.focus()`. The flag is
   * therefore lifted for the duration of the blur and restored immediately. React
   * recomputes `disabled` from state on its next render, so nothing is left
   * inconsistent: the only observable effect is the one a browser would have
   * produced by itself, `document.activeElement === document.body`.
   */
  const wasDisabled = control.disabled === true;

  if (wasDisabled) {
    control.disabled = false;
  }

  control.blur();

  if (wasDisabled) {
    control.disabled = true;
  }
};

/**
 * Every `console.error` call made during the current test, newest last.
 *
 * Captured rather than merely silenced, because the component's failure paths are
 * required to log a SANITISED record — the axios error itself carries the bearer
 * token in `config.headers` and the request body in `config.data`, so writing it
 * down would publish a live credential to the console and to every telemetry
 * agent mirroring it (CWE-532). Holding the calls here lets the tests assert what
 * was written as well as what was not.
 */
let loggedErrors: unknown[][] = [];

/** The captured records whose first argument is `message`. */
const logsFor = (message: string): unknown[][] =>
  loggedErrors.filter((entry) => entry[0] === message);

describe('RatingSubmissionForm', () => {
  /**
   * Both handles are RESET rather than merely cleared, so neither the recorded
   * calls nor the resolved or rejected value set by one test can reach another.
   * Every test below states its own server behaviour explicitly.
   *
   * `console.error` is replaced with a collector rather than left alone: three of
   * the component's paths log deliberately, and an unreplaced console would print
   * those records as suite noise while leaving nothing to assert against.
   */
  beforeEach(() => {
    mockedFetchEligibility.mockReset();
    mockedSubmitRating.mockReset();

    loggedErrors = [];
    vi.spyOn(console, 'error').mockImplementation((...args: unknown[]) => {
      loggedErrors.push(args);
    });
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  describe('the eligibility check', () => {
    it('reports the check in progress and holds every control inert until an answer arrives', () => {
      // Never settles, so the loading state is observable for the whole test
      // rather than for one microtask.
      mockedFetchEligibility.mockImplementation(
        () => new Promise<EligibilityDecision>(() => undefined),
      );

      render(<RatingSubmissionForm transactionId={TRANSACTION_ID} />);

      // The wait is ANNOUNCED, politely. A form that rendered nothing here would
      // leave a screen-reader user with three disabled controls and no
      // explanation, which is the WCAG 2.1 AA failure this region exists for.
      expect(statusRegion()).toHaveTextContent(CHECKING_MESSAGE);

      // Nothing has gone wrong, so the assertive region stays silent. Asserting
      // this is how the suite proves "checking" is never reported as an error.
      expect(alertRegion()).toBeEmptyDOMElement();

      expect(submitButton()).toBeDisabled();
      expect(reviewField()).toBeDisabled();
      expect(scoreOption()).toBeDisabled();

      // The label still reads as the action, not as progress: nothing has been
      // submitted, and 'Submitting...' here would misdescribe the wait.
      expect(submitButton()).toHaveTextContent('Submit rating');

      // Exactly one question was asked, about THIS transaction.
      expect(mockedFetchEligibility).toHaveBeenCalledTimes(1);
      expect(mockedFetchEligibility).toHaveBeenCalledWith(TRANSACTION_ID);

      // The write endpoint is not consulted while the read is outstanding.
      expect(mockedSubmitRating).not.toHaveBeenCalled();
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
        expect(statusRegion()).toHaveTextContent(reason);
      });

      // The form asked about THIS transaction. Without this the assertions above
      // would also pass for a form that asked about the wrong one.
      expect(mockedFetchEligibility).toHaveBeenCalledWith(TRANSACTION_ID);

      // Every control is inert, so there is no way to compose a rating that the
      // server has already said it will refuse. All three are found by role, so
      // each query doubles as an accessibility assertion.
      expect(submitButton()).toBeDisabled();
      expect(reviewField()).toBeDisabled();
      expect(scoreOption()).toBeDisabled();

      // An answered "no" is final, so no re-ask is offered. The retry control is
      // reserved for a check that FAILED, and rendering it here would imply the
      // server's decision might change on a second question.
      expect(
        screen.queryByRole('button', { name: RETRY_BUTTON_NAME }),
      ).toBeNull();

      // Nothing was sent. An ineligible caller must not reach the write endpoint
      // at all, rather than being refused there.
      expect(mockedSubmitRating).not.toHaveBeenCalled();
    });

    it('falls back to its own sentence when the server refuses without giving a reason', async () => {
      /*
       * THE ONE FIXTURE IN THIS FILE THAT IS NOT PARSED BY THE PRODUCTION SCHEMA,
       * and the exception is the point of the case.
       *
       * `EligibilityDecisionSchema` refuses an ineligible decision whose `reason`
       * is null — "An ineligible decision must explain why" — so `decision()`
       * cannot build this value and the service could not decode it either. The
       * branch it exercises is therefore a genuine CONTRACT BACKSTOP: unreachable
       * while the schema holds, and the difference between a disabled control with
       * an explanation and a disabled control with none if it ever stops holding.
       * Pinning it here is what keeps that fallback from being quietly deleted as
       * dead code, and the value is written as a plain literal — no cast is needed,
       * because `reason` is typed `string | null` and only the runtime refinement
       * objects.
       */
      const decisionWithoutReason: EligibilityDecision = {
        eligible: false,
        reason: null,
        rateeId: null,
        direction: null,
        alreadyRated: false,
      };

      mockedFetchEligibility.mockResolvedValue(decisionWithoutReason);

      render(<RatingSubmissionForm transactionId={TRANSACTION_ID} />);

      await waitFor(() => {
        expect(statusRegion()).toHaveTextContent(NO_REASON_MESSAGE);
      });

      expect(submitButton()).toBeDisabled();

      // No direction was derived, so no role is claimed. Inventing one here would
      // tell the user they are rating a party the server never identified.
      expect(screen.queryByText(BUYER_TO_SELLER_LABEL)).toBeNull();
      expect(screen.queryByText(SELLER_TO_BUYER_LABEL)).toBeNull();
    });

    it('names the seller as the counterparty when the server derived that direction, without revealing their identifier', async () => {
      mockedFetchEligibility.mockResolvedValue(
        decision({ direction: 'buyer_to_seller', rateeId: 'user-seller-42' }),
      );

      render(<RatingSubmissionForm transactionId={TRANSACTION_ID} />);
      await awaitEnabledControls();

      expect(screen.getByText(BUYER_TO_SELLER_LABEL)).toBeInTheDocument();

      // The opaque identifier is display context the component received and must
      // NOT render: it is meaningless to the user and is another party's internal
      // key. The role is the useful half, and the only half shown.
      expect(document.body.textContent).not.toContain('user-seller-42');
    });

    it('names the buyer as the counterparty when the server derived the other direction', async () => {
      mockedFetchEligibility.mockResolvedValue(
        decision({ direction: 'seller_to_buyer', rateeId: 'user-buyer-7' }),
      );

      render(<RatingSubmissionForm transactionId={TRANSACTION_ID} />);
      await awaitEnabledControls();

      expect(screen.getByText(SELLER_TO_BUYER_LABEL)).toBeInTheDocument();
      expect(document.body.textContent).not.toContain('user-buyer-7');
    });

    it('reports a failed check as unknown rather than as a refusal, and offers a way to re-ask', async () => {
      // A transport failure: no response, so no server message to render.
      mockedFetchEligibility.mockRejectedValue(new Error('connection dropped'));

      render(<RatingSubmissionForm transactionId={TRANSACTION_ID} />);

      // "We could not check" and "you may not" are different facts. Reporting the
      // second when only the first is known would wrongly tell a legitimate rater
      // they are barred, so the exact sentence is pinned here.
      await waitFor(() => {
        expect(alertRegion()).toHaveTextContent(CHECK_FAILED_MESSAGE);
      });

      // The controls stay inert because nothing is known about eligibility.
      expect(submitButton()).toBeDisabled();
      expect(scoreOption()).toBeDisabled();

      // A real button, typed `button` so it cannot submit the form it sits beside.
      const retry = screen.getByRole('button', { name: RETRY_BUTTON_NAME });
      expect(retry).toBeEnabled();
      expect(retry).toHaveAttribute('type', 'button');

      // The failure was logged as a SANITISED record. The axios error itself
      // carries the bearer token on `config.headers`, so the assertion is that a
      // flat description was written rather than the error object.
      const records = logsFor(ELIGIBILITY_LOG);
      expect(records).toHaveLength(1);
      expect(records[0][1]).toEqual({
        method: 'UNKNOWN',
        url: 'unknown',
        status: null,
        code: 'none',
        message: 'connection dropped',
      });
    });

    it('prefers the server explanation when the failed check carried one', async () => {
      // A 404 from the eligibility endpoint means the transaction does not exist,
      // which the server can explain and this component cannot.
      mockedFetchEligibility.mockRejectedValue(
        refusal(404, 'Transaction not found'),
      );

      render(<RatingSubmissionForm transactionId={TRANSACTION_ID} />);

      await waitFor(() => {
        expect(alertRegion()).toHaveTextContent('Transaction not found');
      });

      // Specifically NOT the generic sentence: a form that fell back to its own
      // copy here would replace a precise, actionable explanation with a vague one.
      expect(alertRegion()).not.toHaveTextContent(CHECK_FAILED_MESSAGE);

      // The status carries the server's detail, and the log carries it too — with
      // the status code, and still no error object.
      const records = logsFor(ELIGIBILITY_LOG);
      expect(records).toHaveLength(1);
      expect(records[0][1]).toMatchObject({
        status: 404,
        detail: 'Transaction not found',
      });
    });

    it('re-checks on demand, labels the check in flight, and hands focus to the controls when it succeeds', async () => {
      const user = userEvent.setup();
      const secondAnswer = deferred<EligibilityDecision>();

      mockedFetchEligibility.mockRejectedValueOnce(new Error('connection dropped'));
      mockedFetchEligibility.mockImplementationOnce(() => secondAnswer.promise);

      render(<RatingSubmissionForm transactionId={TRANSACTION_ID} />);

      await waitFor(() => {
        expect(
          screen.getByRole('button', { name: RETRY_BUTTON_NAME }),
        ).toBeInTheDocument();
      });

      await user.click(screen.getByRole('button', { name: RETRY_BUTTON_NAME }));

      // The control STAYS MOUNTED while the re-check runs, relabelled and
      // disabled. It is the accessible half of the disabled state: a button that
      // only greys out reports nothing about why, and one that disappeared would
      // leave the user watching their recovery option vanish.
      const checking = await screen.findByRole('button', {
        name: RETRY_IN_FLIGHT_NAME,
      });
      expect(checking).toBeDisabled();

      secondAnswer.resolve(decision());
      await awaitEnabledControls();

      // The re-ask succeeded, so there is nothing left to retry.
      expect(
        screen.queryByRole('button', { name: RETRY_BUTTON_NAME }),
      ).toBeNull();

      // The button the user was standing on has just been removed, so focus is
      // moved to the group's tab stop rather than dropped to the document body
      // (WCAG 2.4.3). With no score chosen yet the tab stop is the first option,
      // which is also the next thing to do.
      expect(document.activeElement).toBe(scoreOption(RATING_MIN));

      // The failure message is gone: it described a state that no longer holds.
      expect(alertRegion()).toBeEmptyDOMElement();

      expect(mockedFetchEligibility).toHaveBeenCalledTimes(2);
    });

    it('restores focus to the retry control when the re-check fails again', async () => {
      const user = userEvent.setup();
      const secondAnswer = deferred<EligibilityDecision>();

      mockedFetchEligibility.mockRejectedValueOnce(new Error('connection dropped'));
      mockedFetchEligibility.mockImplementationOnce(() => secondAnswer.promise);

      render(<RatingSubmissionForm transactionId={TRANSACTION_ID} />);

      const retry = await screen.findByRole('button', {
        name: RETRY_BUTTON_NAME,
      });

      await user.click(retry);

      // Deferred, so the in-flight state — during which the button is disabled and
      // the browser therefore takes focus away — actually exists to act on.
      await screen.findByRole('button', { name: RETRY_IN_FLIGHT_NAME });
      dropFocusAsABrowserWould();
      expect(document.activeElement).toBe(document.body);

      await act(async () => {
        secondAnswer.reject(new Error('still down'));
      });

      // The harder of the two focus cases: this path puts the button back exactly
      // as it was, so without the restoration the interface shows a ready control
      // while the keyboard has been left on `document.body` with no indicator
      // anywhere. Re-enabling a control does not hand focus back on its own.
      await waitFor(() => {
        expect(document.activeElement).toBe(
          screen.getByRole('button', { name: RETRY_BUTTON_NAME }),
        );
      });

      // The explanation is still on screen — a second failure is not a reason to
      // stop explaining the first.
      expect(alertRegion()).toHaveTextContent(CHECK_FAILED_MESSAGE);
      expect(mockedFetchEligibility).toHaveBeenCalledTimes(2);
    });

    it('ignores a late answer that belongs to a transaction it is no longer rating', async () => {
      const firstAnswer = deferred<EligibilityDecision>();

      mockedFetchEligibility.mockImplementationOnce(() => firstAnswer.promise);
      mockedFetchEligibility.mockResolvedValue(decision());

      const { rerender } = render(
        <RatingSubmissionForm transactionId={TRANSACTION_ID} />,
      );

      // The screen moves to a different transaction before the first answer
      // arrives, which is what happens when a user navigates between two
      // completed sales faster than the network responds.
      rerender(<RatingSubmissionForm transactionId={OTHER_TRANSACTION_ID} />);
      await awaitEnabledControls();

      // The first request now answers, with a decision about the OLD transaction.
      // Settled inside `act` so the continuation and any render it would schedule
      // are flushed before the assertions: without that the test could pass simply
      // by asserting too early, which is the one way this case can lie.
      await act(async () => {
        firstAnswer.resolve(
          decision({
            eligible: false,
            reason: 'You have already rated this transaction',
            alreadyRated: true,
          }),
        );
      });

      // Given the chance to apply, it must not: the request-sequence guard
      // invalidated it when the transaction changed. Applying it would disable a
      // form the current transaction is eligible for and explain the refusal of a
      // different sale.
      expect(mockedFetchEligibility).toHaveBeenCalledTimes(2);
      expect(statusRegion()).toBeEmptyDOMElement();
      expect(scoreOption()).toBeEnabled();
      expect(mockedFetchEligibility).toHaveBeenNthCalledWith(1, TRANSACTION_ID);
      expect(mockedFetchEligibility).toHaveBeenNthCalledWith(
        2,
        OTHER_TRANSACTION_ID,
      );
    });
  });


  describe('the submission lifecycle', () => {
    it('sends one request per submission and reports the wait on the control that started it', async () => {
      const user = userEvent.setup();
      const submission = deferred<Rating>();

      mockedFetchEligibility.mockResolvedValue(decision());
      mockedSubmitRating.mockImplementation(() => submission.promise);

      render(<RatingSubmissionForm transactionId={TRANSACTION_ID} />);
      await awaitEnabledControls();

      await user.click(scoreOption());
      await user.click(submitButton());

      // The label changes, which is the accessible half of the disabled state: a
      // control that only greys out reports nothing about why it cannot be used.
      const inFlight = await screen.findByRole('button', {
        name: IN_FLIGHT_BUTTON_NAME,
      });
      expect(inFlight).toHaveTextContent('Submitting...');
      expect(inFlight).toBeDisabled();

      // The whole form goes inert, not just the button: editing the score or the
      // review while the request they produced is in flight would leave the screen
      // describing a payload that is not the one being sent.
      expect(reviewField()).toBeDisabled();
      expect(scoreOption()).toBeDisabled();

      // Pressing again does nothing. This is the double-submit suppression, and it
      // is asserted by the count rather than by the attribute — a second POST would
      // be refused with a 409 by the datastore-enforced one-rating-per-rater rule
      // and the user would be told their successful submission had failed.
      await user.click(inFlight);
      expect(mockedSubmitRating).toHaveBeenCalledTimes(1);

      submission.resolve(created());
      await awaitConfirmation();

      // Still one, across the whole lifecycle.
      expect(mockedSubmitRating).toHaveBeenCalledTimes(1);
    });

    it('confirms an unpublished rating by explaining the double-blind reveal, and echoes the recorded score', async () => {
      const user = userEvent.setup();

      mockedFetchEligibility.mockResolvedValue(decision());
      // The server reports a score the user did not choose. That cannot happen
      // today, and asserting it is the only way to prove the confirmation states
      // what was RECORDED rather than what was requested — the property that keeps
      // the sentence honest if the server ever normalises what it accepted.
      mockedSubmitRating.mockResolvedValue(
        created({ score: ECHOED_SCORE, isPublished: false }),
      );

      render(<RatingSubmissionForm transactionId={TRANSACTION_ID} />);
      await awaitEnabledControls();

      await user.click(scoreOption(SELECTED_SCORE));
      await user.click(submitButton());

      await awaitConfirmation();

      // Exact equality on the whole sentence. A new rating is genuinely invisible
      // until the counterparty submits or the window closes, so silence would read
      // as a failed submission — and the two reveal paths are stated separately
      // because only the reciprocal one publishes a pair.
      expect(statusRegion().textContent).toBe(
        `${recordedSentence(ECHOED_SCORE)}${UNPUBLISHED_SUFFIX}`,
      );

      // No number of days is quoted: the window is a server setting that is not
      // published to this client, so a figure here would be free to drift.
      expect(statusRegion().textContent).not.toMatch(/\d+\s*days?/);

      // Nothing went wrong, so the assertive region stays silent beside it.
      expect(alertRegion()).toBeEmptyDOMElement();
    });

    it('confirms a published rating as already visible on the counterparty profile', async () => {
      const user = userEvent.setup();

      mockedFetchEligibility.mockResolvedValue(decision());
      // Published on creation is the reciprocal case: the counterparty had already
      // rated, so this submission revealed both.
      mockedSubmitRating.mockResolvedValue(
        created({ score: SELECTED_SCORE, isPublished: true }),
      );

      render(<RatingSubmissionForm transactionId={TRANSACTION_ID} />);
      await awaitEnabledControls();

      await user.click(scoreOption());
      await user.click(submitButton());

      await awaitConfirmation();

      expect(statusRegion().textContent).toBe(
        `${recordedSentence(SELECTED_SCORE)}${PUBLISHED_SUFFIX}`,
      );

      // The withheld wording must NOT appear: telling a user their visible rating
      // is private is the same defect as the reverse, in the other direction.
      expect(statusRegion().textContent).not.toContain('stays private');
    });

    it('removes the controls once a rating exists and offers no way to change it', async () => {
      const user = userEvent.setup();

      mockedFetchEligibility.mockResolvedValue(decision());
      mockedSubmitRating.mockResolvedValue(created());

      render(<RatingSubmissionForm transactionId={TRANSACTION_ID} />);
      await awaitEnabledControls();

      await user.click(scoreOption());
      fireEvent.change(reviewField(), { target: { value: ACCEPTED_REVIEW } });
      await user.click(submitButton());

      await awaitConfirmation();

      // Reputation records are append-only: no endpoint accepts a rewrite and no
      // schema describes one, so ANY control left here would advertise a capability
      // the system does not have. Asserting the absence of every button — rather
      // than of the submit button alone — is what catches an "Edit" or "Undo"
      // control being added later.
      expect(screen.queryAllByRole('button')).toHaveLength(0);
      expect(screen.queryAllByRole('radio')).toHaveLength(0);
      expect(screen.queryAllByRole('textbox')).toHaveLength(0);

      // The confirmation is what replaces them, so the section is not left blank.
      expect(statusRegion()).toHaveTextContent(recordedSentence(SELECTED_SCORE));
    });

    it('moves focus to the confirmation, which is a programmatic target and not a tab stop', async () => {
      const user = userEvent.setup();

      mockedFetchEligibility.mockResolvedValue(decision());
      mockedSubmitRating.mockResolvedValue(created());

      render(<RatingSubmissionForm transactionId={TRANSACTION_ID} />);
      await awaitEnabledControls();

      await user.click(scoreOption());
      await user.click(submitButton());
      await awaitConfirmation();

      // The submit button was removed with the user standing on it, so focus is
      // moved to the sentence that answers what they just did. Landing on the
      // document body instead would lose a keyboard user's place entirely and
      // leave the outcome unread (WCAG 2.4.3).
      await waitFor(() => {
        expect(document.activeElement).toBe(statusRegion());
      });

      // `-1` is what makes that legal: focusable programmatically, and never a
      // stop in the tab sequence, because it is a message rather than a control.
      expect(statusRegion()).toHaveAttribute('tabindex', '-1');
    });

    it('restores focus to the submit control after a retryable failure so another attempt is one keypress away', async () => {
      const user = userEvent.setup();
      const submission = deferred<Rating>();
      /*
       * A 503, deliberately, and not one of the terminal statuses. Restoration is
       * the contract for a failure a retry could still resolve: an unreachable
       * datastore is reported with `Retry-After` and the same request may be sent
       * again unchanged. The four terminal statuses (401, 403, 404, 409) leave the
       * controls INERT instead, because each proves the caller may not rate this
       * transaction at all - asserting restoration on one of those would demand a
       * live button for an attempt that cannot succeed.
       */
      const detail =
        'The service could not reach its datastore. This is temporary - ' +
        'please retry shortly.';

      mockedFetchEligibility.mockResolvedValue(decision());
      mockedSubmitRating.mockImplementation(() => submission.promise);

      render(<RatingSubmissionForm transactionId={TRANSACTION_ID} />);
      await awaitEnabledControls();

      await user.click(scoreOption());
      await user.click(submitButton());

      await screen.findByRole('button', { name: IN_FLIGHT_BUTTON_NAME });
      dropFocusAsABrowserWould();
      expect(document.activeElement).toBe(document.body);

      await act(async () => {
        submission.reject(refusal(503, detail));
      });

      await waitFor(() => {
        expect(alertRegion()).toHaveTextContent(detail);
      });

      // Disabling the focused button during the request handed focus to
      // `document.body`, and re-enabling it does not hand it back. Without this the
      // interface reads as ready while the keyboard is nowhere, and reaching the
      // button again costs several Tab presses back past the star group and the
      // textarea.
      await waitFor(() => {
        expect(document.activeElement).toBe(submitButton());
      });

      expect(submitButton()).toBeEnabled();
    });

    it('leaves focus alone after a retryable failure when the user has moved on', async () => {
      const user = userEvent.setup();
      const submission = deferred<Rating>();
      // Retryable for the same reason as the test above: this asserts the OTHER
      // half of the restoration contract, so it has to exercise the path that
      // restores.
      const detail =
        'The service could not reach its datastore. This is temporary - ' +
        'please retry shortly.';

      mockedFetchEligibility.mockResolvedValue(decision());
      mockedSubmitRating.mockImplementation(() => submission.promise);

      // A control OUTSIDE the form, because everything inside it is disabled while
      // a submission is in flight — so somewhere else on the page is the only place
      // a user can actually be during the request, and it is where they go when
      // they Tab away rather than waiting.
      render(
        <>
          <RatingSubmissionForm transactionId={TRANSACTION_ID} />
          <button type="button">Somewhere else</button>
        </>,
      );
      await awaitEnabledControls();

      await user.click(scoreOption());
      await user.click(submitButton());
      await screen.findByRole('button', { name: IN_FLIGHT_BUTTON_NAME });

      const elsewhere = screen.getByRole('button', { name: /somewhere else/i });
      elsewhere.focus();

      await act(async () => {
        submission.reject(refusal(503, detail));
      });

      await waitFor(() => {
        expect(alertRegion()).toHaveTextContent(detail);
      });

      // The other half of the contract, and the reason the failure paths test the
      // document rather than moving focus unconditionally: being yanked back to a
      // button you deliberately left is worse than the loss the restoration fixes.
      // A component that focused on every failure would fail here and nowhere else.
      expect(document.activeElement).toBe(elsewhere);
    });

    it('notifies the parent exactly once, with the rating the server created', async () => {
      const user = userEvent.setup();
      const rating = created();
      const onSubmitted = vi.fn();

      mockedFetchEligibility.mockResolvedValue(decision());
      mockedSubmitRating.mockResolvedValue(rating);

      render(
        <RatingSubmissionForm
          transactionId={TRANSACTION_ID}
          onSubmitted={onSubmitted}
        />,
      );
      await awaitEnabledControls();

      await user.click(scoreOption());
      await user.click(submitButton());
      await awaitConfirmation();

      expect(onSubmitted).toHaveBeenCalledTimes(1);

      // Identity, not equality: the parent receives the SERVER's rating object so
      // it can re-read whatever it derived from the previous state — an eligibility
      // decision that is now `alreadyRated`, an aggregate that has moved.
      expect(onSubmitted.mock.calls[0][0]).toBe(rating);
    });

    it('keeps a parent refresh failure away from the user when the callback throws', async () => {
      const user = userEvent.setup();

      mockedFetchEligibility.mockResolvedValue(decision());
      mockedSubmitRating.mockResolvedValue(created());

      const onSubmitted = vi.fn(() => {
        throw new Error('the parent refresh blew up');
      });

      render(
        <RatingSubmissionForm
          transactionId={TRANSACTION_ID}
          onSubmitted={onSubmitted}
        />,
      );
      await awaitEnabledControls();

      await user.click(scoreOption());
      await user.click(submitButton());
      await awaitConfirmation();

      // The rating WAS created, so the confirmation stands and the assertive region
      // stays silent. Reporting "your rating could not be submitted" here would be
      // false, and the remedy it invites — submitting again — could only be refused
      // with a 409 by the one-rating-per-rater rule.
      expect(statusRegion()).toHaveTextContent(recordedSentence(SELECTED_SCORE));
      expect(alertRegion()).toBeEmptyDOMElement();

      // It is a defect in the parent, so it reaches a developer instead.
      const records = logsFor(CALLBACK_LOG);
      expect(records).toHaveLength(1);
      expect(records[0][1]).toMatchObject({
        message: 'the parent refresh blew up',
      });

      // And it is NOT reported as a submission failure, which would send whoever
      // reads the log looking at the wrong request.
      expect(logsFor(SUBMISSION_LOG)).toHaveLength(0);
    });

    it('keeps a parent refresh failure away from the user when the callback rejects', async () => {
      const user = userEvent.setup();

      mockedFetchEligibility.mockResolvedValue(decision());
      mockedSubmitRating.mockResolvedValue(created());

      // The prop is declared to return `void`, and TypeScript admits an `async`
      // function wherever a `void`-returning one is expected — which a parent whose
      // refresh is a network read will very likely write. An unawaited rejection
      // escapes a surrounding `try` entirely and surfaces as an unhandled promise
      // rejection, so the returned promise has to be caught explicitly.
      const onSubmitted = vi.fn(() =>
        Promise.reject(new Error('the parent refresh rejected')),
      );

      render(
        <RatingSubmissionForm
          transactionId={TRANSACTION_ID}
          /*
           * Cast because the prop's declared type is `void`-returning and this
           * deliberately returns a promise — which is exactly the mismatch the
           * component is written to survive. Writing the cast here keeps that
           * intent visible rather than hiding it behind a permissive prop type.
           */
          onSubmitted={onSubmitted as unknown as (rating: Rating) => void}
        />,
      );
      await awaitEnabledControls();

      await user.click(scoreOption());
      await user.click(submitButton());
      await awaitConfirmation();

      await waitFor(() => {
        expect(logsFor(CALLBACK_LOG)).toHaveLength(1);
      });

      expect(logsFor(CALLBACK_LOG)[0][1]).toMatchObject({
        message: 'the parent refresh rejected',
      });

      // The user still sees the truth: their rating was recorded.
      expect(statusRegion()).toHaveTextContent(recordedSentence(SELECTED_SCORE));
      expect(alertRegion()).toBeEmptyDOMElement();
    });

    it('starts over when the transaction changes, discarding the previous confirmation and entry', async () => {
      const user = userEvent.setup();

      mockedFetchEligibility.mockResolvedValue(decision());
      mockedSubmitRating.mockResolvedValue(created());

      const { rerender } = render(
        <RatingSubmissionForm transactionId={TRANSACTION_ID} />,
      );
      await awaitEnabledControls();

      await user.click(scoreOption());
      fireEvent.change(reviewField(), { target: { value: ACCEPTED_REVIEW } });
      await user.click(submitButton());
      await awaitConfirmation();

      rerender(<RatingSubmissionForm transactionId={OTHER_TRANSACTION_ID} />);
      await awaitEnabledControls();

      // A different transaction is a different rating, so nothing may survive: a
      // confirmation shown against the wrong transaction is worse than showing
      // nothing at all.
      expect(statusRegion()).toBeEmptyDOMElement();

      // The controls are back, and empty.
      expect(submitButton()).toBeInTheDocument();
      expect(reviewField()).toHaveValue('');
      expect(
        screen
          .getAllByRole('radio')
          .filter((option) => option.getAttribute('aria-checked') === 'true'),
      ).toHaveLength(0);

      // With no score chosen the control is correctly unavailable again, rather
      // than carrying over the previous submission's readiness.
      expect(submitButton()).toBeDisabled();

      // Each transaction was asked about on its own behalf.
      expect(mockedFetchEligibility).toHaveBeenNthCalledWith(1, TRANSACTION_ID);
      expect(mockedFetchEligibility).toHaveBeenNthCalledWith(
        2,
        OTHER_TRANSACTION_ID,
      );

      // And the previous submission is not repeated for the new transaction.
      expect(mockedSubmitRating).toHaveBeenCalledTimes(1);
    });
  });


  describe('local refusals', () => {
    it('refuses a review longer than the shared limit and makes no request', async () => {
      const user = userEvent.setup();

      mockedFetchEligibility.mockResolvedValue(decision());

      render(<RatingSubmissionForm transactionId={TRANSACTION_ID} />);
      await awaitEnabledControls();

      // A valid score, chosen through the real star control, so the only thing
      // wrong with this submission is the review's length.
      await user.click(scoreOption());

      const field = reviewField();

      // One change event carrying the whole value, rather than 2001 keystrokes:
      // typing character by character is prohibitively slow and tests the
      // browser's input handling rather than this form's bound. The real
      // `prepareReviewText` — DOMPurify sanitisation, Unicode normalisation and a
      // code-point count — runs on it, so the refusal below comes from the
      // production measurement and not from a stub.
      fireEvent.change(field, { target: { value: OVER_LONG_REVIEW } });

      // The user is TOLD, in the announced region, and told again by the counter,
      // and the field itself reports the state to assistive technology. Three
      // independent channels, none of which is colour.
      expect(statusRegion()).toHaveTextContent(OVER_LIMIT_FRAGMENT);
      expect(counterFor(field)).toHaveTextContent(
        overLimitCounterText(REVIEW_MAX_LENGTH + 1),
      );
      expect(field).toHaveAttribute('aria-invalid', 'true');

      // The visible gate: there is nothing to press while the review is too long.
      expect(submitButton()).toBeDisabled();

      // The gate behind it. Submitting the form directly bypasses the disabled
      // button exactly as a stray Enter key or a re-enabled control would, and the
      // handler must still refuse. A form that relied on the disabled attribute
      // alone would send the request here.
      fireEvent.submit(formOf(field));

      // Yields to the microtask queue before the final assertion, so a request
      // that had been started and awaited would already have been recorded.
      await waitFor(() => {
        expect(statusRegion()).toHaveTextContent(OVER_LIMIT_FRAGMENT);
      });

      // The point of the case: the client bound stops the REQUEST, rather than
      // merely styling the field and letting the server refuse it.
      expect(mockedSubmitRating).not.toHaveBeenCalled();

      // And nothing is written to the console: a local refusal is a normal outcome
      // of a user typing, not a fault to report.
      expect(logsFor(SUBMISSION_LOG)).toHaveLength(0);
    });

    it('accepts a review of exactly the limit, which the bound must not exclude', async () => {
      const user = userEvent.setup();

      mockedFetchEligibility.mockResolvedValue(decision());
      mockedSubmitRating.mockResolvedValue(created());

      render(<RatingSubmissionForm transactionId={TRANSACTION_ID} />);
      await awaitEnabledControls();

      await user.click(scoreOption());

      const field = reviewField();
      fireEvent.change(field, { target: { value: AT_LIMIT_REVIEW } });

      // The other side of the off-by-one. A bound written `>=` instead of `>` fails
      // exactly here and nowhere else, and it would refuse text the server accepts —
      // the one direction a convenience layer must never fail in.
      expect(counterFor(field)).toHaveTextContent(
        counterText(REVIEW_MAX_LENGTH),
      );
      expect(field).not.toHaveAttribute('aria-invalid');
      expect(statusRegion()).toBeEmptyDOMElement();
      expect(submitButton()).toBeEnabled();

      await user.click(submitButton());
      await awaitConfirmation();

      expect(mockedSubmitRating).toHaveBeenCalledWith({
        transactionId: TRANSACTION_ID,
        score: SELECTED_SCORE,
        review: AT_LIMIT_REVIEW,
      });
    });

    it('refuses a submission with no score, names the scale, and makes no request', async () => {
      mockedFetchEligibility.mockResolvedValue(decision());

      render(<RatingSubmissionForm transactionId={TRANSACTION_ID} />);
      await awaitEnabledControls();

      // The button is correctly unavailable with no score chosen, so the event is
      // dispatched at the form — the same route a stray Enter key or a
      // `requestSubmit()` would take, and the reason the handler re-checks rather
      // than trusting the control's `disabled` attribute.
      expect(submitButton()).toBeDisabled();
      fireEvent.submit(formOf(submitButton()));

      await waitFor(() => {
        expect(alertRegion()).toHaveTextContent(SCORE_REQUIRED_MESSAGE);
      });

      // A rating with no score is not a rating, so nothing is sent.
      expect(mockedSubmitRating).not.toHaveBeenCalled();
    });

    it('drops the stored refusal as soon as the payload changes', async () => {
      const user = userEvent.setup();

      mockedFetchEligibility.mockResolvedValue(decision());

      render(<RatingSubmissionForm transactionId={TRANSACTION_ID} />);
      await awaitEnabledControls();

      fireEvent.submit(formOf(submitButton()));

      await waitFor(() => {
        expect(alertRegion()).toHaveTextContent(SCORE_REQUIRED_MESSAGE);
      });

      // Choosing a score makes that sentence false, and an assertive region that
      // kept asserting it would interrupt a screen reader with a stale claim.
      await user.click(scoreOption());

      expect(alertRegion()).toBeEmptyDOMElement();
      expect(submitButton()).toBeEnabled();
    });

    it('refuses a transaction reference the shared schema rejects, without a request', async () => {
      const user = userEvent.setup();

      mockedFetchEligibility.mockResolvedValue(decision());

      // An empty reference fails `RatingCreateSchema`'s own `min(1)` rule, so the
      // real `validateRatingInput` raises before the request is built. This is the
      // path that turns a Zod issue into copy written for a person: Zod's own
      // message names a schema and a path, and rendering that would put the shape
      // of the API in front of somebody rating a car.
      render(<RatingSubmissionForm transactionId="" />);
      await awaitEnabledControls();

      await user.click(scoreOption());
      await user.click(submitButton());

      await waitFor(() => {
        expect(alertRegion()).toHaveTextContent(INVALID_REFERENCE_MESSAGE);
      });

      expect(mockedSubmitRating).not.toHaveBeenCalled();

      // The issues reach a developer as PATHS and CODES, and never as the offending
      // value: a validation failure must not copy a review body into a log. Both
      // are reported because an empty reference breaks two of the field's rules —
      // its minimum length, and the path-safety refinement that refuses `""`
      // outright — and the log is a developer's record of what was wrong rather
      // than a summary of the first thing noticed.
      const records = logsFor(SUBMISSION_LOG);
      expect(records).toHaveLength(1);
      expect(records[0][1]).toEqual({
        validation: [
          { path: 'transactionId', code: 'too_small' },
          { path: 'transactionId', code: 'custom' },
        ],
      });
    });
  });

  describe('server refusals', () => {
    /**
     * Every status `POST /api/ratings` can answer with, paired with the exact
     * sentence the server sends.
     *
     * The strings are the backend's own, pinned here as literals rather than
     * imported: the whole value of this table is that it fails if the API's
     * published wording changes or if two messages are swapped, which an
     * implementation-derived expectation cannot detect.
     */
    const SERVER_REFUSALS: ReadonlyArray<{
      readonly name: string;
      readonly status: number;
      readonly detail: string;
    }> = [
      {
        name: 'an unusable session (401)',
        status: 401,
        detail: 'Could not validate credentials',
      },
      {
        name: 'an unverified rater (403, requirement R1)',
        status: 403,
        detail: 'Only verified users can submit ratings',
      },
      {
        name: 'a caller who is not a party to the transaction (403, requirement R2)',
        status: 403,
        detail: 'You are not a party to this transaction',
      },
      {
        name: 'an unknown transaction (404)',
        status: 404,
        detail: 'Transaction not found',
      },
      {
        name: 'a second rating for the same transaction (409)',
        status: 409,
        detail: 'You have already rated this transaction',
      },
      {
        name: 'a transaction that has not completed (409)',
        status: 409,
        detail: 'Ratings require a completed transaction',
      },
      {
        name: 'a transaction record a rating cannot be built from (409)',
        status: 409,
        detail:
          'This transaction is missing information a rating requires, so it cannot be rated until its record is repaired',
      },
    ];

    SERVER_REFUSALS.forEach(({ name, status, detail }) => {
      it(`renders the server's own explanation of ${name}`, async () => {
        const user = userEvent.setup();

        mockedFetchEligibility.mockResolvedValue(decision());
        mockedSubmitRating.mockRejectedValue(refusal(status, detail));

        render(<RatingSubmissionForm transactionId={TRANSACTION_ID} />);
        await awaitEnabledControls();

        await user.click(scoreOption());
        await user.click(submitButton());

        // Exact equality on the server's own sentence. The server is the only
        // party that can explain a specific refusal, so a form that paraphrased it
        // — or fell back to its own generic copy — would tell the user something
        // other than what happened.
        //
        // Asserted in the POLITE region, and that placement is the contract for
        // these four statuses rather than an accident. 401, 403, 404 and 409 are
        // TERMINAL: each one proves the caller may not rate this transaction at
        // all, so the refusal is recorded as the ineligible decision it is - the
        // controls stay inert and the sentence becomes the standing explanation
        // beside them, exactly as it would read had the eligibility check itself
        // returned that reason a moment earlier. Announcing it assertively and
        // re-enabling the button would invite an attempt that cannot succeed; a
        // 409 in particular is enforced by a document-ID collision, so a retry is
        // refused for the same reason forever.
        await waitFor(() => {
          expect(statusRegion().textContent).toBe(detail);
        });

        // One attempt: a refusal must not be retried behind the user's back, least
        // of all a 409, where the retry would be refused for the same reason.
        expect(mockedSubmitRating).toHaveBeenCalledTimes(1);

        // Nothing is claimed twice: the assertive region stays empty, because the
        // explanation lives in the standing one, and the controls are inert.
        expect(alertRegion()).toBeEmptyDOMElement();
        expect(submitButton()).toBeDisabled();

        // And the keyboard is left somewhere real. Disabling the focused button
        // during the request handed focus to `document.body`; since the button is
        // not coming back, focus goes to the sentence that explains why.
        await waitFor(() => {
          expect(document.activeElement).toBe(statusRegion());
        });

        // The failure is logged with its status and detail, and as a flat record —
        // the axios error carries the bearer token on `config.headers`.
        const records = logsFor(SUBMISSION_LOG);
        expect(records).toHaveLength(1);
        expect(records[0][1]).toMatchObject({ status, detail });
      });
    });

    it('renders the server refusal of an out-of-range score verbatim and submits no identity claim', async () => {
      const user = userEvent.setup();

      // `backend/app/schema/rating.py` bounds `score` with `conint(strict=True,
      // ge=settings.RATING_MIN, le=settings.RATING_MAX)`, so such a request is
      // refused by Pydantic before any handler runs, and FastAPI reports that as a
      // 422. The detail is Pydantic v1's own wording for an exceeded `le` bound,
      // with the ceiling interpolated from the shared constant so the fixture
      // follows the scale instead of pinning a number to it.
      const detail = OUT_OF_RANGE_DETAIL;

      mockedFetchEligibility.mockResolvedValue(decision());
      mockedSubmitRating.mockRejectedValue(refusal(422, detail));

      render(<RatingSubmissionForm transactionId={TRANSACTION_ID} />);
      await awaitEnabledControls();

      await user.click(scoreOption());
      fireEvent.change(reviewField(), { target: { value: ACCEPTED_REVIEW } });

      await user.click(submitButton());

      expect(await screen.findByText(detail)).toBeInTheDocument();

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
        expect(submitButton()).toBeEnabled();
      });
    });

    it('joins the issue messages when the server rejects the body before any handler runs', async () => {
      const user = userEvent.setup();

      mockedFetchEligibility.mockResolvedValue(decision());
      // The OTHER shape FastAPI emits `detail` in. A refusal raised by the router
      // is a string; a request Pydantic rejected is an ARRAY of issue objects,
      // because the backend registers no custom `RequestValidationError` handler.
      // Reading only the string case meant every such 422 degraded to axios's own
      // "Request failed with status code 422" and told the user nothing about what
      // to change.
      mockedSubmitRating.mockRejectedValue(
        validationRefusal([
          {
            loc: ['body', 'score'],
            msg: 'value is not a valid integer',
            type: 'type_error.integer',
          },
          {
            loc: ['body', 'review'],
            msg: 'value is not a valid integer',
            type: 'type_error.integer',
          },
          {
            loc: ['body', 'transaction_id'],
            msg: 'field required',
            type: 'value_error.missing',
          },
        ]),
      );

      render(<RatingSubmissionForm transactionId={TRANSACTION_ID} />);
      await awaitEnabledControls();

      await user.click(scoreOption());
      await user.click(submitButton());

      // Every distinct message, joined; the repeat collapsed. `loc` is deliberately
      // absent — it is a JSON pointer written for a developer, and the messages
      // read as sentences without it.
      await waitFor(() => {
        expect(alertRegion().textContent).toBe(
          'value is not a valid integer; field required',
        );
      });

      // No pointer, no field name, no type code reaches the screen.
      expect(alertRegion().textContent).not.toContain('body');
      expect(alertRegion().textContent).not.toContain('type_error');
    });

    it('reports a response it cannot interpret in one stable sentence, and leaks none of the diagnosis', async () => {
      const user = userEvent.setup();

      mockedFetchEligibility.mockResolvedValue(decision());
      // The server ACCEPTED the request and answered with something that does not
      // match the rating contract, which is a different fact from a refusal and is
      // raised as a different error type.
      mockedSubmitRating.mockRejectedValue(
        new RatingContractError(
          'POST /api/ratings',
          'createdAt: expected date, received string',
          new Error('decode failed'),
        ),
      );

      render(<RatingSubmissionForm transactionId={TRANSACTION_ID} />);
      await awaitEnabledControls();

      await user.click(scoreOption());
      await user.click(submitButton());

      await waitFor(() => {
        expect(alertRegion().textContent).toBe(CONTRACT_ERROR_MESSAGE);
      });

      // The assertion that matters: the endpoint label and the schema diagnosis
      // stay off the screen. A user cannot act on "createdAt: expected date", and
      // an attacker should not be handed a description of the API's shape by a
      // malformed response.
      expect(document.body.textContent).not.toContain('POST /api/ratings');
      expect(document.body.textContent).not.toContain('createdAt');

      // The diagnosis is not lost — it goes where developers read it.
      expect(logsFor(SUBMISSION_LOG)).toHaveLength(1);
    });
  });

  describe('the outbound payload', () => {
    /**
     * The payload the client built, read from the recorded call.
     *
     * Reading the argument is the only way to see what was sent: the request body
     * is not observable from the DOM, and it is the half of this component's
     * behaviour a rendering assertion can never reach.
     */
    const sentPayload = (): { review?: unknown } =>
      mockedSubmitRating.mock.calls[0][0] as { review?: unknown };

    it('sends the sanitised, normalised review the counter measured, and nothing else', async () => {
      const user = userEvent.setup();

      mockedFetchEligibility.mockResolvedValue(decision());
      mockedSubmitRating.mockResolvedValue(created());

      render(<RatingSubmissionForm transactionId={TRANSACTION_ID} />);
      await awaitEnabledControls();

      await user.click(scoreOption());

      const field = reviewField();
      fireEvent.change(field, { target: { value: MESSY_REVIEW } });

      // The counter measures the PREPARED text, which is the value that will be
      // sent and stored. Measuring the raw text instead — which this component
      // used to do — charged the user for markup that was never going to leave the
      // browser, and at the boundary disabled the submit button over a string
      // nobody would receive.
      expect(counterFor(field)).toHaveTextContent(
        counterText(MESSY_REVIEW_ON_THE_WIRE.length),
      );

      await user.click(submitButton());
      await awaitConfirmation();

      // Markup removed, the control character and the zero-width space stripped,
      // CRLF folded, the blank-line run collapsed, the ends trimmed — and the
      // result asserted by exact equality, so any one of those transformations
      // going missing fails here.
      expect(mockedSubmitRating).toHaveBeenCalledWith({
        transactionId: TRANSACTION_ID,
        score: SELECTED_SCORE,
        review: MESSY_REVIEW_ON_THE_WIRE,
      });
    });

    it('sends no review at all when the text was entirely markup', async () => {
      const user = userEvent.setup();

      mockedFetchEligibility.mockResolvedValue(decision());
      mockedSubmitRating.mockResolvedValue(created());

      render(<RatingSubmissionForm transactionId={TRANSACTION_ID} />);
      await awaitEnabledControls();

      await user.click(scoreOption());
      fireEvent.change(reviewField(), { target: { value: MARKUP_ONLY_REVIEW } });

      // Nothing survives sanitisation, so the counter reads zero rather than the
      // length of the markup the user pasted.
      expect(counterFor(reviewField())).toHaveTextContent(counterText(0));

      await user.click(submitButton());
      await awaitConfirmation();

      const payload = sentPayload();

      // `review` is OPTIONAL on the wire, and omitting it says "no review was
      // written" while `""` says "one was written and is blank". A value that
      // sanitises to nothing is the first of those.
      expect(payload.review).toBeUndefined();

      // Which is what reaches the server: `JSON.stringify` — the serialisation
      // axios performs — drops an `undefined` member entirely, so the request body
      // carries no `review` key.
      expect(JSON.stringify(payload)).not.toContain('review');

      // And no fragment of the script survived anywhere in the payload.
      expect(JSON.stringify(payload)).not.toContain('script');
      expect(JSON.stringify(payload)).not.toContain('alert');
    });

    it('composes a decomposed grapheme the way the server counts and stores it', async () => {
      const user = userEvent.setup();

      mockedFetchEligibility.mockResolvedValue(decision());
      mockedSubmitRating.mockResolvedValue(created());

      render(<RatingSubmissionForm transactionId={TRANSACTION_ID} />);
      await awaitEnabledControls();

      await user.click(scoreOption());

      const field = reviewField();
      fireEvent.change(field, { target: { value: DECOMPOSED_REVIEW } });

      // Six, not seven. The server composes to NFC before measuring, so a client
      // that counted the raw code points would charge for the combining mark and
      // disagree with the stored length.
      expect(counterFor(field)).toHaveTextContent(
        counterText(COMPOSED_REVIEW.length),
      );

      await user.click(submitButton());
      await awaitConfirmation();

      expect(mockedSubmitRating).toHaveBeenCalledWith({
        transactionId: TRANSACTION_ID,
        score: SELECTED_SCORE,
        review: COMPOSED_REVIEW,
      });
    });

    it('sends no review key when nothing was written', async () => {
      const user = userEvent.setup();

      mockedFetchEligibility.mockResolvedValue(decision());
      mockedSubmitRating.mockResolvedValue(created());

      render(<RatingSubmissionForm transactionId={TRANSACTION_ID} />);
      await awaitEnabledControls();

      // The review is optional (F010-2 is a capability, not an obligation), so a
      // score on its own is a complete rating.
      await user.click(scoreOption());
      await user.click(submitButton());
      await awaitConfirmation();

      const payload = sentPayload();

      expect(payload.review).toBeUndefined();
      expect(JSON.stringify(payload)).not.toContain('review');
    });
  });
});
