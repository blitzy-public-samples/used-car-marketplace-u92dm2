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
 * single Tab stop, a visible focus indicator at every position, `aria-required`
 * with matching visible copy when the owning form makes the score mandatory, and a
 * redundant textual echo of the value.
 *
 * FOCUS CAN BE MOVED IN, BECAUSE A PARENT SOMETIMES HAS TO
 * -----------------------------------------------------------------------------
 * The component forwards a ref exposing one method, `focus()`, which lands on the
 * group's current tab stop. `RatingSubmissionForm` needs it for a case rendering
 * cannot solve: when its retry succeeds, the button the user was standing on is
 * removed from the page, and focus would otherwise fall back to the document body,
 * losing a keyboard user's place entirely (WCAG 2.4.3). Exposing the method here
 * is what keeps the parent from querying into this component's markup for
 * `[role="radio"]`, which would couple it to internals it does not own.
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
   *
   * A number the scale cannot represent — `0`, `6`, `4.5`, a stale score from a
   * differently bounded scale — is treated as exactly that same "nothing is
   * selected" state, coherently in every channel: no glyph is filled, no option
   * is `aria-checked`, the echo says "No score selected", and the first option
   * still carries the group's tab stop. The component never renders a value it
   * cannot honestly represent, and never announces one thing while showing
   * another.
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

  /**
   * Declares the score mandatory in the form that owns this control. Defaults to
   * `false`.
   *
   * When set, the requirement is expressed TWICE, in the two channels that carry
   * it independently: `aria-required="true"` on the group, and the word
   * "(required)" inside the visible label — which is the element the group is
   * named by, so the accessible name becomes "Your rating (required)" and a screen
   * reader states the obligation as part of the name rather than only as a
   * property.
   *
   * Both are needed. A sighted user never hears `aria-required`, and WCAG 3.3.2
   * asks for the instruction to be visible; a screen-reader user benefits from the
   * attribute because it is announced on entry, before the label is re-read. And a
   * form whose submit button is simply disabled until a score is chosen explains
   * nothing about WHY — the requirement has to be stated before the attempt, not
   * discovered by its absence.
   *
   * It is deliberately NOT the native `required` attribute on each option. These
   * options are `<button role="radio">` rather than `<input type="radio">`, so
   * `required` neither validates nor announces anything on them, and a native
   * constraint would additionally invite the browser's own validation bubble into
   * a form that reports its refusals through its own live region.
   */
  required?: boolean;
}

/**
 * The imperative surface this control exposes to the form that owns it.
 *
 * Deliberately one method. A parent that needs to move focus INTO the group — after
 * a control it was standing on has been removed from the page, which is the case
 * `RatingSubmissionForm` has — cannot do it by rendering: focus is not derivable
 * from state, and querying into another component's DOM for `[role="radio"]` would
 * couple the parent to markup it does not own.
 */
export interface StarRatingInputHandle {
  /**
   * Moves focus to the group's single tab stop — the selected option, or the first
   * option when nothing is selected yet.
   *
   * A no-op while the group is disabled, because focusing an inert control is a
   * dead end for a keyboard user: it reports "not now" and offers nowhere to go.
   * The caller therefore does not have to know the group's state to call this
   * safely.
   */
  focus: () => void;
}

