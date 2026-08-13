/**
 * Tests for `../rating`, the typed client for the bidirectional peer reputation
 * API and the single owner of the snake_case <-> camelCase adaptation for this
 * domain — the client half of SRS F010 "Review and Rating System".
 *
 * WHY THIS SUITE EXISTS SEPARATELY FROM THE COMPONENT SUITES
 * -----------------------------------------------------------------------------
 * Every rating screen mocks this module, which is correct — a component test that
 * reached the network would be testing the network. The consequence is that
 * nothing else in the repository exercises the module itself, so a caller/backend
 * mismatch here breaks every screen while every component suite stays green: a
 * renamed wire field, a path that lost its `/user` segment, a token that stopped
 * being attached, a timestamp that silently became an `Invalid Date`. This file is
 * the only place those are visible, so it asserts the WIRE CONTRACT rather than
 * behaviour any component can observe.
 *
 * WHAT IS FAKED, AND WHY IT IS `axios.create` AND NOTHING ELSE
 * -----------------------------------------------------------------------------
 * `vi.spyOn(axios, 'create')` replaces exactly one function: the factory the
 * module calls to build a client. Everything else is production code —
 * `resolveApiBaseUrl`, both interceptor registrations, `readAuthToken`,
 * `readServerDetail`, `describeRequestFailure`, `decode`, `toDate`, all seven
 * mappers and the six endpoint wrappers.
 *
 * A whole-module `vi.mock('axios', …)` was deliberately avoided: it would replace
 * `axios.isAxiosError`, which is the gate `readServerDetail` opens on, so every
 * error-detail assertion in this file would then be measuring the fake rather than
 * the real predicate. Spying on the factory keeps that predicate real while still
 * making the requests observable and offline.
 *
 * The fake instance records what was asked of it and answers from state the test
 * sets — `serverReturns(...)` / `serverFails(...)` — because the service builds
 * its instance INSIDE each call, so a per-instance `mockResolvedValue` could never
 * be armed in time. Interceptors are recorded rather than run, which is what makes
 * them individually assertable: the registered handlers are pulled out of the
 * recorder and invoked directly.
 *
 * WHY THE BASE-URL CASES RE-IMPORT THE MODULE
 * -----------------------------------------------------------------------------
 * `RAW_API_BASE_URL` is read once, at module load, from
 * `process.env.REACT_APP_API_BASE_URL` — this project's environment convention,
 * supplied to client code through the `define` map in `../../../vite.config.ts`.
 * Changing the variable after import therefore has no effect on the module already
 * loaded, so those cases stub the variable, reset the module registry and import a
 * FRESH copy. That is the only way to reach the four refusals, and reaching them
 * matters: an unset base URL makes axios resolve every path against whatever
 * origin the page happens to be served from, which is a silent misconfiguration
 * rather than a loud one.
 *
 * `describe`, `it`, `expect`, `beforeEach` and `afterEach` are injected globals
 * (`test.globals: true`), typed by the ambient `@types/jest` declarations
 * TypeScript includes automatically; `vi` is declared below because those
 * declarations know nothing about it. Note that `tsconfig.json` excludes every
 * `.test.ts` file from the type program, recursively, while including every
 * `.test.tsx` one, so this file is checked by neither `tsc --noEmit` nor
 * `npm run build` — it is nevertheless written to type-check, and was verified
 * against the project's own compiler options with that exclusion lifted.
 */

import axios from 'axios';
import type { AxiosInstance } from 'axios';

import * as apiClient from '../api';
import {
  CONTRACT_ERROR_MESSAGE,
  RatingContractError,
  describeRequestFailure,
  fetchRatingEligibility,
  fetchTransactionRatings,
  fetchUserRatings,
  fetchUserReputation,
  moderateRating,
  readAuthToken,
  readServerDetail,
  submitRating,
  toEligibilityDecision,
  toModeratedRating,
  toModerationWire,
  toRating,
  toRatingAggregate,
  toRatingCreateWire,
  toUserRatingsResponse,
} from '../rating';
import type {
  EligibilityDecisionWire,
  ModeratedRatingWire,
  RatingAggregateWire,
  RatingWire,
  UserRatingsResponseWire,
} from '../rating';

/**
 * `vi` as a TYPE ONLY, resolved to Vitest's injected global at runtime.
 *
 * `typeof import(...)` is a type position, so this emits no runtime import: the
 * identifier resolves to the global Vitest injects.
 */
declare const vi: typeof import('vitest')['vi'];

/* -------------------------------------------------------------------------- */
/* The offline axios double                                                   */
/* -------------------------------------------------------------------------- */

/** A request as the fake recorded it. */
interface RecordedRequest {
  method: 'get' | 'post' | 'patch';
  path: string;
  body?: unknown;
}

/** The shape a request interceptor is handed and must return. */
interface InterceptedConfig {
  headers: Record<string, string>;
}

type RequestInterceptor = (config: InterceptedConfig) => InterceptedConfig;
type ResponseRejectionHandler = (error: unknown) => Promise<unknown>;

/**
 * One fake axios instance: it records every call and every interceptor
 * registration, and answers requests from the module-level response state.
 */
interface FakeInstance {
  /** The config the module passed to `axios.create`. */
  config: unknown;
  requests: RecordedRequest[];
  requestInterceptors: RequestInterceptor[];
  responseFulfilled: Array<(response: unknown) => unknown>;
  responseRejections: ResponseRejectionHandler[];
  get: (path: string) => Promise<{ data: unknown }>;
  post: (path: string, body?: unknown) => Promise<{ data: unknown }>;
  patch: (path: string, body?: unknown) => Promise<{ data: unknown }>;
  interceptors: {
    request: { use: (handler: RequestInterceptor) => void };
    response: {
      use: (
        onFulfilled: (response: unknown) => unknown,
        onRejected: ResponseRejectionHandler,
      ) => void;
    };
  };
}

/** Every instance the module built during the current test, in order. */
let createdInstances: FakeInstance[] = [];

/** What the fake answers with while no failure is armed. */
let responseBody: unknown = null;

/** The rejection the fake answers with, or `null` to succeed. */
let armedFailure: unknown = null;

/** Arms the fake to answer every request with `data`. */
const serverReturns = (data: unknown): void => {
  responseBody = data;
  armedFailure = null;
};

/** Arms the fake to reject every request with `error`. */
const serverFails = (error: unknown): void => {
  armedFailure = error;
};

const serve = (): Promise<{ data: unknown }> =>
  armedFailure === null
    ? Promise.resolve({ data: responseBody })
    : Promise.reject(armedFailure);

