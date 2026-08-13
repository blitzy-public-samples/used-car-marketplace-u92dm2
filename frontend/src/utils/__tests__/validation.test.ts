/**
 * Tests for `readListingSellerId` in `../validation` — the read that decides
 * WHOSE reputation the vehicle details page fetches for its seller badge
 * (SRS F010-3, "Display of aggregate ratings on user profiles").
 *
 * WHY THIS FUNCTION HAS A TEST FILE OF ITS OWN
 * -----------------------------------------------------------------------------
 * The page shipped reading `listingDetails.sellerId`. Nothing on the wire is
 * spelled that way: `backend/app/schema/listing.py` declares
 * `VehicleListing.seller_id`, and `GET /api/listings/{id}` returns that model
 * with no camelCase mapper anywhere on the path. So the value was `undefined` on
 * every real response, the page's own "no seller id, no request" guard was taken
 * every single time, and the badge was permanently empty — on the one screen
 * whose purpose is to tell a buyer about the person they are about to transact
 * with. Nothing failed, nothing logged, and every test passed: a field read is
 * exactly the kind of defect that is invisible until someone looks at the
 * screen.
 *
 * `VehicleDetailsPage` itself cannot be rendered here. Five of its imports use
 * an `@/…` specifier that resolves in neither the type-checker nor the bundler,
 * which is a pre-existing defect it shares with some thirty other files and is
 * out of scope to repair. Extracting the field read into a module that CAN be
 * exercised is what makes the fix provable, and this file is that proof.
 *
 * WHAT IS ASSERTED, AND WHAT IS DELIBERATELY NOT
 * -----------------------------------------------------------------------------
 * Three properties, each of which could regress independently:
 *
 *   1. The WIRE spelling is read, and it wins when both spellings are present.
 *      A payload carrying `seller_id: 'a'` beside `sellerId: 'b'` must yield
 *      `'a'`, because a normaliser added later must not be able to shadow the
 *      authoritative field with a stale copy of it.
 *   2. A value that could not address the endpoint is reported as ABSENT rather
 *      than passed on. The result is interpolated into
 *      `/api/ratings/user/{userId}/aggregate`, whose path parameter the server
 *      validates against the Firestore document-ID grammar, so an empty or
 *      whitespace-only string, one carrying a `/`, or one longer than the
 *      grammar allows would spend a round trip to be answered 404 or 422 — and
 *      `/ratings/user//aggregate` would not even address the intended route.
 *   3. Nothing else about a listing is asserted. The payload type is `unknown`
 *      because that is what it is, and this function narrows one field; a test
 *      that demanded a whole listing shape would be testing a contract this
 *      function does not have.
 *
 * The realistic fixture below is built from the server model's field list rather
 * than from a minimal object, so a passing test means the function works against
 * what the API actually sends rather than against a shape invented here.
 */
import { readListingSellerId } from '../validation';

/**
 * A listing payload with the fields `VehicleListing` declares, snake_case
 * throughout, as `GET /api/listings/{id}` returns them.
 *
 * @param overrides Fields to replace or add.
 * @returns The payload, typed loosely on purpose — the function under test
 *   accepts `unknown`, and a precise type here would assert a contract the page
 *   does not have either.
 */
const listingPayload = (
  overrides: Record<string, unknown> = {}
): Record<string, unknown> => ({
  id: 'test-listing-0000000001',
  seller_id: 'test-seller-00000000001',
  make: 'Toyota',
  model: 'Corolla',
  year: 2019,
  mileage: 41200,
  price: 12500.0,
  condition: 'good',
  photos: ['https://example.test/photo.jpg'],
  maintenance_records: [],
  status: 'active',
  created_at: '2024-01-01T00:00:00Z',
  updated_at: '2024-01-02T00:00:00Z',
  ...overrides
});

describe('readListingSellerId', () => {
  describe('the wire field', () => {
    it('reads seller_id from a listing shaped as the API returns it', () => {
      expect(readListingSellerId(listingPayload())).toBe(
        'test-seller-00000000001'
      );
    });

    it('reads seller_id even when a camelCase copy is also present', () => {
      // The regression this guards: a normaliser introduced later must not
      // be able to shadow the authoritative field with a stale copy.
      const listing = listingPayload({ sellerId: 'stale-camel-case-id' });

      expect(readListingSellerId(listing)).toBe('test-seller-00000000001');
    });

    it('accepts a camelCase payload from a caller that normalised it', () => {
      // Tolerated, second, so a consumer that has already mapped the
      // payload is not broken by the wire spelling taking precedence.
      const listing = listingPayload();
      delete listing.seller_id;
      listing.sellerId = 'test-seller-00000000002';

      expect(readListingSellerId(listing)).toBe('test-seller-00000000002');
    });

    it('trims surrounding whitespace rather than sending it', () => {
      const listing = listingPayload({
        seller_id: '  test-seller-00000000003  '
      });

      expect(readListingSellerId(listing)).toBe('test-seller-00000000003');
    });
  });

  describe('nothing to fetch', () => {
    it('reports undefined while the listing has not loaded', () => {
      // The page holds `null` until its fetch resolves, and this is the
      // state that must read as "nothing to fetch" rather than as an error.
      expect(readListingSellerId(null)).toBeUndefined();
      expect(readListingSellerId(undefined)).toBeUndefined();
    });

    it('reports undefined for a payload that is not an object', () => {
      expect(readListingSellerId('a string')).toBeUndefined();
      expect(readListingSellerId(42)).toBeUndefined();
      expect(readListingSellerId(true)).toBeUndefined();
    });

    it('reports undefined when the listing carries no seller field', () => {
      const listing = listingPayload();
      delete listing.seller_id;

      expect(readListingSellerId(listing)).toBeUndefined();
    });

    it('reports undefined when the seller field is not a string', () => {
      // A non-string cannot be interpolated into a path without becoming
      // the literal text `undefined`, `null` or `[object Object]`.
      for (const value of [null, undefined, 7, {}, [], true]) {
        expect(
          readListingSellerId(listingPayload({ seller_id: value }))
        ).toBeUndefined();
      }
    });
  });

  describe('values that could not address the endpoint', () => {
    it('refuses an empty or whitespace-only id', () => {
      // `/ratings/user//aggregate` does not address the intended route at
      // all, so this must never reach a request.
      for (const value of ['', '   ', '\t', '\n']) {
        expect(
          readListingSellerId(listingPayload({ seller_id: value }))
        ).toBeUndefined();
      }
    });

    it('refuses an id containing a path separator', () => {
      // A `/` addresses a different resource, and the server refuses it
      // with a 422 — a round trip spent to learn nothing.
      for (const value of [
        'seller/child',
        '/leading',
        'trailing/',
        'a/b/c'
      ]) {
        expect(
          readListingSellerId(listingPayload({ seller_id: value }))
        ).toBeUndefined();
      }
    });

    it('refuses an id longer than the document-ID grammar allows', () => {
      const tooLong = 'a'.repeat(129);
      const longest = 'a'.repeat(128);

      expect(
        readListingSellerId(listingPayload({ seller_id: tooLong }))
      ).toBeUndefined();
      // The bound itself is legal: refusing at the limit would reject an
      // id the server accepts.
      expect(
        readListingSellerId(listingPayload({ seller_id: longest }))
      ).toBe(longest);
    });
  });
});
