/**
 * Tests for `../RatingList`, the list of ratings a user has received — SRS
 * F010-3 "Display of aggregate ratings on user profiles", of which this is the
 * itemised half beside `../ReputationBadge`.
 *
 * WHAT THIS COMPONENT HAS TO GET RIGHT
 * -----------------------------------------------------------------------------
 * It renders other people's words about somebody, so the risks are not visual:
 *
 *   ORDER            the server returns published ratings newest first, and the
 *                    list must not reorder, group or sort them. A list that
 *                    silently sorted would show a reputation the server did not
 *                    report.
 *   THE SCORE AS TEXT   a star glyph is decoration; the number and its scale are
 *                    the content. A screen reader must hear "4 out of 5", not
 *                    "black star".
 *   THE DATE TWICE   once for a reader and once for a machine, because a rating
 *                    with no date reads as timeless and a `<time>` with no
 *                    `dateTime` is not machine-readable.
 *   AN UNSTAMPED RATING must not take the page down. `formatDate` throws
 *                    `RangeError` on an `Invalid Date` — it formats through
 *                    `Intl.DateTimeFormat` with no guard of its own — so the
 *                    component has to check before it formats. This is a real
 *                    reachable state: `fromStoredRating` turns a corrupted stored
 *                    timestamp into exactly such a value without complaint.
 *   THE EMPTY STATE  "no ratings yet" and "ratings withheld under the
 *                    double-blind reveal" both arrive here as an empty array, and
 *                    the caller supplies the wording that distinguishes them.
 *
 * WHAT IS MOCKED: NOTHING
 * -----------------------------------------------------------------------------
 * The real `../../utils/formatting` and the real `../../schema/rating` are used.
 * Stubbing the formatter would make every date assertion a test of the stub, and
 * the one thing worth proving about formatting is that this component DELEGATES it
 * rather than composing its own — which only holds if the real function runs.
 *
 * WHY THE VISIBLE DATE IS ASSERTED THROUGH THE PRODUCTION FORMATTER
 * -----------------------------------------------------------------------------
 * `formatDate` renders through `Intl.DateTimeFormat` in the runtime's own time
 * zone, so a literal expectation like "May 1, 2024" is correct in UTC and wrong in
 * UTC+13 — a suite that pinned it would pass here and fail on another machine for
 * a reason that has nothing to do with this component. The delegation is asserted
 * instead, and independence is kept by ALSO asserting the rendered text against a
 * long-form date shape, so a component that rendered a raw ISO string or an empty
 * string still fails. The `dateTime` attribute is pinned exactly, because that
 * value is an instant rather than a rendering and does not vary.
 *
 * `describe`, `it` and `expect` are injected globals (`test.globals: true`), typed
 * by the ambient `@types/jest` declarations, which is also what makes jest-dom's
 * matchers type-check. This file is a `.tsx`, so unlike its `.test.ts` siblings it
 * IS inside the TypeScript program and is checked by `tsc --noEmit`.
 */

import { render, screen } from '@testing-library/react';

import RatingList from '../RatingList';
import { RATING_MAX, RatingSchema } from '../../schema/rating';
import type { Rating } from '../../schema/rating';
import { formatDate } from '../../utils/formatting';

/** The default empty-state wording, pinned rather than imported. */
const DEFAULT_EMPTY_MESSAGE = 'No reviews yet';

/** A long-form date, e.g. "May 1, 2024" — the shape, not a specific instant. */
const LONG_DATE_PATTERN = /^[A-Z][a-z]+ \d{1,2}, \d{4}$/;

/**
 * One received rating, parsed through the production schema so no fixture here can
 * be a shape the server could not have sent.
 */
const rating = (overrides: Partial<Rating> = {}): Rating =>
  RatingSchema.parse({
    id: 'txn-1_user-buyer-7',
    transactionId: 'txn-1',
    vehicleListingId: 'listing-9',
    raterId: 'user-buyer-7',
    rateeId: 'user-seller-42',
    direction: 'buyer_to_seller',
    score: 4,
    review: 'The paperwork was in order and the handover was punctual.',
    isPublished: true,
    createdAt: new Date('2024-05-01T10:00:00.000Z'),
    updatedAt: new Date('2024-05-01T10:00:00.000Z'),
    ...overrides,
  });

