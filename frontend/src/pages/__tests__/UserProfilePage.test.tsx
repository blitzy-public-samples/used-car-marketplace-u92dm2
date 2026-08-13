/**
 * Tests for `../UserProfilePage` — the reputation region this feature added to it,
 * which is SRS F010-3 "Display of aggregate ratings on user profiles".
 *
 * WHAT IS UNDER TEST
 * -----------------------------------------------------------------------------
 * The region replaced a `HUMAN ASSISTANCE NEEDED` placeholder, and it has four
 * observable states that a screen must not confuse with one another:
 *
 *   LOADING     the request has not answered. Rendering "No reviews yet" here
 *               tells a user with a good reputation that they have none, which is
 *               the single worst thing this region can do.
 *   FAILED      the request failed. Distinct from empty for the same reason: "we
 *               could not load your ratings" and "you have no ratings" are
 *               different facts and only one of them is about the user.
 *   EMPTY       the user exists and has no PUBLISHED ratings — which under the
 *               double-blind reveal also covers a user whose ratings are submitted
 *               but withheld, so the wording has to explain that rather than imply
 *               nobody rated them.
 *   POPULATED   the aggregate and the list, both from the same single response.
 *
 * The fifth property is that ONE request feeds both children. The badge and the
 * list are two views of one response, and fetching twice would let them disagree
 * with each other.
 *
 * The profile form, the listing management block and the profile fetch itself are
 * NOT under test: they predate this feature and neither exists as a module (see
 * `../../testing/legacyChildDouble`).
 *
 * HOW THE PAGE IS MADE LOADABLE, AND WHAT THAT REVEALS
 * -----------------------------------------------------------------------------
 * Three of this page's imports cannot be satisfied as written, all pre-existing:
 *
 *   `@/components/ProfileForm` and `@/components/ListingManagement` do not exist
 *   anywhere in the repository, and Vite fails at TRANSFORM time on an
 *   unresolvable import — before any module code runs — so `vi.mock` cannot help.
 *   The Vitest-only `test.alias` block in `../../../vite.config.ts` resolves them
 *   to documented stand-ins; it is read by Vitest alone, so `tsc` and
 *   `vite build` keep reporting the defect exactly as before.
 *
 *   `@/services/api` has no `fetchUserProfile` and no `updateUserProfile` export.
 *   The page imports two functions that do not exist, so it is replaced here.
 *
 *   `@/store/userSlice` exports neither `useSelector` nor `selectCurrentUser` —
 *   the page imports a react-redux hook from a slice module, and a selector the
 *   slice never defined. It is replaced with a double that supplies exactly what
 *   the page calls, which is also why this suite needs no `<Provider>`: the page
 *   never reaches react-redux.
 *
 * Only `../../services/rating` is mocked for a normal reason — it reaches the
 * network. Both rating children are the REAL `ReputationBadge` and `RatingList`,
 * so what is asserted below is what a user would see.
 *
 * `describe`, `it`, `expect`, `beforeEach` and `afterEach` are injected globals
 * (`test.globals: true`) typed by the ambient `@types/jest` declarations. `vi` is
 * declared below.
 */

import { render, screen } from '@testing-library/react';

/** `vi` as a type only; the identifier resolves to Vitest's injected global. */
declare const vi: typeof import('vitest')['vi'];

/**
 * The user the page believes is signed in, mutable so a case can change it.
 *
 * Read by the `useSelector` double below only when the page renders, so the
 * hoisted factory never touches it before this initialiser has run.
 */
let currentUser: { id: string; isSeller: boolean } = {
  id: 'user-42',
  isSeller: false,
};

vi.mock('@/store/userSlice', () => ({
  // Faithful to what the page calls: `useSelector(selectCurrentUser)`. The
  // selector is identity over the state, and the double supplies the state.
  selectCurrentUser: (state: unknown) => state,
  useSelector: (selector: (state: unknown) => unknown) => selector(currentUser),
}));