/**
 * The selectable scores, ascending, derived from the shared bounds rather than
 * hardcoded.
 *
 * `RATING_MIN`/`RATING_MAX` are the client half of a cross-stack contract that
 * NOTHING ENFORCES MECHANICALLY. `settings.RATING_MIN` and `settings.RATING_MAX`
 * are ordinary defaulted settings on the server, overridable from the
 * environment, so this control trusts the numbers by convention rather than by
 * construction. It is deliberately NOT true that "widening the scale
 * server-side widens this control with no edit here" — this file is compiled
 * into a separate artefact that cannot read a server environment variable, so a
 * server-only change leaves the two disagreeing: an extra star the server
 * answers with 422, or a missing one a rater cannot choose. Changing the scale
 * means editing both sides and shipping them together.
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
 * COLOUR IS CHOSEN FOR MEASURED CONTRAST, NOT FOR DECORATION. A filled star is
 * the state indicator of a control, so WCAG 1.4.11 holds it to 3:1 against the
 * white page, and the figures below are computed rather than eyeballed —
 * relative luminance per WCAG's own formula, against `#ffffff`:
 *
 *   yellow-500 #eab308 → 1.92:1   FAILS
 *   yellow-600 #ca8a04 → 2.94:1   FAILS — close enough to look fine and still short
 *   yellow-700 #a16207 → 4.92:1   PASSES, and is the resting tone below
 *   yellow-800 #854d0e → 6.85:1   PASSES, hover
 *   yellow-900 #713f12 → 8.67:1   PASSES, pressed
 *   gray-500   #6b7280 → 4.83:1   PASSES, unselected resting tone
 *   gray-300   #d1d5db → 1.47:1   FAILS
 *
 * `yellow-600` was previously the resting tone for a filled star and measures
 * 2.94:1 — under the threshold, by a margin small enough that it reads as
 * acceptable on a good display and disappears on a poor one or for a reader with
 * low vision. It is exactly the kind of near-miss the measurement exists to
 * catch, so the scale is stepped one further to `yellow-700`. No design source
 * overrides these minimums for this project, so they are met rather than
 * deferred, and each tone is a Tailwind default-scale token rather than a
 * hand-picked hex.
 *
 * The hover and pressed tones are identical for every option — including options
 * below the current score — so pointer feedback communicates "this is
 * interactive" and never "this score is better". Selected and unselected options
 * differ only in the resting tone that pairs with the glyph.
 *
 * FOUR STATES, NOT THREE. `hover:` says "this is interactive", the base focus
 * ring says "you are here", `disabled:` says "not now", and `active:` says "your
 * press registered" — the feedback between pressing a star and the selection
 * committing. Tailwind emits `active` after `hover`, so the pressed tone wins
 * while the pointer is down, and a disabled option is never `:active` at all, so
 * the disabled branch below needs no pressed variant to override.
 *
 * Disabled options drop to a muted neutral with a `not-allowed` cursor, and carry
 * neither hover nor pressed variants. Inactive controls are explicitly exempt
 * from the contrast minimums, and the muting is uniform across all five, so a
 * disabled group reveals nothing about the score it would have accepted.
 */
const optionStateClasses = (isFilled: boolean, isDisabled: boolean): string => {
  if (isDisabled) {
    return 'cursor-not-allowed text-gray-400';
  }

  return isFilled
    ? 'text-yellow-700 hover:text-yellow-800 active:text-yellow-900'
    : 'text-gray-500 hover:text-yellow-800 active:text-yellow-900';
};

const StarRatingInput = React.forwardRef<
  StarRatingInputHandle,
  StarRatingInputProps
