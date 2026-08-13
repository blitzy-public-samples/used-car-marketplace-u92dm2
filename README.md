# Used Car Marketplace

A comprehensive platform for buying and selling used cars, connecting sellers with potential buyers in a user-friendly and efficient manner.

## Features

- User registration and authentication
- Car listing creation and management
- Advanced search and filtering options
- Messaging system for buyer-seller communication
- User ratings and reviews
- Favorite listings and saved searches
- Admin panel for platform management

## Technologies Used

This list separates what the repository **installs** from what it only
**imports** or **specifies**, because the setup procedure below materializes
only the first group. Every entry is cross-checked against
`documentation/Technical Specifications.md` (sections 6.1 through 6.4),
`frontend/package.json`, and the tracked source or infrastructure code that
uses it. Read the whole list as the declared stack, not as a claim that every
piece is wired up end to end; the groups below say which is which.

Installed by `npm install`, because `frontend/package.json` declares them:

- Frontend: React 18 with TypeScript, built and served by Vite
- Routing: React Router
- State Management: Redux Toolkit (`@reduxjs/toolkit`)
- HTTP Client: Axios
- Styling: Tailwind CSS
- Payments, client side: Stripe.js and React Stripe.js
- Frontend Testing: Vitest with React Testing Library and jest-dom, in a jsdom
  environment. Installed and runnable, but there are no frontend test files yet,
  so `npm test` currently executes no assertions - see "Known limitations"
- Frontend Linting: ESLint with the TypeScript ESLint toolchain

Imported by tracked backend source, but installed by nothing in this
repository: no `requirements.txt` or other Python project metadata is
committed, so neither `npm install` nor `scripts/setup_environment.sh` installs
any of these. Install them yourself before running the service.

- Backend: Python with FastAPI (service entry point: `backend/app/main.py`)
- Authentication: JSON Web Tokens (JWT), issued by the backend with python-jose
  and paired with passlib password hashing (`backend/app/core/security.py`)
- Settings and validation: Pydantic
- Background jobs: Celery
- Payments, server side: the Stripe Python SDK
- Google Cloud client libraries: Firestore, Cloud Storage, Vision, Document AI
- Document and image processing: Pillow and PyPDF2

Specified by the requirements and provisioned by infrastructure code, rather
than installed by either setup path:

- Data: Google Cloud Firestore is the only datastore the tracked backend code
  actually reads and writes. Google Cloud SQL (PostgreSQL) and Google Cloud
  Storage are specified in Technical Specifications 6.3 and declared as Terraform
  resources in `infrastructure/terraform/main.tf`, but no code path under
  `backend/` reaches either one yet - transaction records are written to
  Firestore, not to SQL.
- Deployment: Docker and Kubernetes (Google Kubernetes Engine), provisioned
  with Terraform (`infrastructure/`)

## Getting Started

### Prerequisites

- Node.js 20.x with npm 9 or later. Read this as a **compatibility** statement,
  not a support statement: Node 20.20.2 with npm 10.8.2 is the runtime this
  setup procedure was validated against, and it matches the `@types/node`
  version the frontend declares, but the Node 20 line reached **end of life on
  30 April 2026** and receives no further security patches from the Node.js
  project.
- The Node.js lines that are still supported are 22.x (Maintenance LTS, end of
  life 30 April 2027) and 24.x (Active LTS, end of life 30 April 2028).
  **Neither has been validated against this repository.** Moving onto a
  supported line is a separate piece of work that has to be validated on its
  own, because the frontend toolchain is pinned to Vite 4 and Vitest 0.34 and
  Vitest 1.x requires Vite 5 - a major-version bump of both. Nothing here
  should be read as a recommendation to run an end-of-life runtime in
  production.
- The hard floor declared in the `engines` field of both `package.json` and
  `frontend/package.json` is Node >= 18.0.0 and npm >= 9.0.0. That floor exists
  to keep a workspace-incapable npm out; it is a *minimum*, not a support
  statement. Node 18 and Node 20 both satisfy it and both are end-of-life (Node
  18 since 30 April 2025), while the currently supported 22.x and 24.x lines
  satisfy it as well.
- Node.js 14 and Node.js 16 are **not** supported at all. This repository is an
  npm workspace, and npm workspaces require npm 7 or later. Node 14 ships
  npm 6, which can neither resolve a `workspaces` array nor read the committed
  `lockfileVersion 3` lockfile.
