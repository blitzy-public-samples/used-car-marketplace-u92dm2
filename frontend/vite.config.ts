/// <reference types="vitest" />
//
// The triple-slash directive above is load-bearing, not decoration. `defineConfig`
// is typed by Vite, and Vite's own `UserConfig` has no `test` key — that key is
// contributed by Vitest. Referencing Vitest's types here widens the accepted shape
// so the `test` block below type-checks instead of being reported as an unknown
// property. Removing this line does not change runtime behaviour; it silently
// removes the only compile-time guard this file has.

import path from 'path';

import { defineConfig, loadEnv } from 'vite';
import react from '@vitejs/plugin-react';

/**
 * Vite build, dev-server and Vitest configuration for the Used Car Marketplace
 * frontend.
 *
 * WHY THIS FILE EXISTS
 * --------------------
 * `package.json` has always declared five scripts that assume a Vite project —
 * `start: "vite"`, `build: "tsc && vite build"`, `preview: "vite preview"` and
 * `test: "vitest"` — while no Vite configuration file existed anywhere in the
 * repository. Three of those five were therefore invoking a config-less Vite:
 * the path aliases that `tsconfig.json` declares were unknown to the bundler,
 * the project's `REACT_APP_*` environment convention was not exposed to client
 * code, and `vitest` had no DOM environment, no setup file and no test glob. This
 * file supplies exactly those three things and deliberately nothing else.
 *
 * SCOPE DISCIPLINE — WHAT IS DELIBERATELY ABSENT
 * ----------------------------------------------
 * This configuration is intentionally minimal. Each of the following is omitted
 * as a considered decision rather than an oversight, and each omission is
 * documented at its point of relevance below:
 *
 *   - No blanket `define: { 'process.env': … }` shim. A narrowly scoped,
 *     per-variable form is used instead — see the note on `define`.
 *   - No `build` section. Vite's defaults (`outDir: 'dist'`, esbuild minify,
 *     browser targets read from the `browserslist` field already declared in
 *     package.json) are correct for this project, and `dist/` is already covered
 *     by .gitignore. Overriding them would add configuration that nothing reads.
 *   - No `server` or `preview` section, and specifically no `server.proxy`. The
 *     API base URL is supplied per environment through `REACT_APP_API_BASE_URL`
 *     and consumed by the axios client, so requests never need rewriting by the
 *     dev server. Hardcoding a proxy target here would introduce a second,
 *     competing source of truth for the backend address.
 *   - No `css.postcss` key. `postcss.config.js` sits beside this file at the
 *     package root, which is exactly where Vite, Vitest and a direct PostCSS run
 *     all look by convention. Naming it explicitly here would duplicate that
 *     discovery and create a path that could drift from the CLI's behaviour.
 *   - No `root` and no `build.rollupOptions.input`. Vite resolves its entry from
 *     `index.html` at the package root, which is where the entry document lives.
 *     `public/index.html` is a leftover Create React App template — it still
 *     carries unsubstituted `%PUBLIC_URL%` placeholders and no module script —
 *     and is served as a static asset only. Pointing the build at it would
 *     produce a document Vite cannot process.
 *   - No additional plugins. `@vitejs/plugin-react` is the only plugin the
 *     application needs, and it is the only one declared in package.json;
 *     naming any other would make the build depend on a package that
 *     `npm ci` does not install.
 *
 * The export is the function form of `defineConfig` rather than a plain object
 * because `loadEnv` needs the resolved `mode` to pick the right `.env` files,
 * and `mode` is only supplied to the function form. Nothing else depends on it.
 */
