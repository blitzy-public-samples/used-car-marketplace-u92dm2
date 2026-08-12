import axios, { AxiosInstance } from 'axios';
import {
  EligibilityDecisionSchema,
  RatingAggregateSchema,
  RatingSchema,
  UserRatingsResponseSchema,
  type EligibilityDecision,
  type ModerationStatus,
  type Rating,
  type RatingAggregate,
  type RatingCreate,
  type UserRatingsResponse
} from '../schema/rating';

/**
 * Typed client for the bidirectional peer reputation API, and the single owner
 * of the snake_case <-> camelCase adaptation for the rating domain.
 *
 * Implements the client half of F010 "Review and Rating System"
 * (`documentation/Software Requirements Specifications (SRS).md` L431). A buyer
 * rates the seller and the seller rates the buyer for a purchase they both took
 * part in. F010-5, integrating ratings into search-result ranking, is
 * deliberately out of scope: nothing here touches `GET /listings` or issues a
 * listings request of any kind.
 *
 * THE TWO AUTHORIZATION GATES LIVE ON THE SERVER
 * -----------------------------------------------------------------------------
 * Only a verified user may rate, and only the two counterparties of the same
 * completed transaction may rate each other. Both are enforced in
 * `backend/app/services/rating.py` and surface as 403. This module's job is to
 * REPORT that decision — through `fetchRatingEligibility` — and to transmit
 * nothing that could spoof it. It never makes the decision itself, and a request
 * this module is willing to send may still be refused. A successful local
 * validation is not permission.
 *
 * `raterId`, `rateeId` and `direction` are therefore absent from every request
 * body constructed here. The rater is the authenticated caller and the other two
 * are derived server-side from the cited transaction, which is what makes
 * counterparty spoofing and self-rating structurally impossible rather than
 * merely validated against. `toRatingCreateWire` builds its body from three
 * literal keys for exactly this reason — never by spreading a wider object,
 * which would let an extra key reach the wire.
 *
 * BASE PATH: `REACT_APP_API_BASE_URL` IS EXPECTED TO INCLUDE `/api`
 * -----------------------------------------------------------------------------
 * Every path below omits the `/api` segment — `'/ratings'`, not
 * `'/api/ratings'` — matching the existing convention in `./api`, whose methods
 * call `'/listings'` and `'/upload'` against a backend that mounts them under
 * `prefix='/api/listings'`. So `REACT_APP_API_BASE_URL` must carry the prefix,
 * for example `http://localhost:8000/api`. With that base the five paths resolve
 * to the five endpoints the router declares relative to its own
 * `prefix='/api/ratings'`:
 *
 *   POST   /api/ratings                              submit a rating
 *   GET    /api/ratings/user/{userId}                published ratings + aggregate
 *   GET    /api/ratings/transaction/{transactionId}  a transaction's ratings
 *   GET    /api/ratings/eligibility/{transactionId}  may the caller rate?
 *   PATCH  /api/ratings/{ratingId}/moderation        admin-only state transition
 *
 * The ratings router declares its paths relative to its prefix on purpose,
 * avoiding the double-prefix defect the other three routers carry (their
 * `@router.post('/listings')` under `prefix='/api/listings'` resolves to
 * `/api/listings/listings`). Do not add a resource segment here to "match" them.
 *
 * TIMESTAMPS: THE WIRE CARRIES ISO STRINGS, THIS MODULE PRODUCES `Date`
 * -----------------------------------------------------------------------------
 * `../schema/rating` declares `createdAt`/`updatedAt` as `z.date()` — matching
 * every sibling schema — and deliberately NOT `z.coerce.date()`, so that one
 * boundary has one owner. JSON has no date type, so the conversion is performed
 * here as part of the same adaptation that re-cases the keys. The server stamps
 * `datetime.now(timezone.utc)` on the model it returns, so the ISO strings carry
 * a `+00:00` offset and are unambiguous to `new Date`.
 *
 * ERRORS ARE NEVER SWALLOWED
 * -----------------------------------------------------------------------------
 * The server's own message is the message the interface renders. The router
 * reuses each domain exception's human-readable text as `HTTPException.detail`
 * precisely so those strings match the `reason` an `EligibilityDecision`
 * carries, so every failure here rejects with the original error and
 * `error.response.status` and `error.response.data.detail` intact:
 *
 *   401  no credentials
 *   403  the rater is not verified, or is not a party to the transaction
 *   404  no such transaction, user or rating
 *   409  already rated, or the transaction is not completed
 *   422  score out of range, review too long, or a self-rating
 *
 * There is deliberately no client-side status-to-message table: a second source
 * of wording for one decision drifts away from the first. Nor is there a
 * `catch` that returns `null` or `{ success: false }` — both patterns exist
 * elsewhere in this folder and both destroy the server's message.
 *
 * DOUBLE-BLIND PUBLICATION IS THE DESIGN, NOT A BUG TO WORK AROUND
 * -----------------------------------------------------------------------------
 * `fetchUserRatings` returns published ratings only, and the aggregate counts
 * published ratings only. A rating that has just been submitted legitimately
 * appears in neither until the counterparty submits theirs or the rating window
 * elapses; that reveal is what removes the incentive for review extortion. No
 * optimistic insertion, no local cache and no client-side workaround pretends
 * otherwise. Re-fetching eligibility after a successful submit is the correct
 * follow-up, because that is where the caller learns `alreadyRated`.
 *
 * WHAT IS ABSENT, AND WHY
 * -----------------------------------------------------------------------------
 * There is no `updateRating`, `editRating` or `deleteRating`, and no PUT or
 * DELETE call. Reputation records are append-only: a submitted score is never
 * rewritten, and a correction is the moderation transition below, which records
 * a policy basis and leaves the original score and words intact.
 *
 * No function here reads, branches on, filters by, sorts by or defaults from a
 * `score`. Moderation is sentiment-neutral: the FTC Rule on the Use of Consumer
 * Reviews and Testimonials (16 CFR Part 465) prohibits suppressing reviews on
 * the basis of rating or negative sentiment, so score-correlated behaviour is a
 * compliance failure and not merely a style choice.
 *
 * Nothing here sanitises or truncates. `review` is transmitted verbatim.
 * Sanitisation belongs to `sanitizeUserInput` in `../utils/validation`, which
 * `validateRatingInput` already applies before a submission reaches this module.
 */

