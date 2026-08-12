/**
 * Accessibility and behaviour contract for `../StarRatingInput`, the score half
 * of the bidirectional peer reputation system.
 *
 * WHAT THIS SUITE IS FOR
 * -----------------------------------------------------------------------------
 * `documentation/Software Requirements Specifications (SRS).md` states the same
 * accessibility obligation three times — "Adherence to Web Content
 * Accessibility Guidelines (WCAG) 2.1 Level AA" among the user-interface
 * constraints (L187), "Accessibility compliance with WCAG 2.1 Level AA
 * standards" among the usability requirements (L525), and "WCAG 2.1 Level AA
 * compliance for web accessibility" plus "Support for screen readers and
 * keyboard navigation" among the standards (L556-L557). F010-1 "User rating
 * submission interface" (L437) is the feature those obligations land on.
 *
 * Until this file existed, all of that was prose. A star strip is the canonical
 * way to fail it — five click handlers on non-interactive elements are
 * unreachable by keyboard, expose no role or state, and communicate the value by
 * colour alone — and prose does not catch a regression into that shape. This
 * suite turns each obligation into an executable assertion:
 *
 *   - keyboard operability          arrow / Home / End / Space / Tab tests
 *   - exposed roles                 `getByRole('radiogroup')`, `getAllByRole('radio')`
 *   - exposed accessible names      `toHaveAccessibleName('Rate 3 out of 5')`
 *   - exposed state                 `aria-checked` tracking
 *   - a single, predictable Tab stop roving-`tabindex` tests
 *   - a VISIBLE focus indicator     focus-indicator contract tests
 *   - value not carried by colour   textual-echo tests
 *
 * Queries are deliberately role- and accessible-name-based rather than
 * class- or `data-testid`-based. That is the point of them: when the query is
 * `getByRole('radio', { name: 'Rate 3 out of 5' })`, the act of finding the
 * element is itself the assertion that assistive technology can find it too. A
 * `data-testid` selector would pass just as happily against a `<div>` soup that
 * no screen reader can operate. There is no component library anywhere in this
 * repository, so there are also no library test ids to fall back on.
 *
 * The focus-indicator tests are the one documented exception, and they are an
 * exception because jsdom applies no stylesheet: computed style is empty for
 * every element, so visible focus cannot be observed and has to be asserted
 * against the source contract instead. The reasoning, and how those assertions
 * stay implementation-agnostic, is set out above that block.
 *
 * HOW THE TEST GLOBALS RESOLVE (do not "fix" this with a Vitest import)
 * -----------------------------------------------------------------------------
 * `describe`, `it` and `expect` are injected — `vite.config.ts` sets
 * `test.globals: true` — and are deliberately NOT imported. Three facts make
 * that the only arrangement that type-checks:
 *
 *   1. `tsconfig.json` excludes files ending `.test.ts` but NOT files ending
 *      `.test.tsx`, while its `include` covers every `.ts` and `.tsx` file under
 *      `src`. This file is therefore inside the `tsc --noEmit` program and is
 *      held to the same zero-new-errors bar as production source. (Glob syntax is
 *      spelled out in words here because a `*` followed by a `/` would close this
 *      comment.)
 *   2. `tsconfig.json`'s `"types"` key sits outside `compilerOptions`, so
 *      `compilerOptions.types` is unset and every `node_modules/@types` package
 *      is auto-included. That is what supplies `@types/jest`'s ambient
 *      `describe` / `it` / `expect`, and `@types/testing-library__jest-dom`'s
 *      augmentation of the matcher interface those globals expose — which is why
 *      `toBeInTheDocument()` and `toHaveAccessibleName()` below type-check
 *      without importing `@testing-library/jest-dom` here.
 *   3. An `expect` pulled in from the Vitest package carries none of that
 *      augmentation, so every jest-dom matcher call would become a fresh type
 *      error in a fresh file. A triple-slash reference to Vitest's globals types
 *      is worse still: it redeclares the identifiers `@types/jest` already
 *      provides and produces TS2451 "cannot redeclare" errors attributed to this
 *      file. Both are therefore absent by design, not by omission.
 *
 * WHAT IS DELIBERATELY ABSENT
 * -----------------------------------------------------------------------------
 *   - No mock functions and no mocking of any kind. `StarRatingInput` imports only
 *     `react` and `../schema/rating`; it runs no effect, performs no fetch and
 *     touches neither the Redux store nor any service module. There is nothing to
 *     mock, and a mock would only be able to hide a real regression.
 *   - No `<Provider>` wrapper, for the same reason.
 *   - No manual `cleanup()`. `@testing-library/react` v14 registers its own
 *     `afterEach` because `globals: true` provides one.
 *   - No snapshot assertions. A snapshot of a star strip records the markup
 *     without checking any of the six guarantees listed above, and would have to
 *     be re-blessed on every styling change.
 *   - No hardcoded `5`. Every expectation is derived from `RATING_MIN` and
 *     `RATING_MAX`, so widening the scale server-side does not silently leave
 *     this suite asserting the old range.
 */

