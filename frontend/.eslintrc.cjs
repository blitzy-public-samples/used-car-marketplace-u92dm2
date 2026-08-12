/**
 * ESLint configuration for the Used Car Marketplace frontend.
 *
 * WHY THIS FILE EXISTS
 * --------------------
 * `package.json` already declares
 *     "lint": "eslint src --ext ts,tsx --report-unused-disable-directives --max-warnings 0"
 * and `.github/workflows/frontend_ci.yml` already runs `npm run lint`, but no
 * ESLint configuration file existed anywhere in the repository. The command was
 * therefore declared but not executable. This file makes it executable.
 *
 * WHY THE `.cjs` EXTENSION
 * ------------------------
 * `package.json` declares no `"type": "module"`, so `.js` would already be
 * treated as CommonJS today — but the `.cjs` suffix guarantees CommonJS loading
 * regardless of any future `"type"` change, so `module.exports` can never be
 * mis-parsed as ESM.
 *
 * PRECEDENCE OVER `package.json#eslintConfig`
 * -------------------------------------------
 * `package.json` still carries a stale `eslintConfig` block that extends
 * `react-app` / `react-app/jest`. Neither `eslint-config-react-app` nor its
 * transitive plugins are declared dependencies, so honouring that block aborts
 * the run before a single file is read — `eslint --no-eslintrc --config
 * package.json` fails with `ESLint couldn't find the config "react-app" to
 * extend from`. ESLint resolves exactly ONE configuration per directory, in
 * this order:
 *
 *     .eslintrc.js > .eslintrc.cjs > .eslintrc.yaml > .eslintrc.yml >
 *     .eslintrc.json > package.json#eslintConfig
 *
 * This file therefore SUPERSEDES that block rather than merging with it, which
 * renders it harmlessly inert. The block is deliberately left untouched in
 * `package.json`: removing it is not part of this change, and it has no effect
 * while this file is present.
 *
 * SEVERITY IS NOT ADVISORY HERE
 * -----------------------------
 * The `lint` script passes `--max-warnings 0`, so a rule configured at `warn`
 * fails the command exactly as a rule configured at `error` does. Every entry
 * inherited from the shared configs below should be read with that in mind.
 *
 * DEPENDENCY DISCIPLINE — READ BEFORE ADDING TO `extends` OR `plugins`
 * -------------------------------------------------------------------
 * A config may only reference packages the manifest actually declares, or the
 * install stays clean while the lint run dies at startup. The frontend declares
 * exactly four ESLint packages, and this file uses all four and nothing else:
 *
 *     eslint                            8.57.1  (supplies `eslint:recommended`)
 *     @typescript-eslint/parser         5.62.0
 *     @typescript-eslint/eslint-plugin  5.62.0
 *     eslint-plugin-react-hooks         4.6.2
 *
 * `eslint-plugin-react` and `eslint-config-react-app` are intentionally NOT
 * dependencies of this project. Do not add `plugin:react/recommended`,
 * `react-app`, `airbnb`, `prettier`, or any other shared config here without
 * first declaring and pinning the package that provides it.
 */

