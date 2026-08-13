import axios, { AxiosInstance } from 'axios';
/*
 * The bearer token comes from `./rating`, which owns the project's single
 * reader. This module previously imported `getAuthToken` from
 * `app/utils/auth` — a module that does not exist, since `src/app` is not a
 * directory in this project — so the import was an unresolvable specifier that
 * made this entire module fail to type-check and, after `npm ci`, fail to load.
 * Every method here was unreachable as a result, including the three that
 * predate the rating feature.
 *
 * Creating `app/utils/auth` was not the fix: it is explicitly out of scope, and
 * inventing a module to satisfy a broken import adds a file whose only purpose
 * is to have been imported. Re-implementing the reader here was also rejected —
 * two copies of "where the token lives" drift the moment the storage key or its
 * guards change. So this module delegates to `./rating`, which it already
 * imports for the mappers below, and which reads the token from the key
 * `./auth` writes on login.
 *
 * `describeRequestFailure` comes from the same place and for the same reason. It
 * is the project's one answer to "what of a failed request is safe to write to a
 * log", and the answer has to be one answer: a per-module judgement about whether
 * an error object still has a bearer token on it only has to be got wrong once.
 */
import {
  describeRequestFailure,
  fetchRatingEligibility as fetchRatingEligibilityRequest,
  fetchUserRatings as fetchUserRatingsRequest,
  fetchUserReputation as fetchUserReputationRequest,
  readAuthToken,
  submitRating as submitRatingRequest

} from './rating';
import type {
  EligibilityDecision,
  Rating,
  RatingAggregate,
  RatingCreate,
  UserRatingsResponse
} from '../schema/rating';

const RAW_API_BASE_URL = process.env.REACT_APP_API_BASE_URL;

/**
 * Resolve the base URL for every call in this module, refusing an unusable value.
 *
 * axios accepts `undefined` or `''` as a `baseURL` and then routes every path
 * relative to whatever origin served the page, so an unset variable does not
 * produce an error — it produces requests that quietly go somewhere else. Under
 * the dev server that "somewhere else" answers with the SPA's index document and
 * a 200, so a caller sees a successful response whose body is HTML and fails much
 * later, somewhere unrelated. Refusing here names the cause once, at the first
 * call.
 *
 * Mirrors `resolveApiBaseUrl` in `./rating`, which validates the same variable for
 * the rating endpoints. The shape is replicated rather than shared because this
 * module already imports the wire mappers FROM `./rating`, so importing a helper
 * back would close a cycle between the two.
 *
 * The trailing slash is trimmed, not rejected: every path below starts with `/`,
 * and a hand-written value ending in `/` is the commonest way this variable is
 * written.
 *
 * @returns The base URL, without a trailing slash.
 * @throws {Error} The variable is unset, blank, or not an absolute http(s) URL.
 */
const resolveApiBaseUrl = (): string => {
  const raw = (RAW_API_BASE_URL ?? '').trim();

  if (raw === '') {
    throw new Error(
      'REACT_APP_API_BASE_URL is not set, so API requests have no server ' +
        'to reach. Set it to the API root including its /api path, for ' +
        'example http://localhost:8000/api',
    );
  }

  let parsed: URL;
  try {
    parsed = new URL(raw);
  } catch {
    throw new Error(
      'REACT_APP_API_BASE_URL must be an absolute URL including its ' +
        'scheme, for example http://localhost:8000/api',
    );
  }

  if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') {
    throw new Error(
      'REACT_APP_API_BASE_URL must use http or https, for example ' +
        'http://localhost:8000/api',
    );
  }

  return raw.replace(/\/+$/, '');
};


