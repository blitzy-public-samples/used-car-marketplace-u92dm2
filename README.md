# Used Car Marketplace

A comprehensive platform for buying and selling used cars, connecting sellers with potential buyers in a user-friendly and efficient manner.

## Features

Delivered as HTTP endpoints today:

- Car listing creation, retrieval, update and deletion, with role and ownership checks
- Search and filtering over listings
- Messaging between a buyer and a seller about a listing
- Reading a transaction, restricted to its buyer and seller
- **User ratings and reviews**: buyers rate sellers and sellers rate buyers on a 1 to 5 scale, with an optional written review, restricted to the two counterparties of a completed transaction. See [Ratings and Reviews](#ratings-and-reviews)
- Token-based authorization on every protected endpoint: the bearer token's subject is resolved against the `users` collection on each request, and the caller's role and verification state are read from that document

Specified but **not** implemented — do not expect these to work:

- **Registration, login and token issuance.** The authentication router mounts no routes at all, so there is no in-product way to create an account or obtain a token. See [Obtaining a token](#obtaining-a-token).
- **Creating a transaction and taking payment.** The transaction `POST` handler reads request fields the `Transaction` model does not declare (`vehicle_id`, `payment_method`) and then reads the payment service's result as an object although that service returns a dictionary, so the call answers `500` rather than completing a purchase. Transactions consequently have to be written into the datastore directly, which is also how a `completed` transaction is arranged for rating.
- Favourite listings and saved searches
- An administrator panel. Administrator *authorization* exists and is enforced (rating moderation is admin-only), but no administrative user interface ships.
- Any user interface at all, in practice: the React application is present in source but does not currently mount in a browser. See [Known Limitations](#known-limitations).

## Technologies Used

- Frontend: React 18 with TypeScript, built and served by Vite 4
- Backend: Python 3.9 with FastAPI
- Database: Google Cloud Firestore in native mode. Collections in use include `users`, `listings`, `transactions`, `messages`, `ratings` and `vehicles`
- Authentication: JSON Web Tokens (JWT). No authorization state travels in the token: the caller's user document is re-read on every request, so a role or verification change takes effect immediately
- State Management: Redux Toolkit
- Styling: Tailwind CSS (utility-first), compiled through PostCSS
- Testing: pytest for the backend, Vitest with React Testing Library for the frontend
- Infrastructure: Terraform declarations for Google Cloud, plus a Docker Compose file, a `scripts/deploy.sh` and Firestore index declarations. The container and Kubernetes assets are **incomplete and not currently runnable** — see [Known Limitations](#known-limitations)

## Getting Started

The repository holds two independently built applications: a FastAPI service in `backend/` and a Vite + React client in `frontend/`. There is no package at the repository root and no single install command — each application is installed and run in its own directory.

### Prerequisites

- **Python 3.9** — the version `.github/workflows/backend_ci.yml` pins. Pydantic v1 is required (`app/core/config.py` imports `BaseSettings` from `pydantic`), so do not substitute Pydantic v2.
- **Node.js 14.18 or later** — the installed toolchain declares `engines` of `^14.18.0 || >=16.0.0` (Vite 4.5.14) and `>=v14.18.0` (Vitest 0.34.6), so 14.0–14.17 will not run it. `.github/workflows/frontend_ci.yml` pins the `14.x` line; locally, any 14.18+, 16+ or later LTS release works.
- **A Google Cloud project with Firestore in native mode**, or the Firestore emulator from the `gcloud` CLI for local work. The Firestore, Cloud Storage, Vision and Document AI clients are constructed at module import, so credentials — or an emulator standing in for them — must be in place before the application can even be imported.
- **A Stripe test-mode account**, if you intend to exercise the payment path.

### 1. Get the source

No public remote is published with this repository, so use the URL your project owner provides:

```bash
git clone <repository-url> used-car-marketplace
cd used-car-marketplace
```

### 2. Install the backend

```bash
python3.9 -m venv backend/.venv
backend/.venv/bin/python -m pip install --upgrade pip
backend/.venv/bin/pip install -r backend/requirements.txt
```

`requirements.txt` pins every direct dependency to an exact version, and those versions were resolved and installed together on Python 3.9, so the set is known to be mutually consistent. No transitive lock file is shipped: the versions of indirect dependencies are whatever pip resolves at install time. CI installs the same way, with `pip install -r requirements.txt` from inside `backend/`.

### 3. Install the frontend

```bash
cd frontend
npm ci
cd ..
```

Use `npm ci` rather than `npm install`: it installs exactly what the committed `frontend/package-lock.json` records, which is also what `.github/workflows/frontend_ci.yml` runs.

### 4. Configure the environment

Each application has its own committed template, and every key is documented inline in the template itself. Copy both, then edit them:

```bash
cp backend/.env.example backend/.env
cp frontend/.env.example frontend/.env
```

Both `.env` files are git-ignored. Nothing in `frontend/.env` is secret in any case: every value there is compiled into the JavaScript bundle and is readable by anyone who opens it, so secrets belong in `backend/.env` only.

**`SECRET_KEY` must be replaced before the application will start.** The template ships a sentinel value, and because that file is committed, `Settings` refuses it at import rather than letting a public string become a live signing key. Generate a real one:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

#### Backend settings — read from `backend/.env` or the process environment

`settings = Settings()` is evaluated at import time, so every **required** key below must be present or the application cannot be imported at all.

| Key | Type | Required | Default | Read by | Example |
|---|---|:---:|---|---|---|
| `PROJECT_NAME` | string, non-blank | Yes | — | FastAPI application title | `Used Car Marketplace` |
| `API_V1_STR` | string, non-blank | Yes | — | OAuth2 `tokenUrl` in `app/core/security.py`; must match the prefixes `app/main.py` mounts | `/api` |
| `SECRET_KEY` | string, ≥32 chars, not a known placeholder | Yes | — | JWT signing and verification in `app/api/auth.py` | output of the command above |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | integer | Yes | — | Declared and validated, but **not yet consumed**: both auth modules hardcode a 15-minute lifetime | `30` |
| `GOOGLE_CLOUD_PROJECT` | string, non-blank | Yes | — | Firestore, Cloud Storage and Document AI clients, at import | `my-gcp-project-id` |
| `GOOGLE_CLOUD_STORAGE_BUCKET` | string, non-blank | Yes | — | `app/db/cloud_storage.py`, at import. Bucket name only, no `gs://` and no trailing slash | `my-bucket-name` |
| `STRIPE_API_KEY` | string, non-blank | Yes | — | `app/services/payment.py`, at import. Use a test-mode key | `sk_test_…` |
| `STRIPE_WEBHOOK_SECRET` | string, non-blank | Yes | — | Stripe webhook signature verification | `whsec_…` |
| `ALGORITHM` | string | No | `HS256` | Both auth modules. HS256 is correct because the symmetric `SECRET_KEY` signs the token | `HS256` |
| `ALLOWED_ORIGINS` | JSON array of origins | No | `["http://localhost:3000"]` | CORS in `app/main.py`. `*`, `null`, blank entries and anything carrying a path are refused, because credentials are allowed | `'["http://localhost:3000"]'` |
| `SENTRY_DSN` | string | No | unset | Error reporting; leave the line commented to keep it `None` rather than empty | — |
| `RATING_MIN` | integer, not greater than `RATING_MAX` | No | `1` | Lower bound of the rating scale. Mirrored by the client, so both sides must move together | `1` |
| `RATING_MAX` | integer, not less than `RATING_MIN` (checked at import) | No | `5` | Upper bound of the rating scale. Mirrored by the client, so both sides must move together | `5` |
| `RATING_REVIEW_MAX_LENGTH` | integer | No | `2000` | Maximum review length, measured on the normalized text. Mirrored by the client's character counter | `2000` |
| `RATING_WINDOW_DAYS` | integer, 1–365 (checked at import) | No | `14` | Days after which an unreciprocated rating becomes due for publication. Freely tunable; `0` is refused because it would silently collapse the double-blind reveal | `14` |

#### Google client variables — process environment only

Three names are read by the Google client libraries **straight from `os.environ`**, not from `Settings`. Pydantic's `env_file` support parses `backend/.env` only to populate declared settings and exports nothing, so writing these three into that file alone has no effect. Export them, or source the file with `set -a` so the shell exports every key. When sourcing, leave `ALLOWED_ORIGINS` quoted exactly as the template writes it — `ALLOWED_ORIGINS='["http://localhost:3000"]'` — because the shell strips the outer quotes and the inner JSON has to survive intact.

| Variable | Purpose |
|---|---|
| `GOOGLE_APPLICATION_CREDENTIALS` | Absolute path to a service-account key file. Vision AI and Document AI construct their clients at import and have no emulator, so a loadable key is needed even offline. Keep the key outside the repository. |
| `FIRESTORE_EMULATOR_HOST` | Points the Firestore client at a local emulator instead of a real project, e.g. `localhost:8080`. No credentials are needed while it is set. |
| `STORAGE_EMULATOR_HOST` | Makes the Cloud Storage client anonymous so the module imports without real credentials, e.g. `http://localhost:9199`. There is no local GCS emulator here, so a real upload still needs a real bucket. |

#### Frontend variables — `frontend/.env` or the process environment

| Variable | Required | Read by | Notes |
|---|:---:|---|---|
| `REACT_APP_API_BASE_URL` | Yes, to serve or build | `src/services/api.ts`, `src/services/rating.ts` | `frontend/vite.config.ts` refuses to serve or build without it. The value must be an absolute `http(s)` URL whose path ends in `/api`, e.g. `http://localhost:8000/api` |
| `REACT_APP_STRIPE_PUBLIC_KEY` | For the payment path | `src/index.tsx`, `src/services/payment.ts` | Stripe publishable (test) key |

Only variables prefixed `REACT_APP_` are exposed to client code, and they are baked into the bundle at build time — never put a secret in one.

### 5. Run the applications

Start the API from `backend/`, with the environment loaded:

```bash
cd backend
set -a && . ./.env && set +a
export GOOGLE_APPLICATION_CREDENTIALS=/absolute/path/to/service-account-key.json
.venv/bin/python -m uvicorn app.main:app --reload --port 8000
```

Interactive, generated API documentation is then served at `http://localhost:8000/docs`.

To use a local Firestore instead of a real project, start the emulator first and export its address:

```bash
gcloud emulators firestore start --host-port=localhost:8080
export FIRESTORE_EMULATOR_HOST=localhost:8080
```

Start the client from `frontend/`:

```bash
cd frontend
npm start
```

That runs Vite, which serves on `http://localhost:3000` — the port is pinned, so it reports a conflict rather than quietly moving to another one, and it is the origin the backend's default CORS allow-list names.

> **The client does not render today.** `npm start` returns an HTTP 200 document whose React root stays empty, and `npm run build` fails, both because of a pre-existing frontend defect unrelated to any recent feature work. The precise cause and its blast radius are recorded under [Known Limitations](#known-limitations).

## Usage

The API is the usable surface today; the client is not (see [Known Limitations](#known-limitations)). Working against the API directly, the flow is:

1. Obtain a bearer token — there is no registration or login endpoint, so see [Obtaining a token](#obtaining-a-token) below
2. Browse listings, or create one as a seller
3. Filter listings by the supported query parameters
4. Message the other party about a listing
5. Arrange a transaction. The transaction *creation* endpoint does not currently work (see Features above), so write the document into the `transactions` collection directly. Give it the whole `Transaction` field set — `id`, `buyer_id`, `seller_id`, `vehicle_listing_id`, `amount`, `status`, `stripe_payment_intent_id`, `created_at`, `updated_at` — because the read endpoint validates against that model and answers `500` on a document missing any of them
6. Once that transaction's status is `completed`, rate your counterparty: one rating per participant, from a verified account, scored 1 to 5 with an optional written review

### Obtaining a token

`backend/app/api/auth.py` mounts an `APIRouter` that **declares no routes**: registration, login and token issuance belong to a feature that has not been built, so no `POST /api/auth/login` or equivalent exists. The module does provide the pieces the rest of the API depends on — `create_access_token` and the `get_current_user` dependency — so a token has to be minted out of band, for example from a Python shell with the application's environment loaded:

```bash
cd backend
set -a && . ./.env && set +a
.venv/bin/python -c "
from datetime import timedelta
from app.api.auth import create_access_token
print(create_access_token({'sub': 'the-user-document-id'}, timedelta(minutes=60)))
"
```

Send it as `Authorization: Bearer <token>`. Two things determine whether that token resolves to a caller:

- The `sub` claim must be the **document ID of an existing document in the `users` collection**, because `get_current_user` re-reads that document on every request.
- That document must satisfy the `User` model — including `created_at` and `updated_at`, which are required. A document missing them is answered `401`, not `403`.

For the rating endpoints, the same document must also carry `is_verified: true`, and rating moderation additionally requires `role: "admin"`.

## Ratings and Reviews

The marketplace runs a bidirectional peer reputation system: for a purchase they both took part in, the buyer rates the seller and the seller rates the buyer. A rating is an integer score from 1 to 5, plus an optional written review whose length the server bounds.

Two authorization gates govern every submission, and the server enforces both:

- The rater must be a verified user. Accounts carry an `is_verified` flag, and an unverified caller is refused with `403`. Verified here means account (email) verification only; the platform performs no identity or KYC verification, which is an explicitly excluded feature of the project.
- The rater and the rated party must be the two counterparties of the same transaction, and that transaction must be completed. A caller who is neither the buyer nor the seller of the cited transaction is refused with `403`, and a transaction that has not completed is refused with `409`.

Each participant may rate a given transaction once. A second submission is refused with `409`, and that constraint is enforced by the datastore rather than by an application check, so it holds even when two submissions arrive at the same moment.

Who is rated is decided by the server, not by the rater. The rated party and the direction of the rating (buyer to seller, or seller to buyer) are derived from the transaction record rather than read from the request, so a rating cannot be redirected at somebody else and nobody can rate themselves.

Publication is double-blind. A submitted rating stays unpublished, and is excluded from the recipient's average, until the counterparty submits theirs or the rating window elapses. The length of that window is configurable (`RATING_WINDOW_DAYS`, 14 days by default).

The reciprocal reveal is immediate: the submission that completes a pair publishes both sides in a single transaction. Expiry, by contrast, makes publication *due* rather than automatic. Window expiry is not driven by a scheduled job, and no such job runs in this deployment - no broker is provisioned and nothing dispatches the background task. Instead, a read of a user's reputation publishes whatever is already due before it answers, oldest first, which is what makes the deferred reveal correct without a worker. The practical consequence, stated plainly: a rating whose window has closed and whose recipient's reputation nobody reads stays unpublished until somebody reads it, and a single read settles a bounded number of overdue ratings rather than an unlimited backlog.

Double-blind publication **reduces** retaliation rather than eliminating it, and the residual path is worth stating: while both ratings are still pending, neither party can see the other's, so there is nothing to retaliate against. But no deadline is imposed on submission, so a party who has not yet rated can read a rating that published on window expiry and then submit their own - which publishes immediately, because its counterpart already exists. Nothing adjudicates a rating one party believes is unfair; there is no dispute process.

Every user carries a `rating_average` and a `rating_count` computed from published ratings only, and the average is the exact mean of those scores, rounded only for display. Both figures are read from the user's own document rather than recomputed, so a reputation costs one document read. A reputation read returns at most the 50 most recent ratings a reader may see, and there is no pagination, so `aggregate` can legitimately describe more ratings than `items` lists.

The profile page and the seller block of a vehicle's detail page both mount the badge that renders those figures, and one honest caveat applies to both screens: the frontend as a whole does not currently build, so neither can be reached today ([Known Limitations](#known-limitations)). Around thirty pre-existing files, including both of those pages, import through an `@/...` path alias that nothing declares, and repairing that is a separate change this feature does not undertake. The rating components, the API client and the field they read have been verified directly, in a browser against the running backend; what has not been verified is the pages rendering inside an application that compiles. The values themselves are readable from the API.

An administrator may withhold a review for a policy violation (abuse, personally identifying information, or profanity), and a rejection cannot be recorded without a stated reason. **Moderation governs the review text, never the score**: a review's words are published only once a moderator approves them - until then the score is shown and the text is withheld - while `rating_average` and `rating_count` count every published rating regardless of how low it is, and are never adjusted by a moderation decision. The recorded reason is visible only to administrators through the moderation endpoint - not to the rating's author, not on any public read, and not in the logs, which record that a reason exists and how long it is but never its text. Sentiment neutrality is a policy the code supports rather than proves: there is no score threshold and no score-correlated path anywhere in the feature, but nothing inspects the *meaning* of a stated reason.

Ratings do not affect search result ranking. The aggregate data that would enable that is exposed through the API, but listing query semantics are unchanged.

Six endpoints serve the feature:

- `POST /api/ratings` - submit a rating; the caller must be authenticated, verified, and a participant of a completed transaction
- `GET /api/ratings/user/{user_id}` - up to the 50 newest published ratings a user has received that the reader may be shown, plus their aggregate over all published ratings (public read, no pagination)
- `GET /api/ratings/user/{user_id}/aggregate` - just the aggregate, for the reputation badge (public read)
- `GET /api/ratings/transaction/{transaction_id}` - the ratings attached to one transaction (participants only)
- `GET /api/ratings/eligibility/{transaction_id}` - whether the caller may rate, and why not if they may not
- `PATCH /api/ratings/{rating_id}/moderation` - move a rating between moderation states (administrator only)

For the data model, the full status-code matrix, the eligibility rules and the configuration tunables, please refer to the [Ratings and Reviews](./docs/features/ratings.md) documentation.

## Testing and Quality Checks

Every command below is run from the directory named. The results quoted are what the current tree produces, so a different result is a change you introduced.

Backend, from `backend/` with the environment loaded (`set -a && . ./.env && set +a`):

```bash
.venv/bin/python -m pytest -q                                                          # 410 passed
.venv/bin/python -m flake8 . --exclude=.venv --count                                   # 125 style violations
```

A bare `pytest` — the command CI runs — is green: `tests/conftest.py` excludes the three
legacy modules that cannot be imported at all, with the reason recorded beside the list (see
below). None of the 125 style violations is in a file this feature owns; every one is in a
module it does not touch.

Frontend, from `frontend/`:

```bash
npx vitest run           # 11 test files, 445 tests, all passing
npx tsc --noEmit         # 92 errors across 22 files (pre-existing)
npm run lint             # 0 errors, 0 warnings
npm run build            # FAILS at the tsc step, on those 92 pre-existing errors
```

What the tests do and do not cover:

- The rating suites are self-contained. `backend/tests/conftest.py` seeds every required setting **before** the application is imported, replaces Firestore with an in-memory double that reproduces the behaviour the feature depends on (notably an `AlreadyExists` error on a second create to the same document ID, so the one-rating-per-transaction rule is genuinely exercised), stubs the Google Cloud clients, and installs a guard over `socket.connect` so an accidental outbound call fails loudly.
- **No test touches a live Firestore, a real Google Cloud project, or Stripe.** Anything only a real datastore can prove — composite index enforcement above all, since an undeclared index fails at request time in production and nowhere else — is outside what the suite can tell you.
- The three legacy modules `tests/test_api.py`, `tests/test_services.py` and `tests/test_tasks.py` fail at **collection**, because they import modules that do not exist (`app.models`, `app.database`, `app.auth`, `backend.tasks`) and patch an AWS Textract client this codebase does not use. They are excluded by name in `tests/conftest.py`, which is what lets a bare `pytest` be a working gate; they are excluded rather than modified, so repairing them stays visible work. Remove a name from that list the moment its module imports.
- The frontend suites cover the rating components only, including the accessibility contract of the score control: it renders as a `radiogroup` with five `radio` options carrying `aria-checked`, exposes a single tab stop with roving focus, moves with the arrow keys (wrapping) and Home/End, selects with Space, shows a visible focus ring at every position, and echoes the value as text ("4 out of 5", or "No score selected") so the score is never conveyed by colour or shape alone.

## API Documentation

The API documents itself. With the backend running, FastAPI serves the generated contract, which is the authoritative description of every route, request body, response model and status code:

- `http://localhost:8000/docs` — interactive Swagger UI
- `http://localhost:8000/redoc` — ReDoc rendering of the same schema
- `http://localhost:8000/openapi.json` — the raw OpenAPI document

For the rating feature specifically — the data model, the complete per-endpoint status matrix, the ordered eligibility guards, the publication model, the moderation policy and the configuration — see [`docs/features/ratings.md`](./docs/features/ratings.md).

Note that the three older routers each repeat their resource segment inside the router as well as in the mount prefix, so their paths resolve as `/api/listings/listings`, `/api/transactions/transactions` and `/api/messages/messages`. The generated documents above show the paths as they really resolve; the rating router does not share that defect.

## Known Limitations

These are the boundaries of what currently works. Each is stated so nobody has to discover it by running into it.

- **The web client does not render.** `npm start` serves an HTTP 200 document, but the React root stays empty on every route, and `npm run build` fails at the `tsc` step. There are two independent, pre-existing causes on the entry path: `src/index.tsx` imports `@stripe/react-stripe-js` and `@stripe/stripe-js`, which `package.json` does not declare and `node_modules` does not contain; and `src/App.tsx` — the root component — imports through an `@/…` path prefix that neither `tsconfig.json` nor `vite.config.ts` declares, as do 15 other modules, among them the profile and transaction pages. Repairing either alone is not enough. The consequence for this documentation: every user-interface capability described above exists as reviewed source and is covered by component tests, but **no screen can be reached in a browser today**, so treat the API as the delivered surface.
- **No authentication endpoints.** As above: no registration, login, token issuance, password reset or email verification route exists. A token and a verified user document have to be arranged out of band, which also means account verification is a data operation rather than a product flow.
- **Deployment assets are incomplete.** `infrastructure/docker/docker-compose.yml` references Dockerfiles that do not exist and PostgreSQL-era variables that no longer match a Firestore-backed application; there is no `k8s/` directory; and `scripts/deploy.sh` runs under `set -e` while building image contexts (`./backend/api`, `./backend/auth`, `./backend/search`) and entering a `terraform` directory that do not exist at those paths. It therefore aborts before reaching its later steps, including the Firestore composite-index installation. The index *declarations* in `infrastructure/firestore.indexes.json` are correct and complete; installing them presently means running the equivalent `gcloud firestore indexes composite create` commands yourself.
- **A whole-project static check cannot be clean.** The 92 TypeScript errors across 22 files come from the defects named above — undeclared packages, absent modules, the unmapped `@/…` alias and React Router v5 API against v6 — and not one of them names a rating module. They are the baseline, recorded here so a regression can be told apart from the existing condition rather than mistaken for it. `npm run lint` is clean at `--max-warnings 0`.
- **No platform rate limiting, account lockout or audit logging** exists anywhere in the repository, and there are no Firestore security rules, so the datastore itself enforces nothing that this API is not asked to enforce. Anything holding credentials that reach Firestore directly is outside every control described here.
- **No background worker runs.** No task in the codebase is ever dispatched and no broker is provisioned, so scheduled work — including the rating window sweep — does not happen on its own.
- **Ratings do not influence search ranking.** The data that would enable it is delivered and readable; listing query semantics are unchanged.

## Contributing

There is no separate contributing guide; the checks a change has to satisfy are the ones the pipelines run, and they are the same commands listed under [Testing and Quality Checks](#testing-and-quality-checks).

- `.github/workflows/backend_ci.yml` installs `backend/requirements.txt`, then runs `flake8 .` and `pytest` from `backend/`.
- `.github/workflows/frontend_ci.yml` runs `npm ci`, `npm run lint`, `npm test` and `npm run build` from `frontend/`.

Before proposing a change: keep server-side validation authoritative rather than relying on the client, mirror any schema change on both sides (a Pydantic model in `backend/app/schema/` and its Zod counterpart in `frontend/src/schema/`), and add tests beside the existing ones for the behaviour you change. Please do not "fix" a documented limitation above by deleting the statement of it.

## License

**No `LICENSE` file is distributed with this repository, so no licence terms are established here.** An earlier revision of this file asserted the MIT License and linked to a file that does not exist; rather than substitute another claim that cannot be verified from the repository, the position is stated as it is. Confirm the licensing with the project owner before using, redistributing or contributing to this code.

## Support

If you encounter any issues or have questions, please open an issue on the project's issue tracker or contact the maintainers at support@usedcarmarketplace.com.
