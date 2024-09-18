# Main Terraform configuration file for provisioning Google Cloud resources

# Provider configuration for Google Cloud
provider "google" {
  project = var.project_id
  region  = var.region
}

# Resource definitions for Google Kubernetes Engine cluster
resource "google_container_cluster" "primary" {
  name     = "${var.project_id}-gke"
  location = var.region
  
  # We can't create a cluster with no node pool defined, but we want to only use
  # separately managed node pools. So we create the smallest possible default
  # node pool and immediately delete it.
  remove_default_node_pool = true
  initial_node_count       = 1

  network    = google_compute_network.vpc.name
  subnetwork = google_compute_subnetwork.subnet.name
}

resource "google_container_node_pool" "primary_nodes" {
  name       = "${google_container_cluster.primary.name}-node-pool"
  location   = var.region
  cluster    = google_container_cluster.primary.name
  node_count = var.gke_num_nodes

  node_config {
    oauth_scopes = [
      "https://www.googleapis.com/auth/logging.write",
      "https://www.googleapis.com/auth/monitoring",
    ]

    labels = {
      env = var.project_id
    }

    machine_type = "n1-standard-1"
    tags         = ["gke-node", "${var.project_id}-gke"]
    metadata = {
      disable-legacy-endpoints = "true"
    }
  }
}

# Resource definitions for Google Cloud Storage buckets
resource "google_storage_bucket" "static_assets" {
  name          = "${var.project_id}-static-assets"
  location      = "US"
  force_destroy = true

  uniform_bucket_level_access = true

  website {
    main_page_suffix = "index.html"
    not_found_page   = "404.html"
  }

  cors {
    origin          = ["*"]
    method          = ["GET", "HEAD", "PUT", "POST", "DELETE"]
    response_header = ["*"]
    max_age_seconds = 3600
  }
}

# Resource definitions for Google Cloud Firestore
resource "google_firestore_database" "database" {
  project     = var.project_id
  name        = "(default)"
  location_id = "us-central"
  type        = "FIRESTORE_NATIVE"
}

# Resource definitions for Google Cloud SQL instance
resource "google_sql_database_instance" "main" {
  name             = "${var.project_id}-db-instance"
  database_version = "POSTGRES_13"
  region           = var.region

  settings {
    tier = "db-f1-micro"
  }

  deletion_protection = false
}

resource "google_sql_database" "database" {
  name     = "${var.project_id}-database"
  instance = google_sql_database_instance.main.name
}

# Resource definitions for networking components (VPC, subnets, firewall rules)
resource "google_compute_network" "vpc" {
  name                    = "${var.project_id}-vpc"
  auto_create_subnetworks = "false"
}

resource "google_compute_subnetwork" "subnet" {
  name          = "${var.project_id}-subnet"
  region        = var.region
  network       = google_compute_network.vpc.name
  ip_cidr_range = "10.10.0.0/24"
}

resource "google_compute_firewall" "default" {
  name    = "${var.project_id}-firewall"
  network = google_compute_network.vpc.name

  allow {
    protocol = "tcp"
    ports    = ["80", "443", "8080", "1000-2000"]
  }

  source_tags = ["web"]
}

# Resource definitions for IAM roles and service accounts
resource "google_service_account" "default" {
  account_id   = "${var.project_id}-sa"
  display_name = "Service Account for ${var.project_id}"
}

resource "google_project_iam_member" "service_account_role" {
  project = var.project_id
  role    = "roles/editor"
  member  = "serviceAccount:${google_service_account.default.email}"
}

# HUMAN ASSISTANCE NEEDED
# The following IAM roles might need to be adjusted based on the specific requirements of the project
# Please review and modify as necessary
resource "google_project_iam_member" "additional_roles" {
  for_each = toset([
    "roles/cloudsql.client",
    "roles/datastore.user",
    "roles/storage.objectAdmin",
  ])
  
  project = var.project_id
  role    = each.key
  member  = "serviceAccount:${google_service_account.default.email}"
}