/**
 * Tests for `../ratingSlice` and its registration in `../index` — the Redux half
 * of the bidirectional peer reputation feature (SRS F010).
 *
 * WHAT IS ACTUALLY AT RISK HERE, AND WHY IT NEEDS ITS OWN SUITE
 * -----------------------------------------------------------------------------
 * A slice looks like the least interesting file in a feature, and it carries two
 * failures that no component test can see:
 *
 *   1. REGISTRATION. `../index` maps `rating` onto this reducer. Nothing in the
 *      application fails loudly if that key is misspelled or dropped — Redux
 *      simply has no such branch, every `useSelector((state) => state.rating…)`
 *      reads `undefined`, and the screens render their empty states as though the
 *      user genuinely had no ratings. The store is also built at module load, so
 *      a broken map is a blank application rather than a failed assertion.
 *
 *   2. SERIALISABILITY. `Rating` carries two `Date` instances, and Redux state
 *      must be serialisable — a `Date` in the store breaks time-travel debugging
 *      and persistence, and Redux Toolkit's own development middleware reports it.
 *      This slice therefore stores `StoredRating`, whose timestamps are ISO
 *      strings, and converts at the boundary. That conversion is the load-bearing
 *      part of the file and is exercised in both directions below, including the
 *      failure it deliberately raises rather than swallows.
 *
 * WHAT IS MOCKED: NOTHING
 * -----------------------------------------------------------------------------
 * No module is replaced. Reducers are pure functions of state and action, the
 * store is real, and Redux Toolkit's development middleware is left in place —
 * which is what lets one case below prove the serialisability check FIRES for a
 * `Date` payload and stays silent for a converted one. Replacing anything here
 * would leave only tautologies.
 *
 * A NOTE ON THE TWO BRANCHES THIS SUITE DOES NOT ASSERT
 * -----------------------------------------------------------------------------
 * `../index` also maps `user` and `listing`, importing both reducers by NAME
 * while `../userSlice` and `../listingSlice` export theirs only as DEFAULTS. Both
 * therefore resolve to `undefined`, Redux logs "No reducer provided for key" for
 * each, and the built store carries neither branch. That predates this feature —
 * the plan for it records the mismatch explicitly and states that the rating slice
 * exports its reducer BOTH ways so it resolves under either style, rather than
 * changing two files it does not own. So this suite asserts the rating branch and
 * says nothing about the other two: pinning their absence would enshrine a defect
 * as expected behaviour and would fail the day somebody fixes it.
 *
 * `describe`, `it`, `expect` and `beforeEach` are injected globals
 * (`test.globals: true`), typed by the ambient `@types/jest` declarations;
 * `vi` is declared below because those declarations know nothing about it.
 * `tsconfig.json` excludes every `.test.ts` file from the type program, so this
 * file is not covered by `tsc --noEmit`; it is nevertheless written to
 * type-check, and was verified against the project's own compiler options with
 * that exclusion lifted.
 */

import { store } from '../index';
import ratingReducerDefault, {
  fromStoredRating,
  ratingReducer,
  setAggregate,
  setEligibility,
  setError,
  setLoading,
  setRatings,
  toStoredRating,
} from '../ratingSlice';
import type { StoredRating } from '../ratingSlice';
import { RatingSchema } from '../../schema/rating';
import type { EligibilityDecision, Rating, RatingAggregate } from '../../schema/rating';

/** `vi` as a type only; the identifier resolves to Vitest's injected global. */
declare const vi: typeof import('vitest')['vi'];

/**
 * The state a fresh slice starts in, written out rather than imported.
 *
 * `initialState` is not exported, and asserting against a literal is the point:
 * every screen's first render reads exactly this, so a changed default is a
 * changed interface. `aggregate` and `eligibility` start as `null` rather than as
 * empty objects because "not fetched yet" and "fetched, and empty" are different
 * facts — a badge that cannot tell them apart renders "No ratings yet" while the
 * request is still in flight.
 */
