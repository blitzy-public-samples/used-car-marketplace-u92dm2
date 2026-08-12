/* eslint-env node */

/**
 * ESLint configuration.
 *
 * `.github/workflows/frontend_ci.yml` runs `npm run lint`, which is
 * `eslint src --ext ts,tsx --report-unused-disable-directives --max-warnings 0`,
 * but no ESLint configuration file existed and the stale `eslintConfig` block
 * in package.json extends `react-app`, whose plugin (eslint-config-react-app)
 * is not a declared dependency. This file takes precedence over that block
 * (ESLint resolves .eslintrc.cjs before package.json) and uses only plugins
 * that are actually declared in package.json.
 */
module.exports = {
  root: true,
  env: {
    browser: true,
    es2021: true,
    node: true,
  },
  parser: '@typescript-eslint/parser',
  parserOptions: {
    ecmaVersion: 2021,
    sourceType: 'module',
    ecmaFeatures: { jsx: true },
  },
  plugins: ['@typescript-eslint', 'react-hooks'],
  extends: [
    'eslint:recommended',
    'plugin:@typescript-eslint/recommended',
  ],
  rules: {
    // TypeScript resolves identifiers itself; the core rule double-reports and
    // does not understand type-only globals.
    'no-undef': 'off',
    'no-unused-vars': 'off',
    '@typescript-eslint/no-unused-vars': [
      'error',
      { argsIgnorePattern: '^_', varsIgnorePattern: '^_' },
    ],
    'react-hooks/rules-of-hooks': 'error',
    'react-hooks/exhaustive-deps': 'warn',
  },
  ignorePatterns: [
    'node_modules/',
    'dist/',
    'build/',
    'coverage/',
    '*.config.js',
    '*.config.cjs',
  ],
  overrides: [
    {
      files: ['**/*.{test,spec}.{ts,tsx}', 'vitest.setup.ts'],
      env: { node: true },
    },
  ],
};
