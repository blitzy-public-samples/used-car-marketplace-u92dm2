import { z } from 'zod';

/**
 * Zod contracts for the bidirectional peer reputation system.
 *
 * A buyer rates the seller and the seller rates the buyer for a purchase they
 * both took part in, implementing F010 "Review and Rating System" from
 * `documentation/Software Requirements Specifications (SRS).md` (L431,
 * sub-requirements L437-L441). F010-2 written review is carried by
 * `Rating.review`; F010-3 aggregate display by `RatingAggregate` together with
 * `ratingAverage`/`ratingCount` on `./user`; F010-4 moderation by
 * `ModeratedRating`, which is the admin-only shape and deliberately not part of
 * `Rating`, because moderation state is operational and the visibility decision
 * it drives has already been applied to what a reader receives. F010-5
 * search-ranking integration is
 * deliberately out of scope, so no ranking, weight, boost or relevance field
 * appears anywhere below — this module supplies the enabling data and nothing
 * more.
 *
 * THIS IS A MIRROR, NOT A DESIGN
 * -----------------------------------------------------------------------------
 * Every declaration here mirrors `backend/app/schema/rating.py` 1:1, snake_case
 * on the server becoming camelCase on the client, per the project's dual-schema
 * convention. The server is the authoritative validator: because Pydantic
 * validates the request body before any handler logic runs, an out-of-range or
 * non-integer score is refused with a 422 whether or not it ever reached these
 * schemas. What this module adds is a fast, local, identical answer, so a form
 * can refuse an impossible submission without a round trip.
 *
 * The corollary matters as much as the rule: passing validation here guarantees
 * nothing. The two authorization gates — the rater must be a verified user, and
 * both parties must be counterparties of the same transaction — are enforced
 * server-side and are invisible to a schema. A payload that parses cleanly can
 * still legitimately be answered 403 (not verified, or not a participant), 409
 * (already rated, or the transaction is not completed) or 422. Consumers must
 * handle those outcomes rather than treating a successful parse as permission.
 *
 * KEYS ARE camelCase; VALUES ARE NOT
 * -----------------------------------------------------------------------------
 * The wire format is snake_case, because FastAPI serialises the Pydantic models
 * as declared. These schemas are camelCase. Parsing a raw wire payload directly
 * against them therefore FAILS by design, and that is not an oversight: the
 * snake_case <-> camelCase adaptation is owned in both directions by
 * `../services/rating`, which is the single place the boundary is crossed.
 *
 * Consequently this module deliberately contains no snake_case aliases, no
 * duplicate keys, no key-renaming `.transform()`, and no second wire-shaped
 * schema per model. One camelCase schema per model is exactly what the service
 * adapts to, and adding a parallel shape would create two sources of truth for
 * the same document.
 *
 * Enumeration VALUES are the exception that proves the rule. `buyer_to_seller`,
 * `seller_to_buyer`, `pending`, `approved` and `rejected` stay snake_case and
 * lower-case on the client, because they are data travelling over the wire
 * rather than field names. Only keys are re-cased at this boundary.
 *
 * TIMESTAMPS ARE `Date`, AND THE SERVICE PERFORMS THE CONVERSION
 * -----------------------------------------------------------------------------
 * `createdAt` and `updatedAt` are `z.date()`, matching every sibling schema in
 * this folder (`./user`, `./listing`, `./transaction`, `./message`). JSON has no
 * date type, so the wire carries ISO 8601 strings and `../services/rating`
 * converts them to `Date` as part of the same adaptation that re-cases the keys.
 * `z.coerce.date()` was deliberately NOT used: it would accept the raw string
 * and quietly split the responsibility for one boundary across two modules.
 *
 * This is safe rather than merely conventional. The server never emits a null or
 * sentinel timestamp on a response: `backend/app/services/rating.py` stamps a
 * real UTC datetime onto the model it returns from both the create path and the
 * moderation path, precisely because the value actually written to Firestore is
 * a server-side sentinel that cannot be serialised. A response therefore always
 * carries two real timestamps.
 *
 * WHAT IS ABSENT, AND WHY
 * -----------------------------------------------------------------------------
 * There is no `RatingUpdate`, `RatingEdit` or `RatingPatch` schema. Reputation
 * records are append-only: a submitted score is never rewritten, so no shape
 * exists to rewrite one with. A correction is a moderation-state transition that
 * records why, which is what `PATCH /api/ratings/{ratingId}/moderation` performs
 * on the moderation fields alone, leaving the original score and words intact.
 *
 * Nothing here sanitises, and the one transform there is exists to keep the
 * client and the server measuring the same string.
 *
 * `RatingCreateSchema.review` applies `.transform(normalizeReviewText)`, which is
 * a faithful port of the server's own `as_plain_text` normalisation minus its
 * length check: NFC composition, CRLF folding, removal of control and format
 * characters other than newline and tab, collapsing runs of blank lines, and a
 * trim. The bound is then measured against the normalised text, because that is
 * the value the server stores and the value a reader counts. So the value this
 * schema approves IS the value that goes on the wire, and it is what the server
 * would have normalised the input to anyway — the transform cannot make the two
 * disagree, which is the property a length rule split across two layers needs.
 *
 * What it deliberately does NOT do is edit anybody's words. No markup is
 * stripped and no character is escaped: text still carrying a `<` or `>` after
 * normalisation is REFUSED with a message naming the character, exactly as the
 * server refuses it, rather than being quietly rewritten. XSS sanitisation is
 * separate and belongs to the DOMPurify wrapper `sanitizeUserInput` in
 * `../utils/validation`, which the submission path applies before parsing.
 *
 * No response schema transforms at all. A rating read back from the server is
 * validated as received, so the value a reader sees is the value that was
 * validated.
 */

