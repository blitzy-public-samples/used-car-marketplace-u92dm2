import { defineConfig, loadEnv } from 'vite';
import react from '@vitejs/plugin-react';
import path from 'path';

/**
 * Vite build / dev-server / Vitest configuration.
 *
 * package.json already declares `start: vite`, `build: tsc && vite build`,
 * `preview: vite preview` and `test: vitest`, but no Vite configuration file
 * existed, so none of those scripts could resolve an entry, an alias or a test
 * environment. This file supplies all three.
 */
export default defineConfig(({ mode }) => {
  // The source tree was written for Create React App and still reads
  // `process.env.REACT_APP_*` (src/index.tsx, src/services/api.ts). Vite does
  // not expose `process.env` to browser code, so the REACT_APP_* prefix is
  // loaded from .env files and injected here instead of touching the sources.
  const env = loadEnv(mode, __dirname, 'REACT_APP_');

  return {
    plugins: [react()],

    resolve: {
      alias: {
        // Mirrors the "paths" entries declared in tsconfig.json.
        '@components': path.resolve(__dirname, 'src/components'),
        '@pages': path.resolve(__dirname, 'src/pages'),
        '@utils': path.resolve(__dirname, 'src/utils'),
        '@styles': path.resolve(__dirname, 'src/styles'),
        '@hooks': path.resolve(__dirname, 'src/hooks'),
        '@context': path.resolve(__dirname, 'src/context'),
        // Bundler-side accommodation only: ~30 existing files import via the
        // "@/..." prefix, which tsconfig.json does NOT declare. Repairing those
        // imports (and the resulting TypeScript errors) is explicitly out of
        // scope, so the alias is provided here to keep the dev server and the
        // bundler able to resolve them. `tsc` behaviour is intentionally left
        // unchanged so the pre-existing error baseline stays measurable.
        '@': path.resolve(__dirname, 'src'),
      },
    },

    define: {
      'process.env': JSON.stringify(env),
    },

    server: {
      host: true,
      // README and infrastructure/docker/docker-compose.yml both expect 3000.
      port: Number(process.env.PORT) || 3000,
      strictPort: false,
    },

    preview: {
      host: true,
      port: Number(process.env.PREVIEW_PORT) || 4173,
    },

    build: {
      outDir: 'dist',
      sourcemap: true,
    },

    test: {
      globals: true,
      environment: 'jsdom',
      setupFiles: ['./vitest.setup.ts'],
      include: ['src/**/*.{test,spec}.{ts,tsx}'],
      css: false,
      coverage: {
        provider: 'v8',
        reporter: ['text', 'lcov'],
        reportsDirectory: './coverage',
      },
    },
  };
});
