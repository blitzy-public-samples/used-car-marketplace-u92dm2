import { z } from 'zod';

/**
 * Zod contracts for the bidirectional peer reputation system.
 *
 * A buyer rates the seller and the seller rates the buyer for a purchase they
 * both took part in, implementing F010 "Review and Rating System" from
 * `documentation/Software Requirements Specifications (SRS).md` (L431,
 * sub-requirements L437-L441). F010-2 written review is carried by
 * `Rating.review`; F010-3 aggregate display by `RatingAggregate` together with
 * `ratingAverage`/`ratingCount` on `./user`; F010-4 moderation by
 * `moderationStatus`/`moderationReason`. F010-5 search-ranking integration is
 * deliberately out of scope, so no ranking, weight, boost or relevance field
 * appears anywhere below — this module supplies the enabling data and nothing
 * more.
 *
 * THIS IS A MIRROR, NOT A DESIGN
 * -----------------------------------------------------------------------------
 * Every declaration here mirrors `backend/app/schema/rating.py` 1:1, snake_case
 * on the server becoming camelCase on the client, per the project's dual-schema
 * convention. The server is the authoritative validator: because Pydantic
 * validates the request body before any handler logic runs, an out-of-range or
 * non-integer score is refused with a 422 whether or not it ever reached these
 * schemas. What this module adds is a fast, local, identical answer, so a form
 * can refuse an impossible submission without a round trip.
 *
 * The corollary matters as much as the rule: passing validation here guarantees
 * nothing. The two authorization gates — the rater must be a verified user, and
 * both parties must be counterparties of the same transaction — are enforced
 * server-side and are invisible to a schema. A payload that parses cleanly can
 * still legitimately be answered 403 (not verified, or not a participant), 409
 * (already rated, or the transaction is not completed) or 422. Consumers must
 * handle those outcomes rather than treating a successful parse as permission.
 *
 * KEYS ARE camelCase; VALUES ARE NOT
 * -----------------------------------------------------------------------------
 * The wire format is snake_case, because FastAPI serialises the Pydantic models
 * as declared. These schemas are camelCase. Parsing a raw wire payload directly
 * against them therefore FAILS by design, and that is not an oversight: the
 * snake_case <-> camelCase adaptation is owned in both directions by
 * `../services/rating`, which is the single place the boundary is crossed.
 *
 * Consequently this module deliberately contains no snake_case aliases, no
 * duplicate keys, no key-renaming `.transform()`, and no second wire-shaped
 * schema per model. One camelCase schema per model is exactly what the service
 * adapts to, and adding a parallel shape would create two sources of truth for
 * the same document.
 *
 * Enumeration VALUES are the exception that proves the rule. `buyer_to_seller`,
 * `seller_to_buyer`, `pending`, `approved` and `rejected` stay snake_case and
 * lower-case on the client, because they are data travelling over the wire
 * rather than field names. Only keys are re-cased at this boundary.
 *
 * TIMESTAMPS ARE `Date`, AND THE SERVICE PERFORMS THE CONVERSION
 * -----------------------------------------------------------------------------
 * `createdAt` and `updatedAt` are `z.date()`, matching every sibling schema in
 * this folder (`./user`, `./listing`, `./transaction`, `./message`). JSON has no
 * date type, so the wire carries ISO 8601 strings and `../services/rating`
 * converts them to `Date` as part of the same adaptation that re-cases the keys.
 * `z.coerce.date()` was deliberately NOT used: it would accept the raw string
 * and quietly split the responsibility for one boundary across two modules.
 *
 * This is safe rather than merely conventional. The server never emits a null or
 * sentinel timestamp on a response: `backend/app/services/rating.py` stamps a
 * real UTC datetime onto the model it returns from both the create path and the
 * moderation path, precisely because the value actually written to Firestore is
 * a server-side sentinel that cannot be serialised. A response therefore always
 * carries two real timestamps.
 *
 * WHAT IS ABSENT, AND WHY
 * -----------------------------------------------------------------------------
 * There is no `RatingUpdate`, `RatingEdit` or `RatingPatch` schema. Reputation
 * records are append-only: a submitted score is never rewritten, so no shape
 * exists to rewrite one with. A correction is a moderation-state transition that
 * records why, which is what `PATCH /api/ratings/{ratingId}/moderation` performs
 * on the moderation fields alone, leaving the original score and words intact.
 *
 * Nothing here sanitises. `review` is BOUNDED at its maximum length and is
 * otherwise passed through untouched — no `.transform()` strips, escapes or
 * rewrites an author's words. Sanitisation belongs to the existing DOMPurify
 * wrapper `sanitizeUserInput` in `../utils/validation` and to the submission
 * form; the server independently normalises the text it stores. A schema that
 * silently rewrote content would make the value a reader sees differ from the
 * value that was validated.
 */

