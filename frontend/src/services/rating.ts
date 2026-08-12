import axios, { AxiosInstance } from 'axios';
import {
  EligibilityDecisionSchema,
  ModeratedRatingSchema,
  RatingAggregateSchema,
  RatingSchema,
  UserRatingsResponseSchema,
  type EligibilityDecision,
  type ModeratedRating,
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
 * The one failure the server has no words for is a response that does not match
 * the contract, and that is `RatingContractError`. Its `message` is the fixed
 * `CONTRACT_ERROR_MESSAGE`, and the internal detail lives on `diagnostics` and in
 * the developer console instead — the interface renders `error.message`, so an
 * endpoint label and a list of schema field paths would otherwise be shown to
 * whoever was rating a car.
 *
 * CREDENTIALS GO ONLY WHERE THE SERVER USES THEM
 * -----------------------------------------------------------------------------
 * Four of the six calls below are authenticated and two are public, exactly as
 * the router declares: the submission, the eligibility check, the per-transaction
 * read and the moderation transition all resolve a caller, while the ratings a
 * user has received and their reputation aggregate are readable by anyone,
 * matching the unauthenticated precedent of `GET /listings`.
 *
 * The bearer token is therefore attached PER ENDPOINT rather than per module —
 * `createRatingApiInstance` installs the request interceptor only for the
 * authenticated policy. Sending a credential to an endpoint that reads nothing
 * from it spends least privilege for nothing, makes a cacheable public response
 * private to every shared cache, and turns an expired token into a needless 401
 * on a page that had no reason to care.
 *
 * NOTHING SENSITIVE IS EVER LOGGED
 * -----------------------------------------------------------------------------
 * An `AxiosError` carries the `config` it came from, and on an authenticated call
 * the request interceptor has written `Authorization: Bearer <token>` into its
 * headers — so logging the error object itself publishes a live credential to the
 * console and to anything mirroring it. Every log in this module therefore goes
 * through `describeRequestFailure`, which builds a fresh four-primitive object by
 * allow-list. The error is still REJECTED WITH in full, because the caller needs
 * the status and the detail; it is simply never written down.
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
 * declared convention and is what `./api` reads.
 *
 * `vite.config.ts` guarantees that read, and the guarantee is unconditional: its
 * `KNOWN_ENV_KEYS` list names this variable, so a `define` entry for this exact
 * member expression is emitted WHETHER OR NOT the variable is set — with the
 * loaded value when there is one and with the literal `undefined` when there is
 * not. In a production build the expression is statically replaced; under the
 * dev server Vite's client materialises the same dotted keys onto `globalThis`
 * before any application module runs. Either way `process` is never touched as a
 * bare global in a browser, which is what previously threw
 * `ReferenceError: process is not defined` at import time and took down the
 * whole entry graph rather than just the base URL.
 *
 * The read is therefore left bare, and wrapping it in a `typeof process` guard
 * would be actively harmful: the build-time substitution replaces only the inner
 * member expression, so the surviving guard evaluates false in a bundle and
 * silently yields no base URL at all.
 *
 * `undefined` is what this constant holds when the variable is unset, and it is
 * NOT treated as a legitimate configuration: `resolveApiBaseUrl` below throws on
 * it. Falling back to the page's own origin — which is what axios does with an
 * absent `baseURL` — is the failure that does not look like one, because the
 * requests then succeed against the wrong server. So this constant is the raw
 * read, and the decision about whether it is usable belongs to the one function
 * that can name the variable while refusing it.
 */
const RAW_API_BASE_URL = process.env.REACT_APP_API_BASE_URL;

/**
 * Resolve the base URL, refusing a value no request could be routed with.
 *
 * axios accepts a `baseURL` of `undefined` or `''` without complaint and then
 * treats every path as relative to whatever origin served the page. That failure
 * is the dangerous kind: the requests do not error, they go somewhere else — in a
 * development setup, straight back at the dev server, which answers the SPA's
 * index document with a 200, so a caller sees a successful response whose body is
 * HTML and reports a schema violation from a completely unrelated place. A
 * malformed value behaves the same way. Throwing at the first call instead names
 * the actual cause once.
 *
 * `frontend/vite.config.ts` validates the same variable at config load and
 * refuses to build or serve without it, which is the earlier and better gate.
 * This one exists because that gate does not cover every path to this module: a
 * test importing it directly, or a consumer bundled by other means, reaches here
 * without ever loading that config. The two are complementary rather than
 * redundant, and both name the variable.
 *
 * A trailing slash is TRIMMED rather than rejected. Every path below begins with
 * `/`, so `…/api/` would compose `…/api//ratings`; that resolves on most servers
 * and is a needless difference between environments, and a trailing slash is the
 * single commonest way this variable is written by hand.
 *
 * @returns The base URL, without a trailing slash.
 * @throws {Error} The variable is unset, blank, or not an absolute http(s) URL.
 */
const resolveApiBaseUrl = (): string => {
  const raw = (RAW_API_BASE_URL ?? '').trim();

  if (raw === '') {
    throw new Error(
      'REACT_APP_API_BASE_URL is not set, so rating requests have no ' +
        'server to reach. Set it to the API root including its /api path, ' +
        'for example http://localhost:8000/api'
    );
  }

  let parsed: URL;
  try {
    parsed = new URL(raw);
  } catch {
    throw new Error(
      'REACT_APP_API_BASE_URL must be an absolute URL including its ' +
        'scheme, for example http://localhost:8000/api'
    );
  }

  if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') {
    throw new Error(
      'REACT_APP_API_BASE_URL must use http or https, for example ' +
        'http://localhost:8000/api'
    );
  }

  return raw.replace(/\/+$/, '');
};

