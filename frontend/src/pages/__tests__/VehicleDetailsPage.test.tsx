/**
 * Tests for `../VehicleDetailsPage` — the "Seller Information" region this feature
 * added, which is the counterparty half of SRS F010-3 and the point at which a
 * buyer's decision is actually made.
 *
 * WHAT IS UNDER TEST
 * -----------------------------------------------------------------------------
 * The region shows the SELLER's reputation on the page where a buyer decides
 * whether to transact with them, so four properties matter:
 *
 *   WHOSE REPUTATION. It is read for the seller named by the LOADED LISTING. The
 *   page has a listing id from the route and a seller id from the document, and
 *   asking about the wrong one would put a stranger's reputation beside this car.
 *
 *   WHEN IT IS READ. Only once a seller is known. The listing arrives first, so the
 *   read is a second effect keyed on the seller id, and a listing without one must
 *   produce no request at all rather than a request for `undefined`.
 *
 *   WHAT IS SHOWN BEFORE IT ARRIVES. `average: null` with `count: 0`, which is the
 *   badge's first-class "No ratings yet" state. `0` would be a lie of a different
 *   kind: the scale's floor is 1, so a zero average states an earned reputation
 *   nobody has.
 *
 *   WHAT HAPPENS WHEN IT FAILS. The listing still renders. A reputation is
 *   supporting information, and losing it must not cost the buyer the car.
 *
 * Nothing keys on the VALUE of the aggregate — no threshold, no colour tier, no
 * hiding of a low score — because the aggregate counts every published rating
 * whatever it says. One case below pins that: a 1.2 renders exactly as a 4.9 does.
 *
 * HOW THE PAGE IS MADE LOADABLE
 * -----------------------------------------------------------------------------
 * Five of this page's children cannot be loaded as written, all pre-existing:
 * `PhotoGallery`, `VehicleSpecs` and `MaintenanceHistory` do not exist anywhere,
 * while `MessageBox` and `PaymentForm` do exist but are imported BY NAME while
 * exporting only defaults — and `PaymentForm` additionally imports
 * `@stripe/react-stripe-js`, which is not a declared dependency. Vite fails at
 * TRANSFORM time on the unresolvable ones, before any module code runs, so
 * `vi.mock` cannot substitute for them; the Vitest-only `test.alias` block in
 * `../../../vite.config.ts` resolves all five to documented stand-ins and is read
 * by Vitest alone, so `tsc` and `vite build` keep reporting the defect as before.
 *
 * `@/services/api` is replaced because the real module has no `fetchListingDetails`
 * export — the page imports a function that does not exist. `react-router-dom` has
 * only `useParams` replaced, over the real module. `../../services/rating` is
 * mocked for the ordinary reason: it reaches the network. The badge itself is the
 * REAL `ReputationBadge`, so what is asserted below is what a buyer would read.
 *
 * `describe`, `it`, `expect`, `beforeEach` and `afterEach` are injected globals
 * (`test.globals: true`) typed by the ambient `@types/jest` declarations. `vi` is
 * declared below.
 */

import { render, screen, waitFor } from '@testing-library/react';

/** `vi` as a type only; the identifier resolves to Vitest's injected global. */
declare const vi: typeof import('vitest')['vi'];

/** The route parameters the page reads, mutable so a case can state its own. */
let routeParams: Record<string, string | undefined> = {};

vi.mock('react-router-dom', async () => {
  const actual =
    await vi.importActual<typeof import('react-router-dom')>('react-router-dom');

  return { ...actual, useParams: () => routeParams };
});

/**
 * The replaced legacy service, created through `vi.hoisted` so the handle exists
 * before the hoisted `vi.mock` factory below runs.
 *
 * Importing the mocked module to obtain it is not an option: `@/services/api` is
 * not a specifier `tsconfig.json` declares, so `tsc` would report it as an
 * unresolved module and this file would add type errors to a baseline it must leave
 * untouched. The real module also exports no `fetchListingDetails`, so there is no
 * signature to infer from.
 */
const legacyApi = vi.hoisted(() => ({
  fetchListingDetails: vi.fn(),
}));

vi.mock('@/services/api', () => legacyApi);

