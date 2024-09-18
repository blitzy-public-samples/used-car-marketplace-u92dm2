#!/bin/bash

set -e

# Build frontend application
echo "Building frontend application..."
cd frontend
npm run build
cd ..

# Build and push Docker images for backend services
echo "Building and pushing Docker images..."
docker build -t gcr.io/used-car-marketplace/api:latest ./backend/api
docker push gcr.io/used-car-marketplace/api:latest

docker build -t gcr.io/used-car-marketplace/worker:latest ./backend/worker
docker push gcr.io/used-car-marketplace/worker:latest

# Apply Terraform configurations
echo "Applying Terraform configurations..."
cd terraform
terraform init
terraform apply -auto-approve
cd ..

# Deploy backend services to Google Kubernetes Engine
echo "Deploying backend services to GKE..."
gcloud container clusters get-credentials used-car-marketplace-cluster --zone us-central1-a
kubectl apply -f kubernetes/

# Update Google Cloud Storage buckets
echo "Updating Google Cloud Storage buckets..."
gsutil rsync -r frontend/build gs://used-car-marketplace-frontend

# Configure Google Cloud Firestore
echo "Configuring Google Cloud Firestore..."
gcloud firestore indexes create firestore.indexes.json

# Update DNS settings
echo "Updating DNS settings..."
gcloud dns record-sets transaction start --zone=used-car-marketplace-zone
gcloud dns record-sets transaction add --name=api.usedcarmarketplace.com. --type=A --ttl=300 "$(kubectl get service api-service -o=jsonpath='{.status.loadBalancer.ingress[0].ip}')" --zone=used-car-marketplace-zone
gcloud dns record-sets transaction execute --zone=used-car-marketplace-zone

# Run post-deployment tests
echo "Running post-deployment tests..."
cd tests
npm run post-deployment
cd ..

echo "Deployment completed successfully!"