import axios, { AxiosInstance } from 'axios';
import { getAuthToken } from 'app/utils/auth';
import {
  toEligibilityDecision,
  toRating,
  toRatingCreateWire,
  toUserRatingsResponse,
  type EligibilityDecisionWire,
  type RatingWire,
  type UserRatingsResponseWire
} from './rating';
import type {
  EligibilityDecision,
  Rating,
  RatingAggregate,
  RatingCreate,
  UserRatingsResponse
} from '../schema/rating';

const API_BASE_URL = process.env.REACT_APP_API_BASE_URL;

const createApiInstance = (): AxiosInstance => {
  const instance = axios.create({
    baseURL: API_BASE_URL,
  });

  instance.interceptors.request.use(async (config) => {
    const token = await getAuthToken();
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
      console.error('API request failed:', error);
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
 * BASE PATH: `REACT_APP_API_BASE_URL` IS EXPECTED TO INCLUDE `/api`
 * Every path below omits the `/api` segment, matching the three methods above,
 * which call `'/listings'` and `'/upload'` against a backend that mounts them
 * under `prefix='/api/listings'`. `REACT_APP_API_BASE_URL` must therefore
 * already carry the prefix — for example `http://localhost:8000/api` — and with
 * that base these paths resolve to the endpoints the ratings router declares
 * relative to its own `prefix='/api/ratings'`:
 *
 *   POST /api/ratings                               submitRating
 *   GET  /api/ratings/user/{user_id}                 fetchUserRatings,
 *                                                    fetchUserReputation
 *   GET  /api/ratings/eligibility/{transaction_id}   fetchRatingEligibility
 *
 * `./rating` uses the identical convention on purpose: the two modules must
 * agree or one of them would 404 at run time. The ratings router declares its
 * paths relative to its prefix precisely to avoid the double-prefix defect the
 * other three routers carry (their `@router.post('/listings')` under
 * `prefix='/api/listings'` resolves to `/api/listings/listings`), so do NOT add
 * a second `ratings` segment here to "match" them.
 *
 * THE snake_case <-> camelCase BOUNDARY HAS ONE OWNER, AND IT IS `./rating`
 * The wire is snake_case because FastAPI serialises the Pydantic models as
 * declared, while `../schema/rating` is camelCase. Each request body is built by
 * a `to*Wire` mapper and each response adapted by a `to*` mapper imported from
 * `./rating`, which also converts the ISO-8601 strings the wire carries into the
 * real `Date` values the schemas declare. That mapping is deliberately NOT
 * reimplemented here — two copies of one boundary drift apart on the next field
 * rename — and every mapper validates against its own Zod schema internally, so
 * these methods return parsed, guaranteed-shaped data without repeating the
 * parse. Returning a raw `response.data` while claiming a camelCase return type
 * would be a silent lie that yields `undefined` at every call site.
 *
 * Each interpolated path segment goes through `encodeURIComponent`: an ID
 * containing a `/` would otherwise be read as extra path structure and address a
 * different route entirely.
 *
 * ERRORS ARE NEVER SWALLOWED
 * None of these methods carries a `try`/`catch`, and that is the point. A
 * failure falls through to the response interceptor above, which logs and then
 * rejects with the ORIGINAL error, so `error.response.status` and
 * `error.response.data.detail` reach the caller intact. The router reuses each
 * domain exception's own human-readable text as `HTTPException.detail` so those
 * strings match the `reason` an `EligibilityDecision` carries, which is what
 * lets the submission form render the server's own words verbatim:
 *
 *   401  no credentials
 *   403  the rater is not verified, or is not a party to the transaction
 *   404  no such transaction or user
 *   409  already rated, or the transaction is not completed
 *   422  score out of range, review too long, or a self-rating
 *
 * There is consequently no client-side status-to-message table — a second source
 * of wording for one decision drifts away from the first — and no `catch` that
 * returns `null` or `{ success: false }`, both of which destroy that message.
 *
 * WHAT IS ABSENT, AND WHY
 * There is no `updateRating`, `editRating` or `deleteRating`, and no PUT or
 * DELETE call. Reputation records are append-only: a submitted score is never
 * rewritten, and a correction is a moderation-state transition that records a
 * policy basis and leaves the original score and words intact. Moderation and
 * the per-transaction read are served by `./rating`; the four methods below are
 * the surface this module owns.
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
 * @throws {ZodError} When the created rating violates `RatingSchema`.
 */
export const submitRating = async (input: RatingCreate): Promise<Rating> => {
  const api = createApiInstance();
  const response = await api.post<RatingWire>(
    '/ratings',
    toRatingCreateWire(input)
  );

  return toRating(response.data);
};

/**
 * Read the ratings one user has received, with their aggregate. F010-3.
 *
 * A public read, matching the unauthenticated precedent of `GET /listings`: a
 * reputation is what a prospective counterparty consults before deciding to
 * transact, so it cannot require an account to see.
 *
 * Published ratings only, in both halves, so an unreciprocated rating appears in
 * neither until it is revealed. A user with no ratings is a first-class state
 * rather than an error — an empty `items` beside `average: null, count: 0`.
 *
 * @param userId The user whose received ratings are wanted.
 * @returns The published ratings and the aggregate computed from them.
 * @throws {AxiosError} 404 when no such user exists.
 * @throws {ZodError} When the envelope violates `UserRatingsResponseSchema`.
 */
export const fetchUserRatings = async (
  userId: string
): Promise<UserRatingsResponse> => {
  const api = createApiInstance();
  const response = await api.get<UserRatingsResponseWire>(
    `/ratings/user/${encodeURIComponent(userId)}`
  );

  return toUserRatingsResponse(response.data);
};

/**
 * Read just one user's reputation summary. F010-3.
 *
 * NOT a separate endpoint. It reads `GET /api/ratings/user/{user_id}` — the very
 * same single request `fetchUserRatings` issues, reused here rather than
 * duplicated — and returns only the `aggregate` half. There is no `/reputation`
 * path to call and deliberately no second request: the aggregate is
 * denormalised onto the user document precisely so that reading a reputation
 * costs one document read, which is what keeps a profile view inside the 200 ms
 * budget the SRS sets for 95% of API responses.
 *
 * Reflects published ratings only, and includes every one of them whatever the
 * score.
 *
 * @param userId The user whose reputation is wanted.
 * @returns The aggregate. `average` is null, with `count` 0, for a user who has
 *   never been rated — never 0, which would instead claim a genuine, earned
 *   one-star reputation.
 * @throws {AxiosError} 404 when no such user exists.
 * @throws {ZodError} When the envelope violates `UserRatingsResponseSchema`.
 */
export const fetchUserReputation = async (
  userId: string
): Promise<RatingAggregate> => {
  const { aggregate } = await fetchUserRatings(userId);

  return aggregate;
};

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
 * @throws {ZodError} When the decision violates `EligibilityDecisionSchema`.
 */
export const fetchRatingEligibility = async (
  transactionId: string
): Promise<EligibilityDecision> => {
  const api = createApiInstance();
  const response = await api.get<EligibilityDecisionWire>(
    `/ratings/eligibility/${encodeURIComponent(transactionId)}`
  );

  return toEligibilityDecision(response.data);
};

// HUMAN ASSISTANCE NEEDED
// Define the VehicleListing interface based on your backend API structure
interface VehicleListing {
  // Add properties for the vehicle listing
}