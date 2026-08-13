import React, { useEffect, useId, useRef, useState } from 'react';
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
 * four of those whose target directory exists (`src/hooks` and `src/context` do
 * not). So `@/…` is honoured by neither the compiler nor the bundler, which
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

/**
 * Every distinguishable state the reputation region can be in.
 *
 * See the note at the `useState` call for why this is one atom rather than three
 * variables, and what each state means.
 */
type ProfileRatings =
  | { readonly status: 'unavailable' }
  | { readonly status: 'pending' }
  | { readonly status: 'failed' }
  | {
      readonly status: 'loaded';
      /*
       * `Rating[]` rather than `readonly Rating[]`: `RatingList` declares its prop
       * as a mutable array, and this page does not own that component's signature.
       * The PROPERTY is readonly, which is what stops this state being mutated in
       * place; the array is handed on exactly as the service returned it.
       */
      readonly items: Rating[];
      readonly aggregate: RatingAggregate;
    };

/**
 * Read a user identifier off the authenticated-user value, or report its absence.
 *
 * `currentUser` arrives from `useSelector(selectCurrentUser)` on line 5, and
 * BOTH of those symbols are imported from a specifier that resolves nowhere:
 * `selectCurrentUser` is not exported by `../store/userSlice` at all (that module
 * exports `setUser`, `setLoading`, `setError` and a default reducer), and
 * `useSelector` belongs to `react-redux`. Creating the missing selector is out of
 * scope, so this page cannot treat `currentUser` as guaranteed to be an object
 * with a string `id`; dereferencing it unconditionally is how a signed-out or
 * not-yet-hydrated store turns this whole region into a thrown TypeError.
 *
 * The two pre-existing dereferences elsewhere in this file are left exactly as
 * they are — they belong to the profile fetch and the update handler, which are
 * not part of this change — so this reader is used by the ratings effect alone.
 *
 * @param value The authenticated-user value, of unknown shape.
 * @returns The identifier, or `null` when there is no usable one.
 */
const readUserId = (value: unknown): string | null => {
  if (typeof value !== 'object' || value === null) {
    return null;
  }

  const { id } = value as { id?: unknown };

  return typeof id === 'string' && id.length > 0 ? id : null;
};

