import { VehicleListingSchema } from '../schema/listing';
import { RatingCreateSchema, type RatingCreate } from '../schema/rating';
import DOMPurify from 'dompurify';

export const validateVehicleDetails = (vehicleDetails: any) => {
  const validatedData = VehicleListingSchema.parse(vehicleDetails);
  return validatedData;
};

/**
 * Strip active content from a string of user-authored text.
 *
 * Supplies the DOMPurify half of the XSS defence required by
 * `documentation/Technical Specifications.md` L679 ("Use of React's built-in XSS
 * protection and DOMPurify for additional sanitization"). React escapes what it
 * renders; this covers text that this application stores and forwards.
 *
 * Always call the `sanitize` METHOD, never the module's default export directly.
 * That export is a callable factory — typed
 * `{ (root?: WindowLike): DOMPurify; sanitize(dirty: string | Node): string }` —
 * so `DOMPurify(input)` is read as "build a new instance bound to this window"
 * and hands back another factory function instead of sanitised text. It does not
 * throw, which is what makes the mistake dangerous: the untouched input flows
 * onward while the declared return type still claims `string`. This wrapper
 * previously did exactly that, so `<img src=x onerror=alert(1)>hello` was
 * returned entirely unsanitised. Through the method it becomes
 * `<img src="x">hello`.
 *
 * Called with no configuration on purpose, so DOMPurify's default profile
 * applies. That profile already removes scripts, event-handler attributes and
 * `javascript:` URLs, and a bespoke allow-list here would be a second policy to
 * keep correct for no benefit.
 */
export const sanitizeUserInput = (input: string): string => {
  return DOMPurify.sanitize(input);
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
 * forward an unsanitised review by forgetting a step.
 *
 * Validation runs BEFORE sanitisation, deliberately. The length bound is then
 * measured against the text the user actually typed, which is what a live
 * character counter shows them, so counter and validator can never disagree.
 * Sanitising afterwards may change the length in either direction (quoting a
 * bare attribute value lengthens it), so a review accepted here can still draw a
 * 422 — that is the server being authoritative, as above, not a fault here.
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
export const validateRatingInput = (input: unknown): RatingCreate => {
  const validatedData = RatingCreateSchema.parse(input);
  const { review } = validatedData;

  // Sanitise only when there is text to sanitise. `review` is optional, so an
  // absent key must stay absent: rebuilding the object unconditionally would
  // add `review: undefined` to a submission that never carried the field. An
  // empty string likewise stays an empty string rather than becoming undefined.
  if (typeof review === 'string' && review.length > 0) {
    return { ...validatedData, review: sanitizeUserInput(review) };
  }

  return validatedData;
};