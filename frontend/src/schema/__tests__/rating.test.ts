/**
 * The client half of the rating contract, branch by branch.
 *
 * WHAT THIS SUITE OWNS
 * ====================
 * `../rating` is the client's mirror of the server's rating rules, and
 * `../../utils/validation` is the one path a submission travels before it
 * reaches the wire. Between them they decide what a user is allowed to type,
 * what a character counter promises, which transaction references can be
 * submitted at all, and whether a response the server sent is accepted or
 * reported as a contract failure. None of it was directly tested: the component
 * suites exercised a handful of these rules incidentally, through a form, which
 * left the boundaries themselves - the exact limit, the value one past it, the
 * astral character that measures differently under two length rules - unpinned.
 *
 * Its companion is `backend/tests/test_rating_contract_parity.py`, which
 * compares these declarations against the server's own. The division is
 * deliberate and worth stating, because neither test can do the other's job:
 *
 * - The parity gate proves the two sides declare the SAME numbers, names,
 *   enumerations and routes. It reads this module as text, so it cannot
 *   execute a refinement or a transform.
 * - This suite proves those declarations BEHAVE as the contract requires -
 *   that the limit is applied to the normalised text rather than the raw
 *   text, that the bound counts code points rather than UTF-16 units, that a
 *   forged key is stripped before any request is built.
 *
 * WHY THE VALIDATION HELPERS ARE TESTED HERE RATHER THAN SEPARATELY
 * ================================================================
 * `prepareReviewText` and `validateRatingInput` are thin compositions of this
 * module's own primitives - `normalizeReviewText`, `textLength` and
 * `RatingCreateSchema` - and they share every fixture with them. The
 * relationship between the three is the thing most worth asserting: the
 * counter, the submit gate and the payload must all measure the SAME string,
 * because when they disagreed a user was told a review was too long while the
 * payload that would have been sent was comfortably inside the bound. Splitting
 * them across two files would duplicate the fixtures and hide that
 * relationship.
 *
 * Every expectation here is measured against the real modules. Nothing is
 * mocked, because there is nothing to mock: these are pure functions and
 * schemas, and the one impure dependency - DOMPurify, reached through
 * `sanitizeUserInput` - is exercised for real under jsdom, which is the
 * environment it runs in.
 */
import { describe, expect, it } from 'vitest';
import { z } from 'zod';

import {
  EligibilityDecisionSchema,
  ModeratedRatingSchema,
  ModerationStatusSchema,
  RATING_MAX,
  RATING_MIN,
  RatingAggregateSchema,
  RatingCreateSchema,
  RatingDirectionSchema,
  RatingSchema,
  REVIEW_MAX_LENGTH,
  REVIEW_RAW_LENGTH_FACTOR,
  REVIEW_RAW_MAX_LENGTH,
  UserRatingsResponseSchema,
  codePointLength,
  normalizeReviewText,
  textLength
} from '../rating';
import { UserSchema } from '../user';
import {
  prepareReviewText,
  sanitizeUserInput,
  validateRatingInput
} from '../../utils/validation';

/**
 * Parse a value and return the issues it was refused with.
 *
 * Throws when the value PARSES, rather than returning an empty list: a test
 * that expected a refusal and silently received an acceptance would otherwise
 * assert nothing at all, which is the failure mode this helper exists to
 * prevent.
 *
 * @param run Parse call expected to be refused.
 * @returns One `field: message` string per issue, in Zod's own order.
 */
const refusalOf = (run: () => unknown): string[] => {
  try {
    run();
  } catch (error) {
    if (error instanceof z.ZodError) {
      return error.issues.map(
        (issue) => `${issue.path.join('.') || '(root)'}: ${issue.message}`
      );
    }
    throw error;
  }

  throw new Error(
    'expected the value to be refused, but it parsed successfully'
  );
};

/** A valid submission body, from which each negative case deviates once. */
const submission = (overrides: Record<string, unknown> = {}) => ({
  transactionId: 'test-transaction-000001',
  score: 4,
  ...overrides
});

/** An instant used wherever a timestamp's value is not what is under test. */
const MOMENT = new Date('2024-05-01T10:00:00.000Z');

/** A valid rating response, from which each negative case deviates once. */
const ratingResponse = (overrides: Record<string, unknown> = {}) => ({
  id: 'test-transaction-000001_test-buyer-000000000001',
  transactionId: 'test-transaction-000001',
  vehicleListingId: 'test-listing-0000000001',
  raterId: 'test-buyer-000000000001',
  rateeId: 'test-seller-00000000001',
  direction: 'buyer_to_seller',
  score: 4,
  review: null,
  isPublished: true,
  createdAt: MOMENT,
  updatedAt: MOMENT,
  ...overrides
});

/** A valid eligible decision, from which each impossible case deviates once. */
const eligibleDecision = (overrides: Record<string, unknown> = {}) => ({
  eligible: true,
  reason: null,
  rateeId: 'test-seller-00000000001',
  direction: 'buyer_to_seller',
  alreadyRated: false,
  ...overrides
});