import { useRef, useState } from 'react';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import StarRatingInput, {
  type StarRatingInputHandle,
} from '../StarRatingInput';
import { RATING_MIN, RATING_MAX } from '../../schema/rating';

/**
 * Every selectable score, ascending — the same derivation the component itself
 * performs, so the two cannot disagree about the size of the scale.
 */
const SCORES = Array.from(
  { length: RATING_MAX - RATING_MIN + 1 },
  (_, index) => RATING_MIN + index,
);

/**
 * How many options the group must expose. Computed rather than written as `5`:
 * hardcoding it would let a widened scale pass this suite while rendering the
 * wrong number of stars.
 */
const OPTION_COUNT = RATING_MAX - RATING_MIN + 1;

/**
 * A score in the middle of the scale, used wherever a test needs "some ordinary
 * selection" that is neither edge. Derived so it stays interior at any scale.
 */
const MID_SCORE = SCORES[Math.floor((SCORES.length - 1) / 2)];

/** The textual echo the component renders while nothing has been selected. */
const NO_SCORE_TEXT = 'No score selected';

/**
 * The two star glyphs, as escapes rather than literal characters so a
 * re-encoding of this file cannot silently change what is being asserted.
 *
 * U+2605 BLACK STAR marks a position at or below the score, U+2606 WHITE STAR a
 * position beyond it. They are declared here rather than imported because the
 * component keeps them module-local — deliberately, since they are an
 * implementation detail of its rendering — and restating them is what lets this
 * suite assert the VISUAL channel independently of the ARIA one.
 */
const FILLED_STAR = '\u2605';
const EMPTY_STAR = '\u2606';

/**
 * The accessible name each option must expose. Naming an option by the value it
 * selects is what makes the choice unambiguous when announced out of visual
 * context; the star glyph alone would announce as a punctuation character.
 */
const optionName = (score: number): string => `Rate ${score} out of ${RATING_MAX}`;

/** The textual echo the component renders once a score is selected. */
const echoText = (score: number): string => `${score} out of ${RATING_MAX}`;

/**
 * A deliberately inert `onChange` for the static-rendering tests, which assert
 * what a given `value` renders as and never drive an interaction. Written as an
 * expression body returning `undefined` rather than as `() => {}`, because an
 * empty block is reported by `@typescript-eslint/no-empty-function` and the lint
 * gate runs with `--max-warnings 0`.
 */
const ignoreScore = (): void => undefined;

/**
 * Utility that REMOVES the indicator the browser draws for a focused element.
 *
 * Matched rather than compared, so the `focus-visible:` variant of the same
 * suppression counts, as does `outline-hidden`.
 */
const OUTLINE_SUPPRESSOR = /^focus(-visible)?:outline-(none|0|hidden)$/;

/**
 * Any focus-state utility that could constitute an indicator, EXCLUDING the
 * suppressor above — which is a focus-state utility too, and the one thing that
 * must never be mistaken for an indicator.
 *
 * Deliberately broad: ring, outline, border, shadow and background are all
 * legitimate ways to mark focus, and this suite has no business dictating which.
 */
const FOCUS_INDICATOR =
  /^focus(-visible)?:(ring|outline|border|shadow|bg)(-|$)/;

/**
 * A focus indicator carrying a WIDTH, which is what makes it actually drawn —
 * e.g. `focus:ring-2`, `focus:outline-2`, `focus:border-4`. A colour alone
 * (`focus:ring-blue-600`) paints nothing.
 *
 * The offset variants are excluded, since `focus:ring-offset-2` is a gap rather
 * than a stroke and would otherwise satisfy this on its own.
 */
const SIZED_FOCUS_INDICATOR =
  /^focus(-visible)?:(ring|outline|border)-\d+$/;

/** The gap that keeps the indicator clear of the glyph it surrounds. */
const FOCUS_INDICATOR_OFFSET =
  /^focus(-visible)?:(ring|outline)-offset-\d+$/;

/**
 * An element's classes as a list, with empty entries dropped so a double space
 * in a composed `className` cannot produce a phantom member.
 */
const classListOf = (element: HTMLElement): string[] =>
  element.className.split(/\s+/).filter((name) => name.length > 0);

/**
 * The focus-indicator classes on an element, if any.
 *
 * A helper rather than an inline filter because three tests below ask the same
 * question of different elements, and because it keeps the definition of "is
 * there an indicator" in exactly one place.
 */
const focusIndicatorClasses = (element: HTMLElement): string[] =>
  classListOf(element).filter(
    (name) => FOCUS_INDICATOR.test(name) && !OUTLINE_SUPPRESSOR.test(name),
  );

interface HarnessProps {
  /** Score the harness starts with; `null` models the pre-choice state. */
  initial?: number | null;
  /** Forwarded group label, so the accessible-name plumbing can be exercised. */
  label?: string;
  /** Forwarded inert flag. */
  disabled?: boolean;
  /** Observer invoked with every score the component reports, in order. */
  onSelect?: (score: number) => void;
}

