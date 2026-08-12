/** @type {import('tailwindcss').Config} */

/**
 * Tailwind CSS configuration.
 *
 * tailwindcss, postcss and autoprefixer were already declared in
 * package.json, but neither tailwind.config.js nor postcss.config.js existed,
 * so the styling pipeline was declared and completely inert. Tailwind's
 * default scale is the project's design-token source; extend `theme.extend`
 * here rather than hardcoding values in components.
 */
module.exports = {
  content: [
    './index.html',
    './src/**/*.{js,jsx,ts,tsx}',
  ],
  theme: {
    extend: {},
  },
  plugins: [],
};
