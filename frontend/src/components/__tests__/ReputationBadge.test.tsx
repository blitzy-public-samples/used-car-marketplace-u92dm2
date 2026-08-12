/**
 * Component tests for `../ReputationBadge`, the aggregate reputation display of
 * the bidirectional peer rating system.
 *
 * The component implements F010-3, "Display of aggregate ratings on user
 * profiles" (`documentation/Software Requirements Specifications (SRS).md`
 * L439), and it mounts in two places where a reader draws a conclusion from it:
 * a user's own profile page, and the seller block of a listing where a
 * prospective buyer is deciding whether to transact at all. Getting it wrong is
 * therefore not a cosmetic bug — it misrepresents a person's reputation.
 *
 * WHAT THIS SUITE PROTECTS
 * -----------------------------------------------------------------------------
 * Two behaviours carry essentially all of the risk, and both are asserted here
 * rather than assumed:
 *
 *   1. `average: null` means "never been rated", NOT "rated zero". The empty
 *      state must render the component's own wording and must never render a
 *      score. A regression that defaulted `null` to `0` would present an
 *      unrated user as having earned a genuine one-star reputation — the single
 *      worst thing this component could do — and a suite that only checked the
 *      populated path would not notice. Hence the explicit negative assertions.
 *
 *   2. The score is rendered through the REAL `formatRating`, so a whole number
 *      shows as `4.0` and never as a bare `4`, `4.56` shows as `4.6`, and the
 *      maximum comes from `RATING_MAX` rather than from a `5` written here. All
 *      three are pinned below, against the production function rather than
 *      against a copy of it, and nothing in this file stubs the formatter — see
 *      the note on the module graph.
 *
 * A third property is asserted because it is a legal constraint rather than a
 * preference: presentation is SENTIMENT-NEUTRAL. The FTC Rule on the Use of
 * Consumer Reviews and Testimonials (16 CFR Part 465) prohibits suppressing
 * reviews on the basis of their rating or of negative sentiment, so a low
 * average must be displayed exactly as fully as a high one. Two tests guard it
 * at different depths: `renders a low average as completely as a high one`
 * catches outright suppression, and `presents a low average identically to a
 * high one` catches everything subtler — a conditional class, a colour or
 * opacity change, an added warning — by comparing a low render against a high
 * one as markup.
 *
 * ACCESSIBILITY IS ASSERTED THROUGH THE QUERIES THEMSELVES
 * -----------------------------------------------------------------------------
 * WCAG 2.1 Level AA is a stated requirement (SRS L187, L556). This component is
 * a non-interactive display, so there is no keyboard obligation to verify, but
 * the score must be legible as TEXT and must not be conveyed by colour or shape
 * alone. Every assertion about what the component SHOWS therefore goes through a
 * text query — never through a class name, an inline style or a `data-testid` —
 * which is precisely what proves the value reaches a screen reader as words. The
 * decorative star is separately asserted to be hidden from assistive technology,
 * so it adds no noise ahead of the score it decorates.
 *
 * There is no component library anywhere in this repository, so semantic content
 * is the only stable selector; coupling to Tailwind utility classes would make
 * these tests fail on a restyle that changed nothing a user perceives.
 *
 * The sentiment-parity test is the single deliberate exception, and it does not
 * couple to any particular class: it compares two renders against EACH OTHER
 * with their digits masked, so a restyle that treats every score alike still
 * passes while a restyle that treats a low score differently cannot. Styling is
 * the medium a de-emphasis would use, so a text-only comparison could not see
 * one at all.
 *
 * NO PROVIDER, NO ROUTER, AND NO MOCKS AT ALL
 * -----------------------------------------------------------------------------
 * `ReputationBadge` is a pure presenter: no state, no effect, no fetch, no
 * Redux. It is rendered bare below, and that is a deliberate assertion about its
 * design rather than a shortcut — the day it needs a `<Provider>` to render is
 * the day it stopped being a presenter.
 *
 * Nothing is mocked either, and that is the more consequential half. The
 * component's one collaborator is `formatRating` from `../../utils/formatting`,
 * and this suite exercises the REAL function: the assertions below are about
 * what a user actually sees, so substituting a local reimplementation of the
 * formatter would have left the production one unexercised and free to regress
 * — to two decimals, to truncation, to a different scale — with every test in
 * this file still green. `renders the production formatter's rounding` and
 * `delegates the score text to the production formatter` are the two assertions
 * that would notice.
 *
 * Loading that module used to be the obstacle: it imported `format` from
 * `date-fns`, a package declared in neither `package.json` nor
 * `package-lock.json` and therefore not installed by the `npm ci` CI runs, so
 * pulling the real module into the graph made this suite fail at transform time.
 * That is why an earlier version of this file mocked the formatter wholesale —
 * a false green that reported a required display artifact as working while it
 * could not load at all. The dependency has since been removed at its source:
 * `formatDate` now uses the platform's `Intl.DateTimeFormat`, so
 * `../../utils/formatting` resolves with nothing installed and the real
 * `formatRating` is exercised here. Reaching a green run is itself proof that
 * the production module graph resolves.
 */