const makeFakeInstance = (config: unknown): FakeInstance => {
  const instance: FakeInstance = {
    config,
    requests: [],
    requestInterceptors: [],
    responseFulfilled: [],
    responseRejections: [],
    get: (path) => {
      instance.requests.push({ method: 'get', path });
      return serve();
    },
    post: (path, body) => {
      instance.requests.push({ method: 'post', path, body });
      return serve();
    },
    patch: (path, body) => {
      instance.requests.push({ method: 'patch', path, body });
      return serve();
    },
    interceptors: {
      request: {
        use: (handler) => {
          instance.requestInterceptors.push(handler);
        },
      },
      response: {
        use: (onFulfilled, onRejected) => {
          instance.responseFulfilled.push(onFulfilled);
          instance.responseRejections.push(onRejected);
        },
      },
    },
  };

  createdInstances.push(instance);

  return instance;
};

/**
 * Installs the double over `axios.create` and returns nothing.
 *
 * `mockImplementation` rather than `mockReturnValue`, so each call gets its OWN
 * recorder: the module builds a fresh client per request, and sharing one would
 * make "was a request interceptor registered for THIS call" unanswerable.
 */
const installAxiosDouble = (): void => {
  vi.spyOn(axios, 'create').mockImplementation(
    (config?: unknown) => makeFakeInstance(config) as unknown as AxiosInstance,
  );
};

/** The instance the call under test built. Fails loudly when there is none. */
const onlyInstance = (): FakeInstance => {
  if (createdInstances.length !== 1) {
    throw new Error(
      `Expected exactly one axios instance to have been created, and found ${createdInstances.length}.`,
    );
  }

  return createdInstances[0];
};

/** The single request the call under test made. */
const onlyRequest = (): RecordedRequest => {
  const requests = createdInstances.flatMap((instance) => instance.requests);

  if (requests.length !== 1) {
    throw new Error(
      `Expected exactly one request to have been made, and found ${requests.length}.`,
    );
  }

  return requests[0];
};

/* -------------------------------------------------------------------------- */
/* Console capture                                                            */
/* -------------------------------------------------------------------------- */

/**
 * Runs `call`, requires it to REJECT, and returns the reason typed as `T`.
 *
 * `await call().catch((error) => error)` was the obvious spelling and is worse in
 * two ways. Its result is typed as the union of the resolved value and the
 * rejection, so every assertion on a rejection property needs a cast; and a call
 * that unexpectedly SUCCEEDS flows on silently, so the assertions then run against
 * a decoded rating and report something unrelated to the actual failure. This
 * throws instead, naming what happened.
 *
 * @param call The invocation under test.
 * @throws When the call resolves, which means the case did not reproduce.
 */
const rejectionOf = async <T,>(call: () => Promise<unknown>): Promise<T> => {
  try {
    await call();
  } catch (error) {
    return error as T;
  }

  throw new Error(
    'Expected the call under test to reject, and it resolved instead.',
  );
};

/**
 * Every `console.error` call made during the current test, newest last.
 *
 * Captured rather than merely silenced because this module logs deliberately and
 * the CONTENT of those logs is a security property: an axios error carries the
 * bearer token in `config.headers` and the request body in `config.data`, so
 * writing the error object down would publish a live credential to the console
 * and to every telemetry agent mirroring it (CWE-532).
 */
let loggedErrors: unknown[][] = [];

const logsFor = (message: string): unknown[][] =>
  loggedErrors.filter((entry) => entry[0] === message);

/* -------------------------------------------------------------------------- */
/* Wire fixtures — what the SERVER sends, in the server's own casing          */
/* -------------------------------------------------------------------------- */

/**
 * One rating exactly as `RatingView` serialises it in
 * `backend/app/schema/rating.py`: snake_case keys, ISO-8601 timestamps as
 * strings, `review` nullable, `is_published` false for a fresh rating.
 */
const RATING_WIRE: RatingWire = {
  id: 'txn-1_user-buyer-7',
  transaction_id: 'txn-1',
  vehicle_listing_id: 'listing-9',
  rater_id: 'user-buyer-7',
  ratee_id: 'user-seller-42',
  direction: 'buyer_to_seller',
  score: 4,
  review: 'The paperwork was in order.',
  is_published: false,
  created_at: '2024-05-01T10:00:00Z',
  updated_at: '2024-05-01T10:30:00Z',
};

/** The same rating as the admin-only moderation response returns it. */
const MODERATED_RATING_WIRE: ModeratedRatingWire = {
  ...RATING_WIRE,
  moderation_status: 'approved',
  moderation_reason: null,
};

/** A reputation aggregate for a user with two published ratings. */
const AGGREGATE_WIRE: RatingAggregateWire = { average: 4.5, count: 2 };

/** The aggregate for a user who exists and has never been rated. */
const UNRATED_AGGREGATE_WIRE: RatingAggregateWire = { average: null, count: 0 };

/** One published rating plus its aggregate, as the public read returns them. */
const USER_RATINGS_WIRE: UserRatingsResponseWire = {
  items: [{ ...RATING_WIRE, is_published: true }],
  aggregate: { average: 4, count: 1 },
};

/** The decision for a caller who may rate. */
const ELIGIBLE_WIRE: EligibilityDecisionWire = {
  eligible: true,
  reason: null,
  ratee_id: 'user-seller-42',
  direction: 'buyer_to_seller',
  already_rated: false,
};

/** The decision for a caller who has already rated this transaction. */
const INELIGIBLE_WIRE: EligibilityDecisionWire = {
  eligible: false,
  reason: 'You have already rated this transaction',
  ratee_id: 'user-seller-42',
  direction: 'buyer_to_seller',
  already_rated: true,
};

/** The base URL the test environment supplies, and the one every call must use. */
const EXPECTED_BASE_URL = 'http://localhost:8000/api';

/** The storage key the client reads its bearer token from. */
const TOKEN_KEY = 'authToken';

/* -------------------------------------------------------------------------- */
/* Fresh-module helper for the configuration cases                            */
/* -------------------------------------------------------------------------- */

/**
 * Loads a FRESH copy of the module with `REACT_APP_API_BASE_URL` set to `value`,
 * and returns it together with a fresh axios double.
 *
 * Both imports happen after `vi.resetModules()`, so the copy under test and the
 * `axios` this spy is installed on are the same fresh instances — spying on the
 * outer, already-loaded axios would have no effect on a re-imported module.
 *
 * @param value The environment value to load the module with.
 */
const loadModuleWithBaseUrl = async (
  value: string,
): Promise<typeof import('../rating')> => {
  vi.resetModules();
  vi.stubEnv('REACT_APP_API_BASE_URL', value);

  const freshAxios = (await import('axios')).default;
  vi.spyOn(freshAxios, 'create').mockImplementation(
    (config?: unknown) => makeFakeInstance(config) as unknown as AxiosInstance,
  );

  return import('../rating');
};

