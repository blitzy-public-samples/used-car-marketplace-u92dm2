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

This list reflects what the repository actually declares and installs,
cross-checked against `documentation/Technical Specifications.md` (sections 6.1
through 6.4).

- Frontend: React 18 with TypeScript, built and served by Vite
- Routing: React Router
- State Management: Redux Toolkit (`@reduxjs/toolkit`)
- HTTP Client: Axios
- Styling: Tailwind CSS
- Backend: Python with FastAPI (service entry point: `backend/app/main.py`)
- Data: Google Cloud Firestore (primary NoSQL store), Google Cloud SQL
  (PostgreSQL) for transaction and financial records, and Google Cloud Storage
  for vehicle photos and maintenance documents
- Authentication: JSON Web Tokens (JWT)
- Payments: Stripe
- Frontend Testing: Vitest with React Testing Library
- Deployment: Docker and Kubernetes (Google Kubernetes Engine), provisioned
  with Terraform

## Getting Started

### Prerequisites

- Node.js 20.x LTS and npm 9 or later (recommended). The hard floor declared in
  the `engines` field of both `package.json` and `frontend/package.json` is
  Node >= 18.0.0 and npm >= 9.0.0; 20.x LTS is the runtime this setup procedure
  was validated against, and it matches the `@types/node` version the frontend
  declares.
- Node.js 14 and Node.js 16 are **not** supported. This repository is an npm
  workspace, and npm workspaces require npm 7 or later. Node 14 ships npm 6,
  which can neither resolve a `workspaces` array nor read the committed
  `lockfileVersion 3` lockfile. This is also why the Frontend CI workflow pins
  a 20.x runtime.
- Python 3.9 or later, for the backend service
  (`.github/workflows/backend_ci.yml` pins 3.9).
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
with `ENOENT` (errno -2, process exit code 254). npm resolves the manifest as
the literal path `<current directory>/package.json`; it does not search parent
directories and it does not search subdirectories. So with no manifest at the
invocation directory, the command aborted before any dependency resolution
began. The root workspace manifest gives npm a manifest to open at exactly the
path it resolves, and the `workspaces` array then redirects dependency
resolution into `frontend/package.json`.

`frontend/` has always been the intended npm project root, and three tracked
artifacts already treated it as such: `scripts/deploy.sh` changes into
`frontend` before running `npm run build`;
`infrastructure/docker/docker-compose.yml` builds the frontend service with
`context: ../../frontend`; and the requirements specification in
`documentation/` states that dependency management uses "npm for frontend, pip
for backend".

### Installation

1. Clone the repository:
   ```
   git clone https://github.com/your-username/used-car-marketplace.git
   cd used-car-marketplace
   ```

2. Install dependencies. Both of the following invocations are supported; the
   first is recommended.

   From the repository root, which installs every workspace and hoists the
   result into a single root `node_modules/`:
   ```
   npm install
   ```

   Or from the frontend workspace directly, which is the directory that holds
   the application manifest:
   ```
   cd frontend
   npm install
   ```

   For a deterministic install that reproduces the committed root
   `package-lock.json` exactly, run the following from the repository root
   instead. This is the command the Frontend CI workflow uses, and it works
   because a single root lockfile is committed:
   ```
   npm ci
   ```

3. Set up environment variables (optional). No `.env.example` template is
   committed to this repository yet, so there is nothing to copy. If you need
   local overrides, create a `.env` file yourself and populate it with your own
   configuration. `.env` and `.env.*.local` are listed in `.gitignore`, so
   local credentials are never committed by accident.

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

Installing and starting the project works, but the application does not render
yet. These are pre-existing defects in the application source, not regressions
introduced by the setup fix described above, and they are recorded here so the
accurate commands are not mistaken for a fully working application.

- `npm start` boots the Vite dev server successfully, but
  `http://localhost:5173/` returns HTTP 404. This was true both before and
  after the packaging fix.