vi.mock('../../services/rating', async () => {
  const actual =
    await vi.importActual<typeof import('../../services/rating')>(
      '../../services/rating',
    );

  return {
    ...actual,
    fetchUserReputation: vi.fn(),
    fetchUserRatings: vi.fn(),
    fetchRatingEligibility: vi.fn(),
    submitRating: vi.fn(),
  };
});

/**
 * Every props object `ReputationBadge` was rendered with, in order.
 *
 * WHY A SPY WRAPPER RATHER THAN A REPLACEMENT. The badge defends itself: its
 * `hasRatings` guard requires an average within the scale AND a positive count, so
 * it renders "No ratings yet" for `average: 0, count: 0` exactly as it does for
 * `average: null, count: 0`. That is correct of the badge and it makes the page's
 * own choice — passing `null` rather than `0` — invisible through the rendering.
 * The choice still matters: `0` is not a low reputation but a claim of one, since
 * the scale's floor is 1, and a future badge with a different guard would surface
 * it as a zero-star seller.
 *
 * So the double RECORDS the props and then renders the REAL component, which keeps
 * every rendering assertion in this file genuine while making the wiring
 * assertable. A replacement would have traded one of those for the other.
 */
const badgeProps: Array<{
  average: number | null;
  count: number;
  label?: string;
}> = [];

vi.mock('../../components/ReputationBadge', async () => {
  const actual =
    await vi.importActual<typeof import('../../components/ReputationBadge')>(
      '../../components/ReputationBadge',
    );
  const RealBadge = actual.default;

  const RecordingBadge: typeof RealBadge = (props) => {
    badgeProps.push({
      average: props.average,
      count: props.count,
      label: props.label,
    });

    return <RealBadge {...props} />;
  };

  return { ...actual, default: RecordingBadge };
});

import VehicleDetailsPage from '../VehicleDetailsPage';
import { fetchUserRatings, fetchUserReputation } from '../../services/rating';
import { RATING_MAX } from '../../schema/rating';
import type { RatingAggregate } from '../../schema/rating';

/** The handle on the replaced listing read, from the hoisted registry above. */
const mockedFetchListing = legacyApi.fetchListingDetails;

const mockedFetchReputation = vi.mocked(fetchUserReputation);

/** The region's heading, which is how it is found in the accessibility tree. */
const REGION_HEADING = /seller information/i;

/** The label the page gives the badge, which prefixes its spoken sentence. */
const BADGE_LABEL = 'Seller rating';

/** The listing id carried by the route. */
const LISTING_ID = 'listing-9';

/** The seller named by the loaded listing document. */
const SELLER_ID = 'seller-42';

/**
 * The listing shape this page reads.
 *
 * `title`, `price` and `sellerId` are the fields it touches; the rest are handed to
 * children that stand in for absent components and are never inspected here.
 */
const listing = (
  overrides: Record<string, unknown> = {},
): Record<string, unknown> => ({
  title: '  2019 Volvo V60',
  sellerId: SELLER_ID,
  price: 18500,
  photos: [],
  specs: {},
  maintenanceHistory: [],
  ...overrides,
});

/** The badge's spoken sentence for a rated seller. */
const spokenRating = (average: string, count: string): string =>
  `${BADGE_LABEL}: ${average} out of ${RATING_MAX} from ${count}`;

/**
 * The badge's text when there is no reputation to report.
 *
 * Read from the region rather than matched as a single node, because the badge's
 * empty branch composes this sentence from THREE siblings — the visible caption,
 * an `sr-only` ": " separator, and the visible state — so that screen readers and
 * copied text both receive one sentence while the rendered badge keeps its designed
 * spacing. `getByText` matches per element and would find none of it. The populated
 * branch is the opposite shape: its fragments are `aria-hidden` and the sentence is
 * one `sr-only` node, which is why the rated cases below can query it directly.
 */
const SPOKEN_UNRATED = `${BADGE_LABEL}: No ratings yet`;