describe('the shared rating scale', () => {
  it('is the 1 to 5 scale the project fixed', () => {
    expect(RATING_MIN).toBe(1);
    expect(RATING_MAX).toBe(5);
  });

  it('accepts every score on the scale and nothing beyond it', () => {
    for (let score = RATING_MIN; score <= RATING_MAX; score += 1) {
      expect(RatingCreateSchema.parse(submission({ score })).score).toBe(
        score
      );
    }

    expect(refusalOf(() => RatingCreateSchema.parse(
      submission({ score: RATING_MIN - 1 })
    ))).toEqual([`score: A rating must be at least ${RATING_MIN}`]);
    expect(refusalOf(() => RatingCreateSchema.parse(
      submission({ score: RATING_MAX + 1 })
    ))).toEqual([`score: A rating must be at most ${RATING_MAX}`]);
  });

  it('refuses a fraction rather than rounding it to a vote', () => {
    // The server is strict here for a security reason rather than a tidy
    // one: a coercing validator would turn 4.7 into 4 and record a score
    // the rater never chose.
    expect(refusalOf(() => RatingCreateSchema.parse(
      submission({ score: 4.5 })
    ))).toEqual(['score: A rating must be a whole number of stars']);
  });

  it('refuses a score that is not a number at all', () => {
    for (const score of ['4', true, null, undefined]) {
      expect(refusalOf(() => RatingCreateSchema.parse(
        submission({ score })
      )).join('')).toContain('score');
    }
  });

  it('bounds a score arriving on a response by the same scale', () => {
    expect(
      RatingSchema.parse(ratingResponse({ score: RATING_MAX })).score
    ).toBe(RATING_MAX);
    expect(refusalOf(() => RatingSchema.parse(
      ratingResponse({ score: RATING_MAX + 1 })
    )).join('')).toContain('score');
  });
});

describe('normalising a review the way the server does', () => {
  it('composes decomposed text, as NFC', () => {
    // 'e' + COMBINING ACUTE composes to a single 'é'. The server composes
    // before measuring, so a client that did not would count a different
    // number of characters for the same word.
    expect(normalizeReviewText('e\u0301clair')).toBe('\u00e9clair');
    expect(codePointLength(normalizeReviewText('e\u0301clair'))).toBe(6);
  });

  it('folds every line ending to a newline', () => {
    expect(normalizeReviewText('a\r\nb')).toBe('a\nb');
    expect(normalizeReviewText('a\rb')).toBe('a\nb');
  });

  it('removes control and format characters', () => {
    // Cc (a BEL) and Cf (a zero-width space, and a soft hyphen) - the two
    // Unicode categories the server strips. A zero-width space is the one
    // that matters in practice: it is invisible, so a review containing a
    // run of them would pass a length check while displaying as nothing.
    expect(normalizeReviewText('a\u0007b')).toBe('ab');
    expect(normalizeReviewText('a\u200Bb')).toBe('ab');
    expect(normalizeReviewText('a\u00ADb')).toBe('ab');
  });

  it('keeps the two control characters prose legitimately contains', () => {
    expect(normalizeReviewText('a\tb')).toBe('a\tb');
    expect(normalizeReviewText('a\nb')).toBe('a\nb');
  });

  it('keeps spacing characters that are not control characters', () => {
    // A non-breaking space is category Zs and a line separator is Zl, so
    // neither is stripped. Asserted because "remove the invisible
    // characters" is a tempting over-simplification of the rule, and one
    // that would silently edit somebody's text.
    expect(normalizeReviewText('a\u00A0b')).toBe('a\u00A0b');
    expect(normalizeReviewText('a\u2028b')).toBe('a\u2028b');
  });

  it('collapses a run of blank lines to a single blank line', () => {
    expect(normalizeReviewText('a\n\n\n\n\nb')).toBe('a\n\nb');
    expect(normalizeReviewText('a\n\nb')).toBe('a\n\nb');
    expect(normalizeReviewText('a\nb')).toBe('a\nb');
  });

  it('trims the result, so no separate trim is ever needed', () => {
    expect(normalizeReviewText('   a   ')).toBe('a');
    expect(normalizeReviewText('\ta\t')).toBe('a');
    expect(normalizeReviewText('\n\na\n\n')).toBe('a');
  });

  it('reduces text with nothing in it to the empty string', () => {
    for (const blank of ['', '   ', '\n\n', '\t', '\u200B']) {
      expect(normalizeReviewText(blank)).toBe('');
    }
  });

  it('is idempotent, so normalising a normalised review changes nothing', () => {
    // Load-bearing rather than incidental. `validateRatingInput` normalises
    // whatever it is handed, including a value a caller has already prepared
    // for its counter, so the value measured has to be the value sent.
    const messy = '  <b>Great</b>\u0007 car\r\n\r\n\r\nreally\u200Bgood  ';
    const once = normalizeReviewText(messy);
    expect(normalizeReviewText(once)).toBe(once);
  });

  it('leaves emoji and other astral characters intact', () => {
    expect(normalizeReviewText('\u{1F600}\u{1F600}')).toBe(
      '\u{1F600}\u{1F600}'
    );
  });
});