/**
 * Base URL for every request issued by this module.
 *
 * Read through `process.env.REACT_APP_API_BASE_URL`, which is this project's
 * declared convention and is what `./api` reads. `vite.config.ts` serves it two
 * ways: `envPrefix: 'REACT_APP_'` publishes the variable on `import.meta.env`,
 * and a `define` entry statically replaces this exact member expression with the
 * loaded value. The `define` block is the one that matters here, and it names
 * this module explicitly. The read is therefore left bare — wrapping it in a
 * `typeof process` guard would be actively harmful, because the substitution
 * replaces only the inner expression and the surviving guard evaluates false in
 * a browser bundle, silently yielding no base URL at all.
 */
const API_BASE_URL = process.env.REACT_APP_API_BASE_URL;

/**
 * Path prefix shared by all five endpoints, relative to `API_BASE_URL`.
 */
const RATINGS_PATH = '/ratings';

/**
 * Browser-storage key holding the bearer token.
 *
 * `./auth` persists the token under this exact key on a successful login
 * (`setItem('authToken', response.data.token)`), so this is where a token is
 * found rather than a name invented here.
 */
const AUTH_TOKEN_STORAGE_KEY = 'authToken';

/**
 * Read the bearer token, tolerating every context in which storage is absent.
 *
 * `./api` obtains the token from `getAuthToken` in `app/utils/auth`, and
 * `./auth` from a `getItem` wrapper in `app/utils/storage`. Neither module
 * exists — `src/app` is not a directory in this project — and creating either is
 * out of scope, so their absence is already a compile error attributed to those
 * two files. Importing the same missing specifier from a new module would add a
 * new one, so the value is read here from the key `./auth` writes.
 *
 * Guarded twice, for two distinct failures. `localStorage` is undefined outside
 * a DOM, so an unguarded read would throw under a `node`-environment test or in
 * any server-side render. Access can also throw when it IS defined — Safari's
 * private mode and a blocked-cookies policy both raise on the property — so the
 * call itself is wrapped.
 *
 * Returning `null` on either failure is not error suppression. No token means an
 * unauthenticated request, which the server answers with a 401 that propagates
 * to the caller untouched, exactly as an expired token would. The alternative,
 * throwing here, would replace a specific and actionable server response with an
 * opaque client-side crash.
 */
const readAuthToken = (): string | null => {
  if (typeof localStorage === 'undefined') {
    return null;
  }

  try {
    return localStorage.getItem(AUTH_TOKEN_STORAGE_KEY);
  } catch {
    return null;
  }
};