/**
 * A minimal stateful owner for the component under test.
 *
 * This is required, not convenience scaffolding. `StarRatingInput` is
 * controlled: it never changes its own `value`, so rendering it directly with a
 * fixed `value` can only ever prove what a given value LOOKS like. A keypress
 * would call `onChange` and then re-render with the same `value`, so a test
 * written that way could not distinguish a working arrow key from one that fires
 * `onChange` with the wrong score — or from one that does nothing at all.
 *
 * Holding the score here closes that gap: `onChange` feeds straight back into
 * `value`, exactly as the real parent (`RatingSubmissionForm`) does, so the
 * assertions below observe the selection actually moving.
 */
const Harness = ({
  initial = null,
  label = 'Rating',
  disabled = false,
  onSelect,
}: HarnessProps) => {
  const [value, setValue] = useState<number | null>(initial);

  const handleChange = (score: number): void => {
    setValue(score);

    if (onSelect) {
      onSelect(score);
    }
  };

  return (
    <StarRatingInput
      value={value}
      onChange={handleChange}
      label={label}
      disabled={disabled}
    />
  );
};

/**
 * Renders the harness and returns a `userEvent` session bound to it.
 *
 * `userEvent.setup()` is called per test rather than once at module scope so no
 * keyboard or pointer state (a held modifier, a pressed button) can leak from
 * one test into the next.
 */
const renderHarness = (props: HarnessProps = {}) => {
  const user = userEvent.setup();
  render(<Harness {...props} />);

  return user;
};

/** Every option, in document order. */
const options = (): HTMLElement[] => screen.getAllByRole('radio');

/**
 * The option that selects `score`, located by its accessible name. Using the
 * name rather than an index means a renamed or unnamed option fails the lookup
 * instead of quietly passing.
 */
const optionFor = (score: number): HTMLElement =>
  screen.getByRole('radio', { name: optionName(score) });

/**
 * Asserts that `aria-checked` reports exactly `expected` as selected and every
 * other option as unselected. Pass `null` for the pre-choice state.
 *
 * Checking all five on every call is what makes the wrap-around tests meaningful
 * — a clamping implementation would leave the edge option still checked, and
 * only an exhaustive assertion notices.
 */
const expectCheckedScore = (expected: number | null): void => {
  const rendered = options();

  SCORES.forEach((score, index) => {
    expect(rendered[index]).toHaveAttribute(
      'aria-checked',
      score === expected ? 'true' : 'false',
    );
  });

  expect(screen.queryAllByRole('radio', { checked: true })).toHaveLength(
    expected === null ? 0 : 1,
  );
};

/**
 * Asserts the roving tabindex: `expected` is the group's single tab stop and
 * every other option is removed from the tab order.
 *
 * The negative half matters as much as the positive one. Five options each with
 * `tabIndex={0}` would still be keyboard reachable and would still pass a naive
 * "can I Tab to it" check, while forcing five Tab presses to cross one control.
 */
const expectSingleTabStop = (expected: number): void => {
  const rendered = options();

  SCORES.forEach((score, index) => {
    expect(rendered[index]).toHaveAttribute(
      'tabindex',
      score === expected ? '0' : '-1',
    );
  });
};

/**
 * Tabs into the group from a known starting selection and sends `keys`.
 *
 * Focus is established with a real Tab press rather than a programmatic
 * `.focus()` call, so each keyboard test also re-proves that the group is
 * reachable the way a keyboard user actually reaches it.
 */
const tabInAndPress = async (
  initial: number | null,
  keys: string,
): Promise<void> => {
  const user = renderHarness({ initial });
  await user.tab();
  await user.keyboard(keys);
};

describe('StarRatingInput structure and ARIA semantics', () => {
  it('exposes a single radio group carrying an accessible name', () => {
    render(<StarRatingInput value={null} onChange={ignoreScore} />);

    expect(screen.getAllByRole('radiogroup')).toHaveLength(1);
    expect(screen.getByRole('radiogroup')).toHaveAccessibleName('Rating');
  });

  it('takes its group accessible name from the supplied label', () => {
    render(
      <StarRatingInput
        value={null}
        onChange={ignoreScore}
        label="Rate the seller"
      />,
    );

    expect(screen.getByRole('radiogroup')).toHaveAccessibleName(
      'Rate the seller',
    );
  });

  it('exposes one radio per score across the configured scale', () => {
    render(<StarRatingInput value={null} onChange={ignoreScore} />);

    expect(options()).toHaveLength(OPTION_COUNT);
    expect(within(screen.getByRole('radiogroup')).getAllByRole('radio')).toHaveLength(
      OPTION_COUNT,
    );
  });

  it('names every option by the score it selects', () => {
    render(<StarRatingInput value={null} onChange={ignoreScore} />);

    const rendered = options();

    SCORES.forEach((score, index) => {
      expect(rendered[index]).toHaveAccessibleName(optionName(score));
    });
  });

  it('renders every option as a non-submitting button', () => {
    render(<StarRatingInput value={null} onChange={ignoreScore} />);

    options().forEach((option) => {
      expect(option).toHaveAttribute('type', 'button');
    });
  });
});