describe('measuring a review the way the server does', () => {
  it('counts code points rather than UTF-16 code units', () => {
    // An emoji is one code point stored as a surrogate pair. A `.length`
    // counter would tell somebody who wrote 1200 emoji they had used 2400
    // of their 2000 characters.
    expect('\u{1F600}'.length).toBe(2);
    expect(codePointLength('\u{1F600}')).toBe(1);
    expect(textLength('\u{1F600}')).toBe(1);
  });

  it('composes before counting, for prose', () => {
    // The server composes a review before measuring it, so 'e' + combining
    // acute is one character to both sides.
    expect(codePointLength('e\u0301')).toBe(2);
    expect(textLength('e\u0301')).toBe(1);
  });

  it('does not compose when counting an identifier', () => {
    // The distinction is why the two functions both exist: the server does
    // NOT normalise a document ID before measuring it, so measuring one
    // after composition would count fewer code points than the rule being
    // mirrored and would accept a value the server refuses.
    expect(codePointLength('e\u0301clair')).toBe(7);
    expect(textLength('e\u0301clair')).toBe(6);
  });

  it('agrees with the plain count for text inside the basic plane', () => {
    expect(codePointLength('Great car')).toBe(9);
    expect(textLength('Great car')).toBe(9);
    expect('Great car'.length).toBe(9);
  });
});

describe('the raw ceiling and the semantic limit', () => {
  it('derives the raw ceiling from the semantic limit', () => {
    expect(REVIEW_MAX_LENGTH).toBe(2000);
    expect(REVIEW_RAW_LENGTH_FACTOR).toBe(4);
    expect(REVIEW_RAW_MAX_LENGTH).toBe(
      REVIEW_RAW_LENGTH_FACTOR * REVIEW_MAX_LENGTH
    );
  });

  it('accepts a review of exactly the semantic limit', () => {
    const review = 'y'.repeat(REVIEW_MAX_LENGTH);
    const parsed = RatingCreateSchema.parse(submission({ review }));
    expect(parsed.review).toBe(review);
    expect(textLength(parsed.review as string)).toBe(REVIEW_MAX_LENGTH);
  });

  it('refuses a review one character past the semantic limit', () => {
    expect(refusalOf(() => RatingCreateSchema.parse(
      submission({ review: 'y'.repeat(REVIEW_MAX_LENGTH + 1) })
    ))).toEqual([
      `review: A review must be at most ${REVIEW_MAX_LENGTH} characters`
    ]);
  });

  it('accepts a review of emoji up to the limit, counted as code points', () => {
    // The case a `.max()` bound would refuse and the server would accept:
    // 2000 emoji measure 4000 UTF-16 code units. This is the whole reason
    // the bound is a refinement rather than `.max()`.
    const review = '\u{1F600}'.repeat(REVIEW_MAX_LENGTH);
    const parsed = RatingCreateSchema.parse(submission({ review }));
    expect((parsed.review as string).length).toBe(REVIEW_MAX_LENGTH * 2);
    expect(textLength(parsed.review as string)).toBe(REVIEW_MAX_LENGTH);
  });

  it('refuses raw text past the ceiling before doing any other work', () => {
    // The ceiling is a denial-of-service guard, not the number a counter
    // shows, and its message must not claim the semantic limit was reached.
    expect(refusalOf(() => RatingCreateSchema.parse(
      submission({ review: 'y'.repeat(REVIEW_RAW_MAX_LENGTH + 1) })
    ))).toEqual([
      'review: A review must be at most ' +
        `${REVIEW_RAW_MAX_LENGTH} characters before formatting is removed`
    ]);
  });

  it('applies the semantic limit to the normalised text', () => {
    // Inside the raw ceiling, over the semantic limit. The order of the two
    // bounds is the contract: applying the semantic one to the RAW text
    // would refuse reviews the server accepts.
    expect(refusalOf(() => RatingCreateSchema.parse(
      submission({ review: 'y'.repeat(REVIEW_RAW_MAX_LENGTH) })
    ))).toEqual([
      `review: A review must be at most ${REVIEW_MAX_LENGTH} characters`
    ]);
  });

  it('measures what will be stored, not what was typed', () => {
    // Formatting that normalisation removes does not count against the
    // limit, because it is not part of the value the server stores.
    const padded = `  ${'y'.repeat(REVIEW_MAX_LENGTH)}  \r\n`;
    expect(padded.length).toBeGreaterThan(REVIEW_MAX_LENGTH);
    expect(RatingCreateSchema.parse(submission({ review: padded })).review)
      .toBe('y'.repeat(REVIEW_MAX_LENGTH));
  });

  it('refuses markup rather than silently editing it out', () => {
    // Refused, not stripped: quietly rewriting somebody's review is worse
    // than telling them which character to remove.
    expect(refusalOf(() => RatingCreateSchema.parse(
      submission({ review: 'a <b> c' })
    ))).toEqual([
      'review: A review must be plain text and must not contain "<" or ">"'
    ]);
    expect(refusalOf(() => RatingCreateSchema.parse(
      submission({ review: '2 > 1' })
    )).join('')).toContain('plain text');
  });

  it('normalises a blank review to the empty string and sends it', () => {
    // A deliberate asymmetry with the server, which normalises blank text
    // to null itself. Second-guessing it here would be this layer
    // inventing a rule the contract does not have.
    expect(RatingCreateSchema.parse(submission({ review: '   ' })).review)
      .toBe('');
  });

  it('bounds a review arriving on a response by the semantic limit', () => {
    expect(
      RatingSchema.parse(
        ratingResponse({ review: 'y'.repeat(REVIEW_MAX_LENGTH) })
      ).review
    ).toHaveLength(REVIEW_MAX_LENGTH);
    expect(refusalOf(() => RatingSchema.parse(
      ratingResponse({ review: 'y'.repeat(REVIEW_MAX_LENGTH + 1) })
    ))).toEqual([
      `review: A review must be at most ${REVIEW_MAX_LENGTH} characters`
    ]);
  });

  it('accepts a response review of emoji the server had accepted', () => {
    // The direction that matters on a response: a client stricter than the
    // server would report a contract failure for a review the server had
    // already validated and stored.
    expect(
      RatingSchema.parse(
        ratingResponse({ review: '\u{1F600}'.repeat(REVIEW_MAX_LENGTH) })
      ).review
    ).toHaveLength(REVIEW_MAX_LENGTH * 2);
  });
});

