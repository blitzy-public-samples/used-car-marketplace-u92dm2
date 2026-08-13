import React, { useEffect, useId, useRef, useState } from 'react';
import { useParams } from 'react-router-dom';
import { PhotoGallery } from '@/components/PhotoGallery';
import { VehicleSpecs } from '@/components/VehicleSpecs';
import { MaintenanceHistory } from '@/components/MaintenanceHistory';
import { MessageBox } from '@/components/MessageBox';
import { PaymentForm } from '@/components/PaymentForm';
import { fetchListingDetails } from '@/services/api';
import { readListingSellerId } from '../utils/validation';

/*
 * Seller reputation, for the bidirectional peer rating system (F010).
 *
 * RELATIVE PATHS, DELIBERATELY. The five imports above use an `@/…` prefix that
 * resolves nowhere. `tsconfig.json` declares six aliases — `@components/*`,
 * `@pages/*`, `@utils/*`, `@styles/*`, `@hooks/*` and `@context/*` — and there is
 * no bare `@/*` among them; `vite.config.ts` mirrors exactly those six and
 * records that an `'@' → src` entry was tried and rejected. So `@/…` resolves in
 * neither the type-checker nor the bundler, which is precisely what the six
 * TS2307 errors this file already carries in the measured baseline are.
 * Repairing them means rewriting the imports of some thirty files and is a
 * separate change; reproducing the pattern in a NEW import would just add a
 * seventh error, so these three are relative.
 *
 * `ReputationBadge` is a DEFAULT export, as every component in this tree is —
 * including the `MessageBox` and `PaymentForm` that the two named imports above
 * get wrong. This is the same badge the profile page mounts, and its prop names
 * and types are a contract shared between the two call sites.
 *
 * The service comes from `../services/rating`, NOT `../services/api`. Both
 * expose rating methods, but `api.ts` imports `getAuthToken` from the
 * unresolvable specifier `app/utils/auth`, whereas `../services/rating` builds
 * its own axios instance specifically to avoid depending on it.
 *
 * `RatingAggregate` is imported as a TYPE: `isolatedModules` is set, so the
 * type-only form is unambiguous to a single-file transform and the specifier is
 * erased instead of being emitted as a runtime import.
 */
import ReputationBadge from '../components/ReputationBadge';
import { fetchUserReputation } from '../services/rating';
import type { RatingAggregate } from '../schema/rating';

// HUMAN ASSISTANCE NEEDED
// The following component may need additional error handling, loading states, and responsive design considerations for production readiness.

/**
 * Every distinguishable state the seller's reputation region can be in.
 *
 * A single `RatingAggregate | null` was ambiguous in a way that mattered on this
 * screen: it read the same whether the request had not resolved yet, the listing
 * carried no seller identifier at all, the request had failed, or the seller had
 * genuinely never been rated. `ReputationBadge` renders "No ratings yet" for the
 * first three of those, so a buyer deciding whether to transact was shown a
 * factual claim about the seller's reputation that the page had no evidence for.
 *
 * `'loaded'` is the ONLY state that carries an aggregate, so the badge can only
 * be rendered when there is a real server answer behind it, and the other three
 * are each explained in their own words instead.
 */
type SellerReputation =
  | { readonly status: 'pending' }
  | { readonly status: 'unavailable' }
  | { readonly status: 'failed' }
  | { readonly status: 'loaded'; readonly aggregate: RatingAggregate };