describe('StarRatingInput selection state', () => {
  it('reports nothing as checked before a score is chosen', () => {
    render(<StarRatingInput value={null} onChange={ignoreScore} />);

    expectCheckedScore(null);
  });

  it('reports exactly the current score as checked', () => {
    render(<StarRatingInput value={MID_SCORE} onChange={ignoreScore} />);

    expectCheckedScore(MID_SCORE);
  });

  it('moves the checked option when a different score is chosen', async () => {
    const user = renderHarness();

    await user.click(optionFor(MID_SCORE));
    expectCheckedScore(MID_SCORE);

    await user.click(optionFor(RATING_MAX));
    expectCheckedScore(RATING_MAX);
  });
});

/*
 * A value the scale cannot represent.
 *
 * `value` is typed `number | null`, so nothing stops a caller passing a score
 * from a differently bounded scale, an uninitialised `0`, or a fractional average
 * meant for a different component. The requirement is not that the component
 * guess what was intended — it is that all FOUR channels agree on one answer,
 * because the alternative is a control that contradicts itself: five filled
 * glyphs for a sighted user, "nothing selected" for a screen reader, and an
 * impossible sentence in between.
 *
 * Each case below therefore asserts the same four things at once — checked state,
 * glyphs, echo and tab stop — and the tab stop matters most: a group with no
 * `tabIndex={0}` is unreachable by keyboard, which is a WCAG 2.1.1 failure rather
 * than a cosmetic one.
 */
describe('StarRatingInput controlled value outside the scale', () => {
  const UNREPRESENTABLE = [RATING_MAX + 1, RATING_MIN - 1, 0, MID_SCORE + 0.5];

  UNREPRESENTABLE.forEach((value) => {
    it(`treats ${value} as no selection, coherently in every channel`, () => {
      render(<StarRatingInput value={value} onChange={ignoreScore} />);

      expectCheckedScore(null);
      expectSingleTabStop(RATING_MIN);
      expect(screen.getByText(NO_SCORE_TEXT)).toBeInTheDocument();
      expect(screen.queryByText(echoText(value))).not.toBeInTheDocument();
    });
  });

  it('fills no glyph for an out-of-scale value, rather than filling all of them', () => {
    const { container } = render(
      <StarRatingInput value={RATING_MAX + 1} onChange={ignoreScore} />,
    );

    // The filled glyph is U+2605 and the outline is U+2606. Asserted on the
    // rendered characters because that is what a sighted reader actually sees;
    // the class names could change without the appearance changing.
    expect(container.textContent).not.toContain('\u2605');
    expect(
      (container.textContent ?? '').split('\u2606').length - 1,
    ).toBe(OPTION_COUNT);
  });

  it('recovers a real selection made after an out-of-scale value', async () => {
    const user = renderHarness({ initial: RATING_MAX + 1 });

    expectCheckedScore(null);

    await user.click(optionFor(MID_SCORE));

    expectCheckedScore(MID_SCORE);
    expect(screen.getByText(echoText(MID_SCORE))).toBeInTheDocument();
  });
});

describe('StarRatingInput roving tabindex', () => {
  it('puts the single tab stop on the first option before any choice', async () => {
    const user = renderHarness();

    expectSingleTabStop(RATING_MIN);

    await user.tab();
    expect(optionFor(RATING_MIN)).toHaveFocus();
  });

  it('puts the single tab stop on the selected option', async () => {
    const user = renderHarness({ initial: MID_SCORE });

    expectSingleTabStop(MID_SCORE);

    await user.tab();
    expect(optionFor(MID_SCORE)).toHaveFocus();
  });

  it('follows the selection with the tab stop as the score changes', async () => {
    const user = renderHarness({ initial: RATING_MIN });

    await user.click(optionFor(RATING_MAX));

    expectSingleTabStop(RATING_MAX);
  });

  it('leaves the group on a second Tab, proving it is one stop and not five', async () => {
    const user = renderHarness({ initial: MID_SCORE });

    await user.tab();
    expect(optionFor(MID_SCORE)).toHaveFocus();

    await user.tab();
    expect(document.body).toHaveFocus();
  });
});

/*
 * WCAG 2.4.7 Focus Visible — the half the tests above cannot reach.
 *
 * Everything before this point proves that focus MOVES: Tab reaches the group,
 * arrows walk it, `document.activeElement` lands where it should. None of it
 * proves that a sighted keyboard user can SEE where focus landed, and the two are
 * separate failures with separate causes. WCAG 2.1 Level AA is a stated
 * requirement (SRS L187, L525, L556-L557), and 2.4.7 is the criterion that a
 * keyboard-operable control most commonly fails while every focus-movement test
 * stays green.
 *
 * The specific hazard is concrete rather than hypothetical. This component sets
 * `focus:outline-none`, which REMOVES the indicator the browser supplies for
 * free, and substitutes a ring. Delete the ring utilities and nothing observable
 * to any other test in this file changes — focus still moves, `aria-checked`
 * still tracks, the echo still updates — while a keyboard user is left with no
 * indication at all of which of the five stars they are on. That is the
 * regression these tests exist to catch.
 *
 * WHY THESE ASSERT ON CLASS NAMES, WHEN NOTHING ELSE IN THIS FILE DOES
 * -----------------------------------------------------------------------------
 * Reluctantly, and because the alternative does not exist here. Proving an
 * indicator is visible needs computed style, which needs the stylesheet — and
 * the styles are Tailwind utilities compiled by PostCSS at build time, so under
 * jsdom every element's computed style is empty whatever classes it carries. A
 * `getComputedStyle` assertion would therefore pass identically against a
 * component with no focus styling whatsoever: worse than no test, because it
 * would read as coverage.
 *
 * So the contract is asserted at the level the source can actually be held to,
 * and it is written as a GUARANTEE rather than as a literal — "the native
 * outline is not suppressed without a sized, offset replacement" — so a
 * legitimate refactor to `focus-visible:`, or to a real outline instead of a
 * ring, keeps passing while a deletion fails. The utility names are matched by
 * pattern for the same reason: `focus:ring-4` or `focus-visible:outline-2` are
 * as acceptable as today's `focus:ring-2`.
 */
