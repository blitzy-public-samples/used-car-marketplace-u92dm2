import React, { useState, useEffect } from 'react';
import { useParams } from 'react-router-dom';
import { PhotoGallery } from '@/components/PhotoGallery';
import { VehicleSpecs } from '@/components/VehicleSpecs';
import { MaintenanceHistory } from '@/components/MaintenanceHistory';
import { MessageBox } from '@/components/MessageBox';
import { PaymentForm } from '@/components/PaymentForm';
import { fetchListingDetails } from '@/services/api';

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

const VehicleDetailsPage: React.FC = () => {
  const { id } = useParams<{ id: string }>();
  const [listingDetails, setListingDetails] = useState<any>(null);

  /*
   * The seller's aggregate reputation, or `null` while there is none to show.
   *
   * `null` covers two genuinely different situations, and this deliberately does
   * not distinguish them, because `ReputationBadge` renders the same thing for
   * both: the fetch has not resolved yet, and the seller has never been rated.
   * What matters is that neither is ever coerced to a number. Passing `0` as a
   * stand-in for a missing average would render an unrated seller as an earned
   * one-star reputation, on the exact screen where a buyer decides whether to
   * transact — see the note at the call site.
   */
  const [sellerRating, setSellerRating] = useState<RatingAggregate | null>(null);

  /*
   * Whose reputation to fetch, read off the loaded listing.
   *
   * Annotated explicitly. `listingDetails` is `any`, so the annotation is what
   * stops that `any` from propagating into the effect below and into its
   * dependency array, without this change introducing an `any` of its own.
   *
   * `undefined` is a legitimate value, not a transient one to be waited out: it
   * holds while the listing is still loading, and it can persist afterwards
   * because the listing endpoint is owned elsewhere and this page cannot
   * guarantee the field is present. The effect treats it as "nothing to fetch"
   * rather than as a failure.
   */
  const sellerId: string | undefined = listingDetails
    ? listingDetails.sellerId
    : undefined;

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
   * corrupts React's hook ordering. That is exactly why the derivation above
   * tolerates `listingDetails === null` instead of this effect being moved down
   * past the guard where the seller id would always be available.
   */
  useEffect(() => {
    /*
     * No seller id, no request.
     *
     * `sellerRating` is already `null`, so the badge renders its own empty state
     * and there is nothing to fetch and nothing to report — an absent field on a
     * payload this page does not own is not an error to surface to a buyer. It
     * also avoids spending a round trip on `/ratings/user/undefined` to be
     * answered 404.
     */
    if (!sellerId) {
      return;
    }

    const loadSellerRating = async () => {
      try {
        const aggregate = await fetchUserReputation(sellerId);
        setSellerRating(aggregate);
      } catch (error) {
        /*
         * Reputation is supporting information, not the subject of this page, so
         * a failure is logged and the badge stays in its empty state instead of
         * replacing the listing with an error. `fetchUserReputation` can reject
         * with an `AxiosError` (404, or a transport failure) or with a
         * `RatingContractError` when the envelope cannot be interpreted; neither
         * is actionable by the buyer, and neither should cost them the listing
         * they came here to read.
         */
        console.error('Failed to fetch seller reputation:', error);
      }
    };

    loadSellerRating();
  }, [sellerId]);

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
        `<h1>` is emitted.

        STYLING. Tailwind default-scale utilities only, which is the whole token
        source here — `theme.extend` in `tailwind.config.js` is empty and no
        component library is installed, so there is no `Card`, `Panel` or `Section`
        to reuse and a semantic element carrying system utilities is the correct
        resolution. No raw colour, no pixel dimension, no inline `style`. The
        page's existing `vehicle-details-page` class is unbacked kebab-case that no
        stylesheet defines; the two idioms coexist and reconciling them is a
        repository-wide restyle rather than part of this change.
      */}
      <section className="mt-6">
        <h2 className="text-lg font-semibold mb-4">Seller Information</h2>
        {/*
          `null` rather than `0` for a missing average, which is the whole reason
          these props are branched instead of defaulted: `0` is not a low
          reputation, it is not a reputation at all — the scale's floor is 1 — so
          coercing it would state an earned one star for a seller nobody has rated.
          `average === null` with `count === 0` is the badge's first-class "No
          ratings yet" state, and it covers both the pre-fetch and the never-rated
          case.

          The figures are passed through UNTOUCHED: not rounded, not clamped, not
          recomputed. The server is the authority for the aggregate and the badge
          owns its one-decimal presentation, so nothing is left for this call site
          to decide. Nothing here keys on the VALUE either — no threshold, no
          colour tier, no hiding of a low score — because the aggregate counts
          every published rating whatever it says, and a 1.2 is rendered exactly as
          a 4.9 is.
        */}
        <ReputationBadge
          average={sellerRating ? sellerRating.average : null}
          count={sellerRating ? sellerRating.count : 0}
          label="Seller rating"
        />
      </section>
      <MessageBox listingId={id} />
      <PaymentForm listingId={id} price={listingDetails.price} />
    </div>
  );
};

export default VehicleDetailsPage;