- There is no `frontend/vite.config.ts`. Vite boots on its built-in defaults.
- The entry HTML at `frontend/public/index.html` is unmigrated Create React App
  markup: it still uses `%PUBLIC_URL%` placeholders, its title reads "Personal
  Finance Tracker", and it contains no `<script type="module">` Vite entry, so
  it is not a usable Vite entry point and is not at the Vite project root.
- `frontend/tsconfig.json` declares the path aliases `@components/*`,
  `@pages/*`, `@utils/*`, `@styles/*`, `@hooks/*`, and `@context/*`, while the
  application source imports through `@/...` and `app/...` prefixes instead.
  None of the conventions the source actually uses is declared. Four components
  referenced through those aliases - `ListingManagement`, `MaintenanceHistory`,
  `PaymentStatus`, and `PhotoGallery` - do not exist as files at all.
- `frontend/src/index.tsx` reads `process.env.REACT_APP_STRIPE_PUBLIC_KEY`, a
  Create React App convention that Vite does not implement; Vite exposes
  `import.meta.env.VITE_*` instead.

Repairing any of the above means editing application source, which is outside
the scope of the packaging and documentation fix. Two command outcomes
therefore remain expected, and neither is a regression:

- `npm run build` exits with code 2, reporting 56 `TS2307` "cannot find module"
  errors. Every one of them is an internal path-alias failure of the kind
  described above. Before the fix there were 102 such errors; the count for
  **external** packages is now zero, which is the result the dependency fix
  targeted.
- `npm run lint` exits with code 1, reporting 11 problems (10 errors and 1
  warning): seven `@typescript-eslint/no-explicit-any`, three
  `@typescript-eslint/no-unused-vars`, and one `react-hooks/exhaustive-deps`.
  All are pre-existing findings in application source. Before the fix the
  linter could not run at all, failing with `eslint: not found` and exit code
  127, so genuine findings are the linter working as intended.

For reference, since JSON cannot carry comments, these are the reasons the
manifests contain what they contain:

- Eight runtime packages - `zod`, `formik`, `react-dropzone`,
  `@stripe/react-stripe-js`, `@stripe/stripe-js`, `browser-image-compression`,
  `date-fns`, and `dompurify` - are declared in `frontend/package.json` because
  tracked source imports them while the manifest previously did not declare
  them, and `tsc` reported `TS2307` for each.
- `vitest`, `jsdom`, `eslint`, and the TypeScript ESLint toolchain are declared
  because the manifest's own `test` and `lint` scripts named executables that
  were never declared, so both scripts failed with exit code 127.
- Both manifests declare an `engines` field because the complete absence of any
  runtime floor is how an end-of-life Node 14 pin survived unchallenged in
  continuous integration.

## Usage

1. Register an account or log in if you already have one
2. Browse car listings or create your own listing
3. Use the search and filter options to find specific cars
4. Contact sellers through the messaging system
5. Leave reviews and ratings for completed transactions

## API Documentation

A dedicated API reference (`docs/api.md`) is not present in this repository
yet. Until it is written, the authoritative descriptions of the API surface,
data model, and technology stack are the specifications under `documentation/`:

- `documentation/Technical Specifications.md` - system design, API endpoints,
  and technology stack
- `documentation/Software Requirements Specifications (SRS).md` - functional
  and non-functional requirements
- `documentation/Software Project Proposal.md` - product scope and objectives

The backend routes themselves live under `backend/app/api/`.

## Contributing

We welcome contributions to the Used Car Marketplace project. A
`CONTRIBUTING.md` guide is not present in this repository yet; until it is
added, please open an issue to discuss the change you intend to make, keep pull
requests focused, and before submitting confirm that `npm test` still passes
and that `npm run lint` and `npm run build` report no findings beyond the
pre-existing ones described under "Known limitations".

## License

This project is intended to be licensed under the MIT License. A `LICENSE` file
is not present in this repository yet.

## Support

If you encounter any issues or have questions, please open an issue on our GitHub repository or contact our support team at support@usedcarmarketplace.com.