describe('StarRatingInput visible focus indicator', () => {
  it('gives every option a focus-state indicator', () => {
    render(<StarRatingInput value={null} onChange={ignoreScore} />);

    options().forEach((option) => {
      expect(focusIndicatorClasses(option).length).toBeGreaterThan(0);
    });
  });

  it('never suppresses the native outline without replacing it', () => {
    render(<StarRatingInput value={MID_SCORE} onChange={ignoreScore} />);

    options().forEach((option) => {
      const classes = classListOf(option);

      if (!classes.some((name) => OUTLINE_SUPPRESSOR.test(name))) {
        // Nothing was taken away, so nothing has to be given back: the
        // browser's own indicator is still in place and is sufficient.
        return;
      }

      // A width, so the replacement is actually drawn. `focus:ring-blue-600`
      // on its own paints nothing.
      expect(
        classes.filter((name) => SIZED_FOCUS_INDICATOR.test(name)),
      ).not.toHaveLength(0);

      // An offset, so the ring sits clear of the glyph it surrounds rather
      // than on top of it, where it is easily mistaken for part of the star.
      expect(
        classes.filter((name) => FOCUS_INDICATOR_OFFSET.test(name)),
      ).not.toHaveLength(0);
    });
  });

  it('carries the indicator on the option that actually receives focus', async () => {
    const user = renderHarness({ initial: MID_SCORE });

    await user.tab();

    // Ties the two halves together. The assertions above could be satisfied by
    // an indicator declared on elements that never take focus; this one starts
    // from `document.activeElement` — reached by a real Tab press — and requires
    // the indicator to be on THAT element.
    const focused = document.activeElement as HTMLElement;

    expect(focused).toBe(optionFor(MID_SCORE));
    expect(focusIndicatorClasses(focused).length).toBeGreaterThan(0);
  });

  it('keeps the indicator on each option as focus moves across the group', async () => {
    const user = renderHarness({ initial: RATING_MIN });
    await user.tab();

    for (let step = 1; step < OPTION_COUNT; step += 1) {
      await user.keyboard('{ArrowRight}');

      const focused = document.activeElement as HTMLElement;

      expect(focused).toBe(optionFor(RATING_MIN + step));
      expect(focusIndicatorClasses(focused).length).toBeGreaterThan(0);
    }
  });
});

/*
 * ARIA Authoring-Practices radio-group keyboard semantics.
 *
 * `MID_SCORE` is an interior score by construction, so `MID_SCORE + 1` and
 * `MID_SCORE - 1` are both inside the scale and the step tests below never
 * accidentally exercise the wrap path — which the four wrap tests own instead.
 *
 * The wrap tests are the ones that would catch the most likely regression here.
 * Clamping at the edges is the intuitive implementation and it is WRONG for a
 * radio group: pressing forward at the top must return to the bottom. Because
 * `expectCheckedScore` asserts all five options, a clamping implementation fails
 * on both halves — the expected score is not checked and the edge score still is.
 *
 * Space and Enter are asserted at the level of the GUARANTEE — "this key operates
 * the control" — rather than by which internal branch delivers it, and that is a
 * deliberate boundary. Because each option is a real `<button>`, both keys reach
 * `onChange` either through the component's own key handling or through native
 * button activation, and a test that pinned one route would fail on a refactor
 * that legitimately chose the other while the user experienced no change at all.
 *
 * What genuinely must not regress is that one keypress reports one choice. If the
 * component handles Space and forgets to suppress the click the platform would
 * otherwise synthesise, `onChange` fires TWICE for a single press. Nothing is
 * persisted by that — this control is a leaf that owns no state, issues no
 * request and touches neither the store nor any service, so the only thing a
 * duplicate call can corrupt is the parent's controlled value: two updates for
 * one press, a doubled entry in whatever history the parent keeps, and any
 * parent-side effect keyed on a change running twice. That failure is pinned by
 * the exact, ordered call log in the `onChange` suite below, not here.
 */
