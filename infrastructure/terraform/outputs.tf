output "kubernetes_cluster_endpoint" {
  description = "The endpoint for the Kubernetes cluster"
  value       = google_container_cluster.primary.endpoint
}

output "kubernetes_cluster_name" {
  description = "The name of the Kubernetes cluster"
  value       = google_container_cluster.primary.name
}

output "storage_bucket_urls" {
  description = "The URLs of the created storage buckets"
  value = {
    for bucket in google_storage_bucket.buckets :
    bucket.name => bucket.url
  }
}

output "database_connection_strings" {
  description = "Connection strings for the databases"
  sensitive   = true
  value = {
    postgres = "postgresql://${google_sql_database_instance.postgres.name}:${google_sql_database_instance.postgres.private_ip_address}"
    redis    = "${google_redis_instance.cache.host}:${google_redis_instance.cache.port}"
  }
}

output "vpc_network_name" {
  description = "The name of the VPC network"
  value       = google_compute_network.vpc_network.name
}

output "vpc_subnet_information" {
  description = "Information about the VPC subnets"
  value = {
    for subnet in google_compute_subnetwork.subnets :
    subnet.name => {
      name        = subnet.name
      ip_cidr_range = subnet.ip_cidr_range
      region      = subnet.region
    }
  }
}

# HUMAN ASSISTANCE NEEDED
# Please verify that the resource names (e.g., google_container_cluster.primary, google_storage_bucket.buckets)
# match the actual resource names used in other Terraform files.
# Also, ensure that the database and Redis instance names are correct.