/**
 * Build an axios instance carrying the bearer token and preserving errors.
 *
 * Mirrors `createApiInstance` in `./api`, which is a module-local `const` with
 * no `export` keyword. It is genuinely unexported, so the shape is replicated
 * here rather than imported. The reverse direction is also deliberate: `./api`
 * imports the mappers below, so this module must never import `./api` back.
 *
 * The response interceptor is load-bearing for the whole feature's UX. It logs
 * and then rejects with the ORIGINAL error, unwrapped and unreplaced, which is
 * the only reason `error.response.data.detail` survives to be rendered verbatim
 * by the submission form. Constructing a new error here — however tidy the
 * message — would discard the server's own explanation of the refusal.
 */
const createRatingApiInstance = (): AxiosInstance => {
  const instance = axios.create({
    baseURL: API_BASE_URL
  });

  instance.interceptors.request.use((config) => {
    const token = readAuthToken();
    if (token) {
      config.headers['Authorization'] = `Bearer ${token}`;
    }
    return config;
  });

  instance.interceptors.response.use(
    (response) => response,
    (error) => {
      console.error('Rating API request failed:', error);
      return Promise.reject(error);
    }
  );

  return instance;
};

/**
 * Raised when a 2xx response cannot be interpreted as the contract it claims.
 *
 * Distinct from a transport or HTTP failure, and deliberately so. An axios error
 * means the server refused the request and carries a `response.status` and a
 * `response.data.detail` the interface renders; this means the server ACCEPTED
 * the request and answered with something that does not match
 * `../schema/rating`. The two demand different responses from a developer — the
 * first is a normal, expected outcome, the second is a contract breach — so they
 * are never conflated into one error type.
 *
 * Exported so a caller can tell them apart with `instanceof` and so a
 * misinterpreted response is never mistaken for a rejected one.
 *
 * A parse failure is always surfaced. There is no fallback to unvalidated data
 * and no default value: an aggregate or a rating that failed validation is not
 * something to render approximately.
 *
 * Carries no ES2022 `cause`. `tsconfig.json` targets ES2020, so the underlying
 * failure is exposed as an explicit `originalError` property instead. For a
 * validation failure that is the `ZodError`, whose `issues` name every offending
 * field.
 */
export class RatingContractError extends Error {
  /** Label of the endpoint whose response could not be interpreted. */
  readonly endpoint: string;

  /** The underlying failure, typically a `ZodError` with per-field issues. */
  readonly originalError: unknown;

  constructor(endpoint: string, reason: string, originalError?: unknown) {
    super(`${endpoint} returned a response that does not match the rating contract: ${reason}`);
    this.name = 'RatingContractError';
    this.endpoint = endpoint;
    this.originalError = originalError;
  }
}

/**
 * Reduce an unknown thrown value to a message worth showing a developer.
 *
 * A `ZodError` is an `Error` whose `message` is the formatted issue list, so the
 * common case needs no special handling and no import of zod's error class.
 */
const describeDecodeFailure = (error: unknown): string => {
  if (error instanceof Error && error.message.length > 0) {
    return error.message;
  }

  return 'the payload did not match the expected shape';
};

/**
 * Run a response mapping, labelling any failure with the endpoint it came from.
 *
 * Called only AFTER the request has already resolved, which is the structural
 * guarantee that this helper can never intercept an HTTP failure: an axios
 * rejection propagates out of the `await` before the mapping is reached, so 401,
 * 403, 404, 409 and 422 all reach the caller untouched, with their `detail`
 * intact. The only thing that can throw inside here is the interpretation of a
 * response the server already considered a success.
 *
 * @param endpoint Human-readable label such as `POST /ratings`, used verbatim in
 *   the error message so a failure names where it came from.
 * @param decodeResponse Thunk performing the key mapping and schema validation.
 * @throws {RatingContractError} When the payload cannot be interpreted.
 */
const decode = <T>(endpoint: string, decodeResponse: () => T): T => {
  try {
    return decodeResponse();
  } catch (error) {
    throw new RatingContractError(endpoint, describeDecodeFailure(error), error);
  }
};