const createApiInstance = (): AxiosInstance => {
  const instance = axios.create({
    baseURL: resolveApiBaseUrl(),
  });

  /*
   * Attach the bearer token to every outgoing request.
   *
   * The token is read through `readAuthToken` from `./rating`, which returns the
   * value `./auth` stores under the `authToken` key on a successful login and
   * tolerates every context in which browser storage is absent or throws. This
   * module previously imported a `getAuthToken` from `app/utils/auth`, a module
   * that does not exist — `src/app` is not a directory in this project — so the
   * whole module, including all four rating methods below, could not be
   * imported. Creating that module is out of scope, and duplicating the reader
   * here would give the storage key a second spelling that could drift, so the
   * one working helper is shared instead.
   *
   * Synchronous, because reading `localStorage` is. The previous `await` existed
   * only because the missing helper was declared to return a promise; nothing
   * about the request pipeline changes, since the header is still set before the
   * config is handed on and an absent token still yields an unauthenticated
   * request that the server answers with a 401 the caller can act on.
   */

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
      // HUMAN ASSISTANCE NEEDED
      // Add more specific error handling based on your application's requirements
      //
      // What is NOT outstanding here is the redaction. This used to log the raw
      // error, which carries the `config` the request was made with — and the
      // interceptor above has already written `Authorization: Bearer <token>`
      // into those headers, so the token was being published to the browser
      // console and to anything mirroring it (CWE-532). It also carried the
      // request body and the response body. `describeRequestFailure` builds a
      // fresh object from four primitives by allow-list instead, and it is
      // shared with `./rating` so there is one answer to "what is safe to log"
      // rather than one per module. The error is still rejected with in full,
      // because the caller needs the status and the server's `detail`.
      console.error('API request failed', describeRequestFailure(error));

      return Promise.reject(error);
    }
  );

  return instance;
};

export const fetchListings = async (filters: object): Promise<VehicleListing[]> => {
  const api = createApiInstance();
  const response = await api.get('/listings', { params: filters });
  return response.data;
};

export const createListing = async (listingData: VehicleListing): Promise<VehicleListing> => {
  const api = createApiInstance();
  const response = await api.post('/listings', listingData);
  return response.data;
};

export const uploadPhoto = async (photo: File): Promise<string> => {
  const api = createApiInstance();
  const formData = new FormData();
  formData.append('photo', photo);
  const response = await api.post('/upload', formData, {
    headers: {
      'Content-Type': 'multipart/form-data',
    },
  });
  return response.data;
};

/*
 * -----------------------------------------------------------------------------
 * RATING CLIENT METHODS — F010 "Review and Rating System"
 * -----------------------------------------------------------------------------
 * The client surface of the bidirectional peer reputation system: a buyer rates
 * the seller and the seller rates the buyer for a purchase they both took part
 * in. The two authorization gates — the rater must be a verified user, and both
 * parties must be counterparties of the same completed transaction — are
 * enforced server-side and surface as 403. These methods REPORT that decision,
 * through `fetchRatingEligibility`; they never make it, and a request this
 * module is willing to send may still legitimately be refused.
 *
 * EVERY ONE OF THEM DELEGATES TO `./rating`, AND THAT IS THE WHOLE DESIGN
 * -----------------------------------------------------------------------------
 * Each function below is a one-line call into the identically named function in
 * `./rating`. Nothing here builds a request, interpolates a path, maps a wire
 * shape or interprets a response, because `./rating` already does all four and
 * doing them twice is how two copies of one contract drift apart.
 *
 * These are not cosmetic wrappers, and the difference they remove is real. This
 * module previously issued its own requests and called `./rating`'s mappers
 * directly, WITHOUT the `decode` wrapper those mappers are normally used behind.
 * So the same malformed response produced a `RatingContractError` naming the
 * endpoint when a caller used `./rating`, and a bare `ZodError` or `TypeError`
 * when the very same caller used this module — two different error types, and
 * only one of them documented, for one failure. A caller cannot write a correct
 * `catch` against that. Delegation makes both paths one path.
 *
 * The four names are preserved because they are this module's published surface
 * and existing screens import them from here. What changed is where the work
 * happens, not what a caller may call.
 *
 * WHAT DELEGATION MEANS FOR THE THINGS THAT USED TO BE DOCUMENTED HERE
 * -----------------------------------------------------------------------------
 * All of it still holds, and all of it now has exactly one owner in `./rating`:
 *
 *   - PATHS. `REACT_APP_API_BASE_URL` is expected to include the `/api` prefix,
 *     matching the three methods above which call `'/listings'` and `'/upload'`
 *     against a backend that mounts them under `prefix='/api/listings'`. With
 *     that base, `./rating`'s paths resolve to `POST /api/ratings`,
 *     `GET /api/ratings/user/{user_id}` and
 *     `GET /api/ratings/eligibility/{transaction_id}`. Every interpolated
 *     segment is encoded there.
 *   - THE snake_case <-> camelCase BOUNDARY. The wire is snake_case because
 *     FastAPI serialises the Pydantic models as declared, while
 *     `../schema/rating` is camelCase, and `./rating` owns the adaptation in both
 *     directions — including the ISO-8601-to-`Date` conversion and the Zod
 *     validation that makes the returned values trustworthy rather than merely
 *     typed.
 *   - AUTHENTICATION, PER ENDPOINT. `./rating` attaches the bearer token from the
 *     same storage key through a request interceptor it installs only for the
 *     calls the server authenticates, so a delegated call carries exactly the
 *     credentials a direct one does. `submitRating` and `fetchRatingEligibility`
 *     are authenticated; `fetchUserRatings` and `fetchUserReputation` are public
 *     reads and deliberately send no `Authorization` header, because the routes
 *     they call declare no authentication dependency and read nothing from the
 *     caller's identity.
 *   - ERRORS ARE NEVER SWALLOWED. No `try`/`catch` is added on either side of the
 *     delegation, so a refusal keeps its status and its `detail`:
 *
 *       401  no credentials
 *       403  the rater is not verified, or is not a party to the transaction
 *       404  no such transaction or user
 *       409  already rated, or the transaction is not completed
 *       422  score out of range, review too long, or a self-rating
 *
 *     There is consequently no client-side status-to-message table — a second
 *     source of wording for one decision drifts away from the first — and no
 *     `catch` that returns `null` or `{ success: false }`, both of which destroy
 *     that message.
 *
 * WHAT IS ABSENT, AND WHY
 * -----------------------------------------------------------------------------
 * There is no `updateRating`, `editRating` or `deleteRating`, and no PUT or
 * DELETE call. Reputation records are append-only: a submitted score is never
 * rewritten, and a correction is a moderation-state transition that records a
 * policy basis and leaves the original score and words intact. Moderation and
 * the per-transaction read are served by `./rating` only; the four methods below
 * are the surface this module publishes.
 *
 * No method below reads, branches on, filters by, sorts by or defaults from a
 * `score`. Moderation is sentiment-neutral because the FTC Rule on the Use of
 * Consumer Reviews and Testimonials (16 CFR Part 465) prohibits suppressing
 * reviews on the basis of rating or negative sentiment, so score-correlated
 * behaviour here would be a compliance failure rather than a style choice.
 */

