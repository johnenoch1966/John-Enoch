# ---------------------------------------------------------------------------
# Runtime: container registry, long-term memory bucket, Cloud Run Job, schedule
# ---------------------------------------------------------------------------
resource "google_artifact_registry_repository" "images" {
  location      = var.region
  repository_id = local.name
  format        = "DOCKER"
  description   = "Container images for the adoption research agent"
  labels        = local.labels

  cleanup_policies {
    id     = "keep-recent"
    action = "KEEP"
    most_recent_versions {
      keep_count = 10
    }
  }
  depends_on = [google_project_service.services]
}

# Long-term memory (evidence.db, catalog, report history) survives between runs.
resource "google_storage_bucket" "state" {
  name                        = "${var.project_id}-${local.name}-state"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  labels                      = local.labels

  versioning {
    enabled = true
  }
  lifecycle_rule {
    condition {
      num_newer_versions = 20
    }
    action {
      type = "Delete"
    }
  }
}

resource "google_storage_bucket_iam_member" "runtime_state_rw" {
  bucket = google_storage_bucket.state.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.runtime.email}"
}

resource "google_cloud_run_v2_job" "agent" {
  name                = local.name
  location            = var.region
  deletion_protection = var.environment == "prod"
  labels              = local.labels

  template {
    task_count = 1
    template {
      service_account = google_service_account.runtime.email
      timeout         = "10800s" # a full research cycle can take a few hours
      max_retries     = 1

      containers {
        image = var.image
        args  = ["python", "-m", "adoption_agent.cli", "run"]

        resources {
          limits = { cpu = "2", memory = "2Gi" }
        }

        env {
          name  = "GOOGLE_CLOUD_PROJECT"
          value = var.project_id
        }
        env {
          name  = "GOOGLE_CLOUD_LOCATION"
          value = "global"
        }
        env {
          name  = "GOOGLE_GENAI_USE_VERTEXAI"
          value = "TRUE"
        }
        env {
          name  = "ADOPTION_AGENT_MODEL"
          value = var.model
        }
        env {
          name  = "ADOPTION_AGENT_STATE_BUCKET"
          value = google_storage_bucket.state.name
        }
        env {
          name  = "ADOPTION_AGENT_TRACE_TO_CLOUD"
          value = "true"
        }
        env {
          name  = "ADOPTION_AGENT_PII_BACKEND"
          value = var.pii_backend
        }
      }
    }
  }

  # CI/CD rolls out new images; Terraform owns everything else.
  lifecycle {
    ignore_changes = [template[0].template[0].containers[0].image, client, client_version]
  }
  depends_on = [google_project_service.services, google_project_iam_member.runtime_roles]
}

resource "google_cloud_run_v2_job_iam_member" "scheduler_invoker" {
  name     = google_cloud_run_v2_job.agent.name
  location = var.region
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.scheduler.email}"
}

resource "google_cloud_scheduler_job" "refresh" {
  count       = var.schedule == "" ? 0 : 1
  name        = "${local.name}-refresh"
  region      = var.region
  description = "Automated research refresh for the adoption agent"
  schedule    = var.schedule
  time_zone   = "Etc/UTC"

  http_target {
    http_method = "POST"
    uri         = "https://run.googleapis.com/v2/projects/${var.project_id}/locations/${var.region}/jobs/${google_cloud_run_v2_job.agent.name}:run"
    oauth_token {
      service_account_email = google_service_account.scheduler.email
    }
  }
  depends_on = [google_cloud_run_v2_job_iam_member.scheduler_invoker]
}