/**
 * Convert one wire timestamp to a `Date`, or fail loudly.
 *
 * `../schema/rating` declares `createdAt`/`updatedAt` as a required `z.date()`,
 * and the server guarantees both on every response because it stamps a real UTC
 * datetime onto the model it returns — the value actually written to Firestore is
 * a server-side sentinel that cannot be serialised, so it is replaced before the
 * response is built. The Pydantic model nevertheless types both fields as
 * `Optional[Any]`, so "absent" is representable on the wire and is handled here
 * rather than assumed away.
 *
 * Neither failure mode is papered over. `new Date(undefined)` yields an Invalid
 * Date, which `z.date()` rejects with a message that names neither the field nor
 * the reason, and substituting `new Date()` would be worse still: it would
 * fabricate a timestamp the server never sent and display it as fact. So an
 * absent or unparseable value throws here, where the field can be named, and
 * `decode` adds the endpoint.
 *
 * @param value Raw wire value: an ISO 8601 string, or null/absent.
 * @param field Dotted field name used in the error message.
 * @throws {TypeError} When the value is absent, blank or not a valid timestamp.
 */
const toDate = (value: string | null | undefined, field: string): Date => {
  if (typeof value !== 'string' || value.trim().length === 0) {
    const received = value === null ? 'null' : typeof value;
    throw new TypeError(
      `${field} is required: expected an ISO 8601 timestamp, received ${received}`
    );
  }

  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) {
    throw new TypeError(`${field} is not a valid ISO 8601 timestamp: "${value}"`);
  }

  return parsed;
};

/*
 * -----------------------------------------------------------------------------
 * THE WIRE SHAPES
 * -----------------------------------------------------------------------------
 * FastAPI serialises the Pydantic models exactly as declared, so the wire is
 * snake_case while `../schema/rating` is camelCase. Parsing a raw payload
 * straight against those schemas therefore fails by design, and this module is
 * the single place the boundary is crossed.
 *
 * The shapes are written out explicitly rather than derived, and no generic
 * recursive case-converter is used. That is the whole safety argument: with a
 * declared wire interface, renaming a field on either side of the boundary
 * becomes a COMPILE error, whereas a converter would map the new name to a key
 * nobody reads and hand back a silent `undefined`.
 *
 * Optionality mirrors `backend/app/schema/rating.py` field by field, so a value
 * the server may legitimately omit is declared as omittable here and handled by
 * the mappers, rather than being asserted present and crashing at run time.
 *
 * Enumerated fields are typed as `string` on the way in, because a wire value is
 * unvalidated until a schema has seen it. The Zod enums narrow them to their
 * literal unions during mapping, which is what turns an unrecognised value into
 * a legible failure instead of a bad cast.
 *
 * These interfaces are exported so `./api` can apply the mappers with the same
 * type safety rather than re-deriving the boundary, and so the wire contract this
 * module owns is documented in the type system rather than only in prose.
 */

/**
 * `Rating` as it travels: the thirteen fields of the Pydantic model, in order.
 *
 * `review` and `moderation_reason` are omittable-or-null because the server
 * returns the key carrying `null` when there is no value — review text is
 * withheld until moderation approves it, and the moderator's internal note is
 * redacted on every path. `created_at`/`updated_at` are declared the same way
 * because the Pydantic model types them `Optional[Any]`; see `toDate`.
 */
export interface RatingWire {
  id: string;
  transaction_id: string;
  vehicle_listing_id: string;
  rater_id: string;
  ratee_id: string;
  direction: string;
  score: number;
  review?: string | null;
  is_published: boolean;
  moderation_status: string;
  moderation_reason?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
}

/**
 * `RatingAggregate` as it travels.
 *
 * `average` is omittable-or-null because null is a first-class state meaning "no
 * ratings yet" (`Optional[float] = None` on the server), not an error and not a
 * synonym for zero.
 */
export interface RatingAggregateWire {
  average?: number | null;
  count: number;
}

/**
 * Envelope returned by `GET /api/ratings/user/{userId}`.
 *
 * The two halves travel together because they must agree: the list a reader can
 * see has to account for the average they are shown. Both cover published
 * ratings only.
 */
export interface UserRatingsResponseWire {
  items: RatingWire[];
  aggregate: RatingAggregateWire;
}

/**
 * `EligibilityDecision` as it travels.
 *
 * `reason`, `ratee_id` and `direction` are all omittable-or-null. There is
 * nothing to explain when the caller is eligible, and the server can only derive
 * the counterparty and the direction once it has confirmed the caller is a
 * participant — a decision that never got that far reports both as null.
 */
export interface EligibilityDecisionWire {
  eligible: boolean;
  reason?: string | null;
  ratee_id?: string | null;
  direction?: string | null;
  already_rated: boolean;
}