/**
 * The replaced legacy service, created through `vi.hoisted` so the handles exist
 * before the hoisted `vi.mock` factory below runs.
 *
 * Importing the mocked module to obtain them is not an option: `@/services/api` is
 * not a specifier `tsconfig.json` declares, so `tsc` reports it as an unresolved
 * module and this file would add type errors to a baseline it must leave untouched.
 * It also states the situation honestly — the real module exports neither of these
 * functions, so there is no signature to infer from.
 */
const legacyApi = vi.hoisted(() => ({
  fetchUserProfile: vi.fn(),
  updateUserProfile: vi.fn(),
}));

vi.mock('@/services/api', () => legacyApi);

vi.mock('../../services/rating', async () => {
  const actual =
    await vi.importActual<typeof import('../../services/rating')>(
      '../../services/rating',
    );

  return {
    ...actual,
    fetchUserRatings: vi.fn(),
    fetchUserReputation: vi.fn(),
    fetchRatingEligibility: vi.fn(),
    submitRating: vi.fn(),
  };
});

import UserProfilePage from '../UserProfilePage';
import { fetchUserRatings, fetchUserReputation } from '../../services/rating';
import { RATING_MAX, RatingSchema } from '../../schema/rating';
import type { Rating, UserRatingsResponse } from '../../schema/rating';
import { formatDate } from '../../utils/formatting';

/** The handle on the replaced profile read, from the hoisted registry above. */
const mockedFetchProfile = legacyApi.fetchUserProfile;

const mockedFetchUserRatings = vi.mocked(fetchUserRatings);

/** The region's heading, which is how it is found in the accessibility tree. */
const REGION_HEADING = /ratings & reviews/i;

/** The wording the page shows while the ratings request is outstanding. */
const RATINGS_LOADING = 'Loading ratings…';

/** The wording the page shows when the ratings request failed. */
const RATINGS_FAILED = 'Unable to load ratings right now.';

/**
 * The empty-state wording this page passes to `RatingList`.
 *
 * Pinned in full because it is the page's own copy and it carries a fact the
 * default wording does not: under the double-blind reveal an empty list may mean
 * "withheld until the counterparty submits" rather than "never rated".
 */
const EMPTY_MESSAGE =
  'No reviews yet. A rating becomes visible once both parties have submitted theirs, or once the rating window closes.';

/** One received rating, parsed through the production schema. */
const rating = (overrides: Partial<Rating> = {}): Rating =>
  RatingSchema.parse({
    id: 'txn-1_user-buyer-7',
    transactionId: 'txn-1',
    vehicleListingId: 'listing-9',
    raterId: 'user-buyer-7',
    rateeId: 'user-42',
    direction: 'buyer_to_seller',
    score: 4,
    review: 'The paperwork was in order.',
    isPublished: true,
    createdAt: new Date('2024-05-01T10:00:00.000Z'),
    updatedAt: new Date('2024-05-01T10:00:00.000Z'),
    ...overrides,
  });

/** The response shape the public per-user read returns. */
const response = (
  items: Rating[],
  aggregate: UserRatingsResponse['aggregate'],
): UserRatingsResponse => ({ items, aggregate });