/**
 * Path prefix shared by all five endpoints, relative to the resolved base URL.
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
 * The project's ONE token reader, and exported for that reason. `./auth`
 * persists the token under `AUTH_TOKEN_STORAGE_KEY` on a successful login, so
 * this is where a token is found rather than a name invented here.
 *
 * `./api` previously imported `getAuthToken` from `app/utils/auth`, a module
 * that does not exist — `src/app` is not a directory in this project — and
 * creating it is out of scope. So `./api` reads the token through this function
 * instead. That direction is deliberate and non-circular: `./api` already
 * imports the mappers below, and this module must never import `./api` back.
 * Duplicating the reader in both files was rejected: two copies of "where the
 * token lives" drift the moment the storage key or the guard changes, and the
 * copy that drifts is the one nobody is looking at.


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
export const readAuthToken = (): string | null => {
  if (typeof localStorage === 'undefined') {
    return null;
  }

  try {
    return localStorage.getItem(AUTH_TOKEN_STORAGE_KEY);
  } catch {
    return null;
  }
};

/*
 * -----------------------------------------------------------------------------
 * FAILURE REPORTING — WHAT MAY BE READ FROM AN ERROR, AND WHAT MAY BE LOGGED
 * -----------------------------------------------------------------------------
 * Two different jobs, deliberately split into two exported functions, because
 * conflating them is how a bearer token ends up in a log aggregator.
 *
 * `readServerDetail` extracts the message the server WROTE FOR A PERSON, which an
 * interface renders verbatim. `describeRequestFailure` builds the one line that
 * may be logged. Nothing anywhere in this feature logs an axios error object.
 *
 * That prohibition is concrete rather than precautionary. An `AxiosError` carries
 * `config`, and `config` carries `headers` — including, on an authenticated call,
 * the `Authorization: Bearer …` header the request interceptor below attached —
 * and `data`, the request body, which on the submission path is the review the
 * user wrote. `console.error('…', error)` serialises all of it into the browser
 * console, and from there into any telemetry or session-replay tool that scrapes
 * console output. So the log line is assembled from four approved parts and
 * nothing else: the endpoint label the caller supplies, the HTTP status, axios's
 * own transport `code`, and the server's `detail` string.
 */

/**
 * Read the server's human-readable explanation out of a rejected request.
 *
 * The router reuses each domain exception's own message as `HTTPException.detail`
 * precisely so those strings match the `reason` an `EligibilityDecision`
 * carries, which is what lets an interface render the server's own words instead
 * of a locally invented table of copy keyed by status code.
 *
 * BOTH SHAPES OF `detail` ARE HANDLED, because FastAPI emits two. A refusal
 * raised by the router is a string ("Only verified users can submit ratings").
 * A request Pydantic rejected before any handler ran is an ARRAY of issue
 * objects — `[{loc, msg, type}]` — since no custom `RequestValidationError`
 * handler is registered anywhere in the backend. Reading only the string case
 * silently discards every 422: a score outside the scale or an over-long review
 * would degrade to axios's own "Request failed with status code 422", which
 * tells the user nothing about which field to fix.
 *
 * Each issue contributes its `msg`, duplicates are collapsed, and the results are
 * joined. `loc` is deliberately not rendered: it is a JSON pointer written for a
 * developer ("body", "score"), and the messages read as sentences without it.
 *
 * @param error Any rejection value, of genuinely unknown shape.
 * @returns The server's message, or `null` when the error carries none — a
 *   transport failure, or a response with no body.
 */
export const readServerDetail = (error: unknown): string | null => {
  if (!axios.isAxiosError(error)) {
    return null;
  }

  const detail: unknown = error.response?.data?.detail;

  if (typeof detail === 'string') {
    return detail.length > 0 ? detail : null;
  }

  if (Array.isArray(detail)) {
    const messages = detail
      .map((issue) =>
        typeof issue === 'object' && issue !== null
          ? (issue as { msg?: unknown }).msg
          : undefined
      )
      .filter(
        (message): message is string =>
          typeof message === 'string' && message.length > 0
      );

    const unique = Array.from(new Set(messages));

    return unique.length > 0 ? unique.join('; ') : null;
  }

  return null;
};


/**
 * Reduce a failed request to the metadata that is SAFE to log.
 *
 * The project's one redaction point for request failures, exported so `./api`
 * and the rating components use this function rather than each deciding for
 * itself what is safe — a decision that only has to be got wrong once.
 *
 * WHY A RAW AXIOS ERROR MUST NEVER REACH A LOG SINK
 * ---------------------------------------------------------------------------
 * An `AxiosError` carries its originating `config`, and by the time an error
 * exists the request interceptor above has already written
 * `Authorization: Bearer <token>` into `config.headers` for any authenticated
 * call — which is four of the six. So
 * `console.error('...', error)` publishes a live bearer token to the browser
 * console and to every telemetry agent that mirrors it (CWE-532, insertion of
 * sensitive information into log file). The same object also holds
 * `config.data` — the request body — and `response.data`, which is the server's
 * payload rather than anything a log needs.
 *
 * This builds a FRESH object from four primitives, by allow-list. Nothing is
 * spread, no nested object is forwarded, and the original error is not a member
 * of the result, so there is no path by which a header, a cookie or a body can
 * appear in the output even if axios grows new fields.
 *
 * The four fields are what actually makes a failure diagnosable: which call was
 * made, against which path, what the server said, and — for a failure that never
 * reached a server — the axios code such as `ERR_NETWORK` or `ECONNABORTED`.
 * The query string is stripped from the URL rather than kept: it carries nothing
 * this feature needs (only `limit` and `after`), and a deployment that ever put
 * a credential in a query would otherwise leak it here.
 *
 * @param error Any thrown value, axios or not.
 * @returns A flat object of primitives, safe to pass to a log sink.
 */
