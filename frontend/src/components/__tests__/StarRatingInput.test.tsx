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

import { useState } from 'react';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import StarRatingInput from '../StarRatingInput';
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
 * What genuinely must not regress is the arithmetic of it: if the component
 * handles Space and forgets to suppress the click the platform would otherwise
 * synthesise, `onChange` fires twice for one keypress and writes the same score
 * into the reputation aggregate twice. That failure is pinned — by the exact,
 * ordered call log in the `onChange` suite below, not here.
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
 * The reported value.
 *
 * `onChange` is observed through a plain closure that appends to a typed array
 * rather than through a mock function, deliberately: this suite needs no mocking at
 * all, and reaching for a spy just to keep a call log would pull an otherwise
 * unused mocking facility into a mock-free file. An array also asserts ORDER and
 * COUNT, which is what the "exactly once" guarantee actually needs — a
 * double-fired handler would write two identical scores into the reputation
 * aggregate.
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

