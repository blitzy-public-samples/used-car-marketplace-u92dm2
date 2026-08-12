import React from 'react';

import { RATING_MAX } from '../schema/rating';
import { formatRating } from '../utils/formatting';

/**
 * Aggregate reputation display for the bidirectional peer rating system.
 *
 * Implements F010-3, "Display of aggregate ratings on user profiles"
 * (`documentation/Software Requirements Specifications (SRS).md` L437). It is
 * the one place in the interface that answers "how is this person regarded",
 * and it mounts in two very different contexts: a user's own profile
 * (`../pages/UserProfilePage`) and the seller block of a listing
 * (`../pages/VehicleDetailsPage`), where a prospective buyer is deciding
 * whether to transact at all.
 *
 * PURELY PRESENTATIONAL, BY DESIGN
 * -----------------------------------------------------------------------------
 * This component holds no state, runs no effect, reads no store and issues no
 * request. It renders the two numbers it is handed and nothing else. The owning
 * page fetches the aggregate — from `GET /api/ratings/user/{userId}`, or from
 * the `ratingAverage`/`ratingCount` fields already denormalised onto the user
 * document — and passes it down.
 *
 * That is a deliberate architectural choice rather than a simplification. A
 * badge that fetched for itself would issue one request per mount, and the
 * listing page mounts it beside a seller whose profile data has already been
 * loaded. Keeping it dumb also means it can be rendered from a test, a fixture
 * or a storybook with no provider, no router and no mock server.
 *
 * SENTIMENT NEUTRALITY IS A LEGAL CONSTRAINT, NOT A STYLE PREFERENCE
 * -----------------------------------------------------------------------------
 * The average is displayed EXACTLY as supplied, with the same visual weight at
 * every value. There is deliberately no colour tiering, no "warning" treatment
 * for a low score, no de-emphasis, no flattering rounding, and no threshold
 * below which the badge renders nothing or renders differently. A 1.2 average
 * is presented identically to a 4.9 — same element, same classes, same size.
 *
 * The reason is external to this codebase. The FTC Rule on the Use of Consumer
 * Reviews and Testimonials (16 CFR Part 465) prohibits suppressing reviews on
 * the basis of their rating or of negative sentiment, so the aggregate counts
 * every published rating whatever its score and this component must show what
 * it is given. Anyone tempted to add `average < 3 ? 'text-red-600' : ...` here
 * should read that rule first: score-correlated presentation is the beginning
 * of exactly the behaviour the rule addresses.
 *
 * The single guard below that can hide a value is an ABSENT-value guard, never a
 * low-value one — see the note on `hasRatings`.
 *
 * STYLING: TAILWIND'S DEFAULT SCALE IS THE ENTIRE TOKEN SOURCE
 * -----------------------------------------------------------------------------
 * Tailwind CSS is the declared design system (SRS L443), and no component
 * library of any kind is installed — there is no `Badge`, `Tag`, `Chip` or
 * `Rate` to reuse — so this is a semantic-HTML composition styled with system
 * utilities. `../../tailwind.config.js` leaves `theme.extend` empty, which makes
 * Tailwind 3.4's default scale the only token source: every colour, size,
 * weight and spacing value below is a default-scale utility, and there is no raw
 * hex, no pixel dimension and no inline `style` anywhere in this file.
 *
 * The kebab-case class names used by most sibling components (`listing-card`,
 * `message-box`) are deliberately NOT used: no stylesheet in the repository
 * defines them, so they style nothing.
 */
interface ReputationBadgeProps {
  /**
   * Mean of the published ratings this user has received, or `null` when they
   * have never been rated.
   *
   * `null` is a first-class state, not an error and not a missing value to be
   * defaulted. It mirrors `RatingAggregate.average` (`../schema/rating`) and
   * `ratingAverage` (`../schema/user`), both declared `z.number().nullable()`.
   * Coercing it to `0` would render an unrated user as a genuine, earned
   * one-star reputation, which is the single worst thing this component could
   * do, so the two cases are branched rather than merged.
   *
   * Reflects PUBLISHED ratings only. Under the double-blind publication model a
   * rating stays unpublished until its counterpart arrives or the rating window
   * elapses, so a user with a submitted-but-unpublished rating against them
   * legitimately still reports `null` here. That is correct, not stale.
   *
   * Deliberately unbounded. It is a rounded running mean the server owns and
   * validates; refusing to display a server-computed figure would turn a
   * rounding artefact into a broken profile page.
   */
  average: number | null;

  /**
   * How many published ratings the average is computed from.
   *
   * Shown alongside the score because an average is close to meaningless
   * without it — "5.0 from 1 rating" and "4.6 from 300 ratings" describe very
   * different reputations, and a reader is entitled to the denominator.
   *
   * The server keeps this consistent with `average` by construction
   * (`count === 0` if and only if `average === null`), but this component does
   * not assume that invariant holds on the wire; see `hasRatings`.
   */
  count: number;