/**
 * Lowest permitted score, inclusive.
 *
 * Mirrors `settings.RATING_MIN` (`backend/app/core/config.py`), which the
 * backend binds into the Pydantic field bound at class-definition time. The 1..5
 * scale is fixed by the project's own success metric — "4.5/5 star average
 * rating from both buyers and sellers" — and is shared with the five options of
 * the star control.
 *
 * "Mirrors" IS A DISCIPLINE, NOT A GUARANTEE, and the difference is worth being
 * precise about. `settings.RATING_MIN` and `settings.RATING_MAX` are ordinary
 * defaulted settings — `1` and `5`, overridable from the environment, with only
 * a coherence check that refuses an inverted pair. Nothing on the server refuses
 * a different scale, and nothing here can detect one: this file is compiled into
 * a separate artefact that cannot read a server environment variable. So an
 * operator who sets `RATING_MAX=4` without editing the line below leaves this
 * control offering a fifth star that the server answers with `422`, and one who
 * widens it to `6` leaves a score no rater can choose. Change the scale on both
 * sides, in the same commit; the value below is the client half of that pair and
 * has no other way to learn about the first.
 *
 * Exported because `../components/StarRatingInput` renders the range and
 * `../components/RatingSubmissionForm` validates against it; one shared constant
 * beats three copies of a magic number.
 *
 * Declared as a bare numeric literal, with no `as const` and no explicit
 * annotation, and both halves of that are deliberate.
 *
 * `as const` is avoided because these are numeric BOUNDS, not discriminants:
 * freezing one into a readonly literal type buys no safety and would make it
 * awkward to use in the arithmetic every consumer performs on it. A bare `const`
 * still carries the widening literal type `1`, which behaves as `number` in every
 * inference position that matters — `useState(RATING_MIN)` infers
 * `number`, and `RATING_MAX - RATING_MIN + 1` is a `number` — so arithmetic and
 * state both work without ceremony.
 *
 * An explicit `: number` annotation was tried and rejected: it is redundant to
 * the compiler and the project's ESLint configuration reports it as an error
 * (`@typescript-eslint/no-inferrable-types`), which the lint gate treats as
 * fatal. Should a consumer ever need the widened type in an explicit generic
 * position, it can write `number` there directly.
 */
export const RATING_MIN = 1;

/**
 * Highest permitted score, inclusive. Mirrors `settings.RATING_MAX`.
 */
export const RATING_MAX = 5;

/**
 * Maximum length of the free-text review, measured AFTER normalisation.
 *
 * Mirrors `settings.RATING_REVIEW_MAX_LENGTH`, which the server applies to the
 * text it has normalised rather than to the bytes that arrived — that normalised
 * value is what gets stored, read back and counted by a reader, so it is the only
 * value the number can honestly describe. `normalizeReviewText` below reproduces
 * that normalisation exactly, so this bound means the same thing on both sides of
 * the wire.
 *
 * This is the SEMANTIC bound, the one that means "2000 characters" to a person
 * writing a review. The server applies the same number to the text it has
 * NORMALISED — Unicode composed, control characters removed, line endings and
 * blank runs collapsed, ends trimmed — which is the text that actually gets
 * stored and read back. So this bound is applied to the normalised text here
 * too, by `normalizeReviewText` below, and the two therefore describe the same
 * thing.
 *
 * That ordering is the whole point and it was previously wrong here: the bound
 * was applied to the RAW text before any normalisation. The claim that
 * "normalisation can only ever shorten text, so anything this accepts the server
 * accepts" is true and is the wrong direction to worry about — the failure was
 * the other way round. Text the server would happily have stored was refused
 * locally, and by exactly the amount normalisation removes, which is largest for
 * the people least able to diagnose it: macOS and iOS emit decomposed (NFD)
 * Unicode, so every accented letter costs two code points raw and one composed,
 * and a reviewer writing in a diacritic-heavy language lost up to half of a
 * stated allowance. Pasted text carrying CRLF line endings, zero-width
 * characters or long runs of blank lines lost the same way.
 *
 * The server also carries a second, far larger ceiling on the raw request body
 * purely to keep an unbounded string away from its normaliser. That IS mirrored,
 * as `REVIEW_RAW_MAX_LENGTH` below, because without it the normaliser here would
 * be handed an unbounded string too.
 *
 * Exported because `../components/RatingSubmissionForm` needs the figure for its
 * live character counter, and a counter that disagrees with the validator is
 * worse than no counter at all.

 */
export const REVIEW_MAX_LENGTH = 2000;

/**
 * Headroom the RAW submitted review is allowed over the bound that actually
 * applies to it, as a multiple of `REVIEW_MAX_LENGTH`.
 *
 * Mirrors `REVIEW_RAW_LENGTH_FACTOR` in `backend/app/schema/rating.py`. Four,
 * because a decomposed character can carry more than one combining mark —
 * Vietnamese reaches three code points for a single letter — so a raw string up
 * to four times the bound can still normalise into range, while anything beyond
 * that is not prose that grew in transit but a payload.
 */
export const REVIEW_RAW_LENGTH_FACTOR = 4;

/**
 * Ceiling on the RAW review text, before normalisation.
 *
 * Mirrors `REVIEW_RAW_MAX_LENGTH` in `backend/app/schema/rating.py` and is
 * derived from the same figure, so the two cannot drift.
 *
 * WHY TWO BOUNDS RATHER THAN ONE, which is the whole point of this pair:
 * applying the semantic bound directly to the raw text — which is what this
 * schema used to do — makes the raw length the binding constraint and refuses
 * submissions the server would have accepted. Every one of these is legitimate
 * and normalises to 2000 characters or fewer: text typed on macOS or iOS, which
 * emits decomposed (NFD) Unicode so each accented letter costs two code points
 * until it is composed; text pasted with CRLF line endings, zero-width
 * characters or trailing whitespace; and text with long runs of blank lines that
 * collapse. A reviewer writing in a diacritic-heavy language would have lost
 * roughly half of a stated allowance, and the client and server would have
 * disagreed about what "2000 characters" means.
 *
 * This ceiling is the denial-of-service guard the server also carries: it exists
 * only so an unbounded string never reaches the normaliser.
 */