describe('path-safe transaction references', () => {
  it('accepts an ordinary document ID', () => {
    for (const transactionId of [
      'test-transaction-000001',
      'a.b',
      'a',
      'a'.repeat(128)
    ]) {
      expect(
        RatingCreateSchema.parse(submission({ transactionId })).transactionId
      ).toBe(transactionId);
    }
  });

  it('refuses a reference that would address a different document', () => {
    // A '/' is read by the Firestore client as path structure rather than
    // as part of an ID, so this is the one client-supplied value that can
    // redirect a document read.
    expect(refusalOf(() => RatingCreateSchema.parse(
      submission({ transactionId: 'a/b' })
    ))).toEqual([
      'transactionId: A transaction reference must not contain "/" or a ' +
        'control character, and must not be ".", ".." or of the form ' +
        '"__name__"'
    ]);
  });

  it('refuses the reserved identifiers Firestore keeps for itself', () => {
    for (const transactionId of ['.', '..', '__name__', '__x__']) {
      expect(refusalOf(() => RatingCreateSchema.parse(
        submission({ transactionId })
      )).join('')).toContain('must not contain "/"');
    }
  });

  it('refuses a control character in a reference', () => {
    // A newline inside an identifier is how a log line gets forged, and a
    // NUL or DEL produces a document whose key cannot be typed back.
    for (const transactionId of ['a\u0000b', 'a\u001Fb', 'a\u007Fb', 'a\nb']) {
      expect(refusalOf(() => RatingCreateSchema.parse(
        submission({ transactionId })
      )).join('')).toContain('control character');
    }
  });

  it('refuses an empty reference, and says both things that are wrong', () => {
    // Two issues rather than one: it is empty, and an empty string is not a
    // path-safe identifier. Asserted as a pair because a form renders every
    // issue it is given.
    expect(refusalOf(() => RatingCreateSchema.parse(
      submission({ transactionId: '' })
    ))).toHaveLength(2);
    expect(refusalOf(() => RatingCreateSchema.parse(
      submission({ transactionId: '' })
    ))[0]).toBe('transactionId: A transaction reference is required');
  });

  it('bounds a reference at the length the server bounds it', () => {
    expect(refusalOf(() => RatingCreateSchema.parse(
      submission({ transactionId: 'a'.repeat(129) })
    ))).toEqual([
      'transactionId: A transaction reference must be at most 128 characters'
    ]);
  });

  it('measures a reference in code points, as the server does', () => {
    // 128 astral characters measure 256 by `.length`, so a `.max()` bound
    // would refuse a reference the server accepts - and a transaction whose
    // ID contained one could then never be rated from this client at all.
    const astral = '\u{1F600}'.repeat(128);
    expect(astral.length).toBe(256);
    expect(
      RatingCreateSchema.parse(submission({ transactionId: astral }))
        .transactionId
    ).toBe(astral);
  });
});

describe('the submission body', () => {
  it('carries exactly the three keys the server accepts', () => {
    expect(
      Object.keys(RatingCreateSchema.parse(submission({ review: 'Good' })))
    ).toEqual(['transactionId', 'score', 'review']);
  });

  it('omits the review entirely when none was written', () => {
    const parsed = RatingCreateSchema.parse(submission());
    expect(Object.keys(parsed)).toEqual(['transactionId', 'score']);
    expect('review' in parsed).toBe(false);
  });

  it('strips a forged counterparty or direction claim', () => {
    // The client STRIPS an unrecognised key where the server REFUSES one.
    // Both are safe and both are safe in the same direction: no spoofed
    // field can reach the wire, and none can reach a document. What matters
    // here is that the stripping happens before any request is built, so
    // `rateeId` and `direction` are not merely ignored by the server - they
    // are never sent.
    const parsed = RatingCreateSchema.parse(
      submission({
        rateeId: 'a-victim',
        direction: 'seller_to_buyer',
        isPublished: true,
        raterId: 'somebody-else'
      })
    );
    expect(Object.keys(parsed)).toEqual(['transactionId', 'score']);
  });

  it('refuses a body that is not an object', () => {
    for (const candidate of ['nope', null, 7, []]) {
      expect(refusalOf(() => RatingCreateSchema.parse(candidate)).join(''))
        .toContain('Expected object');
    }
  });

  it('reports every missing field by name', () => {
    expect(refusalOf(() => RatingCreateSchema.parse({}))).toEqual([
      'transactionId: Required',
      'score: Required'
    ]);
  });
});

