/**
 * PostCSS configuration — the switch that turns CSS generation on.
 *
 * `tailwindcss`, `postcss` and `autoprefixer` are all declared in package.json,
 * yet no PostCSS configuration existed, so the styling pipeline was declared but
 * inert: every utility class in the source tree resolved to no CSS at all. This
 * file is one of four links needed to make styling work — tailwind.config.js
 * supplies the design tokens, src/styles/index.css holds the `@tailwind`
 * directives, this file registers the plugins that turn those directives into
 * rules, and the stylesheet must be imported once from the application entry
 * point. Any one of the four missing fails silently: the markup still carries the
 * class names, and nothing is styled.
 *
 * STATUS: three of the four are in place; the fourth is NOT YET DONE. `src/index.tsx`
 * carries no CSS import today, so nothing pulls src/styles/index.css into the
 * bundle and no rule is emitted for any class name in the application. That
 * import is the remaining step, and it belongs to the entry-point change that
 * has not been made yet. Until it lands, the pipeline configured here is correct
 * and idle — this file is what makes the final import work, not evidence that it
 * has happened.

 *
 * That silence is why this file is load-bearing rather than boilerplate. No
 * component library is installed, so Tailwind's utilities ARE the design system,
 * including the visible focus indicator that WCAG 2.1 Level AA requires of every
 * interactive control (`focus:outline-none focus:ring-2 focus:ring-offset-2`).
 * Without the `tailwindcss` plugin registered below, no rule backs those class
 * names and the focus ring never appears, while the component's markup and tests
 * continue to look correct. The same is true, today, of the missing entry-point
 * import described above: the focus ring will not render until it lands.
 *
 * Discovery is by convention, not by reference: vite.config.ts deliberately sets
 * no `css.postcss` option, so Vite, Vitest and any direct PostCSS run locate this
 * file from the package root. Renaming or relocating it disables the pipeline.
 *
 * CommonJS is required, not stylistic: package.json declares no
 * `"type": "module"`, so Node treats a bare `.js` file in this package as
 * CommonJS and `export default` here would throw while the config is loaded.
 */

module.exports = {
  /**
   * Registered in execution order, which is not interchangeable.
   *
   * - `tailwindcss` runs first and replaces the `@tailwind` directives with the
   *   generated base, component and utility rules. Its options are empty because
   *   the entire configuration — content globs, theme tokens, plugins — belongs
   *   in tailwind.config.js, which Tailwind resolves for itself.
   * - `autoprefixer` runs second so that it prefixes the CSS Tailwind has
   *   already produced; placed ahead of Tailwind it would see only the
   *   directives and emit nothing. Its options are empty because it derives its
   *   targets from the `browserslist` field already declared in package.json.
   *
   * Exactly these two plugins are registered and no other may be added here:
   * package.json declares no further PostCSS plugin, so naming one would make
   * the build depend on a package that `npm ci` does not install.
   */
  plugins: {
    tailwindcss: {},
    autoprefixer: {},
  },
};