export const REVIEW_RAW_MAX_LENGTH =
  REVIEW_RAW_LENGTH_FACTOR * REVIEW_MAX_LENGTH;

/**
 * Control and format characters, which prose cannot legitimately contain.
 *
 * The Unicode general categories `Cc` (control) and `Cf` (format), matching the
 * server's `unicodedata.category(character) not in ('Cc', 'Cf')` test exactly.
 * Newline and tab are excluded from removal by the caller rather than by this
 * pattern, mirroring the server's `_ALLOWED_CONTROL_CHARACTERS`.
 */
const CONTROL_OR_FORMAT_CHARACTER = /\p{Cc}|\p{Cf}/u;

/**
 * Reduce author-supplied review text to the value the server will store.
 *
 * A faithful port of `as_plain_text` in `backend/app/schema/rating.py`, minus its
 * length check, which the schema below applies instead. It exists so that client
 * and server MEASURE THE SAME STRING: the server bounds the normalised text, so
 * a client bounding the raw text would accept and reject different submissions
 * than the server does, in both directions.
 *
 * The five steps, in the server's order, because order changes the result:
 *
 *   1. Compose Unicode (NFC), so visually identical strings count the same.
 *   2. Fold `\r\n` and bare `\r` to `\n`.
 *   3. Drop control and format characters other than newline and tab. They are
 *      invisible in a review yet break log lines, terminal output and CSV
 *      exports.
 *   4. Collapse runs of three or more newlines to two. `\n{3,}` in one pass is
 *      equivalent to the server's repeated three-to-two replacement: both leave
 *      a run of exactly two, whatever its original length.
 *   5. Trim surrounding whitespace.
 *
 * This is NOT a sanitiser: it removes what prose cannot contain and composes what
 * is equivalent, and it neither strips markup nor escapes anything. XSS is
 * separately handled by `sanitizeUserInput` in `../utils/validation`.
 *
 * It is used twice, for one purpose. `RatingCreateSchema.review` applies it as a
 * `.transform()`, so the value that reaches the wire is the normalised text, and
 * the bound below is then measured on that same text. Both uses exist so client
 * and server MEASURE AND STORE THE SAME STRING — the server normalises the copy
 * it persists identically, so the transform cannot change what a submission means
 * and cannot make the accepted value differ from the stored one. A reader
 * measuring the raw text instead would be charged for characters the server
 * removes.
 *
 * @param value Raw review text as authored.
 * @returns The normalised text, which may be empty when the input carried
 *   nothing but whitespace and invisible characters.
 */
export const normalizeReviewText = (value: string): string => {
  const composed = value
    .normalize('NFC')
    .replace(/\r\n/g, '\n')
    .replace(/\r/g, '\n');

  const printable = Array.from(composed)
    .filter(
      (character) =>
        character === '\n' ||
        character === '\t' ||
        !CONTROL_OR_FORMAT_CHARACTER.test(character)
    )
    .join('');

  return printable.replace(/\n{3,}/g, '\n\n').trim();
};

/**

/**
 * The characters the server refuses in stored review text.
 *
 * Mirrors `_MARKUP_CHARACTERS` in `backend/app/schema/rating.py`. Without a `<`
 * or a `>` no element, comment, CDATA section or processing instruction can be
 * formed from the stored value in HTML, XHTML, XML or SVG — which is what makes
 * the server's plain-text guarantee a property of the data rather than a hope
 * about its consumers.
 *
 * Mirrored here so the refusal is reported next to the textarea that produced
 * it, rather than arriving as a remote 422 after the user has pressed submit.
 * `&`, quotes and apostrophes are deliberately absent, exactly as on the server:
 * an entity reference decodes to text and can never form an element on its own,
 * and all three are ordinary punctuation in a review of a car.
 */
const REVIEW_TAG_DELIMITERS = /[<>]/;

/**
 * Count Unicode CODE POINTS, with no normalisation of any kind.
 *
 * The primitive `textLength` is built on, and the correct measure for a value the
 * server bounds WITHOUT normalising it — an identifier rather than prose.
 * Pydantic's `constr(max_length=…)` measures `len(value)` on the string exactly
 * as received and Python's `len` counts code points, so a bound written with
 * Zod's `.max()` — which counts UTF-16 CODE UNITS — is a different, stricter rule
 * for any text outside the Basic Multilingual Plane: a 128-code-point identifier
 * of astral characters measures 256 by `.length` and would be refused here while
 * the server accepted it. A client silently overriding the authoritative rule is
 * the failure mode, and this function is how it is avoided.
 *
 * `Array.from` iterates by code point (`String.prototype[Symbol.iterator]` yields
 * whole surrogate pairs), which is what makes the count correct rather than
 * approximately correct.
 *
 * Kept as its own function rather than folded into `textLength`, because NFC
 * composition is right for prose and wrong for an identifier: the server composes
 * a review before measuring it and does not compose a document ID at all, so
 * measuring an ID after composition would count fewer code points than the rule
 * being mirrored and would accept a value the server refuses.
 *
 * @param text Text exactly as received.
 * @returns The number of Unicode code points.
 */
export const codePointLength = (text: string): number => Array.from(text).length;

