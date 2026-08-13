

# 1. SETUP

## 1.1 WHAT THIS SYSTEM IS

The Used Car Marketplace is a **Python/FastAPI** service whose application persistence is **Google Cloud Firestore** (native mode, NoSQL documents), using **Google Cloud Vision** to analyse vehicle photographs — with **Pillow** decoding the image bytes on the way in — **PyPDF2** to read maintenance documents, and **Stripe** to settle payments. A **React/TypeScript** single-page application in `frontend/` consumes the API over JSON.

Two points of precision about the data and integration story, because the repository contains material that suggests otherwise:

- **No application code issues a SQL query.** Every read and write in `backend/app/` goes through the single Firestore client in [`app/db/firestore.py`](../backend/app/db/firestore.py); there is no ORM, no connection string and no migration directory. You will nonetheless find SQL named elsewhere: [`Technical Specifications.md`](Technical%20Specifications.md) lists SQL among the project's languages (§4.1, §5.1, §6.1), names SQLAlchemy in its backend framework list (§6.2) and describes Google Cloud SQL as a relational store for transaction history (§6.3), while [`infrastructure/docker/docker-compose.yml`](../infrastructure/docker/docker-compose.yml) declares a `postgres:13` service and hands the backend a `DATABASE_URL`. **That compose service is vestigial** — nothing in the codebase reads `DATABASE_URL`, and that compose file cannot start at all today (**NT-15**). Treat the relational plan as unimplemented intent, not as a second live datastore.
- **Google Cloud Storage is present but unwired.** [`app/db/cloud_storage.py`](../backend/app/db/cloud_storage.py) builds a client at import and offers `upload_file`, `delete_file` and `get_file_url`, but **no module imports it**, so no request path uploads or serves an object. `GOOGLE_CLOUD_STORAGE_BUCKET` is required all the same — `Settings` declares it without a default, so startup fails without it — and the only code that reads it is that unimported module. Photo handling today stores whatever URL strings the caller supplies (**HCF-5**).

