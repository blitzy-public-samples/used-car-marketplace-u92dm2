import React from 'react';

import { RATING_MAX, Rating } from '../schema/rating';
import { formatDate } from '../utils/formatting';

/**
 * The list of ratings a user has received, for the bidirectional peer
 * reputation system.
 *
 * Implements F010-2, "Written review functionality", and the per-review half of
 * F010-3, "Display of aggregate ratings on user profiles"
 * (`documentation/Software Requirements Specifications (SRS).md` L436-L437). It
 * is the reading counterpart to `./ReputationBadge`: the badge answers "how is
 * this person regarded overall", this component answers "on what evidence". The
 * two mount together in `../pages/UserProfilePage`, where the badge supplies the
 * average and this list supplies the individual scores and words behind it.
 *
 * PURELY PRESENTATIONAL, BY DESIGN
 * -----------------------------------------------------------------------------
 * This component holds no state, runs no effect, reads no store and issues no
 * request. It renders the array it is handed and nothing else. The owning page
 * fetches from `GET /api/ratings/user/{userId}` — via `fetchUserRatings` in
 * `../services/rating` — and passes `items` down.
 *
 * That is an architectural choice, not a simplification. A list that fetched for
 * itself would need the user ID, a loading state, an error state and a store
 * subscription, all of which the owning page already has; and it could then only
 * be rendered inside a `<Provider>`, so a test, a fixture or a storybook would
 * have to construct one. Handed a plain array, it renders anywhere.
 *
 * THE PUBLISHED-ONLY AND NEWEST-FIRST GUARANTEES BELONG TO THE SERVER
 * -----------------------------------------------------------------------------
 * Read this before "helpfully" adding a `.filter()` or a `.sort()` below. The
 * array is rendered EXACTLY as supplied — same items, same order — because both
 * properties a reader depends on are already established upstream:
 *
 * - PUBLISHED ONLY. Under the double-blind publication model a rating stays
 *   invisible until its counterpart arrives or the rating window elapses, which
 *   is what removes the incentive for review extortion. `GET /api/ratings/user/
 *   {userId}` returns published ratings only, so an unpublished rating never
 *   reaches this component. Re-checking `isPublished` here would place the same
 *   policy in two files that can then disagree, and the client would lose.
 * - NEWEST FIRST. The server orders by `created_at DESC` through the
 *   `(ratee_id, is_published, created_at DESC)` composite index declared in
 *   `infrastructure/firestore.indexes.json`. Re-sorting here would silently
 *   override a paged, indexed ordering with a local one.
 *
 * The same reasoning covers moderation. `review` arrives as `null` whenever the
 * server is withholding the text — because moderation has not approved it, or
 * because the rater simply wrote nothing — so the guard below tests the VALUE it
 * was given and never inspects `moderationStatus`. A client-side
 * `moderationStatus === 'approved'` test would duplicate a policy decision the
 * server has already made and applied.
 *
 * SENTIMENT NEUTRALITY IS A LEGAL CONSTRAINT, NOT A STYLE PREFERENCE
 * -----------------------------------------------------------------------------
 * Every rating in the array is rendered, in the order supplied, with IDENTICAL
 * visual treatment at every score. There is deliberately no filtering by score,
 * no reordering by score, no colour tiering, no "warning" styling, no
 * de-emphasis, no differential truncation or collapsing, and no threshold below
 * which an item renders differently or not at all. A one-star review is the same
 * element with the same classes as a five-star review.
 *
 * The reason is external to this codebase. The FTC Rule on the Use of Consumer
 * Reviews and Testimonials (16 CFR Part 465) prohibits suppressing reviews on
 * the basis of their rating or of negative sentiment. Anyone tempted to add
 * `rating.score < 3 ? 'text-red-600' : ...`, or to slice off the low scores,
 * should read that rule first: score-correlated presentation is where exactly
 * the behaviour it addresses begins.
 *
 * REPUTATION IS APPEND-ONLY, SO THERE ARE NO ITEM CONTROLS
 * -----------------------------------------------------------------------------
 * No item offers an edit, delete, hide, report or overflow affordance, and that
 * is deliberate rather than unfinished. A submitted score is never rewritten: a
 * correction is a server-side moderation-state transition that records why,
 * performed through `PATCH /api/ratings/{ratingId}/moderation` by an
 * administrator. That is also why `../store/ratingSlice` exposes no per-item
 * mutator and `../schema/rating` declares no `RatingUpdate` shape — there is no
 * client path by which a rating can be altered, so offering a control that
 * implied one would be a lie.
 *
 * NOTHING HERE UN-ESCAPES USER-AUTHORED TEXT
 * -----------------------------------------------------------------------------
 * `review` is free text written by a member of the public. It is rendered as a
 * JSX child, so React escapes it automatically. That escaping is the protection
 * and it must never be bypassed: no raw-HTML injection prop appears anywhere in
 * this file, and introducing one would reopen precisely the XSS exposure the
 * project guards against (see the input validation and XSS requirements at
 * `documentation/Technical Specifications.md` §7.3.2, which name DOMPurify). No
 * `sanitizeUserInput` call is needed on this path: sanitisation belongs to the
 * write path in `./RatingSubmissionForm`, and the server independently
 * normalises what it stores. The obligation here is narrower and absolute —
 * never un-escape.
 *
 * STYLING: TAILWIND'S DEFAULT SCALE IS THE ENTIRE TOKEN SOURCE
 * -----------------------------------------------------------------------------
 * Tailwind CSS is the declared design system (SRS L443), and no component
 * library of any kind is installed — there is no `List`, `Table`, `Card` or
 * `Timeline` to reuse — so this is a semantic-HTML composition styled with
 * system utilities. `../../tailwind.config.js` leaves `theme.extend` empty,
 * which makes Tailwind 3.4's default scale the only token source: every colour,
 * size, weight and spacing value below is a default-scale utility, and there is
 * no raw hex, no pixel dimension and no inline `style` anywhere in this file.
 *
 * The kebab-case class names used by most sibling components (`listing-card`,
 * `message-box`) are deliberately NOT used: no stylesheet in the repository
 * defines them, so they style nothing.
 */