/**
 * Measure author-supplied text the way the server measures it: NFC-composed
 * Unicode code points.
 *
 * This is a contract detail, not a refinement. The server bounds a review with
 * `len(unicodedata.normalize('NFC', text))`, and Python's `len` counts CODE
 * POINTS, whereas JavaScript's `String.prototype.length` counts UTF-16 CODE
 * UNITS. The two disagree on exactly the text people actually write:
 *
 *   - every emoji and every character outside the Basic Multilingual Plane is
 *     one code point stored as a surrogate PAIR, so `'🙂'.length === 2`. A review
 *     of 1200 emoji measures 2400 by `.length` and would be refused locally
 *     while the server would accept it — the client silently overriding the
 *     authoritative rule.
 *   - decomposed scripts shrink under NFC: `'e' + '\u0301'` is two code points
 *     until it is composed into the single `'é'`. Measuring before composing
 *     therefore over-counts text the server will store as shorter.
 *
 * Only NFC composition is applied here. The server additionally strips control
 * characters and collapses blank runs before measuring, both of which can only
 * ever SHORTEN the text — so this measurement is never smaller than the server's,
 * and a review this length rule accepts can never be one the server's bound
 * rejects for length. That direction is the safe one, and it is the reason the
 * rest of the normalisation is deliberately not reimplemented client-side: this
 * module rewrites nobody's words.
 *
 * `Array.from` iterates by code point (`String.prototype[Symbol.iterator]`
 * yields whole surrogate pairs), which is what makes the count correct rather
 * than approximately correct. Exported because the same figure has to drive the
 * live character counter in `../components/RatingSubmissionForm`; a counter that
 * counts differently from the validator is worse than no counter at all.
 *
 * @param text Raw text exactly as authored.
 * @returns The number of NFC-composed Unicode code points.
 */
export const textLength = (text: string): number =>
  codePointLength(text.normalize('NFC'));

/**
 * A string bounded by the server's length rule rather than by `.length`.
 *
 * Zod's own `.max()` measures UTF-16 code units, so it is deliberately not used
 * for author-supplied prose: it would reject valid emoji and decomposed-script
 * text the server accepts (see `textLength`). A refinement is the only way to
 * express the rule, because the count has to happen after NFC composition.
 *
 * Used for the fields of author-supplied prose on a rating - the public review
 * and the moderator's recorded reason - because the server applies the same
 * normalise-then-count function to both, differing only in the bound and in the
 * name it uses in the error message.
 *
 * @param maxLength Maximum number of NFC-composed code points.
 * @param message Message rendered when the bound is exceeded, written for the
 *   person holding the keyboard rather than for a developer.
 */
const boundedText = (maxLength: number, message: string) =>
  z.string().refine((value) => textLength(value) <= maxLength, { message });


/**
 * Maximum length of a moderator's recorded reason.
 *
 * Mirrors `MODERATION_REASON_MAX_LENGTH` in `backend/app/schema/rating.py`. It
 * is deliberately far smaller than the review bound: a policy citation is a
 * short phrase such as "contains a phone number", written by staff rather than
 * by the public.
 *
 * Module-local rather than exported. No component consumes the figure — the
 * moderation surface is API-only in this release — and an export with no
 * consumer is public surface area that has to be maintained for nothing.
 */
const MODERATION_REASON_MAX_LENGTH = 500;

/**
 * Maximum length of any single Firestore document ID this feature handles.
 *
 * Mirrors `DOCUMENT_ID_MAX_LENGTH` in `backend/app/schema/rating.py`, which
 * holds a document ID well inside Firestore's own limit because two IDs are
 * joined to form a rating's key, so each half must leave room for the other. A
 * Firestore auto-ID occupies twenty characters, so this is generous.
 *
 * Exported because it bounds more than one identifier: a transaction reference
 * on the way to submission, and the seller ID `../utils/validation` reads off a
 * listing before spending a request on it. Both are document IDs and both are
 * bounded by the server's single constant, so they mirror it once here rather
 * than twice.
 */
export const DOCUMENT_ID_MAX_LENGTH = 128;

/**
 * Maximum length of a transaction reference accepted for submission.
 *
 * A transaction reference IS a document ID, so this is that bound under the name
 * the submission schema reads it by. Two names for one number would be two
 * things to keep in step with the server.
 */
const TRANSACTION_ID_MAX_LENGTH = DOCUMENT_ID_MAX_LENGTH;

/**
 * Path-safety grammar for a transaction reference supplied by a client.
 *
 * An EXACT mirror of the backend's `DocumentId` constraint
 * (`_DOCUMENT_ID_PATTERN` in `backend/app/schema/rating.py`), because this is
 * the one value in this whole module that travels from a client INTO a document
 * path — `transactionId` on a submission addresses the transaction the server
 * reads and, composed with the rater's ID, the rating it creates.
 *
 * The four rules, and each one's reason, are the server's own:
 *
 *   - `(?!\.\.?$)` — neither `.` nor `..`, which Firestore reserves as relative
 *     path segments rather than identifiers.
 *   - `(?!__.*__$)` — not Firestore's reserved `__*__` namespace.
 *   - `[^/]+` — at least one character, and no forward slash: `document('a/b')`
 *     is read as a nested path, so it addresses something else entirely or
 *     raises.
 *   - no ASCII control character — checked by `hasControlCharacter` below rather
 *     than by this pattern. Nothing in this system legitimately produces one, and
 *     a newline inside an identifier is how a log line gets forged.
 *
 * WHAT THIS PATTERN DELIBERATELY DOES NOT DO IS REJECT A SPACE. That is the
 * correction that matters, and getting it backwards is easy: the previous
 * `/^[^/\s]+$/` refused every whitespace character, while the server's character
 * class excludes only control characters — so `U+0020` is a legal document ID on
 * the server. A narrower client rule is the one genuinely dangerous asymmetry
 * available here, because it refuses a reference the server would have accepted
 * and blocks a rating that was actually permitted, with a local rule silently
 * overriding the authoritative one. Mirroring exactly removes the question.
 *
 * The empty case is the one most likely to occur in practice rather than in
 * theory — an unresolved route parameter stringifies to nothing, and a request
 * built from it would otherwise be sent and refused remotely instead of being
 * refused here with something a form can display.
 *
 * The grammar MIRRORS the server's exactly, and the asymmetry it used to carry is
 * worth recording because it was backwards from what its own comment claimed.
 * The pattern was `/^[^/\s]+$/`, described as "deliberately a little WIDER than
 * the server's" — and `\s` made it NARROWER in the one way that matters: the
 * backend's grammar excludes only `/` and ASCII control characters, so a
 * transaction reference containing an ordinary space is perfectly acceptable to
 * it, and this pattern refused it. That is a local rule silently overriding the
 * authoritative one, blocking a rating the server would have permitted.
 *
 * The direction of the error is what makes it worth fixing rather than merely
 * documenting. Being wider than the server costs at most a remote 422 on a value
 * no legitimate caller sends. Being narrower blocks a legitimate rating with no
 * server involvement at all, so nothing in a log ever shows it happening.
 *
 * The four encoded rules are the backend's own, in the same order: at least one
 * character, not `.` or `..`, not inside Firestore's reserved `__*__` namespace,
 * and no `/` or ASCII control character.
 *
 * It is a predicate rather than one regular expression on purpose. The control
 * range can only be written into a character class as literal control escapes,
 * which `no-control-regex` reports — correctly, since a control character inside
 * a pattern is nearly always a mistake rather than an intention. Testing the code
 * points directly says the same thing in a form that reads as deliberate, needs
 * no rule suppression, and names in one place the two ranges the server excludes.
 * `\u007F` is DEL and `\u0000`-`\u001F` are the C0 controls, which include the
 * newline that log forgery depends on.
 */