export const describeRequestFailure = (
  error: unknown
): {
  method: string;
  url: string;
  status: number | null;
  code: string;
  message: string;
  detail?: string;
} => {
  const source = (
    typeof error === 'object' && error !== null ? error : {}
  ) as {
    code?: unknown;
    config?: unknown;
    response?: unknown;
    message?: unknown;
  };
  const config = (source.config ?? {}) as { method?: unknown; url?: unknown };
  const response = (source.response ?? {}) as { status?: unknown };
  const message = source.message;

  const method =
    typeof config.method === 'string' ? config.method.toUpperCase() : 'UNKNOWN';
  const path = typeof config.url === 'string' ? config.url.split('?')[0] : '';
  const detail = readServerDetail(error);

  return {
    method,
    url: path.length > 0 ? path : 'unknown',
    status: typeof response.status === 'number' ? response.status : null,
    code: typeof source.code === 'string' ? source.code : 'none',
    message:
      typeof message === 'string' && message.length > 0
        ? message
        : 'Request failed',
    ...(detail === null ? {} : { detail })
  };
};

/**
 * Whether an instance is for calls the server authenticates, or for public reads.
 *
 * Named rather than a bare boolean because the value appears at every call site
 * below, and `createRatingApiInstance(false)` at a call site says nothing about
 * what the false means. `'authenticated'` and `'public'` state the endpoint's own
 * contract, so a reader of `submitRating` or `fetchUserRatings` can see which of
 * the two it claims to be without leaving the line.
 */
type RatingAuthPolicy = 'authenticated' | 'public';

/**
 * Build an axios instance for one auth policy, preserving errors either way.
 *
 * Mirrors `createApiInstance` in `./api`, which is a module-local `const` with
 * no `export` keyword. It is genuinely unexported, so the shape is replicated
 * here rather than imported. The reverse direction is also deliberate: `./api`
 * imports the mappers below, so this module must never import `./api` back.
 *
 * CREDENTIALS ARE ATTACHED PER ENDPOINT, NOT PER MODULE
 * -----------------------------------------------------------------------------
 * The request interceptor is installed ONLY for the `'authenticated'` policy. It
 * used to be installed unconditionally, so the two public reads — the ratings a
 * user has received and their reputation aggregate — sent a live bearer token to
 * an endpoint that declares no authentication dependency at all and reads
 * nothing from the caller's identity. Four things were wrong with that, and none
 * of them is theoretical:
 *
 *   - LEAST PRIVILEGE. A credential should reach exactly the requests that need
 *     it. The reputation read is rendered beside every listing, so the token was
 *     being put on the wire more often than on every other call combined, and
 *     each of those requests is another place it can be logged by a proxy, an
 *     error reporter or a browser extension.
 *   - CACHING. A public GET carrying `Authorization` is treated by shared caches
 *     and CDNs as private, so the one response in this feature that is genuinely
 *     the same for every reader could not be cached for any of them.
 *   - FAILURE MODE. An expired token on a public read is a needless way to turn a
 *     working page into a 401 the interface has no reason to handle there.
 *   - HONESTY. The endpoint's public contract is documented in three places; a
 *     client that always authenticates makes that claim untestable from the
 *     outside, because nothing ever exercises the unauthenticated path.
 *
 * The response interceptor is installed for BOTH, because it is load-bearing for
 * the whole feature's UX. It logs REDACTED metadata and then rejects with the
 * ORIGINAL error, unwrapped and unreplaced, which is the only reason
 * `error.response.data.detail` survives to be rendered verbatim by the submission
 * form. Constructing a new error here — however tidy the message — would discard
 * the server's own explanation of the refusal.
 *
 * Logging and rejecting are deliberately different in what they carry. The
 * rejection keeps everything, because the caller is code that needs the status
 * and the detail; the log keeps only `describeRequestFailure`'s five primitives,
 * because a log is a durable artefact that outlives the request and that people
 * and telemetry agents read. On an authenticated call the bearer token is on the
 * error and must not be in that artefact.
 *
 * @param policy `'authenticated'` attaches the bearer token when one is stored;
 *   `'public'` never attaches it, whether or not the reader is signed in.
 */
const createRatingApiInstance = (policy: RatingAuthPolicy): AxiosInstance => {
  const instance = axios.create({
    baseURL: resolveApiBaseUrl()
  });

  if (policy === 'authenticated') {
    instance.interceptors.request.use((config) => {
      const token = readAuthToken();
      if (token) {
        config.headers['Authorization'] = `Bearer ${token}`;
      }
      return config;
    });
  }

  instance.interceptors.response.use(
    (response) => response,
    (error) => {
      console.error('Rating API request failed', describeRequestFailure(error));


      return Promise.reject(error);
    }
  );

  return instance;
};

/**
 * The one sentence a user is shown when a response cannot be interpreted.
 *
 * Fixed rather than composed, and exported so the components render this exact
 * text and the tests assert against the same constant rather than a copy of it.
 * It says what happened in terms of the user's situation and what to do about
 * it, and it says nothing about endpoints, field names or schemas: a user cannot
 * act on "expected date, received string at createdAt", and an attacker should
 * not be handed a description of the API's shape by a malformed response.
 *
 * Stability is part of the contract. The string does not vary with the endpoint
 * or the failure, so it cannot be used to probe which call broke or how.
 */
