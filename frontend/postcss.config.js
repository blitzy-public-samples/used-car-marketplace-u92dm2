/**
 * PostCSS configuration.
 *
 * Activates the tailwindcss and autoprefixer plugins that package.json already
 * declares. Without this file Vite processes CSS without ever running
 * Tailwind, so no utility class in the source tree resolves to real CSS.
 */
module.exports = {
  plugins: {
    tailwindcss: {},
    autoprefixer: {},
  },
};
