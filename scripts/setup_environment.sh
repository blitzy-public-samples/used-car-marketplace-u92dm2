#!/bin/bash

# Check and install required system dependencies
echo "Checking and installing system dependencies..."
if ! command -v python3 &> /dev/null; then
    sudo apt-get update
    sudo apt-get install -y python3 python3-pip
fi

if ! command -v node &> /dev/null; then
    curl -sL https://deb.nodesource.com/setup_14.x | sudo -E bash -
    sudo apt-get install -y nodejs
fi

if ! command -v gcloud &> /dev/null; then
    echo "deb [signed-by=/usr/share/keyrings/cloud.google.gpg] https://packages.cloud.google.com/apt cloud-sdk main" | sudo tee -a /etc/apt/sources.list.d/google-cloud-sdk.list
    curl https://packages.cloud.google.com/apt/doc/apt-key.gpg | sudo apt-key --keyring /usr/share/keyrings/cloud.google.gpg add -
    sudo apt-get update && sudo apt-get install -y google-cloud-sdk
fi

# Resolve the repository root, so every path below is anchored to the checkout
# rather than to wherever this script happened to be invoked from. The manifests
# this script installs live in backend/ and frontend/, not at the root, and the
# bare filenames used previously resolved to nothing whenever the caller's
# working directory was not the root.
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Set up Python virtual environment
echo "Setting up Python virtual environment..."
python3 -m venv "${REPO_ROOT}/backend/.venv"
source "${REPO_ROOT}/backend/.venv/bin/activate"

# Install Python dependencies from backend/requirements.txt
echo "Installing Python dependencies..."
pip install -r "${REPO_ROOT}/backend/requirements.txt"

# Set up Node.js environment
echo "Setting up Node.js environment..."
npm install -g npm@latest

# Install frontend dependencies from frontend/package.json
echo "Installing frontend dependencies..."
(cd "${REPO_ROOT}/frontend" && npm install)

# Configure environment variables
echo "Configuring environment variables..."
# backend/.env is what app/core/config.py reads (`env_file = ".env"`, resolved
# against the backend working directory), so the copy has to land there.
cp "${REPO_ROOT}/backend/.env.example" "${REPO_ROOT}/backend/.env"
# The copied SECRET_KEY is a SENTINEL and the application will refuse to start
# until it is replaced - Settings rejects published placeholders by value. Fill
# in a real one now:
#   python3 -c "import secrets; print(secrets.token_urlsafe(48))"
echo "Edit ${REPO_ROOT}/backend/.env before starting the API:"
echo "  * SECRET_KEY is a placeholder and is REFUSED at import until replaced"
echo "  * GOOGLE_CLOUD_*, STRIPE_* need your own project and test-mode keys"

# Initialize Google Cloud SDK and authenticate
echo "Initializing Google Cloud SDK..."
gcloud init
gcloud auth application-default login

# Set up local development database
echo "Setting up local development database..."
# HUMAN ASSISTANCE NEEDED
# TODO: Add commands to set up your local development database (e.g., PostgreSQL, MySQL)

echo "Environment setup complete!"