describe('a rating response', () => {
  it('accepts the shape the server sends', () => {
    const parsed = RatingSchema.parse(ratingResponse());
    expect(Object.keys(parsed)).toEqual([
      'id',
      'transactionId',
      'vehicleListingId',
      'raterId',
      'rateeId',
      'direction',
      'score',
      'review',
      'isPublished',
      'createdAt',
      'updatedAt'
    ]);
    expect(parsed.createdAt).toBeInstanceOf(Date);
  });

  it('requires real Date timestamps, not the strings on the wire', () => {
    // The mapper in `../../services/rating` converts them, so a string
    // reaching this schema means the conversion was skipped.
    expect(refusalOf(() => RatingSchema.parse(
      ratingResponse({ createdAt: MOMENT.toISOString() })
    ))).toEqual(['createdAt: Expected date, received string']);
  });

  it('accepts only the two directions the contract declares', () => {
    for (const direction of RatingDirectionSchema.options) {
      expect(RatingSchema.parse(ratingResponse({ direction })).direction)
        .toBe(direction);
    }
    expect(refusalOf(() => RatingSchema.parse(
      ratingResponse({ direction: 'sideways' })
    )).join('')).toContain('Invalid enum value');
  });

  it('accepts a null review and refuses an absent one', () => {
    // The distinction the mappers exist to preserve: null is the server
    // saying "no review was written", while an absent key means the payload
    // was truncated or reshaped - and rendering the second as the first
    // would state something the server never said.
    expect(RatingSchema.parse(ratingResponse({ review: null })).review)
      .toBeNull();
    const { review, ...withoutReview } = ratingResponse();
    expect(review).toBeNull();
    expect(refusalOf(() => RatingSchema.parse(withoutReview))).toEqual([
      'review: Required'
    ]);
  });

  it('strips a moderation field that has no business on a public read', () => {
    const parsed = RatingSchema.parse(
      ratingResponse({ moderationStatus: 'approved' })
    );
    expect('moderationStatus' in parsed).toBe(false);
  });
});

describe('a moderated rating response', () => {
  const moderated = (overrides: Record<string, unknown> = {}) => ({
    ...ratingResponse(),
    moderationStatus: 'rejected',
    moderationReason: 'contains a phone number',
    ...overrides
  });

  it('accepts the public fields plus the two moderation fields', () => {
    expect(Object.keys(ModeratedRatingSchema.parse(moderated()))).toEqual([
      'id',
      'transactionId',
      'vehicleListingId',
      'raterId',
      'rateeId',
      'direction',
      'score',
      'review',
      'isPublished',
      'createdAt',
      'updatedAt',
      'moderationStatus',
      'moderationReason'
    ]);
  });

  it('accepts only the three moderation states the contract declares', () => {
    for (const moderationStatus of ModerationStatusSchema.options) {
      expect(
        ModeratedRatingSchema.parse(
          moderated({
            moderationStatus,
            moderationReason:
              moderationStatus === 'rejected' ? 'a policy reason' : null
          })
        ).moderationStatus
      ).toBe(moderationStatus);
    }
    expect(refusalOf(() => ModeratedRatingSchema.parse(
      moderated({ moderationStatus: 'hidden' })
    )).join('')).toContain('Invalid enum value');
  });

  it('bounds a moderator reason at 500 characters, in code points', () => {
    expect(
      ModeratedRatingSchema.parse(
        moderated({ moderationReason: 'r'.repeat(500) })
      ).moderationReason
    ).toHaveLength(500);
    expect(refusalOf(() => ModeratedRatingSchema.parse(
      moderated({ moderationReason: 'r'.repeat(501) })
    ))).toEqual([
      'moderationReason: A moderation reason must be at most 500 characters'
    ]);
  });

  it('accepts a null reason, which every displayed state carries', () => {
    expect(
      ModeratedRatingSchema.parse(
        moderated({ moderationStatus: 'approved', moderationReason: null })
      ).moderationReason
    ).toBeNull();
  });
});