module.exports = {
  /**
   * Stop the upward search for further configuration at this directory.
   *
   * Mandatory: `frontend/` is a nested package and the repository root sits
   * outside it. Without `root`, ESLint would keep walking parent directories
   * (and, in a developer's checkout, beyond the repository itself), so the
   * effective rule set would depend on where the clone happens to live.
   */
  root: true,

  /**
   * Ambient globals.
   *
   * - `browser` — `document`, `window`, `fetch`, `FormData`, `File`, `localStorage`
   *   and friends, all used throughout `src/`.
   * - `es2020` — matches `parserOptions.ecmaVersion` below and the `target: "ES2020"`
   *   declared in `tsconfig.json`, so `Promise`, `BigInt` and `globalThis` resolve.
   * - `node` — `process`, `module`, `__dirname`. Required because the source tree
   *   still reads `process.env.REACT_APP_*` (see `src/index.tsx`, `src/services/api.ts`),
   *   and because the package's root-level tooling files are CommonJS.
   */
  env: {
    browser: true,
    es2020: true,
    node: true,
  },

  /**
   * TypeScript-aware parser. The default parser (espree) cannot read type
   * annotations, interfaces, generics or `satisfies`, so it is not an option
   * for a `.ts`/`.tsx` source tree.
   */
  parser: '@typescript-eslint/parser',

  parserOptions: {
    // Aligned with `tsconfig.json`'s `target: "ES2020"`.
    ecmaVersion: 2020,
    // Every file under `src/` is an ES module (`import` / `export`).
    sourceType: 'module',
    // `tsconfig.json` sets `jsx: "react-jsx"`; the parser must accept JSX syntax.
    ecmaFeatures: {
      jsx: true,
    },
    /**
     * `project` is deliberately NOT set, so linting stays purely syntactic.
     *
     * Enabling type-aware linting would (a) make every lint run depend on a
     * successful type program, which currently reports a large pre-existing
     * error baseline across the legacy source tree, (b) slow each run by the
     * cost of a full type-check, and (c) enable an additional class of rules
     * that fire heavily on legacy code this change is not permitted to rewrite.
     * No rule configured below requires type information.
     */
  },

  /**
   * Rule providers. Both are declared dependencies.
   *
   * `react-hooks` is the one React-specific plugin this project needs: it is
   * what catches conditional hook calls and stale-closure dependency arrays,
   * which are correctness bugs rather than style preferences.
   */
  plugins: ['@typescript-eslint', 'react-hooks'],

  /**
   * Shared configurations, applied in order — later entries win on conflict.
   *
   * 1. `eslint:recommended`
   *    Core JavaScript correctness rules (`no-cond-assign`, `no-dupe-keys`,
   *    `no-fallthrough`, ...).
   *
   * 2. `plugin:@typescript-eslint/recommended`
   *    Chains `plugin:@typescript-eslint/eslint-recommended`, which switches off
   *    the ~20 core rules the TypeScript compiler already enforces better — most
   *    importantly `no-undef`, which cannot see type-only declarations or
   *    ambient globals and would otherwise produce false positives on `.ts`/`.tsx`
   *    files. It then enables the TypeScript equivalents, including
   *    `@typescript-eslint/no-unused-vars`, `@typescript-eslint/no-explicit-any`
   *    and `@typescript-eslint/no-empty-interface`.
   *
   * 3. `plugin:react-hooks/recommended`
   *    `react-hooks/rules-of-hooks` at `error` and `react-hooks/exhaustive-deps`
   *    at `warn`. Under `--max-warnings 0` both are effectively fatal, which is
   *    the intended strictness for new components.
   *
   * There is no fourth entry. See "DEPENDENCY DISCIPLINE" in the header.
   *
   * `settings` is intentionally absent: `settings.react.version` is only read by
   * `eslint-plugin-react`, which this project does not install. React 18's
   * automatic JSX runtime (`jsx: "react-jsx"`) also means no `React` import is
   * required in scope, and with that plugin absent no rule can demand one.
   */
  extends: [
    'eslint:recommended',
    'plugin:@typescript-eslint/recommended',
    'plugin:react-hooks/recommended',
  ],

  // ---------------------------------------------------------------------------
  // NOTE — test files need no `overrides` entry, and none is carried here.
  //
  // `vite.config.ts` sets `test.globals: true`, so Vitest injects `describe`,
  // `it`, `test`, `expect`, `beforeEach` and `vi` instead of requiring an import
  // from `vitest`. The obvious worry is that core `no-undef` would reject those
  // identifiers. It does not: `plugin:@typescript-eslint/eslint-recommended`
  // (chained by the second `extends` entry above) disables `no-undef` for
  // `.ts`/`.tsx` files. That was verified by resolving the effective config for
  // a test-file path and by linting a Vitest-globals test file — zero findings,
  // and identical results with and without a `jest`-env override. Adding one
  // would therefore be inert configuration, so it is omitted. The TypeScript
  // compiler is what validates those globals, via the ambient `@types/jest`
  // declarations `package.json` already declares.
  //
  // Related: `tsconfig.json` excludes `.test.ts` from the type program but does
  // NOT exclude `.test.tsx`, so `.tsx` test files are both type-checked and
  // linted, and are held to exactly the same rules as production sources.
  // ---------------------------------------------------------------------------

  /**
   * Paths never linted.
   *
   * Dependencies and generated output only. Nothing under `src/` is ignored:
   * hiding source files from the linter would defeat the purpose of wiring it
   * up, and the new rating components in particular must be held to this
   * configuration.
   */
  ignorePatterns: ['node_modules', 'build', 'dist', 'coverage'],
};