export const CONTRACT_ERROR_MESSAGE =
  'We could not read the rating information the server sent. Please try again in a moment.';

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
 * THE MESSAGE IS FOR THE USER; THE DIAGNOSTICS ARE FOR THE DEVELOPER
 * ---------------------------------------------------------------------------
 * `message` is a fixed, user-safe sentence and carries NO internal detail. That
 * is a deliberate split, because the interface renders `error.message`: the
 * message used to be composed as `"<endpoint> returned a response that does not
 * match the rating contract: <ZodError issue list>"`, so a contract breach put
 * an endpoint label and a list of internal field paths on screen in front of
 * whoever happened to be rating a car. That tells a user nothing they can act on
 * and tells an attacker about the shape of the API.
 *
 * The detail is not discarded — discarding it would make a real breach
 * undiagnosable. It is moved to `diagnostics`, and `decode` writes it to the
 * developer console before throwing. So the information lives where developers
 * read it and not where users do.
 *
 * Carries no ES2022 `cause`. `tsconfig.json` targets ES2020, so the underlying
 * failure is exposed as an explicit `originalError` property instead. For a
 * validation failure that is the `ZodError`, whose `issues` name every offending
 * field.
 */
export class RatingContractError extends Error {
  /** Label of the endpoint whose response could not be interpreted. */
  readonly endpoint: string;

  /**
   * Developer-facing account of what did not match: the `ZodError` issue list,
   * or the field-named reason a timestamp or envelope half was rejected.
   *
   * NEVER render this. `message` is the string for a user; this is the string
   * for a console, a bug report or a test assertion.
   */
  readonly diagnostics: string;

  /** The underlying failure, typically a `ZodError` with per-field issues. */
  readonly originalError: unknown;

  constructor(endpoint: string, diagnostics: string, originalError?: unknown) {
    super(CONTRACT_ERROR_MESSAGE);
    this.name = 'RatingContractError';
    this.endpoint = endpoint;
    this.diagnostics = diagnostics;
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
 * The endpoint label and the reason are written to the developer console HERE,
 * which is what allows the thrown error's `message` to stay user-safe. A contract
 * breach is a real defect and has to be diagnosable; it just does not have to be
 * diagnosable from the screen of the person who was rating a car. The log carries
 * no request metadata of its own, because a decode failure follows a SUCCESSFUL
 * response — the interceptor has not run, and there is no axios error and so no
 * `Authorization` header anywhere in scope.
 *
 * @param endpoint Human-readable label such as `POST /ratings`, recorded on the
 *   error and in the developer log so a failure names where it came from.
 * @param decodeResponse Thunk performing the key mapping and schema validation.
 * @throws {RatingContractError} When the payload cannot be interpreted.
 */
const decode = <T>(endpoint: string, decodeResponse: () => T): T => {
  try {
    return decodeResponse();
  } catch (error) {
    const diagnostics = describeDecodeFailure(error);
    console.error('Rating API response did not match the contract', {
      endpoint,
      diagnostics
    });
    throw new RatingContractError(endpoint, diagnostics, error);
  }
};

/**
 * Convert one wire timestamp to a `Date`, or fail loudly.
 *
 * `../schema/rating` declares `createdAt`/`updatedAt` as a required `z.date()`,
 * and the server's RESPONSE model agrees: `RatingView` types both as a required
 * `datetime` and the projection that builds it refuses to emit a rating whose
 * timestamps have not resolved. The value written to Firestore is a server-side
 * sentinel that cannot be serialised, so it is replaced with a real UTC datetime
 * before any response is built. Both sides of the boundary therefore require
 * these fields, which is the alignment this helper depends on.
 *
 * It still validates rather than trusting, because a wire value is unvalidated
 * until something has looked at it: an intermediary, an older deployment or a
 * hand-written fixture can all present a payload the contract forbids, and this
 * is where that is caught and named.
 *
 * Neither failure mode is papered over. `new Date(undefined)` yields an Invalid
 * Date, which `z.date()` rejects with a message that names neither the field nor
 * the reason, and substituting `new Date()` would be worse still: it would
 * fabricate a timestamp the server never sent and display it as fact. So an
 * absent or unparseable value throws here, where the field can be named, and
 * `decode` adds the endpoint.
 *
 * THE WIRE FORMAT IS CHECKED BEFORE `new Date` IS ALLOWED NEAR IT, because
 * `new Date(string)` is far more permissive than the contract. Outside the ISO
 * formats it is implementation-defined, and every browser accepts input this
 * server never sends: `new Date('1')` is the first of January 2001,
 * `new Date('2024-05-01 12:00:00')` is accepted with a space separator and NO
 * offset, so it is silently interpreted in the reader's local time zone. A
 * `Date` built that way is a valid `Date` — `z.date()` accepts it, the rating
 * renders, and only the displayed instant is wrong, by however many hours the
 * reader happens to be from UTC. A timestamp that is quietly wrong is worse than
 * one that fails, so the string must present as a date, a time and an EXPLICIT
 * offset before it is parsed.
 *
 * Requiring the offset costs nothing against this server and is what makes the
 * value unambiguous: `backend/app/services/rating.py` stamps
 * `datetime.now(timezone.utc)` on the model it returns, and a timestamp read
 * back from Firestore is an aware UTC datetime, so pydantic serialises both with
 * a `+00:00` suffix. `Z` and a `±HH:MM`/`±HHMM` offset are accepted because all
 * three are valid ISO 8601 spellings of the same fact; a naive local timestamp is
 * not accepted, because there is no fact in it.
 *
 * MATCHING THE SHAPE IS NOT ENOUGH, because the shape admits dates that do not
 * exist and the parser SILENTLY REPAIRS them rather than refusing them — see
 * `isRealCalendarInstant`, which is what stands between "2024-02-31" and a rating
 * displayed under 2 March. The three checks run in the order a reader can act on:
 * present, then well-formed, then real.
 *
 * @param value Raw wire value: an ISO 8601 timestamp with an offset, or
 *   null/absent.
 * @param field Dotted field name used in the error message.
 * @throws {TypeError} When the value is absent, not an offset-qualified ISO 8601
 *   timestamp, does not name a real date and time, or is not a representable
 *   instant.
 */
const ISO_TIMESTAMP_PATTERN =
  /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2})(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})$/;

