import React, { useEffect, useId, useState } from 'react';
import { ZodError } from 'zod';

import StarRatingInput from './StarRatingInput';
import { fetchRatingEligibility, submitRating } from '../services/rating';
import { sanitizeUserInput, validateRatingInput } from '../utils/validation';
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
 * WHY THE NARROWING IS WRITTEN OUT BY HAND
 * -----------------------------------------------------------------------------
 * `tsconfig.json` sets `strict: true`, so under TypeScript 5.1 a `catch` binding
 * is `unknown` and the shape has to be proven before it can be read. The obvious
 * shortcut — casting the error to the unchecked escape-hatch type and reading
 * `response.data.detail` off it — is rejected by this project's lint gate
 * (`@typescript-eslint/no-explicit-any`, fatal under `--max-warnings 0`), and
 * importing axios purely for `isAxiosError` would add a dependency this
 * component otherwise does not need.
 *
 * So the property is proven present with `in`, then read through a narrow
 * structural assertion that describes only the path being walked, with every
 * level optional and the leaf typed `unknown`. The `typeof detail === 'string'`
 * test is what actually admits the value; the assertion merely gets the compiler
 * to the point where that test can be applied.
 *
 * The empty-string guard matters: a `detail` of `''` is present but says
 * nothing, and rendering it would leave a visibly blank error region.
 *
 * @param error The rejection value, of genuinely unknown shape.
 * @param fallback Returned when no usable message can be found.
 * @returns The server's message, else the error's own message, else `fallback`.
 */
