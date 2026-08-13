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
 * The dev-server and preview port, pinned rather than left to Vite's default.
 *
 * This is one half of a cross-process contract, not a preference. The backend
 * configures `CORSMiddleware` from `settings.ALLOWED_ORIGINS`, whose default and
 * documented value is `["http://localhost:3000"]`, and README.md tells a reader
 * the frontend runs on port 3000. Left unset, Vite serves on 5173, so every
 * request the browser made would carry an `Origin` the API does not allow and
 * would be refused by CORS — a failure that surfaces as unexplained blocked
 * requests in the console rather than as anything naming a port.
 *
 * `strictPort` is what makes it a contract instead of a preference: without it
 * Vite silently increments to the next free port when 3000 is taken, which
 * reintroduces exactly the mismatch this pin exists to remove. Failing to start
 * is the correct outcome — it says which port is occupied, at the moment a
 * developer can act on it.
 */
const DEV_SERVER_PORT = 3000;

/**
 * Environment variables that must be present and well-formed to build or serve.
 *
 * `REACT_APP_API_BASE_URL` is the base of every request the application issues:
 * `src/services/api.ts` and `src/services/rating.ts` both construct their axios
 * instance from it, so all five rating endpoints plus listings, transactions and
 * messages are addressed relative to it. Absent, `loadEnv` simply does not return
 * it, the `define` map below gains no entry for it, and each read resolves to
 * `undefined` — at which point axios treats every path as relative to whatever
 * origin happens to be serving the page. The requests do not fail loudly; they
 * go to the wrong place, and a bundle built that way is broken in a way no test
 * of the source can detect.
 *
 * The `/api` segment is validated too, because it is part of the contract rather
 * than an accident: the backend mounts every router under `/api/<resource>`, and
 * `src/services/api.ts` documents that this variable is expected to include
 * `/api`. A value of `https://api.example.com` with the segment omitted produces
 * 404s from a server that is working perfectly.
 */
const REQUIRED_API_BASE_URL = 'REACT_APP_API_BASE_URL';

/**
 * Modes that produce something a browser will actually run.
 *
 * The requirement above is enforced for these and deliberately not for others.
 * `vitest` loads this file with `mode: 'test'`, and a component test issues no
 * network request — its axios instances are never called, or are stubbed — so
 * demanding a deployment URL there would make the suite unrunnable to no
 * purpose. `serve` and `build` are different: each yields an artefact whose whole
 * job is to reach the API.
 */
const MODES_REQUIRING_API_BASE_URL = ['development', 'production'];

/**
 * The Stripe publishable key the entry point asserts is present.
 *
 * Required in the same modes as the API base URL and for the same reason:
 * both are read at module scope by code that runs in the browser, so an
 * absent value is a runtime failure in a shipped bundle rather than a
 * build-time one. See `assertStripePublicKey`.
 */
const REQUIRED_STRIPE_PUBLIC_KEY = 'REACT_APP_STRIPE_PUBLIC_KEY';

/**
 * Reject a missing or malformed public API base URL at config-load time.
 *
 * Throwing here is the point. This is the earliest moment the value is knowable
 * and the last moment before it is baked into a bundle as a literal, so a failure
 * here costs a developer one clear message, while the same mistake carried
 * forward costs a deployment that returns 404s or misroutes credentialed
 * requests to its own origin.
 *
 * @param mode The resolved Vite mode.
 * @param env The variables `loadEnv` resolved for that mode.
 * @throws {Error} The variable is absent, is not an absolute http(s) URL, or does
 *   not carry the `/api` path the backend mounts its routers under.
 */
