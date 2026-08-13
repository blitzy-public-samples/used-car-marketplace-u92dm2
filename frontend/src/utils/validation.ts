import { VehicleListingSchema } from '../schema/listing';
import {
  DOCUMENT_ID_MAX_LENGTH,
  RatingCreateSchema,
  REVIEW_MAX_LENGTH,
  REVIEW_RAW_MAX_LENGTH,
  normalizeReviewText,
  textLength,
  type RatingCreate,
} from '../schema/rating';
import DOMPurify from 'dompurify';

/**
 * Validate an unvalidated vehicle listing payload.
 *
 * The parameter is `unknown` rather than `any`, which is the correct type for a
 * value whose shape has not been established yet and is also what `parse`
 * accepts. `any` disabled type checking on every use of the argument inside this
 * function and was reported by `@typescript-eslint/no-explicit-any` — fatal under
 * the `--max-warnings 0` lint gate — while buying nothing: the value is passed
 * straight to the schema, which is what decides its shape.
 *
 * @param vehicleDetails Unvalidated candidate, typically a parsed request body
 *   or form state.
 * @returns The parsed listing, narrowed by `VehicleListingSchema`.
 * @throws {ZodError} When the candidate does not satisfy the schema.
 */
export const validateVehicleDetails = (vehicleDetails: unknown) => {
  const validatedData = VehicleListingSchema.parse(vehicleDetails);
  return validatedData;
};

/**
 * Reduce a string of user-authored text to safe PLAIN TEXT.
 *
 * Supplies the DOMPurify half of the XSS defence required by
 * `documentation/Technical Specifications.md` L679 ("Use of React's built-in XSS
 * protection and DOMPurify for additional sanitization"). React escapes what it
 * renders; this covers text that this application stores and forwards.
 *
 * THE RETURN VALUE IS TEXT, NOT HTML, AND THAT DISTINCTION IS THE WHOLE FUNCTION.
 * Every caller — the rating review, the search query — stores this value and then
 * renders it as a JSX child, where React escapes it again. DOMPurify's normal
 * output is an HTML FRAGMENT, so feeding that pipeline produced doubly-encoded
 * text: a review reading `5 < 6` was sanitised to the markup `5 &lt; 6` and then
 * displayed, literally, as `5 &lt; 6`. Any tag DOMPurify's default profile
 * happens to allow survived as visible markup in the same way. The user's own
 * words came back mangled, and the server — which normalises but never escapes —
 * stored the mangled form as the record of what they said.
 *
 * So the sanitiser is asked for a DOM fragment with NO tag and NO attribute
 * permitted, and its `textContent` is taken. `KEEP_CONTENT` defaults to true, so
 * an element is removed while the words inside it remain, and reading
 * `textContent` decodes entities exactly once: `5 < 6` round-trips unchanged, and
 * `<img src=x onerror=alert(1)>hello` becomes `hello`. Markup is therefore
 * REMOVED rather than escaped — the stored value is what a reader sees, which is
 * what "plain text" has to mean for the value to be trustworthy.
 *
 * Always call the `sanitize` METHOD, never the module's default export directly.
 * That export is a callable factory, so `DOMPurify(input)` is read as "build a new
 * instance bound to this window" and hands back another factory function instead
 * of sanitised text, without throwing — the declared return type still claims
 * `string` while the untouched input flows onward.
 *
 * The two guards on the result are for genuinely reachable states rather than
 * defensive noise: `textContent` is typed nullable, and DOMPurify returns the
 * RAW STRING instead of a fragment when it finds no DOM to work with (a
 * `node`-environment test, or a server-side render), which is the one case where
 * this function cannot sanitise and says so by passing the input through
 * unchanged — the same behaviour the library itself chooses.
 */
export const sanitizeUserInput = (input: string): string => {
  const sanitized = DOMPurify.sanitize(input, {
    ALLOWED_TAGS: [],
    ALLOWED_ATTR: [],
    RETURN_DOM_FRAGMENT: true,
  });

  const text: string | null =
    typeof sanitized === 'string' ? sanitized : sanitized.textContent;

  return text ?? '';
};

/**
 * One review, prepared: the exact value that will be submitted, and its length.
 *
 * Returned by `prepareReviewText`, which is the single place a review is turned
 * from what somebody typed into what the server will receive.
 */
