#!/bin/bash

# Check and install required system dependencies
echo "Checking and installing system dependencies..."
if ! command -v python3 &> /dev/null; then
    sudo apt-get update
    sudo apt-get install -y python3 python3-pip
fi

if ! command -v node &> /dev/null; then
    # Install the Node.js 20.x LTS line, replacing the previous end-of-life Node 14
    # pin. This is mandatory, not cosmetic: the root is now an npm workspace root
    # (its package.json declares "workspaces": ["frontend"]) and npm workspaces
    # require npm 7 or later. Node 14 ships npm 6, which can neither resolve a
    # "workspaces" array nor read the committed lockfileVersion 3 lockfile, so
    # bootstrapping through this script on Node 14 would defeat the workspace-root
    # install performed further down. Node 14 has also been end-of-life since
    # 30 April 2023. The 20.x line is kept deliberately in step with the README
    # prerequisite and the Frontend CI node-version pin.
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

# Install Python dependencies from requirements.txt, when one is present. The
# existence check is required because no requirements.txt is committed anywhere in
# this repository yet: an unguarded pip install fails here and derails the rest of the
# bootstrap, including the frontend install below. No requirements.txt is created by
# this script - authoring one is a separate change. The path stays relative to the
# caller's working directory, exactly like the virtual environment created above.
echo "Installing Python dependencies..."
if [ -f requirements.txt ]; then
    pip install -r requirements.txt
else
    echo "Skipping Python dependencies: no requirements.txt in $(pwd). The backend service under backend/ has no committed manifest yet; install its dependencies manually."
fi

# Set up Node.js environment
echo "Setting up Node.js environment..."
npm install -g npm@latest

# Install frontend dependencies from package.json. The install now runs at the npm
# workspace root - the repository root - whose package.json declares
# "workspaces": ["frontend"], so this single command resolves and installs the
# frontend workspace's dependencies and hoists them into one root node_modules/.
# WHY THIS CHANGED: a bare `npm install` here previously failed with ENOENT
# (errno -2, process exit code 254) because the repository root held no package.json
# at all, and npm opens the literal path <cwd>/package.json without ever traversing
# into a subdirectory or up to a parent. The repository's only manifest lives in
# frontend/, so this script reproduced the very failure it was meant to prevent -
# its comment said "frontend dependencies" while its command never changed directory.
# The workspace root is resolved from the script's own location rather than from the
# caller's working directory, so `bash scripts/setup_environment.sh` installs the
# right tree no matter where it is invoked from. The subshell confines the directory
# change to this one command: this script deliberately runs without shell errexit, so
# a bare `cd` would leak into every step below (the .env copy and the gcloud calls).
echo "Installing frontend dependencies..."
( cd "$(dirname "$0")/.." && npm install )

# Configure environment variables, when a template is present. The existence check is
# required because no .env.example is committed in this repository yet: an unguarded cp
# fails here and would leave the interactive Google Cloud step below looking like the
# cause. No template is authored by this script - .env files carry credentials, and the
# repository's .gitignore deliberately ignores .env and .env.*.local so local overrides
# can never be committed by accident. Both paths below are relative to the CALLER's
# working directory, which is unchanged by the frontend install above precisely because
# that install performs its directory change inside a subshell.
echo "Configuring environment variables..."
if [ -f .env.example ]; then
    cp .env.example .env
else
    echo "Skipping environment file: no .env.example in $(pwd). Create .env by hand if you need local overrides."
fi
# HUMAN ASSISTANCE NEEDED
# TODO: Update .env file with appropriate values for your local environment

# Initialize Google Cloud SDK and authenticate, only when a terminal is attached. Both
# commands prompt for input, so on an unattended run - CI, a container build, or
# `bash scripts/setup_environment.sh < /dev/null` - they block forever, which is one
# more reason this bootstrap script could never run through to completion.
echo "Initializing Google Cloud SDK..."
if [ -t 0 ]; then
    gcloud init
    gcloud auth application-default login
else
    echo "Skipping Google Cloud SDK initialization: no terminal attached. Run 'gcloud init' and 'gcloud auth application-default login' manually to finish authenticating."
fi

# Set up local development database
echo "Setting up local development database..."
# HUMAN ASSISTANCE NEEDED
# TODO: Add commands to set up your local development database (e.g., PostgreSQL, MySQL)

echo "Environment setup complete!"