const assertApiBaseUrl = (mode: string, env: Record<string, string>): void => {
  if (!MODES_REQUIRING_API_BASE_URL.includes(mode)) {
    return;
  }

  const raw = (env[REQUIRED_API_BASE_URL] ?? '').trim();

  if (raw === '') {
    throw new Error(
      `${REQUIRED_API_BASE_URL} is required to build or serve the frontend, ` +
        `and no value was found for mode "${mode}". Set it in frontend/.env ` +
        'or in the environment, for example ' +
        'REACT_APP_API_BASE_URL=http://localhost:8000/api'
    );
  }

  let parsed: URL;
  try {
    parsed = new URL(raw);
  } catch {
    throw new Error(
      `${REQUIRED_API_BASE_URL} must be an absolute URL including its ` +
        `scheme, got ${JSON.stringify(raw)}. Example: ` +
        'http://localhost:8000/api'
    );
  }

  if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') {
    throw new Error(
      `${REQUIRED_API_BASE_URL} must use http or https, got ` +
        `${JSON.stringify(parsed.protocol)}`
    );
  }

  // Credentials in a URL are a leak, not a convenience. This value is
  // INLINED INTO THE BUNDLE by the `define` block below, so a `user:pass@`
  // prefix here publishes those credentials to every visitor who opens the
  // JavaScript, and browsers strip userinfo from fetch requests anyway, so it
  // could not have authenticated anything. Refuse it where the mistake is
  // still recoverable.
  if (parsed.username !== '' || parsed.password !== '') {
    throw new Error(
      `${REQUIRED_API_BASE_URL} must not contain credentials: a ` +
        'user:pass@host value would be published in the client bundle and ' +
        'is ignored by the browser in any case. Use an absolute origin ' +
        'plus /api and authenticate with the bearer token.'
    );
  }

  // A query or fragment is not part of a base URL. axios appends each request
  // path to this value, so `?debug=1` or `#x` lands in the MIDDLE of the
  // resulting URL ("…/api?debug=1/listings"), producing 404s from a server
  // that is working perfectly.
  if (parsed.search !== '' || parsed.hash !== '') {
    throw new Error(
      `${REQUIRED_API_BASE_URL} must be an origin plus path only, with no ` +
        `query string and no fragment, got ${JSON.stringify(raw)}. Every ` +
        'request path is appended to this value.'
    );
  }

  const pathname = parsed.pathname.replace(/\/+$/, '');
  if (!pathname.endsWith('/api')) {
    throw new Error(
      `${REQUIRED_API_BASE_URL} must include the /api path the backend ` +
        `mounts its routers under, got ${JSON.stringify(raw)}. Example: ` +
        'http://localhost:8000/api'
    );
  }
};

/**
 * Reject a missing or malformed Stripe publishable key at config-load time.
 *
 * `src/index.tsx` reads this variable at module scope and hands it to
 * `loadStripe(STRIPE_PUBLIC_KEY!)` — a non-null assertion on a value nothing
 * had ever checked. That file is do-not-touch for this change, so the assertion
 * stays; what changes is that it becomes TRUE by construction. A build or a dev
 * server that reaches the browser now cannot exist without the key, so the
 * assertion is backed by this gate rather than by hope, and the failure moves
 * from a runtime `IntegrationError` inside Stripe's SDK — after the bundle has
 * shipped — to a build that stops with the variable named.
 *
 * Only the publishable key belongs in a bundle. It is designed to be public
 * (that is the difference between `pk_` and `sk_`), so it is required here; a
 * SECRET key would be a serious leak, and one that starts `sk_` is refused
 * outright rather than quietly inlined.
 *
 * @param mode The Vite mode being configured.
 * @param env The variables `loadEnv` resolved for that mode.
 * @throws {Error} The variable is unset or blank in a browser-facing mode, or
 *   it is a secret key.
 */
const assertStripePublicKey = (
  mode: string,
  env: Record<string, string>
): void => {
  const raw = (env[REQUIRED_STRIPE_PUBLIC_KEY] ?? '').trim();

  // A secret key is refused in EVERY mode, including test: a value that starts
  // `sk_` is wrong wherever it appears, and saying so early is the only cheap
  // moment to catch it.
  if (raw.startsWith('sk_') || raw.startsWith('rk_')) {
    throw new Error(
      `${REQUIRED_STRIPE_PUBLIC_KEY} looks like a SECRET key. Only the ` +
        'publishable key (pk_test_… or pk_live_…) may be built into a ' +
        'client bundle; a secret key there is readable by every visitor.'
    );
  }

  if (!MODES_REQUIRING_API_BASE_URL.includes(mode)) {
    return;
  }

  if (raw === '') {
    throw new Error(
      `${REQUIRED_STRIPE_PUBLIC_KEY} is required to build or serve the ` +
        `frontend, and no value was found for mode "${mode}". ` +
        'src/index.tsx passes it straight to loadStripe(), so a bundle ' +
        'built without it fails in the browser rather than here. Set it in ' +
        'frontend/.env or in the environment - see frontend/.env.example.'
    );
  }
};

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

 *   - No `server.proxy`. The API base URL is supplied per environment through
 *     `REACT_APP_API_BASE_URL` and consumed by the axios client, so requests
 *     never need rewriting by the dev server. Hardcoding a proxy target here
 *     would introduce a second, competing source of truth for the backend
 *     address. The `server` and `preview` sections that ARE present exist only to
 *     pin the port — see `DEV_SERVER_PORT`.

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
 *   - No additional plugins. `@vitejs/plugin-react` is the only plugin this
 *     project needs, and it is the only one declared in package.json; naming any
 *     other would make the build depend on a package that `npm ci` does not
 *     install. A Vitest-only virtual `date-fns` module was carried here while
 *     `src/utils/formatting.ts` still imported that undeclared package; the
 *     import has since been removed at its source in favour of the platform's
 *     `Intl.DateTimeFormat`, so the real formatting module now loads under
 *     Vitest, under the dev server and in a build alike, and no specifier
 *     interception is needed. Nothing here may shadow a bare specifier: a stub
 *     left in place would silently pre-empt the real package if it were ever
 *     declared.
 *
 * The export is the function form of `defineConfig` rather than a plain object
 * because `loadEnv` needs the resolved `mode` to pick the right `.env` files,
 * and `mode` is only supplied to the function form. Nothing else depends on it.
 */
