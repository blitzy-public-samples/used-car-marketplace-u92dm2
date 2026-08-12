import { createSlice, PayloadAction } from '@reduxjs/toolkit';
import { Rating, RatingAggregate, EligibilityDecision } from '../schema/rating';

/**
 * Redux state for the bidirectional peer reputation system — the third branch of
 * the client store, joining `user` and `listing`.
 *
 * Implements the client-state half of F010 "Review and Rating System"
 * (`documentation/Software Requirements Specifications (SRS).md` L431). A buyer
 * rates the seller and the seller rates the buyer for a purchase they both took
 * part in. Three surfaces read this branch:
 *
 *   - the aggregate reputation on a user's profile (F010-3), rendered from
 *     `aggregate` with the ratings that user has received in `items`;
 *   - the seller reputation badge on the vehicle details page, rendered from
 *     `aggregate` alone;
 *   - the submission form on the transaction page, whose enabled state and
 *     displayed explanation both come from `eligibility`.
 *
 * F010-5, integrating ratings into search-result ranking, is deliberately out of
 * scope: nothing here is keyed by listing, sorted by score, or shaped for a
 * result set. This branch supplies the enabling data and nothing more.
 *
 * EVERYTHING HELD HERE IS SERIALISABLE, AND `Rating` IS NOT
 * -----------------------------------------------------------------------------
 * `Rating` (`../schema/rating`) types `createdAt`/`updatedAt` as `z.date()`, so a
 * rating that has come through `../services/rating` carries real `Date` objects.
 * A `Date` is not a plain value by Redux Toolkit's reckoning: `configureStore`
 * installs `serializableCheck` in its default middleware, and that check reports
 * every non-plain value it finds in a dispatched action AND in the resulting
 * state. Storing `Rating[]` here therefore produced a pair of development
 * warnings on every load of a profile page, naming `rating.items.0.createdAt`.
 *
 * The warnings are the symptom. The reason the rule exists is that a store whose
 * contents are not serialisable cannot be persisted, rehydrated, time-travelled or
 * transferred, and a `Date` in particular does not survive the JSON round trip
 * those all rely on — it comes back a string, so the code that read `.getTime()`
 * before rehydration reads `undefined` after it.
 *
 * So this branch stores `StoredRating`: the same rating with its two timestamps as
 * ISO 8601 strings, which is the shape the wire already uses. `toStoredRating`
 * converts at the dispatch boundary and `fromStoredRating` converts back at the
 * rendering boundary, both exported below. The alternative — relaxing
 * `serializableCheck` where the reducer is registered — was rejected: it disables
 * the check for the WHOLE store, so the next non-serialisable value anyone puts in
 * any branch would go unreported, and it would put a rule about this feature in a
 * file that belongs to all three.
 *
 * A PURE STATE CONTAINER
 * -----------------------------------------------------------------------------
 * This module holds state and does nothing else. There is no HTTP, no async, no
 * thunk, and no second reducer block responding to externally created actions.
 * `../services/rating` owns every request, the snake_case <-> camelCase
 * adaptation at the wire boundary, and the ISO-string to `Date` conversion; a
 * caller dispatches the plain actions below around its own `await`. Five
 * reducers, each one a single whole-field assignment.
 *
 * That boundary is deliberate rather than unfinished. The two sibling slices are
 * built the same way, so a reader of this folder meets one pattern instead of
 * two, and the async surface stays where the error semantics already live: the
 * service rejects with the server's own message and status intact, which is what
 * lets a form render "Only verified users can submit ratings" verbatim. A thunk
 * here would have to catch that rejection to reach `setError`, and every catch is
 * an opportunity to flatten a 403 into a generic failure.
 *
 * PUBLISHED RATINGS ONLY — THE DOUBLE-BLIND REVEAL IS THE DESIGN
 * -----------------------------------------------------------------------------
 * `items` holds published ratings only, and `aggregate` counts published ratings
 * only. A rating is created unpublished and becomes visible when the counterparty
 * submits theirs or the rating window elapses. That reveal is what strips review
 * extortion of its leverage — neither side can price their score against the
 * other's — so it is a feature requirement, not latency to be smoothed over.
 *
 * Consequently there is NO optimistic insertion, no pending-items collection and
 * no local "just submitted" flag anywhere in this state. A score the user has
 * genuinely recorded legitimately appears in neither field yet, and the honest
 * response is to tell them so; that message belongs to the submission form,
 * which learns the fact from `eligibility.alreadyRated` after re-fetching. State
 * that quietly showed the rating early would be state that disagrees with the
 * server, and the disagreement would surface as a value appearing and then
 * vanishing on the next load.
 *
 * THE SERVER IS THE AUTHORITATIVE VALIDATOR
 * -----------------------------------------------------------------------------
 * No reducer below validates, bounds, rounds or refuses anything. `score` is not
 * confined to the 1..5 scale here, `review` is not shortened to its maximum
 * length, and `aggregate.average` is never derived from `items`. The bounds are
 * declared once in `../schema/rating` (`RATING_MIN`, `RATING_MAX`,
 * `REVIEW_MAX_LENGTH`) and enforced authoritatively by Pydantic before any
 * handler logic runs; the average is computed server-side inside a Firestore
 * transaction over ratings this client may never have seen. A local arithmetic
 * pass would therefore produce a second, quietly different figure for the same
 * reputation, and a reducer that clamped a value would record a score its author
 * never chose.
 *
 * For the same reason nothing here synthesises `rateeId` or `direction`. Both are
 * derived server-side from the cited transaction and reach the client only inside
 * a whole server payload — a `Rating` or an `EligibilityDecision` — which is what
 * makes counterparty spoofing and self-rating structurally impossible rather than
 * merely validated against. No reducer accepts either value on its own.
 *
 * WHAT IS ABSENT, AND WHY
 * -----------------------------------------------------------------------------
 * There is no per-item mutator: no reducer takes a single rating and rewrites,
 * withdraws or discards it, and none indexes into the collection. Reputation
 * records are append-only. A submitted score is never rewritten — a correction is
 * a server-side moderation-state transition that records a policy basis and
 * leaves the original score and words intact — so a client reducer implying
 * otherwise would advertise a capability the API does not offer. Every write here
 * replaces a whole field from a server response.
 *
 * There are no selectors. Neither sibling slice defines one, and the alternative
 * is worse than the omission: a typed selector needs `RootState`, `RootState`
 * lives in `./index`, and `./index` imports this module, so importing it back
 * would close a value-level cycle through the file that constructs the store.
 * Consumers read this branch the way the rest of this codebase already does, with
 * an inline callback — `useSelector((state: RootState) => state.rating.aggregate)`.
 *
 * There is no state interface export. The shape is module-private, matching both
 * siblings; a consumer that needs it derives `RootState['rating']`, which cannot
 * drift from what the store actually holds. `StoredRating` and its two mappers ARE
 * exported, because they are the branch's boundary contract rather than its shape:
 * a caller has to be able to name what it converts into before dispatching and out
 * of before rendering.
 */
