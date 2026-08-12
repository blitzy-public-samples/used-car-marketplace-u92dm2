export function formatCurrency(amount: number, currencyCode: string): string {
  return new Intl.NumberFormat('en-US', {
    style: 'currency',
    currency: currencyCode,
  }).format(amount);
}

/**
 * The single formatter behind `formatDate`, built once at module scope.
 *
 * `Intl.DateTimeFormat` construction is the expensive part of formatting a date
 * and the instance is stateless, so one shared instance is created here rather
 * than per call - `RatingList` renders one date per rating and `ListingCard` one
 * per card, so building it per call would repeat that cost for every row.
 *
 * The options reproduce exactly what this module produced through `date-fns`'
 * `'MMMM d, yyyy'` pattern: full month name, day with no leading zero, four-digit
 * year, comma-separated - "August 12, 2026". The locale is pinned to `en-US`
 * rather than left to the host, matching `formatCurrency` directly above, so a
 * list rendered on two machines cannot disagree.
 */
const DATE_FORMATTER = new Intl.DateTimeFormat('en-US', {
  month: 'long',
  day: 'numeric',
  year: 'numeric',
});

/**
 * Render a date as a full month name, day and year - "August 12, 2026".
 *
 * Uses the platform's `Intl.DateTimeFormat` rather than a formatting library, and
 * that is a DEPENDENCY-CLOSURE requirement rather than a preference. This module
 * previously imported `format` from `date-fns`, a package declared in neither
 * `package.json` nor `package-lock.json`, so `npm ci` - which is what CI runs -
 * never installed it. Every consumer of this module therefore had an unresolvable
 * import in its graph: `tsc` reported it, a production bundle could not be built,
 * and the component test for `ReputationBadge` had to mock this whole module away
 * to run at all, which is a false green rather than a fix. It also meant the
 * module appeared to work on any developer machine with a stale copy in
 * `node_modules` while failing a reproducible install.
 *
 * Declaring the package was not the answer either - the undeclared frontend
 * packages serving untouched code paths stay undeclared by design - so the
 * dependency is removed instead. `Intl` is part of the language, needs no
 * manifest entry and no supply chain to audit, and `formatCurrency` above
 * already relies on it.
 *
 * Behaviour is unchanged for every caller, including `ListingCard`, which is not
 * part of the rating feature: the output string is identical to what the
 * `'MMMM d, yyyy'` pattern produced, and an unusable date still throws rather
 * than rendering. Throwing is the contract the consumers were written against -
 * `RatingList` guards a possibly-invalid `createdAt` before calling precisely
 * because both `toISOString()` and this function throw on one - so returning a
 * placeholder string instead would silently disable that guard and let a
 * malformed timestamp render as an empty date rather than being handled.
 *
 * @param date A `Date`, or anything `new Date(...)` accepts - an ISO string in
 *   practice, which is what the API returns.
 * @returns The formatted date.
 * @throws {RangeError} When the value cannot be read as a real date, which is the
 *   same failure the previous implementation raised on an Invalid Date. Callers
 *   that may hold one guard before calling; `RatingList` does exactly that.
 */
export function formatDate(date: Date | string): string {
  return DATE_FORMATTER.format(new Date(date));


}

/**
 * Formats a reputation average for display with exactly one decimal place —
 * `4.5` renders as `"4.5"` and `4` renders as `"4.0"`, never `"4"`.
 *
 * The parameter is `number | null` because it mirrors `RatingAggregate.average`
 * (`../schema/rating`) and `ratingAverage` (`../schema/user`), both declared
 * `z.number().nullable()`. One fractional digit is the precision the project's
 * own success metric states — "4.5/5 star average rating from both buyers and
 * sellers" — and this is what F010-3, display of aggregate ratings on user
 * profiles, renders reputation through.
 *
 * `null` means "no ratings yet" and returns an EMPTY STRING, never `"0.0"`: a
 * user who has never been rated has not earned a zero. The empty-state wording
 * deliberately lives in `../components/ReputationBadge`, which renders its own
 * "No ratings yet" copy off exactly that null, so a second phrasing here would
 * collide with it. `NaN` and `Infinity` yield the same empty string, so an
 * arithmetic accident upstream cannot surface as the literal text "NaN".
 *
 * Formatting only: the value is neither validated nor clamped into the 1..5
 * scale, and no "/5" suffix is appended, because that copy belongs to the
 * components. `toFixed` is locale-independent, so the result cannot drift with
 * the host locale.
 */
export function formatRating(average: number | null): string {
  if (average === null || !Number.isFinite(average)) {
    return '';
  }
  return average.toFixed(1);
}