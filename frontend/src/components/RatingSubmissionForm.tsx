import React, {
  useCallback,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
} from 'react';
import { ZodError } from 'zod';

import StarRatingInput, { type StarRatingInputHandle } from './StarRatingInput';
import {
  CONTRACT_ERROR_MESSAGE,
  RatingContractError,
  describeRequestFailure,
  fetchRatingEligibility,
  readServerDetail,
  submitRating,
} from '../services/rating';
import { prepareReviewText, validateRatingInput } from '../utils/validation';

import { RATING_MAX, RATING_MIN, REVIEW_MAX_LENGTH } from '../schema/rating';
import type { EligibilityDecision, Rating } from '../schema/rating';

/**
 * The rating submission surface for the bidirectional peer reputation system.
 *
 * Implements F010-1 "User rating submission interface" and F010-2 "Written
 * review functionality" from `documentation/Software Requirements Specifications
 * (SRS).md`: a buyer rates the seller and the seller rates the buyer for a
 * purchase they both took part in. The score is collected by the sibling
 * `./StarRatingInput`; this component owns the optional written review, the
 * eligibility conversation with the server, the submission, and every way that
 * submission can fail.
 *
 * THIS IS WHERE TWO AUTHORIZATION GATES BECOME VISIBLE TO A PERSON
 * -----------------------------------------------------------------------------
 * Two rules govern who may rate: the rater must be a VERIFIED user, and the two
 * parties must be counterparties of the SAME completed transaction. Both are
 * enforced server-side and neither is knowable from anything on this client.
 *
 * So this form asks first. `GET /api/ratings/eligibility/{transactionId}`
 * returns the server's decision — `eligible`, a human-readable `reason`, the
 * derived `rateeId` and `direction`, and whether the caller has `alreadyRated` —
 * and the form renders the controls DISABLED WITH THAT REASON rather than
 * letting somebody compose a rating and only then be refused with a 403.
 *
 * That is not a nicety. WCAG 2.1 Level AA is a stated non-functional
 * requirement of this project, and a failure mode where the user cannot tell why
 * an action is unavailable is itself an accessibility failure. The eligibility
 * endpoint exists precisely so that this component never has to present one.
 *
 * THE SERVER'S WORDS ARE THE WORDS THE USER READS
 * -----------------------------------------------------------------------------
 * `EligibilityDecision.reason` and `HTTPException.detail` are deliberately the
 * SAME copy on the server — `backend/app/services/rating.py` reuses each domain
 * exception's message for both — and `../services/rating` rejects with the
 * original error so `error.response.data.detail` survives intact. Both are
 * therefore rendered verbatim here.
 *
 * There is consequently NO client-side status-to-message lookup table in this
 * file. Inventing local copy for 403 / 409 / 422 would create a second source of
 * wording for one decision, free to drift away from the first, and would tell
 * the user something subtly different from what actually happened. The only
 * strings this component authors are for conditions the server never saw: no
 * score chosen yet, and a review refused locally before it was sent.
 *
 * SERVER-SIDE VALIDATION IS AUTHORITATIVE; THE LOCAL CHECKS ARE A CONVENIENCE
 * -----------------------------------------------------------------------------
 * `documentation/Technical Specifications.md` L672-L675 puts server-side
 * validation of every user input first and casts client-side validation as UX.
 * That ordering is honoured literally: the local score and length checks exist
 * to save a round trip, and passing them is never treated as permission. Every
 * refusal the server can still return — 401, 403, 404, 409, 422 — is caught and
 * surfaced legibly. A form that assumed its own checks were sufficient would
 * present a successful-looking submission that had in fact been rejected.
 *
 * IDENTITY COMES FROM PROPS AND FROM THE SERVER, NEVER FROM THE STORE
 * -----------------------------------------------------------------------------
 * This component reads no Redux state at all: it subscribes to no selector,
 * dispatches no action, and imports nothing from the store module. Two reasons,
 * one principled and one practical.
 *
 * Principled: it does not need to. Every input to the decision — including who
 * the counterparty is and which direction the rating travels — is computed
 * SERVER-SIDE from the authenticated caller and returned on the eligibility
 * decision. A client-held notion of "who I am" could only ever be a second,
 * weaker copy of something the server already settled.
 *
 * Practical: the store cannot supply it. `../store/userSlice` exports only
 * `setUser`, `setLoading` and `setError`, so the current-user selector that
 * three existing components import from it DOES NOT EXIST, and `../store/index`
 * imports both reducers by name against default-only exports. Reaching into that
 * would add a new compile error to a new file for no gain.
 *
 * The side benefit is testability: this component renders with no `<Provider>`
 * wrapper and only `../services/rating` needs mocking.
 *
 * NEVER TRUST A CLIENT-SUPPLIED RELATIONSHIP CLAIM
 * -----------------------------------------------------------------------------
 * The submitted body is exactly `{ transactionId, score, review? }`. `raterId`,
 * `rateeId` and `direction` are NEVER sent: all three are derived server-side
 * from the authenticated caller and the cited transaction, which is what makes
 * both direction spoofing and self-rating structurally impossible rather than
 * merely validated against. `EligibilityDecision` does hand this component
 * `rateeId` and `direction`; they are display context and nothing more.
 *
 * REPUTATION IS APPEND-ONLY
 * -----------------------------------------------------------------------------
 * After a successful submission the controls are replaced by a confirmation and
 * there is NO edit affordance and NO delete affordance anywhere. A submitted
 * score is never rewritten — no endpoint accepts a rewrite, and `../schema/
 * rating` deliberately declares no update shape — so offering a control that
 * implied otherwise would be a lie about what the system can do.
 *
 * PUBLICATION IS DOUBLE-BLIND, AND SAYING SO IS PART OF THE FEATURE
 * -----------------------------------------------------------------------------
 * A new rating is created UNPUBLISHED. It becomes visible when the counterparty
 * submits theirs, or when the rating window closes — whichever comes first. That
 * reveal is what removes the incentive for review extortion, and it means a
 * perfectly successful submission legitimately shows up nowhere for a while.
 *
 * Left unexplained, that reads exactly like a failed submission. So the
 * confirmation states it in plain language whenever `isPublished` is false. This
 * is a product requirement, not decoration.
 *
 * STYLING
 * -----------------------------------------------------------------------------
 * Tailwind utilities from the default scale only. `../../tailwind.config.js`
 * ships an intentionally empty `theme.extend`, so the default scale is this
 * project's entire token source, and no component library is installed — there
 * is no `Button`, `TextArea` or `Alert` to reuse, so semantic HTML delivers the
 * accessibility a library component would have supplied. No hardcoded colour,
 * no pixel dimension and no `style` attribute appears below.
 */
interface RatingSubmissionFormProps {
  /**
   * The completed transaction being rated. Sent as-is to the eligibility and
   * submission endpoints, which decide independently whether it exists, whether
   * the caller is a party to it, and whether it has reached `completed`.
   */
  transactionId: string;

