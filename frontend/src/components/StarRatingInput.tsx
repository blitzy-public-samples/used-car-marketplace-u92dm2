import React, { useId, useRef } from 'react';
import { RATING_MIN, RATING_MAX } from '../schema/rating';

/**
 * Accessible 1..5 star score control for the bidirectional peer reputation
 * system (F010 "Review and Rating System", `documentation/Software Requirements
 * Specifications (SRS).md`; F010-1 "User rating submission interface").
 *
 * This is the score half of a rating submission. The optional written review,
 * the eligibility messaging and the network call all belong to the parent
 * `RatingSubmissionForm`; this component renders a value and reports a new one.
 *
 * WHY A RADIO GROUP AND NOT FIVE CLICKABLE DIVS
 * -----------------------------------------------------------------------------
 * WCAG 2.1 Level AA is a stated non-functional requirement of this project
 * ("Adherence to Web Content Accessibility Guidelines (WCAG) 2.1 Level AA",
 * "Support for screen readers and keyboard navigation"). A star strip built from
 * click handlers on non-interactive elements is the canonical way to fail it: it
 * is unreachable by keyboard, exposes no role or state, and communicates its
 * value through colour alone.
 *
 * No component library is installed in this repository — there is no `Rate`,
 * `Radio`, `RadioGroup` or `Button` to reuse — so the accessibility a library
 * component would have supplied is delivered here explicitly, by being a real
 * radio group: `role="radiogroup"` with an accessible name, five `role="radio"`
 * children each carrying its own accessible name and `aria-checked`, ARIA
 * Authoring-Practices keyboard semantics, a roving tabindex so the group is a
 * single Tab stop, a visible focus indicator at every position, and a redundant
 * textual echo of the value.
 *
 * SELECTION IS NEVER CARRIED BY COLOUR ALONE
 * -----------------------------------------------------------------------------
 * Three independent, redundant channels express the same state, so it survives
 * greyscale, colour-blindness, and a screen reader:
 *   1. glyph   — a filled star for a selected position, an outline star beyond it
 *   2. colour  — a warm tone up to the score, a neutral tone beyond it
 *   3. text    — a rendered sentence ("3 out of 5") plus `aria-checked`
 * Removing any one of the three still leaves the value legible.
 *
 * PRESENTATION IS SENTIMENT-NEUTRAL, DELIBERATELY
 * -----------------------------------------------------------------------------
 * Every score from `RATING_MIN` to `RATING_MAX` is presented in identical
 * weight, size, spacing and colour treatment. There is no red/amber/green
 * tiering by value, no warning styling on a low score, and no discouraging or
 * persuasive copy anywhere. A control that visually editorialises against low
 * scores nudges raters upward and corrupts the aggregate it feeds, which is
 * exactly the score-correlated behaviour the feature's moderation policy
 * forbids. Neutrality here is a correctness property of the reputation data,
 * not a stylistic preference.
 *
 * CONTROLLED, AND FREE OF SIDE EFFECTS
 * -----------------------------------------------------------------------------
 * The selected score is owned entirely by the parent: this component holds no
 * score state, runs no effect, performs no fetch, and touches neither the Redux
 * store nor any service module. Its only two imports are React and the shared
 * score bounds. That makes it trivially testable — it renders under no provider
 * and with no mocking — and it makes the component reusable for displaying a
 * submitted score as readily as for collecting a new one.
 */
interface StarRatingInputProps {
  /**
   * The currently selected score, or `null` when nothing has been selected yet.
   *
   * `null` is a first-class state rather than a stand-in for zero: it renders
   * every star as an outline and echoes "No score selected", so the pre-choice
   * condition is visually and audibly distinct from any real score.
   */
  value: number | null;

  /**
   * Called with the newly selected score whenever the user picks one, by click
   * or by keyboard. Never called while `disabled`, and never called with a
   * value outside `RATING_MIN..RATING_MAX`.
   */
  onChange: (score: number) => void;

  /**
   * Visible group label, which is also the group's accessible name via
   * `aria-labelledby`. Defaults to `'Rating'`.
   */
  label?: string;

  /**
   * Renders the group inert: no pointer or keyboard interaction reaches
   * `onChange`. Defaults to `false`.
   */
  disabled?: boolean;
}

/**
 * The selectable scores, ascending, derived from the shared bounds rather than
 * hardcoded. `RATING_MIN`/`RATING_MAX` mirror the server's `settings.RATING_MIN`
 * and `settings.RATING_MAX`, which bound the Pydantic field that authoritatively
 * validates the submission — so widening the scale server-side widens this
 * control with no edit here.
 *
 * Computed once at module scope: the bounds are module constants, so the result
 * is invariant and there is nothing to recompute per render.
 */