/*
 * The three sentences that are NOT the badge.
 *
 * The region distinguishes four situations, and only the last carries an
 * aggregate: the read is still in flight, the listing names no seller at all, the
 * read failed, or a reputation was loaded. Conflating them into the badge's empty
 * state was the defect these sentences replaced — "No ratings yet" is a factual
 * claim about a stranger's reputation, and it must not stand in for "we have not
 * asked yet", "this payload does not say who the seller is" or "the request
 * failed". Each is asserted by its own literal so a state that silently borrowed
 * another's wording would fail here.
 */
const SPOKEN_PENDING = 'Loading the seller\u0027s rating\u2026';
const SPOKEN_UNAVAILABLE = 'Seller rating is unavailable for this listing.';
const SPOKEN_FAILED = 'Seller rating could not be loaded right now.';

/**
 * The seller region's text with its heading removed and whitespace collapsed —
 * what any consumer reading text rather than looking at boxes receives.
 */
const sellerRegionText = (): string => {
  const heading = screen.getByRole('heading', { name: REGION_HEADING });
  const section = heading.closest('section');

  if (section === null) {
    throw new Error(
      'Expected the seller heading to be inside a <section>, and it was not.',
    );
  }

  return (section.textContent ?? '')
    .replace(heading.textContent ?? '', '')
    .replace(/\s+/g, ' ')
    .trim();
};

