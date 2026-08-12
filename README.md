# Used Car Marketplace

A comprehensive platform for buying and selling used cars, connecting sellers with potential buyers in a user-friendly and efficient manner.

> **New here?** Start with **[documentation/ONBOARDING.md](documentation/ONBOARDING.md)**. It covers domain context, common pitfalls, how to extend the project, and the running list of suggested next tasks. This README covers the clean-machine setup path and points you at everything else.

## Features

### Implemented today

- User registration, login, logout and profile retrieval, using JWT bearer authentication
- Vehicle listing creation, retrieval, update and deletion
- Search and filtering of listings by make, model, year and price range
- AI-assisted vehicle photo analysis and maintenance-document processing during listing creation
- Stripe-backed transaction creation and retrieval
- Role-based access control across the `buyer`, `seller` and `admin` roles

### Specified but not yet implemented

These are described in the requirements documents but are **not** served by any working endpoint today:

- Buyer-seller messaging — the endpoints are mounted and reachable, but not yet functional (see [Current status and known limitations](#current-status-and-known-limitations))
- User ratings and reviews
- Favourite listings and saved searches
- An administrator panel — the `admin` role is enforced in code, but no admin-only endpoint exists

## Technology Stack

| Area | Implementation |
| --- | --- |
| Backend | Python 3.9–3.12, FastAPI 0.95.2, Pydantic **v1** (1.10.26), Uvicorn 0.23.2 |
| Datastore | Google Cloud Firestore (native mode) |
| Object storage | Google Cloud Storage |
| AI and documents | Google Cloud Vision for vehicle-photo analysis; PyPDF2 3.0.1 with Pillow for maintenance documents |
| Payments | Stripe |
| Authentication | JWT signed with HS256 via python-jose 3.3.0; password hashing with passlib 1.7.4 and bcrypt 4.0.1 |
| Background work | Celery (declared in `backend/app/tasks/`; not yet runnable — see [Current status and known limitations](#current-status-and-known-limitations)) |
| Frontend | React 18 with TypeScript, Redux Toolkit, React Router and Axios, built with Vite |
| Infrastructure | Terraform for Google Cloud (GKE, Cloud Storage, Firestore, VPC) in `infrastructure/terraform/`, plus a Docker Compose stack in `infrastructure/docker/` |

Two corrections worth stating plainly, because earlier revisions of this file were wrong about both:

- The backend is **Python/FastAPI over Firestore**. Node.js, Express and MongoDB are **not used anywhere** in this repository.
- Styled Components is **not installed**. Tailwind CSS is declared in `frontend/package.json` but is not yet wired up — the repository contains no stylesheets.

**Do not change a pinned version** while working on the backend. The `passlib 1.7.4` with `bcrypt 4.0.1` pairing is especially deliberate: `passlib 1.7.4` cannot read the version metadata of `bcrypt 4.1` or newer, which breaks password hashing outright.

## Getting Started

### Prerequisites

- **Python 3.9 to 3.12.** 3.9 is the floor pinned by continuous integration in [`.github/workflows/backend_ci.yml`](.github/workflows/backend_ci.yml); 3.12 is the validated version, and the pinned dependency set below has not been verified above it. All backend code must stay source-compatible with 3.9. If your system `python3` is newer than 3.12, install an in-range interpreter and call it explicitly — for example `python3.12` — wherever `python3` appears below.
- **Git.**
- **A Google Cloud project** with Firestore (native mode), Cloud Storage and Cloud Vision enabled, plus Application Default Credentials available locally. Credentials are required for full runtime because the Firestore and Cloud Vision clients are constructed **at module import** — but they are *not* required to compile the code or to run lint checks.
- **A Stripe test API key.**
- **Node.js 18 or later, with npm** — only if you also want to run the React SPA in `frontend/`.
- **Docker and Terraform** — optional, and only for the deployment path under `infrastructure/`.

### Installation

Run every command below from the repository root.

**1. Clone the repository and enter it.**

```bash
git clone <your-remote-url> used-car-marketplace
cd used-car-marketplace
```

**2. Create an isolated Python environment and install the pinned dependency set.**

There is deliberately **no `requirements.txt`** in this repository, so the dependencies are installed explicitly. Adding a pinned manifest is a tracked next task in [documentation/ONBOARDING.md](documentation/ONBOARDING.md) — its absence is a known gap, not an oversight.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install \
  "fastapi==0.95.2" "pydantic==1.10.26" "uvicorn==0.23.2" \
  "python-jose==3.3.0" "passlib==1.7.4" "bcrypt==4.0.1" \
  "PyPDF2==3.0.1" python-multipart \
  google-cloud-firestore google-cloud-storage google-cloud-vision Pillow stripe
pip check   # expected: "No broken requirements found."
```

The repository carries no `.gitignore`, so keep your environment directory out of your commits — or create it outside the working tree.

**3. Export the environment variables.**

The backend's settings object declares eleven fields. The **eight below have no default**: if any one of them is missing, importing `app.core.config` fails immediately with a Pydantic `ValidationError`, and nothing starts.

```bash
export PROJECT_NAME="used-car-marketplace"
export API_V1_STR="/api"
export SECRET_KEY="$(openssl rand -hex 32)"
export ACCESS_TOKEN_EXPIRE_MINUTES="60"
export GOOGLE_CLOUD_PROJECT="your-gcp-project"
export GOOGLE_CLOUD_STORAGE_BUCKET="your-bucket"
export STRIPE_API_KEY="sk_test_xxx"
export STRIPE_WEBHOOK_SECRET="whsec_xxx"
```

The remaining three fields are optional and already carry working defaults:

| Variable | Default | Notes |
| --- | --- | --- |
| `ALLOWED_ORIGINS` | `["http://localhost:3000"]` | Origins the CORS middleware accepts. The default matches the frontend dev-server port used in `infrastructure/docker/docker-compose.yml`. |
| `ALGORITHM` | `HS256` | JWT signing algorithm. Change it only if you also change how tokens are validated. |
| `SENTRY_DSN` | unset | Optional error-reporting endpoint. |

`ALLOWED_ORIGINS` accepts **either** a comma-separated list **or** a JSON array, so both of these work:

```bash
export ALLOWED_ORIGINS="http://localhost:3000,http://localhost:5173"
export ALLOWED_ORIGINS='["http://localhost:3000","http://localhost:5173"]'
```

Settings are also read from a `.env` file in the working directory if one is present. No `.env.example` file exists in this repository and none is intended — the export block above is the reference configuration.

**4. Set `PYTHONPATH` and start the API.**

`PYTHONPATH` is mandatory, not a convenience: `backend/` contains no `__init__.py` files, so `app` resolves only as an implicit namespace package and the interpreter cannot find it otherwise. Skip this and you get `ModuleNotFoundError: No module named 'app'`.

```bash
export PYTHONPATH="$PWD/backend"
uvicorn app.main:app --reload --port 8000
```

If startup stops with `ImportError: cannot import name 'Stripe' from 'stripe'`, you have not misconfigured anything — that is known issue **HCF-3**, described under [Current status and known limitations](#current-status-and-known-limitations).

**5. Open the interactive API documentation.**

- Swagger UI: <http://localhost:8000/docs>
- OpenAPI schema: <http://localhost:8000/openapi.json>

**6. Optionally, run the React SPA.**

```bash
cd frontend
npm install
npm start        # runs Vite; there is no "npm run dev" script
```

The SPA is served at <http://localhost:3000> and reads its API base URL from `REACT_APP_API_BASE_URL`. Note three pre-existing inconsistencies in this area, all left as-is and tracked in [documentation/ONBOARDING.md](documentation/ONBOARDING.md): `frontend/package.json` is misnamed `task-management-frontend`, `frontend/public/index.html` is titled "Personal Finance Tracker", and `infrastructure/docker/docker-compose.yml` sets a differently named `REACT_APP_API_URL`.

There is no `package.json` at the repository root, so `npm install` only ever runs inside `frontend/`.

## Usage

1. Register an account through `POST /api/auth/register`, or log in with `POST /api/auth/login`
2. Browse listings, or create your own — creating a listing requires the `seller` role
3. Narrow results with the make, model, year and price filters on the listings endpoint
4. Complete a purchase through the transaction endpoint, which settles payment via Stripe
5. Contact sellers through the messaging endpoints *(reachable but not yet functional)*
6. Leave reviews and ratings for completed transactions *(not yet implemented)*

## API Documentation

The table below lists the routes **exactly as they are served**. All of them are also visible at `/docs` once the API is running.

| Method(s) | Path | Notes |
| --- | --- | --- |
| POST | `/api/auth/register` | Create an account; returns 201, or 409 if the email is already registered |
| POST | `/api/auth/login` | Exchange credentials for an access token |
| POST | `/api/auth/logout` | Acknowledges a sign-out; see the note on statelessness below |
| GET | `/api/auth/me` | The authenticated user's public profile |
| POST, GET | `/api/listings/listings` | Create a listing (`seller` role required); list listings with optional `make`, `model`, `year`, `min_price` and `max_price` filters |
| GET, PUT, DELETE | `/api/listings/listings/{listing_id}` | Read, update or delete one listing; update and delete require ownership, or the `admin` role |
| POST | `/api/transactions/transactions` | Create a transaction and settle payment |
| GET | `/api/transactions/transactions/{transaction_id}` | Read one transaction; restricted to its participants |
| POST, GET | `/api/messages/messages` | Send and list messages *(reachable but not yet functional)* |

That is 13 routes across 9 unique paths.

**The repeated path segments are real, not a typo.** Each router is mounted under `/api/<area>` while its own decorators repeat the segment, so the served path really is `/api/listings/listings`. This diverges from the single-segment `/api/listings` form described in [documentation/Technical Specifications.md](documentation/Technical%20Specifications.md) §5.3, and reconciling the two is tracked as known issue **HCF-6**. The paths are deliberately left untouched here so that no client contract changes silently. The four authentication paths match the specification exactly.

### Authentication contract

- Authenticate every protected request with an `Authorization: Bearer <token>` header.
- `POST /api/auth/login` takes a JSON body — `{"email": "...", "password": "..."}` — and returns `access_token`, `token_type`, `token` and `user`. `access_token` and `token` carry the same value: the first follows the OAuth2 convention, the second is what the SPA reads. `POST /api/auth/register` returns the same four keys.
- Bad credentials return 401 with a `WWW-Authenticate: Bearer` header, and an unknown email is answered identically to a wrong password so that responses cannot be used to enumerate accounts.
- `GET /api/auth/me` returns exactly seven public user fields — `id`, `email`, `first_name`, `last_name`, `role`, `created_at` and `updated_at`. A password hash is never returned by any endpoint.
- Tokens are signed with HS256 and carry only the `sub` and `exp` claims. Their lifetime comes from `ACCESS_TOKEN_EXPIRE_MINUTES`.
- **Logout is stateless.** It answers 200 so a client can clear its stored token, but it does **not** revoke the JWT, which stays valid until `exp`. There is no denylist or server-side session in this codebase.

Because login consumes a JSON body rather than form data, the Swagger UI "Authorize" control cannot complete the password flow. Obtain a token by calling `POST /api/auth/login` directly, then send it as a bearer header.

For the full API design, data models and architecture, see [documentation/Technical Specifications.md](documentation/Technical%20Specifications.md). For guidance on adding an endpoint of your own, see [documentation/ONBOARDING.md](documentation/ONBOARDING.md).

## Current status and known limitations

The backend imports cleanly and all four routers mount, and authentication is wired end to end. A few known gaps remain, deliberately unfixed and tracked rather than hidden:

- **Stripe SDK mismatch (HCF-3).** `backend/app/services/payment.py` does `from stripe import Stripe`, a symbol current Stripe SDKs do not expose — they provide `StripeClient` instead. Because that module sits on the import path of the transactions router, this aborts startup with `ImportError: cannot import name 'Stripe' from 'stripe'`. It is the one gap that stands between a fresh clone and a served API, and it is the highest-value next task in the register.
- **Cloud Vision SDK mismatch (HCF-4).** `backend/app/services/ai_vision.py` calls a client method the installed Cloud Vision SDK does not provide, so real photo analysis fails. Listing creation tolerates this: each photo is analysed inside a guarded block, so a failure is logged with a correlation id and degrades the analysis instead of failing the request.
- **Messaging is reachable but not functional (HCF-8).** Both `/api/messages/messages` endpoints are mounted and answer, but their handlers still need work.
- **Background workers are not runnable.** The Celery task module reads a `CELERY_BROKER_URL` setting that the settings object does not declare.
- **Continuous integration is red.** [`.github/workflows/backend_ci.yml`](.github/workflows/backend_ci.yml) installs from a `requirements.txt` that does not exist and runs a test suite under `backend/tests/` that cannot be collected. Both are tracked next tasks.

The complete register of known issues and suggested next tasks lives in [documentation/ONBOARDING.md](documentation/ONBOARDING.md).

## Documentation

| Document | What it covers |
| --- | --- |
| [documentation/ONBOARDING.md](documentation/ONBOARDING.md) | Setup, domain context, common pitfalls, how to extend the project, and suggested next tasks |
| [documentation/Technical Specifications.md](documentation/Technical%20Specifications.md) | System architecture, data models, API design and interfaces |
| [documentation/Software Requirements Specifications (SRS).md](documentation/Software%20Requirements%20Specifications%20%28SRS%29.md) | Functional and non-functional requirements, users and personas |
| [documentation/Software Project Proposal.md](documentation/Software%20Project%20Proposal.md) | Business case, objectives and scope boundary |
| [blitzy/documentation/Project Guide.md](blitzy/documentation/Project%20Guide.md) | A point-in-time assessment of the codebase; historical reference |

## Contributing

Contributions are welcome. Start with [documentation/ONBOARDING.md](documentation/ONBOARDING.md) for the development workflow, the pitfalls worth knowing before your first change, how to extend the project, and the list of suggested next tasks — it is the best place to find something useful to pick up. For requirements context, see [documentation/Software Requirements Specifications (SRS).md](documentation/Software%20Requirements%20Specifications%20%28SRS%29.md).

This repository has no `CONTRIBUTING.md` file yet; writing one is a tracked next task.

## License

No license file is currently present in this repository, so the terms of use are not yet defined. Adding one is a tracked next task in [documentation/ONBOARDING.md](documentation/ONBOARDING.md). Please raise the question with the project owners before redistributing this code.

## Support

If you encounter any issues or have questions, please open an issue on our GitHub repository or contact our support team at support@usedcarmarketplace.com.
