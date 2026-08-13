import React, { useState, useEffect } from 'react';
import { ProfileForm } from '@/components/ProfileForm';
import { ListingManagement } from '@/components/ListingManagement';
import { fetchUserProfile, updateUserProfile } from '@/services/api';
import { useSelector, selectCurrentUser } from '@/store/userSlice';
/*
 * Rating imports for F010-3, "Display of aggregate ratings on user profiles"
 * (`documentation/Software Requirements Specifications (SRS).md` L439).
 *
 * RELATIVE PATHS, DELIBERATELY. The four imports above use a `@/…` prefix that
 * resolves nowhere: `../../tsconfig.json` declares six aliases — `@components/*`,
 * `@pages/*`, `@utils/*`, `@styles/*`, `@hooks/*`, `@context/*` — every one of
 * them WITHOUT a trailing-slash-only form, and `../../vite.config.ts` mirrors the
 * same six. So `@/…` is honoured by neither the compiler nor the bundler, which
 * is why those four lines account for four of this file's pre-existing TS2307
 * errors. Repairing them belongs to a repository-wide import rewrite that is out
 * of scope here, so they are left exactly as they are — and the new lines below
 * deliberately do NOT copy the pattern.
 *
 * DEFAULT IMPORTS for both components, matching how every component in this
 * repository is exported (`ReputationBadge.tsx` L364, `RatingList.tsx` L316).
 * The named-import style on lines 2-3 above is part of the same pre-existing
 * breakage and is likewise not copied.
 *
 * `fetchUserRatings` comes from `../services/rating` rather than
 * `../services/api`: both expose rating methods, but `api.ts` imports from the
 * unresolvable specifier `app/utils/auth`, whereas `services/rating.ts` builds
 * its own axios instance with its own bearer-token interceptor precisely so this
 * surface does not depend on that broken module.
 */
import ReputationBadge from '../components/ReputationBadge';
import RatingList from '../components/RatingList';
import { fetchUserRatings } from '../services/rating';
// Type-only: `tsconfig.json` sets `isolatedModules`, so flagging these as types
// makes it unambiguous that nothing is imported here at runtime.
import type { Rating, RatingAggregate } from '../schema/rating';