/**
 * Whether the calendar components a timestamp DECLARES are the components a real
 * instant has.
 *
 * The check the `Number.isNaN` test below cannot make, because JavaScript's date
 * parser does not reject an out-of-range day: it NORMALISES it. Every one of these
 * is well-formed against the pattern above and silently becomes a different date —
 *
 *   "2024-02-31T00:00:00Z"       -> 2 March 2024
 *   "2023-02-29T00:00:00Z"       -> 1 March 2023   (2023 is not a leap year)
 *   "2024-02-30T12:00:00+02:00"  -> 1 March 2024
 *
 * — so the earlier comment claiming that such a value produced an Invalid Date was
 * simply wrong, and the guard it justified never fired. A rating would then be
 * rendered and sorted under a date the server never sent, off by a day or more,
 * with nothing anywhere reporting a problem. A timestamp that is quietly wrong is
 * worse than one that fails, which is the same reasoning that requires an explicit
 * offset.
 *
 * The test is a strict round trip. The declared components are written onto a UTC
 * date, which applies exactly the normalisation the parser applies, and then read
 * back: a real instant survives unchanged, while 31 February comes back as 2 March
 * and fails on the day. It therefore needs no month-length table and no leap-year
 * rule of its own — the platform's calendar is the authority, which is what makes
 * it correct for every year rather than for the cases someone thought of.
 *
 * `setUTCFullYear` is used rather than `Date.UTC`, whose two-digit-year mapping
 * would read a declared year of `0024` as 1924 and report a legitimate — if
 * implausible — timestamp as malformed.
 *
 * The seconds component is optional in the pattern and defaults to 0, matching
 * `new Date`. The fractional part is deliberately not round-tripped: a fraction
 * cannot be out of range, and V8 truncates beyond milliseconds, so comparing it
 * would reject the perfectly valid microsecond precision Python's `isoformat`
 * emits.
 *
 * @param components The pattern's capture groups: year, month, day, hour, minute
 *   and optional second, as written.
 * @returns Whether those components name a real instant.
 */
const isRealCalendarInstant = (components: {
  year: number;
  month: number;
  day: number;
  hour: number;
  minute: number;
  second: number;
}): boolean => {
  const normalised = new Date(0);
  normalised.setUTCFullYear(
    components.year,
    components.month - 1,
    components.day
  );
  normalised.setUTCHours(components.hour, components.minute, components.second, 0);

  return (
    normalised.getUTCFullYear() === components.year &&
    normalised.getUTCMonth() === components.month - 1 &&
    normalised.getUTCDate() === components.day &&
    normalised.getUTCHours() === components.hour &&
    normalised.getUTCMinutes() === components.minute &&
    normalised.getUTCSeconds() === components.second
  );
};