  /**
   * Optional caption naming whose reputation this is, e.g. `"Seller rating"`.
   *
   * Renders nothing at all when omitted. No default caption is invented,
   * because the correct wording depends entirely on the surrounding page: on a
   * profile the heading already supplies the context, whereas beside a listing
   * the badge needs to say which party it describes.
   */
  label?: string;
}

/**
 * Renders a user's aggregate reputation, or an explicit empty state when they
 * have not been rated yet.
 *
 * @example
 * // Populated — renders: Seller rating ★ 4.5/5 12 ratings
 * <ReputationBadge average={4.5} count={12} label="Seller rating" />
 *
 * @example
 * // Never rated — renders: No ratings yet
 * <ReputationBadge average={null} count={0} />
 */
const ReputationBadge: React.FC<ReputationBadgeProps> = ({
  average,
  count,
  label,
}) => {
  /**
   * Whether there is a real, displayable reputation to show.
   *
   * Branched on the PROPS themselves rather than on whether `formatRating`
   * happened to return an empty string. That distinction matters: the empty
   * state is this component's own concern and its wording lives here, so
   * inferring it from another module's return value would couple the two and
   * leave the meaning of the state defined in the wrong file.
   *
   * Three conditions, each earning its place:
   *
   * - `average !== null` is the documented "never been rated" signal.
   * - `Number.isFinite(average)` catches `NaN` and `Infinity`. This is an
   *   ABSENT-VALUE guard, emphatically NOT a score threshold: `NaN` is not a
   *   low rating, it is an arithmetic accident upstream, and without this check
   *   the populated branch would render a bare `/5` with nothing in front of it.
   *   No finite value is ever excluded, however low, so sentiment neutrality is
   *   fully preserved.
   * - `count > 0` covers a zero — and, defensively, a negative — denominator.
   *   An average computed from no ratings is not a reputation.
   *
   * Because the populated branch is the only caller of `formatRating`, the
   * string `"0.0"` cannot reach the screen for an unrated user by any path.
   */
  const hasRatings = average !== null && Number.isFinite(average) && count > 0;

  /**
   * Pluralised denominator. `count === 1` is the case that reads as broken when
   * it is missed, and it is the case a brand-new user hits first.
   */
  const countLabel = `${count} ${count === 1 ? 'rating' : 'ratings'}`;

  return (
    /*
     * `span` rather than `div`, chosen for where this component is mounted.
     *
     * Both call sites are owned by other modules, and a block-level element
     * nested inside a paragraph or a heading is invalid HTML that the browser
     * silently repairs by closing the parent early — a layout bug that is hard
     * to trace back to here. An inline-level element is valid in every inline
     * and block context, so no mount point can misuse it. `inline-flex`
     * restores the flex layout that would otherwise be lost.
     *
     * `gap-1.5` spaces the parts; sibling margins are deliberately not used to
     * simulate spacing, so nothing has to be undone on the first or last child.
     */
    <span className="inline-flex items-center gap-1.5 text-sm">
      {label ? <span className="text-gray-600">{label}</span> : null}

      {hasRatings ? (
        <>
          {/*
           * Purely decorative, and hidden from assistive technology precisely
           * because it is: the score immediately after it carries the same
           * meaning as real text, so announcing "black star" first would add
           * noise rather than information. Colour is fixed for every score.
           */}
          <span aria-hidden="true" className="text-yellow-500">
            ★
          </span>

          {/*
           * The score as a single text node, in the "4.5/5" form the project's
           * own success metric states — "4.5/5 star average rating from both
           * buyers and sellers" (documentation/Software Project Proposal.md
           * L76). The denominator comes from RATING_MAX so the scale is defined
           * once, in the schema shared with the server, instead of being a 5
           * hardcoded here that would silently disagree if the scale changed.
           *
           * `whitespace-nowrap` keeps "4.5" and "/5" from being split across
           * two lines in a narrow container, which would read as two values.
           */}
          <span className="whitespace-nowrap font-semibold text-gray-900">
            {`${formatRating(average)}/${RATING_MAX}`}
          </span>

          <span className="text-gray-600">{countLabel}</span>
        </>
      ) : (
        /*
         * The empty state, stated in words. "No ratings yet" is information a
         * buyer needs — a seller nobody has rated is a different proposition
         * from one rated badly — so it is rendered plainly rather than being
         * left blank or hidden. Every user document that predates this feature
         * deserialises to this state, so it is what a fresh deployment shows
         * first, and it is a designed state rather than a fallback.
         */
        <span className="text-gray-500">No ratings yet</span>
      )}
    </span>
  );
};

export default ReputationBadge;