const SCORES = Array.from(
  { length: RATING_MAX - RATING_MIN + 1 },
  (_, index) => RATING_MIN + index,
);

/**
 * Star glyphs, written as escapes rather than literal characters so the rendered
 * output cannot be altered by a re-encoding of this file.
 *
 * U+2605 BLACK STAR marks a position at or below the score; U+2606 WHITE STAR
 * marks a position beyond it. Both are decorative — each is wrapped in
 * `aria-hidden` and the real name comes from the option's `aria-label` — so a
 * screen reader announces "Rate 3 out of 5", never "black star".
 */
const FILLED_STAR = '\u2605';
const EMPTY_STAR = '\u2606';

/**
 * Classes shared by all five options, in every state.
 *
 * `min-h-11 min-w-11` is the 2.75rem (44px) step of Tailwind's spacing scale,
 * giving each option the recommended minimum touch target from the scale itself
 * rather than from a hardcoded dimension. `min-*` rather than a fixed size so a
 * reader who scales text up enlarges the target instead of clipping the glyph.
 *
 * `focus:outline-none` is only safe because `focus:ring-2 focus:ring-offset-2`
 * replaces what it removes: suppressing the native outline without substituting
 * a visible indicator is itself a WCAG failure, so these three always travel
 * together. The offset lifts the ring clear of the glyph so it stays legible.
 *
 * The transition is scoped to `motion-safe:`, which Tailwind emits as
 * `@media (prefers-reduced-motion: no-preference)`, so a reader who has asked
 * for reduced motion gets an instant colour change instead.
 */
const OPTION_BASE_CLASSES = [
  'inline-flex min-h-11 min-w-11 items-center justify-center',
  'rounded-md text-2xl leading-none',
  'focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-blue-600',
  'motion-safe:transition-colors motion-safe:duration-150 motion-safe:ease-out',
].join(' ');

/**
 * State-dependent classes for one option.
 *
 * Colour is chosen for contrast, not decoration. Against the white page these
 * clear WCAG 1.4.11's 3:1 minimum for a control's state indicator, which the
 * lighter steps of the same scales do not: `yellow-600` reaches roughly 3.0:1
 * and `gray-500` roughly 4.8:1, where `yellow-500` manages only about 1.9:1 and
 * `gray-300` about 1.6:1. No design source overrides these minimums for this
 * project, so they are met rather than deferred.
 *
 * The hover tone is identical for every option — including options below the
 * current score — so hovering communicates "this is interactive" and never
 * "this score is better". Selected and unselected options differ only in the
 * resting tone that pairs with the glyph.
 *
 * Disabled options drop to a muted neutral with a `not-allowed` cursor. Inactive
 * controls are explicitly exempt from the contrast minimums, and the muting is
 * uniform across all five, so a disabled group reveals nothing about the score
 * it would have accepted.
 */
const optionStateClasses = (isFilled: boolean, isDisabled: boolean): string => {
  if (isDisabled) {
    return 'cursor-not-allowed text-gray-400';
  }

  return isFilled
    ? 'text-yellow-600 hover:text-yellow-700'
    : 'text-gray-500 hover:text-yellow-700';
};