  /**
   * Called once with the created rating after a successful submission.
   *
   * The parent screen uses it to re-read whatever it derived from the previous
   * state — an eligibility decision that is now `alreadyRated`, or a reputation
   * aggregate that may have moved. Optional, because the form is complete and
   * correct on its own; a parent that needs nothing refreshed simply omits it.
   */
  onSubmitted?: (rating: Rating) => void;
}

/**
 * Shown when the eligibility request itself fails and the failure carries no
 * server message — a network outage, a CORS refusal, a 5xx with an empty body.
 *
 * Deliberately does NOT claim the user is ineligible. "We could not check" and
 * "you may not" are different facts, and reporting the second when only the
 * first is known would wrongly tell a legitimate rater they are barred.
 */
const ELIGIBILITY_FAILURE_MESSAGE =
  'We could not check whether you can rate this transaction. Please try again.';

/**
 * Shown when a submission fails and no more specific explanation is available.
 */
const SUBMISSION_FAILURE_MESSAGE =
  'Your rating could not be submitted. Please try again.';

/**
 * Shown when the server reports the caller is ineligible but sends no reason.
 *
 * `EligibilityDecision.reason` is typed `string | null` and the server always
 * populates it alongside `eligible: false`, so this is a contract backstop
 * rather than an expected path. Without it an ineligible caller would face a
 * silently disabled control — the exact WCAG failure this form exists to avoid.
 */
const UNAVAILABLE_MESSAGE = 'You cannot rate this transaction right now.';

/**
 * Read a server-authored explanation out of a rejected request.
 *
 * `../services/rating` rejects with the ORIGINAL axios error, so a refusal
 * arrives carrying `response.data.detail` — the server's own human-readable
 * message for a 401, 403, 404, 409 or 422. That string is what the interface
 * renders, which is the whole reason this helper exists rather than a table of
 * locally invented copy keyed by status code.
 *
 * THE READING ITSELF BELONGS TO `../services/rating`, VIA `readServerDetail`.
 * Two reasons, and the second is the one that bit. First, one owner: the service
 * already needs the same extraction to build its sanitised log line, and two
 * hand-rolled readers of one wire shape drift apart. Second, FastAPI emits `detail`
 * in TWO shapes and a local reader only ever handled one. A refusal raised by the
 * router is a string; a request Pydantic rejected before any handler ran is an
 * ARRAY of issue objects — `[{loc, msg, type}]` — because the backend registers no
 * custom `RequestValidationError` handler. Reading only the string case meant every
 * 422 degraded to axios's own "Request failed with status code 422", so a score
 * outside the scale or an over-long review told the user nothing about what to
 * change. `readServerDetail` handles both and reports the issue messages.
 *
 * What remains here is the FALLBACK ORDER, which is a presentation decision and so
 * belongs to the component: the server's words first, then the error's own message
 * — which for a `RatingContractError` or a transport failure is genuinely
 * informative and names the endpoint — and only then the generic copy.
 *
 * WHAT IS DELIBERATELY NOT RENDERED: AN ARBITRARY `error.message`
 * -----------------------------------------------------------------------------
 * This used to fall back to `error.message` for any `Error`, reasoning that it
 * was "more informative than the generic fallback". It was more informative to a
 * developer and worse for everyone else, because the messages that reach that
 * branch are internal: a `RatingContractError` message once carried an endpoint
 * label and a `ZodError` issue list, and an axios transport failure carries
 * strings like `"Request failed with status code 500"` or `"Network Error"`.
 * Rendering those put the shape of the API on screen in front of somebody who was
 * rating a car, and told them nothing they could do about it.
 *
 * So there are now exactly three outcomes, in order of who authored the words:
 *
 * 1. The server's `detail`, which is prose written for a user and is the only
 *    message that can explain a specific refusal — "Only verified users can
 *    submit ratings", "You have already rated this transaction".
 * 2. `CONTRACT_ERROR_MESSAGE` for a `RatingContractError`, which is a real defect
 *    but not one a user can act on beyond retrying. The endpoint and the schema
 *    detail stay on the error's `diagnostics` and in the developer console.
 * 3. The caller's stable fallback for everything else, including any transport
 *    failure.
 *
 * @param error The rejection value, of genuinely unknown shape.
 * @param fallback Returned when no user-facing message can be found.
 * @returns The server's message, else a stable user-facing sentence.
 */
const extractDetail = (error: unknown, fallback: string): string => {
  const detail = readServerDetail(error);

  if (detail !== null) {
    return detail;
  }

  // A 2xx response that did not match the contract. The constant is used rather
  // than `error.message` even though they are equal today, so that this
  // component's rendering does not silently start displaying internals again if
  // that message is ever made specific for developers.
  if (error instanceof RatingContractError) {
    return CONTRACT_ERROR_MESSAGE;
  }

  return fallback;
};

/**
 * Turn a local validation failure into something worth reading.
 *
 * `validateRatingInput` parses through `RatingCreateSchema` and lets `ZodError`
 * propagate, so the issues arriving here describe a payload this form built. Zod
 * messages are accurate but written for a developer; each field gets copy
 * written for the person holding the keyboard, composed from the SHARED bounds
 * so it can never contradict the schema that produced the failure or the
 * character counter rendered beside the textarea.
 *
 * The review case is a BACKSTOP rather than an expected path, and it is worth
 * saying why. The bound is applied to the sanitised text — the value that is
 * actually sent and stored — and sanitising to plain text can only ever shorten a
 * string, so a review the counter showed as within the limit cannot be refused
 * here for length. The branch is kept because a programmatic value change can
 * still reach the submit handler, and because a schema failure should never fall
 * through to a message about something else.
 *
 * Zod's own wording is never rendered at all, and neither is the unstyled
 * `error.message`, which is a JSON dump of every issue. Every field this schema
 * declares has copy of its own below, and anything outside that set is an
 * internal divergence answered with the stable fallback rather than with a
 * sentence written for a developer.
 *
 * @param error The validation failure raised while building the payload.
 * @returns One sentence naming what to change.
 */
const describeValidationFailure = (error: ZodError): string => {
  const issue = error.issues.length > 0 ? error.issues[0] : undefined;

  if (issue === undefined) {
    return SUBMISSION_FAILURE_MESSAGE;
  }

  const field = issue.path.length > 0 ? issue.path[0] : undefined;

  if (field === 'score') {
    return `Choose a whole number of stars from ${RATING_MIN} to ${RATING_MAX}.`;
  }

  if (field === 'review') {
    return `Your review must be ${REVIEW_MAX_LENGTH} characters or fewer. Please shorten it.`;
  }

  if (field === 'transactionId') {
    return 'This transaction cannot be rated because its reference is not valid.';
  }

  // Unreachable for a well-formed payload: the three branches above cover every
  // field `RatingCreateSchema` declares. So arriving here means the schema and
  // this component have diverged, which is an internal defect - and `issue.message`
  // for an unexpected path is Zod's own developer wording ("Expected date,
  // received string"). The stable sentence is shown instead, and the issue paths
  // reach the developer console through `describeSubmissionForLog`.
  return SUBMISSION_FAILURE_MESSAGE;
};