/**
 * Every `process.env.REACT_APP_*` member expression that `src` actually reads.
 *
 * Enumerated by grepping the source tree rather than assumed, and complete as of
 * this commit:
 *
 *   REACT_APP_API_BASE_URL       src/services/api.ts, src/services/rating.ts
 *   REACT_APP_STRIPE_PUBLIC_KEY  src/index.tsx, src/services/payment.ts
 *
 * The list exists because a browser has no `process`, so each of these reads
 * MUST be statically replaced whether or not the variable is set — see the
 * `define` note below. A new `process.env.REACT_APP_*` reader belongs here at
 * the same time it is written; the alternative is a module that crashes only in
 * the environments where the variable happens to be unset.
 */
const KNOWN_ENV_KEYS = [
  'REACT_APP_API_BASE_URL',
  'REACT_APP_STRIPE_PUBLIC_KEY',
] as const;

/**
 * Build the `define` map: one entry per KNOWN variable, and nothing else.
 *
 * The key list is `KNOWN_ENV_KEYS` alone — an explicit allow-list of what this
 * application actually reads — rather than the union of that list with whatever
 * `loadEnv` happened to return. The difference is a disclosure boundary, not a
 * tidiness preference: `define` substitutes values into the bundle as literals,
 * where anybody can read them, so unioning in every loaded key means any
 * `REACT_APP_`-prefixed variable that exists in the shell or in a `.env` file is
 * published to every visitor. That is a live hazard, because the prefix is the
 * Create React App convention and an operator who sets, say,
 * `REACT_APP_ADMIN_TOKEN` for a script would be publishing it here without
 * touching this file or being told. An allow-list cannot do that: adding a
 * reader means adding its key, deliberately, in the same commit.
 *
 * Anything loaded but not listed is reported once, so the boundary is visible
 * rather than silent - a genuinely new reader shows up as a message telling you
 * to add it, instead of as a variable that is mysteriously `undefined`.
 *
 * Every listed key is defined whether or not it is set. An unset variable maps
 * to the literal `undefined`, which is exactly what a Create React App build
 * produced for one, so a missing value degrades to "no value" instead of
 * surviving as a bare `process` access that throws
 * `ReferenceError: process is not defined` at import time and takes the whole
 * entry graph down with it.
 *
 * @param loaded Variables `loadEnv` found for the current mode, which may be —
 *   and in a fresh checkout is — empty.
 * @returns A `process.env.<KEY>` → source-text map, one entry per known key.
 */
const buildProcessEnvReplacements = (
  loaded: Record<string, string>,
): Record<string, string> => {
  const known = new Set<string>(KNOWN_ENV_KEYS);
  const unlisted = Object.keys(loaded).filter((key) => !known.has(key));

  if (unlisted.length > 0) {
    // Written to stderr rather than thrown: an unlisted variable is very often
    // a leftover from another project in the same shell, which is not a reason
    // to refuse to build. It is, however, a reason to say something, because
    // the alternative is a variable that is silently `undefined` at the point
    // it is read.
    console.warn(
      `[vite.config] ignoring ${unlisted.join(', ')}: only the variables ` +
        'listed in KNOWN_ENV_KEYS are inlined into the bundle. Add a key ' +
        'there in the same change that adds its process.env reader.'
    );
  }

  return Object.fromEntries(
    KNOWN_ENV_KEYS.map((key) => [
      `process.env.${key}`,
      // `JSON.stringify` of a string yields the quoted literal Vite needs;
      // `undefined` has no JSON form, so the token is written out directly.
      key in loaded ? JSON.stringify(loaded[key]) : 'undefined',
    ]),
  );
};