const INITIAL_STATE = {
  items: [],
  aggregate: null,
  eligibility: null,
  isLoading: false,
  error: null,
};

/** One rating, parsed through the production schema so it cannot be a shape the server could not send. */
const RATING: Rating = RatingSchema.parse({
  id: 'txn-1_user-buyer-7',
  transactionId: 'txn-1',
  vehicleListingId: 'listing-9',
  raterId: 'user-buyer-7',
  rateeId: 'user-seller-42',
  direction: 'buyer_to_seller',
  score: 4,
  review: 'The paperwork was in order.',
  isPublished: true,
  createdAt: new Date('2024-05-01T10:00:00.000Z'),
  updatedAt: new Date('2024-05-02T11:30:00.000Z'),
});

const STORED_RATING: StoredRating = toStoredRating(RATING);

const AGGREGATE: RatingAggregate = { average: 4.5, count: 2 };

const ELIGIBILITY: EligibilityDecision = {
  eligible: true,
  reason: null,
  rateeId: 'user-seller-42',
  direction: 'buyer_to_seller',
  alreadyRated: false,
};

/** An action no reducer in this slice handles, used to read the initial state. */
const UNKNOWN_ACTION = { type: 'unrelated/action' };

/**
 * Every branch populated — the state a profile screen is in once its fetch has
 * settled, and the starting point for the clearing half of the isolation checks.
 */
const POPULATED_STATE = {
  items: [STORED_RATING],
  aggregate: AGGREGATE,
  eligibility: ELIGIBILITY,
  isLoading: true,
  error: 'Could not load ratings',
};