/*
 * WHICH KEY IS AUTHORITATIVE, and why the reader lives in `utils/validation.ts`.
 *
 * `GET /api/listings/{listing_id}` answers with the `VehicleListing` Pydantic
 * model, whose field is `seller_id` (`backend/app/schema/listing.py`), and
 * FastAPI serialises Pydantic field names verbatim — so `seller_id` is what
 * actually arrives over the wire. The client's own Zod model
 * (`frontend/src/schema/listing.ts`) declares the camelCase `sellerId`, and no
 * mapper exists anywhere in this repository to bridge the two for listings;
 * `frontend/src/services/rating.ts` owns that adaptation for ratings only.
 * Reading `sellerId` alone therefore found `undefined` on every real response,
 * which silently skipped the request and left the badge asserting "No ratings
 * yet" for every seller on the site.
 *
 * `readListingSellerId` prefers the wire key and honours the camelCase key
 * second, because both are declared contracts here: the first is what the server
 * sends today, the second keeps this page working unchanged once a listing
 * mapper is introduced. It also trims, and refuses a value that is empty, longer
 * than a document ID may be, or slash-bearing — so no round trip is spent on an
 * identifier the ratings endpoint could not address. It is a reader, not a
 * mapper: it narrows one field and converts nothing.
 *
 * It is imported rather than written inline because this page cannot be rendered
 * in a test at all — its five `@/…` imports resolve nowhere — so the read is only
 * testable once it is extracted. `frontend/src/utils/__tests__/validation.test.ts`
 * is what guards the behaviour this page depends on.
 *
 * The reader reports absence as `undefined`; it is normalised to `null` at the
 * single call site below, because an unidentifiable seller is a gap in a payload
 * this page does not own and the region says so rather than describing it as an
 * unrated seller.
 */