/**
 * Lowest permitted score, inclusive.
 *
 * Mirrors `settings.RATING_MIN` (`backend/app/core/config.py`), which the
 * backend binds into the Pydantic field bound at class-definition time. The 1..5
 * scale is fixed by the project's own success metric — "4.5/5 star average
 * rating from both buyers and sellers" — and is shared with the five options of
 * the star control.
 *
 * Exported because `../components/StarRatingInput` renders the range and
 * `../components/RatingSubmissionForm` validates against it; one shared constant
 * beats three copies of a magic number.
 *
 * Declared as a bare numeric literal, with no `as const` and no explicit
 * annotation, and both halves of that are deliberate.
 *
 * `as const` is avoided because these are numeric BOUNDS, not discriminants:
 * freezing one into a readonly literal type buys no safety and would make it
 * awkward to use in the arithmetic every consumer performs on it. A bare `const`
 * still carries the widening literal type `1`, which behaves as `number` in every
 * inference position that matters — `useState(RATING_MIN)` infers
 * `number`, and `RATING_MAX - RATING_MIN + 1` is a `number` — so arithmetic and
 * state both work without ceremony.
 *
 * An explicit `: number` annotation was tried and rejected: it is redundant to
 * the compiler and the project's ESLint configuration reports it as an error
 * (`@typescript-eslint/no-inferrable-types`), which the lint gate treats as
 * fatal. Should a consumer ever need the widened type in an explicit generic
 * position, it can write `number` there directly.
 */
export const RATING_MIN = 1;

/**
 * Highest permitted score, inclusive. Mirrors `settings.RATING_MAX`.
 */
export const RATING_MAX = 5;

/**
 * Maximum length of the free-text review.
 *
 * Mirrors `settings.RATING_REVIEW_MAX_LENGTH`. Exported because
 * `../components/RatingSubmissionForm` needs the figure for its live character
 * counter, and a counter that disagrees with the validator is worse than no
 * counter at all.
 *
 * This is the SEMANTIC bound, the one that means "2000 characters" to a person
 * writing a review. The server applies the same number to the text it has
 * normalised — Unicode composed, control characters removed, blank runs
 * collapsed — which is the text that actually gets stored and read back, so the
 * counter here and the limit there describe the same thing. The server carries a
 * second, far larger ceiling on the raw request body purely to keep an unbounded
 * string away from its normaliser; that is a denial-of-service guard rather than
 * a contract, so it is deliberately not mirrored. Normalisation can only ever
 * shorten text, so a review this bound accepts can never be one the server's
 * bound rejects.
 */
export const REVIEW_MAX_LENGTH = 2000;

/**
 * Maximum length of a moderator's recorded reason.
 *
 * Mirrors `MODERATION_REASON_MAX_LENGTH` in `backend/app/schema/rating.py`. It
 * is deliberately far smaller than the review bound: a policy citation is a
 * short phrase such as "contains a phone number", written by staff rather than
 * by the public.
 *
 * Module-local rather than exported. No component consumes the figure — the
 * moderation surface is API-only in this release — and an export with no
 * consumer is public surface area that has to be maintained for nothing.
 */
const MODERATION_REASON_MAX_LENGTH = 500;

/**
 * Maximum length of a transaction reference accepted for submission.
 *
 * Mirrors `DOCUMENT_ID_MAX_LENGTH` in `backend/app/schema/rating.py`, which
 * holds a document ID well inside Firestore's own limit because two IDs are
 * joined to form a rating's key, so each half must leave room for the other. A
 * Firestore auto-ID occupies twenty characters, so this is generous.
 */
const TRANSACTION_ID_MAX_LENGTH = 128;

