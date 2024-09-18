# Project ID variable
variable "project_id" {
  description = "The ID of the Google Cloud project"
  type        = string
}

# Region and zone variables
variable "region" {
  description = "The region to deploy resources"
  type        = string
  default     = "us-central1"
}

variable "zone" {
  description = "The zone within the region to deploy resources"
  type        = string
  default     = "us-central1-a"
}

# Cluster configuration variables
variable "gke_num_nodes" {
  description = "Number of nodes in the GKE cluster"
  type        = number
  default     = 3
}

variable "gke_machine_type" {
  description = "Machine type for GKE nodes"
  type        = string
  default     = "n1-standard-2"
}

# Storage bucket name variables
variable "storage_bucket_name" {
  description = "Name of the Google Cloud Storage bucket"
  type        = string
}

# Database configuration variables
variable "db_instance_name" {
  description = "Name of the Cloud SQL instance"
  type        = string
}

variable "db_version" {
  description = "Database version for Cloud SQL"
  type        = string
  default     = "POSTGRES_13"
}

variable "db_tier" {
  description = "Machine type for Cloud SQL instance"
  type        = string
  default     = "db-f1-micro"
}

# Networking variables
variable "vpc_cidr" {
  description = "CIDR block for the VPC"
  type        = string
  default     = "10.0.0.0/16"
}

variable "subnet_cidr" {
  description = "CIDR block for the subnet"
  type        = string
  default     = "10.0.1.0/24"
}

variable "subnet_name" {
  description = "Name of the subnet"
  type        = string
  default     = "main-subnet"
}