describe('rating API client', () => {
  beforeEach(() => {
    createdInstances = [];
    responseBody = null;
    armedFailure = null;
    loggedErrors = [];

    localStorage.clear();
    installAxiosDouble();

    vi.spyOn(console, 'error').mockImplementation((...args: unknown[]) => {
      loggedErrors.push(args);
    });
  });

  afterEach(() => {
    // Restores `axios.create` and `console.error`, and drops any spy installed on
    // a freshly imported axios by `loadModuleWithBaseUrl`.
    vi.restoreAllMocks();
    // Undoes `vi.stubEnv` and returns the registry to the statically imported
    // copies, so a configuration case cannot leak into the next test.
    vi.unstubAllEnvs();
    vi.resetModules();
    localStorage.clear();
  });

  describe('server configuration', () => {
    it('builds every client against the configured base URL, with no extra options', async () => {
      serverReturns(ELIGIBLE_WIRE);

      await fetchRatingEligibility('txn-1');

      // Exact equality on the whole config: axios options are load-bearing (a
      // stray `withCredentials`, `timeout` or `headers` here would change every
      // rating request at once), so the assertion is that nothing else is set.
      expect(onlyInstance().config).toEqual({ baseURL: EXPECTED_BASE_URL });
    });

    it('refuses to build a client when the base URL is not configured', async () => {
      const fresh = await loadModuleWithBaseUrl('');

      // The failure is deliberately at CALL time rather than import time: a module
      // that threw while loading would take the whole bundle down, and the message
      // has to reach whoever is configuring the deployment.
      await expect(fresh.fetchUserRatings('user-1')).rejects.toThrow(
        /REACT_APP_API_BASE_URL is not set/,
      );

      // Nothing was attempted. An unset base URL makes axios resolve every path
      // against whatever origin served the page, which is a silent
      // misconfiguration; refusing before the request is what makes it loud.
      expect(createdInstances).toHaveLength(0);
    });

    it('treats a blank base URL as unset rather than as a relative origin', async () => {
      const fresh = await loadModuleWithBaseUrl('   ');

      await expect(fresh.fetchUserRatings('user-1')).rejects.toThrow(
        /REACT_APP_API_BASE_URL is not set/,
      );
    });

    it('refuses a base URL that is not absolute', async () => {
      // A path without an origin is the classic mistake — it looks right in a
      // `.env` file and resolves against the page's own host at run time.
      const fresh = await loadModuleWithBaseUrl('/api');

      await expect(fresh.fetchUserRatings('user-1')).rejects.toThrow(
        /must be an absolute URL/,
      );
    });

    it('refuses a base URL whose scheme is not http or https', async () => {
      const fresh = await loadModuleWithBaseUrl('ftp://files.example.com/api');

      await expect(fresh.fetchUserRatings('user-1')).rejects.toThrow(
        /must use http or https/,
      );
    });

    it('refuses a host and port with no scheme, which parses as a URL but is not one', async () => {
      // `new URL('localhost:8000/api')` succeeds with a protocol of `localhost:`,
      // so the absolute-URL check alone would let this through — the scheme check
      // is what catches it, and this case is why both exist.
      const fresh = await loadModuleWithBaseUrl('localhost:8000/api');

      await expect(fresh.fetchUserRatings('user-1')).rejects.toThrow(
        /must use http or https/,
      );
    });

    it('trims trailing slashes so a path is never joined onto a doubled separator', async () => {
      const fresh = await loadModuleWithBaseUrl('http://localhost:8000/api///');
      serverReturns(USER_RATINGS_WIRE);

      await fresh.fetchUserRatings('user-1');

      expect(onlyInstance().config).toEqual({ baseURL: EXPECTED_BASE_URL });
    });
  });

  describe('authentication policy', () => {
    it('attaches the bearer token to an authenticated request', async () => {
      localStorage.setItem(TOKEN_KEY, 'token-abc');
      serverReturns(ELIGIBLE_WIRE);

      await fetchRatingEligibility('txn-1');

      const [attachToken] = onlyInstance().requestInterceptors;
      expect(attachToken).toBeDefined();

      // The interceptor is invoked with the config axios would have handed it, so
      // the assertion is on what it WRITES rather than on its mere registration.
      expect(attachToken({ headers: {} }).headers).toEqual({
        Authorization: 'Bearer token-abc',
      });
    });

    it('sends no authorization header when no token is stored', async () => {
      serverReturns(ELIGIBLE_WIRE);

      await fetchRatingEligibility('txn-1');

      const [attachToken] = onlyInstance().requestInterceptors;

      // Absent, not empty: `Authorization: Bearer ` would be a malformed header
      // the server would have to reject rather than treat as anonymous.
      expect(attachToken({ headers: {} }).headers).toEqual({});
    });

    it('reads the token from the shared storage key and no other', async () => {
      localStorage.setItem(TOKEN_KEY, 'token-abc');
      localStorage.setItem('token', 'wrong-key');

      // Asserted through the exported reader rather than through a request, so the
      // key itself is pinned: this is the one string that has to agree with
      // `./auth`, which writes it.
      expect(readAuthToken()).toBe('token-abc');
    });

    it('reports no token rather than throwing when storage is unavailable', () => {
      // Safari's private mode and a storage quota refusal both make `getItem`
      // throw. A client that propagated that would turn a browser setting into a
      // dead rating screen, so the reader answers "anonymous" instead.
      vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
        throw new Error('access denied');
      });

      expect(readAuthToken()).toBeNull();
    });

    /**
     * Which endpoints carry a credential, stated as a table because it IS the
     * authorization contract: three of the reads and both writes are
     * authenticated, and the public read must not attach a token at all.
     */
    const AUTHENTICATED_CALLS: ReadonlyArray<{
      readonly name: string;
      readonly wire: unknown;
      readonly call: () => Promise<unknown>;
    }> = [
      {
        name: 'submitRating',
        wire: RATING_WIRE,
        call: () => submitRating({ transactionId: 'txn-1', score: 4 }),
      },
      {
        name: 'fetchTransactionRatings',
        wire: [RATING_WIRE],
        call: () => fetchTransactionRatings('txn-1'),
      },
      {
        name: 'fetchRatingEligibility',
        wire: ELIGIBLE_WIRE,
        call: () => fetchRatingEligibility('txn-1'),
      },
      {
        name: 'moderateRating',
        wire: MODERATED_RATING_WIRE,
        call: () => moderateRating('txn-1_user-buyer-7', 'approved'),
      },
    ];

    AUTHENTICATED_CALLS.forEach(({ name, wire, call }) => {
      it(`sends ${name} as an authenticated request`, async () => {
        serverReturns(wire);

        await call();

        expect(onlyInstance().requestInterceptors).toHaveLength(1);
      });
    });

    const PUBLIC_CALLS: ReadonlyArray<{
      readonly name: string;
      readonly call: () => Promise<unknown>;
      readonly wire: unknown;
    }> = [
      {
        name: 'fetchUserRatings',
        call: () => fetchUserRatings('user-42'),
        wire: USER_RATINGS_WIRE,
      },
      // Its own body, because the two public reads answer with different shapes:
      // the reputation read addresses the aggregate-only endpoint and decodes the
      // body AS a `RatingAggregate`, so feeding it the full response would fail
      // decoding for a reason that has nothing to do with the credential policy
      // under test here.
      {
        name: 'fetchUserReputation',
        call: () => fetchUserReputation('user-42'),
        wire: AGGREGATE_WIRE,
      },
    ];

    PUBLIC_CALLS.forEach(({ name, call, wire }) => {
      it(`sends ${name} as a public request with no credential`, async () => {
        localStorage.setItem(TOKEN_KEY, 'token-abc');
        serverReturns(wire);

        await call();

        // No request interceptor is registered AT ALL for a public client, which is
        // stronger than one that happens to find no token: the endpoint is
        // unauthenticated by design and a reputation read must not be able to leak
        // the caller's credential to it.
        expect(onlyInstance().requestInterceptors).toHaveLength(0);
      });
    });
  });

  describe('endpoint contracts', () => {
    it('submits a rating as a POST carrying only the three permitted fields', async () => {
      serverReturns(RATING_WIRE);

      await submitRating({
        transactionId: 'txn-1',
        score: 4,
        review: 'The paperwork was in order.',
      });

      // Exact shape, in the server's casing. `rater_id`, `ratee_id` and
      // `direction` are derived server-side from the authenticated caller and the
      // cited transaction, which is what makes counterparty spoofing and
      // self-rating structurally impossible rather than merely validated against —
      // so an extra key here has to fail, and only exact equality does that.
      expect(onlyRequest()).toEqual({
        method: 'post',
        path: '/ratings',
        body: {
          transaction_id: 'txn-1',
          score: 4,
          review: 'The paperwork was in order.',
        },
      });
    });

    it('omits the review key entirely when no review was written', async () => {
      serverReturns(RATING_WIRE);

      await submitRating({ transactionId: 'txn-1', score: 4 });

      const body = onlyRequest().body as Record<string, unknown>;

      // `review` is optional on the wire: omitting the key states that no review
      // was written, while `null` or `""` states that one was and is blank.
      expect(body).toEqual({ transaction_id: 'txn-1', score: 4 });
      expect('review' in body).toBe(false);
    });

    it('reads a user reputation from the public per-user path', async () => {
      serverReturns(USER_RATINGS_WIRE);

      await fetchUserRatings('user-42');

      expect(onlyRequest()).toEqual({
        method: 'get',
        path: '/ratings/user/user-42',
      });
    });

    it('reads a transaction pair from the participant-only path', async () => {
      serverReturns([RATING_WIRE]);

      await fetchTransactionRatings('txn-1');

      expect(onlyRequest()).toEqual({
        method: 'get',
        path: '/ratings/transaction/txn-1',
      });
    });

    it('asks about eligibility on the dedicated path', async () => {
      serverReturns(ELIGIBLE_WIRE);

      await fetchRatingEligibility('txn-1');

      expect(onlyRequest()).toEqual({
        method: 'get',
        path: '/ratings/eligibility/txn-1',
      });
    });

    it('moderates a rating as a PATCH on the rating, carrying the state and its reason', async () => {
      serverReturns(MODERATED_RATING_WIRE);

      await moderateRating(
        'txn-1_user-buyer-7',
        'rejected',
        'Contains personally identifying information',
      );

      expect(onlyRequest()).toEqual({
        method: 'patch',
        path: '/ratings/txn-1_user-buyer-7/moderation',
        body: {
          moderation_status: 'rejected',
          moderation_reason: 'Contains personally identifying information',
        },
      });
    });

    it('omits the moderation reason when none is given', async () => {
      serverReturns(MODERATED_RATING_WIRE);

      await moderateRating('txn-1_user-buyer-7', 'approved');

      const body = onlyRequest().body as Record<string, unknown>;

      expect(body).toEqual({ moderation_status: 'approved' });
      expect('moderation_reason' in body).toBe(false);
    });

    it('percent-encodes an identifier so it cannot introduce a path segment', async () => {
      serverReturns(USER_RATINGS_WIRE);

      // A slash in an identifier is the interesting case: unencoded, `../admin`
      // and `a/b` would each rewrite the URL into a different endpoint, which is
      // how a client turns a data value into a routing decision.
      await fetchUserRatings('a/b');

      expect(onlyRequest().path).toBe('/ratings/user/a%2Fb');
    });

    it('encodes every identifier the five paths interpolate', async () => {
      const awkward = 'id with spaces/and?query#hash';
      const encoded = encodeURIComponent(awkward);

      serverReturns([RATING_WIRE]);
      await fetchTransactionRatings(awkward);
      expect(onlyRequest().path).toBe(`/ratings/transaction/${encoded}`);

      createdInstances = [];
      serverReturns(ELIGIBLE_WIRE);
      await fetchRatingEligibility(awkward);
      expect(onlyRequest().path).toBe(`/ratings/eligibility/${encoded}`);

      createdInstances = [];
      serverReturns(MODERATED_RATING_WIRE);
      await moderateRating(awkward, 'approved');
      expect(onlyRequest().path).toBe(`/ratings/${encoded}/moderation`);

      // The interpolated value never escapes its own segment: the raw string,
      // which carries a `/`, a `?` and a `#`, appears nowhere in the URL.
      expect(onlyRequest().path).not.toContain(awkward);
      expect(onlyRequest().path).not.toContain('?');
      expect(onlyRequest().path).not.toContain('#');
    });

    it('reads the aggregate from the aggregate-only endpoint', async () => {
      serverReturns(AGGREGATE_WIRE);

      const aggregate = await fetchUserReputation('user-42');

      // ONE request, and to the endpoint that answers with the summary alone. It
      // used to read `/ratings/user/{id}` and discard every item, which made a
      // badge cost the server a paginated scan of the ratee's rating history -
      // against a 200 ms budget, the difference between one document read and
      // hundreds. The path is asserted, not just the request count, because a
      // reputation read that quietly went back to the full list would still be
      // one call.
      expect(onlyRequest()).toEqual({
        method: 'get',
        path: '/ratings/user/user-42/aggregate',
      });
      expect(aggregate).toEqual({ average: 4.5, count: 2 });
    });
  });


  describe('response decoding', () => {
    it('maps every field of a rating from the server casing to the client casing', async () => {
      serverReturns(RATING_WIRE);

      const rating = await submitRating({ transactionId: 'txn-1', score: 4 });

      // Exact equality on the whole object: a mapper that dropped a field, or
      // carried a snake_case key through unchanged, has to fail here. `Date`
      // instances compare by value under `toEqual`, so the timestamps are checked
      // as instants rather than as strings.
      expect(rating).toEqual({
        id: 'txn-1_user-buyer-7',
        transactionId: 'txn-1',
        vehicleListingId: 'listing-9',
        raterId: 'user-buyer-7',
        rateeId: 'user-seller-42',
        direction: 'buyer_to_seller',
        score: 4,
        review: 'The paperwork was in order.',
        isPublished: false,
        createdAt: new Date('2024-05-01T10:00:00Z'),
        updatedAt: new Date('2024-05-01T10:30:00Z'),
      });

      // Real `Date` objects, not strings that happen to compare equal: every
      // consumer formats these, and a string would throw on `.getTime()`.
      expect(rating.createdAt).toBeInstanceOf(Date);
      expect(rating.updatedAt).toBeInstanceOf(Date);
    });

    it('preserves an absent review as null rather than as an empty string', async () => {
      serverReturns({ ...RATING_WIRE, review: null });

      const rating = await submitRating({ transactionId: 'txn-1', score: 4 });

      // The server says "no review was written" with null and "one was written and
      // is blank" with an empty string. Collapsing them would make a scoreless
      // remark indistinguishable from silence.
      expect(rating.review).toBeNull();
    });

    it('carries the published flag through as the server reported it', async () => {
      serverReturns({ ...RATING_WIRE, is_published: true });

      const rating = await submitRating({ transactionId: 'txn-1', score: 4 });

      // Under the double-blind reveal this is the difference between "recorded and
      // visible" and "recorded and withheld", which is what the submitting screen
      // tells the user — so it must never be inferred or defaulted.
      expect(rating.isPublished).toBe(true);
    });

    it('projects a moderated rating as the rating fields plus exactly two more', () => {
      // Asserted on the mapper directly and by exact equality, because the
      // moderated view is the ONLY response that carries moderation state: if it
      // ever grew a field, or lost one, every admin screen would silently read a
      // different shape. Going through the mapper rather than the endpoint is what
      // makes the whole projection visible in one assertion.
      expect(
        toModeratedRating({
          ...MODERATED_RATING_WIRE,
          moderation_status: 'rejected',
          moderation_reason: 'Abusive language',
        }),
      ).toEqual({
        id: 'txn-1_user-buyer-7',
        transactionId: 'txn-1',
        vehicleListingId: 'listing-9',
        raterId: 'user-buyer-7',
        rateeId: 'user-seller-42',
        direction: 'buyer_to_seller',
        score: 4,
        review: 'The paperwork was in order.',
        isPublished: false,
        createdAt: new Date('2024-05-01T10:00:00Z'),
        updatedAt: new Date('2024-05-01T10:30:00Z'),
        moderationStatus: 'rejected',
        moderationReason: 'Abusive language',
      });
    });

    it('maps the moderation fields the admin response adds', async () => {
      serverReturns({
        ...MODERATED_RATING_WIRE,
        moderation_status: 'rejected',
        moderation_reason: 'Contains personally identifying information',
      });

      const moderated = await moderateRating(
        'txn-1_user-buyer-7',
        'rejected',
        'Contains personally identifying information',
      );

      expect(moderated.moderationStatus).toBe('rejected');
      expect(moderated.moderationReason).toBe(
        'Contains personally identifying information',
      );

      // And the rating fields are still mapped, so the moderated view is a
      // superset rather than a different shape.
      expect(moderated.id).toBe('txn-1_user-buyer-7');
      expect(moderated.createdAt).toBeInstanceOf(Date);
    });

    it('maps an eligibility decision, including a refusal and its reason', async () => {
      serverReturns(INELIGIBLE_WIRE);

      const refused = await fetchRatingEligibility('txn-1');

      expect(refused).toEqual({
        eligible: false,
        reason: 'You have already rated this transaction',
        rateeId: 'user-seller-42',
        direction: 'buyer_to_seller',
        alreadyRated: true,
      });
    });

    it('maps a decision that names neither counterparty nor direction', async () => {
      serverReturns({
        eligible: false,
        reason: 'You are not a party to this transaction',
        ratee_id: null,
        direction: null,
        already_rated: false,
      });

      const refused = await fetchRatingEligibility('txn-1');

      // Both are null until the caller is confirmed a participant, so a rejected
      // caller learns nothing about a transaction they are not part of. Nulls have
      // to survive the mapping rather than becoming `undefined` or `''`.
      expect(refused.rateeId).toBeNull();
      expect(refused.direction).toBeNull();
    });

    it('maps a user reputation response, items and aggregate together', async () => {
      serverReturns(USER_RATINGS_WIRE);

      const response = await fetchUserRatings('user-42');

      expect(response.aggregate).toEqual({ average: 4, count: 1 });
      expect(response.items).toHaveLength(1);
      expect(response.items[0].isPublished).toBe(true);
      expect(response.items[0].createdAt).toBeInstanceOf(Date);
    });

    it('maps the aggregate of a user who exists and has never been rated', async () => {
      serverReturns({ items: [], aggregate: UNRATED_AGGREGATE_WIRE });

      const response = await fetchUserRatings('user-42');

      // A null average with a zero count is valid data and the normal state of a
      // new account — not an error, and not something to default to zero, which
      // would render as a one-star reputation nobody earned.
      expect(response.aggregate).toEqual({ average: null, count: 0 });
      expect(response.items).toEqual([]);
    });

    it('maps a transaction pair, in the order the server returned them', async () => {
      const other: RatingWire = {
        ...RATING_WIRE,
        id: 'txn-1_user-seller-42',
        rater_id: 'user-seller-42',
        ratee_id: 'user-buyer-7',
        direction: 'seller_to_buyer',
        score: 5,
      };

      serverReturns([RATING_WIRE, other]);

      const ratings = await fetchTransactionRatings('txn-1');

      expect(ratings.map((rating) => rating.direction)).toEqual([
        'buyer_to_seller',
        'seller_to_buyer',
      ]);
      expect(ratings.map((rating) => rating.score)).toEqual([4, 5]);
    });

    it('maps an empty transaction pair to an empty list', async () => {
      serverReturns([]);

      await expect(fetchTransactionRatings('txn-1')).resolves.toEqual([]);
    });
  });

  describe('timestamp decoding', () => {
    /**
     * The timestamp forms the backend can emit, each with the instant it names.
     *
     * `firestore.SERVER_TIMESTAMP` is serialised by FastAPI as an ISO-8601 string,
     * and the exact form has varied with the client library — with and without
     * fractional seconds, with `Z` or with a numeric offset written either
     * `+02:00` or `+0200`. All of them are the same kind of value and all must
     * decode, because a rejected timestamp fails the WHOLE response.
     */
    const ACCEPTED_TIMESTAMPS: ReadonlyArray<{
      readonly value: string;
      readonly instant: string;
    }> = [
      { value: '2024-05-01T10:00:00Z', instant: '2024-05-01T10:00:00.000Z' },
      {
        value: '2024-05-01T10:00:00.123456Z',
        instant: '2024-05-01T10:00:00.123Z',
      },
      { value: '2024-05-01T10:00Z', instant: '2024-05-01T10:00:00.000Z' },
      {
        value: '2024-05-01T12:00:00+02:00',
        instant: '2024-05-01T10:00:00.000Z',
      },
      { value: '2024-05-01T12:00:00+0200', instant: '2024-05-01T10:00:00.000Z' },
      {
        value: '2024-02-29T23:59:59Z',
        instant: '2024-02-29T23:59:59.000Z',
      },
    ];

    ACCEPTED_TIMESTAMPS.forEach(({ value, instant }) => {
      it(`accepts ${value} and decodes it to the instant it names`, () => {
        const rating = toRating({ ...RATING_WIRE, created_at: value });

        expect(rating.createdAt.toISOString()).toBe(instant);
      });
    });

    /**
     * Values that must be REFUSED, each for its own reason.
     *
     * A timestamp is the one field a naive client gets wrong silently: `new
     * Date('nonsense')` yields an `Invalid Date` rather than throwing, and that
     * object flows all the way to a screen and renders as "Invalid Date" beside a
     * real review. Every entry here is a value that would have produced one.
     */
    const REFUSED_TIMESTAMPS: ReadonlyArray<{
      readonly name: string;
      readonly value: unknown;
    }> = [
      { name: 'an empty string', value: '' },
      { name: 'whitespace only', value: '   ' },
      { name: 'null', value: null },
      { name: 'undefined', value: undefined },
      { name: 'a number of milliseconds', value: 1714557600000 },
      { name: 'a date with no time', value: '2024-05-01' },
      { name: 'a time with no offset', value: '2024-05-01T10:00:00' },
      { name: 'prose', value: 'the first of May' },
      { name: 'a 31st of February', value: '2024-02-31T10:00:00Z' },
      { name: 'a 29th of February in a common year', value: '2023-02-29T10:00:00Z' },
      { name: 'a 25th hour', value: '2024-05-01T25:00:00Z' },
      { name: 'a 60th minute', value: '2024-05-01T10:60:00Z' },
      { name: 'a 13th month', value: '2024-13-01T10:00:00Z' },
    ];

    REFUSED_TIMESTAMPS.forEach(({ name, value }) => {
      it(`refuses ${name} as a timestamp`, () => {
        // Through the mapper rather than through `toDate`, which is private: the
        // guarantee that matters is that a bad timestamp cannot become a decoded
        // rating, whichever helper enforces it.
        expect(() =>
          toRating({ ...RATING_WIRE, created_at: value as string }),
        ).toThrow(TypeError);
      });
    });

    it('names the field it refused, so a contract failure is diagnosable', () => {
      expect(() =>
        toRating({ ...RATING_WIRE, updated_at: 'nonsense' }),
      ).toThrow(/Rating\.updated_at/);
    });
  });

  describe('mapper input validation', () => {
    it('refuses an aggregate with no average key at all', () => {
      // Distinct from `average: null`, which is a user with no ratings. A missing
      // key means the server sent a different shape, and defaulting it would
      // invent a reputation.
      expect(() =>
        toRatingAggregate({ count: 0 } as unknown as RatingAggregateWire),
      ).toThrow(/RatingAggregate\.average is required/);
    });

    it('refuses an aggregate whose count and average contradict each other', () => {
      // A positive count with no average, and an average with a zero count, are
      // each impossible states of the same denormalised pair — the aggregate is
      // maintained transactionally beside the rating, so a mismatch means the two
      // writes did not commit together.
      expect(() => toRatingAggregate({ average: null, count: 3 })).toThrow();
      expect(() => toRatingAggregate({ average: 4.5, count: 0 })).toThrow();
    });

    it('refuses an average outside the rating scale', () => {
      expect(() => toRatingAggregate({ average: 5.5, count: 2 })).toThrow();
      expect(() => toRatingAggregate({ average: 0, count: 2 })).toThrow();
    });

    it('accepts a fractional average, which is what an average of whole scores is', () => {
      expect(toRatingAggregate({ average: 4.5, count: 2 })).toEqual({
        average: 4.5,
        count: 2,
      });
    });

    it('refuses a user ratings response whose items are not a list', () => {
      expect(() =>
        toUserRatingsResponse({
          items: null,
          aggregate: AGGREGATE_WIRE,
        } as unknown as UserRatingsResponseWire),
      ).toThrow(/UserRatingsResponse\.items is required/);
    });

    it('refuses a user ratings response with no aggregate', () => {
      expect(() =>
        toUserRatingsResponse({
          items: [],
        } as unknown as UserRatingsResponseWire),
      ).toThrow(/UserRatingsResponse\.aggregate is required/);
    });

    it('refuses a decision that claims eligibility without naming the counterparty', () => {
      // The server populates both only once the caller is confirmed a participant,
      // so "eligible" with no counterparty is a contradiction rather than a
      // partially filled answer.
      expect(() =>
        toEligibilityDecision({ ...ELIGIBLE_WIRE, ratee_id: null }),
      ).toThrow();
      expect(() =>
        toEligibilityDecision({ ...ELIGIBLE_WIRE, direction: null }),
      ).toThrow();
      expect(() =>
        toEligibilityDecision({ ...ELIGIBLE_WIRE, already_rated: true }),
      ).toThrow();
    });

    it('refuses a refusal that explains nothing', () => {
      expect(() =>
        toEligibilityDecision({ ...INELIGIBLE_WIRE, reason: null }),
      ).toThrow();
    });

    it('refuses a direction outside the two the domain defines', () => {
      expect(() =>
        toRating({ ...RATING_WIRE, direction: 'seller_to_seller' }),
      ).toThrow();
    });

    it('refuses a score outside the scale, and a fractional one', () => {
      // A coercing decoder would turn 4.7 into 4 and report a score the rater
      // never chose, which is why the bound is strict on both sides of the wire.
      expect(() => toRating({ ...RATING_WIRE, score: 6 })).toThrow();
      expect(() => toRating({ ...RATING_WIRE, score: 0 })).toThrow();
      expect(() => toRating({ ...RATING_WIRE, score: 4.7 })).toThrow();
    });

    it('builds a create body from three literal keys, ignoring anything else offered', () => {
      const body = toRatingCreateWire({
        transactionId: 'txn-1',
        score: 4,
        review: 'Fine',
        // The cast is the point of the case: a caller that has somehow acquired a
        // wider object must not be able to widen the request. The mapper names its
        // keys literally rather than spreading, so the extra field is dropped here
        // rather than reaching the server and drawing a 422.
      } as unknown as Parameters<typeof toRatingCreateWire>[0]);

      expect(body).toEqual({
        transaction_id: 'txn-1',
        score: 4,
        review: 'Fine',
      });
    });

    it('drops a spoofed counterparty claim instead of forwarding it', () => {
      const body = toRatingCreateWire({
        transactionId: 'txn-1',
        score: 4,
        rateeId: 'user-i-would-rather-rate',
        direction: 'seller_to_buyer',
        raterId: 'somebody-else',
      } as unknown as Parameters<typeof toRatingCreateWire>[0]);

      expect(Object.keys(body).sort()).toEqual(['score', 'transaction_id']);
    });

    it('builds a moderation body from the state and, when given, the policy reason', () => {
      expect(toModerationWire('pending')).toEqual({
        moderation_status: 'pending',
      });
      expect(toModerationWire('rejected', 'Abusive language')).toEqual({
        moderation_status: 'rejected',
        moderation_reason: 'Abusive language',
      });
    });
  });

  describe('contract failures', () => {
    it('raises a contract error, not a rating, when a 2xx response does not match', async () => {
      // The server ACCEPTED the request and answered with something else. That is a
      // different fact from a refusal and demands a different response from a
      // developer, so it is a distinct error type rather than a rejected promise
      // that looks like an HTTP failure.
      serverReturns({ ...RATING_WIRE, score: 'four' });

      await expect(
        submitRating({ transactionId: 'txn-1', score: 4 }),
      ).rejects.toBeInstanceOf(RatingContractError);
    });

    it('labels the contract failure with its endpoint and keeps the diagnosis off the message', async () => {
      serverReturns({ ...RATING_WIRE, created_at: 'nonsense' });

      const failure = await rejectionOf<RatingContractError>(() =>
        submitRating({ transactionId: 'txn-1', score: 4 }),
      );

      // The message is the one stable, user-safe sentence — a component renders it
      // verbatim, so it must not name a field or an endpoint.
      expect(failure.message).toBe(CONTRACT_ERROR_MESSAGE);
      expect(failure.message).not.toContain('created_at');
      expect(failure.message).not.toContain('/ratings');

      // The diagnosis is not discarded, which would make a real breach
      // undiagnosable — it moves to where developers read it.
      expect(failure.endpoint).toBe('POST /ratings');
      expect(failure.diagnostics).toContain('Rating.created_at');
      expect(failure.originalError).toBeInstanceOf(TypeError);
      expect(failure.name).toBe('RatingContractError');
    });

    it('writes the endpoint and diagnosis to the console rather than to the message', async () => {
      serverReturns({ ...RATING_WIRE, created_at: 'nonsense' });

      await rejectionOf<RatingContractError>(() =>
        submitRating({ transactionId: 'txn-1', score: 4 }),
      );

      const records = logsFor('Rating API response did not match the contract');
      expect(records).toHaveLength(1);
      expect(records[0][1]).toEqual({
        endpoint: 'POST /ratings',
        diagnostics: expect.stringContaining('Rating.created_at'),
      });
    });

    /**
     * Each endpoint's own label, so a contract failure names the call that broke.
     *
     * The `{userId}` and `{transactionId}` placeholders are deliberate: the label
     * is a template rather than a concrete URL, so an error report cannot leak the
     * identifier of the user or transaction being read.
     */
    const CONTRACT_ENDPOINT_LABELS: ReadonlyArray<{
      readonly label: string;
      readonly wire: unknown;
      readonly call: () => Promise<unknown>;
    }> = [
      {
        label: 'GET /ratings/user/{userId}',
        wire: { items: [{ ...RATING_WIRE, score: 9 }], aggregate: AGGREGATE_WIRE },
        call: () => fetchUserRatings('user-42'),
      },
      {
        label: 'GET /ratings/transaction/{transactionId}',
        wire: [{ ...RATING_WIRE, score: 9 }],
        call: () => fetchTransactionRatings('txn-1'),
      },
      {
        label: 'GET /ratings/eligibility/{transactionId}',
        wire: { ...ELIGIBLE_WIRE, direction: 'sideways' },
        call: () => fetchRatingEligibility('txn-1'),
      },
      {
        label: 'PATCH /ratings/{ratingId}/moderation',
        wire: { ...MODERATED_RATING_WIRE, moderation_status: 'maybe' },
        call: () => moderateRating('txn-1_user-buyer-7', 'approved'),
      },
    ];

    CONTRACT_ENDPOINT_LABELS.forEach(({ label, wire, call }) => {
      it(`labels a contract failure from ${label} with that endpoint`, async () => {
        serverReturns(wire);

        const failure = await rejectionOf<RatingContractError>(call);

        expect(failure).toBeInstanceOf(RatingContractError);
        expect(failure.endpoint).toBe(label);

        // The template, not the value: no identifier reaches the label.
        expect(failure.endpoint).not.toContain('user-42');
        expect(failure.endpoint).not.toContain('txn-1');
      });
    });

    it('refuses a transaction list that is not a list', async () => {
      serverReturns({ ratings: [] });

      await expect(fetchTransactionRatings('txn-1')).rejects.toBeInstanceOf(
        RatingContractError,
      );
    });

    it('does not convert an HTTP refusal into a contract error', async () => {
      const refusal = {
        isAxiosError: true,
        response: { status: 403, data: { detail: 'Only verified users can submit ratings' } },
        message: 'Request failed with status code 403',
      };
      serverFails(refusal);

      const failure = await rejectionOf<unknown>(() =>
        submitRating({ transactionId: 'txn-1', score: 4 }),
      );

      // The rejection propagates by IDENTITY. A 403 is a normal, expected outcome
      // carrying the server's own explanation, and wrapping it would replace that
      // explanation with the generic contract sentence — telling a user who needs
      // to verify their account that the response could not be read.
      expect(failure).toBe(refusal);
      expect(failure).not.toBeInstanceOf(RatingContractError);
    });
  });

  describe('failure reporting', () => {
    it('reads the server explanation out of a refusal', () => {
      expect(
        readServerDetail({
          isAxiosError: true,
          response: { data: { detail: 'You are not a party to this transaction' } },
        }),
      ).toBe('You are not a party to this transaction');
    });

    it('joins the issue messages when the server rejected the body before any handler ran', () => {
      // FastAPI emits `detail` in two shapes, and this is the one a naive reader
      // misses: a request Pydantic refused arrives as an ARRAY of issue objects,
      // because the backend registers no custom validation handler. Reading only
      // the string case left every such 422 showing axios's own "Request failed
      // with status code 422".
      expect(
        readServerDetail({
          isAxiosError: true,
          response: {
            data: {
              detail: [
                { loc: ['body', 'score'], msg: 'value is not a valid integer' },
                { loc: ['body', 'review'], msg: 'value is not a valid integer' },
                { loc: ['body', 'transaction_id'], msg: 'field required' },
              ],
            },
          },
        }),
      ).toBe('value is not a valid integer; field required');
    });

    it('reports nothing rather than an empty explanation', () => {
      expect(
        readServerDetail({ isAxiosError: true, response: { data: { detail: '' } } }),
      ).toBeNull();
      expect(
        readServerDetail({
          isAxiosError: true,
          response: { data: { detail: [{ loc: ['body'] }] } },
        }),
      ).toBeNull();
      expect(
        readServerDetail({
          isAxiosError: true,
          response: { data: { detail: { code: 42 } } },
        }),
      ).toBeNull();
      expect(readServerDetail({ isAxiosError: true })).toBeNull();
    });

    it('reports nothing for a value that is not an axios error at all', () => {
      // The gate is `axios.isAxiosError`, so a plain object that merely LOOKS like
      // a response carries no server explanation. Without this gate a thrown
      // domain object could put arbitrary text on screen as though the server had
      // written it.
      expect(
        readServerDetail({ response: { data: { detail: 'not from axios' } } }),
      ).toBeNull();
      expect(readServerDetail(new Error('boom'))).toBeNull();
      expect(readServerDetail(null)).toBeNull();
      expect(readServerDetail('a string')).toBeNull();
    });

    it('reduces a failed request to an allow-listed record and nothing else', () => {
      const record = describeRequestFailure({
        isAxiosError: true,
        code: 'ERR_BAD_REQUEST',
        message: 'Request failed with status code 409',
        config: {
          method: 'post',
          url: '/ratings?limit=5&after=abc',
          // The two fields that must never be logged: the interceptor has already
          // written the bearer token into `headers`, and `data` is the request body.
          headers: { Authorization: 'Bearer super-secret-token' },
          data: '{"transaction_id":"txn-1","score":4}',
        },
        response: {
          status: 409,
          data: { detail: 'You have already rated this transaction' },
        },
      });

      // Exact equality on the whole record. The function builds a FRESH object from
      // primitives by allow-list, so this assertion is what proves nothing is
      // spread: a future axios field cannot appear here without failing it.
      expect(record).toEqual({
        method: 'POST',
        url: '/ratings',
        status: 409,
        code: 'ERR_BAD_REQUEST',
        message: 'Request failed with status code 409',
        detail: 'You have already rated this transaction',
      });

      // Said again as a property of the serialised form, because that is what
      // reaches a log sink: no token, no body, no query string.
      const serialised = JSON.stringify(record);
      expect(serialised).not.toContain('super-secret-token');
      expect(serialised).not.toContain('Authorization');
      expect(serialised).not.toContain('limit=5');
      expect(serialised).not.toContain('score');
    });

    it('describes a transport failure that never reached a server', () => {
      const record = describeRequestFailure({
        isAxiosError: true,
        code: 'ERR_NETWORK',
        message: 'Network Error',
        config: { method: 'get', url: '/ratings/user/user-42' },
      });

      // No status, because there was no response; the axios code is what makes the
      // failure diagnosable instead.
      expect(record).toEqual({
        method: 'GET',
        url: '/ratings/user/user-42',
        status: null,
        code: 'ERR_NETWORK',
        message: 'Network Error',
      });
      expect('detail' in record).toBe(false);
    });

    it('describes a thrown value that is not an axios error without inventing detail', () => {
      expect(describeRequestFailure(new Error('something broke'))).toEqual({
        method: 'UNKNOWN',
        url: 'unknown',
        status: null,
        code: 'none',
        message: 'something broke',
      });

      expect(describeRequestFailure(undefined)).toEqual({
        method: 'UNKNOWN',
        url: 'unknown',
        status: null,
        code: 'none',
        message: 'Request failed',
      });
    });

    it('logs the redacted record and rejects the original error from the response interceptor', async () => {
      serverReturns(ELIGIBLE_WIRE);
      await fetchRatingEligibility('txn-1');

      const [reject] = onlyInstance().responseRejections;
      expect(reject).toBeDefined();

      const original = {
        isAxiosError: true,
        code: 'ERR_BAD_REQUEST',
        message: 'Request failed with status code 401',
        config: {
          method: 'get',
          url: '/ratings/eligibility/txn-1',
          headers: { Authorization: 'Bearer super-secret-token' },
        },
        response: { status: 401, data: { detail: 'Could not validate credentials' } },
      };

      const propagated = await rejectionOf<unknown>(() => reject(original));

      // IDENTITY. The caller needs the status and the server's `detail`, so the
      // interceptor observes and re-throws rather than wrapping or swallowing.
      expect(propagated).toBe(original);

      const records = logsFor('Rating API request failed');
      expect(records).toHaveLength(1);
      expect(records[0][1]).toEqual({
        method: 'GET',
        url: '/ratings/eligibility/txn-1',
        status: 401,
        code: 'ERR_BAD_REQUEST',
        message: 'Request failed with status code 401',
        detail: 'Could not validate credentials',
      });
      expect(JSON.stringify(records[0][1])).not.toContain('super-secret-token');
    });

    it('passes a successful response through the interceptor untouched', async () => {
      serverReturns(ELIGIBLE_WIRE);
      await fetchRatingEligibility('txn-1');

      const [passThrough] = onlyInstance().responseFulfilled;
      const response = { data: ELIGIBLE_WIRE, status: 200 };

      expect(passThrough(response)).toBe(response);

      // And nothing was logged for a call that worked.
      expect(logsFor('Rating API request failed')).toHaveLength(0);
    });
  });

  describe('the shared api module', () => {
    /**
     * `../api` re-exports four rating calls so that screens already importing that
     * module do not have to learn a second one. They must be the SAME calls: a
     * re-export that quietly built its own request, or dropped an argument, would
     * make one of two doors into this API behave differently from the other.
     */
    it('submits a rating through the same endpoint and body as the rating client', async () => {
      serverReturns(RATING_WIRE);

      const rating = await apiClient.submitRating({
        transactionId: 'txn-1',
        score: 4,
        review: 'Fine',
      });

      expect(onlyRequest()).toEqual({
        method: 'post',
        path: '/ratings',
        body: { transaction_id: 'txn-1', score: 4, review: 'Fine' },
      });
      expect(rating.id).toBe('txn-1_user-buyer-7');
    });

    it('reads a user ratings response through the public per-user path', async () => {
      serverReturns(USER_RATINGS_WIRE);

      const response = await apiClient.fetchUserRatings('user-42');

      expect(onlyRequest()).toEqual({
        method: 'get',
        path: '/ratings/user/user-42',
      });
      expect(response.aggregate).toEqual({ average: 4, count: 1 });
      expect(onlyInstance().requestInterceptors).toHaveLength(0);
    });

    it('reads a reputation aggregate through the aggregate-only path', async () => {
      serverReturns(AGGREGATE_WIRE);

      await expect(apiClient.fetchUserReputation('user-42')).resolves.toEqual({
        average: 4.5,
        count: 2,
      });
      expect(onlyRequest().path).toBe('/ratings/user/user-42/aggregate');
    });

    it('asks about eligibility through the authenticated eligibility path', async () => {
      localStorage.setItem(TOKEN_KEY, 'token-abc');
      serverReturns(ELIGIBLE_WIRE);

      const decision = await apiClient.fetchRatingEligibility('txn-1');

      expect(onlyRequest()).toEqual({
        method: 'get',
        path: '/ratings/eligibility/txn-1',
      });
      expect(decision.eligible).toBe(true);

      const [attachToken] = onlyInstance().requestInterceptors;
      expect(attachToken({ headers: {} }).headers).toEqual({
        Authorization: 'Bearer token-abc',
      });
    });
  });
});