describe('the aggregate', () => {
  it('accepts a real reputation', () => {
    expect(RatingAggregateSchema.parse({ average: 4.5, count: 2 })).toEqual({
      average: 4.5,
      count: 2
    });
  });

  it('accepts the unrated pair, which is null and zero', () => {
    // Never zero and zero: a zero average renders as an earned one-star
    // reputation for somebody who has simply never been rated.
    expect(RatingAggregateSchema.parse({ average: null, count: 0 })).toEqual({
      average: null,
      count: 0
    });
  });

  it('refuses a count with no average', () => {
    expect(refusalOf(() => RatingAggregateSchema.parse(
      { average: null, count: 3 }
    ))).toEqual(['average: A positive rating count requires an average']);
  });

  it('refuses an average with no count', () => {
    expect(refusalOf(() => RatingAggregateSchema.parse(
      { average: 4, count: 0 }
    ))).toEqual(['count: An average requires a positive rating count']);
  });

  it('bounds the average by the shared scale', () => {
    expect(refusalOf(() => RatingAggregateSchema.parse(
      { average: RATING_MIN - 0.5, count: 1 }
    ))[0]).toBe(`average: A rating average must be at least ${RATING_MIN}`);
    expect(refusalOf(() => RatingAggregateSchema.parse(
      { average: RATING_MAX + 0.5, count: 1 }
    ))[0]).toBe(`average: A rating average must be at most ${RATING_MAX}`);
  });

  it('refuses an average that is not a finite number', () => {
    expect(refusalOf(() => RatingAggregateSchema.parse(
      { average: Number.NaN, count: 1 }
    )).join('')).toContain('received nan');
    expect(refusalOf(() => RatingAggregateSchema.parse(
      { average: Number.POSITIVE_INFINITY, count: 1 }
    ))[0]).toBe('average: A rating average must be a finite number');
  });

  it('refuses a count that is not a whole non-negative number', () => {
    expect(refusalOf(() => RatingAggregateSchema.parse(
      { average: 4, count: 1.5 }
    ))[0]).toBe('count: A rating count must be a whole number');
    expect(refusalOf(() => RatingAggregateSchema.parse(
      { average: null, count: -1 }
    ))[0]).toBe('count: A rating count cannot be negative');
  });

  it('reports an absent key by name rather than assuming null', () => {
    expect(refusalOf(() => RatingAggregateSchema.parse({ count: 0 })))
      .toEqual(['average: Required']);
  });
});

describe('the ratings envelope', () => {
  it('accepts the published ratings and the aggregate together', () => {
    const parsed = UserRatingsResponseSchema.parse({
      items: [ratingResponse()],
      aggregate: { average: 4, count: 1 }
    });
    expect(Object.keys(parsed)).toEqual(['items', 'aggregate']);
    expect(parsed.items).toHaveLength(1);
  });

  it('accepts an empty page of ratings for an unrated user', () => {
    expect(
      UserRatingsResponseSchema.parse({
        items: [],
        aggregate: { average: null, count: 0 }
      }).items
    ).toEqual([]);
  });

  it('accepts a count larger than the number of items returned', () => {
    // The contract, not a discrepancy: `items` omits any rating whose
    // review moderation rejected, while `aggregate` counts every published
    // rating whatever its score and state. A suite that refused this would
    // be encoding a rule the server does not have.
    expect(
      UserRatingsResponseSchema.parse({
        items: [],
        aggregate: { average: 4, count: 3 }
      }).aggregate.count
    ).toBe(3);
  });

  it('refuses a page that is not an array of ratings', () => {
    expect(refusalOf(() => UserRatingsResponseSchema.parse({
      items: ratingResponse(),
      aggregate: { average: 4, count: 1 }
    }))).toEqual(['items: Expected array, received object']);
  });

  it('refuses a page whose ratings are malformed, naming the field', () => {
    // The path locates the offending rating within the page, which is what
    // makes a contract failure diagnosable rather than merely reported. The
    // message is Zod's own here, and deliberately so: the request schema
    // carries hand-written messages because a user reads them, while a
    // malformed RESPONSE is a developer-facing fault nobody should ever see.
    expect(refusalOf(() => UserRatingsResponseSchema.parse({
      items: [ratingResponse({ score: RATING_MAX + 4 })],
      aggregate: { average: 4, count: 1 }
    }))).toEqual([
      `items.0.score: Number must be less than or equal to ${RATING_MAX}`
    ]);
  });
});

describe('an eligibility decision', () => {
  it('accepts the eligible decision the server emits', () => {
    expect(EligibilityDecisionSchema.parse(eligibleDecision())).toEqual(
      eligibleDecision()
    );
  });

  it('accepts every refusal the server emits', () => {
    // The five refusals `evaluate_eligibility` can report, each carrying
    // the sentence the write path would have raised. The two that are
    // reached before participation is established name no counterparty,
    // which is why those fields are nullable at all.
    const refusals = [
      {
        reason: 'Only verified users can submit ratings',
        rateeId: null,
        direction: null,
        alreadyRated: false
      },
      {
        reason: 'Transaction not found',
        rateeId: null,
        direction: null,
        alreadyRated: false
      },
      {
        reason: 'You are not a party to this transaction',
        rateeId: null,
        direction: null,
        alreadyRated: false
      },
      {
        reason: 'Ratings require a completed transaction',
        rateeId: 'test-seller-00000000001',
        direction: 'buyer_to_seller',
        alreadyRated: false
      },
      {
        reason: 'You have already rated this transaction',
        rateeId: 'test-seller-00000000001',
        direction: 'buyer_to_seller',
        alreadyRated: true
      }
    ];

    for (const refusal of refusals) {
      expect(
        EligibilityDecisionSchema.parse({ eligible: false, ...refusal }).reason
      ).toBe(refusal.reason);
    }
  });

  it('refuses an eligible decision that names no counterparty', () => {
    // The form would tell somebody they may rate while being unable to say
    // whom, so this is refused rather than rendered.
    expect(refusalOf(() => EligibilityDecisionSchema.parse(
      eligibleDecision({ rateeId: null })
    ))).toEqual([
      'rateeId: An eligible decision must name the counterparty being rated'
    ]);
  });

  it('refuses an eligible decision with no direction', () => {
    expect(refusalOf(() => EligibilityDecisionSchema.parse(
      eligibleDecision({ direction: null })
    ))).toEqual([
      'direction: An eligible decision must carry the rating direction'
    ]);
  });

  it('refuses an eligible decision from a caller who already rated', () => {
    expect(refusalOf(() => EligibilityDecisionSchema.parse(
      eligibleDecision({ alreadyRated: true })
    ))).toEqual([
      'alreadyRated: A caller who has already rated this transaction ' +
        'cannot be eligible'
    ]);
  });

  it('refuses a refusal that does not explain itself', () => {
    // Without the reason the form disables its control with no
    // explanation, which is the accessibility failure the eligibility
    // endpoint exists to prevent.
    expect(refusalOf(() => EligibilityDecisionSchema.parse(
      eligibleDecision({ eligible: false, reason: null })
    ))).toEqual(['reason: An ineligible decision must explain why']);
  });

  it('refuses a direction outside the two the contract declares', () => {
    expect(refusalOf(() => EligibilityDecisionSchema.parse(
      eligibleDecision({ direction: 'sideways' })
    )).join('')).toContain('Invalid enum value');
  });
});

