# Used Car Marketplace

A comprehensive platform for buying and selling used cars, connecting sellers with potential buyers in a user-friendly and efficient manner.

> **New here?** Start with **[documentation/ONBOARDING.md](documentation/ONBOARDING.md)**. It covers domain context, common pitfalls, how to extend the project, and the running list of suggested next tasks. This README covers the clean-machine setup path and points you at everything else.

## Features

### Implemented today

**A clean install does not serve any of this yet.** The application cannot be imported until known issue **HCF-3** is repaired — see [Current status and known limitations](#current-status-and-known-limitations). What follows is what the code implements, verified by compiling and linting it and by driving the handlers in process with that one import stubbed:

- User registration, login, logout and profile retrieval, using JWT bearer authentication
- Vehicle listing creation, retrieval, update and deletion
- Search and filtering of listings by make, model, year and price range
- Maintenance-document processing during listing creation, and AI-assisted photo analysis — the photo call site is correct and guarded, but real Cloud Vision calls still fail (**HCF-4**), so the analysis degrades to nothing rather than producing labels
- Stripe-backed transaction creation and retrieval — the vehicle must be `available`, the card is charged synchronously, and the vehicle is marked `sold` once the charge succeeds. The call site is correct, though the Stripe module itself does not import (**HCF-3**) and nothing creates the `vehicles` document the availability check reads (**HCF-7**)
- Role-based access control across the `buyer`, `seller` and `admin` roles, enforced inline in each handler. Read [Current status and known limitations](#current-status-and-known-limitations) before you treat any of it as hardened: the role is taken from the registration request body, and the purchase amount and seller from the transaction request body

### Specified but not yet implemented

These are described in the requirements documents but are **not** served by any working endpoint today:

- Buyer-seller messaging — the endpoints are mounted and reachable, but not yet functional (see [Current status and known limitations](#current-status-and-known-limitations))
- User ratings and reviews
- Favourite listings and saved searches
- An administrator panel — the `admin` role is enforced in code, but no endpoint is admin-only and no workflow grants the role deliberately. What exists instead is a gap: registration stores whatever `role` string it is sent, verbatim and unchecked, so **an anonymous caller can register itself as `admin`** and take the one privilege the role carries — deleting any seller's listing. Closing that, and adding a real elevation path, are tracked as tasks **NT-24** and **NT-14** in [ONBOARDING §5](documentation/ONBOARDING.md#5-suggested-next-tasks); [§2.2](documentation/ONBOARDING.md#22-roles-and-who-may-do-what) sets out how to contain it in the meantime

## Technology Stack

| Area | Implementation |
| --- | --- |
| Backend | Python 3.9–3.12, FastAPI 0.95.2, Pydantic **v1** (1.10.26), Uvicorn 0.23.2 |
| Datastore | Google Cloud Firestore (native mode) |
| Object storage | Google Cloud Storage |
| AI and documents | Google Cloud Vision for vehicle-photo analysis, with Pillow decoding the image bytes; PyPDF2 3.0.1 for maintenance documents |
| Payments | Stripe |
| Authentication | JWT signed with HS256 via python-jose 3.3.0; password hashing with passlib 1.7.4 and bcrypt 4.0.1 |
| Background work | Celery (declared in `backend/app/tasks/`; not yet runnable — see [Current status and known limitations](#current-status-and-known-limitations)) |
| Frontend | React 18 with TypeScript, Redux Toolkit, React Router and Axios, built with Vite |
| Infrastructure | Terraform for Google Cloud (GKE, Cloud Storage, Firestore, VPC) in `infrastructure/terraform/`, plus a Docker Compose stack in `infrastructure/docker/` |

Two corrections worth stating plainly, because earlier revisions of this file were wrong about both:

- The **backend** is **Python/FastAPI over Firestore**. Express and MongoDB appear nowhere in this repository, and no Node.js process serves the API. Node.js *is* still part of the toolchain — it builds and serves the React SPA through Vite, which is why it appears in the prerequisites below.
- Styled Components is **not installed**. Tailwind CSS is declared in `frontend/package.json` but is not yet wired up — the repository contains no stylesheets.

**Do not change a pinned version** while working on the backend. Two of the pins are load-bearing in ways that are not obvious — Pydantic must stay on v1, and `passlib 1.7.4` must be paired with `bcrypt` below 4.1 or password hashing breaks outright. Both are explained in [ONBOARDING §3.2](documentation/ONBOARDING.md#32-pydantic-v1-is-mandatory) and [§3.3](documentation/ONBOARDING.md#33-passlib-174-needs-bcrypt-below-41).

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

This repository publishes no canonical remote, so `<REPO_URL>` below is **a required substitution, not a value** — replace it with the remote you were given before you run the block. The only other values you must supply yourself are your own cloud project, bucket and Stripe test credentials in step 3; every command apart from those runs exactly as written.

```bash
git clone <REPO_URL> used-car-marketplace   # substitute <REPO_URL> first
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

**The repository carries no `.gitignore`, so nothing is kept out of your commits automatically.** Keep your environment directory out of every `git add` — or create it outside the working tree — and **never commit a file containing secrets**: no `.env`, no service-account JSON, no exported key. `SECRET_KEY` signs every access token and `STRIPE_API_KEY` moves money, so either one in history means rotating it, not deleting it. For local ignore patterns without adding a tracked `.gitignore`, use `.git/info/exclude`; for anything past local development, load secrets from a secret manager at deploy time. [documentation/ONBOARDING.md](documentation/ONBOARDING.md) §1.3 has the full practice.

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

# Optional — this is the value the field already defaults to. Set it explicitly
# so the origin your browser uses is visible in your shell rather than implied.
export ALLOWED_ORIGINS="http://localhost:3000"
```

The remaining three fields are optional and already carry working defaults:

| Variable | Default | Notes |
| --- | --- | --- |
| `ALLOWED_ORIGINS` | `["http://localhost:3000"]` | Origins the CORS middleware accepts. The default matches the frontend dev-server port used in `infrastructure/docker/docker-compose.yml`. |
| `ALGORITHM` | `HS256` | JWT signing algorithm. Change it only if you also change how tokens are validated. |
| `SENTRY_DSN` | unset | Optional error-reporting endpoint. |

`ALLOWED_ORIGINS` accepts **either** a comma-separated list **or** a JSON array — `"http://localhost:3000,http://localhost:5173"` and `'["http://localhost:3000","http://localhost:5173"]'` both work. Set exact origins; `*` is a local diagnostic only, never a deployed value, for the reason given in [ONBOARDING §3.4](documentation/ONBOARDING.md#34-allowed_origins-takes-two-forms-and-neither-may-crash).

```bash
export ALLOWED_ORIGINS="http://localhost:3000,http://localhost:5173"
export ALLOWED_ORIGINS='["http://localhost:3000","http://localhost:5173"]'
```

**Export the variables; do not create a `.env` file.** The settings class names `env_file = ".env"`, but Pydantic v1 can only read that file through its optional `python-dotenv` integration, which is **not** part of this project's dependency set. So with the install above, a `.env` file sitting in the working directory does not add configuration — it stops the process before it starts:

```text
ImportError: python-dotenv is not installed, run `pip install pydantic[dotenv]`
```

If you want `.env` support, install that extra explicitly (`pip install "pydantic[dotenv]"`) and keep the pinned `pydantic==1.10.26`. Otherwise the export block above is the reference configuration. There is no `.env.example` in this repository and this change added none; whether to add one, and whether to authorise the dependency that would make it work, is tracked in [documentation/ONBOARDING.md](documentation/ONBOARDING.md).

**4. Set `PYTHONPATH` and start the API.**

`PYTHONPATH` is mandatory, not a convenience: the source root is `backend/`, while every import in the codebase is written as top-level `app.…`, so Python finds `app` only when `backend/` — the directory containing it — is on `sys.path`. Running from the repository root puts the repository root there instead. Skip this and you get `ModuleNotFoundError: No module named 'app'`.

```bash
export PYTHONPATH="$PWD/backend"
uvicorn app.main:app --reload --port 8000
```

**On a clean install this command stops with `ImportError: cannot import name 'Stripe' from 'stripe'`, and that is expected** — you have not misconfigured anything. It is known issue **HCF-3**, described under [Current status and known limitations](#current-status-and-known-limitations), and until it is repaired the repository has no served HTTP surface. Verify your work with the compile and lint gates and by driving handlers in process instead — [ONBOARDING §1.9](documentation/ONBOARDING.md#19-verify-your-checkout) has both and neither needs a server, and [§1.10](documentation/ONBOARDING.md#110-the-full-acceptance-protocol) is the full acceptance protocol, every assertion of which is reproducible without one.

Application log lines go to standard error, at `INFO` and above, in the form `<timestamp> <LEVEL> <logger>: <message> [key=value ...]`. `app/main.py` configures the `app` logger namespace itself, because Uvicorn's logging configuration covers only its own `uvicorn*` loggers and would otherwise leave every `app.*` record discarded at `WARNING` with no handler. The bracketed pairs are whatever a handler attached through `extra` — the `correlation_id` on listing-creation records, for example — with control characters escaped so a value cannot forge a line. To route the records yourself, configure the `app` logger before importing `app.main`; the application then leaves your configuration alone.

**5. Open the interactive API documentation** — available once HCF-3 is repaired and the command above stays up.

- Swagger UI: <http://localhost:8000/docs>
- OpenAPI schema: <http://localhost:8000/openapi.json>

**6. Optionally, run the React SPA.**

```bash
cd frontend
npm install
npm start -- --port 3000    # runs Vite; there is no "npm run dev" script
```

**Pass the port explicitly.** The repository ships no `vite.config.ts`, so `npm start` on its own serves on Vite's default port **5173**, which is not in the default `ALLOWED_ORIGINS` — every request the browser makes would then fail the CORS check while the same call succeeds from `curl`. Serving on 3000, as above, matches the default. If you would rather use 5173, add it to `ALLOWED_ORIGINS` instead.

The dev server starts on <http://localhost:3000> and reads its API base URL from `REACT_APP_API_BASE_URL`, but **the application does not render** — that, and several other pre-existing frontend inconsistencies (a misnamed package, a wrong page title, a differently named API variable in the compose file), are catalogued as task NT-15 in [documentation/ONBOARDING.md](documentation/ONBOARDING.md#52-work-worth-picking-up). None of them affects backend work.

There is no `package.json` at the repository root, so `npm install` only ever runs inside `frontend/`.

## Usage

Once the API is served — which today means after known issue **HCF-3** is repaired:

1. Register an account through `POST /api/auth/register`, naming the `buyer` or `seller` role — nothing rejects any other value today, which is the gap described above — or log in with `POST /api/auth/login`
2. Browse listings, or create your own — creating a listing requires the `seller` role
3. Narrow results with the make, model, year and price filters on the listings endpoint
4. Complete a purchase through the transaction endpoint, which settles payment via Stripe. The vehicle must exist in the `vehicles` collection with `status: 'available'`; the charge uses the request's `stripe_payment_intent_id` as the token, its `amount`, and the currency `usd` *(the call site is correct, but no module creates the `vehicles` document its availability check reads — **HCF-7**)*
5. Contact sellers through the messaging endpoints *(reachable but not yet functional)*
6. Leave reviews and ratings for completed transactions *(not yet implemented)*

## API Documentation

**13 routes across 9 unique paths**, listed here exactly as they are served — and read from the router prefixes and decorators on disk, not from a running server.

| Method(s) | Path | Access |
| --- | --- | --- |
| POST | `/api/auth/register` | Public. 201, or 409 on a duplicate email. The `role` you send is stored verbatim, `admin` included — **NT-24** |
| POST | `/api/auth/login` | Public. JSON body; exchanges credentials for an access token, 401 on bad credentials |
| POST | `/api/auth/logout` | Authenticated. Acknowledgement only — see the note on statelessness below |
| GET | `/api/auth/me` | Authenticated. Seven public profile fields |
| POST, GET | `/api/listings/listings` | Create requires the `seller` role; list is open and filterable by `make`, `model`, `year`, `min_price`, `max_price`. Creation answers 422 for more than 20 maintenance records, more than 900 KiB of maintenance content, or a serialized listing over 1 MB |
| GET, PUT, DELETE | `/api/listings/listings/{listing_id}` | Read is open; **update requires ownership**, with no administrator override; **delete permits the owner or an `admin`** |
| POST | `/api/transactions/transactions` | Authenticated. 403 unless the caller is the request’s `buyer_id`; 400 if the vehicle is absent or not `available`, or if the charge fails |
| GET | `/api/transactions/transactions/{transaction_id}` | Authenticated; participants only |
| POST, GET | `/api/messages/messages` | Authenticated *(reachable but not yet functional)* |

That is 13 routes registered across 9 unique paths, of which 11 work — the two messaging routes answer but are not yet functional, and none of them can be reached from a served port until the Stripe import below is fixed.

**The repeated path segments are real, not a typo** — each router's prefix and its decorators both name the area, so the served path is `/api/listings/listings`. That diverges from the single-segment form in the specification and the SPA, so it is frozen rather than tidied, and tracked as HCF-6 in [ONBOARDING §3.9](documentation/ONBOARDING.md#39-the-doubled-path-segments-are-deliberate). The four authentication paths match the specification exactly.

Four things about this API surprise people. Each is documented in full where it belongs, rather than twice:

| What | In short | Detail |
| --- | --- | --- |
| Authentication | `Authorization: Bearer <token>`. Login takes a **JSON** body and returns `access_token`, `token_type`, `token` and `user`; register returns the same four keys. Tokens are HS256 with only `sub` and `exp`, and live for `ACCESS_TOKEN_EXPIRE_MINUTES` | [ONBOARDING §2.5](documentation/ONBOARDING.md#25-authentication-and-tokens), [§2.6](documentation/ONBOARDING.md#26-the-spa-contract--treat-it-as-frozen) |
| Logout does not revoke | It answers 200 so a client can clear its stored token; the JWT stays valid until `exp`. There is no denylist or server-side session anywhere in this codebase | [ONBOARDING §2.5.1](documentation/ONBOARDING.md#251-logout-is-stateless) |
| Only some writes are bounded | Maintenance record count, aggregate maintenance content size and serialized listing size are each rejected with a 422 **before** the Firestore write. The photo list, the message body and every registration field carry **no** bound at all — tasks **NT-24** and **NT-25** | [ONBOARDING §2.8](documentation/ONBOARDING.md#28-what-the-write-endpoints-bound) |
| Blocking I/O runs on the event loop | Cloud Vision, PyPDF2, Stripe and Firestore are all synchronous clients, yet every route handler except the four authentication ones is `async def` and calls them directly — so one slow call delays every other in-flight request. The authentication handlers are plain `def` and go to Starlette's threadpool, which is the pattern the rest should follow: task **NT-22** | [ONBOARDING §4.1](documentation/ONBOARDING.md#41-add-a-router) |

Two consequences you will meet in practice: because login consumes JSON rather than form data, the Swagger UI "Authorize" control cannot complete the password flow — get a token from `POST /api/auth/login` and send it as a bearer header. And a rejected credential is always 401 with an identical body for an unknown email and a wrong password, while a body that is not *shaped* like a login attempt is a 422 — the two are told apart in [ONBOARDING §2.2.1](documentation/ONBOARDING.md#221-what-registration-accepts).

For the full API design, data models and architecture, see [documentation/Technical Specifications.md](documentation/Technical%20Specifications.md). For guidance on adding an endpoint of your own, see [documentation/ONBOARDING.md](documentation/ONBOARDING.md).

## Current status and known limitations

**What is verified, and how.** The import chain that composes the HTTP application — `app.main` and the four routers it mounts — is closed: no first-party name on it is missing, misnamed or used without being imported. With the one third-party blocker below stood in for, `app.main` imports and composes **13 routes across 9 unique paths**, of which 11 answer as documented. **That closure covers the HTTP chain only.** One first-party module outside it still cannot be imported at all: `backend/app/tasks/background_jobs.py`, which uses `List` without importing it, reads an undeclared `CELERY_BROKER_URL` setting and imports a `process_refund` that the payment service does not define — see *Background workers are not runnable* below.

Authentication, listing creation and the purchase path were exercised **in-process, against an in-memory Firestore double, with the Stripe and Cloud Vision constructors stood in for and the vision and payment helpers replaced by recording spies** — no cloud project, no Firestore emulator, no network and no served port were involved. The procedure is published in full, and is runnable in about four seconds, as [section 6 of documentation/ONBOARDING.md](documentation/ONBOARDING.md#6-verifying-behaviour-in-process): 85 assertions covering the route table and the OpenAPI `tokenUrl`, all ten authentication cases including the exact JWT claims and lifetime, the vision call count and argument types, the exact payment arguments with the no-write behaviour on a declined or malformed result, both CORS preflight outcomes, seven `ALLOWED_ORIGINS` forms in isolated subprocesses, and the logging subsystem. What it deliberately does **not** cover is listed there too — most importantly that no real Stripe or Cloud Vision traffic is exercised, and that an in-memory double cannot reproduce the concurrency and uniqueness gaps described below.

**A clean checkout is not runnable yet:** `uvicorn app.main:app` still stops at `backend/app/services/payment.py` line 1, which is outside the change set that repaired the rest, so the composition above is verified statically and in-process rather than against a served port. Read the route count as routes *registered*, never as endpoints *working* — two of them do not work.

**One project gate does not pass.** `python -m flake8 --select=F401,F811,F821,F841 backend/app/` is this project's own static acceptance gate, and its criterion is one finding; the delivered tree reports ten. All ten are pre-existing findings in files this change set was not authorised to modify, two of them the undefined names that keep the Celery module un-importable. They are enumerated, with the reason the gate is reported as blocked rather than redefined, in [section 1.9.2 of documentation/ONBOARDING.md](documentation/ONBOARDING.md#192-gate-2--undefined-and-unused-names-across-the-whole-package).

The known gaps, deliberately unfixed and tracked rather than hidden:

- **Stripe SDK mismatch (HCF-3).** `backend/app/services/payment.py` does `from stripe import Stripe`, a symbol current Stripe SDKs do not expose — they provide `StripeClient` instead. Because that module sits on the import path of the transactions router, this aborts startup with `ImportError: cannot import name 'Stripe' from 'stripe'`. It is the one gap that stands between a fresh clone and a served API, and it is the highest-value next task in the register. Until it is resolved, verify backend work with the compile and lint gates and by driving the app in-process, not by starting a server.
- **Cloud Vision SDK mismatch (HCF-4).** `backend/app/services/ai_vision.py` calls a client method the installed Cloud Vision SDK does not provide, so real photo analysis fails. Listing creation tolerates this: each photo is analysed inside a guarded block, so a failure is logged as `photo analysis failed` with the request's `correlation_id` rendered in brackets, and degrades the analysis instead of failing the request.
- **Messaging is reachable but not functional (HCF-8).** Both `/api/messages/messages` endpoints are mounted and answer, but their handlers still need work.
- **The authentication and purchase paths are correct, not hardened.** They do what this repair set was authorized to make them do — no more. Specifically: registration accepts `role` from the request body, so a caller can claim `admin`, which is the one role permitted to delete another seller's listing; register and login accept unconstrained strings, so there is no email-format, name or password policy and no guard against a password bcrypt would silently truncate at 72 bytes; email uniqueness rests on a query run before the write, so two simultaneous registrations for one address can both succeed and `authenticate_user`'s `limit(1)` then resolves sign-in to whichever comes back first; neither public route is throttled, and every attempt costs a bcrypt verification; the photo list is unbounded in count and size, and each entry costs one call into the vision provider on the event loop; and the purchase amount and `seller_id` are taken from the request rather than from the vehicle record, while the availability read, the charge, the transaction write and the `sold` update are four independent, non-idempotent steps. Each of these is written up with its options in [documentation/ONBOARDING.md](documentation/ONBOARDING.md) — do not treat this API as internet-facing until they are settled.
- **Background workers are not runnable, because their module cannot be imported.** `backend/app/tasks/background_jobs.py` has four independent defects: it annotates with `List[str]` without importing `typing`, it reads a `CELERY_BROKER_URL` setting that the settings object does not declare, it imports a `process_refund` that `backend/app/services/payment.py` does not define (that module defines `create_refund`), and it passes a URL string to the `bytes` parameter of `analyze_vehicle_photo`. This is the one first-party import gap the repair set left standing — it sits outside the HTTP composition chain and outside the authorised change set — and it is tracked as **NT-6**.
- **Continuous integration is red.** [`.github/workflows/backend_ci.yml`](.github/workflows/backend_ci.yml) installs from a `requirements.txt` that does not exist and runs a test suite under `backend/tests/` that cannot be collected. Both are tracked next tasks.

Every one of these — with the position taken, the reason, and what a fix would involve — is in the register at [ONBOARDING §5](documentation/ONBOARDING.md#5-suggested-next-tasks), which is also the best place to find something worth picking up.

## Documentation

| Document | What it covers |
| --- | --- |
| [documentation/ONBOARDING.md](documentation/ONBOARDING.md) | Setup, domain context, common pitfalls, how to extend the project, and suggested next tasks |
| [documentation/Technical Specifications.md](documentation/Technical%20Specifications.md) | System architecture, data models, API design and interfaces |
| [documentation/Software Requirements Specifications (SRS).md](documentation/Software%20Requirements%20Specifications%20%28SRS%29.md) | Functional and non-functional requirements, users and personas |
| [documentation/Software Project Proposal.md](documentation/Software%20Project%20Proposal.md) | Business case, objectives and scope boundary |
| [blitzy/documentation/Project Guide.md](blitzy/documentation/Project%20Guide.md) | A point-in-time assessment of the codebase; historical reference |

## Contributing

Contributions are welcome, and there is no `CONTRIBUTING.md` yet — writing one is itself a tracked task. Until then, [documentation/ONBOARDING.md](documentation/ONBOARDING.md) is the contributor guide: run the two gates in [§1.9](documentation/ONBOARDING.md#19-verify-your-checkout) before every commit and the full acceptance protocol in [§1.10](documentation/ONBOARDING.md#110-the-full-acceptance-protocol) before you call a change done, follow the patterns in its [§4](documentation/ONBOARDING.md#4-how-to-extend) rather than habits from other FastAPI projects, and pick your first change from its [§5](documentation/ONBOARDING.md#5-suggested-next-tasks).

## License

No license file is currently present in this repository, so the terms of use are not yet defined. Adding one is a tracked next task in [documentation/ONBOARDING.md](documentation/ONBOARDING.md). Please raise the question with the project owners before redistributing this code.

## Getting help

**This repository declares no support channel** — it publishes no canonical remote, no issue tracker and no contact address, so any that were named here would be guesswork. Earlier revisions of this file named a support mailbox that has no basis anywhere in the repository; it has been removed rather than left to send people nowhere. Until the project owners publish one, use what is actually here:

1. **Check whether it is already known.** [documentation/ONBOARDING.md §3.1](documentation/ONBOARDING.md#31-symptom-lookup) maps the failures you are most likely to hit — including the two that look like your mistake and are not — to the section that explains each one.
2. **Check the register of known issues and next tasks** in [documentation/ONBOARDING.md §5](documentation/ONBOARDING.md#5-suggested-next-tasks). Every gap this project knows about is listed there with the position taken and why.
3. **Ask whoever gave you this repository.** They hold the clone URL, the cloud project and the Stripe account, none of which this file can tell you.

If you are setting the project up for a team, publishing an issue tracker and a contact address — and replacing this section with them — is worth doing early.
