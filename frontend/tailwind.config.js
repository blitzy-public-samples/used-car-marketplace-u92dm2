/**
 * Tailwind CSS configuration — this project's entire design-token source.
 *
 * `tailwindcss`, `postcss` and `autoprefixer` were all declared in
 * package.json, yet no Tailwind or PostCSS configuration existed, so the
 * styling pipeline was declared but inert and every utility class in the
 * source tree resolved to no CSS at all. Together with postcss.config.js,
 * src/styles/index.css and that stylesheet's import from the application
 * entry point, this file is what makes styling work.
 *
 * STATUS: the chain is COMPLETE and the tokens below are live. All four links
 * are in place - this file supplies the tokens, postcss.config.js registers the
 * `tailwindcss` and `autoprefixer` plugins, src/styles/index.css holds the
 * `@tailwind base/components/utilities` directives, and `src/index.tsx` imports
 * './styles/index.css', so the stylesheet reaches the bundle, PostCSS runs the
 * `tailwindcss` plugin over its `@tailwind` directives, and the tokens below
 * decide what is emitted. Verified by compiling src/styles/index.css through
 * postcss with this configuration and confirming the output contains the
 * accessibility-critical `ring-2`, `ring-offset-2` and `outline-none` rules
 * named below - not by inspecting the import statement, which is necessary for
 * styling to work but not sufficient. *
 * No component library is installed, so Tailwind's utilities are the whole
 * design system. Accessibility-critical utilities — the visible focus
 * indicator `focus:outline-none focus:ring-2 focus:ring-offset-2` required by
 * WCAG 2.1 Level AA — are only emitted while `theme` is EXTENDED rather than
 * replaced (which would delete the default ring scales) and while `content`
 * scans the files that reference them (or the unused-class purge removes
 * them). Both conditions are load-bearing, and both fail silently.
 */

/** @type {import('tailwindcss').Config} */
module.exports = {
  // Every source file lives under src/ and is .ts or .tsx: there are no
  // .js/.jsx sources, and neither index.html carries a class attribute, so
  // this single glob covers the complete class-name surface and nothing more.
  content: ['./src/**/*.{ts,tsx}'],

  // Deliberately empty. Tailwind's default scale is the token source for
  // colour, spacing, typography, radius, shadow, ring and breakpoints, and it
  // already covers every value the interface uses, so no custom token is
  // warranted. `extend` is where the project adds tokens in future — extending
  // keeps the defaults reachable, whereas replacing `theme` discards them.
  theme: {
    extend: {},
  },

  // package.json declares no Tailwind plugin; adding one here would introduce
  // an undeclared dependency.
  plugins: [],
};