import { render, screen, within } from '@testing-library/react';

import ReputationBadge from '../ReputationBadge';
import { RATING_MAX, RATING_MIN } from '../../schema/rating';
import type { RatingAggregate } from '../../schema/rating';
import { formatRating } from '../../utils/formatting';
// The whole module is bound as well as the one function, and both bindings are
// load-bearing. The assertions use `formatRating` directly, while the `import
// closure` block below reaches for `formatting.formatDate` and
// `formatting.formatCurrency` to prove the module's ENTIRE export surface
// resolved - which is the check that fails if any import inside that closure
// stops resolving after a clean install.
import * as formatting from '../../utils/formatting';

/**
 * NO MODULE MOCK, and consequently no `vi` in this file — no mock, no spy, no
 * fake timer.
 *
 * `describe`, `it` and `expect` are injected as globals (`test.globals: true` in
 * `../../../vite.config.ts`) and typed by the ambient `@types/jest` declarations
 * TypeScript picks up automatically, which is also what makes jest-dom's
 * `toBeInTheDocument()` type-check — jest-dom v5 augments the matcher interface
 * those same globals expose. Importing `expect` from `vitest` instead would drop
 * that augmentation and turn every `toBeInTheDocument()` here into a type error,
 * and a triple-slash reference to `vitest/globals` would redeclare
 * `describe`/`it`/`expect` against `@types/jest` and produce TS2451 errors, so
 * neither is used. The directive is described rather than written out so that it
 * cannot be reintroduced by copying this comment.

/*
 * THE REAL MODULE GRAPH IS LOADED, DELIBERATELY AND WITH NO MOCK
 * -----------------------------------------------------------------------------
 * This file used to replace `../../utils/formatting` with a factory mock, and
 * the justification was a supply-chain fact rather than a testing preference:
 * that module's first line imported `format` from `date-fns`, a package declared
 * in neither `package.json` nor `package-lock.json` and therefore absent after
 * the `npm ci` that CI runs. Keeping it out of the graph was the only way the
 * suite could run.
 *
 * That fact no longer holds. `formatDate` was reimplemented on
 * `Intl.DateTimeFormat`, which needs no supply chain, so the formatter's import
 * closure is now entirely first-party and standard-library.
 *
 * The mock is gone rather than merely unnecessary, because with the dependency
 * repaired the mock became the problem it was working around. A suite that
 * substitutes the formatter cannot detect an unresolvable import inside it — the
 * exact regression that made this component un-buildable in a reproducible
 * install while appearing to work on any developer machine with a stale copy in
 * `node_modules`. Importing the module for real makes that regression a FAILING
 * TEST here: an undeclared dependency reintroduced anywhere in the formatter's
 * closure fails this file at transform time, in the pipeline, on the commit that
 * introduced it.
 *
 * Unit isolation is not lost by this. `formatRating` is a pure, three-line,
 * synchronous function over a number — there is no clock, no network, no
 * randomness and nothing to stub — so loading the real one costs nothing and
 * removes the possibility of a mock that flatters the component with behaviour
 * the real formatter does not have. `renders exactly what the real formatter
 * returns` pins the two together directly, which no mocked assertion could.
 *
 * `import * as formatting` rather than a named import is what allows those
 * assertions to call the same binding the component calls, and to state the
 * import-closure requirement as an assertion about the module itself.

 */

