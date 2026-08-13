# Used Car Marketplace

A platform for buying and selling used cars: a **Python/FastAPI** backend on **Google Cloud Firestore**, with **Cloud Vision** analysing vehicle photographs, **PyPDF2** reading maintenance documents, **Stripe** settling payments, and a **React/TypeScript** single-page application consuming the API over JSON.

> **This README is the front door: what the system is, how to get it running, and where everything else lives.** The manual is **[documentation/ONBOARDING.md](documentation/ONBOARDING.md)** — setup in depth, domain context, the traps worth knowing before you hit them, how to extend the codebase, and the register of known gaps and next tasks. Where the two overlap, the guide is authoritative and this file links to it rather than repeating it.

## Status at a glance

| | Where it stands |
| --- | --- |
| **Backend** | Composes **13 routes across 9 paths**; **11 of them work**. Verified by compiling and linting the tree, by driving it in process against an in-memory datastore, and over HTTP against a local profile |
| **Can I run it?** | **Not from an unmodified checkout** — `uvicorn app.main:app` stops on a third-party import in a frozen module (known issue **HCF-3**). The four-step [local development profile](documentation/ONBOARDING.md#171-the-local-development-profile) does serve all 13 routes locally, and needs no cloud or Stripe account |
| **Frontend** | Does not render and could not reach this API if it did — pre-existing, all inside `frontend/`, tracked as **NT-15** and **NT-30** to **NT-34** |
| **Background workers** | The Celery module cannot be imported at all, so no worker has ever run (**NT-6**) |
| **Tests and CI** | `backend/tests/` does not collect and continuous integration is red, both for pre-existing reasons. Verification is the gates in [ONBOARDING §1.9](documentation/ONBOARDING.md#19-verify-your-checkout) plus the 139-assertion harness in [§6](documentation/ONBOARDING.md#6-verifying-behaviour-in-process) |
| **Hardening** | **Begun at the front door, unfinished behind it — the row to read twice.** Registration now validates every field, so `admin` cannot be self-granted; both public routes count attempts and answer 429; a failed sign-in is timing-uniform; and every response carries the security headers this application can set correctly, with `no-store` on the token-bearing ones. Still open: the attempt counters live in one process, no *write* body is bounded, a purchase can be charged twice and two buyers can both pay for one car, and no setting is checked beyond its type. Every one of them is written down with what a fix involves — read [§5.3](documentation/ONBOARDING.md#53-where-to-start) before exposing this to anyone |

## Features

**Implemented today** — what the code does, with the caveat that an unmodified checkout serves none of it until you take step 5 below:

- User registration, login, logout and profile retrieval, using JWT bearer authentication
- Vehicle listing creation, retrieval, update and deletion, with search and filtering by make, model, year and price range
- Maintenance-document processing during listing creation, and AI-assisted photo analysis — the photo call site is correct and guarded, though real Cloud Vision calls still fail (**HCF-4**), so the analysis degrades to nothing rather than failing the request
- Stripe-backed transaction creation and retrieval: the caller must be the named buyer, the vehicle must be `available`, and the vehicle is marked `sold` once the charge succeeds. Read the caveats before trusting it with money — the amount and the seller come from the request and are checked against nothing (**NT-41**), a replayed payment intent is charged again (**NT-27**), and nothing in the codebase creates the `vehicles` document the availability check reads (**HCF-7**)
- Role-based access control across `buyer`, `seller` and `admin`, enforced inline in each handler. **Registration grants only `buyer` or `seller`** — the role is trimmed, lower-cased and checked against that allow-list, so the anonymous self-promotion to `admin` that `delete_listing` used to trust is refused with a 422 and logged. What is still missing is the opposite half: no endpoint grants `admin` deliberately either, so an administrator is a direct datastore write with the controls [ONBOARDING §2.2](documentation/ONBOARDING.md#22-roles-and-who-may-do-what) sets out, until **NT-14** lands
- Bounded, shape-checked registration input: a canonicalised 254-character address with a format check, non-blank names capped and refused if they carry markup, and a password band of 8 to 72 UTF-8 bytes — the ceiling being bcrypt's own, so a long password is refused rather than silently truncated ([ONBOARDING §2.2.1](documentation/ONBOARDING.md#221-what-registration-accepts))
- A duplicate-email check on registration and one uniform 401 — same body, same header, **same response time** — for every failed sign-in, including a blank credential and an over-long password. Two narrower truths are documented rather than glossed: the duplicate check is a query followed by a write, so two simultaneous registrations for one address can both succeed (**NT-40**), and the attempt ceiling that fronts both routes is per process, so several workers multiply it (**NT-26**)

**Specified but not yet implemented** — described in the requirements documents, served by no working endpoint:

- Buyer-seller messaging — the two endpoints are mounted and reachable, but their handlers still fail on their own logic (**HCF-8**)
- User ratings and reviews; favourite listings and saved searches
- An administrator panel. No endpoint is admin-only and no workflow grants the role deliberately — today it is self-granted at registration, which is the defect **NT-24** closes, and the audited grant path that has to replace it is task **NT-14**

## Technology stack

| Area | Implementation |
| --- | --- |
| Backend | Python 3.10–3.12 to install (**3.12 validated**; source stays 3.9-compatible — see the prerequisites), FastAPI 0.95.2, Pydantic **v1** (1.10.26), Uvicorn 0.23.2 |
| Datastore | Google Cloud Firestore (native mode). No SQL, no ORM — the `postgres` service in `infrastructure/docker/` is vestigial |
| Object storage | Google Cloud Storage — present in code, imported by nothing (**HCF-5**) |
| AI and documents | Cloud Vision for photo analysis with Pillow decoding the bytes; PyPDF2 3.0.1 for maintenance documents |
| Payments | Stripe |
| Authentication | HS256 JWT via python-jose 3.3.0 — **a pinned version with published advisories** (CVE-2024-33663, CVE-2024-33664, addressed from 3.4.0), see [Known limitations](#known-limitations); password hashing with passlib 1.7.4 and bcrypt 4.0.1 |
| Background work | Celery, declared in `backend/app/tasks/` and not runnable (**NT-6**) |
| Frontend | React 18 with TypeScript, Redux Toolkit, React Router and Axios, built with Vite. Tailwind is declared but unwired — the repository contains no stylesheets |
| Infrastructure | Terraform for Google Cloud in `infrastructure/terraform/`, plus a Docker Compose stack in `infrastructure/docker/` that cannot build today (**NT-15**) — and must not be made runnable as written, because it hardcodes a database password and a placeholder `JWT_SECRET`, a *known* signing key rather than a weak one |

**Do not change a pinned version.** Two pins are load-bearing in ways that are not obvious: Pydantic must stay on v1, and `passlib 1.7.4` must be paired with `bcrypt` below 4.1 or password hashing breaks. Both are explained in [ONBOARDING §3.2](documentation/ONBOARDING.md#32-pydantic-v1-is-mandatory) and [§3.3](documentation/ONBOARDING.md#33-passlib-174-needs-bcrypt-below-41).

## Getting started

**Prerequisites.** Linux or macOS with **bash** or **zsh** (on Windows, use WSL 2 — every command here is POSIX shell); **Python 3.10–3.12 to install — use 3.12, the validated version.** Two floors are at play and conflating them wastes an afternoon: *source* stays compatible with **3.9**, the version continuous integration pins, while *installation* needs **3.10 or newer**, because five of the cloud and media packages declare `Requires-Python >= 3.10` and a 3.9 environment therefore resolves a different dependency set than the documented one (**NT-16**). Also `git`, `curl` and `openssl`; **Docker or the Google Cloud CLI** if you want the local profile's Firestore emulator; Node.js 18+ only for the SPA. Google Cloud credentials are needed **only** for real cloud traffic — not to compile, lint, verify, or run the local profile. [ONBOARDING §1.2](documentation/ONBOARDING.md#12-prerequisites) is the full list and [§1.2.1](documentation/ONBOARDING.md#121-google-credentials-what-needs-them-and-how-to-establish-them) establishes credentials when you do need them.

Run everything below from the repository root.

**1. Clone the repository.** This repository publishes no canonical remote, so `<REPO_URL>` is a substitution you must supply, not a value:

```bash
git clone <REPO_URL> used-car-marketplace
cd used-car-marketplace
```

**2. Create a virtual environment and install the dependencies.** There is deliberately **no `requirements.txt`** — recreating one needs a decision, not an initiative (**NT-1**), so the set is installed explicitly:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install \
  "fastapi==0.95.2" "pydantic==1.10.26" "uvicorn==0.23.2" \
  "python-jose==3.3.0" "passlib==1.7.4" "bcrypt==4.0.1" \
  "PyPDF2==3.0.1" "python-multipart==0.0.32" \
  "google-cloud-firestore==2.28.1" "google-cloud-storage==3.13.1" \
  "google-cloud-vision==3.15.0" "Pillow==12.3.0" "stripe==15.5.0"
pip install "flake8==7.3.0" "httpx==0.27.2"   # the gates and the verification harness
pip check                                     # expected: No broken requirements found.
```

**Every version above is pinned, the cloud and media packages included.** An unpinned install resolves whatever is current on the day you run it, which is a different set from the one this project was verified against — and on Python 3.9 it resolves a different set again. This is documentation, not a manifest: there is still no `requirements.txt` (**NT-1**). [ONBOARDING §1.4](documentation/ONBOARDING.md#14-install-dependencies) lists every validated version, and the `celery==5.6.3` extra you need only if you are working on **NT-6**.

**The repository carries no `.gitignore`**, so nothing stays out of your commits automatically. Keep the environment directory and any credential file out of every `git add` — `SECRET_KEY` signs every token and `STRIPE_API_KEY` moves money, so either one in history means rotating it. Use `.git/info/exclude` for local ignore patterns.

**3. Export the environment variables.** Eight have no default: if one is missing, importing `app.core.config` fails with a Pydantic `ValidationError` and nothing starts.

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

Three further settings are optional and already correct by default: `ALLOWED_ORIGINS` (defaults to `["http://localhost:3000"]`, and accepts either a comma-separated list or a JSON array — it is also the list the 500 handler checks before echoing an origin), `ALGORITHM` (`HS256`) and `SENTRY_DSN` (unset). **No setting is validated beyond its type**, so four wrong values import cleanly and then govern: a `SECRET_KEY` short enough to brute-force, an `ACCESS_TOKEN_EXPIRE_MINUTES` of `0` — which mints tokens that are already expired — or of three days, a mistyped `ALGORITHM`, and an `ALLOWED_ORIGINS` of `*`, which is an allow-all rather than a half-measure because the middleware runs with `allow_credentials=True` and Starlette then echoes back every requesting origin. Refusing each of them at import is task **NT-39**; until it lands, treat these four values as things to review rather than things the application checks. **Export the variables: a `.env` file is not read.** The settings object declares no `env_file`, because the dependency that would read one is not installed — while it was declared, a `.env` in the working directory stopped the process at import instead of configuring it. Values you put in a `.env` now go unread, so the settings simply come back missing. [ONBOARDING §1.5](documentation/ONBOARDING.md#15-configure-the-environment) has every setting, its default, and what actually reads it.

**4. Set `PYTHONPATH`.** Mandatory, not a convenience: the source root is `backend/` while every import is written as top-level `app.…`, and `backend/` contains no `__init__.py` files. Omit it and everything fails with `ModuleNotFoundError: No module named 'app'`.

```bash
export PYTHONPATH="$PWD/backend"
```

**5. Run it.** On an unmodified checkout `uvicorn app.main:app --reload --port 8000` stops with `ImportError: cannot import name 'Stripe' from 'stripe'`, raised in a module outside the change set that repaired the rest of the backend — that is **HCF-3**, and you have not misconfigured anything. **[ONBOARDING §1.7.1](documentation/ONBOARDING.md#171-the-local-development-profile) is the path that does serve**: start the Firestore emulator, write one fail-closed profile file *outside* the working tree, add it to `PYTHONPATH`, and run the same Uvicorn command. It takes four steps, needs no Google or Stripe account, sends nothing to either provider, and gives you Swagger UI at <http://localhost:8000/docs> with all 13 routes live. It is a development profile and must never be deployed. Logging is one `logging.basicConfig(level=logging.INFO)` call on the **root** logger, so application lines go to standard error in its default `LEVEL:logger:message` form — `INFO:app.main:startup complete: …` on a clean start — and so do `INFO` lines from any library that logs, `httpx` and the Google client stack included. Two consequences to know before you rely on the output: the default formatter renders **no** `extra` keys, so the per-request `correlation_id` that listing creation attaches to every record is carried on the record and never printed — the purchase path works around it by naming its fields in the message text, which is why a failed charge prints one readable `ERROR:app.api.transactions:payment failed: provider_status=… provider_error=… vehicle=… buyer=… correlation_id=…` line and never the payment token; and a guarded provider failure in listing creation is logged with `logger.exception`, so it prints a full traceback, which carries absolute paths and whatever text the provider chose to return. Giving this a formatter, per-namespace levels and a request id assigned in middleware is task **NT-29**; [ONBOARDING §1.11](documentation/ONBOARDING.md#111-where-the-logs-go) explains what is configured today and how to take it over.

**6. Optionally, run the SPA** — `cd frontend && npm install && npm start -- --port 3000`, which runs Vite (there is no `npm run dev` script; pass the port so the origin matches `ALLOWED_ORIGINS`). **The dev server starts and the application renders nothing at all** — this is measured, not predicted: the only URL that answers 200 is `/index.html` and it paints a blank page with an empty mount point and no stylesheet, every one of the app's own routes answers 404 with a zero-length body so the browser shows its own error interstitial, and three subresources answer 500 on every load. The causes are all pre-existing and all in `frontend/`: the entry `index.html` is in the wrong place for Vite and has no module script, four `%PUBLIC_URL%` placeholders nothing substitutes, no history fallback and no catch-all route, a client module graph that does not resolve, an API base read through an expression Vite never satisfies, and eight packages `src/` imports that `frontend/package.json` does not declare. Each is inventoried with line numbers in [ONBOARDING §2.6.2](documentation/ONBOARDING.md#262-why-none-of-it-executes-yet) and [§5.2](documentation/ONBOARDING.md#52-work-worth-picking-up) as **NT-15**, **NT-30** to **NT-34** and **NT-42** to **NT-44**. **Use `/docs` rather than the SPA to exercise this API**, and treat any clean frontend measurement as vacuous until those land — an empty page passes a responsive, accessibility or theming check by having nothing in it.

**Verify a change** with the two static gates and the behavioural harness — none of them needs a server, and all of them run with the virtual environment of step 2 active:

```bash
python -m py_compile backend/app/main.py           # and whatever else you changed
python -m flake8 --select=F401,F811,F821,F841 backend/app/
```

That selection is this project's own acceptance gate. It reports **ten** findings today, all pre-existing in files the last change set was not authorised to touch, so **the gate does not pass** — [ONBOARDING §1.9.2](documentation/ONBOARDING.md#192-gate-2--undefined-and-unused-names-across-the-whole-package) enumerates all ten and explains why it is reported as blocked rather than redefined. Two of them are why the Celery module cannot be imported. For behaviour, run the harness in [§6](documentation/ONBOARDING.md#6-verifying-behaviour-in-process); [§1.10](documentation/ONBOARDING.md#110-before-you-call-a-change-done) is the short list of what must hold before a change is done.

## API

**13 routes across 9 unique paths**, served under **`http://localhost:8000/api`** — the Uvicorn origin plus `API_V1_STR`:

| Method(s) | Path | Access |
| --- | --- | --- |
| POST | `/api/auth/register` | Public, **twenty per minute per connection** then 429. 201; 409 on a duplicate address, matched on its canonical form so a different case is a duplicate; 422 for a `role` outside `buyer`/`seller`, a malformed or over-long address, a blank name or one carrying markup, or a password outside 8–72 UTF-8 bytes |
| POST | `/api/auth/login` | Public, **ten failures per minute per connection and per address** then 429 with `Retry-After`. JSON body `{"email": "…", "password": "…"}`; one 401 for a wrong password, an unknown address, a blank credential and an over-long one alike, every failure held to the same 0.75-second floor so timing tells a caller nothing. Any spelling of a registered address signs in |
| POST | `/api/auth/logout` | Bearer token. Acknowledgement only — it does **not** revoke the token (**HCF-10**) |
| GET | `/api/auth/me` | Bearer token. Seven public profile fields; no password hash. `created_at` and `updated_at` are UTC RFC-3339 strings with an explicit `+00:00` offset, byte-identical to the pair `register` and `login` return for the same user |
| GET | `/api/listings/listings` | **Public read.** Filterable by `make`, `model`, `year`, `min_price`, `max_price` |
| POST | `/api/listings/listings` | Bearer token, and `role` must be `seller`. 422 for more than 20 maintenance records, more than 900 KiB of maintenance content, or a serialized listing over 1 MB. **`photos` is unbounded** — one request makes one Cloud Vision call per entry, however many you send (**NT-25**) |
| GET | `/api/listings/listings/{listing_id}` | **Public read.** |
| PUT, DELETE | `/api/listings/listings/{listing_id}` | Bearer token. Update requires ownership, with no administrator override; delete permits the owner or an `admin` |
| POST | `/api/transactions/transactions` | Bearer token. 403 unless the caller is the request's `buyer_id`; 400 if the vehicle is absent, not `available`, or the charge fails. The amount and the seller are taken from the request unchecked (**NT-41**), and a payment intent already recorded is charged again (**NT-27**) |
| GET | `/api/transactions/transactions/{transaction_id}` | Bearer token; participants only |
| POST, GET | `/api/messages/messages` | Bearer token *(reachable, not yet functional — **HCF-8**)* |

**Protected operations take an `Authorization: Bearer <token>` header**; the two listing reads above take none, which is deliberate for a marketplace. **The repeated path segments are real, not a typo** — each router's prefix and its decorators both name the area, so the served path is `/api/listings/listings`. That diverges from the specification and the SPA, so it is frozen rather than tidied (**HCF-6**). The four authentication paths match the specification exactly.

Three things about this API surprise people, each documented where it belongs: login takes a **JSON** body and returns `access_token`, `token_type`, `token` and `user`, which is also why Swagger's "Authorize" control cannot complete the password flow ([§2.5](documentation/ONBOARDING.md#25-authentication-and-tokens), [§2.6](documentation/ONBOARDING.md#26-the-spa-contract--treat-it-as-frozen)); logout is stateless ([§2.5.1](documentation/ONBOARDING.md#251-logout-is-stateless)); and the only bounds on a write are the three in `create_listing` — a maintenance record count, an aggregate content size and a serialized document size — all of which sit *after* the provider calls they cannot prevent, so a rejected request has already paid for them ([§2.8](documentation/ONBOARDING.md#28-what-the-write-endpoints-bound)). **Every response now carries `x-content-type-options`, `x-frame-options`, `referrer-policy` and `strict-transport-security`**, plus `cache-control: no-store` on anything under `/api/auth` or requested with a credential, and a `default-src 'none'` content-security policy on every path except the four documentation ones — those load their bundles from a CDN and would go blank under it. An unhandled failure answers `{"detail": "Internal server error"}` as JSON **with the CORS headers**, so a browser client can tell a server error from an outage rather than seeing `Failed to fetch` for both. TLS, HSTS preloading and host validation remain the edge's (**NT-38**). For the full design, data models and architecture, see [documentation/Technical Specifications.md](documentation/Technical%20Specifications.md).

## Known limitations

Every one of these is deliberate and tracked, not hidden — with the position taken, the reasoning, and what a fix involves, in [ONBOARDING §5](documentation/ONBOARDING.md#5-suggested-next-tasks):

| | What it means for you |
| --- | --- |
| **HCF-3** — Stripe SDK mismatch | `backend/app/services/payment.py` imports a symbol current Stripe SDKs do not expose, so an unmodified checkout cannot boot. The highest-value task in the register; use the [local profile](documentation/ONBOARDING.md#171-the-local-development-profile) until it is fixed |
| **HCF-4** — Cloud Vision SDK mismatch | Real photo analysis fails. Listing creation logs `photo analysis failed` once per photo with a traceback and the request's `correlation_id` in the message, stores an empty analysis, and still answers 200 |
| **HCF-7** — nothing creates a vehicle | The purchase path reads `vehicles/{id}` for its `status` and no module ever writes one, so you must create it yourself — `{"status": "available"}` is all the handler reads — to exercise the endpoint at all. It is also what blocks **NT-41**: with no record carrying a `price` or a `seller_id`, there is nothing to settle a purchase's terms against, so the caller's own figures govern |
| **HCF-8** — messaging | Both message endpoints are mounted and answer, and both still fail on their own logic |
| **NT-6** — no background workers | `backend/app/tasks/background_jobs.py` cannot be imported. In the order you meet them: Celery is not in the runtime install set at all; then line 7's import of `app.services.payment` hits **HCF-3**; then `process_refund` does not exist (the service defines `create_refund`); then `settings.CELERY_BROKER_URL` is undeclared, which exporting the variable does not fix; then `List` is used without importing `typing`. Call-site defects follow |
| **NT-42** — the SPA renders nothing | Not "unstyled" or "partly wired": a blank document with zero interactive elements at every width. Ten of the source modules its graph imports do not exist on disk, one of them on the entry file's first dependency hop; `App.tsx` renders a router component the installed version does not export and declares no `/login` or `/register` route. So the four authentication routes have **no consumer** — the API contract matches the client's code exactly, and that code cannot execute |
| **NT-30**–**NT-34** — the SPA's other halves | It does not compile, cannot address this API, imports eight undeclared packages, and its error logging can put a password or a live token in the browser console |
| **NT-43**, **NT-44** — the documentation surfaces | `/redoc` is blank, because FastAPI 0.95.2 hard-codes a CDN path that now 404s; `/docs` renders but its **Authorize** control cannot complete against a JSON-body login, so nine of the thirteen operations cannot be exercised from the only working UI. Both bundles come from a public CDN, so an air-gapped deployment has neither |
| **NT-16** — continuous integration | Red: it installs from a `requirements.txt` that does not exist, runs a test suite that cannot be collected, and builds an image with no Dockerfile |
| **NT-25**–**NT-28**, **NT-39**–**NT-41** — not hardened | The largest block of open work, and the reason this must not face an untrusted caller. Registration input and the attempt ceilings are now in place (**NT-24**, **NT-26**), but the counters live in one process, so several workers multiply the ceiling. No write body is bounded, so one request decides how many provider calls it makes (**NT-25**). A purchase is four separate steps — availability read, charge, transaction write, `sold` update — with no reservation, no replay check and no idempotency key, so a retry pays twice, two simultaneous buyers are both charged, and a crash between the last two writes leaves a charged card with no transaction (**NT-27**, **NT-28**); its amount and seller are the caller's own (**NT-41**). No setting is checked beyond its type (**NT-39**), and uniqueness is a query rather than a constraint (**NT-40**). [§5.3](documentation/ONBOARDING.md#53-where-to-start) orders all of it by exposure |
| **Public reads expose raw maintenance content** | `create_listing` stores whatever `maintenance_records[*].content` a seller sends and both listing reads are unauthenticated, so a receipt's names, addresses and VIN are world-readable. Send only what you would publish until the handler stores it privately (**NT-13**) |
| **`python-jose 3.3.0` has published advisories** | CVE-2024-33663 and CVE-2024-33664, addressed from 3.4.0. Every token here is HS256 JWS, which is not the algorithm-confusion surface those turn on — though nothing *enforces* that, since `ALGORITHM` is a plain string setting with no allow-list behind it (**NT-39**). The pin is a known-vulnerable version and upgrading it is a dependency change this repair set was not authorized to make |

## Documentation

| Document | What it covers |
| --- | --- |
| [documentation/ONBOARDING.md](documentation/ONBOARDING.md) | Setup, domain context, common pitfalls, how to extend the project, and suggested next tasks |
| [documentation/Technical Specifications.md](documentation/Technical%20Specifications.md) | System architecture, data models, API design and interfaces |
| [documentation/Software Requirements Specifications (SRS).md](documentation/Software%20Requirements%20Specifications%20%28SRS%29.md) | Functional and non-functional requirements, users and personas |
| [documentation/Software Project Proposal.md](documentation/Software%20Project%20Proposal.md) | Business case, objectives and scope boundary |
| [blitzy/documentation/Project Guide.md](blitzy/documentation/Project%20Guide.md) | A point-in-time assessment of the codebase; historical reference |

## Contributing

There is no `CONTRIBUTING.md` yet — writing one is tracked as **NT-19**. Until then [documentation/ONBOARDING.md](documentation/ONBOARDING.md) is the contributor guide: run the gates in [§1.9](documentation/ONBOARDING.md#19-verify-your-checkout) before every commit, check the list in [§1.10](documentation/ONBOARDING.md#110-before-you-call-a-change-done) before you call a change done, follow the patterns in [§4](documentation/ONBOARDING.md#4-how-to-extend) rather than habits from other FastAPI projects, and pick your first change from [§5](documentation/ONBOARDING.md#5-suggested-next-tasks).

## License

No license file is present, so the terms of use are undefined. Adding one is tracked as **NT-4** — **do not invent a license**; only the project owners can settle it. Raise the question with them before redistributing this code.

## Getting help

**This repository declares no support channel** — no canonical remote, no issue tracker, no contact address, so any named here would be guesswork. Use what is actually here: [ONBOARDING §3.1](documentation/ONBOARDING.md#31-symptom-lookup) maps the failures you are most likely to hit — including the two that look like your mistake and are not — to the section that explains each one, and [§5](documentation/ONBOARDING.md#5-suggested-next-tasks) is the register of everything this project knows is missing. For the clone URL, the cloud project and the Stripe account, ask whoever gave you this repository. If you are setting it up for a team, publishing an issue tracker and a contact address — and replacing this section with them — is worth doing early.