const RESERVED_DOCUMENT_IDS = /^\.\.?$/;

const RESERVED_DOCUMENT_ID_NAMESPACE = /^__.*__$/;

const isPathSafeDocumentId = (value: string): boolean => {
  if (value.length === 0) {
    return false;
  }

  if (RESERVED_DOCUMENT_IDS.test(value) ||
      RESERVED_DOCUMENT_ID_NAMESPACE.test(value)) {
    return false;
  }

  for (let index = 0; index < value.length; index += 1) {
    const code = value.charCodeAt(index);

    // 0x2F is '/', which the Firestore client reads as path structure
    // rather than as part of an ID; 0x00-0x1F and 0x7F are the ASCII
    // control characters.
    if (code === 0x2f || code <= 0x1f || code === 0x7f) {
      return false;
    }
  }

  return true;
};


/**
 * Which way along a transaction a rating travels — the R0 bidirectionality
 * discriminator, and the reason this feature is a peer reputation system rather
 * than a seller review system.
 *
 * Modelled with `z.enum` rather than `z.string()` so the inferred type is a
 * union of literals. That is the whole point: a consumer switching on direction
 * gets exhaustiveness checking and narrowing, and a typo becomes a compile
 * error instead of a branch that silently never runs.
 *
 * The value is derived SERVER-SIDE from the cited transaction document and is
 * never accepted from a client — which is why it is absent from
 * `RatingCreateSchema`. That absence is what makes direction spoofing
 * structurally impossible rather than merely validated against.
 */
export const RatingDirectionSchema = z.enum([
  'buyer_to_seller',
  'seller_to_buyer'
]);

export type RatingDirection = z.infer<typeof RatingDirectionSchema>;

/**
 * Policy state governing whether a review's text may be displayed.
 *
 * Exactly three states, and none of them is derived from the score. Transitions
 * are driven by policy violations only — abuse, personally identifying
 * information, profanity — and never by how low a rating is. A one-star rating
 * is not a violation.
 *
 * That constraint is not stylistic, which is why no fourth member and no
 * score-correlated field exists anywhere in this module: the FTC Rule on the Use
 * of Consumer Reviews and Testimonials (16 CFR Part 465) prohibits suppressing
 * reviews on the basis of rating or negative sentiment. The aggregate
 * consequently counts every published rating whatever its value, and the score
 * is shown for every published rating regardless of moderation state — it is the
 * free-text review, the only part a policy violation can live in, that is
 * withheld until `approved`.
 */
export const ModerationStatusSchema = z.enum([
  'pending',
  'approved',
  'rejected'
]);

export type ModerationStatus = z.infer<typeof ModerationStatusSchema>;

/**
 * One directional rating, as RETURNED by the API.
 *
 * Mirrors `RatingView` in `backend/app/schema/rating.py` — the server's response
 * projection — field for field, and deliberately NOT the persisted `Rating`
 * model behind it. Two differences follow from that, and both were defects here
 * until the server grew a response model of its own:
 *
 * `moderationStatus` and `moderationReason` are ABSENT. They are operational
 * state, written for the people who run the platform, and no endpoint a normal
 * caller reaches returns them: the visibility decision they drive has already
 * been applied to what arrives, so a withheld review simply comes back with
 * `review: null`. They live on `ModeratedRatingSchema` below, which only the
 * admin-only moderation response uses. Nothing in this application's interface
 * reads either field, so removing them from the general contract costs nothing
 * and stops the moderation queue being published to every reader.
 *
 * `createdAt` and `updatedAt` are REQUIRED dates, and now safely so. The
 * persisted model types both `Optional[Any]` because its write path assigns a
 * Firestore server-side sentinel; the response model requires a real `datetime`,
 * so a response carrying null is no longer constructible server-side. Requiring
 * them here previously meant this client would throw on a response the server
 * was entitled to emit — the worst kind of contract breach, because both sides
 * were behaving as specified.
 *
 * `review` is NULLABLE rather than optional, and that distinction is
 * load-bearing. The server returns the key always, carrying `null` when there is
 * no value, so modelling it as a bare `z.string()` would reject the commonest
 * rating there is — one with no written review. It is also null whenever
 * moderation has not approved the text.
 */