describe('StarRatingInput keyboard adjustment', () => {
  it('advances one score on ArrowRight', async () => {
    await tabInAndPress(MID_SCORE, '{ArrowRight}');

    expectCheckedScore(MID_SCORE + 1);
    expect(optionFor(MID_SCORE + 1)).toHaveFocus();
    expectSingleTabStop(MID_SCORE + 1);
  });

  it('advances one score on ArrowDown', async () => {
    await tabInAndPress(MID_SCORE, '{ArrowDown}');

    expectCheckedScore(MID_SCORE + 1);
    expect(optionFor(MID_SCORE + 1)).toHaveFocus();
  });

  it('retreats one score on ArrowLeft', async () => {
    await tabInAndPress(MID_SCORE, '{ArrowLeft}');

    expectCheckedScore(MID_SCORE - 1);
    expect(optionFor(MID_SCORE - 1)).toHaveFocus();
    expectSingleTabStop(MID_SCORE - 1);
  });

  it('retreats one score on ArrowUp', async () => {
    await tabInAndPress(MID_SCORE, '{ArrowUp}');

    expectCheckedScore(MID_SCORE - 1);
    expect(optionFor(MID_SCORE - 1)).toHaveFocus();
  });

  it('wraps from the highest score round to the lowest on ArrowRight', async () => {
    await tabInAndPress(RATING_MAX, '{ArrowRight}');

    expectCheckedScore(RATING_MIN);
    expect(optionFor(RATING_MIN)).toHaveFocus();
  });

  it('wraps from the highest score round to the lowest on ArrowDown', async () => {
    await tabInAndPress(RATING_MAX, '{ArrowDown}');

    expectCheckedScore(RATING_MIN);
  });

  it('wraps from the lowest score round to the highest on ArrowLeft', async () => {
    await tabInAndPress(RATING_MIN, '{ArrowLeft}');

    expectCheckedScore(RATING_MAX);
    expect(optionFor(RATING_MAX)).toHaveFocus();
  });

  it('wraps from the lowest score round to the highest on ArrowUp', async () => {
    await tabInAndPress(RATING_MIN, '{ArrowUp}');

    expectCheckedScore(RATING_MAX);
  });

  it('selects the lowest score on Home', async () => {
    await tabInAndPress(RATING_MAX, '{Home}');

    expectCheckedScore(RATING_MIN);
    expect(optionFor(RATING_MIN)).toHaveFocus();
  });

  it('selects the highest score on End', async () => {
    await tabInAndPress(RATING_MIN, '{End}');

    expectCheckedScore(RATING_MAX);
    expect(optionFor(RATING_MAX)).toHaveFocus();
  });

  it('selects the focused option on Space when nothing is selected yet', async () => {
    const user = renderHarness();

    await user.tab();
    expect(optionFor(RATING_MIN)).toHaveFocus();
    expect(optionFor(RATING_MIN)).toHaveAttribute('aria-checked', 'false');

    await user.keyboard('{ }');

    expectCheckedScore(RATING_MIN);
  });

  it('selects the focused option on Enter, via native button activation', async () => {
    await tabInAndPress(null, '{Enter}');

    expectCheckedScore(RATING_MIN);
  });

  it('leaves the selection untouched for keys it does not handle', async () => {
    await tabInAndPress(MID_SCORE, '{Escape}');

    expectCheckedScore(MID_SCORE);
    expect(optionFor(MID_SCORE)).toHaveFocus();
  });
});

/*
 * The redundant textual channel.
 *
 * WCAG 1.4.1 forbids conveying information by colour alone, and a star strip
 * conveys its value through exactly that plus glyph shape. The rendered sentence
 * is the third, non-visual channel, so these tests are what stop it being
 * dropped as "duplicate" of the stars.
 *
 * It must also sit OUTSIDE the group: a sixth child of a `radiogroup` is
 * announced as a malformed group member, which turns an accessibility aid into an
 * accessibility defect.
 */
describe('StarRatingInput textual score echo', () => {
  it('states that no score is selected before a choice is made', () => {
    render(<StarRatingInput value={null} onChange={ignoreScore} />);

    expect(screen.getByText(NO_SCORE_TEXT)).toBeInTheDocument();
    expect(screen.queryByText(echoText(MID_SCORE))).not.toBeInTheDocument();
  });

  it('states the chosen score in words, replacing the empty state', async () => {
    const user = renderHarness();

    expect(screen.getByText(NO_SCORE_TEXT)).toBeInTheDocument();

    await user.click(optionFor(MID_SCORE));

    expect(screen.getByText(echoText(MID_SCORE))).toBeInTheDocument();
    expect(screen.queryByText(NO_SCORE_TEXT)).not.toBeInTheDocument();
  });

  it('keeps the echo in step with every subsequent change', async () => {
    const user = renderHarness({ initial: RATING_MIN });

    expect(screen.getByText(echoText(RATING_MIN))).toBeInTheDocument();

    await user.tab();
    await user.keyboard('{End}');

    expect(screen.getByText(echoText(RATING_MAX))).toBeInTheDocument();
    expect(screen.queryByText(echoText(RATING_MIN))).not.toBeInTheDocument();
  });

  it('renders the echo outside the radio group, not as a group member', () => {
    render(<StarRatingInput value={MID_SCORE} onChange={ignoreScore} />);

    const group = screen.getByRole('radiogroup');
    const echo = screen.getByText(echoText(MID_SCORE));

    expect(echo).toBeInTheDocument();
    expect(group).not.toContainElement(echo);
    expect(
      within(group).queryByText(echoText(MID_SCORE)),
    ).not.toBeInTheDocument();
  });
});