/**
 * Path-safety grammar for a transaction reference supplied by a client.
 *
 * Requires at least one character and forbids both the path separator and any
 * whitespace. This mirrors the intent of the backend's `DocumentId` constraint
 * for the one value in this whole module that travels from a client INTO a
 * document path — `transactionId` on a submission addresses the transaction the
 * server reads and, composed with the rater's ID, the rating it creates.
 *
 * Unconstrained, that value admits two failures worth catching at the boundary:
 * a value containing `/` is read by the Firestore client as a nested path rather
 * than as an ID, and a value containing a newline is how a log line gets forged.
 * The empty case is the one most likely to occur in practice rather than in
 * theory — an unresolved route parameter stringifies to nothing, and a request
 * built from it would otherwise be sent and refused remotely instead of being
 * refused here with something a form can display.
 *
 * The grammar is deliberately a little WIDER than the server's. The backend also
 * rejects `.`, `..` and Firestore's reserved `__*__` namespace; those are
 * datastore-internal edge cases that the authoritative validator still catches,
 * and encoding them here would buy nothing while rejecting values such as
 * `__test__` that are perfectly reasonable in a fixture.
 *
 * Wider is the safe direction for a convenience layer, and the asymmetry is
 * worth stating because it is easy to get backwards. Being wider than the server
 * costs at most a remote 422 on a value no legitimate caller sends. Being
 * NARROWER would refuse a reference the server would have accepted, blocking a
 * rating that was genuinely permitted — a local rule silently overriding the
 * authoritative one. So this pattern is allowed to admit a little more than the
 * server, and must never admit less.
 */
const TRANSACTION_ID_PATTERN = /^[^/\s]+$/;

/**
 * Which way along a transaction a rating travels — the R0 bidirectionality
 * discriminator, and the reason this feature is a peer reputation system rather
 * than a seller review system.
 *
 * Modelled with `z.enum` rather than `z.string()` so the inferred type is a
 * union of literals. That is the whole point: a consumer switching on direction
 * gets exhaustiveness checking and narrowing, and a typo becomes a compile
 * error instead of a branch that silently never runs.
 *
 * The value is derived SERVER-SIDE from the cited transaction document and is
 * never accepted from a client — which is why it is absent from
 * `RatingCreateSchema`. That absence is what makes direction spoofing
 * structurally impossible rather than merely validated against.
 */
export const RatingDirectionSchema = z.enum([
  'buyer_to_seller',
  'seller_to_buyer'
]);

export type RatingDirection = z.infer<typeof RatingDirectionSchema>;

/**
 * Policy state governing whether a review's text may be displayed.
 *
 * Exactly three states, and none of them is derived from the score. Transitions
 * are driven by policy violations only — abuse, personally identifying
 * information, profanity — and never by how low a rating is. A one-star rating
 * is not a violation.
 *
 * That constraint is not stylistic, which is why no fourth member and no
 * score-correlated field exists anywhere in this module: the FTC Rule on the Use
 * of Consumer Reviews and Testimonials (16 CFR Part 465) prohibits suppressing
 * reviews on the basis of rating or negative sentiment. The aggregate
 * consequently counts every published rating whatever its value, and the score
 * is shown for every published rating regardless of moderation state — it is the
 * free-text review, the only part a policy violation can live in, that is
 * withheld until `approved`.
 */
export const ModerationStatusSchema = z.enum([
  'pending',
  'approved',
  'rejected'
]);

export type ModerationStatus = z.infer<typeof ModerationStatusSchema>;

/**
 * One directional rating, as persisted and as returned by the read endpoints.
 *
 * Mirrors the thirteen fields of `Rating` in `backend/app/schema/rating.py`, in
 * the same order.
 *
 * `review` and `moderationReason` are NULLABLE rather than optional, and that
 * distinction is load-bearing. The server returns both keys always, carrying
 * `null` when there is no value, so modelling either as a bare `z.string()`
 * would reject the commonest rating there is — one with no written review. Both
 * are routinely null on a public read: the server withholds review text until
 * moderation has approved it, and redacts the moderator's internal note on
 * every path, approved or not.
 */