/**
 * One rating as this branch holds it: `Rating` with serialisable timestamps.
 *
 * Identical to `Rating` (`../schema/rating`) in every field except `createdAt` and
 * `updatedAt`, which are ISO 8601 strings here rather than `Date` objects. Derived
 * with `Omit` rather than written out, so a field added to `Rating` appears here
 * automatically and cannot be silently dropped on the way into the store.
 *
 * Exported because both boundaries need to name it: the page that dispatches a
 * server response converts into it, and the component that renders one converts
 * out of it.
 */
export type StoredRating = Omit<Rating, 'createdAt' | 'updatedAt'> & {
  createdAt: string;
  updatedAt: string;
};

/**
 * Convert one rating INTO the store's serialisable shape.
 *
 * Call this on every rating before dispatching `setRatings`, so no `Date` ever
 * reaches an action or the state:
 *
 *   dispatch(setRatings(response.items.map(toStoredRating)));
 *
 * `toISOString` always emits UTC with a `Z` suffix, which is the same instant the
 * server sent and a lossless round trip for `fromStoredRating`. It THROWS a
 * `RangeError` on an Invalid Date, so an unusable timestamp is refused here rather
 * than stored as the string "Invalid Date" and rendered as one — and it is not
 * reachable through the normal path anyway, because `z.date()` rejects an Invalid
 * Date before a `Rating` exists.
 *
 * @param rating A validated rating, as `../services/rating` returns it.
 * @returns The same rating with ISO 8601 timestamps.
 * @throws {RangeError} When either timestamp is an Invalid Date.
 */