interface RatingListProps {
  /**
   * The ratings to render, in the order they should appear.
   *
   * Expected to be the `items` array of `UserRatingsResponse`
   * (`../schema/rating`) — the ratings a user has RECEIVED. Rendered verbatim;
   * see the note on server guarantees above for why this component neither
   * filters nor sorts.
   *
   * An empty array is a normal, first-class state rather than a missing value:
   * every user starts with no ratings, and a user whose only rating is still
   * unpublished legitimately receives an empty array here. It renders
   * `emptyMessage`.
   */
  ratings: Rating[];

  /**
   * Copy shown in place of the list when `ratings` is empty.
   *
   * Overridable because the right wording depends on the surrounding page — a
   * profile says one thing about its owner, a listing says another about its
   * seller — while the default suits the common case. It is deliberately a
   * caller-supplied string rather than a rendered node, so no markup can be
   * injected through this prop.
   */
  emptyMessage?: string;
}

/**
 * Renders the ratings a user has received as a semantic list of score, date and
 * review text, or an explicit empty state when there are none.
 *
 * @example
 * // Populated — one item per rating, newest first as supplied by the server
 * <RatingList ratings={response.items} />
 *
 * @example
 * // Empty, with page-appropriate wording
 * <RatingList ratings={[]} emptyMessage="This seller has no reviews yet" />
 */