/**
 * Describe any submission failure, local or remote.
 *
 * Order is deliberate. A `ZodError` never reached the network, so it is answered
 * with local copy about the payload. Everything else did reach the network, so
 * the server's own explanation is preferred over anything written here.
 */
const describeSubmissionFailure = (error: unknown): string =>
  error instanceof ZodError
    ? describeValidationFailure(error)
    : extractDetail(error, SUBMISSION_FAILURE_MESSAGE);

/**
 * Reduce a submission failure to what is safe AND useful to log.
 *
 * Two kinds of failure reach the submit handler's `catch`, and they need
 * different log records rather than one compromise.
 *
 * A `ZodError` was raised by `validateRatingInput` before any request was made,
 * so there is no axios `config` to redact and no request metadata to report; what
 * a developer needs is which field was refused. Only the issue PATH and CODE are
 * recorded — never `issue.message` and never the offending value — so a review
 * body cannot be copied into a log by a validation failure.
 *
 * Everything else did reach the network and arrives as an axios error carrying
 * the bearer token in `config.headers` and the request body in `config.data`, so
 * it goes through the client's own `describeRequestFailure` allow-list.
 *
 * @param error The rejection value from the submit handler.
 * @returns A flat record of primitives, safe to pass to a log sink.
 */
const describeSubmissionForLog = (error: unknown): Record<string, unknown> =>
  error instanceof ZodError
    ? {
        validation: error.issues.map((issue) => ({
          path: issue.path.join('.'),
          code: issue.code,
        })),
      }
    : describeRequestFailure(error);

/**
 * The one piece of copy for an over-long review, and it is deliberately CONSTANT.
 *
 * It quotes the limit and NOT the current length, which is the whole point. The
 * sentence used to interpolate the live count — "your review is 2001 characters" —
 * and it was rendered inside an assertive live region, so every keystroke past the
 * bound produced a different string and every different string INTERRUPTED a screen
 * reader mid-word. Someone editing a long review was talked over on every
 * character they typed, which is a worse failure than the one the message was
 * reporting.
 *
 * A constant fixes that by construction: the text is identical on the second
 * keystroke over the limit and on the two-hundredth, so the DOM does not change,
 * so nothing is re-announced. The threshold is announced ONCE, when it is crossed.
 *
 * The exact figure is not lost — it stays in the character counter beside the
 * field, which is associated by `aria-describedby` and is NOT a live region, so it
 * is read on demand rather than shouted. That is the right division: the count is
 * reference information a reader consults, while crossing the bound is an event.
 */
const OVER_LENGTH_REVIEW_MESSAGE =
  `Your review is over the ${REVIEW_MAX_LENGTH}-character limit once formatting is removed. Shorten it before submitting; the counter below the field shows the current length.`;

/**
 * Name the counterparty's ROLE from the server's decision, for display only.
 *
 * The eligibility decision carries a derived direction and ratee precisely
 * because only the server can work them out: it reads the cited transaction,
 * finds the caller on one side of it, and reports the other side. Surfacing the
 * role closes the loop for the user — "you are rating the seller" is the
 * difference between a form they understand and one they guess at.
 *
 * Read-only, and that boundary is the point. Neither value is ever placed in a
 * request body: they are derived server-side on every submission too, which is
 * what makes spoofing a direction, or naming oneself as the ratee, structurally
 * impossible rather than something a validator has to catch.
 *
 * Both are reported as null until the caller is confirmed a participant — an
 * unknown transaction, or a caller party to neither side, never gets that far —
 * so the empty string is the normal answer for an ineligible caller, and the
 * caller's own opaque identifier is deliberately never rendered.
 *
 * The parameter is the whole decision rather than the bare direction so that
 * `null` and "no decision yet" collapse into one branch here instead of forcing
 * every call site to unpack a possibly-absent object first.
 *
 * @param decision The server's decision, or `null` before one has arrived.
 * @returns A sentence naming the role, or `''` when it is not yet derivable.
 */
const describeCounterparty = (
  decision: EligibilityDecision | null,
): string => {
  if (decision === null) {
    return '';
  }

  if (decision.direction === 'buyer_to_seller') {
    return 'You are rating the seller for this purchase.';
  }

  if (decision.direction === 'seller_to_buyer') {
    return 'You are rating the buyer for this purchase.';
  }

  return '';
};

/**
 * Classes for the review textarea.
 *
 * `border-gray-500` rather than a lighter step: the boundary of an input is a
 * non-text visual indicator, which WCAG 1.4.11 holds to 3:1 against its
 * background. Against white, `gray-500` reaches roughly 4.8:1 while `gray-300`
 * manages about 1.6:1 — a border that looks tidy in a mock-up and is invisible
 * to a reader with low vision. The sibling star control reasons the same way
 * about its own colours, so the two agree.
 *
 * `focus:outline-none` is only safe because `focus:ring-2 focus:ring-offset-2`
 * replaces what it removes. Suppressing the native outline without substituting
 * a visible indicator is itself a WCAG failure, so these three always travel
 * together, and the ring colour matches the star control's so the focus
 * indicator is one consistent thing across the whole form.
 *
 * The disabled treatment changes the background as well as the cursor, so the
 * ineligible state is legible without relying on the cursor — which never
 * appears on a touch device at all.
 */
const TEXTAREA_CLASSES = [
  'w-full rounded-md border border-gray-500 p-2',
  'text-base text-gray-900 shadow-sm',
  'focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-blue-600',
  'disabled:cursor-not-allowed disabled:border-gray-400',
  'disabled:bg-gray-100 disabled:text-gray-500',
].join(' ');

/**
 * Classes for the submit button.
 *
 * White on `blue-600` clears 4.5:1 (roughly 5.1:1), matching the interactive
 * accent already used across the application's header. The disabled state drops
 * to a flat neutral: inactive controls are exempt from the contrast minimums,
 * and pairing it with `cursor-not-allowed` gives two independent signals.
 *
 * FOUR INTERACTIVE STATES, NOT THREE. `hover:` says "this is interactive",
 * `focus:` says "you are here", `disabled:` says "not now" — and `active:` says
 * "your press registered". That last one is the feedback a pointer user gets
 * between pressing and the request starting, and without it a slow submit feels
 * like a button that did nothing. `blue-800` is a step darker than the hover tone
 * so the two are distinguishable, and Tailwind emits `active` after `hover` so
 * the pressed tone wins while the pointer is down. `disabled:hover:bg-gray-400`
 * keeps the disabled treatment authoritative; `:active` cannot apply to a
 * disabled button at all, so no `disabled:active:` override is needed.
 *
 * `motion-safe:` scopes the colour transition to
 * `@media (prefers-reduced-motion: no-preference)`, so a reader who has asked
 * for reduced motion gets an instant change instead.
 */
