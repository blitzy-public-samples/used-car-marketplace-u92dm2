#!/bin/bash

# Check and install required system dependencies
echo "Checking and installing system dependencies..."
if ! command -v python3 &> /dev/null; then
    sudo apt-get update
    sudo apt-get install -y python3 python3-pip
fi

if ! command -v node &> /dev/null; then
    # Install the Node.js 20.x line, replacing the previous Node 14 pin. Moving off
    # 14 is mandatory, not cosmetic: the root is now an npm workspace root (its
    # package.json declares "workspaces": ["frontend"]) and npm workspaces require
    # npm 7 or later. Node 14 ships npm 6, which can neither resolve a "workspaces"
    # array nor read the committed lockfileVersion 3 lockfile, so bootstrapping
    # through this script on Node 14 would defeat the workspace-root install
    # performed further down. Node 14 has been end-of-life since 30 April 2023.
    #
    # 20.x is NOT an LTS line and is NOT a supported runtime: Node 20 reached
    # end-of-life on 30 April 2026 and receives no further security patches. It is
    # installed here only because it is the runtime this repository's toolchain was
    # validated on, and because it is kept deliberately in step with the README
    # prerequisite and the Frontend CI node-version pin. Moving to a supported line
    # (Node 22 Maintenance LTS or Node 24 Active LTS) requires major upgrades of
    # Vite and Vitest, so it is separate modernization work rather than part of
    # this fix - see the Prerequisites section of README.md.
    curl -sL https://deb.nodesource.com/setup_20.x | sudo -E bash -
    sudo apt-get install -y nodejs
fi

if ! command -v gcloud &> /dev/null; then
    echo "deb [signed-by=/usr/share/keyrings/cloud.google.gpg] https://packages.cloud.google.com/apt cloud-sdk main" | sudo tee -a /etc/apt/sources.list.d/google-cloud-sdk.list
    curl https://packages.cloud.google.com/apt/doc/apt-key.gpg | sudo apt-key --keyring /usr/share/keyrings/cloud.google.gpg add -
    sudo apt-get update && sudo apt-get install -y google-cloud-sdk
fi

# Set up Python virtual environment
echo "Setting up Python virtual environment..."
python3 -m venv venv
source venv/bin/activate

# Install Python dependencies from requirements.txt - guarded because no
# requirements.txt is committed anywhere in this repository yet, so the bare
# install this replaces always failed and derailed the rest of the bootstrap
# before it ever reached the frontend install below. The file is deliberately
# not created here; authoring it is outside this fix.
echo "Installing Python dependencies..."
if [ -f requirements.txt ]; then
    pip install -r requirements.txt
else
    echo "Skipping: no requirements.txt found in $(pwd). Install the backend dependencies manually, or re-run this script once one is committed."
fi

# Set up Node.js environment
echo "Setting up Node.js environment..."
npm install -g npm@latest

# Install frontend dependencies from package.json - the install runs at the npm
# workspace root, whose package.json declares "workspaces": ["frontend"], so it
# installs the frontend workspace's dependencies. A bare `npm install` here
# previously failed with ENOENT (errno -2, exit code 254) because the repository
# root held no manifest at all: npm picks the package it acts on by walking UP from
# the current directory to the nearest ancestor holding a package.json or
# node_modules, and never searches DOWN into a subdirectory - so it never looked
# inside frontend/, where the repository's only manifest lives, and no ancestor of
# the repository root held one either, leaving npm to abort on
# open(<repo root>/package.json). The root manifest added by this same fix is what
# makes the command below work. The root is resolved from this script's own
# location, so the step no longer depends on the caller's working directory, and
# the directory change is confined to a subshell so that the steps after it still
# run where the caller stood.
echo "Installing frontend dependencies..."
( cd "$(dirname "$0")/.." && npm install )

# Configure environment variables - guarded because no .env.example is committed
# in this repository yet, so the bare copy this replaces always failed. No
# template is created here: adding one is outside this fix, and the root
# .gitignore ignores .env and .env.*.local, which lowers the chance of committing
# local overrides by accident but is not a secret-management control: it does not
# cover an already-tracked file, a deliberate `git add -f`, or credentials pasted
# into a differently named file. Both paths are relative, and the install step
# above confines its directory change to a subshell, so they resolve in the
# caller's working directory exactly as they did before. Where a .env has to sit
# is runtime-specific: Vite reads frontend/.env and the backend reads a .env in the
# directory its process is launched from, so a .env left here is picked up only if
# this happens to be that directory - see installation step 3 in README.md.
echo "Configuring environment variables..."
if [ -f .env.example ]; then
    cp .env.example .env
else
    echo "Skipping: no .env.example found in $(pwd). Create .env yourself if you need local overrides."
fi
# HUMAN ASSISTANCE NEEDED
# TODO: Update .env file with appropriate values for your local environment

# Initialize Google Cloud SDK and authenticate - both commands prompt for input,
# so they are skipped when no terminal is attached. Unattended runs (CI, a
# container build, `bash scripts/setup_environment.sh < /dev/null`) used to block
# here indefinitely, which is one of the reasons this script could never complete.
echo "Initializing Google Cloud SDK..."
if [ -t 0 ]; then
    gcloud init
    gcloud auth application-default login
else
    echo "Skipping: no terminal attached. Run 'gcloud init' and 'gcloud auth application-default login' manually when you need Google Cloud access."
fi

# Set up local development database
echo "Setting up local development database..."
# HUMAN ASSISTANCE NEEDED
# TODO: Add commands to set up your local development database (e.g., PostgreSQL, MySQL)

echo "Environment setup complete!"