describe('UserProfilePage', () => {
  beforeEach(() => {
    currentUser = { id: 'user-42', isSeller: false };

    mockedFetchProfile.mockReset();
    mockedFetchUserRatings.mockReset();
    vi.mocked(fetchUserReputation).mockReset();

    mockedFetchProfile.mockResolvedValue({ firstName: 'Ada' });

    // The page logs a failed ratings read, and one case below provokes it.
    vi.spyOn(console, 'error').mockImplementation(() => undefined);
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('shows the profile wait before anything else, with no reputation on screen', () => {
    mockedFetchProfile.mockImplementation(() => new Promise(() => undefined));
    mockedFetchUserRatings.mockImplementation(
      () => new Promise<UserRatingsResponse>(() => undefined),
    );

    render(<UserProfilePage />);

    expect(screen.getByText('Loading...')).toBeInTheDocument();

    // The whole page is the profile's wait state, so the reputation region is not
    // yet rendered at all — asserted so a future change that renders the region
    // early has to state what it shows there.
    expect(screen.queryByRole('heading', { name: REGION_HEADING })).toBeNull();
  });

  it('shows the profile error instead of the page, and no reputation region', async () => {
    mockedFetchProfile.mockRejectedValue(new Error('profile unreachable'));
    mockedFetchUserRatings.mockResolvedValue(
      response([rating()], { average: 4, count: 1 }),
    );

    render(<UserProfilePage />);

    expect(await screen.findByText('Failed to load user profile')).toBeInTheDocument();

    // Pre-existing behaviour: the profile failure replaces the page. What matters
    // here is that a successfully loaded reputation is not rendered beside an error
    // that says the profile could not be read.
    expect(screen.queryByRole('heading', { name: REGION_HEADING })).toBeNull();
  });

  it('says the ratings are loading rather than saying there are none', async () => {
    mockedFetchUserRatings.mockImplementation(
      () => new Promise<UserRatingsResponse>(() => undefined),
    );

    render(<UserProfilePage />);

    // Announced through `role="status"`, so the wait is conveyed rather than merely
    // displayed. This is the state most easily got wrong: rendering the empty list
    // here would tell a well-rated user they have no reviews.
    const status = await screen.findByRole('status');
    expect(status).toHaveTextContent(RATINGS_LOADING);

    expect(screen.queryByText(EMPTY_MESSAGE)).toBeNull();
    expect(screen.queryByText(/No ratings yet/i)).toBeNull();
  });

  it('reports a failed ratings read as a failure, not as an empty reputation', async () => {
    mockedFetchUserRatings.mockRejectedValue(new Error('ratings unreachable'));

    render(<UserProfilePage />);

    const status = await screen.findByRole('status');
    expect(status).toHaveTextContent(RATINGS_FAILED);

    // Neither child is rendered: a badge reading "No ratings yet" beside a failure
    // would assert something the page does not know.
    expect(screen.queryByText(EMPTY_MESSAGE)).toBeNull();
    expect(screen.queryByText(/out of /i)).toBeNull();
    expect(screen.queryByRole('list')).toBeNull();
  });

  it('renders the aggregate and the reviews once they arrive', async () => {
    /*
     * The NEWEST rating carries the LOWER score, deliberately. The endpoint orders
     * published ratings by recency, so this is the order the list must keep — and
     * choosing scores that ascend with age is what makes a local sort observable: a
     * page that sorted by score would move the 5 to the top and this case is the
     * only thing that would notice. With the scores the other way round, a sorted
     * and an unsorted list are indistinguishable.
     */
    const first = rating({
      id: 'first',
      score: 3,
      review: 'Took a while to reply.',
      createdAt: new Date('2024-05-02T09:00:00.000Z'),
    });
    const second = rating({
      id: 'second',
      score: 5,
      review: 'Immaculate service.',
      createdAt: new Date('2024-04-28T09:00:00.000Z'),
    });

    mockedFetchUserRatings.mockResolvedValue(
      response([first, second], { average: 4, count: 2 }),
    );

    render(<UserProfilePage />);

    // The badge, asserted through the sentence assistive technology is given
    // rather than through the visual fragments beside it.
    expect(
      await screen.findByText(`4.0 out of ${RATING_MAX} from 2 ratings`),
    ).toBeInTheDocument();

    // The list, in the order the server returned — newest first, as that endpoint
    // orders it. A page that sorted locally would present a reputation the server
    // did not report.
    const items = screen.getAllByRole('listitem');
    expect(items).toHaveLength(2);
    expect(items[0]).toHaveTextContent('Took a while to reply.');
    expect(items[1]).toHaveTextContent('Immaculate service.');

    // And each carries its own score and date, so the two children agree about the
    // same response.
    expect(items[0]).toHaveTextContent(`3 out of ${RATING_MAX}`);
    expect(items[0]).toHaveTextContent(formatDate(first.createdAt));
    expect(items[1]).toHaveTextContent(`5 out of ${RATING_MAX}`);
  });

  it('distinguishes a user with no published ratings from one whose ratings failed to load', async () => {
    mockedFetchUserRatings.mockResolvedValue(
      response([], { average: null, count: 0 }),
    );

    render(<UserProfilePage />);

    // The badge's own empty state — `average: null` with `count: 0` is real data,
    // and the badge says so rather than rendering a zero-star reputation.
    expect(await screen.findByText(/No ratings yet/i)).toBeInTheDocument();

    // The list's empty state carries the page's own wording, which explains the
    // double-blind reveal. Without it a user whose rating is merely WITHHELD reads
    // "no reviews" and concludes the other party never rated them.
    expect(screen.getByText(EMPTY_MESSAGE)).toBeInTheDocument();

    // Not the failure wording, and not the loading wording.
    expect(screen.queryByText(RATINGS_FAILED)).toBeNull();
    expect(screen.queryByText(RATINGS_LOADING)).toBeNull();
  });

  it('feeds both children from one request for the signed-in user', async () => {
    mockedFetchUserRatings.mockResolvedValue(
      response([rating()], { average: 4, count: 1 }),
    );

    render(<UserProfilePage />);
    await screen.findByRole('list');

    // ONE request, for the current user's id. Two would let the badge and the list
    // describe different responses — an aggregate of five ratings above a list of
    // four — and would double the cost of every profile view.
    expect(mockedFetchUserRatings).toHaveBeenCalledTimes(1);
    expect(mockedFetchUserRatings).toHaveBeenCalledWith('user-42');

    // The per-user read is the only rating call the page makes: it does not also
    // ask the reputation-only endpoint, which would be the same request twice.
    expect(vi.mocked(fetchUserReputation)).not.toHaveBeenCalled();
  });

  it('reads the reputation of whichever user is signed in', async () => {
    currentUser = { id: 'user-99', isSeller: true };
    mockedFetchUserRatings.mockResolvedValue(
      response([], { average: null, count: 0 }),
    );

    render(<UserProfilePage />);
    await screen.findByText(/No ratings yet/i);

    // The identifier comes from the signed-in user rather than from a constant or
    // from the route, which is what makes this the viewer's own profile.
    expect(mockedFetchUserRatings).toHaveBeenCalledWith('user-99');
  });

  it('places the region under a heading of its own, one level below the page title', async () => {
    mockedFetchUserRatings.mockResolvedValue(
      response([rating()], { average: 4, count: 1 }),
    );

    render(<UserProfilePage />);
    await screen.findByRole('list');

    const headings = screen
      .getAllByRole('heading')
      .map((heading) => `${heading.tagName}:${heading.textContent ?? ''}`);

    // The region replaced a placeholder that had no heading at all, so it was not
    // findable by heading navigation. `<h2>` under the page's single `<h1>` skips
    // no level (WCAG 1.3.1).
    expect(headings).toContain('H1:User Profile');
    expect(headings).toContain('H2:Ratings & Reviews');
  });

  it('renders nobody\u2019s identifier in the reputation region', async () => {
    mockedFetchUserRatings.mockResolvedValue(
      response([rating({ raterId: 'user-buyer-7' })], { average: 4, count: 1 }),
    );

    render(<UserProfilePage />);
    await screen.findByRole('list');

    const text = document.body.textContent ?? '';

    // The response carries rater and transaction identifiers because the records
    // do, not because they are for display: they are opaque keys, and a review is
    // deliberately attributed to nobody on this page.
    expect(text).not.toContain('user-buyer-7');
    expect(text).not.toContain('txn-1');
    expect(text).not.toContain('listing-9');
  });
});