export const toStoredRating = (rating: Rating): StoredRating => ({
  ...rating,
  createdAt: rating.createdAt.toISOString(),
  updatedAt: rating.updatedAt.toISOString(),
});

/**
 * Convert one stored rating BACK to the domain shape, for rendering.
 *
 * The inverse of `toStoredRating`, and the boundary a component reads through:
 *
 *   const items = useSelector((state: RootState) => state.rating.items);
 *   <RatingList ratings={items.map(fromStoredRating)} />
 *
 * `../components/RatingList` takes `Rating[]` because it renders a `<time>`
 * element and formats a date, so it needs the parsed value rather than the string.
 * It already guards each timestamp with `instanceof Date && !Number.isNaN(...)` and
 * omits an unusable one, which is why this function parses without refusing: a
 * value that somehow arrived unparseable degrades one rating's date rather than
 * throwing during a render and taking the whole list down with it. `Date` is the
 * only field this touches; everything else is passed through.
 *
 * @param stored A rating as held in this branch.
 * @returns The rating with `Date` timestamps.
 */
export const fromStoredRating = (stored: StoredRating): Rating => ({
  ...stored,
  createdAt: new Date(stored.createdAt),
  updatedAt: new Date(stored.updatedAt),
});

interface RatingState {
  /**
   * Published ratings currently loaded — those received by one user, or those
   * belonging to one transaction, depending on which surface last wrote here.
   *
   * Named `items` rather than `ratings` so `state.rating.items` reads without
   * stuttering, and so it matches the `items` key of the `GET
   * /api/ratings/user/{userId}` payload it is most often assigned from.
   *
   * An empty collection is the initial value and also a legitimate loaded value:
   * a user with no published ratings is indistinguishable in this field from a
   * user whose ratings have not been requested. `aggregate` is what separates
   * them — see below — and a component that must tell the two apart reads that
   * field rather than the length of this one.
   *
   * Held as `StoredRating`, so the timestamps are ISO strings and nothing in this
   * branch is unserialisable. `fromStoredRating` is how a component gets the
   * `Date`-carrying `Rating` it renders.
   */
  items: StoredRating[];
  /**
   * Aggregate reputation of the user whose ratings were last loaded, or `null`
   * when no aggregate has been loaded.
   *
   * Two distinct nulls meet in this field and neither may be collapsed into the
   * other. `aggregate === null` means "not loaded"; a loaded
   * `aggregate.average === null` means "loaded, and this user has no published
   * ratings yet". The first is a state of this client, the second is a fact about
   * the user, and `ReputationBadge` renders a genuine "No ratings yet" only for
   * the second.
   */
  aggregate: RatingAggregate | null;
  /**
   * The server's decision on whether the current caller may rate the transaction
   * currently in view, or `null` when no decision has been loaded.
   *
   * Held as the whole decision rather than as a bare boolean because the
   * accompanying `reason` is what the submission form displays when the control
   * is disabled, and because `alreadyRated` is deliberately distinct from
   * `eligible`: "you have had your say" and "you were never entitled to one" are
   * different states that a form must not conflate.
   */
  eligibility: EligibilityDecision | null;
  /**
   * Whether a rating request is in flight. Set by the caller around its own
   * `await`, matching `isLoading` in both sibling slices.
   */
  isLoading: boolean;
  /**
   * Human-readable failure text for the last rating operation, or `null` when
   * there is none.
   *
   * A `string | null`, matching both siblings, and deliberately not an `Error` or
   * an unknown: what reaches this field is the server's own `detail`, which the
   * router populates from each domain exception's message precisely so it matches
   * the `reason` an `EligibilityDecision` carries. Storing the message keeps the
   * value renderable and serialisable; storing a thrown object would put a
   * non-serialisable value in the store and invite a component to build a second,
   * competing wording for a decision the server has already worded.
   */
  error: string | null;
}