export default defineConfig(({ mode }) => ({
  /**
   * React support: JSX/TSX transform, Fast Refresh in development and the
   * automatic JSX runtime, which matches `"jsx": "react-jsx"` in tsconfig.json.
   * No options are passed — the plugin's defaults already align with the
   * TypeScript configuration, so passing any would risk the two diverging.
   */
  plugins: [react()],

  resolve: {
    /**
     * A byte-for-byte mirror of `compilerOptions.paths` in tsconfig.json
     * (`baseUrl: "src"`), so that the bundler and the type-checker agree on
     * every module specifier. Six entries there, six entries here.
     *
     * Absolute targets are required. Vite resolves an alias replacement as
     * written, so a relative value would be interpreted against the importing
     * module rather than against this package, and would break for any file
     * outside the package root. `__dirname` is the package root because Vite
     * loads this file as CommonJS — package.json declares no `"type": "module"`
     * and the extension is `.ts`, not `.mts`. This was verified by loading the
     * config and inspecting the resolved values, not assumed.
     *
     * Two of the six targets, `src/hooks` and `src/context`, do not exist:
     * tsconfig.json declares aliases for directories that were never created.
     * They are mirrored anyway, because parity with the type-checker is the
     * whole point of this block and an alias to a missing directory is inert
     * until something imports through it — at which point the resolver fails
     * loudly, exactly as the type-checker already does. The directories are
     * deliberately not created here; a directory with no module in it would be
     * noise, and nothing in this feature imports through either alias.
     *
     * The seventh entry, `'@'` → `src`, has no counterpart in
     * `compilerOptions.paths` and is present for the bundler alone. Sixteen
     * pre-existing modules import through a `@/…` prefix that tsconfig.json
     * does not declare, and `src/App.tsx` — the root component, reached
     * directly from `src/index.tsx` — is one of them: it pulls `Header`,
     * `Footer`, five pages and the store through that prefix. Without this
     * alias the dev server cannot resolve the application's own root module
     * ("Failed to resolve import \"@/components/Header\" from
     * \"src/App.tsx\""), so `npm start` serves a document whose entry graph
     * never loads and nothing renders at all.
     *
     * The alias cannot hide a type error behind a passing build, because the
     * two gates are chained rather than independent: `build` is
     * `tsc && vite build`, so `tsc` — which reads tsconfig.json and never
     * reads this file — runs first and fails the whole command on exactly the
     * `@/…` specifiers it rejects. A bundler that additionally resolves them
     * therefore changes nothing about what CI reports; it only decides whether
     * the dev server can serve the app while those specifiers are still being
     * corrected. Correcting them in the affected modules remains the real
     * repair and is explicitly out of scope, so the alias is the bridge until
     * that lands, not a substitute for it.
     */
    alias: {
      '@components': path.resolve(__dirname, 'src/components'),
      '@pages': path.resolve(__dirname, 'src/pages'),
      '@utils': path.resolve(__dirname, 'src/utils'),
      '@styles': path.resolve(__dirname, 'src/styles'),
      '@hooks': path.resolve(__dirname, 'src/hooks'),
      '@context': path.resolve(__dirname, 'src/context'),
      '@': path.resolve(__dirname, 'src'),
    },
  },

  /**
   * Expose environment variables under this project's own declared convention.
   *
   * Vite's default prefix is `VITE_`, and this codebase does not use a single
   * `VITE_`-prefixed variable. It uses the Create React App convention in three
   * places — `src/services/api.ts` and the new `src/services/rating.ts` read
   * `REACT_APP_API_BASE_URL`, while `src/index.tsx` and `src/services/payment.ts`
   * read `REACT_APP_STRIPE_PUBLIC_KEY`. Left at the default, Vite would expose
   * none of them and every one would be `undefined` at runtime, so setting the
   * prefix is configuration of the convention the project already declares
   * rather than a change to it.
   *
   * BOUNDARY, stated precisely so it is not mistaken for an oversight:
   * `envPrefix` governs which variables Vite loads from `.env` files and from
   * the ambient environment and publishes on `import.meta.env`. It does NOT
   * populate `process.env`, which does not exist in a browser. The modules
   * listed above read `process.env.REACT_APP_*`, so this setting alone leaves
   * every one of them `undefined`; the `define` block below is what actually
   * serves them. Both are configured, because they cover different consumers:
   * `envPrefix` is the forward path for code written against
   * `import.meta.env`, and `define` keeps the existing readers working until
   * their specifiers are migrated. Migrating them is out of scope here.
   *
   * One further condition, verified rather than assumed, so that nobody reads
   * the paragraph above as an invitation that quietly costs them errors: the
   * values land on `import.meta.env` at run time, but `ImportMeta.env` is not
   * typed anywhere in this project's TypeScript program. The augmentation lives
   * in `vite/client`, and no `types` entry and no triple-slash reference in
   * `src` pulls it in, so `import.meta.env.REACT_APP_API_BASE_URL` reads
   * correctly at run time while `tsc` reports "Property 'env' does not exist on
   * type 'ImportMeta'". Referencing those types means adding an ambient
   * declaration to `src`, which is a separate change nobody has asked for, so a
   * module reading the value today should narrow `import.meta` locally at the
   * single point of use. Recorded here because this file is what makes the
   * values available, and the limitation belongs with the capability.
   */
  envPrefix: 'REACT_APP_',

  /**
   * Statically replace each `process.env.REACT_APP_*` read with the value
   * loaded for the current mode, so the modules that already use the Create
   * React App convention keep working in a browser.
   *
   * This is required, not cosmetic. `src/index.tsx` reads
   * `REACT_APP_STRIPE_PUBLIC_KEY` at module scope and `src/services/api.ts`
   * reads `REACT_APP_API_BASE_URL` to construct the axios instance that every
   * API call — including all five rating endpoints — is issued through. Without
   * a replacement those reads resolve to `undefined` in a production bundle
   * (Vite rewrites the bare object to `{}`) and throw
   * `ReferenceError: process is not defined` under the dev server, which
   * performs no such rewrite. Either way the axios client is built with no base
   * URL and requests are silently misrouted.
   *
   * ONE KEY PER VARIABLE, deliberately, rather than a single blanket
   * `'process.env'` entry. Replacing the whole object would inline the entire
   * loaded environment into every bundle as a literal and would substitute
   * `process.env` everywhere it appears, including inside dependencies that
   * legitimately probe `process.env.NODE_ENV`, breaking their production
   * branches. Targeting the individual member expressions keeps the blast
   * radius to exactly the variables this application reads: `process.env` and
   * `process.env.NODE_ENV` are left to Vite's own handling.
   *
   * The list is derived from `loadEnv` rather than hardcoded, so a variable
   * added to `.env` under the declared prefix is served without editing this
   * file, and a variable that is absent is emitted as `undefined` — the same
   * value the read would have produced anyway — instead of failing the build.
   */
  define: Object.fromEntries(
    Object.entries(loadEnv(mode, __dirname, 'REACT_APP_')).map(
      ([key, value]) => [
        `process.env.${key}`,
        JSON.stringify(value),
      ],
    ),
  ),

  test: {
    /**
     * Inject `describe`, `it`, `expect`, `beforeEach` and friends as globals.
     *
     * This is mandatory rather than a matter of taste, because of how this
     * project's type-checking is arranged. `tsconfig.json` excludes, recursively,
     * every `*.spec.ts` and every `*.test.ts` — but NOT `*.test.tsx` — while its
     * `include` covers every `.ts` and `.tsx` file under `src`. Component test
     * files are therefore inside the TypeScript program and are checked by
     * `tsc --noEmit` and by `npm run build`. Those files resolve their globals
     * from the
     * `@types/jest` ambient declarations that TypeScript picks up
     * automatically, and `@testing-library/jest-dom` (v5) augments the matcher
     * interface those same globals expose, which is what makes
     * `expect(x).toBeInTheDocument()` type-check.
     *
     * With globals injected, the identifiers the type-checker describes are the
     * identifiers that exist at run time and the two agree. Without them, every
     * test file would have to import `expect` from `vitest`; that imported
     * `expect` carries none of the jest-dom augmentation, so each jest-dom
     * matcher would become a fresh type error in a fresh file. It also matters
     * at run time: jest-dom's entry point calls `expect.extend(...)` against the
     * global `expect`, which must therefore exist before `vitest.setup.ts` is
     * loaded.
     */
    globals: true,

    /**
     * A real DOM. The component suites assert on focus management, keyboard
     * interaction and ARIA semantics, none of which have any meaning in the
     * default `node` environment — `document` would simply be undefined. jsdom
     * is declared in package.json for exactly this purpose.
     */
    environment: 'jsdom',

    /**
     * Runs once per test file, before any test, and registers the
     * `@testing-library/jest-dom` matchers on the global `expect` established by
     * `globals: true` above. The path is relative to this config file's
     * directory. If this entry is wrong or missing, the suites still run and
     * every jest-dom assertion fails with "not a function" rather than with a
     * meaningful message.
     */
    setupFiles: ['./vitest.setup.ts'],

    /**
     * Restrict collection to test files inside the application source tree.
     * Scoping this explicitly keeps Vitest from walking unrelated directories
     * and makes the contract obvious: a spec is picked up when it lives under
     * `src/` and is named `*.test.ts(x)` or `*.spec.ts(x)`.
     */
    include: ['src/**/*.{test,spec}.{ts,tsx}'],

    /**
     * Coverage via the V8 provider, matching the `@vitest/coverage-v8` package
     * declared in package.json — it is only ever consulted when a run is
     * invoked with `--coverage`. No thresholds are configured: a threshold is a
     * policy decision about what level of coverage should fail a build, no such
     * policy is defined for this project, and inventing one here would start
     * failing pipelines on a number nobody agreed to.
     */
    coverage: {
      provider: 'v8',
    },
  },
}));