This guide takes you from a clean machine to a **running** local application — configured, compiled, linted, served on a port you can call with `curl` or a browser, and ready to modify. Getting that served port today goes through the **local development profile** in [section 1.7.1](#171-the-local-development-profile), because an unmodified checkout stops on a third-party import that sits outside the change set which repaired the rest of the backend (**HCF-3**, [section 3.8](#38-a-clean-boot-still-stops-in-the-payment-module)). The profile is four steps, needs no Google Cloud account and no Stripe account, keeps every stand-in **outside** the repository, and is what every claim in this guide about a served port was checked against. It then explains the domain, the traps, and how to add to the codebase. It is deliberately the *deep* companion to [`README.md`](../README.md), which carries the same setup path in brief. For requirements, personas and scope, read [`Software Requirements Specifications (SRS).md`](Software%20Requirements%20Specifications%20%28SRS%29.md); for architecture, data models and API design, read [`Technical Specifications.md`](Technical%20Specifications.md); for the business case and scope boundary, read [`Software Project Proposal.md`](Software%20Project%20Proposal.md). This guide does not restate those documents — where they are authoritative it links to them, and where the running code has diverged from them it says so explicitly and names the reason.

> **Read this before you judge the system — and before you trust any number in it.** `uvicorn app.main:app` **does not start on an unmodified checkout.** The import chain stops in `app/services/payment.py`, a module that lies outside the change set which made the rest of this backend importable (**HCF-3**, [section 3.8](#38-a-clean-boot-still-stops-in-the-payment-module)). The supported way to get a served port today is the local development profile in [section 1.7.1](#171-the-local-development-profile), which stands that one import in from outside the repository. So every claim below is labelled with how it was established:
>
> | Claim | How it was established |
> | --- | --- |
> | The seven recently changed modules compile and are free of undefined and unused names | `py_compile` and the `flake8` F-code gate, both of which parse rather than import ([section 1.9](#19-verify-your-checkout)) |
> | **13 routes across 9 unique paths** | Read statically off disk — four `include_router` prefixes against thirteen `@router` decorators ([section 2.7](#27-the-routes-as-actually-served)) |
> | The four authentication routes, listing creation and transaction creation behave as documented | Exercised **in process** against an in-memory datastore, with the one out-of-scope Stripe import stood in for, using Starlette’s `TestClient` ([section 6](#6-verifying-behaviour-in-process)) |
> | The same routes answer **over HTTP on a real port** | Driven with `curl` against the local development profile — Uvicorn, the Firestore emulator, and fail-closed stand-ins for Stripe and Cloud Vision ([section 1.7.1](#171-the-local-development-profile)) |
> | Two of the thirteen registered routes still fail on their own logic | Both message routes — reproduced under the profile above, and tracked as **HCF-8** ([section 3.10](#310-messaging-is-reachable-but-not-functional)) |
>
> **The first-party import chain is closed, and the closure stops at that chain.** Every name `app.main` and the four routers it mounts need now exists, is spelled the way its module exports it, and is imported where it is used. One first-party module outside that chain is still un-importable: `backend/app/tasks/background_jobs.py` uses `List` without importing it, reads a `CELERY_BROKER_URL` setting that `Settings` does not declare, and imports a `process_refund` that `app/services/payment.py` does not define. No worker can run until that is fixed (task **NT-6**), and nothing in this guide should be read as saying otherwise.
>
> **Read 13 as routes *registered*, never as endpoints *working*** — two of them do not work, and no port is served until you either take the local profile in [section 1.7.1](#171-the-local-development-profile) or repair **HCF-3** properly.
>
> Every one of those claims is reproducible, and no way of reproducing them needs a running server: [section 1.9](#19-verify-your-checkout) is the gate set you run before every commit, [section 1.10](#110-before-you-call-a-change-done) is the full protocol — ten categories and forty-four numbered assertions, each naming the artefact that owns it — and [section 6](#6-verifying-behaviour-in-process) is a self-contained script that asserts a hundred and thirty-nine of the same properties in a few seconds with no network, no credential and no server. Each says plainly what it cannot prove, and each points at the two properties that need a Firestore emulator instead, because an in-memory double cannot model a race.
>
> Every gap named here is recorded with its reasons in [section 5](#5-suggested-next-tasks). None of them is something you have misconfigured, and [section 3](#3-common-pitfalls) tells you what each failure looks like so you can recognise it in a second rather than debug it for an hour.

## 1.2 PREREQUISITES

| Requirement | Version | Why |
| --- | --- | --- |
| **A Unix-like OS and a POSIX shell** | Linux or macOS; **bash** 4+ or **zsh** | Every command in this guide is POSIX shell — `export`, `source`, `$PWD`, single-quoted heredocs — and several write scratch files under `/tmp`. On **Windows, use WSL 2** and run everything inside the Linux environment; PowerShell equivalents are not published here, and `cmd`-style quoting will break the JSON payloads below. |
| Python | **3.10 – 3.12**, and use **3.12** | Two different floors, explained below: 3.9 is the *source* floor pinned by continuous integration in [`.github/workflows/backend_ci.yml`](../.github/workflows/backend_ci.yml) (line 19), while installing the verified dependency set needs 3.10 or newer. 3.12 is the validated version. |
| **Command-line tools** | any recent | `git` (repository access), `curl` (every request example, and the local profile's smoke test), and `openssl` **or** Python's `secrets` module for generating `SECRET_KEY`. All of them ship with a stock macOS and with any mainstream Linux. |
| **Docker** *or* **the Google Cloud CLI** | Docker 20.10+ / `gcloud` any recent | Needed **only** to run the Firestore emulator that backs the local development profile in [section 1.7.1](#171-the-local-development-profile) — which is the only way to get a served port today. Either one is enough; you do not need both, and neither is needed for the compile, lint or in-process gates. |
| A Google Cloud project | — | Firestore (native mode), Cloud Storage and Cloud Vision enabled, with Application Default Credentials available locally. Needed **only** for real cloud traffic — see [section 1.2.1](#121-google-credentials-what-needs-them-and-how-to-establish-them). |
| A Stripe test API key | — | `STRIPE_API_KEY` is a required setting, so a value must be present for anything to import. A real key is needed only if you intend to send a real charge. |
| Node.js + npm | 18 or later | Only if you also want to run the SPA ([section 1.8](#18-run-the-spa-optional)). |
| Terraform | — | Optional, and only for the deployment material under `infrastructure/`. |

**All backend code must stay source-compatible with Python 3.9, and you cannot install this dependency set on 3.9.** Both halves are true and they are not in conflict, so keep them apart in your head:

- **Source floor, 3.9.** Continuous integration runs on 3.9, so a genuinely 3.10+ construct — `match`, or an `X | Y` union in an annotation that is evaluated at runtime — will pass locally and fail there.
- **Install floor, 3.10.** The current releases of `google-cloud-firestore`, `google-cloud-storage`, `google-cloud-vision`, `Pillow` and `python-multipart` all declare `Requires-Python >= 3.10`. On 3.9, pip does not fail — it quietly resolves *older* versions of all five, so a 3.9 environment is a different dependency set from the one this project was verified against, and a defect you hit there may not exist here. Pin the versions in [section 1.4](#14-install-dependencies) and use 3.10 or newer.

The two together mean the CI job cannot install what it needs, which is one of the reasons continuous integration is red (**NT-16**). Do not "fix" it by dropping the 3.9 source rule without changing that workflow: the floor and the runner have to move together.

Separately from that floor, **use `typing.List`, `typing.Optional` and `typing.Dict`** rather than the builtin generics `list[...]` and `dict[...]`. This one is house style, not a compatibility requirement: PEP 585 made the builtin generics work in 3.9, so `dict[str, str]` would run — but every existing module uses the `typing` spelling, and a file that mixes the two is harder to read than either.

**Do not exceed 3.12.** Nothing above 3.12 has been validated against this pinned dependency set, and the risk is concentrated in `passlib 1.7.4`, which is unmaintained and predates 3.13. It is not a specific known failure: `passlib` guards its optional `from crypt import crypt` in a `try/except ImportError`, so the standard library's removal of `crypt` in 3.13 does not by itself break the bcrypt backend this project uses. Treat 3.13 and above as untested rather than as broken, and if you must go there, verify hashing and verification first.

Check what you have before you go any further:

```bash
python3 --version
```

If that reports anything outside 3.10–3.12, **do not proceed with it.** Install an in-range interpreter and call it explicitly, substituting `python3.12` wherever `python3` appears below. Skipping this wastes real time: on a 3.13 host, `python3 -m venv` can fail outright, and when it does not, the failure surfaces much later as a confusing dependency error.

### 1.2.1 Google Credentials: What Needs Them, And How To Establish Them

Read this before you go looking for a cloud project, because most of what you will do here needs no credentials at all. The Firestore, Cloud Storage and Cloud Vision clients are constructed when their modules are imported ([section 3.11](#311-clients-are-built-at-import-not-at-startup)), but in the installed SDK versions construction does **not** resolve credentials — the first real API call does. So:

| What you are doing | Google credentials needed? |
| --- | --- |
| Compiling, and the `flake8` F-code gate ([section 1.9](#19-verify-your-checkout)) | **No.** Both parse the source; neither imports it |
| The in-process behavioural harness ([section 6](#6-verifying-behaviour-in-process)) | **No.** It replaces Firestore with an in-memory double and deletes every credential variable from its own environment |
| The local development profile ([section 1.7.1](#171-the-local-development-profile)) | **No.** The Firestore emulator uses anonymous credentials, and Cloud Vision is stood in for. This was verified with `GOOGLE_APPLICATION_CREDENTIALS` unset |
| Talking to a real Firestore, Cloud Storage bucket or Cloud Vision endpoint | **Yes** — Application Default Credentials, established one of the two ways below |

**Option A — the Google Cloud CLI** (best on a workstation you own; the credentials live in your own account):

```bash
# install the CLI first: https://cloud.google.com/sdk/docs/install
gcloud auth application-default login          # opens a browser, writes ADC to your home directory
gcloud config set project <your-gcp-project>
gcloud services enable firestore.googleapis.com storage.googleapis.com vision.googleapis.com
```

**Option B — a service-account key file** (for a CI runner, a container, or a machine with no browser). Create the account and grant it `roles/datastore.user`, `roles/storage.objectAdmin` and `roles/serviceusage.serviceUsageConsumer` in the Cloud console, download the JSON key, then:

```bash
# keep the key OUTSIDE the repository -- this tree has no .gitignore (section 1.3)
export GOOGLE_APPLICATION_CREDENTIALS="$HOME/.config/used-car-marketplace/sa.json"
chmod 600 "$GOOGLE_APPLICATION_CREDENTIALS"
```

**Verify whichever you chose**, before you blame the application for a permission error. Run it with the virtual environment of [section 1.3](#13-create-an-isolated-environment) active, since `google-auth` arrives with the cloud packages:

```bash
python3 -c "import google.auth; c, p = google.auth.default(); print('ADC ok, project:', p)"
```

That prints the project your credentials resolve to. `google.auth.exceptions.DefaultCredentialsError` means neither option is in place — go back and do one of them, or use the local profile, which needs none. A `403 PERMISSION_DENIED ... CONSUMER_INVALID` from a running request means the opposite: credentials resolved, but to a project that is not yours or has the API disabled.

## 1.3 CREATE AN ISOLATED ENVIRONMENT

Unless a command says otherwise, run everything below from the **repository root**, in the order given. Nothing here assumes prior state.

```bash
python3 -m venv .venv
source .venv/bin/activate
```

**The repository has no `.gitignore`, so nothing keeps anything out of your commits automatically.** That is a build-artefact nuisance and a credential hazard, and the second one matters more. Before you create anything:

- Keep the environment directory out of every `git add` you run, or create it outside the working tree entirely.
- **Never commit a file containing secrets** — no `.env`, no service-account JSON, no exported key. `SECRET_KEY` signs every access token, and `STRIPE_API_KEY` moves money; either one in history is a rotation exercise, not a `git rm`. Note that this guide deliberately steers you to *exported* variables rather than a `.env` file ([section 1.5](#15-configure-the-environment)), which keeps the values out of the working tree in the first place.
- If you want local ignores without adding a tracked `.gitignore` — which is a repository-wide decision, not yours to make in passing — put your patterns in `.git/info/exclude`. It is per-clone, never committed, and does the same job.
- Keep your Google credentials file and any key material **outside the repository directory** and point `GOOGLE_APPLICATION_CREDENTIALS` at it there. For anything beyond local development, use a secret manager (Google Secret Manager, or your platform's equivalent) and inject the values as environment variables at deploy time rather than storing them in a file at all.
- Before your first push, run `git status` and read it. In a repository with no ignore rules, that one habit is the whole safety net.

## 1.4 INSTALL DEPENDENCIES

There is deliberately **no `requirements.txt`** in this repository, so the dependency set is installed explicitly. Do not create the manifest as a convenience — one was removed on purpose, and recreating it needs a decision rather than an initiative (task **NT-1** in [section 5](#5-suggested-next-tasks)).

Install in three steps, because the three sets answer different questions and only the first is imported by `backend/app/`.

**1. The application runtime.** Everything `app.main` and its import graph need:

```bash
pip install \
  "fastapi==0.95.2" "pydantic==1.10.26" "uvicorn==0.23.2" \
  "python-jose==3.3.0" "passlib==1.7.4" "bcrypt==4.0.1" \
  "PyPDF2==3.0.1" "python-multipart==0.0.32" \
  "google-cloud-firestore==2.28.1" "google-cloud-storage==3.13.1" \
  "google-cloud-vision==3.15.0" "Pillow==12.3.0" "stripe==15.5.0"
pip check
```

`pip check` must print `No broken requirements found.` Every version above is the one that was actually exercised; **treat all of them as fixed.** Two of the pins are load-bearing in ways that are not obvious, and both are explained in [section 3](#3-common-pitfalls): `pydantic` must stay on v1, and `passlib` must be paired with `bcrypt` below 4.1.

The five cloud and media packages are deliberately **unpinned**, because no pin was ever fixed for them — which is also how **HCF-3** and **HCF-4** arose: the code was written against older SDK generations, and `pip install` gives you today's. The versions the claims in this guide were verified against are in the snapshot table below; pin to those if you want to reproduce them exactly rather than rediscover a provider change.

**The cloud and media packages are pinned here too, and earlier revisions of this guide were wrong to leave them open.** An unpinned install resolves whatever is current on the day it runs, which is not the set anyone verified — and on Python 3.9 it resolves an older set again ([section 1.2](#12-prerequisites)). The versions above are what the assertions in [section 6](#6-verifying-behaviour-in-process) were run against. Note what this block still is not: a *reproducible* install needs a manifest with hashes, which is task **NT-1**; a list of versions in a document only stops the accidental drift.

One of these pins carries a known risk rather than a compatibility constraint. `python-jose 3.3.0` is the subject of CVE-2024-33663 and CVE-2024-33664, both addressed from 3.4.0 onwards. Every token this codebase issues and verifies is HS256 JWS and `ALGORITHM` is restricted to the HMAC family ([section 1.5.1](#151-every-setting-and-what-actually-reads-it)), which is not the algorithm-confusion surface the first advisory turns on — but the pin is a known-vulnerable version and upgrading it is a dependency decision nobody has taken yet (**NT-35**). Do not treat its presence in this list as a clean bill of health.

**2. The tooling the gates need.** Not imported by any application module, and not project dependencies — but the exact commands in [section 1.9](#19-verify-your-checkout) and the script in [section 6](#6-verifying-behaviour-in-process) are asserted against these versions, so pin them:

```bash
pip install "flake8==7.3.0" "httpx==0.27.2"
```

`flake8` is pinned because [section 1.9.2](#192-gate-2--undefined-and-unused-names-across-the-whole-package) states an exact finding count, and a different Pyflakes generation can report a different set. `httpx` is pinned **below 0.28** because 0.28 removed the `Client(app=…)` shortcut that `starlette 0.27`'s `TestClient` relies on — with 0.28 installed, the harness in section 6 cannot construct a client at all.

**3. The worker extra — only if you are working on the task module.** `backend/app/tasks/background_jobs.py` imports Celery, and nothing else in the repository does:

```bash
pip install "celery==5.6.3"
```

Installing it does **not** get you a working worker: that module cannot be imported for four independent reasons and has never executed. It is task **NT-6**, and [section 5.2](#52-work-worth-picking-up) lists the failures in the order you will meet them. Skip this step unless NT-6 is your task; nothing on the HTTP path needs it. There is no broker either — `CELERY_BROKER_URL` is read by that module and declared by no settings object.

Note what none of the three sets contains: **`python-dotenv`**. Pydantic reads a `.env` file only through that optional extra, so configuration comes from exported environment variables — [section 1.5](#15-configure-the-environment) explains what happens if you create a `.env` anyway.

**The validated snapshot.** These are the exact versions every command, count and behavioural claim in this guide was checked against. Nothing here is a repository pin — no manifest exists to hold one — so treat it as the reproducible reference, not as a constraint the tooling enforces:

| | Validated version |
| --- | --- |
| Interpreter | Python **3.12.13** |
| Application runtime | `fastapi 0.95.2`, `pydantic 1.10.26`, `starlette 0.27.0` (pulled by FastAPI), `uvicorn 0.23.2`, `python-jose 3.3.0`, `passlib 1.7.4`, `bcrypt 4.0.1`, `PyPDF2 3.0.1`, `python-multipart 0.0.32` |
| Cloud and media (unpinned above) | `google-cloud-firestore 2.28.1`, `google-cloud-storage 3.13.1`, `google-cloud-vision 3.15.0`, `google-cloud-core 2.6.1`, `Pillow 12.3.0`, `stripe 15.5.0` |
| Gate tooling | `flake8 7.3.0`, `httpx 0.27.2` |
| Worker extra | `celery 5.6.3` |
| Packaging toolchain | `pip 26.2.1`, `setuptools 80.9.0`, `wheel 0.48.0` |

Two notes on that last row, because it is the one people trip over. Nothing in this dependency set **requires** `setuptools` at runtime, and a `python3 -m venv` on 3.12 no longer installs it, so an install that never mentions it works fine — the pin is recorded because it is what was validated. Keep it at **80.9.0** if your environment does provide `setuptools`: later releases drop the bundled `pkg_resources`, and one installed module still imports that at module scope (`passlib/pwd.py`, the password *generator* — nothing this application imports). If you upgrade and something unrelated starts raising `ModuleNotFoundError: No module named 'pkg_resources'`, that is the cause.

## 1.5 CONFIGURE THE ENVIRONMENT

The settings object declares **eleven** fields. **Eight have no default**: if any one of them is unset, importing `app.core.config` raises a Pydantic `ValidationError` and nothing starts.

```bash
export PROJECT_NAME="used-car-marketplace"
export API_V1_STR="/api"
export SECRET_KEY="$(openssl rand -hex 32)"
export ACCESS_TOKEN_EXPIRE_MINUTES="60"
export GOOGLE_CLOUD_PROJECT="your-gcp-project"
export GOOGLE_CLOUD_STORAGE_BUCKET="your-bucket"
export STRIPE_API_KEY="sk_test_xxx"
export STRIPE_WEBHOOK_SECRET="whsec_xxx"

# Optional, and shown because it is the one setting whose absence is invisible:
# this is exactly the value the field already defaults to, so exporting it changes
# nothing today but puts the origin your browser will use in front of you. Accepts
# a comma-separated list or a JSON array -- see section 3.4.
export ALLOWED_ORIGINS="http://localhost:3000"
```

`sk_test_xxx` and `whsec_xxx` are placeholders — substitute your own test credentials. If `openssl` is unavailable, generate the key with `python3 -c "import secrets; print(secrets.token_hex(32))"` instead.

**Three of these values are secrets, and this repository has no `.gitignore` to protect you.** `SECRET_KEY` signs and verifies every access token — anyone holding it can mint a valid token for any user id — and `STRIPE_API_KEY` and `STRIPE_WEBHOOK_SECRET` are live payment credentials on a real Stripe account, test mode or not. Use a distinct `SECRET_KEY` per environment and never reuse a production one locally; keep all three out of the working tree, out of shell history where you can (a leading space, or a sourced file kept outside the repository), and out of any file you might `git add`. Beyond local development, load them from a secret manager at deploy time rather than from a file. See [section 1.3](#13-create-an-isolated-environment) for the local-ignore mechanism that needs no committed file.

**Export the variables. Do not reach for a `.env` file.** `Settings` does declare `env_file = ".env"` (UTF-8), but the installed dependency set cannot honour it: Pydantic 1.10.26 reads an env file through `python-dotenv`, that package is not installed, and adding a dependency is outside the current change scope. The consequence is worse than the feature simply being absent — if a `.env` file exists in your working directory, importing `app.core.config` **fails** instead of falling back to the environment:

```text
ImportError: python-dotenv is not installed, run `pip install pydantic[dotenv]`
```

and, because `settings = Settings()` runs at module scope, that happens while `app.core.config` is still being imported — so nothing starts. So the export block above is the supported configuration path. If you want `.env` support, get `python-dotenv` authorised and installed first (keeping `pydantic==1.10.26`), and only then create the file; the alternative is to drop the `env_file` declaration altogether so the exports are the only path.

No `.env.example` exists in this repository and this change did not add one; whether to add it, and whether to authorise the dependency that would make it work, is an open task (**NT-2**).

### 1.5.1 Every Setting, And What Actually Reads It

Two of the required variables are not yet consumed by any code. They are still mandatory — the settings object refuses to construct without them — so set them anyway and do not spend time looking for their effect.

| Variable | Required | Default | Purpose |
| --- | --- | --- | --- |
| `PROJECT_NAME` | Yes | — | **No consumer yet.** `app/main.py` constructs `FastAPI()` with no title, so this value is required but unused (task **NT-8**). |
| `API_V1_STR` | Yes | — | Builds the OAuth2 `tokenUrl` published in the OpenAPI schema. **Keep it as `/api`**: the router prefixes in `app/main.py` are literals, so a different value desynchronises the advertised token URL from the real one. |
| `SECRET_KEY` | Yes | — | HS256 signing and verification key for every access token. **At least 32 characters, or the import fails** — `openssl rand -hex 32` gives 64. Never reuse one across environments. |
| `ALGORITHM` | No | `HS256` | JWT algorithm, used for both signing and verification. **`HS256`, `HS384` or `HS512` only**; case is normalised. Anything else is refused at import — an asymmetric algorithm would need a key pair no setting supplies, and `none` would switch verification off entirely. |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | Yes | — | Access-token lifetime in minutes. Honoured on every issued token. **Must be between 1 and 1440.** `0` is refused rather than accepted, because a falsey delta is what `create_access_token` reads as "unset" before substituting 15 minutes — so a zero would silently mean something else. The ceiling is a day because nothing here can revoke a token before it expires ([section 2.5.1](#251-logout-is-stateless)). |
| `GOOGLE_CLOUD_PROJECT` | Yes | — | Project for the Firestore client, which every import of the application builds, and for the Cloud Storage client, which is built only if `app/db/cloud_storage.py` is imported — and nothing imports it ([section 3.11](#311-clients-are-built-at-import-not-at-startup)). |
| `GOOGLE_CLOUD_STORAGE_BUCKET` | Yes | — | Bucket handle acquired at import by `app/db/cloud_storage.py`. |
| `STRIPE_API_KEY` | Yes | — | Handed to the Stripe client that `app/services/payment.py` constructs at its line 5 — a line no clean import currently reaches (**HCF-3**). Still required regardless. |
| `STRIPE_WEBHOOK_SECRET` | Yes | — | **No consumer yet.** No webhook route exists; the variable is required regardless (task **NT-9**). |
| `SENTRY_DSN` | No | unset | Optional error-reporting endpoint. No consumer yet. |
| `ALLOWED_ORIGINS` | No | `["http://localhost:3000"]` | Origins accepted by the CORS middleware. Accepts a comma-separated list **or** a JSON array, and blank entries are dropped. **No entry may contain `*`** — see [section 3.4](#34-allowed_origins-takes-two-forms-and-neither-may-crash) for why a wildcard is refused rather than merely discouraged. |

**Four of these settings are validated, not merely typed, and a value outside the policy stops the process at import with a message naming the fix.** That is deliberate: every one of them is read by code that cannot defend itself — the signer takes `ALGORITHM` as given, the token helper reads a falsey lifetime as "unset", and the CORS middleware runs with credentials enabled. A refusal at import is the one failure mode an operator cannot miss; a weak value that works is the one nobody notices.

`CELERY_BROKER_URL` is **not** in this table on purpose. The background-task module reads it, but the settings object does not declare it, so that module cannot import at all (task **NT-6**).

## 1.6 SET `PYTHONPATH` — MANDATORY

```bash
export PYTHONPATH="$PWD/backend"
```

This is not a convenience. **The source root is `backend/`, while every import in the codebase is written as top-level `app.…`** — `app.api.auth`, never `backend.app.api.auth`. Python resolves `app` only when the directory that *contains* it is on `sys.path`, and running from the repository root puts the repository root there, not `backend/`. Omit this and every command in this guide fails identically:

```text
ModuleNotFoundError: No module named 'app'
```

Because the path is set to `backend/`, imports are always written `app.api.auth`, never `backend.app.api.auth`. A secondary detail, worth knowing once: `backend/` contains no `__init__.py` files, so `app` and its subdirectories resolve as implicit namespace packages. That works, but it is also why a mistyped path yields a silently empty package rather than an import error.

## 1.7 RUN THE API

The command itself is one line:

```bash
uvicorn app.main:app --reload --port 8000
```

> **Run exactly that on an unmodified checkout and it stops before it binds a port — that is expected, and it is not you.** Startup aborts with `ImportError: cannot import name 'Stripe' from 'stripe'`, raised at line 1 of `backend/app/services/payment.py`: a module that sits on the transactions router's import path and **outside** the change set which made the rest of this backend importable. It is known issue **HCF-3**, the highest-value task in [section 5](#5-suggested-next-tasks), and [section 3.8](#38-a-clean-boot-still-stops-in-the-payment-module) explains it in full. The failure is unrelated to credentials or configuration — it reproduces with and without them.

So there are two ways to run this application, and only the first works today:

| | What it gives you | What it costs |
| --- | --- | --- |
| **[Section 1.7.1](#171-the-local-development-profile) — the local development profile** | A real Uvicorn process serving all 13 routes on a port, backed by the Firestore emulator, with fail-closed stand-ins for the two third-party surfaces this code cannot reach: the `Stripe` symbol the installed SDK does not export, and the Vision client's absent `.image()` method | Four steps, and one file that lives **outside** the repository. Not production, and it sends nothing to Stripe or Cloud Vision |
| **[Section 1.7.2](#172-running-against-real-google-cloud-and-stripe) — real Google Cloud and Stripe** | The system as intended | Blocked: **HCF-3** must be repaired first, which needs authorization. Then credentials ([section 1.2.1](#121-google-credentials-what-needs-them-and-how-to-establish-them)) and a real Stripe key |

Everything in [section 1.9](#19-verify-your-checkout) works under either, and under neither, because none of those gates needs a server running.

### 1.7.1 The Local Development Profile

This is the clean-machine path to a **running, modifiable** application. It changes nothing in the repository: the two stand-ins live in one file in a scratch directory, reached through `PYTHONPATH`, and the datastore is the Firestore emulator. Every expectation printed below was observed from this exact sequence.

**Step 1 — start the Firestore emulator.** Either route works; pick the one whose prerequisite you already have.

```bash
# Route A -- Docker. The `emulators` tag is the Cloud SDK image that ships them.
docker run -d --name firestore-emulator -p 8080:8080 \
  gcr.io/google.com/cloudsdktool/google-cloud-cli:emulators \
  gcloud emulators firestore start --host-port=0.0.0.0:8080

# Route B -- the Google Cloud CLI, if it is installed (the emulator needs a JRE)
gcloud emulators firestore start --host-port=localhost:8080
```

Confirm it answers before you go on. It replies with the literal string `Ok`:

```bash
curl -s http://localhost:8080/
```

**Step 2 — write the profile file, outside the working tree.** It is named `sitecustomize.py` because Python imports that module automatically at interpreter start if it is on `sys.path` — which is what makes it survive `--reload`, since Uvicorn's reloader spawns a fresh interpreter that inherits the same `PYTHONPATH`. It does three things: refuses to run unless the emulator is configured, stands in for `stripe.Stripe` with a client that declines every charge, and stands in for the Vision client's missing method.

```bash
mkdir -p /tmp/ucm-localdev
cat > /tmp/ucm-localdev/sitecustomize.py <<'PY'
"""Local development profile for this backend. NOT part of the repository.

Stands in for the two third-party symbols the installed SDKs do not provide
(HCF-3, HCF-4) and refuses to start unless the Firestore emulator is
configured, so a local run can reach neither a real Google Cloud project nor
a real Stripe account.
"""
import os
import sys
import types

if not os.environ.get("FIRESTORE_EMULATOR_HOST"):
    # Exit rather than raise: the interpreter imports this module through
    # `site`, which swallows exceptions and carries on, so a raise here would
    # only print a warning and leave the stand-ins uninstalled.
    sys.stderr.write(
        "local profile: FIRESTORE_EMULATOR_HOST is unset, so the Firestore "
        "client would address a real project. Refusing to start.\n"
    )
    os._exit(1)


class StripeError(Exception):
    """Stands in for stripe.error.StripeError."""


class _Charges:
    """No charge and no refund ever leaves the machine.

    It declines by default. Set LOCAL_PROFILE_FAKE_CHARGE=1 to report a fake
    settlement instead, which is the only way to walk the purchase path's
    success branch locally.
    """

    @staticmethod
    def create(*_args, **_kwargs):
        if os.environ.get("LOCAL_PROFILE_FAKE_CHARGE") == "1":
            return types.SimpleNamespace(id="ch_local_fake", status="succeeded")
        raise StripeError("local profile: no charge is sent to Stripe")


class _FailClosedStripe:
    """Stands in for the absent `stripe.Stripe` symbol (HCF-3)."""

    def __init__(self, api_key=None):
        self.api_key = api_key
        self.error = types.SimpleNamespace(StripeError=StripeError)
        self.Charge = _Charges
        self.Refund = _Charges


_stripe = types.ModuleType("stripe")
_stripe.Stripe = _FailClosedStripe
_stripe_error = types.ModuleType("stripe.error")
_stripe_error.StripeError = StripeError
_stripe.error = _stripe_error
sys.modules.setdefault("stripe", _stripe)
sys.modules.setdefault("stripe.error", _stripe_error)


class _FailClosedVision:
    """Stands in for ImageAnnotatorClient, whose `.image()` is absent (HCF-4)."""

    def image(self, *_args, **_kwargs):
        raise RuntimeError("local profile: Cloud Vision is not called")


_vision = types.ModuleType("google.cloud.vision")
_vision.ImageAnnotatorClient = _FailClosedVision
sys.modules.setdefault("google.cloud.vision", _vision)
PY
```

**Step 3 — configure the shell.** This is the export block from [section 1.5](#15-configure-the-environment) plus three profile-specific values. Run it from the repository root, with the virtual environment active:

```bash
export FIRESTORE_EMULATOR_HOST="localhost:8080"   # the profile refuses to start without this
export GOOGLE_CLOUD_PROJECT="localdev-$(date +%s)" # a throwaway id: a fresh id is a fresh, empty dataset
unset GOOGLE_APPLICATION_CREDENTIALS               # prove to yourself that no real credential is in play
export PYTHONPATH="/tmp/ucm-localdev:$PWD/backend" # the profile FIRST, then the source root
```

The other required variables — `PROJECT_NAME`, `API_V1_STR`, `SECRET_KEY`, `ACCESS_TOKEN_EXPIRE_MINUTES`, `GOOGLE_CLOUD_STORAGE_BUCKET`, `STRIPE_API_KEY`, `STRIPE_WEBHOOK_SECRET` — must still be set, exactly as [section 1.5](#15-configure-the-environment) shows. Their values can be placeholders here: no real Stripe key is used, and no bucket is touched.

**Step 4 — run it.**

```bash
uvicorn app.main:app --reload --port 8000
```

A successful start prints the application's own startup line, and the interactive documentation is then live:

```text
2026-01-01 12:00:00,000 INFO app.main: startup complete: firestore and vision clients initialised at import
INFO:     Uvicorn running on http://127.0.0.1:8000 (Press CTRL+C to quit)
```

- Swagger UI — <http://localhost:8000/docs>
- OpenAPI schema — <http://localhost:8000/openapi.json>

**Step 5 — smoke-test it, in a second shell.** These four calls are the fastest proof that the profile is working end to end, and each answer below is what it actually returns:

```bash
BASE=http://localhost:8000/api
EMAIL="ada-$(date +%s)@example.test"
STATUS='\n-> HTTP %{http_code}\n'

# 201, with keys access_token, token_type, token, user -- and no password hash anywhere
curl -sS -w "$STATUS" -X POST "$BASE/auth/register" -H 'Content-Type: application/json' \
  -d "{\"email\":\"$EMAIL\",\"password\":\"correct-horse\",\"first_name\":\"Ada\",\"last_name\":\"Lovelace\",\"role\":\"seller\"}"

# 200; the same four keys. Keep the token for the call after this one.
TOKEN=$(curl -sS -X POST "$BASE/auth/login" -H 'Content-Type: application/json' \
  -d "{\"email\":\"$EMAIL\",\"password\":\"correct-horse\"}" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])')

# 200 {"user": {...}} -- exactly the seven public fields
curl -sS -w "$STATUS" "$BASE/auth/me" -H "Authorization: Bearer $TOKEN"

# 200 [] -- a public read: note that it carries no Authorization header at all
curl -sS -w "$STATUS" "$BASE/listings/listings"

# 401 with a WWW-Authenticate: Bearer header -- the rejection path, on purpose
curl -sS -w "$STATUS" -X POST "$BASE/auth/login" -H 'Content-Type: application/json' \
  -d "{\"email\":\"$EMAIL\",\"password\":\"wrong\"}"
```

**What the profile does and does not stand in for.** Read this column by column before you report a result:

| Behaviour under the profile | Why |
| --- | --- |
| All four authentication routes, both listing reads, listing creation, both transaction routes: **as documented** | Real handler code, real Pydantic validation, real bcrypt, real JWTs, real Firestore queries against the emulator |
| Listing creation with photos answers **200**, stores `photo_analysis: []` on the document, and logs `photo analysis failed` once per photo with a shared `correlation_id` | The Vision stand-in raises, and the per-photo call is guarded — the same degraded path a real **HCF-4** failure takes ([section 4.4](#44-call-services-synchronously-and-guard-them)). The response body carries the listing's own fields only, so the analysis is visible in Firestore and in the log, not in the answer |
| A purchase answers **400 `Payment processing failed`**, writes no transaction and leaves the vehicle `available` | The Stripe stand-in declines every charge. This is the profile failing closed, not a defect |
| Set `LOCAL_PROFILE_FAKE_CHARGE=1` and the same purchase answers **200**, writes the transaction as `completed` and marks the vehicle `sold` | The stand-in reports a fake settlement. **Nothing was charged.** Use it to walk the success branch, and never treat a green run as evidence that Stripe integration works |
| A purchase needs a vehicle document you wrote yourself | Nothing in this codebase creates one (**HCF-7**, [section 2.9](#29-the-purchase-path)). Write `vehicles/{id}` with `{"status": "available"}` against the emulator first |
| Both message routes answer **500** | **HCF-8**, unchanged and expected ([section 3.10](#310-messaging-is-reachable-but-not-functional)) |
| The SPA still cannot reach it | The client's own blockers, all pre-existing ([section 2.6.2](#262-why-none-of-it-executes-yet)) |

**Limits, stated once and plainly. This profile is for local development only.**

- **It is not a deployment, and it must never be one.** The stand-ins mean no payment is ever taken and no photo is ever analysed. Anything that ran this way and reported success has proved nothing about Stripe or Cloud Vision.
- **The emulator is not Firestore.** It enforces no security rules and needs no indexes, so an index a real project would demand goes unnoticed here.
- **The state is disposable.** A new `GOOGLE_CLOUD_PROJECT` id gives you an empty dataset; the emulator keeps nothing when it stops.
- **It does not close HCF-3.** The repository still cannot boot unaided. Repairing that properly — deciding which Stripe API generation to target — is the real fix, and the profile is what makes the codebase workable until someone is authorized to do it.
- **Report the gate you used.** "Verified under the local development profile" and "verified against real providers" are different claims, and only the first is available today.

**Teardown.** Nothing to undo inside the repository, because nothing there was touched:

```bash
# stop Uvicorn with Ctrl-C, then:
docker rm -f firestore-emulator     # or Ctrl-C the gcloud emulator
rm -rf /tmp/ucm-localdev
```

### 1.7.2 Running Against Real Google Cloud And Stripe

There is no working sequence to publish here yet, and pretending otherwise would waste your afternoon. `app/services/payment.py` cannot be imported against a current Stripe SDK (**HCF-3**), and repairing it means editing a file that is frozen pending authorization — so the honest instruction is: get **HCF-3** authorized and fixed first ([section 5.1](#51-decisions-awaiting-confirmation)). Once it is, this path needs exactly three things beyond [section 1.7.1](#171-the-local-development-profile): Application Default Credentials for a project with Firestore, Cloud Storage and Cloud Vision enabled ([section 1.2.1](#121-google-credentials-what-needs-them-and-how-to-establish-them)), a real `STRIPE_API_KEY`, and **no** `FIRESTORE_EMULATOR_HOST` in the environment — with that variable set, every read and write silently goes to the emulator instead of your project. Expect **HCF-4** to keep real photo analysis failing until it too is repaired; listing creation degrades rather than erroring, which is easy to mistake for success.

## 1.8 RUN THE SPA (OPTIONAL)

The SPA is not required to work on the backend. If you want it:

```bash
cd frontend
npm install
npm start -- --port 3000
```

`npm start` runs **Vite**. Two things to know before you run it:

- **There is no `npm run dev` script.** Earlier documentation instructed one; it has never existed here. The scripts are `start`, `build`, `test`, `lint` and `preview`, and `test` and `lint` both fail immediately because neither `vitest` nor `eslint` is declared as a dependency.
- **Pass the port explicitly.** The repository ships no `vite.config.ts`, so Vite falls back to its own default of `5173` — while `ALLOWED_ORIGINS` defaults to `http://localhost:3000`. Serving on 3000, as above, is what makes the browser's cross-origin requests pass the CORS check.

The dev server starts, and then two things are true that no amount of configuration at your end will change: **the application does not render, and it could not reach this API if it did.** Both are pre-existing, both live in `frontend/`, which the backend change set this guide documents was not authorised to touch, and each is tracked. In brief:

- **Nothing is served to render.** Vite resolves its entry `index.html` from the project root, and this repository keeps it at `frontend/public/index.html` (**NT-15**).
- **`npm install` does not install everything `src/` imports.** `frontend/package.json` declares seven runtime dependencies, and the source imports eight further packages that appear in no manifest: `@stripe/react-stripe-js`, `@stripe/stripe-js`, `browser-image-compression`, `date-fns`, `dompurify`, `formik`, `react-dropzone` and `zod`. Every module that imports one of them fails to resolve, and adding them is a manifest change this backend work was not authorised to make (**NT-34**).
- **The client's module graph does not resolve**, so `tsc` and Vite both fail before any request is built: `createApiInstance` is a module-local `const` in `frontend/src/services/api.ts` and is never exported, yet `services/auth.ts` and `services/payment.ts` import it; `app/utils/auth` and `app/utils/storage` are imported but exist nowhere in the repository; and the `app/…` prefix those imports use is mapped neither in `frontend/tsconfig.json` nor by a bundler alias, because there is no `vite.config.ts` (**NT-30**).
- **Axios never receives a base URL.** `api.ts` reads `process.env.REACT_APP_API_BASE_URL`, and Vite exposes only `VITE_`-prefixed variables, through `import.meta.env` — it puts no `process` in a browser build. With no base, axios resolves every path against the page's own origin, so `/auth/login` would be sent to the dev server rather than to this API (**NT-31**).

**The base a browser client needs is `http://localhost:8000/api`** — the Uvicorn origin from [section 1.7](#17-run-the-api) plus `API_V1_STR`. Under that base the client's `/auth/login`, `/auth/logout` and `/auth/me` compose exactly onto the routes this backend serves; its `/listings` calls still do not, for the reason in [section 3.9](#39-the-doubled-path-segments-are-deliberate). [Section 2.6.2](#262-why-none-of-it-executes-yet) is the full inventory, blocker by blocker, with the configuration a repair has to establish.

## 1.9 VERIFY YOUR CHECKOUT

Four static gates, then one behavioural gate. None of the static gates needs cloud credentials or a running server; the behavioural gate needs neither either, because it runs the application in-process against doubles — it is [section 6](#6-verifying-behaviour-in-process), and it is where every claim in this guide about what the endpoints *do* comes from.

### 1.9.1 Gate 1 — compile every module you changed

The list below is the seven most recently changed — substitute your own files. Silence and exit code 0 is a pass.

```bash
PYTHONPYCACHEPREFIX=/tmp/blitzy-pycache python -m py_compile \
  backend/app/main.py backend/app/core/config.py backend/app/api/auth.py \
  backend/app/api/listings.py backend/app/api/transactions.py \
  backend/app/api/messages.py backend/app/schema/message.py
```

**`PYTHONPYCACHEPREFIX` is there for a reason.** Without it, `py_compile` writes a `__pycache__` directory next to every file it compiles — inside the working tree, in a repository with no `.gitignore` ([section 1.3](#13-create-an-isolated-environment)). Gate 3 below is what would catch that, but only if you read its untracked-file half; sending the bytecode outside the tree means there is nothing to catch. If you prefer no temporary directory at all, compile in memory instead:

```bash
python - <<'EOF'
import pathlib, sys
for name in sys.argv[1:] or [str(p) for p in pathlib.Path('backend/app').rglob('*.py')]:
    compile(pathlib.Path(name).read_text(), name, 'exec')
print('compiled, nothing written')
EOF
```

### 1.9.2 Gate 2 — undefined and unused names, across the whole package

This F-code selection is the project's authoritative static gate; it is what catches the class of defect — a name used but never imported — that previously stopped this backend from importing at all. The gate is the **package**, not a file list:

```bash
python -m flake8 --select=F401,F811,F821,F841 backend/app/
```

**The expected result is one finding.** That is the acceptance criterion the remediation plan for this backend set for this exact command: six findings before the work, one after — a single unused `typing.Optional` import in a file it deliberately did not touch.

**Measured against the delivered tree the command reports ten, so this gate does not pass.** The discrepancy is in the *before* figure, not in the work: run the same selection over the baseline commit and it reports **fifteen**, not six —

```bash
mkdir -p /tmp/baseline
git archive d758168f8b898822b87bc700ddb44a4585abade3 backend/app | tar -x -C /tmp/baseline
(cd /tmp/baseline && python -m flake8 --select=F401,F811,F821,F841 backend/app/ | wc -l)
```

Five of those fifteen were the undefined names that stopped the backend importing — `User` twice in `api/transactions.py`, `User` twice and `firestore` once in `api/messages.py` — and all five are gone. The remaining ten were never counted. Every one of them:

| # | Finding | File:line | Tracked as |
| --- | --- | --- | --- |
| 1 | `F401` unused `typing.Optional` | `backend/app/schema/listing.py:2` | **NT-10** |
| 2 | `F401` unused `typing.Optional` | `backend/app/schema/transaction.py:2` | **NT-10** |
| 3 | `F401` unused `typing.List` | `backend/app/services/ai_vision.py:4` | **NT-10**, in a file frozen by **HCF-4** |
| 4 | `F401` unused `app.core.config.settings` | `backend/app/services/ai_vision.py:5` | **NT-10**, in a file frozen by **HCF-4** |
| 5 | `F841` local `image` assigned and never used | `backend/app/services/ai_vision.py:14` | **HCF-4** — the value the provider call should have used |
| 6 | `F841` local `e` assigned and never used | `backend/app/services/payment.py:46` | **NT-10**, in a file frozen by **HCF-3** |
| 7 | `F841` local `e` assigned and never used | `backend/app/services/payment.py:88` | **NT-10**, in a file frozen by **HCF-3** |
| 8 | `F401` unused `celery.schedules.crontab` | `backend/app/tasks/background_jobs.py:2` | **NT-6** |
| 9 | `F821` undefined name `List` | `backend/app/tasks/background_jobs.py:12` | **NT-6** |
| 10 | `F821` undefined name `List` | `backend/app/tasks/background_jobs.py:34` | **NT-6** |

Every one of the ten sits in a file that the change set which repaired the boot chain was not authorised to modify, so **the gate is reported as blocked rather than rewritten to fit**: the command above stays exactly as it is, the milestone it belongs to is short of its own criterion by nine findings, and it keeps failing until **NT-6** and **NT-10** in [section 5.2](#52-work-worth-picking-up) are authorised and done. Two of the ten are not cosmetic — the `F821` pair at entries 9 and 10 is why `backend/app/tasks/background_jobs.py` cannot be imported at all, so clearing them is a functional repair, not tidying.

Until then, treat those ten as the baseline a clean checkout reports. **An eleventh finding is yours, and so is any `F821` in a file you touched.**

While you are working, the same selection over only your own files is the faster pre-commit check. It is a convenience, **not** this gate, and a clean result from it says nothing about whether the gate above passed:

```bash
python -m flake8 --select=F401,F811,F821,F841 <the files you changed>
```

### 1.9.3 Gate 3 — change containment

Check that you changed only what you meant to change. Run the diff against the commit your work started from; for the change set that repaired this backend, that commit is `d758168f8b898822b87bc700ddb44a4585abade3`:

```bash
git diff d758168f8b898822b87bc700ddb44a4585abade3 --name-status
```

For that change set the output is exactly these nine paths, and nothing else:

```text
M	README.md
M	backend/app/api/auth.py
M	backend/app/api/listings.py
M	backend/app/api/messages.py
M	backend/app/api/transactions.py
M	backend/app/core/config.py
M	backend/app/main.py
A	backend/app/schema/message.py
A	documentation/ONBOARDING.md
```

**Any additional path fails the gate.** When you run the same command against your own branch point, read every line: a path you did not intend to change is either a mistake or a decision you have not written down yet.

**That command is only half the gate, and the missing half is the dangerous one.** `git diff` compares *tracked* content; it says nothing at all about files git has never seen. This repository has no `.gitignore` ([section 1.3](#13-create-an-isolated-environment)), so a `.venv`, a `__pycache__` directory, a scratch harness, an editor swap file, a service-account key or a `.env` you created is **untracked and invisible to the command above** — a clean diff is not a clean tree. Run this too, every time, and treat it as the same gate:

```bash
git status --porcelain --untracked-files=all
```

Every line it prints is either something you meant to add or something that must not be committed. For a change set that is finished and staged, the expected output is exactly the same nine paths with an `M`/`A` in the first column and nothing else; for work in progress, expect your own untracked artefacts and check each one. Patterns you put in `.git/info/exclude` are honoured here as well, which is precisely why that is the right place for them: what the command still prints is then what you have not consciously excluded. `git ls-files --others --exclude-standard` lists only the untracked entries if you want them on their own. Two of the things you will see there matter more than the rest: **a credential file in the tree is a rotation exercise the moment it is committed** ([section 1.5](#15-configure-the-environment)), and the verification script in [section 6](#6-verifying-behaviour-in-process) is meant to live outside the tree for exactly this reason.

### 1.9.4 Gate 4 — style, which is not clean and is not a gate you can pass

There is no Flake8 configuration file anywhere, so a bare `python -m flake8 backend/app/` applies the defaults — including an unusually narrow 79-character line limit — and reports dozens of pre-existing `E501` and `E302` findings in code nobody is being asked to reformat. Do not reformat them, and do not add to them either: keep the lines you write within 79 characters, and put two blank lines before a new top-level `def` or `class`. Comparing the output for a file before and after your change is the quickest way to see that you added nothing.

### 1.9.5 Gate 5 — behaviour

`pytest` cannot serve as this gate: it collects nothing here ([section 3.7](#37-pytest-is-not-a-gate)), and an unstubbed server does not start ([section 3.8](#38-a-clean-boot-still-stops-in-the-payment-module)). What is available is the in-process harness in [section 6](#6-verifying-behaviour-in-process): one script, no credentials, no network, 139 assertions covering the routes, the authentication flow and its policy, the attempt ceilings, both write paths and every input bound, CORS, the settings policy and the logging subsystem. Run it before you claim any behaviour works. When what you changed is the *shape* of a request or a response, follow it with the smoke test in [section 1.7.1](#171-the-local-development-profile) — that is the only check here that puts a real socket, a real HTTP parser and a real JSON body in the path.

## 1.10 BEFORE YOU CALL A CHANGE DONE

[Section 1.9](#19-verify-your-checkout) is what you run before every commit. This section is what you run before you call a change **done**. It is **ten categories and forty-four numbered assertions**, and it is written out in full so that nobody has to re-derive it from prose — the whole point is that two people checking the same change check the same things.

Every assertion here is reproducible on a laptop with no cloud credentials and **no running server**. Nothing in it is aspirational: each one was executed against this checkout and each one passed.

Two sections cover verification, and they are not the same thing. This one is the **checklist** — what has to be true, written out so a reviewer and an author check the same things. [Section 6](#6-verifying-behaviour-in-process) is the **executable** form: one self-contained script that installs the same two stubs, drives the same flows against an in-memory datastore and asserts a hundred and thirty-nine properties in a few seconds. Run section 6 to get an answer; use this section to know what the answer has to cover, including the categories the script deliberately leaves to you.

| # | Category | Assertions | Needs |
| --- | --- | ---: | --- |
| 1 | Compilation | A1 | nothing |
| 2 | F-code lint | A2–A3 | nothing |
| 3 | Import probes | A4–A6 | the stubs in [1.10.1](#1101-the-two-sanctioned-stubs) for A6 |
| 4 | Route and OpenAPI inventory | A7–A9 | stubs |
| 5 | Authentication, and its policy | A10–A22 | stubs + a datastore |
| 6 | Listing write path, and its bounds | A23–A29 | stubs + a datastore |
| 7 | Transaction write path, and its guards | A30–A37 | stubs + a datastore |
| 8 | CORS and settings policy | A38–A41 | stubs for A38–A39 |
| 9 | Log assertions | A42–A43 | stubs + a datastore |
| 10 | Change-set containment | A44 | git |

**Two things are deliberately *not* gates here.** `pytest` collects nothing ([section 3.7](#37-pytest-is-not-a-gate)), and an unstubbed `uvicorn app.main:app` cannot start ([section 3.8](#38-a-clean-boot-still-stops-in-the-payment-module)). Neither can be used, and neither is a substitute for what follows.

**One further property is outside every category above**, because no in-process assertion can reach it: that the routes actually answer over HTTP, through a real socket, a real HTTP parser and a real JSON body. The smoke test in [section 1.7.1](#171-the-local-development-profile) is what establishes it, and it is worth running whenever you change the shape of a request or a response.

**Two of these properties cannot be established in-process at all**, and saying so is part of the protocol rather than a footnote to it: that a registration race resolves to exactly one account, and that a replayed payment intent is refused by a real query. Both depend on datastore behaviour an in-memory double does not model. Verify them against a Firestore emulator, under the fail-closed conditions in [1.10.1](#1101-the-two-sanctioned-stubs), and say which gate you used when you report the result.

### 1.10.1 The two sanctioned stubs

Categories 3 through 9 import `app.main`, which means they need the two out-of-scope third-party constructors replaced. **Stub them in the process, never in the repository** — editing `app/services/payment.py` or `app/services/ai_vision.py` is a different change with its own authorization. Put this at the top of your scratch harness, before any `app.*` import:

```python
import sys, types

_stripe = types.ModuleType('stripe')                 # HCF-3: the installed SDK
class _StripeStub:                                   # exposes StripeClient, not Stripe
    def __init__(self, api_key=None): self.api_key = api_key
_stripe.Stripe = _StripeStub
_err = types.ModuleType('stripe.error')
class _StripeError(Exception): pass
_err.StripeError = _StripeError
_stripe.error = _err
sys.modules['stripe'] = _stripe
sys.modules['stripe.error'] = _err

_vision = types.ModuleType('google.cloud.vision')    # HCF-4: no .image() method
class _ImageAnnotatorClientStub:
    def image(self, content=None): raise RuntimeError('stubbed')
_vision.ImageAnnotatorClient = _ImageAnnotatorClientStub
sys.modules['google.cloud.vision'] = _vision
```

**For the datastore, replace the client with an in-memory double, and do it by default.** That is the whole procedure, and it is the same one [section 3.8](#38-a-clean-boot-still-stops-in-the-payment-module) and [section 6](#6-verifying-behaviour-in-process) publish — one answer, not a choice to make per run:

```python
stack.enter_context(mock.patch('google.cloud.firestore.Client',
                               lambda *a, **k: store))
```

It fails closed. A double cannot reach anything you care about, whereas the variable that points at an emulator (`FIRESTORE_EMULATOR_HOST`) is absent by default, and an unguarded run with it absent falls through to whatever `GOOGLE_APPLICATION_CREDENTIALS` names — which may be a real project. Remove that variable and the credential variable from the harness environment, and assert afterwards that `app.db.firestore.db` **is** your double, before you drive anything.

**Two properties a double cannot model**, and they are exactly the two that matter most: whether a registration race resolves to one account, and whether a query really refuses a replayed payment intent. For those, and only those, use an emulator — under these conditions, all of them:

```bash
gcloud emulators firestore start --host-port=localhost:8080
export FIRESTORE_EMULATOR_HOST=localhost:8080
export GOOGLE_CLOUD_PROJECT="probe-$(date +%s)"   # a throwaway, per run
```

- **Assert the variables before importing anything.** `if not os.environ.get('FIRESTORE_EMULATOR_HOST'): sys.exit('refusing to run: no emulator')`. An emulator run that silently became a production run is the failure this guards.
- **Give every run its own `GOOGLE_CLOUD_PROJECT`.** `app/db/firestore.py` builds its client with `project=settings.GOOGLE_CLOUD_PROJECT`, so a fresh id is a fresh, empty dataset and two runs cannot contaminate each other.
- **Delete what you wrote, in a `finally`.** Stream each collection you touched and delete the documents. An emulator that outlives the run keeps its data.
- **Never point one of these runs at a shared emulator or a real project**, and say which gate you used when you report the result: "verified in-process against doubles" and "verified against an emulator" are different claims.

Drive the app with Starlette's `TestClient` (`from fastapi.testclient import TestClient`). Keep `httpx` below 0.28 — 0.28 removed the `Client(app=…)` shortcut `starlette 0.27` relies on. Use it as a **context manager**, or the startup event never fires and A35 cannot pass.

### 1.10.2 Categories 1–4 — the static and structural gates

| # | Assertion | How |
| --- | --- | --- |
| **A1** | All seven changed modules compile | `python -m py_compile` over them exits 0 ([section 1.9](#19-verify-your-checkout)) |
| **A2** | Those modules report **zero** `F401`/`F811`/`F821`/`F841` | the changed-file `flake8` command in [section 1.9](#19-verify-your-checkout) prints nothing |
| **A3** | The tree-wide F-code baseline is still **ten** findings, none in a file you changed | the package-wide `flake8` command in [section 1.9](#19-verify-your-checkout); compare against the table there |
| **A4** | `Message.__fields__` is exactly `['content', 'id', 'read', 'recipient_id', 'sender_id', 'timestamp', 'vehicle_listing_id']` | `python -c "from app.schema.message import Message; print(sorted(Message.__fields__))"` — no stubs needed |
| **A5** | `app.api.messages` and `app.api.transactions` each import with no `ModuleNotFoundError` and no `NameError` | `python -c "import app.api.messages, app.api.transactions"` with the stubs in place |
| **A6** | `import app.main` succeeds | prints your own marker; this is the assertion that the boot chain is whole |
| **A7** | **13 routes across 9 unique `/api` paths**, and the four auth paths are exactly `/api/auth/register`, `/login`, `/logout`, `/me` | the route-table snippet in [section 2.7](#27-the-routes-as-actually-served) |
| **A8** | The nine pre-existing routes are unchanged, **doubled segments included** — `/api/listings/listings` (GET, POST), `/api/listings/listings/{listing_id}` (GET, PUT, DELETE), `/api/transactions/transactions` (POST), `/api/transactions/transactions/{transaction_id}` (GET), `/api/messages/messages` (GET, POST) | same snippet; aggregate the methods per path before comparing, since each method is its own route object |
| **A9** | The OpenAPI security scheme publishes `tokenUrl: /api/auth/login`, and `GET /openapi.json` returns 200 | `app.openapi()['components']['securitySchemes']['OAuth2PasswordBearer']['flows']['password']['tokenUrl']`, then a `TestClient` GET |

A8 is the one people skip, and it is the one that catches an accidental "tidy" of the doubled paths ([section 3.9](#39-the-doubled-path-segments-are-deliberate)). Assert it every time.

### 1.10.3 Categories 5–7 — the behavioural gates

**Category 5 — authentication and its policy (A10–A22).** The first ten are the flow; the last three are the policy that keeps the flow from being abused.

| # | Request | Expect |
| --- | --- | --- |
| **A10** | `POST /api/auth/register`, fresh email | **201**; body keys exactly `access_token`, `token_type`, `token`, `user`; header `Cache-Control: no-store`; the string `hashed_password` appears **nowhere** in the serialized payload |
| **A11** | the same body again | **409** `Email is already registered`, and the `users` query for that email still returns **one** document |
| **A12** | `POST /api/auth/login`, correct credentials | **200**; `access_token == token`; `user` is an object; header `Cache-Control: no-store` |
| **A13** | login, wrong password | **401** with header `WWW-Authenticate: Bearer` |
| **A14** | login, unknown email | **401** with a detail **byte-identical** to A13 — this is the no-enumeration property |
| **A15** | `GET /api/auth/me` with the token | **200**; `user` holds exactly `id, email, first_name, last_name, role, created_at, updated_at`; header `Cache-Control: no-store` |
| **A16** | `/me` with no `Authorization` header | **401** + `WWW-Authenticate: Bearer` (detail `Not authenticated` — see [section 2.5](#25-authentication-and-tokens)) |
| **A17** | `/me` with `Bearer not-a-real-token` | **401** + `WWW-Authenticate: Bearer` (detail `Could not validate credentials`) |
| **A18** | `POST /api/auth/logout` with the token | **200**, body exactly `{"detail": "Logged out"}` |
| **A19** | decode the issued token with `SECRET_KEY` | claims are exactly `['exp', 'sub']`, `sub` is the user's document id, and the measured lifetime matches `ACCESS_TOKEN_EXPIRE_MINUTES` — **not** the 15-minute fallback |
| **A20** | register with `role: "admin"`, then `"root"`, then `"  "` | **422** every time, and **no** document written. Then register with `role: "Seller"` and read the stored role back: it is `seller`, lower-cased, because every authorization check compares the string exactly |
| **A21** | register with a malformed address, an address over 254 characters, a password of 7 characters, a password over 72 **bytes** once UTF-8 encoded, a blank name, and a name over 100 characters | **422** for each, all of them **before** the duplicate lookup, bcrypt and Firestore, so the write counter never moves. Then register `" Seller@Harness.TEST "` and confirm the stored address is `seller@harness.test`, that `SELLER@harness.test` answers **409**, and that logging in with either spelling answers **200** |
| **A22** | drive `POST /api/auth/login` past ten attempts in one minute, then `POST /api/auth/register` past five | **429** with a `Retry-After` header and a detail that mentions neither the address nor whether it exists; nothing written for the refused registration. A correct sign-in clears its own counters. Registration also writes a `user_emails/{sha256(email)}` marker — pre-create one for an unused address and registration answers **409** with no account written, which is the atomic half of uniqueness |

**Category 6 — listing write path and its bounds (A23–A29).** Patch the module-level name, not the service module: `app.api.listings.analyze_vehicle_photo = my_fake`. That is the call site under test.

| # | Request | Expect |
| --- | --- | --- |
| **A23** | `POST` a listing (as a `seller`) with two photo URLs | **200**; the helper is called **exactly twice**, and **each argument is `bytes`** |
| **A24** | the same with `photos: []` | **200**; the helper is **not** called; the stored `photo_analysis` is `[]` |
| **A25** | the same where the helper raises on **every** photo | **200** — a degraded analysis, never a 500; stored `photo_analysis` is `[]` |
| **A26** | the same where it raises on one of two | **200**, and the one successful result is retained |
| **A27** | `POST` a listing as a `buyer` | **403** `Only sellers can create listings`, and **no** helper call |
| **A28** | `POST` with thirteen photos; with one entry over 256 KiB; with four 250 KB entries; with a blank entry | **422** every time, **zero** helper calls and **zero** writes — the bounds run before the first outbound call, not after it. The record-count, aggregate-maintenance-content and serialized-size guards still answer 422 as well, and the serialized-size case is worth asserting through its log line so it cannot pass by tripping a photo bound instead |
| **A29** | under A25, read the captured log | one `photo analysis failed` record per failed photo, all sharing one `correlation_id`, each carrying `error_type` — and **no traceback and no provider text at `INFO`**. Set the `app` namespace to `DEBUG`, repeat, and both appear |

A23 through A26 also prove there is no `await` in front of the call: awaiting a `dict` would turn every one of them into a 500.

**Category 7 — transaction write path and its guards (A30–A37).** Patch `app.api.transactions.process_payment`. Create the vehicle document yourself — nothing in this codebase creates one (**HCF-7**) — and give each attempt its own `stripe_payment_intent_id` unless the case is deliberately replaying one.

| # | Request | Expect |
| --- | --- | --- |
| **A30** | `POST` a transaction as the buyer, vehicle `status: 'available'` | **200**; the helper receives **exactly three** arguments — the transaction's `stripe_payment_intent_id`, its `amount`, and `'usd'` — and the vehicle document becomes `{'status': 'sold'}` |
| **A31** | the same | the vehicle was looked up by **`vehicle_listing_id`**; no `AttributeError` for a field the schema does not declare |
| **A32** | the helper returns `success: False` | **400**; **no** transaction document written; the vehicle **not** marked sold |
| **A33** | the helper returns a dict with **no** `success` key | **400** — `.get('success')` fails safe where `['success']` would raise `KeyError` |
| **A34** | `POST` a transaction whose `buyer_id` is not the caller | **403**, and **no** payment attempted |
| **A35** | `POST` against a `sold` vehicle, and against a vehicle id that does not exist | **400** both times, and **no** payment attempted |
| **A36** | give the vehicle document a `price` and a `seller_id`, then `POST` terms that match, terms with a different amount, and terms naming a different seller | **200** for the match; **400** for each disagreement, raised **before** the charge, with no payment attempted and the vehicle left `available` |
| **A37** | `POST` again with a `stripe_payment_intent_id` a transaction already records | **409**, **zero** helper calls, and still exactly one transaction document — a retry must not charge a second time |

### 1.10.4 Categories 8–9 — configuration, CORS and logs

| # | Assertion | How |
| --- | --- | --- |
| **A38** | A preflight from an allowed origin returns **200** with `access-control-allow-origin` echoing that origin and `access-control-allow-credentials: true` | `client.options('/api/auth/login', headers={'Origin': 'http://localhost:3000', 'Access-Control-Request-Method': 'POST', 'Access-Control-Request-Headers': 'content-type'})` |
| **A39** | A preflight from an origin outside the list returns **400** with **no** allow-origin header | the same with `Origin: http://evil.test` |
| **A40** | `ALLOWED_ORIGINS` resolves correctly for all six accepted forms and **never** raises `SettingsError` — unset → `['http://localhost:3000']`, one origin, comma-separated → both origins, JSON array → both origins, empty or whitespace → `[]` — while **any** entry containing `*` is refused at import, `'*'` and `'https://*.example.com'` included | set the variable and construct `Settings()` in a fresh subprocess, once per form; see [section 3.4](#34-allowed_origins-takes-two-forms-and-neither-may-crash) |
| **A41** | Unsetting **any one** of the eight required variables still fails validation, and so does a `SECRET_KEY` under 32 characters, an `ACCESS_TOKEN_EXPIRE_MINUTES` of `0` or above 1440, and an `ALGORITHM` outside `HS256`/`HS384`/`HS512` | loop over them, one change at a time, constructing `Settings()` in a subprocess; each iteration must raise |
| **A42** | The startup hook logs exactly `startup complete: firestore and vision clients initialised at import`, and no initializer traceback | attach a `logging.Handler` to the `app` namespace, then enter the `TestClient` context |
| **A43** | Under A25, `photo analysis failed` is logged **once per failed photo**, every record carries a `correlation_id` and an `error_type`, **all records from one request share the same id**, and no traceback appears at `INFO` | inspect the captured records; the request must still be a 200 |

A40 and A41 need **no** stubs and no server — they construct `Settings` in isolation. Run them even when you are only changing configuration.

### 1.10.5 Category 10 — change-set containment

| # | Assertion | How |
| --- | --- | --- |
| **A44** | The change set is exactly the paths the current work authorises, and the working tree holds nothing else at all | `git diff --name-status <base-commit> HEAD` **and** `git status --porcelain`; the second must be empty ([section 1.9.3](#193-gate-3--change-containment)) |

This is a scope gate, not a quality gate, and it is the cheapest one on the list. An extra path in the first output means a frozen file was touched — see [section 5](#5-suggested-next-tasks) for why several of them are frozen and what it takes to unfreeze one. Anything in the second output that you did not put there deliberately is a stray file, and if it looks like a credential, delete it rather than ignore it.

### 1.10.6 What this protocol cannot prove

Say this plainly rather than letting a green run imply more than it earned:

- **No real Cloud Vision or Stripe traffic is exercised.** Categories 6 and 7 verify the **call sites** — arity, types, synchronicity, result handling. The services themselves are stubbed, and both have known defects behind the stub (**HCF-3**, **HCF-4**).
- **Nothing here proves a concurrency property.** A22's marker assertion shows the *mechanism* — a pre-claimed address is refused — but the double serialises everything, so only an emulator run can show that four simultaneous registrations for one address yield one account. Same for a replayed payment intent under real query semantics. Run those separately, as [1.10.1](#1101-the-two-sanctioned-stubs) describes.
- **Rate limiting is proven per process only.** The counters live in module state, so the protocol proves the ceiling and the 429; it cannot prove anything about a deployment running several workers, where each one carries its own counters (**NT-26**).
- **A clean install still serves nothing.** Every category from 3 onwards runs under the stubs. On an unstubbed checkout the import fails ([section 3.8](#38-a-clean-boot-still-stops-in-the-payment-module)), so a fully green protocol and a working deployment are not the same claim.
- **The two messaging endpoints are out of scope for categories 5–7 on purpose.** They are reachable and non-functional (**HCF-8**); asserting their behaviour would only assert a known defect.
- **The frontend is untested here.** The SPA does not render ([section 1.8](#18-run-the-spa-optional)), so the contract in [section 2.6](#26-the-spa-contract--treat-it-as-frozen) is verified by reading the client, not by driving it. Anything that needs a browser — rendering, real preflight behaviour in a browser, the login round trip through the SPA — belongs to whoever can run one, and is not covered by any assertion above.

## 1.11 WHERE THE LOGS GO

`backend/app/main.py` configures logging for this application's own loggers, and it has to. **Uvicorn configures only its `uvicorn*` loggers**, so without that the whole `app.*` namespace inherits root's `WARNING` with no handler attached: an `INFO` record is created and then dropped, and a `WARNING` escapes through `logging.lastResort`, which prints the bare message with no timestamp, no level, no logger name and none of its context. Every audit and diagnostic line this backend emits was invisible for exactly that reason.

What the composition root sets up, once, at import:

| Property | Value |
| --- | --- |
| Namespace | `app` — every logger this application currently creates is named with `logging.getLogger(__name__)`, so all of them inherit it |
| Level | `INFO` |
| Destination | one `StreamHandler`, on standard error, alongside Uvicorn's own output |
| Format | `%(asctime)s %(levelname)s %(name)s: %(message)s`, then the context |
| Context | every field the record carries beyond the standard `LogRecord` attributes — `extra` at the call site is how you add one — appended as `[key=value ...]` in alphabetical order, with backslash, newline, carriage return and tab escaped so a value cannot forge a line. Those four characters only; it is not general control-character sanitisation |
| Propagation | off, so a handler on the root logger cannot print the same record a second time |

A successful start therefore prints exactly one line from the startup hook:

```text
2026-01-01 12:00:00,000 INFO app.main: startup complete: firestore and vision clients initialised at import
```

and a guarded failure prints its context and the class of the failure. Listing creation generates one `correlation_id` per request and attaches it to every line it emits, which is what ties the photo loop and the maintenance loop of a single request together:

```text
2026-01-01 12:00:05,123 WARNING app.api.listings: photo analysis failed [correlation_id=3f2b9c14-... error_type=RuntimeError]
```

**What is not in that line is deliberate.** No traceback, and no text from the provider or the parser. A traceback publishes absolute paths and library internals, and a third-party error message can quote the request that produced it — a document's contents, a URL, an address — so at `INFO` and `WARNING` this application logs *what class of thing went wrong and which request it belonged to*, and nothing more.

The detail still exists. It goes to `DEBUG`:

```text
2026-01-01 12:00:05,124 DEBUG app.api.listings: photo analysis failure detail [correlation_id=3f2b9c14-...]
Traceback (most recent call last):
  ...
RuntimeError: vision provider unavailable
```

so `logging.getLogger('app').setLevel(logging.DEBUG)` is how you get it while you are debugging. **If you turn that on anywhere shared, send it to a sink whose readers are allowed to see request content**, because that is what it contains. Treat `DEBUG` on this namespace as a restricted diagnostic channel rather than a verbosity setting.

**Response headers are a related ownership question, and this application answers only part of it.** The token and profile routes set `Cache-Control: no-store` themselves, because a bearer token must not be written to a shared or on-disk cache. Everything else a browser needs — HSTS, a content-security policy, frame and content-type options, referrer policy, host validation — is **not** set here and belongs to whatever terminates TLS in front of this application, which is also the only component that knows the deployed origin. There is no ingress configuration in this repository to inherit them from, so if you deploy this, that configuration is yours to write.

**Adding context needs no change to the formatter.** `logger.warning("...", extra={"listing_id": listing_id})` renders as `[listing_id=...]`: the formatter prints whatever a record carries beyond the standard `LogRecord` attributes, so there is no key registry to keep in step. Keep using `correlation_id` for the per-request identifier — [section 4.4](#44-call-services-synchronously-and-guard-them) shows the shape — and never put a password, a token, an email address or a payload into a log line or an `extra` value. The attempt-limit records in `app/api/auth.py` are the pattern to copy: they log the *bucket* that filled, never the address or the peer that filled it.

**That last rule binds a client just as hard, and the frozen SPA breaks it** — it logs whole rejected axios errors, which carry the sign-in body and a live bearer token. Nothing in the backend does this; the client-side fix is task **NT-32**, and [section 2.6.2](#262-why-none-of-it-executes-yet) has the line numbers.

**To take the configuration over**, configure the `app` logger yourself *before* `app.main` is imported — and **attach a handler while you do it**, whatever else you change. The guard is `if app_logger.handlers: return`, so an attached handler is what makes the application leave your configuration alone; a level or a propagation flag set on its own attaches nothing and is overwritten. Attach your own handler, point it at a JSON formatter, set whatever level you want on top of that, and none of it is touched.

Every row of the table above, that last guarantee included, is asserted by gate H of the harness in [section 6](#6-verifying-behaviour-in-process) — the escaping, the alphabetical context, the exclusion of standard `LogRecord` fields, the traceback landing beneath the message rather than inside it when a record carries one, the single handler, the `INFO` level, the suppressed propagation, and the fact that configuring twice changes nothing. Gate C asserts the other half, at the call site: that a handler's failure line carries its `correlation_id` and `error_type` and **not** a traceback at `INFO`, and that the same failure at `DEBUG` carries both. If you touch this code, run that gate: none of it is covered by any committed test, because this project has none that collect ([section 3.7](#37-pytest-is-not-a-gate)).

# 2. DOMAIN CONTEXT

## 2.1 THE SHAPE OF THE BACKEND

```mermaid
graph LR
    SPA["React SPA<br/>frontend/src"] -->|"JSON + Bearer token"| M["app/main.py<br/>CORS + 4 routers"]
    M --> A["api/auth.py<br/>register login logout me"]
    M --> L["api/listings.py"]
    M --> T["api/transactions.py"]
    M --> G["api/messages.py"]
    A --> DB[("Firestore<br/>users listings transactions<br/>messages vehicles")]
    L --> DB
    T --> DB
    G --> DB
    L -->|"one call per photo"| V["services/ai_vision.py<br/>Cloud Vision + Pillow"]
    L -->|"one call per record, max 20"| D["services/document_processing.py<br/>PyPDF2"]
    T -->|"token amount currency<br/>after the availability check"| P["services/payment.py<br/>Stripe"]
%% Every service call is synchronous. See section 3.5.
%% The leftmost edge is a contract, not a working integration: the SPA cannot
%% compile or address this API today. See section 2.6.2.
```

Four API modules, each exporting an `APIRouter` named `router`, are mounted by `app/main.py` under `/api/auth`, `/api/listings`, `/api/transactions` and `/api/messages`. Handlers own their HTTP concerns and delegate real work to `app/services/`; persistence goes through the single Firestore client in `app/db/firestore.py`. Request/response bodies are Pydantic **v1** models under `app/schema/`.

## 2.2 ROLES AND WHO MAY DO WHAT

Every user document carries a `role` string. The three values the system recognises are **`buyer`**, **`seller`** and **`admin`**, as specified in [`Technical Specifications.md`](Technical%20Specifications.md) §5.2.

**Registration accepts only the two roles a caller may give itself: `buyer` and `seller`.** The value is trimmed and lower-cased, then checked against that allow-list; anything else — `admin`, `root`, a blank string — is a **422** and the attempt is logged. That check exists because the role is persisted, read back on every request by `get_current_user`, and trusted by the listing-delete branch below, so a role accepted verbatim would have let any anonymous caller make itself an administrator and delete any seller's listing. Case is normalised for a second reason worth knowing: every authorization check compares the stored string exactly, so `role: 'Seller'` stored as written would fail the seller gate on listing creation while looking correctly configured.

**What that leaves open is the other half: there is no way to grant `admin` through this API at all.** No endpoint hands the role out, and nothing audits a grant, so an administrator today means writing the user document directly — which the application neither authenticates nor records. That is task **NT-14** (an authenticated, administrator-only elevation *and revocation* path that writes its own audit record). Until it lands, the direct write is the intended procedure, and it needs controls the code cannot give you:

- Provision administrators only through an **authorized operator channel**: a named service account or human principal with an IAM grant scoped to the `users` collection, never a shared or long-lived key, and never the same credential the running service uses.
- Require the same **change approval** you would require of a deployment — who asked, who approved, which user id, why — and keep that record where your team already keeps change history.
- Keep an **audit trail**. Firestore's own audit logging is the cheapest route: with Data Access audit logs enabled on the project, a direct write is attributable to a principal after the fact, which a bare edit from a developer laptop is not.
- **Grant the minimum.** `admin` confers exactly one privilege in this codebase — deleting another seller's listing — so an over-broad IAM grant buys far more than the role does.
- Remember that a role change takes effect on the **very next request** with an existing token, because `get_current_user` re-reads the document every time ([section 2.5](#25-authentication-and-tokens)). That is what makes revocation by direct write immediate; it is also why an accidental grant is immediate.

Authorization is enforced inline in each handler — there is no shared dependency or decorator for it. These are all of the checks that exist:

| Action | Rule | On violation |
| --- | --- | --- |
| Register | `role` must be `buyer` or `seller` after trimming and lower-casing | 422, and the attempt is logged |
| Create a listing | `role` must be exactly `seller` | 403 `Only sellers can create listings` |
| Update a listing | Caller must be the listing's `seller_id` | 403 |
| Delete a listing | Caller must be the listing's `seller_id` **or** have `role == 'admin'` | 403 |
| Create a transaction | Caller's id must equal the request's `buyer_id`, and the amount and seller must agree with the vehicle record wherever it carries them | 403, or 400 for terms that disagree |
| Read a transaction | Caller must be the transaction's `buyer_id` or `seller_id` | 403 |
| Send or list messages | Authenticated only; no role restriction | 401 if unauthenticated |

`admin` grants exactly one privilege — deleting another seller's listing. There is no admin-only endpoint, and no endpoint that hands the role out at all (**NT-14**).

### 2.2.1 What Registration Accepts

`POST /api/auth/register` is the only unauthenticated write in this API, so `RegisterRequest` in `app/api/auth.py` validates every field it takes. All of it runs as Pydantic v1 validators, which means a bad body is a **422 before** the duplicate lookup, before bcrypt and before Firestore — no work is done for a request that is going to be refused.

| Field | What is checked | Stored as |
| --- | --- | --- |
| `email` | Trimmed and lower-cased, then shape-checked — a local part, an `@`, and a dotted domain with no empty label — and capped at 254 characters, the longest address SMTP carries. `not-an-email` and `a@b..test` are 422s | The canonical form, so ` A@B.com ` and `a@b.com` are the same account rather than two |
| `password` | At least 8 characters, and **at most 72 bytes once UTF-8 encoded**. The byte ceiling is not arbitrary: bcrypt hashes only the first 72 bytes and `passlib` discards the rest in silence, so a longer password would promise strength it does not have and any two sharing a 72-byte prefix would be interchangeable. Refusing is the honest answer | Never stored; only its bcrypt hash is |
| `first_name`, `last_name` | Trimmed, required to be non-blank, and capped at 100 characters | The trimmed value |
| `role` | Trimmed, lower-cased, and required to be `buyer` or `seller` — see [section 2.2](#22-roles-and-who-may-do-what) | The normalised value |

Shape validation is where this stops, and the distinction matters: **nothing proves the address belongs to the person registering it.** There is no verification email and no confirmation step, so an attacker can register an address it does not control and hold the account that a real owner would later expect. Adding that flow is a feature this codebase does not have (**NT-36**), not a validator someone forgot.

Two more properties of this route are worth knowing before you build on it:

- **Uniqueness is atomic, not merely checked.** Registration claims `user_emails/{sha256(canonical_email)}` with a conditional `create()` before it writes the account, so two simultaneous registrations for one address cannot both succeed — the loser gets the same **409 `Email is already registered`** as a sequential duplicate. The pre-write query is still there as the fast path, and it also covers any account created before that marker collection existed. If the account write fails after the claim, the claim is released, so an address is never left unusable with nothing behind it.
- **Both public routes are throttled.** Ten sign-ins and five registrations per fixed one-minute window, counted per client address and per account, answering **429** with a `Retry-After` header and a message that says nothing about whether the account exists. A correct sign-in clears its own counters. The counters live in the worker process, so several workers multiply the ceiling — a cluster-wide limit needs a shared store or a gateway policy (**NT-26**).

`LoginRequest` validates the address only enough to canonicalise it — trimmed and lower-cased, so the same account signs in whatever the spelling — and **deliberately does not shape-check it or bound the password.** Nothing about the *credential* is judged before `authenticate_user` runs, which is what keeps every credential rejection uniform: an unknown email and a wrong password are both **401 `Incorrect email or password`** with a `WWW-Authenticate: Bearer` header, so the response cannot be used to enumerate accounts. A 422 on a sign-in attempt would leak exactly what the 401 is careful not to.

That uniformity now extends to timing, which is where a matching response can still give an answer away. `authenticate_user` returns as soon as its query comes back empty, so an unknown address used to answer measurably faster than a known one with the wrong password. The failure path therefore spends one deliberate bcrypt verification against a throwaway hash when no account holds the address, and discards the result. Both branches cost the same work.

**Two different rejections live at this route, and they are easy to confuse.** FastAPI validates the request body before `login()` is entered, so a body that is not *shaped* like a login attempt never reaches the credential check at all:

| Request | Answer | Rejected by |
| --- | --- | --- |
| No body; `{}`; only `email`; only `password`; `null` for either; a value no coercion can turn into a string; malformed JSON; a form-encoded body | **422**, listing the offending `body/…` locations, with **no** `WWW-Authenticate` header | FastAPI request validation, before the handler |
| Both fields present and coercible to strings — including `""`, whitespace, an unknown email, a wrong password, or a number Pydantic v1 coerces to `str` | **401 `Incorrect email or password`** with `WWW-Authenticate: Bearer` | `authenticate_user`, inside the handler |
| More than ten attempts from one client, or against one address, inside a minute | **429** with `Retry-After`, and a detail that mentions neither the address nor its existence | the attempt limiter, before the credential check |

Adding validators to force those 422s into 401s was considered and rejected: it would hide malformed-request bugs from clients that are simply posting the wrong shape, and the enumeration argument does not apply — a 422 names a *field of the request*, never whether an account exists. What must stay uniform is the credential answer, and it is.

## 2.3 FIRESTORE COLLECTIONS

Firestore is schemaless: collections exist because code writes to them, and nothing enforces field presence, types or uniqueness. The documented models live in [`Technical Specifications.md`](Technical%20Specifications.md) §5.2 and are not repeated here — what follows is what the code actually does.

| Collection | Written by | Read by | Notes |
| --- | --- | --- | --- |
| `users` | `app/api/auth.py` (register) | `app/api/auth.py` (authenticate, resolve token subject), `app/api/messages.py` (recipient exists) | Document id is also stored in the document’s own `id` field. The email is stored in canonical form — trimmed and lower-cased — so one address is one account. Firestore still has **no unique index on `email`**; uniqueness comes from the `user_emails` marker below plus the pre-write duplicate query. |
| `user_emails` | `app/api/auth.py` (register claims a marker; releases it if the account write fails) | nothing reads it except registration itself, and the sign-in path to decide whether an address is claimed | One document per registered address, its id the SHA-256 digest of the canonical address — a fixed-length legal Firestore id that carries no readable address. It exists because a query cannot make uniqueness atomic and a conditional `create()` can: two simultaneous registrations for one address cannot both succeed. Nothing here is user data beyond the account id it points at. |
| `listings` | `app/api/listings.py` | `app/api/listings.py` | Stores the listing plus derived `photo_analysis` and `maintenance_data`. |
| `transactions` | `app/api/transactions.py` | `app/api/transactions.py` | Written only after payment succeeds, with `status: 'completed'` and its own document id in `id`. It **is** read before charging, by `stripe_payment_intent_id`, so a replayed intent answers 409 instead of paying twice; what remains unguarded is two genuinely simultaneous requests, which need a reservation and a provider-level idempotency key — **HCF-14**. |
| `messages` | `app/api/messages.py` | `app/api/messages.py` | The handler writes `message.dict()` straight in, so the bound lives on the model: `Message.content` is trimmed, required to be non-blank, and capped at 4000 characters and 16 KiB encoded, which is what keeps an unbounded body out of Firestore. A client-supplied `id` or `read` flag is still stored as sent, because the same model is rebuilt from stored documents on the read path and a coercing validator would discard server values too — that one waits for the route logic (**HCF-8**). Reachable but not yet functional. |
| `vehicles` | `app/api/transactions.py` (updates `status` only) | `app/api/transactions.py` | Read to check `status == 'available'` **and to settle the purchase's terms** — where the document carries a `price` or a `seller_id`, the request must agree with it — then updated to `'sold'` on a successful purchase. What is missing is a **creator**: **no module ever creates or populates a vehicle document**, so a purchase can only be tested against a document you write yourself, and a document without a price is why the terms check can only warn rather than refuse. See **HCF-7** and **HCF-16**. |

Note that a listing lives in `listings` while the availability check reads `vehicles`. That gap is the substance of **HCF-7**, not an accident of naming, and resolving it is a data-model decision rather than a bug fix.

## 2.4 INTEGRATION POINTS

| Integration | Entry point | Called from | Shape |
| --- | --- | --- | --- |
| Cloud Vision | `analyze_vehicle_photo(image_data: bytes)` | `app/api/listings.py`, once per photo, inline, **capped at 12 photos and 900 KiB per request** ([section 2.8](#28-what-the-write-endpoints-bound)) | Synchronous; returns a `dict` of extracted vehicle details. This is the only module that imports **Pillow**, which it uses to open the image bytes before the Vision call |
| PyPDF2 | `process_maintenance_document(document_data: bytes, document_type: str)` | `app/api/listings.py`, once per record, at most `_MAX_MAINTENANCE_RECORDS` (20) per request | Synchronous; returns a `dict`. PDFs are parsed with `PyPDF2.PdfReader`; the module imports no imaging library |
| Stripe | `process_payment(token: str, amount: float, currency: str)` | `app/api/transactions.py`, once per request, after the availability check | Synchronous; returns a `dict` with a `success` key. Validates `currency` against `usd`, `eur`, `gbp` |
| Cloud Storage | `upload_file`, `delete_file`, `get_file_url` | **nothing** | The module is complete but has no importer; photo upload is not wired to it |

**Every one of these is a plain `def`.** None may be awaited. That rule has its own pitfall entry — [section 3.5](#35-never-await-a-service-function-and-never-read-a-dict-by-attribute) — because violating it was one of the defects this codebase was just repaired for.

**Because they are blocking, the handler that calls them has to be a plain `def` — and six of the thirteen now are.** Starlette runs a synchronous endpoint in its threadpool, so a slow Vision or Stripe call occupies one worker thread instead of the event loop every other request shares; inside an `async def` a single 1-second payment call delays every concurrent request by the full second. The six that get it right are the four authentication handlers plus **`create_listing` and `create_transaction`** — which were the two that mattered most, because between them they make every Cloud Vision, PyPDF2 and Stripe call in the request path. The remaining seven are still `async def` bodies doing synchronous I/O directly on the event loop: `get_listings`, `get_listing`, `update_listing`, `delete_listing`, `get_transaction` and both message routes. Converting those is task **NT-22**; it changes handler signatures that are frozen for the current work, so it needs authorization rather than initiative. One consequence of the conversions already done is recorded in [section 4.1](#41-add-a-router) and worth reading before you do another: a `SIGALRM`-based timeout does not fire off the main thread, so `document_processing.py`'s wall-clock guard on PDF extraction no longer applies to `create_listing` — the byte and record ceilings in [section 2.8](#28-what-the-write-endpoints-bound) are what bound that work now.

## 2.5 AUTHENTICATION AND TOKENS

Tokens are **HS256 JWTs** signed with `SECRET_KEY`, carrying **exactly two claims**:

| Claim | Value |
| --- | --- |
| `sub` | The Firestore document id of the user |
| `exp` | Expiry, computed as `datetime.utcnow()` plus `ACCESS_TOKEN_EXPIRE_MINUTES` |

There is no `iat`, no `jti`, no role claim and no refresh token. Authorization is resolved per request by loading the user document named by `sub`, so a role change takes effect on the next request rather than on the next login.

`ACCESS_TOKEN_EXPIRE_MINUTES` is honoured on every token issued. This is worth stating because the setting had no reader at all until the login path added one, and `create_access_token`'s hard-coded 15-minute fallback governs any caller that omits `expires_delta` — so the first token-issuing path written without that argument would have handed a deployment configured for 60 minutes a 15-minute token. Nothing was ever observed doing it, because no route minted a token at all; the gap was latent, not live. The login path passes the configured lifetime explicitly. The fallback still exists in `create_access_token` for callers that supply no lifetime — **if you add a token-issuing path, pass the lifetime explicitly**, exactly as `_issue_token` in `app/api/auth.py` does.

There is one more way that fallback could bite, and configuration closes it: `create_access_token` tests `if expires_delta:`, and `timedelta(minutes=0)` is falsey, so a lifetime of `0` would have been read as "unset" and silently become 15 minutes. `Settings` therefore refuses a lifetime below 1 or above 1440 minutes ([section 1.5.1](#151-every-setting-and-what-actually-reads-it)), which makes the configured value the only one that can govern. The ceiling exists because of the section immediately below: nothing here can withdraw a token early, so its lifetime is the whole of its blast radius.

Responses that carry a token or a profile — all four authentication routes — are sent with `Cache-Control: no-store`, so a bearer token is not written to a shared or on-disk cache. That is the only response header this application sets for itself; the browser and transport policy headers belong to whatever terminates TLS in front of it ([section 1.11](#111-where-the-logs-go)).

A rejected request yields **401** with a `WWW-Authenticate: Bearer` header, but **the body depends on which of two guards rejected it**, and they are different pieces of code:

| Request | Rejected by | Detail |
| --- | --- | --- |
| No `Authorization` header at all | `OAuth2PasswordBearer` (`fastapi.security`), before any handler code runs | `Not authenticated` |
| An `Authorization` header that is not a Bearer header — `Basic …`, a bare token, any other scheme | The same, for the same reason: it looks for the `Bearer` scheme and finds something else | `Not authenticated` |
| `Bearer <token>` where the token is malformed, unsigned, signed with another key, or expired | `get_current_user`, when `jose` raises `JWTError` | `Could not validate credentials` |
| `Bearer <token>` that is valid but whose `sub` names no user document | `get_current_user`, after the Firestore lookup misses | `Could not validate credentials` |
| `Bearer ` with an empty token | `get_current_user` — the scheme matched, so the empty string is passed on and fails to decode | `Could not validate credentials` |

The practical consequence: **`Not authenticated` means the header never arrived in a form the framework recognised; `Could not validate credentials` means a bearer token arrived and was rejected on its merits.** When a client reports the first, look at how it is attaching the header, not at the token. Unifying the two bodies would mean constructing the scheme with `auto_error=False` and handling the missing-token case in `get_current_user` — a change to the frozen dependency, so it has not been made.

Registration and login are the only two authentication routes that answer anything other than 401 on rejection. Register answers **409** on a duplicate email, or **422** when a field is absent or holds a value no coercion can turn into a string — that is the whole of what it rejects ([section 2.2.1](#221-what-registration-accepts)).

**Login has two different rejections and they are easy to confuse.** FastAPI validates the body before `login()` is entered, so a request that is not *shaped* like a login attempt never reaches the credential check:

| Request | Answer | Rejected by |
| --- | --- | --- |
| No body; `{}`; only `email`; only `password`; `null` for either; a value no coercion can turn into a string; malformed JSON; a form-encoded body | **422**, listing the offending `body/…` locations, with **no** `WWW-Authenticate` header | FastAPI request validation, before the handler |
| Both fields present and coercible to strings — including `""`, whitespace, an unknown email, a wrong password, or a number Pydantic v1 coerces to `str` | **401 `Incorrect email or password`** with `WWW-Authenticate: Bearer` | `authenticate_user`, inside the handler |

Forcing those 422s into 401s was considered and rejected: it would hide malformed-request bugs from clients that are simply posting the wrong shape, and the enumeration argument does not apply — a 422 names a *field of the request*, never whether an account exists. What must stay uniform is the credential answer, and it is.

Passwords are hashed with bcrypt through `passlib`'s `CryptContext`. Hashes are stored on the user document as `hashed_password` and are **never returned by any endpoint** — see [section 4.5](#45-projection-discipline-there-is-no-response_model).

### 2.5.1 Logout Is Stateless

`POST /api/auth/logout` returns **200** and does nothing else. There is no denylist, no revocation list, no server-side session and no token store anywhere in this codebase, and the token carries no identifier that could be revoked. **The JWT remains valid until `exp`, including after a successful logout.** You can observe this directly: call `/api/auth/me` with the same token after logging out and it still answers 200.

The route exists because the SPA posts to it and clears its stored token in a `finally` block whatever the response — before this route existed nothing matched that post, so a 404 is what every sign-out would have met as soon as either side could run. Its value is that the client's request succeeds. (That `finally` clears the token through a storage helper the client imports and does not have, so today it cannot run at all — **NT-30**, [section 2.6.2](#262-why-none-of-it-executes-yet). The route's contract is unaffected.) **Do not describe or rely on this endpoint as revocation.** Real revocation is task **HCF-10**.

### 2.5.2 What The Public Routes Do And Do Not Check

`POST /api/auth/register` and `POST /api/auth/login` are the only two unauthenticated routes, so what they check is worth reading as a list rather than inferring from the code. [Section 2.2.1](#221-what-registration-accepts) has the detail; this is the summary, and the second half is the part to plan around.

| Enforced | How |
| --- | --- |
| A role allow-list | `buyer` or `seller` only, trimmed and lower-cased; anything else is a 422 and is logged |
| Address shape and normalisation | trimmed, lower-cased, syntax-checked, capped at 254 characters — so one address is one account |
| A password rule | at least 8 characters, at most 72 **bytes** encoded, because bcrypt silently ignores the rest |
| A name rule | trimmed, non-blank, at most 100 characters |
| Atomic uniqueness | a `user_emails/{sha256(email)}` marker claimed with a conditional `create()` before the account is written, and released if that write fails |
| Attempt limits | 10 sign-ins and 5 registrations per minute, per client address and per account, answering 429 with `Retry-After` and a message that reveals nothing |
| Uniform credential rejection | one 401 body for an unknown address and for a wrong password, and one deliberate bcrypt verification on the unknown-address path so the two cost the same |

| Not enforced | What that permits today | Tracked as |
| --- | --- | --- |
| Proof that the address belongs to the registrant | anyone can register an address they do not control, and hold the account its real owner would expect | **NT-36** |
| A cluster-wide attempt limit | the counters are per worker process, so N workers permit N times the ceiling | **NT-26** |
| Any limit on what an account may then create | one account may write unlimited listings and messages, each bounded individually but unbounded in number | **NT-25** |
| A privacy lifecycle for what registration stores | an address, both names, a role and a hash are persisted with no retention rule, no export path and no deletion path | **NT-37** |
| A grant path for `admin` | the role cannot be obtained through the API at all, so an administrator means a direct database write the application neither authenticates nor audits | **NT-14** |

## 2.6 THE SPA CONTRACT — TREAT IT AS FROZEN

The React client is already written against a specific contract, and the backend was shaped to satisfy it rather than the reverse. **Read this section as two claims, because only one of them is operational.** The wire shapes below are what the client's source expresses; they are frozen, and changing any of them breaks it. Whether the client can *execute* them is a separate question, and today the answer is no — [section 2.6.2](#262-why-none-of-it-executes-yet) sets out every reason. **Nothing described here has ever been observed over a real HTTP request**, from this repository or anywhere else; it was established by reading the client, and the assertions that back it ([section 6](#6-verifying-behaviour-in-process)) drive the backend directly, not through the SPA.

Changing any of the following breaks the SPA, whenever it starts working:

- **Login takes a JSON body**, `{"email": "...", "password": "..."}` — *not* an OAuth2 form post. The client sends JSON, so the route binds a request model. One consequence: Swagger's "Authorize" control, which submits a form, cannot complete the password flow (**HCF-9**). Get a token by calling `POST /api/auth/login` directly and paste it as a bearer header.
- **Login and register return four keys**: `access_token`, `token_type`, `token` and `user`. `access_token` and `token` hold the same string — the first honours the OAuth2 convention, the second is the one the client actually reads. It raises `Login failed: Invalid response from server` if a top-level `token` is missing.
- **`GET /api/auth/me` returns `{"user": {...}}`**, nested, because the client reads `response.data.user`. A flat body hands it `undefined`.
- **The public user projection is exactly seven fields**: `id`, `email`, `first_name`, `last_name`, `role`, `created_at`, `updated_at`. `hashed_password` appears in no response, anywhere.
- **Protected requests are meant to carry `Authorization: Bearer <token>`**, attached by the request interceptor in `frontend/src/services/api.ts`. The interceptor is written; it cannot run, because it reads the token through a `getAuthToken` imported from a module that does not exist (**NT-30**).
- The client reads its base URL from **`process.env.REACT_APP_API_BASE_URL`** — an expression Vite never satisfies, so no base reaches axios (**NT-31**). The value it needs is **`http://localhost:8000/api`**. Note also that `infrastructure/docker/docker-compose.yml` sets a differently named `REACT_APP_API_URL`, so even the name that is read is not the name that is set.

The client has **no register function**, so registration is API-only today and no client contract constrains it. **Its request authority is `RegisterRequest` in `app/api/auth.py`** — `email`, `password`, `first_name`, `last_name`, `role`, all plain required strings. That field set was *derived* from the `User` schema, as the model's own comment records, but the two are not the same shape: `RegisterRequest` adds `password`, which `User` never holds, and omits `id`, `created_at`, `updated_at` and `hashed_password`, every one of which the handler assigns itself. Post the `User` field list and you get a 422 for the missing `password`, with the four server-assigned fields you supplied silently ignored. What the route accepts, and the little it checks, is in [section 2.2.1](#221-what-registration-accepts).

### 2.6.1 What The Client Calls, And What Answers It

Seven calls, in the three modules that reach this API. The paths are the client's own — relative to whatever base it is given — so read each row as "base + path":

| Client call | What it sends and reads | This backend serves | Verdict |
| --- | --- | --- | --- |
| `POST /auth/login` — `services/auth.ts:12` | JSON `{email, password}`; reads `data.token` and `data.user`, and stores the token under `authToken` | `POST /api/auth/login` | **Shapes match exactly.** This route exists because of this call |
| `POST /auth/logout` — `services/auth.ts:25` | No body; bearer header; clears the stored token in a `finally` whatever the answer | `POST /api/auth/logout` | **Shapes match.** The 200 is an acknowledgement only ([section 2.5.1](#251-logout-is-stateless)) |
| `GET /auth/me` — `services/auth.ts:36` | Bearer header; reads `data.user` | `GET /api/auth/me` | **Shapes match.** Nested `user`, exactly seven fields |
| `GET /listings` — `services/api.ts:34` | `filters` as query parameters; reads an array | `GET /api/listings/listings` | **Path mismatch.** The client's single-segment path is not served — **HCF-6** |
| `POST /listings` — `services/api.ts:40` | A listing body; reads an object | `POST /api/listings/listings` | **Path mismatch**, same cause — **HCF-6** |
| `POST /upload` — `services/api.ts:48` | `multipart/form-data` with the file under the field name `photo`; reads a string | **Nothing.** No route serves `/upload` under any prefix | **No such endpoint** — **NT-33** |
| `POST /payments/create-intent` — `services/payment.ts:21` | JSON `{amount, currency}`; reads `data.clientSecret` | **Nothing.** No payment-intent route exists; the backend charges inside `POST /api/transactions/transactions` | **No such endpoint** — **NT-33** |

The first three rows are why three of the four authentication routes exist and are shaped as they are; `register` has no client call at all, and takes its shape from the specification and the `User` schema instead. The last four rows are pre-existing client debt that no backend change here touched: two are the doubled-segment divergence, and two name endpoints this API has never had.

### 2.6.2 Why None Of It Executes Yet

Six blockers, all of them in `frontend/`, all pre-existing, none of them repaired by the backend work this guide documents — `frontend/` is outside its authorised change set, so each is recorded rather than fixed. This is the inventory of what stops the *client-to-API seam*; it is not a full audit of the SPA, and **NT-15** carries the further identity and entry-point gaps. The first five are listed in the order a build hits them; the sixth is not a failure at all but a disclosure:

| # | Blocker | Where | What it prevents |
| --- | --- | --- | --- |
| 1 | `createApiInstance` is a module-local `const` and is never exported; the module exports only `fetchListings`, `createListing` and `uploadPhoto` | declared `services/api.ts:6`; imported at `services/auth.ts:1` and `services/payment.ts:2` | The auth and payment clients cannot obtain the shared client, so neither module compiles |
| 2 | `app/utils/auth` and `app/utils/storage` do not exist — there is no `frontend/src/app/` directory, and no `utils/auth.*` or `utils/storage.*` anywhere in the tree | imported at `services/api.ts:2` and `services/auth.ts:2` | `getAuthToken` cannot be resolved, so the bearer interceptor cannot read a token; `setItem`/`removeItem` cannot be resolved, so login cannot store one and logout cannot clear one |
| 3 | The `app/…` prefix is mapped nowhere. `frontend/tsconfig.json` sets `baseUrl: "src"` and maps only `@components/*`, `@pages/*`, `@utils/*`, `@styles/*`, `@hooks/*` and `@context/*`; there is no `vite.config.ts`, so no bundler alias exists either | `frontend/tsconfig.json:18-25` | Even if the two modules above were written, nothing would resolve the import specifier that names them. The same gap applies to the `@/…` prefix the pages and components use |
| 4 | The API base is read as `process.env.REACT_APP_API_BASE_URL`. `REACT_APP_` is a Create-React-App convention; Vite exposes only `VITE_`-prefixed names, through `import.meta.env`, and supplies no `process` in the browser | `services/api.ts:4`, and the same pattern for `REACT_APP_STRIPE_PUBLIC_KEY` at `services/payment.ts:4` | No base reaches axios: with no `process` the read throws `ReferenceError: process is not defined`, and where something has shimmed `process.env` it yields `undefined` instead. Either way axios resolves every path against the page's own origin, so a login goes to the dev server. Exporting the variable does not help — nothing reads it |
| 5 | Eight packages that `frontend/src` imports are declared in no manifest: `@stripe/react-stripe-js`, `@stripe/stripe-js`, `browser-image-compression`, `date-fns`, `dompurify`, `formik`, `react-dropzone`, `zod` | `frontend/package.json` declares seven runtime dependencies; the imports sit in `src/index.tsx`, four `src/components/`, four `src/schema/`, three `src/utils/` and two `src/services/` modules | `npm install` cannot install them, so every module importing one fails to resolve for `tsc` and for Vite alike. Declaring them is a manifest change, which needs the same authorisation as any other frontend edit (**NT-34**) |
| 6 | The response interceptor logs the whole rejected axios error, and the auth layer logs some of them again | `services/api.ts:24`; `services/auth.ts:27` and `:39` | Not a compile failure, a disclosure: that object carries `config.data`, which on a sign-in is the `{email, password}` body, and `config.headers.Authorization`, which is a live token ([section 1.11](#111-where-the-logs-go)) |

Rows 1 to 3 are task **NT-30**, row 4 is **NT-31**, row 5 is **NT-34**, row 6 is **NT-32**. **Rows 1 to 5 have to be settled together**: the first three and the fifth are what make the client compile, and without the fourth a compiling client still addresses the wrong host. What a repair has to establish, stated exactly — **none of this is in the repository, and writing it is a frontend change that needs authorisation first**:

```text
new   frontend/vite.config.ts        resolve.alias: { app: <absolute path to frontend/src> }
edit  frontend/tsconfig.json         paths: { "app/*": ["*"] }   -- baseUrl is already "src"
edit  frontend/src/services/api.ts   export const createApiInstance = …
                                     const API_BASE_URL = import.meta.env.VITE_API_BASE_URL
new   frontend/src/utils/auth.ts     getAuthToken() reads the 'authToken' key
new   frontend/src/utils/storage.ts  setItem / getItem / removeItem on that same key
new   frontend/.env.local            VITE_API_BASE_URL=http://localhost:8000/api
```

Two details decide whether that actually works. **One key name, everywhere:** `services/auth.ts` writes and clears `authToken`, so whatever supplies `getAuthToken` must read that exact key or the interceptor will never find the token login just stored. **Both resolvers, not one:** `tsc` reads `tsconfig.json` and Vite reads its own config, so mapping the prefix in only one of them leaves either a type error or a runtime resolution failure.

With all of that in place, `http://localhost:8000/api` + `/auth/login` is `/api/auth/login`, and the three authentication rows of [section 2.6.1](#261-what-the-client-calls-and-what-answers-it) work. The listing rows still do not (**HCF-6**), the two unmatched endpoints still 404 (**NT-33**), and nothing is reachable at all until **HCF-3** lets a server start ([section 3.8](#38-a-clean-boot-still-stops-in-the-payment-module)). CORS is the one part already aligned: `ALLOWED_ORIGINS` defaults to `http://localhost:3000`, which is the origin the SPA is served from when you pass the port as [section 1.8](#18-run-the-spa-optional) shows.

## 2.7 THE ROUTES AS ACTUALLY SERVED

Thirteen routes registered across nine unique paths, eleven of them working ([section 3.10](#310-messaging-is-reachable-but-not-functional)). The OpenAPI security scheme publishes `tokenUrl: /api/auth/login`.

| Method | Served path | Router prefix + decorator | Bearer token |
| --- | --- | --- | --- |
| POST | `/api/auth/register` | `/api/auth` + `/register` (201) | no |
| POST | `/api/auth/login` | `/api/auth` + `/login` | no |
| POST | `/api/auth/logout` | `/api/auth` + `/logout` | **yes** |
| GET | `/api/auth/me` | `/api/auth` + `/me` | **yes** |
| GET | `/api/listings/listings` | `/api/listings` + `/listings` | no — **public read** |
| POST | `/api/listings/listings` | `/api/listings` + `/listings` | **yes**, and `role` must be `seller` |
| GET | `/api/listings/listings/{listing_id}` | `/api/listings` + `/listings/{listing_id}` | no — **public read** |
| PUT, DELETE | `/api/listings/listings/{listing_id}` | `/api/listings` + `/listings/{listing_id}` | **yes**, plus ownership ([section 2.2](#22-roles-and-who-may-do-what)) |
| POST | `/api/transactions/transactions` | `/api/transactions` + `/transactions` | **yes** |
| GET | `/api/transactions/transactions/{transaction_id}` | `/api/transactions` + `/transactions/{transaction_id}` | **yes** |
| POST, GET | `/api/messages/messages` | `/api/messages` + `/messages` | **yes** |

The four authentication paths match [`Technical Specifications.md`](Technical%20Specifications.md) §5.3 exactly.

**Two of the thirteen operations need no token at all.** `GET /api/listings/listings` and `GET /api/listings/listings/{listing_id}` declare no `current_user` dependency, so a browse is open to anyone — deliberate for a marketplace, and worth knowing before you assume a 401 protects a read. Every other operation except register and login rejects an unauthenticated caller with 401 ([section 2.5](#25-authentication-and-tokens)).

**The repeated segments in the other five paths are real.** Each router is mounted under a prefix that already names its area, while its own decorators repeat that segment — so the served path is `/api/listings/listings`, not `/api/listings`. The specification documents the single-segment form, and the SPA calls the single-segment form. **Do not tidy this.** The paths are frozen so that no client contract changes silently as a side effect of unrelated work; reconciling them is a deliberate, breaking change tracked as **HCF-6** and covered again in [section 3.9](#39-the-doubled-path-segments-are-deliberate).

See [section 4.2](#42-mount-it-in-mainpy) for how a prefix and a decorator concatenate into a served path.

To read the two halves off disk — which works today, needs no imports and no server, and shows exactly thirteen decorators against four prefixes:

```bash
grep -n "include_router" backend/app/main.py
grep -rn "^@router\." backend/app/api/
```

Asking the application itself is authoritative, and it needs one thing the two `grep`s do not: the stand-in for the third-party import that stops a bare `python -c "import app.main"` ([section 3.8](#38-a-clean-boot-still-stops-in-the-payment-module)). The harness in [section 6](#6-verifying-behaviour-in-process) supplies it, so its first gate is exactly this question, asked of the composed application and answered as assertions rather than as output you have to eyeball:

```text
PASS 13 routes are registered
PASS across 9 unique paths
PASS the four auth operations are exactly as documented
PASS the nine pre-existing operations keep their served paths
PASS OpenAPI publishes tokenUrl /api/auth/login
PASS GET /openapi.json answers 200
```

The fourth of those is the regression guard for this table: it compares the nine non-auth operations against the literal list above, so a prefix "tidied" by accident fails the gate instead of silently changing a client contract.

## 2.8 WHAT THE WRITE ENDPOINTS BOUND

Two facts make bounds a correctness concern here rather than a nicety: **a Firestore document may not exceed 1 MiB**, and handlers write `model.dict()` straight into a collection, so an unbounded field is an unbounded document. Every external call these endpoints make is also blocking, which makes **where each bound sits** as important as whether it exists.

`create_listing` runs in this order: the seller-role gate, then the **photo bounds**, then the photo loop — one blocking Cloud Vision call per entry — then the maintenance record-count check and the maintenance loop it guards, then assembly, then the serialized-size check, then the Firestore write. Read two consequences off that order: **no rejected request ever writes to Firestore**, because every guard precedes the write; and **no rejected request pays for a provider call either**, because the photo bounds sit above the loop rather than below it. A 422 from the maintenance or serialized-size check still arrives after the Vision calls for that request, which is why the photo bounds are deliberately tighter than the document ceiling.

| Endpoint | Bound | Answer when exceeded | Enforced by |
| --- | --- | --- | --- |
| `POST /api/listings/listings` | At most **12 photos** (`_MAX_PHOTOS`), at most **256 KiB per entry** (`_MAX_PHOTO_BYTES`), at most **900 KiB across all of them** (`_MAX_PHOTO_TOTAL_BYTES`), and no blank entry. Measured on the **encoded** payload, which is what is handed to the provider | 422 `Unable to process vehicle photos` | The handler, **before the first Cloud Vision call**. The response names no bound; the log line does, keyed to the request's `correlation_id` |
| `POST /api/listings/listings` | At most **20 maintenance records** (`_MAX_MAINTENANCE_RECORDS`), and at most **900 KiB of decoded content in aggregate** across them (`_MAX_MAINTENANCE_CONTENT_BYTES`). A record whose `content` is **present but empty**, or of a type that is neither `str`, `bytes` nor `bytearray`, is rejected — but a record with **no `content` key at all, or `content: null`, is skipped silently** rather than rejected, and contributes nothing to `maintenance_data` | 422 `Unable to process maintenance documents` | The handler; the aggregate is checked as it accumulates, so it stops at the record that crosses the line |
| `POST /api/listings/listings` | The serialized listing — request fields plus the derived `photo_analysis` and `maintenance_data` — must stay under **1,000,000 bytes** (`_MAX_SERIALIZED_LISTING_BYTES`) | 422 `Listing payload is too large to store` | The handler, after assembly and **before** the Firestore write |
| `POST /api/messages/messages` | `content` is trimmed, must be **non-blank**, and must be at most **4000 characters** and **16 KiB encoded** | 422, raised by the model before the handler is entered | `app/schema/message.py` |
| `POST /api/auth/register` | Address shape and length, password length in characters **and** bytes, name length, role allow-list — [section 2.2.1](#221-what-registration-accepts) | 422, raised by the model before the handler is entered | `app/api/auth.py`'s `RegisterRequest` |

**What is still unbounded is quantity, not size.** Nothing limits how many listings or messages one account may create, and no list endpoint pages its results — `GET /api/listings/listings` and `GET /api/messages/messages` both stream whole collections ([NT-20](#52-work-worth-picking-up)). A caller who is willing to make many small, individually valid requests can still grow a response until it is expensive to serve, and creation quotas are the missing half of that ([NT-25](#52-work-worth-picking-up)).

Three things to carry from the table:

- **Where a bound lives changes what it protects.** A bound on the model rejects the request before your handler exists, which is the right home for a plain field like `Message.content`; a bound in the handler is the only option when the value is derived, as the serialized-listing check is, or when it must precede an outbound call, as the photo bounds do.
- **These are byte bounds, not length bounds.** `len()` on a `str` under-counts every non-ASCII character, so what is checked is always the size of the encoded payload that is actually handed on. Copy that when you add one.
- **The 1 MiB ceiling is Firestore's, not ours.** An over-size document that slips past these checks is rejected by Firestore with an error no handler here catches — which is why the serialized check exists at all, and why any new field you add to a written model needs one too ([section 4.3](#43-add-a-schema)).

## 2.9 THE PURCHASE PATH

`POST /api/transactions/transactions` moves real money against an external provider and then writes to Firestore, so read it before you change anything near it. What the handler does, in order:

1. **Authorise.** The caller must be the request's `buyer_id`, or 403.
2. **Check availability.** `vehicles/{vehicle_listing_id}` must exist with `status == 'available'`, or 400. Note the field: the schema has no `vehicle_id`, and reading one was the `AttributeError` that made this endpoint fail on every request.
3. **Settle the terms against that record.** Where the vehicle document carries a `price`, the request's `amount` must match it to within half a cent; where it carries a `seller_id`, the request's must match exactly. Either disagreement is a **400** raised before any charge. Where the document carries neither — which today is every vehicle document, because nothing creates one — the request's own terms are used and that fact is logged rather than hidden (**HCF-7**, **HCF-16**).
4. **Refuse a payment intent already charged.** If any transaction records the same `stripe_payment_intent_id`, the request answers **409** and no charge is attempted. That is what turns the common failure — a client retrying a request whose answer it never saw — into a refusal rather than a second charge.
5. **Charge**, synchronously — `process_payment(stripe_payment_intent_id, amount, 'usd')`. The token is the request's payment intent id because that is the only Stripe-credential-shaped field the frozen schema declares (**HCF-1**), and the currency is pinned because the schema has no currency field.
6. **Read the result by key.** `payment_result.get('success')` — the helper returns a plain `dict`, and `.get` rather than `['success']` means a malformed result is treated as a failed payment instead of raising. Falsey means 400, before anything is written.
7. **On success**, write the transaction document with `status: 'completed'` and its own id, then update the vehicle to `status: 'sold'`.

**Three things this path still does not do, all of which matter if real money is involved.** They are the delivered state, each recorded in [section 5.1](#51-decisions-awaiting-confirmation), and none of them is something you have misconfigured:

- **Two buyers can both pay for one car.** The availability read and the `'sold'` update are separate, unguarded steps, so two genuinely concurrent requests can both see `'available'` and both be charged. The intent check in step 4 does not help here: two concurrent requests carry two different intents. Only an atomic reservation does (**HCF-14**).
- **The two writes are not atomic.** A crash between them leaves a charged card with either no transaction document or a vehicle still marked `'available'` (**HCF-14**).
- **The provider has no idempotency key.** `process_payment` sends none, so the guarantee in step 4 is application-side only: it stops a replay this service can see, not one that reaches Stripe by another path (**NT-27**).
- **Terms are only as authoritative as the record.** Step 3 can only compare against fields the vehicle document actually carries, and nothing populates one, so on today's data it warns rather than refuses (**HCF-7**, **HCF-16**).

One consequence for local work: nothing in this system creates a vehicle document (**HCF-7**), so to exercise the endpoint at all you must write `vehicles/{id}` yourself with `{"status": "available"}` — and give it a `price` and a `seller_id` too if you want step 3 to do anything, which is also the shape whatever eventually creates these documents should write.

# 3. COMMON PITFALLS

Most of what follows was learned the hard way. Several entries exist because the exact mistake they describe was shipped into this repository, stopped the backend from importing, and had to be diagnosed from a stack trace — including three defects nobody had reported and which only surfaced once the reported ones were cleared.

## 3.1 SYMPTOM LOOKUP

Start here. Match the message, then read the section.

| What you see | What it means | Section |
| --- | --- | --- |
| `ModuleNotFoundError: No module named 'app'` | `PYTHONPATH` is not set | [1.6](#16-set-pythonpath--mandatory) |
| `pydantic...ValidationError: 1 validation error for Settings` | Either one of the eight required variables is unset, or a value is outside its policy — a `SECRET_KEY` under 32 characters, an `ACCESS_TOKEN_EXPIRE_MINUTES` outside 1–1440, an `ALGORITHM` outside the HMAC family, or an `ALLOWED_ORIGINS` entry containing `*`. The message names which | [1.5](#15-configure-the-environment) |
| `ImportError: python-dotenv is not installed` | You have a `.env` file and the dependency that reads it is not installed. Delete the file and export the variables | [1.5](#15-configure-the-environment) |
| `SettingsError: error parsing env var "allowed_origins"` | You are running an older checkout; the current one accepts both forms | [3.4](#34-allowed_origins-takes-two-forms-and-neither-may-crash) |
| `ALLOWED_ORIGINS may not contain a wildcard` at import | Deliberate. Credentials are enabled on the CORS middleware, so a wildcard would admit every origin — name each one in full | [3.4](#34-allowed_origins-takes-two-forms-and-neither-may-crash) |
| A failure is logged but you wanted the traceback | By design: tracebacks and provider text sit behind `DEBUG`. Raise the `app` namespace to `DEBUG`, into a sink whose readers may see request content | [1.11](#111-where-the-logs-go) |
| `ImportError: cannot import name 'Stripe' from 'stripe'` | Known, out of scope, not your setup | [3.8](#38-a-clean-boot-still-stops-in-the-payment-module) |
| `TypeError: object dict can't be used in 'await' expression` | You awaited a synchronous service function | [3.5](#35-never-await-a-service-function-and-never-read-a-dict-by-attribute) |
| `AttributeError: 'dict' object has no attribute 'success'` | You read a dict result by attribute instead of by key | [3.5](#35-never-await-a-service-function-and-never-read-a-dict-by-attribute) |
| `NameError: name 'User' is not defined` at import | A name used in an annotation was never imported | [3.6](#36-annotations-are-evaluated-at-import) |
| `AttributeError: 'Settings' object has no attribute '…'` | A setting is read but not declared on `Settings` | [4.6](#46-add-a-setting) |
| `ImportError: cannot import name 'x_router'` | A router was imported under a name its module does not export | [4.2](#42-mount-it-in-mainpy) |
| 404 on a path you are sure exists | Probably the doubled segment: try `/api/listings/listings` | [3.9](#39-the-doubled-path-segments-are-deliberate) |
| 422 from `POST /api/auth/register` | Either a field is absent, or a value failed policy: `role` outside `buyer`/`seller`, a malformed or over-254-character address, a password under 8 characters or over 72 bytes, a blank or over-100-character name. The message names the field | [2.2.1](#221-what-registration-accepts) |
| 422 from `POST /api/auth/login` where you expected 401 | The body is not shaped like a login attempt — a field is missing, `null`, uncoercible, or the payload is not JSON. Credential rejection is 401; request-shape rejection is 422 | [2.5](#25-authentication-and-tokens) |
| 422 `field required` for `id`, `seller_id`/`buyer_id`, `status`, `created_at` or `updated_at` when creating a listing or transaction | Not your mistake: `VehicleListing` and `Transaction` declare every field required, so the body has to carry all of them. **Send real values rather than placeholders, though, because the handlers overwrite almost none of them** — creating a listing replaces only `seller_id`, from your token; creating a transaction replaces only `id` and `status`. Everything else you send is stored as sent, and a placeholder `buyer_id` answers **403** rather than 200, because it is compared with the caller instead of being assigned. **NT-23** | [4.3](#43-add-a-schema) |
| 422 `Unable to process vehicle photos` | More than 12 photos, an entry over 256 KiB, over 900 KiB of photo payload in total, or a blank entry. Which one is in the log line, not in the response | [2.8](#28-what-the-write-endpoints-bound) |
| 429 with a `Retry-After` header from register or login | The attempt ceiling for this minute is reached — 10 sign-ins or 5 registrations, per client address and per account. Wait the header out; it says nothing about whether the account exists | [2.2.1](#221-what-registration-accepts) |
| 409 `A transaction is already recorded for this payment` | That `stripe_payment_intent_id` has already been charged. This is the guard against a retry paying twice, not an error in your request — use a new intent | [2.9](#29-the-purchase-path) |
| 400 `Purchase terms do not match the vehicle record` | The vehicle document carries a `price` or a `seller_id` and your request disagrees with it. Fix the request, or the record | [2.9](#29-the-purchase-path) |
| 422 from `POST /api/messages/messages` | The body is blank, over 4000 characters, or over 16 KiB encoded | [2.8](#28-what-the-write-endpoints-bound) |
| 401 `Not authenticated` where you expected `Could not validate credentials` | The `Authorization` header is missing or is not a Bearer header, so the token never reached the dependency | [2.5](#25-authentication-and-tokens) |
| `TypeError: … got multiple values for keyword argument 'id'` on `GET /api/messages/messages` | Known messaging defect | [3.10](#310-messaging-is-reachable-but-not-functional) |
| CORS failure in the browser with the API answering fine in `curl` | Origin is not in `ALLOWED_ORIGINS`, or the SPA is on Vite's default port 5173 | [1.8](#18-run-the-spa-optional), [3.4](#34-allowed_origins-takes-two-forms-and-neither-may-crash) |
| `Failed to resolve import "app/services/api"` from Vite, or `Cannot find module 'app/utils/auth'` from `tsc` | Pre-existing and not your setup: the SPA's client module graph does not resolve — an unexported client, two modules that do not exist, and a prefix mapped nowhere. **NT-30** | [1.8](#18-run-the-spa-optional), [2.6.2](#262-why-none-of-it-executes-yet) |
| `ReferenceError: process is not defined` in the browser, or a request from the SPA arriving at the dev server — `http://localhost:3000/auth/login` rather than port 8000 | The client reads `process.env.REACT_APP_API_BASE_URL` and Vite satisfies neither half of that: no `process`, and only `VITE_`-prefixed names on `import.meta.env`. The base it needs is `http://localhost:8000/api`. **NT-31** | [1.8](#18-run-the-spa-optional), [2.6.2](#262-why-none-of-it-executes-yet) |
| `pytest` reports collection errors | Expected; the suite is broken | [3.7](#37-pytest-is-not-a-gate) |
| `ImportError: python-dotenv is not installed` | There is a `.env` file in the working directory and the optional extra is not installed | [1.5](#15-configure-the-environment) |
| 409 `Email is already registered` on register | An earlier registration used that address — in any spelling, since addresses are canonicalised — or a `user_emails` marker for it exists without an account behind it | [2.5.2](#252-what-the-public-routes-do-and-do-not-check) |
| 422 `Unable to process maintenance documents` | More than 20 records, over 900 KiB of content in total, or a record whose `content` is present but empty or of an unusable type. A record carrying **no** `content` is skipped rather than rejected, so it is not this | [4.4](#44-call-services-synchronously-and-guard-them) |
| 400 `Vehicle is not available for purchase` | No `vehicles/{vehicle_listing_id}` document, or its `status` is not `available` — remember **no module creates or populates** one, so you have to write it yourself; the purchase path only ever *updates* a document that already exists, to `'sold'` | [2.9](#29-the-purchase-path) |
| Nothing at all in the log where you expected a line | Something reconfigured the `app` logger, or you are reading a logger outside that namespace | [1.11](#111-where-the-logs-go) |

## 3.2 PYDANTIC V1 IS MANDATORY

**Symptom.** After a well-meant `pip install --upgrade pydantic`, `BaseSettings` cannot be imported from `pydantic` and every schema module fails.

**Cause.** `fastapi 0.95.2` is built against Pydantic **v1**. The pin is `pydantic==1.10.26`. Pydantic v2 moved `BaseSettings` to the separate `pydantic-settings` package and changed validator, config and parsing APIs — `app/core/config.py` depends on all of them.

**What to do.** Keep the pin. Write v1-style models: `class Config`, `@validator`, `Optional[X] = None`, `.dict()`. If you find v2 syntax in a snippet you are copying (`model_config`, `@field_validator`, `.model_dump()`), translate it. Upgrading both FastAPI and Pydantic together is a real project, not a housekeeping step.

## 3.3 `passlib 1.7.4` NEEDS `bcrypt` BELOW 4.1

**Symptom.** Password hashing or verification raises, or logs a `bcrypt version` error, and login stops working.

**Cause.** `passlib 1.7.4` reads the bcrypt backend's version through an attribute that `bcrypt 4.1` removed. The pin `bcrypt==4.0.1` is deliberate, not incidental.

**What to do.** Never upgrade `bcrypt` on its own. If hashing breaks, check `pip show bcrypt` first — it is the likeliest cause and takes ten seconds to rule out.

## 3.4 `ALLOWED_ORIGINS` TAKES TWO FORMS, AND NEITHER MAY CRASH

**Symptom.** Historically, exporting the obvious `ALLOWED_ORIGINS=http://localhost:3000` raised `SettingsError: error parsing env var "allowed_origins"` **at import**, before the application existed.

**Cause.** Pydantic v1 JSON-decodes complex-typed environment values inside its settings source, *before* any field validator can run. A bare comma-separated string is not valid JSON, so the decode failed and took the whole process with it — the very class of import-time failure this backend was repaired for. A `pre=True` validator cannot intercept it, because it never runs.

**What to do.** Nothing — it is handled, via a `Config.parse_env_var` override that special-cases this one field and delegates everything else to the default behaviour. Both of these are correct, and so are the edge cases:

```bash
export ALLOWED_ORIGINS="http://localhost:3000,http://localhost:5173"
export ALLOWED_ORIGINS='["http://localhost:3000","http://localhost:5173"]'
```

| Value | Result |
| --- | --- |
| unset | `['http://localhost:3000']` |
| `http://a.test,http://b.test` | `['http://a.test', 'http://b.test']` |
| `["http://a.test","http://b.test"]` | `['http://a.test', 'http://b.test']` |
| empty, or whitespace only | `[]` — restrictive, and never a crash |
| ` http://a.test , , http://b.test ` | `['http://a.test', 'http://b.test']` — entries trimmed, blanks dropped |
| `*`, or any entry containing `*` | **refused at import**, with a message naming the fix; see below |

> **`ALLOWED_ORIGINS='*'` is refused at import, and that is deliberate.** Any entry containing `*` — a bare wildcard or a pattern like `https://*.example.com`, which this middleware could not honour anyway — stops the process with a message telling you to name each origin in full. The alternative was to accept it and hope nobody deployed it, which is how this kind of value reaches production.
>
> **The reason it cannot be a "local diagnostic" is that the framework does not fail safe.** Because `allow_credentials=True`, Starlette answers a preflight by echoing back the *exact requesting origin* rather than a literal `*` (`preflight_explicit_allow_origin` in `starlette/middleware/cors.py`), so **every** origin passes the check. The wildcard is not a half-measure; it is an allow-all. Set exact origins: `ALLOWED_ORIGINS="https://app.example.com,https://admin.example.com"`.
>
> **What that allow-all actually buys an attacker, stated precisely.** This API authenticates with a bearer token that the SPA stores and attaches explicitly, and it sets no cookie and keeps no server-side session — so a page the user did not visit cannot make an *authenticated* request to this backend today. A browser only attaches ambient credentials, and CORS only relaxes the same-origin policy for *reading responses*; a token in `localStorage` is not ambient. What a permissive origin list does give away is (a) the ability for any site to read **unauthenticated** responses that a browser could otherwise not read cross-origin — here that includes the public listing endpoints, and [section 2.8](#28-what-the-write-endpoints-bound) explains why those responses carry raw maintenance-document content — and (b) the whole attack surface the moment anyone moves this API to cookie or session authentication, which is a one-line change on the client and a total change in exposure. Treat exact origins as the state you want to be in *before* that happens, not after.
>
> Narrowing the method and header lists, which are still `['*']`, is task **NT-11**; they are not the origin check and do not become safe because the origin list is now exact.

If you add another complex-typed setting, it needs the same treatment; [section 4.6](#46-add-a-setting) shows where.

A browser CORS failure while `curl` succeeds means the browser's `Origin` is not in this list — `curl` sends no `Origin` header, so it never exercises the check at all.

## 3.5 NEVER AWAIT A SERVICE FUNCTION, AND NEVER READ A DICT BY ATTRIBUTE

**Symptom.** One of three, on every single request to the endpoint, empty input included. `TypeError: object dict can't be used in 'await' expression` when you await a call that returned normally. A `TypeError` raised *inside* the callee when the argument is the wrong type, which is what an awaited call with a bad argument actually reports — `a bytes-like object is required, not 'list'` was the one this backend hit, from the photo helper's first statement, and it fires before there is any dict to await. And `AttributeError: 'dict' object has no attribute 'success'` when you read the result. They are independent defects: fixing the argument leaves the `await` wrong, and fixing the `await` leaves the attribute read wrong.

**Cause.** Everything in `app/services/` is a plain `def` returning a plain value. `async def` handlers make it *look* as though `await` belongs in front of a call, and it does not. Two live call sites had this exact defect, which is why neither listing creation nor transaction creation could ever succeed.

**What to do.** Call them directly and read their results by key:

```python
# Correct — synchronous call, one image at a time, result read by key.
analysis = analyze_vehicle_photo(payload)          # payload must be bytes
result = process_payment(token, amount, 'usd')     # all three arguments required
if not result.get('success'):                      # .get, not ['success'], not .success
    raise HTTPException(status_code=400, detail="Payment processing failed")
```

Three details are worth copying exactly:

- **`.get('success')`, not `['success']`.** A malformed result then reads as a payment failure instead of raising `KeyError` — it fails safe.
- **`analyze_vehicle_photo` takes one image as `bytes`**, not a collection and not a URL string. Loop, and normalise each entry.
- **`process_payment` takes all three of `(token, amount, currency)`.** Passing two raises `TypeError`, and passing them in the wrong order silently sends an amount where a token belongs. `currency` is validated against `usd`, `eur` and `gbp`.

One refinement, not an exception: inside an `async def` handler you may hand the call to a worker thread with `await run_in_threadpool(analyze_vehicle_photo, payload)`. That is still calling a synchronous function synchronously — the `await` is on the thread, never on the callee — and it stops a provider round trip from stalling the event loop. No handler does it today (task **NT-22**), and `await analyze_vehicle_photo(payload)` remains wrong in every context.

Before you call anything in `app/services/`, read its signature. Only two of the three service modules are even reachable at runtime today (see **HCF-3**, **HCF-4**), so the signature is the contract.

## 3.6 ANNOTATIONS ARE EVALUATED AT IMPORT

**Symptom.** `NameError: name 'User' is not defined` when a module is imported — not when a handler is called.

**Cause.** Python evaluates function annotations when the `def` statement executes. No module here uses `from __future__ import annotations`, so every name in a signature must be importable at import time. Two modules annotated `current_user: User = Depends(get_current_user)` without importing `User`, and the whole backend failed to boot as a result. This is precisely the class of defect the F-code lint gate in [section 1.9](#19-verify-your-checkout) catches, and why that gate is worth running before every commit.

**What to do.** Import every name you annotate with. Run the `F821` gate. Do not "fix" this by deferring annotation evaluation — the codebase does not use that mechanism anywhere, and FastAPI needs real objects to build its request models from.

The same rule explains a related convention: **`datetime.utcnow()` is this codebase's UTC clock.** It is non-deprecated on the 3.9 CI floor, though 3.12 warns about it. Use it in new code for consistency; migrating the whole codebase to timezone-aware datetimes is a coordinated change, tracked as task **NT-12**, not something to do incidentally in a feature branch.

## 3.7 `pytest` IS NOT A GATE

**Symptom.** `pytest` reports `3 errors during collection` and runs zero tests.

**Cause.** All three modules under `backend/tests/` were written against a completely different application: `test_api.py` imports `app.models`, `app.database` and `app.auth`; `test_services.py` imports top-level `services.*` and `integrations.*`; `test_tasks.py` imports `backend.tasks`. None of those modules has ever existed here.

**What to do.** Use `py_compile` and the `flake8` F-code selection from [section 1.9](#19-verify-your-checkout) as your static gates, and the in-process harness in [section 6](#6-verifying-behaviour-in-process) as your behavioural one — it drives the app with Starlette's `TestClient`, so it needs the `httpx==0.27.2` from step 2 of [section 1.4](#14-install-dependencies). Repairing the suite so that `pytest` becomes a real gate is task **NT-5** and is a genuinely valuable first contribution — section 6 is a ready-made specification for what those tests should assert.

## 3.8 A CLEAN BOOT STILL STOPS IN THE PAYMENT MODULE

**Symptom.**

```text
File ".../backend/app/services/payment.py", line 1, in <module>
    from stripe import Stripe
ImportError: cannot import name 'Stripe' from 'stripe'
```

**Cause.** Current Stripe SDKs expose `StripeClient`; they have no `Stripe` symbol. `app/services/payment.py` sits on the import chain `app.main` → `app.api.transactions` → `app.services.payment`, so this one line stops the whole application.

**What to do.** Recognise it and move on — **this is not something you have misconfigured.** It is known issue **HCF-3**, it lies outside the change set that made this backend importable, and repairing it needs a decision about which Stripe API generation to target, because the surrounding code also calls a legacy Charge creation. Until that is authorised, **an unstubbed `uvicorn app.main:app` cannot be an acceptance gate** for backend work. Two things stand in its place, and they answer different questions:

| You want to | Use | Datastore |
| --- | --- | --- |
| Assert behaviour — before a commit, or after changing a handler | The in-process harness in [section 6](#6-verifying-behaviour-in-process): one script, 139 assertions, a few seconds | An **in-memory double**. No emulator, no network, no credentials — and it asserts that before it runs |
| Actually serve traffic — to poke it with `curl`, open Swagger, or point a client at it | The local development profile in [section 1.7.1](#171-the-local-development-profile) | The **Firestore emulator**, with a throwaway project id, under a profile that refuses to start without it |

Both install the same two stand-ins, and neither puts them in the repository: editing `app/services/payment.py` or `app/services/ai_vision.py` is a different change with its own authorization. Use one of the two rather than assembling your own, because three details are easy to get wrong and each has cost someone here an afternoon:

- **Replace the constructors *before* `app.main` is imported.** All three clients are built at module import ([section 3.11](#311-clients-are-built-at-import-not-at-startup)), so a patch applied afterwards patches nothing.
- **Then assert the substitution took.** `assert app.db.firestore.db is my_double` fails closed; without it a silent miss means your "verified" run went somewhere real.
- **Use `TestClient` as a context manager.** A bare `TestClient(app)` never fires the startup event, so anything you assert about startup is vacuous.

**Which datastore, and why it matters.** An in-process double cannot touch anything you care about; an emulator can be reached by anything that resolves `FIRESTORE_EMULATOR_HOST`, and if that variable is *absent* the same client silently falls through to whatever `GOOGLE_APPLICATION_CREDENTIALS` names — which is why section 6 deletes both variables and asserts they are gone, and why the local profile refuses to start unless the emulator variable is set. If you need an emulator inside a verification run — to check an index, or a query the double does not model — carry those guards with you: assert the variable before you import anything, give the run its own throwaway project id, and delete what you wrote afterwards.

**State the gate you used when you report results:** "verified in-process against doubles, with the payment import stood in for" and "verified against a running server" are different claims, and only the first is available today.


**What "everything else works" does and does not mean.** With this one import stubbed, the whole authentication flow — register, login, logout, `/me`, and every 401 branch — was driven end to end in process, as were listing creation and transaction creation. That is the extent of the claim. It excludes four things that remain broken with or without the stub: both message routes fail on their own logic (**HCF-8**), real Cloud Vision calls fail so photo analysis degrades to nothing (**HCF-4**), transaction creation cannot find an `available` vehicle document because no module ever creates one (**HCF-7**), and the SPA neither renders nor can address this API ([section 1.8](#18-run-the-spa-optional), [section 2.6.2](#262-why-none-of-it-executes-yet)).

## 3.9 THE DOUBLED PATH SEGMENTS ARE DELIBERATE

**Symptom.** `POST /api/listings` returns 404 and you are certain the route exists.

**Cause.** It does exist, at `/api/listings/listings`. The prefix in `app/main.py` and the path in the decorator each contribute the same segment. Only the four authentication routes are free of this, because that module had no routes to preserve when it was given its HTTP surface.

**What to do.** Use the served paths from [section 2.7](#27-the-routes-as-actually-served) and print the route table when in doubt. **Do not renumber the prefixes to make them prettier.** The specification and the SPA both use the single-segment form, so correcting this is a breaking change that has to be coordinated across both — tracked as **HCF-6**.

## 3.10 MESSAGING IS REACHABLE BUT NOT FUNCTIONAL

**Symptom.** `GET /api/messages/messages` answers 200 while you have no messages, then fails once one exists. `POST` fails with a serialization error mentioning a `Sentinel` object; the subsequent `GET` fails with `TypeError: … Message() got multiple values for keyword argument 'id'`.

**Cause.** Two defects inside handler logic that the import-level repair did not touch. Sending writes Firestore's `SERVER_TIMESTAMP` sentinel into the object it then returns, and the response encoder cannot serialize a sentinel. Listing hydrates each document with `Message(**msg.to_dict(), id=msg.id)` while the stored document already carries an `id` key, so the keyword is supplied twice.

**What to do.** Do not chase it as a regression, and do not "fix" it by changing the message schema — a schema that rejects the sentinel simply fails one line earlier. Both defects are in route logic that is out of scope for the current work and are tracked together as **HCF-8**. Stated precisely, with the qualification that matters: **13 routes are registered — read off disk — and 11 of them behaved as documented, both in process against a double and over HTTP under the local development profile ([section 1.7.1](#171-the-local-development-profile)). On an unmodified checkout none of the 13 answers at all**, because the application does not import ([section 3.8](#38-a-clean-boot-still-stops-in-the-payment-module)).

**The body itself is bounded, and that part is done.** `Message.content` is trimmed, required to be non-blank, and capped at 4000 characters and 16 KiB encoded by a validator on the model, so an oversized or empty message is a **422 before the handler is entered** — which is the one messaging improvement reachable without touching the frozen route logic. Two residuals remain in that logic, and both wait for **HCF-8**: a client-supplied `id` or `read` flag is still stored as sent, because the same model is rebuilt from stored documents on the read path and a validator that discarded an incoming `id` would discard the server's own on the way back out; and **sending writes before it fails**, so a client retrying a request whose answer it never saw duplicates the message. Whoever repairs the route should give it idempotency in the same change.

## 3.11 CLIENTS ARE BUILT AT IMPORT, NOT AT STARTUP

**Symptom.** An import of almost any module fails on credentials or configuration, long before you have started a server or called an endpoint.

**Cause.** Module-level construction, in four places. Read the last two columns together, because a constructor at module level only runs if something imports that module *and* the import gets past its first line: one of these four is never imported by anything, and one is imported but never reached.

| Module | Line | Constructed when this module is imported | Built by a clean `import app.main` today? |
| --- | --- | --- | --- |
| `app/db/firestore.py` | 5 | Firestore `Client` | **Yes** — all four API modules import `db`, and this is the first of the three reached |
| `app/services/ai_vision.py` | 7 | `ImageAnnotatorClient` | **Yes** — via `app.api.listings` |
| `app/services/payment.py` | 5 | The Stripe client | **No, not today.** The module *is* on the path, via `app.api.transactions`, but its line 1 `from stripe import Stripe` raises before line 5 ever runs — so this is where the chain stops and the client is never constructed (**HCF-3**). It is built only once that import is stubbed or repaired |
| `app/db/cloud_storage.py` | 4–5 | Storage `Client` and a bucket handle | **No** — the module has zero importers, so nothing on the request path ever builds it |

So a clean `import app.main` builds two of the four — Firestore, then Vision — and then stops inside `app/services/payment.py` without building the Stripe client ([section 3.8](#38-a-clean-boot-still-stops-in-the-payment-module)). Three of the four are on the import path and only the fourth is not: the Cloud Storage client is built solely if you import `app.db.cloud_storage` yourself, which nothing does (**HCF-5**) — yet `GOOGLE_CLOUD_STORAGE_BUCKET` is still required, because `Settings` declares it without a default and settings validation does not care who reads it. Configuration must be valid *before* any import — which is why [section 1.5](#15-configure-the-environment) comes before [section 1.7](#17-run-the-api).

**What to do.** Export the variables first. Note the corollary, because it explains something you will otherwise find puzzling: **the startup hook in `app/main.py` deliberately performs no initialization.** It once imported and awaited `initialize_db`, `initialize_vision_model` and `initialize_document_processor`, none of which was ever defined anywhere in the repository — three `ImportError`s waiting at line 8. Since the clients are already built at import and the document processor needs no client, there was no initialization work left to do, so the awaits were **removed rather than stubbed out**, and the hook now logs one line — `startup complete: firestore and vision clients initialised at import`, which you will see because the same module configures the `app` logger at `INFO` ([section 1.11](#111-where-the-logs-go)). Whether to reintroduce real initializers — for lazy construction, or a startup health check — is an open decision, **HCF-2**. If you add one, put it in that hook; do not add a second startup event.

Being built at import is also why credentials are not needed for the compile and lint gates: those never import the modules, they only parse them.

## 3.12 ONE ADDRESS IS ONE ACCOUNT, AND HOW THAT IS ENFORCED

**Symptom.** You expect a duplicate registration to be refused, and you want to know what actually stops it — a query, or something stronger.

**Cause of the old behaviour.** Uniqueness used to rest on a query run before the write, and a query cannot see a request that has not committed yet: two simultaneous registrations both found nothing and both wrote. `authenticate_user` then resolved sign-in with `.limit(1)`, so which of the two passwords worked was not determined. Firestore's equality filter is also case-sensitive, so ` A@b.test ` and `a@b.test` were two accounts as far as that query was concerned.

**What happens now.** Two things, in this order:

1. **The address is canonicalised** — trimmed and lower-cased — by a validator on `RegisterRequest` and on `LoginRequest`, so one address has one representation everywhere: in the duplicate query, in the stored document, and in the sign-in lookup.
2. **The address is claimed atomically.** Registration creates `user_emails/{sha256(canonical_email)}` with `create()`, which is a *conditional* write: it fails with `AlreadyExists` if the document is there. That happens **before** the account is written, so of two simultaneous registrations exactly one proceeds and the other gets the same **409 `Email is already registered`** a sequential duplicate gets. If the account write then fails, the claim is released, so an address is never stranded.

The pre-write query is still there. It is the fast path, and it also covers any account created before that marker collection existed — those have no marker, so the query is the only thing that catches them.

**What to do.** Nothing, for uniqueness. Two things worth knowing:

- **A `user_emails` document with no user behind it means a 409 nobody can explain.** That can only happen if a process died between the claim and the compensating release. Delete the marker to free the address.
- **An in-memory double cannot prove any of this.** It serialises everything, so a race never happens. The concurrency assertion needs a Firestore emulator — see [section 1.10.1](#1101-the-two-sanctioned-stubs) for the fail-closed conditions, and expect exactly one 201 out of four simultaneous attempts.

# 4. HOW TO EXTEND

The patterns below are the ones the codebase already uses. Follow them rather than importing habits from other FastAPI projects — several of the conventions here exist because the alternative demonstrably broke this application.

## 4.1 ADD A ROUTER

Create `backend/app/api/<area>.py`. **Export the router under the name `router`** — every one of the four existing API modules does, and `app/main.py` relies on it.

```python
from fastapi import APIRouter, Depends, HTTPException
from app.schema.user import User          # import every name you annotate with
from app.db.firestore import db
from app.api.auth import get_current_user

router = APIRouter()                       # the name must be `router`


# A plain `def`, not `async def`: the body's only real work is a blocking
# Firestore read, so Starlette runs this in its threadpool instead of on the
# event loop. See the note below before you reach for `async`.
@router.get('/widgets/{widget_id}')
def get_widget(widget_id: str, current_user: User = Depends(get_current_user)):
    doc = db.collection('widgets').document(widget_id).get()
    if not doc.exists:
        raise HTTPException(status_code=404, detail="Widget not found")
    return doc.to_dict()
```

`Depends(get_current_user)` is the whole authentication story: it resolves the bearer token, loads the user document and raises 401 with a `WWW-Authenticate: Bearer` header if anything is wrong. Add authorization inline in the handler, matching the style in [section 2.2](#22-roles-and-who-may-do-what).

**`def` or `async def` is the one decision in this template that has a wrong answer, so make it deliberately.** The rule is simple: `async def` is correct only if the body actually `await`s something. Every I/O client in this codebase is synchronous — the Firestore client, Cloud Vision, PyPDF2, Stripe — so a handler that touches any of them has nothing to await, and declaring it `async` puts blocking calls directly on the event loop where they delay every other in-flight request. A plain `def` handler goes to Starlette's threadpool instead, which is what you want. That is why the example above is a `def`, and why the six handlers named below are.

Three consequences worth carrying:

- **Six handlers get this right and seven do not; copy the six.** The four authentication handlers, `create_listing` and `create_transaction` are plain `def`, so their Cloud Vision, PyPDF2, Stripe and Firestore calls run in the threadpool. `get_listings`, `get_listing`, `update_listing`, `delete_listing`, `get_transaction` and both message routes are still `async def` bodies performing synchronous I/O on the event loop, left that way only because converting frozen route logic needs authorization — tracked as **NT-22**. Follow the rule in new code rather than the majority of the existing code.
- **Never make a handler `async` and then `await` something that is not awaitable.** That was a real defect in this repository, on two live call sites; see [section 3.5](#35-never-await-a-service-function-and-never-read-a-dict-by-attribute).
- **One thing to check when you move a body off the event loop**, because it caught this codebase: code that relies on `signal.SIGALRM` only works on the main thread, and a threadpool thread is not the main thread. `app/services/document_processing.py` guards PDF extraction with `SIGALRM` when it can and falls back to an unguarded read when it cannot, so `create_listing` loses that wall-clock ceiling by being a `def`. That was the right trade — a slow document now occupies one thread instead of the whole process, and the byte, page and aggregate ceilings still apply — but if the code you are converting has a timeout of that shape, replace it with one that does not depend on signals.

## 4.2 MOUNT IT IN `main.py`

In `backend/app/main.py`, import the router **with an alias** and include it:

```python
from app.api.widgets import router as widgets_router
...
app.include_router(widgets_router, prefix='/api/widgets', tags=['Widgets'])
```

The alias is not decoration. `app/main.py` previously did `from app.api.auth import auth_router` against modules that export `router`, which raised `ImportError: cannot import name 'auth_router'` on line 3 of the composition root — the backend could not be imported at all, so no port was ever bound. Aliasing on import keeps each module's public surface as `router` while `app/main.py` reads clearly. **Keep the pattern.**

**The prefix and the decorator path concatenate.** `prefix='/api/widgets'` with a decorator of `@router.get('/widgets/{widget_id}')` serves `/api/widgets/widgets/{widget_id}`. That is exactly how the existing doubled paths arose ([section 3.9](#39-the-doubled-path-segments-are-deliberate)). For a new router, pick one place to name the area — a decorator of `/{widget_id}` under prefix `/api/widgets` serves the clean `/api/widgets/{widget_id}` — and verify with the route-table commands in [section 2.7](#27-the-routes-as-actually-served) before you commit.

## 4.3 ADD A SCHEMA

Put request and response models in `backend/app/schema/` as **Pydantic v1** `BaseModel` subclasses:

```python
from pydantic import BaseModel
from typing import Any, Optional


class Widget(BaseModel):
    id: Optional[str] = None       # server-assigned: optional on input
    owner_id: Optional[str] = None # server-assigned from the token
    name: str                      # client-supplied and required
    active: bool = False
    created_at: Optional[Any] = None
```

Conventions that matter:

- **Fields your handler assigns must be optional**, or the client cannot post a valid body — and a field counts as server-assigned only if a handler actually writes it. Get that distinction right before you copy anything from the two pre-existing write-path schemas, because **they declare every field required while their handlers overwrite almost none of them:**
  - `create_listing` overwrites exactly one field: `seller_id`, from the token. The **stored** document keeps the `id` you sent — only the object returned to you carries the generated document id — and `status`, `created_at` and `updated_at` are persisted exactly as submitted.
  - `create_transaction` overwrites exactly two: `id` and `status`. `buyer_id` is not assigned at all — it is **compared** with the caller and answers 403 on a mismatch, so it is request input that a placeholder cannot satisfy. `seller_id`, `amount`, `stripe_payment_intent_id`, `created_at` and `updated_at` are persisted exactly as submitted, which is the same fact [section 2.9](#29-the-purchase-path) records as **HCF-16**.

  So a caller has to send all of those fields today, and most of what it sends is what ends up stored. `Message` is the schema that follows the rule: only `recipient_id` and `content` are required. Follow `Message` in anything new — mark a field `Optional[...] = None` **only where your handler is the one writing it**, and write it there — and see **NT-23** for why loosening the two existing schemas is a handler change as much as a schema change. Both schema files and both handler bodies are frozen for now, so this is documented rather than fixed.
- **Use `typing.Optional` and `typing.List`.** `X | None` in an annotation genuinely breaks on the 3.9 CI floor; `list[X]` would work there (PEP 585) but is not the spelling any module in this repository uses — see [section 1.2](#12-prerequisites).
- **Pydantic v1 ignores unknown keys and permits attribute assignment.** That is why handlers can construct a model from a Firestore dict carrying extra derived keys, and then set `model.id = doc_ref.id` afterwards.
- **A Firestore sentinel value needs a permissive annotation.** `Optional[Any]` is used for a timestamp field that holds `SERVER_TIMESTAMP` on the way out and a real timestamp on the way back in.
- **Validate what an endpoint accepts, on the model.** A `@validator` runs while FastAPI binds the body, so a bad request is a 422 that names the field and your handler never runs — nothing is queried, hashed or written. Return the normalised value from the validator so the handler has exactly one thing to trust. There are three worked examples to copy: `Message.content` in `app/schema/message.py` bounds one field, `RegisterRequest` in `app/api/auth.py` normalises an address and constrains a role against an allow-list ([section 2.2.1](#221-what-registration-accepts)), and `Settings` in `app/core/config.py` validates configuration at import. Note what all three return — the *cleaned* value, trimmed and case-normalised — because every authorization check in this codebase compares stored strings exactly, so a value normalised in the validator is a value the rest of the code never has to normalise again.
- **Bound every field that reaches Firestore, authenticated or not.** A Firestore document may not exceed **1 MiB**, and handlers here write `model.dict()` straight into a collection, so an unbounded `str` is an unbounded document. The minimum is a named `_MAX_*_BYTES` ceiling, a non-empty check, and a length measured on the **UTF-8 encoding** rather than the character count — `len(value)` under-counts every non-ASCII string. Two in-repo precedents to copy from: `_MAX_CONTENT_BYTES` in `app/schema/message.py` for a plain client-supplied field, and `_MAX_MAINTENANCE_CONTENT_BYTES` in `app/api/listings.py` for a value the handler has to accumulate before it can be measured. Authentication only changes who can do it, not whether it works.

### 4.3.1 When The Code And The Specification Disagree, The Code Wins

`backend/app/schema/message.py` names its fields `recipient_id` and `timestamp`, while [`Technical Specifications.md`](Technical%20Specifications.md) §5.2 names the same concepts `receiverId` and `createdAt`. The schema follows the code because `backend/app/api/messages.py` — which already reads `message.recipient_id` and writes `timestamp`, and whose route logic is out of scope for the current work — is the frozen consumer. A specification-faithful schema would have raised `AttributeError` on the first request.

The judgement generalises: **when a frozen consumer and a document disagree, conform to the consumer and record the divergence** rather than silently breaking working code or silently contradicting the specification. Every divergence in this repository is written down in [section 5](#5-suggested-next-tasks); add yours there too.

## 4.4 CALL SERVICES SYNCHRONOUSLY, AND GUARD THEM

Copy this shape from `app/api/listings.py`. Bound the input before any outbound call; one correlation id per request, hoisted so every log line in the handler shares it; one call per item, each guarded, so a failure degrades the result instead of failing the request.

The block below is an **excerpt of the real handler, quoted as it stands on disk** — comments trimmed for length and a few added for orientation, nothing else altered. The body stops after the photo loop; the real handler continues with the same bound-then-guard-then-call shape for maintenance records, then assembly, a serialized-size check and the Firestore write.

```python
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException

from app.api.auth import get_current_user
from app.schema.listing import VehicleListing
from app.schema.user import User
from app.services.ai_vision import analyze_vehicle_photo

logger = logging.getLogger(__name__)        # renders under the `app` namespace

_MAX_PHOTOS = 12                            # bounds live beside the handler
_MAX_PHOTO_BYTES = 256 * 1024
_MAX_PHOTO_TOTAL_BYTES = 900 * 1024

router = APIRouter()


def _bounded_photo_payloads(photos, correlation_id):
    """Normalise to bytes and refuse the list before any provider call."""
    if len(photos) > _MAX_PHOTOS:
        logger.warning("too many photos",
                       extra={"correlation_id": correlation_id,
                              "count": len(photos)})
        raise HTTPException(status_code=422,
                            detail="Unable to process vehicle photos")
    payloads, total_bytes = [], 0
    for photo in photos:
        # Normalise the argument to the type the callee declares -- bytes here.
        payload = photo.encode('utf-8') if isinstance(photo, str) else photo
        # ... a blank entry, an entry over _MAX_PHOTO_BYTES, or an aggregate
        # over _MAX_PHOTO_TOTAL_BYTES each answer the same 422, each logged
        # with the correlation id so the log says which bound, not the client.
        total_bytes += len(payload)
        payloads.append(payload)
    return payloads


# A plain `def`: nothing in this body is awaited, and all of its work blocks.
@router.post('/listings')
def create_listing(listing: VehicleListing,
                   current_user: User = Depends(get_current_user)):
    if current_user.role != 'seller':
        raise HTTPException(status_code=403, detail="Only sellers can create listings")

    correlation_id = str(uuid.uuid4())      # once per request, not once per loop

    payloads = _bounded_photo_payloads(listing.photos, correlation_id)
    photo_analysis = []
    for payload in payloads:
        try:
            photo_analysis.append(analyze_vehicle_photo(payload))
        except Exception as failure:
            logger.warning(
                "photo analysis failed",
                extra={"correlation_id": correlation_id,
                       "error_type": type(failure).__name__},
            )
            logger.debug(
                "photo analysis failure detail",
                exc_info=True,
                extra={"correlation_id": correlation_id},
            )
    # ... the real handler goes on to process maintenance records, assemble the
    # listing, check its serialized size and write it to Firestore.
```

Six things to copy from it:

- **Bound the input before the first outbound call.** The count, the per-item size and the aggregate are all checked while the data is still in memory, which costs nothing, and a rejected request therefore performs **no** provider call at all. A bound placed after the loop — as the maintenance and serialized-size guards necessarily are — stops the write but not the work that was already paid for ([section 2.8](#28-what-the-write-endpoints-bound)).
- **Measure the encoded payload, not the string.** `len()` on a `str` under-counts every non-ASCII character, and the encoded payload is what you hand on.
- **Say less in the response than in the log.** The 422 names no bound; the log line names which one, with the request's correlation id. Which ceiling a request hit is operator information, and telling a caller lets it tune around them.
- **No `await` on the callee.** It is synchronous ([section 3.5](#35-never-await-a-service-function-and-never-read-a-dict-by-attribute)), so awaiting the dict it returns is invalid on its own. The old `await analyze_vehicle_photo(listing.photos)` never got as far as proving that, because the wrong argument (next bullet) raised `TypeError` inside the callee first, on every request. Two independent defects, and this loop removes both — never reintroduce either.
- **One item per call**, with the argument normalised to the declared type. `analyze_vehicle_photo` declares `image_data: bytes` and takes one image, not a collection; the old call passed the whole `List[str]`.
- **Two log calls, not one, in the `except`.** A `logger.warning` carrying the correlation id and `error_type` is what a production process prints; the traceback and the provider's own text go to `logger.debug(..., exc_info=True)`, which only appears when an operator asks for it ([section 1.11](#111-where-the-logs-go)). `logger.exception` here would publish absolute paths, library internals and whatever the provider chose to quote back. The guard itself matters too: the photo pipeline has a known unresolved defect (**HCF-4**), so an unguarded call would turn every listing creation into a 500 for the seller.

One thing this handler still does not do, so do not read it as complete: **the calls are inline**, so a provider round trip occupies one threadpool thread for the duration of the request, and `analyze_vehicle_photo` accepts no timeout. Moving this work to a background job, and giving the provider calls deadlines, is part of task **NT-22**.

## 4.5 PROJECTION DISCIPLINE: THERE IS NO `response_model`

**No route in this repository declares a `response_model`.** Whatever a handler returns is what the client receives, so nothing stops a model with sensitive fields from being serialized in full — and the `User` schema carries `hashed_password`.

Project explicitly. The reference is `_public_user` in `backend/app/api/auth.py`, which returns exactly the seven public fields and makes hash leakage structurally impossible rather than merely unlikely:

```python
def _public_user(user: User) -> dict:
    return {
        'id': user.id, 'email': user.email,
        'first_name': user.first_name, 'last_name': user.last_name,
        'role': user.role,
        'created_at': user.created_at, 'updated_at': user.updated_at,
    }
```

**Never return a `User` object from a handler.** If you introduce another model with sensitive fields, give it a projection helper in the same way. Adding `response_model` declarations across the API would enforce this at the framework level and is task **NT-13**; until then the discipline is manual.

One subtlety to know before you add a return annotation: **FastAPI infers a `response_model` from one.** Five handlers carry annotations — four in `app/api/listings.py` (`get_listings` → `List[VehicleListing]`, `get_listing` → `VehicleListing`, `update_listing` → `VehicleListing`, `delete_listing` → `dict`) and one in `app/api/messages.py` (`get_messages` → `List[Message]`) — so those five are filtered through the annotated model whether or not that was intended. The four authentication handlers and both transaction handlers deliberately carry none, returning plain dicts so that the four-key body the SPA expects survives intact. Annotate a return type only when you want that filtering, and check what it removes before you do.

## 4.6 ADD A SETTING

Declare it on `Settings` in `backend/app/core/config.py`. Reading `settings.ANYTHING_UNDECLARED` raises `AttributeError` at the point of use — and because middleware is registered at module scope, that means **at import**, which is exactly how `ALLOWED_ORIGINS` once prevented the application from starting.

- **Required**: annotate with no default, and accept that every environment must now supply it or fail to boot. Eight settings are in this category.
- **Optional**: give it a default. `SENTRY_DSN: Optional[str] = None` is the in-file precedent.
- **Complex-typed** (`List`, `Dict`, nested models): it needs handling in `Config.parse_env_var`, or a plain comma-separated value from an operator raises `SettingsError` at import. See [section 3.4](#34-allowed_origins-takes-two-forms-and-neither-may-crash).
- **Security-relevant**: give it a `@validator` and refuse a value outside policy, rather than letting the code that reads it discover the problem. Four settings already do this — key length, algorithm allow-list, token-lifetime bounds and the wildcard-origin refusal ([section 1.5.1](#151-every-setting-and-what-actually-reads-it)) — and the test is simple: if a wrong value would be *accepted and then quietly weaken something*, it belongs in a validator. A refusal at import is the one failure an operator cannot miss.

Then document it in [section 1.5.1](#151-every-setting-and-what-actually-reads-it) — a required variable with no documentation is a boot failure waiting for the next person — and give it a reader in the same change. Two required settings currently have no consumer at all (**NT-8**, **NT-9**), which is a small trap for everyone who follows.

## 4.7 UNIQUENESS AND MONEY: ONE PATTERN IS IN PLACE, ONE IS NOT

Firestore has no unique index and no multi-document constraint, and an external payment provider has no rollback. Of the two places that need a pattern for that, **registration now has one and the purchase path still does not** — read the first as the shape to copy, and the second as what to build when it is authorized.

**A marker document is how you get uniqueness, and registration uses one.** A query cannot enforce it: two requests can both find nothing and both write. Derive a document id from the value that must be unique and claim it with a *conditional* write. `create()` fails if the document exists, which makes the claim atomic on its own — no transaction required:

```python
marker_ref = db.collection('user_emails').document(
    hashlib.sha256(canonical_email.encode('utf-8')).hexdigest()
)
try:
    marker_ref.create({'user_id': user_ref.id, 'created_at': now})
except AlreadyExists:                    # google.api_core.exceptions
    raise HTTPException(status_code=409, detail="Email is already registered")
try:
    user_ref.set(user_data)
except Exception:
    marker_ref.delete()                  # release, or the address is stranded
    raise
```

A digest rather than the raw value, because a Firestore id may not contain `/` and may not be `.` or `..` — and because a digest keeps the address itself out of an id that other things may read. **The compensating delete is not optional**: without it, a failed account write leaves an address nobody can ever register, with nothing behind it to explain why.

If you need more than one document to appear together — a record and its marker, say — reach for a transaction instead, reading inside it so contention is detected:

```python
transaction = db.transaction()

@firestore.transactional
def _commit(txn):
    if marker_ref.get(transaction=txn).exists:
        raise DuplicateError()
    txn.create(marker_ref, {...})
    txn.create(record_ref, record_data)      # both, or neither

_commit(transaction)
```

**Reserve before you call an external service, and compensate if it fails — this half does not exist yet.** The purchase path refuses a payment intent it has already recorded, which stops a *retry* from paying twice, but two genuinely simultaneous buyers carry two different intents and nothing stops both from being charged. What is missing is a reservation: claim the thing being bought — move the vehicle to a `pending_payment` status and record the attempt — inside one transaction, *then* charge, then either release on a decline or settle on success (**HCF-14**, **NT-27**, **NT-28**).

Three rules that come from getting this wrong:

- **Translate datastore errors, never let them surface.** Contention aborts a transaction without saying why: `google.api_core.exceptions.AlreadyExists` means someone else won, and a bare `GoogleAPICallError` means "unknown — re-read and decide". Catch both and answer 409 or 503; an uncaught abort is a 500 for what is really a race you already handle.
- **Do everything that can fail before the write that cannot be undone.** Registration follows this: it builds the `User` and mints the token *before* it claims the marker and writes the account, so a validation or signing failure cannot leave an account whose owner was never told it exists — and whose retry would answer 409.
- **Make a retry safe.** Look for the work already recorded — by payment credential, by marker, by whatever identifies the operation — and answer with that result, or a 409, instead of repeating the side effect. The transaction path does this by `stripe_payment_intent_id`; do the same for anything that moves money or sends a message.

# 5. SUGGESTED NEXT TASKS

Everything below was found while making this backend importable and giving authentication an HTTP surface. None of it was in scope for that work, and none of it is in the delivered code. Each entry records the **interim position** actually taken, so you can see what the code does today before deciding what it should do.

Two groups. **HCF** entries need a *decision* — a human has to choose between defensible options, and picking one unilaterally would be the wrong kind of initiative. **NT** entries need *work*, and most are self-contained enough to be a good first contribution.

**Read HCF-11 through HCF-16 before you deploy anything.** Six entries about holding up against an uncooperative caller: **four are now implemented** and their rows say so, and the two that remain — HCF-14 and HCF-16, both on the purchase path — are the reason this API should not take real money yet. [Section 5.3](#53-where-to-start) orders everything by exposure.

## 5.1 DECISIONS AWAITING CONFIRMATION

| ID | Item | Interim position taken |
| --- | --- | --- |
| **HCF-1** | Should `Transaction` gain `payment_method`, `currency`, or a `vehicle_id` distinct from `vehicle_listing_id`? Explicit fields would enable genuine multi-currency and multi-instrument payments. | The **caller** was corrected instead of the schema: it now uses the existing `vehicle_listing_id` and passes `stripe_payment_intent_id` as the payment token, with currency pinned to `'usd'`. `backend/app/schema/transaction.py` is untouched. Related, and part of the same decision: `stripe_payment_intent_id` names a **PaymentIntent**, while `process_payment` passes its `token` argument to a legacy **Charge** creation — a generation mismatch that a currency or payment-method field would not fix on its own. |
| **HCF-2** | Should the three startup initializers be reinstated as real functions? | Removed, with the rationale recorded in the code. The Firestore and Vision clients are already built at import and the document processor needs no client, so there was nothing left to initialize; the awaits were removed rather than stubbed. Confirm that no lazy construction or startup health check is wanted. See [section 3.11](#311-clients-are-built-at-import-not-at-startup). |
| **HCF-3** | `backend/app/services/payment.py` line 1 does `from stripe import Stripe`, which the installed SDK cannot satisfy — it exposes `StripeClient`. | Flagged, not fixed: authorization is needed to touch `app/services/payment.py`. **This is why an unstubbed boot cannot be the acceptance gate** for backend work. The highest-value task here. |
| **HCF-4** | `backend/app/services/ai_vision.py` lines 20 and 44 call `client.image(...)`, which the installed Cloud Vision SDK does not provide, so real photo analysis fails. | Flagged; it sits inside logic that is out of scope. The calling loop is guarded, so a failure is logged with a correlation id and degrades the analysis instead of failing the request. |
| **HCF-5** | `VehicleListing.photos` holds URL **strings** while `analyze_vehicle_photo` declares **bytes**, so a fetch step is needed before analysis can produce meaningful labels. | The corrected call is type-correct and guarded — each entry is encoded to bytes — but no fetch was added, because that would be a behavioural expansion. Do not expect `app/db/cloud_storage.py` to supply the missing half: it exposes `upload_file`, `delete_file` and `get_file_url`, and `get_file_url` returns a URL string, so there is **no download or read-bytes path anywhere in the codebase**. Whoever takes this on has to write one, and **a fetch of a caller-supplied URL is a server-side request forgery primitive unless it is written as a hardened fetch from the start** — a timeout alone is nowhere near enough. The minimum: allow only `http`/`https` and reject every other scheme; resolve the host and check the resolved IP against a deny-list of private, loopback, link-local, multicast and reserved ranges **and** the cloud metadata endpoint (`169.254.169.254`, and `metadata.google.internal` here), re-checking after every redirect rather than only before the first request, because DNS can change between the check and the connection; cap redirects at a small number and revalidate each hop; require a response `Content-Type` in an image allow-list and cap the body in bytes *while streaming*, not after; set a connect and a read timeout; and never forward the response body or error text back to the caller, since that is how an internal service's reply becomes readable. Decide too whether photos should be uploaded through `cloud_storage.py` in the first place — an upload path the client uses is a smaller attack surface than a fetch path an attacker names. Settle **HCF-13** in the same change: real image bytes make an unbounded photo list far more expensive than URL strings do. |
| **HCF-6** | Served paths are `/api/listings/listings` and siblings, not the documented `/api/listings`. | Paths and prefixes left exactly as they are, per the contract freeze. Both the specification and the SPA use the single-segment form, so fixing this is a coordinated breaking change. See [section 3.9](#39-the-doubled-path-segments-are-deliberate). |
| **HCF-7** | Nothing **creates or populates** a `'vehicles'` document. `app/api/transactions.py` reads one to check availability and updates it to `status = 'sold'`, so the collection is written — but only ever updated, never created, and by no other module. | Flagged; explicitly out of scope. Consequence today: transaction creation cannot succeed against data this system produced, because no vehicle document with `status == 'available'` ever comes into existence. You have to write a `vehicles/{id}` document by hand to exercise the endpoint at all ([section 2.9](#29-the-purchase-path)). Deciding whether `listings` and `vehicles` are one collection or two is the data-model decision behind **HCF-1**; the obvious resolution is for listing creation to write it, and whatever does so should also carry the `price` and `seller_id` that **HCF-16** needs in order to settle a purchase’s terms from the record. |
| **HCF-8** | `backend/app/api/messages.py` writes a non-serializable Firestore `SERVER_TIMESTAMP` sentinel into its own response (line 38), and lines 54 and 56 hydrate with `Message(**msg.to_dict(), id=msg.id)` against a document that already carries an `id` key, raising a duplicate-keyword `TypeError` once any message exists. | Creating the missing schema restored **importability and reachability only** — **both message endpoints remain non-functional.** Post-fix state: 13 routes registered (read statically off disk), of which 11 behaved as documented — in process against a double, and over HTTP under the local development profile of [section 1.7.1](#171-the-local-development-profile); an unmodified checkout still serves none of them (**HCF-3**). A schema change cannot fix this; the route logic has to change. See [section 3.10](#310-messaging-is-reachable-but-not-functional). |
| **HCF-9** | Swagger's "Authorize" password flow cannot complete, because login consumes JSON rather than form data. | The frozen client contract governs; a separate form-encoded token route would be a tenth endpoint and exceed scope. Obtain a token from `POST /api/auth/login` and paste it as a bearer header. |
| **HCF-10** | Logout is stateless — no revocation mechanism exists anywhere. | Flagged and documented as a limitation, never presented as revocation. Any real solution needs a token store or a denylist, plus a `jti` claim to key it on. See [section 2.5.1](#251-logout-is-stateless). |

The six entries below are the ones that decide whether this API holds up against an uncooperative caller. **Four have since been implemented** — a dedicated security review found them, and each row now records what was built rather than what was proposed. **Two remain open, both on the purchase path**, and they are the reason this service should not take real money yet. Read all six before this API meets an untrusted caller.

| ID | Item | Interim position taken |
| --- | --- | --- |
| **HCF-11** | Should `POST /api/auth/register` restrict `role`? | **Implemented.** `role` is trimmed, lower-cased and checked against `('buyer', 'seller')`; anything else — `admin` included — answers **422** and the attempt is logged. Nothing that follows from that decision is outstanding except the other half of the problem: **NT-14**, an authenticated and audited way to grant the role deliberately, since there is now no way to obtain it through the API at all. See [section 2.2](#22-roles-and-who-may-do-what). |
| **HCF-12** | Should the credential fields be validated and email uniqueness made atomic? | **Implemented.** The address is normalised (trim, lower-case, syntax check, 254-character ceiling); a name must be non-blank and at most 100 characters; a password must be at least 8 characters and at most 72 **bytes**, because bcrypt hashes only the first 72 and passlib truncates the rest silently. Uniqueness is atomic through a `user_emails/{sha256(email)}` marker claimed with a conditional `create()` and released if the account write fails, so four simultaneous registrations for one address yield exactly one account — verified against a Firestore emulator, since a double cannot show it. Sign-in timing was equalised in the same change: the unknown-address path spends one deliberate bcrypt verification so it cannot be told apart from a wrong password. One thing deliberately **not** done: nothing proves the address belongs to the registrant (**NT-36**). See [section 3.12](#312-one-address-is-one-account-and-how-that-is-enforced) and [section 4.7](#47-uniqueness-and-money-one-pattern-is-in-place-one-is-not). |
| **HCF-13** | Should the photo list be bounded? | **Implemented.** More than 12 photos, an entry over 256 KiB, an aggregate over 900 KiB, or a blank entry each answer **422** — measured on the encoded payload and enforced **before the first outbound call**, so a rejected request pays for no provider work. `Message.content` was bounded in the same change (non-blank, 4000 characters, 16 KiB encoded). Two things this did not do: it did not bound *how many* listings or messages an account may create (**NT-25**), and the ceilings are sized for URL strings — **recheck them alongside HCF-5**, since real image bytes change what 256 KiB means. |
| **HCF-14** | Should the purchase path be made concurrency-safe and idempotent? | **Half done, and the open half is the dangerous one.** A repeat is now refused: before charging, the handler looks for a transaction already recording the same `stripe_payment_intent_id` and answers **409** with no provider call, which covers the common case of a client retrying a request whose answer it never saw. **Still unguarded:** two genuinely simultaneous buyers carry two different intents, so both can read `'available'` and both be charged; the transaction write and the `'sold'` update are still two separate writes, so a crash between them leaves a charged card with no transaction or an unsold vehicle; and the provider call still carries no idempotency key. What is needed is a reservation — the vehicle into a `pending_payment` status with an attempt record, in one Firestore transaction, released on a decline — plus **NT-27** and **NT-28**, in one change. See [section 2.9](#29-the-purchase-path) and [section 4.7](#47-uniqueness-and-money-one-pattern-is-in-place-one-is-not). |
| **HCF-15** | Should the public authentication routes be throttled? | **Implemented.** Fixed one-minute windows per client peer address and per account — 10 sign-ins, 5 registrations — answering **429** with `Retry-After` and one message whether or not the account exists, clearing the counters after a successful sign-in, logging the bucket that filled but never the address, pruning windows that can no longer be consulted, and ignoring `X-Forwarded-For` deliberately (nothing here terminates TLS or strips it, so honouring it would let a caller reset its own counter). The counters are **per process**, so a deployment with several workers multiplies the ceiling — that half is **NT-26**, and it is a real limitation rather than a footnote. |
| **HCF-16** | Should a purchase's amount and seller be settled from the vehicle record rather than the request? | **Implemented as far as the data allows, which today is not far enough.** Where the vehicle document carries a `price`, the request's amount must match it to within half a cent; where it carries a `seller_id`, the request's must match exactly; either disagreement is a **400** raised before the charge. But **nothing creates or populates a vehicle document** (**HCF-7**), so on data this system produced there is no price and no seller to compare against, and the handler logs that it is proceeding on the request's own terms. The check is therefore a guard that becomes real the moment HCF-7 is settled — **whatever starts writing `vehicles/{id}` must write `price` and `seller_id`**, and until then a caller can still name its own price on a hand-written record that omits them. Making the record mandatory (a **409** when it cannot supply the terms) is the remaining decision, and it belongs with HCF-7. |

## 5.2 WORK WORTH PICKING UP

| ID | Task | Detail |
| --- | --- | --- |
| **NT-1** | Add a pinned dependency manifest | `backend/requirements.txt` is absent, and a previous one was removed as out of scope, so recreating it needs authorization rather than initiative. Until then the install block in [section 1.4](#14-install-dependencies) is the manifest — and continuous integration installs from a file that does not exist. |
| **NT-2** | Make `.env` work, then add `.env.example` | Two halves, and the order matters. `Settings` declares `env_file = ".env"`, but `python-dotenv` is not in the installed set, so a `.env` file makes `app.core.config` raise `ImportError` rather than load ([section 1.5](#15-configure-the-environment)). Authorising and installing that dependency is the first half — it is blocked by the same no-new-dependency scope as **NT-1**. Only then is a committed `.env.example` useful; until both land, the export block in [section 1.5](#15-configure-the-environment) is the configuration. |
| **NT-3** | Add Dockerfiles — and clean the compose file's credentials before it can start | There is no Dockerfile anywhere in the repository, so `infrastructure/docker/docker-compose.yml` cannot build either service and the CI `docker build` step cannot work. **Do not simply add the two Dockerfiles and start the stack**, because the compose file that is harmless while it cannot run stops being harmless the moment it can: it hardcodes `DATABASE_URL=postgresql://user:password@…` and `JWT_SECRET=your_jwt_secret_here`, so the first stack that boots from it runs with a published password and a placeholder signing key — and a placeholder `SECRET_KEY`/`JWT_SECRET` is not a weak secret, it is a *known* one, so anyone can mint a token for any user id ([section 1.5](#15-configure-the-environment)). Replace both with values injected from the environment or a secret manager, and settle **NT-15**'s wider compose contradictions in the same change — the stack also runs a `postgres:13` service that no application code uses. |
| **NT-4** | Decide on a license | No `LICENSE` file exists. Earlier documentation asserted MIT; **do not invent a license** — the terms of use are genuinely undetermined and only the project owners can settle them. |
| **NT-5** | Repair the test suite | All three modules under `backend/tests/` import packages that have never existed here, so nothing collects. Rewriting them against the real `app.*` layout would give the project its first real gate. See [section 3.7](#37-pytest-is-not-a-gate). |
| **NT-6** | Fix the Celery task module | `backend/app/tasks/background_jobs.py` **cannot be imported**, so no worker can run and none of this code has ever executed. Do not treat it as working code with a bug in it — read it as a draft. Every defect below is deterministic, and the order is the order you will actually meet them, each stage only observable once the one before it is out of the way. **Stage 0 — the dependency.** Celery is not in the application runtime set at all, so line 1 raises `ModuleNotFoundError: No module named 'celery'` until you install the worker extra ([section 1.4](#14-install-dependencies), step 3). **Stage 1 — HCF-3, reached through this module's own imports.** Line 7 imports from `app.services.payment`, whose line 1 is the unsatisfiable `from stripe import Stripe`, so the failure you see is `ImportError: cannot import name 'Stripe' from 'stripe'` — the same blocker as everywhere else ([section 3.8](#38-a-clean-boot-still-stops-in-the-payment-module)), and it fires before anything about this module is diagnosable. **Stage 2 — the wrong symbol.** With Stripe repaired or stood in for, that same line 7 raises `ImportError: cannot import name 'process_refund' from 'app.services.payment'`: the service defines `process_payment` and `create_refund`, and never `process_refund`. **Stage 3 — the undeclared setting.** Line 9 reads `settings.CELERY_BROKER_URL` and raises `AttributeError: 'Settings' object has no attribute 'CELERY_BROKER_URL'`. **Exporting the variable does not help** — `Settings` does not declare the field, so Pydantic never reads it; declaring it is the fix ([section 4.6](#46-add-a-setting)), and it needs a broker to point at, which this project does not run. **Stage 4 — the undefined name.** Lines 12 and 34 annotate parameters `List[str]` while the module imports no `typing` name: `NameError: name 'List' is not defined`, and these are the two `F821` entries in the [section 1.9](#19-verify-your-checkout) baseline. **Then the call sites, once it imports at all:** line 19 passes a URL string to `analyze_vehicle_photo`, which declares `image_data: bytes` — the same class of defect as **HCF-5**; line 41 calls `process_maintenance_document(url)` whose real signature is `(document_data: bytes, document_type: str)`, so it is both the wrong type *and* one argument short; line 74 calls the refund helper with one argument while `create_refund(charge_id, amount)` takes two; and lines 76, 83 and 85 read `refund_result.success` and `refund_result.error_message`, while every return path in `payment.py` builds a plain `dict` whose failure key is `error` — so the access style and the key name are both wrong. **And logic that would still be wrong afterwards:** the six helpers at the foot of the file — `aggregate_photo_results`, `check_for_discrepancies`, `aggregate_document_results`, `check_for_inconsistencies`, `is_listing_expired`, `log_failed_refund` — are `pass` stubs, so each task would write `None` into Firestore or silently never fire; and `@celery_app.task` is stacked over `@celery_app.on_after_configure.connect` on lines 55–56 and 66–67, which registers a signal receiver rather than a schedule, so nothing is periodic and the unused `crontab` import at line 2 (the baseline's `F401`) is the trace of a schedule that was never written. Fix stages 2 to 4 first — nothing else is observable until the module imports — and note that **NT-21** builds directly on this. |
| **NT-7** | Remove or adopt `backend/app/core/security.py` | Dead code: zero importers anywhere. It duplicates the password and token helpers from `app/api/auth.py` and holds a *correct* `tokenUrl` expression that can never take effect. Two copies of security logic, one of them unreachable, is a trap for the next reader — delete it or make it the single source. |
| **NT-8** | Wire up `PROJECT_NAME` | Required, and read by nothing: `app/main.py` calls `FastAPI()` with no `title`. Pass it, so `/docs` and the OpenAPI document are named. |
| **NT-9** | Wire up or drop `STRIPE_WEBHOOK_SECRET` | Required, and read by nothing, because no webhook route exists. Either add webhook handling or stop demanding the value. |
| **NT-10** | Clear the residual lint baseline — and with it, the project's own acceptance gate | Ten findings, in five files across four areas of the package, all listed with their line numbers in [section 1.9.2](#192-gate-2--undefined-and-unused-names-across-the-whole-package): two unused `typing.Optional` imports (`schema/listing.py:2`, `schema/transaction.py:2`); three in `services/ai_vision.py` — an unused `typing.List`, an unused `settings`, and an `image` local that is assigned and then never passed to the provider call; two unused `except … as e` bindings in `services/payment.py` (lines 46 and 88); and three in `tasks/background_jobs.py` — an unused `crontab` plus the two `F821 undefined name 'List'` entries that are the reason that module cannot be imported. **This is not one edit.** Each area needs its own judgement: deleting an unused import is trivial, but the `image` local is a symptom of **HCF-4**, the two `e` bindings sit in the module frozen behind **HCF-3** and should either log the exception or stop capturing it, and the `background_jobs.py` entries belong with the rest of **NT-6**. The two schema files are the only genuinely mechanical part, and they were left untouched because the change set that repaired the boot chain was not authorised to touch them. Clearing all ten is what takes the gate in section 1.9.2 from failing to passing, so it is worth more than its size suggests. |
| **NT-11** | Narrow the CORS policy | `allow_methods` and `allow_headers` are both `['*']` with `allow_credentials=True`. Only the missing *setting* was a defect; narrowing the policy is a security improvement that was not authorized here. |
| **NT-12** | Migrate to timezone-aware datetimes | `datetime.utcnow()` is the codebase's convention and is non-deprecated on the 3.9 floor, but 3.12 warns and a future release removes it. This has to be done consistently across token issuance, expiry and every stored timestamp — coordinated, not incidental. |
| **NT-13** | Declare `response_model` on the routes — and strip raw maintenance content while you are there | No route declares one today, which is why hand-written projection is the only thing keeping password hashes out of responses ([section 4.5](#45-projection-discipline-there-is-no-response_model)). It is also why a second, larger disclosure is still open: `create_listing` keeps whatever `maintenance_records[*].content` a seller sends, writes it into the listing document beside the derived `maintenance_data`, and **both listing reads are unauthenticated**, so a receipt or service invoice — names, addresses, phone numbers, a VIN — becomes world-readable. Do both halves in one change: store the raw content privately, or not at all, and return only approved derived metadata through an explicit response model. Until then, treat the maintenance fields as public and send only what you would publish. |
| **NT-14** | Add a privileged administrator-elevation workflow | There is no deliberate way to create an administrator: no endpoint grants the role and nothing audits a grant. What is missing in code is an authenticated, administrator-only path that grants the role and writes its own audit record — and, since a role change takes effect on the very next request ([section 2.5](#25-authentication-and-tokens)), a corresponding path to revoke it. Until then the intended procedure is a direct Firestore write, which is **unauthenticated, unauthorized and unrecorded by the application**; [section 2.2](#22-roles-and-who-may-do-what) sets out the operator-channel, approval and audit-logging controls that have to compensate. Its stated prerequisite is met: **NT-24** is done, so registration now refuses `admin` and an elevation path would no longer be guarding a door that is already open. It was not added here because the change set is fixed at four authentication routes; a fifth would need authorization. |
| **NT-15** | Fix the frontend's identity and its entry point | Several independent pre-existing gaps, none of which is the client-to-API seam — **NT-30** and **NT-31** own that. `frontend/package.json` is named `task-management-frontend` and described as a task-management app; `frontend/public/index.html` is titled "Personal Finance Tracker"; the SPA serves nothing to render, because Vite resolves its entry `index.html` from the project root while the file sits in `public/` and there is no `vite.config.ts` to point it elsewhere; the compose backend is published on port 5000 rather than 8000; and the compose stack runs a `postgres:13` service with a `DATABASE_URL`, contradicting the Firestore implementation entirely. The `@/…` import prefix that the pages and components use is mapped nowhere either, and eight of its targets — including `components/PhotoGallery`, `components/ProfileForm` and `services/document_processing` — are files that do not exist. |
| **NT-16** | Get continuous integration green | [`.github/workflows/backend_ci.yml`](../.github/workflows/backend_ci.yml) has four defects you can prove from this clone and one dependency you cannot: it installs from a `requirements.txt` that does not exist (**NT-1**), runs a non-collectable test suite (**NT-5**), builds a Docker image with no Dockerfile (**NT-3**), and **pins Python 3.9, on which the verified dependency set cannot be installed at all** — five of the cloud and media packages declare `Requires-Python >= 3.10`, so that runner would silently resolve older versions than anyone tested ([section 1.2](#12-prerequisites)). Moving the runner and keeping the 3.9 *source* rule, or moving both together, is a decision to take explicitly rather than by dropping whichever is inconvenient. It also references four repository secrets — `GCP_PROJECT_ID`, `GCP_SA_KEY`, `GKE_CLUSTER_NAME` and `GKE_ZONE` — and whether those are configured is **not observable from a local checkout**; confirm it in the repository settings rather than assuming either way. Its `flake8 .` step lints the whole tree with default rules, not the F-code selection this project actually uses, so it would report a different set of findings from [section 1.9](#19-verify-your-checkout). |
| **NT-17** | Adopt a component library or design system | None is in use. Tailwind CSS is declared in `frontend/package.json` but is not wired up — there is no `tailwind.config.js`, no `postcss.config.js`, and **not one `.css` file in the repository**. Adopting one is a separate deliverable, not a side effect of backend work. |
| **NT-18** | Rewrite or delete the shell scripts | Both are stale enough to mislead. `scripts/setup_environment.sh` installs Node 14, runs `pip install -r requirements.txt` and `cp .env.example .env` against files that do not exist, runs `npm install` at a root with no `package.json`, calls `gcloud init` interactively, and sets up a local SQL database this system does not use. `scripts/deploy.sh` builds `./backend/api`, `./backend/auth` and `./backend/search`, none of which exist — the backend is a single application — and changes into a root `terraform/` directory that lives at `infrastructure/terraform/`. **Do not run either script; follow [section 1](#1-setup) instead.** |
| **NT-19** | Add `CONTRIBUTING.md` | No such file exists, and [`README.md`](../README.md) points at this guide in its place. A short document covering branch naming, the commit style already visible in the log, and the two gates in [section 1.9](#19-verify-your-checkout) would be enough. |
| **NT-20** | Page and order the collection queries | Every list query in this codebase streams a whole collection with no `limit`, no `order_by` and no cursor, so response time and memory grow with the data: `app/api/listings.py`'s `get_listings` streams all `listings` matching its filters (and applies none of them when a filter is zero-like, a truthiness quirk worth fixing in the same pass); `app/api/messages.py` runs **two** unbounded streams and then sorts the combined result **in memory**; `app/db/firestore.py`'s `query_documents` helper streams whatever it is given; and `app/tasks/background_jobs.py` scans `listings` and `transactions` by status. Add `limit`/`start_after` paging and explicit `order_by`, and return a page cursor rather than a bare list. Firestore also needs a composite index for any ordered multi-filter query, so this task includes declaring those indexes. |
| **NT-21** | Batch the per-document writes in the task module | `app/tasks/background_jobs.py` issues **one `update()` per result** — `update_listing_status` writes once per expired listing, `process_scheduled_refunds` once per refunded transaction, and both re-resolve the document reference they already hold. That is an N+1 write pattern against a per-second write budget. Use `db.batch()` (or `bulk_writer()`), reuse the reference from the streamed snapshot, and chunk at Firestore's 500-write batch limit. **NT-6** must land first — the module cannot import today. |
| **NT-22** | Move the remaining blocking handlers off the event loop, and give the providers deadlines | Six handlers are now plain `def` and get Starlette's threadpool: the four authentication routes plus `create_listing` and `create_transaction`, which were the two worst — Cloud Vision once per photo, PyPDF2 once per record, and a network round trip to Stripe. **Seven are still `async def` bodies doing synchronous I/O directly on the event loop**: `get_listings`, `get_listing`, `update_listing`, `delete_listing`, `get_transaction` and both message routes. One slow read there still delays every other in-flight request. Converting them is mechanical — delete the `async` keyword, since none of them awaits anything — but it changes handler signatures that are frozen for the current work. Do it together with **NT-20**, whose paging changes the same lines. Two things to carry from the conversions already done: `fastapi.concurrency.run_in_threadpool` is the alternative where a signature must stay `async`, and a `SIGALRM`-based timeout stops working off the main thread ([section 4.1](#41-add-a-router)) — which is why `analyze_vehicle_photo` and `process_payment` need real deadline parameters when their signatures are opened, rather than a signal-based guard. |
| **NT-23** | Loosen the write-path schemas **and make the handlers the writers**, in one change | `VehicleListing` (`backend/app/schema/listing.py`) and `Transaction` (`backend/app/schema/transaction.py`) declare every field required, including `id`, `seller_id`/`buyer_id`, `status`, `created_at` and `updated_at`, so a caller has to invent placeholder values purely to pass body validation and any client written the obvious way gets a 422 listing five fields it should never have had to send. **The schema half on its own is not the fix, because the server does not currently write most of those fields:** `create_listing` overwrites only `seller_id`, and `create_transaction` only `id` and `status` — everything else a caller sends is what gets stored, and `buyer_id` is *compared* with the caller rather than assigned, so optionalising it alone would turn every valid purchase into a 403 against `None`. Do both halves together: give each field `Optional[...] = None` as `Message` does ([section 4.3](#43-add-a-schema)), and in the same change derive `buyer_id` from `current_user`, the seller and the amount from the vehicle record (**HCF-16**), `status` and both timestamps server-side, and persist the generated document id rather than the caller's `id`. Anything less admits incomplete documents and a response hydration that fails on the fields nobody filled in. It was not done here because both schema files and both handler bodies are frozen for the current work — and because it changes what an existing client may send, it wants the same coordination as **HCF-6**. |
| **NT-24** | ~~Validate and bound the registration body~~ — **done**; kept for the reasoning, because the same reasoning applies to the next unauthenticated body anyone adds | `POST /api/auth/register` is the only unauthenticated write in this API, and it used to validate nothing beyond field presence. What it enforces now is listed in [section 2.2.1](#221-what-registration-accepts); this entry records *why* each part of it is there. **(a) `role` was stored verbatim, `admin` included.** The role is read back on every request by `get_current_user` and trusted by `delete_listing`, so any anonymous caller could register itself as an administrator and delete any seller's listing — the reason the field is now constrained to an allow-list of the roles a caller may give itself, `buyer` and `seller`, with `admin` refused and the attempt logged. The case is normalised in the same validator because every authorization check compares the stored string exactly, so `Seller` would otherwise be stored as written and then fail the seller gate. **(b) No field carried a bound.** `email` is trimmed, shape-checked and capped at 254 characters, the longest address SMTP carries — hand-rolled, because pydantic's `EmailStr` needs `email-validator`, which is not installed and is blocked by the same no-new-dependency scope as **NT-1**. The password has a minimum length and a hard **72-byte UTF-8 ceiling**: bcrypt hashes only the first 72 bytes and `passlib` discards the rest silently, so a longer password would promise strength it does not have and two sharing a 72-byte prefix would be interchangeable — refusing it is the honest answer where truncating quietly is the one option to avoid. The names are trimmed, rejected when whitespace-only, and capped. All of it lives in `@validator`s on `RegisterRequest` rather than in the handler, so a bad body is a 422 before the duplicate lookup, before bcrypt and before Firestore ([section 4.3](#43-add-a-schema)), and each validator returns the normalised value — which is also what closes the ` a@b.com ` / `a@b.com` duplicate-account gap, since the pre-write duplicate check and the uniqueness marker now see the same trimmed address that gets stored. `LoginRequest` is deliberately left with canonicalisation only and no shape check, so a sign-in attempt can never be answered 422 in a way that distinguishes it from a rejected credential ([section 2.5](#25-authentication-and-tokens)). What is **not** done, and is tracked separately: proving the address belongs to the person registering it (**NT-31**). |
| **NT-25** | Bound *quantity*, not just size — the per-request bounds are done | The three per-request gaps this entry used to list are closed: the photo list is capped in count, per-entry bytes and aggregate bytes before the first provider call, and `Message.content` is bounded and required to be non-blank by its model ([section 2.8](#28-what-the-write-endpoints-bound)). What remains is the other axis. **(a) Nothing limits how much an account may create.** One registered seller may write unlimited listings, one account unlimited messages — each individually valid, all of them permanent, and every one of them enlarging an unpaginated list response. A per-account daily quota, or a cost-based limit, is the missing control, and it pairs with **NT-20**: an unbounded collection is only expensive because nothing pages it. **(b) The registration throttle is per process** (**NT-26**), so creation volume is bounded per worker rather than globally. **(c) Recheck the photo ceilings once HCF-5 lands** — a 256 KiB per-entry cap sized for a URL string is the wrong cap once photos are real image bytes. |
| **NT-26** | Give the attempt counters somewhere shared to live | The throttle itself exists (**HCF-15**), and its counters are module state in one worker process. That means the effective ceiling is *N* times the configured one for *N* workers, and a restart forgets every window — so it raises the cost of credential stuffing without bounding it. Move the counters to a shared store (Redis, Memorystore, Firestore with a TTL) or push the policy to a gateway. **Note the constraint before you start:** no dependency may be added to this project without authorization, so a Redis-backed implementation needs that decision first — which is part of why the in-process version shipped rather than nothing. Whatever replaces it should keep the properties the current one has: one message whether or not the account exists, a `Retry-After` header, counters cleared on a successful sign-in, and the bucket logged rather than the address. |
| **NT-27** | Give the payment call an idempotency key | `process_payment` sends none, so nothing at the provider prevents a double charge and nothing application-side does either today (**HCF-14**). A provider-level key makes the guarantee end-to-end, but it means editing `app/services/payment.py`, which is frozen pending **HCF-3**. Do all three in one change. |
| **NT-28** | Finalise a purchase in one transaction, and reconcile | The transaction document and the vehicle’s `'sold'` update are two separate writes, so a crash between them leaves a charged card with no matching transaction, or a sale with an unsold vehicle. Move both into one Firestore transaction, and add a job that sweeps charges with no transaction: settle them, or refund through `create_refund` and release the vehicle. Part of the same work as **HCF-14**. |
| **NT-29** | Decide what the logs should feed | [Section 1.11](#111-where-the-logs-go) configures the `app` namespace at `INFO` with a plain text formatter, which is right for a terminal and wrong for a log aggregator. If this system gets one, swap the formatter for JSON and give each request an id at middleware level rather than per handler, so that every line of a request — not only listing creation’s — carries the same identifier. |
| **NT-30** | Make the SPA's API client importable | The client does not compile, so **nothing in the contract at [section 2.6](#26-the-spa-contract--treat-it-as-frozen) can execute** however exactly the backend satisfies it. Three defects, all in `frontend/`, each with its line number in [section 2.6.2](#262-why-none-of-it-executes-yet): `createApiInstance` is a module-local `const` in `services/api.ts` that `services/auth.ts` and `services/payment.ts` both import; `app/utils/auth` and `app/utils/storage` are imported and exist nowhere in the tree; and the `app/…` prefix is mapped neither in `frontend/tsconfig.json` nor by a bundler alias, there being no `vite.config.ts`. Export the client, supply the two modules — or repoint both imports at a single storage module — and map the prefix **in `tsconfig.json` *and* in a Vite config, because the two resolvers are independent and one without the other leaves either a type error or a runtime resolution failure**. Use one key name throughout: `services/auth.ts` writes and clears `authToken`, so `getAuthToken` has to read that key or the interceptor never finds the token login just stored. Do this with **NT-31** — a client that compiles but cannot address the API is no further forward. The `@/…` prefix needs the same mapping treatment and belongs with **NT-15**. |
| **NT-31** | Give the SPA a usable API base | `frontend/src/services/api.ts:4` reads `process.env.REACT_APP_API_BASE_URL`. `REACT_APP_` is a Create-React-App convention and this is a Vite project: Vite exposes only `VITE_`-prefixed variables, through `import.meta.env`, and puts no `process` in a browser build — so that read throws or yields nothing, axios is left resolving every path against the page's own origin, and exporting the variable changes nothing because nothing reads it. Adopt `import.meta.env.VITE_API_BASE_URL` and give it the value **`http://localhost:8000/api`** — the Uvicorn origin plus `API_V1_STR` — from `frontend/.env.local` in development and from the deployment environment elsewhere; `services/payment.ts:4` needs the same treatment for `REACT_APP_STRIPE_PUBLIC_KEY`. Then align `infrastructure/docker/docker-compose.yml`, which sets a third name, `REACT_APP_API_URL`, and publishes the backend on port 5000 rather than 8000. Afterwards the three authentication calls compose exactly onto their routes and the listing calls still answer 404 (**HCF-6**) — expect that rather than reading it as a new defect. Pairs with **NT-30**. |
| **NT-32** | Redact the client's error logging | `frontend/src/services/api.ts:24` logs the whole rejected axios error from its response interceptor, and `frontend/src/services/auth.ts:27` and `:39` log some of them a second time. That object is not a message: `config.data` on a sign-in **is** the `{email, password}` body and `config.headers.Authorization` **is** a live bearer token, so a browser console — and anything that forwards console output onwards — ends up holding plaintext passwords and usable tokens. Log an allow-list instead: the HTTP status, a stable application code, a safe message. Never `config`, `data`, `headers`, the token, the email or the password; and drop the duplicated auth-layer logging rather than redacting it twice. This is the client-side half of the rule the backend already follows ([section 1.11](#111-where-the-logs-go)), and it is the only frontend item on this list that is a security fix rather than a repair. Decide the storage question in the same change: the token currently lives in web storage, which is readable by any script that runs on the page, and moving to a cookie would need the CORS and CSRF story rewritten (**NT-11**), so it is a decision, not a refactor. |
| **NT-33** | Settle the client's two unmatched endpoints | The client calls two paths this API does not serve under any prefix, and both answer 404 even after **NT-30** and **NT-31**: `services/api.ts:48`'s `uploadPhoto()` posts `multipart/form-data` with the file under the field name `photo` to `/upload` and expects a string back, and `services/payment.ts:21`'s `createPaymentIntent()` posts `{amount, currency}` to `/payments/create-intent` and expects a `clientSecret`. Each can be settled two ways, and both ways need a decision. **Implement them** — which means new routes, and the change set that gave this backend its authentication surface was fixed at four, so a fifth needs authorization. An upload route at least has somewhere to land: `app/db/cloud_storage.py` is complete, exposes `upload_file`, and has no importer today. A payment-intent route would also have to settle the generation mismatch in **HCF-1**, since `process_payment` creates a legacy Charge out of what the schema calls a PaymentIntent id. **Or remove the calls**, if the SPA is not going to use them. Decide the upload half together with **HCF-5** and **NT-25**: it determines where photo bytes come from, and therefore what has to bound them. |
| **NT-34** | Declare the SPA's undeclared dependencies | `frontend/src` imports eight packages that `frontend/package.json` does not declare: `@stripe/react-stripe-js` and `@stripe/stripe-js` (`src/index.tsx`, `components/PaymentForm.tsx`, `services/payment.ts`), `browser-image-compression` (`utils/imageProcessing.ts`, `services/imageProcessing.ts`), `date-fns` (`utils/formatting.ts`), `dompurify` (`utils/validation.ts`), `formik` (`components/VehicleDetailsForm.tsx`), `react-dropzone` (`components/MaintenanceDocumentUploader.tsx`, `components/PhotoUploader.tsx`) and `zod` (`utils/validation.ts` and all four `src/schema/` modules). `npm install` therefore leaves every one of those imports unresolvable, so the affected modules fail for `tsc` and for Vite whatever **NT-30** does about the client's own graph. Two decisions, not one: whether each package is wanted at all, and for those that stay, which versions. Check the call sites while you are there rather than only the manifest — `utils/validation.ts:3` imports `dompurify` as a default export and then calls it, `sanitize(input)`, but DOMPurify's default export is an object with a `sanitize` method, so that line is a `TypeError` waiting to happen even once the package is installed. Declaring a dependency is a manifest change and this project authorises none without a decision, the same constraint as **NT-1**. Also missing and worth settling in the same pass: `eslint` and `vitest`, which `package.json`'s own `lint` and `test` scripts invoke and which nothing declares, so both scripts fail immediately. |

| **NT-35** | Upgrade `python-jose`, and plan the framework upgrade behind it | The pinned `python-jose 3.3.0` is affected by CVE-2024-33663 and CVE-2024-33664, both addressed from 3.4.0. Reachability is limited today — every token is HS256 JWS and `ALGORITHM` is restricted to the HMAC family — but this is a known-vulnerable pin, not a cleared one. Upgrading is a dependency change and needs the same authorization as **NT-1**. Look at the wider set in the same pass: `fastapi 0.95.2` and `starlette 0.27.0` are also old enough to carry advisories that the current route surface happens not to exercise, and every one of them gets harder to patch the longer the pin set sits. Any framework move has to be planned against the Pydantic-v1 constraint ([section 3.2](#32-pydantic-v1-is-mandatory)) and the runtime floors in [section 1.2](#12-prerequisites). |
| **NT-36** | Verify that an address belongs to the person registering it | Registration validates an address's *shape* and enforces its uniqueness, and neither is ownership: anyone can register an address they do not control, and hold the account its real owner would later expect — which also makes any future password-reset-by-email flow unsafe to build on top. What is missing is a confirmation step: a signed, single-use, expiring token sent to the address, an account marked unverified until it is presented, and a decision about what an unverified account may do. Password reset and email change need the same machinery, so design all three together rather than bolting the first one on. |
| **NT-37** | Give stored personal data a lifecycle | Registration persists an address, both names, a role and a password hash; messages persist their bodies; listings persist whatever maintenance content a seller sent (**NT-13**). None of it has a retention period, a consent record, an export path or a deletion path — **deleting a user is not an operation this API has**, and nothing cascades, so a subject request cannot be answered today. Decide the purpose and retention for each collection, then add an authenticated export and an authenticated deletion with defined cascade rules (what happens to a sold vehicle's transaction record, which almost certainly must be retained, versus the messages around it, which need not be). This is the work that turns a demo datastore into one that can hold real users. |
| **NT-38** | Own the browser security headers at the edge | This application sets exactly one response header of its own: `Cache-Control: no-store` on the token and profile routes. HSTS, a content-security policy, `X-Content-Type-Options`, frame options, referrer policy and host validation are all absent, and they belong to whatever terminates TLS in front of it — which this repository does not contain, so there is nothing to inherit them from. Write that configuration alongside the ingress, and decide there whether any of it should instead be middleware here (a `TrustedHostMiddleware` and a small header middleware are the usual answer when the app can be deployed without a known proxy). Pair it with **NT-11**: the CORS method and header lists are still `['*']`. |

## 5.3 WHERE TO START

**Read this list in order if anyone might expose this service.** It is ordered by what an uncooperative caller can do today, not by size, and the first three are the reason this API should not be internet-facing yet. Nothing on it is "merely unfinished".

| # | Blocker | What it permits today | Task |
| --- | --- | --- | --- |
| 1 | **A purchase is not atomic and the provider has no idempotency key** | Two simultaneous buyers can both be charged for one vehicle, and a crash between the two final writes leaves a charged card with no transaction record or an unsold vehicle. Money moves; nothing reconciles it | **HCF-14**, **NT-27**, **NT-28** |
| 2 | **Purchase terms are only as authoritative as a record nothing writes** | The amount and seller are checked against the vehicle document *where it carries them*, and nothing creates one — so on this system's own data a caller still names its own price. Settle what writes `vehicles/{id}`, with `price` and `seller_id`, then make the record mandatory | **HCF-7**, **HCF-16** |
| 3 | **Raw maintenance-document content is publicly readable** | Whatever a seller uploads is stored on the listing and returned by two unauthenticated endpoints, so a receipt or invoice publishes names, addresses and a VIN. This one needs no attacker at all — an ordinary crawler is enough | **NT-13** |
| 4 | **The attempt ceilings are per worker process** | The throttle raises the cost of credential stuffing but does not bound it: *N* workers permit *N* times the ceiling, and a restart forgets every window | **NT-26** |
| 5 | **No account can be deleted and nothing has a retention rule** | A subject access or deletion request cannot be answered, and personal data accumulates with no defined purpose or lifetime | **NT-37** |
| 6 | **A known-vulnerable JWT library is pinned** | Reachability is limited today because every token is HS256 JWS, but the pin is a published-advisory version and the whole pin set is old enough to be hard to patch | **NT-35** |
| 7 | **Browser security headers have no owner** | No HSTS, no CSP, no frame or content-type options, no referrer policy, no host validation — and no ingress configuration in this repository to hold them | **NT-38**, **NT-11** |
| 8 | **Anyone can register an address they do not own** | Shape and uniqueness are enforced; ownership is not, so any future password-reset flow inherits the problem | **NT-36** |
| 9 | **The frontend logs bearer tokens** | Whole Axios errors, headers included, reach `console.error`. Latent only because the SPA does not render | **NT-32** |
| 10 | **List endpoints are unpaginated and creation is unquantified** | One account may create unlimited listings and messages, and every list response streams a whole collection | **NT-20**, **NT-25** |

**If you want the single most useful change to the *project* rather than to its security posture: HCF-3.** It is the one gap between a fresh clone and a served API, and everything else becomes easier to verify once `uvicorn app.main:app` actually stays up. It is not on the list above because a service that cannot start cannot be attacked — which is the only reason the ten above are not already urgent, and a poor reason to relax about them.

**If you want the SPA to reach this API at all, NT-30 and NT-31 are one job, not two.** The client cannot compile without the first and cannot address the API without the second, and neither is observable end to end until **HCF-3** lets a server start — so plan all three together, and expect the listing calls to keep answering 404 afterwards (**HCF-6**). **NT-32** is the other frontend item worth doing regardless of that sequencing: it is a handful of lines, needs no decision, and keeps passwords and bearer tokens out of the browser console.

If you would rather start small, **NT-8**, **NT-10**, **NT-19** and **NT-32** are each self-contained, need no decision from anyone, and are low-risk — but each still has something to check before you call it done:

| Task | How you know it worked |
| --- | --- |
| **NT-8** — pass `PROJECT_NAME` to `FastAPI()` | The compile and F-code gates in [section 1.9](#19-verify-your-checkout) stay clean, and `app.openapi()['info']['title']` reads back your value. That second check imports `app.main`, so it needs the **HCF-3** blocker resolved or stubbed ([section 3.8](#38-a-clean-boot-still-stops-in-the-payment-module)). |
| **NT-10** — delete the two unused schema imports | `python -m flake8 --select=F401,F811,F821,F841 backend/app/` reports **8** findings instead of 10, and the two files still compile. Do not touch the other eight ([NT-10](#52-work-worth-picking-up) explains why). Note this moves the A3 baseline in [section 1.10](#110-before-you-call-a-change-done) from ten to eight — update that assertion in the same change. |
| **NT-19** — write `CONTRIBUTING.md` | There is no automated gate, which is exactly why it needs care: check that every link in it resolves, and that the gates it documents are the same commands as [section 1.9](#19-verify-your-checkout) and [section 1.10](#110-before-you-call-a-change-done) rather than a paraphrase that will drift. |
| **NT-32** — redact the client's error logging | `grep -rn "console.error" frontend/src/services/` shows no call passing a raw error object, and a failed sign-in in the browser console shows a status and a message with no `config`, `headers`, `data`, email, password or token anywhere in it. Check the logout and current-user handlers too — they log the same errors a second time, and both have to stop. |

If you want to make the project verifiable, **NT-5** — a collectable test suite — is worth more than any other single contribution on this list. The harness in [section 6](#6-verifying-behaviour-in-process) is its specification: 139 assertions that already exist and that nothing runs for you.

If you are looking at this before it carries real traffic, do **NT-20**, **NT-21** and **NT-22** while the data is still small. Each one is a pattern repeated in several places, and every one of them gets more expensive to change once there are documents to migrate and clients depending on a list response that has no page cursor.

# 6. VERIFYING BEHAVIOUR IN-PROCESS

## 6.1 WHAT THIS IS, AND WHAT IT IS NOT

Everything this guide says about what the endpoints *do* — that registration answers 201 and a repeat answers 409, that the vision helper is called once per photo with `bytes`, that a declined charge writes nothing, that a log line carries its correlation id — was established by running the script in [section 6.3](#63-the-script). It is published here so that you can repeat it rather than take it on trust, and so that when you change a handler you can find out in a few seconds whether you broke one of those properties.

It is **verification only**, and that has consequences worth being explicit about:

- **It is not a test suite and it is not committed.** Save it outside the working tree — `/tmp/verify_backend.py` is fine — or delete it when you are done. The repository has no `.gitignore` ([section 1.3](#13-create-an-isolated-environment)), so a copy left in the tree shows up as an untracked path in `git status --porcelain` — which is the half of [gate 3](#193-gate-3--change-containment) that catches it. A committed-diff comparison would not: git does not track it, so there is nothing to diff. Making these checks permanent means rewriting `backend/tests/` against the real `app.*` layout, which is task **NT-5**; until someone does, this script is the closest thing the project has to a specification for that work.
- **It needs no cloud credentials, no emulator, no server and no network.** It replaces Firestore with an in-memory double and both unsatisfiable third-party constructors with stand-ins, and it removes every environment variable that could point a client somewhere real. It cannot reach a Google Cloud project, a shared Firestore emulator or a Stripe account, and it asserts that before it does anything else. That is the fail-closed default described in [section 1.10.1](#1101-the-two-sanctioned-stubs), and it is the only procedure this guide publishes for driving the app.
- **Two properties are deliberately outside it**, because an in-memory double cannot model them: that four simultaneous registrations for one address yield exactly one account, and that a replayed payment intent is refused under real query semantics. Verify those against a Firestore emulator, under the conditions in [section 1.10.1](#1101-the-two-sanctioned-stubs) — assert the emulator variables before importing anything, use a throwaway project id, delete what you wrote — and report which gate you used.
- **The one dependency it adds is not a project dependency.** `TestClient` needs `httpx`, and `httpx 0.28` removed the `Client(app=…)` shortcut that `starlette 0.27` relies on, so install `httpx==0.27.2` for the harness. Do not add it to any manifest: no dependency may be added to this project without authorisation ([section 5.2](#52-work-worth-picking-up)), and nothing in `backend/app/` imports it.

## 6.2 BEFORE YOU RUN IT

```bash
# from the repository root, with the virtual environment active
export PYTHONPATH="$PWD/backend"
pip install "httpx==0.27.2"          # step 2 of section 1.4, if you skipped it
```

That is all the preparation there is. You do **not** need the eight required environment variables set: the script exports its own throwaway values for all of them, which is deliberate, because it keeps a real `STRIPE_API_KEY` out of a verification run and makes the token-lifetime assertion deterministic (`ACCESS_TOKEN_EXPIRE_MINUTES=60`). It then deletes `GOOGLE_APPLICATION_CREDENTIALS`, `FIRESTORE_EMULATOR_HOST`, `FIRESTORE_DATASET` and `GCLOUD_PROJECT` from its own environment, and refuses to continue if `PYTHONPATH` is unset.

Save the script and run it:

```bash
python /tmp/verify_backend.py
```

It prints one line per assertion and a summary, takes a few seconds, and exits 0 only when every assertion held. It may pause briefly before the attempt-limit gate: those counters run in fixed one-minute windows, and starting a burst inside a fresh window is cheaper than making the assertion tolerant of a rollover it could not tell apart from a broken ceiling.

```text
PASS fail closed: the application holds the double, not a client
PASS fail closed: no emulator or credential variable is in the environment
PASS the startup hook logs exactly one line
PASS 13 routes are registered
...
PASS an unset required setting still refuses to import

139 assertions, 139 passed, 0 failed
```

A `FAIL` line prints what it got and what it wanted, so the label plus that pair is normally enough to locate the change that caused it.

## 6.3 THE SCRIPT

```python
"""Behavioural verification harness for this backend. VERIFICATION ONLY.

Not application code, not a pytest module, and not committed: save it
outside the working tree, or delete it when you are done. Run it from the
repository root:

    export PYTHONPATH="$PWD/backend"
    pip install "httpx==0.27.2"          # TestClient needs httpx < 0.28
    python /tmp/verify_backend.py

It supplies its own throwaway configuration, replaces Firestore with an
in-memory double, and stands in for the two third-party surfaces this code
cannot reach: the `Stripe` symbol payment.py imports and the installed SDK
does not export (HCF-3), and the `.image()` method ai_vision.py calls on a
Vision client that has no such method (HCF-4). So it needs no cloud
credentials and reaches no network. Exit code 0 means every assertion held.
"""

import io
import json
import logging
import os
import subprocess
import sys
import time
import types
import uuid
from contextlib import ExitStack, contextmanager
from datetime import datetime
from unittest import mock

from google.api_core.exceptions import AlreadyExists

# --- 0. Fail closed --------------------------------------------------------
# Configuration is supplied here and every variable that could point a
# client at real or shared infrastructure is removed, so the harness cannot
# reach a live Firestore, a Firestore emulator or a real Stripe account even
# when the shell that launched it is configured for one.
HARNESS_ENV = {
    'PROJECT_NAME': 'verification-harness',
    'API_V1_STR': '/api',
    'SECRET_KEY': 'harness-only-' + uuid.uuid4().hex,
    'ACCESS_TOKEN_EXPIRE_MINUTES': '60',
    'GOOGLE_CLOUD_PROJECT': 'harness-no-such-project',
    'GOOGLE_CLOUD_STORAGE_BUCKET': 'harness-no-such-bucket',
    'STRIPE_API_KEY': 'sk_test_harness_not_a_real_key',
    'STRIPE_WEBHOOK_SECRET': 'whsec_harness_not_a_real_secret',
    'ALLOWED_ORIGINS': 'http://localhost:3000',
}
FORBIDDEN_ENV = ('GOOGLE_APPLICATION_CREDENTIALS', 'FIRESTORE_EMULATOR_HOST',
                 'FIRESTORE_DATASET', 'GCLOUD_PROJECT')
os.environ.update(HARNESS_ENV)
for _name in FORBIDDEN_ENV:
    os.environ.pop(_name, None)
if not os.environ.get('PYTHONPATH'):
    sys.exit('PYTHONPATH is unset: export PYTHONPATH="$PWD/backend" first')

RESULTS = []
STAMP = '2026-01-01T12:00:00'
PASSWORD = 'harness-password'


def ok(label, condition, detail=''):
    RESULTS.append(bool(condition))
    print('%-4s %s%s' % ('PASS' if condition else 'FAIL', label,
                         '' if condition else ' -> ' + str(detail)))


def eq(label, actual, expected):
    ok(label, actual == expected, 'got %r, wanted %r' % (actual, expected))


# --- 1. In-memory Firestore double ----------------------------------------
# Exactly the surface the four api modules use. It is a stand-in for call
# and response shape, not an emulator: it enforces no index, no uniqueness
# and no ordering, and an operator it does not implement raises instead of
# quietly matching everything.
class Snap:
    def __init__(self, doc_id, data):
        self.id, self._data = doc_id, data

    @property
    def exists(self):
        return self._data is not None

    def to_dict(self):
        return None if self._data is None else dict(self._data)


class Doc:
    def __init__(self, store, collection, doc_id):
        self._store, self._name, self.id = store, collection, doc_id

    def get(self):
        return Snap(self.id, self._store.docs(self._name).get(self.id))

    def set(self, data):
        self._store.docs(self._name)[self.id] = dict(data)
        self._store.writes += 1

    def create(self, data):
        # A conditional write, which is the whole point of the email marker in
        # app/api/auth.py: the real client raises AlreadyExists rather than
        # overwriting, so the double must too or the atomicity assertion below
        # would pass for the wrong reason.
        if self.id in self._store.docs(self._name):
            raise AlreadyExists('%s/%s already exists' % (self._name, self.id))
        self.set(data)
        return Snap(self.id, self._store.docs(self._name)[self.id])

    def update(self, data):
        docs = self._store.docs(self._name)
        if self.id not in docs:
            raise KeyError('%s/%s does not exist' % (self._name, self.id))
        docs[self.id].update(dict(data))
        self._store.writes += 1

    def delete(self):
        self._store.docs(self._name).pop(self.id, None)
        self._store.writes += 1


class Query:
    OPS = {'==': lambda v, t: v == t,
           '>=': lambda v, t: v is not None and v >= t,
           '<=': lambda v, t: v is not None and v <= t}

    def __init__(self, store, collection, filters=(), cap=None):
        self._store, self._name = store, collection
        self._filters, self._cap = tuple(filters), cap

    def where(self, field, operator, value):
        if operator not in self.OPS:
            raise NotImplementedError('operator %r not doubled' % operator)
        return Query(self._store, self._name,
                     self._filters + ((field, operator, value),), self._cap)

    def limit(self, count):
        return Query(self._store, self._name, self._filters, count)

    def get(self):
        found = []
        for doc_id, data in self._store.docs(self._name).items():
            if all(self.OPS[op](data.get(f), v) for f, op, v in self._filters):
                found.append(Snap(doc_id, data))
            if self._cap is not None and len(found) >= self._cap:
                break
        return found

    def stream(self):
        return iter(self.get())


class Collection(Query):
    def document(self, doc_id=None):
        return Doc(self._store, self._name, doc_id or uuid.uuid4().hex[:20])

    def add(self, data):
        ref = self.document()
        ref.set(data)
        return (datetime(2026, 1, 1, 12, 0), ref)


class Firestore:
    def __init__(self):
        self._collections, self.writes = {}, 0

    def collection(self, name):
        return Collection(self, name)

    def docs(self, name):
        return self._collections.setdefault(name, {})

    def count(self, name):
        return len(self.docs(name))

    def reset(self):
        self._collections.clear()
        self.writes = 0


def stripe_stand_in():
    """The `Stripe` symbol app/services/payment.py line 1 imports.

    The installed SDK exposes StripeClient and no Stripe, which is known
    issue HCF-3 and outside the authorised change set, so the symbol is
    supplied here rather than repaired there.
    """
    module = types.ModuleType('stripe')

    class StripeError(Exception):
        pass

    charge = types.SimpleNamespace(create=lambda **kw: types.SimpleNamespace(
        id='ch_harness', status='succeeded'))
    module.Stripe = lambda api_key: types.SimpleNamespace(
        Charge=charge, Refund=charge,
        error=types.SimpleNamespace(StripeError=StripeError))
    module.error = types.SimpleNamespace(StripeError=StripeError)
    return module


class Spy:
    """Records every call, then returns a result or raises."""

    def __init__(self, result=None, raises=None):
        self.result, self.raises, self.calls = result, raises, []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.raises is not None:
            raise self.raises
        return self.result


@contextmanager
def spying(module, name, spy):
    with mock.patch.object(module, name, spy):
        yield spy


def listing_body(photos=(), records=()):
    return {'id': 'server-assigned', 'seller_id': 'server-assigned',
            'make': 'Toyota', 'model': 'Corolla', 'year': 2019,
            'mileage': 42000, 'price': 15000.0, 'condition': 'good',
            'photos': list(photos), 'maintenance_records': list(records),
            'status': 'available', 'created_at': STAMP, 'updated_at': STAMP}


def purchase_body(buyer_id, seller_id, vehicle_id, amount=15000.0,
                  intent='pi_harness_abc'):
    # Each attempt names its own payment intent unless a case is deliberately
    # replaying one: an intent that has already been charged is refused.
    return {'id': 'server-assigned', 'buyer_id': buyer_id,
            'seller_id': seller_id, 'vehicle_listing_id': vehicle_id,
            'amount': amount, 'status': 'pending',
            'stripe_payment_intent_id': intent,
            'created_at': STAMP, 'updated_at': STAMP}


def register(client, email, role):
    """Register one account and return its public user and auth header."""
    reply = client.post('/api/auth/register', json={
        'email': email, 'password': PASSWORD, 'first_name': 'Test',
        'last_name': 'User', 'role': role})
    assert reply.status_code == 201, reply.text
    body = reply.json()
    return body['user'], {'Authorization': 'Bearer ' + body['token']}


def promote_to_admin(store, user):
    """Make one account an administrator the only way the API allows.

    Registration refuses the role, so an administrator can only come from an
    out-of-band write to the document. get_current_user re-reads that document
    on every request, so the change takes effect on the next call made with the
    token the account already holds.
    """
    store.docs('users')[user['id']]['role'] = 'admin'


def reset_limits(app):
    """Clear the in-process attempt counters between gates.

    They are per-process and deliberately tight (10 sign-ins, 5 registrations
    per address or peer per minute), and every request here arrives from one
    peer, so unrelated gates would otherwise exhaust a ceiling that gate_limits
    exercises on purpose.
    """
    app.api.auth._rate_counters.clear()


# --- 2. Gates -------------------------------------------------------------
def gate_composition(app, client, log):
    eq('the startup hook logs exactly one line', log.getvalue().count(
        'startup complete: firestore and vision clients initialised at '
        'import'), 1)
    rows = sorted((r.path, ','.join(sorted(r.methods)))
                  for r in app.main.app.routes if r.path.startswith('/api'))
    eq('13 routes are registered', len(rows), 13)
    eq('across 9 unique paths', len({path for path, _ in rows}), 9)
    eq('the four auth operations are exactly as documented',
       [r for r in rows if r[0].startswith('/api/auth')],
       [('/api/auth/login', 'POST'), ('/api/auth/logout', 'POST'),
        ('/api/auth/me', 'GET'), ('/api/auth/register', 'POST')])
    eq('the nine pre-existing operations keep their served paths',
       [r for r in rows if not r[0].startswith('/api/auth')],
       [('/api/listings/listings', 'GET'),
        ('/api/listings/listings', 'POST'),
        ('/api/listings/listings/{listing_id}', 'DELETE'),
        ('/api/listings/listings/{listing_id}', 'GET'),
        ('/api/listings/listings/{listing_id}', 'PUT'),
        ('/api/messages/messages', 'GET'),
        ('/api/messages/messages', 'POST'),
        ('/api/transactions/transactions', 'POST'),
        ('/api/transactions/transactions/{transaction_id}', 'GET')])
    eq('OpenAPI publishes tokenUrl /api/auth/login',
       app.main.app.openapi()['components']['securitySchemes']
       ['OAuth2PasswordBearer']['flows']['password']['tokenUrl'],
       '/api/auth/login')
    eq('GET /openapi.json answers 200',
       client.get('/openapi.json').status_code, 200)


def gate_auth(app, store, client, jwt, settings):
    fresh = client.post('/api/auth/register', json={
        'email': ' Seller@Harness.TEST ', 'password': PASSWORD,
        'first_name': ' Sam ', 'last_name': 'Seller', 'role': 'Seller'})
    eq('a fresh registration answers 201', fresh.status_code, 201)
    eq('it returns exactly four keys', sorted(fresh.json()),
       ['access_token', 'token', 'token_type', 'user'])
    ok('no hash appears anywhere in that payload',
       'hashed_password' not in fresh.text and '$2b$' not in fresh.text)
    eq('one user document is written', store.count('users'), 1)
    eq('the token response is not cacheable',
       fresh.headers.get('cache-control'), 'no-store')
    eq('the address is stored trimmed and lower-cased',
       fresh.json()['user']['email'], 'seller@harness.test')
    eq('the role is stored lower-cased, so the seller gate matches it',
       fresh.json()['user']['role'], 'seller')
    eq('the name is stored trimmed', fresh.json()['user']['first_name'], 'Sam')
    eq('one uniqueness marker is written beside the account',
       store.count('user_emails'), 1)

    again = client.post('/api/auth/register', json={
        'email': 'SELLER@harness.test', 'password': 'another-password',
        'role': 'seller', 'first_name': 'Sam', 'last_name': 'Seller'})
    eq('a duplicate email answers 409 with its own detail',
       (again.status_code, again.json()['detail']),
       (409, 'Email is already registered'))
    eq('and writes no second document', store.count('users'), 1)

    # The role a caller may give itself, and every field bound. Each of these
    # is a 422 raised by a validator, so none of them reaches Firestore.
    def registration(**overrides):
        body = {'email': 'fresh@harness.test', 'password': PASSWORD,
                'first_name': 'Fresh', 'last_name': 'Caller', 'role': 'buyer'}
        body.update(overrides)
        return client.post('/api/auth/register', json=body)

    writes = store.writes
    for label, overrides in (
            ('role admin', {'role': 'admin'}),
            ('role root', {'role': 'root'}),
            ('a blank role', {'role': '  '}),
            ('an address with no domain', {'email': 'not-an-address'}),
            ('an address with an empty label', {'email': 'a@b..test'}),
            ('an over-long address',
             {'email': 'x' * 250 + '@harness.test'}),
            ('a seven-character password', {'password': 'sevench'}),
            ('a password over 72 bytes', {'password': 'p' * 73}),
            ('a password over 72 bytes once encoded',
             {'password': 'p' * 60 + 'é' * 7}),
            ('a blank name', {'first_name': '   '}),
            ('an over-long name', {'last_name': 'n' * 101})):
        eq('registration with %s answers 422' % label,
           registration(**overrides).status_code, 422)
    eq('and none of them wrote anything',
       (store.writes, store.count('users')), (writes, 1))

    # The marker is the atomic half of uniqueness: a query cannot stop two
    # simultaneous requests, a conditional write can. Claiming the address
    # first is what a concurrent registration effectively does.
    claimed = 'race@harness.test'
    store.collection('user_emails').document(
        app.api.auth._email_key(claimed)).set({'user_id': 'someone-else'})
    raced = registration(email=claimed)
    eq('an address already claimed by a marker answers 409',
       (raced.status_code, raced.json()['detail']),
       (409, 'Email is already registered'))
    eq('and writes no account for it', store.count('users'), 1)

    good = client.post('/api/auth/login', json={
        'email': ' SELLER@harness.TEST ', 'password': PASSWORD})
    eq('login accepts the same address in any case or spacing',
       good.status_code, 200)
    eq('the token response is not cacheable',
       good.headers.get('cache-control'), 'no-store')
    eq('access_token and token carry one value',
       good.json()['access_token'], good.json()['token'])
    eq('the public user comes back with it',
       good.json()['user']['email'], 'seller@harness.test')

    wrong = client.post('/api/auth/login', json={
        'email': 'seller@harness.test', 'password': 'not-the-password'})
    eq('a wrong password answers 401 with WWW-Authenticate: Bearer',
       (wrong.status_code, wrong.headers.get('www-authenticate')),
       (401, 'Bearer'))
    unknown = client.post('/api/auth/login', json={
        'email': 'nobody@harness.test', 'password': PASSWORD})
    eq('an unknown email answers 401 with the identical detail',
       (unknown.status_code, unknown.json()['detail']),
       (401, wrong.json()['detail']))

    token = good.json()['token']
    headers = {'Authorization': 'Bearer ' + token}
    me = client.get('/api/auth/me', headers=headers)
    eq('GET /me with a valid token answers 200', me.status_code, 200)
    eq('/me returns exactly the seven public fields', sorted(me.json()
       ['user']), ['created_at', 'email', 'first_name', 'id', 'last_name',
                   'role', 'updated_at'])
    ok('no hash appears in the /me payload either',
       'hashed_password' not in me.text and '$2b$' not in me.text)
    for label, sent in (('no token', {}),
                        ('a malformed token',
                         {'Authorization': 'Bearer not.a.jwt'})):
        refused = client.get('/api/auth/me', headers=sent)
        eq('/me with %s answers 401 and the Bearer challenge' % label,
           (refused.status_code, refused.headers.get('www-authenticate')),
           (401, 'Bearer'))
    out = client.post('/api/auth/logout', headers=headers)
    eq('logout answers 200 and acknowledges',
       (out.status_code, out.json()), (200, {'detail': 'Logged out'}))
    eq('the profile response is not cacheable either',
       me.headers.get('cache-control'), 'no-store')

    claims = jwt.decode(token, settings.SECRET_KEY,
                        algorithms=[settings.ALGORITHM])
    eq('the token carries exactly sub and exp', sorted(claims),
       ['exp', 'sub'])
    lifetime = claims['exp'] - int(time.time())
    ok('the configured lifetime governs, not the 15-minute fallback',
       3480 < lifetime <= 3600, '%d seconds' % lifetime)


def gate_listings(app, store, client, log):
    seller, as_seller = register(client, 'sell@harness.test', 'seller')
    buyer, as_buyer = register(client, 'buy@harness.test', 'buyer')
    refused = client.post('/api/auth/register', json={
        'email': 'admin@harness.test', 'password': PASSWORD,
        'first_name': 'No', 'last_name': 'Admin', 'role': 'admin'})
    eq('nobody can register itself as an administrator',
       refused.status_code, 422)
    admin, as_admin = register(client, 'admin@harness.test', 'buyer')
    promote_to_admin(store, admin)
    vision = Spy(result={'type': 'Car'})
    with spying(app.api.listings, 'analyze_vehicle_photo', vision):
        made = client.post('/api/listings/listings', headers=as_seller,
                           json=listing_body(photos=['https://a/1.jpg',
                                                     'https://a/2.jpg']))
    eq('two photos answer 200', made.status_code, 200)
    eq('the vision helper is called once per photo', len(vision.calls), 2)
    eq('each argument arrives as encoded bytes',
       [call[0][0] for call in vision.calls],
       [b'https://a/1.jpg', b'https://a/2.jpg'])
    listing_id = made.json()['id']
    eq('every result is stored against the listing',
       store.docs('listings')[listing_id]['photo_analysis'],
       [{'type': 'Car'}, {'type': 'Car'}])

    idle = Spy(result={'type': 'Car'})
    with spying(app.api.listings, 'analyze_vehicle_photo', idle):
        none = client.post('/api/listings/listings', headers=as_seller,
                           json=listing_body())
    eq('an empty photo list answers 200 without calling the helper',
       (none.status_code, len(idle.calls)), (200, 0))

    log.seek(0), log.truncate(0)
    failing = Spy(raises=RuntimeError('vision provider unavailable'))
    with spying(app.api.listings, 'analyze_vehicle_photo', failing):
        degraded = client.post('/api/listings/listings', headers=as_seller,
                               json=listing_body(photos=['https://a/1.jpg',
                                                         'https://a/2.jpg']))
    logged = log.getvalue()
    eq('every photo failing still answers 200', degraded.status_code, 200)
    eq('the analysis degrades to empty instead of failing the request',
       store.docs('listings')[degraded.json()['id']]['photo_analysis'], [])
    eq('each failure is logged once', logged.count('photo analysis failed'), 2)
    eq('both lines share one correlation id',
       len({line.split('[correlation_id=')[1].split()[0].rstrip(']')
            for line in logged.splitlines() if '[correlation_id=' in line}), 1)
    eq('each line names the class of failure',
       logged.count('error_type=RuntimeError'), 2)
    ok('and neither the traceback nor the provider text is emitted at INFO',
       'Traceback (most recent call last):' not in logged
       and 'vision provider unavailable' not in logged, logged[-400:])

    # The detail still exists; it lives behind DEBUG, which an operator opts
    # into, rather than in the default output of a production service.
    log.seek(0), log.truncate(0)
    namespace = logging.getLogger('app')
    namespace.setLevel(logging.DEBUG)
    try:
        with spying(app.api.listings, 'analyze_vehicle_photo',
                    Spy(raises=RuntimeError('vision provider unavailable'))):
            client.post('/api/listings/listings', headers=as_seller,
                        json=listing_body(photos=['https://a/1.jpg']))
    finally:
        namespace.setLevel(logging.INFO)
    debugged = log.getvalue()
    ok('at DEBUG the traceback and the provider text are both available',
       'Traceback (most recent call last):' in debugged
       and 'RuntimeError: vision provider unavailable' in debugged,
       debugged[-400:])

    # The photo list is bounded before the first provider call, so an
    # unbounded request cannot amplify into arbitrarily many of them.
    idle_after_bounds = Spy(result={'type': 'Car'})
    with spying(app.api.listings, 'analyze_vehicle_photo',
                idle_after_bounds):
        writes = store.writes
        for label, photos in (
                ('more than 12 photos', ['https://a/%d.jpg' % n
                                         for n in range(13)]),
                ('a photo entry over 256 KiB', ['u' * 262145]),
                ('an aggregate over 900 KiB', ['u' * 250000] * 4),
                ('a blank photo entry', ['https://a/1.jpg', ''])):
            eq('%s answers 422' % label,
               client.post('/api/listings/listings', headers=as_seller,
                           json=listing_body(photos=photos)).status_code, 422)
        eq('and none of them called the provider or wrote anything',
           (len(idle_after_bounds.calls), store.writes), (0, writes))

    with spying(app.api.listings, 'analyze_vehicle_photo', Spy(result={})):
        eq('a caller who is not a seller answers 403',
           client.post('/api/listings/listings', headers=as_buyer,
                       json=listing_body()).status_code, 403)
        eq('more than 20 maintenance records answers 422',
           client.post('/api/listings/listings', headers=as_seller,
                       json=listing_body(records=[
                           {'content': 'oil change', 'format': 'text'}] * 21)
                       ).status_code, 422)
        heavy = listing_body(records=[
            {'content': 'x' * 500000, 'format': 'text'},
            {'content': 'y' * 500000, 'format': 'text'}])
        eq('more than 900 KiB of maintenance content answers 422',
           client.post('/api/listings/listings', headers=as_seller,
                       json=heavy).status_code, 422)
        # Inside both input bounds and still over the document ceiling, which
        # is what keeps the serialized-size guard reachable and asserted. The
        # log line is asserted too, so this case cannot pass by tripping one
        # of the input bounds instead of the guard it is here to cover.
        log.seek(0), log.truncate(0)
        eq('a serialized listing over 1 MB answers 422',
           client.post('/api/listings/listings', headers=as_seller,
                       json=listing_body(
                           photos=['u' * 250000] * 3,
                           records=[{'content': 'x' * 450000,
                                     'format': 'text'}] * 2)
                       ).status_code, 422)
        ok('and it is the serialized-size guard that refused it',
           'listing exceeds serialized size budget' in log.getvalue(),
           log.getvalue()[-200:])
    body = listing_body()
    eq('the owner may update the listing',
       client.put('/api/listings/listings/' + listing_id, headers=as_seller,
                  json=body).status_code, 200)
    eq('another seller may not update it',
       client.put('/api/listings/listings/' + listing_id, headers=as_buyer,
                  json=body).status_code, 403)
    eq('a non-owner who is not an admin may not delete it',
       client.delete('/api/listings/listings/' + listing_id,
                     headers=as_buyer).status_code, 403)
    eq('an admin may delete another seller listing',
       client.delete('/api/listings/listings/' + listing_id,
                     headers=as_admin).status_code, 200)


def gate_purchase(app, store, client):
    buyer, as_buyer = register(client, 'buy@harness.test', 'buyer')
    seller, as_seller = register(client, 'sell@harness.test', 'seller')
    store.collection('vehicles').document('veh-1').set(
        {'status': 'available'})
    body = purchase_body(buyer['id'], seller['id'], 'veh-1')

    settled = Spy(result={'success': True, 'charge_id': 'ch_harness',
                          'amount': 15000.0, 'currency': 'usd',
                          'status': 'succeeded'})
    with spying(app.api.transactions, 'process_payment', settled):
        bought = client.post('/api/transactions/transactions',
                             headers=as_buyer, json=body)
    eq('a purchase answers 200', bought.status_code, 200)
    eq('the provider receives exactly (token, amount, currency)',
       settled.calls, [(('pi_harness_abc', 15000.0, 'usd'), {})])
    eq('the transaction is recorded as completed',
       (bought.json()['status'], store.count('transactions')),
       ('completed', 1))
    eq('the vehicle is marked sold', store.docs('vehicles')['veh-1'],
       {'status': 'sold'})
    recorded = next(iter(store.docs('transactions')))

    for label, result, intent in (
            ('a declined payment',
             {'success': False, 'status': 'failed'}, 'pi_declined'),
            ('a result with no success key', {}, 'pi_malformed')):
        store.docs('vehicles')['veh-1'] = {'status': 'available'}
        writes = store.writes
        with spying(app.api.transactions, 'process_payment', Spy(result)):
            refused = client.post(
                '/api/transactions/transactions', headers=as_buyer,
                json=purchase_body(buyer['id'], seller['id'], 'veh-1',
                                   intent=intent))
        eq('%s answers 400 and writes nothing' % label,
           (refused.status_code, store.writes, store.count('transactions'),
            store.docs('vehicles')['veh-1']),
           (400, writes, 1, {'status': 'available'}))

    # Replaying a payment intent that has already been charged must not reach
    # the provider a second time.
    store.docs('vehicles')['veh-1'] = {'status': 'available'}
    replayed = Spy(result={'success': True, 'charge_id': 'ch_again'})
    with spying(app.api.transactions, 'process_payment', replayed):
        again = client.post('/api/transactions/transactions',
                            headers=as_buyer, json=body)
    eq('a repeated payment intent answers 409 without charging again',
       (again.status_code, len(replayed.calls), store.count('transactions')),
       (409, 0, 1))

    # The amount and the seller are settled against the vehicle record wherever
    # it carries them, so a caller cannot name its own price or its own seller.
    priced = Spy(result={'success': True, 'charge_id': 'ch_priced'})
    store.collection('vehicles').document('veh-2').set(
        {'status': 'available', 'price': 15000.0, 'seller_id': seller['id']})
    with spying(app.api.transactions, 'process_payment', priced):
        agreed = client.post('/api/transactions/transactions',
                             headers=as_buyer,
                             json=purchase_body(buyer['id'], seller['id'],
                                                'veh-2', intent='pi_agreed'))
    eq('terms that match the vehicle record answer 200',
       (agreed.status_code, len(priced.calls)), (200, 1))

    for label, record, sent in (
            ('an amount the record disagrees with',
             {'status': 'available', 'price': 20000.0,
              'seller_id': seller['id']}, {'amount': 15000.0}),
            ('a seller the record disagrees with',
             {'status': 'available', 'price': 15000.0,
              'seller_id': 'someone-else'}, {})):
        store.collection('vehicles').document('veh-3').set(record)
        writes = store.writes
        untouched = Spy(result={'success': True})
        with spying(app.api.transactions, 'process_payment', untouched):
            wrong = client.post(
                '/api/transactions/transactions', headers=as_buyer,
                json=dict(purchase_body(buyer['id'], seller['id'], 'veh-3',
                                        intent='pi_wrong_' + label[:6]),
                          **sent))
        eq('%s answers 400 before the charge' % label,
           (wrong.status_code, len(untouched.calls), store.writes),
           (400, 0, writes))
        eq('and the vehicle is left alone',
           store.docs('vehicles')['veh-3']['status'], 'available')

    with spying(app.api.transactions, 'process_payment', Spy({})):
        eq('a caller who is not the buyer answers 403',
           client.post('/api/transactions/transactions', headers=as_seller,
                       json=purchase_body(buyer['id'], seller['id'], 'veh-1',
                                          intent='pi_not_buyer')
                       ).status_code, 403)
        eq('an absent vehicle answers 400',
           client.post('/api/transactions/transactions', headers=as_buyer,
                       json=purchase_body(buyer['id'], seller['id'], 'gone',
                                          intent='pi_absent')
                       ).status_code, 400)
        store.docs('vehicles')['veh-1'] = {'status': 'sold'}
        eq('a vehicle that is not available answers 400',
           client.post('/api/transactions/transactions', headers=as_buyer,
                       json=purchase_body(buyer['id'], seller['id'], 'veh-1',
                                          intent='pi_sold')
                       ).status_code, 400)
    eq('a participant may read the transaction',
       client.get('/api/transactions/transactions/' + recorded,
                  headers=as_buyer).status_code, 200)
    outsider, as_outsider = register(client, 'third@harness.test', 'buyer')
    eq('a third party may not', client.get(
        '/api/transactions/transactions/' + recorded,
        headers=as_outsider).status_code, 403)
    eq('an unknown transaction answers 404', client.get(
        '/api/transactions/transactions/nope',
        headers=as_buyer).status_code, 404)


def gate_limits(app, store, client):
    """The two public routes are throttled, and the refusal says nothing.

    The ceiling itself is reached by calling the limiter directly rather than
    by sending ten sign-ins: each of those would cost a real bcrypt
    verification, and a burst that slow can straddle a window boundary, which
    looks exactly like a ceiling that does not work. One HTTP request then
    proves the refusal reaches the client with its header.
    """
    auth = app.api.auth
    reset_limits(app)
    register(client, 'limited@harness.test', 'buyer')

    def fill(bucket, subject, ceiling):
        # Start inside a fresh window, so the count cannot be split across two.
        remaining = auth._RATE_LIMIT_WINDOW_SECONDS - (
            time.time() % auth._RATE_LIMIT_WINDOW_SECONDS)
        if remaining < 5:
            time.sleep(remaining + 0.1)
        reset_limits(app)
        for _ in range(ceiling):
            auth._enforce_attempt_limit(bucket, subject, ceiling)

    fill('login-peer', 'testclient', auth._MAX_LOGINS_PER_WINDOW)
    refused = client.post('/api/auth/login', json={
        'email': 'limited@harness.test', 'password': PASSWORD})
    eq('a sign-in past the ceiling answers 429 with Retry-After',
       (refused.status_code, bool(refused.headers.get('retry-after'))),
       (429, True))
    ok('the refusal says nothing about the account',
       'limited@harness.test' not in refused.text
       and 'account' not in refused.json()['detail'].lower(),
       refused.json())
    unknown = client.post('/api/auth/login', json={
        'email': 'never-registered@harness.test', 'password': PASSWORD})
    eq('an unknown address is refused by the same counter, not distinguished',
       (unknown.status_code, unknown.json()['detail']),
       (429, refused.json()['detail']))

    reset_limits(app)
    good = client.post('/api/auth/login', json={
        'email': 'limited@harness.test', 'password': PASSWORD})
    eq('a correct credential answers 200 once the window is clear',
       good.status_code, 200)
    ok('and clears the counters it was charged against',
       not any(subject in ('limited@harness.test', 'testclient')
               for _, subject in auth._rate_counters))

    fill('register', 'testclient', auth._MAX_REGISTRATIONS_PER_WINDOW)
    writes = store.writes
    beyond = client.post('/api/auth/register', json={
        'email': 'beyond@harness.test', 'password': PASSWORD,
        'first_name': 'Beyond', 'last_name': 'Ceiling', 'role': 'buyer'})
    eq('a registration past the ceiling answers 429', beyond.status_code, 429)
    eq('and writes neither the account nor its marker',
       (store.writes, store.count('users'), store.count('user_emails')),
       (writes, 1, 1))

    # Counters are pruned, so the map is bounded by live traffic rather than by
    # every address ever submitted.
    for index in range(50):
        auth._enforce_attempt_limit('login-account', 'probe%d' % index, 99)
    stale = (int(time.time() // auth._RATE_LIMIT_WINDOW_SECONDS) - 4, 1)
    auth._rate_counters[('login-account', 'ancient')] = stale
    auth._enforce_attempt_limit('login-account', 'trigger-prune', 99)
    ok('a window that can no longer be consulted is dropped',
       ('login-account', 'ancient') not in auth._rate_counters)
    reset_limits(app)


def gate_cors(client):
    request = {'Access-Control-Request-Method': 'POST',
               'Access-Control-Request-Headers': 'content-type'}
    allowed = client.options('/api/auth/login', headers=dict(
        request, Origin='http://localhost:3000'))
    eq('a preflight from an allowed origin answers 200 and echoes it',
       (allowed.status_code,
        allowed.headers.get('access-control-allow-origin'),
        allowed.headers.get('access-control-allow-credentials')),
       (200, 'http://localhost:3000', 'true'))
    denied = client.options('/api/auth/login', headers=dict(
        request, Origin='http://evil.harness.test'))
    eq('a preflight from any other origin answers 400 with no allow header',
       (denied.status_code,
        'access-control-allow-origin' in denied.headers), (400, False))


def gate_messaging(app, store, TestClient):
    """HCF-8: both message routes are mounted, and neither works.

    Server exceptions become responses here so the two known defects are
    observed rather than raised. This is what substantiates "13 registered,
    11 functional" instead of asserting it.
    """
    with TestClient(app.main.app, raise_server_exceptions=False) as client:
        sender, as_sender = register(client, 'sender@harness.test', 'buyer')
        recipient, _ = register(client, 'recipient@harness.test', 'seller')
        missing = client.post('/api/messages/messages', headers=as_sender,
                              json={'recipient_id': 'nobody',
                                    'content': 'hello'})
        eq('the route is mounted: an unknown recipient is its own 404',
           (missing.status_code, missing.json()['detail']),
           (404, 'Recipient user not found'))
        sent = client.post('/api/messages/messages', headers=as_sender,
                           json={'recipient_id': recipient['id'],
                                 'content': 'hello'})
        eq('sending still fails on the unserializable timestamp sentinel',
           sent.status_code, 500)
        eq('the document is written before that failure',
           store.count('messages'), 1)
        eq('listing still fails on the duplicated id keyword',
           client.get('/api/messages/messages',
                      headers=as_sender).status_code, 500)

        # The body is bounded by the schema, so an unbounded or blank message
        # is refused before the recipient lookup and before the write that
        # HCF-8 cannot yet make safe.
        writes = store.writes
        for label, content in (('a blank body', '   '),
                               ('a body over 4000 characters', 'x' * 4001),
                               ('a body over 16 KiB once encoded',
                                'é' * 9000)):
            eq('%s answers 422' % label,
               client.post('/api/messages/messages', headers=as_sender,
                           json={'recipient_id': recipient['id'],
                                 'content': content}).status_code, 422)
        eq('and none of them wrote a message',
           (store.writes, store.count('messages')), (writes, 1))


def gate_logging(app):
    formatter = app.main.ContextFormatter('%(levelname)s %(name)s: '
                                          '%(message)s')

    def record(message='hello', exc_info=None, **context):
        item = logging.LogRecord('app.probe', logging.INFO, __file__, 1,
                                 message, None, exc_info)
        for key, value in context.items():
            setattr(item, key, value)
        return item

    eq('a record with no context renders no brackets',
       formatter.format(record()), 'INFO app.probe: hello')
    eq('context renders in alphabetical order', formatter.format(
        record(listing_id='l1', correlation_id='c1')),
       'INFO app.probe: hello [correlation_id=c1 listing_id=l1]')
    eq('backslash, newline, CR and tab are escaped, so no value forges a line',
       formatter.format(record(note='a\nb\rc\td\\e')),
       'INFO app.probe: hello [note=a\\nb\\rc\\td\\\\e]')
    ok('no standard LogRecord field is rendered as context',
       all(field not in formatter.format(record(k='v')) for field in
           ('levelno=', 'msg=', 'args=', 'pathname=', 'lineno=', 'name=')))
    try:
        raise RuntimeError('boom')
    except RuntimeError:
        failed = record('photo analysis failed', sys.exc_info(),
                        correlation_id='c9')
    rendered = formatter.format(failed).splitlines()
    eq('the context stays on the message line, above the traceback',
       (rendered[0], rendered[1]),
       ('INFO app.probe: photo analysis failed [correlation_id=c9]',
        'Traceback (most recent call last):'))
    ok('the exception itself is rendered', 'RuntimeError: boom' in
       '\n'.join(rendered))

    namespace = logging.getLogger('app')
    handler = namespace.handlers[0]
    eq('the app namespace carries exactly one handler',
       len(namespace.handlers), 1)
    ok('it formats with ContextFormatter and the documented format string',
       isinstance(handler.formatter, app.main.ContextFormatter)
       and handler.formatter._fmt ==
       '%(asctime)s %(levelname)s %(name)s: %(message)s')
    eq('the namespace is INFO with propagation off',
       (namespace.level, namespace.propagate), (logging.INFO, False))
    app.main._configure_application_logging()
    eq('configuring again adds no second handler',
       (len(namespace.handlers), namespace.handlers[0]), (1, handler))

    saved = (list(namespace.handlers), namespace.level, namespace.propagate)
    operator = logging.NullHandler()
    try:
        namespace.handlers = [operator]
        namespace.setLevel(logging.WARNING)
        namespace.propagate = True
        app.main._configure_application_logging()
        eq('a namespace an operator already configured is left untouched',
           (namespace.handlers, namespace.level, namespace.propagate),
           ([operator], logging.WARNING, True))
    finally:
        namespace.handlers, level, propagate = saved
        namespace.setLevel(level)
        namespace.propagate = propagate

    stream, root_stream = io.StringIO(), io.StringIO()
    handler.setStream(stream)
    root_handler = logging.StreamHandler(root_stream)
    logging.getLogger().addHandler(root_handler)
    try:
        logging.getLogger('app.probe').info('probe line', extra={'k': 'v'})
    finally:
        logging.getLogger().removeHandler(root_handler)
    eq('an INFO record reaches the app handler exactly once',
       stream.getvalue().count('probe line [k=v]'), 1)
    eq('and never reaches a root handler as a duplicate',
       root_stream.getvalue(), '')


def gate_settings():
    """Each form in its own process: Settings is built once, at import."""
    probe = ('from app.core.config import settings\n'
             'import json; print(json.dumps(settings.ALLOWED_ORIGINS))')

    def parse(value):
        environment = dict(os.environ)
        if value is None:
            environment.pop('ALLOWED_ORIGINS', None)
        else:
            environment['ALLOWED_ORIGINS'] = value
        done = subprocess.run([sys.executable, '-c', probe], env=environment,
                              capture_output=True, text=True)
        if done.returncode:
            return done.stderr.strip().splitlines()[-1]
        return json.loads(done.stdout)

    for label, value, expected in (
            ('unset falls back to the default', None,
             ['http://localhost:3000']),
            ('one origin parses', 'http://localhost:3000',
             ['http://localhost:3000']),
            ('a comma-separated list parses', 'http://a.test, http://b.test',
             ['http://a.test', 'http://b.test']),
            ('a JSON array parses', '["http://a.test", "http://b.test"]',
             ['http://a.test', 'http://b.test']),
            ('an empty value yields no origins rather than raising', '', []),
            ('whitespace yields no origins rather than raising', '   ', [])):
        eq('ALLOWED_ORIGINS: ' + label, parse(value), expected)

    for label, value in (('a bare wildcard', '*'),
                         ('a wildcard subdomain', 'https://*.example.com'),
                         ('a wildcard beside a real origin', 'http://a.test,*')):
        refused = parse(value)
        ok('ALLOWED_ORIGINS: %s is refused at import' % label,
           isinstance(refused, str) and 'may not contain a wildcard' in refused,
           refused)

    for label, overrides, fragment in (
            ('a short SECRET_KEY', {'SECRET_KEY': 'too-short'},
             'at least 32 characters'),
            ('a zero token lifetime', {'ACCESS_TOKEN_EXPIRE_MINUTES': '0'},
             'at least 1 minute'),
            ('a token lifetime over a day',
             {'ACCESS_TOKEN_EXPIRE_MINUTES': '4321'}, 'must not exceed'),
            ('an unsupported ALGORITHM', {'ALGORITHM': 'none'},
             'must be one of')):
        environment = dict(os.environ, ALLOWED_ORIGINS='http://localhost:3000')
        environment.update(overrides)
        done = subprocess.run([sys.executable, '-c', probe], env=environment,
                              capture_output=True, text=True)
        ok('settings: %s is refused at import' % label,
           done.returncode != 0 and fragment in done.stderr,
           done.stderr.strip()[-160:])

    without = dict(os.environ)
    without.pop('SECRET_KEY')
    done = subprocess.run([sys.executable, '-c', probe], env=without,
                          capture_output=True, text=True)
    ok('an unset required setting still refuses to import',
       done.returncode != 0 and 'validation error' in done.stderr.lower(),
       done.stderr.strip()[-120:])


def main():
    store = Firestore()
    with ExitStack() as stack:
        # sys.modules is mutated through patch.dict so the entry is removed
        # again on exit, and both client constructors are replaced before
        # the application imports them, because both are built at import.
        stack.enter_context(mock.patch.dict(sys.modules,
                                            {'stripe': stripe_stand_in()}))
        stack.enter_context(mock.patch('google.cloud.firestore.Client',
                                       lambda *a, **k: store))
        stack.enter_context(mock.patch(
            'google.cloud.vision.ImageAnnotatorClient', lambda *a, **k: None))
        from fastapi.testclient import TestClient
        from jose import jwt
        import app.api.auth              # noqa: F401  probed below
        import app.api.listings          # noqa: F401  patched below
        import app.api.transactions      # noqa: F401  patched below
        import app.db.firestore
        import app.main
        from app.core.config import settings

        ok('fail closed: the application holds the double, not a client',
           app.db.firestore.db is store)
        ok('fail closed: no emulator or credential variable is in the '
           'environment', not any(os.environ.get(n) for n in FORBIDDEN_ENV))

        log = io.StringIO()
        logging.getLogger('app').handlers[0].setStream(log)
        # TestClient as a context manager, so the startup event runs; a bare
        # TestClient(app) never fires it.
        with TestClient(app.main.app) as client:
            gate_composition(app, client, log)
            store.reset(), reset_limits(app)
            gate_auth(app, store, client, jwt, settings)
            store.reset(), reset_limits(app)
            gate_listings(app, store, client, log)
            store.reset(), reset_limits(app)
            gate_purchase(app, store, client)
            store.reset()
            gate_limits(app, store, client)
            gate_cors(client)
        store.reset(), reset_limits(app)
        gate_messaging(app, store, TestClient)
        gate_logging(app)
        gate_settings()

    passed = sum(RESULTS)
    print('\n%d assertions, %d passed, %d failed'
          % (len(RESULTS), passed, len(RESULTS) - passed))
    return 0 if passed == len(RESULTS) else 1


if __name__ == '__main__':
    sys.exit(main())
```

## 6.4 WHAT IT ASSERTS

Two fail-closed preflight assertions and nine gates, 139 assertions in all. The order matters: the preflight first, because a run that reached real infrastructure would be worse than no run; composition next, because nothing else can be true if the application does not compose; and the settings gate last, because it spawns subprocesses.

| Gate | Assertions | What it establishes |
| --- | --- | --- |
| Fail closed | 2 | The application under test holds the in-memory double rather than a Firestore client, and no credential or emulator variable survives in the environment. |
| A — composition | 7 | The startup hook logs exactly one line; 13 routes register across 9 unique paths; the four authentication operations are exactly `POST /register`, `POST /login`, `POST /logout`, `GET /me` under `/api/auth`; the nine pre-existing operations still serve the paths in [section 2.7](#27-the-routes-as-actually-served), character for character; OpenAPI publishes `tokenUrl: /api/auth/login`; `GET /openapi.json` answers 200. |
| B — authentication and its policy | 40 | The flow: a fresh registration answers 201 with exactly `access_token`, `token_type`, `token`, `user`, one user document **and one `user_emails` marker**, and `Cache-Control: no-store`; a repeat of that address in different case answers 409 with `Email is already registered` and writes nothing more; a correct login answers 200 with `access_token` equal to `token`, again `no-store`; a wrong password and an unknown email both answer 401 with an identical detail and `WWW-Authenticate: Bearer`; `GET /me` answers 200 with exactly the seven public fields and `no-store`; `/me` with no token and with a malformed token both answer 401 with the same challenge; logout answers 200 with `{"detail": "Logged out"}`; the issued token carries exactly `sub` and `exp`, with the configured hour rather than the helper's 15-minute fallback. The policy: the address, the role and the names come back trimmed and lower-cased where they should be; eleven bad bodies — three role values, three addresses, three passwords, two names — each answer 422 with the write counter unchanged; and an address whose marker already exists answers 409 with no account written. No response payload contains `hashed_password` or a bcrypt prefix — checked against the raw response text, not the parsed body, so a nested occurrence cannot slip through. |
| C — listing creation and its bounds | 27 | Two photos produce exactly two calls into the vision helper, each receiving one `bytes` payload, and both results are stored on the listing document; an empty photo list calls it not at all; every photo raising still answers 200, stores an empty analysis, and logs `photo analysis failed` twice with one shared `correlation_id` and an `error_type` — **and with no traceback and no provider text at `INFO`**, while the same failure at `DEBUG` carries both. Four unbounded photo requests — thirteen entries, an over-256-KiB entry, an over-900-KiB aggregate, a blank entry — each answer 422 with **zero** provider calls and zero writes. A caller who is not a seller answers 403; the record-count, aggregate-content and serialized-size guards each answer 422, the last one asserted through its own log line so it cannot pass by tripping a photo bound instead; the owner may update, another seller may not, a non-owner who is not an admin may not delete, and an administrator — created by an out-of-band write, because registration refuses the role — may. |
| D — the purchase path and its guards | 18 | A purchase answers 200; the provider receives exactly `('pi_harness_abc', 15000.0, 'usd')` — three positional arguments, the payment-intent id as the token, the pinned currency; the transaction document is written once with `status: 'completed'` and the vehicle becomes `{'status': 'sold'}`. A declined result and a result with **no** `success` key both answer 400 with the store's write counter unchanged, no transaction recorded and the vehicle still `'available'`. Replaying a payment intent already recorded answers 409 with **zero** provider calls. Terms that match a vehicle record carrying `price` and `seller_id` answer 200; a disagreeing amount and a disagreeing seller each answer 400 before the charge, with the vehicle untouched. A caller who is not the buyer answers 403; an absent vehicle and a vehicle that is not `'available'` answer 400; a participant may read the transaction, a third party answers 403, an unknown id answers 404. |
| E — attempt limits | 8 | The ceiling is reached by calling the limiter directly rather than by sending ten real sign-ins, because each of those costs a bcrypt verification and a burst that slow can straddle a window boundary — which looks exactly like a ceiling that does not work. One HTTP request then proves the refusal reaches the client: 429 with a `Retry-After` header and a detail naming neither the address nor its existence; an unknown address is refused by the same counter rather than distinguished; a correct credential answers 200 once the window is clear and clears the counters it was charged against; a registration past its own ceiling answers 429 and writes neither account nor marker; and a window that can no longer be consulted is pruned, so the counter map is bounded by live traffic. |
| F — CORS | 2 | A preflight from `http://localhost:3000` answers 200 with the origin echoed and `access-control-allow-credentials: true`; a preflight from any other origin answers 400 with no allow-origin header at all. |
| G — messaging | 8 | Both message routes are mounted — an unknown recipient produces the handler's own 404, not a route miss — and both are still broken: sending answers 500 on the unserializable `SERVER_TIMESTAMP` sentinel *after* the document has been written, and listing answers 500 on the duplicated `id` keyword (**HCF-8**). What *is* enforced is the body: blank, over 4000 characters, and over 16 KiB once encoded each answer 422 with nothing written. This is what makes "13 registered, 11 functional" a measurement rather than a claim. |
| H — logging | 13 | See the table below. |
| I — settings policy | 14 | Six accepted `ALLOWED_ORIGINS` forms — unset, one origin, comma-separated, JSON array, empty, whitespace — each parsed in its own subprocess, because `Settings` is constructed once at import and a value cannot be re-read in a live process; three wildcard forms each **refused** at import; a `SECRET_KEY` under 32 characters, a zero and an over-a-day token lifetime, and an unsupported `ALGORITHM` each refused; plus proof that unsetting a required variable still refuses the import with a `ValidationError`. |

Gate H covers the logging code in `backend/app/main.py` branch by branch, because that code is what decides whether a diagnostic line is legible, forgeable or printed twice:

| Behaviour | Assertion |
| --- | --- |
| A record with no `extra` renders no empty brackets | formatted output equals the format string's own result |
| Several context keys render in a stable order | `[correlation_id=c1 listing_id=l1]`, alphabetical, from an unordered `extra` |
| A context value cannot forge a log line | backslash, newline, carriage return and tab all escaped, backslash first so the escapes are unambiguous |
| No standard `LogRecord` attribute leaks into the context | none of `levelno=`, `msg=`, `args=`, `pathname=`, `lineno=`, `name=` appears |
| Context belongs to the message line, not the traceback | with `exc_info` set, line 1 ends in `[correlation_id=c9]` and line 2 is `Traceback (most recent call last):` |
| The exception itself still reaches the log | `RuntimeError: boom` present in the rendered record |
| The namespace is configured once, and correctly | exactly one handler on `app`, a `ContextFormatter`, the documented format string, level `INFO` |
| Reconfiguration is idempotent | calling `_configure_application_logging()` again leaves the same single handler object |
| An operator's configuration is never overwritten | with a handler already attached and level `WARNING`, propagation on, the call changes nothing |
| Nothing prints twice | one `app.*` `INFO` record reaches the app handler exactly once and a root handler not at all |

## 6.5 WHAT IT DOES NOT COVER

Stated plainly, because a coverage claim is worth nothing without its boundary:

- **No real provider traffic.** Cloud Vision and Stripe are stood in for, so the defects inside them (**HCF-3**, **HCF-4**) are neither reproduced nor cleared here. The harness proves the *call sites* are correct — arity, types, synchronicity, how the result is read — and nothing about the providers themselves.
- **The double is not an emulator.** It models call and response shape only: no index requirements, no ordering guarantees, no transactions, and no concurrency of any kind. Gate B asserts the *mechanism* that makes registration unique — a pre-claimed marker answers 409 — but only an emulator run can show that four simultaneous registrations for one address yield exactly one account, and the same goes for a replayed payment intent under real query semantics. Run both separately, under the fail-closed conditions in [section 1.10.1](#1101-the-two-sanctioned-stubs), and do not read a green harness as covering them.
- **The attempt ceilings are proven per process.** Gate E shows the ceiling and the 429; the counters are module state, so nothing here says anything about a deployment running several workers (**NT-26**).
- **The two open purchase defects are not reproduced.** Gate D covers the replay guard, which exists; it does not and cannot cover two genuinely simultaneous buyers, or a crash between the two final writes (**HCF-14**).
- **A `photos` entry that is already `bytes`.** The handler's `isinstance(photo, str)` guard has a bytes branch, and it is unreachable through HTTP: `VehicleListing.photos` is `List[str]`, and Pydantic v1 coerces every entry to `str` before the handler sees it. The branch is defensive, and it is covered by inspection rather than by a request — do not read the 85 as covering it.
- **Nothing outside the backend's HTTP surface.** The SPA, the Celery module (**NT-6**, which cannot even be imported), the Terraform under `infrastructure/`, and performance or load behaviour of any kind.
- **It is not a regression gate.** Nothing runs it for you. Continuous integration is red for four unrelated reasons (**NT-16**) and would not run this script even when green, so it protects the properties above only at the moment you choose to run it.

## 6.6 EXTENDING IT

Add a case where its gate lives, and keep the four properties that make the script safe to run: state is reset between gates with `store.reset()`, the attempt counters are reset with `reset_limits(app)` in the same breath — they are per-process module state, and an unrelated gate would otherwise exhaust a ceiling that gate E exercises on purpose — a stand-in is installed with `mock.patch.object` so it is removed again afterwards, and no assertion depends on the order of anything Firestore returned. Two habits are worth copying — assert against the *store* as well as the response when a handler's real effect is a write, and assert a negative with the write counter (`store.writes` unchanged) rather than with the absence of a document, because the second passes for the wrong reason when a name is misspelled.

If a case you want needs a Firestore behaviour the double does not model — an index requirement, a query shape it does not implement — do not quietly point this script at an emulator. Take the emulator-backed profile in [section 1.7.1](#171-the-local-development-profile) instead, which already carries the guards ([section 3.8](#38-a-clean-boot-still-stops-in-the-payment-module) contrasts the two), and keep this script's fail-closed preflight exactly as it is.
