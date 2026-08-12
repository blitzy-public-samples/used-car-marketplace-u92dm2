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
 *   2. The score is rendered through `formatRating`, so a whole number shows as
 *      `4.0` and never as a bare `4`, and the maximum comes from `RATING_MAX`
 *      rather than from a `5` written here. Both are pinned below.
 *
 * A third property is asserted because it is a legal constraint rather than a
 * preference: presentation is SENTIMENT-NEUTRAL. The FTC Rule on the Use of
 * Consumer Reviews and Testimonials (16 CFR Part 465) prohibits suppressing
 * reviews on the basis of their rating or of negative sentiment, so a low
 * average must be displayed exactly as fully as a high one. `renders a low
 * average as completely as a high one` is that guard, and it is the test that
 * would fail first if anyone added score-correlated hiding or de-emphasis.
 *
 * ACCESSIBILITY IS ASSERTED THROUGH THE QUERIES THEMSELVES
 * -----------------------------------------------------------------------------
 * WCAG 2.1 Level AA is a stated requirement (SRS L187, L556). This component is
 * a non-interactive display, so there is no keyboard obligation to verify, but
 * the score must be legible as TEXT and must not be conveyed by colour or shape
 * alone. Every assertion below therefore goes through a text query — never
 * through a class name, an inline style or a `data-testid` — which is precisely
 * what proves the value reaches a screen reader as words. The decorative star is
 * separately asserted to be hidden from assistive technology, so it adds no
 * noise ahead of the score it decorates.
 *
 * There is no component library anywhere in this repository, so semantic content
 * is the only stable selector; coupling to Tailwind utility classes would make
 * these tests fail on a restyle that changed nothing a user perceives.
 *
 * NO PROVIDER, NO ROUTER
 * -----------------------------------------------------------------------------
 * `ReputationBadge` is a pure presenter: no state, no effect, no fetch, no
 * Redux. It is rendered bare below, and that is a deliberate assertion about its
 * design rather than a shortcut — the day it needs a `<Provider>` to render is
 * the day it stopped being a presenter.
 */

import { render, screen } from '@testing-library/react';

import ReputationBadge from '../ReputationBadge';
import { RATING_MAX, RATING_MIN } from '../../schema/rating';
import type { RatingAggregate } from '../../schema/rating';

/**
 * Type-only binding for Vitest's `vi` helper.
 *
 * `describe`, `it` and `expect` need no declaration: they are injected as
 * globals (`test.globals: true` in `../../../vite.config.ts`) and typed by the
 * ambient `@types/jest` declarations that TypeScript picks up automatically,
 * which is also what makes jest-dom's `toBeInTheDocument()` type-check —
 * jest-dom v5 augments the matcher interface those same globals expose.
 *
 * `vi` is the one identifier that arrangement does not cover, because
 * `@types/jest` has no equivalent. It is declared here instead of imported, and
 * the distinction is load-bearing in three separate ways:
 *
 *   - `typeof import('vitest')` is a TYPE position, so nothing is emitted and no
 *     runtime import of `vitest` is created. The identifier resolves through the
 *     scope chain to the injected global at run time.
 *   - Importing `expect` from `vitest` instead would drop the jest-dom
 *     augmentation and turn every `toBeInTheDocument()` in this file into a
 *     fresh type error, because that augmentation targets the global.
 *   - A triple-slash reference directive pointing at `vitest/globals` would ALSO
 *     work at run time, but it declares global `describe`/`it`/`expect` that
 *     collide with the `@types/jest` declarations already in the program,
 *     producing TS2451 "cannot redeclare" errors attributed to this file. It is
 *     deliberately not used, and the directive is described rather than written
 *     out here so that it cannot be reintroduced by copying this comment.
 *
 * Mock hoisting is unaffected: Vitest finds `vi.mock(...)` syntactically and
 * lifts it above the imports whether `vi` was imported or not.
 */
declare const vi: typeof import('vitest')['vi'];