- Four surfaces in this repository govern which runtime you end up on, and they
  are deliberately consistent with each other: the `engines` floors in
  `package.json` and in `frontend/package.json`, the NodeSource installer in
  `scripts/setup_environment.sh`, and the `node-version` pin in
  `.github/workflows/frontend_ci.yml`. The last two both name the 20.x line, and
  the two floors admit it. A move onto a supported line therefore means changing
  all four together and re-validating against them, which is why it is not part
  of this change.
- Python 3.9 for the backend service. This is the exact version pinned by
  `.github/workflows/backend_ci.yml`, and that pin is the only version
  authority in the repository: because no `requirements.txt` or other Python
  project metadata is committed, compatibility with any later Python version is
  neither declared nor verified here.
- Docker (optional, for containerized deployment)

### Repository layout

This is a two-runtime repository. Read this before running any install command,
because which directory holds the Node package manifest is not obvious from the
top level.

- `frontend/`: the React + TypeScript application, built with Vite. Until the
  root workspace manifest described below was added, this directory held the
  repository's **only** `package.json`.
- `backend/`: the Python service built with FastAPI. Its entry point is
  `backend/app/main.py`, and its dependencies are managed with `pip`, not npm.
- The repository **root**: an npm *workspace root*. The `package.json` here
  declares `"workspaces": ["frontend"]`, so a root-level `npm install` resolves
  and installs the `frontend` workspace and hoists the result into a single
  root `node_modules/` directory. The root manifest is marked `private` and is
  never published; it only ties the workspace together and delegates the common
  scripts (`start`, `dev`, `build`, `test`, `lint`) into `frontend`.
- `scripts/`: environment bootstrap and deployment automation.
- `infrastructure/`: infrastructure as code (Docker Compose, nginx, Terraform).
- `documentation/`: the project proposal, requirements specification, and
  technical specification.
- `.github/`: continuous integration workflows.

Why this section exists: the previous absence of a `package.json` at the
repository root is exactly what made `npm install` at the repository root fail
with `ENOENT` (errno -2, process exit code 254). npm chooses the package it
operates on by resolving a *local prefix*: it walks **upward** from the current
directory to the nearest ancestor that contains a `package.json` or a
`node_modules` directory, and it never searches **downward** into
subdirectories. Run at the repository root, it therefore never looked inside
`frontend/`, where the repository's only manifest lived; and because no
ancestor of the repository root held a manifest either, npm fell back to the
invocation directory and aborted on `open('<repository root>/package.json')`
before any dependency resolution began. The upward half of that resolution does
work, and you can confirm it in this repository today: `npm prefix` run from
`frontend/src` or from `backend/app` prints the repository root. The root
workspace manifest gives npm a manifest to open at the path it resolves, and
the `workspaces` array then redirects dependency resolution into
`frontend/package.json`.

`frontend/` has always been the intended npm project root, and three tracked
artifacts already treated it as such: `scripts/deploy.sh` changes into
`frontend` before running `npm run build`;
`infrastructure/docker/docker-compose.yml` builds the frontend service with
`context: ../../frontend`; and the requirements specification in
`documentation/` states that dependency management uses "npm for frontend, pip
for backend".

### Installation

1. Start from a local checkout of this repository, and run every command below
   from the repository root unless the step says otherwise. No canonical remote
   URL is published for this project, so this guide deliberately documents no
   `git clone` command: clone from whichever remote you were granted access to,
   then change into the checkout. Earlier revisions of this section printed a
   placeholder GitHub clone URL that does not resolve, which meant the very
   first step of the documented setup could never be followed as written; it has
   been removed rather than replaced with another guess.

2. Install dependencies. Both of the following invocations are supported; the
   first is recommended.

   From the repository root, which installs every workspace and hoists the
   result into a single root `node_modules/`:
   ```
   npm install
   ```
   Measured: **exit code 0**, `added 492 packages, and audited 494 packages`,
   and **zero `ENOENT`** occurrences in the output - the failure this command
   produced before the root manifest existed. The package counts are the
   reference measurement and move with the dependency graph; the exit code and
   the absence of `ENOENT` are the outcome that matters.

   Or from the frontend workspace directly, which is the directory that holds
   the application manifest:
   ```
   cd frontend
   npm install
   ```
   Measured: **exit code 0**. This path worked before the fix and still works.

   For a deterministic install that reproduces the committed root
   `package-lock.json` exactly, run the following from the repository root
   instead. This is the command the Frontend CI workflow uses, and it works
   because a single root lockfile is committed:
   ```
   npm ci
   ```
   Measured: **exit code 0**.

   Installing is not the same as building: `npm run build` and `npm run lint`
   still report the pre-existing application-source failures described under
   "Known limitations" below.

