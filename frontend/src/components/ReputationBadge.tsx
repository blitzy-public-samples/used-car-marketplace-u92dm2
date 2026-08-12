import React from 'react';

import { RATING_MAX, RATING_MIN } from '../schema/rating';
// Both imports are dependency-free: `../schema/rating` needs only `zod`, and
// `../utils/formatting` now formats through the platform's `Intl` rather than
// through `date-fns`, which was declared in neither the manifest nor the
// lockfile and so could not be resolved by the clean `npm ci` install CI
// performs. This component is mounted on the profile page and on the seller
// block of a listing, so an unresolvable import here failed the whole bundle.
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
   * WHAT A VALID VALUE ACTUALLY IS, per the contract both sides of the boundary
   * declare: either `null`, or a finite number within `RATING_MIN..RATING_MAX`
   * accompanied by a positive `count`. That is not this component's invention —
   * `RatingAggregate` in `../schema/rating` refuses a non-finite average, an
   * average outside the scale, and either half of the pair without the other, and
   * `RatingAggregate` in `backend/app/schema/rating.py` applies the same three
   * rules with the same wording. An earlier version of this comment claimed the
   * prop was "deliberately unbounded", which contradicted both schemas; the type
   * `number | null` is simply wider than the contract, as a TypeScript type
   * usually is.
   *
   * So `1.0` is the LOWEST value a real reputation can hold and `0` is not a
   * reputation at all — a distinction that matters, because the scale's floor is 1
   * and an average of zero can only arise from data that never came through either
   * validator. `hasRatings` treats such a value as absent rather than rendering it,
   * and that is an absent-value guard, never a score threshold: see the note there.
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
   * (`count === 0` if and only if `average === null`), and `RatingAggregateSchema`
   * refuses a payload that breaks the pair — but a prop can be passed from anywhere,
   * including a page that assembled it from two separately-read user fields, so this
   * component does not assume the invariant survived. See `hasRatings`.
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
   * Four conditions, each earning its place:
   *
   * - `average !== null` is the documented "never been rated" signal.
   * - `Number.isFinite(average)` catches `NaN` and `Infinity`. `NaN` is not a low
   *   rating, it is an arithmetic accident upstream, and without this check the
   *   populated branch would render a bare `/5` with nothing in front of it.
   * - the average lies within `RATING_MIN..RATING_MAX`, which is the range BOTH
   *   validated schemas require of it. A `0` or a `7.2` is not a reputation this
   *   system can produce; it is data that reached this component without passing
   *   either validator, and rendering it would state "0.0/5" or "7.2/5" on
   *   somebody's profile as fact.
   * - `count > 0` covers a zero — and, defensively, a negative — denominator.
   *   An average computed from no ratings is not a reputation.
   *
   * ALL FOUR ARE ABSENT-VALUE GUARDS, AND NONE IS A SCORE THRESHOLD. That
   * distinction is a compliance requirement rather than a nicety, so it is worth
   * being exact about why the range check does not violate it: the scale's floor is
   * `RATING_MIN`, so the lowest average a real rater can produce is `1.0`, and
   * `1.0` is displayed here in full — same element, same classes, same size as a
   * `5.0`. Nothing a user could earn is excluded by any of these conditions, and
   * every value inside the scale is presented identically. A condition that
   * excluded, softened or de-emphasised a value the scale CAN hold would be the
   * beginning of exactly the behaviour 16 CFR Part 465 addresses.
   *
   * Because the populated branch is the only caller of `formatRating`, the
   * string `"0.0"` cannot reach the screen for an unrated user by any path.
   */
  const hasRatings =
    average !== null &&
    Number.isFinite(average) &&
    average >= RATING_MIN &&
    average <= RATING_MAX &&
    count > 0;

  /**
   * Pluralised denominator. `count === 1` is the case that reads as broken when
   * it is missed, and it is the case a brand-new user hits first.
   */
  const countLabel = `${count} ${count === 1 ? 'rating' : 'ratings'}`;

  /**
   * The whole badge as ONE sentence, for anything that reads text rather than
   * looking at boxes.
   *
   * WHY IT EXISTS. The visible badge is three or four adjacent inline elements
   * spaced by a flex `gap`, and a gap is not text. So every consumer that
   * concatenates the subtree — a screen reader reading the run of static text, a
   * user copying the badge, a text-only export, an automated summary — saw the
   * fragments run together: `Seller rating` + `4.5/5` + `12 ratings` came out as
   * "Seller rating4.5/512 ratings", in which the score and the count have merged
   * into the unreadable number "512". The information was on screen and absent from
   * every non-visual channel.
   *
   * The fix is to say it once, properly, rather than to sprinkle separators through
   * the layout: the visual fragments are marked `aria-hidden` and this sentence is
   * rendered `sr-only` in their place, so assistive technology receives "Seller
   * rating: 4.5 out of 5 from 12 ratings" — the same facts, in an order that reads
   * aloud. "out of" and "from" are used rather than "/" because a solidus is
   * announced inconsistently ("slash", or nothing at all) and reads as a fraction.
   *
   * The label is included when there is one, because a badge beside a listing has
   * to say WHOSE reputation it describes, and that is no less true for a reader who
   * cannot see which block it sits in.
   *
   * It is deliberately NOT an `aria-label` on the wrapper. A label would override
   * the subtree for assistive technology but leave `textContent` — and therefore
   * copied text and every text-extracting tool — exactly as broken as before. Real
   * text in the DOM fixes both.
   *
   * Sentiment neutrality holds here too: the sentence is composed identically for
   * every score, so a 1.0 is announced exactly as a 5.0 is.
   */
  const spokenSummary = hasRatings
    ? `${label ? `${label}: ` : ''}${formatRating(average)} out of ${RATING_MAX} from ${countLabel}`
    : `${label ? `${label}: ` : ''}No ratings yet`;

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
      {label ? (
        /*
         * Hidden from assistive technology ONLY in the populated branch, where
         * `spokenSummary` restates it as part of one sentence. In the empty branch
         * it stays exposed, because that branch has no summary to replace it and a
         * caption is exactly what tells a reader whose reputation is missing.
         */
        <span
          aria-hidden={hasRatings ? true : undefined}
          className="text-gray-600"
        >
          {label}
        </span>
      ) : null}
      {/* Textual separation for the consumers that read the subtree rather than
          looking at it — see the note on the literal spaces below. Only in the
          populated branch: the empty branch separates with an `sr-only` ": "
          instead, and both would read "Seller rating : No ratings yet". */}
      {label && hasRatings ? ' ' : null}

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
            A LITERAL SPACE between each visual fragment, in addition to the flex
            `gap`. It is invisible and it is load-bearing: a whitespace-only text
            node between flex items is ignored for layout, so the rendered badge is
            unchanged, but the node still exists in the DOM — which means
            `textContent` reads "4.5/5 12 ratings" instead of the merged
            "4.5/512 ratings".
            That matters for the consumers `aria-hidden` cannot help: a user
            copying the badge, a text-only export, anything scraping the page.
            Assistive technology gets the `sr-only` sentence below instead of these
            fragments, so both channels are coherent by different means.
          */}
          {' '}

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
          <span
            aria-hidden="true"
            className="whitespace-nowrap font-semibold text-gray-900"
          >
            {`${formatRating(average)}/${RATING_MAX}`}
          </span>
          {' '}
          <span aria-hidden="true" className="text-gray-600">
            {countLabel}
          </span>
          {' '}

          {/*
            The same facts as one sentence, for every channel that reads text
            instead of looking at boxes — see `spokenSummary`. The three fragments
            above are `aria-hidden` so this is announced INSTEAD of them rather
            than after them, which is what keeps the badge one utterance and not
            two. `sr-only` is Tailwind's own clip-not-hide utility, so the text is
            in the accessibility tree and in `textContent` while occupying no
            space; `display: none` would remove it from both.
          */}
          <span className="sr-only">{spokenSummary}</span>
        </>
      ) : (
        <>
          {/*
            A REAL separator between the caption and the state, so this branch
            reads as a sentence in text as well as on screen. The visible spacing
            is a flex `gap`, which no text-reading consumer can see, so without
            this the subtree concatenated to "Seller ratingNo ratings yet".

            `sr-only` rather than a visible colon: it puts the punctuation in the
            accessibility tree and in `textContent` — so screen readers and copied
            text both get "Seller rating: No ratings yet" — while leaving the
            rendered badge exactly as designed. Rendered only when there is a
            caption to separate from.
          */}
          {label ? <span className="sr-only">: </span> : null}

          {/*
           * The empty state, stated in words. "No ratings yet" is information a
           * buyer needs — a seller nobody has rated is a different proposition
           * from one rated badly — so it is rendered plainly rather than being
           * left blank or hidden. Every user document that predates this feature
           * deserialises to this state, so it is what a fresh deployment shows
           * first, and it is a designed state rather than a fallback.
           *
           * It also covers a malformed aggregate — see `hasRatings` — which is the
           * honest thing to show for a figure that cannot be true: "no ratings yet"
           * understates a reputation, while "0.0/5" or "7.2/5" would assert one
           * that does not exist.
           */}
          <span className="text-gray-500">No ratings yet</span>
        </>
      )}
    </span>
  );
};

export default ReputationBadge;