/**
 * Body of `POST /api/ratings`. Three keys, and there is no fourth.
 *
 * `rater_id`, `ratee_id` and `direction` are absent BY DESIGN, not by omission:
 * all three are derived server-side, and their absence from this type is what
 * makes it impossible to send them by accident. Never widen this interface.
 */
export interface RatingCreateWire {
  transaction_id: string;
  score: number;
  review?: string;
}

/**
 * Body of `PATCH /api/ratings/{ratingId}/moderation`. Exactly two keys.
 *
 * The router's model sets `extra = 'forbid'`, so any additional key — notably a
 * `score` or `review`, which the append-only contract never permits editing — is
 * refused with a 422 naming the offending field rather than silently ignored.
 * `moderation_reason` is cleared when omitted.
 */
export interface ModerationWire {
  moderation_status: ModerationStatus;
  moderation_reason?: string;
}

/*
 * -----------------------------------------------------------------------------
 * WIRE -> DOMAIN
 * -----------------------------------------------------------------------------
 * Every mapper re-cases the KEYS and leaves the VALUES alone, then validates the
 * result against its schema and returns the parsed value. Validating inside the
 * mapper rather than beside it means a mapper cannot be used without its schema,
 * so `./api` gets the same guarantee without repeating the call.
 *
 * The order is mandatory: map, then convert timestamps, then parse. Parsing a raw
 * snake_case payload first would fail on every single response.
 *
 * Enumeration values pass through untouched. `buyer_to_seller`,
 * `seller_to_buyer`, `pending`, `approved` and `rejected` stay exactly as the
 * wire spells them, because they are data rather than field names and the Zod
 * enums declare those literals. Re-casing `buyer_to_seller` to `buyerToSeller`
 * would break both the enum parse and the backend contract.
 *
 * Absent and null collapse to `null`, never to `undefined`, because the schemas
 * declare these fields `.nullable()` and the two are not interchangeable to Zod.
 * `??` is used rather than `||` so that a falsy-but-real value survives: an empty
 * `review` stays an empty string and an `average` of 0 stays 0.
 */

/**
 * Adapt one wire rating to the domain model.
 *
 * All thirteen fields are mapped; none is dropped and none is left under its
 * snake_case name. Exported so `./api` can apply the identical adaptation.
 *
 * @param wire Raw rating object from any of the four endpoints that return one.
 * @returns The validated, camelCase rating with real `Date` timestamps.
 * @throws {TypeError} When a timestamp is absent or unparseable.
 * @throws {ZodError} When any field violates `RatingSchema`.
 */
export const toRating = (wire: RatingWire): Rating =>
  RatingSchema.parse({
    id: wire.id,
    transactionId: wire.transaction_id,
    vehicleListingId: wire.vehicle_listing_id,
    raterId: wire.rater_id,
    rateeId: wire.ratee_id,
    direction: wire.direction,
    score: wire.score,
    review: wire.review ?? null,
    isPublished: wire.is_published,
    moderationStatus: wire.moderation_status,
    moderationReason: wire.moderation_reason ?? null,
    createdAt: toDate(wire.created_at, 'Rating.created_at'),
    updatedAt: toDate(wire.updated_at, 'Rating.updated_at')
  });

/**
 * Adapt one wire aggregate to the domain model.
 *
 * Both keys are single words, so nothing is re-cased; the work this mapper does
 * is preserving the difference between "no ratings yet" and "rated badly".
 * `average` stays `null` when the server says null and is NEVER defaulted to 0 —
 * `../components/ReputationBadge` renders its empty state off exactly this null,
 * and a zero would instead claim a genuine, earned one-star reputation, the worst
 * possible thing to show someone who has simply never been rated.
 *
 * @param wire Raw aggregate object.
 * @returns The validated aggregate, with `average: null` preserved as null.
 * @throws {ZodError} When either field violates `RatingAggregateSchema`.
 */
export const toRatingAggregate = (wire: RatingAggregateWire): RatingAggregate =>
  RatingAggregateSchema.parse({
    average: wire.average ?? null,
    count: wire.count
  });

/**
 * Adapt one wire eligibility decision to the domain model.
 *
 * All five fields are mapped. `reason` is prose composed by the server and is
 * carried through verbatim for the interface to render — it is deliberately not
 * translated into a local code, because two sources of wording for one decision
 * drift apart. `alreadyRated` is reported faithfully and is distinct from
 * `eligible`: "you have had your say" and "you were never entitled to one" are
 * different states that a form must not conflate.
 *
 * @param wire Raw decision object from the eligibility endpoint.
 * @returns The validated decision.
 * @throws {ZodError} When any field violates `EligibilityDecisionSchema`.
 */
