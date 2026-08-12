import { VehicleListingSchema } from '../schema/listing';
import { RatingCreateSchema, type RatingCreate } from '../schema/rating';
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
 * sanitise separately.
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

  return { ...candidate, review: sanitizeUserInput(candidate.review) };
};