export const RatingSchema = z.object({
  /** Document ID, composed server-side as `{transactionId}_{raterId}`. */
  id: z.string(),
  /** The transaction whose participation authorises this rating. */
  transactionId: z.string(),
  /**
   * Denormalised from the transaction so a rating can be rendered with its
   * context — "rated after buying this car" — without a second document read.
   */
  vehicleListingId: z.string(),
  /** Author of the rating. Always the authenticated caller, server-side. */
  raterId: z.string(),
  /** Recipient. Derived server-side as the transaction's other participant. */
  rateeId: z.string(),
  direction: RatingDirectionSchema,
  /**
   * The vote. An integer within the inclusive scale — never a fraction, and
   * never a rounded one. The server is strict about this for a security reason
   * rather than a tidiness one: a coercing validator would turn 4.7 into 4 and
   * record a score the rater never chose.
   */
  score: z.number().int().min(RATING_MIN).max(RATING_MAX),
  /** Free-text review (F010-2), or null when unwritten or withheld. */
  review: z.string().max(REVIEW_MAX_LENGTH).nullable(),
  /**
   * Whether this rating is visible and counted.
   *
   * Ratings are created unpublished. Under the double-blind reveal a rating
   * becomes visible only once the counterparty submits theirs or the rating
   * window elapses, and the aggregate reflects published ratings only — which is
   * what removes the incentive for review extortion. This is a first-class
   * stored field rather than something a client can infer, and an unpublished
   * rating is recorded rather than lost: a submission that is not yet visible
   * must be reported to its author as exactly that, never as a silent failure.
   */
  isPublished: z.boolean(),
  moderationStatus: ModerationStatusSchema,
  /** Policy basis recorded by a moderator. Never a score-based reason. */
  moderationReason: z.string().max(MODERATION_REASON_MAX_LENGTH).nullable(),
  createdAt: z.date(),
  updatedAt: z.date()
});

export type Rating = z.infer<typeof RatingSchema>;

/**
 * The request body accepted by `POST /api/ratings`.
 *
 * Three keys, written out literally, and no more. This is the most important
 * shape in the module and its narrowness is the point.
 *
 * `raterId`, `rateeId` and `direction` are absent BY DESIGN. The rater is the
 * authenticated caller and the other two are derived server-side from the cited
 * transaction, where the counterparty is computed as "the seller if the caller
 * is the buyer, otherwise the buyer". Because they are derived rather than
 * accepted, there is no input a client can supply that produces a self-rating or
 * redirects a rating to a third party — counterparty spoofing is structurally
 * impossible instead of merely validated against. Never add them here for
 * convenience or for symmetry with `RatingSchema`.
 *
 * That is also why this schema is declared standalone rather than derived from
 * `RatingSchema` with `.pick()`, `.omit()`, `.partial()`, `.extend()` or
 * `.merge()`. Every one of those leaves the forbidden values reachable on the
 * inferred type — as optional or as never-quite-removed members — which defeats
 * the guarantee. Three literal keys cannot drift into carrying a fourth.
 *
 * The schema is intentionally NOT `.strict()`. Zod's default object behaviour is
 * to strip unrecognised keys, so parsing an over-supplied object through this
 * schema yields exactly the three permitted fields and a stray `rateeId` never
 * reaches the wire at all. Refusing the object instead would trade a guarantee
 * for an error, and the guarantee is worth more: the server tolerates a supplied
 * `ratee_id` only so that it can compare the claim against the derived
 * counterparty and answer 403, and the best outcome is that the claim is never
 * sent. Note that the server refuses every OTHER unrecognised key with a 422, so
 * stripping here also keeps a caller's mistake from becoming a rejected request.
 *
 * `review` is `.optional()` here while it is `.nullable()` on `RatingSchema`. The
 * asymmetry is correct and intentional: a client simply omits the key when there
 * is nothing to say, whereas the server always returns the key and uses null to
 * say the same thing. Do not harmonise them.
 */