export const RatingSchema = z.object({
  /** Document ID, composed server-side as `{transactionId}_{raterId}`. */
  id: z.string(),
  /** The transaction whose participation authorises this rating. */
  transactionId: z.string(),
  /**
   * Denormalised from the transaction so a rating can be rendered with its
   * context — "rated after buying this car" — without a second document read.
   */
  vehicleListingId: z.string(),
  /** Author of the rating. Always the authenticated caller, server-side. */
  raterId: z.string(),
  /** Recipient. Derived server-side as the transaction's other participant. */
  rateeId: z.string(),
  direction: RatingDirectionSchema,
  /**
   * The vote. An integer within the inclusive scale — never a fraction, and
   * never a rounded one. The server is strict about this for a security reason
   * rather than a tidiness one: a coercing validator would turn 4.7 into 4 and
   * record a score the rater never chose.
   */
  score: z.number().int().min(RATING_MIN).max(RATING_MAX),
  /**
   * Free-text review (F010-2), or null when unwritten or withheld.
   *
   * Bounded through `boundedText`, so the length rule is the server's — NFC-
   * composed code points — rather than UTF-16 code units. A response carrying a
   * review of 1200 emoji is valid data and must parse.
   */
  review: boundedText(
    REVIEW_MAX_LENGTH,
    `A review must be at most ${REVIEW_MAX_LENGTH} characters`,
  ).nullable(),

  /**
   * Whether this rating is visible and counted.
   *
   * Ratings are created unpublished. Under the double-blind reveal a rating
   * becomes visible only once the counterparty submits theirs or the rating
   * window elapses, and the aggregate reflects published ratings only — which is
   * what removes the incentive for review extortion. This is a first-class
   * stored field rather than something a client can infer, and an unpublished
   * rating is recorded rather than lost: a submission that is not yet visible
   * must be reported to its author as exactly that, never as a silent failure.
   */
  isPublished: z.boolean(),

  createdAt: z.date(),
  updatedAt: z.date()
});

export type Rating = z.infer<typeof RatingSchema>;

/**
 * One rating as the ADMINISTRATOR's moderation response returns it.
 *
 * Mirrors `ModeratedRatingView` in `backend/app/schema/rating.py`:
 * `RatingSchema` plus the moderation fields. Returned by
 * `PATCH /api/ratings/{ratingId}/moderation` and by nothing else, because that
 * endpoint is gated on `role === 'admin'` and its caller just set the state they
 * are reading back.
 *
 * `moderationReason` is the policy basis a moderator recorded: prose describing
 * the violation in the review CONTENT — abuse, personally identifying
 * information, profanity — and never anything derived from the score. The server
 * permits it ONLY beside `rejected`, and requires it there, so this pair can
 * never arrive as a displayed review carrying a violation against it. Nullable
 * rather than optional because the server always sends the key, with `null` on
 * every state that displays the review.
 */
export const ModeratedRatingSchema = RatingSchema.extend({
  moderationStatus: ModerationStatusSchema,
  /*
   * `boundedText` rather than `.max()`, exactly as the review above and for the
   * same reason: the server bounds this through `as_plain_text(…,
   * max_length=MODERATION_REASON_MAX_LENGTH)`, which measures NFC-composed code
   * points, while Zod's `.max()` measures UTF-16 code units. This field arrives
   * on a RESPONSE, so the mismatch would reject a payload the server had already
   * validated and stored — a moderator's reason containing an emoji would report
   * as a contract failure to the administrator who had just written it.
   */
  moderationReason: boundedText(
    MODERATION_REASON_MAX_LENGTH,
    `A moderation reason must be at most ${MODERATION_REASON_MAX_LENGTH} characters`
  ).nullable()
});

export type ModeratedRating = z.infer<typeof ModeratedRatingSchema>;

/**
 * The request body accepted by `POST /api/ratings`.
 *
 * Three keys, written out literally, and no more. This is the most important
 * shape in the module and its narrowness is the point.
 *
 * `raterId`, `rateeId` and `direction` are absent BY DESIGN. The rater is the
 * authenticated caller and the other two are derived server-side from the cited
 * transaction, where the counterparty is computed as "the seller if the caller
 * is the buyer, otherwise the buyer". Because they are derived rather than
 * accepted, there is no input a client can supply that produces a self-rating or
 * redirects a rating to a third party — counterparty spoofing is structurally
 * impossible instead of merely validated against. Never add them here for
 * convenience or for symmetry with `RatingSchema`.
 *
 * That is also why this schema is declared standalone rather than derived from
 * `RatingSchema` with `.pick()`, `.omit()`, `.partial()`, `.extend()` or
 * `.merge()`. Every one of those leaves the forbidden values reachable on the
 * inferred type — as optional or as never-quite-removed members — which defeats
 * the guarantee. Three literal keys cannot drift into carrying a fourth.
 *
 * The schema is intentionally NOT `.strict()`. Zod's default object behaviour is
 * to strip unrecognised keys, so parsing an over-supplied object through this
 * schema yields exactly the three permitted fields and a stray `rateeId` never
 * reaches the wire at all. Refusing the object instead would trade a guarantee
 * for an error, and the guarantee is worth more: the server tolerates a supplied
 * `ratee_id` only so that it can compare the claim against the derived
 * counterparty and answer 403, and the best outcome is that the claim is never
 * sent. Note that the server refuses every OTHER unrecognised key with a 422, so
 * stripping here also keeps a caller's mistake from becoming a rejected request.
 *
 * `review` is `.optional()` here while it is `.nullable()` on `RatingSchema`. The
 * asymmetry is correct and intentional: a client simply omits the key when there
 * is nothing to say, whereas the server always returns the key and uses null to
 * say the same thing. Do not harmonise them.
 */