/*
 * An unrepresentable controlled value.
 *
 * `value` is typed `number | null`, so a parent can pass a number this scale
 * cannot represent: a stale score from a widened scale, an uninitialised `0`, a
 * fraction taken from an average, or a parsed query parameter. The component is
 * controlled and cannot correct its own prop, so the only question is what it
 * RENDERS - and every channel has to give the same answer.
 *
 * They used to disagree. The roving tab stop was guarded and `aria-checked`
 * reported the raw value, but the stars filled on `score <= value` and the echo
 * interpolated the raw value - so `value={RATING_MAX + 5}` lit every star and
 * announced "10 out of 5" while telling assistive technology that nothing was
 * checked. A visual claim of a maximum score that a screen reader denies is worse
 * than either answer alone, and the number shown was not on the scale at all.
 *
 * The whole block asserts one rule: an unrepresentable value renders as the
 * UNSELECTED state, in every channel at once, and is never clamped into a score
 * the user did not choose.
 */
describe('StarRatingInput unrepresentable controlled value', () => {
  const unrepresentable = [
    ['above the scale', RATING_MAX + 5],
    ['below the scale', RATING_MIN - 1],
    ['zero', 0],
    ['negative', -3],
    ['fractional', RATING_MIN + 0.5],
    ['not a number', Number.NaN],
  ] as const;

  unrepresentable.forEach(([label, value]) => {
    it(`reports nothing as checked for a value ${label}`, () => {
      render(<StarRatingInput value={value} onChange={ignoreScore} />);

      expectCheckedScore(null);
    });

    it(`echoes the empty state for a value ${label}`, () => {
      render(<StarRatingInput value={value} onChange={ignoreScore} />);

      expect(screen.getByText(NO_SCORE_TEXT)).toBeInTheDocument();
      // The raw value must not appear anywhere in the rendered text - not as
      // "10 out of 5", and not on its own either.
      expect(
        screen.queryByText(new RegExp(`${String(value).replace('.', '\\.')}`)),
      ).not.toBeInTheDocument();
    });

    it(`fills no star for a value ${label}`, () => {
      const { container } = render(
        <StarRatingInput value={value} onChange={ignoreScore} />,
      );

      // Read from the DOM rather than from a class name: the filled and empty
      // glyphs are the visual channel, and a filled star beside "No score
      // selected" is the contradiction this asserts against.
      const text = container.textContent ?? '';

      expect(text).not.toContain(FILLED_STAR);
      expect(
        (text.match(new RegExp(EMPTY_STAR, 'g')) ?? []).length,
      ).toBe(OPTION_COUNT);
    });

    it(`keeps exactly one tab stop for a value ${label}`, () => {
      render(<StarRatingInput value={value} onChange={ignoreScore} />);

      // WCAG 2.1.1: whatever the prop says, the control stays reachable. The
      // first option carries the tab stop when nothing is selected.
      expectSingleTabStop(RATING_MIN);
    });
  });

  it('recovers to a real selection once the parent supplies one', async () => {
    // The unrepresentable value is not sticky: it renders as unselected and the
    // very next legitimate choice behaves exactly as it would have from `null`.
    const user = renderHarness({ initial: RATING_MAX + 5 });

    expectCheckedScore(null);
    expect(screen.getByText(NO_SCORE_TEXT)).toBeInTheDocument();

    await user.click(optionFor(MID_SCORE));

    expectCheckedScore(MID_SCORE);
    expect(screen.getByText(echoText(MID_SCORE))).toBeInTheDocument();
  });
});

/*
 * The reported value.
 *
 * `onChange` is observed through a plain closure that appends to a typed array
 * rather than through a mock function, deliberately: this suite needs no mocking at
 * all, and reaching for a spy just to keep a call log would pull an otherwise
 * unused mocking facility into a mock-free file. An array also asserts ORDER and
 * COUNT, which is what the "exactly once" guarantee actually needs — a
 * double-fired handler would hand the parent two updates for a single user
 * action, so the parent's controlled value, and any effect it keys on a change,
 * would run twice for one choice.
 */
describe('StarRatingInput onChange contract', () => {
  it('reports the chosen score as a number, once per pointer choice', async () => {
    const reported: number[] = [];
    const user = renderHarness({
      onSelect: (score) => {
        reported.push(score);
      },
    });

    await user.click(optionFor(MID_SCORE));

    expect(reported).toEqual([MID_SCORE]);
  });

  it('reports each keyboard adjustment exactly once, in order', async () => {
    const reported: number[] = [];
    const user = renderHarness({
      initial: RATING_MIN,
      onSelect: (score) => {
        reported.push(score);
      },
    });

    await user.tab();
    await user.keyboard('{ArrowRight}{End}{Home}{ }');

    expect(reported).toEqual([
      RATING_MIN + 1,
      RATING_MAX,
      RATING_MIN,
      RATING_MIN,
    ]);
  });

  it('never reports a score while the group is disabled', async () => {
    const reported: number[] = [];
    const user = renderHarness({
      disabled: true,
      onSelect: (score) => {
        reported.push(score);
      },
    });

    expect(screen.getByRole('radiogroup')).toHaveAttribute(
      'aria-disabled',
      'true',
    );
    options().forEach((option) => {
      expect(option).toBeDisabled();
    });

    await user.tab();
    expect(document.body).toHaveFocus();

    await user.click(optionFor(MID_SCORE));

    expect(reported).toEqual([]);
    expectCheckedScore(null);
  });
});