export const RatingCreateSchema = z.object({
  /**
   * The transaction being rated. Bounded rather than a bare string because this
   * is the one client-supplied value that constructs a document path; see
   * `TRANSACTION_ID_PATTERN`.
   */
  transactionId: z
    .string()
    .min(1, 'A transaction reference is required')
    .max(
      TRANSACTION_ID_MAX_LENGTH,
      `A transaction reference must be at most ${TRANSACTION_ID_MAX_LENGTH} characters`
    )
    .regex(
      TRANSACTION_ID_PATTERN,
      'A transaction reference must not contain spaces or "/"'
    ),
  score: z
    .number()
    .int('A rating must be a whole number of stars')
    .min(RATING_MIN, `A rating must be at least ${RATING_MIN}`)
    .max(RATING_MAX, `A rating must be at most ${RATING_MAX}`),
  review: z
    .string()
    .max(
      REVIEW_MAX_LENGTH,
      `A review must be at most ${REVIEW_MAX_LENGTH} characters`
    )
    .optional()
});

export type RatingCreate = z.infer<typeof RatingCreateSchema>;

/**
 * Denormalised reputation summary for a single user (F010-3).
 *
 * Mirrors `ratingAverage`/`ratingCount` on `./user`, which the server maintains
 * inside the same transaction that publishes a rating so that reading a profile
 * costs one document read rather than a scan of every rating a user received.
 *
 * `average` is NULLABLE, and null is a first-class state meaning "no ratings
 * yet". It is not an error and it must never be defaulted to 0:
 * `../components/ReputationBadge` renders an explicit empty state off exactly
 * this null, and a zero would instead render as a genuine, earned one-star
 * reputation — the worst possible thing to show a user who has simply never been
 * rated. The two fields are consistent by construction on the server, where
 * `count === 0` if and only if `average === null`.
 *
 * No bound is placed on `average`. It is a rounded running mean the server owns
 * and validates, and a client that refused to display a server-computed figure
 * would turn a rounding artefact into a broken profile page. `count` mirrors the
 * expression already used for `ratingCount` on `./user`, so the two
 * representations of one reputation validate identically.
 *
 * Reflects published ratings only, and includes every one of them whatever the
 * score.
 */
export const RatingAggregateSchema = z.object({
  average: z.number().nullable(),
  count: z.number().int()
});

export type RatingAggregate = z.infer<typeof RatingAggregateSchema>;

/**
 * Outcome of the rating eligibility guard sequence.
 *
 * Returned by `GET /api/ratings/eligibility/{transactionId}` so the interface
 * can disable the submission control with a specific explanation, instead of
 * letting someone compose a rating and only then fail on a 403. Reporting the
 * reason up front is what makes an unavailable control perceivable rather than
 * merely inert, which is an accessibility obligation and not a nicety.
 *
 * `reason` is PROSE, rendered verbatim. It holds sentences composed by the
 * server — "Only verified users can submit ratings", "You have already rated
 * this transaction", "Ratings require a completed transaction" — and it is
 * deliberately not a `z.enum` of codes the client maps to its own copy. Two
 * sources of wording for one decision drift apart, and the server already needs
 * the sentence for its own error detail. It is nullable because there is nothing
 * to explain when the caller is eligible.
 *
 * `rateeId` and `direction` are nullable because the server can only derive them
 * once the caller is confirmed a participant; a decision that never got that far
 * — an unknown transaction, or a caller who is party to neither side — reports
 * them as null. Modelling them as nullable is what lets a form render the
 * disabled-with-reason state instead of failing on absent data.
 */
export const EligibilityDecisionSchema = z.object({
  eligible: z.boolean(),
  reason: z.string().nullable(),
  rateeId: z.string().nullable(),
  direction: RatingDirectionSchema.nullable(),
  /**
   * Whether this caller has already rated this transaction. Distinct from
   * `eligible`, because "you have had your say" and "you were never entitled to
   * one" are different states that a profile or a form should not conflate.
   */
  alreadyRated: z.boolean()
});

export type EligibilityDecision = z.infer<typeof EligibilityDecisionSchema>;

/**
 * Payload of `GET /api/ratings/user/{userId}` — the ratings a user has received,
 * together with the aggregate computed from them.
 *
 * The two travel together because they must agree: the list a reader can see has
 * to account for the average they are shown. Published ratings only, so an
 * unpublished rating appears in neither half.
 */
export const UserRatingsResponseSchema = z.object({
  items: z.array(RatingSchema),
  aggregate: RatingAggregateSchema
});

export type UserRatingsResponse = z.infer<typeof UserRatingsResponseSchema>;