export const toEligibilityDecision = (
  wire: EligibilityDecisionWire
): EligibilityDecision =>
  EligibilityDecisionSchema.parse({
    eligible: wire.eligible,
    reason: wire.reason ?? null,
    rateeId: wire.ratee_id ?? null,
    direction: wire.direction ?? null,
    alreadyRated: wire.already_rated
  });

/**
 * Adapt the user-ratings envelope, mapping every element and the aggregate.
 *
 * The envelope keys need no re-casing, but neither half may be passed through
 * raw: each item goes through `toRating` and the summary through
 * `toRatingAggregate`, so the whole payload is camelCase and validated.
 *
 * Both halves are checked for presence first. The server declares both as
 * required, so an absent one is a contract breach — but reading `.map` off
 * `undefined` reports it as an anonymous run-time error, whereas naming the field
 * says which half of the contract was broken. Neither is defaulted: substituting
 * an empty list would render a user's reputation as "no ratings yet" on the
 * strength of a malformed response.
 *
 * @param wire Raw `{ items, aggregate }` envelope.
 * @returns The validated envelope: published ratings and their aggregate.
 * @throws {TypeError} When `items` is not an array or `aggregate` is missing.
 * @throws {ZodError} When any rating or the aggregate fails validation.
 */
export const toUserRatingsResponse = (
  wire: UserRatingsResponseWire
): UserRatingsResponse => {
  if (!Array.isArray(wire.items)) {
    throw new TypeError(
      'UserRatingsResponse.items is required: expected an array of ratings'
    );
  }

  if (!wire.aggregate) {
    throw new TypeError(
      'UserRatingsResponse.aggregate is required: expected an average and a count'
    );
  }

  return UserRatingsResponseSchema.parse({
    items: wire.items.map((item) => toRating(item)),
    aggregate: toRatingAggregate(wire.aggregate)
  });
};

/*
 * -----------------------------------------------------------------------------
 * DOMAIN -> WIRE
 * -----------------------------------------------------------------------------
 * The write direction of the same boundary. Both mappers build their result from
 * literal keys, one assignment at a time, and NEITHER spreads its input. That is
 * a security property rather than a stylistic one: a spread would forward every
 * key the input happens to carry, so a caller who had assembled an object with a
 * stray `rateeId` or `moderation_status` on it would put that key on the wire
 * without anyone writing a line of code that says so.
 */

/**
 * Build the `POST /api/ratings` body from a validated submission.
 *
 * Exactly three keys, assembled explicitly: `transaction_id`, `score`, and
 * `review` when there is one. `rater_id`, `ratee_id` and `direction` are never
 * sent under any spelling — the rater is the authenticated caller and the other
 * two are derived from the cited transaction, which is what makes redirecting a
 * rating to a third party, or to oneself, impossible rather than merely refused.
 *
 * `review` is omitted only when it is `undefined`, matching `RatingCreate`, where
 * the field is `.optional()` and an absent key is how a caller says "nothing to
 * add". Any string is forwarded verbatim, including an empty one: the server
 * normalises blank text to null itself, so second-guessing it here would be this
 * module inventing a rule the contract does not have. Nothing is sanitised or
 * truncated on this path — `validateRatingInput` has already sanitised the text,
 * and re-processing it would make the value that was validated differ from the
 * value that is sent.
 *
 * @param input A submission, ideally one already through `validateRatingInput`.
 * @returns The exact three-key snake_case body, with no fourth key possible.
 */
export const toRatingCreateWire = (input: RatingCreate): RatingCreateWire => {
  const body: RatingCreateWire = {
    transaction_id: input.transactionId,
    score: input.score
  };

  if (input.review !== undefined) {
    body.review = input.review;
  }

  return body;
};

/**
 * Build the moderation `PATCH` body. F010-4.
 *
 * Two keys at most, because the router's model forbids extras and because the
 * append-only contract has nothing else to say: a state and, when there is one,
 * the policy basis for reaching it. The original score and words are untouched by
 * this request and there is no shape here capable of altering them.
 *
 * The reason must cite a POLICY violation — abuse, personally identifying
 * information, profanity. A low score is never itself a violation, and this
 * mapper cannot see the score at all, which is the structural half of the
 * guarantee that moderation stays sentiment-neutral as 16 CFR Part 465 requires.
 *
 * @param moderationStatus Target state; the union is enforced by the caller's type.
 * @param moderationReason Policy basis. Omitted from the body when undefined,
 *   which the server reads as clearing any recorded reason. The server refuses a
 *   rejection that carries none, surfacing as a 422 — that rule has one owner and
 *   is deliberately not duplicated here.
 * @returns The one- or two-key snake_case body.
 */