/**
 * Submit one rating for a completed transaction. F010-1, F010-2.
 *
 * The body carries exactly three keys: `transaction_id`, `score`, and `review`
 * when there is one. `rater_id`, `ratee_id` and `direction` are never sent under
 * any spelling — the rater is the authenticated caller and the other two are
 * derived server-side from the cited transaction, which is what makes
 * redirecting a rating to a third party, or to oneself, structurally impossible
 * rather than merely refused. `toRatingCreateWire` assembles those three keys
 * literally and never by spreading its input, so a stray key carried on the
 * submission object cannot reach the wire.
 *
 * Success is a 201 and the created rating, which will normally arrive with
 * `isPublished: false`. That is the double-blind reveal working as designed and
 * must not be surfaced as a failure: the score IS recorded, and it becomes
 * visible once the counterparty submits theirs or the rating window elapses.
 * Re-fetching eligibility afterwards is the correct follow-up, because that is
 * where the caller learns `alreadyRated`.
 *
 * @param input A submission, ideally already through `validateRatingInput`.
 * @returns The created rating, validated and camelCase.
 * @throws {AxiosError} 401 unauthenticated; 403 the caller is not verified, or
 *   is not a party to the transaction; 404 no such transaction; 409 already
 *   rated, or the transaction is not completed; 422 score out of range or a
 *   self-rating. The server's message is on `error.response.data.detail`.
 * @throws {RatingContractError} When the created rating cannot be interpreted as
 *   `RatingSchema`. Identical to calling `./rating` directly, which is the point
 *   of delegating rather than reimplementing.
 */
export const submitRating = (input: RatingCreate): Promise<Rating> =>
  submitRatingRequest(input);