/*
 * REQUIRED STATE
 * -----------------------------------------------------------------------------
 * The score is mandatory in the form that owns this control, and a mandatory field
 * has to SAY so: WCAG 3.3.2 asks for the instruction to be available to a sighted
 * reader, and `aria-required` is what an assistive technology reports on entry.
 * Previously neither existed, so the only signal that a score was needed was a
 * submit button that silently refused to respond.
 *
 * Both channels are asserted, and so is their ABSENCE by default — a control that
 * announced everything as required would be no more informative than one that
 * announced nothing.
 */
describe('StarRatingInput required state', () => {
  it('marks the group as required when the form says it is', () => {
    render(<StarRatingInput value={null} onChange={ignoreScore} required />);

    expect(screen.getByRole('radiogroup')).toHaveAttribute(
      'aria-required',
      'true',
    );
  });

  it('states the requirement visibly, not only to assistive technology', () => {
    render(<StarRatingInput value={null} onChange={ignoreScore} required />);

    // A word rather than an asterisk: `*` is announced inconsistently, means
    // nothing without a key, and is easy to miss at this size.
    expect(screen.getByText(/\(required\)/i)).toBeInTheDocument();
  });

  it('carries the requirement in the group accessible name', () => {
    render(
      <StarRatingInput
        value={null}
        onChange={ignoreScore}
        label="Your rating"
        required
      />,
    );

    // The marker lives INSIDE the labelling element, so it is part of the name the
    // group is announced with rather than a detached note a reader may never reach.
    expect(screen.getByRole('radiogroup')).toHaveAccessibleName(
      'Your rating (required)',
    );
  });

  it('asserts nothing about requirement by default', () => {
    render(
      <StarRatingInput
        value={null}
        onChange={ignoreScore}
        label="Your rating"
      />,
    );

    expect(screen.getByRole('radiogroup')).not.toHaveAttribute('aria-required');
    expect(screen.queryByText(/\(required\)/i)).not.toBeInTheDocument();
    expect(screen.getByRole('radiogroup')).toHaveAccessibleName('Your rating');
  });

  it('changes nothing else about the control', () => {
    render(
      <StarRatingInput value={MID_SCORE} onChange={ignoreScore} required />,
    );

    // Requirement is a statement about the field, not a change of behaviour: the
    // same five options, the same selection, the same echo.
    expect(options()).toHaveLength(OPTION_COUNT);
    expectCheckedScore(MID_SCORE);
    expect(
      screen.getByText(`${MID_SCORE} out of ${RATING_MAX}`),
    ).toBeInTheDocument();
  });
});

/*
 * IMPERATIVE FOCUS
 * -----------------------------------------------------------------------------
 * The parent form has two transitions that delete the element the user is standing
 * on — a successful submit and a successful retry — and after the retry the right
 * destination is the first enabled control, which is this group. Focus cannot be
 * expressed as rendered state, so the control exposes one method for it. These
 * tests pin the contract the form depends on: focus lands on the group's single tab
 * stop, and a disabled group is never made the destination.
 */
describe('StarRatingInput imperative focus', () => {
  const FocusHarness = ({
    value,
    disabled = false,
  }: {
    value: number | null;
    disabled?: boolean;
  }) => {
    const handle = useRef<StarRatingInputHandle | null>(null);

    return (
      <div>
        <button type="button" onClick={() => handle.current?.focus()}>
          move focus
        </button>
        <StarRatingInput
          ref={handle}
          value={value}
          onChange={ignoreScore}
          disabled={disabled}
        />
      </div>
    );
  };

  it('focuses the first option when nothing is selected yet', async () => {
    const user = userEvent.setup();
    render(<FocusHarness value={null} />);

    await user.click(screen.getByRole('button', { name: 'move focus' }));

    expect(optionFor(RATING_MIN)).toHaveFocus();
  });

  it('focuses the selected option, which is the group tab stop', async () => {
    const user = userEvent.setup();
    render(<FocusHarness value={MID_SCORE} />);

    await user.click(screen.getByRole('button', { name: 'move focus' }));

    expect(optionFor(MID_SCORE)).toHaveFocus();
    expect(optionFor(MID_SCORE)).toHaveAttribute('tabindex', '0');
  });

  it('does not move focus into a disabled group', async () => {
    const user = userEvent.setup();
    render(<FocusHarness value={MID_SCORE} disabled />);

    const trigger = screen.getByRole('button', { name: 'move focus' });
    await user.click(trigger);

    // Focusing an inert control is a dead end: it reports "not now" and offers
    // nowhere to go, so the caller does not have to know the state to call safely.
    expect(optionFor(MID_SCORE)).not.toHaveFocus();
  });
});