const UserProfilePage: React.FC = () => {
  const [profileData, setProfileData] = useState(null);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const currentUser = useSelector(selectCurrentUser);

  /*
   * Rating state for the reputation region below. F010-3.
   *
   * Held locally rather than in `../store/ratingSlice` because nothing else on
   * this page reads it: local state keeps the change additive and avoids coupling
   * this page to `../store/index.ts`, whose named-versus-default reducer import
   * mismatch is a known, deliberately unrepaired defect.
   *
   * Declared HERE, above the two early returns further down, so the hook call
   * order is identical on every render. A hook placed after a conditional return
   * would break the Rules of Hooks, which `.eslintrc.cjs` enforces at `error`.
   *
   * `ratingAggregate` starts as `null`, and that null means exactly ONE thing:
   * the fetch has not returned yet. It does NOT also mean "never been rated" —
   * `UserRatingsResponse` declares `aggregate` non-nullable, so a successful
   * response always hands back a real `RatingAggregate` object, and a user who
   * has never been rated is represented by `{ average: null, count: 0 }` — an
   * object whose INNER `average` is null. The wrapper null and the inner null are
   * different states.
   *
   * That distinction is load-bearing rather than pedantic: because the wrapper
   * null is unambiguous, it doubles as the region's "still loading" signal, which
   * is what lets the render below withhold the empty state until the answer is
   * actually known — without needing a fourth state variable to track it.
   */
  const [ratings, setRatings] = useState<Rating[]>([]);
  const [ratingAggregate, setRatingAggregate] = useState<RatingAggregate | null>(
    null
  );
  /*
   * Failure state for the ratings fetch ALONE, and the reason it cannot reuse
   * `error` above.
   *
   * `error` drives a whole-page early return: when it is truthy the entire
   * profile — form, listing management and all — is replaced by a single error
   * div. Routing a ratings failure into it would delete the whole page because a
   * secondary panel failed, so this region owns its own failure flag and renders
   * it inside itself.
   *
   * It also exists to prevent the page telling a lie. Without it, a failed fetch
   * leaves `ratings` empty and `ratingAggregate` null, and the two children would
   * then state as fact that this user has no ratings — when the truth is that we
   * were unable to find out. "We could not load this" and "there is nothing here"
   * are different claims and are reported differently.
   */
  const [ratingsError, setRatingsError] = useState<string | null>(null);

  useEffect(() => {
    const loadUserProfile = async () => {
      try {
        const data = await fetchUserProfile(currentUser.id);
        setProfileData(data);
      } catch (err) {
        setError('Failed to load user profile');
      } finally {
        setIsLoading(false);
      }
    };

    loadUserProfile();
  }, [currentUser.id]);

  /*
   * Load the ratings this user has RECEIVED. F010-3.
   *
   * A SECOND, SEPARATE EFFECT rather than an addition to the one above, and that
   * separation is the point: the profile fetch and the ratings fetch fail
   * independently and are consumed by different parts of the page, so neither
   * should be able to take the other down. Folding them together would mean a
   * ratings outage delayed or aborted the profile the page exists to show.
   *
   * ONE REQUEST FEEDS BOTH CHILDREN. `GET /api/ratings/user/{userId}` answers with
   * `{ items, aggregate }` from a single pinned snapshot on the server, so the
   * badge's average and the list's rows cannot contradict each other. A second
   * call to `fetchUserReputation` would only re-read the same envelope.
   *
   * DELIBERATELY DOES NOT TOUCH `setError` OR `setIsLoading`. Both drive
   * whole-page early returns; see the note on `ratingsError` above. `isLoading`
   * in particular is owned by the profile fetch and is cleared in its `finally`,
   * so joining it here would gate the entire page on a secondary panel.
   *
   * Published-only semantics are the SERVER's and are left intact. Under the
   * double-blind publication model a rating stays invisible until its counterpart
   * arrives or the rating window elapses, which is what removes the incentive for
   * review extortion. A rating that legitimately has not surfaced yet is simply
   * absent from this response; there is no second unfiltered request and no
   * optimistic local insert to work around it. The empty-state copy passed to
   * `RatingList` below says so in plain words instead.
   */
  useEffect(() => {
    const loadRatings = async () => {
      try {
        const response = await fetchUserRatings(currentUser.id);
        /*
         * Assigned straight through, untouched and in the order received.
         *
         * No `.sort()`, no `.filter()`, no `.slice()`, and the average is taken
         * from `aggregate` rather than recomputed from `items`. Each of those is
         * a requirement rather than a preference:
         *
         * - The server already guarantees published-only and newest-first, the
         *   latter through the `(ratee_id, is_published, created_at DESC)`
         *   composite index. Re-sorting here would override an indexed ordering
         *   with a local one.
         * - `items` omits any rating whose review moderation rejected while
         *   `aggregate` counts every published rating, so `aggregate.count` may
         *   legitimately exceed `items.length`. Deriving the average from `items`
         *   would therefore produce a different, wrong number.
         * - Score-correlated presentation — filtering, reordering or hiding by
         *   score — is the behaviour 16 CFR Part 465 addresses. Passing the array
         *   through verbatim is what keeps this page neutral by construction.
         */
        setRatings(response.items);
        setRatingAggregate(response.aggregate);
        // Clear any earlier failure so a recovered fetch stops reporting one.
        setRatingsError(null);
      } catch (err) {
        /*
         * Contained to this region. `fetchUserRatings` rejects with an
         * `AxiosError` on a transport or 404 failure and a `RatingContractError`
         * when the envelope cannot be interpreted; neither is something this
         * page can resolve, and neither justifies destroying the profile around
         * it. Logged for diagnosis, surfaced in words, and nothing else.
         */
        console.error('Failed to load user ratings:', err);
        setRatingsError('Unable to load ratings right now.');
      }
    };

    loadRatings();
  }, [currentUser.id]);

  const handleProfileUpdate = async (updatedData: any) => {
    try {
      const result = await updateUserProfile(currentUser.id, updatedData);
      setProfileData(result);
    } catch (err) {
      setError('Failed to update profile');
    }
  };

  if (isLoading) return <div>Loading...</div>;
  if (error) return <div>{error}</div>;

  return (
    <div className="user-profile-page">
      <h1>User Profile</h1>
      {profileData && (
        <ProfileForm
          initialData={profileData}
          onSubmit={handleProfileUpdate}
        />
      )}
      {currentUser.isSeller && (
        <ListingManagement userId={currentUser.id} />
      )}
      {/*
       * Reputation region — F010-3, "Display of aggregate ratings on user
       * profiles". This replaces the unimplemented placeholder region that stood
       * here, which carried an assistance-needed marker and rendered a bare
       * heading with no content beneath it.
       *
       * `section` rather than `div`: this is a distinct, self-contained part of the
       * page with its own heading, which is exactly what `section` denotes. Paired
       * with the heading below it becomes a navigable region, so a screen-reader
       * user can jump to the ratings instead of walking the whole page to find
       * them.
       *
       * STYLING. Tailwind utilities from the default scale only —
       * `../../tailwind.config.js` leaves `theme.extend` empty, so that scale is
       * the entire token source. There is no raw hex, no pixel dimension and no
       * inline `style` here. The kebab-case class names used elsewhere on this page
       * (`user-profile-page` on the wrapper above) are deliberately not extended
       * into this region: no stylesheet in the repository defines them, so they
       * style nothing. The two idioms are left to coexist rather than reconciled —
       * restyling the existing page is not part of this change.
       */}
      <section className="mt-6">
        {/*
         * `h2`, matching the level of the heading this region replaces and sitting
         * directly under the page's single `h1` above. The level is not a visual
         * choice: it keeps the document outline h1 → h2 with no skipped level and
         * no second h1, which is what lets assistive technology present a correct
         * heading map. `text-lg font-semibold` supplies the appearance, mirroring
         * the section headings in `../components/Footer.tsx`.
         */}
        <h2 className="text-lg font-semibold mb-4">Ratings &amp; Reviews</h2>
        {ratingsError ? (
          /*
           * The honest failure state, and the reason it is not simply an empty
           * list: rendering the badge and the list here would assert "No ratings
           * yet" on the strength of a request that never succeeded, stating as
           * fact something we do not know. So the two children are replaced
           * rather than fed empty data.
           *
           * `role="status"` announces the message to assistive technology when it
           * appears, since it arrives asynchronously and nothing else on the page
           * changes to signal it. `status` is the correct politeness level — this
           * is information, not an interruption.
           */
          <p role="status" className="text-sm text-gray-500">
            {ratingsError}
          </p>
        ) : ratingAggregate === null ? (
          /*
           * STILL LOADING — and this branch is the whole reason the region does
           * not simply fall through to the badge's empty state.
           *
           * Before the request settles, `ratings` is `[]` and `ratingAggregate` is
           * null. Rendering the two children on that state would print "No ratings
           * yet" — a definite claim about this user's reputation, asserted while we
           * do not yet know it. Locally the flash is a few milliseconds and easy to
           * miss; on a slow connection it persists for seconds, and if the request
           * then FAILS the user was told "you have no ratings" for the entire wait
           * before being told the truth. That is precisely the misstatement the
           * error branch above exists to prevent, so it must not be reachable
           * through the pending path either.
           *
           * Distinguishing pending from empty needs no extra state: a successful
           * response always sets a non-null aggregate object, so a null wrapper can
           * only mean "no answer yet". See the note on the state declaration.
           *
           * `role="status"` because this text is replaced asynchronously; `status`
           * is polite, so it does not interrupt.
           */
          <p role="status" className="text-sm text-gray-500">
            Loading ratings…
          </p>
        ) : (
          <>
            {/*
             * The aggregate, passed exactly as the server reported it.
             * `ReputationBadge` owns the one-decimal "4.5/5" presentation through
             * `formatRating`, so nothing is formatted, rounded or clamped here.
             *
             * No null-coalescing is needed: `ratingAggregate` is narrowed to a real
             * object by the check above, so the values go straight through. `0` is
             * therefore never substituted for a missing average — which matters,
             * because the scale starts at 1 and a zero would render an unrated user
             * as a real, earned one-star reputation. A genuinely unrated user still
             * reaches the badge's first-class "No ratings yet" rendering, via the
             * inner `average: null` the server sends for them.
             *
             * No prop is keyed on the score — no conditional colour, no threshold,
             * no de-emphasis. A 1.0 is passed and displayed exactly as a 5.0 is.
             */}
            <ReputationBadge
              average={ratingAggregate.average}
              count={ratingAggregate.count}
            />
            <div className="mt-4">
              {/*
               * The individual ratings behind that average, rendered verbatim.
               *
               * The empty-state copy is overridden because the default, "No
               * reviews yet", is true but incomplete on a profile: under the
               * double-blind model a rating that has genuinely been submitted
               * against this user stays invisible until the counterparty submits
               * theirs or the window closes. Saying so plainly is the honest
               * disclosure of that policy — not a workaround for it.
               */}
              <RatingList
                ratings={ratings}
                emptyMessage="No reviews yet. A rating becomes visible once both parties have submitted theirs, or once the rating window closes."
              />
            </div>
          </>
        )}
      </section>
    </div>
  );
};

export default UserProfilePage;