3. Set up environment variables (optional, and specific to each runtime). No
   `.env.example` template is committed to this repository yet, so there is
   nothing to copy, and there is no single place to put one file: which
   directory a `.env` has to sit in depends on the runtime meant to read it. A
   `.env` at the repository root is read by neither runtime.

   Frontend - put local overrides in `frontend/.env`. Vite reads its `.env`
   files from its own project root, and that root is `frontend/` on both
   documented paths: `npm start` at the repository root delegates the script
   into the workspace, so it runs with `frontend/` as its working directory,
   and `cd frontend && npm start` starts there to begin with. Vite exposes only
   variables whose names begin with `VITE_`, and only through
   `import.meta.env`. **This contract is not functional today.** The tracked
   source reads `process.env.REACT_APP_API_BASE_URL`
   (`frontend/src/services/api.ts`) and
   `process.env.REACT_APP_STRIPE_PUBLIC_KEY` (`frontend/src/index.tsx`,
   `frontend/src/services/payment.ts`) - a Create React App convention that
   Vite does not implement - so no value you set reaches the application until
   that source is migrated to `import.meta.env.VITE_*`. See "Known limitations"
   below. The `REACT_APP_API_URL` variable in
   `infrastructure/docker/docker-compose.yml` is a third, different name that
   applies to the container build only, not to local development.

   Backend - the service reads a file named `.env` resolved against the working
   directory of the Python process, because `backend/app/core/config.py`
   declares `env_file = ".env"` as a relative path. Put it in the directory you
   launch the service from (`backend/`, if you run `uvicorn app.main:app` from
   there). Its settings class requires `PROJECT_NAME`, `API_V1_STR`,
   `SECRET_KEY`, `ACCESS_TOKEN_EXPIRE_MINUTES`, `GOOGLE_CLOUD_PROJECT`,
   `GOOGLE_CLOUD_STORAGE_BUCKET`, `STRIPE_API_KEY` and `STRIPE_WEBHOOK_SECRET`
   - none of which has a default, so the process fails at import if one is
   missing - and accepts an optional `SENTRY_DSN`. Supplying these values does
   not by itself get the service running; see "Known limitations" below.

   `.gitignore` lists exactly two environment-file patterns, `.env` and
   `.env.*.local` (matching `.env.development.local`, for example), and they
   apply at the repository root and inside `frontend/` alike. Every other name is
   still tracked - including `.env.local`, which the mode-qualified
   `.env.*.local` pattern does not match, and `.env.production` - so keep real
   credentials in one of the two ignored names. Even then, ignore rules lower the
   chance of an accidental commit but are not a secret-management control: they
   do not cover a file that is already tracked, a deliberate `git add -f`, or
   credentials pasted into a differently named file. Keep real credentials
   outside version control, and review what you have staged before you commit.

4. Start the development server. Run this from the repository root, where it is
   delegated to the `frontend` workspace, or from `frontend/` directly:
   ```
   npm start
   ```

   `npm run dev` is an equivalent alias for the same Vite dev server, available
   at both the repository root and in `frontend/`.

5. Open your browser and navigate to `http://localhost:5173`, which is the port
   the Vite dev server binds. Note that the `3000:3000` mapping in
   `infrastructure/docker/docker-compose.yml` applies to the containerized
   deployment only and is unrelated to this local dev server. See "Known
   limitations" below for what to expect on that page today.

### Known limitations

Installing dependencies and starting the Vite dev server both work now, but the
frontend does not render yet and the backend service does not start. These are
pre-existing defects in the application source, not regressions introduced by
the setup fix described above, and they are recorded here so the accurate
commands are not mistaken for a fully working application.

