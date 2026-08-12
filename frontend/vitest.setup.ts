/**
 * Vitest global setup.
 *
 * Registers the @testing-library/jest-dom matchers (toBeInTheDocument,
 * toHaveAccessibleName, ...) for every test file. `@testing-library/jest-dom`
 * is already declared in package.json devDependencies; this file is what
 * actually wires it into the Vitest expect instance.
 */
import '@testing-library/jest-dom';