const BUTTON_CLASSES = [
  'inline-flex items-center justify-center rounded-md',
  'bg-blue-600 px-4 py-2 text-base font-semibold text-white shadow-sm',
  'hover:bg-blue-700 active:bg-blue-800',
  'focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-blue-600',
  'disabled:cursor-not-allowed disabled:bg-gray-400 disabled:hover:bg-gray-400',
  'motion-safe:transition-colors motion-safe:duration-150 motion-safe:ease-out',
].join(' ');

/**
 * Classes for the "Check again" button that appears when the eligibility request
 * itself failed.
 *
 * A secondary treatment — bordered rather than filled — so it reads as a recovery
 * action beside the primary submit control rather than competing with it. The
 * border is `blue-600` for the same 3:1 non-text-contrast reason the textarea's
 * border is `gray-500`, and it carries the same four interactive states as the
 * submit button, including the `active:` pressed tone.
 */
const RETRY_BUTTON_CLASSES = [
  'inline-flex items-center justify-center self-start rounded-md',
  'border border-blue-600 px-3 py-1.5 text-sm font-semibold text-blue-700',
  'hover:bg-blue-50 active:bg-blue-100',
  'focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-blue-600',
  'disabled:cursor-not-allowed disabled:border-gray-400 disabled:text-gray-500',
  'disabled:hover:bg-transparent',
  'motion-safe:transition-colors motion-safe:duration-150 motion-safe:ease-out',
].join(' ');

