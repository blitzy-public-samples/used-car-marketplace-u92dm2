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

docker build -t gcr.io/used-car-marketplace/auth:latest ./backend/auth
docker push gcr.io/used-car-marketplace/auth:latest

docker build -t gcr.io/used-car-marketplace/search:latest ./backend/search
docker push gcr.io/used-car-marketplace/search:latest

# Apply Terraform configurations
echo "Applying Terraform configurations..."
cd terraform
terraform init
terraform apply -auto-approve
cd ..

# Deploy backend services to Google Kubernetes Engine
echo "Deploying backend services to GKE..."
gcloud container clusters get-credentials used-car-marketplace-cluster --zone us-central1-a
kubectl apply -f k8s/

# Update Google Cloud Storage buckets
echo "Updating Google Cloud Storage buckets..."
gsutil rsync -r frontend/build gs://used-car-marketplace-frontend

# Configure Google Cloud Firestore
#
# The composite indexes the ratings collection needs are declared at
# infrastructure/firestore.indexes.json, beside the Terraform that provisions
# the database itself. Installing them here is the only step that turns that
# declaration into something the datastore will actually serve, and getting it
# wrong is not a loud failure: a query needing an undeclared composite index
# fails at request time, in production, long after deployment reported success.
#
# `gcloud firestore indexes create <file>` is NOT a command. gcloud has no
# apply-a-file verb for composite indexes at all - `gcloud firestore indexes`
# offers `composite create|delete|describe|list` and `fields describe|list|
# update`, and passing a filename to a non-existent `create` verb exits 2 with
# "Invalid choice: 'create'". Under `set -e` that aborts the deploy; without it
# the indexes would silently never exist. So each declared index is installed
# with one real `gcloud firestore indexes composite create` invocation, whose
# arguments are DERIVED from the declaration file rather than restated here:
# the file stays the single source of truth, and an index added there is
# installed without editing this script.
#
# An index that already exists makes the command exit non-zero. That is the
# normal case on every deploy after the first, so it is recognised and treated
# as success; any other failure still stops the deploy.
echo "Configuring Google Cloud Firestore..."
FIRESTORE_INDEX_FILE="infrastructure/firestore.indexes.json"
firestore_index_args="$(python3 -c "
import json, sys

with open(sys.argv[1]) as declaration:
    declared = json.load(declaration)

for index in declared.get('indexes', []):
    args = [
        '--collection-group=' + index['collectionGroup'],
        '--query-scope=' + index.get('queryScope', 'COLLECTION').lower(),
    ]
    for field in index['fields']:
        parts = ['field-path=' + field['fieldPath']]
        if 'order' in field:
            parts.append('order=' + field['order'].lower())
        elif 'arrayConfig' in field:
            parts.append('array-config=' + field['arrayConfig'].lower())
        else:
            raise SystemExit(
                'Index field {0!r} declares neither order nor '
                'arrayConfig'.format(field)
            )
        args.append('--field-config=' + ','.join(parts))
    print(' '.join(args))
" "$FIRESTORE_INDEX_FILE")"

while IFS= read -r index_spec; do
  [ -n "$index_spec" ] || continue
  echo "  installing composite index: $index_spec"
  # Word splitting is intended: each line is a list of complete gcloud flags,
  # and a field path or collection group can contain no whitespace.
  # shellcheck disable=SC2086
  if ! index_output="$(gcloud firestore indexes composite create $index_spec 2>&1)"; then
    if printf '%s' "$index_output" | grep -qiE 'already exists|ALREADY_EXISTS'; then
      echo "  already present, leaving it in place"
    else
      printf '%s\n' "$index_output" >&2
      exit 1
    fi
  fi
done <<EOF
$firestore_index_args
EOF

# Update DNS settings
echo "Updating DNS settings..."
gcloud dns record-sets transaction start --zone=used-car-marketplace-zone
gcloud dns record-sets transaction add --name=api.usedcarmarketplace.com. --type=A --ttl=300 "$(kubectl get svc api-service -o jsonpath='{.status.loadBalancer.ingress[0].ip}')" --zone=used-car-marketplace-zone
gcloud dns record-sets transaction execute --zone=used-car-marketplace-zone

# Run post-deployment tests
echo "Running post-deployment tests..."
cd tests
npm run post-deployment-tests
cd ..

echo "Deployment completed successfully!"