export const RatingCreateSchema = z.object({
  /**
   * The transaction being rated. Bounded rather than a bare string because this
   * is the one client-supplied value that constructs a document path; see
   * `isPathSafeDocumentId`.
   */
  transactionId: z
    .string()
    .min(1, 'A transaction reference is required')
    /*
     * A CODE-POINT bound, not Zod's `.max()`, for the same reason the review's
     * bound is a refinement: the server's `DocumentId` is
     * `constr(max_length=128)`, which measures Python's `len` — code points —
     * while `.max()` measures UTF-16 code units. The two disagree on any
     * identifier containing an astral character, where a 128-code-point value
     * measures 256 by `.length`, and the disagreement runs the wrong way: this
     * layer would refuse a reference the server accepts, so a transaction whose
     * ID happened to contain one could never be rated from this client at all.
     * `codePointLength` rather than `textLength` because the server does not
     * normalise an ID before measuring it, so neither may this.
     */
    .refine(
      (value) => codePointLength(value) <= TRANSACTION_ID_MAX_LENGTH,
      {
        message: `A transaction reference must be at most ${TRANSACTION_ID_MAX_LENGTH} characters`
      }
    )
    .refine(isPathSafeDocumentId, {
      message:
        'A transaction reference must not contain "/" or a control character, ' +
        'and must not be ".", ".." or of the form "__name__"'

    }),
  score: z
    .number()
    .int('A rating must be a whole number of stars')
    .min(RATING_MIN, `A rating must be at least ${RATING_MIN}`)
    .max(RATING_MAX, `A rating must be at most ${RATING_MAX}`),
  /**
   * The free-text review, validated exactly as the server validates it.
   *
   * Four steps, in the server's own order, and the order is the contract:
   *
   * 1. The RAW ceiling, so an unbounded string never reaches the normaliser.
   *    Not the number a counter shows; a denial-of-service guard.
   * 2. Normalisation, which is what produces the text the server will store.
   * 3. The tag-delimiter refusal, so the plain-text guarantee holds. Refused
   *    rather than stripped: silently editing somebody's review is worse than
   *    telling them which character to remove.
   * 4. The SEMANTIC bound, measured against the normalised text — the only
   *    value the limit can honestly describe, and the value a reader counts.
   *
   * Applying step 4 before step 2, which is what this schema used to do, made a
   * local rule refuse text the server would have accepted. See
   * `REVIEW_MAX_LENGTH`.
   *
   * The transform means the value that reaches the wire is the NORMALISED text,
   * which is also what the server would have normalised it to — so what the user
   * is shown as accepted is exactly what gets stored.
   */
  review: z
    .string()
    .max(
      REVIEW_RAW_MAX_LENGTH,
      `A review must be at most ${REVIEW_RAW_MAX_LENGTH} characters before formatting is removed`
    )
    .transform(normalizeReviewText)
    .refine((text) => !REVIEW_TAG_DELIMITERS.test(text), {
      message: 'A review must be plain text and must not contain "<" or ">"'
    })
    .refine((text) => textLength(text) <= REVIEW_MAX_LENGTH, {
      message: `A review must be at most ${REVIEW_MAX_LENGTH} characters`
    })
    .optional()

});

export type RatingCreate = z.infer<typeof RatingCreateSchema>;

/**
 * Denormalised reputation summary for a single user (F010-3).
 *
 * Mirrors `ratingAverage`/`ratingCount` on `./user`, which the server maintains
 * inside the same transaction that publishes a rating so that reading a profile
 * costs one document read rather than a scan of every rating a user received.
 *
 * `average` is NULLABLE, and null is a first-class state meaning "no ratings
 * yet". It is not an error and it must never be defaulted to 0:
 * `../components/ReputationBadge` renders an explicit empty state off exactly
 * this null, and a zero would instead render as a genuine, earned one-star
 * reputation — the worst possible thing to show a user who has simply never been
 * rated.
 *
 * THE INVARIANTS ARE MIRRORED, NOT ASSUMED
 * -----------------------------------------------------------------------------
 * `average: z.number().nullable(), count: z.number().int()` was the whole schema
 * here, and it admitted four impossible reputations, every one of which would
 * have been RENDERED TO A USER AS FACT about another person:
 *
 * - a negative `count` ("−3 reviews");
 * - a fractional count, which `z.number().int()` does catch, but which paired
 *   with the others made the set inconsistent;
 * - `NaN` or `Infinity` as the average — both survive `z.number()`, and `NaN`
 *   reaches `toFixed` as the literal text "NaN";
 * - an average outside the 1..5 scale, so "8.0/5";
 * - and the two cross-field contradictions: a positive count with a null
 *   average, or an average with a count of zero.
 *
 * `RatingAggregate` in `backend/app/schema/rating.py` refuses all of them, and
 * the reason to refuse them here too is not distrust of the server. It is that
 * this schema is the last point at which a wrong figure is still a legible
 * validation error rather than a confident-looking number on a profile page. The
 * earlier comment argued that "a client that refused to display a server-computed
 * figure would turn a rounding artefact into a broken profile page" — but none of
 * these is a rounding artefact, and the 1..5 bound is exactly the range a rounded
 * mean of 1..5 scores can occupy.
 *
 * `average` stays NULLABLE, and null is a first-class state meaning "no ratings
 * yet". It is not an error and it must never be defaulted to 0:
 * `../components/ReputationBadge` renders an explicit empty state off exactly
 * this null, and a zero would instead render as a genuine, earned one-star
 * reputation — the worst possible thing to show a user who has simply never been
 * rated.

 *
 * Reflects published ratings only, and includes every one of them whatever the
 * score.
 */