- `npm start` boots the Vite dev server successfully (measured: "VITE v4.5.14
  ready in 184 ms" on port 5173), but `http://localhost:5173/` returns HTTP
  404. This was true both before and after the packaging fix.
- There is no `frontend/vite.config.ts`. Vite boots on its built-in defaults,
  which means the project root is `frontend/` and the public directory is
  `frontend/public/`.
- The entry HTML at `frontend/public/index.html` is unmigrated Create React App
  markup: it still uses `%PUBLIC_URL%` placeholders, its title reads "Personal
  Finance Tracker", and it contains no `<script type="module">` Vite entry, so
  it is not a usable Vite entry point and is not at the Vite project root.
  Because it sits in the public directory it is still reachable, so the two
  paths behave differently and neither renders the application:
  `http://localhost:5173/` returns 404 with a zero-byte body (no `index.html`
  exists at the Vite project root), while `http://localhost:5173/index.html`
  returns 200, serves that CRA file, and renders a blank page - its `#root`
  element stays empty because nothing bootstraps React, and the three literal
  `%PUBLIC_URL%` subresource paths each return 500.
- `frontend/tsconfig.json` declares the path aliases `@components/*`,
  `@pages/*`, `@utils/*`, `@styles/*`, `@hooks/*`, and `@context/*`, while the
  application source imports through `@/...` and `app/...` prefixes instead.
  None of the conventions the source actually uses is declared. Those imports
  are what `tsc` reports as 56 unresolved modules, spanning 36 distinct module
  paths. Declaring the two missing conventions would resolve 26 of the 36 (46
  of the 56 occurrences); the other 10 name modules that do not exist as files
  anywhere in the repository, so no configuration change can resolve them:
  `@/components/ListingManagement`, `@/components/MaintenanceHistory`,
  `@/components/PaymentStatus`, `@/components/PhotoGallery`,
  `@/components/ProfileForm`, `@/components/TransactionDetails`,
  `@/components/VehicleSpecs`, `@/services/document_processing`,
  `app/utils/auth`, and `app/utils/storage`.
- `frontend/src/index.tsx` reads `process.env.REACT_APP_STRIPE_PUBLIC_KEY`, a
  Create React App convention that Vite does not implement; Vite exposes
  `import.meta.env.VITE_*` instead. `frontend/src/services/api.ts` and
  `frontend/src/services/payment.ts` read `REACT_APP_*` names the same way, so
  the frontend environment contract described in installation step 3 cannot
  take effect until those reads are migrated.
- The backend does not start either, for two separate reasons that this change
  does not touch. No `requirements.txt` or other Python project metadata is
  committed, so its dependencies (FastAPI, Pydantic, Celery, Stripe, the Google
  Cloud client libraries, Pillow, PyPDF2) have to be installed by hand; and
  `backend/app/main.py` imports `auth_router`, `listings_router`,
  `transactions_router` and `messages_router`, none of which its API modules
  define - three of them define `router`, and `backend/app/api/auth.py` defines
  no router object at all - so importing the entrypoint raises `ImportError`
  before any configuration is read.

Repairing any of the above means editing application source, which is outside
the scope of the packaging and documentation fix. The static outcome below is
therefore expected in full, and none of it is a regression introduced by that
fix. It is stated completely so the packaging fix is not mistaken for a working
application:

- `npm run build` runs `tsc` before `vite build` and exits with code 2,
  reporting **85** TypeScript diagnostics in total: **56** `TS2307` "cannot
  find module" errors and **29** further diagnostics. Before the fix there were
  102 `TS2307` errors; the count for **external** packages is now **zero**,
  which is the result the dependency fix targeted. Every remaining `TS2307` is
  an internal path-alias failure of the kind described above.
- The 29 non-`TS2307` diagnostics are typing and package-contract failures in
  application source rather than packaging failures. By error code: 8 `TS2322`,
  6 `TS2345`, 3 `TS2339`, 3 `TS18046`, 3 `TS2304`, 2 `TS2305`, 2 `TS7006`, and
  2 `TS2614`. The clusters are:
  - `frontend/src/App.tsx` (7 diagnostics): the router is used through the
    React Router v5 API (`Switch`, and `exact`/`component` on `Route`), while
    the package this repository declares and installs is v6, which exports
    `Routes` and takes an `element` prop.
  - `frontend/src/services/payment.ts` (2): a possibly-undefined publishable
    key is passed to `loadStripe`, and a possibly-null card element is passed
    to `confirmCardPayment`.
  - `frontend/src/utils/validation.ts` (2): the `dompurify` default import is
    called as a function, whereas DOMPurify v3 exposes `DOMPurify.sanitize()`.
  - `frontend/src/store/index.ts` and `frontend/src/store/userSlice.ts` (4):
    the two slice reducers are imported as named exports although they are
    default exports, and the `User` type the slice annotates with is never
    defined or imported.
  - the remaining 14 are untyped Redux selector state, implicit `any`
    parameters, an undefined `VehicleListing` type name, and an import of
    `setupInterceptors` that `services/api` does not export, in `HomePage`,
    `SearchResultsPage`, `ListingCreationPage`, `MessageBox`, `Header`,
    `PricingInput`, `PhotoUploader`, `MaintenanceDocumentUploader`, and
    `index.tsx`.
- `npm run lint` exits with code 1, reporting 11 problems (10 errors and 1
  warning): seven `@typescript-eslint/no-explicit-any`, three
  `@typescript-eslint/no-unused-vars`, and one `react-hooks/exhaustive-deps`.
  All are pre-existing findings in application source. Before the fix the
  linter could not run at all, failing with `eslint: not found` and exit code
  127, so genuine findings are the linter working as intended.
- Some defects in the same source files are semantic rather than static, so
  neither `tsc` nor ESLint reports them. They are listed here so the two counts
  above are not read as the complete picture: `VehicleDetailsForm` passes a Zod
  `parse` wrapper as Formik's `validate` callback, which returns values or
  throws instead of returning an errors object, so the form cannot complete a
  valid submit; both image-processing modules request `useWebWorker: true`
  without a `libURL`, so `browser-image-compression` loads its worker from a
  public CDN at run time instead of from the integrity-locked dependency tree;
  those modules also pass a `quality` option that v2 ignores (it reads
  `initialQuality`) and never revoke the object URLs they create;
  `MaintenanceDocumentUploader` calls its completion callback with state from
  the previous render, so the newest upload is omitted; neither uploader limits
  file size or type or reports rejected files; and the Formik, Dropzone and
  Stripe UI lacks bound labels, error relationships, and status announcements.

A green `npm test` is a third outcome that needs reading carefully, because it
is the one most easily mistaken for a verdict on the application. The repository
contains **zero** frontend test files. This search returns nothing:
`git ls-files | grep -E '\.(test|spec)\.(ts|tsx|js|jsx)$'`, and Vitest agrees,
reporting "No test files found, exiting with code 0" against its default
`**/*.{test,spec}.?(c|m)[jt]s?(x)` discovery pattern, which nothing in this
repository narrows. So:

- `npm test` runs `vitest run --passWithNoTests --environment jsdom --globals`
  in the `frontend` workspace and exits 0, but it exits 0 *because*
  `--passWithNoTests` treats an empty test set as a pass instead of a failure.
  **Zero assertions execute.**
- What that validates is infrastructure only: the runner is installed, its
  dependencies resolve, the command is non-watch so it cannot hang a CI runner,
  and the no-test-file case is handled. It is not behavioural coverage, and it
  says nothing about whether any component, page, service, or utility works.
  There is no meaningful frontend coverage figure to quote.
- It does not imply that Frontend CI succeeds either.
  `.github/workflows/frontend_ci.yml` runs `npm run lint` *before* `npm test`,
  and that step exits 1 on the 11 pre-existing findings above, so the workflow
  stops before the test step is reached.
- The runtime is ready for the first test file somebody writes, which is the
  point of declaring the test stack at all. Both `test` and `test:watch` select
  the jsdom environment - `jsdom` is declared as a dependency, but it is not
  Vitest's default, and under the default `node` environment a React render
  fails outright with `document is not defined` - and both inject Jest-style
  globals. Those globals are what the declared `@types/jest` types describe,
  what `@testing-library/jest-dom` version 5 needs in order to extend the global
  `expect`, and what lets React Testing Library register its automatic
  between-test cleanup on the global `afterEach`. Because no Vitest setup file
  is committed, and adding one is outside the scope of this packaging fix, each
  test file must `import '@testing-library/jest-dom'` itself to get those
  matchers.

For reference, since JSON cannot carry comments, these are the reasons the
manifests contain what they contain:

- Eight runtime packages - `zod`, `formik`, `react-dropzone`,
  `@stripe/react-stripe-js`, `@stripe/stripe-js`, `browser-image-compression`,
  `date-fns`, and `dompurify` - are declared in `frontend/package.json` because
  tracked source imports them while the manifest previously did not declare
  them, and `tsc` reported `TS2307` for each.
- `vitest`, `jsdom`, `eslint`, and the TypeScript ESLint toolchain are declared
  because the manifest's own `test` and `lint` scripts named executables that
  were never declared, so both scripts failed with exit code 127. The `test` and
  `test:watch` scripts then pass `--environment jsdom --globals`, because
  declaring `jsdom` is not the same as selecting it: Vitest defaults to a `node`
  environment with globals switched off, and the declared React Testing Library
  and jest-dom stack cannot run under those defaults.
- Both manifests declare an `engines` field because the complete absence of any
  runtime floor is how an end-of-life Node 14 pin survived unchallenged in
  continuous integration.

The dependency graph the committed lockfile pins is not free of published
security advisories. Rather than leave that implicit, the state measured on the
toolchain this fix was validated against (Node 20.20.2, npm 10.8.2) is recorded
here. Reproduce it from the repository root with:

```
npm audit             # 11 advisories: 1 critical, 7 high, 3 moderate
npm audit --omit=dev  # 2 advisories, both moderate, both React Router
```

Advisory databases change daily, so re-run those commands rather than trusting
the counts: they are the state on the day they were taken, not a fixed property
of the manifests. The eleven entries come from four root causes, and none can be
closed without changing a package version - which this change deliberately does
not do, because it is scoped to manifest location, dependency declaration and
setup documentation, and holds every package at the project's declared React 18,
TypeScript 5.1 and Vite 4 baseline. The advisories below are therefore accepted
deliberately rather than overlooked, on the terms stated with each one.

- `vitest` (declared `^0.34.6`, locked 0.34.6) is inside the affected range of
  GHSA-5xrq-8626-4rwp, rated critical, which npm reports as `<3.2.6`. The
  advisory describes arbitrary file read and execution **when the Vitest UI
  server is listening**, and that server cannot be started in this graph:
  `@vitest/ui` and `@vitest/browser` are optional peer dependencies of Vitest
  and neither is installed, so `vitest run --ui` fails at once with
  `MISSING DEP  Can not find dependency '@vitest/ui'`. No script in either
  manifest and no step in `.github/workflows/frontend_ci.yml` passes `--ui`,
  `--api`, or a browser-mode flag, and `npm test` was measured opening no
  listening TCP socket at all. No patched Vitest is installable here: 0.34.6 is
  the last release of the 0.34.x line, and every patched line brings Vite 5 or
  newer with it (Vitest 2.1.9 depends on `vite ^5.0.0`, 3.2.6 on
  `vite ^5.0.0 || ^6.0.0 || ^7.0.0-0`, and the 4.x line npm nominates as the fix
  peers `vite ^6.0.0 || ^7.0.0 || ^8.0.0`), while this project declares
  `vite ^4.4.2`. Until that upgrade lands, keep the vulnerable surface
  unreachable: do not add `@vitest/ui` or `@vitest/browser`, do not pass `--ui`
  or `--api` to Vitest, and prefer the non-watch `npm test`. Note that
  `npm run test:watch` runs Vite in middleware mode and was measured opening
  Vite's default HMR websocket on port 24678, bound to the wildcard address
  rather than to localhost, so run the watch script on a trusted network only.
- Six of the seven high-severity entries are one problem counted six times.
  `@typescript-eslint/eslint-plugin`, `/parser`, `/type-utils`,
  `/typescript-estree`, `/utils` and `minimatch` itself all trace to a single
  edge: `@typescript-eslint/typescript-estree@6.21.0` depends on `minimatch` at
  the exact version `9.0.3`, and `minimatch` below 9.0.7 carries the ReDoS
  advisories GHSA-3ppc-4f35-3m26, GHSA-7r86-cg39-jmmj and GHSA-23c5-xmqv-rm74.
  Because that dependency is an exact pin rather than a range, regenerating
  `package-lock.json` cannot reach a patched 9.0.7 or later; closing it needs
  either a root `overrides` entry or a move off the affected
  `@typescript-eslint` range, which npm reports as `6.16.0 - 7.5.0`. Both are
  version changes outside this fix. The exposure is lint-time only: these are
  devDependencies, they are absent from `npm audit --omit=dev`, none of their
  code is shipped to a browser, and the only glob patterns that reach
  `minimatch` here come from this repository's own ESLint configuration and its
  `npm run lint` invocation.
- The seventh high entry is `vite` itself (locked 4.5.14, affected `<=6.4.2`),
  which also brings the moderate `esbuild` development-server advisory, and the
  two remaining moderate entries are `react-router` and `react-router-dom`
  (locked 6.30.4). `vite` and `react-router-dom` were both declared in
  `frontend/package.json` before this change and their version ranges are
  untouched by it, while `esbuild` and `react-router` arrive only as their
  transitive dependencies - so these advisories predate the packaging fix rather
  than arriving with it. Neither has an in-range remedy: 6.30.4 is the newest
  release on the React Router 6 line, and a patched Vite is a major upgrade.

Two follow-on changes clear the accepted items. Both are version work that
belongs in its own change with its own validation, and neither is attempted
here:

- Move Vite from 4 to 5 or newer together with Vitest to 3.2.6 or newer, with
  the matching `@vitejs/plugin-react`, then regenerate the root lockfile and
  re-audit. That is what closes the critical entry, and it closes the `vite` and
  `esbuild` entries with it.
- Move `@typescript-eslint/parser` and `@typescript-eslint/eslint-plugin` off
  the `6.16.0 - 7.5.0` range. 7.18.0 was checked as peering `eslint ^8.56.0`, so
  it fits the declared `eslint ^8.50.0` (locked 8.57.1) without forcing the
  ESLint 9 flat-config migration. Regenerate the root lockfile, re-audit, and
  expect the 11 lint findings above to be re-counted against the newer rules.

## Usage

1. Register an account or log in if you already have one
2. Browse car listings or create your own listing
3. Use the search and filter options to find specific cars
4. Contact sellers through the messaging system
5. Leave reviews and ratings for completed transactions

## API Documentation

There is no API reference committed to this repository: a dedicated
`docs/api.md` is not present, so the link that once pointed at it has been
removed rather than left dangling.

The specifications under `documentation/` are the fullest description of the
API available, but they describe the **intended design**, not the current
implementation. Read them as design targets, and read the code under
`backend/app/api/` as the only statement of what exists today:

- `documentation/Technical Specifications.md` - intended system design, the
  target API endpoint list (section 5.3), and technology stack
- `documentation/Software Requirements Specifications (SRS).md` - functional
  and non-functional requirements
- `documentation/Software Project Proposal.md` - product scope and objectives

## Contributing

We welcome contributions to the Used Car Marketplace project. Two things are
missing that a contributor would normally rely on, and neither is invented here:
no `CONTRIBUTING.md` guide is present in this repository yet, and no issue
tracker is published for it - because the project has no canonical remote, there
is no repository-side queue to file against, which is why this section no longer
directs you to file one.

Until both exist: discuss the change you intend to make with whoever maintains
the remote you cloned from, keep pull requests focused, and before submitting
confirm that `npm test` still exits 0, and that `npm run lint` and
`npm run build` report no findings beyond the pre-existing ones described under
"Known limitations". Read that `npm test` result for what it is worth: with no
frontend test files committed it proves that the runner and its dependencies
still resolve, and nothing more - not that behaviour is unbroken. If your change
adds or alters behaviour, add tests for it; the jsdom environment and Jest-style
globals the `test` script selects are already in place, and a new file named
`*.test.ts`, `*.test.tsx`, `*.spec.ts`, or `*.spec.tsx` is discovered with no
further configuration.

## License

This project is intended to be licensed under the MIT License. A `LICENSE` file
is not present in this repository yet.

## Support

No monitored support channel is documented for this repository, and this section
no longer names one. There is no published issue tracker (see "Contributing"
above), and the support mailbox this section previously advertised is not
verifiable from anything in the repository - nothing here establishes that the
address exists or that anyone reads it - so it has been removed rather than left
standing as a promise the project cannot keep.

The only contact address the repository itself contains is the one rendered in
the application footer (`frontend/src/components/Footer.tsx`). That is product
placeholder copy inside the user interface, not a verified maintainer channel,
so it is deliberately not repeated here as a support route. Until a canonical
channel is confirmed, and the footer aligned to it, route questions to whoever
gave you access to this repository.
