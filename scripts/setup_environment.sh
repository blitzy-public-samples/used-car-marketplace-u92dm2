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

# Set up Python virtual environment
echo "Setting up Python virtual environment..."
python3 -m venv venv
source venv/bin/activate

# Install Python dependencies from requirements.txt
echo "Installing Python dependencies..."
pip install -r requirements.txt

# Set up Node.js environment
echo "Setting up Node.js environment..."
npm install -g npm@latest

# Install frontend dependencies from package.json
echo "Installing frontend dependencies..."
npm install

# Configure environment variables
echo "Configuring environment variables..."
cp .env.example .env
# HUMAN ASSISTANCE NEEDED
# TODO: Update .env file with appropriate values for your local environment

# Initialize Google Cloud SDK and authenticate
echo "Initializing Google Cloud SDK..."
gcloud init
gcloud auth application-default login

# Set up local development database
echo "Setting up local development database..."
# HUMAN ASSISTANCE NEEDED
# TODO: Add commands to set up your local development database (e.g., PostgreSQL, MySQL)

echo "Environment setup complete!"