const StarRatingInput: React.FC<StarRatingInputProps> = ({
  value,
  onChange,
  label = 'Rating',
  disabled = false,
}) => {
  /**
   * Ties the visible label to the group's `aria-labelledby`. `useId` keeps that
   * association correct when several of these controls share a page — two
   * groups with a hardcoded id would both point at the first label.
   */
  const labelId = useId();

  /**
   * The five option elements, indexed by position, so arrow-key navigation can
   * move real DOM focus. Populated by the `ref` callback on each option.
   */
  const optionRefs = useRef<Array<HTMLButtonElement | null>>([]);

  /**
   * The option that holds the group's single tab stop.
   *
   * A radio group is one Tab stop: the selected option is what Tab reaches, and
   * arrow keys move within the group. Before anything is selected, the first
   * option carries it.
   *
   * The `SCORES.includes` guard is load-bearing, not defensive noise. `value` is
   * typed `number | null`, so a caller can legitimately pass a value outside the
   * scale — a stale score from a widened scale, or an uninitialised `0`. Under a
   * bare `value === score` test that case gives the group NO element with
   * `tabIndex={0}`, making the whole control unreachable by keyboard and failing
   * WCAG 2.1.1. Falling back to the first option guarantees exactly one tab stop
   * for every possible value of the prop.
   *
   * `aria-checked` is deliberately NOT derived from this fallback: it reports the
   * true `value`, so an out-of-range score correctly presents as "nothing
   * selected" rather than silently claiming the first option is chosen.
   */
  const activeScore =
    value !== null && SCORES.includes(value) ? value : RATING_MIN;

  /**
   * Commits a score and moves focus onto it.
   *
   * Both halves are required. Selection without focus leaves the roving tabindex
   * pointing at an element the user is not on, so the next arrow key would move
   * from the wrong origin; focus without selection is not radio-group behaviour,
   * where moving within the group also chooses.
   */
  const selectScore = (score: number): void => {
    onChange(score);
    optionRefs.current[score - RATING_MIN]?.focus();
  };

  /**
   * ARIA Authoring-Practices keyboard semantics for a radio group.
   *
   * Both arrow axes are honoured, because a horizontal strip may be read as
   * either orientation, and both WRAP rather than clamp — moving forward past
   * the top score returns to the bottom, and back past the bottom returns to the
   * top. `Home` and `End` jump to the extremes. `Space` selects the focused
   * option, which is what makes the group usable when Tab arrived at a group
   * where nothing was selected yet.
   *
   * `preventDefault` is called only for keys actually handled, so arrows and
   * Space do not additionally scroll the page while every other key — Tab above
   * all, which must keep moving focus out of the group — behaves natively.
   *
   * Handling Space here and preventing the default also suppresses the click a
   * button would otherwise synthesise from it, so `onChange` fires exactly once
   * per keypress. `Enter` is intentionally not intercepted: the native button
   * activation already routes it through the click handler and selects the
   * focused option.
   */
  const handleKeyDown = (
    event: React.KeyboardEvent<HTMLButtonElement>,
    score: number,
  ): void => {
    if (disabled) {
      return;
    }

    let nextScore: number;

    switch (event.key) {
      case 'ArrowRight':
      case 'ArrowDown':
        nextScore = score === RATING_MAX ? RATING_MIN : score + 1;
        break;
      case 'ArrowLeft':
      case 'ArrowUp':
        nextScore = score === RATING_MIN ? RATING_MAX : score - 1;
        break;
      case 'Home':
        nextScore = RATING_MIN;
        break;
      case 'End':
        nextScore = RATING_MAX;
        break;
      case ' ':
        nextScore = score;
        break;
      default:
        return;
    }

    event.preventDefault();
    selectScore(nextScore);
  };

  /**
   * Pointer selection. Focus is moved explicitly rather than left to the
   * browser: several browsers do not focus a button on click, which would leave
   * the group's focus origin stale and break the first arrow key pressed after a
   * click.
   */
  const handleClick = (score: number): void => {
    if (disabled) {
      return;
    }

    selectScore(score);
  };

  /**
   * The redundant textual channel, so the value is never conveyed by colour or
   * shape alone.
   *
   * No live region: each option already announces its own state through
   * `aria-checked` as focus moves across the group, so announcing this sentence
   * as well would say the same thing twice on every keystroke.
   */
  const scoreEcho =
    value === null ? 'No score selected' : `${value} out of ${RATING_MAX}`;

  return (
    <div className="flex flex-col gap-2">
      <span id={labelId} className="text-base font-semibold text-gray-800">
        {label}
      </span>

      <div
        role="radiogroup"
        aria-labelledby={labelId}
        aria-disabled={disabled}
        className="flex items-center gap-1"
      >
        {SCORES.map((score, index) => {
          const isFilled = value !== null && score <= value;

          return (
            <button
              key={score}
              /*
               * A statement-block body, not an expression body: an
               * expression-bodied arrow returns the assigned element, and React
               * 18 treats a value returned from a ref callback as a cleanup
               * function.
               */
              ref={(element) => {
                optionRefs.current[index] = element;
              }}
              /*
               * Mandatory. This control renders inside the parent form, where a
               * button defaults to `type="submit"` — without this, choosing a
               * star would submit the rating instead of selecting a score.
               */
              type="button"
              role="radio"
              aria-checked={value === score}
              /*
               * Names the option by the value it selects — "Rate 3 out of 5" —
               * so the choice is unambiguous when announced out of visual
               * context. The glyph alone would announce as a star character.
               */
              aria-label={`Rate ${score} out of ${RATING_MAX}`}
              tabIndex={score === activeScore ? 0 : -1}
              disabled={disabled}
              onClick={() => handleClick(score)}
              onKeyDown={(event) => handleKeyDown(event, score)}
              className={`${OPTION_BASE_CLASSES} ${optionStateClasses(
                isFilled,
                disabled,
              )}`}
            >
              <span aria-hidden="true">
                {isFilled ? FILLED_STAR : EMPTY_STAR}
              </span>
            </button>
          );
        })}
      </div>

      {/*
        Rendered as a sibling of the group, never inside it: a sixth child of a
        radiogroup would be announced as a malformed group member.
      */}
      <p className="text-sm text-gray-600">{scoreEcho}</p>
    </div>
  );
};

export default StarRatingInput;