/**
 * Replaces the first-party formatting module for the duration of this file.
 *
 * This is REQUIRED for the suite to run at all, and the reason is a supply-chain
 * fact rather than a testing preference. `../ReputationBadge` imports
 * `formatRating` from `../../utils/formatting`, whose very first line imports
 * `format` from `date-fns` — and `date-fns` is declared NOWHERE:
 * `npm ls date-fns` reports it as `extraneous`, it is absent from
 * `package.json`, and it is absent from `package-lock.json` entirely. It is
 * therefore not installed by `npm ci`, which is what CI runs. Pulling the real
 * module into the graph would make this suite pass on a developer machine that
 * happens to have a stale copy lying in `node_modules` and fail in the pipeline
 * — the worst of both outcomes.
 *
 * Declaring `date-fns` would "fix" the import and is explicitly not the answer:
 * the undeclared frontend packages serving untouched code paths stay undeclared,
 * so the correct response is to keep the unresolvable module out of the graph
 * rather than to widen the manifest for it. A factory mock does
 * exactly that — Vitest never loads or transforms the real `formatting.ts`, so
 * `date-fns` is never resolved. Mocking `'date-fns'` itself would NOT work,
 * because that leaves `formatting.ts` in the graph where its unresolvable bare
 * import can fail at transform time, before any mock registry is consulted.
 *
 * The path is written relative to THIS file while the component writes its own
 * relative to itself; both resolve to `src/utils/formatting.ts`, and mock keys
 * are resolved paths, so the same registration covers the component's import.
 *
 * The factory replaces the WHOLE module, so it must supply everything the
 * component imports from it — which is `formatRating` and nothing else; the
 * component's other two imports are `react` and `../schema/rating`. The
 * implementation mirrors the real function's semantics exactly, including its
 * non-finite handling, so the mock cannot flatter the component with behaviour
 * the real formatter does not have. Fidelity is then pinned independently by
 * `renders the formatter output rather than the raw number`, which fails if the
 * component ever stops routing the score through this function.
 */
vi.mock('../../utils/formatting', () => ({
  formatRating: (average: number | null): string =>
    average === null || !Number.isFinite(average) ? '' : average.toFixed(1),
}));

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
 * Used only for whole-subtree negative assertions. `queryByText` inspects each
 * element's own text, so it cannot see a value that has been split across
 * elements; reading `textContent` can. The two are used together where "this
 * string must not appear ANYWHERE" is the actual requirement.
 */
const renderedText = (element: HTMLElement): string =>
  (element.textContent ?? '').replace(/\s+/g, ' ').trim();

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
      // is what proves the component routes the score through `formatRating`
      // instead of interpolating the prop directly — the regression the mocked
      // formatter would otherwise be able to hide.
      expect(screen.getByText(scorePattern('4.0'))).toBeInTheDocument();
      expect(screen.queryByText(`4/${RATING_MAX}`)).not.toBeInTheDocument();
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

      // The wording belongs to the component, not to the formatter — which is
      // what keeps this assertion meaningful even with the formatter mocked.
      // It is also information a buyer needs: a seller nobody has rated is a
      // different proposition from one rated badly, so the state is stated in
      // words rather than left blank.
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

    it('accepts an unrated RatingAggregate exactly as the API returns it', () => {
      // Every user document that predates this feature deserialises to precisely
      // this shape, so it is what a fresh deployment renders first.
      const aggregate: RatingAggregate = { average: null, count: 0 };

      render(<ReputationBadge {...aggregate} />);

      expect(screen.getByText(/no ratings yet/i)).toBeInTheDocument();
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
      // softened. This is the test that fails first if anyone ever adds
      // score-correlated hiding, de-emphasis or flattering rounding.
      expect(screen.getByText(scorePattern('1.2'))).toBeInTheDocument();
      expect(screen.getByText(/^4\s+ratings$/)).toBeInTheDocument();
      expect(screen.getByText('★')).toBeInTheDocument();
      expect(screen.queryByText(/no ratings yet/i)).not.toBeInTheDocument();
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
});