const extractDetail = (error: unknown, fallback: string): string => {
  if (typeof error === 'object' && error !== null && 'response' in error) {
    const { response } = error as {
      response?: { data?: { detail?: unknown } };
    };
    const detail = response?.data?.detail;

    if (typeof detail === 'string' && detail.length > 0) {
      return detail;
    }
  }

  // A transport failure, or a 2xx response that did not match the contract
  // (`../services/rating` throws for that case), carries a real message but no
  // HTTP body. It is still more informative than the generic fallback.
  if (error instanceof Error && error.message.length > 0) {
    return error.message;
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
 * The review case is the one worth explaining, because it can fire on a review
 * that the counter showed as within the limit. Text is sanitised before it is
 * validated, and sanitising can LENGTHEN a string — `5 < 6` becomes `5 &lt; 6` —
 * so a review at the very edge of the bound can cross it once it has been made
 * safe to display. The bound then applies to the text that would actually be
 * stored, which is the correct place to apply it, and the message says "once it
 * has been prepared for display" rather than accusing the user of miscounting.
 *
 * Zod's own message is the last resort rather than the first, and the unstyled
 * `error.message` — a JSON dump of every issue — is never rendered.
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
    return `Your review must be ${REVIEW_MAX_LENGTH} characters or fewer once it has been prepared for display. Please shorten it.`;
  }

  if (field === 'transactionId') {
    return 'This transaction cannot be rated because its reference is not valid.';
  }

  return issue.message.length > 0 ? issue.message : SUBMISSION_FAILURE_MESSAGE;
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
 * The one piece of copy for an over-long review, in one place.
 *
 * Defined once because it is rendered from two independent triggers — reactively
 * the moment the text crosses the bound, and again from the submit handler if a
 * submission is attempted anyway — and two hand-written copies of the same
 * sentence would eventually disagree with each other.
 *
 * The actual length is quoted rather than only the limit, because "your review is
 * 2001 characters" tells the reader how much to cut while "too long" does not.
 *
 * @param length The current review length, in characters.
 * @returns One sentence naming the overage and the bound.
 */
const describeOverLengthReview = (length: number): string =>
  `Your review is ${length} characters. Shorten it to ${REVIEW_MAX_LENGTH} characters or fewer before submitting.`;

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
 * `motion-safe:` scopes the colour transition to
 * `@media (prefers-reduced-motion: no-preference)`, so a reader who has asked
 * for reduced motion gets an instant change instead.
 */
const BUTTON_CLASSES = [
  'inline-flex items-center justify-center rounded-md',
  'bg-blue-600 px-4 py-2 text-base font-semibold text-white shadow-sm',
  'hover:bg-blue-700',
  'focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-blue-600',
  'disabled:cursor-not-allowed disabled:bg-gray-400 disabled:hover:bg-gray-400',
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

  useEffect(() => {
    /**
     * Guards against a state update after unmount, and against a slow response
     * for a previous `transactionId` overwriting a newer one. Flipped by the
     * cleanup below, which React runs both on unmount and before re-running the
     * effect for a changed id.
     */
    let active = true;

    // A different transaction is a different rating, so nothing from the
    // previous one may survive: a stale confirmation or a stale reason shown
    // against the wrong transaction would be worse than showing nothing. Each
    // of these is already at its initial value on first mount, so React bails
    // out of the re-render and this costs nothing in the common case.
    setIsLoadingEligibility(true);
    setEligibility(null);
    setSubmittedRating(null);
    setErrorMessage(null);
    setScore(null);
    setReview('');

    const loadEligibility = async () => {
      try {
        const decision = await fetchRatingEligibility(transactionId);

        if (active) {
          setEligibility(decision);
        }
      } catch (error) {
        console.error('Failed to fetch rating eligibility:', error);

        if (active) {
          setErrorMessage(extractDetail(error, ELIGIBILITY_FAILURE_MESSAGE));
        }
      } finally {
        // In `finally` so the controls leave their loading state on both paths.
        // Only the success branch enables them; a failure leaves `eligibility`
        // null, which keeps them disabled while the error explains why.
        if (active) {
          setIsLoadingEligibility(false);
        }
      }
    };

    loadEligibility();

    return () => {
      active = false;
    };
  }, [transactionId]);

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
   * The handler still re-checks both. A form can be submitted by pressing Enter
   * inside a text control, which does not consult the button at all.
   */
  const isReviewOverLimit = review.length > REVIEW_MAX_LENGTH;

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

    // Re-checked rather than trusted from `canSubmit`: Enter in the textarea
    // submits the form without the button being involved.
    if (score === null) {
      setErrorMessage(
        `Choose a score from ${RATING_MIN} to ${RATING_MAX} before submitting.`,
      );
      return;
    }

    /**
     * Measured on the RAW text, which is exactly what the counter displays, so
     * the two can never disagree about whether the review is too long. The
     * textarea's `maxLength` stops this being reachable by typing, but it does
     * not constrain a programmatic value change, so the check is real rather
     * than defensive decoration. Returning here means the request is never
     * sent: a refusal this local should not cost a round trip.
     *
     * It deliberately stores NOTHING. The message is already on screen, derived
     * from `review.length` by `alertMessage`, and storing a second copy is what
     * makes it go stale: a stored "your review is 2001 characters" survives the
     * user fixing the review and then sits, visibly false, above a counter
     * reading "6 / 2000". Derived state cannot rot, so the derivation is left as
     * the single source and this branch only refuses to send.
     */
    if (isReviewOverLimit) {
      return;
    }

    setIsSubmitting(true);

    try {
      const trimmed = review.trim();

      /**
       * Sanitised before it goes anywhere, satisfying the DOMPurify obligation
       * in `documentation/Technical Specifications.md` L676-L679. Doing it here
       * as well as inside `validateRatingInput` is intentional belt and braces
       * rather than an oversight: DOMPurify is idempotent, so the second pass
       * cannot corrupt the first, and the reviewable guarantee is that no path
       * out of this component forwards unsanitised free text.
       *
       * Omitted entirely when there is nothing to say. `review` is optional on
       * the wire, so an empty string would send a present-but-blank review
       * where the correct statement is that no review was written.
       */
      const safeReview =
        trimmed.length > 0 ? sanitizeUserInput(trimmed) : undefined;

      /**
       * The returned value is what gets submitted, never the raw object built
       * above. The schema strips unrecognised keys, so it is also the last
       * structural guarantee that no `raterId`, `rateeId` or `direction` can
       * reach the wire from here.
       */
      const payload = validateRatingInput({
        transactionId,
        score,
        review: safeReview,
      });

      const created = await submitRating(payload);

      setSubmittedRating(created);
      onSubmitted?.(created);
    } catch (error) {
      console.error('Failed to submit rating:', error);
      setErrorMessage(describeSubmissionFailure(error));
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
   * order of what matters: what just happened, then what is being waited on,
   * then why the controls are inert.
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
     */
    const recorded = `Your ${submittedRating.score}-star rating has been recorded.`;

    statusMessage = submittedRating.isPublished
      ? `${recorded} It is now visible on their profile.`
      : `${recorded} It stays private until the other party submits their rating or the rating window closes, and then both ratings become visible together.`;
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
   * The one assertive announcement, resolved by priority.
   *
   * An over-long review OUTRANKS a stored error, and it is reported reactively
   * rather than only on submit. Both halves matter.
   *
   * Reactively, because the bound is knowable the instant the text crosses it,
   * and a form that stays silent until the user presses a button they can no
   * longer press would be a dead end. This is the one local rule that can be
   * evaluated without the server, so it is the one worth reporting immediately.
   *
   * Outranking, because it is the CURRENT, actionable obstacle: showing a
   * leftover "you have already rated this transaction" from an earlier attempt,
   * while the real reason the button is inert is the length, would send the user
   * to fix the wrong thing.
   *
   * The length message is DERIVED here and stored nowhere, and `errorMessage` is
   * dropped by every input handler. Together those two rules are what stop this
   * region going stale: whichever branch wins, its text is true of the payload
   * as it stands right now.
   */
  const alertMessage = isReviewOverLimit
    ? describeOverLengthReview(review.length)
    : errorMessage ?? '';

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

        Deliberately NOT inside a live region: it never changes, and a static
        sentence in a live region is announced again on every unrelated update.
      */}
      <p className="mt-1 text-sm text-gray-600">
        Both sides of a completed sale rate each other. Neither rating is shown
        to anyone until the other one arrives or the rating window closes.
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
        role="status"
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
          <StarRatingInput
            value={score}
            onChange={handleScoreChange}
            label="Your rating"
            disabled={controlsDisabled}
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
               * First line of defence only. It stops typing past the bound, but
               * it does not constrain a programmatic value change, so the
               * explicit check in the submit handler is the real gate.
               */
              maxLength={REVIEW_MAX_LENGTH}
              /*
               * `rows` rather than a CSS height so the field grows with the
               * reader's font size instead of clipping its own text.
               */
              rows={4}
              aria-describedby={counterId}
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
              {`${review.length} / ${REVIEW_MAX_LENGTH} characters`}
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