/**
 * The state a fresh store starts in, and the state a caller returns this branch
 * to by clearing it.
 *
 * `aggregate` and `eligibility` start `null` — the honest "nothing has been
 * loaded" value. Neither is seeded with a plausible-looking object, and for
 * `aggregate` that is a correctness requirement rather than a preference: a
 * seeded `{ average: 0, count: 0 }` would be indistinguishable from a real,
 * loaded reputation, so every profile and every seller badge would render a
 * five-star scale pinned at zero before its first request had even resolved. A
 * fabricated zero is the worst possible default for a reputation, because it is
 * the one value a real user can never earn.
 *
 * `items` starts as an empty array, following `listingSlice`.
 */
const initialState: RatingState = {
  items: [],
  aggregate: null,
  eligibility: null,
  isLoading: false,
  error: null,
};

const ratingSlice = createSlice({
  /**
   * Singular and lower-case, matching `'user'` and `'listing'`. This is both the
   * action-type prefix — dispatching `setRatings` produces `rating/setRatings` —
   * and the name the store's reducer map keys this branch under, which is what
   * makes the state readable as `state.rating`.
   */
  name: 'rating',
  initialState,
  reducers: {
    /**
     * Replaces the loaded ratings wholesale with a server-issued collection.
     *
     * Wholesale replacement is the only write this collection accepts, and the
     * payload is the entire array rather than one rating for the same reason: a
     * merge would have to decide which of two versions of a document wins, and
     * this client has no basis for that decision. The server's response is the
     * answer, in the server's order.
     *
     * The payload is `StoredRating[]`, NOT `Rating[]`, and the difference is
     * load-bearing rather than cosmetic: Redux Toolkit's `serializableCheck`
     * inspects the dispatched action as well as the resulting state, so accepting
     * `Rating[]` here would report a non-serialisable `Date` even if this reducer
     * converted on the way in. Callers convert first — `setRatings(items.map(
     * toStoredRating))` — which is also what makes the conversion visible at the
     * call site instead of hidden in a reducer.
     */
    setRatings: (state, action: PayloadAction<StoredRating[]>) => {
      state.items = action.payload;
    },
    /**
     * Assigns the aggregate reputation, or clears it with `null`.
     *
     * `null` is accepted so a component can return this branch to its unloaded
     * state — on unmount, or when navigating from one profile to another, so the
     * previous user's reputation is never shown beside the next user's name. A
     * separate clearing action would be a sixth reducer for something the payload
     * type already expresses.
     */
    setAggregate: (state, action: PayloadAction<RatingAggregate | null>) => {
      state.aggregate = action.payload;
    },
    /**
     * Assigns the server's eligibility decision, or clears it with `null`.
     *
     * Clearing matters more here than anywhere else in this branch: a decision is
     * scoped to exactly one transaction, so a stale one left in place would gate
     * the form on the wrong transaction — potentially offering a submission that
     * the server will refuse, or hiding one it would have allowed.
     */
    setEligibility: (state, action: PayloadAction<EligibilityDecision | null>) => {
      state.eligibility = action.payload;
    },
    /**
     * Assigns the in-flight flag. Named and typed identically in both sibling
     * slices; the actions are module-scoped, so a consumer importing from this
     * path gets this branch's action and no other.
     */
    setLoading: (state, action: PayloadAction<boolean>) => {
      state.isLoading = action.payload;
    },
    /**
     * Assigns the failure text, or clears it with `null` — which a caller does
     * before each attempt, so a resolved failure never lingers beside a
     * subsequent success.
     */
    setError: (state, action: PayloadAction<string | null>) => {
      state.error = action.payload;
    },
  },
});

export const {
  setRatings,
  setAggregate,
  setEligibility,
  setLoading,
  setError,
} = ratingSlice.actions;

/**
 * The reducer, exported under a name, and then again as the default.
 *
 * Both forms are required, and the duplication is a bridge rather than
 * indecision. `./index` imports its reducers by name — `import { userReducer }
 * from './userSlice'` — while both existing slices export theirs as a default
 * only and declare no such name, so those two imports do not resolve. Exporting
 * both forms means this branch registers correctly under either style, which is
 * what lets the store be assembled without touching two reference-only files
 * that this change has no mandate to alter.
 *
 * The name is spelled `ratingReducer` because that is the symbol the store's
 * reducer map imports. Removing either export would break a real consumer.
 */
export const ratingReducer = ratingSlice.reducer;
export default ratingSlice.reducer;