const UserProfilePage: React.FC = () => {
  const [profileData, setProfileData] = useState(null);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const currentUser = useSelector(selectCurrentUser);

  /*
   * The accessible name for the reputation region below. `useId` rather than a
   * literal so the value is unique even if this page is ever mounted twice, and
   * so it matches the convention the rating components already follow.
   */
  const ratingsHeadingId = useId();

  /*
   * Whose ratings to load, guarded rather than assumed. See `readUserId` for why
   * `currentUser` cannot be dereferenced here, and the ratings effect below for
   * what each outcome means. `null` is a state the region renders, not an error.
   */
  const ratingsUserId: string | null = readUserId(currentUser);

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
   * ONE STATE ATOM, FOUR NAMED STATES, rather than an array plus a nullable
   * aggregate plus an error string. Three independent variables could hold
   * combinations that describe nothing real — items from the previous user beside
   * the next user's aggregate, or a stale error beside a fresh answer — and each
   * of those combinations is a sentence this page would have said to a user. A
   * discriminated union makes every reset atomic and every render branch
   * exhaustive, so the region can only ever be in a state that is true.
   *
   * `'loaded'` is the ONLY state carrying data, and it carries BOTH halves of the
   * server's single envelope together. `GET /api/ratings/user/{userId}` answers
   * `{ items, aggregate }` from one pinned snapshot, so keeping them in one value
   * is what guarantees the badge's average and the list's rows cannot contradict
   * each other.
   *
   * The wrapper and the inner null remain different things, and the union makes
   * that explicit instead of relying on a reader noticing it. `UserRatingsResponse`
   * declares `aggregate` non-nullable, so a successful response always hands back a
   * real `RatingAggregate`; a user who has never been rated is
   * `{ average: null, count: 0 }` — an object whose INNER `average` is null, in the
   * `'loaded'` state. "Not answered yet" is `'pending'`, which is a different
   * state entirely and renders different words.
   *
   * `'failed'` exists to stop the page telling a lie, and it is why a ratings
   * failure cannot reuse the `error` variable above: `error` drives a whole-page
   * early return, so routing a ratings outage into it would delete the entire
   * profile because a secondary panel failed. Reporting the failure as an empty
   * list would be worse still — "we could not load this" and "there is nothing
   * here" are different claims about a person's reputation.
   *
   * `'unavailable'` covers a page with no identifiable user to ask about. That is
   * reachable here: `currentUser` comes from a selector that this module cannot
   * resolve, so it is not something this page may assume is an object with an id.
   */
  const [profileRatings, setProfileRatings] = useState<ProfileRatings>({
    status: 'pending',
  });

  /*
   * Invalidates in-flight ratings requests.
   *
   * Bumped when a request starts and again on cleanup, so a response that arrives
   * after the profile's user changed — or after this page unmounted — is discarded
   * rather than written into state. Without it the slower of two responses wins
   * and one person's reviews are shown on another person's profile.
   */
  const ratingsRequestRef = useRef(0);

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
   * whole-page early returns; see the note on the union above. `isLoading` in
   * particular is owned by the profile fetch and is cleared in its `finally`, so
   * joining it here would gate the entire page on a secondary panel.
   *
   * KEYED ON A GUARDED IDENTIFIER, not on `currentUser.id`. The identifier is read
   * through `readUserId`, so an absent or not-yet-hydrated user is a value this
   * effect handles rather than an exception it throws — and because the dependency
   * is that identifier, the effect re-runs when the profile's subject changes and
   * resets the region as its first act, instead of leaving one person's reviews on
   * screen underneath another person's name.
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
    /*
     * One generation per run of this effect. Every state write below is gated on
     * its own generation still being current, so a response for a previous
     * subject — or for a page that has since unmounted — is dropped.
     */
    const generation = (ratingsRequestRef.current += 1);
    const isCurrent = (): boolean => ratingsRequestRef.current === generation;

    /*
     * No identifiable user, no request, and the region says so rather than
     * showing a spinner that will never resolve or an empty state that would
     * assert this person has no ratings. It also avoids spending a round trip on
     * `/api/ratings/user/undefined` to be answered 404.
     */
    if (ratingsUserId === null) {
      if (isCurrent()) {
        setProfileRatings((current) =>
          current.status === 'unavailable' ? current : { status: 'unavailable' }
        );
      }

      return;
    }

    /*
     * A new subject starts from "not answered yet". This is the reset the region
     * previously lacked: without it, a change of user left the previous user's
     * items, aggregate and error on screen for the whole duration of the next
     * request, under the new user's name.
     */
    if (isCurrent()) {
      setProfileRatings((current) =>
        current.status === 'pending' ? current : { status: 'pending' }
      );
    }

    const loadRatings = async () => {
      try {
        const response = await fetchUserRatings(ratingsUserId);

        if (isCurrent()) {
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
           *
           * Committing both halves in ONE state write also replaces any earlier
           * failure atomically, so a recovered fetch cannot leave a stale error
           * beside fresh data.
           */
          setProfileRatings({
            status: 'loaded',
            items: response.items,
            aggregate: response.aggregate,
          });
        }
      } catch (err) {
        /*
         * Contained to this region. `fetchUserRatings` rejects with an
         * `AxiosError` on a transport or 404 failure and a `RatingContractError`
         * when the envelope cannot be interpreted; neither is something this
         * page can resolve, and neither justifies destroying the profile around
         * it. Logged for diagnosis, surfaced in words, and nothing else.
         */
        console.error('Failed to load user ratings:', err);

        if (isCurrent()) {
          setProfileRatings({ status: 'failed' });
        }
      }
    };

    loadRatings();

    return () => {
      // Invalidates whatever is in flight, so a late response cannot write state
      // after unmount or against a newer subject.
      ratingsRequestRef.current += 1;
    };
  }, [ratingsUserId]);

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
      <section className="mt-6" aria-labelledby={ratingsHeadingId}>
        {/*
         * `h2`, matching the level of the heading this region replaces and sitting
         * directly under the page's single `h1` above. The level is not a visual
         * choice: it keeps the document outline h1 → h2 with no skipped level and
         * no second h1, which is what lets assistive technology present a correct
         * heading map. `text-lg font-semibold` supplies the appearance, mirroring
         * the section headings in `../components/Footer.tsx`.
         *
         * Its `id` NAMES the section through `aria-labelledby`. A `<section>` is
         * only exposed as a landmark ("region") when it has an accessible name, so
         * without this pairing the element is announced as an anonymous group and
         * is absent from the landmark list a screen-reader user navigates by —
         * which is the one navigation aid this region was added to provide.
         */}
        <h2 id={ratingsHeadingId} className="text-lg font-semibold mb-4">
          Ratings &amp; Reviews
        </h2>
        {profileRatings.status === 'failed' ? (
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
            Unable to load ratings right now.
          </p>
        ) : profileRatings.status === 'unavailable' ? (
          /*
           * No identifiable subject to report on. Distinct from both "no answer
           * yet" and "no ratings": there is nobody to have ratings, so neither of
           * the other two sentences would be true.
           */
          <p role="status" className="text-sm text-gray-500">
            Sign in to see your ratings.
          </p>
        ) : profileRatings.status === 'pending' ? (
          /*
           * STILL LOADING — and this branch is the whole reason the region does
           * not simply fall through to the badge's empty state.
           *
           * Rendering the two children before the request settles would print "No
           * ratings yet" — a definite claim about this user's reputation, asserted
           * while we do not yet know it. Locally the flash is a few milliseconds and
           * easy to miss; on a slow connection it persists for seconds, and if the
           * request then FAILS the user was told "you have no ratings" for the
           * entire wait before being told the truth. That is precisely the
           * misstatement the failure branch above exists to prevent, so it must not
           * be reachable through the pending path either.
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
             * No null-coalescing is needed: the `'loaded'` state carries a real
             * aggregate object, so the values go straight through. `0` is therefore
             * never substituted for a missing average — which matters, because the
             * scale starts at 1 and a zero would render an unrated user as a real,
             * earned one-star reputation. A genuinely unrated user still reaches the
             * badge's first-class "No ratings yet" rendering, via the inner
             * `average: null` the server sends for them.
             *
             * No prop is keyed on the score — no conditional colour, no threshold,
             * no de-emphasis. A 1.0 is passed and displayed exactly as a 5.0 is.
             */}
            <ReputationBadge
              average={profileRatings.aggregate.average}
              count={profileRatings.aggregate.count}
            />
            <div className="mt-4">
              {/*
               * The individual ratings behind that average, rendered verbatim.
               *
               * THE EMPTY COPY IS DERIVED FROM BOTH FIGURES, because "no rows" has
               * two different meanings on this screen and the default, "No reviews
               * yet", is only ever right about one of them.
               *
               * `aggregate.count === 0` — nothing has been published against this
               * user. Either nobody has rated them, or a rating exists and the
               * double-blind model is holding it back until the counterparty submits
               * or the window closes. Both are covered by saying when a rating
               * becomes visible, which is the honest disclosure of that policy
               * rather than a workaround for it.
               *
               * `aggregate.count > 0` with no rows — ratings HAVE been published and
               * are already counted in the average beside this list, but none of them
               * arrived with a review body to show. Repeating "no reviews yet" there
               * would contradict the count the user can see immediately above, and
               * would suggest the average had been computed from nothing. So the copy
               * says the average covers every rating received and that no written
               * review is on display.
               *
               * It says that WITHOUT naming the mechanism: which records carry a
               * visible review is a moderation decision, and this page is not the
               * place to publish moderation state about a third party. Nor is the
               * copy keyed on the score in any way — the same sentence is shown for a
               * 1.0 as for a 5.0, and no rating is withheld, reordered or
               * de-emphasised here on the strength of its value.
               */}
              <RatingList
                ratings={profileRatings.items}
                emptyMessage={
                  profileRatings.aggregate.count > 0
                    ? 'No written reviews to show. The average above counts every rating this user has received.'
                    : 'No reviews yet. A rating becomes visible once both parties have submitted theirs, or once the rating window closes.'
                }
              />
            </div>
          </>
        )}
      </section>
    </div>
  );
};

export default UserProfilePage;