export interface PreparedReview {
  /**
   * The prepared review, or `undefined` when nothing was written.
   *
   * `undefined` rather than `''` because `review` is OPTIONAL on the wire and the
   * two say different things: omitting the key states that no review was written,
   * while a present empty string states that one was written and is blank.
   */
  value: string | undefined;

  /**
   * The prepared review's length in NFC-composed Unicode code points — the figure
   * a character counter must show, because it is the figure the bound is applied
   * to on both sides of the boundary.
   */
  length: number;

  /** Whether `length` exceeds `REVIEW_MAX_LENGTH`, i.e. the server would refuse it. */
  isOverLimit: boolean;
}

/**
 * Prepare one review once, for the counter, the submit gate AND the payload.
 *
 * THE POINT IS THAT THERE IS ONLY ONE OF THESE. The submission path sanitises
 * before it validates, so the string the schema bounds and the server stores is
 * the sanitised, normalised one — but the interface used to measure the RAW text
 * for its character counter and its disabled state. The two therefore disagreed
 * whenever sanitisation shrank the value, which is exactly what it does to the
 * text people paste: `<b>Great car</b>` is 17 raw characters and 10 prepared ones,
 * so a review could be counted over the limit, have its submit button disabled and
 * be reported as too long while the payload that would actually have been sent was
 * comfortably inside the bound. The user was blocked by a measurement of a string
 * that was never going to be transmitted.
 *
 * The three steps are the submission path's own, in its order:
 *
 *   1. `sanitizeUserInput` — DOMPurify with no tag and no attribute allowed, whose
 *      result is plain TEXT rather than markup. This is the step the counter was
 *      missing.
 *   2. `normalizeReviewText` — the schema's faithful port of the server's
 *      `as_plain_text` normalisation: NFC composition, CRLF folding, removal of
 *      control and format characters other than newline and tab, collapsing runs
 *      of blank lines, and a trim. It is also why no separate `.trim()` is needed
 *      here or at any call site.
 *   3. `textLength` — CODE POINTS, not UTF-16 code units, because `String.length`
 *      is wrong for exactly the text people write: an emoji is one code point
 *      stored as a surrogate pair, so a `.length` counter would tell someone who
 *      wrote 1200 emoji they had used 2400 of their 2000 characters.
 *
 * It reports rather than refuses. `isOverLimit` is what a form needs in order to
 * explain the problem while the user is still typing, and refusing here would
 * force a `try`/`catch` around a keystroke. `validateRatingInput` remains the
 * authority, and it applies the identical steps to whatever it is handed, so
 * passing `value` to it is idempotent and the guarantee that no unsanitised text
 * can reach the wire stays structural rather than a matter of call order.
 *
 * A RAW CEILING IS APPLIED BEFORE ANY OF IT. See the guard's own note: step 1
 * parses its input as HTML, and this function runs on every keystroke and on every
 * paste, so it must never be handed an unbounded string.
 *
 * @param raw Review text exactly as typed, including the empty string.
 * @returns The prepared value, its code-point length, and whether it is too long.
 */
export const prepareReviewText = (raw: string): PreparedReview => {
  /*
   * REFUSE BEFORE PARSING. `sanitizeUserInput` builds a DOM fragment from its
   * argument, which is proportional in cost to the input and can be far worse than
   * linear for pathological markup — and this function is called on every render
   * of the review field, including the render that follows a paste. Handing it a
   * multi-megabyte clipboard payload freezes the tab, on the main thread, with no
   * ceiling of any kind. The cheap check therefore comes first and the expensive
   * work is never reached for input that could not be accepted anyway.
   *
   * THE MEASURE IS `String.length` DELIBERATELY, and it is compared against
   * `REVIEW_RAW_MAX_LENGTH`, because that is exactly the measure and the bound
   * `RatingCreateSchema.review` applies with `.max(REVIEW_RAW_MAX_LENGTH)` before
   * its own transform runs. So this refuses precisely what the authority refuses —
   * it is not a stricter client-side rule — and `String.length` costs nothing to
   * read, where a code-point count would mean scanning the whole hostile payload
   * to refine a number that is already an order of magnitude past the bound.
   *
   * The reported `length` is that same code-unit figure, so the counter's number
   * and the reason it is refused agree with each other. It is the one input for
   * which `length` is not a code-point count, and it is the one input for which
   * counting them would be the defect.
   */
  if (raw.length > REVIEW_RAW_MAX_LENGTH) {
    return {
      value: undefined,
      length: raw.length,
      isOverLimit: true,
    };
  }

  const prepared = normalizeReviewText(sanitizeUserInput(raw));
  const length = textLength(prepared);

  return {
    value: prepared.length > 0 ? prepared : undefined,
    length,
    isOverLimit: length > REVIEW_MAX_LENGTH,
  };
};

