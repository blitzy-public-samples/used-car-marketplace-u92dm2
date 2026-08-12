import { format } from 'date-fns';

export function formatCurrency(amount: number, currencyCode: string): string {
  return new Intl.NumberFormat('en-US', {
    style: 'currency',
    currency: currencyCode,
  }).format(amount);
}

export function formatDate(date: Date | string): string {
  return format(new Date(date), 'MMMM d, yyyy');
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