const toDate = (value: string | null | undefined, field: string): Date => {
  if (typeof value !== 'string' || value.trim().length === 0) {
    const received = value === null ? 'null' : typeof value;
    throw new TypeError(
      `${field} is required: expected an ISO 8601 timestamp, received ${received}`
    );
  }

  const match = ISO_TIMESTAMP_PATTERN.exec(value);
  if (match === null) {
    throw new TypeError(
      `${field} is not an ISO 8601 timestamp with a UTC offset: "${value}"`
    );
  }

  // The pattern admits well-formed impossibilities - 31 February, an hour of 25 -
  // so the components are checked against the calendar before the string is
  // parsed. `match` groups are digit runs by construction, so each parses.
  if (
    !isRealCalendarInstant({
      year: Number(match[1]),
      month: Number(match[2]),
      day: Number(match[3]),
      hour: Number(match[4]),
      minute: Number(match[5]),
      second: match[6] === undefined ? 0 : Number(match[6])
    })
  ) {
    throw new TypeError(
      `${field} does not name a real date and time: "${value}"`
    );
  }

  const parsed = new Date(value);
  // Reached only for a value whose components ARE a real instant, so this catches
  // what remains: an offset the platform refuses, or a year beyond the range a
  // `Date` can represent.
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
 * Nullability mirrors `backend/app/schema/rating.py` field by field, and so does
 * REQUIREDNESS, which is the distinction that matters: FastAPI serialises the
 * declared response model, so every field of it is present on every payload and a
 * nullable one carries `null` rather than vanishing. Each is therefore declared
 * `T | null` and never `T | null | undefined`. Nothing here is optional, because
 * nothing the server sends is optional — an absent key is a malformed payload,
 * and modelling it as legitimate is what allowed the mappers to substitute a
 * plausible value for it.
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
 * `Rating` as it travels: the eleven fields of `RatingView`, in order.
 *
 * This mirrors the RESPONSE model — `RatingView` in
 * `backend/app/schema/rating.py` — and deliberately not the persisted `Rating`
 * document model. The two differ in exactly the two ways that matter here.
 *
 * `created_at`/`updated_at` are REQUIRED strings, not omittable-or-null. The
 * stored document types them `Optional[Any]` because a write stamps
 * `firestore.SERVER_TIMESTAMP`, a sentinel that carries no value until the
 * server resolves it; the response model types them `datetime` and the
 * projection refuses to emit a rating whose timestamps have not resolved. So a
 * response either carries both or is not produced at all, and declaring them
 * omittable here would model a payload the server cannot send while hiding the
 * one it does.
 *
 * `moderation_status` and `moderation_reason` are ABSENT.
 * Moderation state is operational: the visibility decision it drives has already
 * been applied to `is_published` and to whether `review` carries text, so a
 * public or participant reader needs none of it and is given none of it. Only
 * the admin-only moderation endpoint returns those fields, under
 * `ModeratedRatingWire` below.
 *
 * `review` is NULLABLE BUT REQUIRED: the server always returns the key, carrying
 * `null` when there is no value — the review was never written, or moderation has
 * not approved the text. Declaring it omittable would model a payload the server
 * cannot send, and it let the mapper report a truncated response as a rating whose
 * author had written nothing.
 */
export interface RatingWire {
  id: string;
  transaction_id: string;
  vehicle_listing_id: string;
  rater_id: string;
  ratee_id: string;
  direction: string;
  score: number;
  review: string | null;
  is_published: boolean;
  created_at: string;
  updated_at: string;
}

/**
 * `ModeratedRatingView` as it travels: `RatingWire` plus the moderation fields.
 *
 * Returned by `PATCH /api/ratings/{ratingId}/moderation` and by nothing else,
 * because that endpoint is gated on `role === 'admin'` and its caller has just
 * set the state they are reading back. Keeping this a separate interface from
 * `RatingWire` is what makes the wider projection impossible to reach from a
 * public read by accident: the read paths are typed to the narrow shape, so a
 * mapper that leaked moderation state would not compile.
 *
 * `moderation_reason` is the policy basis the moderator recorded: prose about
 * the review CONTENT, never anything derived from the score. The server permits
 * it only beside `rejected` and requires it there, so it carries `null` on every
 * state that displays the review.
 *
 * It is nullable but REQUIRED. `moderation_reason` is a declared field on
 * `ModeratedRatingView` and the route sets no `response_model_exclude_*`, so
 * FastAPI serialises the key on every response. Declaring it omittable would
 * model a malformed payload rather than the server, and would erase the
 * difference between "the server recorded no reason" and "the key never arrived".
 */
export interface ModeratedRatingWire extends RatingWire {
  moderation_status: string;
  moderation_reason: string | null;
}

/**
 * `RatingAggregate` as it travels.
 *
 * `average` is NULLABLE BUT NOT OMITTABLE, and the distinction is the whole
 * point of the field. `null` is a first-class state meaning "no ratings yet"
 * (`Optional[float] = None` on the server) — an ABSENT key means something else
 * entirely, and there is no legitimate response in which it happens: FastAPI
 * serialises the declared model, so `average` is always present, carrying null
 * when there is no reputation yet.
 *
 * Declaring it omittable would therefore not model the server; it would model a
 * malformed payload, and would let `toRatingAggregate` collapse a missing key
 * into `null` and render "No ratings yet" for a user who may well have a
 * reputation. Requiring the key turns that payload into a named contract failure
 * instead.
 */
export interface RatingAggregateWire {
  average: number | null;
  count: number;
}

/**
 * Envelope returned by `GET /api/ratings/user/{userId}`.
 *
 * Two keys, mirroring the server exactly. Page metadata was mirrored here and
 * removed again with the API expansion it belonged to.
 *
 * The two halves travel together because they must agree, and the server
 * establishes both from ONE pinned snapshot so they cannot disagree. Both cover
 * published ratings only.
 *
 * `items` is bounded and omits any rating whose review moderation rejected;
 * `aggregate` counts every published rating whatever its score and state. So
 * `aggregate.count` may legitimately exceed `items.length` — the contract, not a
 * contradiction.
 */
export interface UserRatingsResponseWire {
  items: RatingWire[];
  aggregate: RatingAggregateWire;
}

/**
 * `EligibilityDecision` as it travels.
 *
 * All five keys are REQUIRED, and three of them are nullable. There is nothing to
 * explain when the caller is eligible, and the server can only derive the
 * counterparty and the direction once it has confirmed the caller is a
 * participant — a decision that never got that far reports both as null. What it
 * never does is omit a key, so `undefined` here is a malformed payload and is
 * refused rather than read as "no reason" or "no counterparty".
 *
 * The combinations those three may legitimately take are constrained together by
 * `EligibilityDecisionSchema`: an eligible decision names its counterparty and
 * its direction, an ineligible one carries the refusal's own sentence, and having
 * already rated implies ineligible.
 */
export interface EligibilityDecisionWire {
  eligible: boolean;
  reason: string | null;
  ratee_id: string | null;
  direction: string | null;
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
 *
 * `moderation_reason` is the policy basis for the transition, describing the
 * violation in the review content. The server governs it with a matrix rather
 * than a default: mandatory when rejecting, refused on any other state, and
 * cleared by the transition that leaves a rejection. Both halves surface as a
 * 422, and neither is duplicated here.
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
 * NULL IS FORWARDED; AN ABSENT KEY IS REFUSED. The two are different facts and
 * neither mapper below collapses one into the other.
 *
 * Every nullable field on a rating response is nullable-but-REQUIRED on the
 * server: FastAPI serialises the declared model and no rating route sets
 * `response_model_exclude_none` or `response_model_exclude_unset`, so `review`,
 * `reason`, `ratee_id`, `direction` and `moderation_reason` are all present on
 * every payload, carrying `null` when there is nothing to report. The wire
 * interfaces above therefore declare them `T | null` rather than
 * `T | null | undefined`, and the mappers pass each value through UNCHANGED
 * instead of applying `?? null`.
 *
 * That coalescing was the defect. `wire.review ?? null` turns a truncated,
 * reshaped or wrong-version payload into a confident statement of fact — "this
 * rating carries no review", "this caller is refused for no stated reason", "this
 * user has no ratings yet" — and every one of those is rendered to a user as
 * though the server had said it. Passing the value through instead means an absent key
 * reaches a `.nullable()` schema, which refuses `undefined` and reports the
 * missing field BY NAME, and `decode` adds the endpoint. A malformed success is
 * then a named contract failure rather than a plausible-looking lie.
 *
 * Nothing else is defaulted for the same reason, and the schemas additionally
 * enforce the rules that span two fields — an eligible decision names its
 * counterparty, an ineligible one explains itself, and a positive rating count
 * requires an average.
 */

/**
 * Adapt one wire rating to the domain model.
 *
 * All eleven fields of `RatingView` are mapped; none is dropped and none is left
 * under its snake_case name. Exported so `./api` can apply the identical
 * adaptation.
 *
 * No moderation field is read here even if one is present on the object. The read
 * endpoints do not send them, and a mapper that opportunistically forwarded
 * whatever it found would carry operational state into a public projection the
 * moment a server change started including it. `toModeratedRating` is the one
 * place that state is admitted, and it is reachable only from the admin-only
 * moderation call.
 *
 * @param wire Raw rating object from any of the three endpoints that return one.
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
    review: wire.review,
    isPublished: wire.is_published,
    createdAt: toDate(wire.created_at, 'Rating.created_at'),
    updatedAt: toDate(wire.updated_at, 'Rating.updated_at')
  });

/**
 * Adapt one wire MODERATED rating to the domain model.
 *
 * The fourteen fields of `ModeratedRatingView`: everything `toRating` maps, plus
 * the three moderation fields. Used by `moderateRating` alone.
 *
 * The base fields are mapped literally rather than by delegating to `toRating`
 * and spreading its result, because `toRating` returns a value already parsed
 * against the narrow schema and re-parsing it against the wider one would
 * validate the same data twice while making the field list harder to audit. One
 * schema sees this payload once.
 *
 * `moderationReason` is carried through verbatim as the policy basis the server
 * recorded. It is not interpreted here: nothing in this module reads, branches on
 * or renders a decision from it, and nothing here can see a score alongside it,
 * so sentiment-neutral moderation is preserved by there being no mechanism to
 * violate it.
 *
 * @param wire Raw moderated rating object from the moderation endpoint.
 * @returns The validated, camelCase rating including its moderation state.
 * @throws {TypeError} When a timestamp is absent or unparseable.
 * @throws {ZodError} When any field violates `ModeratedRatingSchema`.
 */
export const toModeratedRating = (
  wire: ModeratedRatingWire
): ModeratedRating =>
  ModeratedRatingSchema.parse({
    id: wire.id,
    transactionId: wire.transaction_id,
    vehicleListingId: wire.vehicle_listing_id,
    raterId: wire.rater_id,
    rateeId: wire.ratee_id,
    direction: wire.direction,
    score: wire.score,
    review: wire.review,
    isPublished: wire.is_published,
    createdAt: toDate(wire.created_at, 'ModeratedRating.created_at'),
    updatedAt: toDate(wire.updated_at, 'ModeratedRating.updated_at'),
    moderationStatus: wire.moderation_status,
    moderationReason: wire.moderation_reason
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
 * The presence check is what stops that preservation from becoming a lie. `null`
 * and "the key was not there" are different facts, and `wire.average ?? null`
 * erases the difference: a truncated or reshaped payload would then render as a
 * confident "No ratings yet" on somebody's profile. So an absent key is refused
 * here, where the field can be named, and `decode` adds the endpoint. The
 * remaining rules — a finite average inside the scale, a non-negative whole
 * count, and the count/average bi-implication — are `RatingAggregateSchema`'s,
 * mirroring the server's own paired validators.
 *
 * @param wire Raw aggregate object.
 * @returns The validated aggregate, with `average: null` preserved as null.
 * @throws {TypeError} When the aggregate or its `average` key is absent.
 * @throws {ZodError} When either field, or the pair, violates
 *   `RatingAggregateSchema`.
 */
export const toRatingAggregate = (wire: RatingAggregateWire): RatingAggregate => {
  if (typeof wire !== 'object' || wire === null || !('average' in wire)) {
    throw new TypeError(
      'RatingAggregate.average is required: expected a number, or null for a user with no ratings yet'
    );
  }

  return RatingAggregateSchema.parse({
    average: wire.average,
    count: wire.count
  });
};

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
    reason: wire.reason,
    rateeId: wire.ratee_id,
    direction: wire.direction,
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
 * @returns The validated envelope: the published ratings a reader may see, and
 *   the aggregate over all of them.
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
 * append-only contract has nothing else to say: a state, and the policy reason
 * that justifies reaching it. The original score and words are untouched by this
 * request and there is no shape here capable of altering them.
 *
 * The reason is forwarded verbatim rather than composed, checked or classified
 * here. This mapper cannot see the score at all, which is the structural half of
 * the guarantee that moderation stays sentiment-neutral as 16 CFR Part 465
 * requires; the other half is the server's, which never lets a score drive a
 * transition.
 *
 * @param moderationStatus Target state; the union is enforced by the caller's type.
 * @param moderationReason The policy basis, describing the violation in the
 *   review content. Omitted from the body when undefined, which the server reads
 *   as clearing any recorded reason. The server requires one for a rejection and
 *   refuses one for any other state, both surfacing as a 422 — those rules have
 *   one owner and are deliberately not duplicated here.
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
  const api = createRatingApiInstance('authenticated');
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
 * transact, so it cannot require an account to see. This request therefore sends
 * NO bearer token, whether or not the reader happens to be signed in — the route
 * declares no authentication dependency and reads nothing from the caller's
 * identity, so a credential here would be spent for nothing and would make a
 * response every reader shares private to one of them.
 *
 * Published ratings only, in both halves, so an unreciprocated rating appears in
 * neither. A user with no ratings is a first-class state and not an error — an
 * empty `items` beside `average: null, count: 0`.
 *
 * THE USER ID IS THE WHOLE REQUEST
 * ---------------------------------------------------------------------------
 * No page size, no cursor, no mode. `items` is bounded by the server and omits
 * any rating whose review moderation rejected, while `aggregate` counts every
 * published rating, so `aggregate.count` may exceed `items.length` — the
 * contract, not a discrepancy. Page controls were sent from here and have been
 * removed with the server-side expansion they belonged to: the cursor was a
 * rating ID a caller supplied and the server looked up, which made a public read
 * answer questions about ratings the caller could not otherwise see.
 *
 * The server settles any publication already due before answering, and takes
 * both halves from one pinned snapshot, so this response can neither be waiting
 * on a worker nor contradict itself.
 *
 * @param userId The user whose received ratings are wanted.
 * @returns The published ratings received, newest first, and the aggregate over
 *   all of them.
 * @throws {AxiosError} 404 when no such user exists.
 * @throws {RatingContractError} When the envelope cannot be interpreted.
 */
export const fetchUserRatings = async (
  userId: string
): Promise<UserRatingsResponse> => {
  // PUBLIC: no credential is attached, matching the endpoint's own contract.
  const api = createRatingApiInstance('public');
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
 * NOT a separate endpoint and not a separate mode: it reads the same
 * `GET /api/ratings/user/{userId}` response and keeps the `aggregate` half. An
 * `aggregate_only=true` mode was requested from here and has been removed,
 * because it answered from the user document WITHOUT settling publications that
 * were already due — and this is the surface that renders beside every listing,
 * so the most-read reputation in the product was the one that could sit stale
 * while a worker that will never run was nominally responsible for revealing it.
 *
 * The cost of dropping it is honest and bounded: this call transfers the bounded
 * `items` array it does not use. Correct-and-settled beats cheap-and-stale for a
 * number the whole feature exists to report, and the aggregate itself is still
 * one denormalised document read on the server.
 *
 * A public read, so it sends no bearer token — it delegates to
 * `fetchUserRatings`, which attaches none. This is the surface that renders
 * beside every listing, so it is also the one where an unnecessary credential
 * would have been put on the wire most often.
 *
 * SETTLEMENT IS NEVER SKIPPED
 * ---------------------------------------------------------------------------
 * The server settles any rating whose window has elapsed before it answers,
 * which it must: publication has no worker behind it, so a read is the only
 * thing that ever performs one. A mode that skipped settlement would make this
 * the one reputation figure in the system permitted to be behind — on the
 * surface a buyer consults before transacting, and visibly disagreeing with the
 * same user's profile page. Reading the aggregate out of the settled envelope is
 * what guarantees this function and `fetchUserRatings` can never report
 * different reputations for the same user.
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
  // PUBLIC: delegates to `fetchUserRatings`, which attaches no credential.
  const response = await fetchUserRatings(userId);
  return response.aggregate;
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
  const api = createRatingApiInstance('authenticated');
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
  const api = createRatingApiInstance('authenticated');
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
 * This is the ONLY call that resolves with a `ModeratedRating`. Every read path
 * resolves with the narrower `Rating`, which carries no moderation state at all,
 * because the visibility decision that state drives has already been applied to
 * what a reader receives. The wider shape is reachable here and nowhere else.
 *
 * @param ratingId The rating to transition.
 * @param moderationStatus Target state: `pending`, `approved` or `rejected`.
 * @param moderationReason The policy basis justifying the transition. Required
 *   for a rejection and refused on any other state, both enforced by the server;
 *   omitting it clears any recorded reason.
 * @returns The updated rating, including its moderation state.
 * @throws {AxiosError} 401 unauthenticated; 403 the caller is not an
 *   administrator; 404 no such rating; 422 a rejection carrying no policy basis,
 *   or a reason supplied for a state that displays the review.
 * @throws {RatingContractError} When the updated rating cannot be interpreted.
 */
export const moderateRating = async (
  ratingId: string,
  moderationStatus: ModerationStatus,
  moderationReason?: string
): Promise<ModeratedRating> => {
  const api = createRatingApiInstance('authenticated');
  const response = await api.patch<ModeratedRatingWire>(
    `${RATINGS_PATH}/${encodeURIComponent(ratingId)}/moderation`,
    toModerationWire(moderationStatus, moderationReason)
  );

  return decode(`PATCH ${RATINGS_PATH}/{ratingId}/moderation`, () =>
    toModeratedRating(response.data)
  );
};