export default defineConfig(({ mode }) => {
  /*
   * Resolved ONCE, then both validated and published. Calling `loadEnv` twice
   * would risk the check and the `define` map below disagreeing about what was
   * loaded — the exact class of bug this validation exists to prevent.

   */
  const env = loadEnv(mode, __dirname, 'REACT_APP_');

  assertApiBaseUrl(mode, env);
  assertStripePublicKey(mode, env);

  return {
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
       * every module specifier - for every alias that resolves to a real
       * directory. Six entries there, four here; the two omitted are named
       * below.
       *
       * Absolute targets are required. Vite resolves an alias replacement as
       * written, so a relative value would be interpreted against the importing
       * module rather than against this package, and would break for any file
       * outside the package root. `__dirname` is the package root because Vite
       * loads this file as CommonJS — package.json declares no `"type": "module"`
       * and the extension is `.ts`, not `.mts`. This was verified by loading the
       * config and inspecting the resolved values, not assumed.
       *
       * FOUR ENTRIES, NOT SIX. tsconfig.json also declares `@hooks/*` and
       * `@context/*`, and both point at directories — `src/hooks`, `src/context`
       * — that do not exist and that nothing imports through. Mirroring them
       * here would map a specifier onto an absolute path that resolves to
       * nothing, which is not parity with the type-checker so much as a
       * duplicated dead end, and it would suggest a place to put a module that
       * has never been agreed. They are omitted deliberately, and the omission is
       * the whole of the difference from tsconfig.json: create the directory and
       * add the entry in the same change that adds the first real module, and the
       * two files line up again. tsconfig.json is reference-only for this work,
       * so the stale declarations there are left as found.
       *
       * AND NO `'@'` ENTRY. An `'@'` → `src` alias was tried here and has been
       * removed, because it made the bundler resolve specifiers the
       * type-checker rejects, and a resolver that disagrees with the type-checker
       * is worse than one that is merely incomplete: it lets code run that cannot
       * be verified, and it moves the point of failure from `tsc` to whoever next
       * changes the module.
       *
       * The specifiers it was covering are real and remain broken. Sixteen
       * pre-existing modules import through a `@/…` prefix that tsconfig.json does
       * not declare, and `src/App.tsx` — the root component reached from
       * `src/index.tsx` — is one of them, so the dev server cannot resolve the
       * application's own root module and `npm start` serves a document that
       * renders nothing. That is a PRE-EXISTING defect with a known, single root
       * cause (the missing `@/*` mapping versus 16 files' import style) and its
       * repair is explicitly out of scope here; bridging it from the bundler side
       * only hid it from the one gate that reports it, since `build` is
       * `tsc && vite build` and `tsc` never reads this file. tsconfig.json is the
       * single alias authority, and this block mirrors every one of its entries
       * that resolves.
       */
      alias: {
        '@components': path.resolve(__dirname, 'src/components'),
        '@pages': path.resolve(__dirname, 'src/pages'),
        '@utils': path.resolve(__dirname, 'src/utils'),
        '@styles': path.resolve(__dirname, 'src/styles'),
      },


    },

    /**
     * Serve on the port the backend's CORS allow-list names. See
     * `DEV_SERVER_PORT` for why this is a contract rather than a preference.
     */
    server: {
      port: DEV_SERVER_PORT,
      strictPort: true,
    },

    /**
     * The same port for `vite preview`, so a production bundle is exercised from
     * the origin the API allows rather than from a second, unallowed one.
     */
    preview: {
      port: DEV_SERVER_PORT,
      strictPort: true,
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
     * EVERY KNOWN VARIABLE IS DEFINED, WHETHER OR NOT IT IS SET. This is the
     * part that is easy to get wrong, and getting it wrong is silent. Deriving
     * the key list from `loadEnv` ALONE looks equivalent and is not: with no
     * `.env` file and no ambient `REACT_APP_*` variable — the state of a fresh
     * checkout, and of CI — `loadEnv` returns `{}`, so no key is defined, so
     * `process.env.REACT_APP_API_BASE_URL` survives verbatim into the served
     * module and throws `ReferenceError: process is not defined` in the browser
     * at import time. That single throw takes down the whole entry graph, not
     * just the axios base URL. `KNOWN_ENV_KEYS` is therefore the floor: each of
     * its entries is always replaced, with the loaded value when there is one and
     * with the literal `undefined` when there is not — which is exactly the value
     * a Create React App build would have produced for an unset variable, so an
     * absent variable degrades to "no base URL" rather than to a crash.
     *
     * The union with `loadEnv` keeps the forward path open: a variable added to
     * `.env` under the declared prefix is served without editing this file. Only
     * variables read as `process.env.…` in `src` need to appear in the list
     * below; a value consumed through `import.meta.env` needs no `define` entry
     * at all, because `envPrefix` above already publishes it.
     */
    define: buildProcessEnvReplacements(env),


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
       * Resolution overrides that apply ONLY while Vitest is running.
       *
       * READ THE `resolve.alias` NOTE ABOVE BEFORE CHANGING THIS. That block
       * explains why an `'@'` → `src` alias was removed from it: bridging the
       * pre-existing `@/…` specifiers from the bundler side made the resolver
       * disagree with the type-checker and hid a real defect from the one gate
       * that reports it. Nothing here weakens that. `test.alias` is read by
       * Vitest alone — `vite build` does not consult it, `tsc` never reads this
       * file, and `npm run build` is `tsc && vite build` — so every specifier
       * bridged below stays broken, and stays REPORTED, everywhere outside a test
       * run. The 94 type errors and the failing production build are unchanged by
       * this block, which was verified by measuring both with and without it.
       *
       * WHY IT IS NEEDED AT ALL. Vite fails at TRANSFORM time on an unresolvable
       * import, before any module code executes, so `vi.mock` cannot substitute
       * for these: the mock registry is consulted during execution and execution
       * never begins. Resolution is therefore the only layer at which the three
       * rating page integrations can be loaded by a test, and without them the
       * rating surfaces on those pages — where the feature is actually reached —
       * would be the only part of it with no test at all.
       *
       * The array form is used rather than the object form because order matters
       * and objects do not guarantee it: the nine exact specifiers must be
       * matched before the trailing pattern, which would otherwise map them onto
       * files that are absent or unloadable.
       *
       * Two kinds of entry, and each stand-in documents its own reason:
       *
       *   NINE COMPONENT AND SERVICE SPECIFIERS the pages import and the test
       *   runtime cannot load — seven files that do not exist anywhere, plus
       *   `MessageBox` and `PaymentForm`, which exist but are imported by name
       *   while exporting only defaults, and `PaymentForm` additionally needs an
       *   undeclared Stripe package. `@/services/payment` is in the same
       *   position. All are pre-existing and all are out of scope for this
       *   feature, which is why they are stood in for rather than repaired.
       *
       *   ONE TRAILING PATTERN mapping every remaining `@/…` specifier onto
       *   `src/…`, which is what tsconfig.json's `baseUrl: "src"` already means
       *   for the six aliases it does declare. It covers the modules that DO load
       *   — `@/services/api`, `@/store/userSlice` — so a test can then replace
       *   them with `vi.mock` and state their behaviour explicitly, which is the
       *   right layer for a collaborator that resolves but whose behaviour the
       *   test needs to control.
       */
      alias: [
        {
          find: '@/components/TransactionDetails',
          replacement: path.resolve(__dirname, 'src/testing/legacyChildDouble'),
        },
        {
          find: '@/components/PaymentStatus',
          replacement: path.resolve(__dirname, 'src/testing/legacyChildDouble'),
        },
        {
          find: '@/components/ProfileForm',
          replacement: path.resolve(__dirname, 'src/testing/legacyChildDouble'),
        },
        {
          find: '@/components/ListingManagement',
          replacement: path.resolve(__dirname, 'src/testing/legacyChildDouble'),
        },
        {
          find: '@/components/PhotoGallery',
          replacement: path.resolve(__dirname, 'src/testing/legacyChildDouble'),
        },
        {
          find: '@/components/VehicleSpecs',
          replacement: path.resolve(__dirname, 'src/testing/legacyChildDouble'),
        },
        {
          find: '@/components/MaintenanceHistory',
          replacement: path.resolve(__dirname, 'src/testing/legacyChildDouble'),
        },
        {
          find: '@/components/MessageBox',
          replacement: path.resolve(__dirname, 'src/testing/legacyChildDouble'),
        },
        {
          find: '@/components/PaymentForm',
          replacement: path.resolve(__dirname, 'src/testing/legacyChildDouble'),
        },
        {
          find: '@/services/payment',
          replacement: path.resolve(__dirname, 'src/testing/legacyPaymentDouble'),
        },
        {
          find: /^@\/(.*)$/,
          replacement: `${path.resolve(__dirname, 'src')}/$1`,
        },
      ],

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
  };
});