describe('VehicleDetailsPage', () => {
  beforeEach(() => {
    routeParams = { id: LISTING_ID };

    badgeProps.length = 0;

    mockedFetchListing.mockReset();
    mockedFetchReputation.mockReset();
    vi.mocked(fetchUserRatings).mockReset();

    // The page logs both a failed listing read and a failed reputation read, and
    // cases below provoke each.
    vi.spyOn(console, 'error').mockImplementation(() => undefined);
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('asks for no reputation before it knows which seller it is looking at', () => {
    mockedFetchListing.mockImplementation(() => new Promise(() => undefined));

    render(<VehicleDetailsPage />);

    expect(screen.getByText('Loading...')).toBeInTheDocument();

    // The seller is not known until the listing arrives, and a read keyed on
    // `undefined` would ask the API about a user called "undefined" — a request
    // that can only 404, on every listing view.
    expect(mockedFetchReputation).not.toHaveBeenCalled();
  });

  it('reads the reputation of the seller the loaded listing names', async () => {
    mockedFetchListing.mockResolvedValue(listing());
    mockedFetchReputation.mockResolvedValue({ average: 4.5, count: 2 });

    render(<VehicleDetailsPage />);

    await waitFor(() => {
      expect(mockedFetchReputation).toHaveBeenCalledWith(SELLER_ID);
    });

    // Once, and for the seller rather than for the listing. The two identifiers are
    // both in scope on this page, and asking with the listing id would return a
    // stranger's reputation — or a 404 — beside this car.
    expect(mockedFetchReputation).toHaveBeenCalledTimes(1);
    expect(mockedFetchReputation).not.toHaveBeenCalledWith(LISTING_ID);
  });

  it('shows the seller reputation where the buyer is deciding', async () => {
    mockedFetchListing.mockResolvedValue(listing());
    mockedFetchReputation.mockResolvedValue({ average: 4.5, count: 2 });

    render(<VehicleDetailsPage />);

    // Asserted through the sentence assistive technology is given, which is the
    // badge's accessible contract; the visual fragments beside it are decoration.
    expect(
      await screen.findByText(spokenRating('4.5', '2 ratings')),
    ).toBeInTheDocument();

    // Under a heading of its own, so the region is reachable by heading navigation
    // rather than only by eye.
    expect(
      screen.getByRole('heading', { name: REGION_HEADING }),
    ).toBeInTheDocument();
  });

  it('shows no reputation rather than a zero one while the read is in flight', async () => {
    mockedFetchListing.mockResolvedValue(listing());
    mockedFetchReputation.mockImplementation(
      () => new Promise<RatingAggregate>(() => undefined),
    );

    render(<VehicleDetailsPage />);

    // The region is present from the moment the listing renders, and it says the
    // read is in progress rather than showing the badge's empty state. `0` would
    // not be a low reputation, it would be a reputation the seller has not got:
    // the scale's floor is 1, so a zero average asserts an earned one star - and
    // "No ratings yet" would assert something equally untrue while the answer is
    // still on its way.
    await waitFor(() => {
      expect(screen.getByRole('heading', { name: REGION_HEADING })).toBeInTheDocument();
    });
    expect(sellerRegionText()).toBe(SPOKEN_PENDING);
    expect(screen.queryByText(/out of /i)).toBeNull();
    expect(screen.queryByText(SPOKEN_UNRATED)).toBeNull();
  });

  it('reports a seller nobody has rated as unrated', async () => {
    mockedFetchListing.mockResolvedValue(listing());
    // Real data for a seller who exists and has never been rated: the endpoint
    // returns exactly this rather than a 404.
    mockedFetchReputation.mockResolvedValue({ average: null, count: 0 });

    render(<VehicleDetailsPage />);

    await waitFor(() => {
      expect(mockedFetchReputation).toHaveBeenCalledWith(SELLER_ID);
    });

    expect(sellerRegionText()).toBe(SPOKEN_UNRATED);
  });

  it('makes no reputation request for a listing that names no seller', async () => {
    mockedFetchListing.mockResolvedValue(listing({ sellerId: undefined }));

    render(<VehicleDetailsPage />);

    // The listing still renders in full, including the region — which reports the
    // only thing that is true.
    expect(
      await screen.findByRole('heading', { name: REGION_HEADING }),
    ).toBeInTheDocument();
    // A gap in a payload this page does not own, reported as itself. Borrowing the
    // badge's empty state here would describe the seller as unrated on the
    // strength of a field that was never present.
    expect(sellerRegionText()).toBe(SPOKEN_UNAVAILABLE);
    expect(screen.queryByText(SPOKEN_UNRATED)).toBeNull();

    // And nothing was asked. This is the guard that keeps a malformed document from
    // producing a request per listing view.
    expect(mockedFetchReputation).not.toHaveBeenCalled();
  });

  it('keeps the listing usable when the reputation cannot be read', async () => {
    mockedFetchListing.mockResolvedValue(listing());
    mockedFetchReputation.mockRejectedValue(new Error('reputation unreachable'));

    render(<VehicleDetailsPage />);

    // The car is still there. A reputation is supporting information for a
    // decision, and a failure to load it must not cost the buyer the listing —
    // which is why the failure is caught beside the badge rather than at the page.
    expect(
      await screen.findByRole('heading', { name: '2019 Volvo V60' }),
    ).toBeInTheDocument();

    await waitFor(() => {
      expect(mockedFetchReputation).toHaveBeenCalledWith(SELLER_ID);
    });

    // And the region reports the failure rather than an invented value - neither a
    // zero average nor the unrated state, which would both be claims about the
    // seller rather than about the request.
    await waitFor(() => {
      expect(sellerRegionText()).toBe(SPOKEN_FAILED);
    });
    expect(screen.queryByText(SPOKEN_UNRATED)).toBeNull();
  });

  it('renders the listing but no reputation when the listing itself cannot be read', async () => {
    mockedFetchListing.mockRejectedValue(new Error('listing unreachable'));

    render(<VehicleDetailsPage />);

    // Pre-existing behaviour: the page has no error state and stays on the wait.
    // What matters for this feature is that no seller is inferred from a listing
    // that never arrived.
    await waitFor(() => {
      expect(screen.getByText('Loading...')).toBeInTheDocument();
    });

    expect(mockedFetchReputation).not.toHaveBeenCalled();
    expect(
      screen.queryByRole('heading', { name: REGION_HEADING }),
    ).toBeNull();
  });

  /**
   * The whole scale, rendered identically.
   *
   * The moderation policy for this feature is sentiment-neutral by requirement —
   * the aggregate counts every published rating regardless of score, and nothing
   * may suppress or restyle a low one. This table is that requirement as an
   * executable assertion: a threshold introduced anywhere between the response and
   * the badge fails here.
   */
  const SCORES: ReadonlyArray<{
    readonly aggregate: RatingAggregate;
    readonly spoken: string;
  }> = [
    { aggregate: { average: 1, count: 1 }, spoken: spokenRating('1.0', '1 rating') },
    {
      aggregate: { average: 1.2, count: 5 },
      spoken: spokenRating('1.2', '5 ratings'),
    },
    {
      aggregate: { average: 3, count: 12 },
      spoken: spokenRating('3.0', '12 ratings'),
    },
    {
      aggregate: { average: 4.9, count: 5 },
      spoken: spokenRating('4.9', '5 ratings'),
    },
    {
      aggregate: { average: RATING_MAX, count: 3 },
      spoken: spokenRating('5.0', '3 ratings'),
    },
  ];

  SCORES.forEach(({ aggregate, spoken }) => {
    it(`reports an average of ${String(aggregate.average)} exactly as the server sent it`, async () => {
      mockedFetchListing.mockResolvedValue(listing());
      mockedFetchReputation.mockResolvedValue(aggregate);

      render(<VehicleDetailsPage />);

      expect(await screen.findByText(spoken)).toBeInTheDocument();
    });
  });

  it('renders no seller identifier and reads no rating list', async () => {
    mockedFetchListing.mockResolvedValue(listing());
    mockedFetchReputation.mockResolvedValue({ average: 4.5, count: 2 });

    render(<VehicleDetailsPage />);
    await screen.findByText(spokenRating('4.5', '2 ratings'));

    // The seller's opaque key is data the page needed to make its request, not
    // something to put on screen.
    expect(document.body.textContent ?? '').not.toContain(SELLER_ID);

    // And the page reads the AGGREGATE only: pulling the full list here would fetch
    // every review of the seller to render one badge.
    expect(vi.mocked(fetchUserRatings)).not.toHaveBeenCalled();
    expect(screen.queryByRole('list')).toBeNull();
  });

  describe('the props the badge is given', () => {
    it('is not rendered at all until an aggregate has been read', async () => {
      mockedFetchListing.mockResolvedValue(listing());
      mockedFetchReputation.mockImplementation(
        () => new Promise<RatingAggregate>(() => undefined),
      );

      render(<VehicleDetailsPage />);
      await screen.findByRole('heading', { name: REGION_HEADING });

      // The badge is only reachable from the one state that carries an aggregate,
      // which is what makes "no ratings yet" impossible to render on a seller
      // nobody has asked about. It is a stronger guarantee than passing `null`:
      // the scale's floor is 1, so `0` would assert an earned reputation the
      // seller has not got, and `null` alone still renders a factual claim.
      expect(badgeProps).toEqual([]);
      expect(sellerRegionText()).toBe(SPOKEN_PENDING);
    });

    it('passes the aggregate through untouched once it arrives', async () => {
      mockedFetchListing.mockResolvedValue(listing());
      // A value that would be changed by rounding, clamping or recomputing.
      mockedFetchReputation.mockResolvedValue({ average: 4.44, count: 9 });

      render(<VehicleDetailsPage />);

      await waitFor(() => {
        expect(badgeProps).toContainEqual({
          average: 4.44,
          count: 9,
          label: BADGE_LABEL,
        });
      });

      // Not rounded here: the server is the authority for the aggregate and the
      // badge owns the one-decimal presentation, so nothing is left for this call
      // site to decide.
      expect(
        badgeProps.some((props) => props.average === 4.4),
      ).toBe(false);
    });

    it('is not rendered for a listing that names no seller', async () => {
      mockedFetchListing.mockResolvedValue(listing({ sellerId: undefined }));

      render(<VehicleDetailsPage />);
      await screen.findByRole('heading', { name: REGION_HEADING });

      // Nothing was read, so there is nothing to render a reputation from, and the
      // region says exactly that instead.
      expect(badgeProps).toEqual([]);
      expect(sellerRegionText()).toBe(SPOKEN_UNAVAILABLE);
    });

    it('labels the badge as the seller\u2019s, so the number is attributed', async () => {
      mockedFetchListing.mockResolvedValue(listing());
      mockedFetchReputation.mockResolvedValue({ average: 4.5, count: 2 });

      render(<VehicleDetailsPage />);
      await screen.findByText(spokenRating('4.5', '2 ratings'));

      // The label is what makes the badge's spoken sentence say WHOSE reputation
      // this is. Unlabelled, a screen reader announces "4.5 out of 5 from 2
      // ratings" on a page about a car, attributed to nothing.
      expect(
        badgeProps.every((props) => props.label === BADGE_LABEL),
      ).toBe(true);
    });
  });
});