describe('RatingList', () => {
  describe('the empty state', () => {
    it('says there is nothing to show rather than rendering an empty list', () => {
      render(<RatingList ratings={[]} />);

      expect(screen.getByText(DEFAULT_EMPTY_MESSAGE)).toBeInTheDocument();

      // No list at all, rather than a list with no items: an empty `<ul>` is
      // announced as "list, 0 items", which tells a screen-reader user that
      // something is there and that they are missing it.
      expect(screen.queryByRole('list')).toBeNull();
      expect(screen.queryAllByRole('listitem')).toHaveLength(0);
    });

    it('lets the caller say what the emptiness means', () => {
      // The caller knows which emptiness this is. Under the double-blind reveal a
      // user with submitted-but-unpublished ratings has an empty list for a
      // completely different reason from a user nobody has rated, and only the
      // screen that fetched the data can tell them apart.
      const message = 'Ratings appear once both sides have submitted theirs';

      render(<RatingList ratings={[]} emptyMessage={message} />);

      expect(screen.getByText(message)).toBeInTheDocument();
      expect(screen.queryByText(DEFAULT_EMPTY_MESSAGE)).toBeNull();
    });
  });

  describe('list semantics', () => {
    it('renders one list item per rating', () => {
      render(
        <RatingList
          ratings={[
            rating({ id: 'a' }),
            rating({ id: 'b' }),
            rating({ id: 'c' }),
          ]}
        />,
      );

      expect(screen.getByRole('list')).toBeInTheDocument();
      expect(screen.getAllByRole('listitem')).toHaveLength(3);
    });

    it('keeps the order the server returned, without sorting or grouping', () => {
      render(
        <RatingList
          ratings={[
            rating({ id: 'a', score: 2, review: 'first from the server' }),
            rating({ id: 'b', score: 5, review: 'second from the server' }),
            rating({ id: 'c', score: 3, review: 'third from the server' }),
          ]}
        />,
      );

      const items = screen.getAllByRole('listitem');

      // Verbatim. The endpoint orders published ratings by recency, so any local
      // reordering — by score, say — would present a reputation the server did not
      // report and would move a bad review off the top of the list.
      expect(items[0]).toHaveTextContent('first from the server');
      expect(items[1]).toHaveTextContent('second from the server');
      expect(items[2]).toHaveTextContent('third from the server');
    });
  });

  describe('the score', () => {
    it('states the score and the scale as text', () => {
      render(<RatingList ratings={[rating({ score: 4 })]} />);

      // Interpolated from the shared constant, so the assertion follows the scale
      // rather than pinning "out of 5" to it. Text rather than stars because a
      // count of glyphs is not something a screen reader conveys, and the scale is
      // what makes a 4 meaningful at all.
      expect(
        screen.getByText(`4 out of ${RATING_MAX}`),
      ).toBeInTheDocument();
    });

    it('hides the star glyph from assistive technology', () => {
      render(<RatingList ratings={[rating()]} />);

      // The glyph is decoration duplicating the text beside it. Left exposed it is
      // announced as "black star" before every score, which is noise a reader
      // cannot skip.
      expect(screen.getByText('★')).toHaveAttribute('aria-hidden', 'true');
    });

    it('renders each score at the boundaries of the scale', () => {
      render(
        <RatingList
          ratings={[
            rating({ id: 'low', score: 1 }),
            rating({ id: 'high', score: RATING_MAX }),
          ]}
        />,
      );

      expect(screen.getByText(`1 out of ${RATING_MAX}`)).toBeInTheDocument();
      expect(
        screen.getByText(`${RATING_MAX} out of ${RATING_MAX}`),
      ).toBeInTheDocument();
    });
  });

  describe('the date', () => {
    it('shows when the rating was submitted, formatted for a reader', () => {
      const createdAt = new Date('2024-05-01T10:00:00.000Z');

      render(<RatingList ratings={[rating({ createdAt })]} />);

      const rendered = screen.getByText(formatDate(createdAt));
      expect(rendered).toBeInTheDocument();

      // Independent of the formatter: whatever the runtime's locale and time zone,
      // the result must be a long-form date rather than an ISO string, an epoch
      // number or an empty node.
      expect(rendered.textContent).toMatch(LONG_DATE_PATTERN);
    });

    it('marks the date up as a machine-readable instant', () => {
      const createdAt = new Date('2024-05-01T10:00:00.000Z');

      render(<RatingList ratings={[rating({ createdAt })]} />);

      const element = screen.getByText(formatDate(createdAt));

      // A `<time>` carrying the exact instant, so the displayed text can stay
      // locale-dependent while the value stays unambiguous. Pinned exactly,
      // because an ISO instant does not vary with the runtime.
      expect(element.tagName).toBe('TIME');
      expect(element).toHaveAttribute('datetime', '2024-05-01T10:00:00.000Z');
    });

    it('shows the submission date rather than the last-updated date', () => {
      const createdAt = new Date('2024-05-01T10:00:00.000Z');
      const updatedAt = new Date('2024-09-09T09:09:09.000Z');

      render(<RatingList ratings={[rating({ createdAt, updatedAt })]} />);

      // `updatedAt` moves when a rating is published or moderated, neither of which
      // is when the rater wrote it — dating a review by its moderation would be
      // wrong about the only thing the date is for.
      expect(
        screen.getByText(formatDate(createdAt)),
      ).toHaveAttribute('datetime', '2024-05-01T10:00:00.000Z');
      expect(screen.queryByText(formatDate(updatedAt))).toBeNull();
    });

    it('omits the date rather than crashing when the timestamp is not a real instant', () => {
      /*
       * The fixture bypasses `RatingSchema`, which refuses an invalid `Date`. The
       * state is nevertheless reachable: `fromStoredRating` in
       * `../../store/ratingSlice` turns a corrupted stored string into an
       * `Invalid Date` without complaint, and `formatDate` throws `RangeError` on
       * one — so without the component's own guard a single bad timestamp takes
       * down the whole profile page rather than one line of it.
       */
      const unstamped: Rating = {
        ...rating(),
        createdAt: new Date('nonsense'),
      };

      render(<RatingList ratings={[unstamped]} />);

      // The rating still renders, minus the part that cannot be rendered.
      expect(screen.getByText(`4 out of ${RATING_MAX}`)).toBeInTheDocument();
      expect(screen.getByRole('listitem')).toBeInTheDocument();
      expect(document.querySelector('time')).toBeNull();
    });
  });

  describe('the review', () => {
    it('renders the review text when one was written', () => {
      const review = 'Clear paperwork, and the car matched the listing exactly.';

      render(<RatingList ratings={[rating({ review })]} />);

      expect(screen.getByText(review)).toBeInTheDocument();
    });

    it('renders a score-only rating with no empty review paragraph', () => {
      render(<RatingList ratings={[rating({ review: null })]} />);

      const item = screen.getByRole('listitem');

      // A review is optional (F010-2 is a capability, not an obligation), and an
      // empty paragraph would leave an unexplained gap under the score.
      expect(item.textContent).toContain(`4 out of ${RATING_MAX}`);
      expect(item.querySelectorAll('p')).toHaveLength(1);
    });

    it('treats a review of nothing but whitespace as no review', () => {
      // Reachable despite the server's own normalisation, because this component
      // renders whatever it is handed — from a cache, a stale store entry, or a
      // record written before that normalisation existed.
      const blank: Rating = { ...rating(), review: '   \n\t  ' };

      render(<RatingList ratings={[blank]} />);

      expect(screen.getByRole('listitem').querySelectorAll('p')).toHaveLength(1);
    });

    it('renders a mix of reviewed and score-only ratings, each on its own terms', () => {
      render(
        <RatingList
          ratings={[
            rating({ id: 'with', review: 'Prompt and straightforward.' }),
            rating({ id: 'without', review: null }),
          ]}
        />,
      );

      const [first, second] = screen.getAllByRole('listitem');

      expect(first).toHaveTextContent('Prompt and straightforward.');
      expect(second.querySelectorAll('p')).toHaveLength(1);
    });
  });

  describe('what is deliberately not rendered', () => {
    it('names nobody, and exposes no identifier from the record', () => {
      render(<RatingList ratings={[rating()]} />);

      const text = document.body.textContent ?? '';

      // The endpoint returns rater and ratee identifiers because the records carry
      // them, not because they are for display: they are opaque keys, meaningless
      // to a reader, and a rating is deliberately attributed to nobody in this
      // list. The transaction and listing references are likewise internal.
      expect(text).not.toContain('user-buyer-7');
      expect(text).not.toContain('user-seller-42');
      expect(text).not.toContain('txn-1');
      expect(text).not.toContain('listing-9');
    });

    it('says nothing about publication state, which is the server\u2019s to decide', () => {
      render(<RatingList ratings={[rating({ isPublished: true })]} />);

      const text = document.body.textContent ?? '';

      // Only published ratings reach this list — the endpoint filters them — so
      // labelling each one "published" would be noise, and labelling one
      // "withheld" would contradict the reason it is on screen at all.
      expect(text).not.toContain('published');
      expect(text).not.toContain('withheld');
    });
  });
});