describe('the user shape this feature extended', () => {
  const user = (overrides: Record<string, unknown> = {}) => ({
    id: 'test-buyer-000000000001',
    email: 'buyer@example.test',
    firstName: 'Test',
    lastName: 'User',
    role: 'buyer',
    createdAt: MOMENT,
    updatedAt: MOMENT,
    isVerified: true,
    ratingAverage: 4.5,
    ratingCount: 2,
    ...overrides
  });

  it('carries the three fields the rating feature added', () => {
    // `isVerified` is the R1 gate and the other two are the denormalised
    // aggregate the profile reads, so a client missing them could neither
    // explain a refusal nor render a reputation.
    const parsed = UserSchema.parse(user());
    expect(parsed.isVerified).toBe(true);
    expect(parsed.ratingAverage).toBe(4.5);
    expect(parsed.ratingCount).toBe(2);
  });

  it('accepts a user who has never been rated', () => {
    const parsed = UserSchema.parse(
      user({ ratingAverage: null, ratingCount: 0 })
    );
    expect(parsed.ratingAverage).toBeNull();
    expect(parsed.ratingCount).toBe(0);
  });

  it('accepts an unverified user, who simply cannot rate', () => {
    // Not verified is an ordinary state rather than an error: the server
    // defaults the flag to false, so every user who predates verification
    // deserialises this way.
    expect(UserSchema.parse(user({ isVerified: false })).isVerified)
      .toBe(false);
  });

  it('refuses a verification flag that is not a boolean', () => {
    // Mirrors the server's `StrictBool`: 1 and 'true' are refused rather
    // than coerced, because a coerced truthy value would grant the R1 gate
    // to a record that never asserted verification.
    for (const isVerified of [1, 'true', null]) {
      expect(refusalOf(() => UserSchema.parse(user({ isVerified })))
        .join('')).toContain('isVerified');
    }
  });

  it('refuses a fractional rating count', () => {
    expect(refusalOf(() => UserSchema.parse(user({ ratingCount: 1.5 })))
      .join('')).toContain('ratingCount');
  });

  it('reports each of the three fields by name when it is absent', () => {
    for (const field of ['isVerified', 'ratingAverage', 'ratingCount']) {
      const incomplete: Record<string, unknown> = user();
      delete incomplete[field];
      expect(refusalOf(() => UserSchema.parse(incomplete))).toEqual([
        `${field}: Required`
      ]);
    }
  });

  it('does not bound the average here, as the server does not either', () => {
    // Recorded rather than endorsed. Neither `app/schema/user.py` nor this
    // schema constrains the denormalised average to the scale, and making
    // the client stricter than the server on a RESPONSE would refuse a
    // payload the server had already stored. The range check lives in
    // `../../components/ReputationBadge`, which renders its empty state
    // rather than an impossible reputation - so this assertion exists to
    // stop the guard being "moved" here and turned into a rejection.
    expect(UserSchema.parse(user({ ratingAverage: 9 })).ratingAverage).toBe(9);
  });
});