/**
 * Validate one rating submission and return it ready to send.
 *
 * Mirrors the bounds `POST /api/ratings` enforces — a whole-number score within
 * `RATING_MIN`..`RATING_MAX`, and a review no longer than `REVIEW_MAX_LENGTH` —
 * by parsing through `RatingCreateSchema`. The figures therefore live in exactly
 * one place, `../schema/rating`, which is itself a mirror of the backend
 * settings; restating them here would create a second source of truth free to
 * drift away from the server.
 *
 * This is a CONVENIENCE, not an authority. `documentation/Technical
 * Specifications.md` L674-L675 puts server-side validation of every user input
 * first and casts client-side validation as UX. Passing this function therefore
 * promises only that a payload is well-formed. The request may still be refused
 * with 403 (the rater is not a verified user, or is not a party to the cited
 * transaction), 409 (already rated, or the transaction is not completed) or 422,
 * because those conditions live on the server and are invisible to a schema.
 * Callers must handle those outcomes; a clean parse is not permission.
 *
 * XSS: the review is the one piece of free text a user authors here, so the
 * returned value carries it already passed through `sanitizeUserInput`. Doing
 * that inside this function makes the guarantee structural — no caller can
 * forward an unsanitised review by forgetting a step, and no caller needs to
 * sanitise separately. A caller that has already prepared its review with
 * `prepareReviewText` loses nothing by that: sanitising plain text and normalising
 * normalised text are both idempotent, so the value it measured is the value that
 * is validated and sent.
 *
 * SANITISATION RUNS BEFORE VALIDATION, so the bound is applied to the value that
 * is actually returned, sent and stored. Order matters and the previous order was
 * wrong in a way that showed: text was validated and then transformed, so the
 * string the schema approved was not the string that went on the wire. It is safe
 * as well as correct now that `sanitizeUserInput` yields plain text — removing
 * markup and decoding entities can only ever SHORTEN a string, never lengthen it
 * — so a review the live counter shows as within the limit can never be refused
 * here for length. The conservative direction is the right one: this layer must
 * never refuse something the server would have accepted.
 *
 * The length rule itself lives in `RatingCreateSchema`, which measures
 * NFC-composed code points exactly as the server does, so emoji and
 * decomposed-script reviews are counted the way the server counts them.

 *
 * `ZodError` is left to propagate, matching `validateVehicleDetails`, because it
 * carries per-field issues a form needs in order to render each message against
 * the control that caused it. It is deliberately not caught, not flattened to a
 * boolean, and not wrapped in a result object.
 *
 * @param input Unvalidated candidate, typically raw form state. Typed `unknown`
 *   so that narrowing is Zod's job rather than an assertion's.
 * @returns The three permitted fields — `transactionId`, `score`, and a
 *   sanitised `review` when one was supplied. Unrecognised keys are stripped by
 *   the schema, so a spoofed `rateeId` or `direction` never reaches the wire.
 * @throws {ZodError} When a field is missing, mistyped, or out of bounds.
 */
export const validateRatingInput = (input: unknown): RatingCreate =>
  RatingCreateSchema.parse(withSanitizedReview(input));