const VehicleDetailsPage: React.FC = () => {
  const { id } = useParams<{ id: string }>();
  const [listingDetails, setListingDetails] = useState<any>(null);

  /*
   * The accessible name for the seller region below. `useId` rather than a
   * literal so the value is unique even if this page is ever mounted twice, and
   * so it matches the convention the rating components already follow.
   */
  const sellerHeadingId = useId();

  /*
   * The seller's reputation, as one of four named states rather than a nullable
   * aggregate. `'pending'` is the initial state because that is the truth on
   * first render: the listing has not arrived, so nothing has been asked yet.
   *
   * An average is never coerced to a number in any state. Passing `0` as a
   * stand-in for a missing average would render an unrated seller as an earned
   * one-star reputation — the scale's floor is 1, so `0` is not a low reputation,
   * it is not a reputation at all — on the exact screen where a buyer decides
   * whether to transact.
   */
  const [sellerReputation, setSellerReputation] = useState<SellerReputation>({
    status: 'pending',
  });

  /*
   * Invalidates in-flight reputation requests.
   *
   * Bumped when a request starts and again when the effect is cleaned up, so a
   * response that arrives after the seller changed — or after this page
   * unmounted — is discarded instead of being written into state. Two listings by
   * different sellers viewed in quick succession is the ordinary way that
   * happens, and without this the slower of the two responses wins and the page
   * shows one seller's reputation under another seller's listing.
   *
   * A ref rather than state: it must be readable and writable without causing a
   * render, and its value is never displayed.
   */
  const reputationRequestRef = useRef(0);

  /*
   * Whose reputation to fetch, read off the loaded listing.
   *
   * `readListingSellerId` consumes the authoritative wire key — see the note
   * above for why the previously-read `sellerId` found nothing on a real
   * response. Explicitly annotated, which is what stops `listingDetails`'s `any`
   * from propagating into the effect below and into its dependency array.
   *
   * `null` is a legitimate resting value, not a transient one to be waited out: it
   * holds while the listing is still loading, and it can persist afterwards
   * because the listing endpoint is owned elsewhere and this page cannot
   * guarantee the field is present. The effect distinguishes the two, which is
   * the whole point — the first is `'pending'`, the second is `'unavailable'`.
   */
  const sellerId: string | null = readListingSellerId(listingDetails) ?? null;

  useEffect(() => {
    const fetchDetails = async () => {
      try {
        const details = await fetchListingDetails(id);
        setListingDetails(details);
      } catch (error) {
        console.error('Error fetching listing details:', error);
        // TODO: Implement proper error handling
      }
    };

    fetchDetails();
  }, [id]);

  /*
   * Load the seller's aggregate reputation.
   *
   * A SECOND, SEPARATE EFFECT rather than an addition to the one above, for two
   * reasons. It is triggered by `sellerId`, which only exists once the listing
   * has resolved, so it must re-run on a different dependency than `[id]`; and
   * folding it in would let a reputation failure abort the listing load, whereas
   * the two now fail independently of one another.
   *
   * Declared ABOVE the `if (!listingDetails)` return below, which is a
   * correctness requirement rather than a preference: a hook placed after a
   * conditional return is called on some renders and skipped on others, which
   * corrupts React's hook ordering. That is exactly why `readListingSellerId`
   * tolerates a missing payload instead of this effect being moved down past the
   * guard where the listing would always be in hand.
   */
  useEffect(() => {
    /*
     * One generation per run of this effect. Everything below writes state only
     * while its own generation is still the current one, so a response for a
     * previous seller — or for a page that has since unmounted — is dropped.
     */
    const generation = (reputationRequestRef.current += 1);
    const isCurrent = (): boolean => reputationRequestRef.current === generation;

    /*
     * No seller id, no request — but the two reasons there is no seller id are
     * reported differently rather than both being passed over in silence.
     *
     * `readListingSellerId` reports absence both while the listing is still loading and
     * when a loaded listing does not identify its seller. The second is a gap in a
     * payload this page does not own, and saying "no ratings yet" for it would
     * state a fact about the seller that nothing supports — so it becomes
     * `'unavailable'`, while the first stays `'pending'`. Either way no request is
     * made, which also avoids spending a round trip on `/ratings/user/undefined`
     * to be answered 404.
     */
    if (sellerId === null) {
      /*
       * The same predicate the page's own `if (!listingDetails)` early return
       * uses, so the two agree by construction: this region is only ever rendered
       * on a truthy listing, which is exactly when `'unavailable'` is the true
       * statement. Until then nothing has been asked, so the state is `'pending'`.
       */
      const idle: SellerReputation = listingDetails
        ? { status: 'unavailable' }
        : { status: 'pending' };

      if (isCurrent()) {
        /*
         * Functional form, and the equality check is why: a fresh object with the
         * same status would fail `Object.is` and cost a render that changes
         * nothing on screen. Returning `current` lets React bail out.
         */
        setSellerReputation((current) =>
          current.status === idle.status ? current : idle,
        );
      }

      return;
    }

    /*
     * A new seller starts from "not answered yet" rather than keeping the
     * previous seller's figures on screen while the next request is in flight.
     */
    if (isCurrent()) {
      setSellerReputation((current) =>
        current.status === 'pending' ? current : { status: 'pending' },
      );
    }

    const loadSellerRating = async () => {
      try {
        const aggregate = await fetchUserReputation(sellerId);

        if (isCurrent()) {
          setSellerReputation({ status: 'loaded', aggregate });
        }
      } catch (error) {
        /*
         * Reputation is supporting information, not the subject of this page, so
         * a failure is logged and reported in one line instead of replacing the
         * listing with an error. `fetchUserReputation` can reject with an
         * `AxiosError` (404, or a transport failure) or with a
         * `RatingContractError` when the envelope cannot be interpreted; neither
         * is actionable by the buyer, and neither should cost them the listing
         * they came here to read.
         *
         * It is emphatically NOT reported as an unrated seller. A failed request
         * is evidence of nothing about the seller, and the badge's "No ratings
         * yet" is a factual claim, so the failure gets its own words.
         */
        console.error('Failed to fetch seller reputation:', error);

        if (isCurrent()) {
          setSellerReputation({ status: 'failed' });
        }
      }
    };

    loadSellerRating();

    return () => {
      // Invalidates whatever is in flight, so a late response cannot write state
      // after unmount or against a newer seller.
      reputationRequestRef.current += 1;
    };
  }, [sellerId, listingDetails]);

  if (!listingDetails) {
    return <div>Loading...</div>; // TODO: Replace with a proper loading component
  }

  return (
    <div className="vehicle-details-page">
      <h1>{listingDetails.title}</h1>
      <PhotoGallery photos={listingDetails.photos} />
      <VehicleSpecs specs={listingDetails.specs} />
      <MaintenanceHistory history={listingDetails.maintenanceHistory} />
      {/*
        SELLER INFORMATION — the counterparty's reputation, at the point of decision.

        PLACEMENT. `documentation/Technical Specifications.md` L452-L464 defines
        this screen's regions, and L460 reads `G[Seller Information] --> H[Contact
        Form]`. The contact form in code is the `MessageBox` immediately below, so
        this region sits directly before it and the block reads seller reputation
        -> contact -> payment. That is the order a buyer needs it in: whether to
        transact with this person is decided before how to reach them.

        A SIBLING, NOT A WRAPPER. Nothing around it is re-nested — `MessageBox` and
        `PaymentForm` are not moved inside this section, and the three components
        above are left in place. Every other defect on this page (the unresolvable
        `@/…` specifiers, the three imported components that do not exist, the
        `recipientId` that `MessageBox` requires and is not given) is pre-existing,
        accounted for in the measured baseline, and out of scope here.

        HEADING. A real `<h2>`, so the region appears in a screen reader's heading
        list rather than being findable only by eye — WCAG 2.1 Level AA is a stated
        requirement of this project. The page's only other heading is the `<h1>`
        above, so `<h2>` is the next level down: no level is skipped and no second
        `<h1>` is emitted. The heading also NAMES the section, through
        `aria-labelledby` pointing at its `id`: a `<section>` is only exposed as a
        landmark ("region") when it has an accessible name, so without this the
        element is announced as a plain group, is absent from the landmark list a
        screen-reader user navigates by, and the heading beside it is the only clue
        it exists. `useId` supplies the id so it is unique per mount.

        STYLING. Tailwind default-scale utilities only, which is the whole token
        source here — `theme.extend` in `tailwind.config.js` is empty and no
        component library is installed, so there is no `Card`, `Panel` or `Section`
        to reuse and a semantic element carrying system utilities is the correct
        resolution. No raw colour, no pixel dimension, no inline `style`. The
        page's existing `vehicle-details-page` class is unbacked kebab-case that no
        stylesheet defines; the two idioms coexist and reconciling them is a
        repository-wide restyle rather than part of this change.
      */}
      <section className="mt-6" aria-labelledby={sellerHeadingId}>
        <h2 id={sellerHeadingId} className="text-lg font-semibold mb-4">
          Seller Information
        </h2>
        {/*
          FOUR STATES, FOUR DIFFERENT SENTENCES. The badge is rendered only for
          `'loaded'`, because "No ratings yet" is a factual claim about the seller
          and only a real server answer supports it. Waiting on the request, a
          listing that does not identify its seller, and a request that failed are
          each said in their own words — none of them is evidence that nobody has
          rated this person, and on the screen where a buyer decides whether to
          transact the difference is the difference between an honest blank and a
          made-up one.

          `role="status"` on the three non-loaded lines, so a reader who is already
          past this point in the page is told politely when the answer arrives
          rather than having to go back and look. They are not `role="alert"`:
          supporting information that has not loaded is not an emergency, and
          interrupting a screen reader over it would be worse than saying nothing.

          The loaded figures are passed through UNTOUCHED: not rounded, not clamped,
          not recomputed. The server is the authority for the aggregate and the
          badge owns its one-decimal presentation and its own first-class "No
          ratings yet" state for a genuinely unrated seller (`average === null` with
          `count === 0`), so nothing is left for this call site to decide. Nothing
          here keys on the VALUE either — no threshold, no colour tier, no hiding of
          a low score — because the aggregate counts every published rating whatever
          it says, and a 1.2 is rendered exactly as a 4.9 is.
        */}
        {sellerReputation.status === 'loaded' ? (
          <ReputationBadge
            average={sellerReputation.aggregate.average}
            count={sellerReputation.aggregate.count}
            label="Seller rating"
          />
        ) : sellerReputation.status === 'pending' ? (
          <p role="status" className="text-sm text-gray-600">
            Loading the seller&apos;s rating…
          </p>
        ) : sellerReputation.status === 'unavailable' ? (
          <p role="status" className="text-sm text-gray-600">
            Seller rating is unavailable for this listing.
          </p>
        ) : (
          <p role="status" className="text-sm text-gray-600">
            Seller rating could not be loaded right now.
          </p>
        )}
      </section>
      <MessageBox listingId={id} />
      <PaymentForm listingId={id} price={listingDetails.price} />
    </div>
  );
};

export default VehicleDetailsPage;