describe('ratingSlice', () => {
  describe('initial state', () => {
    it('starts empty, with nothing fetched and nothing wrong', () => {
      expect(ratingReducer(undefined, UNKNOWN_ACTION)).toEqual(INITIAL_STATE);
    });

    it('exports the same reducer as a name and as the default', () => {
      // `../index` imports it by NAME while every sibling slice exports only a
      // default. Both forms are exported so this slice resolves under either
      // style, and this is the assertion that keeps that true.
      expect(ratingReducerDefault).toBe(ratingReducer);
    });
  });

  describe('reducers', () => {
    it('replaces the item list rather than appending to it', () => {
      const withOne = ratingReducer(undefined, setRatings([STORED_RATING]));
      expect(withOne.items).toEqual([STORED_RATING]);

      const replacement: StoredRating = {
        ...STORED_RATING,
        id: 'txn-2_user-buyer-7',
      };
      const withReplacement = ratingReducer(withOne, setRatings([replacement]));

      // Each fetch answers the whole question — "the published ratings this user
      // has received" — so accumulating would show a user ratings from a profile
      // they navigated away from.
      expect(withReplacement.items).toEqual([replacement]);
    });

    it('stores an empty list as an empty list', () => {
      const emptied = ratingReducer(
        ratingReducer(undefined, setRatings([STORED_RATING])),
        setRatings([]),
      );

      // A user who has never been rated is a legitimate answer, and it has to be
      // distinguishable from a fetch that has not happened.
      expect(emptied.items).toEqual([]);
    });

    it('stores and clears the aggregate', () => {
      const withAggregate = ratingReducer(undefined, setAggregate(AGGREGATE));
      expect(withAggregate.aggregate).toEqual(AGGREGATE);

      expect(ratingReducer(withAggregate, setAggregate(null)).aggregate).toBeNull();
    });

    it('stores an aggregate for a user who has never been rated', () => {
      const unrated: RatingAggregate = { average: null, count: 0 };

      // `average: null` with `count: 0` is real data, not a missing value, so it
      // must round-trip through the store unchanged rather than being normalised
      // to `null` — which is what "not fetched" means.
      expect(ratingReducer(undefined, setAggregate(unrated)).aggregate).toEqual(
        unrated,
      );
    });

    it('stores and clears the eligibility decision', () => {
      const withDecision = ratingReducer(undefined, setEligibility(ELIGIBILITY));
      expect(withDecision.eligibility).toEqual(ELIGIBILITY);

      expect(
        ratingReducer(withDecision, setEligibility(null)).eligibility,
      ).toBeNull();
    });

    it('raises and lowers the loading flag', () => {
      const loading = ratingReducer(undefined, setLoading(true));
      expect(loading.isLoading).toBe(true);

      expect(ratingReducer(loading, setLoading(false)).isLoading).toBe(false);
    });

    it('stores and clears the error message', () => {
      const failed = ratingReducer(undefined, setError('Could not load ratings'));
      expect(failed.error).toBe('Could not load ratings');

      // Cleared with `null` rather than with `''`: an empty string is a message,
      // and a region rendering it would announce nothing at all.
      expect(ratingReducer(failed, setError(null)).error).toBeNull();
    });

    /**
     * Each action applied ALONE to the initial state, with the whole resulting
     * state compared to the initial one plus its single expected change.
     *
     * This is the assertion that catches a reducer writing outside its own branch,
     * and the cumulative case below cannot do it: applying all five in sequence
     * leaves every branch populated, so a `setError` that also raised the loading
     * flag would be indistinguishable from the `setLoading(true)` that preceded it.
     * One action at a time against exact equality has nowhere to hide.
     */
    const SINGLE_ACTIONS: ReadonlyArray<{
      readonly name: string;
      readonly action: { type: string; payload: unknown };
      readonly expected: Record<string, unknown>;
    }> = [
      {
        name: 'setRatings',
        action: setRatings([STORED_RATING]),
        expected: { items: [STORED_RATING] },
      },
      {
        name: 'setAggregate',
        action: setAggregate(AGGREGATE),
        expected: { aggregate: AGGREGATE },
      },
      {
        name: 'setEligibility',
        action: setEligibility(ELIGIBILITY),
        expected: { eligibility: ELIGIBILITY },
      },
      {
        name: 'setLoading',
        action: setLoading(true),
        expected: { isLoading: true },
      },
      {
        name: 'setError',
        action: setError('Could not load ratings'),
        expected: { error: 'Could not load ratings' },
      },
    ];

    SINGLE_ACTIONS.forEach(({ name, action, expected }) => {
      it(`applies ${name} to its own branch and to nothing else`, () => {
        expect(ratingReducer(undefined, action)).toEqual({
          ...INITIAL_STATE,
          ...expected,
        });
      });
    });

    /**
     * The same isolation check from the OPPOSITE starting point: every branch
     * populated, and each action asked to clear only its own.
     *
     * Both directions are needed and neither alone is enough. From the initial
     * state a reducer that also wrote `null` into a sibling changes nothing
     * observable, because the sibling is already `null`; from a populated state a
     * reducer that also wrote a value into a sibling can coincide with the value
     * already there. Running each action against both states leaves no such
     * coincidence available.
     */
    const CLEARING_ACTIONS: ReadonlyArray<{
      readonly name: string;
      readonly action: { type: string; payload: unknown };
      readonly expected: Record<string, unknown>;
    }> = [
      { name: 'setRatings', action: setRatings([]), expected: { items: [] } },
      {
        name: 'setAggregate',
        action: setAggregate(null),
        expected: { aggregate: null },
      },
      {
        name: 'setEligibility',
        action: setEligibility(null),
        expected: { eligibility: null },
      },
      {
        name: 'setLoading',
        action: setLoading(false),
        expected: { isLoading: false },
      },
      { name: 'setError', action: setError(null), expected: { error: null } },
    ];

    CLEARING_ACTIONS.forEach(({ name, action, expected }) => {
      it(`lets ${name} clear its own branch without disturbing the others`, () => {
        expect(ratingReducer(POPULATED_STATE, action)).toEqual({
          ...POPULATED_STATE,
          ...expected,
        });
      });
    });

    it('changes only the branch its action names', () => {
      const populated = [
        setRatings([STORED_RATING]),
        setAggregate(AGGREGATE),
        setEligibility(ELIGIBILITY),
        setLoading(true),
        setError('Could not load ratings'),
      ].reduce(ratingReducer, ratingReducer(undefined, UNKNOWN_ACTION));

      // Every branch was set by its own action and none was disturbed by another,
      // which is what makes the five reducers independently usable by a screen
      // that only cares about one of them.
      expect(populated).toEqual({
        items: [STORED_RATING],
        aggregate: AGGREGATE,
        eligibility: ELIGIBILITY,
        isLoading: true,
        error: 'Could not load ratings',
      });
    });

    it('treats the previous state as immutable', () => {
      const previous = ratingReducer(undefined, setRatings([STORED_RATING]));
      const snapshot = JSON.parse(JSON.stringify(previous)) as unknown;

      const next = ratingReducer(previous, setLoading(true));

      // Immer gives the slice mutable-looking syntax over immutable updates. If a
      // reducer ever escaped that — by mutating a payload array, say — this is
      // where it shows: `useSelector` compares by reference, so an in-place edit
      // renders nothing and the screen silently stops updating.
      expect(next).not.toBe(previous);
      expect(previous).toEqual(snapshot);
    });

    it('ignores an action it does not own', () => {
      const populated = ratingReducer(undefined, setRatings([STORED_RATING]));

      // Returned unchanged BY IDENTITY, which is what keeps every rating-connected
      // component from re-rendering on unrelated dispatches.
      expect(ratingReducer(populated, { type: 'user/setUser' })).toBe(populated);
    });
  });

  describe('action types', () => {
    it('namespaces every action under the slice name', () => {
      // The prefix is the slice's public identity: it appears in devtools, in any
      // middleware that keys off `action.type`, and in a persisted action log.
      // Renaming the slice silently renames all five, so the set is pinned.
      expect([
        setRatings.type,
        setAggregate.type,
        setEligibility.type,
        setLoading.type,
        setError.type,
      ]).toEqual([
        'rating/setRatings',
        'rating/setAggregate',
        'rating/setEligibility',
        'rating/setLoading',
        'rating/setError',
      ]);
    });
  });

  describe('timestamp serialisation', () => {
    it('stores timestamps as ISO strings, leaving nothing unserialisable in state', () => {
      const stored = toStoredRating(RATING);

      expect(stored.createdAt).toBe('2024-05-01T10:00:00.000Z');
      expect(stored.updatedAt).toBe('2024-05-02T11:30:00.000Z');

      // The property that matters, asserted structurally rather than field by
      // field: nothing in the stored shape is a `Date`, so the whole object
      // survives `JSON.stringify` and comes back identical.
      expect(JSON.parse(JSON.stringify(stored))).toEqual(stored);
      expect(
        Object.values(stored).some(
          // Typed `unknown` because `Object.values` of this shape includes `null`,
          // which `instanceof` refuses as a left-hand operand under `strict`.
          (value: unknown) => value instanceof Date,
        ),
      ).toBe(false);
    });

    it('carries every other field through untouched', () => {
      const stored = toStoredRating(RATING);

      // Only the two timestamps change. A conversion that dropped a field would
      // leave a rating that renders without its score or its review.
      expect(stored).toEqual({
        ...RATING,
        createdAt: RATING.createdAt.toISOString(),
        updatedAt: RATING.updatedAt.toISOString(),
      });
    });

    it('restores the same instants when read back out', () => {
      const restored = fromStoredRating(toStoredRating(RATING));

      expect(restored.createdAt).toBeInstanceOf(Date);
      expect(restored.updatedAt).toBeInstanceOf(Date);
      expect(restored.createdAt.getTime()).toBe(RATING.createdAt.getTime());
      expect(restored.updatedAt.getTime()).toBe(RATING.updatedAt.getTime());

      // The whole round trip, so the assertion covers the fields as well as the
      // instants: what goes into the store is what comes out of it.
      expect(restored).toEqual(RATING);
    });

    it('refuses to store a rating whose timestamp is not a real instant', () => {
      /*
       * `new Date('nonsense')` is an `Invalid Date` rather than an error, and
       * `toISOString()` on one throws `RangeError: Invalid time value`. That throw
       * is the desired behaviour and is why this case exists: the alternative is
       * storing the string "Invalid Date", which serialises happily, restores as
       * another `Invalid Date`, and renders as "Invalid Date" beside a real review
       * — a defect that surfaces three layers away from its cause.
       *
       * The fixture bypasses `RatingSchema`, which refuses an invalid `Date`
       * outright; reaching this guard therefore requires constructing the value
       * directly, and that is the point — the guard exists for a value the schema
       * would never have produced.
       */
      const unstamped: Rating = { ...RATING, createdAt: new Date('nonsense') };

      expect(() => toStoredRating(unstamped)).toThrow(RangeError);
    });

    it('reports an unparseable stored timestamp as an invalid date rather than pretending', () => {
      // The reverse direction cannot throw — `new Date('nonsense')` does not — so
      // the honest statement of the boundary is that a corrupted stored value
      // becomes an `Invalid Date` and any renderer must check for one. `RatingList`
      // does exactly that, and its own suite proves it.
      const restored = fromStoredRating({
        ...STORED_RATING,
        createdAt: 'nonsense',
      });

      expect(restored.createdAt).toBeInstanceOf(Date);
      expect(Number.isNaN(restored.createdAt.getTime())).toBe(true);
    });
  });

  describe('store registration', () => {
    it('exposes the slice under the rating key', () => {
      const state = store.getState() as Record<string, unknown>;

      // The key is what every `useSelector((state) => state.rating…)` reads. A
      // misspelling here is invisible: the selector returns `undefined`, the
      // screens render their empty states, and nothing throws.
      expect(state).toHaveProperty('rating');
      expect(state.rating).toEqual(INITIAL_STATE);
    });

    it('routes a dispatched rating action into that branch', () => {
      store.dispatch(setAggregate(AGGREGATE));

      expect(
        (store.getState() as { rating: { aggregate: RatingAggregate | null } })
          .rating.aggregate,
      ).toEqual(AGGREGATE);

      // Left as it was found, so a later test reading the initial state is not
      // affected by the order the suite happens to run in.
      store.dispatch(setAggregate(null));
      expect(
        (store.getState() as { rating: { aggregate: RatingAggregate | null } })
          .rating.aggregate,
      ).toBeNull();
    });

    it('accepts a converted rating without tripping the serialisability check', () => {
      const warnings: unknown[][] = [];
      vi.spyOn(console, 'error').mockImplementation((...args: unknown[]) => {
        warnings.push(args);
      });

      try {
        store.dispatch(setRatings([toStoredRating(RATING)]));

        // Redux Toolkit's development middleware inspects every action and every
        // resulting state for non-serialisable values and reports them through
        // `console.error`. Silence here is the proof that `StoredRating` earns its
        // keep — see the next case for the same dispatch WITHOUT the conversion.
        expect(warnings).toEqual([]);
      } finally {
        store.dispatch(setRatings([]));
        vi.restoreAllMocks();
      }
    });

    it('reports a Date-carrying payload as non-serialisable, which is why the conversion exists', () => {
      const warnings: unknown[][] = [];
      vi.spyOn(console, 'error').mockImplementation((...args: unknown[]) => {
        warnings.push(args);
      });

      try {
        // The cast is deliberate and is the whole case: it is what a screen would
        // do if it dispatched a decoded `Rating` straight from the API client. The
        // middleware catches it, which is the failure `toStoredRating` prevents —
        // and a suite that never provoked the warning could not tell whether the
        // check was enabled at all, making the silence above meaningless.
        store.dispatch(setRatings([RATING as unknown as StoredRating]));

        expect(warnings.length).toBeGreaterThan(0);
        expect(
          warnings.some((entry) =>
            entry.some(
              (part) =>
                typeof part === 'string' && part.includes('non-serializable'),
            ),
          ),
        ).toBe(true);
      } finally {
        store.dispatch(setRatings([]));
        vi.restoreAllMocks();
      }
    });
  });
});