>(function StarRatingInput(
  { value, onChange, label = 'Rating', disabled = false, required = false },
  ref,
) {
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
   * The selected score, or `null` — ONE validated value that every channel reads.
   *
   * `value` is typed `number | null`, so a caller can legitimately hand over
   * something the scale cannot represent: a stale score from a widened scale, an
   * uninitialised `0`, a fractional average passed to the wrong component. This
   * is the single place that judgement is made, and everything downstream —
   * `aria-checked`, the glyph fill, the textual echo and the tab-stop fallback —
   * derives from the result.
   *
   * That centralisation is the fix for a real incoherence rather than a tidiness
   * preference. When each channel decided for itself, a `value` of 6 produced a
   * control that contradicted itself in three directions at once: the tab stop
   * fell back to the first option, `score <= value` filled ALL FIVE glyphs, the
   * echo announced "6 out of 5", and `aria-checked` was false everywhere. A
   * sighted user saw a full five-star selection, a screen-reader user was told
   * nothing was selected, and the text said something impossible. Now an
   * unrepresentable value is one state — "nothing is selected" — expressed
   * identically in all four channels.
   *
   * `Number.isInteger` is checked as well as membership because a non-integer can
   * never be a member and the intent is to reject it explicitly rather than by
   * accident.
   */
  const selectedScore =
    value !== null && Number.isInteger(value) && SCORES.includes(value)
      ? value
      : null;


  /**
   * The option that holds the group's single tab stop.
   *
   * A radio group is one Tab stop: the selected option is what Tab reaches, and
   * arrow keys move within the group. Before anything is selected — including
   * when the controlled value is one this scale cannot represent — the first
   * option carries it.
   *
   * The fallback is load-bearing, not defensive noise. Under a bare
   * `value === score` test an unrepresentable value gives the group NO element
   * with `tabIndex={0}`, making the whole control unreachable by keyboard and
   * failing WCAG 2.1.1. Falling back to the first option guarantees exactly one
   * tab stop for every possible value of the prop.
   *
   * `aria-checked` is deliberately NOT derived from this fallback: it reports
   * `selectedScore`, so an unrepresentable score presents as "nothing selected"
   * rather than silently claiming the first option is chosen.

   */
  const activeScore = selectedScore ?? RATING_MIN;

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
   * The imperative handle, wired to the same tab stop keyboard navigation uses.
   *
   * `activeScore` rather than a fixed first option, so focus lands where the group
   * says it is: on the selected star once one is chosen, and on the first otherwise.
   * That is the element carrying `tabIndex={0}`, so a Tab press afterwards leaves
   * the group from where the user actually is.
   *
   * The dependency list is `[disabled, activeScore]` because both are read; React
   * re-applies the handle when either changes, so a stale closure cannot focus the
   * wrong option or focus a group that has since been disabled.
   */
  React.useImperativeHandle(
    ref,
    () => ({
      focus: () => {
        if (disabled) {
          return;
        }

        optionRefs.current[activeScore - RATING_MIN]?.focus();
      },
    }),
    [disabled, activeScore],
  );

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
   *
   * Reads `selectedScore`, never the raw prop, so it can only ever state a score
   * this scale represents. Reading `value` produced sentences such as "10 out of
   * 5" and "0 out of 5" — a redundant channel that contradicts both the stars and
   * `aria-checked` is worse than no redundant channel at all.
   */
  const scoreEcho =
    selectedScore === null
      ? 'No score selected'
      : `${selectedScore} out of ${RATING_MAX}`;

  return (
    <div className="flex flex-col gap-2">
      {/*
        The visible label, and the group's accessible name via `aria-labelledby`.

        When the score is required the word is INSIDE this element rather than
        beside it, so it is part of the name the group is announced with — "Your
        rating (required)" — and a sighted reader sees the same fact. A separate
        node marked `aria-hidden`, or an asterisk with a legend elsewhere, would
        split one requirement across two channels that can drift apart.

        The marker is a word rather than a `*`: an asterisk is announced
        inconsistently across screen readers, means nothing without a key, and is
        easy to miss at this size. Its colour is not the signal either — the text
        carries the meaning, so it survives greyscale and colour-blindness.

        The gap is a LITERAL SPACE rather than a margin utility, which is a
        difference an eye cannot see and a screen reader can: an accessible name is
        computed by concatenating text nodes, so a margin produced the name "Your
        rating(required)" — announced as one run-together word — while a real space
        produces "Your rating (required)". Chrome's accessibility tree was the check
        that caught it.
      */}
      <span id={labelId} className="text-base font-semibold text-gray-800">
        {label}
        {required ? (
          <>
            {' '}
            <span className="font-normal text-gray-600">(required)</span>
          </>
        ) : null}
      </span>

      <div
        role="radiogroup"
        aria-labelledby={labelId}
        aria-disabled={disabled}
        /*
         * Present only when the score is required, because `aria-required="false"`
         * is not the same statement as its absence to every assistive technology
         * and there is nothing to gain by asserting the negative.
         */
        aria-required={required ? true : undefined}
        className="flex items-center gap-1"
      >
        {SCORES.map((score, index) => {
          // Filled from `selectedScore`, so the stars agree with `aria-checked`
          // and with the textual echo for every possible value of the prop.

          const isFilled = selectedScore !== null && score <= selectedScore;

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
              aria-checked={score === selectedScore}

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
});

export default StarRatingInput;