/**
 * Builds a matcher for the rendered score, e.g. `4.5/5`.
 *
 * The maximum is interpolated from `RATING_MAX` so this file contains no
 * hardcoded `5`; if the scale ever changed in the schema shared with the server,
 * these assertions would follow it instead of failing spuriously.
 *
 * A regular expression rather than a string literal, so the assertion tolerates
 * incidental whitespace around the separator and any surrounding punctuation
 * instead of breaking on a purely presentational change. It is deliberately not
 * anchored: `getByText` matches an element against its own direct text children,
 * so the score's element matches while its container — which has element
 * children only — does not, and the match stays unambiguous.
 *
 * @param formatted A formatted numeric score such as `'4.5'`; its decimal points
 *   are escaped so they match literally rather than as regex wildcards.
 */
const scorePattern = (formatted: string): RegExp =>
  new RegExp(`${formatted.replace(/\./g, '\\.')}\\s*/\\s*${RATING_MAX}`);

/**
 * Collapses an element's entire rendered text to a single normalised string.
 *
 * Used for whole-subtree assertions in both directions. `queryByText` inspects
 * each element's own text, so it can neither see a value split across elements
 * nor confirm that one is absent everywhere; reading `textContent` can do both.
 * The two are used together wherever "this string must (not) appear ANYWHERE" is
 * the actual requirement.
 *
 * Note that adjacent elements' text concatenates with no separator here, because
 * the visible spacing comes from a flex `gap` rather than from whitespace in the
 * markup — so a score of `4.5` beside a count of `12` reads as `4.5/512 ratings`
 * in this string. That is why the positive assertions below check for their own
 * fragment with `toContain` rather than comparing the whole line.
 */
const renderedText = (element: HTMLElement): string =>
  (element.textContent ?? '').replace(/\s+/g, ' ').trim();

/**
 * Reduces rendered markup to its presentation SKELETON: every digit run is
 * replaced by a `#`, so two renders that differ only in the numbers they display
 * collapse to the same string.
 *
 * This exists to make sentiment neutrality checkable as an equality rather than
 * as a list of things to look for. Comparing skeletons catches every way a low
 * score could be presented differently from a high one — a conditional class, an
 * opacity or colour change, a warning icon, extra copy, a different element, an
 * added ARIA attribute, a removed one — including the ones nobody thought to
 * write an assertion for. Enumerating suspects instead would only ever cover the
 * mechanisms imagined in advance.
 *
 * `innerHTML` rather than `textContent`, deliberately: the copy is only half of
 * the concern and the styling is the half that a de-emphasis would use.
 */
const presentationSkeleton = (element: HTMLElement): string =>
  element.innerHTML.replace(/\d+/g, '#');