/**
 * Read the seller's user ID off a loaded vehicle listing. F010-3.
 *
 * The reputation badge on the vehicle details page needs to know WHOSE
 * reputation to fetch, and the only place that is stated is the listing payload
 * the page has already loaded. This function is that read, extracted so it can be
 * tested directly: the page itself cannot be rendered in a test, because five of
 * its imports use an `@/…` specifier that resolves in neither the type-checker
 * nor the bundler — a pre-existing, out-of-scope defect it shares with some
 * thirty other files.
 *
 * `seller_id` IS THE FIELD, AND IT IS SNAKE_CASE
 * -----------------------------------------------------------------------------
 * The authority is the server: `backend/app/schema/listing.py` declares
 * `VehicleListing.seller_id`, and `GET /api/listings/{id}` returns that model
 * verbatim with no camelCase mapper anywhere on the path. The page previously
 * read `sellerId`, which is `undefined` on every real response — so its own
 * "no seller id, no request" guard was taken every time and the badge was
 * permanently empty on a screen whose entire purpose is to inform a buyer about
 * the person they are about to transact with. The camelCase spelling exists only
 * in `frontend/src/schema/listing.ts`, which nothing on this path parses through.
 *
 * `sellerId` is still accepted, second, and deliberately not first: a caller that
 * has already normalised the payload should not be broken by this, but the wire
 * spelling has to win so that a normaliser introduced later cannot silently
 * shadow the authoritative field with a stale copy of it.
 *
 * WHY THE VALUE IS CHECKED AND NOT JUST READ
 * -----------------------------------------------------------------------------
 * The result is fed to `/api/ratings/user/{userId}/aggregate`, whose path
 * parameter the server validates against the Firestore document-ID grammar. An
 * empty or whitespace-only string, or a value carrying a `/`, would spend a round
 * trip to be answered 404 or 422 — and `/ratings/user//aggregate` would not even
 * address the intended route. Those are reported here as "no seller id" instead,
 * which is the state the page already handles by leaving the badge in its empty
 * state. Anything longer than the grammar permits is refused for the same reason.
 *
 * Nothing about a listing is asserted beyond this one field. The payload is
 * `unknown` because that is what it is: this page's listing fetch is untyped, and
 * narrowing one field is all this function claims to do.
 *
 * @param listing The loaded listing payload, of unestablished shape — typically
 *   still `null` while the fetch is in flight.
 * @returns The seller's user ID, or `undefined` when the payload carries no
 *   usable one. `undefined` is a legitimate, non-error state: it holds while the
 *   listing is loading and it can persist afterwards, since the listing endpoint
 *   is owned elsewhere and cannot be assumed to include the field.
 */
export const readListingSellerId = (
  listing: unknown
): string | undefined => {
  if (typeof listing !== 'object' || listing === null) {
    return undefined;
  }

  const candidate = listing as { seller_id?: unknown; sellerId?: unknown };
  const value =
    typeof candidate.seller_id === 'string'
      ? candidate.seller_id
      : candidate.sellerId;

  if (typeof value !== 'string') {
    return undefined;
  }

  const trimmed = value.trim();

  if (
    trimmed.length === 0 ||
    trimmed.length > DOCUMENT_ID_MAX_LENGTH ||
    trimmed.includes('/')
  ) {
    return undefined;
  }

  return trimmed;
};

/**
 * Replace a candidate's `review` with its plain-text form, leaving everything
 * else exactly as supplied.
 *
 * Runs before parsing, on a value that is still `unknown`, so it must narrow
 * rather than assume: anything that is not an object with a non-empty string
 * `review` is passed through untouched and left for the schema to reject or
 * accept on its own terms. An absent `review` therefore STAYS ABSENT — rebuilding
 * the object unconditionally would add `review: undefined` to a submission that
 * never carried the field, which is a different statement from "no review".
 *
 * `sanitizeUserInput` can return an empty string, when the entire review was
 * markup. That empty string is forwarded rather than dropped: the server
 * normalises blank text to null itself, so second-guessing it here would be this
 * layer inventing a rule the contract does not have.
 *
 * Unrecognised keys are NOT stripped here; that is the schema's job, and it
 * happens immediately afterwards. This function only ever narrows one field.
 */
const withSanitizedReview = (input: unknown): unknown => {
  if (typeof input !== 'object' || input === null) {
    return input;
  }

  const candidate = input as { review?: unknown };

  if (typeof candidate.review !== 'string' || candidate.review.length === 0) {
    return input;
  }

  /*
   * PAST THE RAW CEILING, HAND IT ON UNSANITISED AND LET THE SCHEMA REFUSE IT.
   *
   * Sanitising means parsing, and parsing an unbounded string is the cost this
   * guard exists to avoid — a caller that reaches `validateRatingInput` directly,
   * without going through `prepareReviewText`, would otherwise pay it here
   * instead. Returning the input untouched is safe rather than a bypass: the very
   * next thing that happens is `RatingCreateSchema.parse`, whose `review` field
   * applies `.max(REVIEW_RAW_MAX_LENGTH)` to the string BEFORE its transform, so
   * an oversized review is rejected with a `ZodError` and no such value can reach
   * the wire. The comparison here uses `String.length` precisely because that is
   * the measure `.max` uses, so the two cannot disagree about which side of the
   * ceiling a string falls on.
   */
  if (candidate.review.length > REVIEW_RAW_MAX_LENGTH) {
    return input;
  }

  return { ...candidate, review: sanitizeUserInput(candidate.review) };
};