const RatingSubmissionForm: React.FC<RatingSubmissionFormProps> = ({
  transactionId,
  onSubmitted,
}) => {
  /**
   * The chosen score, or `null` before anything is chosen. `null` is a real
   * state rather than a stand-in for zero: the star control renders it as
   * "No score selected" and submission stays blocked while it holds.
   */
  const [score, setScore] = useState<number | null>(null);

  /** Raw review text exactly as typed. Sanitised at submission, never here. */
  const [review, setReview] = useState('');

  /** The server's decision, or `null` until it arrives (or fails to). */
  const [eligibility, setEligibility] = useState<EligibilityDecision | null>(
    null,
  );

  /**
   * Starts `true`. The decision is unknown on the very first render, and
   * "unknown" must present as unavailable rather than as available: starting
   * `false` would flash an enabled control that the server may be about to
   * refuse, and a fast click would submit into a 403.
   */
  const [isLoadingEligibility, setIsLoadingEligibility] = useState(true);

  /** True for the duration of an in-flight submission. Blocks double posting. */
  const [isSubmitting, setIsSubmitting] = useState(false);

  /**
   * The created rating once one exists. Non-null is the terminal state of this
   * form: the controls are replaced by a confirmation, and nothing offers to
   * change or remove what was recorded.
   */
  const [submittedRating, setSubmittedRating] = useState<Rating | null>(null);

  /** Current error text, or `null`. Rendered in the assertive live region. */
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  /**
   * Whether the eligibility REQUEST failed, as opposed to succeeding with an
   * ineligible decision.
   *
   * Tracked separately because the two demand different affordances, and
   * conflating them is what made a transient network failure permanent. An
   * ineligible decision is an answer: the server has told the user why, and
   * asking again will say the same thing. A failed request is the absence of an
   * answer — a timeout, a 5xx, a dropped connection — and the only useful
   * response is to try once more. Without this flag the form could not tell which
   * state it was in, so it offered no retry and left every control disabled
   * forever; the reader was told "we could not check" and given no way to act on
   * it, which is the WCAG failure this component exists to avoid.
   */
  const [eligibilityFailed, setEligibilityFailed] = useState(false);

  /**
   * One generated base, suffixed per element.
   *
   * `useId` rather than a hardcoded string because two of these forms can share
   * a page — a buyer viewing two completed transactions — and duplicate ids
   * would point every label and every description at the first instance.
   */
  const baseId = useId();
  const headingId = `${baseId}-heading`;
  const reviewId = `${baseId}-review`;
  const counterId = `${baseId}-counter`;
  const statusId = `${baseId}-status`;

  /**
   * Sequence number of the most recently STARTED eligibility request.
   *
   * A boolean `active` flag scoped to the effect was enough while the effect was
   * the only thing that could issue this request. It is not enough now that a
   * retry button can issue one too: two in-flight requests would both believe
   * they were current, and the slower one — quite possibly the one for the
   * previous `transactionId`, or the failure the user just retried past — would
   * land last and win. A monotonic counter gives every request an identity, and a
   * response is applied only while its own number is still the latest. The
   * effect's cleanup bumps the counter, so an unmount or a changed id invalidates
   * everything already in flight and no state is set after unmount.
   */
  const eligibilityRequestRef = useRef(0);

  /**
   * The confirmation region, so focus can be MOVED to it once a rating exists.
   *
   * The polite `role="status"` paragraph carries the confirmation and is the only
   * element that is present both before and after the form unmounts, which is what
   * makes it the right destination — see `pendingFocus`.
   */
  const statusRef = useRef<HTMLParagraphElement | null>(null);

  /**
   * The star group, so focus can be moved INTO it after a successful retry.
   */
  const scoreGroupRef = useRef<StarRatingInputHandle | null>(null);

  /**
   * Where focus must go once the DOM reflects the change that removed it.
   *
   * WHY THIS EXISTS. Two transitions in this component delete the element the user
   * was standing on:
   *
   *   - a successful SUBMIT unmounts the whole form, taking the submit button with
   *     it, because a recorded rating is terminal and nothing offers to change it;
   *   - a successful RETRY unmounts the "Check again" button, because there is no
   *     longer anything to retry.
   *
   * When focus is inside an element that is removed, the browser moves it to
   * `document.body`. For a keyboard or screen-reader user that is not a small
   * inconvenience: their position in the document is gone, nothing is announced
   * where they now are, and the next Tab starts again from the top of the page —
   * WCAG 2.4.3 Focus Order, and the reason a live region alone is not sufficient
   * here. It also silently breaks the confirmation: a submission that "worked" left
   * the user nowhere, with no way to reach the sentence saying so except by
   * re-traversing the page.
   *
   * WHY IT IS STATE AND NOT A CALL IN THE HANDLER. The destination does not exist
   * yet at the moment the decision is made. `setSubmittedRating(created)` schedules
   * a render; the confirmation is only focusable after that render commits. So the
   * handler records an INTENT and the effect below acts on it once React has
   * flushed, which is also what makes the behaviour correct under batching and
   * under React 18's double-invoked development renders.
   *
   * `null` means "nothing to move", which is the normal state.
   */
  const [pendingFocus, setPendingFocus] = useState<
    'confirmation' | 'controls' | null
  >(null);

  /**
   * Ask the server whether this caller may rate this transaction.
   *
   * Used for the initial load and for every retry, deliberately the same
   * function: a retry that differed from the first attempt in any way would be a
   * second code path to keep correct.
   *
   * @param isRetry Whether this call came from the retry BUTTON, which is what
   *   decides whether focus is moved on success — the button is about to be
   *   unmounted with the user standing on it, while the initial load has no focus
   *   to rescue and must not steal any. It is a parameter rather than a read of
   *   `eligibilityFailed` because this callback is a dependency of the effect
   *   below: depending on that flag would change the callback's identity the moment
   *   a request failed, re-running the effect, resetting the form and issuing
   *   another request — an unbounded retry loop, from a state that is reached by a
   *   single dropped connection.
   */
  const loadEligibility = useCallback(async (isRetry = false) => {
    const requestId = eligibilityRequestRef.current + 1;
    eligibilityRequestRef.current = requestId;

    const isCurrent = () => eligibilityRequestRef.current === requestId;

    setIsLoadingEligibility(true);
    setErrorMessage(null);

    try {
      const decision = await fetchRatingEligibility(transactionId);

      if (isCurrent()) {
        setEligibility(decision);
        /*
         * Cleared HERE, on success, and deliberately not at the start of the
         * request. Clearing it up front looks tidier and silently breaks the
         * retry control: `eligibilityFailed` is what mounts that button, so
         * setting it false in the same batched update that sets the loading flag
         * unmounts the button for the whole duration of the retry — its
         * "Checking..." label and its `disabled` prop become unreachable dead
         * code, and the user watches the control disappear instead of watching it
         * work. Keeping the flag until an answer arrives means the button stays
         * put, disabled and labelled, and vanishes only once there is genuinely
         * nothing left to retry.
         */
        setEligibilityFailed(false);

        /*
         * A retry that SUCCEEDS removes the button the user pressed, so the intent
         * to move focus is recorded here. It is recorded only when a retry was in
         * fact outstanding: the initial load reaches this line too, and the user is
         * not standing on anything then - they may be typing elsewhere on the page,
         * and stealing focus on a background load would be its own defect.
         *
         * Where it goes depends on the answer, because "the first enabled control"
         * only exists when the decision is that the caller may rate. An eligible
         * decision enables the star group, which is the next thing to do; an
         * ineligible one enables nothing, so focus goes to the region that has just
         * been given the server's reason - which is the only useful destination and
         * is announced as it receives focus.
         */
        if (isRetry) {
          setPendingFocus(decision.eligible ? 'controls' : 'confirmation');
        }
      }
    } catch (error) {
      // Sanitised: `describeRequestFailure` emits the endpoint, the status or
      // transport code and the server's detail, and never the axios error
      // object, whose `config` carries the bearer token this request was sent
      // with.
      console.error(
        'Failed to fetch rating eligibility',
        describeRequestFailure(error)
      );

      if (isCurrent()) {
        setEligibilityFailed(true);
        setErrorMessage(extractDetail(error, ELIGIBILITY_FAILURE_MESSAGE));
      }
    } finally {
      // In `finally` so the controls leave their loading state on both paths.
      // Only the success branch enables them; a failure leaves `eligibility`
      // null, which keeps them disabled while the error explains why and the
      // retry control offers a way out.
      if (isCurrent()) {
        setIsLoadingEligibility(false);
      }
    }
  }, [transactionId]);

  useEffect(() => {
    // A different transaction is a different rating, so nothing from the
    // previous one may survive: a stale confirmation or a stale reason shown
    // against the wrong transaction would be worse than showing nothing. Each
    // of these is already at its initial value on first mount, so React bails
    // out of the re-render and this costs nothing in the common case.
    setEligibility(null);
    setSubmittedRating(null);
    setScore(null);
    setReview('');
    // A new transaction starts from "no failure yet", so the retry control does
    // not carry over from the previous one. `loadEligibility` no longer clears
    // this flag itself — see the note there — so the reset belongs here, where
    // per-transaction state is reset anyway.
    setEligibilityFailed(false);
    // Any focus move queued against the previous transaction is abandoned with it:
    // moving focus now would land the user on a confirmation for a rating that is
    // no longer on screen.
    setPendingFocus(null);


    loadEligibility();

    return () => {
      // Invalidates whatever is in flight, so a late response cannot set state
      // after unmount or against a newer transaction.
      eligibilityRequestRef.current += 1;
    };
  }, [transactionId, loadEligibility]);

  /**
   * Move focus once the render that removed its previous home has committed.
   *
   * This runs AFTER the DOM is updated, which is the only point at which the
   * destination exists: the confirmation is focusable only once
   * `submittedRating` has rendered, and the star group is enabled only once an
   * eligible decision has. Doing it in the handler would focus an element that was
   * still disabled, or one React was about to replace.
   *
   * `tabIndex={-1}` on the status paragraph is what makes it focusable at all
   * without adding it to the tab sequence — it is not a control, so Tab must not
   * stop on it, but focus may be placed there programmatically. Focusing it is also
   * what guarantees the confirmation is READ: a live region announces a change, and
   * a region the user is standing on is announced again on demand and is where
   * their next Tab starts from.
   *
   * The intent is cleared immediately, so a later unrelated re-render cannot move
   * focus a second time and drag it away from wherever the user has since gone.
   * `focus()` on the star group is a no-op while it is disabled, so a decision that
   * arrived eligible and was disabled again in the same commit cannot leave focus
   * on an inert control.
   */
  useEffect(() => {
    if (pendingFocus === null) {
      return;
    }

    if (pendingFocus === 'controls') {
      scoreGroupRef.current?.focus();
    } else {
      statusRef.current?.focus();
    }

    setPendingFocus(null);
  }, [pendingFocus]);

  const isEligible = eligibility !== null && eligibility.eligible;
  const hasSubmitted = submittedRating !== null;

  /**
   * Whether the controls accept input.
   *
   * Ineligible does NOT mean unmounted. The specified behaviour is present but
   * disabled, alongside the reason: removing the form entirely would leave the
   * user with no indication that rating is a thing that exists here, and no
   * explanation of why they cannot do it.
   */
  const controlsDisabled = isLoadingEligibility || isSubmitting || !isEligible;

  /**
   * Whether submission may proceed, in one place so the button's `disabled` and
   * the handler's guards cannot disagree.
   *
   * `score !== null` is included, so the button stays disabled until a score is
   * chosen — a rating with no score is not a rating. An over-long review is
   * included for the same reason: a form in a state the server would refuse
   * should not offer to send it.
   *
   * The handler still re-checks both, and does not treat this flag as a
   * precondition it can assume. A `submit` event does not have to come from the
   * button: `requestSubmit()` on the form element, a devtools-dispatched event, or
   * an implicit submission from a single-line input added here later all reach the
   * handler without consulting `disabled`. A disabled control is an interface
   * affordance; the guard is the rule.
   */
  /**
   * The review, prepared ONCE for every purpose this component has for it.
   *
   * `prepareReviewText` performs the submission path's own three steps in its own
   * order - sanitise with DOMPurify, normalise as the server normalises, then
   * count CODE POINTS rather than UTF-16 code units - and returns the value, its
   * length and whether it exceeds the bound. Everything downstream reads this one
   * result: the character counter, the over-limit message, `canSubmit`, the
   * submit handler's re-check, and the `review` that actually goes on the wire.
   *
   * Deriving all five from one value is the fix for a real divergence rather than
   * a tidiness preference. This component used to measure `textLength(
   * normalizeReviewText(review))` - the RAW text - while `validateRatingInput`
   * sanitised before validating, so the two disagreed by however much
   * sanitisation removed. Paste `<b>Great car</b>` and the counter charged 17
   * characters for a payload of 10; at the boundary that meant a review could be
   * shown as over the limit, have its submit button disabled and be described as
   * too long, on the strength of a string that was never going to be sent.
   *
   * `useMemo` because the preparation parses the text with DOMPurify, and this
   * component re-renders for reasons that have nothing to do with the review -
   * an eligibility response, a star selection, a submission starting. The
   * dependency is the raw text, which is the only thing the result depends on.
   */
  const preparedReview = useMemo(() => prepareReviewText(review), [review]);

  const reviewLength = preparedReview.length;

  const isReviewOverLimit = preparedReview.isOverLimit;

  const canSubmit =
    isEligible &&
    !hasSubmitted &&
    !isSubmitting &&
    !isLoadingEligibility &&
    !isReviewOverLimit &&
    score !== null;

  /**
   * Discard a stored submission error.
   *
   * Called from every input handler, because a stored error describes the
   * OUTCOME OF ONE ATTEMPT ON ONE PAYLOAD. The moment the payload changes that
   * error stops describing anything true, and an assertive live region that
   * keeps asserting it is worse than one that says nothing: it interrupts a
   * screen reader with a stale claim and, in the length case, with a claim the
   * adjacent character counter visibly contradicts.
   *
   * This is deliberately NOT limited to the review. The score is part of the
   * payload too, so choosing a different star equally invalidates "choose a
   * score before submitting" and any refusal the server gave for the previous
   * score.
   */
  const clearStoredError = (): void => {
    setErrorMessage(null);
  };

  /**
   * The retry button's handler.
   *
   * A wrapper rather than `onClick={loadEligibility}` for two reasons: the click
   * event would otherwise be passed as `isRetry` — truthy by accident, correct only
   * by luck — and this is where the call is declared to BE a retry, which is what
   * arranges the focus move on success. The returned promise is deliberately not
   * awaited and needs no `catch`: `loadEligibility` handles its own failure and
   * never rejects.
   */
  const handleRetry = (): void => {
    void loadEligibility(true);
  };

  /**
   * Wraps `setScore` so choosing a star also drops a stale error. Passed to the
   * star control instead of the bare setter for exactly that reason.
   */
  const handleScoreChange = (nextScore: number): void => {
    setScore(nextScore);
    clearStoredError();
  };

  const handleReviewChange = (
    event: React.ChangeEvent<HTMLTextAreaElement>,
  ): void => {
    setReview(event.target.value);
    clearStoredError();
  };

  const handleSubmit = async (event: React.FormEvent): Promise<void> => {
    event.preventDefault();

    // Clear the previous failure first, so a second attempt never displays a
    // stale error beside a fresh outcome.
    clearStoredError();

    // Re-checked rather than trusted from `canSubmit`, because a `submit` event
    // need not have come from the button - see the note there. Enter inside the
    // textarea does NOT submit this form; it inserts a newline, which is normal
    // textarea behaviour and is why the review can be written as paragraphs.
    if (score === null) {
      setErrorMessage(
        `Choose a score from ${RATING_MIN} to ${RATING_MAX} before submitting.`,
      );
      return;
    }

    /**
     * Measured on the PREPARED text - `preparedReview`, the same result the
     * counter shows and the same value the payload carries - so the gate, the
     * message, the counter and the request cannot disagree about whether the
     * review is too long. There is no `maxLength` on the textarea to fall back on,
     * deliberately (see the note on the element), so this is the real gate rather
     * than defensive decoration. Returning here means the request is never sent: a
     * refusal this local should not cost a round trip.
     *
     * It deliberately stores NOTHING. The explanation is already on screen,
     * derived from `preparedReview`, and storing a second copy is what makes it go
     * stale: a stored message survives the user fixing the review and then sits,
     * visibly false, above a counter that now reads "6 / 2000". Derived state
     * cannot rot, so the derivation is left as the single source and this branch
     * only refuses to send.
     */
    if (isReviewOverLimit) {
      return;
    }

    setIsSubmitting(true);

    try {
      /**
       * The review submitted is THE VALUE THAT WAS MEASURED — `preparedReview
       * .value`, the same result the counter, the over-limit message and the
       * submit gate all read. Nothing is re-derived here, so there is no second
       * expression that could drift from the first.
       *
       * It is already sanitised and normalised, and `validateRatingInput` applies
       * both again on the way through. That is deliberate rather than wasteful:
       * sanitising plain text and normalising normalised text are idempotent, so
       * the value is unchanged, while the guarantee that no path out of this
       * component can forward unsanitised free text stays structural — it holds
       * because of what `validateRatingInput` does, not because this component
       * remembered to prepare first.
       *
       * `undefined` when there is nothing to say. `review` is optional on the
       * wire, so an empty string would send a present-but-blank review where the
       * correct statement is that no review was written; `prepareReviewText`
       * already collapses whitespace-only and markup-only text to that state.
       */
      const payload = validateRatingInput({
        transactionId,
        score,
        review: preparedReview.value,
      });

      const created = await submitRating(payload);

      /**
       * The rating is persisted. Commit the success state BEFORE telling anyone
       * else about it, and never inside the same `try` as the request.
       *
       * `onSubmitted` belongs to the parent screen, which typically re-reads an
       * eligibility decision or a reputation aggregate from the network. Those
       * reads can fail — and when the callback ran inside this `try`, their
       * failure was caught by the handler for the SUBMISSION and reported as
       * "Your rating could not be submitted", above a form that then invited the
       * user to submit again. The rating had been created; a second attempt would
       * have been refused with a 409, because one rating per rater per transaction
       * is enforced by the datastore. So the user was told a truthful action had
       * failed and offered a remedy that could only fail too.
       *
       * The callback is therefore isolated in its own `try`. Its failure is a
       * defect in the parent's refresh, so it is logged for a developer and not
       * shown to the user: the thing the user asked for succeeded, and the state
       * this component renders — `submittedRating` — already says so.
       */
      setSubmittedRating(created);

      /*
       * The controls are about to unmount with the focused submit button inside
       * them, so focus is moved to the confirmation. That sentence is the answer to
       * what the user just did, and being on it means a screen reader reads it and
       * a keyboard user continues from the right place instead of from the document
       * body.
       */
      setPendingFocus('confirmation');

      try {
        /*
         * The prop is declared to return `void`, but TypeScript admits an
         * `async` function wherever a `void`-returning one is expected — and a
         * parent whose refresh is a network read will very likely write one. An
         * unawaited rejection escapes a surrounding `try` entirely and surfaces
         * as an unhandled promise rejection, so a returned promise is caught
         * explicitly rather than assumed absent. It is deliberately not awaited:
         * the submission is complete and this component must not keep the submit
         * button disabled while somebody else's refresh finishes.
         */
        const settled: unknown = onSubmitted?.(created);

        if (settled instanceof Promise) {
          settled.catch((callbackError: unknown) => {
            console.error(
              'A rating was submitted but the parent refresh failed',
              describeSubmissionForLog(callbackError),
            );
          });
        }
      } catch (callbackError) {
        console.error(
          'A rating was submitted but the parent refresh failed',
          describeSubmissionForLog(callbackError),
        );
      }
    } catch (error) {
      // Scrubbed record only, for the same reason as the eligibility load above:
      // the bearer token is on the axios error's `config.headers` and the request
      // body is on `config.data`, so the error object itself must never be
      // written down. A local `ZodError` is reported as field paths instead -
      // see `describeSubmissionForLog`.
      console.error('Failed to submit rating', describeSubmissionForLog(error));


      setErrorMessage(describeSubmissionFailure(error));

      return;
    } finally {
      // In `finally` so a failure re-enables the button for another attempt.
      setIsSubmitting(false);
    }
  };


  /**
   * The single polite announcement, resolved by priority.
   *
   * One region rather than several, because a screen reader queues live regions
   * and three competing ones would talk over each other. The order below is the
   * order of what matters: what just happened, then the one local rule the user can
   * act on immediately, then what is being waited on, then why the controls are
   * inert.
   *
   * THE OVER-LENGTH SENTENCE IS ANNOUNCED HERE, POLITELY, AND NOT IN THE ASSERTIVE
   * REGION. It used to live there, carrying the live character count, so it was
   * rewritten and re-announced on every keystroke past the bound — assertively,
   * which means interrupting whatever the screen reader was saying, including the
   * word the user was in the middle of typing. Being over a length limit is not an
   * emergency: it is a condition the reader needs to know about and then work on,
   * which is precisely what `role="status"` is for. Because the text is a constant
   * it is announced once, when the threshold is crossed, and stays silent
   * afterwards no matter how much more is typed.
   *
   * It still outranks the loading and ineligibility messages, because it is the
   * CURRENT obstacle the user can do something about, and it sits below the
   * submission confirmation, which is terminal — once a rating exists the controls
   * are gone and the review length is history.
   *
   * The empty string is a legitimate value — see the region's own comment for
   * why it stays mounted while it has nothing to say.
   */
  let statusMessage = '';

  if (submittedRating !== null) {
    /**
     * The score echoed back is the SERVER's, not the local state's. They agree
     * today, and reading it from the response keeps that true if the server ever
     * normalises what it accepted: a confirmation must state what was actually
     * recorded, not what was requested.
     *
     * The unpublished wording is the important half. A brand-new rating is
     * genuinely invisible until the counterparty submits or the window closes,
     * so silence here would read as a failed submission. No number of days is
     * quoted: the window is a server setting (`RATING_WINDOW_DAYS`) that is not
     * published to this client, and a hardcoded figure would be free to drift
     * away from the real one.
     *
     * The two reveal paths are stated as two paths, because they do not have the
     * same outcome. This used to promise that "both ratings become visible
     * together" — true of the reciprocal path, where the counterparty's
     * submission publishes both records atomically, and false of the other one:
     * when the window closes on an unanswered rating, only this rating is
     * published, and there is no second rating to reveal. A user who was told to
     * expect the counterparty's rating and then found their own published alone
     * would reasonably conclude the other side had been suppressed. So the
     * sentence names what happens in each case instead of describing one case as
     * though it were both.

     */
    const recorded = `Your ${submittedRating.score}-star rating has been recorded.`;

    statusMessage = submittedRating.isPublished
      ? `${recorded} It is now visible on their profile.`
      : `${recorded} It stays private for now. If the other party rates you too, both ratings become visible at the same time; if they never do, yours becomes visible on its own once the rating window closes.`;

  } else if (isReviewOverLimit) {
    statusMessage = OVER_LENGTH_REVIEW_MESSAGE;
  } else if (isLoadingEligibility) {
    statusMessage = 'Checking whether you can rate this transaction...';
  } else if (eligibility !== null && !eligibility.eligible) {
    /**
     * Rendered VERBATIM. The server authored this sentence for exactly this
     * purpose and reuses the same text as the `detail` of the matching refusal,
     * so passing it through unchanged is what makes the disabled control and any
     * later error agree with each other.
     */
    statusMessage = eligibility.reason ?? UNAVAILABLE_MESSAGE;
  }

  /**
   * Display context derived from the server's decision.
   *
   * `direction` and `rateeId` arrive on the eligibility decision because the
   * server derived them; they are read here to tell the user WHO they are about
   * to rate, and for nothing else. Neither is ever placed in a request body —
   * that is what makes direction spoofing and self-rating impossible rather than
   * merely discouraged. Empty while the caller is not a confirmed participant,
   * because the server reports both as null until it can derive them.
   */
  /**
   * The one assertive announcement: a refusal, and nothing else.
   *
   * Assertive is for an outcome the user did not ask for and must hear about now —
   * a submission the server rejected, or a local guard that stopped one. It is
   * emphatically NOT for a condition that changes as somebody types: the
   * over-length sentence was moved to the polite region above precisely because
   * updating an `aria-live="assertive"` node on every keystroke interrupts the
   * screen reader on every keystroke.
   *
   * What remains is `errorMessage`, which is set only by an attempt — a failed
   * eligibility check, a refused submission, a submit with no score chosen — and is
   * dropped by every input handler, so this region never asserts something the
   * current payload has already made false. One event, one announcement.
   */
  const alertMessage = errorMessage ?? '';

  const counterpartyLabel = describeCounterparty(eligibility);

  const counterClasses = isReviewOverLimit
    ? 'text-sm font-semibold text-red-700'
    : 'text-sm text-gray-600';

  return (
    <section
      aria-labelledby={headingId}
      className="w-full rounded-lg bg-white p-4 shadow-md sm:p-6"
    >
      <h2 id={headingId} className="text-lg font-semibold text-gray-900">
        Rate the other party
      </h2>

      {/*
        The double-blind model, stated up front rather than only in the
        confirmation. Someone deciding how candid to be deserves to know before
        they write that the other side cannot read it and retaliate.

        The wording is precise about WHO cannot see it and about the two reveal
        paths, because the earlier "shown to anyone ... until the other one
        arrives or the rating window closes" was wrong twice over: the author can
        always retrieve their own rating, and an expiring window reveals only the
        one rating that exists rather than a pair.

        Deliberately NOT inside a live region: it never changes, and a static
        sentence in a live region is announced again on every unrelated update.
      */}
      <p className="mt-1 text-sm text-gray-600">
        Both sides of a completed sale rate each other. Yours stays hidden from
        the other party, and off their public profile, until they submit theirs —
        then both are revealed together. If they never submit one, yours is
        revealed on its own when the rating window closes.
      </p>

      {counterpartyLabel.length > 0 ? (
        <p className="mt-1 text-sm text-gray-600">{counterpartyLabel}</p>
      ) : null}

      {/*
        Polite live region, ALWAYS mounted, empty when there is nothing to say.

        Mounting a live region and its content in the same commit frequently
        means the content is never announced — the region has to already exist
        for the update to be observed. Keeping it present also gives the region a
        stable identity for `aria-describedby` and a stable query for tests.
      */}
      <p
        id={statusId}
        ref={statusRef}
        role="status"
        /*
          `-1` keeps this out of the tab sequence — it is a message, not a control,
          so Tab must not stop here — while making it a legitimate PROGRAMMATIC focus
          target. That is what the two transitions above need: when the form or the
          retry button is unmounted, focus is moved here rather than being dropped
          to the document body, so the user keeps their place and the sentence
          explaining what happened is where they are standing.
        */
        tabIndex={-1}
        className={
          hasSubmitted
            ? 'mt-3 text-sm font-semibold text-gray-900'
            : 'mt-3 text-sm text-gray-700'
        }
      >
        {statusMessage}
      </p>

      {/*
        Assertive live region, kept separate from the polite one for the same
        reason the ARIA roles are separate: "you cannot do this yet" is context,
        while "your submission was refused" interrupts. Sharing one region would
        force every refusal to wait behind whatever was being announced.
      */}
      <p role="alert" className="mt-1 text-sm font-semibold text-red-700">
        {alertMessage}
      </p>

      {/*
        The way out of a failed eligibility CHECK.

        Rendered only when the request itself failed — never when the server
        answered that the caller is ineligible, because that answer will not change
        on a second ask and offering to re-ask would imply otherwise. A failed
        check is the opposite case: the form knows nothing, every control is
        disabled, and without this button that state is permanent for as long as
        the screen stays mounted. A network blip would cost the user their ability
        to rate at all.

        A real `<button type="button">`, so it is reachable by Tab, operated by
        Enter and Space, and reported as a button — and typed `button` explicitly
        because it sits inside a `<section>` that also contains a form. Its label
        changes while a check is in flight, which is the accessible half of its
        disabled state: a control that only greys out reports nothing about why.
        The adjacent assertive region already carries the reason it appeared, so
        the button needs no `aria-describedby` of its own.
      */}
      {eligibilityFailed && !hasSubmitted ? (
        <button
          type="button"
          onClick={handleRetry}
          disabled={isLoadingEligibility}
          className={`mt-3 ${RETRY_BUTTON_CLASSES}`}
        >
          {isLoadingEligibility ? 'Checking...' : 'Check again'}
        </button>
      ) : null}

      {/*
        The controls are gone once a rating exists, and nothing replaces them.
        Reputation records are append-only: no endpoint accepts a rewrite and no
        schema describes one, so an edit or delete control here would advertise a
        capability the system does not have.

        While INELIGIBLE the form is still rendered, merely disabled. That is the
        specified behaviour: the reason above explains why, and unmounting would
        leave the user unable to tell that rating exists at all.
      */}
      {hasSubmitted ? null : (
        <form onSubmit={handleSubmit} className="mt-4 space-y-4">
          {/*
            `required` because a rating with no score is not a rating: the submit
            button stays disabled until one is chosen, and a control that is
            mandatory has to SAY so rather than leaving the user to infer it from a
            button that will not respond. The prop renders `aria-required` on the
            group and the word "(required)" inside its visible label, so the
            obligation reaches a screen reader and a sighted reader by independent
            channels.

            The review beside it is labelled "(optional)" in the same way, so the
            two fields state their status in the same voice.
          */}
          <StarRatingInput
            ref={scoreGroupRef}
            value={score}
            onChange={handleScoreChange}
            label="Your rating"
            disabled={controlsDisabled}
            required
          />

          <div className="flex flex-col gap-1">
            {/*
              A real label, associated by `htmlFor`. A placeholder is not a
              label: it disappears on first keystroke and is not reliably
              exposed as an accessible name. "(optional)" is in the label itself
              so the same fact reaches a screen reader and a sighted reader.
            */}
            <label
              htmlFor={reviewId}
              className="text-base font-semibold text-gray-800"
            >
              Review (optional)
            </label>

            <textarea
              id={reviewId}
              value={review}
              onChange={handleReviewChange}
              disabled={controlsDisabled}
              /*
               * NO `maxLength`. The attribute counts UTF-16 code units, so it
               * cannot express the server's bound of 2000 NFC-composed code
               * points: it would stop someone writing emoji at half the real
               * limit, silently, mid-word, with the counter beside it still
               * reading under the limit — a client-side rule quietly refusing
               * text the server would accept. The bound is carried instead by the
               * counter below, by the reactive over-limit message, by the submit
               * button's disabled state and by the explicit check in the submit
               * handler, all of which measure with `textLength`.
               */

              /*
               * `rows` rather than a CSS height so the field grows with the
               * reader's font size instead of clipping its own text.
               */
              rows={4}
              aria-describedby={counterId}
              /*
               * The over-limit state as a PROPERTY of the field, which is what a
               * screen reader reports when the user moves back to it: "invalid
               * entry", alongside the counter it is described by. Without it the
               * only machine-readable signal was a live-region announcement that had
               * already been made and could not be re-read from the field itself.
               *
               * `undefined` rather than `false` when the review is within bounds, so
               * the attribute is absent instead of asserting validity on a field
               * that has not been validated for anything else.
               */
              aria-invalid={isReviewOverLimit ? true : undefined}
              className={TEXTAREA_CLASSES}
            />

            {/*
              The counter is TEXT, and stays text when the bound is exceeded.
              Colour is added then, never substituted: turning the digits red is
              invisible to a colour-blind reader and to anyone in greyscale, so
              the words "over the limit" carry the same fact independently.

              Associated by `aria-describedby` rather than made a live region: a
              counter that announced itself on every keystroke would make the
              field unusable with a screen reader.
            */}
            <p id={counterId} className={counterClasses}>
              {`${reviewLength} / ${REVIEW_MAX_LENGTH} characters`}
              {isReviewOverLimit ? ' (over the limit)' : ''}
            </p>
          </div>

          {/*
            A native submit button, so Enter and Space activate it and assistive
            technology reports it as a button without being told. Its label
            changes while a submission is in flight, which is the accessible half
            of the disabled state: a control that only greys out reports nothing
            about why.
          */}
          <button
            type="submit"
            disabled={!canSubmit}
            aria-describedby={statusId}
            className={BUTTON_CLASSES}
          >
            {isSubmitting ? 'Submitting...' : 'Submit rating'}
          </button>
        </form>
      )}
    </section>
  );
};

export default RatingSubmissionForm;