export const toModerationWire = (
  moderationStatus: ModerationStatus,
  moderationReason?: string
): ModerationWire => {
  const body: ModerationWire = {
    moderation_status: moderationStatus
  };

  if (moderationReason !== undefined) {
    body.moderation_reason = moderationReason;
  }

  return body;
};

/*
 * -----------------------------------------------------------------------------
 * THE FIVE ENDPOINTS, AS SIX FUNCTIONS
 * -----------------------------------------------------------------------------
 * Every function is a plain async function that resolves with validated domain
 * data or THROWS. That is what the call sites need: consumers await these inside
 * a `try`/`catch` in a `useEffect` or a submit handler, so a sentinel return
 * value would be silently rendered as though it were data.
 *
 * Every interpolated path segment goes through `encodeURIComponent`. An ID
 * containing a `/` would otherwise be read as extra path structure and address a
 * different route entirely.
 *
 * The `decode` call always sits AFTER the `await`, so an HTTP failure has already
 * propagated by the time any mapping runs. This is what keeps 401, 403, 404, 409
 * and 422 intact, with `error.response.data.detail` available to be rendered.
 */

/**
 * Submit one rating for a completed transaction. F010-1, F010-2.
 *
 * The request carries only the transaction reference, the score and an optional
 * review; the recipient and the direction are the server's to determine.
 *
 * Success is a 201 and the created rating — which will normally arrive with
 * `isPublished: false`. That is not a failure and must not be reported as one:
 * under the double-blind reveal a rating becomes visible once the counterparty
 * submits theirs or the rating window elapses. The score IS recorded. Callers
 * should re-fetch eligibility afterwards, which is where `alreadyRated` becomes
 * true.
 *
 * @param input A submission, ideally already through `validateRatingInput`.
 * @returns The created rating.
 * @throws {AxiosError} 401 unauthenticated; 403 the caller is not verified, or is
 *   not a party to the transaction; 404 no such transaction; 409 already rated, or
 *   the transaction is not completed; 422 score out of range or a self-rating. The
 *   server's own message is on `error.response.data.detail`.
 * @throws {RatingContractError} When the created rating cannot be interpreted.
 */
export const submitRating = async (input: RatingCreate): Promise<Rating> => {
  const api = createRatingApiInstance();
  const response = await api.post<RatingWire>(
    RATINGS_PATH,
    toRatingCreateWire(input)
  );

  return decode(`POST ${RATINGS_PATH}`, () => toRating(response.data));
};

/**
 * Read the ratings one user has received, with their aggregate. F010-3.
 *
 * A public read, matching the unauthenticated precedent of `GET /listings`: a
 * reputation is what a prospective counterparty consults before deciding to
 * transact, so it cannot require an account to see.
 *
 * Published ratings only, in both halves, so an unreciprocated rating appears in
 * neither. A user with no ratings is a first-class state and not an error — an
 * empty `items` beside `average: null, count: 0`.
 *
 * @param userId The user whose received ratings are wanted.
 * @returns The published ratings and the aggregate computed from them.
 * @throws {AxiosError} 404 when no such user exists.
 * @throws {RatingContractError} When the envelope cannot be interpreted.
 */
export const fetchUserRatings = async (
  userId: string
): Promise<UserRatingsResponse> => {
  const api = createRatingApiInstance();
  const response = await api.get<UserRatingsResponseWire>(
    `${RATINGS_PATH}/user/${encodeURIComponent(userId)}`
  );

  return decode(`GET ${RATINGS_PATH}/user/{userId}`, () =>
    toUserRatingsResponse(response.data)
  );
};

/**
 * Read just one user's reputation summary. F010-3.
 *
 * NOT a separate endpoint. It reads `GET /api/ratings/user/{userId}` — the same
 * single request `fetchUserRatings` issues — and returns only the `aggregate`
 * half. There is no `/reputation` path to call and deliberately no second
 * request: the aggregate is denormalised onto the user document precisely so that
 * reading a reputation costs one document read, which is what keeps a profile
 * view inside the 200 ms budget the SRS sets for 95% of API responses.
 *
 * Reflects published ratings only, and includes every one of them whatever the
 * score.
 *
 * @param userId The user whose reputation is wanted.
 * @returns The aggregate. `average` is null, with `count` 0, for a user who has
 *   never been rated — never 0, which would claim a one-star reputation.
 * @throws {AxiosError} 404 when no such user exists.
 * @throws {RatingContractError} When the envelope cannot be interpreted.
 */