const RatingList: React.FC<RatingListProps> = ({
  ratings,
  emptyMessage = 'No reviews yet',
}) => {
  /*
   * The empty state, stated in words and returned early.
   *
   * Rendered as a real line of text rather than as an empty `<ul>` or as
   * nothing at all, because "nobody has reviewed this person" is information a
   * reader needs — a seller nobody has rated is a different proposition from
   * one rated badly — and an element with no children reads to assistive
   * technology as an empty list rather than as an answer. It is a designed
   * state, and it is what every profile shows before its first rating is
   * published.
   */
  if (ratings.length === 0) {
    return <p className="text-sm text-gray-500">{emptyMessage}</p>;
  }

  return (
    /*
     * `role="list"` is redundant in principle and load-bearing in practice.
     *
     * Tailwind's Preflight (`@tailwind base` in `../styles/index.css`) resets
     * `list-style: none` on every `ul`, and Safari with VoiceOver responds by
     * dropping the implicit list role — so the list semantics, and the "list of
     * N items" announcement that comes with them, are lost exactly where this
     * markup would otherwise have supplied them for free. Restating the role
     * costs nothing visually and restores it.
     *
     * `divide-y` draws the separators as `> * + *`, so no rule is ever painted
     * after the last item and nothing has to be undone at the edges. Spacing
     * lives on the items rather than as margins between siblings.
     */
    <ul role="list" className="divide-y divide-gray-200">
      {/*
       * Rendered with `.map` over the array exactly as received: no `.filter`,
       * no `.sort`, no `.slice`. The published-only and newest-first guarantees
       * are the server's — see the module note above before changing this.
       */}
      {ratings.map((rating) => {
        /*
         * Whether there is review text to show.
         *
         * `review` is `string | null` (`../schema/rating`), null meaning the
         * rater wrote nothing or the server is withholding the text pending
         * moderation. The `typeof` test narrows away null without a non-null
         * assertion, which `strict` mode would otherwise require, and the trim
         * test additionally rejects text that is only whitespace so a blank
         * paragraph is never emitted into the layout.
         *
         * This is an ABSENT-VALUE guard, emphatically not a score threshold:
         * the score, date and separator of a rating with no words render
         * exactly as they do for every other rating, and a score with no review
         * is a fully supported submission rather than an incomplete one.
         */
        const hasReview =
          typeof rating.review === 'string' && rating.review.trim().length > 0;

        /*
         * Whether the timestamp can be rendered.
         *
         * `createdAt` is typed `Date` and `../services/rating` guarantees a
         * real one — it converts the wire's ISO string and raises rather than
         * substituting a placeholder. This guard exists for every OTHER caller:
         * a fixture, or a store rehydrated from JSON, can hand over an Invalid
         * Date, and both `toISOString()` and `formatDate` throw on one. Without
         * the check, a single malformed timestamp would take down the entire
         * profile page instead of one date.
         *
         * Also an absent-value guard, not a policy filter. When it fails the
         * score and review still render, so no rating is lost, and no date is
         * shown rather than the literal text "Invalid Date".
         */
        const submittedAt = rating.createdAt;
        const hasTimestamp =
          submittedAt instanceof Date && !Number.isNaN(submittedAt.getTime());

        return (
          <li key={rating.id} className="py-4 first:pt-0 last:pb-0">
            {/*
             * Score and date on one row. `flex-wrap` with separate axis gaps
             * lets the date drop below the score in a narrow column instead of
             * being squeezed, which is the responsive behaviour the design
             * needs without inventing a breakpoint for it.
             */}
            <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
              <p className="flex items-baseline gap-1.5 text-sm">
                {/*
                 * Purely decorative, and hidden from assistive technology
                 * precisely because it is: the score immediately after it
                 * carries the same meaning as real text, so announcing "black
                 * star" first would add noise rather than information. One
                 * glyph rather than a filled-and-empty row of five, matching
                 * `./ReputationBadge`, which also keeps the visual treatment
                 * structurally identical at every score. Its colour is fixed
                 * and never varies with the value.
                 */}
                <span aria-hidden="true" className="text-yellow-500">
                  ★
                </span>

                {/*
                 * The score as ordinary text, which is what makes it
                 * independent of colour and shape and therefore perceivable to
                 * a screen-reader user and to anyone who cannot distinguish the
                 * glyph. "4 out of 5" is read aloud correctly, where a bare "4"
                 * or a "4/5" is ambiguous.
                 *
                 * The maximum comes from `RATING_MAX` so the scale is defined
                 * once, in the schema shared with the server, instead of being
                 * a 5 hardcoded here that would silently disagree if the scale
                 * ever changed.
                 */}
                <span className="font-semibold text-gray-900">
                  {`${rating.score} out of ${RATING_MAX}`}
                </span>
              </p>

              {hasTimestamp ? (
                /*
                 * `<time>` carries the machine-readable value in `dateTime`
                 * while the text content stays the human-readable string from
                 * the project's shared `formatDate`. That helper is the only
                 * sanctioned route to a formatted date here — it owns the
                 * project's single date-formatting dependency, so importing
                 * that library directly from this file would add an
                 * undeclared-package error the shared helper already absorbs.
                 */
                <time
                  dateTime={submittedAt.toISOString()}
                  className="text-sm text-gray-500"
                >
                  {formatDate(submittedAt)}
                </time>
              ) : null}
            </div>

            {hasReview ? (
              /*
               * The review, as a JSX child so React escapes it. Raw HTML is
               * never injected here; see the module note above.
               *
               * `whitespace-pre-line` keeps the author's paragraph breaks,
               * which the server's normalisation preserves as single newlines,
               * and `break-words` stops an unbroken run — a pasted URL, or a
               * long word — from widening the container past its bounds. Text
               * is shown in full: no line clamp and no "read more", since
               * truncating a review is a display decision that would land
               * hardest on the longest and most detailed ones.
               */
              <p className="mt-2 whitespace-pre-line break-words text-base text-gray-700">
                {rating.review}
              </p>
            ) : null}
          </li>
        );
      })}
    </ul>
  );
};

export default RatingList;