describe('ReputationBadge', () => {
  describe('populated state', () => {
    it('renders the average to one decimal place against the maximum score', () => {
      render(<ReputationBadge average={4.5} count={12} />);

      expect(screen.getByText(scorePattern('4.5'))).toBeInTheDocument();
    });

    it('renders the number of ratings the average is computed from', () => {
      render(<ReputationBadge average={4.5} count={12} />);

      // The denominator is not decoration: "5.0 from 1 rating" and "4.6 from 300
      // ratings" describe very different reputations, and a reader is entitled
      // to know which one they are looking at.
      expect(screen.getByText(/^12\s+ratings$/)).toBeInTheDocument();
    });

    it('uses the singular noun for exactly one rating', () => {
      render(<ReputationBadge average={5} count={1} />);

      // Anchored, because an unanchored /1\s+rating/ would also match the
      // incorrect "1 ratings" and defeat the point of the test. This is the
      // first state every newly rated user passes through.
      expect(screen.getByText(/^1\s+rating$/)).toBeInTheDocument();
      expect(screen.queryByText(/^1\s+ratings$/)).not.toBeInTheDocument();
    });

    it('renders the formatter output rather than the raw number', () => {
      render(<ReputationBadge average={4} count={3} />);

      // A whole-number average must display as "4.0", never as a bare "4". This
      // is what proves the component routes the score through the real
      // `formatRating` instead of interpolating the prop directly.
      expect(screen.getByText(scorePattern('4.0'))).toBeInTheDocument();
      expect(screen.queryByText(`4/${RATING_MAX}`)).not.toBeInTheDocument();
    });

    it("renders the production formatter's rounding", () => {
      render(<ReputationBadge average={4.56} count={9} />);

      // The whole-number case above proves PRECISION — that a trailing ".0" is
      // added. This proves ROUNDING, which is a different property and the one
      // that a reimplementation of the formatter is most likely to get wrong:
      // truncation would render "4.5", two decimals "4.56", and `Math.round`
      // applied to the wrong scale "5.0". The expected string is written out as
      // a literal rather than derived from `formatRating`, because a comparison
      // against the function under test would move with it and prove nothing.
      expect(screen.getByText(scorePattern('4.6'))).toBeInTheDocument();
      expect(screen.queryByText(/4\.56/)).not.toBeInTheDocument();
      expect(screen.queryByText(scorePattern('4.5'))).not.toBeInTheDocument();
    });

    it('rounds down as well as up, at one decimal place', () => {
      render(<ReputationBadge average={4.44} count={9} />);

      // The other half of rounding. Together with the case above this pins
      // half-up-to-nearest at one fractional digit, which is the contract
      // `formatRating` documents and the precision the project's own
      // "4.5/5 average rating" success metric implies.
      expect(screen.getByText(scorePattern('4.4'))).toBeInTheDocument();
      expect(screen.queryByText(/4\.44/)).not.toBeInTheDocument();
    });

    it('delegates the score text to the production formatter', () => {
      const average = 3.25;

      const { container } = render(
        <ReputationBadge average={average} count={7} />,
      );

      // Complements the literal assertions above from the other direction: they
      // pin what the formatter must produce, this pins that the component does
      // not reformat, re-round or re-render the number on its own. If the
      // component ever stopped routing the score through `formatRating` — say by
      // interpolating `average.toFixed(2)` inline — the two would disagree here
      // even though `formatRating` itself was untouched.
      expect(renderedText(container)).toContain(
        `${formatRating(average)}/${RATING_MAX}`,
      );
    });

    it('renders exactly what the real formatter returns', () => {
      // The strongest form of the assertion above: not "looks like one decimal
      // place" but "is character-for-character the real `formatRating` output".
      // Nothing here restates the formatter's rule, so the two cannot drift -
      // change the formatter and this test follows it rather than failing on a
      // hardcoded expectation.
      const average = 3.14159;

      render(<ReputationBadge average={average} count={9} />);

      expect(
        screen.getByText(scorePattern(formatRating(average))),
      ).toBeInTheDocument();
    });

    it('hides the decorative star from assistive technology', () => {
      render(<ReputationBadge average={4.5} count={12} />);

      // The star carries no information the adjacent text does not already
      // carry, so announcing it would add noise ahead of the score. Its being
      // hidden is what makes "the score is conveyed as text" the whole truth
      // rather than half of it.
      expect(screen.getByText('★')).toHaveAttribute('aria-hidden', 'true');
    });

    it('accepts a RatingAggregate exactly as the API returns it', () => {
      // Typed deliberately: the props are the same field pair as
      // `RatingAggregate`, so a caller can spread an aggregate straight through
      // with no adaptation. If either name or type drifted apart, this stops
      // compiling — a contract check the runtime assertions could not make.
      const aggregate: RatingAggregate = { average: 4.5, count: 12 };

      render(<ReputationBadge {...aggregate} />);

      expect(screen.getByText(scorePattern('4.5'))).toBeInTheDocument();
      expect(screen.getByText(/^12\s+ratings$/)).toBeInTheDocument();
    });
  });

  describe('empty state', () => {
    it('renders its own "no ratings yet" copy when the average is null', () => {
      render(<ReputationBadge average={null} count={0} />);

      // The wording belongs to the component, not to the formatter: the
      // formatter returns an empty string for `null` and deliberately supplies
      // no copy, so this sentence can only have come from here. It is also
      // information a buyer needs — a seller nobody has rated is a different
      // proposition from one rated badly — so the state is stated in words
      // rather than left blank.
      expect(screen.getByText(/no ratings yet/i)).toBeInTheDocument();
    });

    it('never renders 0.0 for a user who has not been rated', () => {
      const { container } = render(<ReputationBadge average={null} count={0} />);

      // The single most important assertion in this file. `null` means "no
      // ratings", NOT "rated zero", and the entire reason the prop is
      // `number | null` rather than `number` is to keep those two apart. A
      // regression that coerced `null` to `0` would present an unrated user as
      // having earned a genuine one-star reputation.
      //
      // Asserted twice on purpose: `queryByText` inspects each element's own
      // text, so it would miss a value split across elements; reading the whole
      // subtree's text cannot. "Nowhere at all" is the actual requirement.
      expect(screen.queryByText(/0\.0/)).not.toBeInTheDocument();
      expect(renderedText(container)).not.toContain('0.0');
    });

    it('renders no score at all when the average is null', () => {
      render(<ReputationBadge average={null} count={0} />);

      expect(screen.queryByText(scorePattern('0.0'))).not.toBeInTheDocument();
      // Not even a bare "/5" with nothing in front of it.
      expect(
        screen.queryByText(new RegExp(`/\\s*${RATING_MAX}`)),
      ).not.toBeInTheDocument();
      // The star belongs to the score; with no score there is nothing to
      // decorate, so a lone star would imply a rating that does not exist.
      expect(screen.queryByText('★')).not.toBeInTheDocument();
    });

    it('renders no rating count when the average is null', () => {
      render(<ReputationBadge average={null} count={0} />);

      // Anchored deliberately: the empty-state copy itself contains the word
      // "ratings", so an unanchored /ratings/ would match it and the assertion
      // would be testing nothing. What must be absent is the COUNT line.
      expect(screen.queryByText(/^0\s+ratings$/)).not.toBeInTheDocument();
    });

    it('falls back to the empty state when the count is zero despite an average', () => {
      render(<ReputationBadge average={4.5} count={0} />);

      // An average computed from no ratings is not a reputation. The server keeps
      // `count === 0` if and only if `average === null`, but this component does
      // not assume that invariant survived the wire, and neither does this test.
      expect(screen.getByText(/no ratings yet/i)).toBeInTheDocument();
      expect(screen.queryByText(scorePattern('4.5'))).not.toBeInTheDocument();
    });

    it('renders the empty state rather than the text NaN for a non-finite average', () => {
      const { container } = render(
        <ReputationBadge average={Number.NaN} count={4} />,
      );

      // NaN is an arithmetic accident upstream, not a low score — so excluding
      // it is an absent-value guard and not a sentiment threshold. Without it the
      // score element would render the literal "NaN" or a bare "/5", either of
      // which reads to a user as a broken profile page.
      expect(screen.getByText(/no ratings yet/i)).toBeInTheDocument();
      expect(renderedText(container)).not.toContain('NaN');
    });

    it('renders the empty state for an infinite average', () => {
      const { container } = render(
        <ReputationBadge average={Number.POSITIVE_INFINITY} count={4} />,
      );

      // The other non-finite value, asserted separately because the guard is
      // documented as `Number.isFinite` rather than `Number.isNaN` and the two
      // are not interchangeable: a `Number.isNaN`-only check would let this case
      // through and render the literal "Infinity" beside a "/5".
      expect(screen.getByText(/no ratings yet/i)).toBeInTheDocument();
      expect(renderedText(container)).not.toContain('Infinity');
      expect(renderedText(container)).not.toContain('∞');
    });

    it('renders the empty state when the average is null despite a nonzero count', () => {
      const { container } = render(
        <ReputationBadge average={null} count={7} />,
      );

      // An inconsistent aggregate: the server maintains `count === 0` if and
      // only if `average === null`, so this pair should never arrive — but it is
      // a wire shape the props permit, and "no average" is the half that decides.
      // Rendering the count on its own would state a reputation of seven ratings
      // while showing no score to go with it.
      expect(screen.getByText(/no ratings yet/i)).toBeInTheDocument();
      expect(screen.queryByText(/^7\s+ratings$/)).not.toBeInTheDocument();
      expect(renderedText(container)).not.toContain('7');
    });

    it('accepts an unrated RatingAggregate exactly as the API returns it', () => {
      // Every user document that predates this feature deserialises to precisely
      // this shape, so it is what a fresh deployment renders first.
      const aggregate: RatingAggregate = { average: null, count: 0 };

      render(<ReputationBadge {...aggregate} />);

      expect(screen.getByText(/no ratings yet/i)).toBeInTheDocument();
    });
  });

  describe('declared numeric contract', () => {
    it('renders a zero average that was actually earned', () => {
      const { container } = render(<ReputationBadge average={0} count={3} />);

      // The distinction this test defends is the whole reason the prop is
      // `number | null`: the empty state is chosen by the ABSENCE of a value —
      // `null`, or a non-finite one, or no ratings to average — and never by the
      // value being small or falsy. `0` with three ratings behind it is a
      // present, finite, server-computed figure, so it is displayed.
      //
      // The likeliest regression here is a one-character one: writing the guard
      // as `average ? … : empty` instead of `average !== null`. That reads
      // identically for every other input in this file and sends exactly this
      // case to the empty state, silently converting a real aggregate into "no
      // ratings yet". Below the scale's floor of `RATING_MIN` a zero should not
      // arise from real data at all, which is precisely why nothing else would
      // catch it.
      expect(screen.getByText(scorePattern('0.0'))).toBeInTheDocument();
      expect(screen.getByText(/^3\s+ratings$/)).toBeInTheDocument();
      expect(screen.queryByText(/no ratings yet/i)).not.toBeInTheDocument();
      expect(renderedText(container)).toContain(`0.0/${RATING_MAX}`);
    });

    it('renders an average above the top of the scale exactly as given', () => {
      const aboveScale = RATING_MAX + 0.4;

      const { container } = render(
        <ReputationBadge average={aboveScale} count={11} />,
      );

      // The prop is documented as deliberately unbounded: the average is a
      // rounded running mean the server owns and validates, and this component's
      // job is to display what it is handed. Clamping to `RATING_MAX` here would
      // quietly disagree with the number the server computed and with every
      // other surface that reads the same aggregate, so no clamp is asserted for
      // in both directions — the given value is present, the clamped one is not.
      expect(renderedText(container)).toContain(
        `${formatRating(aboveScale)}/${RATING_MAX}`,
      );
      expect(renderedText(container)).not.toContain(
        `${formatRating(RATING_MAX)}/${RATING_MAX}`,
      );
      expect(screen.queryByText(/no ratings yet/i)).not.toBeInTheDocument();
    });
  });

  describe('optional label', () => {
    it('renders the caption when one is supplied', () => {
      render(
        <ReputationBadge average={4.5} count={12} label="Seller rating" />,
      );

      expect(screen.getByText(/^Seller rating$/)).toBeInTheDocument();
      expect(screen.getByText(scorePattern('4.5'))).toBeInTheDocument();
    });

    it('renders the caption alongside the empty state as well', () => {
      render(<ReputationBadge average={null} count={0} label="Seller rating" />);

      // Beside a listing the badge has to say WHOSE reputation it describes, and
      // that is no less true when the answer is "nobody has rated them yet".
      expect(screen.getByText(/^Seller rating$/)).toBeInTheDocument();
      expect(screen.getByText(/no ratings yet/i)).toBeInTheDocument();
    });

    it('renders the score and count with no caption when none is supplied', () => {
      const { container } = render(
        <ReputationBadge average={4.5} count={12} />,
      );

      expect(screen.getByText(scorePattern('4.5'))).toBeInTheDocument();
      expect(screen.getByText(/^12\s+ratings$/)).toBeInTheDocument();
      expect(renderedText(container)).toContain(`4.5/${RATING_MAX}`);
      expect(renderedText(container)).toContain('12 ratings');
      // No caption is invented when none is passed: the correct wording depends
      // entirely on the surrounding page, so the component stays silent instead
      // of guessing. On a profile the heading already supplies the context.
      expect(screen.queryByText(/^Seller rating$/)).not.toBeInTheDocument();
    });
  });

  describe('sentiment neutrality', () => {
    it('renders a low average as completely as a high one', () => {
      render(<ReputationBadge average={1.2} count={4} />);

      // 16 CFR Part 465 prohibits suppressing reviews on the basis of their
      // rating or of negative sentiment, so this is a compliance assertion and
      // not a styling one. A poor average must be shown as plainly as a good
      // one: same wording, same completeness, nothing hidden and nothing
      // softened. This case covers the crudest violation — a low score simply
      // not being rendered; the parity test below covers the subtler ones.
      expect(screen.getByText(scorePattern('1.2'))).toBeInTheDocument();
      expect(screen.getByText(/^4\s+ratings$/)).toBeInTheDocument();
      expect(screen.getByText('★')).toBeInTheDocument();
      expect(screen.queryByText(/no ratings yet/i)).not.toBeInTheDocument();
    });

    it('presents a low average identically to a high one', () => {
      // Same count, same label, same everything but the score, so the only
      // difference the two renders can legitimately have is the digits — which
      // the skeleton masks. Anything else that differs is score-correlated
      // presentation by definition.
      const low = render(
        <ReputationBadge average={1.2} count={4} label="Seller rating" />,
      );
      const high = render(
        <ReputationBadge average={4.9} count={4} label="Seller rating" />,
      );

      // The assertion the previous test cannot make. Presence checks pass just
      // as happily against `average < 3 ? 'text-red-600 opacity-50' : ''`, an
      // appended "Below average" caption, a warning glyph, or an `aria-label`
      // editorialising the score — every one of which is the kind of
      // score-correlated treatment 16 CFR Part 465 addresses, and every one of
      // which changes the markup. Comparing skeletons is what turns "shown as
      // plainly" from a claim in a comment into something a run can fail on.
      expect(presentationSkeleton(low.container)).toBe(
        presentationSkeleton(high.container),
      );

      // Stated separately because equality alone would also be satisfied by two
      // renders that were both broken in the same way: the low render really
      // does carry the caption, the star, the score and the count. Scoped with
      // `within`, since two badges are mounted and a `screen` query would find
      // both.
      const lowBadge = within(low.container);

      expect(lowBadge.getByText(/^Seller rating$/)).toBeInTheDocument();
      expect(lowBadge.getByText('★')).toBeInTheDocument();
      expect(lowBadge.getByText(scorePattern('1.2'))).toBeInTheDocument();
      expect(lowBadge.getByText(/^4\s+ratings$/)).toBeInTheDocument();
    });

    it('renders the lowest score on the scale without suppressing it', () => {
      render(<ReputationBadge average={RATING_MIN} count={1} />);

      // The floor of the scale, taken from the schema shared with the server
      // rather than written as a literal, is still a real earned rating and is
      // displayed like any other — through the formatter, so it reads "1.0".
      expect(screen.getByText(scorePattern('1.0'))).toBeInTheDocument();
      expect(screen.queryByText(/no ratings yet/i)).not.toBeInTheDocument();
    });
  });

  /**
   * Import-closure coverage: the component's real dependency graph resolves.
   *
   * These assertions are about the MODULE, not about the rendering, and they
   * exist because this component was once un-buildable in a reproducible install
   * while every rendering test passed. `../ReputationBadge` imports
   * `formatRating` from `../../utils/formatting`, and this file imports the same
   * module for real, so the whole closure — component, formatter, schema — is
   * loaded and transformed by Vitest before a single assertion runs.
   *
   * That is what makes an undeclared dependency a failing test rather than a
   * pipeline surprise: a bare import anywhere in that closure that
   * `package-lock.json` does not account for cannot resolve after `npm ci`, and
   * this file fails at transform time on the commit that added it. A mocked
   * formatter cannot make that statement, which is why there is no longer one.
   *
   * The assertions themselves are deliberately about shape rather than values —
   * the values are pinned by the rendering tests above. What is being asserted
   * here is that the real bindings exist and are callable, so the block cannot
   * pass vacuously if the module ever resolved to something empty.
   */
  describe('import closure', () => {
    it('loads the real formatter module the component depends on', () => {
      expect(typeof formatting.formatRating).toBe('function');
      expect(typeof formatting.formatDate).toBe('function');
      expect(typeof formatting.formatCurrency).toBe('function');
    });

    it('renders through the real formatter with no module substituted', () => {
      // If the formatter were mocked, or the module resolved to a stub, the
      // rendered score and this direct call could not both come from the same
      // implementation. Asserting them equal is the check.
      const { container } = render(<ReputationBadge average={4.25} count={8} />);

      expect(renderedText(container)).toContain(
        `${formatting.formatRating(4.25)}/${RATING_MAX}`,
      );
    });

    it('agrees with the real formatter about the unrated case', () => {
      // `formatRating(null)` is the empty string, and the component must answer
      // that case with its own copy rather than by rendering nothing — the two
      // halves of "never been rated" that this suite exists to keep apart.
      expect(formatting.formatRating(null)).toBe('');

      render(<ReputationBadge average={null} count={0} />);

      expect(screen.getByText(/no ratings yet/i)).toBeInTheDocument();
    });
  });
});