describe('preparing a review for submission', () => {
  it('reduces markup to the text it contained', () => {
    expect(sanitizeUserInput('<b>Great</b> car')).toBe('Great car');
    expect(sanitizeUserInput('<script>alert(1)</script>')).toBe('');
    expect(sanitizeUserInput('plain text')).toBe('plain text');
  });

  it('decodes an entity to the character it stands for', () => {
    expect(sanitizeUserInput('a &amp; b')).toBe('a & b');
  });

  it('reports nothing written as an absent value, not an empty one', () => {
    // `undefined` rather than '' because `review` is OPTIONAL on the wire
    // and the two say different things: omitting the key states that no
    // review was written, while an empty string states one was and is blank.
    expect(prepareReviewText('')).toEqual({
      value: undefined,
      length: 0,
      isOverLimit: false
    });
    expect(prepareReviewText('   ')).toEqual({
      value: undefined,
      length: 0,
      isOverLimit: false
    });
  });

  it('reports a review that was entirely markup as nothing written', () => {
    expect(prepareReviewText('<b></b>').value).toBeUndefined();
  });

  it('sanitises and normalises in the submission path\'s own order', () => {
    // The value a counter shows must be the value that will be sent. This
    // fixture exercises all three steps at once: markup removal, control
    // character and zero-width removal, blank-line collapsing and trimming.
    expect(
      prepareReviewText('  <b>Great</b>\u0007 car\r\n\r\n\r\nreally\u200Bgood  ')
    ).toEqual({
      value: 'Great car\n\nreallygood',
      length: 21,
      isOverLimit: false
    });
  });

  it('counts the prepared review in code points', () => {
    expect(prepareReviewText('\u{1F600}\u{1F600}')).toEqual({
      value: '\u{1F600}\u{1F600}',
      length: 2,
      isOverLimit: false
    });
  });

  it('reports the limit as reached only once it is exceeded', () => {
    expect(prepareReviewText('y'.repeat(REVIEW_MAX_LENGTH))).toMatchObject({
      length: REVIEW_MAX_LENGTH,
      isOverLimit: false
    });
    expect(prepareReviewText('y'.repeat(REVIEW_MAX_LENGTH + 1)))
      .toMatchObject({
        length: REVIEW_MAX_LENGTH + 1,
        isOverLimit: true
      });
  });

  it('reports rather than refuses, so a keystroke never throws', () => {
    // A form needs to explain the problem while the user is still typing;
    // refusing here would force a try/catch around a keystroke.
    expect(() => prepareReviewText('y'.repeat(REVIEW_RAW_MAX_LENGTH + 1)))
      .not.toThrow();
  });

  it('is idempotent, so the measured value is the value validated', () => {
    const once = prepareReviewText('  <b>Great</b> car  ');
    expect(prepareReviewText(once.value as string)).toEqual(once);
  });
});

describe('validating a submission', () => {
  it('returns the three permitted fields and nothing else', () => {
    expect(
      validateRatingInput(submission({ review: 'Straightforward sale' }))
    ).toEqual({
      transactionId: 'test-transaction-000001',
      score: 4,
      review: 'Straightforward sale'
    });
  });

  it('sanitises the review before the bound is applied to it', () => {
    // Order matters, and the previous order was wrong in a way that showed:
    // text was validated and then transformed, so the string the schema
    // approved was not the string that went on the wire.
    expect(
      validateRatingInput(submission({ review: '<b>Great</b> car' })).review
    ).toBe('Great car');
    expect(
      validateRatingInput(submission({ review: 'ok <script>x</script>' }))
        .review
    ).toBe('ok');
  });

  it('strips a spoofed counterparty, direction or publication claim', () => {
    // The structural half of "the server derives, the client does not
    // claim": these keys cannot reach the wire even if a caller puts them
    // in its form state.
    expect(
      Object.keys(
        validateRatingInput(
          submission({
            rateeId: 'a-victim',
            direction: 'seller_to_buyer',
            isPublished: true,
            moderationStatus: 'approved'
          })
        )
      )
    ).toEqual(['transactionId', 'score']);
  });

  it('leaves an absent review absent rather than sending an empty one', () => {
    const parsed = validateRatingInput(submission());
    expect('review' in parsed).toBe(false);
  });

  it('forwards a review that sanitised away to an empty string', () => {
    // Not dropped: the server normalises blank text to null itself, so
    // deciding otherwise here would be this layer inventing a rule.
    expect(validateRatingInput(submission({ review: '<b></b>' })).review)
      .toBe('');
  });

  it('refuses a candidate that is not an object', () => {
    for (const candidate of ['nope', null, 7]) {
      expect(refusalOf(() => validateRatingInput(candidate)).join(''))
        .toContain('Expected object');
    }
  });

  it('refuses a review that is not a string, without sanitising it', () => {
    expect(refusalOf(() => validateRatingInput(submission({ review: 7 }))))
      .toEqual(['review: Expected string, received number']);
  });

  it('propagates every issue, so a form can render each in place', () => {
    // Deliberately not caught, not flattened to a boolean and not wrapped
    // in a result object: the per-field issues are what a form needs.
    const issues = refusalOf(() => validateRatingInput({
      transactionId: '',
      score: 9
    }));
    expect(issues.join('')).toContain('transactionId');
    expect(issues.join('')).toContain('score');
  });

  it('applies the same length rule the counter reports', () => {
    // The relationship this suite exists to pin: a review the counter shows
    // as within the limit can never be refused here for length, and one it
    // shows as over is refused.
    const inside = prepareReviewText('y'.repeat(REVIEW_MAX_LENGTH));
    expect(inside.isOverLimit).toBe(false);
    expect(
      validateRatingInput(submission({ review: inside.value })).review
    ).toHaveLength(REVIEW_MAX_LENGTH);

    const outside = prepareReviewText('y'.repeat(REVIEW_MAX_LENGTH + 1));
    expect(outside.isOverLimit).toBe(true);
    expect(refusalOf(() => validateRatingInput(
      submission({ review: outside.value })
    ))).toEqual([
      `review: A review must be at most ${REVIEW_MAX_LENGTH} characters`
    ]);
  });
});