/**
 * Read the ratings one user has received, with their aggregate. F010-3.
 *
 * A public read, matching the unauthenticated precedent of `GET /listings`: a
 * reputation is what a prospective counterparty consults before deciding to
 * transact, so it cannot require an account to see. No bearer token is sent, in
 * this wrapper or in the `./rating` implementation it delegates to.
 *
 * Published ratings only, in both halves, so an unreciprocated rating appears in
 * neither until it is revealed. A user with no ratings is a first-class state
 * rather than an error — an empty `items` beside `average: null, count: 0`.
 *
 * The user ID is the whole request: there is no page size, no cursor and no
 * mode. `items` is bounded by the server and omits any rating whose review
 * moderation rejected, while `aggregate` counts every published rating, so
 * `aggregate.count` may exceed `items.length`. That is the contract and must not
 * be rendered as a discrepancy.
 *
 * @param userId The user whose received ratings are wanted.
 * @returns The published ratings received, newest first, and the aggregate over
 *   all of them.
 * @throws {AxiosError} 404 when no such user exists.
 * @throws {RatingContractError} When the envelope cannot be interpreted as
 *   `UserRatingsResponseSchema`.
 */
export const fetchUserRatings = (
  userId: string
): Promise<UserRatingsResponse> => fetchUserRatingsRequest(userId);


/**
 * Read just one user's reputation summary. F010-3.
 *
 * `GET /api/ratings/user/{user_id}/aggregate`, which returns the two numbers and
 * nothing else. It was previously the `aggregate` half of the full user read
 * with the rest discarded, and this is the surface that renders beside every
 * listing — so the most-frequent read in the product was transferring a page of
 * reviews no caller looked at, and costing the server a ratings query to produce
 * them.
 *
 * An `aggregate_only=true` FLAG existed before that and was removed for a
 * different reason: it answered from the user document WITHOUT settling
 * publications that were already due, which made this the one reputation figure
 * permitted to sit stale waiting on a worker that will never run. The endpoint
 * called now settles on every request, exactly as the full read does, so nothing
 * about that objection is reintroduced.
 *
 * A public read, exactly as `fetchUserRatings` is: no bearer token is sent, in
 * this wrapper or in the `./rating` implementation it delegates to.
 *
 * Both endpoints settle the same due set and read the same denormalised pair off
 * the same user document, so this wrapper and `fetchUserRatings` can never show a
 * reader different reputations for the same user.
 *
 * Reflects published ratings only, and includes every one of them whatever the
 * score.
 *
 * @param userId The user whose reputation is wanted.
 * @returns The aggregate. `average` is null, with `count` 0, for a user who has
 *   never been rated — never 0, which would instead claim a genuine, earned
 *   one-star reputation.
 * @throws {AxiosError} 404 when no such user exists.
 * @throws {RatingContractError} When the payload cannot be interpreted as
 *   `RatingAggregateSchema`.
 */
export const fetchUserReputation = (
  userId: string
): Promise<RatingAggregate> => fetchUserReputationRequest(userId);


/**
 * Ask whether the caller may rate their counterparty on a transaction.
 *
 * This endpoint exists so an interface can disable the submission control WITH A
 * REASON, instead of letting someone compose a rating and only then fail on a
 * 403. Reporting the reason up front is what makes an unavailable control
 * perceivable rather than merely inert, which is an accessibility obligation
 * rather than a nicety.
 *
 * The decision is returned faithfully and never second-guessed here. Both
 * authorization gates live on the server, so the reason it gives — "Only
 * verified users can submit ratings", "You have already rated this transaction",
 * "Ratings require a completed transaction" — is the text to render.
 *
 * @param transactionId The transaction the caller wants to rate.
 * @returns The decision, including the prose `reason` and `alreadyRated`, which
 *   is distinct from `eligible`: "you have had your say" and "you were never
 *   entitled to one" are different states a form must not conflate.
 * @throws {AxiosError} 401 unauthenticated; 404 no such transaction.
 * @throws {RatingContractError} When the decision cannot be interpreted as
 *   `EligibilityDecisionSchema`.
 */
export const fetchRatingEligibility = (
  transactionId: string
): Promise<EligibilityDecision> => fetchRatingEligibilityRequest(transactionId);

// HUMAN ASSISTANCE NEEDED
// Define the VehicleListing interface based on your backend API structure.
//
// The index signature is not the definition this comment is asking for; it is
// there because an interface with an empty body is equivalent to `{}`, which
// `@typescript-eslint/no-empty-interface` reports as an error and `npm run lint`
// fails on (`--max-warnings 0`). An index signature keeps the placeholder
// honest — it says "any field, unknown type", which is exactly what is known
// about this shape today — without weakening anything: `response.data` is `any`,
// so every use site below is unaffected. Replace the whole thing with the real
// field list when the listing contract is settled.
interface VehicleListing {
  [field: string]: unknown;
}