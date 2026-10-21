output "workload_identity_provider" {
  description = "Set as GitHub repository variable GCP_WIF_PROVIDER."
  value       = google_iam_workload_identity_pool_provider.github.name
}

output "deployer_service_account" {
  description = "Set as GitHub repository variable GCP_SERVICE_ACCOUNT."
  value       = google_service_account.deployer.email
}

output "runtime_service_account" {
  value = google_service_account.runtime.email
}

output "artifact_registry" {
  description = "Docker repository for agent images."
  value       = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.images.repository_id}"
}

output "cloud_run_job" {
  value = google_cloud_run_v2_job.agent.name
}

output "state_bucket" {
  description = "Bucket holding the agent's long-term memory."
  value       = google_storage_bucket.state.name
}