export const fetchUserReputation = async (
  userId: string
): Promise<RatingAggregate> => {
  const { aggregate } = await fetchUserRatings(userId);

  return aggregate;
};

/**
 * Read the ratings attached to one transaction.
 *
 * Participant-only, and the response is a BARE ARRAY rather than an envelope. It
 * holds at most two ratings, one in each direction, and is the view a
 * counterparty uses to see what was exchanged once both sides have submitted.
 *
 * @param transactionId The transaction whose ratings are wanted.
 * @returns The transaction's ratings, possibly empty.
 * @throws {AxiosError} 401 unauthenticated; 403 the caller is not a party to the
 *   transaction; 404 no such transaction.
 * @throws {RatingContractError} When the payload cannot be interpreted.
 */
export const fetchTransactionRatings = async (
  transactionId: string
): Promise<Rating[]> => {
  const api = createRatingApiInstance();
  const response = await api.get<RatingWire[]>(
    `${RATINGS_PATH}/transaction/${encodeURIComponent(transactionId)}`
  );

  return decode(`GET ${RATINGS_PATH}/transaction/{transactionId}`, () => {
    if (!Array.isArray(response.data)) {
      throw new TypeError('expected an array of ratings');
    }

    return response.data.map((item) => toRating(item));
  });
};

/**
 * Ask whether the caller may rate their counterparty on a transaction.
 *
 * This endpoint exists so an interface can disable the submission control WITH A
 * REASON instead of letting someone compose a rating and only then fail on a 403.
 * Reporting the reason up front is what makes an unavailable control perceivable
 * rather than merely inert, which is an accessibility obligation and not a
 * nicety.
 *
 * The decision is returned faithfully and is never second-guessed here. The two
 * authorization gates live on the server, so the reason it gives — "Only verified
 * users can submit ratings", "You have already rated this transaction", "Ratings
 * require a completed transaction" — is the text to render.
 *
 * @param transactionId The transaction the caller wants to rate.
 * @returns The decision, including the prose `reason` and `alreadyRated`.
 * @throws {AxiosError} 401 unauthenticated; 404 no such transaction.
 * @throws {RatingContractError} When the decision cannot be interpreted.
 */
export const fetchRatingEligibility = async (
  transactionId: string
): Promise<EligibilityDecision> => {
  const api = createRatingApiInstance();
  const response = await api.get<EligibilityDecisionWire>(
    `${RATINGS_PATH}/eligibility/${encodeURIComponent(transactionId)}`
  );

  return decode(`GET ${RATINGS_PATH}/eligibility/{transactionId}`, () =>
    toEligibilityDecision(response.data)
  );
};

/**
 * Move one rating between moderation states. F010-4. Administrators only.
 *
 * The only other mutation this module performs, and it is not an edit. A
 * submitted score and review are never rewritten: this transitions the moderation
 * fields and records why, leaving what was said intact. There is consequently no
 * `updateRating` and no `deleteRating` anywhere in this module, and no PUT or
 * DELETE request.
 *
 * A transition may be justified only by a POLICY violation — abuse, personally
 * identifying information, profanity — and never by the score. The aggregate
 * continues to count every published rating whatever its value.
 *
 * @param ratingId The rating to transition.
 * @param moderationStatus Target state: `pending`, `approved` or `rejected`.
 * @param moderationReason The policy basis. Required in practice for a rejection,
 *   which the server enforces; omitting it clears any recorded reason.
 * @returns The updated rating.
 * @throws {AxiosError} 401 unauthenticated; 403 the caller is not an
 *   administrator; 404 no such rating; 422 a rejection carrying no policy basis.
 * @throws {RatingContractError} When the updated rating cannot be interpreted.
 */
export const moderateRating = async (
  ratingId: string,
  moderationStatus: ModerationStatus,
  moderationReason?: string
): Promise<Rating> => {
  const api = createRatingApiInstance();
  const response = await api.patch<RatingWire>(
    `${RATINGS_PATH}/${encodeURIComponent(ratingId)}/moderation`,
    toModerationWire(moderationStatus, moderationReason)
  );

  return decode(`PATCH ${RATINGS_PATH}/{ratingId}/moderation`, () =>
    toRating(response.data)
  );
};
