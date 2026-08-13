/**
 * Stand-ins for the page children that cannot be loaded in a test, so that the
 * three rating page integrations can be tested at all.
 *
 * WHY THIS FILE HAS TO EXIST
 * -----------------------------------------------------------------------------
 * `TransactionPage`, `UserProfilePage` and `VehicleDetailsPage` each compose
 * children this repository does not have, and Vite fails at TRANSFORM time on an
 * unresolvable import — before any module code runs — so `vi.mock` cannot rescue
 * them: the mock registry is consulted during execution, and execution never
 * begins. The only intervention that works is at RESOLUTION time, which is what
 * the `test.alias` block in `../../vite.config.ts` does, pointing each of those
 * specifiers at this module.
 *
 * Two distinct problems are covered, and both are pre-existing:
 *
 *   ABSENT FILES. `TransactionDetails`, `PaymentStatus`, `ProfileForm`,
 *   `ListingManagement`, `PhotoGallery`, `VehicleSpecs` and `MaintenanceHistory`
 *   are imported by those pages and do not exist anywhere in the repository. The
 *   plan for this feature lists creating them as explicitly out of scope, and the
 *   rating insertions were deliberately written to be additive so they do not
 *   depend on any of them.
 *
 *   UNLOADABLE FILES. `MessageBox` and `PaymentForm` do exist, and still cannot be
 *   used here. Both are imported BY NAME while each exports only a default, so the
 *   binding is `undefined` and React refuses to render it; `PaymentForm`
 *   additionally imports `@stripe/react-stripe-js`, which is not a declared
 *   dependency, so its transform fails outright. `@/services/payment` is in the
 *   same position: it imports `@stripe/stripe-js` and a module specifier
 *   (`app/services/api`) that resolves to nothing.
 *
 * WHAT THESE DOUBLES DELIBERATELY DO NOT DO
 * -----------------------------------------------------------------------------
 * They render a marker and nothing else. No prop is displayed, no state is kept,
 * no request is made. That is a correctness property rather than laziness: a
 * double that echoed its props would put values into the page's DOM that the page
 * did not put there, and every assertion of the form "this page does not render
 * the seller's internal id" would then be measuring this file. A page test should
 * fail because the PAGE is wrong.
 *
 * The marker is a `data-testid` rather than a role or a label, and that is also
 * deliberate. A stand-in has no semantics of its own — it is not a region, not a
 * form, not a list — so giving it an ARIA role would put a lie in the
 * accessibility tree and would let a page test "find" structure that only the
 * double provides. A test id says exactly what this is: a placeholder whose
 * presence proves composition and whose content proves nothing.
 *
 * EVERY EXPORT SHAPE THE IMPORTERS USE
 * -----------------------------------------------------------------------------
 * One file serves every aliased specifier because it exports both forms: a default
 * for `import TransactionDetails from '@/components/TransactionDetails'`, and a
 * name per child for `import { ProfileForm } from '@/components/ProfileForm'`. A
 * file per specifier would be nine near-identical modules with one comment
 * repeated nine times.
 *
 * This module is never imported by application code and is not reachable from
 * `src/index.tsx`, so it is absent from the production bundle. It lives under
 * `src/` so that `tsc --noEmit` type-checks it like everything else, and it is not
 * named `*.test.tsx`, so Vitest does not try to collect it as a suite.
 */

import React from 'react';

/** Props any stand-in tolerates: whatever the page passes, ignored on purpose. */
type LegacyChildProps = Record<string, unknown>;

/**
 * Build a stand-in that renders one stable marker.
 *
 * @param name The component being stood in for, which names the marker.
 */
const standIn = (name: string): React.FC<LegacyChildProps> => {
  const StandIn: React.FC<LegacyChildProps> = () => (
    <div data-testid={`legacy-child-${name}`} />
  );

  // Named so React devtools and any component-stack in a failure message say
  // which child this was, rather than "StandIn" nine times over.
  StandIn.displayName = `LegacyChildStandIn(${name})`;

  return StandIn;
};

/** Absent: imported by `TransactionPage` as a default. */
export const TransactionDetails = standIn('TransactionDetails');

/** Absent: imported by `TransactionPage` as a default. */
export const PaymentStatus = standIn('PaymentStatus');

/** Absent: imported by `UserProfilePage` by name. */
export const ProfileForm = standIn('ProfileForm');

/** Absent: imported by `UserProfilePage` by name. */
export const ListingManagement = standIn('ListingManagement');

/** Absent: imported by `VehicleDetailsPage` by name. */
export const PhotoGallery = standIn('PhotoGallery');

/** Absent: imported by `VehicleDetailsPage` by name. */
export const VehicleSpecs = standIn('VehicleSpecs');

/** Absent: imported by `VehicleDetailsPage` by name. */
export const MaintenanceHistory = standIn('MaintenanceHistory');

/** Present but unloadable: default-exported, imported by name. */
export const MessageBox = standIn('MessageBox');

/** Present but unloadable: default-exported, imported by name, needs Stripe. */
export const PaymentForm = standIn('PaymentForm');

/**
 * The default export, for the specifiers whose importer uses a default import —
 * today `@/components/TransactionDetails` and `@/components/PaymentStatus`.
 *
 * Generic rather than one of the named components above, because a default import
 * carries no name to match on: the importing page decides what it calls this, and
 * this module cannot know which specifier resolved to it.
 *
 * The consequence is stated plainly rather than worked around: two default-imported
 * children on one page render the SAME marker, so a test cannot tell them apart by
 * it. No test needs to — the pages' rating behaviour is what is under test, and
 * these children exist in that graph only because the page happens to compose
 * them. A test that did need to distinguish two of them should be given its own
 * per-specifier module rather than a cleverer marker here.
 */
const LegacyChildDefault = standIn('default');

export default LegacyChildDefault;