export const RatingAggregateSchema = z
  .object({
    average: z
      .number()
      .finite('A rating average must be a finite number')
      .min(RATING_MIN, `A rating average must be at least ${RATING_MIN}`)
      .max(RATING_MAX, `A rating average must be at most ${RATING_MAX}`)
      .nullable(),
    count: z
      .number()
      .int('A rating count must be a whole number')
      .nonnegative('A rating count cannot be negative')
  })
  .superRefine((aggregate, context) => {
    // Cross-field, so it cannot live on either member: `superRefine` runs only
    // once both have parsed, and reports against the field that is wrong
    // relative to the other rather than against the object as a whole.
    if (aggregate.count > 0 && aggregate.average === null) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ['average'],
        message: 'A positive rating count requires an average'
      });
    }

    if (aggregate.count === 0 && aggregate.average !== null) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ['count'],
        message: 'An average requires a positive rating count'
      });
    }
  });


export type RatingAggregate = z.infer<typeof RatingAggregateSchema>;

/**
 * Outcome of the rating eligibility guard sequence.
 *
 * Returned by `GET /api/ratings/eligibility/{transactionId}` so the interface
 * can disable the submission control with a specific explanation, instead of
 * letting someone compose a rating and only then fail on a 403. Reporting the
 * reason up front is what makes an unavailable control perceivable rather than
 * merely inert, which is an accessibility obligation and not a nicety.
 *
 * `reason` is PROSE, rendered verbatim. It holds sentences composed by the
 * server — "Only verified users can submit ratings", "You have already rated
 * this transaction", "Ratings require a completed transaction" — and it is
 * deliberately not a `z.enum` of codes the client maps to its own copy. Two
 * sources of wording for one decision drift apart, and the server already needs
 * the sentence for its own error detail. It is nullable because there is nothing
 * to explain when the caller is eligible.
 *
 * `rateeId` and `direction` are nullable because the server can only derive them
 * once the caller is confirmed a participant; a decision that never got that far
 * — an unknown transaction, or a caller who is party to neither side — reports
 * them as null. Modelling them as nullable is what lets a form render the
 * disabled-with-reason state instead of failing on absent data.
 */
export const EligibilityDecisionSchema = z
  .object({
    eligible: z.boolean(),
    reason: z.string().nullable(),
    rateeId: z.string().nullable(),
    direction: RatingDirectionSchema.nullable(),
    /**
     * Whether this caller has already rated this transaction. Distinct from
     * `eligible`, because "you have had your say" and "you were never entitled to
     * one" are different states that a profile or a form should not conflate.
     */
    alreadyRated: z.boolean()
  })
  .superRefine((decision, context) => {
    /*
     * THE FIELDS ARE NULLABLE INDIVIDUALLY AND CONSTRAINED TOGETHER.
     *
     * Every field above is satisfiable on its own by a decision that cannot
     * exist, and each impossible combination would be acted on by the submission
     * form as though it were an answer. `evaluate_eligibility` in
     * `backend/app/services/rating.py` returns from exactly four places, so the
     * three rules below are what it can actually produce:
     *
     *   - an ELIGIBLE decision is only reached after the counterparty has been
     *       derived, so `rateeId` and `direction` are both present, and it is
     *       unreachable once a rating exists, so `alreadyRated` is false. A
     *       payload claiming eligibility without a counterparty would let the
     *       form tell someone they may rate while being unable to say whom.
     *   - an INELIGIBLE decision always carries the refusal's own message as
     *       `reason`, because the server composes it from the very exception the
     *       write path would raise. Without it the form disables its control
     *       with no explanation, which is the accessibility failure the endpoint
     *       exists to prevent.
     *   - `alreadyRated` implies ineligible, since having rated is one of the
     *       reasons a caller is refused.
     *
     * Refused here rather than smoothed over, because a malformed decision is
     * the one input this form cannot recover from: it gates a write the server
     * will refuse anyway, and the user is told nothing useful either way.
     */
    if (decision.eligible) {
      if (decision.rateeId === null) {
        context.addIssue({
          code: z.ZodIssueCode.custom,
          path: ['rateeId'],
          message:
            'An eligible decision must name the counterparty being rated'
        });
      }

      if (decision.direction === null) {
        context.addIssue({
          code: z.ZodIssueCode.custom,
          path: ['direction'],
          message: 'An eligible decision must carry the rating direction'
        });
      }

      if (decision.alreadyRated) {
        context.addIssue({
          code: z.ZodIssueCode.custom,
          path: ['alreadyRated'],
          message:
            'A caller who has already rated this transaction cannot be eligible'
        });
      }

      return;
    }

    if (decision.reason === null) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ['reason'],
        message: 'An ineligible decision must explain why'
      });
    }
  });

export type EligibilityDecision = z.infer<typeof EligibilityDecisionSchema>;

/**
 * Payload of `GET /api/ratings/user/{userId}` — the ratings a user has received,
 * together with the aggregate computed from them.
 *
 * Two keys, mirroring the server's `UserRatingsResponse` exactly. Page metadata
 * was mirrored here and removed again: it widened a published contract that is
 * two fields, and the cursor behind it let any rating ID be handed back to a
 * public read and its existence inferred from the answer.
 *
 * The two halves travel together because they are established from ONE pinned
 * snapshot on the server, so the envelope cannot contradict itself. Published
 * ratings only, so an unpublished rating appears in neither.
 *
 * `items` is bounded and omits any rating whose review moderation rejected,
 * while `aggregate` counts every published rating whatever its score and state.
 * So `aggregate.count` may legitimately exceed `items.length`. That is the
 * contract, not a discrepancy, and it must not be rendered as one — in
 * particular `aggregate.average` is never derived from `items`.
 */
export const UserRatingsResponseSchema = z.object({
  items: z.array(RatingSchema),
  aggregate: RatingAggregateSchema
});

export type UserRatingsResponse = z.infer<typeof UserRatingsResponseSchema>;
